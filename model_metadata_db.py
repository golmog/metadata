import os
import json
import traceback
import re
import math
from urllib.parse import urlparse, parse_qs
from datetime import datetime

from sqlalchemy import create_engine, Column, Integer, String, JSON, DateTime, or_, func, text
from sqlalchemy.orm import sessionmaker, scoped_session, load_only
from sqlalchemy.ext.declarative import declarative_base
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
    cursor.execute("PRAGMA mmap_size=268435456") # 256MB
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
            if not code: return False

            originaltitle = entity_dict.get('originaltitle', '')
            if not originaltitle:
                originaltitle = code

            poster_url = ""
            for thumb in entity_dict.get('thumb', []):
                if isinstance(thumb, dict) and thumb.get('aspect') == 'poster':
                    poster_url = thumb.get('value', '')
                    break

            record = av_db_session.query(cls).filter_by(code=code).first()
            if record:
                record.originaltitle = originaltitle
                record.site = entity_dict.get('site', 'unknown')
                record.title = entity_dict.get('title', '')
                record.poster_url = poster_url
                record.json_data = entity_dict
                record.updated_time = datetime.now()
                logger.info(f"[MetaDB] DB 캐시가 최신 데이터로 업데이트 되었습니다: {code}")
            else:
                record = cls(
                    category=category,
                    code=code,
                    originaltitle=originaltitle,
                    site=entity_dict.get('site', 'unknown'),
                    title=entity_dict.get('title', ''),
                    poster_url=poster_url,
                    json_data=entity_dict
                )
                av_db_session.add(record)
                logger.info(f"[MetaDB] 신규 데이터가 DB에 저장되었습니다: {code}")
            
            av_db_session.commit()
            return True
        except Exception as e:
            logger.error(f"[MetaDB] Save Error for {entity_dict.get('code')}: {e}")
            av_db_session.rollback()
            return False

    @classmethod
    def get_metadata(cls, code):
        try:
            record = av_db_session.query(cls).filter_by(code=code).first()
            if record:
                return record.json_data
        except Exception as e:
            logger.error(f"[MetaDB] Get Error for {code}: {e}")
        return None

    @classmethod
    def web_list(cls, req, category=None):        
        try:
            if not category:
                path = req.path.lower()
                if 'uncensored' in path:
                    category = 'UNCEN'
                elif 'western' in path:
                    category = 'WEST'
                else:
                    category = 'CEN'

            page = int(req.form.get('page', 1))
            search_word = req.form.get('search_word', '').strip()
            search_site = req.form.get('search_site', 'all')
            search_order = req.form.get('search_order', 'desc')
            page_size = int(req.form.get('page_size', 10))
            
            query = av_db_session.query(cls).filter_by(category=category)
            
            if search_site != 'all':
                query = query.filter_by(site=search_site)
            
            if search_word:
                search_like = f"%{search_word.replace('-', '%')}%"
                query = query.filter(or_(
                    cls.originaltitle.ilike(search_like),
                    cls.code.ilike(search_like),
                    cls.title.ilike(f'%{search_word}%')
                ))
            
            # --- 품번 정렬 (자연수 정렬: Natural Sort) ---
            if search_order in ['code_asc', 'code_desc']:
                all_records = query.options(load_only(cls.id, cls.originaltitle)).all()
                
                # 자연수 정렬 키 생성 함수 (예: 'ABC-234' -> ['abc-', 234])
                def natural_keys(record):
                    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', record.originaltitle)]
                
                # 파이썬의 내장 정렬 기능을 사용해 자연수 정렬 수행
                all_records.sort(key=natural_keys, reverse=(search_order == 'code_desc'))
                
                # 전체 갯수 및 페이지 슬라이싱
                count = len(all_records)
                page_records = all_records[(page - 1) * page_size : page * page_size]
                page_ids = [r.id for r in page_records]
                
                # 선택된 갯수의 ID만 가지고 풀 데이터를 가져온 뒤, 순서를 다시 맞춤
                if page_ids:
                    items_unordered = av_db_session.query(cls).filter(cls.id.in_(page_ids)).all()
                    items = sorted(items_unordered, key=lambda x: page_ids.index(x.id))
                else:
                    items = []
                    
            # --- 날짜 정렬 ---
            else:
                if search_order == 'asc':
                    query = query.order_by(cls.created_time.asc())
                else: # desc
                    query = query.order_by(cls.created_time.desc())
                    
                count = query.count()
                items = query.offset((page - 1) * page_size).limit(page_size).all()
            
            # 페이징 계산
            total_page = math.ceil(count / page_size) if count > 0 else 1
            start_page = ((page - 1) // 10) * 10 + 1
            end_page = min(start_page + 9, total_page)
            
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

            return {'success': True, 'paging': paging, 'list': item_list}
        except Exception as e:
            logger.error(f"[MetaDB] web_list error: {e}")
            return {'success': False, 'paging': None, 'list': []}

    @classmethod
    def delete_record(cls, code):
        try:
            av_db_session.query(cls).filter_by(code=code).delete()
            av_db_session.commit()
            return True
        except Exception as e:
            logger.error(f"[MetaDB] Delete Error for {code}: {e}")
            av_db_session.rollback()
            return False

    @classmethod
    def update_json(cls, code, new_json_data):
        try:
            logger.debug(f"[MetaDB] JSON 업데이트 시도: {code}")
            record = av_db_session.query(cls).filter_by(code=code).first()
            if record:
                record.json_data = new_json_data
                record.title = new_json_data.get('title', record.title)
                record.originaltitle = new_json_data.get('originaltitle', record.originaltitle)
                record.updated_time = datetime.now()
                av_db_session.commit()
                logger.info(f"[MetaDB] JSON 업데이트 성공: {code}")
                return True
            logger.warning(f"[MetaDB] 업데이트할 데이터를 찾을 수 없음: {code}")
        except Exception as e:
            logger.error(f"[MetaDB] Update JSON Error for {code}: {e}")
            av_db_session.rollback()
        return False

    @classmethod
    def clear_db(cls, category='CEN'):
        try:
            count = av_db_session.query(cls).filter_by(category=category).delete()
            av_db_session.commit()
            logger.info(f"[MetaDB] DB 초기화 완료. {count}건 삭제됨 (Category: {category})")
            return True, count
        except Exception as e:
            logger.error(f"[MetaDB] DB Clear Error: {e}")
            av_db_session.rollback()
            return False, 0

    @classmethod
    def checkpoint_wal(cls):
        try:
            av_db_session.execute(text('PRAGMA wal_checkpoint(TRUNCATE)'))
            av_db_session.commit()
            logger.info("[MetaDB] WAL 강제 병합 및 크기 초기화(TRUNCATE) 완료.")
            return True
        except Exception as e:
            logger.error(f"[MetaDB] WAL Checkpoint Error: {e}")
            av_db_session.rollback()
            return False

    @classmethod
    def vacuum_db(cls):
        try:
            av_db_session.execute(text('VACUUM'))
            av_db_session.execute(text('PRAGMA wal_checkpoint(TRUNCATE)'))
            av_db_session.commit()
            logger.info("[MetaDB] DB VACUUM 및 WAL 완전 병합(TRUNCATE) 완료.")
            return True
        except Exception as e:
            logger.error(f"[MetaDB] DB Vacuum Error: {e}")
            av_db_session.rollback()
            return False

    @classmethod
    def sanitize_for_export(cls, json_data):
        sanitized = json.loads(json.dumps(json_data))
        sanitized['thumb'] = []
        sanitized['fanart'] = []
        sanitized['extras'] = []
        if 'image_url' in sanitized:
            sanitized['image_url'] = ''
        return sanitized

    @classmethod
    def merge_record(cls, category, new_data, mode='update'):
        try:
            code = new_data.get('code')
            if not code: return 'skip'

            record = av_db_session.query(cls).filter_by(code=code).first()
            if record:
                if mode == 'missing':
                    return 'skip'

                # 스마트 병합: 내 기존 이미지/트레일러 보존
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
                record.updated_time = datetime.now()
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
                    json_data=new_data
                )
                av_db_session.add(new_record)
                av_db_session.flush()
                return 'inserted'
        except Exception as e:
            logger.error(f"[MetaDB] merge_record error ({new_data.get('code')}): {e}")
            return 'error'
