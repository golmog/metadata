import os
import json
import copy
import traceback
import math
from urllib.parse import urlparse, parse_qs
from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, or_, func, text
from sqlalchemy.orm import sessionmaker, scoped_session
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy import event
from sqlalchemy.pool import NullPool

from .setup import *

db_path = os.path.join(path_data, 'db', 'metadata_av.db')

engine = create_engine(
    f"sqlite:///{db_path}",
    connect_args={'check_same_thread': False, 'timeout': 15},
    poolclass=NullPool
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.execute("PRAGMA cache_size=-64000")
    cursor.execute("PRAGMA mmap_size=268435456")
    cursor.close()

av_db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))

Base = declarative_base()
Base.query = av_db_session.query_property()


class ModelAvMetadata(Base):
    __tablename__ = 'av_metadata_cache'

    id = Column(Integer, primary_key=True)
    category = Column(String(20), nullable=False, index=True)
    code = Column(String(100), nullable=False, unique=True, index=True)
    originaltitle = Column(String(255), nullable=False, index=True)
    site = Column(String(50), nullable=False)
    title = Column(String(255), nullable=False)
    poster_url = Column(String(500))
    json_data = Column(JSON, nullable=False)
    created_time = Column(DateTime, default=datetime.now)
    updated_time = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    def __init__(self, category, code, originaltitle, site, title, poster_url, json_data):
        self.category = category
        self.code = code
        self.originaltitle = originaltitle
        self.site = site
        self.title = title
        self.poster_url = poster_url
        self.json_data = json_data

    def as_dict(self):
        return {
            'id': self.id,
            'category': self.category,
            'code': self.code,
            'originaltitle': self.originaltitle,
            'site': self.site,
            'title': self.title,
            'poster_url': self.poster_url,
            'created_time': self.created_time.strftime('%Y-%m-%d %H:%M:%S') if self.created_time else '',
            'updated_time': self.updated_time.strftime('%Y-%m-%d %H:%M:%S') if self.updated_time else '',
            'json_data': self.json_data
        }

    @classmethod
    def save_metadata(cls, category, entity_dict):
        try:
            code = entity_dict.get('code')
            if not code:
                logger.warning("[MetaDB] save_metadata: 'code' 필드가 없어 저장할 수 없습니다.")
                return False

            originaltitle = entity_dict.get('originaltitle', '') or code
            site = entity_dict.get('site', 'unknown')
            title = entity_dict.get('title', '')

            poster_url = ""
            for thumb in entity_dict.get('thumb', []):
                if isinstance(thumb, dict) and thumb.get('aspect') == 'poster':
                    poster_url = thumb.get('value', '')
                    break

            record = av_db_session.query(cls).filter_by(code=code).first()
            if record:
                record.originaltitle = originaltitle
                record.site = site
                record.title = title
                record.poster_url = poster_url
                record.json_data = copy.deepcopy(entity_dict)
                flag_modified(record, "json_data")
                record.updated_time = datetime.now()
                logger.info(f"[MetaDB] 레코드 업데이트 완료: [{category}] {code} ({originaltitle})")
            else:
                record = cls(
                    category=category,
                    code=code,
                    originaltitle=originaltitle,
                    site=site,
                    title=title,
                    poster_url=poster_url,
                    json_data=copy.deepcopy(entity_dict)
                )
                av_db_session.add(record)
                logger.info(f"[MetaDB] 신규 레코드 저장 완료: [{category}] {code} ({originaltitle})")

            av_db_session.commit()
            return True
        except Exception as e:
            logger.error(f"[MetaDB] save_metadata 실패 ({entity_dict.get('code')}): {e}")
            logger.error(traceback.format_exc())
            av_db_session.rollback()
            return False

    @classmethod
    def get_metadata(cls, code):
        try:
            logger.debug(f"[MetaDB] get_metadata 조회 시도: {code}")
            record = av_db_session.query(cls).filter_by(code=code).first()
            if record:
                logger.debug(f"[MetaDB] get_metadata 캐시 히트: {code} [{record.category}]")
                return record.json_data
            logger.debug(f"[MetaDB] get_metadata 캐시 미스: {code}")
        except Exception as e:
            logger.error(f"[MetaDB] get_metadata 에러 ({code}): {e}")
            logger.error(traceback.format_exc())
        return None

    @classmethod
    def web_list(cls, req, category=None):
        import re
        from sqlalchemy.orm import load_only
        import math

        try:
            if not category:
                path = req.path.lower()
                if 'uncensored' in path: category = 'UNCEN'
                elif 'western' in path: category = 'WEST'
                else: category = 'CEN'

            page = int(req.form.get('page', 1))
            search_word = req.form.get('search_word', '').strip()
            search_site = req.form.get('search_site', 'all')
            search_order = req.form.get('search_order', 'desc')
            search_status = req.form.get('search_status', 'all')
            page_size = int(req.form.get('page_size', 10))

            # logger.debug(f"[MetaDB] web_list 요청: [{category}] page={page}, size={page_size}, site={search_site}, status={search_status}, order={search_order}, word='{search_word}'")

            query = av_db_session.query(cls).filter_by(category=category)

            # 사이트 필터
            if search_site != 'all':
                query = query.filter_by(site=search_site)

            # 상태별 필터
            if search_status == 'no_poster':
                query = query.filter(or_(cls.poster_url == '', cls.poster_url == None))
            elif search_status == 'no_plot':
                query = query.filter(or_(
                    func.json_extract(cls.json_data, '$.plot') == '',
                    func.json_extract(cls.json_data, '$.plot') == None
                ))
            elif search_status == 'complete':
                query = query.filter(
                    cls.poster_url != '',
                    cls.poster_url != None,
                    func.json_extract(cls.json_data, '$.plot') != '',
                    func.json_extract(cls.json_data, '$.plot') != None
                )

            # 검색어 필터
            if search_word:
                search_like = f"%{search_word.replace('-', '%')}%"
                query = query.filter(or_(
                    cls.originaltitle.ilike(search_like),
                    cls.code.ilike(search_like),
                    cls.title.ilike(f'%{search_word}%')
                ))

            # 정렬
            if search_order in ['code_asc', 'code_desc']:
                all_records = query.options(load_only(cls.id, cls.originaltitle)).all()
                def natural_keys(record):
                    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', record.originaltitle)]
                all_records.sort(key=natural_keys, reverse=(search_order == 'code_desc'))

                count = len(all_records)
                page_records = all_records[(page - 1) * page_size : page * page_size]
                page_ids = [r.id for r in page_records]

                if page_ids:
                    items_unordered = av_db_session.query(cls).filter(cls.id.in_(page_ids)).all()
                    items = sorted(items_unordered, key=lambda x: page_ids.index(x.id))
                else:
                    items = []
            else:
                if search_order == 'asc': query = query.order_by(cls.created_time.asc())
                else: query = query.order_by(cls.created_time.desc())

                count = query.count()
                items = query.offset((page - 1) * page_size).limit(page_size).all()

            total_page = math.ceil(count / page_size) if count > 0 else 1
            start_page = ((page - 1) // 10) * 10 + 1
            end_page = min(start_page + 9, total_page)

            # 실시간 URL 주소 치환
            module_name = 'jav_censored' if category == 'CEN' else ('jav_uncensored' if category == 'UNCEN' else 'western')
            url_mapping_str = P.ModelSetting.get(f"{module_name}_db_image_url_mapping") or ""
            mappings = []
            if url_mapping_str:
                for line in url_mapping_str.split('\n'):
                    line = line.strip()
                    if '|' in line:
                        parts = line.split('|', 1)
                        if parts[0].strip() and parts[1].strip():
                            mappings.append((parts[0].strip(), parts[1].strip()))

            item_list = []
            for item in items:
                d = item.as_dict()
                if d.get('poster_url') and mappings:
                    for src_url, dst_url in mappings:
                        if d['poster_url'].startswith(src_url):
                            d['poster_url'] = d['poster_url'].replace(src_url, dst_url, 1)
                            break
                item_list.append(d)

            paging = {
                'page': page,
                'current_page': page,
                'page_size': page_size,
                'list_step': page_size,
                'total_page': total_page,
                'total_count': count,
                'start_page': start_page,
                'end_page': end_page,
                'last_page': end_page,
                'prev_page': start_page - 1 if start_page > 1 else 0,
                'next_page': end_page + 1 if end_page < total_page else 0,
            }

            # logger.debug(f"[MetaDB] web_list 완료: [{category}] {len(item_list)}개 반환 (전체 {count}개, {page}/{total_page} 페이지)")
            return {'success': True, 'paging': paging, 'list': item_list}
        except Exception as e:
            logger.error(f"[MetaDB] web_list 처리 에러: {e}")
            logger.error(traceback.format_exc())
            return {'success': False, 'paging': None, 'list': []}

    @classmethod
    def delete_record(cls, code):
        try:
            deleted_count = av_db_session.query(cls).filter_by(code=code).delete()
            av_db_session.commit()
            if deleted_count > 0:
                logger.info(f"[MetaDB] 레코드 삭제 완료: {code}")
                return True
            else:
                logger.warning(f"[MetaDB] delete_record: 삭제 대상 레코드 없음 ({code})")
                return False
        except Exception as e:
            logger.error(f"[MetaDB] delete_record 실패 ({code}): {e}")
            logger.error(traceback.format_exc())
            av_db_session.rollback()
            return False

    @classmethod
    def update_json(cls, code, new_json_data):
        try:
            logger.debug(f"[MetaDB] update_json 수동 편집 저장 시도: {code}")
            record = av_db_session.query(cls).filter_by(code=code).first()
            if record:
                record.json_data = copy.deepcopy(new_json_data)
                record.title = new_json_data.get('title', record.title)
                record.originaltitle = new_json_data.get('originaltitle', record.originaltitle)
                flag_modified(record, "json_data")
                record.updated_time = datetime.now()
                av_db_session.commit()
                logger.info(f"[MetaDB] update_json 저장 성공: {code} ({record.originaltitle})")
                return True
            logger.warning(f"[MetaDB] update_json 대상 레코드 없음: {code}")
        except Exception as e:
            logger.error(f"[MetaDB] update_json 실패 ({code}): {e}")
            logger.error(traceback.format_exc())
            av_db_session.rollback()
        return False

    @classmethod
    def sanitize_for_export(cls, json_data):
        sanitized = copy.deepcopy(json_data)
        sanitized['thumb'] = []
        sanitized['fanart'] = []
        sanitized['extras'] = []
        if 'image_url' in sanitized:
            sanitized['image_url'] = ''
        return sanitized

    @classmethod
    def clear_db(cls, category='CEN'):
        try:
            logger.info(f"[MetaDB] clear_db 실행: Category={category}")
            count = av_db_session.query(cls).filter_by(category=category).delete()
            av_db_session.commit()
            logger.info(f"[MetaDB] clear_db 완료: [{category}] {count}건 레코드 삭제됨")
            return True, count
        except Exception as e:
            logger.error(f"[MetaDB] clear_db 실패 ({category}): {e}")
            logger.error(traceback.format_exc())
            av_db_session.rollback()
            return False, 0

    @classmethod
    def checkpoint_wal(cls):
        try:
            logger.debug("[MetaDB] checkpoint_wal(TRUNCATE) 실행 중...")
            av_db_session.execute(text('PRAGMA wal_checkpoint(TRUNCATE)'))
            av_db_session.commit()
            logger.debug("[MetaDB] checkpoint_wal 완료 (WAL 파일 크기 0바이트 초기화)")
            return True
        except Exception as e:
            logger.error(f"[MetaDB] checkpoint_wal 에러: {e}")
            logger.error(traceback.format_exc())
            av_db_session.rollback()
            return False

    @classmethod
    def vacuum_db(cls):
        try:
            logger.info("[MetaDB] vacuum_db 실행 (VACUUM + WAL TRUNCATE)...")
            av_db_session.execute(text('VACUUM'))
            av_db_session.execute(text('PRAGMA wal_checkpoint(TRUNCATE)'))
            av_db_session.commit()
            logger.info("[MetaDB] vacuum_db 완료 (DB 최적화 및 WAL 완전 정리)")
            return True
        except Exception as e:
            logger.error(f"[MetaDB] vacuum_db 에러: {e}")
            logger.error(traceback.format_exc())
            av_db_session.rollback()
            return False

    @classmethod
    def merge_record(cls, category, new_data, mode='update'):
        try:
            code = new_data.get('code')
            if not code:
                logger.warning("[MetaDB] merge_record: 'code' 필드 누락으로 스킵")
                return 'skip'

            record = av_db_session.query(cls).filter_by(code=code).first()
            if record:
                if mode == 'missing':
                    logger.debug(f"[MetaDB] merge_record: 이미 존재하여 스킵 (mode=missing): {code}")
                    return 'skip'

                existing_poster_url = record.poster_url or ''
                existing_thumbs = record.json_data.get('thumb', [])
                existing_fanarts = record.json_data.get('fanart', [])
                existing_extras = record.json_data.get('extras', [])

                merged_json = dict(new_data)
                if existing_thumbs: merged_json['thumb'] = existing_thumbs
                if existing_fanarts: merged_json['fanart'] = existing_fanarts
                if existing_extras: merged_json['extras'] = existing_extras

                record.originaltitle = new_data.get('originaltitle', record.originaltitle)
                record.site = new_data.get('site', record.site)
                record.title = new_data.get('title', record.title)
                record.poster_url = existing_poster_url if existing_poster_url else (merged_json.get('image_url') or '')
                record.json_data = merged_json
                flag_modified(record, "json_data")
                record.updated_time = datetime.now()
                logger.debug(f"[MetaDB] merge_record 스마트 갱신: [{category}] {code} ({record.originaltitle})")
                return 'updated'
            else:
                poster_url = ""
                for thumb in new_data.get('thumb', []):
                    if isinstance(thumb, dict) and thumb.get('aspect') == 'poster':
                        poster_url = thumb.get('value', '')
                        break

                new_record = cls(
                    category=category,
                    code=code,
                    originaltitle=new_data.get('originaltitle', code),
                    site=new_data.get('site', 'unknown'),
                    title=new_data.get('title', ''),
                    poster_url=poster_url,
                    json_data=copy.deepcopy(new_data)
                )
                av_db_session.add(new_record)
                av_db_session.flush()
                logger.debug(f"[MetaDB] merge_record 신규 등록: [{category}] {code} ({new_record.originaltitle})")
                return 'inserted'
        except Exception as e:
            logger.error(f"[MetaDB] merge_record 에러 ({new_data.get('code')}): {e}")
            logger.error(traceback.format_exc())
            return 'error'

    @classmethod
    def update_user_image_by_filename(cls, filename):
        try:
            clean_name = os.path.basename(filename).strip()
            logger.debug(f"[MetaDB] update_user_image_by_filename 시작: {clean_name}")

            if '_pl_user.' in clean_name.lower() or '_pl.' in clean_name.lower():
                target_aspect = 'landscape'
                stem = re.split(r'_pl(?:_user)?\.', clean_name, flags=re.I)[0]
            else:
                target_aspect = 'poster'
                stem = re.split(r'_p(?:_user)?\.', clean_name, flags=re.I)[0]

            if not stem:
                logger.warning(f"[MetaDB] 파일명에서 품번(stem) 추출 실패: {clean_name}")
                return 'skipped', None, f"품번 추출 실패: {clean_name}"

            record = av_db_session.query(cls).filter(
                or_(
                    cls.originaltitle.ilike(stem),
                    cls.code.ilike(stem)
                )
            ).first()

            if not record:
                old_file = clean_name.replace('_user', '')
                record = av_db_session.query(cls).filter(cls.poster_url.ilike(f"%/{old_file}")).first()

            if not record:
                logger.debug(f"[MetaDB] 유저 이미지 매칭 실패 (DB에 레코드 없음): stem='{stem}' ({clean_name})")
                return 'not_found', None, f"DB 레코드 없음: {stem}"

            jd = copy.deepcopy(record.json_data) if record.json_data else {}
            thumbs = jd.get('thumb', [])
            if not isinstance(thumbs, list):
                thumbs = []

            is_already_set = False
            for thumb in thumbs:
                if isinstance(thumb, dict) and thumb.get('aspect') == target_aspect:
                    val = thumb.get('value', '')
                    if val and (val.endswith(f"/{clean_name}") or val == clean_name):
                        is_already_set = True
                        break

            if target_aspect == 'poster' and record.poster_url:
                if not (record.poster_url.endswith(f"/{clean_name}") or record.poster_url == clean_name):
                    is_already_set = False

            if is_already_set:
                logger.debug(f"[MetaDB] 유저 이미지 이미 적용됨 (스킵): {record.code} [{target_aspect}] -> {clean_name}")
                return 'already', record.code, {
                    'code': record.code,
                    'originaltitle': record.originaltitle,
                    'category': record.category,
                    'type': target_aspect,
                    'file': clean_name,
                    'status': 'already_applied'
                }

            new_image_url = None
            updated_thumb = False
            for thumb in thumbs:
                if isinstance(thumb, dict) and thumb.get('aspect') == target_aspect:
                    old_url = thumb.get('value', '')
                    if old_url and '/' in old_url:
                        new_image_url = f"{old_url.rsplit('/', 1)[0]}/{clean_name}"
                        thumb['value'] = new_image_url
                        updated_thumb = True
                        break

            if not updated_thumb:
                base_dir = record.poster_url.rsplit('/', 1)[0] if (record.poster_url and '/' in record.poster_url) else ""
                new_image_url = f"{base_dir}/{clean_name}" if base_dir else clean_name
                thumbs.append({
                    'aspect': target_aspect,
                    'value': new_image_url,
                    'thumb': '',
                    'site': record.site or '',
                    'score': 0
                })

            jd['thumb'] = thumbs

            if target_aspect == 'poster' and new_image_url:
                record.poster_url = new_image_url

            record.json_data = jd
            flag_modified(record, "json_data")
            record.updated_time = datetime.now()

            logger.info(f"[MetaDB] 유저 이미지 갱신 성공: [{record.category}] {record.code} ({record.originaltitle}) [{target_aspect}] -> {clean_name}")
            return 'updated', record.code, {
                'code': record.code,
                'originaltitle': record.originaltitle,
                'category': record.category,
                'type': target_aspect,
                'file': clean_name,
                'url': new_image_url
            }

        except Exception as e:
            logger.error(f"[MetaDB] update_user_image_by_filename 에러 ({filename}): {e}")
            logger.error(traceback.format_exc())
            return 'error', None, str(e)
