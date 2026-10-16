# -*- coding: utf-8 -*-

import copy
import json
import os
import sqlite3
import tempfile
from types import SimpleNamespace

from sqlalchemy import and_, func, inspect, select


class MetadataCopier:
    PAGE_SIZE = 500

    def __init__(self, sources, target_for_category, person_target, mode='merge',
                 status=None, log=None, categories=None, sanitized=False, should_stop=None):
        from .mod_meta_db import (ModuleMetaDb, MetaItem, MetaPerson, MetaTag, MetaMedia,
                                  MetaFingerprint, MetaItemPersonMap, MetaItemTagMap)
        if mode not in ('clean', 'merge', 'missing'):
            raise ValueError('Invalid copy policy')
        self.mod = ModuleMetaDb
        self.Item, self.Person, self.Tag = MetaItem.__table__, MetaPerson.__table__, MetaTag.__table__
        self.Media, self.Fingerprint = MetaMedia.__table__, MetaFingerprint.__table__
        self.PersonMap, self.TagMap = MetaItemPersonMap.__table__, MetaItemTagMap.__table__
        self.sources = sources
        self.target_for_category, self.person_target = target_for_category, person_target
        self.mode, self.categories, self.sanitized = mode, categories, sanitized
        self.should_stop = should_stop or (lambda: False)
        self.status = status if status is not None else {}
        self.log = log or (lambda message: None)
        self.targets = {id(person_target): person_target}
        self.work = tempfile.TemporaryDirectory(prefix='metadata_copy_')
        self.index = sqlite3.connect(os.path.join(self.work.name, 'identities.db'))
        self.index.execute('CREATE TABLE mapping (src INTEGER, kind TEXT, old_id INTEGER, dest INTEGER, new_id INTEGER, PRIMARY KEY(src,kind,old_id,dest))')
        self.index.execute('CREATE TABLE token (domain TEXT, value TEXT, person_id INTEGER, UNIQUE(domain,value,person_id))')
        self.index.execute('CREATE INDEX token_lookup ON token(domain,value)')
        self.index.execute('CREATE TABLE parent (src INTEGER, old_id INTEGER, parent_id INTEGER, dest INTEGER, new_id INTEGER, PRIMARY KEY(src,old_id,dest))')
        self.status.update(current=0, total=0, inserted=0, updated=0, skipped=0, fail=0, ambiguous=0)

    def close(self):
        self.index.close()
        self.work.cleanup()

    def stopped(self):
        if self.should_stop():
            self.status['stop_flag'] = True
        return bool(self.status.get('stop_flag'))

    def pages(self, session, table, conditions=()):
        if not inspect(session.get_bind()).has_table(table.name):
            return
        last_id = 0
        while not self.stopped():
            rows = session.execute(select(table).where(table.c.id > last_id, *conditions)
                                   .order_by(table.c.id).limit(self.PAGE_SIZE)).mappings().all()
            if not rows:
                return
            last_id = rows[-1]['id']
            yield rows

    def target(self, category):
        session = self.target_for_category(category)
        if session is None:
            raise ValueError(f'No destination for {category}')
        self.targets[id(session)] = session
        self.validate_destination(session)
        return session

    def validate_destination(self, session):
        for source in self.sources:
            src, dst = source.get_bind(), session.get_bind()
            src_file, dst_file = src.url.database, dst.url.database
            if src is dst or (src.dialect.name == dst.dialect.name == 'sqlite' and
                    src_file and dst_file and src_file != ':memory:' and dst_file != ':memory:' and
                    os.path.realpath(src_file) == os.path.realpath(dst_file)):
                raise ValueError('Source and destination are the same database')

    def mapped(self, src, kind, old_id, target):
        row = self.index.execute('SELECT new_id FROM mapping WHERE src=? AND kind=? AND old_id=? AND dest=?',
                                 (src, kind, old_id, id(target))).fetchone()
        return (row is not None), (row[0] if row else None)

    def remember(self, src, kind, old_id, target, new_id):
        self.index.execute('INSERT OR REPLACE INTO mapping VALUES (?,?,?,?,?)', (src, kind, old_id, id(target), new_id))

    @staticmethod
    def values(row):
        return {key: copy.deepcopy(value) for key, value in row.items() if key != 'id'}

    @staticmethod
    def tokens(person):
        result = set()
        for value in [person.get('person_idx')] + list(person.get('alt_indices') or []):
            if str(value or '').strip():
                result.add('idx:' + str(value).strip())
        for site, info in ((person.get('extra_info') or {}).get('site_actors') or {}).items():
            if isinstance(info, dict) and info.get('id'):
                result.add('site:' + site + ':' + str(info['id']))
        return result

    def index_person(self, person, target_id):
        self.index.executemany('INSERT OR IGNORE INTO token VALUES (?,?,?)',
            [(person['domain'], token, target_id) for token in self.tokens(person)])

    def matching_person(self, person):
        tokens = self.tokens(person)
        if not tokens:
            return (None, None) if self.initial_person_count == 0 else (None, '인물 식별자 없음')
        matches = set()
        for token in tokens:
            matches.update(row[0] for row in self.index.execute('SELECT person_id FROM token WHERE domain=? AND value=?', (person['domain'], token)))
        if len(matches) > 1:
            return None, '인물 식별자가 여러 대상과 일치'
        return (next(iter(matches)) if matches else None), None

    def merge_person(self, source, target):
        values = self.values(source)
        manual = copy.deepcopy(target.get('manual_data') or {})
        values['manual_data'] = {**(source.get('manual_data') or {}), **manual}
        aliases = {'birth': 'birth_date', 'debut': 'debut_date', 'bra_size': 'cup_size', 'blood': 'blood_type'}
        for field in manual:
            column = aliases.get(field, field)
            if column in target:
                values[column] = copy.deepcopy(target[column])
        for field, derived in (('birth', ('birth_year',)), ('birth_date', ('birth_year',)),
                               ('body_size', ('bust', 'waist', 'hip', 'cup_size'))):
            if field in manual:
                for column in derived:
                    values[column] = target.get(column)
        custom = ('custom_gdrive_fileid', 'custom_local_path', 'custom_remote_path')
        if any(target.get(key) for key in custom):
            for key in custom:
                values[key] = target.get(key)
        for key in ('official_cached_gdrive_fileid', 'official_cached_local_path', 'official_remote_path'):
            if not values.get(key) and target.get(key):
                values[key] = target[key]
        media = copy.deepcopy(values.get('media_src') or {})
        media = self.mod.normalize_person_media(media, domain=values.get('domain'))
        for section, key, column in self.mod.PERSON_MEDIA_COLUMNS:
            if values.get(column) is not None:
                media[section][key] = copy.deepcopy(values[column])
        values['media_src'] = media
        values['thumb_url'] = self.mod.resolve_person_active_thumb(SimpleNamespace(**values))
        values['has_photo'] = bool(values['thumb_url'])
        return values

    def clean_person(self, values):
        values = copy.deepcopy(values)
        media = self.mod.normalize_person_media(values.get('media_src'), domain=values.get('domain'))
        for field in ('local_path', 'official_cached_local_path', 'custom_local_path', 'official_remote_path',
                      'custom_remote_path', 'official_cached_gdrive_fileid', 'custom_gdrive_fileid'):
            values[field] = None
        for section, key, column in self.mod.PERSON_MEDIA_COLUMNS:
            if column in ('local_path', 'official_cached_local_path', 'custom_local_path', 'official_remote_path',
                          'custom_remote_path', 'official_cached_gdrive_fileid', 'custom_gdrive_fileid'):
                media[section][key] = ''
        if values.get('domain') == 'WESTERN':
            values['gdrive_fileid'] = None
            media['official']['google_fileid'] = ''
        media['official'].pop('local_img_url', None)
        values['media_src'] = media
        extra = copy.deepcopy(values.get('extra_info') or {})
        for key in ('local_img_url', 'user_google_fileid', '_storage_media'):
            extra.pop(key, None)
        values['extra_info'] = extra
        values['thumb_url'] = values.get('site_img_url') or ''
        values['has_photo'] = bool(values['thumb_url'])
        return values

    def copy_people(self):
        self.initial_person_count = self.person_target.execute(select(func.count()).select_from(self.Person)).scalar() or 0
        if self.mode != 'clean':
            for rows in self.pages(self.person_target, self.Person):
                for row in rows:
                    self.index_person(row, row['id'])
        for origin, source in enumerate(self.sources):
            for rows in self.pages(source, self.Person):
                for row in rows:
                    if self.stopped():
                        break
                    self.status['current'] += 1
                    self.status['current_code'] = row.get('name_ko') or row['name_org']
                    target_id, reason = (None, None) if self.mode == 'clean' else self.matching_person(row)
                    if reason:
                        self.remember(origin, 'person', row['id'], self.person_target, None)
                        self.status['skipped'] += 1
                        self.status['ambiguous'] += 1
                        self.log(f"인물 제외: {row['name_org']} ({reason})")
                        continue
                    try:
                        with self.person_target.begin_nested():
                            values = self.values(row)
                            if self.sanitized:
                                values = self.clean_person(values)
                            if target_id:
                                if self.mode == 'missing':
                                    action = 'skipped'
                                else:
                                    old = self.person_target.execute(select(self.Person).where(self.Person.c.id == target_id)).mappings().one()
                                    values = self.merge_person(values, old)
                                    self.person_target.execute(self.Person.update().where(self.Person.c.id == target_id).values(**values))
                                    action = 'updated'
                            else:
                                target_id = self.person_target.execute(self.Person.insert().values(**values)).inserted_primary_key[0]
                                action = 'inserted'
                        self.status[action] += 1
                        self.remember(origin, 'person', row['id'], self.person_target, target_id)
                        self.index_person(row, target_id)
                    except Exception as exc:
                        self.remember(origin, 'person', row['id'], self.person_target, None)
                        self.status['fail'] += 1
                        self.log(f"인물 복제 실패: {row['name_org']} ({exc})")
                self.person_target.commit()
                self.index.commit()
                self.log(f"인물 처리: {self.status['current']:,}/{self.status['total']:,}")

    def copy_tags(self):
        for source in self.sources:
            for rows in self.pages(source, self.Tag):
                for row in rows:
                    if self.stopped():
                        break
                    category = 'WESTERN' if str(row['domain']).upper() in ('WESTERN', 'WEST') else str(row['domain']).upper()
                    if category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'MOVIE', 'KTV', 'FTV'):
                        category = 'JAV_CEN'
                    target = self.target(category)
                    try:
                        with target.begin_nested():
                            self.copy_tag(source, row['id'], target)
                    except Exception as exc:
                        self.status['fail'] += 1
                        self.log(f"태그 복제 실패: {row['name_org']} ({exc})")
                for target in self.targets.values():
                    target.commit()

    def copy_tag(self, source, old_id, target):
        row = source.execute(select(self.Tag).where(self.Tag.c.id == old_id)).mappings().one_or_none()
        if row is None:
            raise ValueError(f'Missing source tag {old_id}')
        condition = and_(self.Tag.c.domain == row['domain'], self.Tag.c.name_org == row['name_org'], self.Tag.c.tag_type == row['tag_type'])
        old = target.execute(select(self.Tag).where(condition)).mappings().one_or_none()
        if old:
            if self.mode == 'merge':
                target.execute(self.Tag.update().where(self.Tag.c.id == old['id']).values(**self.values(row)))
            return old['id']
        return target.execute(self.Tag.insert().values(**self.values(row))).inserted_primary_key[0]

    def clean_item(self, row):
        values = self.values(row)
        values['poster_url'] = ''
        extra = copy.deepcopy(values.get('extra_info') or {})
        for key in ('poster_url', 'landscape_url', 'trailer_video', 'preview_clip', 'source_video_path', 'local_img_url', 'actor_cache', '_storage_media'):
            extra.pop(key, None)
        values['extra_info'] = extra
        original = copy.deepcopy(values.get('original') or {})
        if isinstance(original.get('extras'), list):
            original['extras'] = [entry for entry in original['extras']
                if isinstance(entry, dict) and 'mode=preview_' not in str(entry.get('content_url', ''))]
        values['original'] = original
        return values

    def copy_item(self, origin, source, row):
        target = self.target(row['category'])
        old = target.execute(select(self.Item).where(self.Item.c.code == row['code'])).mappings().one_or_none()
        if old and old['category'] != row['category']:
            raise ValueError('동일 코드가 다른 카테고리에 존재합니다')
        if old and self.mode == 'clean':
            raise ValueError('소스에 중복 작품 코드가 있습니다')
        if old and self.mode == 'missing':
            self.remember(origin, 'item', row['id'], target, old['id'])
            return 'skipped'
        values = self.clean_item(row) if self.sanitized else self.values(row)
        parent_id = values.pop('parent_id', None)
        with target.begin_nested():
            if old:
                target_id = old['id']
                extra = copy.deepcopy(values.get('extra_info') or {})
                old_extra = old.get('extra_info') or {}
                manual = old_extra.get('manual_data') or {}
                for field in manual:
                    if field in old:
                        values[field] = copy.deepcopy(old[field])
                if manual:
                    extra['manual_data'] = copy.deepcopy(manual)
                # A missing source cache must not erase a recorded destination video.
                for key in ('preview_clip', 'trailer_video', 'source_video_path'):
                    if key not in extra and key in old_extra:
                        extra[key] = copy.deepcopy(old_extra[key])
                values['extra_info'] = extra
                target.execute(self.Item.update().where(self.Item.c.id == target_id).values(**values))
            else:
                target_id = target.execute(self.Item.insert().values(parent_id=None, **values)).inserted_primary_key[0]
            from .util_metadata import MetaUtil
            def user_asset(media):
                return MetaUtil.is_user_media(media) if row.get('category') in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') else bool(media.get('is_user'))
            source_media = [dict(m) for m in source.execute(select(self.Media).where(self.Media.c.item_id == row['id'])).mappings()]
            existing_media = [dict(m) for m in target.execute(select(self.Media).where(self.Media.c.item_id == target_id)).mappings()] if old else []
            protected = [m for m in existing_media if user_asset(m)]
            for media in protected:
                if not media.get('is_user'):
                    target.execute(self.Media.update().where(self.Media.c.id == media['id']).values(is_user=True, source='user'))
                    media.update(is_user=True, source='user')
            for media in existing_media:
                if media not in protected:
                    # Keep other storage's assets even if the source no longer exposes them.
                    same_asset = any(m['media_type'] == media['media_type'] and bool(m.get('gdrive_fileid')) == bool(media.get('gdrive_fileid'))
                                     and m.get('sort_order') == media.get('sort_order') for m in source_media)
                    if same_asset:
                        target.execute(self.Media.delete().where(self.Media.c.id == media['id']))
            if not self.sanitized:
                for media in source_media:
                    if user_asset(media) and any(m['media_type'] == media['media_type'] and bool(m.get('gdrive_fileid')) == bool(media.get('gdrive_fileid'))
                            and (media['media_type'] != 'fanart' or m.get('sort_order') == media.get('sort_order')) for m in protected):
                        continue
                    candidate = self.values(media)
                    candidate['item_id'] = target_id
                    if user_asset(media):
                        candidate.update(is_user=True, source='user')
                    duplicate = any(m['url'] == media['url'] and m['media_type'] == media['media_type'] for m in existing_media if m in protected)
                    if not duplicate:
                        target.execute(self.Media.insert().values(**candidate))
            if protected:
                poster = next((m for m in protected if m['media_type'] == 'poster' and m['url'] == old.get('poster_url')), None)
                landscape = next((m for m in protected if m['media_type'] == 'landscape' and m['url'] == (old.get('extra_info') or {}).get('landscape_url')), None)
                updates = {}
                if poster:
                    updates['poster_url'] = poster['url']
                if landscape:
                    extra = copy.deepcopy(values.get('extra_info') or {})
                    extra['landscape_url'] = landscape['url']
                    updates['extra_info'] = extra
                if updates:
                    target.execute(self.Item.update().where(self.Item.c.id == target_id).values(**updates))
            for table in (self.Fingerprint, self.TagMap, self.PersonMap):
                if old:
                    # Preserve user-generated fingerprints alongside incoming official data.
                    condition = and_(table.c.item_id == target_id, table.c.source != 'user') if table is self.Fingerprint else table.c.item_id == target_id
                    target.execute(table.delete().where(condition))
                children = source.execute(select(table).where(table.c.item_id == row['id'])).mappings()
                for child in children:
                    values_child = self.values(child)
                    values_child['item_id'] = target_id
                    if table is self.TagMap:
                        values_child['tag_id'] = self.copy_tag(source, child['tag_id'], target)
                    elif table is self.PersonMap:
                        found, person_id = self.mapped(origin, 'person', child['person_id'], self.person_target)
                        if not found:
                            # Split SQLite item databases reference the separate person master DB.
                            candidates = {r[0] for r in self.index.execute(
                                'SELECT DISTINCT new_id FROM mapping WHERE kind=? AND old_id=? AND dest=? AND new_id IS NOT NULL',
                                ('person', child['person_id'], id(self.person_target)))}
                            person_id = next(iter(candidates)) if len(candidates) == 1 else None
                        if person_id is None:
                            self.log(f"인물 연결 제외: {row['code']} / source person {child['person_id']}")
                            continue
                        values_child['person_id'] = person_id
                    elif old and child.get('source') == 'user':
                        duplicate = target.execute(select(table.c.id).where(table.c.item_id == target_id,
                            table.c.algorithm == child['algorithm'], table.c.hash_value == child['hash_value'])).first()
                        if duplicate:
                            continue
                    target.execute(table.insert().values(**values_child))
        self.remember(origin, 'item', row['id'], target, target_id)
        if parent_id:
            self.index.execute('INSERT OR REPLACE INTO parent VALUES (?,?,?,?,?)', (origin, row['id'], parent_id, id(target), target_id))
        return 'updated' if old else 'inserted'

    def run(self):
        conditions = (self.Item.c.category.in_(self.categories),) if self.categories is not None else ()
        self.status['total'] = sum(source.execute(select(func.count()).select_from(self.Item).where(*conditions)).scalar() or 0 for source in self.sources)
        self.status['total'] += sum(source.execute(select(func.count()).select_from(self.Person)).scalar() or 0 for source in self.sources)
        self.validate_destination(self.person_target)
        self.copy_people()
        if self.categories != []:
            self.copy_tags()
        for origin, source in enumerate(self.sources):
            for rows in self.pages(source, self.Item, conditions):
                for row in rows:
                    if self.stopped():
                        break
                    self.status['current'] += 1
                    self.status['current_code'] = row['code']
                    try:
                        action = self.copy_item(origin, source, row)
                        self.status[action] += 1
                    except Exception as exc:
                        self.status['fail'] += 1
                        self.log(f"작품 복제 실패: {row['code']} ({exc})")
                for target in self.targets.values():
                    target.commit()
                self.index.commit()
                self.log(f"DB 처리: {self.status['current']:,}/{self.status['total']:,} | 신규 {self.status['inserted']:,}, 갱신 {self.status['updated']:,}, 건너뜀 {self.status['skipped']:,}, 실패 {self.status['fail']:,}")
        for origin, old_id, parent_id, destination, new_id in self.index.execute('SELECT * FROM parent'):
            target = self.targets[destination]
            found, mapped_parent = self.mapped(origin, 'item', parent_id, target)
            if found and mapped_parent:
                target.execute(self.Item.update().where(self.Item.c.id == new_id).values(parent_id=mapped_parent))
            else:
                self.log(f'부모 연결 제외: source item {old_id} / parent {parent_id}')
        for target in self.targets.values():
            target.commit()
        return not self.stopped() and not self.status['fail']
