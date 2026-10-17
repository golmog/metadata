# -*- coding: utf-8 -*-
"""v0에서 v1으로의 정식 스키마 승격 및 인물 DB 물리 컬럼 정규화 리비전."""

import re
import copy
import time
from datetime import datetime
from sqlalchemy import text
from ..base_revision import BaseRevision
from ..ddl_ops import DdlOperations
from ...setup import logger


class RevisionV0ToV1(BaseRevision):
    """v0 레거시에서 v1 정식 규격으로의 단독 마이그레이션 명세."""

    BACKFILL_BATCH_SIZE = 100

    revision_id = "rev_001_v0_to_v1"
    from_version = 0
    to_version = 1
    description = "v1 정식 규격: 미디어 에셋 확장, 감사 원장 생성 및 인물 DB 정규 물리 컬럼 승격"

    def upgrade_schema(self, ops: DdlOperations, physical_name: str) -> None:
        """
        스키마 구조 변경 (Fast DDL). 데이터 백필과 별도 트랜잭션으로 실행합니다.
        인덱스 생성 시간은 DB 크기와 저장장치 성능에 따라 달라집니다.
        """
        # 공통 감사 원장(History) 테이블 생성
        if not ops.has_table("meta_schema_migration_history"):
            pk_col = "SERIAL PRIMARY KEY" if ops.dialect == 'postgresql' else "INTEGER PRIMARY KEY AUTOINCREMENT"
            ts_type = "TIMESTAMP" if ops.dialect == 'postgresql' else "DATETIME"
            ops.execute_raw(f"""
                CREATE TABLE IF NOT EXISTS meta_schema_migration_history (
                    id {pk_col},
                    revision_id VARCHAR(100) NOT NULL,
                    from_version INTEGER NOT NULL,
                    to_version INTEGER NOT NULL,
                    phase VARCHAR(30) NOT NULL,
                    status VARCHAR(30) NOT NULL,
                    started_at {ts_type} NOT NULL,
                    completed_at {ts_type},
                    duration_sec FLOAT,
                    backup_path VARCHAR(500),
                    error_message TEXT,
                    details_json JSON
                )
            """)

        # 미디어 테이블(meta_media) 확장 컬럼 추가 (PostgreSQL 또는 비-인물 SQLite)
        if ops.has_table("meta_media"):
            ops.add_column("meta_media", "gdrive_fileid", "VARCHAR(255)")
            ops.add_column("meta_media", "relative_path", "VARCHAR(500)")
            ops.add_column("meta_media", "source", "VARCHAR(30)")
            ops.add_column("meta_media", "checksum", "VARCHAR(128)")
            ops.add_column("meta_media", "updated_time", "DATETIME")

            ops.create_index("ix_meta_media_gdrive_fileid", "meta_media", ["gdrive_fileid"])
            ops.create_index("ix_meta_media_source", "meta_media", ["source"])

            # 기초 source 기본값 인라인 보정
            user_expr = "is_user IS TRUE" if ops.dialect == 'postgresql' else "is_user = 1"
            ops.execute_raw(f"""
                UPDATE meta_media SET source = CASE 
                    WHEN {user_expr} THEN 'user' 
                    WHEN lower(url) LIKE '%drive.google.com%' OR lower(url) LIKE '%googleusercontent.com%' THEN 'gds' 
                    WHEN lower(url) LIKE 'http://%' OR lower(url) LIKE 'https://%' THEN 'site' 
                    ELSE 'cache' END 
                WHERE source IS NULL OR trim(source) = ''
            """)

        # 인물 테이블(meta_person) 물리 컬럼 대량 승격 (PostgreSQL 또는 person SQLite)
        if ops.has_table("meta_person"):
            # 기본 프로필 및 스펙 컬럼
            ops.add_column("meta_person", "gender", "VARCHAR(15)", server_default="'female'")
            ops.add_column("meta_person", "birth_date", "VARCHAR(20)")
            ops.add_column("meta_person", "birth_year", "INTEGER")
            ops.add_column("meta_person", "debut_date", "VARCHAR(20)")
            ops.add_column("meta_person", "height", "INTEGER")

            # 신체 사이즈 및 컵
            ops.add_column("meta_person", "bust", "INTEGER")
            ops.add_column("meta_person", "waist", "INTEGER")
            ops.add_column("meta_person", "hip", "INTEGER")
            ops.add_column("meta_person", "body_size", "VARCHAR(30)")
            ops.add_column("meta_person", "cup_size", "VARCHAR(10)")

            # 소속 및 추가 속성
            ops.add_column("meta_person", "blood_type", "VARCHAR(5)")
            ops.add_column("meta_person", "agency", "VARCHAR(100)")
            ops.add_column("meta_person", "country", "VARCHAR(50)")
            ops.add_column("meta_person", "info_url", "VARCHAR(500)")

            # 미디어 자산 상태 (풀스캔 제거용 인덱스 컬럼)
            bool_type = "BOOLEAN" if ops.dialect == 'postgresql' else "INTEGER"
            bool_false = "FALSE" if ops.dialect == 'postgresql' else "0"
            ops.add_column("meta_person", "has_photo", bool_type, server_default=bool_false)
            ops.add_column("meta_person", "thumb_url", "VARCHAR(500)")
            ops.add_column("meta_person", "local_path", "VARCHAR(255)")
            ops.add_column("meta_person", "gdrive_fileid", "VARCHAR(255)")
            ops.add_column("meta_person", "custom_gdrive_fileid", "VARCHAR(255)")
            ops.add_column("meta_person", "custom_local_path", "VARCHAR(255)")
            ops.add_column("meta_person", "official_cached_gdrive_fileid", "VARCHAR(255)")
            ops.add_column("meta_person", "official_cached_local_path", "VARCHAR(500)")
            ops.add_column("meta_person", "official_remote_path", "VARCHAR(500)")
            ops.add_column("meta_person", "custom_remote_path", "VARCHAR(500)")
            ops.add_column("meta_person", "site_img_url", "VARCHAR(500)")
            ops.add_column("meta_person", "photo_urls", "JSON")

            # 다중 ID 통합 및 통계 카운트 컬럼
            ops.add_column("meta_person", "merged_count", "INTEGER", server_default="1")
            ops.add_column("meta_person", "alt_indices", "JSON")
            ops.add_column("meta_person", "merged_details", "JSON")
            ops.add_column("meta_person", "works_count", "INTEGER", server_default="0")

            # 수동 수정 보호 및 원본 보존 JSON 컬럼
            ops.add_column("meta_person", "source_data", "JSON")
            ops.add_column("meta_person", "manual_data", "JSON")
            ops.add_column("meta_person", "created_time", "DATETIME")
            ops.add_column("meta_person", "updated_time", "DATETIME")

            # 고속 검색 및 필터링을 위한 B-Tree 인덱스 생성
            ops.create_index("ix_meta_person_gender", "meta_person", ["gender"])
            ops.create_index("ix_meta_person_birth_year", "meta_person", ["birth_year"])
            ops.create_index("ix_meta_person_height", "meta_person", ["height"])
            ops.create_index("ix_meta_person_cup_size", "meta_person", ["cup_size"])
            ops.create_index("ix_meta_person_agency", "meta_person", ["agency"])
            ops.create_index("ix_meta_person_has_photo", "meta_person", ["has_photo"])
            ops.create_index("ix_meta_person_merged_count", "meta_person", ["merged_count"])
            ops.create_index("ix_meta_person_works_count", "meta_person", ["works_count"])
            ops.create_index("ix_meta_person_gdrive_fileid", "meta_person", ["gdrive_fileid"])

    @property
    def has_backfill(self) -> bool:
        return True

    def run_backfill(self, session, context: dict, progress_callback=None) -> None:
        tables = context.get('tables', {'meta_media', 'meta_item', 'meta_person'})
        if 'meta_media' in tables:
            self._backfill_media_assets(session, context, progress_callback)
        if 'meta_item' in tables:
            self._backfill_item_video_assets(session, context, progress_callback)
        if 'meta_person' in tables:
            self._backfill_person_records(session, context, progress_callback)

    @staticmethod
    def _chunk_start(context, table, last_id):
        logger.debug('[Migration Backfill] %s/%s: chunk start after id=%s',
                     context.get('physical_name', ''), table, last_id)
        return time.monotonic()

    @staticmethod
    def _chunk_complete(context, table, last_id, count, started):
        logger.debug('[Migration Backfill] %s/%s: chunk committed rows=%s last_id=%s (%.3fs)',
                     context.get('physical_name', ''), table, count, last_id,
                     time.monotonic() - started)
        from ...util_runtime import cooperate
        cooperate()

    def _backfill_media_assets(self, session, context: dict, progress_callback=None):
        from ...mod_meta_db import MetaMedia, MetaItem
        from ...util_metadata import MetaUtil
        from sqlalchemy.orm import lazyload, load_only
        last_id = context.get('positions', {}).get('meta_media', 0)
        while True:
            if context.get('stop_flag_checker', lambda: False)():
                raise InterruptedError("사용자 요청으로 백필 작업이 중단되었습니다.")
            started = self._chunk_start(context, 'meta_media', last_id)
            rows = session.query(MetaMedia).options(
                lazyload('*'), load_only(MetaMedia.id, MetaMedia.url, MetaMedia.gdrive_fileid,
                    MetaMedia.relative_path, MetaMedia.media_type, MetaMedia.is_user, MetaMedia.source)
            ).join(MetaItem, MetaMedia.item_id == MetaItem.id).filter(
                MetaItem.category.in_(['JAV_CEN', 'JAV_UNCEN', 'WESTERN']), MetaMedia.id > last_id
            ).order_by(MetaMedia.id).limit(self.BACKFILL_BATCH_SIZE).all()
            if not rows:
                break
            for media in rows:
                clean_url = str(media.url or '').strip()
                if not media.gdrive_fileid and ('drive.google.com' in clean_url or 'googleusercontent.com' in clean_url):
                    match = re.search(r'[?&]id=([^&]+)|/d/([^/]+)', clean_url)
                    if match:
                        media.gdrive_fileid = match.group(1) or match.group(2)
                if clean_url and not clean_url.startswith(('http://', 'https://')) and not media.relative_path:
                    media.relative_path = clean_url
                if media.media_type in ('poster', 'landscape', 'fanart') and MetaUtil.is_user_media(media):
                    media.is_user = True
                    media.source = 'user'
                last_id = media.id
            session.commit()
            if context.get('checkpoint'):
                context['checkpoint']('meta_media', last_id)
            session.expunge_all()
            count = len(rows)
            rows.clear()
            self._chunk_complete(context, 'meta_media', last_id, count, started)
            if progress_callback:
                progress_callback(last_id, last_id + 1, "미디어 자산 이전 중", context.get('physical_name', ''))

    def _backfill_item_video_assets(self, session, context, progress_callback=None):
        from ...mod_meta_db import MetaItem, MetaMedia, ModuleMetaDb
        from sqlalchemy.orm import lazyload, load_only, selectinload
        last_id = context.get('positions', {}).get('meta_item_video', 0)
        while True:
            if context.get('stop_flag_checker', lambda: False)():
                raise InterruptedError("사용자 요청으로 백필 작업이 중단되었습니다.")
            started = self._chunk_start(context, 'meta_item_video', last_id)
            # LIMIT bounds parent items only. Default joined collections would multiply
            # images x actors x tags x fingerprints and repeat large JSON columns.
            # This helper edits video rows only; leave all image relationships untouched.
            rows = session.query(MetaItem).options(
                lazyload('*'), load_only(MetaItem.id, MetaItem.category, MetaItem.extra_info),
                selectinload(MetaItem.media_files.and_(MetaMedia.media_type.in_(('trailer', 'preview'))))
            ).filter(MetaItem.category.in_(
                ['JAV_CEN', 'JAV_UNCEN', 'WESTERN']), MetaItem.id > last_id
            ).order_by(MetaItem.id).limit(self.BACKFILL_BATCH_SIZE).all()
            if not rows:
                break
            for item in rows:
                extra = copy.deepcopy(item.extra_info or {})
                for key in ('trailer_video', 'preview_clip'):
                    info = extra.get(key)
                    if not isinstance(info, dict):
                        continue
                    # Migrate recorded paths only. A filename alone cannot locate its parent folder.
                    recorded = info.get('relative_path') or info.get('remote_relative_path') or info.get('remote_path')
                    if isinstance(recorded, str) and recorded and not recorded.startswith(('/', '\\')) and ':' not in recorded and '..' not in recorded.replace('\\', '/').split('/'):
                        info['relative_path'] = recorded.replace('\\', '/')
                        info['filename'] = info['relative_path'].rsplit('/', 1)[-1]
                item.extra_info = extra
                ModuleMetaDb.sync_item_video_assets(item)
                last_id = item.id
            session.commit()
            if context.get('checkpoint'):
                context['checkpoint']('meta_item_video', last_id)
            session.expunge_all()
            count = len(rows)
            rows.clear()
            self._chunk_complete(context, 'meta_item_video', last_id, count, started)
            if progress_callback:
                progress_callback(last_id, last_id + 1, "영상 저장 경로 이전 중", context.get('physical_name', ''))

    def _backfill_person_records(self, session, context: dict, progress_callback=None):
        from ...mod_meta_db import MetaPerson, ModuleMetaDb
        from sqlalchemy.orm import lazyload, defer
        last_id = context.get('positions', {}).get('meta_person', 0)
        total = session.query(MetaPerson).filter(MetaPerson.domain.in_(['JAV', 'WESTERN'])).count()
        processed = 0
        while True:
            if context.get('stop_flag_checker', lambda: False)():
                raise InterruptedError("사용자 요청으로 백필 작업이 중단되었습니다.")
            started = self._chunk_start(context, 'meta_person', last_id)
            rows = session.query(MetaPerson).options(
                lazyload('*'), defer(MetaPerson.works)
            ).filter(MetaPerson.domain.in_(['JAV', 'WESTERN']), MetaPerson.id > last_id
            ).order_by(MetaPerson.id).limit(self.BACKFILL_BATCH_SIZE).all()
            if not rows:
                break
            for person in rows:
                last_id = person.id
                processed += 1
                # NULL identifies legacy columns. Empty v1 values are intentional data.
                if person.source_data is not None or person.manual_data is not None:
                    continue
                extra = copy.deepcopy(person.extra_info or {})
                manual = copy.deepcopy(extra.get('manual_data') or {})
                media = ModuleMetaDb.normalize_person_media(person.media_src, domain=person.domain)
                aliases = {'birth': 'birth_date', 'debut': 'debut_date', 'bra_size': 'cup_size', 'blood': 'blood_type'}
                source = copy.deepcopy(extra.get('source_data') or {})
                for field in ModuleMetaDb.PERSON_SOURCE_FIELDS:
                    attr = aliases.get(field, field)
                    if field not in source:
                        source[field] = extra.get(field, getattr(person, attr, ''))
                    if not hasattr(person, attr):
                        continue
                    if field in manual:
                        value = manual[field]
                    elif getattr(person, attr, None) not in (None, ''):
                        continue
                    else:
                        value = source.get(field)
                    if field == 'height':
                        value = self._parse_integer(value)
                    setattr(person, attr, value if field == 'height' else (value if value is not None else ''))
                person.source_data = source
                person.manual_data = manual
                person.birth_year = self._parse_integer(str(person.birth_date or '')[:4])
                bust, waist, hip, cup = self._parse_body_size(person.body_size)
                for attr, value in [('bust', bust), ('waist', waist), ('hip', hip), ('cup_size', cup)]:
                    if getattr(person, attr, None) in (None, ''):
                        setattr(person, attr, value)
                raw_gender = str(manual.get('gender', extra.get('gender') or person.gender or 'female')).strip().lower()
                person.gender = 'male' if raw_gender in ('m', 'male', '남', '남성') else 'female'
                source_gender = str(source.get('gender') or person.gender).strip().lower()
                source['gender'] = 'male' if source_gender in ('m', 'male', '남', '남성') else 'female'
                if 'gender' in manual: manual['gender'] = person.gender
                for attr in ['country', 'info_url']:
                    if getattr(person, attr, None) in (None, ''):
                        setattr(person, attr, extra.get(attr) or '')
                person.alt_indices = copy.deepcopy(extra.get('alt_actor_indices') or person.alt_indices or [])
                person.merged_details = copy.deepcopy(extra.get('merged_sub_actors') or person.merged_details or [])
                person.merged_count = max(1, len(person.alt_indices))
                person.works_count = sum(len(v) for v in (person.works or {}).values() if isinstance(v, list))
                # The full works JSON is needed only for its count, not the rest of the chunk.
                session.expire(person, ['works'])
                person.media_src = media
                ModuleMetaDb.sync_person_media_columns(person)
                person.thumb_url = ModuleMetaDb.resolve_person_active_thumb(person)
                person.has_photo = bool(person.thumb_url)
                person.created_time = person.created_time or datetime.now()
                person.updated_time = datetime.now()
                for key in list(aliases) + list(aliases.values()) + list(ModuleMetaDb.PERSON_SOURCE_FIELDS) + ['source_data', 'manual_data', 'alt_actor_indices', 'merged_sub_actors', 'local_img_url']:
                    if key not in ('hobby', 'specialty'):
                        extra.pop(key, None)
                person.extra_info = extra
            session.commit()
            if context.get('checkpoint'):
                context['checkpoint']('meta_person', last_id)
            session.expunge_all()
            count = len(rows)
            rows.clear()
            self._chunk_complete(context, 'meta_person', last_id, count, started)
            if progress_callback:
                progress_callback(processed, total, "인물 데이터 이전 중", context.get('physical_name', 'PERSON'))

    @staticmethod
    def _parse_integer(val):
        """문자열 등에서 순수 정수를 추출하여 반환."""
        if val is None or val == '':
            return None
        m = re.search(r'\d+', str(val))
        return int(m.group()) if m else None

    @staticmethod
    def _parse_body_size(body_str):
        """B85(E)-W58-H86, 85-58-86 등 다양한 형태의 신체 사이즈 문자열을 파싱."""
        bust, waist, hip, cup = None, None, None, None
        if not body_str:
            return bust, waist, hip, cup

        # 컵 사이즈 추출 (예: B85(E) 또는 85E)
        m_cup = re.search(r'[A-Za-z]\s*(?=[0-9])|[B-H]\b|\(([A-Za-z]+)\)', body_str)
        if m_cup:
            cup_cand = m_cup.group(1) if m_cup.group(1) else m_cup.group(0)
            clean_cup = re.sub(r'[^A-Za-z]', '', cup_cand).upper()
            if clean_cup:
                cup = clean_cup

        # 연속된 숫자 3개 추출 (B-W-H)
        numbers = re.findall(r'\b\d{2,3}\b', body_str)
        if len(numbers) >= 3:
            bust = int(numbers[0])
            waist = int(numbers[1])
            hip = int(numbers[2])

        return bust, waist, hip, cup
