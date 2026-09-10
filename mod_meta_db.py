# -*- coding: utf-8 -*-
import os
import re
import copy
import json
import time
import sqlite3
import threading
import traceback
import math
import unicodedata
from datetime import datetime
from io import BytesIO
from urllib.parse import urlparse, urlencode
from flask import send_from_directory, send_file, jsonify, Response, abort, render_template
import requests

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, 
    Text, JSON, DateTime, ForeignKey, or_, and_, func, text, Index,
    case, cast
)
from sqlalchemy.orm import sessionmaker, scoped_session, relationship, lazyload
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.pool import NullPool, QueuePool
from sqlalchemy import event, inspect

from .setup import *
from support import SupportYaml
from framework import db
from .util_metadata import MetaImageUtil

Base = declarative_base()


# =========================================================================
# 1. 도메인 분기 매핑
# =========================================================================

DOMAIN_MAP = {
    'JAV_CEN': ('jav_cen', 'meta_db_jav_cen.db'),
    'JAV_UNCEN': ('jav_uncen', 'meta_db_jav_uncen.db'),
    'WESTERN': ('western', 'meta_db_western.db'),
    'PERSON': ('person', 'meta_db_person.db'),
    'MOVIE': ('movie', 'meta_db_movie.db'),
    'KTV': ('ktv', 'meta_db_ktv.db'),
    'FTV': ('ftv', 'meta_db_ftv.db'),
}


# =========================================================================
# 2. 통합 ORM 스키마 정의
# =========================================================================

class MetaItem(Base):
    """미디어 메타데이터 통합 마스터 테이블"""
    __tablename__ = 'meta_item'

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain = Column(String(30), nullable=False, index=True)
    category = Column(String(30), nullable=False, index=True)
    code = Column(String(100), nullable=False, unique=True, index=True)
    ui_code = Column(String(255), nullable=False, index=True)
    originaltitle = Column(String(255), nullable=False, index=True)
    sorttitle = Column(String(500), index=True)
    site = Column(String(50), nullable=False, index=True)

    title = Column(String(500), nullable=False, index=True)
    tagline = Column(String(500))
    plot = Column(Text)

    director = Column(String(255))
    studio = Column(String(255), index=True)
    series = Column(String(255), index=True)

    premiered = Column(String(20), index=True)
    year = Column(Integer, index=True, default=1900)
    runtime = Column(Integer, default=0)

    rating = Column(Float, default=0.0)
    rating_votes = Column(Integer, default=0)
    mpaa = Column(String(50), default="")
    content_type = Column(String(50), default="")

    poster_url = Column(String(500))
    parent_id = Column(Integer, ForeignKey('meta_item.id', ondelete='SET NULL'), nullable=True, index=True)

    original = Column(JSON, default=dict)
    spec_data = Column(JSON, default=dict)
    extra_info = Column(JSON, default=dict)

    created_time = Column(DateTime, default=datetime.now, index=True)
    updated_time = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    media_files = relationship("MetaMedia", backref="item", cascade="all, delete-orphan", lazy="joined")
    person_maps = relationship("MetaItemPersonMap", backref="item", cascade="all, delete-orphan", lazy="joined", order_by="MetaItemPersonMap.sort_order")
    tag_maps = relationship("MetaItemTagMap", backref="item", cascade="all, delete-orphan", lazy="joined")
    fingerprints = relationship("MetaFingerprint", backref="item", cascade="all, delete-orphan", lazy="joined")


class MetaFingerprint(Base):
    """비디오 지문(OSHash, pHash 등) 고속 B-Tree 색인 테이블"""
    __tablename__ = 'meta_fingerprint'

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey('meta_item.id', ondelete='CASCADE'), nullable=False, index=True)
    category = Column(String(30), nullable=False, default="WESTERN", index=True)
    code = Column(String(100), nullable=False, index=True)
    algorithm = Column(String(20), nullable=False)
    hash_value = Column(String(64), nullable=False, index=True)
    source = Column(String(20), default="user")
    created_time = Column(DateTime, default=datetime.now)

    __table_args__ = (
        Index('ix_fp_algo_hash', 'algorithm', 'hash_value'),
    )


class MetaPerson(Base):
    """통합 인물/배우 마스터 테이블 (도메인: GENERAL, JAV, WESTERN)"""
    __tablename__ = 'meta_person'

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain = Column(String(20), nullable=False, default="GENERAL", index=True)
    name_org = Column(String(100), nullable=False, index=True)
    name_ko = Column(String(100), index=True)
    name_en = Column(String(100), index=True)
    other_names = Column(Text)
    aliases = Column(JSON, default=list)
    person_type = Column(String(30), default="actor", index=True)
    person_idx = Column(String(50), index=True)

    # 성격별로 분리된 3대 페이로드 컬럼
    media_src = Column(JSON, default=dict)
    works = Column(JSON, default=dict)
    extra_info = Column(JSON, default=dict)


class MetaItemPersonMap(Base):
    """작품과 인물 간의 N:M 매핑 테이블"""
    __tablename__ = 'meta_item_person_map'

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey('meta_item.id', ondelete='CASCADE'), nullable=False, index=True)
    person_id = Column(Integer, ForeignKey('meta_person.id', ondelete='CASCADE'), nullable=False, index=True)
    role_type = Column(String(30), default="actor", index=True)
    role_name = Column(String(100), default="출연")
    sort_order = Column(Integer, default=0)

    person = relationship("MetaPerson", lazy="joined")


class MetaTag(Base):
    """장르, 태그 마스터 테이블"""
    __tablename__ = 'meta_tag'

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain = Column(String(20), nullable=False, default="JAV", index=True)
    name = Column(String(100), nullable=False, unique=True, index=True)
    name_org = Column(String(100), index=True)
    tag_type = Column(String(30), default="genre", index=True)


class MetaItemTagMap(Base):
    """작품과 장르/태그 간의 N:M 매핑 테이블"""
    __tablename__ = 'meta_item_tag_map'

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey('meta_item.id', ondelete='CASCADE'), nullable=False, index=True)
    tag_id = Column(Integer, ForeignKey('meta_tag.id', ondelete='CASCADE'), nullable=False, index=True)

    tag = relationship("MetaTag", lazy="joined")


class MetaMedia(Base):
    """멀티미디어 리소스 에셋 테이블"""
    __tablename__ = 'meta_media'

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey('meta_item.id', ondelete='CASCADE'), nullable=False, index=True)
    media_type = Column(String(30), nullable=False, index=True)
    url = Column(String(500), nullable=False)
    is_user = Column(Boolean, default=False, index=True)
    sort_order = Column(Integer, default=0)


# =========================================================================
# 3. 통합 메타 DB 인프라 컨트롤러 클래스: ModuleMetaDb
# =========================================================================

class ModuleMetaDb(PluginModuleBase):
    _engines = {}
    _sessions = {}
    _is_postgres = False
    _cached_actors_map = None
    _cached_actors_ver = None

    def __init__(self, P):
        super(ModuleMetaDb, self).__init__(P, name='meta_db', first_menu='setting')
        self.db_default = {
            f"{self.name}_db_version": "1",
            f"{self.name}_use": "False",
            f"{self.name}_save": "False",
            f"{self.name}_save_only_translated": "False",
            f"{self.name}_include_original": "True",
            f"{self.name}_delete_user_images": "False",
            f"{self.name}_image_url_mapping": "",
            f"{self.name}_use_ff_proxy": "False",
            f"{self.name}_engine_type": "sqlite",
            f"{self.name}_sqlite_dir": os.path.join(path_data, 'db', 'meta_db'),
            f"{self.name}_pg_host": "postgres",
            f"{self.name}_pg_port": "5432",
            f"{self.name}_pg_conn_type": "tcp",
            f"{self.name}_pg_socket_dir": "/var/run/postgresql",
            f"{self.name}_pg_user": "metadata",
            f"{self.name}_pg_pass": "",
            f"{self.name}_pg_name": "metadata",
            f"{self.name}_transfer_direction": "sqlite_to_pg",
            f"{self.name}_transfer_mode": "merge",
            f"{self.name}_import_path": "",
            f"{self.name}_auto_enrich": "True",
            f"{self.name}_enrich_delay": "2.0",

            # PERSON(배우) DB 동기화 표준 설정 키
            f"{self.name}_person_jav_auto_sync_actors": "False",
            f"{self.name}_person_jav_file_version": "0",
            f"{self.name}_person_jav_last_synced_version": "0",
        }

        self.transfer_status = {
            'is_running': False,
            'status': '대기 중',
            'mode': 'merge',
            'total': 0,
            'current': 0,
            'inserted': 0,
            'updated': 0,
            'skipped': 0,
            'fail': 0,
            'current_code': '',
            'stop_flag': False
        }
        self.import_status = {'is_running': False, 'status': '대기 중', 'total': 0, 'current': 0, 'inserted': 0, 'updated': 0, 'skipped': 0, 'fail': 0, 'current_code': '', 'stop_flag': False}

    @classmethod
    def init_engines(cls):
        """도메인별 SQLite 또는 PostgreSQL 통합 커넥션 풀 초기화"""
        for s in cls._sessions.values():
            try: s.remove()
            except: pass
        cls._sessions.clear()

        for e in cls._engines.values():
            try: e.dispose()
            except: pass
        cls._engines.clear()

        db_type = P.ModelSetting.get("meta_db_engine_type") or "sqlite"
        cls._is_postgres = (db_type == "postgres")

        if cls._is_postgres:
            try:
                import psycopg2
            except ImportError:
                logger.error("[MetaDB Engine] PostgreSQL 드라이버(psycopg2-binary)가 설치되어 있지 않습니다.")
                return

            pg_host = P.ModelSetting.get("meta_db_pg_host") or "postgres"
            pg_port = P.ModelSetting.get("meta_db_pg_port") or "5432"
            pg_user = P.ModelSetting.get("meta_db_pg_user") or "metadata"
            pg_pass = P.ModelSetting.get("meta_db_pg_pass") or ""
            pg_name = P.ModelSetting.get("meta_db_pg_name") or "metadata"
            pg_conn_type = P.ModelSetting.get("meta_db_pg_conn_type") or "tcp"

            # 소켓 연결과 TCP 연결 파라미터 분기
            if pg_conn_type == "socket":
                socket_dir = (P.ModelSetting.get("meta_db_pg_socket_dir") or "/var/run/postgresql").strip()
                db_url = f"postgresql+psycopg2://{pg_user}:{pg_pass}@/{pg_name}?client_encoding=utf8"
                connect_args = {"host": socket_dir, "connect_timeout": 10}
                log_target = f"socket:{socket_dir}/{pg_name}"
            else:
                pg_host = P.ModelSetting.get("meta_db_pg_host") or "postgres"
                pg_port = P.ModelSetting.get("meta_db_pg_port") or "5432"
                db_url = f"postgresql+psycopg2://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_name}?client_encoding=utf8"
                connect_args = {"connect_timeout": 10}
                log_target = f"{pg_host}:{pg_port}/{pg_name}"

            pg_engine = create_engine(
                db_url,
                poolclass=QueuePool,
                pool_size=15,
                max_overflow=30,
                pool_recycle=300,
                pool_pre_ping=True,
                connect_args=connect_args
            )

            cls._engines['postgres'] = pg_engine
            cls._sessions['postgres'] = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=pg_engine))
            Base.metadata.create_all(bind=pg_engine)
            cls._auto_sync_table_columns(pg_engine)
            # logger.info(f"[MetaDB Engine] PostgreSQL 통합 엔진 초기화 완료: {pg_user}@{log_target}")

        else:
            db_dir = P.ModelSetting.get("meta_db_sqlite_dir") or os.path.join(path_data, 'db', 'meta_db')
            os.makedirs(db_dir, exist_ok=True)

            json_dump_utf8 = lambda obj: json.dumps(obj, ensure_ascii=False)

            unique_domains = set(v[0] for v in DOMAIN_MAP.values())
            for dom in unique_domains:
                db_filename = next(v[1] for v in DOMAIN_MAP.values() if v[0] == dom)
                db_filepath = os.path.join(db_dir, db_filename)

                sq_engine = create_engine(
                    f"sqlite:///{db_filepath}",
                    connect_args={'check_same_thread': False, 'timeout': 30},
                    poolclass=NullPool,
                    json_serializer=json_dump_utf8
                )

                @event.listens_for(sq_engine, "connect")
                def set_sqlite_pragma(dbapi_connection, connection_record):
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA temp_store=MEMORY")
                    cursor.execute("PRAGMA cache_size=-64000")
                    cursor.execute("PRAGMA mmap_size=268435456")
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.close()

                cls._engines[dom] = sq_engine
                cls._sessions[dom] = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=sq_engine))
                Base.metadata.create_all(bind=sq_engine)
                cls._auto_sync_table_columns(sq_engine)

            # logger.debug(f"[MetaDB Engine] SQLite3 분산 도메인 DB 초기화 완료 (경로: {db_dir})")

    @classmethod
    def ensure_db_ready(cls):
        if not cls._engines or not cls._sessions:
            cls.init_engines()

    @classmethod
    def _auto_sync_table_columns(cls, target_engine):
        try:
            inspector = inspect(target_engine)
            is_pg = (target_engine.dialect.name == 'postgresql')
            with target_engine.connect() as conn:
                for table_name, table_obj in Base.metadata.tables.items():
                    if inspector.has_table(table_name):
                        existing_cols = {c['name']: c for c in inspector.get_columns(table_name)}
                        for column in table_obj.columns:
                            col_name = column.name
                            if col_name not in existing_cols:
                                col_type_sql = column.type.compile(target_engine.dialect)
                                alter_sql = f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type_sql}"
                                try:
                                    conn.execute(text(alter_sql))
                                    logger.info(f"[MetaDB Schema Auto-Sync] {table_name} 테이블에 '{col_name}' ({col_type_sql}) 컬럼 추가 완료")
                                except Exception as e_col:
                                    logger.debug(f"[MetaDB Schema Auto-Sync] {table_name}.{col_name} 스킵: {e_col}")
                            elif is_pg and col_name in ['ui_code', 'originaltitle', 'director', 'studio', 'series']:
                                # PostgreSQL 실존 컬럼의 길이가 작은 경우 255/500으로 자동 확장
                                try:
                                    conn.execute(text(f"ALTER TABLE {table_name} ALTER COLUMN {col_name} TYPE VARCHAR(255)"))
                                except Exception:
                                    pass
                conn.commit()
        except Exception as e:
            logger.debug(f"[MetaDB Schema Auto-Sync] 자동 동기화 예외: {e}")

    @classmethod
    def get_session_and_domain(cls, category):
        if not category:
            logger.error("[MetaDB] category 인자가 누락되었습니다.")
            return None, None, None

        cat_key = str(category).strip().upper()
        if cat_key not in DOMAIN_MAP:
            logger.error(f"[MetaDB] 유효하지 않은 category 입니다: '{category}'")
            return None, None, None

        dom_name, _ = DOMAIN_MAP[cat_key]

        if cls._is_postgres:
            sess = cls._sessions.get('postgres')
        else:
            sess = cls._sessions.get(dom_name)
            if not sess:
                cls.init_engines()
                sess = cls._sessions.get(dom_name)

        return sess, dom_name, cat_key

    @staticmethod
    def _person_domain_from_item_category(item_category):
        cat = str(item_category or "").upper()
        if cat in ["JAV_CEN", "JAV_UNCEN"]:
            return "JAV"
        if cat in ["WEST", "WESTERN"]:
            return "WESTERN"
        return "GENERAL"

    @classmethod
    def is_local_server_url(cls, url):
        if not url or not isinstance(url, str):
            return False
        clean = url.strip().lower()
        if '/images/' in clean or '/metadata/normal/' in clean:
            return True
        for key in ['jav_censored_image_server_url', 'western_image_server_url']:
            srv = (P.ModelSetting.get(key) or '').strip().lower().rstrip('/')
            if srv and clean.startswith(srv):
                return True
        ddns = (F.SystemModelSetting.get('ddns') or '').strip().lower().rstrip('/')
        if ddns and clean.startswith(ddns):
            return True
        return False

    @classmethod
    def find_latest_jav_actors_db(cls):
        target_dir = os.path.join(PLUGIN_ROOT, 'files')
        if not os.path.isdir(target_dir):
            logger.warning(f"[MetaDB ActorSync] 배포 폴더가 존재하지 않습니다: '{target_dir}'")
            P.ModelSetting.set("person_jav_file_version", "0")
            return None, "0"

        candidates = []
        pattern = re.compile(r'^jav_actors(?:2)?_(\d{8})\.db$', re.IGNORECASE)

        try:
            file_list = os.listdir(target_dir)
            for f in file_list:
                m = pattern.match(f)
                if m:
                    full_p = os.path.join(target_dir, f)
                    if os.path.isfile(full_p):
                        candidates.append((m.group(1), full_p))
        except Exception as e_scan:
            logger.error(f"[MetaDB ActorSync] 배포 폴더 스캔 오류 ('{target_dir}'): {e_scan}")
            return None, "0"

        if not candidates:
            logger.warning(f"[MetaDB ActorSync] '{target_dir}' 경로에서 jav_actors_YYYYMMDD.db 파일을 찾지 못했습니다.")
            P.ModelSetting.set("meta_db_person_jav_file_version", "0")
            return None, "0"

        candidates.sort(key=lambda x: x[0], reverse=True)
        latest_ver, latest_path = candidates[0][0], candidates[0][1]
        P.ModelSetting.set("meta_db_person_jav_file_version", latest_ver)
        # logger.debug(f"[MetaDB ActorSync] 배포 DB 파일 감지 완료 -> 최신 버전: {latest_ver}, 경로: '{latest_path}'")
        return latest_path, latest_ver

    @classmethod
    def _cluster_and_merge_actor_rows(cls, raw_rows):
        """
        avdbs 원본 행들을 안전 규칙(1단계: 이름+원문명+사진 일치, 2단계: 이름+영문/원문+생년월일 일치)에 따라
        동일인 클러스터로 묶고 원본 데이터를 보존한 단일 마스터 레코드로 합성합니다.
        """
        clusters = []
        cluster_map = {}

        for r in raw_rows:
            r_dict = dict(r) if hasattr(r, 'keys') else r
            actor_id = str(r_dict.get("actor_id") or "").strip()
            if not actor_id:
                continue

            name_org = str(r_dict.get("name_org") or "").strip()
            name_ko = str(r_dict.get("name_ko") or "").strip()
            site_img = str(r_dict.get("site_img_url") or "").strip()

            if not name_org:
                continue

            # 원문명과 대표 사진 URL 조합으로 동일인 여부를 O(1) 고속 매칭
            match_key = (name_org, site_img) if (name_org and site_img) else None
            matched_cluster = cluster_map.get(match_key) if match_key else None

            if matched_cluster:
                matched_cluster['sub_rows'].append(r_dict)
                if not matched_cluster['master'].get('name_ko') and name_ko:
                    matched_cluster['master']['name_ko'] = name_ko
            else:
                new_cluster = {
                    'master': r_dict,
                    'sub_rows': [r_dict]
                }
                clusters.append(new_cluster)
                if match_key:
                    cluster_map[match_key] = new_cluster

        # 각 클러스터별 대표 선정 및 원본 자산 병합
        merged_entities = []
        for cluster in clusters:
            all_sub = cluster['sub_rows']

            # 완성도 점수 계산하여 최적의 대표 선정 (Google Drive ID, 로컬 경로, 생년월일 등)
            def score_row(row_item):
                sc = 0
                if row_item.get('google_fileid'): sc += 10
                if row_item.get('local_img_path'): sc += 10
                if row_item.get('birth'): sc += 5
                if row_item.get('profile_height') or row_item.get('height'): sc += 2
                if row_item.get('body_size'): sc += 2
                if row_item.get('debut'): sc += 2
                return sc

            best_row = max(all_sub, key=score_row)

            # 별칭, 사진 URL 목록, 서브 ID 수집 (중복 제거)
            merged_aliases = set()
            merged_site_photos = []
            alt_ids = []

            for sub_r in all_sub:
                sub_id = str(sub_r.get('actor_id') or '').strip()
                if sub_id:
                    alt_ids.append(sub_id)

                onm = str(sub_r.get('other_names') or '').strip()
                if onm:
                    for a_tok in re.split(r'[,/]', onm):
                        if a_tok.strip():
                            merged_aliases.add(a_tok.strip())

                sub_en = str(sub_r.get('name_en') or '').strip()
                if sub_en:
                    merged_aliases.add(sub_en)

                sub_img = str(sub_r.get('site_img_url') or '').strip()
                if sub_img and sub_img.startswith('http') and sub_img not in merged_site_photos:
                    merged_site_photos.append(sub_img)

            clean_sub_records = []
            for sub_r in all_sub:
                s_id = str(sub_r.get('actor_id') or '').strip()
                clean_sub_records.append({
                    'actor_id': s_id,
                    'name_org': str(sub_r.get('name_org') or '').strip(),
                    'name_ko': str(sub_r.get('name_ko') or '').strip(),
                    'name_en': str(sub_r.get('name_en') or '').strip(),
                    'local_img_path': str(sub_r.get('local_img_path') or '').strip(),
                    'google_fileid': str(sub_r.get('google_fileid') or '').strip(),
                    'site_img_url': str(sub_r.get('site_img_url') or '').strip(),
                    'info_url': str(sub_r.get('info_url') or '').strip(),
                    'birth': str(sub_r.get('birth') or '').strip(),
                    'height': sub_r.get('profile_height') or sub_r.get('height'),
                    'body_size': str(sub_r.get('body_size') or '').strip(),
                    'bra_size': str(sub_r.get('bra_size') or '').strip(),
                    'debut': str(sub_r.get('debut') or '').strip(),
                    'agency': str(sub_r.get('agency') or '').strip(),
                    'blood': str(sub_r.get('blood') or '').strip(),
                    'hobby': str(sub_r.get('hobby') or '').strip(),
                    'specialty': str(sub_r.get('specialty') or '').strip(),
                })

            merged_entities.append({
                'master_row': best_row,
                'alt_actor_indices': alt_ids,
                'merged_sub_actors': clean_sub_records,
                'aliases': list(merged_aliases),
                'site_img_urls': merged_site_photos
            })

        return merged_entities


    @classmethod
    def get_cached_actors_map(cls):
        """배포 DB의 배우 레코드를 메모리에 클러스터링하여 캐싱. 서브 ID로도 마스터 레코드가 즉시 조회되도록 O(1) 역색인 구성"""
        if cls._cached_actors_map is not None:
            return cls._cached_actors_map, cls._cached_actors_ver

        target_db_path, file_ver = cls.find_latest_jav_actors_db()
        if not target_db_path or not os.path.exists(target_db_path):
            return None, "0"

        actors_map = {}
        try:
            conn = sqlite3.connect(target_db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM actors WHERE site = 'avdbs'")
            raw_rows = c.fetchall()
            conn.close()

            merged_entities = cls._cluster_and_merge_actor_rows(raw_rows)

            for entity_item in merged_entities:
                m_row = entity_item['master_row']
                entity_dict = dict(m_row)
                entity_dict['alt_actor_indices'] = entity_item['alt_actor_indices']
                entity_dict['merged_sub_actors'] = entity_item['merged_sub_actors']
                entity_dict['merged_aliases'] = entity_item['aliases']
                entity_dict['merged_site_photos'] = entity_item['site_img_urls']

                # 마스터 ID 및 모든 서브 ID를 동일한 엔티티 딕셔너리로 직결 등록
                for actor_id in entity_item['alt_actor_indices']:
                    actors_map[actor_id] = entity_dict

                name_cn = str(m_row.get("inner_name_cn") or m_row.get("name_org") or "").strip()
                if name_cn and name_cn not in actors_map:
                    actors_map[name_cn] = entity_dict

                name_kr = str(m_row.get("inner_name_kr") or m_row.get("name_ko") or "").strip()
                if name_kr and name_kr not in actors_map:
                    actors_map[name_kr] = entity_dict

            cls._cached_actors_map = actors_map
            cls._cached_actors_ver = file_ver
            logger.debug(f"[MetaDB ActorCache] 중복 통합 인메모리 캐시 구축 완료: 원본 {len(raw_rows):,}행 ➔ 고유 인물 {len(merged_entities):,}명 (버전: {file_ver})")
            return cls._cached_actors_map, file_ver
        except Exception as e:
            logger.error(f"[MetaDB] 배포 DB 메모리 캐시 로드 실패: {e}")
            return None, "0"


    @classmethod
    def format_actor_thumb_url(cls, thumb_raw, domain='JAV', **kwargs):
        if not thumb_raw or not isinstance(thumb_raw, str):
            return ""
        thumb_clean = thumb_raw.strip()
        if not thumb_clean or thumb_clean.lower() in ['null', 'none', '403', '404', 'deprecated', 'unavailable']:
            return ""

        if thumb_clean.startswith(('http://', 'https://')):
            return thumb_clean

        server_url = (
            P.ModelSetting.get("jav_censored_image_server_url") or 
            f"{F.SystemModelSetting.get('ddns')}/images"
        ).rstrip('/')

        domain_upper = str(domain or 'JAV').upper()
        if domain_upper == 'WESTERN':
            actor_folder = (
                P.ModelSetting.get("western_image_server_actor_path") or 
                P.ModelSetting.get("jav_censored_image_server_actor_path") or 
                "/western/actors"
            ).strip('/\\')
        else:
            actor_folder = (
                P.ModelSetting.get("jav_censored_image_server_actor_path") or 
                "/jav/actors"
            ).strip('/\\')

        clean_rel = thumb_clean.replace('\\', '/').lstrip('/')
        if actor_folder and clean_rel.startswith(actor_folder):
            return f"{server_url}/{clean_rel}"
        if actor_folder:
            return f"{server_url}/{actor_folder}/{clean_rel}"
        return f"{server_url}/{clean_rel}"

    @classmethod
    def resolve_actor_thumb_url(cls, media_src, order_str=None, img_prefix=None, domain=None, **kwargs):
        if not media_src:
            return ""

        m_dict = dict(media_src) if (hasattr(media_src, 'keys') and not isinstance(media_src, dict)) else (media_src if isinstance(media_src, dict) else {})
        target_domain = str(domain or m_dict.get('domain') or 'JAV').upper()

        # 사용자가 직접 크롭/저장한 _user 이미지가 로컬에 존재하면 설정 순서와 상관없이 무조건 최우선 대표로 채택
        local_path_val = str(m_dict.get('local_img_path') or '').strip()
        if local_path_val and '_user.' in local_path_val.lower() and local_path_val.lower() not in ['null', 'none', '403', '404', 'deprecated', 'unavailable']:
            return cls.format_actor_thumb_url(local_path_val, domain=target_domain)

        # 도메인별 배우 이미지 소스 우선순위 결정 (서양은 GDS 파일 ID를 완전히 배제하고 독립 운영)
        if not order_str:
            if target_domain == 'WESTERN':
                custom_west_order = P.ModelSetting.get("western_actor_img_order")
                if custom_west_order:
                    order_str = custom_west_order
                else:
                    west_mode = P.ModelSetting.get("western_actor_image_mode") or "site"
                    order_str = "local_img_path, site_img_url" if west_mode == 'image_server' else "site_img_url, local_img_path"
            elif target_domain == 'GENERAL':
                order_str = "site_img_url"
            else:
                jav_mode = P.ModelSetting.get("jav_censored_actor_image_mode") or "gds"
                if jav_mode == 'image_server':
                    order_str = "local_img_path, google_fileid, site_img_url"
                elif jav_mode == 'site':
                    order_str = "site_img_url, google_fileid, local_img_path"
                else:
                    order_str = "google_fileid, local_img_path, site_img_url"

        parsed_order = [x.strip().lower() for x in re.split(r'[\s,]+', str(order_str)) if x.strip()]

        for opt in parsed_order:
            if opt == "google_fileid":
                val = str(m_dict.get('google_fileid') or '').strip()
                if val and val.lower() not in ['null', 'none', '403', '404', 'deprecated', 'unavailable']:
                    return f"https://drive.google.com/thumbnail?id={val}"

            elif opt == "local_img_path":
                val = str(m_dict.get('local_img_path') or '').strip()
                if val and val.lower() not in ['null', 'none', '403', '404', 'deprecated', 'unavailable']:
                    return cls.format_actor_thumb_url(val, domain=target_domain)

            elif opt == "site_img_url":
                val = str(m_dict.get('site_img_url') or '').strip()
                if val and val.startswith('http') and val.lower() not in ['403', '404', 'deprecated', 'unavailable']:
                    return val

        fallback_site = str(m_dict.get('site_img_url') or '').strip()
        if fallback_site.startswith('http'):
            return fallback_site
        return ""

    @classmethod
    def resolve_person_active_thumb(cls, p):
        if not p: return ""
        media_src = p.media_src if isinstance(p.media_src, dict) else (p.extra_info.get('media_src') if isinstance(p.extra_info, dict) else {})
        return cls.resolve_actor_thumb_url(media_src, domain=p.domain)

    @classmethod
    def _upsert_person_from_actor(
        cls,
        person_session,
        actor_data,
        person_domain,
        source_category=None,
        source_code=None,
    ):
        if not isinstance(actor_data, dict):
            return None

        raw_name_org = (actor_data.get('name_org') or '').strip()
        raw_name_ko = (actor_data.get('name_ko') or '').strip()
        raw_name_en = (actor_data.get('name_en') or '').strip()
        a_thumb = (actor_data.get('thumb') or '').strip()
        a_idx_val = str(actor_data.get('actor_idx') or actor_data.get('person_idx') or '').strip()
        
        if not raw_name_org and not raw_name_ko:
            return None

        if a_idx_val and person_domain == 'JAV':
            m_num = re.search(r'(\d+)', a_idx_val)
            if m_num:
                a_idx_val = f"PA{m_num.group(1)}"

        matched_db_row = None
        if person_domain == 'JAV':
            actors_map, _ = cls.get_cached_actors_map()
            if actors_map:
                if a_idx_val and a_idx_val in actors_map:
                    matched_db_row = actors_map[a_idx_val]
                elif raw_name_org and raw_name_org in actors_map:
                    matched_db_row = actors_map[raw_name_org]

            if matched_db_row:
                a_idx_val = str(matched_db_row.get('actor_id') or '').strip()
                raw_name_org = str(matched_db_row.get('name_org') or matched_db_row.get('inner_name_cn') or raw_name_org).strip()
                if not raw_name_ko:
                    raw_name_ko = str(matched_db_row.get('name_ko') or matched_db_row.get('inner_name_kr') or '').strip()
                if not raw_name_en:
                    raw_name_en = str(matched_db_row.get('name_en') or matched_db_row.get('inner_name_en') or '').strip()

            # JAV 도메인은 한국어 표기명이 없는 경우 메타 인물 DB 등록을 스킵
            if not raw_name_ko:
                return None
        else:
            # Western 등 타 도메인은 원문명/영문명만으로 등록 허용
            if not raw_name_org and not raw_name_ko and not raw_name_en:
                return None

        raw_aliases = actor_data.get('aliases') or actor_data.get('other_names') or actor_data.get('onm') or []
        if isinstance(raw_aliases, str):
            alias_list = [x.strip() for x in re.split(r'[,/]', raw_aliases) if x.strip()]
        elif isinstance(raw_aliases, list):
            alias_list = [str(x).strip() for x in raw_aliases if str(x).strip()]
        else:
            alias_list = []

        if raw_name_en and raw_name_en not in alias_list:
            alias_list.append(raw_name_en)

        p_rec = None
        if a_idx_val:
            p_rec = person_session.query(MetaPerson).filter_by(domain=person_domain, person_idx=a_idx_val).first()
        elif person_domain != 'JAV':
            target_match_name = raw_name_org
            p_rec = person_session.query(MetaPerson).filter(
                MetaPerson.domain == person_domain,
                or_(
                    MetaPerson.name_org == target_match_name,
                    MetaPerson.name_en == target_match_name
                )
            ).first()

        a_media = actor_data.get('media_src') if isinstance(actor_data.get('media_src'), dict) else {}
        a_extra = actor_data.get('extra_info') if isinstance(actor_data.get('extra_info'), dict) else {}

        a_local = str(actor_data.get('local_img_path') or a_media.get('local_img_path') or a_extra.get('local_img_path') or '').strip()
        if a_local:
            parts = [p for p in a_local.replace('\\', '/').strip('/').split('/') if p]
            a_local = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else a_local

        a_site = str(actor_data.get('site_img_url') or a_media.get('site_img_url') or a_extra.get('site_img_url') or '').strip()
        a_google = str(actor_data.get('google_fileid') or a_media.get('google_fileid') or a_extra.get('google_fileid') or '').strip()

        raw_photos = list(actor_data.get('site_img_urls') or a_extra.get('site_img_urls') or a_media.get('site_img_urls') or [])
        a_site_photos = [u.strip() for u in raw_photos if u and isinstance(u, str) and u.strip().startswith('http')]

        if a_site and a_site.startswith('http') and a_site not in a_site_photos:
            a_site_photos.insert(0, a_site)

        media_src_data = {
            'local_img_path': a_local,
            'google_fileid': a_google,
            'site_img_url': a_site,
            'site_img_urls': a_site_photos
        }

        def clean_height(val):
            if val:
                m = re.search(r'\d+', str(val))
                if m and int(m.group()) > 0:
                    return int(m.group())
            return None

        info_url_candidate = str(a_extra.get('info_url') or '').strip()
        if not info_url_candidate and a_idx_val:
            if a_idx_val.startswith('PS'):
                info_url_candidate = f"https://stashdb.org/performers/{a_idx_val[2:]}"
            elif a_idx_val.startswith('PP') or a_idx_val.startswith('PT'):
                info_url_candidate = f"https://theporndb.net/performers/{a_idx_val[2:]}"
            elif a_idx_val.startswith('PA'):
                raw_num = re.sub(r'\D', '', a_idx_val)
                if raw_num: info_url_candidate = f"https://www.avdbs.com/menu/actor.php?actor_idx={raw_num}"

        extra_data = {
            'birth': str(a_extra.get('birth') or '').strip(),
            'height': clean_height(a_extra.get('height')),
            'body_size': str(a_extra.get('body_size') or '').strip(),
            'bra_size': str(a_extra.get('bra_size') or '').strip(),
            'debut': str(a_extra.get('debut') or '').strip(),
            'info_url': info_url_candidate,
            'agency': str(a_extra.get('agency') or '').strip(),
            'blood': str(a_extra.get('blood') or '').strip(),
            'hobby': str(a_extra.get('hobby') or '').strip(),
            'specialty': str(a_extra.get('specialty') or '').strip(),
            'country': str(a_extra.get('country') or '').strip(),
            'gender': str(a_extra.get('gender') or '').strip(),
            'source_origin': 'western' if person_domain == 'WESTERN' else 'jav_actors'
        }

        if matched_db_row:
            col_keys = matched_db_row.keys()
            db_local = str(matched_db_row.get('local_img_path') or '').strip()
            if db_local:
                parts = [p for p in db_local.replace('\\', '/').strip('/').split('/') if p]
                db_local = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else db_local

            db_site = str(matched_db_row.get('site_img_url') or '').strip()
            db_google = str(matched_db_row.get('google_fileid') or '').strip()

            if db_local: media_src_data['local_img_path'] = db_local
            if db_site: media_src_data['site_img_url'] = db_site
            if db_google: media_src_data['google_fileid'] = db_google

            extra_data.update({
                'birth': str(matched_db_row.get('birth') or '').strip(),
                'height': clean_height(matched_db_row.get('height')) or clean_height(a_extra.get('height')),
                'body_size': str(matched_db_row.get('body_size') or '').strip(),
                'bra_size': str(matched_db_row.get('bra_size') or '').strip(),
                'debut': str(matched_db_row.get('debut') or '').strip(),
                'info_url': str(matched_db_row.get('info_url') or '').strip() or info_url_candidate,
                'agency': str(matched_db_row.get('agency') or '').strip(),
                'blood': str(matched_db_row.get('blood') or '').strip(),
                'hobby': str(matched_db_row.get('hobby') or '').strip(),
                'specialty': str(matched_db_row.get('specialty') or '').strip(),
            })

        if not p_rec:
            p_rec = MetaPerson(
                domain=person_domain,
                name_org=raw_name_org or raw_name_ko,
                name_ko=raw_name_ko,
                name_en=raw_name_en,
                other_names=", ".join(alias_list) if alias_list else "",
                aliases=alias_list,
                person_idx=a_idx_val,
                person_type="actor",
                media_src=media_src_data,
                works={},
                extra_info=extra_data,
            )
            person_session.add(p_rec)
            person_session.flush()
        else:
            if raw_name_org: p_rec.name_org = raw_name_org
            if raw_name_ko: p_rec.name_ko = raw_name_ko
            if raw_name_en and not p_rec.name_en: p_rec.name_en = raw_name_en

            merged_media = copy.deepcopy(p_rec.media_src or {})
            if media_src_data['local_img_path']: merged_media['local_img_path'] = media_src_data['local_img_path']
            if media_src_data['site_img_url']: merged_media['site_img_url'] = media_src_data['site_img_url']
            if media_src_data['google_fileid']: merged_media['google_fileid'] = media_src_data['google_fileid']
            p_rec.media_src = merged_media

            merged_extra = copy.deepcopy(p_rec.extra_info or {})
            for k, v in extra_data.items():
                if v and not merged_extra.get(k):
                    merged_extra[k] = v
            p_rec.extra_info = merged_extra

            if a_idx_val and not p_rec.person_idx:
                p_rec.person_idx = a_idx_val
            if alias_list:
                existing_aliases = set(p_rec.aliases or [])
                existing_aliases.update(alias_list)
                p_rec.aliases = list(existing_aliases)
                p_rec.other_names = ", ".join(p_rec.aliases)

        return p_rec

    @classmethod
    def save_metadata(cls, category, entity_dict, target_session=None, target_person_session=None):
        if not P.ModelSetting.get_bool("meta_db_use"):
            return False

        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess:
            logger.error(f"[MetaDB] save_metadata: 세션 획득 실패. category='{category}'")
            return False

        s = target_session or sess
        code = entity_dict.get('code') if isinstance(entity_dict, dict) else None
        if not code:
            logger.warning("[MetaDB] save_metadata: 'code' 필드가 누락되었습니다.")
            return False

        person_sess = target_person_session
        is_internal_person_sess = False
        if person_sess is None and std_cat in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN']:
            person_sess, _, _ = cls.get_session_and_domain('PERSON')
            is_internal_person_sess = True

        try:
            nested_item = s.begin_nested()
            nested_person = person_sess.begin_nested() if (person_sess and person_sess is not s) else None

            originaltitle = str(entity_dict.get('originaltitle') or code)[:255]
            sorttitle = str(entity_dict.get('sorttitle') or entity_dict.get('title') or originaltitle)[:500]
            ui_code = str(entity_dict.get('ui_code') or originaltitle)[:255]
            site = str(entity_dict.get('site') or 'unknown')[:50]
            title = str(entity_dict.get('title') or originaltitle)[:500]
            tagline = entity_dict.get('tagline') or ''
            plot = entity_dict.get('plot') or ''
            director = str(entity_dict.get('director') or '')[:255]
            studio = str(entity_dict.get('studio') or '')[:255]

            orig_dict = entity_dict.get('original') or {}
            if not isinstance(orig_dict, dict):
                orig_dict = {}

            series = str(orig_dict.get('series') or entity_dict.get('series') or '')[:255]
            premiered = entity_dict.get('premiered') or ''

            try: year = int(entity_dict.get('year') or 1900)
            except: year = 1900

            try: runtime = int(entity_dict.get('runtime') or 0)
            except: runtime = 0

            rating, rating_votes = 0.0, 0
            ratings_list = entity_dict.get('ratings') or []
            if isinstance(ratings_list, list) and len(ratings_list) > 0:
                r_obj = ratings_list[0]
                if isinstance(r_obj, dict):
                    rating = float(r_obj.get('value') or 0.0)
                    rating_votes = int(r_obj.get('votes') or 0)
                elif hasattr(r_obj, 'value'):
                    rating = float(getattr(r_obj, 'value', 0.0) or 0.0)
                    rating_votes = int(getattr(r_obj, 'votes', 0) or 0)

            mpaa = entity_dict.get('mpaa') or ''
            content_type = entity_dict.get('content_type') or ''

            # 모든 입력 리스트 변수 안전 선언
            thumbs_list = entity_dict.get('thumb') or []
            fanarts_list = entity_dict.get('fanart') or []
            extras_list = entity_dict.get('extras') or []
            actors_list = entity_dict.get('actor') or []
            genres_list = entity_dict.get('genre') or []
            orig_genres_list = orig_dict.get('genre') or []

            # 기존 레코드 조회 및 신규 생성
            item = s.query(MetaItem).filter_by(code=code).first()
            if not item:
                item = MetaItem(
                    domain=domain, category=std_cat, code=code, ui_code=ui_code,
                    originaltitle=originaltitle, sorttitle=sorttitle, site=site, title=title
                )
                s.add(item)
                s.flush()

            # 번역 전 순수 원문 텍스트/미디어 데이터 조립
            orig_dict = entity_dict.get('original') if isinstance(entity_dict.get('original'), dict) else {}
            orig_thumb = orig_dict.get('thumb') if isinstance(orig_dict.get('thumb'), dict) else {}
            orig_extras = orig_dict.get('extras') if isinstance(orig_dict.get('extras'), list) else []

            raw_site_ps = orig_thumb.get('ps_url') or entity_dict.get('site_img_ps_url') or ''
            raw_site_p = orig_thumb.get('poster') or ''
            image_server_setting = (
                'western_image_server_url' if std_cat == 'WESTERN'
                else 'jav_uncensored_image_server_url' if std_cat == 'JAV_UNCEN'
                else 'jav_censored_image_server_url'
            )
            configured_image_server_url = (
                P.ModelSetting.get(image_server_setting) or ''
            ).rstrip('/')
            if isinstance(raw_site_p, str) and (
                '/metadata/normal/' in raw_site_p or
                '_p_user.' in raw_site_p or
                (configured_image_server_url and raw_site_p.startswith(configured_image_server_url))
            ):
                raw_site_p = ''
            raw_site_pl = orig_thumb.get('landscape') or ''
            raw_site_arts = list(orig_dict.get('fanart') or [])

            raw_trailer_url = ''
            for ex in orig_extras:
                if isinstance(ex, dict) and ex.get('content_url'):
                    raw_trailer_url = ex['content_url']
                    break

            if not raw_trailer_url and isinstance(orig_dict.get('trailer'), str) and orig_dict['trailer'].strip():
                raw_trailer_url = orig_dict['trailer'].strip()

            if not raw_trailer_url:
                for ex_item in extras_list:
                    if isinstance(ex_item, dict) and ex_item.get('content_url'):
                        c_url = ex_item['content_url']
                        if 'url=' in c_url:
                            try:
                                from urllib.parse import parse_qs, urlparse
                                qs = parse_qs(urlparse(c_url).query)
                                if 'url' in qs: raw_trailer_url = qs['url'][0]
                            except Exception: pass
                        elif c_url.startswith('http') and 'metadata/normal' not in c_url:
                            raw_trailer_url = c_url

            clean_original_extras = []
            if raw_trailer_url:
                clean_original_extras.append({
                    'content_url': raw_trailer_url,
                    'content_type': 'trailer'
                })

            clean_original = {
                'title': str(orig_dict.get('title') or entity_dict.get('originaltitle') or originaltitle or '').strip(),
                'tagline': str(orig_dict.get('tagline') or entity_dict.get('tagline') or '').strip(),
                'plot': str(orig_dict.get('plot') or entity_dict.get('plot') or '').strip(),
                'studio': str(orig_dict.get('studio') or entity_dict.get('studio') or studio or '').strip(),
                'series': str(orig_dict.get('series') or entity_dict.get('series') or series or '').strip(),
                'director': str(orig_dict.get('director') or entity_dict.get('director') or director or '').strip(),
                'genre': list(orig_dict.get('genre') or entity_dict.get('genre') or orig_genres_list or []),
                'thumb': {
                    'ps_url': raw_site_ps,
                    'poster': raw_site_p,
                    'landscape': raw_site_pl
                },
                'fanart': raw_site_arts,
                'extras': clean_original_extras
            }

            resolved_poster_url = entity_dict.get('poster_url') or ''
            if isinstance(resolved_poster_url, str) and '/metadata/normal/' in resolved_poster_url:
                resolved_poster_url = ''
            if not resolved_poster_url:
                for th in thumbs_list:
                    if isinstance(th, dict) and th.get('aspect') == 'poster' and th.get('value'):
                        resolved_poster_url = th['value']
                        break
            if not resolved_poster_url and thumbs_list:
                for th in thumbs_list:
                    if isinstance(th, dict) and th.get('value'):
                        resolved_poster_url = th['value']
                        break
            if not resolved_poster_url:
                resolved_poster_url = raw_site_p or raw_site_pl or ''

            merged_extra_info = copy.deepcopy(item.extra_info or {})
            input_extra_info = copy.deepcopy(entity_dict.get('extra_info') or {})
            if isinstance(input_extra_info, dict):
                input_extra_info.pop('actor_cache', None)
                for k_extra, v_extra in input_extra_info.items():
                    if v_extra is not None: merged_extra_info[k_extra] = v_extra

            if 'country' in entity_dict:
                merged_extra_info['country'] = entity_dict.get('country') or []

            if entity_dict.get('info_url'):
                merged_extra_info['info_url'] = str(entity_dict['info_url']).strip()

            item.domain = domain
            item.category = std_cat
            item.ui_code = ui_code
            item.originaltitle = originaltitle
            item.sorttitle = sorttitle
            item.site = site
            item.title = title
            item.tagline = tagline
            item.plot = plot
            item.director = director
            item.studio = studio
            item.series = series
            item.premiered = premiered
            item.year = year
            item.runtime = runtime
            item.rating = rating
            item.rating_votes = rating_votes
            item.mpaa = mpaa
            item.content_type = content_type
            item.poster_url = resolved_poster_url
            item.original = clean_original
            item.extra_info = merged_extra_info
            item.spec_data = copy.deepcopy(entity_dict.get('spec_data') or {})
            item.updated_time = datetime.now()

            item.media_files.clear()
            item.tag_maps.clear()

            # 유저 커스텀 파일 여부만 캐시 기록
            for thumb in thumbs_list:
                if isinstance(thumb, dict):
                    t_val = str(thumb.get('value') or '')
                    if '_p_user' in t_val:
                        item.media_files.append(MetaMedia(media_type="poster", url="_p_user", is_user=True, sort_order=0))
                    elif '_pl_user' in t_val:
                        item.media_files.append(MetaMedia(media_type="landscape", url="_pl_user", is_user=True, sort_order=1))

            # 관계 테이블 동기화 및 양방향 연결 저장
            item.person_maps.clear()

            # 배우 인물 DB 연동 및 관계 매핑
            person_dom = cls._person_domain_from_item_category(std_cat)
            if entity_dict.get('person_domain'):
                person_dom = entity_dict['person_domain']

            stored_actors = []

            for actor_item in actors_list:
                if not actor_item: continue

                if isinstance(actor_item, dict):
                    actor_data = copy.deepcopy(actor_item)
                elif hasattr(actor_item, 'name') or hasattr(actor_item, 'name_org'):
                    actor_data = {
                        'name_org': getattr(actor_item, 'name_org', '') or getattr(actor_item, 'name', '') or '',
                        'name_ko': getattr(actor_item, 'name_ko', '') or '',
                        'name_en': getattr(actor_item, 'name_en', '') or '',
                        'thumb': getattr(actor_item, 'thumb', '') or '',
                        'actor_idx': str(getattr(actor_item, 'actor_idx', '') or getattr(actor_item, 'person_idx', '') or '').strip(),
                        'role': getattr(actor_item, 'role', '출연') or '출연',
                        'gender': getattr(actor_item, 'gender', '') or '',
                        'local_img_path': getattr(actor_item, 'local_img_path', '') or '',
                        'site_img_url': getattr(actor_item, 'site_img_url', '') or '',
                        'extra_info': getattr(actor_item, 'extra_info', {}) if hasattr(actor_item, 'extra_info') else {}
                    }
                elif isinstance(actor_item, str):
                    actor_data = {'name_org': actor_item.strip(), 'name_ko': '', 'name_en': '', 'role': '출연'}
                else:
                    continue

                a_name_org = (actor_data.get('name_org') or '').strip()
                a_name_ko = (actor_data.get('name_ko') or '').strip()
                a_idx = str(actor_data.get('actor_idx') or actor_data.get('person_idx') or '').strip()
                a_role = actor_data.get('role') or '출연'

                if not a_name_org and not a_name_ko:
                    continue

                central_person = None
                if person_sess:
                    central_person = cls._upsert_person_from_actor(
                        person_session=person_sess,
                        actor_data=actor_data,
                        person_domain=person_dom,
                        source_category=std_cat,
                        source_code=code,
                    )
                    if central_person:
                        # RDBMS 정규 관계 테이블(MetaItemPersonMap)에 물리적 매핑 레코드 생성
                        item.person_maps.append(MetaItemPersonMap(
                            person_id=central_person.id,
                            role_type="actor",
                            role_name=a_role,
                            sort_order=len(stored_actors)
                        ))

                        works_dict = copy.deepcopy(central_person.works or {})
                        if not isinstance(works_dict, dict):
                            works_dict = {}
                        cat_works = works_dict.get(std_cat) or []

                        work_entry = {
                            'code': code,
                            'ui_code': ui_code or originaltitle or code,
                            'title': title or originaltitle or ''
                        }

                        # 기존 문자열 코드 또는 동일 코드 존재 여부 확인 후 최신 객체로 갱신
                        work_idx = -1
                        for idx, w in enumerate(cat_works):
                            w_c = w.get('code') if isinstance(w, dict) else str(w)
                            if w_c == code:
                                work_idx = idx
                                break

                        if work_idx != -1:
                            cat_works[work_idx] = work_entry
                        else:
                            cat_works.append(work_entry)

                        works_dict[std_cat] = cat_works
                        central_person.works = works_dict

                final_idx = (central_person.person_idx if central_person else a_idx) or ''
                final_name_org = (central_person.name_org if central_person else (actor_data.get('name_org') or ''))
                final_name_ko = (central_person.name_ko if central_person else (actor_data.get('name_ko') or ''))
                final_name_en = (central_person.name_en if central_person else (actor_data.get('name_en') or ''))
                final_gender = (central_person.extra_info.get('gender') if (central_person and central_person.extra_info) else None) or actor_data.get('gender') or ''

                stored_actors.append({
                    'actor_idx': final_idx,
                    'name_org': final_name_org,
                    'name_ko': final_name_ko,
                    'name_en': final_name_en,
                    'gender': final_gender,
                    'role': a_role
                })

            merged_extra_info['_actors'] = stored_actors
            item.extra_info = merged_extra_info

            # 장르 및 태그 매핑
            for g_idx, g_name in enumerate(genres_list):
                if not g_name or not isinstance(g_name, str): continue
                g_orig = orig_genres_list[g_idx] if (g_idx < len(orig_genres_list) and isinstance(orig_genres_list[g_idx], str)) else g_name

                tag_rec = s.query(MetaTag).filter_by(name=g_name).first()
                if not tag_rec:
                    try:
                        tag_rec = MetaTag(domain=domain, name=g_name, name_org=g_orig, tag_type="genre")
                        s.add(tag_rec)
                        s.flush()
                    except Exception:
                        tag_rec = s.query(MetaTag).filter_by(name=g_name).first()

                if tag_rec:
                    item.tag_maps.append(MetaItemTagMap(tag_id=tag_rec.id))

            # 비디오 지문(Fingerprints) 3-Way 정밀 동기화
            # - 'site': 원격 사이트 최신 상태 반영 (사이트에서 삭제된 오류 지문은 로컬에서도 정리)
            # - 'user': 사용자가 로컬 영상에서 추가한 고유 지문은 사이트 갱신 시에도 영구 보존
            # - '승격': 유저 지문이 추후 사이트 정식 지문으로 등록되면 'site'로 자동 승격
            incoming_site_fps = {}
            incoming_user_fps = {}

            # 1. 사이트 원본 공식 지문 수집 (StashDB, TPDB 원격 응답)
            if isinstance(entity_dict.get('original'), dict):
                for fp in (entity_dict['original'].get('fingerprints') or []):
                    if not isinstance(fp, dict): continue
                    algo = str(fp.get('algorithm') or 'OSHASH').strip().upper()
                    h_val = str(fp.get('hash') or fp.get('hash_value') or '').strip().lower()
                    if algo and h_val:
                        incoming_site_fps[f"{algo}_{h_val}"] = (algo, h_val)

            # 2. 유저 추가 지문 및 extra_info 수집
            all_extra_fps = []
            if isinstance(entity_dict.get('extra_info'), dict):
                all_extra_fps.extend(entity_dict['extra_info'].get('fingerprints') or [])
            if isinstance(entity_dict.get('fingerprints'), list):
                all_extra_fps.extend(entity_dict['fingerprints'])

            for fp in all_extra_fps:
                if not isinstance(fp, dict): continue
                algo = str(fp.get('algorithm') or 'OSHASH').strip().upper()
                h_val = str(fp.get('hash') or fp.get('hash_value') or '').strip().lower()
                src = str(fp.get('source') or 'user').strip().lower()
                if not algo or not h_val: continue

                key = f"{algo}_{h_val}"
                if src == 'site':
                    incoming_site_fps[key] = (algo, h_val)
                else:
                    incoming_user_fps[key] = (algo, h_val)

            # 3. 기존 DB에 저장되어 있던 유저 지문 보존 (사이트 갱신으로 인한 유실 방지)
            for existing_fp in item.fingerprints:
                if existing_fp.source == 'user':
                    key = f"{existing_fp.algorithm}_{existing_fp.hash_value}"
                    if key not in incoming_site_fps:
                        incoming_user_fps[key] = (existing_fp.algorithm, existing_fp.hash_value)

            # 4. 최종 동기화 목록 조립 (사이트 지문 최우선, 중복 유저 지문은 사이트로 승격)
            final_fps_to_save = []
            for k, (algo, h_val) in incoming_site_fps.items():
                final_fps_to_save.append({'algorithm': algo, 'hash_value': h_val, 'source': 'site'})

            for k, (algo, h_val) in incoming_user_fps.items():
                if k not in incoming_site_fps:
                    final_fps_to_save.append({'algorithm': algo, 'hash_value': h_val, 'source': 'user'})

            # 5. 테이블 및 extra_info 일괄 반영
            if final_fps_to_save or item.fingerprints:
                item.fingerprints.clear()
                sync_json_list = []
                for fp_data in final_fps_to_save:
                    item.fingerprints.append(MetaFingerprint(
                        category=std_cat,
                        code=code,
                        algorithm=fp_data['algorithm'],
                        hash_value=fp_data['hash_value'],
                        source=fp_data['source']
                    ))
                    sync_json_list.append({
                        'algorithm': fp_data['algorithm'],
                        'hash': fp_data['hash_value'],
                        'source': fp_data['source']
                    })

                merged_extra_info['fingerprints'] = sync_json_list
                item.extra_info = merged_extra_info

            nested_item.commit()
            if nested_person:
                nested_person.commit()

            if target_session is None:
                s.commit()
                if person_sess and is_internal_person_sess and person_sess is not s:
                    person_sess.commit()
            return True

        except Exception as e:
            logger.error(f"[MetaDB] save_metadata 실패 ({code}): {e}")
            logger.error(traceback.format_exc())
            if target_session is None:
                s.rollback()
                if person_sess and is_internal_person_sess and person_sess is not s:
                    person_sess.rollback()
            return False
        finally:
            if target_session is None:
                s.remove()
            if person_sess and is_internal_person_sess and person_sess is not s:
                person_sess.remove()


    @classmethod
    def sync_jav_actors_db(cls):
        """배포된 jav_actors_YYYYMMDD.db 파일을 읽어 인물(PERSON) DB로 동기화하고 중복 인물을 안전하게 클러스터링 병합합니다."""
        cls.ensure_db_ready()
        target_db_path, file_ver = cls.find_latest_jav_actors_db()
        if not target_db_path:
            return False, "배우 배포 DB(jav_actors_*.db) 파일을 찾을 수 없습니다."

        target_engine = cls._engines.get('postgres') if cls._is_postgres else cls._engines.get('person')
        if not target_engine:
            return False, "인물 DB 엔진 획득 실패"

        # 대량 커밋 시 N+1 Lazy Fetch 방지를 위해 expire_on_commit=False 전용 세션 생성
        sync_session_factory = sessionmaker(bind=target_engine, autocommit=False, autoflush=False, expire_on_commit=False)
        sess = sync_session_factory()

        t_start = time.time()
        conn = None
        raw_rows = []

        try:
            # 배포 DB 파일 읽기용 SQLite 연결 및 데이터 추출
            try:
                conn = sqlite3.connect(target_db_path)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                c.execute("SELECT * FROM actors WHERE site = 'avdbs'")
                raw_rows = c.fetchall()
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

            if not raw_rows:
                return False, "배우 데이터가 없습니다."

            # 원본 배포 DB 행들을 안전 룰 기반으로 동일인 클러스터링
            merged_clusters = cls._cluster_and_merge_actor_rows(raw_rows)

            existing_persons = sess.query(MetaPerson).filter_by(domain="JAV").all()
            person_idx_map = {}
            for p in existing_persons:
                if p.person_idx:
                    person_idx_map[p.person_idx] = p
                # 과거에 등록된 서브 ID도 색인 맵에 등록
                alt_list = (p.extra_info or {}).get('alt_actor_indices', [])
                for alt_id in alt_list:
                    if alt_id not in person_idx_map:
                        person_idx_map[alt_id] = p

            # 작품 출연작(Filmography) 매핑 수집 (거대 조인 방지를 위한 경량 컬럼 프로젝션 쿼리)
            existing_works_map = {}
            for cat in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN']:
                item_engine = cls._engines.get('postgres') if cls._is_postgres else cls._engines.get(DOMAIN_MAP[cat][0])
                if item_engine:
                    work_query_session = sessionmaker(bind=item_engine, autocommit=False, autoflush=False, expire_on_commit=False)()
                    try:
                        light_items = work_query_session.query(
                            MetaItem.code,
                            MetaItem.ui_code,
                            MetaItem.title,
                            MetaItem.originaltitle,
                            MetaItem.extra_info
                        ).filter(MetaItem.category == cat).all()

                        for m_code, m_ui_code, m_title, m_orig_title, m_extra in light_items:
                            extra_d = m_extra if isinstance(m_extra, dict) else {}
                            actor_list = extra_d.get('actor_cache') or extra_d.get('_actors') or []
                            for act in actor_list:
                                act_id = act.get('actor_idx') if isinstance(act, dict) else None
                                if act_id:
                                    if act_id not in existing_works_map:
                                        existing_works_map[act_id] = {}
                                    if cat not in existing_works_map[act_id]:
                                        existing_works_map[act_id][cat] = []

                                    work_entry = {
                                        'code': m_code,
                                        'ui_code': m_ui_code or m_code,
                                        'title': m_title or m_orig_title or ''
                                    }
                                    if not any((w.get('code') if isinstance(w, dict) else str(w)) == m_code for w in existing_works_map[act_id][cat]):
                                        existing_works_map[act_id][cat].append(work_entry)
                    finally:
                        work_query_session.close()

            inserted, updated, merged_duplicates_count = 0, 0, 0
            batch_size = 500

            for idx, cluster in enumerate(merged_clusters, 1):
                try:
                    best_r = cluster['master_row']
                    master_actor_id = str(best_r.get("actor_id") or "").strip()
                    alt_indices = cluster['alt_actor_indices']
                    merged_sub_actors = cluster['merged_sub_actors']
                    merged_aliases = cluster['aliases']
                    site_photos = cluster['site_img_urls']

                    name_org = str(best_r.get("name_org") or best_r.get("inner_name_cn") or "").strip()
                    name_ko = str(best_r.get("name_ko") or best_r.get("inner_name_kr") or "").strip()
                    name_en = str(best_r.get("name_en") or best_r.get("inner_name_en") or "").strip()

                    # 클러스터 내 서브 행들 중 한국어명이 존재하는지 재확인
                    if not name_ko:
                        for sub_item in merged_sub_actors:
                            sub_ko_val = str(sub_item.get('name_ko') or '').strip()
                            if sub_ko_val:
                                name_ko = sub_ko_val
                                break

                    # JAV 도메인 정책: 한국어 표기명이 비어있는 인물은 DB 저장 제외
                    if not name_ko:
                        continue

                    local_path_val = str(best_r.get('local_img_path') or '').strip()
                    if local_path_val:
                        parts = [p for p in local_path_val.replace('\\', '/').strip('/').split('/') if p]
                        local_path_val = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else local_path_val

                    site_img_url_val = str(best_r.get('site_img_url') or '').strip()
                    google_fileid_val = str(best_r.get('google_fileid') or '').strip()

                    # 모든 서브 ID에 등록되어 있던 작품 목록을 하나의 works로 통합
                    combined_works = {}
                    for any_id in alt_indices:
                        if any_id in existing_works_map:
                            for c_key, w_list in existing_works_map[any_id].items():
                                if c_key not in combined_works:
                                    combined_works[c_key] = []
                                for w_item in w_list:
                                    w_code = w_item.get('code') if isinstance(w_item, dict) else str(w_item)
                                    if not any((x.get('code') if isinstance(x, dict) else str(x)) == w_code for x in combined_works[c_key]):
                                        combined_works[c_key].append(w_item)

                    h_val = None
                    raw_height = best_r.get('profile_height') or best_r.get('height')
                    if raw_height:
                        m_h = re.search(r'\d+', str(raw_height))
                        if m_h and int(m_h.group()) > 0:
                            h_val = int(m_h.group())

                    media_src_data = {
                        'local_img_path': local_path_val,
                        'google_fileid': google_fileid_val,
                        'site_img_url': site_img_url_val,
                        'site_img_urls': site_photos
                    }

                    # 기존 레코드 검색: 대표 ID 또는 서브 ID 중 하나라도 일치하는지 확인
                    p_rec = None
                    for check_id in alt_indices:
                        if check_id in person_idx_map:
                            p_rec = person_idx_map[check_id]
                            break

                    # 수동으로 그룹에서 분리했던 ID(split_exclusions)는 서브 목록에서 제외
                    existing_exclusions = set((p_rec.extra_info or {}).get('split_exclusions', []) if p_rec else [])
                    filtered_alt_indices = [x for x in alt_indices if x not in existing_exclusions]
                    filtered_sub_actors = [x for x in merged_sub_actors if x.get('actor_id') not in existing_exclusions]

                    extra_data = {
                        'birth': str(best_r.get('birth') or '').strip(),
                        'height': h_val,
                        'body_size': str(best_r.get('body_size') or '').strip(),
                        'bra_size': str(best_r.get('bra_size') or '').strip(),
                        'debut': str(best_r.get('debut') or '').strip(),
                        'info_url': str(best_r.get('info_url') or '').strip() or f"https://www.avdbs.com/menu/actor.php?actor_idx={master_actor_id.replace('PA', '')}",
                        'agency': str(best_r.get('agency') or '').strip(),
                        'blood': str(best_r.get('blood') or '').strip(),
                        'hobby': str(best_r.get('hobby') or '').strip(),
                        'specialty': str(best_r.get('specialty') or '').strip(),
                        'source_origin': 'jav_actors',
                        'alt_actor_indices': filtered_alt_indices,
                        'merged_sub_actors': filtered_sub_actors,
                        'split_exclusions': list(existing_exclusions)
                    }

                    if not p_rec:
                        p_rec = MetaPerson(
                            domain="JAV",
                            name_org=name_org,
                            name_ko=name_ko,
                            name_en=name_en,
                            other_names=", ".join(merged_aliases),
                            aliases=list(merged_aliases),
                            person_idx=master_actor_id,
                            person_type="actor",
                            media_src=media_src_data,
                            works=combined_works,
                            extra_info=extra_data
                        )
                        sess.add(p_rec)
                        inserted += 1
                        for registered_id in filtered_alt_indices:
                            person_idx_map[registered_id] = p_rec
                    else:
                        p_rec.name_org = name_org
                        p_rec.name_ko = name_ko
                        if name_en and not p_rec.name_en:
                            p_rec.name_en = name_en

                        # 기존 별칭과 합집합
                        combined_alias_set = set(p_rec.aliases or [])
                        combined_alias_set.update(merged_aliases)
                        p_rec.aliases = list(combined_alias_set)
                        p_rec.other_names = ", ".join(p_rec.aliases)

                        # 미디어 갤러리 병합
                        existing_media = copy.deepcopy(p_rec.media_src or {})
                        existing_photos = existing_media.get('site_img_urls', [])
                        for sp in site_photos:
                            if sp not in existing_photos:
                                existing_photos.append(sp)
                        existing_media['site_img_urls'] = existing_photos
                        if not existing_media.get('local_img_path') and local_path_val:
                            existing_media['local_img_path'] = local_path_val
                        if not existing_media.get('google_fileid') and google_fileid_val:
                            existing_media['google_fileid'] = google_fileid_val
                        p_rec.media_src = existing_media

                        # extra_info 갱신
                        current_extra = copy.deepcopy(p_rec.extra_info or {})
                        current_extra.update(extra_data)
                        p_rec.extra_info = current_extra

                        if combined_works:
                            p_rec.works = combined_works

                        updated += 1
                        for registered_id in filtered_alt_indices:
                            person_idx_map[registered_id] = p_rec

                    if idx % batch_size == 0:
                        sess.commit()

                except Exception as e_cluster:
                    sess.rollback()
                    logger.error(f"[MetaDB ActorSync] 클러스터 처리 오류 (#{idx}): {e_cluster}")

            sess.commit()
            cls.checkpoint_wal()

            # DB 내 잔여 서브 중복 레코드 정리(Merge & Clean)
            try:
                all_current_jav_persons = sess.query(MetaPerson.id, MetaPerson.person_idx, MetaPerson.extra_info).filter_by(domain="JAV").all()
                primary_map = {}
                redundant_ids = set()

                for p_id, p_idx, p_extra in all_current_jav_persons:
                    alt_list_chk = (p_extra or {}).get('alt_actor_indices', [])
                    if len(alt_list_chk) > 1:
                        for s_idx in alt_list_chk:
                            if s_idx != p_idx:
                                primary_map[s_idx] = p_id

                for p_id, p_idx, _ in all_current_jav_persons:
                    if p_idx in primary_map and p_id != primary_map[p_idx]:
                        redundant_ids.add(p_id)

                if redundant_ids:
                    # PostgreSQL 파라미터 한계 방어를 위해 청크 단위로 안전 삭제
                    del_id_list = list(redundant_ids)
                    del_chunk_size = 500
                    for chunk_start in range(0, len(del_id_list), del_chunk_size):
                        chunk_ids = del_id_list[chunk_start:chunk_start + del_chunk_size]
                        sess.query(MetaPerson).filter(MetaPerson.id.in_(chunk_ids)).delete(synchronize_session=False)
                    sess.commit()
                    cls.checkpoint_wal()
                    merged_duplicates_count = len(redundant_ids)
                    logger.info(f"[MetaDB ActorSync] DB 내 파편화 레코드 {merged_duplicates_count}건 정리 완료")
            except Exception as e_clean:
                sess.rollback()
                logger.debug(f"[MetaDB ActorSync] 잔여 중복 정리 예외: {e_clean}")

            cls._cached_actors_map = None
            P.ModelSetting.set("meta_db_person_jav_last_synced_version", file_ver)
            elapsed = time.time() - t_start

            raw_total_count = len(raw_rows)
            unique_cluster_count = len(merged_clusters)
            source_merged_count = max(0, raw_total_count - unique_cluster_count)

            msg = (
                f"동기화 성공 ({file_ver}): 원본 {raw_total_count:,}건 중 {source_merged_count:,}건 중복 통합 ➔ "
                f"고유 인물 {unique_cluster_count:,}명 반영 (신규: {inserted:,}건, 갱신: {updated:,}건, "
                f"DB 정리: {merged_duplicates_count:,}건, 소요시간: {elapsed:.2f}초)"
            )
            logger.info(f"[MetaDB ActorSync] {msg}")
            return True, msg

        except Exception as e:
            sess.rollback()
            logger.error(f"[MetaDB ActorSync] 치명적 오류 발생: {e}")
            logger.error(traceback.format_exc())
            return False, f"동기화 중 치명적 오류 발생: {str(e)}"
        finally:
            sess.close()


    @classmethod
    def get_metadata(cls, code, category):
        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess: return None

        try:
            item = sess.query(MetaItem).filter_by(code=code, category=std_cat).first()
            if item: return cls.to_entity_dict(item)
            return None
        except Exception as e:
            logger.error(f"[MetaDB] get_metadata 에러 ({code}): {e}")
            return None
        finally:
            sess.remove()


    @classmethod
    def to_entity_dict(cls, item, for_list=False):
        include_original = P.ModelSetting.get_bool("meta_db_include_original")

        raw_extra = copy.deepcopy(item.extra_info or {})
        raw_original = copy.deepcopy(getattr(item, 'original', {}) or {})
        if not isinstance(raw_original, dict):
            raw_original = {}

        raw_orig_thumb = raw_original.get('thumb')
        if not isinstance(raw_orig_thumb, dict):
            raw_orig_thumb = {}
            raw_original['thumb'] = raw_orig_thumb

        d = {
            'domain': item.domain,
            'category': item.category,
            'code': item.code,
            'ui_code': item.ui_code,
            'originaltitle': item.originaltitle,
            'sorttitle': item.sorttitle or item.title or item.originaltitle,
            'site': item.site,
            'title': item.title,
            'tagline': item.tagline or '',
            'plot': item.plot or '',
            'director': item.director or '',
            'studio': item.studio or '',
            'series': item.series or '',
            'premiered': item.premiered or '',
            'year': item.year,
            'runtime': item.runtime,
            'mpaa': item.mpaa or '',
            'content_type': item.content_type or '',
            'country': raw_extra.get('country') or (["미국"] if item.category == "WESTERN" else ["일본"]),
            'rating': item.rating if item.rating is not None else 0.0,
            'rating_votes': item.rating_votes if item.rating_votes is not None else 0,
            'thumb': [],
            'fanart': [],
            'extras': [],
            'actor': [],
            'genre': [],
            'tag': [],
            'ratings': [{'name': item.site, 'value': item.rating, 'votes': item.rating_votes, 'max': 5}] if (item.rating is not None and item.rating > 0) else [],
            'spec_data': copy.deepcopy(item.spec_data or {}),
            'original': raw_original,
            'extra_info': raw_extra
        }

        # 이미지 모드 및 이미지 서버 설정 로드
        if item.category == 'WESTERN':
            stem = (item.code or item.ui_code or '').lower()
        else:
            stem = (item.ui_code or item.originaltitle or item.code or '').lower()

        module_prefix = 'western' if item.category == 'WESTERN' else ('jav_uncensored' if item.category == 'JAV_UNCEN' else 'jav_censored')
        current_image_mode = P.ModelSetting.get(f"{module_prefix}_image_mode") or P.ModelSetting.get("jav_censored_image_mode") or "ff_proxy"

        target_folder, server_url_prefix = MetaImageUtil.get_server_folder_and_prefix(
            item.domain, item.category, stem, studio=item.studio, year=item.year
        )

        resolved_p_url = ""
        resolved_pl_url = ""
        resolved_fanarts = []
        p_is_local = False
        pl_is_local = False

        # 목록 렌더링 시에는 과도한 디스크 스캔을 건너뛰고 DB에 저장된 주소를 즉시 활용
        if not for_list and current_image_mode == 'image_server' and target_folder and server_url_prefix and os.path.exists(target_folder):
            files_in_folder = {f.lower(): f for f in os.listdir(target_folder)}
            exts = ['jpg', 'jpeg', 'png', 'webp']

            user_p = next((files_in_folder[f"{stem}_p_user.{e}"] for e in exts if f"{stem}_p_user.{e}" in files_in_folder), None)
            sys_p = next((files_in_folder[f"{stem}_p.{e}"] for e in exts if f"{stem}_p.{e}" in files_in_folder), None)
            if user_p:
                resolved_p_url = f"{server_url_prefix}/{user_p}"
                p_is_local = True
            elif sys_p:
                resolved_p_url = f"{server_url_prefix}/{sys_p}"
                p_is_local = True

            user_pl = next((files_in_folder[f"{stem}_pl_user.{e}"] for e in exts if f"{stem}_pl_user.{e}" in files_in_folder), None)
            sys_pl = next((files_in_folder[f"{stem}_pl.{e}"] for e in exts if f"{stem}_pl.{e}" in files_in_folder), None)
            if user_pl:
                resolved_pl_url = f"{server_url_prefix}/{user_pl}"
                pl_is_local = True
            elif sys_pl:
                resolved_pl_url = f"{server_url_prefix}/{sys_pl}"
                pl_is_local = True

            art_files = sorted([files_in_folder[f] for f in files_in_folder if f.startswith(f"{stem}_art_")])
            resolved_fanarts = [f"{server_url_prefix}/{af}" for af in art_files]

        if not resolved_p_url:
            resolved_p_url = item.poster_url or raw_orig_thumb.get('poster') or ""

        if not resolved_pl_url:
            resolved_pl_url = raw_orig_thumb.get('landscape') or ""

        # 최대 아트 수 제한
        raw_max_arts = P.ModelSetting.get("jav_censored_art_count") or "0"
        try:
            max_arts = int(raw_max_arts)
        except (ValueError, TypeError):
            max_arts = 0

        if max_arts <= 0:
            d['fanart'] = []
        else:
            if not resolved_fanarts and isinstance(raw_original.get('fanart'), list):
                resolved_fanarts = list(raw_original['fanart'])
            d['fanart'] = resolved_fanarts[:max_arts]

        if resolved_pl_url:
            d['thumb'].append({'aspect': 'landscape', 'value': resolved_pl_url, 'site': item.site, 'is_local': pl_is_local})

        if resolved_p_url:
            d['thumb'].append({'aspect': 'poster', 'value': resolved_p_url, 'site': item.site, 'is_local': p_is_local})
        elif resolved_pl_url:
            d['thumb'].append({'aspect': 'poster', 'value': resolved_pl_url, 'site': item.site, 'is_local': pl_is_local})

        # 트레일러 URL 구성 (프리뷰 클립 또는 공식 예고편)
        ddns_host = F.SystemModelSetting.get('ddns') or ''
        is_uncen = str(item.category).upper() == 'JAV_UNCEN'
        video_endpoint = 'jav_video_un' if is_uncen else 'jav_video'

        has_preview = False
        if isinstance(raw_extra.get('preview_clip'), dict):
            p_clip = raw_extra['preview_clip']
            preview_stream_url = ''
            if p_clip.get('storage_type') == 'gdrive' and p_clip.get('google_fileid'):
                preview_stream_url = f"{ddns_host}/metadata/normal/{video_endpoint}?mode=preview_gdrive&fileid={p_clip['google_fileid']}&cat={item.category}"
            elif p_clip.get('storage_type') == 'local' and p_clip.get('local_path'):
                from urllib.parse import quote_plus
                preview_stream_url = f"{ddns_host}/metadata/normal/{video_endpoint}?mode=preview_local&path={quote_plus(p_clip['local_path'])}"

            if preview_stream_url:
                d['extras'].append({
                    'mode': 'mp4',
                    'title': f"[Preview] {item.title or item.tagline or item.originaltitle}",
                    'content_url': preview_stream_url,
                    'content_type': 'trailer'
                })
                has_preview = True

        if not has_preview:
            raw_video_url = ''
            orig_extras = raw_original.get('extras') or []
            if isinstance(orig_extras, list):
                for ex in orig_extras:
                    if isinstance(ex, dict) and ex.get('content_url'):
                        raw_video_url = ex['content_url']
                        break

            if not raw_video_url and isinstance(raw_original.get('trailer'), str) and raw_original['trailer'].strip():
                raw_video_url = raw_original['trailer'].strip()

            if raw_video_url:
                use_trailer_proxy = P.ModelSetting.get_bool("meta_db_use_ff_proxy")
                if item.category == 'WESTERN':
                    use_trailer_proxy = use_trailer_proxy and P.ModelSetting.get_bool('western_use_trailer_proxy')

                if use_trailer_proxy and raw_video_url.startswith('http'):
                    from urllib.parse import quote_plus
                    final_trailer_url = f"{ddns_host}/metadata/normal/jav_video?site={item.site}&url={quote_plus(raw_video_url)}"
                else:
                    final_trailer_url = raw_video_url

                d['extras'].append({'mode': 'mp4', 'title': item.tagline or item.title, 'content_url': final_trailer_url, 'content_type': 'trailer'})

        # 배우 다중 저장소 복원 및 역추적 자가 치유(Self-Healing)
        actors_bridge = (
            raw_extra.get('_actors') or
            raw_extra.get('actor_cache') or
            raw_extra.get('actors') or
            []
        )

        if not actors_bridge and hasattr(item, 'person_maps') and item.person_maps:
            for pm in item.person_maps:
                if pm.person:
                    p = pm.person
                    actors_bridge.append({
                        'actor_idx': p.person_idx or '',
                        'name_org': p.name_org or '',
                        'name_ko': p.name_ko or '',
                        'name_en': p.name_en or '',
                        'gender': (p.extra_info or {}).get('gender', ''),
                        'role': pm.role_name or '출연'
                    })

        if not actors_bridge and isinstance(raw_original.get('actor'), list):
            for act in raw_original['actor']:
                if isinstance(act, dict):
                    actors_bridge.append(act)
                elif isinstance(act, str) and act.strip():
                    actors_bridge.append({'name_org': act.strip(), 'name_ko': '', 'role': '출연'})

        if not actors_bridge and not for_list:
            person_sess, _, _ = cls.get_session_and_domain('PERSON')
            person_dom = cls._person_domain_from_item_category(item.category)
            if person_sess:
                try:
                    c_target = item.code
                    ui_target = item.ui_code or item.originaltitle or ''
                    healing_persons = person_sess.query(MetaPerson).filter(
                        MetaPerson.domain == person_dom,
                        or_(
                            cast(MetaPerson.works, Text).ilike(f'%"{c_target}"%'),
                            cast(MetaPerson.works, Text).ilike(f'%"{ui_target}"%')
                        )
                    ).all()

                    for hp in healing_persons:
                        actors_bridge.append({
                            'actor_idx': hp.person_idx or '',
                            'name_org': hp.name_org or '',
                            'name_ko': hp.name_ko or '',
                            'name_en': hp.name_en or '',
                            'thumb': cls.resolve_person_active_thumb(hp),
                            'gender': (hp.extra_info or {}).get('gender', ''),
                            'role': '출연'
                        })

                    if actors_bridge and hasattr(item, 'extra_info'):
                        updated_extra = dict(item.extra_info or {})
                        updated_extra['_actors'] = actors_bridge
                        item.extra_info = updated_extra
                        sess_item, _, _ = cls.get_session_and_domain(item.category)
                        if sess_item:
                            try:
                                sess_item.commit()
                                logger.info(f"[MetaDB Self-Healing] 작품 [{item.code}]에 누락되었던 배우 {len(actors_bridge)}명 역추적 자동 치유 완료")
                            except Exception:
                                sess_item.rollback()
                except Exception as e_heal:
                    logger.debug(f"[MetaDB Self-Healing] 역추적 치유 예외: {e_heal}")
                finally:
                    person_sess.remove()

        if actors_bridge:
            if for_list:
                for a_entry in actors_bridge:
                    a_name_ko = a_entry.get('name_ko') or ''
                    a_name_org = a_entry.get('name_org') or ''
                    d['actor'].append({
                        'name': a_name_ko or a_name_org or '',
                        'name_org': a_name_org,
                        'name_ko': a_name_ko,
                        'name_en': a_entry.get('name_en') or '',
                        'thumb': a_entry.get('thumb') or '',
                        'actor_idx': a_entry.get('actor_idx') or a_entry.get('person_idx') or '',
                        'gender': a_entry.get('gender') or '',
                        'role': a_entry.get('role') or '출연'
                    })
            else:
                person_sess, _, _ = cls.get_session_and_domain('PERSON')
                person_dom = cls._person_domain_from_item_category(item.category)
                if person_sess:
                    try:
                        for a_entry in actors_bridge:
                            a_idx = a_entry.get('actor_idx') or a_entry.get('person_idx') or ''
                            a_name_org = a_entry.get('name_org') or ''
                            a_name_ko = a_entry.get('name_ko') or ''
                            a_name_en = a_entry.get('name_en') or ''
                            role_name = a_entry.get('role') or '출연'

                            p_rec = None
                            if a_idx:
                                p_rec = person_sess.query(MetaPerson).filter_by(domain=person_dom, person_idx=a_idx).first()
                            if not p_rec and a_name_org:
                                p_rec = person_sess.query(MetaPerson).filter_by(domain=person_dom, name_org=a_name_org).first()
                            if not p_rec and a_name_ko:
                                p_rec = person_sess.query(MetaPerson).filter_by(domain=person_dom, name_ko=a_name_ko).first()

                            p_gender = (p_rec.extra_info.get('gender') if (p_rec and p_rec.extra_info) else '') or a_entry.get('gender') or ''
                            display_name = (p_rec.name_ko or p_rec.name_org or '') if p_rec else (a_name_ko or a_name_org or '')

                            if p_rec:
                                d['actor'].append({
                                    'name': display_name,
                                    'name_org': p_rec.name_org or '',
                                    'name_ko': p_rec.name_ko or '',
                                    'name_en': p_rec.name_en or '',
                                    'thumb': cls.resolve_person_active_thumb(p_rec),
                                    'actor_idx': p_rec.person_idx or '',
                                    'gender': p_gender,
                                    'role': role_name
                                })
                            else:
                                d['actor'].append({
                                    'name': display_name,
                                    'name_org': a_name_org,
                                    'name_ko': a_name_ko,
                                    'name_en': a_name_en,
                                    'thumb': a_entry.get('thumb') or '',
                                    'actor_idx': a_idx,
                                    'gender': p_gender,
                                    'role': role_name
                                })
                    finally:
                        person_sess.remove()

        d['extra_info'].pop('_actors', None)

        if raw_extra.get('info_url'):
            d['extra_info']['info_url'] = str(raw_extra['info_url']).strip()

        # 목록 렌더링 시에는 태그/지문 관계 테이블을 탐색하지 않고 JSON 보존 데이터로 경량 처리
        if for_list:
            if isinstance(raw_original.get('genre'), list):
                d['genre'] = list(raw_original['genre'])
            if isinstance(raw_extra.get('fingerprints'), list):
                d['extra_info']['fingerprints'] = raw_extra['fingerprints']
        else:
            for tm in item.tag_maps:
                t = tm.tag
                if t:
                    if t.tag_type == 'genre' and t.name not in d['genre']:
                        d['genre'].append(t.name)
                    elif t.tag_type == 'tag' and t.name not in d['tag']:
                        d['tag'].append(t.name)

            if not d['genre'] and isinstance(raw_original.get('genre'), list) and raw_original['genre']:
                from support_site import SiteAvBase
                for g_org in raw_original['genre']:
                    if isinstance(g_org, str) and g_org.strip():
                        g_trans = SiteAvBase.get_translated_tag(g_org.strip())
                        if g_trans and g_trans not in d['genre']:
                            d['genre'].append(g_trans)

            if hasattr(item, 'fingerprints') and item.fingerprints:
                fp_list = []
                for fp in item.fingerprints:
                    fp_list.append({
                        'algorithm': fp.algorithm,
                        'hash': fp.hash_value,
                        'source': fp.source
                    })
                d['extra_info']['fingerprints'] = fp_list

        if item.studio and item.studio not in d['tag']:
            d['tag'].append(item.studio)
        if item.series and item.series not in d['tag']:
            d['tag'].append(item.series)

        return d


    @classmethod
    def apply_transient_overrides(cls, entity_dict, extra_opts, category=None):
        """DB에 저장된 정규 마스터 데이터는 보존하고, 호출자가 요청한 임시 옵션에 맞추어 사본을 가공 반환합니다."""
        if not entity_dict or not isinstance(entity_dict, dict):
            return entity_dict

        opts = dict(extra_opts)
        override_cfg = opts.get('override_config', {})
        if not isinstance(override_cfg, dict):
            override_cfg = {}

        # DB 원본 및 메모리 캐시 오염을 원천 방지하기 위한 독립 사본 생성
        result = copy.deepcopy(entity_dict)

        if P.ModelSetting.get_bool("meta_db_use_ff_proxy"):
            ddns_host = F.SystemModelSetting.get('ddns') or ''
            is_uncensored = str(category or '').upper() == 'JAV_UNCEN'
            proxy_path = 'jav_video_un' if is_uncensored else 'jav_video'
            image_path = 'jav_image_un' if is_uncensored else 'jav_image'
            image_server_setting = (
                'western_image_server_url' if str(category or '').upper() == 'WESTERN'
                else 'jav_uncensored_image_server_url' if is_uncensored
                else 'jav_censored_image_server_url'
            )
            image_server_url = (
                P.ModelSetting.get(image_server_setting) or ''
            ).rstrip('/')

            def proxy_image(url, is_local=False):
                if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                    return url
                if is_local:
                    return url
                if image_server_url and url.startswith(image_server_url):
                    return url
                if ddns_host and url.startswith(ddns_host):
                    return url
                return f"{ddns_host}/metadata/normal/{image_path}?{urlencode({'site': result.get('site', ''), 'url': url})}"

            def proxy_video(url):
                if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                    return url
                if ddns_host and url.startswith(ddns_host):
                    return url
                return f"{ddns_host}/metadata/normal/{proxy_path}?{urlencode({'site': result.get('site', ''), 'url': url})}"

            result['thumb'] = [
                dict(thumb, value=proxy_image(thumb.get('value'), is_local=thumb.get('is_local', False)))
                if isinstance(thumb, dict) else thumb
                for thumb in result.get('thumb', [])
            ]

            result['fanart'] = [proxy_image(url) for url in result.get('fanart', []) if url]

            result['extras'] = [
                dict(extra, content_url=proxy_video(extra.get('content_url')))
                if isinstance(extra, dict) else extra
                for extra in result.get('extras', [])
            ]

        # 목록 렌더링 시에는 배우 썸네일 재조회 쿼리를 생략하여 속도 보장
        target_actor_order = (
            opts.get('actor_img_order') or
            override_cfg.get('actor_img_order') or
            override_cfg.get('jav_censored_avdbs_img_order') or
            override_cfg.get('western_actor_img_order')
        )
        override_image_mode = (
            opts.get('image_mode') or
            override_cfg.get('image_mode') or
            override_cfg.get('jav_censored_image_mode') or
            override_cfg.get('western_image_mode')
        )

        need_actor_thumb_override = bool(target_actor_order) or (override_image_mode and override_image_mode != 'image_server')

        if not opts.get('for_list') and need_actor_thumb_override and result.get('actor'):
            person_sess, _, _ = cls.get_session_and_domain('PERSON')
            person_dom = cls._person_domain_from_item_category(category or result.get('category'))
            std_order = target_actor_order
            if not std_order and override_image_mode != 'image_server':
                std_order = 'site_img_url, local_img_path' if person_dom == 'WESTERN' else 'google_fileid, site_img_url'

            try:
                for act in result['actor']:
                    act_idx = act.get('actor_idx') if isinstance(act, dict) else getattr(act, 'actor_idx', '')
                    act_name = (act.get('name_org') or act.get('name_ko')) if isinstance(act, dict) else (getattr(act, 'name_org', '') or getattr(act, 'name_ko', ''))

                    media_src = None
                    if person_sess:
                        p_rec = None
                        if act_idx:
                            p_rec = person_sess.query(MetaPerson).filter_by(domain=person_dom, person_idx=act_idx).first()
                        if not p_rec and act_name:
                            p_rec = person_sess.query(MetaPerson).filter(
                                MetaPerson.domain == person_dom,
                                or_(MetaPerson.name_org == act_name, MetaPerson.name_ko == act_name)
                            ).first()

                        if p_rec and p_rec.media_src:
                            media_src = p_rec.media_src

                    if not media_src and isinstance(act, dict) and act.get('extra_info'):
                        e_info = act['extra_info']
                        media_src = {
                            'google_fileid': e_info.get('google_fileid', ''),
                            'site_img_url': e_info.get('site_img_url', ''),
                            'local_img_path': e_info.get('local_img_path', '')
                        }

                    if media_src:
                        new_thumb = cls.resolve_actor_thumb_url(media_src, order_str=std_order, domain=person_dom)
                        if new_thumb:
                            if isinstance(act, dict):
                                act['thumb'] = new_thumb
                            else:
                                act.thumb = new_thumb
            except Exception as e_act_ov:
                logger.debug(f"[MetaDB Transient Override] 배우 썸네일 임시 치환 예외: {e_act_ov}")
            finally:
                if person_sess:
                    person_sess.remove()

        # 미디어 이미지 모드 오버라이드
        if override_image_mode and override_image_mode != 'image_server':
            orig_thumb = result.get('original', {}).get('thumb', {})
            raw_site_poster = orig_thumb.get('poster') or ''
            raw_site_landscape = orig_thumb.get('landscape') or ''

            if raw_site_poster and result.get('poster_url') and '/images/' in result['poster_url']:
                result['poster_url'] = raw_site_poster
                for th in result.get('thumb', []):
                    if isinstance(th, dict) and th.get('aspect') == 'poster':
                        th['value'] = raw_site_poster

            if raw_site_landscape and result.get('landscape_url') and '/images/' in result['landscape_url']:
                result['landscape_url'] = raw_site_landscape
                for th in result.get('thumb', []):
                    if isinstance(th, dict) and th.get('aspect') == 'landscape':
                        th['value'] = raw_site_landscape

            orig_fanarts = result.get('original', {}).get('fanart', [])
            if orig_fanarts and result.get('fanart'):
                if any('/images/' in f for f in result['fanart']):
                    result['fanart'] = list(orig_fanarts)

        if opts.get('strip_images'):
            result['poster_url'] = ''
            result['landscape_url'] = ''
            result['thumb'] = []
            result['fanart'] = []

        current_plot = str(result.get('plot') or '').strip()
        fallback_tagline = str(result.get('tagline') or '').strip()
        if not current_plot and fallback_tagline:
            result['plot'] = fallback_tagline

        return result


    @classmethod
    def _build_works_detailed_map(cls, items_list):
        all_work_codes_by_cat = {}
        for p in items_list:
            p_works = p.works if isinstance(p.works, dict) else {}
            for cat_k, c_list in p_works.items():
                if isinstance(c_list, list) and c_list:
                    if cat_k not in all_work_codes_by_cat:
                        all_work_codes_by_cat[cat_k] = set()
                    for c_item in c_list:
                        c_str = c_item.get('code') if isinstance(c_item, dict) else str(c_item)
                        if c_str:
                            all_work_codes_by_cat[cat_k].add(c_str)

        works_meta_map = {}
        for cat_k, codes_set in all_work_codes_by_cat.items():
            if not codes_set:
                continue
            item_engine = cls._engines.get('postgres') if cls._is_postgres else cls._engines.get(DOMAIN_MAP[cat_k][0])
            if item_engine:
                work_query_session = sessionmaker(bind=item_engine, autocommit=False, autoflush=False, expire_on_commit=False)()
                try:
                    codes_list = list(codes_set)
                    light_rows = work_query_session.query(
                        MetaItem.code,
                        MetaItem.ui_code,
                        MetaItem.title,
                        MetaItem.originaltitle
                    ).filter(
                        MetaItem.category == cat_k,
                        or_(
                            MetaItem.code.in_(codes_list),
                            MetaItem.ui_code.in_(codes_list),
                            MetaItem.originaltitle.in_(codes_list)
                        )
                    ).all()

                    for mi_code, mi_ui_code, mi_title, mi_orig_title in light_rows:
                        info_dict = {
                            'code': mi_code,
                            'ui_code': mi_ui_code or mi_orig_title or mi_code,
                            'title': mi_title or mi_orig_title or ''
                        }
                        works_meta_map[f"{cat_k}_{mi_code}"] = info_dict
                        if mi_ui_code:
                            works_meta_map[f"{cat_k}_{mi_ui_code}"] = info_dict
                        if mi_orig_title:
                            works_meta_map[f"{cat_k}_{mi_orig_title}"] = info_dict
                except Exception as e_wmap:
                    logger.debug(f"[MetaDB WorksMap] 출연작 매핑 쿼리 예외 ({cat_k}): {e_wmap}")
                finally:
                    work_query_session.close()
        return works_meta_map


    @classmethod
    def get_person_detailed_info(cls, person_identifier, domain='JAV'):
        """단일 인물의 소장 출연작을 실시간 MetaItem 테이블에서 조회하고 오염된 이미지 주소를 자동 치유"""
        cls.ensure_db_ready()
        sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess:
            return None

        try:
            target_str = str(person_identifier).strip()
            p_rec = None
            if target_str.isdigit():
                p_rec = sess.query(MetaPerson).filter_by(id=int(target_str)).first()
            if not p_rec and target_str:
                p_rec = sess.query(MetaPerson).filter(
                    MetaPerson.domain == domain,
                    or_(
                        MetaPerson.person_idx == target_str,
                        MetaPerson.name_org == target_str,
                        MetaPerson.name_ko == target_str
                    )
                ).first()
            if not p_rec and target_str:
                p_rec = sess.query(MetaPerson).filter(
                    or_(
                        MetaPerson.person_idx == target_str,
                        MetaPerson.name_org == target_str,
                        MetaPerson.name_ko == target_str
                    )
                ).first()

            if not p_rec:
                return None

            p_works = copy.deepcopy(p_rec.works if isinstance(p_rec.works, dict) else {})
            works_detailed = {}
            is_healing_needed = False

            # 인물에 등록된 출연작 코드를 바탕으로 실시간 MetaItem 테이블에서 최신 정보 조회
            for cat_k, c_list in p_works.items():
                if not isinstance(c_list, list) or not c_list:
                    continue

                std_cat = 'WESTERN' if cat_k.upper() in ['WEST', 'WESTERN'] else ('JAV_UNCEN' if cat_k.upper() in ['JAV_UNCEN', 'UNCENSORED'] else 'JAV_CEN')
                if std_cat not in DOMAIN_MAP:
                    continue

                code_candidates = set()
                for c_item in c_list:
                    c_str = c_item.get('code') if isinstance(c_item, dict) else str(c_item)
                    if c_str:
                        code_candidates.add(c_str)
                        code_candidates.add(c_str.upper())
                        code_candidates.add(c_str.lower())
                    if not isinstance(c_item, dict) or not c_item.get('title'):
                        is_healing_needed = True

                if not code_candidates:
                    continue

                item_engine = cls._engines.get('postgres') if cls._is_postgres else cls._engines.get(DOMAIN_MAP[std_cat][0])
                if not item_engine:
                    continue

                work_query_session = sessionmaker(bind=item_engine, autocommit=False, autoflush=False, expire_on_commit=False)()
                try:
                    search_list = list(code_candidates)
                    lower_search_list = [c.lower() for c in search_list]

                    rows = work_query_session.query(
                        MetaItem.code,
                        MetaItem.ui_code,
                        MetaItem.title,
                        MetaItem.originaltitle
                    ).filter(
                        MetaItem.category == std_cat,
                        or_(
                            MetaItem.code.in_(search_list),
                            MetaItem.ui_code.in_(search_list),
                            MetaItem.originaltitle.in_(search_list),
                            func.lower(MetaItem.code).in_(lower_search_list),
                            func.lower(MetaItem.ui_code).in_(lower_search_list),
                            func.lower(MetaItem.originaltitle).in_(lower_search_list)
                        )
                    ).all()

                    lookup = {}
                    for mi_code, mi_ui_code, mi_title, mi_orig_title in rows:
                        info_dict = {
                            'code': mi_code,
                            'ui_code': mi_ui_code or mi_orig_title or mi_code,
                            'title': mi_title or mi_orig_title or ''
                        }
                        for k_val in [mi_code, mi_ui_code, mi_orig_title]:
                            if k_val:
                                lookup[k_val] = info_dict
                                lookup[k_val.lower()] = info_dict
                                lookup[k_val.upper()] = info_dict

                    works_detailed[std_cat] = []
                    healed_work_entries = []

                    for c_item in c_list:
                        raw_c = c_item.get('code') if isinstance(c_item, dict) else str(c_item)
                        meta_info = lookup.get(raw_c) or lookup.get(raw_c.lower()) or lookup.get(raw_c.upper())

                        if meta_info and meta_info.get('title'):
                            works_detailed[std_cat].append(meta_info)
                            healed_work_entries.append(meta_info)
                        else:
                            fallback_ui = c_item.get('ui_code') if isinstance(c_item, dict) else raw_c
                            fallback_title = c_item.get('title') if isinstance(c_item, dict) else ''
                            entry = {
                                'code': raw_c,
                                'ui_code': fallback_ui or raw_c,
                                'title': fallback_title or (meta_info.get('title', '') if meta_info else '')
                            }
                            works_detailed[std_cat].append(entry)
                            healed_work_entries.append(entry)

                    p_works[std_cat] = healed_work_entries
                finally:
                    work_query_session.close()

            # site_img_urls 내 로컬 이미지 서버 주소 오염 자가 치유 (영구 정제)
            p_media = copy.deepcopy(p_rec.media_src if isinstance(p_rec.media_src, dict) else {})
            if 'site_img_urls' in p_media and isinstance(p_media['site_img_urls'], list):
                cleaned_urls = [u for u in p_media['site_img_urls'] if not cls.is_local_server_url(u)]
                if len(cleaned_urls) != len(p_media['site_img_urls']):
                    p_media['site_img_urls'] = cleaned_urls
                    is_healing_needed = True

            if 'site_img_url' in p_media and cls.is_local_server_url(p_media.get('site_img_url')):
                p_media['site_img_url'] = p_media['site_img_urls'][0] if p_media.get('site_img_urls') else ''
                is_healing_needed = True

            # 레거시 데이터가 실시간 데이터로 교정되었을 경우 DB에 즉시 영구 반영 (Healing Commit)
            if is_healing_needed:
                try:
                    p_rec.works = p_works
                    p_rec.media_src = p_media
                    sess.commit()
                    cls.checkpoint_wal()
                    logger.info(f"[MetaDB Self-Healing] 인물 [{p_rec.name_ko or p_rec.name_org}]의 출연작 및 이미지 URL 자동 치유 완료")
                except Exception as e_p_heal:
                    sess.rollback()
                    logger.debug(f"[MetaDB Self-Healing] 인물 치유 커밋 예외: {e_p_heal}")

            extra_data = copy.deepcopy(p_rec.extra_info or {})
            raw_local = str(p_media.get('local_img_path') or '').strip()
            if raw_local:
                resolved_url = cls.format_actor_thumb_url(raw_local, domain=p_rec.domain)
                p_media['local_img_url'] = resolved_url
                extra_data['local_img_url'] = resolved_url

            return {
                'id': p_rec.id,
                'domain': p_rec.domain,
                'name_org': p_rec.name_org,
                'name_ko': p_rec.name_ko or '',
                'name_en': p_rec.name_en or '',
                'other_names': p_rec.other_names or '',
                'aliases': p_rec.aliases or [],
                'thumb': cls.resolve_person_active_thumb(p_rec),
                'person_idx': p_rec.person_idx or '',
                'person_type': p_rec.person_type or 'actor',
                'media_src': p_media,
                'works': p_works,
                'works_detailed': works_detailed,
                'works_count': sum(len(v) for v in works_detailed.values()),
                'extra_info': extra_data
            }
        except Exception as e:
            logger.error(f"[MetaDB] get_person_detailed_info 오류 ({person_identifier}): {e}")
            return None
        finally:
            sess.remove()


    @classmethod
    def person_search(cls, keyword, domain="ALL", options=None):
        cls.ensure_db_ready()
        sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess: return []

        kw = str(keyword or '').strip()
        if not kw: return []

        opts = options or {}
        include_aliases = opts.get('include_aliases', True)

        try:
            search_like = f"%{kw}%"
            query = sess.query(MetaPerson)
            if domain and str(domain).upper() != 'ALL':
                query = query.filter_by(domain=str(domain).upper())

            filter_conditions = [
                MetaPerson.name_org.ilike(search_like),
                MetaPerson.name_ko.ilike(search_like),
                MetaPerson.person_idx == kw,
                MetaPerson.person_idx.ilike(search_like)
            ]

            if kw.isdigit():
                filter_conditions.append(MetaPerson.id == int(kw))

            if include_aliases:
                filter_conditions.extend([
                    MetaPerson.name_en.ilike(search_like),
                    MetaPerson.other_names.ilike(search_like)
                ])

            items = query.filter(or_(*filter_conditions)).limit(60).all()
            works_meta_map = cls._build_works_detailed_map(items)

            results = []
            for p in items:
                p_media = copy.deepcopy(p.media_src if isinstance(p.media_src, dict) else {})
                p_works = copy.deepcopy(p.works if isinstance(p.works, dict) else {})
                extra_data = copy.deepcopy(p.extra_info or {})
                raw_local = str(p_media.get('local_img_path') or '').strip()
                if raw_local:
                    resolved_url = cls.format_actor_thumb_url(raw_local, domain=p.domain)
                    p_media['local_img_url'] = resolved_url
                    extra_data['local_img_url'] = resolved_url

                works_detailed = {}
                for cat_k, c_list in p_works.items():
                    if isinstance(c_list, list):
                        works_detailed[cat_k] = []
                        for c_item in c_list:
                            code_str = c_item.get('code') if isinstance(c_item, dict) else str(c_item)
                            meta_lookup = works_meta_map.get(f"{cat_k}_{code_str}")
                            if meta_lookup and meta_lookup.get('title'):
                                works_detailed[cat_k].append(meta_lookup)
                            else:
                                works_detailed[cat_k].append({'code': code_str, 'ui_code': code_str, 'title': ''})

                results.append({
                    'id': p.id,
                    'domain': p.domain,
                    'name_org': p.name_org,
                    'name_ko': p.name_ko or '',
                    'name_en': p.name_en or '',
                    'other_names': p.other_names or '',
                    'aliases': p.aliases or [],
                    'thumb': cls.resolve_person_active_thumb(p),
                    'person_idx': p.person_idx or '',
                    'person_type': p.person_type or 'actor',
                    'media_src': p_media,
                    'works': p_works,
                    'works_detailed': works_detailed,
                    'works_count': sum(len(v) for v in p_works.values()) if isinstance(p_works, dict) else 0,
                    'extra_info': extra_data
                })
            return results

        except Exception as e:
            logger.error(f"[MetaDB Person Search] 오류 ({kw}): {e}")
            return []
        finally:
            sess.remove()

    @classmethod
    def person_web_list(cls, req, default_domain='ALL'):
        cls.ensure_db_ready()
        sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess: return {'success': False, 'paging': None, 'list': []}

        try:
            params = {}
            if req and hasattr(req, 'form'):
                for k, v in req.form.items(): params[k] = v
                arg1 = req.form.get('arg1', '')
                if arg1 and '=' in arg1:
                    try:
                        from urllib.parse import parse_qs
                        for pk, pv in parse_qs(arg1).items():
                            if pv: params[pk] = pv[0]
                    except: pass

            try: page = int(params.get('page', 1))
            except: page = 1
            if page < 1: page = 1

            try: page_size = int(params.get('page_size', 30))
            except: page_size = 30
            if page_size < 1: page_size = 30

            search_word = str(params.get('search_word', '')).strip()
            search_domain = str(params.get('search_domain', default_domain)).strip().upper()
            search_status = str(params.get('search_status', 'all')).strip()
            search_order = str(params.get('search_order', 'desc')).strip()

            base_query = sess.query(MetaPerson)
            if search_domain != 'ALL':
                base_query = base_query.filter(func.upper(MetaPerson.domain) == search_domain)

            if search_status == 'no_photo':
                base_query = base_query.filter(
                    or_(
                        MetaPerson.media_src == None,
                        cast(MetaPerson.media_src, Text) == '{}',
                        cast(MetaPerson.media_src, Text) == 'null'
                    )
                )
            elif search_status == 'has_photo':
                base_query = base_query.filter(
                    and_(
                        MetaPerson.media_src != None,
                        cast(MetaPerson.media_src, Text) != '{}',
                        cast(MetaPerson.media_src, Text) != 'null'
                    )
                )
            elif search_status == 'merged_only':
                if cls._is_postgres:
                    base_query = base_query.filter(
                        or_(
                            text("(json_typeof(meta_person.extra_info->'alt_actor_indices') = 'array' AND json_array_length(meta_person.extra_info->'alt_actor_indices') > 1)"),
                            text("(json_typeof(meta_person.extra_info->'merged_sub_actors') = 'array' AND json_array_length(meta_person.extra_info->'merged_sub_actors') > 1)")
                        )
                    )
                else:
                    base_query = base_query.filter(
                        or_(
                            func.json_array_length(MetaPerson.extra_info, '$.alt_actor_indices') > 1,
                            func.json_array_length(MetaPerson.extra_info, '$.merged_sub_actors') > 1
                        )
                    )

            query = base_query.filter(or_(
                func.trim(func.coalesce(MetaPerson.name_org, '')) != '',
                func.trim(func.coalesce(MetaPerson.name_ko, '')) != '',
                func.trim(func.coalesce(MetaPerson.name_en, '')) != '',
                func.trim(func.coalesce(MetaPerson.other_names, '')) != '',
                func.trim(func.coalesce(MetaPerson.person_idx, '')) != ''
            ))

            if search_word:
                search_like = f"%{search_word}%"
                query = query.filter(or_(
                    MetaPerson.name_org.ilike(search_like),
                    MetaPerson.name_ko.ilike(search_like),
                    MetaPerson.name_en.ilike(search_like),
                    MetaPerson.other_names.ilike(search_like),
                    MetaPerson.person_idx.ilike(search_like)
                ))

            exact_kw = search_word.strip()
            raw_num = re.sub(r'^a', '', exact_kw, flags=re.IGNORECASE) if exact_kw else ''

            match_priority = case(
                (MetaPerson.person_idx == exact_kw, 0),
                (MetaPerson.person_idx == f"A{raw_num}", 0) if raw_num else (MetaPerson.person_idx == exact_kw, 0),
                (func.lower(MetaPerson.name_org) == exact_kw.lower(), 1),
                (func.lower(MetaPerson.name_ko) == exact_kw.lower(), 1),
                (func.lower(MetaPerson.name_en) == exact_kw.lower(), 1),
                (MetaPerson.person_idx.ilike(f"{exact_kw}%"), 2),
                (MetaPerson.name_org.ilike(f"{exact_kw}%"), 3),
                (MetaPerson.name_ko.ilike(f"{exact_kw}%"), 3),
                else_=4
            )

            if search_order == 'match' or (search_word and search_order == 'desc'):
                query = query.order_by(
                    match_priority.asc(),
                    func.length(MetaPerson.person_idx).asc(),
                    MetaPerson.person_idx.asc(),
                    MetaPerson.id.desc()
                )
            elif search_order == 'id_asc':
                query = query.order_by(
                    func.length(MetaPerson.person_idx).asc(),
                    MetaPerson.person_idx.asc(),
                    MetaPerson.id.asc()
                )
            elif search_order == 'id_desc':
                query = query.order_by(
                    func.length(MetaPerson.person_idx).desc(),
                    MetaPerson.person_idx.desc(),
                    MetaPerson.id.desc()
                )
            elif search_order == 'asc':
                query = query.order_by(MetaPerson.id.asc())
            elif search_order == 'name_asc':
                query = query.order_by(func.coalesce(MetaPerson.name_ko, MetaPerson.name_org).asc())
            elif search_order == 'name_desc':
                query = query.order_by(func.coalesce(MetaPerson.name_ko, MetaPerson.name_org).desc())
            else:
                query = query.order_by(MetaPerson.id.desc())

            # 인물 테이블 미존재 감지 시 스키마 자동 복구
            try:
                count = query.count()
            except Exception as e_p_tbl:
                logger.debug(f"[MetaDB Person WebList] 인물 테이블 미존재 감지 -> 스키마 자동 복구: {e_p_tbl}")
                sess.rollback()
                target_engine = cls._engines.get('postgres' if cls._is_postgres else 'person')
                if target_engine:
                    Base.metadata.create_all(bind=target_engine)
                    cls._auto_sync_table_columns(target_engine)
                count = query.count()

            if count == 0:
                return {'success': True, 'paging': None, 'list': []}

            total_page = math.ceil(count / page_size) if count > 0 else 1

            if page > total_page and total_page > 0:
                page = total_page

            start_page = ((page - 1) // 10) * 10 + 1
            end_page = min(start_page + 9, total_page)

            items = query.offset((page - 1) * page_size).limit(page_size).all()

            works_meta_map = cls._build_works_detailed_map(items)

            item_list = []
            for p in items:
                p_media = copy.deepcopy(p.media_src if isinstance(p.media_src, dict) else {})
                p_works = copy.deepcopy(p.works if isinstance(p.works, dict) else {})
                extra_data = copy.deepcopy(p.extra_info or {})
                raw_local = str(p_media.get('local_img_path') or '').strip()
                if raw_local:
                    resolved_url = cls.format_actor_thumb_url(raw_local, domain=p.domain)
                    p_media['local_img_url'] = resolved_url
                    extra_data['local_img_url'] = resolved_url

                works_detailed = {}
                for cat_k, c_list in p_works.items():
                    if isinstance(c_list, list):
                        works_detailed[cat_k] = []
                        for c_item in c_list:
                            code_str = c_item.get('code') if isinstance(c_item, dict) else str(c_item)
                            meta_lookup = works_meta_map.get(f"{cat_k}_{code_str}")
                            if meta_lookup and meta_lookup.get('title'):
                                works_detailed[cat_k].append(meta_lookup)
                            else:
                                fallback_ui = c_item.get('ui_code') if isinstance(c_item, dict) else code_str
                                works_detailed[cat_k].append({'code': code_str, 'ui_code': fallback_ui or code_str, 'title': ''})

                item_list.append({
                    'id': p.id,
                    'domain': p.domain,
                    'name_org': p.name_org,
                    'name_ko': p.name_ko or '',
                    'name_en': p.name_en or '',
                    'other_names': p.other_names or '',
                    'aliases': p.aliases or [],
                    'thumb': cls.resolve_person_active_thumb(p),
                    'person_idx': p.person_idx or '',
                    'person_type': p.person_type or 'actor',
                    'media_src': p_media,
                    'works': p_works,
                    'works_detailed': works_detailed,
                    'works_count': sum(len(v) for v in p_works.values()) if isinstance(p_works, dict) else 0,
                    'extra_info': extra_data
                })

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

            img_srv_key = "western_image_server_url" if search_domain == "WESTERN" else "jav_censored_image_server_url"
            server_url_val = (P.ModelSetting.get(img_srv_key) or P.ModelSetting.get("jav_censored_image_server_url") or "").rstrip('/')

            return {
                'success': True,
                'paging': paging,
                'list': item_list,
                'image_server_url': server_url_val
            }

        except Exception as e:
            logger.error(traceback.format_exc())
            return {'success': False, 'paging': None, 'list': []}
        finally:
            sess.remove()

    @classmethod
    def person_save(cls, person_dict):
        cls.ensure_db_ready()
        sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess:
            return False, "세션 생성 실패"

        try:
            pid = person_dict.get('id') if isinstance(person_dict, dict) else None
            p_rec = sess.query(MetaPerson).filter_by(id=pid).first() if pid else None

            if not p_rec:
                p_rec = MetaPerson()
                sess.add(p_rec)

            p_rec.domain = person_dict.get('domain', 'JAV')
            p_rec.name_org = str(person_dict.get('name_org') or '').strip()
            p_rec.name_ko = str(person_dict.get('name_ko') or '').strip()
            p_rec.name_en = str(person_dict.get('name_en') or '').strip()
            p_rec.person_idx = str(person_dict.get('person_idx') or '').strip()
            p_rec.person_type = person_dict.get('person_type', 'actor')

            # JAV 도메인은 한국어 표기명(name_ko) 필수 검증
            if p_rec.domain == 'JAV' and not p_rec.name_ko:
                return False, "JAV 인물은 한국어 표기명(name_ko)을 필수로 입력해야 합니다."

            if not p_rec.name_org and not p_rec.name_ko:
                return False, "원문 이름 또는 한국어 표기명을 입력하세요."

            raw_aliases = person_dict.get('aliases') or person_dict.get('other_names') or []
            if isinstance(raw_aliases, str):
                alias_list = [x.strip() for x in re.split(r'[,/]', raw_aliases) if x.strip()]
            else:
                alias_list = [str(x).strip() for x in raw_aliases if str(x).strip()]

            p_rec.aliases = alias_list
            p_rec.other_names = ", ".join(alias_list)

            # media_src 분리 저장
            p_media = person_dict.get('media_src') or {}
            raw_local = str(person_dict.get('local_img_path') or p_media.get('local_img_path') or '').strip()
            if raw_local:
                parts = [p for p in raw_local.replace('\\', '/').strip('/').split('/') if p]
                clean_local = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else raw_local
            else:
                clean_local = ''

            raw_site_urls = person_dict.get('site_img_urls')
            if isinstance(raw_site_urls, list):
                site_urls_list = [str(u).strip() for u in raw_site_urls if str(u).strip()]
            else:
                site_urls_list = []

            primary_target_url = str(person_dict.get('selected_primary_url') or person_dict.get('thumb') or '').strip()

            # 이미지 서버 사용 시 새 원격 이미지로 대표 지정되었을 경우 디스크 파일 교체
            module_pfx = 'western' if p_rec.domain == 'WESTERN' else 'jav_censored'
            current_image_mode = P.ModelSetting.get(f"{module_pfx}_image_mode") or P.ModelSetting.get("jav_censored_image_mode") or "ff_proxy"

            if current_image_mode == 'image_server' and primary_target_url.startswith('http'):
                from support_site import SiteAvBase
                if p_rec.domain == 'WESTERN':
                    act_dummy = {
                        'thumb': primary_target_url,
                        'name_org': p_rec.name_org,
                        'name_en': p_rec.name_en,
                        'actor_idx': p_rec.person_idx
                    }
                    SiteAvBase.save_western_actor_image(act_dummy)
                    if act_dummy.get('local_img_path'):
                        clean_local = act_dummy['local_img_path']
                        p_rec.thumb = act_dummy.get('thumb') or p_rec.thumb
                else:
                    act_dummy = {
                        'thumb': primary_target_url,
                        'name_org': p_rec.name_org,
                        'name_ko': p_rec.name_ko,
                        'actor_idx': p_rec.person_idx
                    }
                    SiteAvBase.save_actor_image(act_dummy)
                    if act_dummy.get('local_img_path'):
                        clean_local = act_dummy['local_img_path']
                        p_rec.thumb = act_dummy.get('thumb') or p_rec.thumb

            p_rec.media_src = {
                'local_img_path': clean_local,
                'google_fileid': str(person_dict.get('google_fileid') or p_media.get('google_fileid') or '').strip(),
                'site_img_url': str(person_dict.get('site_img_url') or p_media.get('site_img_url') or '').strip(),
                'site_img_urls': site_urls_list
            }

            # extra_info 순수 프로필 분리 저장 및 기존 병합/예외 목록 보존
            h_raw = person_dict.get('height')
            m_h = re.search(r'\d+', str(h_raw)) if h_raw is not None else None
            h_val = int(m_h.group()) if (m_h and int(m_h.group()) > 0) else None

            existing_extra = copy.deepcopy(p_rec.extra_info or {})
            existing_extra.update({
                'birth': str(person_dict.get('birth') or '').strip(),
                'height': h_val,
                'body_size': str(person_dict.get('body_size') or '').strip(),
                'bra_size': str(person_dict.get('bra_size') or '').strip(),
                'debut': str(person_dict.get('debut') or '').strip(),
                'info_url': str(person_dict.get('info_url') or '').strip(),
                'agency': str(person_dict.get('agency') or '').strip(),
                'blood': str(person_dict.get('blood') or '').strip(),
                'hobby': str(person_dict.get('hobby') or '').strip(),
                'specialty': str(person_dict.get('specialty') or '').strip(),
                'source_origin': str(existing_extra.get('source_origin') or 'web').strip()
            })
            p_rec.extra_info = existing_extra

            sess.commit()
            cls.checkpoint_wal()
            logger.info(f"[MetaDB Person Save] 인물 정보 저장 완료: {p_rec.person_idx} ({p_rec.name_ko or p_rec.name_org})")
            return True, "인물 정보가 성공적으로 저장되었습니다."

        except Exception as e:
            sess.rollback(); return False, str(e)
        finally:
            sess.remove()

    @classmethod
    def person_delete(cls, person_id):
        cls.ensure_db_ready()
        sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess:
            return False

        try:
            pid = int(person_id)
            p_rec = sess.query(MetaPerson).filter(MetaPerson.id == pid).first()
            if p_rec:
                person_name_log = p_rec.name_org or p_rec.name_ko or str(pid)
                sess.delete(p_rec)
                sess.commit()
                logger.info(f"[MetaDB] MetaPerson 삭제 완료 (ID: {pid}, Name: {person_name_log})")
                return True
            else:
                logger.warning(f"[MetaDB] 삭제 대상 MetaPerson 없음 (ID: {person_id})")
                return False
        except Exception as e:
            sess.rollback()
            logger.error(f"[MetaDB] person_delete 오류 ({person_id}): {e}")
            return False
        finally:
            sess.remove()

    @classmethod
    def person_clear_db(cls, domain='ALL'):
        cls.ensure_db_ready(); sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess: return False, 0

        target_dom = str(domain or 'ALL').strip().upper()
        try:
            query = sess.query(MetaPerson)
            if target_dom != 'ALL':
                query = query.filter(func.upper(MetaPerson.domain) == target_dom)
            count = query.delete(synchronize_session=False)
            sess.commit(); cls.checkpoint_wal(); return True, count
        except Exception as e:
            sess.rollback(); logger.error(f"[MetaDB Person Clear] 오류: {e}"); return False, 0
        finally:
            sess.remove()

    @classmethod
    def search_by_fingerprint(cls, category, algorithm, hash_value, preferred_site=None):
        """B-Tree 인덱스를 통해 지문 일치 항목들을 조회하고 사용자 사이트 우선순위(western_order 등)에 맞추어 정렬 반환"""
        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess or not hash_value:
            return []

        clean_algo = str(algorithm or 'OSHASH').strip().upper()
        clean_hash = str(hash_value).strip().lower()

        try:
            # 동일 지문에 매핑된 모든 레코드(StashDB, TPDB 등)를 일괄 B-Tree 조회
            fp_matches = sess.query(MetaFingerprint).filter(
                MetaFingerprint.category == std_cat,
                MetaFingerprint.algorithm == clean_algo,
                MetaFingerprint.hash_value == clean_hash
            ).all()

            if not fp_matches:
                return []

            # 사용자가 설정한 사이트 우선순위 로드 (기본: stashdb, tpdb)
            priority_setting_key = "western_order" if std_cat == "WESTERN" else f"{domain}_order"
            site_order_raw = P.ModelSetting.get(priority_setting_key) or "stashdb, tpdb"
            site_priority_list = [s.strip().lower() for s in site_order_raw.split(',') if s.strip()]
            
            # 특정 사이트(호출자) 선호가 있을 경우 최우선으로 배치
            if preferred_site and preferred_site.lower() in site_priority_list:
                site_priority_list.remove(preferred_site.lower())
                site_priority_list.insert(0, preferred_site.lower())

            def get_site_rank(code_str, item_site_str=None):
                target_site = (item_site_str or '').lower()
                if not target_site:
                    if '_S' in code_str or code_str.startswith('WS'): target_site = 'stashdb'
                    elif '_P' in code_str or code_str.startswith('WP'): target_site = 'tpdb'
                try:
                    return site_priority_list.index(target_site)
                except ValueError:
                    return 99

            # 우선순위 순으로 정렬
            sorted_fps = sorted(fp_matches, key=lambda x: get_site_rank(x.code, x.item.site if x.item else None))

            results = []
            for fp in sorted_fps:
                if fp.item:
                    item = fp.item
                    results.append({
                        'code': item.code,
                        'site': item.site,
                        'originaltitle': item.originaltitle,
                        'sorttitle': item.sorttitle or item.title or item.originaltitle,
                        'title': item.title,
                        'poster_url': item.poster_url,
                        'has_item': True,
                        'json_data': cls.to_entity_dict(item)
                    })
                else:
                    # 지문은 있으나 MetaItem이 없는 고아 지문인 경우 (새로 사이트에서 다운로드 유도)
                    infer_site = 'stashdb' if ('_S' in fp.code or fp.code.startswith('WS')) else 'tpdb'
                    results.append({
                        'code': fp.code,
                        'site': infer_site,
                        'originaltitle': fp.code,
                        'sorttitle': fp.code,
                        'title': f"[원격 재수집 대상] {fp.code}",
                        'poster_url': '',
                        'has_item': False,
                        'json_data': {}
                    })

            if results:
                best = results[0]
                logger.info(f"[MetaDB Fingerprint HIT] 지문 일치 항목 {len(results)}건 발견 ➔ 1위 채택: [{best['site'].upper()}] {best['code']} (Algo: {clean_algo}, Hash: {clean_hash})")
            return results

        except Exception as e:
            logger.debug(f"[MetaDB] search_by_fingerprint 조회 예외: {e}")
            return []
        finally:
            sess.remove()

    @classmethod
    def get_alternative_codes_by_code(cls, category, failed_code):
        """특정 코드로 메타 취득 실패 시, 동일 지문(Fingerprint)을 공유하는 타 사이트 대체 코드들을 우선순위 순으로 반환"""
        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess or not failed_code:
            return []

        try:
            # 실패한 코드에 매핑되어 있던 지문 해시값들 조회
            my_fps = sess.query(MetaFingerprint).filter(
                MetaFingerprint.category == std_cat,
                MetaFingerprint.code == failed_code
            ).all()

            if not my_fps:
                return []

            hash_values = [fp.hash_value for fp in my_fps]

            # 동일 해시를 가진 다른 코드들 탐색
            sibling_fps = sess.query(MetaFingerprint).filter(
                MetaFingerprint.category == std_cat,
                MetaFingerprint.hash_value.in_(hash_values),
                MetaFingerprint.code != failed_code
            ).all()

            if not sibling_fps:
                return []

            priority_setting_key = "western_order" if std_cat == "WESTERN" else f"{domain}_order"
            site_order_raw = P.ModelSetting.get(priority_setting_key) or "stashdb, tpdb"
            site_priority_list = [s.strip().lower() for s in site_order_raw.split(',') if s.strip()]

            def get_site_rank(c_str):
                c_upper = c_str.upper()
                target_site = 'stashdb' if ('_S' in c_upper or c_upper.startswith('WS')) else 'tpdb'
                try: return site_priority_list.index(target_site)
                except ValueError: return 99

            sorted_siblings = sorted(sibling_fps, key=lambda x: get_site_rank(x.code))
            alt_codes = []
            for s_fp in sorted_siblings:
                if s_fp.code not in alt_codes:
                    alt_codes.append(s_fp.code)

            if alt_codes:
                logger.info(f"[MetaDB Fingerprint Fallback] 코드 '{failed_code}' 실패 대비 대체 코드 {len(alt_codes)}건 확보: {alt_codes}")
            return alt_codes

        except Exception as e:
            logger.debug(f"[MetaDB] get_alternative_codes_by_code 예외: {e}")
            return []
        finally:
            sess.remove()

    @classmethod
    def append_fingerprint(cls, code, category, algorithm, hash_value, source="user"):
        """기존 메타 레코드에 새 지문이 감지되었을 때 중복 없이 B-Tree 색인 및 extra_info에 자동 추가"""
        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess or not code or not hash_value:
            return False

        clean_algo = str(algorithm or 'OSHASH').strip().upper()
        clean_hash = str(hash_value).strip().lower()

        try:
            # 이미 동일한 지문이 등록되어 있는지 B-Tree 인덱스로 즉시 확인
            exists = sess.query(MetaFingerprint).filter(
                MetaFingerprint.category == std_cat,
                MetaFingerprint.algorithm == clean_algo,
                MetaFingerprint.hash_value == clean_hash
            ).first()

            if exists:
                return True

            item = sess.query(MetaItem).filter_by(code=code, category=std_cat).first()
            if not item:
                return False

            # 새 지문 레코드 추가
            item.fingerprints.append(MetaFingerprint(
                category=std_cat,
                code=code,
                algorithm=clean_algo,
                hash_value=clean_hash,
                source=source
            ))

            # extra_info JSON 동기화
            extra = dict(item.extra_info or {})
            fps = list(extra.get('fingerprints') or [])
            if not any(f.get('algorithm') == clean_algo and f.get('hash') == clean_hash for f in fps):
                fps.append({
                    'algorithm': clean_algo,
                    'hash': clean_hash,
                    'source': source
                })
                extra['fingerprints'] = fps
                item.extra_info = extra

            sess.commit()
            logger.info(f"[MetaDB Fingerprint LEARN] 기존 코드({code})에 새 로컬 지문 자동 학습 등록 완료! (Algo: {clean_algo}, Hash: {clean_hash})")
            return True
        except Exception as e:
            sess.rollback()
            logger.debug(f"[MetaDB] append_fingerprint 예외: {e}")
            return False
        finally:
            sess.remove()

    @classmethod
    def search_for_auto_match(cls, category, keyword):
        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess: return []

        try:
            results = []
            
            if std_cat == 'WESTERN':
                kw_clean = re.sub(r'[^\w\s]', ' ', str(keyword or '').lower()).strip()
                tokens = [t for t in kw_clean.split() if len(t) >= 2]

                query = sess.query(MetaItem).filter(MetaItem.category == std_cat)
                if tokens:
                    conditions = []
                    for t in tokens:
                        conditions.append(or_(
                            MetaItem.originaltitle.ilike(f"%{t}%"),
                            MetaItem.title.ilike(f"%{t}%"),
                            MetaItem.studio.ilike(f"%{t}%"),
                            MetaItem.code.ilike(f"%{t}%")
                        ))
                    query = query.filter(and_(*conditions))
                else:
                    query = query.filter(MetaItem.originaltitle.ilike(f"%{keyword}%"))

                items = query.limit(50).all()
                for item in items:
                    item_title_clean = re.sub(r'[^\w\s]', ' ', str(item.title or item.originaltitle or '').lower())
                    item_tokens = set(item_title_clean.split())
                    kw_token_set = set(tokens)

                    # 단어 교집합 비율 또는 완전 포함 여부 검사
                    intersect = kw_token_set.intersection(item_tokens)
                    is_match = False
                    if kw_token_set and (len(intersect) / len(kw_token_set)) >= 0.7:
                        is_match = True
                    elif kw_clean in item_title_clean or item_title_clean in kw_clean:
                        is_match = True

                    if is_match:
                        light_json = {
                            'ui_code': item.ui_code or item.code,
                            'title': item.title,
                            'year': item.year,
                            'premiered': item.premiered,
                            'studio': item.studio,
                            'plot': item.plot,
                            'actor': (item.extra_info or {}).get('_actors', []),
                            'content_type': item.content_type
                        }
                        results.append({
                            'code': item.code,
                            'site': item.site,
                            'originaltitle': item.originaltitle,
                            'sorttitle': item.sorttitle or item.title or item.originaltitle,
                            'title': item.title,
                            'poster_url': item.poster_url,
                            'json_data': light_json
                        })
                return results

            from support_site import SiteAvBase
            kw_ui_code, kw_label, kw_num = SiteAvBase._parse_ui_code(keyword, category=std_cat)

            query = sess.query(MetaItem).filter(MetaItem.category == std_cat)
            if kw_label and kw_num:
                query = query.filter(
                    and_(
                        MetaItem.originaltitle.ilike(f"%{kw_label}%"),
                        MetaItem.originaltitle.ilike(f"%{kw_num}%")
                    )
                )
            elif kw_num:
                query = query.filter(MetaItem.originaltitle.ilike(f"%{kw_num}%"))
            else:
                query = query.filter(MetaItem.originaltitle.ilike(f"%{keyword}%"))

            items = query.limit(100).all()

            for item in items:
                match_score = SiteAvBase._calculate_score(keyword, item.originaltitle)
                if match_score >= 99:
                    results.append({
                        'code': item.code,
                        'site': item.site,
                        'originaltitle': item.originaltitle,
                        'sorttitle': item.sorttitle or item.title or item.originaltitle,
                        'title': item.title,
                        'poster_url': item.poster_url,
                        'json_data': cls.to_entity_dict(item)
                    })
            return results
        except Exception as e:
            logger.error(f"[MetaDB] search_for_auto_match 에러: {e}")
            return []
        finally:
            sess.remove()


    @classmethod
    def web_list(cls, req, category=None):
        cls.ensure_db_ready()

        params = {}
        if req and hasattr(req, 'form'):
            for k, v in req.form.items(): params[k] = v
            arg1 = req.form.get('arg1', '')
            if arg1 and '=' in arg1:
                try:
                    from urllib.parse import parse_qs
                    for pk, pv in parse_qs(arg1).items():
                        if pv: params[pk] = pv[0]
                except: pass

        try: page = int(params.get('page', 1))
        except: page = 1
        if page < 1: page = 1

        target_cat = category or params.get('category')
        if not target_cat and req and hasattr(req, 'path'):
            path_lower = req.path.lower()
            if 'jav_censored' in path_lower: target_cat = 'JAV_CEN'
            elif 'jav_uncensored' in path_lower: target_cat = 'JAV_UNCEN'
            elif 'western' in path_lower: target_cat = 'WESTERN'

        if not target_cat: target_cat = 'JAV_CEN'

        sess, domain, std_cat = cls.get_session_and_domain(target_cat)
        if not sess:
            logger.error(f"[MetaDB WebList] 세션 획득 실패: target_cat='{target_cat}'")
            return {'success': False, 'paging': None, 'list': []}

        try:
            try: page_size = int(params.get('page_size', 10))
            except: page_size = 10
            if page_size < 1: page_size = 10

            search_word = str(params.get('search_word', '')).strip()
            search_site = str(params.get('search_site', 'all')).strip()
            search_order = str(params.get('search_order', 'desc')).strip()
            search_status = str(params.get('search_status', 'all')).strip()

            filter_conditions = [MetaItem.category == std_cat]

            if search_site and search_site.lower() not in ['all', '']:
                filter_conditions.append(func.lower(MetaItem.site) == search_site.lower())

            if search_status == 'no_poster':
                filter_conditions.append(or_(
                    MetaItem.poster_url == '',
                    MetaItem.poster_url == None,
                    MetaItem.poster_url.ilike('%_pl.jpg'),
                    MetaItem.poster_url.ilike('%_pl.png'),
                    MetaItem.poster_url.ilike('%_pl.webp')
                ))
            elif search_status == 'no_plot':
                filter_conditions.append(or_(MetaItem.plot == '', MetaItem.plot == None))
            elif search_status == 'complete':
                filter_conditions.append(and_(
                    MetaItem.poster_url != '',
                    MetaItem.poster_url != None,
                    MetaItem.plot != '',
                    MetaItem.plot != None
                ))

            # B-Tree 인덱스 컬럼 대상 고속 검색 (전체 JSON 텍스트 캐스팅 풀스캔 제외)
            if search_word:
                search_like = f"%{search_word.replace('-', '%')}%"
                filter_conditions.append(or_(
                    MetaItem.originaltitle.ilike(search_like),
                    MetaItem.ui_code.ilike(search_like),
                    MetaItem.code.ilike(search_like),
                    MetaItem.title.ilike(f'%{search_word}%'),
                    MetaItem.studio.ilike(f'%{search_word}%'),
                    MetaItem.director.ilike(f'%{search_word}%')
                ))

            # 조인 없는 단일 초고속 카운트 쿼리
            try:
                count = sess.query(func.count(MetaItem.id)).filter(and_(*filter_conditions)).scalar() or 0
            except Exception as e_tbl_chk:
                sess.rollback()
                target_engine = cls._engines.get('postgres' if cls._is_postgres else domain)
                if target_engine:
                    Base.metadata.create_all(bind=target_engine)
                    cls._auto_sync_table_columns(target_engine)
                count = sess.query(func.count(MetaItem.id)).filter(and_(*filter_conditions)).scalar() or 0

            if count == 0:
                return {'success': True, 'paging': None, 'list': []}

            total_page = math.ceil(count / page_size) if count > 0 else 1

            if page > total_page and total_page > 0:
                page = total_page

            start_page = ((page - 1) // 10) * 10 + 1
            end_page = min(start_page + 9, total_page)

            # 4중 관계 조인을 비활성화하고 순수 MetaItem 25건만 쿼리
            query = sess.query(MetaItem).filter(and_(*filter_conditions)).options(
                lazyload(MetaItem.media_files),
                lazyload(MetaItem.person_maps),
                lazyload(MetaItem.tag_maps),
                lazyload(MetaItem.fingerprints)
            )

            if search_order == 'asc': query = query.order_by(MetaItem.created_time.asc())
            elif search_order == 'code_asc': query = query.order_by(MetaItem.originaltitle.asc(), MetaItem.code.asc())
            elif search_order == 'code_desc': query = query.order_by(MetaItem.originaltitle.desc(), MetaItem.code.desc())
            else: query = query.order_by(MetaItem.created_time.desc())

            items = query.offset((page - 1) * page_size).limit(page_size).all()

            url_mapping_str = P.ModelSetting.get("meta_db_image_url_mapping") or ""
            mappings = []
            if url_mapping_str:
                for line in url_mapping_str.split('\n'):
                    if '|' in line:
                        p = line.split('|', 1)
                        if p[0].strip() and p[1].strip(): mappings.append((p[0].strip(), p[1].strip()))

            item_list = []
            for item in items:
                # 목록용 고속 렌더링 플래그 전달
                entity_dict = cls.to_entity_dict(item, for_list=True)
                entity_dict = cls.apply_transient_overrides(
                    entity_dict, {'meta_db_display': True, 'for_list': True}, category=item.category
                )
                live_poster_url = entity_dict.get('poster_url') or item.poster_url or ''

                d = {
                    'id': item.id,
                    'category': item.category,
                    'code': item.code,
                    'ui_code': item.ui_code or item.code or '',
                    'originaltitle': item.originaltitle,
                    'sorttitle': item.sorttitle or item.title or item.originaltitle,
                    'site': item.site,
                    'title': item.title,
                    'poster_url': live_poster_url,
                    'created_time': item.created_time.strftime('%Y-%m-%d %H:%M:%S') if item.created_time else '',
                    'updated_time': item.updated_time.strftime('%Y-%m-%d %H:%M:%S') if item.updated_time else '',
                    'json_data': entity_dict
                }
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

            master_image_server_url = P.ModelSetting.get("jav_censored_image_server_url") or ""

            return {
                'success': True,
                'paging': paging,
                'list': item_list,
                'meta_db_use_ff_proxy': P.ModelSetting.get_bool("meta_db_use_ff_proxy"),
                'image_server_url': master_image_server_url.rstrip('/')
            }

        except Exception as e:
            logger.error(f"[MetaDB WebList Item] 에러 ({target_cat}): {e}")
            logger.error(traceback.format_exc())
            return {'success': False, 'paging': None, 'list': []}
        finally:
            sess.remove()


    @classmethod
    def delete_record(cls, code, category=None):
        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess:
            return False
        try:
            item = sess.query(MetaItem).filter_by(code=code).first()
            if not item:
                logger.warning(f"[MetaDB Delete] [{std_cat}] 삭제 대상 메타데이터 없음: {code}")
                return False

            title_log = item.originaltitle or item.title or code

            if P.ModelSetting.get_bool("meta_db_delete_user_images"):
                try:
                    stem = (item.ui_code or item.originaltitle or item.code).lower()
                    target_folder, _ = MetaImageUtil.get_server_folder_and_prefix(
                        item.domain, item.category, stem, studio=item.studio, year=item.year
                    )
                    if target_folder and os.path.exists(target_folder):
                        for f in os.listdir(target_folder):
                            if f.startswith(f"{stem}_p_user.") or f.startswith(f"{stem}_pl_user."):
                                try:
                                    os.remove(os.path.join(target_folder, f))
                                    logger.info(f"[MetaDB Delete] 유저 이미지 파일 삭제 완료: {f}")
                                except Exception as e_del_f:
                                    logger.debug(f"[MetaDB Delete] 파일 삭제 실패: {e_del_f}")
                except Exception as e_user_del:
                    logger.debug(f"[MetaDB Delete] 유저 이미지 정리 예외: {e_user_del}")

            sess.delete(item)
            sess.commit()
            cls.checkpoint_wal()
            logger.info(f"[MetaDB Delete] [{std_cat}] 메타데이터 삭제 완료: {code} ({title_log})")
            return True
        except Exception as e:
            logger.error(f"[MetaDB] delete_record 실패 ({code}): {e}")
            sess.rollback()
            return False
        finally:
            sess.remove()

    @classmethod
    def delete_records(cls, codes, category=None):
        """선택된 복수의 메타데이터 레코드를 일괄 삭제하고 관련 유저 이미지를 정리합니다."""
        if not codes:
            return False, 0

        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess:
            return False, 0

        try:
            if isinstance(codes, str):
                try:
                    code_list = json.loads(codes)
                except Exception:
                    code_list = [c.strip() for c in codes.split(',') if c.strip()]
            elif isinstance(codes, (list, set, tuple)):
                code_list = list(codes)
            else:
                code_list = []

            if not code_list:
                return False, 0

            delete_user_imgs = P.ModelSetting.get_bool("meta_db_delete_user_images")
            items = sess.query(MetaItem).filter(MetaItem.category == std_cat, MetaItem.code.in_(code_list)).all()
            deleted_count = 0

            for item in items:
                if delete_user_imgs:
                    try:
                        stem = (item.ui_code or item.originaltitle or item.code).lower()
                        target_folder, _ = MetaImageUtil.get_server_folder_and_prefix(
                            item.domain, item.category, stem, studio=item.studio, year=item.year
                        )
                        if target_folder and os.path.exists(target_folder):
                            for f in os.listdir(target_folder):
                                if f.startswith(f"{stem}_p_user.") or f.startswith(f"{stem}_pl_user."):
                                    try:
                                        os.remove(os.path.join(target_folder, f))
                                        logger.debug(f"[MetaDB Delete] 유저 이미지 파일 삭제: {f}")
                                    except Exception:
                                        pass
                    except Exception as e_del_img:
                        logger.debug(f"[MetaDB Delete] 이미지 파일 정리 예외: {e_del_img}")

                sess.delete(item)
                deleted_count += 1

            sess.commit()
            cls.checkpoint_wal()
            logger.info(f"[MetaDB Delete] [{std_cat}] 선택 항목 일괄 삭제 완료: 총 {deleted_count}건")
            return True, deleted_count

        except Exception as e:
            logger.error(f"[MetaDB] delete_records 실패: {e}")
            logger.error(traceback.format_exc())
            sess.rollback()
            return False, 0
        finally:
            sess.remove()

    @classmethod
    def clear_db(cls, category=None):
        if not category: return False, 0
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess: return False, 0

        logger.info(f"[MetaDB] [{std_cat}] 카테고리 전체 데이터 초기화 시작...")
        try:
            count = sess.query(MetaItem).filter_by(category=std_cat).delete()
            sess.commit()
            logger.info(f"[MetaDB] [{std_cat}] 데이터 삭제 완료 (총 {count}건)")
            return True, count
        except Exception as e:
            logger.error(f"[MetaDB] clear_db 실패 ({std_cat}): {e}")
            sess.rollback()
            return False, 0
        finally:
            sess.remove()

    @classmethod
    def checkpoint_wal(cls):
        cls.ensure_db_ready()
        try:
            if not cls._is_postgres:
                for dom, eng in cls._engines.items():
                    with eng.connect() as conn:
                        conn.execute(text('PRAGMA wal_checkpoint(TRUNCATE)'))
            return True
        except Exception as e:
            logger.error(f"[MetaDB] checkpoint_wal 에러: {e}")
            return False

    @classmethod
    def vacuum_db(cls):
        cls.ensure_db_ready()
        t_start = time.time()
        engine_label = "PostgreSQL VACUUM ANALYZE" if cls._is_postgres else "SQLite VACUUM & WAL TRUNCATE"
        logger.info(f"[MetaDB Vacuum] 데이터베이스 최적화 시작 ➔ {engine_label}")

        try:
            if cls._is_postgres:
                raw_conn = cls._engines['postgres'].raw_connection()
                raw_conn.set_isolation_level(0)
                cursor = raw_conn.cursor()
                cursor.execute("""
                    VACUUM ANALYZE meta_item;
                    VACUUM ANALYZE meta_person;
                    VACUUM ANALYZE meta_tag;
                    VACUUM ANALYZE meta_media;
                    VACUUM ANALYZE meta_fingerprint;
                    VACUUM ANALYZE meta_item_person_map;
                    VACUUM ANALYZE meta_item_tag_map;
                """)
                cursor.close()
                raw_conn.close()
            else:
                for dom, eng in cls._engines.items():
                    with eng.connect() as conn:
                        conn.execute(text('PRAGMA wal_checkpoint(TRUNCATE)'))
                        conn.execute(text('VACUUM'))

            elapsed = time.time() - t_start
            logger.info(f"[MetaDB Vacuum] 최적화 완료 (소요시간: {elapsed:.2f}초)")
            return True
        except Exception as e:
            logger.error(f"[MetaDB Vacuum] 최적화 실패: {e}")
            logger.error(traceback.format_exc())
            return False

    @classmethod
    def test_connection(cls, db_type, host, port, user, password, dbname):
        logger.info(f"[MetaDB ConnTest] DB 연결 테스트 시작: [{db_type}] Host/Socket: '{host}', Port: '{port}', User: '{user}', DB: '{dbname}'")
        try:
            if db_type == "postgres":
                try:
                    import psycopg2
                except ImportError:
                    msg = "psycopg2-binary 라이브러리가 설치되어 있지 않습니다. (pip install psycopg2-binary)"
                    logger.error(f"[MetaDB ConnTest] {msg}")
                    return False, msg

                target_host = str(host or '').strip() or 'postgres'

                # host 경로가 /로 시작하면 UNIX 도메인 소켓 연결로 처리
                if target_host.startswith('/'):
                    conn = psycopg2.connect(
                        host=target_host,
                        user=user,
                        password=password,
                        dbname=dbname,
                        connect_timeout=5
                    )
                    conn.close()
                    msg = f"PostgreSQL UNIX 소켓 연결 성공! ({target_host}/{dbname})"
                else:
                    target_port = int(port) if (port and str(port).isdigit()) else 5432
                    conn = psycopg2.connect(
                        host=target_host,
                        port=target_port,
                        user=user,
                        password=password,
                        dbname=dbname,
                        connect_timeout=5
                    )
                    conn.close()
                    msg = f"PostgreSQL TCP/IP 연결 성공! ({target_host}:{target_port}/{dbname})"

                logger.info(f"[MetaDB ConnTest] {msg}")
                return True, msg
            else:
                db_dir = P.ModelSetting.get("meta_db_sqlite_dir") or os.path.join(path_data, 'db', 'meta_db')
                os.makedirs(db_dir, exist_ok=True)
                msg = f"SQLite3 디렉토리 접근 확인 완료 ({db_dir})"
                logger.info(f"[MetaDB ConnTest] {msg}")
                return True, msg
        except Exception as e:
            logger.error(f"[MetaDB ConnTest] 연결 테스트 실패 ({host}:{port}/{dbname}): {e}")
            return False, str(e)

    @classmethod
    def pg_admin_action(cls, action, admin_user, admin_pass, host, port, target_db, target_user, target_pass):
        try:
            import psycopg2
            from psycopg2 import sql
        except ImportError:
            msg = "psycopg2-binary 라이브러리가 설치되어 있지 않습니다. (pip install psycopg2-binary)"
            logger.error(f"[MetaDB Admin] {msg}")
            return False, msg

        target_host = str(host or '').strip() or 'postgres'
        target_port = int(port) if (port and str(port).isdigit()) else 5432
        admin_user_clean = str(admin_user or '').strip() or 'postgres'
        admin_pass_clean = str(admin_pass or '').strip()

        logger.info(f"[MetaDB Admin] PostgreSQL 관리자 작업 요청: '{action}' -> Host: '{target_host}:{target_port}', AdminUser: '{admin_user_clean}', TargetDB: '{target_db}'")

        try:
            conn = psycopg2.connect(
                host=target_host,
                port=target_port,
                user=admin_user_clean,
                password=admin_pass_clean,
                dbname="postgres",
                connect_timeout=8
            )
            conn.autocommit = True
            cursor = conn.cursor()

            if action == "test_admin":
                cursor.execute("SELECT version();")
                ver = cursor.fetchone()[0]
                cursor.close()
                conn.close()
                msg = f"관리자 접속 성공! ({ver})"
                logger.info(f"[MetaDB Admin] {target_host}:{target_port} {msg}")
                return True, msg

            elif action == "create_db_and_user":
                logger.info(f"[MetaDB Admin] 데이터베이스 및 유저 생성 시작: DB='{target_db}', User='{target_user}'")
                cursor.execute(sql.SQL("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = %s) THEN
                            CREATE ROLE {} WITH LOGIN PASSWORD %s;
                        ELSE
                            ALTER ROLE {} WITH PASSWORD %s;
                        END IF;
                    END
                    $$;
                """).format(sql.Identifier(target_user), sql.Identifier(target_user)), (target_user, target_pass, target_pass))

                cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_db,))
                if not cursor.fetchone():
                    cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {};").format(sql.Identifier(target_db), sql.Identifier(target_user)))

                cursor.execute(sql.SQL("GRANT ALL PRIVILEGES ON DATABASE {} TO {};").format(sql.Identifier(target_db), sql.Identifier(target_user)))
                cursor.close()
                conn.close()

                # 생성된 신규 데이터베이스에 기본 메타 스키마 테이블들을 선제적으로 자동 생성
                try:
                    init_db_url = f"postgresql+psycopg2://{target_user}:{target_pass}@{target_host}:{target_port}/{target_db}?client_encoding=utf8"
                    connect_args = {"connect_timeout": 8}
                    if target_host.startswith('/'):
                        init_db_url = f"postgresql+psycopg2://{target_user}:{target_pass}@/{target_db}?client_encoding=utf8"
                        connect_args = {"host": target_host, "connect_timeout": 8}

                    init_engine = create_engine(init_db_url, poolclass=NullPool, connect_args=connect_args)
                    Base.metadata.create_all(bind=init_engine)
                    cls._auto_sync_table_columns(init_engine)
                    init_engine.dispose()
                    logger.info(f"[MetaDB Admin] 신규 DB '{target_db}' 내 메타 테이블 자동 생성 완료")
                except Exception as e_init_tbl:
                    logger.warning(f"[MetaDB Admin] 신규 DB 테이블 선제 생성 경고 (최초 접근 시 자동 생성됨): {e_init_tbl}")

                msg = f"데이터베이스 '{target_db}' 및 유저 '{target_user}' 생성 완료!"
                logger.info(f"[MetaDB Admin] {msg}")
                return True, msg

            elif action == "drop_db_and_user":
                logger.info(f"[MetaDB Admin] 데이터베이스 및 유저 삭제(DROP) 시작: DB='{target_db}', User='{target_user}'")
                cursor.execute("""
                    SELECT pg_terminate_backend(pid) 
                    FROM pg_stat_activity 
                    WHERE datname = %s AND pid <> pg_backend_pid();
                """, (target_db,))
                cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {};").format(sql.Identifier(target_db)))
                cursor.execute(sql.SQL("DROP ROLE IF EXISTS {};").format(sql.Identifier(target_user)))
                cursor.close()
                conn.close()
                msg = f"데이터베이스 '{target_db}' 및 유저 '{target_user}' 완전 삭제 완료."
                logger.info(f"[MetaDB Admin] {msg}")
                return True, msg

            else:
                cursor.close()
                conn.close()
                return False, f"알 수 없는 액션: {action}"

        except Exception as e:
            logger.error(f"[MetaDB Admin] 작업 실패 ({action}, Host: {target_host}:{target_port}): {e}")
            return False, str(e)

    @classmethod
    def import_database(cls, raw_paths, mode="update", progress_status=None):
        cls.ensure_db_ready()
        t_start = time.time()

        raw_lines = [p.strip() for p in raw_paths.split('\n') if p.strip()]
        all_targets = []

        for line in raw_lines:
            if '|' not in line:
                logger.warning(f"[MetaDB Import] 카테고리 접두사가 누락되어 건너뜁니다: '{line}'")
                continue

            prefix, path_part = line.split('|', 1)
            cat = prefix.strip().upper()
            ip = path_part.strip()

            if cat not in DOMAIN_MAP:
                logger.warning(f"[MetaDB Import] 지원하지 않는 카테고리입니다: '{cat}'")
                continue

            if not os.path.exists(ip):
                logger.warning(f"[MetaDB Import] 경로를 찾을 수 없음: [{cat}] {ip}")
                continue

            if os.path.isfile(ip) and ip.lower().endswith(('.db', '.sqlite')):
                all_targets.append(('db', cat, ip))
            else:
                json_files = []
                if os.path.isfile(ip) and ip.lower().endswith('.json'):
                    json_files.append(ip)
                else:
                    for root, _, files in os.walk(ip):
                        for f in files:
                            if f.lower().endswith('.json') and not f.endswith('.bak'):
                                json_files.append(os.path.join(root, f))
                all_targets.append(('json_dir', cat, json_files))

        total_units = 0
        for t_type, t_cat, t_val in all_targets:
            if t_type == 'json_dir': total_units += len(t_val)

            elif t_type == 'db':
                conn = None
                try:
                    conn = sqlite3.connect(t_val)
                    c = conn.cursor()
                    c.execute("SELECT name FROM sqlite_master WHERE type='table';")
                    tables = [r[0] for r in c.fetchall()]
                    if 'meta_item' in tables:
                        c.execute("SELECT count(*) FROM meta_item WHERE category = ?", (t_cat,))
                        total_units += c.fetchone()[0]
                except Exception as e:
                    logger.error(f"[MetaDB Import] 레코드 카운트 실패: {e}")
                finally:
                    if conn:
                        try: conn.close()
                        except Exception: pass

        logger.info(f"[MetaDB Import] 작업 시작 ➔ 총 처리 대상: {total_units:,}건, 모드: {mode}")
        if progress_status:
            progress_status.update({'total': total_units, 'current': 0, 'inserted': 0, 'updated': 0, 'skipped': 0, 'fail': 0})

        insert_count, update_count, skip_count, fail_count = 0, 0, 0, 0
        batch_size = 500
        current_idx = 0

        try:
            for t_type, t_cat, t_val in all_targets:
                if progress_status and progress_status.get('stop_flag'): break

                if t_type == 'db':
                    db_file = t_val
                    src_eng = None
                    s_sess = None
                    rows_to_process = []
                    try:
                        src_eng = create_engine(f"sqlite:///{db_file}", poolclass=NullPool)
                        SrcSess = sessionmaker(bind=src_eng)
                        s_sess = SrcSess()
                        rows_to_process = [(t_cat, sm.code, cls.to_entity_dict(sm)) for sm in s_sess.query(MetaItem).filter_by(category=t_cat).all()]
                    finally:
                        if s_sess:
                            try: s_sess.close()
                            except Exception: pass
                        if src_eng:
                            try: src_eng.dispose()
                            except Exception: pass

                    sess, domain, std_cat = cls.get_session_and_domain(t_cat)
                    person_sess, _, _ = cls.get_session_and_domain('PERSON')
                    batch_processed = 0

                    try:
                        for r_cat, r_code, jd in rows_to_process:
                            if progress_status and progress_status.get('stop_flag'): break
                            current_idx += 1
                            if progress_status:
                                progress_status['current'] = current_idx
                                progress_status['current_code'] = jd.get('originaltitle') or r_code

                            existing = cls.get_metadata(r_code, category=r_cat)
                            if existing and mode == 'missing':
                                skip_count += 1
                            else:
                                saved = cls.save_metadata(r_cat, jd, target_session=sess, target_person_session=person_sess)
                                if saved:
                                    if existing: update_count += 1
                                    else: insert_count += 1
                                    batch_processed += 1
                                else:
                                    fail_count += 1

                            if progress_status:
                                progress_status['inserted'] = insert_count
                                progress_status['updated'] = update_count
                                progress_status['skipped'] = skip_count
                                progress_status['fail'] = fail_count

                            if batch_processed >= batch_size:
                                sess.commit()
                                if person_sess: person_sess.commit()
                                batch_processed = 0

                            if current_idx % 500 == 0 or current_idx == total_units:
                                elapsed = time.time() - t_start
                                speed = (current_idx / elapsed) if elapsed > 0 else 0
                                pct = (current_idx / total_units * 100) if total_units > 0 else 100.0
                                rem_sec = int((total_units - current_idx) / speed) if speed > 0 else 0
                                m, s = divmod(rem_sec, 60)
                                h, m = divmod(m, 60)
                                eta_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
                                cur_label = jd.get('originaltitle') or r_code
                                logger.info(
                                    f"[MetaDB Import] 진행률: {current_idx:,}/{total_units:,} ({pct:.1f}%) | "
                                    f"신규: {insert_count:,}, 갱신: {update_count:,}, 건너뜀: {skip_count:,}, 실패: {fail_count} | "
                                    f"속도: {speed:.1f}건/초 (남은시간: {eta_str}) | 현재: {cur_label}"
                                )

                        if batch_processed > 0:
                            sess.commit()
                            if person_sess: person_sess.commit()

                    finally:
                        if sess: sess.remove()
                        if person_sess: person_sess.remove()

                elif t_type == 'json_dir':
                    sess, domain, std_cat = cls.get_session_and_domain(t_cat)
                    person_sess, _, _ = cls.get_session_and_domain('PERSON')
                    batch_processed = 0

                    try:
                        for jf in t_val:
                            if progress_status and progress_status.get('stop_flag'): break
                            current_idx += 1
                            r_code = ""
                            try:
                                with open(jf, 'r', encoding='utf-8') as file: jd = json.load(file)
                                r_code = jd.get('code') or os.path.splitext(os.path.basename(jf))[0]
                                jd['code'] = r_code

                                if progress_status:
                                    progress_status['current'] = current_idx
                                    progress_status['current_code'] = jd.get('originaltitle') or r_code

                                is_valid_new_json = True

                                # 기본 필수 데이터 구조 검증
                                if not isinstance(jd, dict) or not r_code:
                                    is_valid_new_json = False

                                # 배우 데이터가 있는 경우 name_org 필드 존재 여부 확인
                                if is_valid_new_json and 'actor' in jd and isinstance(jd['actor'], list):
                                    for a in jd['actor']:
                                        if isinstance(a, dict):
                                            if not a.get('name_org') and not a.get('name_ko'):
                                                is_valid_new_json = False
                                                break
                                        elif not isinstance(a, str):
                                            is_valid_new_json = False
                                            break

                                if not is_valid_new_json:
                                    logger.debug(f"[MetaDB Import] 새 규격 미충족(구버전) JSON 파일 건너뜀: {jf}")
                                    skip_count += 1
                                    if progress_status:
                                        progress_status['skipped'] = skip_count
                                    continue

                                existing = cls.get_metadata(r_code, category=t_cat)
                                if existing and mode == 'missing':
                                    skip_count += 1
                                else:
                                    saved = cls.save_metadata(t_cat, jd, target_session=sess, target_person_session=person_sess)
                                    if saved:
                                        if existing: update_count += 1
                                        else: insert_count += 1
                                        batch_processed += 1
                                    else:
                                        fail_count += 1
                            except Exception as e_read_jf:
                                fail_count += 1

                            if progress_status:
                                progress_status['inserted'] = insert_count
                                progress_status['updated'] = update_count
                                progress_status['skipped'] = skip_count
                                progress_status['fail'] = fail_count

                            if batch_processed >= batch_size:
                                sess.commit()
                                if person_sess: person_sess.commit()
                                batch_processed = 0

                            if current_idx % 500 == 0 or current_idx == total_units:
                                elapsed = time.time() - t_start
                                speed = (current_idx / elapsed) if elapsed > 0 else 0
                                pct = (current_idx / total_units * 100) if total_units > 0 else 100.0
                                rem_sec = int((total_units - current_idx) / speed) if speed > 0 else 0
                                m, s = divmod(rem_sec, 60)
                                h, m = divmod(m, 60)
                                eta_str = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
                                cur_label = (jd.get('originaltitle') or r_code) if 'jd' in locals() else r_code
                                logger.info(
                                    f"[MetaDB Import] 진행률: {current_idx:,}/{total_units:,} ({pct:.1f}%) | "
                                    f"신규: {insert_count:,}, 갱신: {update_count:,}, 건너뜀: {skip_count:,}, 실패: {fail_count} | "
                                    f"속도: {speed:.1f}건/초 (남은시간: {eta_str}) | 현재: {cur_label}"
                                )

                        if batch_processed > 0:
                            sess.commit()
                            if person_sess: person_sess.commit()

                    finally:
                        if sess: sess.remove()
                        if person_sess: person_sess.remove()

            cls.checkpoint_wal()
            elapsed = time.time() - t_start
            final_msg = f"병합 완료! (총 {total_units:,}건 중 신규: {insert_count:,}건, 갱신: {update_count:,}건, 건너뜀: {skip_count:,}건, 실패: {fail_count}건, 소요시간: {elapsed:.2f}초)"
            logger.info(f"[MetaDB Import] {final_msg}")
            return True, final_msg
        except Exception as e:
            logger.error(f"[MetaDB Import] 오류: {e}")
            return False, str(e)

    @classmethod
    def export_database(cls, category_mode="all", target_category=None, is_sanitized=False):
        """메타데이터 DB를 Standalone SQLite 파일로 내보냅니다."""
        cls.ensure_db_ready()
        t_start = time.time()
        exp_sess = None
        exp_engine = None
        sess = None
        try:
            tmp_dir = os.path.join(path_data, 'tmp')
            os.makedirs(tmp_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            cat_suffix = str(target_category).upper() if category_mode == 'current' else "ALL"
            san_suffix = "_clean" if is_sanitized else "_full"
            filename = f"metadata_{cat_suffix}_{timestamp}{san_suffix}.db"
            filepath = os.path.join(tmp_dir, filename)

            exp_engine = create_engine(f"sqlite:///{filepath}", poolclass=NullPool)
            Base.metadata.create_all(bind=exp_engine)
            ExpSession = sessionmaker(bind=exp_engine)
            exp_sess = ExpSession()

            sess, _, std_cat = cls.get_session_and_domain(target_category or 'JAV_CEN')
            if not sess:
                return False, "소스 DB 세션 획득 실패", 0

            query = sess.query(MetaItem)
            if category_mode == 'current':
                query = query.filter_by(category=std_cat)

            total_records = query.count()
            export_type_label = "공유용 정제 Export (Clean - 사이트 원본 데이터 보존)" if is_sanitized else "전체 백업 Export (Full)"
            logger.info(f"[MetaDB Export] {export_type_label} 시작 ➔ 총 {total_records:,}건")

            records = query.all()
            batch_size = 500
            processed = 0
            count = 0

            for idx, m in enumerate(records, 1):
                m_dict = cls.to_entity_dict(m)

                if is_sanitized:
                    m_dict['poster_url'] = ''
                    m_dict['landscape_url'] = ''
                    m_dict['thumb'] = []
                    m_dict['fanart'] = []
                    m_dict['extras'] = []
                    m_dict['trailer_url'] = ''

                    clean_extra = copy.deepcopy(m_dict.get('extra_info') or {})
                    clean_extra.pop('local_img_url', None)
                    clean_extra.pop('actor_cache', None)
                    m_dict['extra_info'] = clean_extra

                cls.save_metadata(m.category, m_dict, target_session=exp_sess)
                processed += 1
                count += 1

                if processed >= batch_size:
                    exp_sess.commit()
                    processed = 0

            if processed > 0:
                exp_sess.commit()

            # 독립 백업 파일에 인물(MetaPerson) 마스터 데이터도 함께 추출
            person_sess, _, _ = cls.get_session_and_domain('PERSON')
            if person_sess:
                try:
                    all_persons = person_sess.query(MetaPerson).all()
                    for p in all_persons:
                        p_copy = MetaPerson(
                            domain=p.domain,
                            name_org=p.name_org,
                            name_ko=p.name_ko,
                            name_en=p.name_en,
                            other_names=p.other_names,
                            aliases=p.aliases,
                            person_type=p.person_type,
                            person_idx=p.person_idx,
                            media_src=copy.deepcopy(p.media_src or {}),
                            works=copy.deepcopy(p.works or {}),
                            extra_info=copy.deepcopy(p.extra_info or {})
                        )
                        exp_sess.add(p_copy)
                    exp_sess.commit()
                except Exception as e_exp_p:
                    logger.debug(f"[MetaDB Export] 인물 데이터 백업 예외: {e_exp_p}")
                finally:
                    person_sess.remove()

            elapsed = time.time() - t_start
            logger.info(f"[MetaDB Export] 완료: {filename} (총 {count:,}건, 소요시간: {elapsed:.2f}초)")
            return True, filename, count
        except Exception as e:
            logger.error(f"[MetaDB Export] 오류: {e}")
            logger.error(traceback.format_exc())
            return False, str(e), 0
        finally:
            if exp_sess:
                try: exp_sess.close()
                except Exception: pass
            if exp_engine:
                try: exp_engine.dispose()
                except Exception: pass
            if sess:
                try: sess.remove()
                except Exception: pass


    @classmethod
    def transfer_database(cls, source_type, target_type, mode="merge", progress_status=None):
        cls.ensure_db_ready()
        t_start = time.time()
        logger.info(f"[MetaDB Transfer] DB 데이터 복제 시작: {source_type.upper()} ➔ {target_type.upper()} (정책: {mode})")

        src_engines = []
        src_sessions = []
        tgt_engines = {}
        tgt_sessions = {}
        pg_tgt_engine = None
        pg_tgt_session = None

        try:
            # 소스 엔진 및 세션 준비
            if source_type == "sqlite":
                db_dir = P.ModelSetting.get("meta_db_sqlite_dir") or os.path.join(path_data, 'db', 'meta_db')
                for db_file in set(v[1] for v in DOMAIN_MAP.values()):
                    full_p = os.path.join(db_dir, db_file)
                    if os.path.exists(full_p):
                        eng = create_engine(f"sqlite:///{full_p}", poolclass=NullPool)
                        src_engines.append(eng)
                        src_sessions.append(sessionmaker(bind=eng)())
            else:
                pg_user = P.ModelSetting.get("meta_db_pg_user") or "metadata"
                pg_pass = P.ModelSetting.get("meta_db_pg_pass") or ""
                pg_name = P.ModelSetting.get("meta_db_pg_name") or "metadata"
                pg_conn_type = P.ModelSetting.get("meta_db_pg_conn_type") or "tcp"

                if pg_conn_type == "socket":
                    socket_dir = (P.ModelSetting.get("meta_db_pg_socket_dir") or "/var/run/postgresql").strip()
                    src_eng = create_engine(f"postgresql+psycopg2://{pg_user}:{pg_pass}@/{pg_name}?client_encoding=utf8", poolclass=NullPool, connect_args={"host": socket_dir})
                else:
                    pg_host = P.ModelSetting.get("meta_db_pg_host") or "postgres"
                    pg_port = P.ModelSetting.get("meta_db_pg_port") or "5432"
                    src_eng = create_engine(f"postgresql+psycopg2://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_name}?client_encoding=utf8", poolclass=NullPool)

                src_engines.append(src_eng)
                src_sessions.append(sessionmaker(bind=src_eng)())

            # 목적지 엔진 및 세션 준비
            if target_type == "postgres":
                pg_user = P.ModelSetting.get("meta_db_pg_user") or "metadata"
                pg_pass = P.ModelSetting.get("meta_db_pg_pass") or ""
                pg_name = P.ModelSetting.get("meta_db_pg_name") or "metadata"
                pg_conn_type = P.ModelSetting.get("meta_db_pg_conn_type") or "tcp"

                if pg_conn_type == "socket":
                    socket_dir = (P.ModelSetting.get("meta_db_pg_socket_dir") or "/var/run/postgresql").strip()
                    pg_tgt_engine = create_engine(f"postgresql+psycopg2://{pg_user}:{pg_pass}@/{pg_name}?client_encoding=utf8", poolclass=NullPool, connect_args={"host": socket_dir})
                else:
                    pg_host = P.ModelSetting.get("meta_db_pg_host") or "postgres"
                    pg_port = P.ModelSetting.get("meta_db_pg_port") or "5432"
                    pg_tgt_engine = create_engine(f"postgresql+psycopg2://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_name}?client_encoding=utf8", poolclass=NullPool)

                Base.metadata.create_all(bind=pg_tgt_engine)
                cls._auto_sync_table_columns(pg_tgt_engine)
                pg_tgt_session = sessionmaker(bind=pg_tgt_engine)()
            else:
                db_dir = P.ModelSetting.get("meta_db_sqlite_dir") or os.path.join(path_data, 'db', 'meta_db')
                os.makedirs(db_dir, exist_ok=True)
                for dom in set(v[0] for v in DOMAIN_MAP.values()):
                    db_filename = next(v[1] for v in DOMAIN_MAP.values() if v[0] == dom)
                    eng = create_engine(f"sqlite:///{os.path.join(db_dir, db_filename)}", poolclass=NullPool)
                    Base.metadata.create_all(bind=eng)
                    cls._auto_sync_table_columns(eng)
                    tgt_engines[dom] = eng
                    tgt_sessions[dom] = sessionmaker(bind=eng)()

            # 목적지 완전 초기화(Clean) 정책 처리
            if mode == "clean":
                logger.info("[MetaDB Transfer] 목적지 DB 데이터 완전 초기화 시작...")
                if target_type == "postgres" and pg_tgt_session:
                    pg_tgt_session.query(MetaFingerprint).delete(synchronize_session=False)
                    pg_tgt_session.query(MetaMedia).delete(synchronize_session=False)
                    pg_tgt_session.query(MetaItemTagMap).delete(synchronize_session=False)
                    pg_tgt_session.query(MetaItemPersonMap).delete(synchronize_session=False)
                    pg_tgt_session.query(MetaItem).delete(synchronize_session=False)
                    pg_tgt_session.query(MetaPerson).delete(synchronize_session=False)
                    pg_tgt_session.query(MetaTag).delete(synchronize_session=False)
                    pg_tgt_session.commit()
                elif target_type == "sqlite":
                    for ts in tgt_sessions.values():
                        ts.query(MetaFingerprint).delete(synchronize_session=False)
                        ts.query(MetaMedia).delete(synchronize_session=False)
                        ts.query(MetaItemTagMap).delete(synchronize_session=False)
                        ts.query(MetaItemPersonMap).delete(synchronize_session=False)
                        ts.query(MetaItem).delete(synchronize_session=False)
                        ts.query(MetaPerson).delete(synchronize_session=False)
                        ts.query(MetaTag).delete(synchronize_session=False)
                        ts.commit()

            # 소스에서 작품 및 인물 데이터 수집
            all_source_items = []
            all_source_persons = []
            for s_sess in src_sessions:
                try:
                    all_source_items.extend(s_sess.query(MetaItem).all())
                except Exception:
                    pass
                try:
                    all_source_persons.extend(s_sess.query(MetaPerson).all())
                except Exception:
                    pass

            total_count = len(all_source_items) + len(all_source_persons)
            logger.info(f"[MetaDB Transfer] 총 {total_count:,}건 (작품: {len(all_source_items):,}건, 인물: {len(all_source_persons):,}건) 복제 시작...")
            if progress_status:
                progress_status.update({
                    'total': total_count,
                    'current': 0,
                    'inserted': 0,
                    'updated': 0,
                    'skipped': 0,
                    'fail': 0
                })

            batch_size = 500
            processed = 0
            inserted_count = 0
            updated_count = 0
            skipped_count = 0
            fail_count = 0
            current_progress = 0

            # 작품(MetaItem) 복제 진행
            for m in all_source_items:
                if progress_status and progress_status.get('stop_flag'):
                    break
                current_progress += 1
                if progress_status:
                    progress_status['current_code'] = m.originaltitle or m.code
                try:
                    m_dict = cls.to_entity_dict(m)
                    target_dom = DOMAIN_MAP.get(m.category, (m.domain or 'jav_cen', ''))[0]
                    current_tgt_session = pg_tgt_session if target_type == "postgres" else tgt_sessions.get(target_dom)
                    current_person_session = pg_tgt_session if target_type == "postgres" else tgt_sessions.get('person')

                    exists = current_tgt_session.query(MetaItem).filter_by(code=m.code).first()

                    # missing 모드: 목적지에 이미 존재하면 건너뜀 카운트 증가 후 통과
                    if mode == "missing" and exists:
                        skipped_count += 1
                        if progress_status:
                            progress_status['current'] = current_progress
                            progress_status['skipped'] = skipped_count
                        continue

                    saved = cls.save_metadata(m.category, m_dict, target_session=current_tgt_session, target_person_session=current_person_session)
                    if saved:
                        if exists and mode == "merge":
                            updated_count += 1
                        else:
                            inserted_count += 1
                        processed += 1
                    else:
                        fail_count += 1
                except Exception:
                    fail_count += 1

                if progress_status:
                    progress_status['current'] = current_progress
                    progress_status['inserted'] = inserted_count
                    progress_status['updated'] = updated_count
                    progress_status['fail'] = fail_count

                # 웹 요청 처리를 위한 GIL 양보 (1초 상태 조회가 멈추지 않도록 보장)
                if current_progress % 20 == 0:
                    time.sleep(0.002)

                if processed >= batch_size:
                    if target_type == "postgres" and pg_tgt_session: pg_tgt_session.commit()
                    elif target_type == "sqlite":
                        for ts in tgt_sessions.values(): ts.commit()
                    processed = 0

            # 인물(MetaPerson) 복제 진행
            tgt_person_session = pg_tgt_session if target_type == "postgres" else tgt_sessions.get('person')
            for p in all_source_persons:
                if progress_status and progress_status.get('stop_flag'):
                    break
                current_progress += 1
                if progress_status:
                    progress_status['current_code'] = p.name_ko or p.name_org or p.person_idx
                try:
                    p_exists = tgt_person_session.query(MetaPerson).filter_by(domain=p.domain, person_idx=p.person_idx).first() if p.person_idx else None

                    if mode == "missing" and p_exists:
                        skipped_count += 1
                        if progress_status:
                            progress_status['current'] = current_progress
                            progress_status['skipped'] = skipped_count
                        continue

                    p_target = p_exists or MetaPerson()
                    if not p_exists:
                        tgt_person_session.add(p_target)

                    p_target.domain = p.domain
                    p_target.name_org = p.name_org
                    p_target.name_ko = p.name_ko
                    p_target.name_en = p.name_en
                    p_target.other_names = p.other_names
                    p_target.aliases = p.aliases
                    p_target.person_type = p.person_type
                    p_target.person_idx = p.person_idx
                    p_target.media_src = copy.deepcopy(p.media_src or {})
                    p_target.works = copy.deepcopy(p.works or {})
                    p_target.extra_info = copy.deepcopy(p.extra_info or {})

                    if p_exists and mode == "merge":
                        updated_count += 1
                    else:
                        inserted_count += 1
                    processed += 1
                except Exception:
                    fail_count += 1

                if progress_status:
                    progress_status['current'] = current_progress
                    progress_status['inserted'] = inserted_count
                    progress_status['updated'] = updated_count
                    progress_status['fail'] = fail_count

                if current_progress % 20 == 0:
                    time.sleep(0.002)

                if processed >= batch_size:
                    tgt_person_session.commit()
                    processed = 0

            if target_type == "postgres" and pg_tgt_session:
                pg_tgt_session.commit()
            elif target_type == "sqlite":
                for ts in tgt_sessions.values():
                    ts.commit()

            elapsed = time.time() - t_start

            # 전송 모드에 따른 명확한 결과 메시지 생성
            if mode == "missing":
                final_msg = f"복제 완료! (총 {total_count:,}건 중 신규: {inserted_count:,}건, 건너뜀: {skipped_count:,}건, 실패: {fail_count}건, {elapsed:.2f}초)"
            elif mode == "merge":
                final_msg = f"병합 완료! (총 {total_count:,}건 중 신규: {inserted_count:,}건, 갱신: {updated_count:,}건, 건너뜀: {skipped_count:,}건, 실패: {fail_count}건, {elapsed:.2f}초)"
            else:
                final_msg = f"전체 복제 완료! (총 {total_count:,}건 중 성공: {inserted_count:,}건, 실패: {fail_count}건, {elapsed:.2f}초)"

            logger.info(f"[MetaDB Transfer] {final_msg}")
            return True, final_msg
        except Exception as e:
            logger.error(f"[MetaDB Transfer] 오류: {e}")
            logger.error(traceback.format_exc())
            return False, str(e)
        finally:
            for s_sess in src_sessions:
                try: s_sess.close()
                except Exception: pass
            for s_eng in src_engines:
                try: s_eng.dispose()
                except Exception: pass
            if pg_tgt_session:
                try: pg_tgt_session.close()
                except Exception: pass
            if pg_tgt_engine:
                try: pg_tgt_engine.dispose()
                except Exception: pass
            for ts in tgt_sessions.values():
                try: ts.close()
                except Exception: pass
            for te in tgt_engines.values():
                try: te.dispose()
                except Exception: pass


    def plugin_load(self):
        try:
            for key, value in self.db_default.items():
                if P.ModelSetting.get(key) is None:
                    P.ModelSetting.set(key, value)

            if not P.ModelSetting.get_bool(f"{self.name}_use"):
                logger.info(f"[{self.name}] Meta DB is disabled by user setting.")
                return

            self.init_engines()
            latest_path, latest_ver = self.find_latest_jav_actors_db()

            if P.ModelSetting.get_bool(f"{self.name}_person_jav_auto_sync_actors") and latest_path:
                last_synced = P.ModelSetting.get(f"{self.name}_person_jav_last_synced_version") or "0"

                needs_initial_sync = False
                sess, _, _ = self.get_session_and_domain('PERSON')
                if sess:
                    try:
                        jav_person_count = sess.query(MetaPerson).filter_by(domain="JAV").count()
                        if jav_person_count == 0:
                            needs_initial_sync = True
                    except Exception as e_cnt:
                        logger.debug(f"[MetaDB] 인물 레코드 수 확인 실패: {e_cnt}")
                    finally:
                        sess.remove()

                if needs_initial_sync:
                    logger.info(f"[MetaDB] 인물 DB 비어있음 감지 ➔ 배포 파일({latest_ver})로 자동 초기 동기화를 시작합니다...")
                    threading.Thread(target=self.sync_jav_actors_db, daemon=True).start()
                elif latest_ver > last_synced:
                    logger.info(f"[MetaDB] 시작 시 새 배포 DB 파일 감지 ({last_synced} ➔ {latest_ver}). 자동 동기화를 진행합니다...")
                    threading.Thread(target=self.sync_jav_actors_db, daemon=True).start()

            logger.debug(f"[{self.name}] Universal Metadata DB Infrastructure Loaded.")
        except Exception as e:
            logger.error(f"[{self.name}] plugin_load 에러: {e}")

    def setting_save(self, req):
        try:
            change_list = []
            form_keys = set(req.form.keys())

            for key in form_keys:
                if key in ['sub', 'package_name', 'module_name']: continue
                if key in self.db_default:
                    val = req.form[key].strip()
                    try:
                        if P.ModelSetting.set(key, val):
                            change_list.append(key)
                    except Exception:
                        # 프레임워크 ModelSetting.set이 신규 키에서 NoneType 에러를 낼 경우 직접 생성 저장
                        try:
                            from framework import db as framework_db
                            new_setting = P.ModelSetting(key, val)
                            framework_db.session.add(new_setting)
                            framework_db.session.commit()
                            change_list.append(key)
                        except Exception as e_direct_save:
                            logger.error(f"[{self.name}] 신규 설정 키 직접 저장 실패 ({key}): {e_direct_save}")

            for key, default_val in self.db_default.items():
                if default_val in ['True', 'False'] and key not in form_keys:
                    if P.ModelSetting.set(key, 'False'):
                        change_list.append(key)

            self.setting_save_after(change_list)
            return jsonify(True)
        except Exception as e:
            logger.error(f"[{self.name}] setting_save 에러: {e}")
            return jsonify(False)

    def setting_save_after(self, change_list):
        if any('engine' in k or 'pg_' in k for k in change_list):
            self.init_engines()

    @staticmethod
    def resolve_category_context(req, sub=None, command=None):
        form = getattr(req, 'form', {}) if req is not None else {}
        req_sub = str(sub or form.get('sub') or '').strip().lower()
        req_command = str(command or form.get('command') or '').strip().lower()
        req_category = str(form.get('category') or '').strip().upper()
        req_domain = str(form.get('search_domain') or '').strip().upper()
        req_list_type = str(form.get('list_type') or '').strip().lower()
        arg1 = str(form.get('arg1') or '')

        if req_list_type == 'person':
            return 'PERSON', req_sub, req_command
        if req_list_type == 'meta':
            return (req_category or 'JAV_CEN'), req_sub, req_command
        if 'search_domain=' in arg1 or 'category=PERSON' in arg1:
            return 'PERSON', req_sub, req_command
        if req_category == 'PERSON' or req_domain in ['JAV', 'WESTERN', 'ALL']:
            return 'PERSON', req_sub, req_command
        if req_sub in ['person', 'jav', 'western']:
            return 'PERSON', req_sub, req_command
        if req_command in ['person_search', 'person_web_list', 'person_save', 'person_delete', 'person_clear', 'person_sync_jav_actors', 'person_version_status']:
            return 'PERSON', req_sub, req_command
        if req_sub in ['web_list', 'meta_list', 'list'] or req_command in ['web_list', 'meta_list', 'list']:
            return (req_category or 'JAV_CEN'), req_sub, req_command
        return (req_category or 'JAV_CEN'), req_sub, req_command

    def process_ajax(self, sub, req):
        try:
            command = req.form.get('command')
            normalized_cat, req_sub, req_command = self.resolve_category_context(req, sub, command)

            if req_command == 'db_transfer_status' or command == 'db_transfer_status':
                return jsonify({'ret': 'success', 'data': self.transfer_status})
            if req_command == 'db_import_status' or command == 'db_import_status':
                return jsonify({'ret': 'success', 'data': self.import_status})

            if normalized_cat == 'PERSON':
                is_list_query = (req_command in ['web_list', 'list', 'person_web_list'] or not req_command)
                if is_list_query:
                    default_domain = req.form.get('search_domain', 'ALL') or 'ALL'
                    return jsonify(self.person_web_list(req, default_domain=default_domain))

                if req_command in ['person_search', 'person_get_detailed', 'person_get_works', 'person_save', 'person_delete', 'person_clear', 'person_sync_jav_actors', 'person_version_status', 'person_sub_set_master', 'person_sub_split', 'db_vacuum']:
                    res = self.process_command(req_command, req.form.get('arg1'), req.form.get('arg2'), req.form.get('arg3'), req)
                    return res if res is not None else jsonify({'ret': 'error', 'msg': 'PERSON 처리 결과 없음'})

            is_meta_list_request = (
                (req.form.get('list_type') or '').strip().lower() == 'meta' or
                req_command in ['web_list', 'meta_list', 'list'] or
                req_sub in ['web_list', 'meta_list', 'list'] or
                any(key in req.form for key in ['page', 'page_size', 'search_word', 'search_site', 'search_status', 'search_order'])
            )
            if is_meta_list_request:
                category = (req.form.get('category') or getattr(self, 'category', None) or 'JAV_CEN').upper()
                return jsonify(self.web_list(req, category=category))

            # 모든 모듈 커맨드 동적 위임
            target_cmd = req_command or command
            if target_cmd:
                res = self.process_command(target_cmd, req.form.get('arg1'), req.form.get('arg2'), req.form.get('arg3'), req)
                if res is not None:
                    return res

            res = super(ModuleMetaDb, self).process_ajax(sub, req)
            if res is not None:
                return res

            return jsonify({'ret': 'error', 'msg': f'미처리된 AJAX 요청: sub={sub}, command={command}'})

        except Exception as e:
            logger.error(f"[{self.name}] process_ajax 에러: {e}")
            logger.error(traceback.format_exc())
            return jsonify({'ret': 'error', 'msg': str(e)})

    def process_command(self, command, arg1, arg2, arg3, req):
        try:
            if command == 'person_search':
                kw = arg1 or ''
                domain = arg2 or 'ALL'
                opts = {}
                if arg3:
                    try:
                        opts = json.loads(arg3) if isinstance(arg3, str) and arg3.startswith('{') else {}
                    except Exception:
                        opts = {}
                results = self.person_search(kw, domain=domain, options=opts)
                return jsonify({'ret': 'success', 'data': results})

            elif command in ['person_get_detailed', 'person_get_works']:
                person_id = arg1 or ''
                domain = arg2 or 'JAV'
                person_detail = self.get_person_detailed_info(person_id, domain=domain)
                if person_detail:
                    return jsonify({'ret': 'success', 'data': person_detail, 'works_detailed': person_detail.get('works_detailed', {})})
                return jsonify({'ret': 'error', 'msg': '인물 상세 정보를 찾을 수 없습니다.'})

            elif command == 'person_save':
                p_data = json.loads(arg1) if arg1 else {}
                success, msg = self.person_save(p_data)
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == 'person_crop_save':
                person_id = arg1
                crop_data = arg2
                upload_payload = arg3
                target_url = req.form.get('image_url') or ''
                domain_val = req.form.get('domain') or 'JAV'

                logger.info(f"[MetaDB AJAX] person_crop_save 요청 수신 -> PersonID: '{person_id}', Domain: '{domain_val}', TargetURL: '{target_url}'")

                img_b64 = None
                if upload_payload:
                    try:
                        p_json = json.loads(upload_payload)
                        if isinstance(p_json, dict):
                            img_b64 = p_json.get('data')
                            if not target_url and p_json.get('url'):
                                target_url = p_json['url']
                            logger.debug(f"[MetaDB AJAX] Payload 파싱 완료 (Type: '{p_json.get('type')}', Length: {len(img_b64) if img_b64 else 0})")
                    except Exception as e_pjson:
                        img_b64 = upload_payload
                        logger.debug(f"[MetaDB AJAX] Payload 원본 문자열 사용: {e_pjson}")

                success, result_msg, updated_person_obj = MetaImageUtil.save_user_cropped_person_image(
                    person_identifier=person_id,
                    crop_data_or_base64=crop_data,
                    image_base64_data=img_b64,
                    image_url=target_url,
                    domain=domain_val
                )

                person_dict = None
                if success and updated_person_obj:
                    p_media = copy.deepcopy(updated_person_obj.media_src if isinstance(updated_person_obj.media_src, dict) else {})
                    p_works = copy.deepcopy(updated_person_obj.works if isinstance(updated_person_obj.works, dict) else {})
                    extra_data = copy.deepcopy(updated_person_obj.extra_info or {})
                    raw_local = str(p_media.get('local_img_path') or '').strip()
                    if raw_local:
                        resolved_url = self.format_actor_thumb_url(raw_local, domain=updated_person_obj.domain)
                        p_media['local_img_url'] = resolved_url
                        extra_data['local_img_url'] = resolved_url

                    person_dict = {
                        'id': updated_person_obj.id,
                        'domain': updated_person_obj.domain,
                        'name_org': updated_person_obj.name_org,
                        'name_ko': updated_person_obj.name_ko or '',
                        'name_en': updated_person_obj.name_en or '',
                        'other_names': updated_person_obj.other_names or '',
                        'aliases': updated_person_obj.aliases or [],
                        'thumb': self.resolve_person_active_thumb(updated_person_obj),
                        'person_idx': updated_person_obj.person_idx or '',
                        'person_type': updated_person_obj.person_type or 'actor',
                        'media_src': p_media,
                        'works': p_works,
                        'works_count': sum(len(v) for v in p_works.values()) if isinstance(p_works, dict) else 0,
                        'extra_info': extra_data
                    }
                    logger.info(f"[MetaDB AJAX] person_crop_save 처리 성공 -> Code: {updated_person_obj.person_idx}, NewThumb: '{person_dict['thumb']}'")
                else:
                    logger.warning(f"[MetaDB AJAX] person_crop_save 처리 실패 -> 사유: '{result_msg}'")

                return jsonify({
                    'ret': 'success' if success else 'error',
                    'msg': '프로필 사진이 성공적으로 저장되었습니다.' if success else result_msg,
                    'new_url': result_msg if success else None,
                    'person': person_dict
                })

            elif command == 'person_delete':
                success = self.person_delete(arg1)
                return jsonify({'ret': 'success' if success else 'error'})

            elif command == 'db_delete_selected':
                target_cat = arg2 or getattr(self, 'category', 'JAV_CEN')
                success, count = self.delete_records(arg1, category=target_cat)
                msg = f"{count}건의 메타데이터가 삭제되었습니다." if success else "선택 항목 삭제 실패"
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == 'person_sub_set_master':
                target_id_raw = str(arg1 or '').strip()
                target_sub_id = str(arg2 or '').strip()
                sess, _, _ = self.get_session_and_domain('PERSON')
                if not sess or not target_sub_id:
                    logger.warning(f"[MetaDB Person Master Change] 필수 인자 누락: arg1='{arg1}', arg2='{arg2}'")
                    return jsonify({'ret': 'error', 'msg': '필수 인자가 누락되었습니다.'})
                try:
                    p_rec = None
                    if target_id_raw.isdigit():
                        p_rec = sess.query(MetaPerson).filter_by(id=int(target_id_raw)).first()
                    if not p_rec and target_id_raw:
                        p_rec = sess.query(MetaPerson).filter_by(person_idx=target_id_raw).first()
                    if not p_rec:
                        p_rec = sess.query(MetaPerson).filter(
                            or_(
                                MetaPerson.person_idx == target_sub_id,
                                cast(MetaPerson.extra_info, Text).ilike(f'%"{target_sub_id}"%')
                            )
                        ).first()

                    if not p_rec:
                        logger.warning(f"[MetaDB Person Master Change] 대상 레코드 찾을 수 없음: target_id='{target_id_raw}', sub_id='{target_sub_id}'")
                        return jsonify({'ret': 'error', 'msg': f'[{target_sub_id}] 인물의 마스터 레코드를 찾을 수 없습니다.'})

                    old_idx = p_rec.person_idx
                    extra_dict = copy.deepcopy(p_rec.extra_info or {})
                    sub_list = extra_dict.get('merged_sub_actors', [])
                    target_sub = next((s for s in sub_list if s.get('actor_id') == target_sub_id), None)

                    if not target_sub:
                        target_sub = {
                            'actor_id': target_sub_id,
                            'name_org': p_rec.name_org,
                            'name_ko': p_rec.name_ko,
                            'name_en': p_rec.name_en,
                            'site_img_url': p_rec.thumb,
                            'local_img_path': (p_rec.media_src or {}).get('local_img_path', ''),
                            'google_fileid': (p_rec.media_src or {}).get('google_fileid', '')
                        }

                    # 주 레코드 컬럼 및 미디어 스왑
                    p_rec.person_idx = target_sub_id
                    if target_sub.get('name_org'): p_rec.name_org = target_sub['name_org']
                    if target_sub.get('name_ko'): p_rec.name_ko = target_sub['name_ko']
                    if target_sub.get('name_en'): p_rec.name_en = target_sub['name_en']

                    media_dict = copy.deepcopy(p_rec.media_src or {})
                    if target_sub.get('local_img_path'): media_dict['local_img_path'] = target_sub['local_img_path']
                    if target_sub.get('google_fileid'): media_dict['google_fileid'] = target_sub['google_fileid']
                    if target_sub.get('site_img_url'): media_dict['site_img_url'] = target_sub['site_img_url']
                    p_rec.media_src = media_dict
                    p_rec.thumb = self.resolve_person_active_thumb(p_rec)

                    for spec_key in ['birth', 'height', 'body_size', 'bra_size', 'debut', 'info_url', 'agency', 'blood', 'hobby', 'specialty']:
                        if target_sub.get(spec_key) is not None:
                            extra_dict[spec_key] = target_sub[spec_key]

                    alt_list = [x for x in extra_dict.get('alt_actor_indices', []) if x != target_sub_id]
                    alt_list.insert(0, target_sub_id)
                    if old_idx and old_idx not in alt_list:
                        alt_list.append(old_idx)
                    extra_dict['alt_actor_indices'] = alt_list

                    p_rec.extra_info = extra_dict
                    sess.commit()
                    self.checkpoint_wal()
                    self._cached_actors_map = None
                    logger.info(f"[MetaDB Person Master Change] 대표 인물 전환 성공: {old_idx} ➔ {target_sub_id} (이름: {p_rec.name_ko or p_rec.name_org})")
                    return jsonify({'ret': 'success', 'msg': f'[{target_sub_id}] 인물이 새로운 대표로 지정되었습니다.'})
                except Exception as e:
                    sess.rollback()
                    logger.error(f"[MetaDB Person Master Change] 오류: {e}")
                    logger.error(traceback.format_exc())
                    return jsonify({'ret': 'error', 'msg': str(e)})
                finally:
                    sess.remove()

            elif command == 'person_sub_split':
                target_id_raw = str(arg1 or '').strip()
                target_sub_id = str(arg2 or '').strip()
                sess, _, _ = self.get_session_and_domain('PERSON')
                if not sess or not target_sub_id:
                    logger.warning(f"[MetaDB Person Split] 필수 인자 누락: arg1='{arg1}', arg2='{arg2}'")
                    return jsonify({'ret': 'error', 'msg': '필수 인자가 누락되었습니다.'})
                try:
                    p_rec = None
                    if target_id_raw.isdigit():
                        p_rec = sess.query(MetaPerson).filter_by(id=int(target_id_raw)).first()
                    if not p_rec and target_id_raw:
                        p_rec = sess.query(MetaPerson).filter_by(person_idx=target_id_raw).first()
                    if not p_rec:
                        p_rec = sess.query(MetaPerson).filter(
                            or_(
                                MetaPerson.person_idx == target_sub_id,
                                cast(MetaPerson.extra_info, Text).ilike(f'%"{target_sub_id}"%')
                            )
                        ).first()

                    if not p_rec:
                        logger.warning(f"[MetaDB Person Split] 대상 레코드 찾을 수 없음: target_id='{target_id_raw}', sub_id='{target_sub_id}'")
                        return jsonify({'ret': 'error', 'msg': f'[{target_sub_id}] 인물의 마스터 레코드를 찾을 수 없습니다.'})

                    extra_dict = copy.deepcopy(p_rec.extra_info or {})
                    sub_list = extra_dict.get('merged_sub_actors', [])
                    target_sub = next((s for s in sub_list if s.get('actor_id') == target_sub_id), None)
                    if not target_sub:
                        return jsonify({'ret': 'error', 'msg': f'서브 인물 {target_sub_id} 정보를 찾을 수 없습니다.'})

                    # 마스터 레코드에서 서브 인물 제외 및 재병합 방지 등록
                    extra_dict['merged_sub_actors'] = [s for s in sub_list if s.get('actor_id') != target_sub_id]
                    alt_list = extra_dict.get('alt_actor_indices', [])
                    extra_dict['alt_actor_indices'] = [x for x in alt_list if x != target_sub_id]

                    split_ex = set(extra_dict.get('split_exclusions', []))
                    split_ex.add(target_sub_id)
                    extra_dict['split_exclusions'] = list(split_ex)
                    p_rec.extra_info = extra_dict

                    # 보존되어 있던 원본 데이터로 독립 MetaPerson 신규 생성
                    new_standalone = MetaPerson(
                        domain=p_rec.domain,
                        name_org=target_sub.get('name_org') or p_rec.name_org,
                        name_ko=target_sub.get('name_ko') or p_rec.name_ko,
                        name_en=target_sub.get('name_en') or p_rec.name_en,
                        person_idx=target_sub_id,
                        person_type="actor",
                        media_src={
                            'local_img_path': target_sub.get('local_img_path', ''),
                            'google_fileid': target_sub.get('google_fileid', ''),
                            'site_img_url': target_sub.get('site_img_url', ''),
                            'site_img_urls': [target_sub.get('site_img_url')] if target_sub.get('site_img_url') else []
                        },
                        works={},
                        extra_info={
                            'birth': target_sub.get('birth', ''),
                            'height': target_sub.get('height'),
                            'body_size': target_sub.get('body_size', ''),
                            'bra_size': target_sub.get('bra_size', ''),
                            'debut': target_sub.get('debut', ''),
                            'info_url': target_sub.get('info_url', ''),
                            'agency': target_sub.get('agency', ''),
                            'blood': target_sub.get('blood', ''),
                            'hobby': target_sub.get('hobby', ''),
                            'specialty': target_sub.get('specialty', ''),
                            'source_origin': 'split_standalone',
                            'split_exclusions': [p_rec.person_idx],
                            'merged_sub_actors': [target_sub],
                            'alt_actor_indices': [target_sub_id]
                        }
                    )
                    new_standalone.thumb = self.resolve_person_active_thumb(new_standalone)

                    sess.add(new_standalone)
                    sess.commit()
                    self.checkpoint_wal()
                    self._cached_actors_map = None
                    logger.info(f"[MetaDB Person Split] 서브 인물 그룹 분리 성공: {target_sub_id} ({new_standalone.name_ko}) ➔ 독립 레코드 생성")
                    return jsonify({'ret': 'success', 'msg': f'[{target_sub_id}] 인물이 단독 레코드로 분리되었습니다.'})
                except Exception as e:
                    sess.rollback()
                    logger.error(f"[MetaDB Person Split] 오류: {e}")
                    logger.error(traceback.format_exc())
                    return jsonify({'ret': 'error', 'msg': str(e)})
                finally:
                    sess.remove()

            elif command == 'person_clear':
                domain = arg1 or 'ALL'
                success, count = self.person_clear_db(domain)
                msg = f"인물 DB 초기화 완료: {count}건 삭제됨 ({domain})" if success else "인물 DB 초기화 실패"
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == 'person_sync_jav_actors':
                success, msg = self.sync_jav_actors_db()
                target_path, file_ver = self.find_latest_jav_actors_db()
                last_ver = P.ModelSetting.get("person_jav_last_synced_version") or "0"
                version_info = f"파일 버전: {file_ver} / DB 반영 버전: {last_ver}"
                return jsonify({
                    'ret': 'success' if success else 'warning',
                    'msg': msg,
                    'version_info': version_info,
                    'file_version': file_ver,
                    'db_version': last_ver
                })

            elif command == 'person_version_status':
                target_path, detected_ver = self.find_latest_jav_actors_db()
                file_ver = str(detected_ver).strip() if (detected_ver and detected_ver != "0") else "0"
                last_ver = str(P.ModelSetting.get("meta_db_person_jav_last_synced_version") or "0").strip()
                auto_sync = P.ModelSetting.get_bool("meta_db_person_jav_auto_sync_actors")

                def clean_ver_num(v):
                    digits = re.sub(r'\D', '', str(v))
                    return int(digits) if digits else 0

                file_ver_num = clean_ver_num(file_ver)
                last_ver_num = clean_ver_num(last_ver)

                is_empty_db = False
                sess, _, _ = self.get_session_and_domain('PERSON')
                if sess:
                    try:
                        if sess.query(MetaPerson).filter_by(domain="JAV").count() == 0:
                            is_empty_db = True
                    except Exception:
                        pass
                    finally:
                        sess.remove()

                has_update = bool(file_ver_num > 0 and (file_ver_num > last_ver_num or is_empty_db))

                version_info = f"파일 버전: {file_ver if file_ver != '0' else '없음'} / DB 반영 버전: {last_ver if not is_empty_db else '0 (초기화됨)'}"

                return jsonify({
                    'ret': 'success' if file_ver != "0" else 'warning',
                    'version_info': version_info,
                    'file_version': file_ver,
                    'db_version': last_ver,
                    'auto_sync': auto_sync,
                    'has_update': has_update,
                    'file_path': target_path or ''
                })

            elif command == 'db_transfer_status':
                return jsonify({'ret': 'success', 'data': self.transfer_status})

            elif command == 'db_import_status':
                return jsonify({'ret': 'success', 'data': self.import_status})

            elif command == 'db_import_stop':
                self.import_status['stop_flag'] = True
                return jsonify({'ret': 'success', 'msg': '임포트 중단을 요청했습니다.'})

            # code, ui_code, originaltitle 3개 필드 대조 및 카테고리 전방위 폴백 탐색
            elif command == 'get_meta_by_code':
                target_code = (arg1 or '').strip()
                raw_cat = (arg2 or '').strip().upper()

                if not target_code:
                    return jsonify({'ret': 'error', 'msg': '조회할 코드가 없습니다.'})

                target_cat = 'JAV_CEN'
                if raw_cat in DOMAIN_MAP:
                    target_cat = raw_cat
                elif raw_cat in ['STASHDB', 'TPDB'] or target_code.startswith(('WS', 'WP')):
                    target_cat = 'WESTERN'
                elif raw_cat in ['1PONDO', '10MUSUME', 'PACO', 'HEYZO', 'CARIB', 'FC2COM'] or target_code.startswith('E'):
                    target_cat = 'JAV_UNCEN'
                elif target_code.startswith('C'):
                    target_cat = 'JAV_CEN'

                categories_to_try = [target_cat]
                for c_cand in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'MOVIE', 'KTV', 'FTV']:
                    if c_cand not in categories_to_try:
                        categories_to_try.append(c_cand)

                found_item = None
                found_session = None

                # 코드에서 숫자 및 알파벳 추출 (예: CMABP-576 -> ABP, 576)
                num_match = re.search(r'(\d+)', target_code)
                num_part = num_match.group(1) if num_match else ''
                alpha_match = re.search(r'([A-Za-z]+)', target_code)
                alpha_part = alpha_match.group(1) if alpha_match else ''

                for try_cat in categories_to_try:
                    sess, _, std_cat = self.get_session_and_domain(try_cat)
                    if not sess:
                        continue
                    try:
                        # code, ui_code, originaltitle 정확 일치 (대소문자 무시)
                        item = sess.query(MetaItem).filter(
                            or_(
                                func.lower(MetaItem.code) == target_code.lower(),
                                func.lower(MetaItem.ui_code) == target_code.lower(),
                                func.lower(MetaItem.originaltitle) == target_code.lower()
                            )
                        ).first()

                        # ILIKE 부분 포함 검색
                        if not item:
                            search_code_like = f"%{target_code}%"
                            item = sess.query(MetaItem).filter(
                                or_(
                                    MetaItem.code.ilike(search_code_like),
                                    MetaItem.ui_code.ilike(search_code_like),
                                    MetaItem.originaltitle.ilike(search_code_like)
                                )
                            ).first()

                        # 품번 분해 매칭 (알파벳 레이블 + 숫자 동시 포함 검색)
                        if not item and num_part and len(alpha_part) >= 2:
                            pure_alpha = alpha_part[-3:] if len(alpha_part) >= 3 else alpha_part
                            item = sess.query(MetaItem).filter(
                                and_(
                                    or_(
                                        MetaItem.originaltitle.ilike(f"%{pure_alpha}%"),
                                        MetaItem.ui_code.ilike(f"%{pure_alpha}%"),
                                        MetaItem.code.ilike(f"%{pure_alpha}%")
                                    ),
                                    or_(
                                        MetaItem.originaltitle.ilike(f"%{num_part}%"),
                                        MetaItem.ui_code.ilike(f"%{num_part}%"),
                                        MetaItem.code.ilike(f"%{num_part}%")
                                    )
                                )
                            ).first()

                        if item:
                            found_item = item
                            found_session = sess
                            break
                    except Exception as e_find:
                        logger.error(f"[MetaDB get_meta_by_code] 조회 예외 ({try_cat}): {e_find}")
                    finally:
                        if not found_item:
                            sess.remove()

                if found_item and found_session:
                    try:
                        data = self.to_entity_dict(found_item)
                        poster_val = data.get('poster_url') or found_item.poster_url or ''
                        if not poster_val and data.get('thumb'):
                            for t_cand in data['thumb']:
                                if isinstance(t_cand, dict) and t_cand.get('aspect') == 'poster':
                                    poster_val = t_cand.get('value') or ''
                                    break
                            if not poster_val:
                                poster_val = data['thumb'][0].get('value', '')

                        row_dict = {
                            'code': found_item.code,
                            'ui_code': found_item.ui_code,
                            'title': found_item.title,
                            'originaltitle': found_item.originaltitle,
                            'sorttitle': found_item.sorttitle or found_item.title or found_item.originaltitle,
                            'poster_url': poster_val,
                            'landscape_url': data.get('landscape_url') or '',
                            'trailer_url': data.get('trailer_url') or '',
                            'site': found_item.site,
                            'category': found_item.category,
                            'json_data': data
                        }
                        logger.debug(f"[MetaDB get_meta_by_code] 작품 조회 성공: [{found_item.category}] {found_item.code} ({found_item.title})")
                        return jsonify({'ret': 'success', 'data': row_dict})
                    except Exception as e_to_dict:
                        logger.error(f"[MetaDB get_meta_by_code] to_entity_dict 에러: {e_to_dict}")
                        return jsonify({'ret': 'error', 'msg': str(e_to_dict)})
                    finally:
                        found_session.remove()

                logger.warning(f"[MetaDB get_meta_by_code] 작품 조회 실패: '{target_code}'")
                return jsonify({'ret': 'error', 'msg': f'[{target_code}] 작품 데이터를 조회하지 못했습니다.'})

            elif command == "make_preview_clip":
                code = arg1
                cat = arg2 or self.category
                params = {}
                if arg3:
                    try: params = json.loads(arg3) if isinstance(arg3, str) else arg3
                    except: pass

                video_path = params.get('video_path', '').strip()
                logger.info(f"[MetaPreview] 수동 프리뷰 클립 생성 요청 수신 -> Code: '{code}', Cat: '{cat}', VideoPath: '{video_path}'")

                if not video_path:
                    msg = "동영상 파일 경로가 전달되지 않았습니다."
                    logger.warning(f"[MetaPreview] {msg}")
                    return jsonify({'ret': 'error', 'msg': msg})

                if not os.path.exists(video_path):
                    msg = f"컨테이너 내부에서 지정된 동영상 파일을 찾을 수 없습니다: '{video_path}'"
                    logger.error(f"[MetaPreview] {msg}")
                    return jsonify({'ret': 'error', 'msg': msg})

                from .util_preview import MetaPreviewUtil
                success, result = MetaPreviewUtil.process_preview_workflow(code, video_path, category=cat, force=True)
                if success:
                    msg = f"프리뷰 클립 생성 및 등록 완료: [{cat}] {code}"
                    logger.info(f"[MetaPreview] {msg}")
                    return jsonify({'ret': 'success', 'msg': msg, 'clip': result})
                else:
                    msg = f"프리뷰 클립 생성 실패 ({code}): {result}"
                    logger.error(f"[MetaPreview] {msg}")
                    return jsonify({'ret': 'error', 'msg': msg})

            elif command == "delete_preview_clip":
                code = arg1
                cat = arg2 or self.category
                sess, _, std_cat = self.get_session_and_domain(cat)
                if not sess:
                    return jsonify({'ret': 'error', 'msg': '세션 획득 실패'})

                try:
                    item = sess.query(MetaItem).filter_by(code=code).first()
                    if not item:
                        return jsonify({'ret': 'error', 'msg': '작품을 찾을 수 없습니다.'})

                    extra = dict(item.extra_info or {})
                    clip_info = extra.pop('preview_clip', None)
                    if clip_info:
                        from .util_preview import MetaPreviewUtil
                        MetaPreviewUtil.delete_preview_clip(clip_info, category=std_cat)

                    item.extra_info = extra
                    sess.commit()
                    self.checkpoint_wal()
                    logger.debug(f"[MetaPreview] [{std_cat}] {code} 프리뷰 클립 정보 및 파일 삭제 완료")
                    return jsonify({'ret': 'success', 'msg': '프리뷰 클립 파일 및 등록 정보가 완전히 삭제되었습니다.'})
                except Exception as e:
                    sess.rollback()
                    return jsonify({'ret': 'error', 'msg': str(e)})
                finally:
                    sess.remove()

            elif command == "db_test_connection":
                db_type = arg1 or req.form.get('db_type', 'sqlite')
                params = {}
                if arg2:
                    try:
                        params = json.loads(arg2) if isinstance(arg2, str) else arg2
                    except Exception:
                        params = {}

                host = params.get('host') or req.form.get('host')
                port = params.get('port') or req.form.get('port')
                user = params.get('user') or req.form.get('user')
                password = params.get('password') or req.form.get('password')
                dbname = params.get('dbname') or req.form.get('dbname')

                success, msg = self.test_connection(db_type, host, port, user, password, dbname)
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == "db_pg_admin_action":
                action = arg1 or 'test_admin'
                params = {}
                if arg2:
                    try:
                        params = json.loads(arg2) if isinstance(arg2, str) else arg2
                    except Exception:
                        params = {}

                admin_user = params.get('admin_user') or req.form.get('admin_user')
                admin_pass = params.get('admin_pass') or req.form.get('admin_pass')
                host = params.get('host') or req.form.get('host')
                port = params.get('port') or req.form.get('port')
                target_db = params.get('target_db') or req.form.get('target_db')
                target_user = params.get('target_user') or req.form.get('target_user')
                target_pass = params.get('target_pass') or req.form.get('target_pass')

                success, msg = self.pg_admin_action(
                    action, admin_user, admin_pass, host, port, target_db, target_user, target_pass
                )
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == "db_pg_admin_action":
                success, msg = self.pg_admin_action(
                    arg1, req.form.get('admin_user'), req.form.get('admin_pass'),
                    req.form.get('host'), req.form.get('port'),
                    req.form.get('target_db'), req.form.get('target_user'), req.form.get('target_pass')
                )
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == "db_transfer_start":
                if self.transfer_status['is_running']:
                    return jsonify({'ret': 'warning', 'msg': '이미 DB 전송 작업이 진행 중입니다.'})
                src_type, tgt_type = arg1, arg2
                mode = 'merge'
                if arg3:
                    try:
                        p_data = json.loads(arg3) if isinstance(arg3, str) else arg3
                        mode = p_data.get('mode', 'merge')
                    except Exception:
                        pass
                t = threading.Thread(target=self._run_transfer_worker, args=(src_type, tgt_type, mode))
                t.daemon = True
                t.start()
                return jsonify({'ret': 'success', 'msg': f'[{src_type.upper()} ➔ {tgt_type.upper()}] 데이터 복제를 시작했습니다.'})

            elif command == "db_transfer_stop":
                self.transfer_status['stop_flag'] = True
                return jsonify({'ret': 'success', 'msg': 'DB 전송 중단을 요청했습니다.'})

            elif command == "db_import":
                raw_paths, mode = arg1, arg2
                t = threading.Thread(target=self._run_import_worker, args=(raw_paths, mode))
                t.daemon = True
                t.start()
                return jsonify({'ret': 'success', 'msg': '백그라운드에서 임포트 작업을 시작했습니다.'})

            elif command == 'db_vacuum':
                success = self.vacuum_db()
                return jsonify({'ret': 'success' if success else 'error', 'msg': 'DB 최적화 완료' if success else '최적화 실패'})

            elif command == 'db_export':
                cat_mode = arg1 or 'all'
                is_clean = (arg2 == 'true')
                success, filename_or_msg, count = self.export_database(category_mode=cat_mode, is_sanitized=is_clean)
                if success:
                    return jsonify({'ret': 'success', 'filename': filename_or_msg, 'msg': f'Export 완료: {filename_or_msg} (총 {count:,}건)'})
                else:
                    return jsonify({'ret': 'error', 'msg': f'Export 실패: {filename_or_msg}'})

            return jsonify({'ret': 'error', 'msg': f'알 수 없는 명령: {command}'})
        except Exception as e:
            logger.error(f"[{self.name}] process_command 에러: {e}")
            return jsonify({'ret': 'error', 'msg': str(e)})

    def _run_transfer_worker(self, src_type, tgt_type, mode='merge'):
        self.transfer_status.update({
            'is_running': True,
            'status': '작업 중',
            'mode': mode,
            'total': 0,
            'current': 0,
            'inserted': 0,
            'updated': 0,
            'skipped': 0,
            'fail': 0,
            'current_code': '',
            'stop_flag': False
        })

        try:
            success, msg = self.transfer_database(src_type, tgt_type, mode=mode, progress_status=self.transfer_status)
            self.transfer_status['status'] = '완료' if success else f'실패: {msg}'
        except Exception as e:
            self.transfer_status['status'] = f'오류: {e}'
        finally:
            self.transfer_status['is_running'] = False

    def _run_import_worker(self, raw_paths, mode):
        self.import_status.update({'is_running': True, 'status': '작업 중', 'total': 0, 'current': 0, 'inserted': 0, 'updated': 0, 'skipped': 0, 'fail': 0, 'current_code': '', 'stop_flag': False})
        try:
            success, msg = self.import_database(raw_paths, mode=mode, progress_status=self.import_status)
            self.import_status['status'] = '완료' if success else f'실패: {msg}'
        except Exception as e:
            self.import_status['status'] = f'오류: {e}'
        finally:
            self.import_status['is_running'] = False

    def process_normal(self, sub, req):
        if sub == "db_download":
            filename = req.args.get('filename')
            if filename:
                filepath = os.path.join(path_data, 'tmp', filename)
                if os.path.exists(filepath):
                    return send_file(filepath, as_attachment=True, download_name=filename)
            return "File not found.", 404

        return None

    def process_api(self, sub, req):
        try:
            if sub in ["make_preview_clip", "delete_preview_clip"]:
                meta_module = P.get_module('meta_db')
                if not meta_module:
                    return jsonify({'ret': 'error', 'msg': 'meta_db 모듈을 찾을 수 없습니다.'}), 400

                params = req.get_json(silent=True) or {}
                code = params.get('code') or req.values.get('code', '')
                cat = params.get('cat') or req.values.get('cat', '') or getattr(self, 'category', '')
                video_path = (params.get('video_path') or req.values.get('video_path', '')).strip()

                if not code:
                    return jsonify({'ret': 'error', 'msg': 'code 파라미터가 누락되었습니다.'}), 400

                if sub == "make_preview_clip":
                    arg3 = json.dumps({'video_path': video_path})
                    return meta_module.process_command("make_preview_clip", code, cat, arg3, req)
                else:
                    return meta_module.process_command("delete_preview_clip", code, cat, "", req)

        except Exception as e:
            logger.error(f"Exception in process_api (sub={sub}): {e}")
            return jsonify({'ret': 'exception', 'msg': str(e)}), 500
