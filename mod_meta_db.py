# -*- coding: utf-8 -*-
import os
import re
import copy
import json
import time
import sqlite3
import shutil
import threading
import traceback
import math
import unicodedata
from datetime import datetime
from io import BytesIO
from urllib.parse import urlparse, urlencode, quote
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
from .util_runtime import storage_logger

logger = storage_logger(logger)
from support import SupportYaml
from framework import db
from .util_metadata import MetaUtil
from .util_migration import (
    backup_postgres_database,
    backup_sqlite_database,
    restore_postgres_database,
    restore_sqlite_database,
    load_json_state,
    save_json_state,
)

try:
    from framework import celery
except Exception:
    celery = None

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
    """통합 인물/배우 마스터 테이블 (v1 정식 물리 컬럼 규격)"""
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

    # 신체 스펙 및 프로필 물리 컬럼
    gender = Column(String(15), default="female", index=True)
    birth_date = Column(String(20), index=True)
    birth_year = Column(Integer, index=True)
    debut_date = Column(String(20))
    height = Column(Integer, index=True)
    bust = Column(Integer)
    waist = Column(Integer)
    hip = Column(Integer)
    body_size = Column(String(30))
    cup_size = Column(String(10), index=True)
    blood_type = Column(String(5))
    agency = Column(String(100), index=True)
    country = Column(String(50), index=True)
    info_url = Column(String(500))

    # 미디어 자산 상태 (인덱스 기반 고속 조회)
    has_photo = Column(Boolean, default=False, index=True)
    thumb_url = Column(String(500))
    local_path = Column(String(255))
    gdrive_fileid = Column(String(255), index=True)
    custom_gdrive_fileid = Column(String(255), index=True)
    custom_local_path = Column(String(255))
    official_cached_gdrive_fileid = Column(String(255))
    official_cached_local_path = Column(String(500))
    official_remote_path = Column(String(500))
    custom_remote_path = Column(String(500))
    site_img_url = Column(String(500))
    photo_urls = Column(JSON, default=list)

    # 다중 ID 병합 및 통계 집계 컬럼
    merged_count = Column(Integer, default=1, index=True)
    alt_indices = Column(JSON, default=list)
    merged_details = Column(JSON, default=list)
    works_count = Column(Integer, default=0, index=True)

    # 수동 수정 보호 및 하위 호환 페이로드
    source_data = Column(JSON, default=dict)
    manual_data = Column(JSON, default=dict)
    media_src = Column(JSON, default=dict)
    works = Column(JSON, default=dict)
    extra_info = Column(JSON, default=dict)
    created_time = Column(DateTime, default=datetime.now, index=True)
    updated_time = Column(DateTime, default=datetime.now, onupdate=datetime.now)


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

if celery:
    @celery.task(bind=True)
    def task_meta_db_batch_refresh(self, target_scope, target_codes, search_params, refresh_mode, delay_val, action, batch_type='item', batch_options=None):
        meta_module = P.get_module('meta_db')
        if meta_module:
            meta_module.run_batch_refresh_worker(target_scope, target_codes, search_params, refresh_mode, delay_val, action, batch_type=batch_type, batch_options=batch_options)


# Keep the task symbol importable when FF disables Celery.
task_meta_db_legacy_batch = None
if celery:
    @celery.task
    def task_meta_db_legacy_batch(category, kind, options):
        from .util_metadata import MetaUtil
        MetaUtil.run_legacy_batch(category, kind, options)


task_meta_db_maintenance = None
if celery:
    @celery.task
    def task_meta_db_maintenance(operation, options, job_id):
        meta_module = P.get_module('meta_db')
        if meta_module:
            meta_module._run_maintenance_worker(operation, options, job_id)


class MetaTag(Base):
    """장르, 태그 마스터 테이블 (원문 name_org 기준 복합 고유 키 관리)"""
    __tablename__ = 'meta_tag'

    id = Column(Integer, primary_key=True, autoincrement=True)
    domain = Column(String(20), nullable=False, default="JAV", index=True)
    name = Column(String(100), nullable=False, index=True)
    name_org = Column(String(100), nullable=False, index=True)
    tag_type = Column(String(30), default="genre", index=True)

    __table_args__ = (
        Index('ix_meta_tag_domain_org_type', 'domain', 'name_org', 'tag_type', unique=True),
    )


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
    gdrive_fileid = Column(String(255), index=True)
    relative_path = Column(String(500))
    source = Column(String(30), index=True)
    checksum = Column(String(128))
    updated_time = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MetaSchemaVersion(Base):
    """실제 메타데이터 DB의 논리적 스키마 버전 및 분산 락 상태"""
    __tablename__ = 'meta_schema_version'

    id = Column(Integer, primary_key=True, autoincrement=True)
    schema_name = Column(String(30), nullable=False, unique=True)
    schema_version = Column(Integer, nullable=False, default=0)
    migration_id = Column(String(100))
    migration_state = Column(String(30), nullable=False, default='legacy')
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    last_error = Column(Text)
    backup_path = Column(String(500))
    lock_token = Column(String(100))
    locked_at = Column(DateTime)
    locked_by = Column(String(100))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MetaSchemaMigrationHistory(Base):
    """마이그레이션 및 복원 실행 감사 원장(Append-Only) 테이블"""
    __tablename__ = 'meta_schema_migration_history'

    id = Column(Integer, primary_key=True, autoincrement=True)
    revision_id = Column(String(100), nullable=False, index=True)
    from_version = Column(Integer, nullable=False)
    to_version = Column(Integer, nullable=False)
    phase = Column(String(30), nullable=False)
    status = Column(String(30), nullable=False)
    started_at = Column(DateTime, nullable=False, index=True)
    completed_at = Column(DateTime)
    duration_sec = Column(Float)
    backup_path = Column(String(500))
    error_message = Column(Text)
    details_json = Column(JSON)


# =========================================================================
# 3. 통합 메타 DB 인프라 컨트롤러 클래스: ModuleMetaDb
# =========================================================================

class ModuleMetaDb(PluginModuleBase):
    META_DB_SCHEMA_NAME = 'meta_db'
    META_DB_SCHEMA_VERSION = 1
    PERSON_SOURCE_FIELDS = (
        'name_org', 'name_ko', 'name_en', 'other_names',
        'birth', 'height', 'body_size', 'bra_size', 'debut',
        'agency', 'blood', 'hobby', 'specialty', 'info_url', 'gender', 'country',
    )
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

        # 영상(item)과 인물(person) 독립 배치를 위한 2트랙 상태 딕셔너리
        def create_initial_batch_status(default_mode, default_category):
            return {
                'is_running': False,
                'status': '대기 중',
                'mode': default_mode,
                'category': default_category,
                'total': 0,
                'current': 0,
                'success': 0,
                'fail': 0,
                'skipped': 0,
                'current_code': '',
                'stop_flag': False,
                'can_resume': False,
                'resume_index': 0,
                'target_items': [],
                'elapsed_str': '',
                'eta_str': ''
            }

        self.batch_status = {
            'item': create_initial_batch_status('missing', 'AV_ALL'),
            'person': create_initial_batch_status('person_image_sync', 'PERSON')
        }
        self.batch_job_status = self.batch_status['item']

        self.batch_log_subscribers = {'item': [], 'person': []}
        self.batch_log_history = {'item': [], 'person': []}
        self._batch_log_lock = threading.Lock()
        self.batch_state_files = {
            'item': os.path.join(path_data, 'db', f"{P.package_name}_batch_item_state.json"),
            'person': os.path.join(path_data, 'db', f"{P.package_name}_batch_person_state.json")
        }
        self.batch_state_file = self.batch_state_files['item']

        self.meta_schema_log_subscribers = []
        self._meta_schema_log_lock = threading.Lock()

        # 통합 마이그레이션 오케스트레이터 초기화 및 상태 바인딩
        from .migrations import MigrationOrchestrator
        self.orchestrator = MigrationOrchestrator(self)
        self.meta_migration_status = self.orchestrator.status
        self.maintenance_orchestrator = MigrationOrchestrator(self, maintenance=True)
        self._maintenance_start_lock = threading.Lock()


    @classmethod
    def _empty_person_media(cls):
        return {
            'official': {
                'site_img_url': '',
                'site_img_urls': [],
                'google_fileid': '',
                'official_cached_google_fileid': '',
                'cached_local_img_path': '',
                'remote_img_path': '',
                'local_img_path': '',
            },
            'custom': {
                'custom_user_google_fileid': '',
                'remote_img_path': '',
                'local_img_path': '',
            },
        }

    @staticmethod
    def _is_http_url(value):
        value = str(value or '').strip()
        return value.startswith(('http://', 'https://'))

    @classmethod
    def normalize_person_media(cls, media_src, domain='JAV'):
        """v0 평면 media_src와 v1 중첩 media_src를 읽기 전용으로 정규화한다."""
        normalized = cls._empty_person_media()
        raw = media_src if isinstance(media_src, dict) else {}
        official = raw.get('official') if isinstance(raw.get('official'), dict) else {}
        custom = raw.get('custom') if isinstance(raw.get('custom'), dict) else {}

        # v1 값
        for key in normalized['official']:
            if key in official:
                normalized['official'][key] = official[key]
        for key in normalized['custom']:
            if key in custom:
                normalized['custom'][key] = custom[key]

        # v0 평면 값은 손실 없이 v1 의미로 변환한다.
        if not official:
            normalized['official']['site_img_url'] = str(raw.get('site_img_url') or '').strip()
            site_urls = raw.get('site_img_urls')
            normalized['official']['site_img_urls'] = (
                [str(x).strip() for x in site_urls if str(x).strip()]
                if isinstance(site_urls, list) else []
            )
            normalized['official']['google_fileid'] = str(raw.get('google_fileid') or '').strip()
            # 기존 user_google_fileid는 공식 캐시 ID로 정의한다.
            normalized['official']['official_cached_google_fileid'] = str(
                raw.get('official_cached_google_fileid') or raw.get('user_google_fileid') or ''
            ).strip()

            legacy_local = str(raw.get('local_img_path') or '').strip()
            if '_user.' in legacy_local.lower():
                normalized['custom']['local_img_path'] = legacy_local
            else:
                normalized['official']['local_img_path'] = legacy_local

        if not custom:
            normalized['custom']['custom_user_google_fileid'] = str(
                raw.get('custom_user_google_fileid') or ''
            ).strip()
            legacy_custom_local = str(raw.get('local_img_path_custom') or '').strip()
            if legacy_custom_local:
                normalized['custom']['local_img_path'] = legacy_custom_local

        if str(domain or '').upper() == 'WESTERN':
            normalized['official']['google_fileid'] = ''
            # Western has no GDS local path; preserve its legacy private cache separately.
            if not normalized['official']['cached_local_img_path']:
                normalized['official']['cached_local_img_path'] = normalized['official']['local_img_path']

        normalized['official']['site_img_url'] = str(
            normalized['official'].get('site_img_url') or ''
        ).strip()
        normalized['official']['site_img_urls'] = [
            str(x).strip() for x in (normalized['official'].get('site_img_urls') or [])
            if str(x).strip()
        ]
        for section in ('official', 'custom'):
            for key in normalized[section]:
                if key != 'site_img_urls':
                    normalized[section][key] = str(normalized[section].get(key) or '').strip()
        return normalized

    @classmethod
    def person_media(cls, person):
        media = cls.normalize_person_media(person.media_src, domain=person.domain)
        for section, key, column in cls.PERSON_MEDIA_COLUMNS:
            value = getattr(person, column, None)
            if value is not None:
                media[section][key] = copy.deepcopy(value)
        raw_media = person.media_src if isinstance(person.media_src, dict) else {}
        raw_custom = raw_media.get('custom') if isinstance(raw_media.get('custom'), dict) else {}
        raw_official = raw_media.get('official') if isinstance(raw_media.get('official'), dict) else {}
        if (getattr(person, 'custom_gdrive_fileid', None) and
                person.custom_gdrive_fileid == raw_official.get('official_cached_google_fileid') and
                raw_custom.get('custom_user_google_fileid') != person.custom_gdrive_fileid):
            media['custom']['custom_user_google_fileid'] = raw_custom.get('custom_user_google_fileid') or ''
        return media

    PERSON_MEDIA_COLUMNS = (
        ('official', 'local_img_path', 'local_path'),
        ('official', 'cached_local_img_path', 'official_cached_local_path'),
        ('official', 'google_fileid', 'gdrive_fileid'),
        ('official', 'official_cached_google_fileid', 'official_cached_gdrive_fileid'),
        ('official', 'remote_img_path', 'official_remote_path'),
        ('official', 'site_img_url', 'site_img_url'),
        ('official', 'site_img_urls', 'photo_urls'),
        ('custom', 'local_img_path', 'custom_local_path'),
        ('custom', 'remote_img_path', 'custom_remote_path'),
        ('custom', 'custom_user_google_fileid', 'custom_gdrive_fileid'),
    )

    @staticmethod
    def person_image_name(person):
        if str(person.domain or '').upper() == 'WESTERN':
            return person.name_en or person.name_org or person.name_ko or ''
        return person.name_ko or person.name_org or ''

    @classmethod
    def rename_person_custom_asset(cls, person, old_name):
        new_name = cls.person_image_name(person)
        if old_name == new_name:
            return
        renamed = MetaUtil.sync_renamed_person_user_image(person, old_name, new_name, '', '', person.person_idx)
        if renamed:
            media = cls.person_media(person)
            media['custom'].update(renamed)
            person.media_src = media
            cls.sync_person_media_columns(person)

    @classmethod
    def sync_person_media_columns(cls, person):
        media = cls.normalize_person_media(person.media_src, domain=person.domain)
        for section, key, column in cls.PERSON_MEDIA_COLUMNS:
            setattr(person, column, copy.deepcopy(media[section][key]))
        person.media_src = media

    @classmethod
    def normalize_person_extra(cls, extra_info, person=None):
        raw = copy.deepcopy(extra_info) if isinstance(extra_info, dict) else {}
        if person is not None and str(person.domain or '').upper() in ('JAV', 'WESTERN'):
            raw.pop('local_img_url', None)
        if person is not None:
            # v1 columns are authoritative, including intentionally empty dictionaries.
            source = copy.deepcopy(person.source_data) if isinstance(person.source_data, dict) else {}
            manual = copy.deepcopy(person.manual_data) if isinstance(person.manual_data, dict) else {}
        else:
            source = raw.get('source_data') if isinstance(raw.get('source_data'), dict) else {}
            manual = raw.get('manual_data') if isinstance(raw.get('manual_data'), dict) else {}
        return raw, source, manual

    @classmethod
    def build_person_source_data(cls, person, fallback=None):
        fallback = fallback if isinstance(fallback, dict) else {}
        source = copy.deepcopy(person.source_data or {}) if person else {}
        aliases = {'birth': 'birth_date', 'debut': 'debut_date', 'bra_size': 'cup_size', 'blood': 'blood_type'}
        result = {}
        for field in cls.PERSON_SOURCE_FIELDS:
            if field in source:
                result[field] = source[field]
            elif field in fallback:
                result[field] = fallback[field]
            else:
                attr = aliases.get(field, field)
                result[field] = getattr(person, attr, '') if person else ''
                if field in ('hobby', 'specialty') and person:
                    result[field] = (person.extra_info or {}).get(field, '')
        return result

    @classmethod
    def rebuild_effective_person_fields(cls, person):
        if person is None:
            return
        source, manual = person.source_data or {}, person.manual_data or {}
        aliases = {'birth': 'birth_date', 'debut': 'debut_date', 'bra_size': 'cup_size', 'blood': 'blood_type'}
        extra = copy.deepcopy(person.extra_info or {})
        for field in cls.PERSON_SOURCE_FIELDS:
            if field not in manual and field not in source:
                continue
            value = manual[field] if field in manual else source[field]
            attr = aliases.get(field, field)
            if field == 'height':
                value = cls._parse_integer(value)
            if field in ('hobby', 'specialty'):
                extra[field] = value or ''
            elif hasattr(person, attr):
                setattr(person, attr, value if field == 'height' else (value if value is not None else ''))
        person.aliases = [value.strip() for value in re.split(r'[,/]', person.other_names or '') if value.strip()]
        person.birth_year = cls._parse_integer(str(person.birth_date or '')[:4])
        person.bust, person.waist, person.hip, parsed_cup = cls._parse_body_size(person.body_size)
        if 'bra_size' not in manual and 'bra_size' not in source and parsed_cup:
            person.cup_size = parsed_cup
        for key in ('source_data', 'manual_data'):
            extra.pop(key, None)
        person.extra_info = extra

    @classmethod
    def build_person_media_payload(cls, media_src, domain='JAV'):
        """정규화된 이미지 payload를 DB 저장용 복사본으로 반환한다."""
        return cls.normalize_person_media(media_src, domain=domain)

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

            from .util_runtime import configure_postgres_cooperation, install_batch_timeouts
            configure_postgres_cooperation(logger)

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
                pool_timeout=15,
                pool_pre_ping=True,
                connect_args=connect_args
            )

            install_batch_timeouts(pg_engine)
            cls._engines['postgres'] = pg_engine
            cls._sessions['postgres'] = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=pg_engine))
            from .migrations import MigrationOrchestrator
            MigrationOrchestrator.prepare_engine_schema(pg_engine, 'postgres', Base.metadata)
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
                from .migrations import MigrationOrchestrator
                MigrationOrchestrator.prepare_engine_schema(sq_engine, dom, Base.metadata)

            # logger.debug(f"[MetaDB Engine] SQLite3 분산 도메인 DB 초기화 완료 (경로: {db_dir})")

    @classmethod
    def ensure_db_ready(cls):
        if not cls._engines or not cls._sessions:
            cls.init_engines()
        if getattr(cls, '_media_recovery_pid', None) != os.getpid():
            cls._media_recovery_pid = os.getpid()
            MetaUtil.recover_media_replacements()

    @classmethod
    def get_meta_schema_status(cls, category=None):
        """실제 메타 DB 스키마 상태를 반환한다. 플러그인 설정 버전과 분리한다."""
        cls.ensure_db_ready()
        _, domain, _ = cls.get_session_and_domain(category or 'JAV_CEN')
        engine_key = 'postgres' if cls._is_postgres else domain
        engine = cls._engines.get(engine_key)
        if not engine:
            return {
                'schema_name': cls.META_DB_SCHEMA_NAME,
                'schema_version': 0,
                'migration_state': 'unavailable',
                'physical_name': domain or ''
            }
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT schema_version, migration_state, migration_id, "
                        "started_at, completed_at, last_error, backup_path "
                        "FROM meta_schema_version WHERE schema_name = :schema_name"
                    ),
                    {'schema_name': cls.META_DB_SCHEMA_NAME}
                ).fetchone()
            if not row:
                return {
                    'schema_name': cls.META_DB_SCHEMA_NAME,
                    'schema_version': 0,
                    'migration_state': 'migration_required',
                    'physical_name': domain or ''
                }
            return {
                'schema_name': cls.META_DB_SCHEMA_NAME,
                'schema_version': int(row[0] or 0),
                'migration_state': row[1] or 'legacy',
                'migration_id': row[2] or '',
                'started_at': row[3],
                'completed_at': row[4],
                'last_error': row[5] or '',
                'backup_path': row[6] or '',
                'physical_name': domain or ''
            }
        except Exception as e_status:
            logger.error(f"[MetaDB Schema] 상태 조회 실패 ({domain}): {e_status}")
            return {
                'schema_name': cls.META_DB_SCHEMA_NAME,
                'schema_version': 0,
                'migration_state': 'unavailable',
                'physical_name': domain or '',
                'last_error': str(e_status)
            }

    @classmethod
    def get_schema_version(cls, category):
        """대상별 통합 메타 DB 스키마 버전을 반환한다."""
        return int(cls.get_meta_schema_status(category).get('schema_version') or 0)

    @classmethod
    def _ensure_meta_db_writable(cls, category):
        """오케스트레이터의 분산 락 및 버전 상태에 기반하여 쓰기 가능 여부를 검증."""
        meta_module = P.get_module('meta_db')
        if meta_module and hasattr(meta_module, 'orchestrator'):
            return meta_module.orchestrator.is_writable(category)

        status = cls.get_meta_schema_status(category)
        version = int(status.get('schema_version') or 0)
        state = status.get('migration_state')
        return bool(version >= cls.META_DB_SCHEMA_VERSION and state in ['complete', 'backfilling'])

    @classmethod
    def _meta_db_targets(cls):
        cls.ensure_db_ready()
        if cls._is_postgres:
            return [('postgres', cls._engines.get('postgres'), None)]
        db_dir = P.ModelSetting.get("meta_db_sqlite_dir") or os.path.join(path_data, 'db', 'meta_db')
        return [
            (dom, cls._engines.get(dom), os.path.join(db_dir, filename))
            for dom, filename in sorted(
                ((v[0], v[1]) for v in DOMAIN_MAP.values()),
                key=lambda value: value[0]
            )
        ]

    @staticmethod
    def _backup_postgres_database(engine, backup_path, chunk_size=500, progress_callback=None):
        return backup_postgres_database(
            engine, backup_path, chunk_size=chunk_size,
            progress_callback=progress_callback
        )

    @staticmethod
    def _backup_sqlite_database(source_path, backup_path, progress_callback=None, target_name=""):
        return backup_sqlite_database(
            source_path, backup_path,
            progress_callback=progress_callback,
            target_name=target_name
        )

    def _publish_maintenance_status(self, status):
        try:
            cache = F.get_cache(f'{P.package_name}_meta_db_maintenance')
            cache.set('status', json.dumps(status, ensure_ascii=False))
        except Exception as exc:
            logger.debug('[MetaDB Maintenance] 공유 상태 저장 실패: %s', exc)

    def _get_maintenance_status(self):
        """Read the same snapshot in the web process and Celery workers."""
        status = dict(self.maintenance_orchestrator.status)
        try:
            raw = F.get_cache(f'{P.package_name}_meta_db_maintenance').get('status')
            shared = json.loads(raw) if raw else None
            if isinstance(shared, dict):
                status = shared
            else:
                saved = load_json_state(self.maintenance_orchestrator.state_file)
                if isinstance(saved, dict) and saved.get('updated_at', 0) > status.get('updated_at', 0):
                    status = saved
        except Exception as exc:
            logger.debug('[MetaDB Maintenance] 공유 상태 조회 실패: %s', exc)
            saved = load_json_state(self.maintenance_orchestrator.state_file)
            if isinstance(saved, dict) and saved.get('updated_at', 0) > status.get('updated_at', 0):
                status = saved
        if status.get('is_running') and status.get('worker_id'):
            from .migrations.lock_manager import MetaDbLockManager
            if MetaDbLockManager._dead_local_owner(status['worker_id']):
                status.update(is_running=False, phase='failed', status='작업 프로세스 종료로 중단됨',
                              last_error='작업 프로세스가 종료되었습니다. 상태를 확인한 뒤 다시 실행하세요.')
                self._publish_maintenance_status(status)
        return status

    def _save_meta_maintenance_state(self):
        self.maintenance_orchestrator._save_state()

    def _start_maintenance_job(self, operation, options=None):
        if operation not in ('backup', 'restore', 'actor_sync'):
            return False, '지원하지 않는 DB 관리 작업입니다.'
        with self._maintenance_start_lock:
            if self.meta_migration_status.get('is_running') or self._get_maintenance_status().get('is_running'):
                return False, '마이그레이션 또는 DB 관리 작업이 이미 진행 중입니다.'
            if operation == 'actor_sync':
                allowed, message = self._schema_write_guard('PERSON', 'JAV 배우 DB 동기화')
                if not allowed:
                    return False, message
            import uuid
            job_id = uuid.uuid4().hex
            status = self.maintenance_orchestrator.status
            status.clear()
            status.update(is_running=True, operation=operation, job_id=job_id, phase='queued',
                          status='작업 실행 대기 중...', current=0, total=0, current_db='',
                          inserted=0, updated=0, skipped=0, fail=0, last_error='', worker_id='')
            self._save_meta_maintenance_state()
            try:
                if operation == 'actor_sync' and celery and F.config.get('use_celery', False):
                    task_meta_db_maintenance.apply_async(args=[operation, options or {}, job_id])
                else:
                    logger.debug('[MetaDB Maintenance] %s: 웹 프로세스 백그라운드 실행', operation)
                    threading.Thread(target=self._run_maintenance_worker,
                                     args=(operation, options or {}, job_id), daemon=True).start()
            except Exception as exc:
                status.update(is_running=False, phase='failed', status='작업 시작 실패', last_error=str(exc))
                self._save_meta_maintenance_state()
                logger.exception('[MetaDB Maintenance] 작업 시작 실패: %s', operation)
                return False, f'작업을 시작하지 못했습니다: {exc}'
        return True, 'DB 관리 작업을 시작했습니다.'

    def _run_maintenance_worker(self, operation, options, job_id):
        snapshot = self._get_maintenance_status()
        if snapshot.get('job_id') != job_id or not snapshot.get('is_running'):
            return
        status = self.maintenance_orchestrator.status
        status.clear()
        status.update(snapshot)
        from .migrations.lock_manager import MetaDbLockManager
        status.update(phase='starting', status='작업 준비 중...',
                      worker_id=MetaDbLockManager.worker_id('maintenance'))
        self._save_meta_maintenance_state()
        logger.debug('[MetaDB Maintenance] %s 시작: %s', operation, job_id)
        try:
            self.ensure_db_ready()
            if operation == 'backup':
                self.maintenance_orchestrator._run_manual_backup()
            elif operation == 'restore':
                self._run_meta_restore(options['backup_path'], options['category'])
            else:
                last_emit = 0.0
                def progress(**fields):
                    nonlocal last_emit
                    old_phase = status.get('phase')
                    status.update(fields)
                    now = time.time()
                    if now - last_emit >= 0.25 or fields.get('phase') != old_phase or fields.get('current') == fields.get('total'):
                        last_emit = now
                        self._save_meta_maintenance_state()
                success, message = self.sync_jav_actors_db(progress_callback=progress)
                status.update(is_running=False, phase='complete' if success else 'failed',
                              status='배우 DB 동기화 완료' if success else '배우 DB 동기화 실패',
                              message=message, last_error='' if success else message)
                if success:
                    status['percent'] = 100
        except Exception as exc:
            status.update(is_running=False, phase='failed', status='DB 관리 작업 실패', last_error=str(exc))
            logger.exception('[MetaDB Maintenance] %s 실패', operation)
        finally:
            status['is_running'] = False
            if status.get('phase') == 'complete':
                status['percent'] = 100
            self._save_meta_maintenance_state()
            logger.debug('[MetaDB Maintenance] %s 종료: 상태=%s, 오류=%s',
                         operation, status.get('status'), status.get('last_error') or '없음')

    def restore_meta_db(self, category, backup_path=None):
        """SQLite 또는 PostgreSQL 메타 DB 백업을 복원한다."""
        if self.meta_migration_status.get('is_running') or self._get_maintenance_status().get('is_running'):
            return False, "다른 메타 DB 관리 작업이 이미 진행 중입니다."

        category = str(category or '').strip().upper()
        backup_root = os.path.realpath(os.path.join(path_data, 'db', 'meta_db_backups'))
        if backup_path:
            backup_path = os.path.realpath(str(backup_path))
            if os.path.commonpath((backup_root, backup_path)) != backup_root:
                return False, "허용된 메타 DB 백업 디렉터리 외부의 파일은 복원할 수 없습니다."

        if self._is_postgres:
            if not backup_path:
                migration_backup_root = self.meta_migration_status.get('backup_path')
                if migration_backup_root:
                    backup_path = os.path.join(migration_backup_root, 'meta_db_postgres.sqlite3')
            if not backup_path:
                return False, "복원할 PostgreSQL 논리 백업 경로가 지정되지 않았습니다."
            if os.path.basename(backup_path) != 'meta_db_postgres.sqlite3':
                return False, "PostgreSQL 복원에는 meta_db_postgres.sqlite3 논리 백업만 사용할 수 있습니다."

            engine = self._engines.get('postgres')
            if not engine:
                return False, "PostgreSQL 메타 DB 엔진을 찾을 수 없습니다."
            return self._start_meta_restore(backup_path, 'POSTGRES')

        if category == 'ALL' and backup_path:
            filename = os.path.basename(backup_path)
            category = next(
                (
                    name for name, (_, db_filename) in DOMAIN_MAP.items()
                    if db_filename == filename
                ),
                ''
            )

        mapping = DOMAIN_MAP.get(category)
        if not mapping:
            return False, f"알 수 없는 메타 DB 대상입니다: {category}"

        target = next(
            (item for item in self._meta_db_targets() if item[0] == mapping[0]),
            None
        )
        if not target or not target[2]:
            return False, f"복원 대상 DB를 찾을 수 없습니다: {category}"

        if not backup_path:
            migration_backup_root = self.meta_migration_status.get('backup_path')
            if migration_backup_root:
                backup_path = os.path.join(migration_backup_root, os.path.basename(target[2]))

        if not backup_path:
            return False, "복원할 백업 경로가 지정되지 않았습니다."

        if os.path.basename(backup_path) != mapping[1]:
            return False, f"{category} 복원에는 {mapping[1]} 백업만 사용할 수 있습니다."

        return self._start_meta_restore(backup_path, category)

    def _start_meta_restore(self, backup_path, category):
        return self._start_maintenance_job('restore', {'backup_path': backup_path, 'category': category})

    def _run_meta_restore(self, backup_path, category):
        status = self.maintenance_orchestrator.status
        last_emit_time = 0.0

        def throttled_restore_progress(current, total, message, target):
            nonlocal last_emit_time
            now = time.time()
            if now - last_emit_time >= 0.25 or current >= total:
                last_emit_time = now
                status.update({
                    'phase': 'restore',
                    'status': message,
                    'current': current,
                    'total': total,
                    'current_db': target or category
                })
                self._save_meta_maintenance_state()

        try:
            if category == 'POSTGRES':
                engine = self._engines.get('postgres')
                session = self._sessions.get('postgres')
                if session:
                    session.remove()
                restore_postgres_database(
                    engine, backup_path,
                    progress_callback=throttled_restore_progress
                )
            else:
                target = next(
                    item for item in self._meta_db_targets()
                    if item[0] == DOMAIN_MAP[category][0]
                )
                session = self._sessions.get(target[0])
                engine = self._engines.get(target[0])
                if session:
                    session.remove()
                if engine:
                    engine.dispose()
                status.update({
                    'phase': 'restore',
                    'status': f'{category} SQLite 복원 준비 중',
                    'total': 0,
                    'current': 0,
                    'current_db': category
                })
                self._save_meta_maintenance_state()
                restore_sqlite_database(
                    backup_path,
                    target[2],
                    progress_callback=throttled_restore_progress,
                    target_name=category
                )

            status.update({
                'is_running': False,
                'status': '복원 완료',
                'phase': 'complete',
                'can_resume': False,
                'backfill_positions': {}
            })
            logger.info("[MetaDB Schema] %s 백업 복원 완료: %s", category, backup_path)

            # 복원 완료 후 엔진 재접속 및 복원된 스키마 버전 상태 재인식
            self.init_engines()
            self.orchestrator.status.update(can_resume=False, backfill_positions={})
            self.orchestrator._save_state()

        except Exception as exc:
            status.update({
                'is_running': False,
                'status': '복원 실패',
                'phase': 'failed',
                'last_error': str(exc),
                'can_resume': False
            })
            logger.error("[MetaDB Schema] %s 백업 복원 실패: %s", category, exc, exc_info=True)
        finally:
            status['stop_flag'] = False
            self._save_meta_maintenance_state()

    @classmethod
    def get_session_and_domain(cls, category):
        if not category:
            logger.error("[MetaDB] category 인자가 누락되었습니다.")
            return None, None, None

        cat_key = str(category).strip().upper()

        if cat_key == 'AV_ALL':
            if cls._is_postgres:
                return cls._sessions.get('postgres'), 'postgres', 'AV_ALL'
            return None, None, 'AV_ALL'

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

        clean = url.strip()
        if not clean:
            return False

        # 프로토콜 없는 내부 상대 경로 라우트(/...)인 경우
        if clean.startswith('/'):
            return True

        # 설정된 DDNS 주소로 시작하는지 검사
        ddns = (F.SystemModelSetting.get('ddns') or '').strip().rstrip('/')
        if ddns and clean.startswith(ddns):
            return True

        # 설정된 이미지 서버 URL로 시작하는지 검사
        for key in ['western_image_server_url', 'jav_censored_image_server_url', 'jav_uncensored_image_server_url']:
            srv = (P.ModelSetting.get(key) or '').strip().rstrip('/')
            if srv and clean.startswith(srv):
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
    def _cluster_and_merge_actor_rows(cls, raw_rows, progress_callback=None):
        """
        avdbs 원본 행들을 안전 규칙(1단계: 이름+원문명+사진 일치, 2단계: 이름+영문/원문+생년월일 일치)에 따라
        동일인 클러스터로 묶고 원본 데이터를 보존한 단일 마스터 레코드로 합성합니다.
        """
        clusters = []
        cluster_map = {}

        for row_index, r in enumerate(raw_rows, 1):
            if progress_callback and (row_index % 500 == 0 or row_index == len(raw_rows)):
                progress_callback(phase='cluster_group', status='배포 배우 중복 분류 중',
                                  current=row_index, total=len(raw_rows))
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
        for cluster_index, cluster in enumerate(clusters, 1):
            if progress_callback and (cluster_index % 500 == 0 or cluster_index == len(clusters)):
                progress_callback(phase='cluster_merge', status='배포 배우 중복 정보 통합 중',
                                  current=cluster_index, total=len(clusters))
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

        target_domain = str(domain or (media_src.get('domain') if isinstance(media_src, dict) else '') or 'JAV').upper()
        media = cls.normalize_person_media(media_src, domain=target_domain)
        official = media['official']
        custom = media['custom']

        if not order_str:
            if target_domain == 'WESTERN':
                mode = P.ModelSetting.get("western_actor_image_mode") or "site"
            elif target_domain == 'JAV':
                mode = P.ModelSetting.get("jav_censored_actor_image_mode") or "gds"
            else:
                mode = "site"

            if mode in ('gdrive', 'gds'):
                order_str = (
                    "custom_user_google_fileid, official_cached_google_fileid, "
                    "google_fileid, site_img_url"
                )
            elif mode == 'image_server':
                order_str = "custom_local_img_path, cached_local_img_path, local_img_path, google_fileid, site_img_url"
            elif mode == 'site':
                order_str = "site_img_url"
            else:
                order_str = "google_fileid, site_img_url" if target_domain == 'JAV' else "site_img_url"

        values = {
            'custom_user_google_fileid': custom.get('custom_user_google_fileid'),
            'custom_local_img_path': custom.get('local_img_path'),
            'cached_local_img_path': official.get('cached_local_img_path'),
            'official_cached_google_fileid': official.get('official_cached_google_fileid'),
            'local_img_path': official.get('local_img_path'),
            'google_fileid': official.get('google_fileid') if target_domain == 'JAV' else '',
            'site_img_url': official.get('site_img_url'),
        }
        invalid = {'', 'null', 'none', '403', '404', 'deprecated', 'unavailable'}
        parsed_order = [x.strip().lower() for x in re.split(r'[\s,]+', str(order_str)) if x.strip()]

        for opt in parsed_order:
            val = str(values.get(opt) or '').strip()
            if val.lower() in invalid:
                continue
            if opt in ('custom_user_google_fileid', 'official_cached_google_fileid', 'google_fileid'):
                return f"https://drive.google.com/thumbnail?id={val}&sz=s0"
            if opt in ('custom_local_img_path', 'cached_local_img_path', 'local_img_path'):
                return cls.format_actor_thumb_url(val, domain=target_domain)
            if opt == 'site_img_url' and val.startswith(('http://', 'https://')):
                return val

        return ""

    @classmethod
    def resolve_person_active_thumb(cls, p):
        if not p: return ""
        media_src = cls.person_media(p)
        return cls.resolve_actor_thumb_url(media_src, domain=p.domain)

    @classmethod
    def _parse_integer(cls, val):
        """문자열 등에서 순수 정수를 추출하여 반환."""
        if val is None or val == '':
            return None
        m = re.search(r'\d+', str(val))
        return int(m.group()) if m else None

    @classmethod
    def _parse_body_size(cls, body_str):
        """B85(E)-W58-H86, 85-58-86 등 다양한 형태의 신체 사이즈 문자열을 분해 파싱."""
        bust, waist, hip, cup = None, None, None, None
        if not body_str:
            return bust, waist, hip, cup

        clean_str = str(body_str).strip()
        m_cup = re.search(r'[A-Za-z]\s*(?=[0-9])|[B-H]\b|\(([A-Za-z]+)\)', clean_str)
        if m_cup:
            cup_cand = m_cup.group(1) if m_cup.group(1) else m_cup.group(0)
            clean_cup = re.sub(r'[^A-Za-z]', '', cup_cand).upper()
            if clean_cup:
                cup = clean_cup

        numbers = re.findall(r'\b\d{2,3}\b', clean_str)
        if len(numbers) >= 3:
            bust = int(numbers[0])
            waist = int(numbers[1])
            hip = int(numbers[2])

        return bust, waist, hip, cup

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

        a_extra = actor_data.get('extra_info') if isinstance(actor_data.get('extra_info'), dict) else {}
        site_actor_id = str(a_extra.get('site_actor_id') or actor_data.get('site_actor_id') or '').strip()
        site_actor_url = str(a_extra.get('site_actor_url') or actor_data.get('site_actor_url') or '').strip()

        p_rec = None

        if site_actor_id:
            all_domain_persons = person_session.query(MetaPerson).filter_by(domain=person_domain).all()
            for cand_p in all_domain_persons:
                p_site_actors = (cand_p.extra_info or {}).get('site_actors', {})
                for s_key, s_data in p_site_actors.items():
                    if isinstance(s_data, dict) and str(s_data.get('id')) == site_actor_id:
                        p_rec = cand_p
                        logger.debug(f"[MetaDB Actor Link Match] 사이트 고유 ID 일치 ({s_key}:{site_actor_id}) -> {p_rec.name_ko or p_rec.name_org}")
                        break
                if p_rec:
                    break

        matched_db_row = None
        if not p_rec and person_domain == 'JAV':
            actors_map, _ = cls.get_cached_actors_map()
            if actors_map:
                if a_idx_val and a_idx_val in actors_map:
                    matched_db_row = actors_map[a_idx_val]
                elif raw_name_org:
                    for k_map, v_map in actors_map.items():
                        if k_map and k_map.lower() == raw_name_org.lower():
                            matched_db_row = v_map
                            break

            if matched_db_row:
                raw_actor_id = str(matched_db_row.get('actor_id') or '').strip()
                if raw_actor_id:
                    m_num = re.search(r'(\d+)', raw_actor_id)
                    a_idx_val = f"PA{m_num.group(1)}" if m_num else raw_actor_id

                raw_name_org = str(matched_db_row.get('name_org') or matched_db_row.get('inner_name_cn') or raw_name_org).strip()
                if not raw_name_ko:
                    raw_name_ko = str(matched_db_row.get('name_ko') or matched_db_row.get('inner_name_kr') or '').strip()
                if not raw_name_en:
                    raw_name_en = str(matched_db_row.get('name_en') or matched_db_row.get('inner_name_en') or '').strip()

            if not raw_name_ko:
                return None

        if not p_rec and a_idx_val:
            p_rec = person_session.query(MetaPerson).filter_by(domain=person_domain, person_idx=a_idx_val).first()

        if not p_rec:
            match_conditions = []
            if raw_name_org:
                match_conditions.extend([
                    func.lower(MetaPerson.name_org) == raw_name_org.lower(),
                    func.lower(MetaPerson.name_en) == raw_name_org.lower()
                ])
            if raw_name_ko:
                match_conditions.append(MetaPerson.name_ko == raw_name_ko)

            if match_conditions:
                p_rec = person_session.query(MetaPerson).filter(
                    MetaPerson.domain == person_domain,
                    or_(*match_conditions)
                ).first()

        raw_aliases = actor_data.get('aliases') or actor_data.get('other_names') or actor_data.get('onm') or []
        if isinstance(raw_aliases, str):
            alias_list = [x.strip() for x in re.split(r'[,/]', raw_aliases) if x.strip()]
        elif isinstance(raw_aliases, list):
            alias_list = [str(x).strip() for x in raw_aliases if str(x).strip()]
        else:
            alias_list = []

        if raw_name_en and raw_name_en not in alias_list:
            alias_list.append(raw_name_en)

        a_media = actor_data.get('media_src') if isinstance(actor_data.get('media_src'), dict) else {}
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

        # 신체 치수 및 프로필 속성 파싱
        h_val = cls._parse_integer(a_extra.get('height'))
        raw_body_size = str(a_extra.get('body_size') or '').strip()
        bust_val, waist_val, hip_val, cup_val = cls._parse_body_size(raw_body_size)

        raw_bra = str(a_extra.get('bra_size') or a_extra.get('cup_size') or '').strip().upper()
        clean_bra = re.sub(r'[^A-Z]', '', raw_bra)
        if clean_bra:
            cup_val = clean_bra

        birth_val = str(a_extra.get('birth') or a_extra.get('birth_date') or '').strip()
        m_by = re.search(r'\b(19\d{2}|20\d{2})\b', birth_val)
        birth_year_val = int(m_by.group(1)) if m_by else None

        debut_val = str(a_extra.get('debut') or a_extra.get('career_start_year') or '').strip()
        agency_val = str(a_extra.get('agency') or '').strip()
        blood_val = str(a_extra.get('blood') or '').strip().upper().replace('형', '')
        country_val = str(a_extra.get('country') or actor_data.get('country') or ('일본' if person_domain == 'JAV' else '')).strip()

        raw_gender = str(actor_data.get('gender') or a_extra.get('gender') or ('female' if person_domain == 'JAV' else '')).strip().lower()
        gender_val = 'male' if raw_gender in ['m', 'male', '남', '남성'] else 'female'

        info_url_candidate = str(a_extra.get('info_url') or '').strip()
        if not info_url_candidate and a_idx_val:
            if a_idx_val.startswith('PS'):
                info_url_candidate = f"https://stashdb.org/performers/{a_idx_val[2:]}"
            elif a_idx_val.startswith('PP') or a_idx_val.startswith('PT'):
                info_url_candidate = f"https://theporndb.net/performers/{a_idx_val[2:]}"
            elif a_idx_val.startswith('PA'):
                raw_num = re.sub(r'\D', '', a_idx_val)
                if raw_num:
                    info_url_candidate = f"https://www.avdbs.com/menu/actor.php?actor_idx={raw_num}"

        # AVDBS 배포 DB 행 매칭 시 스펙 보강
        if matched_db_row:
            db_local = str(matched_db_row.get('local_img_path') or '').strip()
            if db_local:
                parts = [p for p in db_local.replace('\\', '/').strip('/').split('/') if p]
                a_local = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else db_local

            db_site = str(matched_db_row.get('site_img_url') or '').strip()
            if db_site:
                a_site = db_site
                if db_site not in a_site_photos:
                    a_site_photos.insert(0, db_site)

            db_google = str(matched_db_row.get('google_fileid') or '').strip()
            if db_google:
                a_google = db_google

            db_birth = str(matched_db_row.get('birth') or '').strip()
            if db_birth:
                birth_val = db_birth
                m_by_db = re.search(r'\b(19\d{2}|20\d{2})\b', db_birth)
                birth_year_val = int(m_by_db.group(1)) if m_by_db else birth_year_val

            h_val = cls._parse_integer(matched_db_row.get('profile_height') or matched_db_row.get('height')) or h_val

            db_body = str(matched_db_row.get('body_size') or '').strip()
            if db_body:
                raw_body_size = db_body
                b_v, w_v, h_v, c_v = cls._parse_body_size(db_body)
                bust_val = b_v or bust_val
                waist_val = w_v or waist_val
                hip_val = h_v or hip_val
                cup_val = c_v or cup_val

            db_bra = str(matched_db_row.get('bra_size') or '').strip().upper()
            clean_db_bra = re.sub(r'[^A-Z]', '', db_bra)
            if clean_db_bra:
                cup_val = clean_db_bra

            debut_val = str(matched_db_row.get('debut') or '').strip() or debut_val
            agency_val = str(matched_db_row.get('agency') or '').strip() or agency_val
            blood_val = str(matched_db_row.get('blood') or '').strip().upper().replace('형', '') or blood_val
            info_url_candidate = str(matched_db_row.get('info_url') or '').strip() or info_url_candidate

        media_src_data = {
            'local_img_path': a_local,
            'google_fileid': a_google,
            'site_img_url': a_site,
            'site_img_urls': a_site_photos
        }

        source_data_dict = {
            'name_org': raw_name_org or raw_name_ko,
            'name_ko': raw_name_ko,
            'name_en': raw_name_en,
            'birth': birth_val,
            'height': h_val,
            'body_size': raw_body_size,
            'bra_size': cup_val,
            'debut': debut_val,
            'agency': agency_val,
            'blood': blood_val,
            'info_url': info_url_candidate,
            'gender': gender_val,
            'country': country_val
        }

        extra_data = {
            'hobby': str(a_extra.get('hobby') or (matched_db_row.get('hobby') if matched_db_row else '') or '').strip(),
            'specialty': str(a_extra.get('specialty') or (matched_db_row.get('specialty') if matched_db_row else '') or '').strip(),
            'source_origin': 'western' if person_domain == 'WESTERN' else 'jav_actors'
        }

        # 활성 썸네일 산출 및 사진 보유 플래그 계산
        resolved_thumb = a_thumb or cls.resolve_actor_thumb_url(media_src_data, domain=person_domain)
        has_photo_flag = bool(resolved_thumb and str(resolved_thumb).strip())

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
                gender=gender_val,
                birth_date=birth_val,
                birth_year=birth_year_val,
                debut_date=debut_val,
                height=h_val,
                bust=bust_val,
                waist=waist_val,
                hip=hip_val,
                body_size=raw_body_size,
                cup_size=cup_val,
                blood_type=blood_val,
                agency=agency_val,
                country=country_val,
                info_url=info_url_candidate,
                has_photo=has_photo_flag,
                thumb_url=resolved_thumb,
                local_path=a_local,
                gdrive_fileid=a_google,
                custom_gdrive_fileid=None,
                custom_local_path=None,
                site_img_url=a_site,
                photo_urls=a_site_photos,
                merged_count=1,
                alt_indices=[a_idx_val] if a_idx_val else [],
                merged_details=[],
                works_count=0,
                source_data=source_data_dict,
                manual_data={},
                media_src=media_src_data,
                works={},
                extra_info=extra_data,
            )
            person_session.add(p_rec)
            person_session.flush()

        else:
            # 기존 레코드 존재 시 수동 수정 데이터(manual_data)를 제외한 공백 물리 컬럼 보강
            _, source_data, manual_data = cls.normalize_person_extra(p_rec.extra_info, person=p_rec)
            stored_gender = str(source_data.get('gender') or raw_gender).strip().lower()
            fill_gender = ('male' if stored_gender in ('m', 'male', '남', '남성') else 'female') if stored_gender else gender_val
            fill_country = str(source_data.get('country') or country_val).strip()
            if 'gender' not in manual_data and not p_rec.gender and fill_gender:
                p_rec.gender = fill_gender
            if 'country' not in manual_data and not p_rec.country and fill_country:
                p_rec.country = fill_country
            # Keep the original profile available when a manual override is reset.
            for field, value in (('gender', raw_gender), ('country', country_val)):
                if value and not source_data.get(field):
                    source_data[field] = gender_val if field == 'gender' else value
            p_rec.source_data = source_data
            if 'height' not in manual_data and h_val and not p_rec.height: p_rec.height = h_val
            if 'birth' not in manual_data and 'birth_date' not in manual_data and birth_val and not p_rec.birth_date:
                p_rec.birth_date = birth_val
                p_rec.birth_year = birth_year_val
            if 'debut' not in manual_data and 'debut_date' not in manual_data and debut_val and not p_rec.debut_date: p_rec.debut_date = debut_val
            if 'body_size' not in manual_data and raw_body_size and not p_rec.body_size:
                p_rec.body_size = raw_body_size
                p_rec.bust = bust_val or p_rec.bust
                p_rec.waist = waist_val or p_rec.waist
                p_rec.hip = hip_val or p_rec.hip
            if 'cup_size' not in manual_data and 'bra_size' not in manual_data and cup_val and not p_rec.cup_size:
                p_rec.cup_size = cup_val
            if 'agency' not in manual_data and agency_val and not p_rec.agency: p_rec.agency = agency_val
            if 'blood' not in manual_data and 'blood_type' not in manual_data and blood_val and not p_rec.blood_type: p_rec.blood_type = blood_val
            if 'info_url' not in manual_data and info_url_candidate and not p_rec.info_url: p_rec.info_url = info_url_candidate

            if a_local and not p_rec.local_path: p_rec.local_path = a_local
            if a_google and not p_rec.gdrive_fileid: p_rec.gdrive_fileid = a_google
            if a_site and not p_rec.site_img_url: p_rec.site_img_url = a_site

            # 썸네일 캐시 갱신
            if not p_rec.thumb_url and resolved_thumb:
                p_rec.thumb_url = resolved_thumb
                p_rec.has_photo = True

            # 사이트 고유 식별 ID 보존
            if site_actor_id:
                merged_extra = copy.deepcopy(p_rec.extra_info or {})
                current_site_actors = merged_extra.get('site_actors', {})
                infer_site = 'dmm' if 'dmm' in site_actor_url else ('javbus' if 'javbus' in site_actor_url else ('javdb' if 'javdb' in site_actor_url else 'site'))
                if infer_site not in current_site_actors:
                    current_site_actors[infer_site] = {
                        'id': site_actor_id,
                        'url': site_actor_url
                    }
                    merged_extra['site_actors'] = current_site_actors
                    p_rec.extra_info = merged_extra

        incoming_media = cls.normalize_person_media(actor_data.get('media_src'), domain=person_domain)
        merged_media = cls.person_media(p_rec)
        for key, value in incoming_media['official'].items():
            if value:
                merged_media['official'][key] = copy.deepcopy(value)
        p_rec.media_src = merged_media
        cls.sync_person_media_columns(p_rec)
        p_rec.thumb_url = cls.resolve_person_active_thumb(p_rec) or p_rec.thumb_url
        p_rec.has_photo = bool(p_rec.thumb_url)
        p_rec.works_count = sum(len(v) for v in (p_rec.works or {}).values() if isinstance(v, list))
        return p_rec

    @staticmethod
    def _media_asset_matches(asset, media_type, url):
        if not isinstance(asset, dict) or asset.get('media_type') != media_type:
            return False
        old_url = str(asset.get('url') or '').strip()
        if old_url and old_url == str(url or '').strip():
            return True
        # Thumbnail sizing/query changes do not change the physical Drive asset.
        def file_id(value):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(str(value or ''))
            query = parse_qs(parsed.query)
            host = (parsed.hostname or '').lower()
            if host in ('drive.google.com', 'googleusercontent.com') or host.endswith('.googleusercontent.com'):
                path_match = re.search(r'/d/([^/]+)', parsed.path)
                return (query.get('id') or [''])[0] or (path_match.group(1) if path_match else '')
            return ''
        new_id = file_id(url)
        return bool(new_id and new_id == (str(asset.get('gdrive_fileid') or '').strip() or file_id(old_url)))

    @classmethod
    def save_metadata(cls, category, entity_dict, target_session=None, target_person_session=None, _completion_receipt=None):
        entries = (entity_dict.get('extra_info') or {}).get('_media_replacements') or [] if isinstance(entity_dict, dict) else []
        outcome = {'state': 'not_attempted'}
        warnings = []
        try:
            success = cls._save_metadata(category, entity_dict, target_session, target_person_session, _storage_outcome=outcome)
        except Exception:
            warnings = MetaUtil.finish_media_replacements(entries, None if outcome['state'] == 'unknown' else outcome['state'] == 'committed')
            raise
        finally:
            if isinstance(_completion_receipt, dict):
                _completion_receipt['commit_state'] = outcome['state']
                if outcome['state'] == 'unknown':
                    _completion_receipt.update(state='blocked', error_code='commit_outcome_unknown')
        if entries:
            if success and target_session is not None:
                MetaUtil.defer_media_replacements(target_session, entries)
            else:
                warnings = MetaUtil.finish_media_replacements(entries, None if outcome['state'] == 'unknown' else bool(success))
        if warnings and isinstance(_completion_receipt, dict):
            _completion_receipt.update(cleanup_pending=True, warnings=warnings)
            if _completion_receipt.get('state') == 'finished':
                _completion_receipt['state'] = 'finished_with_warnings'
        return success

    @classmethod
    def _save_metadata(cls, category, entity_dict, target_session=None, target_person_session=None, _storage_outcome=None):
        outcome = _storage_outcome if _storage_outcome is not None else {'state': 'not_attempted'}
        if not P.ModelSetting.get_bool("meta_db_use"):
            return False

        image_storage_incomplete = False
        removed_image_slots = set()
        if str(category or '').upper() in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') and isinstance(entity_dict, dict):
            from support_site import SiteAvBase
            removed_image_slots = {(entry['media_type'], entry.get('sort_order', 0))
                                   for entry in (entity_dict.get('extra_info') or {}).get('_media_replacements', [])
                                   if entry.get('status') == 'removed'}
            removed_image_slots -= {(entry['media_type'], entry.get('sort_order', 0))
                                    for entry in (entity_dict.get('extra_info') or {}).get('_media_replacements', [])
                                    if entry.get('status') == 'generated'}
            image_storage_incomplete = bool(SiteAvBase.image_storage_error(entity_dict, include_actors=False))
            storage_error = SiteAvBase.image_storage_error(entity_dict)
            entity_dict = copy.deepcopy(entity_dict)
            SiteAvBase.sanitize_incomplete_images(entity_dict)
            if storage_error:
                logger.warning('[MetaDB] 미디어 처리 미완료, 확인된 미디어와 메타데이터 저장 (%s): %s', entity_dict.get('code'), storage_error)
            for record in [entity_dict] + list(entity_dict.get('actor') or []):
                extra = record.get('extra_info') if isinstance(record, dict) else getattr(record, 'extra_info', None)
                if isinstance(extra, dict):
                    extra.pop('_image_storage_state', None)
                    extra.pop('storage_error', None)
                    extra.pop('_media_replacements', None)

        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess:
            logger.error(f"[MetaDB] save_metadata: 세션 획득 실패. category='{category}'")
            return False
        if not cls._ensure_meta_db_writable(category):
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
            # 제목(title)과 부제(tagline)의 개행문자(\r, \n, \t)를 단일 공백으로 정규화하여 DB 저장
            title_raw = str(entity_dict.get('title') or originaltitle)
            title = re.sub(r'[\r\n\t]+', ' ', title_raw).strip()[:500]

            tagline_raw = str(entity_dict.get('tagline') or '')
            tagline = re.sub(r'[\r\n\t]+', ' ', tagline_raw).strip()[:500]

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
            from sqlalchemy.orm import selectinload
            item = s.query(MetaItem).options(
                selectinload(MetaItem.media_files),
                selectinload(MetaItem.person_maps),
                selectinload(MetaItem.tag_maps),
                selectinload(MetaItem.fingerprints),
            ).filter_by(code=code).first()
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
                P.ModelSetting.get('jav_censored_image_server_url') or ''
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
                if isinstance(ex, dict) and str(ex.get('content_type', 'trailer')).lower() == 'trailer' and ex.get('content_url'):
                    raw_trailer_url = ex['content_url']
                    break

            if not raw_trailer_url and isinstance(orig_dict.get('trailer'), str) and orig_dict['trailer'].strip():
                raw_trailer_url = orig_dict['trailer'].strip()

            if not raw_trailer_url:
                for ex_item in extras_list:
                    if isinstance(ex_item, dict) and str(ex_item.get('content_type', 'trailer')).lower() == 'trailer' and ex_item.get('content_url'):
                        c_url = ex_item['content_url']
                        if 'url=' in c_url:
                            try:
                                from urllib.parse import parse_qs, urlparse
                                qs = parse_qs(urlparse(c_url).query)
                                if 'url' in qs: raw_trailer_url = qs['url'][0]
                            except Exception: pass
                        elif c_url.startswith('http') and 'metadata/normal' not in c_url:
                            raw_trailer_url = c_url

            clean_original_extras = [copy.deepcopy(ex) for ex in orig_extras
                if isinstance(ex, dict) and ex.get('content_url')]
            from .util_preview import MetaPreviewUtil
            for ex in clean_original_extras:
                source_url = MetaPreviewUtil.trailer_source_url(ex['content_url'])
                matching = next((entry for entry in extras_list if isinstance(entry, dict)
                    and MetaPreviewUtil.trailer_source_url(entry.get('content_url')) == source_url), {})
                for key in ('title', 'mode', 'premiered', 'thumb'):
                    if not ex.get(key) and matching.get(key) is not None:
                        ex[key] = copy.deepcopy(matching[key])
            for ex in extras_list:
                if (isinstance(ex, dict) and ex.get('content_url')
                        and str(ex.get('content_type', 'trailer')).lower() != 'trailer'
                        and not any(saved.get('content_url') == ex['content_url'] for saved in clean_original_extras)):
                    clean_original_extras.append(copy.deepcopy(ex))
            if raw_trailer_url and not any(str(ex.get('content_type', 'trailer')).lower() == 'trailer'
                    for ex in clean_original_extras):
                clean_original_extras.append(MetaPreviewUtil.video_extra(raw_trailer_url,
                    next((ex.get('title', '') for ex in extras_list if isinstance(ex, dict)
                        and str(ex.get('content_type', 'trailer')).lower() == 'trailer'), ''), extras_list))

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

            resolved_poster_url = ''
            for th in thumbs_list:
                if (
                    isinstance(th, dict) and
                    th.get('aspect') == 'poster' and
                    cls._is_http_url(th.get('value'))
                ):
                    resolved_poster_url = th['value']
                    break
            if std_cat not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') and not resolved_poster_url and thumbs_list and not image_storage_incomplete:
                for th in thumbs_list:
                    if isinstance(th, dict) and cls._is_http_url(th.get('value')):
                        resolved_poster_url = th['value']
                        break
            if not resolved_poster_url:
                resolved_poster_url = entity_dict.get('poster_url') or ''
            if isinstance(resolved_poster_url, str) and '/metadata/normal/' in resolved_poster_url:
                resolved_poster_url = ''
            if not resolved_poster_url and not image_storage_incomplete:
                resolved_poster_url = next(
                    (value for value in ((raw_site_p,) if std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') else (raw_site_p, raw_site_pl)) if cls._is_http_url(value)),
                    ''
                )

            resolved_landscape_url = ''
            for th in thumbs_list:
                if (
                    isinstance(th, dict) and
                    th.get('aspect') == 'landscape' and
                    cls._is_http_url(th.get('value'))
                ):
                    resolved_landscape_url = th['value']
                    break
            if not resolved_landscape_url:
                resolved_landscape_url = entity_dict.get('landscape_url') or ''

            if image_storage_incomplete:
                if ('poster', 0) not in removed_image_slots:
                    resolved_poster_url = resolved_poster_url or item.poster_url or ''
                if ('landscape', 1) not in removed_image_slots:
                    resolved_landscape_url = resolved_landscape_url or (item.extra_info or {}).get('landscape_url', '')
            if ('poster', 0) in removed_image_slots:
                resolved_poster_url = ''
            if ('landscape', 1) in removed_image_slots:
                resolved_landscape_url = ''

            # 저장소에 이미 존재하는 비디오 파일 자동 감지 및 흡수
            from .util_preview import MetaPreviewUtil
            MetaPreviewUtil.check_and_adopt_existing_video(entity_dict, category=std_cat)

            # DB 및 extra_info에는 항상 순수 정규 URL만 깨끗하게 저장
            merged_extra_info = copy.deepcopy(item.extra_info or {})
            merged_extra_info.pop('source_video_path', None)
            merged_extra_info.pop('_image_storage_state', None)
            merged_extra_info.pop('storage_error', None)
            input_extra_info = copy.deepcopy(entity_dict.get('extra_info') or {})
            input_extra_info.pop('source_video_path', None)
            if isinstance(input_extra_info, dict):
                input_extra_info.pop('actor_cache', None)
                for k_extra, v_extra in input_extra_info.items():
                    if v_extra is not None: merged_extra_info[k_extra] = v_extra

            if std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') and 'thumb' in entity_dict and not image_storage_incomplete:
                transforms = copy.deepcopy(input_extra_info.get('image_transforms') or {})
                if not any(isinstance(thumb, dict) and thumb.get('aspect') == 'poster' and
                           '/metadata/normal/jav_image?' in str(thumb.get('value') or '') for thumb in thumbs_list):
                    transforms.pop('poster', None)
                if transforms:
                    merged_extra_info['image_transforms'] = transforms
                else:
                    merged_extra_info.pop('image_transforms', None)

            # 정규화된 최신 이미지 주소가 이전 extra_info 값에 의해 덮어씌워지지 않도록 최종 확정 반영
            if resolved_landscape_url:
                merged_extra_info['landscape_url'] = resolved_landscape_url
            elif ('landscape', 1) in removed_image_slots:
                merged_extra_info.pop('landscape_url', None)
            if resolved_poster_url:
                merged_extra_info['poster_url'] = resolved_poster_url
            elif std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') and (not image_storage_incomplete or ('poster', 0) in removed_image_slots):
                merged_extra_info.pop('poster_url', None)

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
            merged_extra_info.pop('_storage_media', None)
            merged_extra_info.pop('unavailable_trailer', None)
            item.extra_info = copy.deepcopy(merged_extra_info)
            item.spec_data = copy.deepcopy(entity_dict.get('spec_data') or {})
            item.updated_time = datetime.now()

            previous_media_assets = [{
                'media_type': m.media_type, 'url': m.url, 'is_user': bool(m.is_user),
                'gdrive_fileid': m.gdrive_fileid, 'relative_path': m.relative_path,
                'source': m.source, 'checksum': m.checksum
            } for m in item.media_files] if std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') else []
            image_prefix = 'western' if std_cat == 'WESTERN' else 'jav_uncensored' if std_cat == 'JAV_UNCEN' else 'jav_censored'
            active_image_mode = P.ModelSetting.get(f'{image_prefix}_image_mode') or P.ModelSetting.get('jav_censored_image_mode') or 'ff_proxy'
            def user_asset(media):
                if std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
                    return MetaUtil.is_user_media(media)
                value = media.get('is_user') if isinstance(media, dict) else media.is_user
                url = media.get('url') if isinstance(media, dict) else media.url
                return bool(value) or '_user' in str(url or '').lower()

            incoming_image_slots = {(t.get('aspect'), 0 if t.get('aspect') == 'poster' else 1)
                                    for t in thumbs_list if isinstance(t, dict) and t.get('value')}
            incoming_image_slots.update(('fanart', index) for index, url in enumerate(fanarts_list) if url)
            preserved_user_media = [
                media for media in item.media_files
                if user_asset(media) or media.media_type in ('trailer', 'preview') or
                (image_storage_incomplete and (media.media_type, (media.sort_order or 0) if media.media_type == 'fanart' else 0 if media.media_type == 'poster' else 1) not in incoming_image_slots | removed_image_slots) or
                (std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') and media.media_type in ('poster', 'landscape', 'fanart') and
                 (media.gdrive_fileid or media.relative_path) and
                 not MetaUtil.media_in_storage(media, active_image_mode))
            ]
            for media in list(item.media_files):
                if media not in preserved_user_media:
                    item.media_files.remove(media)
                elif (std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') and user_asset(media)
                      and MetaUtil.media_in_storage(media, active_image_mode)):
                    media.is_user = True
                    media.source = 'user'
            item.tag_maps.clear()

            # 공식 미디어만 최신 JSON 기준으로 추가한다. 사용자 미디어는 위에서 보존한다.
            def add_media_asset(media_type, media_url, is_user=False, sort_order=0):
                clean_url = str(media_url or '').strip()
                observations = list(input_extra_info.get('_storage_media') or []) + list(entity_dict.get('media') or []) + previous_media_assets
                observed = next((asset for asset in observations if cls._media_asset_matches(asset, media_type, clean_url)), None)
                if observed:
                    is_user = is_user or user_asset(observed)
                if not cls._is_http_url(clean_url):
                    logger.warning(
                        "[MetaDB] 유효하지 않은 미디어 URL을 저장하지 않습니다: %s",
                        clean_url
                    )
                    return
                if is_user:
                    existing_user = next(
                        (m for m in item.media_files
                         if m.media_type == media_type and user_asset(m)
                         and (std_cat not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') or media_type != 'fanart' or m.sort_order == sort_order)
                         and MetaUtil.media_in_storage(m, 'gdrive' if 'drive.google.com' in clean_url or 'googleusercontent.com' in clean_url else 'image_server')),
                        None
                    )
                    if existing_user:
                        if std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
                            existing_user.is_user = True
                            existing_user.source = 'user'
                        existing_user.url = clean_url
                        existing_user.updated_time = datetime.now()
                        return
                is_gdrive = 'drive.google.com' in clean_url or 'googleusercontent.com' in clean_url
                gdrive_id = ''
                if is_gdrive:
                    match = re.search(r'[?&]id=([^&]+)', clean_url)
                    gdrive_id = match.group(1) if match else ''
                item.media_files.append(MetaMedia(
                    media_type=media_type,
                    url=clean_url,
                    is_user=is_user,
                    sort_order=sort_order,
                    gdrive_fileid=gdrive_id or None,
                    source='user' if is_user else ('gds' if is_gdrive else 'site'),
                    updated_time=datetime.now()
                ))

            for thumb in thumbs_list:
                if not isinstance(thumb, dict):
                    continue
                t_val = str(thumb.get('value') or '').strip()
                aspect = str(thumb.get('aspect') or '').lower()
                media_type = 'landscape' if aspect == 'landscape' else 'poster'
                add_media_asset(
                    media_type,
                    t_val,
                    is_user='_user' in t_val.lower(),
                    sort_order=0 if media_type == 'poster' else 1
                )

            for art_idx, art_url in enumerate(fanarts_list):
                if isinstance(art_url, dict):
                    art_url = art_url.get('value') or art_url.get('url')
                add_media_asset(
                    'fanart',
                    art_url,
                    is_user='_user' in str(art_url or '').lower(),
                    sort_order=art_idx
                )

            input_media_assets = list(input_extra_info.get('_storage_media') or [])
            input_media_assets += list(entity_dict.get('media') or [])
            # Latest observed paths win; prior paths require the same URL or Drive ID.
            input_media_assets += previous_media_assets
            if isinstance(input_media_assets, list):
                for media in item.media_files:
                    candidates = [
                        asset for asset in input_media_assets
                        if isinstance(asset, dict) and
                        asset.get('media_type') == media.media_type and
                        user_asset(asset) == user_asset(media)
                    ]
                    if candidates:
                        matches = [candidate for candidate in candidates
                                   if cls._media_asset_matches(candidate, media.media_type, media.url)]
                        asset = next((candidate for candidate in matches if candidate.get('relative_path')),
                                     matches[0] if matches else None)
                        if asset is None:
                            continue
                        media.gdrive_fileid = str(asset.get('gdrive_fileid') or '').strip() or media.gdrive_fileid
                        media.relative_path = str(asset.get('relative_path') or '').strip() or media.relative_path
                        media.source = ('user' if std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') and user_asset(media)
                                        else str(asset.get('source') or '').strip() or media.source)
                        media.checksum = str(asset.get('checksum') or '').strip() or media.checksum
                        media.updated_time = datetime.now()

            cls.sync_item_video_assets(item)

            user_poster = next(
                (media.url for media in item.media_files
                 if media.media_type == 'poster' and user_asset(media)
                 and cls._is_http_url(media.url) and MetaUtil.media_in_storage(media, P.ModelSetting.get(('western' if std_cat == 'WESTERN' else 'jav_uncensored' if std_cat == 'JAV_UNCEN' else 'jav_censored') + '_image_mode') or 'ff_proxy')),
                None
            )
            if user_poster:
                item.poster_url = user_poster
            elif not cls._is_http_url(item.poster_url):
                item.poster_url = resolved_poster_url if cls._is_http_url(resolved_poster_url) else ''

            if std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
                user_landscape = next((m.url for m in item.media_files if m.media_type == 'landscape'
                                       and user_asset(m) and cls._is_http_url(m.url)
                                       and MetaUtil.media_in_storage(m, active_image_mode)), None)
                if user_landscape:
                    merged_extra_info['landscape_url'] = user_landscape
                    item.extra_info = copy.deepcopy(merged_extra_info)

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
                        central_person.works_count = sum(len(v) for v in works_dict.values() if isinstance(v, list))

                final_idx = (central_person.person_idx if central_person else a_idx) or ''
                final_name_org = (central_person.name_org if central_person else (actor_data.get('name_org') or ''))
                final_name_ko = (central_person.name_ko if central_person else (actor_data.get('name_ko') or ''))
                final_name_en = (central_person.name_en if central_person else (actor_data.get('name_en') or ''))
                final_gender = (central_person.gender if central_person else None) or actor_data.get('gender') or ''

                actor_extra = copy.deepcopy(actor_data.get('extra_info') or {})
                if central_person and central_person.extra_info:
                    for k_exp, v_exp in central_person.extra_info.items():
                        if v_exp and not actor_extra.get(k_exp):
                            actor_extra[k_exp] = v_exp

                stored_actors.append({
                    'actor_idx': final_idx,
                    'name': final_name_ko or final_name_org or final_name_en,
                    'name_org': final_name_org,
                    'name_ko': final_name_ko,
                    'name_en': final_name_en,
                    'gender': final_gender,
                    'role': a_role,
                    'extra_info': actor_extra
                })

            if person_sess:
                try:
                    prev_actors = (item.extra_info or {}).get('_actors') or (item.extra_info or {}).get('actor_cache') or []
                    new_actor_indices = {a.get('actor_idx') for a in stored_actors if a.get('actor_idx')}
                    new_actor_names = {a.get('name_org') for a in stored_actors if a.get('name_org')}

                    for prev_a in prev_actors:
                        p_idx = prev_a.get('actor_idx')
                        p_name = prev_a.get('name_org')
                        is_removed = False
                        if p_idx and p_idx not in new_actor_indices:
                            is_removed = True
                        elif not p_idx and p_name and p_name not in new_actor_names:
                            is_removed = True

                        if is_removed:
                            p_target = None
                            if p_idx:
                                p_target = person_sess.query(MetaPerson).filter_by(domain=person_dom, person_idx=p_idx).first()
                            if not p_target and p_name:
                                p_target = person_sess.query(MetaPerson).filter_by(domain=person_dom, name_org=p_name).first()

                            if p_target and p_target.works and std_cat in p_target.works:
                                updated_w_list = []
                                for w_item in p_target.works[std_cat]:
                                    w_code = w_item.get('code') if isinstance(w_item, dict) else str(w_item)
                                    if w_code not in (code, ui_code, originaltitle):
                                        updated_w_list.append(w_item)
                                if len(updated_w_list) != len(p_target.works[std_cat]):
                                    p_target_works = copy.deepcopy(p_target.works)
                                    p_target_works[std_cat] = updated_w_list
                                    p_target.works = p_target_works
                                    p_target.works_count = sum(len(v) for v in p_target_works.values() if isinstance(v, list))
                                    logger.info(f"[MetaDB Auto-Healing] 오매칭 해제: 인물 [{p_target.name_ko or p_target.name_org}]의 출연작에서 [{code}] 자동 삭제 완료")
                except Exception as e_clean_prev:
                    logger.debug(f"[MetaDB Auto-Healing] 이전 배우 출연작 정리 예외: {e_clean_prev}")

            merged_extra_info['_actors'] = stored_actors
            item.extra_info = copy.deepcopy(merged_extra_info)

            # 장르 및 태그 매핑
            source_genres = orig_genres_list if orig_genres_list else genres_list
            seen_translated_genres = set()

            try:
                from support_site.constants import AV_GENRE_IGNORE_JA, AV_GENRE_IGNORE_KO
            except Exception:
                AV_GENRE_IGNORE_JA, AV_GENRE_IGNORE_KO = [], []

            for g_item in source_genres:
                if not g_item or not isinstance(g_item, str):
                    continue
                g_orig_clean = str(g_item).strip()
                if g_orig_clean in AV_GENRE_IGNORE_JA or "％OFF" in g_orig_clean:
                    continue

                from support_site import SiteAvBase
                g_name_clean = SiteAvBase.get_translated_tag(g_orig_clean)
                if not g_name_clean or g_name_clean in AV_GENRE_IGNORE_KO:
                    continue

                # 영상 레벨에서는 최종 번역 장르명이 중복되지 않도록 단일화
                if g_name_clean in seen_translated_genres:
                    continue
                seen_translated_genres.add(g_name_clean)

                # 원문(name_org) 기준으로 태그 레코드 조회 및 신규 등록
                tag_rec = s.query(MetaTag).filter_by(domain=domain, name_org=g_orig_clean, tag_type="genre").first()
                if not tag_rec:
                    try:
                        nested = s.begin_nested()
                        tag_rec = MetaTag(domain=domain, name=g_name_clean, name_org=g_orig_clean, tag_type="genre")
                        s.add(tag_rec)
                        s.flush()
                        nested.commit()
                    except Exception:
                        nested.rollback()
                        tag_rec = s.query(MetaTag).filter_by(domain=domain, name_org=g_orig_clean, tag_type="genre").first()
                elif tag_rec.name != g_name_clean:
                    tag_rec.name = g_name_clean

                if tag_rec:
                    item.tag_maps.append(MetaItemTagMap(tag_id=tag_rec.id))

            # 비디오 지문(Fingerprints) 3-Way 정밀 동기화
            # - 'site': 원격 사이트 최신 상태 반영 (사이트에서 삭제된 오류 지문은 로컬에서도 정리)
            # - 'user': 사용자가 로컬 영상에서 추가한 고유 지문은 사이트 갱신 시에도 영구 보존
            # - '승격': 유저 지문이 추후 사이트 정식 지문으로 등록되면 'site'로 자동 승격
            incoming_site_fps = {}
            incoming_user_fps = {}

            # 사이트 원본 공식 지문 수집 (StashDB, TPDB 원격 응답)
            if isinstance(entity_dict.get('original'), dict):
                for fp in (entity_dict['original'].get('fingerprints') or []):
                    if not isinstance(fp, dict): continue
                    algo = str(fp.get('algorithm') or 'OSHASH').strip().upper()
                    h_val = str(fp.get('hash') or fp.get('hash_value') or '').strip().lower()
                    if algo and h_val:
                        incoming_site_fps[f"{algo}_{h_val}"] = (algo, h_val)

            # 유저 추가 지문 및 extra_info 수집
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

            # 기존 DB에 저장되어 있던 유저 지문 보존 (사이트 갱신으로 인한 유실 방지)
            for existing_fp in item.fingerprints:
                if existing_fp.source == 'user':
                    key = f"{existing_fp.algorithm}_{existing_fp.hash_value}"
                    if key not in incoming_site_fps:
                        incoming_user_fps[key] = (existing_fp.algorithm, existing_fp.hash_value)

            # 최종 동기화 목록 조립 (사이트 지문 최우선, 중복 유저 지문은 사이트로 승격)
            final_fps_to_save = []
            for k, (algo, h_val) in incoming_site_fps.items():
                final_fps_to_save.append({'algorithm': algo, 'hash_value': h_val, 'source': 'site'})

            for k, (algo, h_val) in incoming_user_fps.items():
                if k not in incoming_site_fps:
                    final_fps_to_save.append({'algorithm': algo, 'hash_value': h_val, 'source': 'user'})

            # 테이블 및 extra_info 일괄 반영
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
                item.extra_info = copy.deepcopy(merged_extra_info)

            nested_item.commit()
            if nested_person:
                nested_person.commit()

            if target_session is None:
                from sqlalchemy import event
                actual_session = s() if hasattr(s, 'registry') else s
                def committed(session):
                    outcome['state'] = 'committed'
                event.listen(actual_session, 'after_commit', committed, once=True)
                outcome['state'] = 'unknown'
                s.commit()
                outcome['state'] = 'committed'
                if person_sess and is_internal_person_sess and person_sess is not s:
                    person_sess.commit()
            return True

        except Exception as e:
            logger.error(f"[MetaDB] save_metadata 실패 ({code}): {e}")
            logger.error(traceback.format_exc())
            if target_session is None:
                for cleanup_session in (s, person_sess if is_internal_person_sess and person_sess is not s else None):
                    if cleanup_session is not None:
                        try:
                            cleanup_session.rollback()
                        except Exception:
                            logger.exception('[MetaDB] save_metadata rollback 정리 보류 (%s)', code)
            return outcome['state'] == 'committed'
        finally:
            for cleanup_session in (s if target_session is None else None,
                                    person_sess if is_internal_person_sess and person_sess is not s else None):
                if cleanup_session is not None:
                    try:
                        cleanup_session.remove()
                    except Exception:
                        logger.exception('[MetaDB] save_metadata 세션 정리 보류 (%s)', code)

    @classmethod
    def _schema_write_guard(cls, category, operation):
        status = cls.get_meta_schema_status(category)
        if (
            int(status.get('schema_version') or 0) < cls.META_DB_SCHEMA_VERSION or
            status.get('migration_state') != 'complete' or not cls._ensure_meta_db_writable(category)
        ):
            msg = (
                f"{category} 메타 DB가 최신 스키마가 아닙니다 "
                f"(v{status.get('schema_version', 0)}). '{operation}' 전에 "
                "메타 DB 마이그레이션을 실행하세요."
            )
            logger.warning(f"[MetaDB Schema] {msg}")
            return False, msg
        return True, ""

    @classmethod
    def sync_jav_actors_db(cls, progress_callback=None):
        """배포된 jav_actors_YYYYMMDD.db 파일을 읽어 인물 DB의 신규 물리 컬럼과 원장을 완벽하게 동기화."""
        def progress(**fields):
            if progress_callback:
                progress_callback(**fields)

        progress(phase='checking', status='배우 DB 및 스키마 확인 중', current=0, total=0)
        allowed, guard_msg = cls._schema_write_guard('PERSON', 'JAV 배우 DB 동기화')
        if not allowed:
            return False, guard_msg
        cls.ensure_db_ready()
        target_db_path, file_ver = cls.find_latest_jav_actors_db()
        if not target_db_path:
            return False, "배우 배포 DB(jav_actors_*.db) 파일을 찾을 수 없습니다."

        target_engine = cls._engines.get('postgres') if cls._is_postgres else cls._engines.get('person')
        if not target_engine:
            return False, "인물 DB 엔진 획득 실패"

        sync_session_factory = sessionmaker(bind=target_engine, autocommit=False, autoflush=False, expire_on_commit=False)
        sess = sync_session_factory()

        t_start = time.time()
        conn = None
        raw_rows = []

        try:
            try:
                conn = sqlite3.connect(target_db_path)
                conn.row_factory = sqlite3.Row
                c = conn.cursor()
                progress(phase='reading', status='배포 배우 DB 읽는 중', current=0, total=0)
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

            progress(phase='cluster_group', status='배포 배우 중복 분류 준비 중', current=0, total=len(raw_rows))
            merged_clusters = cls._cluster_and_merge_actor_rows(raw_rows, progress_callback=progress)

            progress(phase='indexing', status='기존 인물 DB 식별 정보 색인 중', current=0, total=0)
            existing_persons = sess.query(MetaPerson).filter_by(domain="JAV").yield_per(500)
            person_idx_map = {}
            ambiguous_indices = set()
            for p in existing_persons:
                for alt_id in [p.person_idx] + list(p.alt_indices or []):
                    if not alt_id:
                        continue
                    if alt_id in person_idx_map and person_idx_map[alt_id].id != p.id:
                        ambiguous_indices.add(alt_id)
                    else:
                        person_idx_map[alt_id] = p

            # 소장 작품 목록 수집
            existing_works_map = {}
            for category_index, cat in enumerate(['JAV_CEN', 'JAV_UNCEN', 'WESTERN']):
                progress(phase='works', status='소장 출연작 색인 중', current=category_index, total=3, current_db=cat)
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
                        ).filter(MetaItem.category == cat).yield_per(500)

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

            inserted, updated, merged_duplicates_count, ambiguous_count = 0, 0, 0, 0
            batch_size = 500

            progress(phase='sync', status='배우 DB 반영 중', current=0, total=len(merged_clusters), current_db='PERSON')
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

                    if not name_ko:
                        for sub_item in merged_sub_actors:
                            sub_ko_val = str(sub_item.get('name_ko') or '').strip()
                            if sub_ko_val:
                                name_ko = sub_ko_val
                                break

                    if not name_ko:
                        continue

                    local_path_val = str(best_r.get('local_img_path') or '').strip()
                    if local_path_val:
                        parts = [p for p in local_path_val.replace('\\', '/').strip('/').split('/') if p]
                        local_path_val = f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else local_path_val

                    site_img_url_val = str(best_r.get('site_img_url') or '').strip()
                    google_fileid_val = str(best_r.get('google_fileid') or '').strip()

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

                    h_val = cls._parse_integer(best_r.get('profile_height') or best_r.get('height'))
                    birth_val = str(best_r.get('birth') or '').strip()
                    m_by = re.search(r'\b(19\d{2}|20\d{2})\b', birth_val)
                    birth_year_val = int(m_by.group(1)) if m_by else None

                    debut_val = str(best_r.get('debut') or '').strip()
                    body_size_val = str(best_r.get('body_size') or '').strip()
                    bust_val, waist_val, hip_val, cup_val = cls._parse_body_size(body_size_val)

                    raw_bra = str(best_r.get('bra_size') or '').strip().upper()
                    clean_bra = re.sub(r'[^A-Z]', '', raw_bra)
                    if clean_bra:
                        cup_val = clean_bra

                    agency_val = str(best_r.get('agency') or '').strip()
                    blood_val = str(best_r.get('blood') or '').strip().upper().replace('형', '')
                    info_url_val = str(best_r.get('info_url') or '').strip() or f"https://www.avdbs.com/menu/actor.php?actor_idx={master_actor_id.replace('PA', '')}"

                    media_src_data = cls.build_person_media_payload({
                        'official': {
                            'local_img_path': local_path_val,
                            'google_fileid': google_fileid_val,
                            'site_img_url': site_img_url_val,
                            'site_img_urls': site_photos,
                        }
                    }, domain='JAV')

                    matches = {person_idx_map[key].id for key in alt_indices if key in person_idx_map}
                    if any(key in ambiguous_indices for key in alt_indices) or len(matches) > 1:
                        ambiguous_count += 1
                        logger.warning(f'[MetaDB ActorSync] 인물 식별 충돌로 제외: {master_actor_id} / {alt_indices}')
                        continue
                    p_rec = None
                    for check_id in alt_indices:
                        if check_id in person_idx_map:
                            p_rec = person_idx_map[check_id]
                            break

                    existing_exclusions = set((p_rec.extra_info or {}).get('split_exclusions', []) if p_rec else [])
                    filtered_alt_indices = [x for x in alt_indices if x not in existing_exclusions]
                    filtered_sub_actors = [x for x in merged_sub_actors if x.get('actor_id') not in existing_exclusions]
                    works_count_val = sum(len(w) for w in combined_works.values())

                    source_data = {
                        'name_org': name_org,
                        'name_ko': name_ko,
                        'name_en': name_en,
                        'birth': birth_val,
                        'height': h_val,
                        'body_size': body_size_val,
                        'bra_size': cup_val,
                        'debut': debut_val,
                        'info_url': info_url_val,
                        'agency': agency_val,
                        'blood': blood_val,
                        'hobby': str(best_r.get('hobby') or '').strip(),
                        'specialty': str(best_r.get('specialty') or '').strip(),
                        'other_names': ", ".join(merged_aliases),
                        'source': 'jav_actors',
                        'gender': 'female', 'country': '일본',
                        'updated_at': datetime.now().isoformat(timespec='seconds'),
                    }
                    extra_data = {
                        'hobby': str(best_r.get('hobby') or '').strip(),
                        'specialty': str(best_r.get('specialty') or '').strip(),
                        'source_origin': 'jav_actors',
                        'split_exclusions': list(existing_exclusions),
                    }

                    # 활성 대표 썸네일 URL 산출
                    active_thumb_val = cls.resolve_actor_thumb_url(media_src_data, domain='JAV')
                    has_photo_val = bool(active_thumb_val and str(active_thumb_val).strip())

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
                            gender="female",
                            birth_date=birth_val,
                            birth_year=birth_year_val,
                            debut_date=debut_val,
                            height=h_val,
                            bust=bust_val,
                            waist=waist_val,
                            hip=hip_val,
                            body_size=body_size_val,
                            cup_size=cup_val,
                            blood_type=blood_val,
                            agency=agency_val,
                            country="일본",
                            info_url=info_url_val,
                            has_photo=has_photo_val,
                            thumb_url=active_thumb_val,
                            local_path=local_path_val,
                            gdrive_fileid=google_fileid_val,
                            custom_gdrive_fileid=None,
                            custom_local_path=None,
                            site_img_url=site_img_url_val,
                            photo_urls=site_photos,
                            merged_count=len(filtered_alt_indices) if len(filtered_alt_indices) > 1 else 1,
                            alt_indices=filtered_alt_indices,
                            merged_details=filtered_sub_actors,
                            works_count=works_count_val,
                            source_data=source_data,
                            manual_data={},
                            media_src=media_src_data,
                            works=combined_works,
                            extra_info=extra_data
                        )
                        sess.add(p_rec)
                        inserted += 1
                        for registered_id in filtered_alt_indices:
                            person_idx_map[registered_id] = p_rec
                    else:
                        old_name_ko = str(p_rec.name_ko or '').strip()
                        old_media = cls.person_media(p_rec)
                        old_local_img = str(old_media['custom'].get('local_img_path') or p_rec.custom_local_path or '').strip()
                        _, _, manual_data = cls.normalize_person_extra(p_rec.extra_info, person=p_rec)

                        current_extra = copy.deepcopy(p_rec.extra_info or {})
                        for redundant_key in [
                            'birth', 'height', 'body_size', 'bra_size', 'debut', 'agency', 'blood',
                            'info_url', 'name_org', 'name_ko', 'name_en', 'other_names',
                            'source', 'updated_at', 'source_data', 'manual_data',
                            'alt_actor_indices', 'merged_sub_actors'
                        ]:
                            current_extra.pop(redundant_key, None)
                        current_extra.update(extra_data)
                        p_rec.extra_info = current_extra

                        # 수동 수정 보호(manual_data)를 고려한 물리 컬럼 반영
                        p_rec.source_data = source_data
                        p_rec.manual_data = manual_data
                        if 'gender' not in manual_data: p_rec.gender = "female"
                        if 'country' not in manual_data: p_rec.country = "일본"

                        if 'name_org' not in manual_data: p_rec.name_org = name_org
                        if 'name_ko' not in manual_data: p_rec.name_ko = name_ko
                        if 'name_en' not in manual_data: p_rec.name_en = name_en
                        if 'height' not in manual_data: p_rec.height = h_val
                        if 'birth' not in manual_data:
                            p_rec.birth_date = birth_val
                            p_rec.birth_year = birth_year_val
                        if 'debut' not in manual_data: p_rec.debut_date = debut_val
                        if 'body_size' not in manual_data:
                            p_rec.body_size = body_size_val
                            p_rec.bust = bust_val
                            p_rec.waist = waist_val
                            p_rec.hip = hip_val
                        if 'cup_size' not in manual_data and 'bra_size' not in manual_data:
                            p_rec.cup_size = cup_val
                        if 'agency' not in manual_data: p_rec.agency = agency_val
                        if 'blood' not in manual_data: p_rec.blood_type = blood_val
                        if 'info_url' not in manual_data: p_rec.info_url = info_url_val

                        # 별칭 합집합
                        combined_alias_set = set(p_rec.aliases or [])
                        combined_alias_set.update(merged_aliases)
                        p_rec.aliases = list(combined_alias_set)
                        if 'other_names' not in manual_data:
                            p_rec.other_names = ", ".join(p_rec.aliases)

                        # 미디어 자산 동기화
                        existing_media = cls.person_media(p_rec)
                        existing_official = existing_media['official']
                        existing_custom = existing_media['custom']
                        existing_photos = existing_official.get('site_img_urls', [])
                        for sp in site_photos:
                            if sp not in existing_photos:
                                existing_photos.append(sp)
                        existing_official['site_img_urls'] = existing_photos

                        effective_name = p_rec.name_ko or p_rec.name_org or ''
                        name_or_path_changed = old_name_ko != effective_name or (old_local_img and local_path_val and old_local_img != local_path_val)
                        if name_or_path_changed:
                            renamed = MetaUtil.sync_renamed_person_user_image(
                                p_rec, old_name_ko, effective_name, old_local_img, local_path_val, p_rec.person_idx
                            )
                            if renamed:
                                existing_custom.update(renamed)

                        existing_official['local_img_path'] = local_path_val
                        existing_official['google_fileid'] = google_fileid_val
                        existing_official['site_img_url'] = site_img_url_val
                        existing_media['official'] = existing_official
                        existing_media['custom'] = existing_custom
                        p_rec.media_src = existing_media

                        # 물리 미디어 컬럼 동기화
                        p_rec.local_path = local_path_val
                        p_rec.gdrive_fileid = google_fileid_val
                        p_rec.site_img_url = site_img_url_val
                        p_rec.photo_urls = existing_photos
                        if existing_custom.get('custom_user_google_fileid'):
                            p_rec.custom_gdrive_fileid = existing_custom['custom_user_google_fileid']
                        if existing_custom.get('local_img_path'):
                            p_rec.custom_local_path = existing_custom['local_img_path']

                        # 썸네일 캐시 및 사진 여부 갱신
                        cls.sync_person_media_columns(p_rec)
                        final_thumb = cls.resolve_person_active_thumb(p_rec)
                        p_rec.thumb_url = final_thumb
                        p_rec.has_photo = bool(final_thumb and str(final_thumb).strip())

                        p_rec.merged_count = len(filtered_alt_indices) if len(filtered_alt_indices) > 1 else 1
                        p_rec.alt_indices = filtered_alt_indices
                        p_rec.merged_details = filtered_sub_actors

                        if combined_works:
                            p_rec.works = combined_works
                        p_rec.works_count = works_count_val

                        updated += 1
                        for registered_id in filtered_alt_indices:
                            person_idx_map[registered_id] = p_rec

                    if idx % batch_size == 0:
                        sess.commit()

                except Exception as e_cluster:
                    sess.rollback()
                    logger.error(f"[MetaDB ActorSync] 클러스터 처리 오류 (#{idx}): {e_cluster}")
                    raise
                finally:
                    if idx % 100 == 0 or idx == len(merged_clusters):
                        progress(phase='sync', status='배우 DB 반영 중', current=idx, total=len(merged_clusters),
                                 current_db=master_actor_id if 'master_actor_id' in locals() else 'PERSON',
                                 inserted=inserted, updated=updated, skipped=idx - inserted - updated,
                                 conflicts=ambiguous_count)

            progress(phase='committing', status='배우 DB 저장 및 캐시 갱신 중', current=0, total=0, current_db='PERSON')
            sess.commit()

            cls._cached_actors_map = None
            P.ModelSetting.set("meta_db_person_jav_last_synced_version", file_ver)
            elapsed = time.time() - t_start

            raw_total_count = len(raw_rows)
            unique_cluster_count = len(merged_clusters)
            source_merged_count = max(0, raw_total_count - unique_cluster_count)

            msg = (
                f"동기화 성공 ({file_ver}): 원본 {raw_total_count:,}건 중 {source_merged_count:,}건 중복 통합 ➔ "
                f"고유 인물 {unique_cluster_count:,}명 반영 (신규: {inserted:,}건, 갱신: {updated:,}건, "
                f"식별 충돌 제외: {ambiguous_count:,}건, 소요시간: {elapsed:.2f}초)"
            )
            progress(phase='complete', status='배우 DB 동기화 완료', current=unique_cluster_count,
                     total=unique_cluster_count, inserted=inserted, updated=updated,
                     skipped=unique_cluster_count - inserted - updated, conflicts=ambiguous_count,
                     source_total=raw_total_count, merged=source_merged_count, current_db='PERSON')
            logger.info(f"[MetaDB ActorSync] {msg}")
            return True, msg

        except Exception as e:
            sess.rollback()
            logger.error(f"[MetaDB ActorSync] 치명적 오류 발생: {e}")
            logger.error(traceback.format_exc())
            return False, f"동기화 중 치명적 오류 발생: {str(e)}"
        finally:
            sess.close()
            cls.checkpoint_wal()


    @classmethod
    def get_metadata(cls, code, category):
        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess: return None

        try:
            from sqlalchemy.orm import selectinload
            item = sess.query(MetaItem).options(
                selectinload(MetaItem.media_files),
                selectinload(MetaItem.person_maps),
                selectinload(MetaItem.tag_maps),
                selectinload(MetaItem.fingerprints),
            ).filter_by(code=code, category=std_cat).first()
            if item: return cls.to_entity_dict(item)
            return None
        except Exception as e:
            logger.error(f"[MetaDB] get_metadata 에러 ({code}): {e}")
            return None
        finally:
            sess.remove()


    @classmethod
    def find_item_category(cls, code, hint_category=None):
        """코드(code)를 기반으로 해당 아이템이 실제로 존재하는 카테고리(JAV_CEN, JAV_UNCEN, WESTERN 등)를 탐색하여 반환"""
        if not code:
            return None

        cls.ensure_db_ready()

        candidate_cats = []
        if hint_category:
            hint_clean = str(hint_category).strip().upper()
            if hint_clean in DOMAIN_MAP:
                candidate_cats.append(hint_clean)

        code_str = str(code).strip()
        if code_str.startswith(('WS', 'WP')):
            if 'WESTERN' not in candidate_cats:
                candidate_cats.append('WESTERN')
        elif code_str.startswith('E'):
            if 'JAV_UNCEN' not in candidate_cats:
                candidate_cats.append('JAV_UNCEN')
        elif code_str.startswith('C'):
            if 'JAV_CEN' not in candidate_cats:
                candidate_cats.append('JAV_CEN')

        for cat_cand in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'MOVIE', 'KTV', 'FTV']:
            if cat_cand not in candidate_cats and cat_cand in DOMAIN_MAP:
                candidate_cats.append(cat_cand)

        for cat_to_check in candidate_cats:
            sess, _, std_cat = cls.get_session_and_domain(cat_to_check)
            if not sess:
                continue
            try:
                exists = sess.query(MetaItem.id).filter_by(code=code_str, category=std_cat).first()
                if exists:
                    return std_cat
            except Exception as e_find:
                logger.debug(f"[MetaDB] find_item_category 탐색 예외 ({cat_to_check}, {code_str}): {e_find}")
            finally:
                sess.remove()

        if hint_category and str(hint_category).strip().upper() in DOMAIN_MAP:
            return str(hint_category).strip().upper()
        return 'JAV_CEN'


    @staticmethod
    def normalize_batch_request(category, codes):
        if category is not None and not isinstance(category, str):
            raise ValueError('category는 문자열이어야 합니다.')
        raw = str(category or '').strip().upper()
        aliases = {'JAV_CENSORED': 'JAV_CEN', 'CENSORED': 'JAV_CEN',
                   'JAV_UNCENSORED': 'JAV_UNCEN', 'UNCENSORED': 'JAV_UNCEN', 'WEST': 'WESTERN'}
        target = aliases.get(raw, raw) if raw else 'JAV_CEN'
        if target not in DOMAIN_MAP:
            raise ValueError('지원하지 않는 category입니다.')
        if isinstance(codes, str):
            text_codes = codes.strip()
            try:
                requested = json.loads(text_codes)
            except ValueError:
                if not text_codes or any(char in text_codes for char in '[]{}"'):
                    raise ValueError('codes JSON 형식 오류') from None
                requested = re.split(r'[\s,]+', text_codes)
        else:
            requested = codes
        if not isinstance(requested, (list, tuple, set)) or any(not isinstance(code, str) for code in requested):
            raise ValueError('codes는 문자열 코드 목록 또는 지원하는 목록 문자열이어야 합니다.')
        clean = [code.strip() for code in requested if code.strip()]
        if not clean:
            raise ValueError('조회할 codes가 없습니다.')
        return target, clean, len(requested)

    @staticmethod
    def _is_av_internal_code(code):
        return bool(re.match(r'^(?:C[BDTMJ]|E[A-Z]|W[SP])', str(code or ''), re.I))

    @classmethod
    def get_metadata_batch(cls, category, codes, options=None):
        """
        요청된 고유 코드(최대 100건)를 정확 조회하고 관계를 분리 로딩하여
        {요청코드: 메타데이터} 딕셔너리로 반환합니다.
        """
        target_cat, clean_codes, _ = cls.normalize_batch_request(category, codes)
        cls.ensure_db_ready()

        sess, domain, std_cat = cls.get_session_and_domain(target_cat)
        if not sess:
            raise RuntimeError('메타 DB 세션을 획득할 수 없습니다.')

        opts = dict(options or {})
        max_batch_size = 100

        try:
            if len(clean_codes) > max_batch_size:
                logger.debug(f"[MetaDB Batch Info] 요청된 코드 수({len(clean_codes)}건)가 최대 한도({max_batch_size}건)를 초과하여 상위 {max_batch_size}건만 처리합니다.")
                clean_codes = clean_codes[:max_batch_size]

            raw_code_set = set(clean_codes)

            from sqlalchemy.orm import selectinload
            # 고유 코드 후보를 한 번 조회하고 네 컬렉션은 별도 SELECT로 로딩
            items = sess.query(MetaItem).options(
                selectinload(MetaItem.media_files),
                selectinload(MetaItem.person_maps),
                selectinload(MetaItem.tag_maps),
                selectinload(MetaItem.fingerprints),
            ).filter(
                MetaItem.category == std_cat,
                MetaItem.code.in_(raw_code_set),
            ).all()

            # DB 고유 코드만 색인하고 요청 순서대로 반환
            exact_lookup = {}
            for item in items:
                entity_dict = cls.to_entity_dict(item)
                final_data = MetaUtil.finalize_info_return(entity_dict, extra_opts=opts, category=std_cat)
                exact_lookup[item.code] = final_data

            # 원본 요청 코드 키를 보존하며 결과 딕셔너리 조립
            result_dict = {}
            for req_code in clean_codes:
                matched_meta = exact_lookup.get(req_code)
                if matched_meta:
                    result_dict[req_code] = matched_meta

            logger.debug(f"[MetaDB Batch Info] [{std_cat}] 대량 조회 완료: 요청 {len(clean_codes)}건 중 {len(result_dict)}건 검색 성공")
            return result_dict

        except Exception as e:
            logger.exception('[MetaDB Batch Info] 조회 실패: category=%s', std_cat)
            raise RuntimeError('메타 DB 일괄 조회에 실패했습니다.') from e
        finally:
            sess.remove()


    def _metadata_batch_response(self, category, codes, options=None):
        if options is not None and not isinstance(options, dict):
            return jsonify({'ret': 'error', 'error_code': 'invalid_request', 'msg': 'options는 JSON 객체여야 합니다.'}), 400
        try:
            target, clean, total_requested = self.normalize_batch_request(category, codes)
        except ValueError as exc:
            return jsonify({'ret': 'error', 'error_code': 'invalid_request', 'msg': str(exc)}), 400
        try:
            data = self.get_metadata_batch(target, clean, options=options)
            return jsonify({'ret': 'success', 'category': category, 'total_requested': total_requested,
                            'total_found': len(data), 'data': data}), 200
        except Exception:
            logger.exception('[MetaDB API] 일괄 조회 실패 (%s)', category)
            return jsonify({'ret': 'error', 'error_code': 'lookup_failed',
                            'msg': '메타 DB 조회에 실패했습니다. FF 로그를 확인하세요.'}), 500

    def _storage_sync_response(self, operation, params):
        """Execute one item synchronously; PMH owns scheduling, retries and progress."""
        def error(code, message, status=400):
            return jsonify({'ret': 'error', 'error_code': code, 'msg': message,
                            'is_updated': False, 'skipped': False, 'changes': []}), status
        if not isinstance(params, dict):
            return error('invalid_request', '요청은 JSON 객체여야 합니다.')
        options = params.get('options')
        if options is None:
            options = {}
        if not isinstance(options, dict):
            return error('invalid_request', 'options는 JSON 객체여야 합니다.')
        allowed_options = ('skip_existing', 'poster', 'landscape', 'fanart', 'trailer') if operation == 'media_sync' else ('skip_existing',)
        if any(key not in allowed_options for key in options) or any(type(value) is not bool for value in options.values()):
            return error('invalid_request', '지원하는 옵션만 JSON boolean으로 지정하세요.')
        result = None
        try:
            if operation == 'media_sync':
                category = str(params.get('category') or '').strip().upper()
                code = params.get('code')
                if category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') or not isinstance(code, str) or not code.strip():
                    return error('invalid_request', 'AV category와 작품 code를 지정하세요.')
                allowed, message = self._schema_write_guard(category, '단건 미디어 저장소 동기화')
                if not allowed:
                    return error('schema_not_writable', message, 409)
                batch_options = {'media_sync_' + key: value for key, value in options.items()}
                result = MetaUtil.sync_item_media_to_storage(None, code.strip(), category,
                                                           batch_opts=batch_options, use_dir_cache=False)
                result.update(code=code.strip(), category=category)
            else:
                domain = str(params.get('domain') or '').strip().upper()
                person_idx = params.get('person_idx')
                if domain not in ('JAV', 'WESTERN') or not isinstance(person_idx, str) or not person_idx.strip():
                    return error('invalid_request', 'JAV/WESTERN domain과 인물 코드 person_idx를 지정하세요.')
                result = self.sync_person_image_to_storage(person_idx.strip(), domain,
                            skip_existing=options.get('skip_existing', True), use_dir_cache=False)
                result.setdefault('person_idx', person_idx.strip())
                result.setdefault('requested_person_idx', person_idx.strip())
                result.setdefault('domain', domain)
            result.setdefault('changes', [])
            result.setdefault('is_updated', False)
            result['skipped'] = result.get('ret') == 'success' and not result['is_updated'] and not result.get('failures')
            if result.get('failures'):
                result.update(ret='error', error_code='media_sync_incomplete', skipped=False,
                              msg='일부 미디어 처리에 실패했습니다. changes와 failures를 확인하세요.')
                return jsonify(result), 502
            if result.get('ret') != 'success':
                code = result.get('error_code') or 'storage_sync_failed'
                status = {'not_found': 404, 'ambiguous_person': 409, 'schema_not_writable': 409,
                          'storage_conflict': 409, 'storage_lookup_failed': 502, 'storage_asset_incomplete': 502,
                          'commit_outcome_unknown': 503,
                          'unsupported_domain': 400, 'download_failed': 502, 'upload_failed': 502}.get(code, 500)
                result.update(error_code=code)
                if status == 500:
                    result['msg'] = '저장소 동기화에 실패했습니다. FF 로그를 확인하세요.'
                return jsonify(result), status
            result.setdefault('msg', result.get('detail_msg') or ('변경 없음' if result['skipped'] else '동기화 완료'))
            return jsonify(result), 200
        except Exception:
            logger.exception('[MetaDB API] 단건 저장소 동기화 실패 (%s)', operation)
            if isinstance(result, dict) and result.get('commit_state') in ('committed', 'unknown'):
                result['cleanup_pending'] = True
                result.setdefault('warnings', []).append('response_preparation_failed')
                status = 503 if result['commit_state'] == 'unknown' else 502 if result.get('failures') else 200
                return jsonify(result), status
            return error('storage_sync_failed', '저장소 동기화에 실패했습니다. FF 로그를 확인하세요.', 500)

    @classmethod
    def sync_person_image_to_storage(cls, person_idx=None, domain=None, skip_existing=True,
                                     use_dir_cache=True, *, person_id=None, source_url=None):
        """Sync one official cache. Public identity is domain + person_idx, never a name."""
        from sqlalchemy.orm import sessionmaker
        from .util_preview import MetaPreviewUtil
        from support_site import SiteAvBase
        from sqlalchemy import cast, Text
        from tempfile import mkstemp
        from .util_runtime import stage_storage_record, commit_storage_record, batch_stage
        from .util_preview import MediaReplacement

        result = {'ret': 'success', 'is_updated': False, 'changes': []}
        def failure(code, message):
            result.update(ret='error', error_code=code, msg=message, is_updated=False, changes=[])
            return result
        allowed, message = cls._schema_write_guard('PERSON', '인물 이미지 저장소 동기화')
        if not allowed:
            return failure('schema_not_writable', message)
        registry, _, _ = cls.get_session_and_domain('PERSON')
        if registry is None:
            return failure('database_unavailable', '인물 DB 세션 획득 실패')
        sess = sessionmaker(bind=registry.get_bind(), expire_on_commit=False)()
        staging = None
        subject = {}
        operation = None
        committed = False
        sess.info['storage_commit_state'] = 'not_attempted'
        try:
            query = sess.query(MetaPerson)
            if person_id is not None:
                query = query.filter(MetaPerson.id == person_id)
                if domain:
                    query = query.filter(func.upper(MetaPerson.domain) == str(domain).upper())
                matches = query.all()
            else:
                requested = str(person_idx or '').strip().casefold()
                if not requested or str(domain or '').upper() not in ('JAV', 'WESTERN'):
                    return failure('unsupported_domain', '인물 코드와 JAV/WESTERN 도메인이 필요합니다.')
                # SQL narrows JSON alias candidates; exact comparison rejects substrings/wildcards.
                candidates = query.filter(func.upper(MetaPerson.domain) == str(domain).upper(), or_(
                    func.lower(MetaPerson.person_idx) == requested,
                    func.lower(cast(MetaPerson.alt_indices, Text)).contains(requested, autoescape=True))).all()
                matches = [p for p in candidates if requested in
                           {str(value).strip().casefold() for value in [p.person_idx] + list(p.alt_indices or [])}]
            if not matches:
                return failure('not_found', '등록된 인물 코드를 찾을 수 없습니다.')
            if len(matches) != 1:
                return failure('ambiguous_person', '인물 코드가 여러 레코드에 연결되어 있습니다.')
            person = matches[0]
            snapshot = stage_storage_record(sess, person)
            subject = MediaReplacement.acquire_subject(os.path.join(path_data, 'db', 'media_replacements'),
                                                       person.domain, person.person_idx)
            current = sess.query(MetaPerson).filter_by(id=person.id).first()
            if person_id is None:
                candidates = sess.query(MetaPerson).filter(func.upper(MetaPerson.domain) == str(domain).upper(), or_(
                    func.lower(MetaPerson.person_idx) == requested,
                    func.lower(cast(MetaPerson.alt_indices, Text)).contains(requested, autoescape=True))).all()
                canonical_matches = [p.id for p in candidates if requested in
                    {str(value).strip().casefold() for value in [p.person_idx] + list(p.alt_indices or [])}]
                if canonical_matches != [person.id]:
                    from .util_runtime import StorageConflict
                    raise StorageConflict('잠금 획득 중 인물 코드 매핑이 변경되었습니다.')
            current_snapshot = stage_storage_record(sess, current) if current is not None else None
            requested = str(person_idx or person.person_idx).strip().casefold()
            if (current_snapshot != snapshot or current is None or requested not in
                    {str(value).strip().casefold() for value in [current.person_idx] + list(current.alt_indices or [])}):
                from .util_runtime import StorageConflict
                raise StorageConflict('잠금 획득 중 인물 식별 또는 DB 정보가 변경되었습니다.')
            person = current
            dom = str(person.domain or '').upper()
            if dom not in ('JAV', 'WESTERN'):
                return failure('unsupported_domain', 'AV 인물만 동기화할 수 있습니다.')
            prefix = 'western' if dom == 'WESTERN' else 'jav_censored'
            mode = P.ModelSetting.get(f'{prefix}_actor_image_mode') or ('site' if dom == 'WESTERN' else 'gds')
            result.update({'ret': 'success', 'person_idx': person.person_idx, 'domain': dom,
                      'requested_person_idx': person_idx or person.person_idx, 'storage_type': mode,
                      'is_updated': False, 'changes': []})
            if mode not in ('gdrive', 'image_server'):
                result.update(skipped=True, reason='storage_mode_not_enabled', msg='개인 이미지 저장소 모드가 아닙니다.')
                return result
            before = (copy.deepcopy(person.media_src), person.official_cached_gdrive_fileid,
                      person.official_cached_local_path, person.official_remote_path, person.thumb_url, person.has_photo)
            media = cls.person_media(person)
            if source_url:
                media['official']['site_img_url'] = source_url
            relative_path = SiteAvBase.actor_storage_relative_path(person, dom, mode)
            folder = os.path.join(path_data, 'db', 'media_replacements')
            target = (MetaPreviewUtil.build_rclone_target_remote_path(
                P.ModelSetting.get('jav_censored_image_gdrive_remote_path'), relative_path) if mode == 'gdrive' else
                os.path.join(P.ModelSetting.get('jav_censored_image_server_local_path') or '/data/images',
                             relative_path.replace('/', os.path.sep)))
            MediaReplacement.assert_available(folder, target)
            def begin_replacement(old_id=''):
                nonlocal operation
                owner = dict(subject, subject_type='person', person_id=person.id,
                    person_idx=person.person_idx, person_domain=person.domain, old_id=old_id)
                operation = MetaUtil.prepare_storage_replacement(person.person_idx, dom, mode,
                    relative_path, 'official', subject=owner)
                MediaReplacement.stage(operation)
            def prepare_image():
                nonlocal staging
                source = ('https://drive.google.com/thumbnail?id=' + media['official']['google_fileid'] + '&sz=s0'
                          if dom == 'JAV' and media['official'].get('google_fileid') else '')
                source = source or media['official'].get('site_img_url') or person.site_img_url
                if source_url:
                    source = source_url
                source = source or next(iter(media['official'].get('site_img_urls') or []), '')
                if not source:
                    result.update(skipped=True, reason='source_unavailable', msg='다운로드 가능한 공식 이미지가 없습니다.')
                    return result
                with batch_stage(logger, 'Person image download', person.person_idx):
                    response = SiteAvBase.get_response(source, timeout=15)
                if response is None or response.status_code != 200:
                    return failure('download_failed', '공식 이미지 다운로드에 실패했습니다.')
                temp_dir = os.path.join(path_data, 'tmp')
                os.makedirs(temp_dir, exist_ok=True)
                fd, staging = mkstemp(prefix='person_sync_', suffix='.jpg', dir=temp_dir)
                os.close(fd)
                if not SiteAvBase._save_image_data(BytesIO(response.content), staging):
                    return failure('download_failed', '공식 이미지 파일 생성에 실패했습니다.')
                return None
            uploaded = False
            if mode == 'gdrive':
                fid = MetaPreviewUtil.get_remote_file_id(relative_path, category=dom, is_image=True,
                                                       use_dir_cache=use_dir_cache)
                if fid and skip_existing:
                    relative_path = MetaPreviewUtil.get_recorded_remote_path(fid, relative_path, category=dom, is_image=True)
                else:
                    prepared = prepare_image()
                    if prepared is not None:
                        return prepared
                    begin_replacement(fid or '')
                    with batch_stage(logger, 'Person image upload', person.person_idx):
                        fid = MetaUtil.upload_image_to_gdrive(staging, relative_path, category=dom, overwrite=not skip_existing)
                    if not fid:
                        return failure('upload_failed', '공식 이미지 업로드에 실패했습니다.')
                    MediaReplacement.generated(operation, fid)
                    MetaPreviewUtil.register_remote_file(relative_path, fid, category=dom, is_image=True)
                    uploaded = True
                media['official']['official_cached_google_fileid'] = fid
                media['official']['remote_img_path'] = relative_path
            else:
                root = P.ModelSetting.get('jav_censored_image_server_local_path') or '/data/images'
                target = os.path.join(root, relative_path.replace('/', os.path.sep))
                exists = MetaPreviewUtil.has_local_file(target, use_cache=use_dir_cache)
                if not (skip_existing and exists):
                    prepared = prepare_image()
                    if prepared is not None:
                        return prepared
                    begin_replacement()
                    SiteAvBase.publish_image_file(staging, target)
                    if not os.path.isfile(target):
                        return failure('download_failed', '인물 이미지 저장 결과 파일을 확인할 수 없습니다.')
                    MediaReplacement.generated(operation)
                    MetaPreviewUtil.register_local_file(target)
                    uploaded = True
                media['official']['cached_local_img_path'] = relative_path
            person.media_src = media
            cls.sync_person_media_columns(person)
            person.thumb_url = cls.resolve_person_active_thumb(person)
            person.has_photo = bool(person.thumb_url)
            after = (person.media_src, person.official_cached_gdrive_fileid, person.official_cached_local_path,
                     person.official_remote_path, person.thumb_url, person.has_photo)
            changed = before != after
            result.update(is_updated=changed or uploaded, skipped=not (changed or uploaded),
                          changes=([('GDrive 이미지 저장' if mode == 'gdrive' else '로컬 이미지 저장')] if uploaded else []) +
                                  (['인물 이미지 DB 연동'] if changed else []),
                          relative_path=relative_path,
                          google_fileid=person.official_cached_gdrive_fileid if mode == 'gdrive' else '',
                          msg='동기화 완료' if changed or uploaded else '기존 이미지 유지')
            if changed or uploaded:
                with batch_stage(logger, 'Person DB commit', person.person_idx):
                    commit_storage_record(sess, person, snapshot)
                committed = True
            result['commit_state'] = 'committed' if committed else 'not_attempted'
            logger.debug('[MetaDB PersonSync] [%s/%s] %s: %s', dom, person.person_idx, mode, result['msg'])
            return result
        except Exception as exc:
            state = sess.info.get('storage_commit_state', 'not_attempted')
            if state == 'committed':
                committed = True
                result.update(commit_state='committed', cleanup_pending=True, warnings=['post_commit_error'])
                return result
            try:
                sess.rollback()
            except Exception:
                logger.exception('[MetaDB PersonSync] 세션 rollback 실패 (%s)', domain)
            logger.exception('[MetaDB PersonSync] 인물 저장소 동기화 실패 (%s/%s)', domain, person_idx or person_id)
            if state == 'unknown':
                failure('commit_outcome_unknown', 'DB 반영 결과를 확인할 수 없습니다. 자동 재시도하지 마세요.')
                result['commit_state'] = 'unknown'
                return result
            code = getattr(exc, 'error_code', 'storage_conflict' if isinstance(exc, FileExistsError) else 'storage_sync_failed')
            return failure(code, '인물 이미지 동기화 중 오류가 발생했습니다.')
        finally:
            state = sess.info.get('storage_commit_state', 'not_attempted')
            warnings = MetaUtil.finish_media_replacements([operation], True if committed else None if state == 'unknown' else False)
            if staging and os.path.exists(staging):
                try:
                    os.remove(staging)
                except OSError:
                    logger.warning('[MetaDB PersonSync] 임시 이미지 정리 실패')
                    warnings.append('temporary_cleanup_failed')
            try:
                sess.close()
            except Exception:
                logger.exception('[MetaDB PersonSync] 세션 정리 실패 (%s)', domain)
                warnings.append('session_cleanup_failed')
            if not operation or not os.path.isfile(operation['journal']):
                try:
                    MediaReplacement.release_subject(subject)
                except Exception:
                    logger.exception('[MetaDB PersonSync] 인물 잠금 정리 보류 (%s)', domain)
                    warnings.append('subject_cleanup_pending')
            if warnings:
                result['warnings'] = list(dict.fromkeys(result.get('warnings', []) + warnings))
                result['cleanup_pending'] = True

    @classmethod
    def sync_registered_actor_image(cls, actor, domain, skip_existing=None):
        """Keep registered-actor scraper/UI writes on the same protected cache path."""
        if not P.ModelSetting.get_bool('meta_db_use'):
            return False
        from sqlalchemy.orm import sessionmaker
        from .util_runtime import StorageLookupError, StorageConflict
        from support_site import SiteAvBase
        get = actor.get if isinstance(actor, dict) else lambda key, default=None: getattr(actor, key, default)
        idx = str(get('actor_idx') or get('person_idx') or '').strip()
        if not idx:
            return False
        registry, _, _ = cls.get_session_and_domain('PERSON')
        if registry is None:
            raise StorageLookupError('인물 저장소의 등록 여부를 확인할 수 없습니다.')
        requested = idx.casefold()
        with sessionmaker(bind=registry.get_bind())() as read:
            rows = read.query(MetaPerson).filter(func.upper(MetaPerson.domain) == domain,
                or_(func.lower(MetaPerson.person_idx) == requested,
                    func.lower(cast(MetaPerson.alt_indices, Text)).contains(requested, autoescape=True))).all()
            matches = [row for row in rows if requested in
                {str(value).strip().casefold() for value in [row.person_idx] + list(row.alt_indices or [])}]
            if not matches:
                return False
            if len(matches) != 1:
                raise StorageConflict('인물 코드가 여러 레코드에 연결되어 있습니다.')
            canonical = matches[0].person_idx
        source = str(get('site_img_url') or get('thumb') or get('thumb_url') or '')
        if (not source.startswith(('http://', 'https://')) or cls.is_local_server_url(source)
                or '/metadata/normal/' in source or 'drive.google.com/' in source or MetaUtil.is_user_media({'url': source})):
            source = None
        keep = not P.ModelSetting.get_bool('jav_censored_image_server_rewrite') if skip_existing is None else skip_existing
        result = cls.sync_person_image_to_storage(canonical, domain, skip_existing=keep,
                                                  use_dir_cache=False, source_url=source)
        if result.get('ret') != 'success':
            SiteAvBase.set_image_storage_state(actor, 'failed', result.get('error_code', 'storage_sync_failed'))
            return True
        if result.get('reason') == 'storage_mode_not_enabled':
            return False
        with sessionmaker(bind=registry.get_bind())() as read:
            person = read.query(MetaPerson).filter_by(domain=domain, person_idx=canonical).one()
            media = cls.person_media(person)
            thumb = cls.resolve_person_active_thumb(person)
        if isinstance(actor, dict):
            actor.update(media_src=media, thumb=thumb)
        else:
            actor.media_src = media
            actor.thumb = thumb
        SiteAvBase.set_image_storage_state(actor, 'complete')
        return True

    @classmethod
    def complete_item_media(cls, code, category, media_path, metadata=None):
        """Complete videos at a confirmed local path without scraping metadata again."""
        from sqlalchemy.orm import selectinload
        from .util_preview import MetaPreviewUtil, MediaReplacement
        from .util_runtime import stage_storage_record, commit_storage_record
        blocked_codes = ('commit_outcome_unknown', 'storage_conflict', 'storage_lookup_failed', 'storage_asset_incomplete')
        def blocked(report):
            return report.get('commit_state') == 'unknown' or report.get('error_code') in blocked_codes
        def settle(entries, report, commit):
            if not blocked(report):
                return MetaUtil.finish_media_replacements(entries, commit)
            warnings = []
            for entry in entries:
                try:
                    MediaReplacement.mark_pending(entry, commit_unknown=(
                        report.get('commit_state') == 'unknown' or report.get('error_code') == 'commit_outcome_unknown'))
                except Exception:
                    logger.exception('[MetaDB] 미디어 보완 복구 증거 보존 보류 (%s)', code)
                warnings.append('media_cleanup_pending')
            return warnings
        if not media_path or not os.path.isfile(media_path):
            return {'ret': 'warning', 'msg': '확정된 영상 파일이 존재하지 않습니다.'}
        if not P.ModelSetting.get_bool('meta_db_use') or not P.ModelSetting.get_bool('meta_db_save'):
            data = copy.deepcopy(metadata or {})
            if not data:
                return {'ret': 'warning', 'msg': '미디어를 보완할 메타데이터가 없습니다.'}
            report = MetaPreviewUtil.complete_media(data, category, media_path)
            warnings = settle((data.get('extra_info') or {}).pop('_media_replacements', []), report, True)
            result = {'ret': 'warning' if report['failures'] else 'success',
                      'data': copy.deepcopy(metadata) if blocked(report) else data, 'failures': report['failures']}
            if report.get('error_code'):
                result['error_code'] = report['error_code']
            if report.get('commit_state') == 'unknown':
                result['commit_state'] = 'unknown'
            if warnings:
                result.update(warnings=warnings, cleanup_pending=True)
            return result
        cls.ensure_db_ready()
        if not cls._ensure_meta_db_writable(category):
            return {'ret': 'warning', 'msg': '메타 DB가 쓰기 가능한 상태가 아닙니다.'}
        registry, _, std_cat = cls.get_session_and_domain(category)
        if registry is None:
            return {'ret': 'warning', 'msg': '메타 DB 세션 획득 실패'}
        session = sessionmaker(bind=registry.get_bind(), expire_on_commit=False)()
        entries = []
        committed = False
        data = None
        report = {}
        result = {'ret': 'warning', 'msg': '미디어 보완을 완료하지 못했습니다.'}
        session.info['storage_commit_state'] = 'not_attempted'
        try:
            item = session.query(MetaItem).options(
                selectinload(MetaItem.media_files),
                selectinload(MetaItem.person_maps),
                selectinload(MetaItem.tag_maps),
                selectinload(MetaItem.fingerprints),
            ).filter_by(code=code, category=std_cat).first()
            if item is None and metadata and P.ModelSetting.get_bool('meta_db_save'):
                session.rollback()
                from support_site import SiteUtil
                plot = metadata.get('plot') or ''
                allowed = not P.ModelSetting.get_bool('meta_db_save_only_translated') or not plot or SiteUtil.is_include_hangul(plot)
                if allowed and cls.save_metadata(std_cat, metadata):
                    item = session.query(MetaItem).options(
                        selectinload(MetaItem.media_files),
                        selectinload(MetaItem.person_maps),
                        selectinload(MetaItem.tag_maps),
                        selectinload(MetaItem.fingerprints),
                    ).filter_by(code=code, category=std_cat).first()
            if item is None:
                return {'ret': 'warning', 'msg': '저장 정책에 따라 메타 DB 등록이 생략되어 미디어 보완을 실행하지 않았습니다.'}
            data = cls.to_entity_dict(item)
            data['original'] = copy.deepcopy(item.original or {})
            snapshot = stage_storage_record(session, item, ('media_files',))
            report = MetaPreviewUtil.complete_media(data, std_cat, str(media_path))
            extra = data.get('extra_info') or {}
            entries = extra.pop('_media_replacements', [])
            if blocked(report):
                result = {'ret': 'warning', 'failures': report.get('failures') or {},
                          'error_code': report.get('error_code') or 'commit_outcome_unknown'}
                if report.get('commit_state') == 'unknown':
                    result['commit_state'] = 'unknown'
                session.rollback()
                return result
            updated_extra = copy.deepcopy(item.extra_info or {})
            for key in ('source_video_path', 'trailer_video', 'preview_clip', 'media_errors'):
                if key in extra:
                    updated_extra[key] = copy.deepcopy(extra[key])
                else:
                    updated_extra.pop(key, None)
            item.extra_info = updated_extra
            item.original = copy.deepcopy(data.get('original') or {})
            cls.sync_item_video_assets(item)
            item.updated_time = datetime.now()
            result = {'ret': 'warning' if report['failures'] else 'success', 'data': data, 'failures': report['failures']}
            if report.get('error_code'):
                result['error_code'] = report['error_code']
            commit_storage_record(session, item, snapshot, ('media_files',))
            committed = True
            return result
        except Exception as exc:
            state = session.info.get('storage_commit_state', 'not_attempted')
            if state == 'committed':
                committed = True
                result.setdefault('warnings', []).append('post_commit_error')
                return result
            try:
                session.rollback()
            except Exception:
                logger.exception('[MetaDB] 미디어 보완 세션 rollback 정리 보류 (%s)', code)
            logger.exception('[MetaDB] 최종 영상 경로 미디어 보완 실패 (%s): %s', code, exc)
            result = {'ret': 'warning', 'msg': '미디어 보완을 완료하지 못했습니다.',
                      'error_code': 'commit_outcome_unknown' if state == 'unknown' else getattr(exc, 'error_code', 'media_completion_failed'),
                      'commit_state': state}
            return result
        finally:
            if not entries and data:
                entries = (data.get('extra_info') or {}).pop('_media_replacements', [])
            state = session.info.get('storage_commit_state', 'not_attempted')
            warnings = settle(entries, report, True if committed else None if state == 'unknown' else False)
            try:
                session.close()
            except Exception:
                logger.exception('[MetaDB] 미디어 보완 세션 정리 보류 (%s)', code)
                warnings.append('session_cleanup_pending')
            if warnings:
                result.setdefault('warnings', []).extend(warnings)
                result['cleanup_pending'] = True

    @classmethod
    def update_item_source_path(cls, code, category, media_path):
        """Legacy caller compatibility: clear old source paths without storing a new one."""
        if not media_path:
            return {'ret': 'warning', 'msg': '영상 경로가 없습니다.'}
        if not P.ModelSetting.get_bool('meta_db_use') or not P.ModelSetting.get_bool('meta_db_save'):
            return {'ret': 'success'}
        cls.ensure_db_ready()
        if not cls._ensure_meta_db_writable(category):
            return {'ret': 'warning', 'msg': '메타 DB가 쓰기 가능한 상태가 아닙니다.'}
        registry, _, std_cat = cls.get_session_and_domain(category)
        if registry is None:
            return {'ret': 'warning', 'msg': '메타 DB 세션 획득 실패'}
        session = sessionmaker(bind=registry.get_bind(), expire_on_commit=False)()
        result = {'ret': 'success'}
        session.info['storage_commit_state'] = 'not_attempted'
        try:
            item = session.query(MetaItem).options(lazyload('*')).filter_by(code=code, category=std_cat).first()
            if item is None:
                return {'ret': 'success'}  # Respect metadata save policies.
            from .util_runtime import stage_storage_record, commit_storage_record
            snapshot = stage_storage_record(session, item)
            extra = copy.deepcopy(item.extra_info or {})
            if 'source_video_path' in extra:
                extra.pop('source_video_path', None)
                item.extra_info = extra
                item.updated_time = datetime.now()
                commit_storage_record(session, item, snapshot)
            return result
        except Exception as exc:
            state = session.info.get('storage_commit_state', 'not_attempted')
            if state == 'committed':
                result.setdefault('warnings', []).append('post_commit_error')
                return result
            try:
                session.rollback()
            except Exception:
                logger.exception('[MetaDB] 이전 원본 경로 rollback 정리 보류 (%s)', code)
            logger.exception('[MetaDB] 이전 원본 영상 경로 정보 정리 실패 (%s)', code)
            result = {'ret': 'warning', 'msg': '이전 원본 영상 경로 정보 정리에 실패했습니다.',
                      'error_code': 'commit_outcome_unknown' if state == 'unknown' else 'source_path_cleanup_failed',
                      'commit_state': state}
            return result
        finally:
            try:
                session.close()
            except Exception:
                logger.exception('[MetaDB] 이전 원본 경로 세션 정리 보류 (%s)', code)
                result.setdefault('warnings', []).append('session_cleanup_pending')
                result['cleanup_pending'] = True

    @classmethod
    def sync_item_video_assets(cls, item):
        """Mirror persisted video metadata into the common media asset table."""
        if item.category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
            return
        from .util_preview import MetaPreviewUtil
        extra = item.extra_info or {}
        for kind, key in (('trailer', 'trailer_video'), ('preview', 'preview_clip')):
            info = extra.get(key)
            if not isinstance(info, dict) or not (info.get('google_fileid') or info.get('local_path')):
                continue
            is_gdrive = info.get('storage_type') == 'gdrive'
            rows = [m for m in item.media_files if m.media_type == kind and
                    bool(m.gdrive_fileid) == is_gdrive]
            stream = MetaPreviewUtil.get_video_stream_url(info, item, video_type=kind, category=item.category)
            if not stream:
                continue
            row = rows[0] if rows else MetaMedia(media_type=kind, is_user=False, sort_order=0)
            if not rows:
                item.media_files.append(row)
            for duplicate in rows[1:]:
                item.media_files.remove(duplicate)
            row.url = stream
            row.gdrive_fileid = info.get('google_fileid') or None
            row.relative_path = info.get('relative_path') or None
            row.source = 'gdrive' if info.get('storage_type') == 'gdrive' else 'local'

    @classmethod
    def to_entity_dict(cls, item, for_list=False):
        include_original = P.ModelSetting.get_bool("meta_db_include_original")

        raw_extra = copy.deepcopy(item.extra_info or {})
        raw_extra.pop('source_video_path', None)
        raw_extra.pop('unavailable_trailer', None)
        raw_original = copy.deepcopy(getattr(item, 'original', {}) or {})
        if not isinstance(raw_original, dict):
            raw_original = {}

        raw_orig_thumb = raw_original.get('thumb')
        if not isinstance(raw_orig_thumb, dict):
            raw_orig_thumb = {}
            raw_original['thumb'] = raw_orig_thumb

        d = {
            'schema': {
                'db_schema_name': cls.META_DB_SCHEMA_NAME,
                'db_schema_version': cls.META_DB_SCHEMA_VERSION,
                'json_format_version': 1
            },
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

        target_folder, server_url_prefix = MetaUtil.get_server_folder_and_prefix(
            item.domain, item.category, stem, studio=item.studio, year=item.year
        )

        resolved_p_url = ""
        resolved_pl_url = ""
        resolved_fanarts = []
        p_is_local = False
        pl_is_local = False

        # 이미지 서버 모드 시 디스크 파일을 확인하여 서버 포스터, 랜드스케이프, 팬아트 주소 조립
        if current_image_mode == 'image_server' and target_folder and server_url_prefix and os.path.exists(target_folder):
            try:
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
            except Exception as e_scan:
                logger.debug(f"[MetaDB] 이미지 서버 디스크 파일 스캔 예외: {e_scan}")

        media_assets = []
        for media in getattr(item, 'media_files', []) or []:
            media_url = str(media.url or '').strip()
            if not cls._is_http_url(media_url) and media.gdrive_fileid:
                media_url = MetaUtil.format_gdrive_thumbnail_url(media.gdrive_fileid)
            media_assets.append({
                'id': media.id,
                'media_type': media.media_type,
                'url': media_url if cls._is_http_url(media_url) else '',
                'gdrive_fileid': media.gdrive_fileid or '',
                'relative_path': media.relative_path or '',
                'filename': str(media.relative_path or '').replace('\\', '/').rsplit('/', 1)[-1],
                'is_user': bool(media.is_user),
                'sort_order': media.sort_order or 0,
                'source': media.source or ('user' if media.is_user else 'site'),
                'checksum': media.checksum or '',
                'updated_time': media.updated_time.isoformat() if media.updated_time else None,
            })
        d['media'] = media_assets
        d['stored_images'] = {
            'poster_url': item.poster_url or '',
            'landscape_url': raw_extra.get('landscape_url') or '',
        }

        media_poster = next(
            (media for media in media_assets
             if media['media_type'] == 'poster' and media['is_user'] and MetaUtil.media_in_storage(media, current_image_mode)),
            None
        )
        if media_poster and media_poster['url']:
            resolved_p_url = media_poster['url']
        media_landscape = next(
            (media for media in media_assets
             if media['media_type'] == 'landscape' and media['is_user'] and MetaUtil.media_in_storage(media, current_image_mode)),
            None
        )
        if media_landscape and media_landscape['url']:
            resolved_pl_url = media_landscape['url']

        if not resolved_p_url:
            resolved_p_url = next(
                (
                    value for value in (item.poster_url, raw_orig_thumb.get('poster'))
                    if cls._is_http_url(value)
                ),
                ''
            )

        if not resolved_pl_url:
            resolved_pl_url = next(
                (
                    value for value in (raw_extra.get('landscape_url'), raw_orig_thumb.get('landscape'))
                    if cls._is_http_url(value)
                ),
                ''
            )

        if current_image_mode == 'ff_proxy':
            from urllib.parse import urlencode
            transforms = raw_extra.get('image_transforms') or {}
            transform = transforms.get('poster') if isinstance(transforms, dict) else None
            if (isinstance(transform, dict) and not media_poster and
                    cls._is_http_url(transform.get('url')) and
                    (transform.get('mode') == 'smart_crop' or
                     re.fullmatch(r'crop_[a-z0-9_.]+', str(transform.get('mode') or '')))):
                resolved_p_url = (F.SystemModelSetting.get('ddns') or '').rstrip('/') + '/metadata/normal/jav_image?' + urlencode({
                    'site': transform.get('site') or item.site, 'url': transform['url'], 'mode': transform['mode']})

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

        d['poster_url'] = resolved_p_url
        d['landscape_url'] = resolved_pl_url

        # 트레일러/프리뷰 비디오 스트림 URL 구성 (우선순위: 프리뷰 클립 > 저장된 트레일러 > 원격 트레일러)
        from .util_preview import MetaPreviewUtil

        has_video_extra = False

        # 프리뷰 클립 (최우선 적용)
        if isinstance(raw_extra.get('preview_clip'), dict):
            preview_stream_url = MetaPreviewUtil.get_video_stream_url(
                raw_extra['preview_clip'], item, video_type='preview', category=item.category
            )
            if preview_stream_url:
                d['extras'].append(MetaPreviewUtil.video_extra(preview_stream_url,
                    f"[Preview] {item.title or item.tagline or item.originaltitle}",
                    [raw_extra['preview_clip'].get('extra')]))
                has_video_extra = True

        # 저장된 공식 트레일러 영상 (프리뷰 클립이 없을 때 차선 적용)
        if not has_video_extra and isinstance(raw_extra.get('trailer_video'), dict):
            trailer_stream_url = MetaPreviewUtil.get_video_stream_url(
                raw_extra['trailer_video'], item, video_type='trailer', category=item.category
            )
            if trailer_stream_url:
                d['extras'].append(MetaPreviewUtil.video_extra(trailer_stream_url,
                    item.tagline or item.title or item.originaltitle,
                    [raw_extra['trailer_video']['extra']] if isinstance(raw_extra['trailer_video'].get('extra'), dict)
                    else raw_original.get('extras')))
                has_video_extra = True

        # 원격 원본 트레일러 스트림 URL (로컬/GDrive 저장 영상이 전혀 없을 때만 적용)
        if not has_video_extra:
            raw_video_url = ''
            orig_extras = raw_original.get('extras') or []
            if isinstance(orig_extras, list):
                for ex in orig_extras:
                    if isinstance(ex, dict) and str(ex.get('content_type', 'trailer')).lower() == 'trailer' and ex.get('content_url'):
                        raw_video_url = ex['content_url']
                        break

            if not raw_video_url and isinstance(raw_original.get('trailer'), str) and raw_original['trailer'].strip():
                raw_video_url = raw_original['trailer'].strip()

            if raw_video_url:
                use_trailer_proxy = P.ModelSetting.get_bool("meta_db_use_ff_proxy")
                if item.category == 'WESTERN':
                    use_trailer_proxy = use_trailer_proxy and P.ModelSetting.get_bool('western_use_trailer_proxy')

                ddns_host = (F.SystemModelSetting.get('ddns') or '').rstrip('/')
                is_uncen = str(item.category).upper() == 'JAV_UNCEN'
                video_endpoint = 'jav_video_un' if is_uncen else 'jav_video'
                from urllib.parse import quote_plus

                if use_trailer_proxy and raw_video_url.startswith('http'):
                    final_trailer_url = f"{ddns_host}/metadata/normal/{video_endpoint}?site={item.site}&url={quote_plus(raw_video_url)}"
                else:
                    final_trailer_url = raw_video_url

                d['extras'].append(MetaPreviewUtil.video_extra(final_trailer_url,
                    item.tagline or item.title, orig_extras))

        d['extras'].extend(copy.deepcopy(ex) for ex in raw_original.get('extras') or []
            if isinstance(ex, dict) and str(ex.get('content_type', 'trailer')).lower() != 'trailer')

        # 배우 다중 저장소 복원 및 역추적 자가 치유(Self-Healing)
        actors_bridge = (
            raw_extra.get('_actors') or
            raw_extra.get('actor_cache') or
            raw_extra.get('actors') or
            []
        )

        # 관계 테이블(MetaItemPersonMap) 매핑 확인
        if not actors_bridge and hasattr(item, 'person_maps') and item.person_maps:
            for pm in item.person_maps:
                if pm.person:
                    p = pm.person
                    actors_bridge.append({
                        'actor_idx': p.person_idx or '',
                        'name_org': p.name_org or '',
                        'name_ko': p.name_ko or '',
                        'name_en': p.name_en or '',
                        'gender': p.gender or '',
                        'role': pm.role_name or '출연'
                    })

        # 원문 백업 데이터(original.actor) 확인
        if not actors_bridge and isinstance(raw_original.get('actor'), list):
            for act in raw_original['actor']:
                if isinstance(act, dict):
                    actors_bridge.append(act)
                elif isinstance(act, str) and act.strip():
                    actors_bridge.append({'name_org': act.strip(), 'name_ko': '', 'role': '출연'})

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
                        'role': a_entry.get('role') or '출연',
                        'extra_info': a_entry.get('extra_info') or {}
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

                            p_gender = (p_rec.gender or '') if p_rec else (a_entry.get('gender') or '')
                            display_name = (p_rec.name_ko or p_rec.name_org or '') if p_rec else (a_name_ko or a_name_org or '')

                            actor_extra_out = copy.deepcopy(a_entry.get('extra_info') or {})
                            if p_rec and p_rec.extra_info:
                                for k_p, v_p in p_rec.extra_info.items():
                                    if v_p and not actor_extra_out.get(k_p):
                                        actor_extra_out[k_p] = v_p

                            if p_rec:
                                d['actor'].append({
                                    'name': display_name,
                                    'name_org': p_rec.name_org or '',
                                    'name_ko': p_rec.name_ko or '',
                                    'name_en': p_rec.name_en or '',
                                    'thumb': cls.resolve_person_active_thumb(p_rec),
                                    'actor_idx': p_rec.person_idx or '',
                                    'gender': p_gender,
                                    'role': role_name,
                                    'extra_info': actor_extra_out
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
                                    'role': role_name,
                                    'extra_info': actor_extra_out
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
                d['fingerprints'] = copy.deepcopy(fp_list)
            else:
                d['fingerprints'] = []

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

        cat_upper = str(category or result.get('category') or 'JAV_CEN').upper()
        is_uncensored = (cat_upper == 'JAV_UNCEN')
        is_western = (cat_upper == 'WESTERN')
        module_pfx = 'western' if is_western else ('jav_uncensored' if is_uncensored else 'jav_censored')

        current_img_mode = (
            opts.get('image_mode') or
            override_cfg.get('image_mode') or
            P.ModelSetting.get(f"{module_pfx}_image_mode") or
            P.ModelSetting.get("jav_censored_image_mode") or
            "ff_proxy"
        )

        use_meta_db_proxy = P.ModelSetting.get_bool("meta_db_use") and P.ModelSetting.get_bool("meta_db_use_ff_proxy")
        should_proxy_images = (current_img_mode == 'ff_proxy') or use_meta_db_proxy

        if should_proxy_images:
            ddns_host = F.SystemModelSetting.get('ddns') or ''
            proxy_path = 'jav_video_un' if is_uncensored else 'jav_video'
            image_path = 'jav_image_un' if is_uncensored else 'jav_image'
            image_server_setting = (
                'western_image_server_url' if is_western
                else 'jav_uncensored_image_server_url' if is_uncensored
                else 'jav_censored_image_server_url'
            )
            image_server_url = (
                P.ModelSetting.get('jav_censored_image_server_url') or ''
            ).rstrip('/')

            # 최종 출구 게이트웨이: 직결 자산(GDrive, 이미지서버, DDNS)을 제외한 순수 외부 원격 URL만 1회 래핑
            def proxy_image(url, is_local=False):
                if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                    return url
                if is_local:
                    return url
                if image_server_url and url.startswith(image_server_url):
                    return url
                if ddns_host and url.startswith(ddns_host):
                    return url
                if 'drive.google.com' in url or 'googleusercontent.com' in url:
                    return url
                return f"{ddns_host}/metadata/normal/{image_path}?{urlencode({'site': result.get('site', ''), 'url': url})}"

            result['thumb'] = [
                dict(thumb, value=proxy_image(thumb.get('value'), is_local=thumb.get('is_local', False)))
                if isinstance(thumb, dict) else thumb
                for thumb in result.get('thumb', [])
            ]

            result['fanart'] = [proxy_image(url) for url in result.get('fanart', []) if url]

            def proxy_video(url):
                if not isinstance(url, str) or not url.startswith(('http://', 'https://')):
                    return url
                if ddns_host and url.startswith(ddns_host):
                    return url
                if 'mode=preview_' in url or 'drive.google.com' in url or 'googleusercontent.com' in url:
                    return url
                if is_western and not P.ModelSetting.get_bool('western_use_trailer_proxy'):
                    return url
                return f"{ddns_host}/metadata/normal/{proxy_path}?{urlencode({'site': result.get('site', ''), 'url': url})}"

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
                            media_src = cls.person_media(p_rec)

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

            if raw_site_poster and result.get('poster_url') and cls.is_local_server_url(result['poster_url']):
                result['poster_url'] = raw_site_poster
                for th in result.get('thumb', []):
                    if isinstance(th, dict) and th.get('aspect') == 'poster':
                        th['value'] = raw_site_poster

            if raw_site_landscape and result.get('landscape_url') and cls.is_local_server_url(result['landscape_url']):
                result['landscape_url'] = raw_site_landscape
                for th in result.get('thumb', []):
                    if isinstance(th, dict) and th.get('aspect') == 'landscape':
                        th['value'] = raw_site_landscape

            orig_fanarts = result.get('original', {}).get('fanart', [])
            if orig_fanarts and result.get('fanart'):
                if any(cls.is_local_server_url(f) for f in result['fanart']):
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

            cat_upper = str(cat_k).strip().upper()
            norm_cat = 'WESTERN' if cat_upper in ['WEST', 'WESTERN'] else ('JAV_UNCEN' if cat_upper in ['JAV_UNCEN', 'UNCENSORED'] else ('JAV_CEN' if cat_upper in ['JAV_CEN', 'CENSORED'] else cat_upper))
            if norm_cat not in DOMAIN_MAP:
                continue
            dom_name = DOMAIN_MAP[norm_cat][0]
            item_engine = cls._engines.get('postgres') if cls._is_postgres else cls._engines.get(dom_name)
            if item_engine:
                work_query_session = sessionmaker(bind=item_engine, autocommit=False, autoflush=False, expire_on_commit=False)()
                try:
                    codes_list = list(codes_set)
                    light_rows = work_query_session.query(
                        MetaItem.code,
                        MetaItem.ui_code,
                        MetaItem.title,
                        MetaItem.originaltitle,
                        MetaItem.year,
                        MetaItem.premiered
                    ).filter(
                        MetaItem.category == norm_cat,
                        or_(
                            MetaItem.code.in_(codes_list),
                            MetaItem.ui_code.in_(codes_list),
                            MetaItem.originaltitle.in_(codes_list)
                        )
                    ).all()

                    for mi_code, mi_ui_code, mi_title, mi_orig_title, mi_year, mi_prem in light_rows:
                        y_val = str(mi_year) if (mi_year and mi_year != 1900) else (str(mi_prem)[:4] if mi_prem else '')
                        info_dict = {
                            'code': mi_code,
                            'ui_code': mi_ui_code or mi_orig_title or mi_code,
                            'title': mi_title or mi_orig_title or '',
                            'year': y_val
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
        """단일 인물의 소장 출연작을 MetaItem 테이블에서 실시간 쿼리하여 최신 제목과 연도로 구성하고 오염 데이터를 자동 치유"""
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

            is_healing_needed = False
            p_works = copy.deepcopy(p_rec.works if isinstance(p_rec.works, dict) else {})
            works_detailed = {}

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
                        MetaItem.originaltitle,
                        MetaItem.year,
                        MetaItem.premiered
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
                    for mi_code, mi_ui_code, mi_title, mi_orig_title, mi_year, mi_prem in rows:
                        y_val = str(mi_year) if (mi_year and mi_year != 1900) else (str(mi_prem)[:4] if mi_prem else '')
                        info_dict = {
                            'code': mi_code,
                            'ui_code': mi_ui_code or mi_orig_title or mi_code,
                            'title': mi_title or mi_orig_title or '',
                            'year': y_val
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
                            fallback_year = c_item.get('year') if isinstance(c_item, dict) else ''
                            entry = {
                                'code': raw_c,
                                'ui_code': fallback_ui or raw_c,
                                'title': fallback_title or (meta_info.get('title', '') if meta_info else ''),
                                'year': fallback_year or (meta_info.get('year', '') if meta_info else '')
                            }
                            works_detailed[std_cat].append(entry)
                            healed_work_entries.append(entry)

                    p_works[std_cat] = healed_work_entries
                finally:
                    work_query_session.close()

            # site_img_urls 내 로컬 이미지 서버 주소 오염 자가 치유
            p_media = cls.person_media(p_rec)
            official_media = p_media['official']
            # 물리 photo_urls와 media_src의 사진 목록을 상호 보존·복원
            source_photos = list(p_rec.photo_urls or official_media.get('site_img_urls') or [])
            cleaned_urls = [u for u in source_photos if u and isinstance(u, str) and u.startswith('http') and not cls.is_local_server_url(u)]

            if set(cleaned_urls) != set(official_media.get('site_img_urls') or []):
                official_media['site_img_urls'] = cleaned_urls
                p_rec.photo_urls = cleaned_urls
                is_healing_needed = True

            current_site_img = official_media.get('site_img_url') or p_rec.site_img_url
            if cls.is_local_server_url(current_site_img):
                official_media['site_img_url'] = cleaned_urls[0] if cleaned_urls else ''
                p_rec.site_img_url = official_media['site_img_url']
                is_healing_needed = True
            elif not p_rec.site_img_url and current_site_img and not cls.is_local_server_url(current_site_img):
                p_rec.site_img_url = current_site_img
                is_healing_needed = True

            # Count all retained works, including categories whose DB is unavailable.
            retained_works_count = sum(len(v) for v in p_works.values() if isinstance(v, list))
            if p_rec.works_count != retained_works_count:
                is_healing_needed = True

            # 레거시 데이터가 실시간 데이터로 교정되었을 경우 DB에 즉시 영구 반영 (Healing Commit)
            if is_healing_needed:
                try:
                    p_rec.works = p_works
                    p_rec.media_src = p_media
                    p_rec.works_count = retained_works_count
                    resolved_thumb = cls.resolve_person_active_thumb(p_rec)
                    p_rec.thumb_url = resolved_thumb
                    p_rec.has_photo = bool(resolved_thumb and str(resolved_thumb).strip())
                    sess.commit()
                    logger.info(f"[MetaDB Self-Healing] 인물 [{p_rec.name_ko or p_rec.name_org}]의 출연작 및 이미지 URL 자동 치유 완료")
                except Exception as e_p_heal:
                    sess.rollback()
                    logger.debug(f"[MetaDB Self-Healing] 인물 치유 커밋 예외: {e_p_heal}")

            extra_data = copy.deepcopy(p_rec.extra_info or {})
            _, source_data, manual_data = cls.normalize_person_extra(extra_data, person=p_rec)
            p_media = cls.person_media(p_rec)
            raw_local = str(p_media['official'].get('local_img_path') or p_media['custom'].get('local_img_path') or '').strip()
            if str(p_rec.domain or '').upper() in ('JAV', 'WESTERN'):
                extra_data.pop('local_img_url', None)
            elif raw_local:
                resolved_url = cls.format_actor_thumb_url(raw_local, domain=p_rec.domain)
                p_media['official']['local_img_url'] = resolved_url
                extra_data['local_img_url'] = resolved_url

            return {
                'schema': {
                    'db_schema_name': cls.META_DB_SCHEMA_NAME,
                    'db_schema_version': cls.META_DB_SCHEMA_VERSION,
                    'json_format_version': 1
                },
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
                'gender': p_rec.gender or 'female',
                'birth_date': p_rec.birth_date or '',
                'birth_year': p_rec.birth_year,
                'debut_date': p_rec.debut_date or '',
                'height': p_rec.height,
                'bust': p_rec.bust,
                'waist': p_rec.waist,
                'hip': p_rec.hip,
                'body_size': p_rec.body_size or '',
                'cup_size': p_rec.cup_size or '',
                'blood_type': p_rec.blood_type or '',
                'agency': p_rec.agency or '',
                'country': p_rec.country or '',
                'info_url': p_rec.info_url or '',
                'has_photo': bool(p_rec.has_photo),
                'local_path': p_rec.local_path or '',
                'gdrive_fileid': p_rec.gdrive_fileid or '',
                'custom_gdrive_fileid': p_rec.custom_gdrive_fileid or '',
                'custom_local_path': p_rec.custom_local_path or '',
                'site_img_url': p_rec.site_img_url or '',
                'photo_urls': p_rec.photo_urls or [],
                'merged_count': p_rec.merged_count or 1,
                'alt_indices': p_rec.alt_indices or [],
                'merged_details': p_rec.merged_details or [],
                'media_src': p_media,
                'works': p_works,
                'works_detailed': works_detailed,
                'works_count': retained_works_count,
                'extra_info': extra_data,
                'source_data': source_data,
                'manual_data': manual_data
            }
        except Exception as e:
            logger.error(f"[MetaDB] get_person_detailed_info 오류 ({person_identifier}): {e}")
            return None
        finally:
            sess.remove()


    @classmethod
    def refresh_person_from_site(cls, person_identifier, domain='WESTERN', protect_manual=True, skip_existing_image=False, use_dir_cache=False, site_activity=None):
        """원본 사이트(StashDB / TPDB)에 접속하여 서양 인물의 최신 프로필을 조회하고 물리 컬럼에 즉시 갱신."""
        cls.ensure_db_ready()
        sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess:
            return False, "인물 DB 세션 획득 실패", None

        try:
            target_str = str(person_identifier).strip()
            p_rec = None
            if target_str.isdigit():
                p_rec = sess.query(MetaPerson).filter_by(id=int(target_str)).first()
            if not p_rec and target_str:
                p_rec = sess.query(MetaPerson).filter_by(person_idx=target_str).first()
            if not p_rec and target_str:
                p_rec = sess.query(MetaPerson).filter(
                    or_(MetaPerson.name_org == target_str, MetaPerson.name_ko == target_str, MetaPerson.name_en == target_str)
                ).first()

            if not p_rec:
                return False, f"인물을 찾을 수 없습니다: {person_identifier}", None

            old_image_name = cls.person_image_name(p_rec)
            target_domain = (p_rec.domain or domain or 'WESTERN').upper()
            if target_domain != 'WESTERN':
                return False, "사이트 직접 갱신은 현재 서양(WESTERN) 도메인에서 지원됩니다.", None

            from support_site.site_av.site_stashdb import SiteStashdb
            from support_site.site_av.site_tpdb import SiteTpdb

            p_idx = (p_rec.person_idx or '').strip().upper()
            info_url = (p_rec.info_url or '').strip().lower()
            source_origin = str((p_rec.source_data or {}).get('source') or (p_rec.extra_info or {}).get('source') or '').strip().lower()

            # 인물의 식별 코드/출처 정보에 따라 대상 사이트 단독 확정
            site_used = None
            if p_idx.startswith('PS') or 'stashdb.org' in info_url or source_origin == 'stashdb':
                site_used = 'stashdb'
            elif p_idx.startswith(('PP', 'PT')) or 'theporndb' in info_url or 'tpdb' in info_url or source_origin == 'tpdb':
                site_used = 'tpdb'
            else:
                w_order = P.ModelSetting.get('western_order') or 'stashdb, tpdb'
                site_used = 'tpdb' if w_order.lower().startswith('tpdb') else 'stashdb'

            performer_data = None

            if site_activity is not None:
                site_activity['requested'] = True
            if site_used == 'stashdb':
                if p_idx.startswith('PS'):
                    performer_data = SiteStashdb.get_performer_by_id(p_idx[2:])
                if not performer_data:
                    actor_dummy = {'name_org': p_rec.name_en or p_rec.name_org or p_rec.name_ko}
                    if SiteStashdb.get_actor_info(actor_dummy):
                        performer_data = SiteStashdb.get_performer_by_id(actor_dummy.get('actor_idx', '').replace('PS', ''))
            else:
                if p_idx.startswith(('PP', 'PT')):
                    performer_data = SiteTpdb.get_performer_by_id(p_idx)
                if not performer_data:
                    actor_dummy = {'name_org': p_rec.name_en or p_rec.name_org or p_rec.name_ko}
                    if SiteTpdb.get_actor_info(actor_dummy):
                        performer_data = SiteTpdb.get_performer_by_id(actor_dummy.get('actor_idx'))

            if not performer_data:
                return False, f"[{site_used.upper()}] 서버 접속 실패 또는 인물 정보를 찾지 못했습니다.", None

            raw_extra, source_data, manual_data = cls.normalize_person_extra(p_rec.extra_info, person=p_rec)
            if not protect_manual:
                manual_data = {}

            # 사이트별 응답 구조 정규화
            if site_used == 'stashdb':
                p_id_raw = str(performer_data.get('id') or '')
                new_idx = f"PS{p_id_raw}" if (p_id_raw and not p_id_raw.startswith('PS')) else p_id_raw
                s_name = performer_data.get('name') or p_rec.name_en or p_rec.name_org
                s_gender = str(performer_data.get('gender') or '').lower()
                s_birth = str(performer_data.get('birth_date') or '').strip()
                s_debut = str(performer_data.get('career_start_year') or '').strip()
                s_height = cls._parse_integer(performer_data.get('height'))
                s_country = str(performer_data.get('country') or '').strip()
                s_info_url = f"https://stashdb.org/performers/{p_id_raw}" if p_id_raw else p_rec.info_url
                s_aliases = performer_data.get('aliases') or []

                band_sz = str(performer_data.get('band_size') or '').strip()
                cup_sz = str(performer_data.get('cup_size') or '').strip().upper()
                waist_sz = cls._parse_integer(performer_data.get('waist_size'))
                hip_sz = cls._parse_integer(performer_data.get('hip_size'))
                bust_val = cls._parse_integer(band_sz)

                body_str = ""
                if band_sz or waist_sz or hip_sz:
                    b_str = f"B{band_sz}{cup_sz}".strip() if band_sz else "B-"
                    w_str = f"W{waist_sz}" if waist_sz else "W-"
                    h_str = f"H{hip_sz}" if hip_sz else "H-"
                    body_str = f"{b_str}-{w_str}-{h_str}"

                images_list = performer_data.get('images') or []
                all_photos = [str(im.get('url')) for im in images_list if isinstance(im, dict) and im.get('url')]
                primary_photo = all_photos[0] if all_photos else ''
            else:
                p_id_raw = str(performer_data.get('id') or '')
                new_idx = f"PP{p_id_raw}" if (p_id_raw and not p_id_raw.startswith(('PP', 'PT'))) else p_id_raw
                s_name = performer_data.get('name') or p_rec.name_en or p_rec.name_org
                extra_sub = performer_data.get('extras') or performer_data.get('extra') or {}
                s_gender = str(extra_sub.get('gender') or '').lower()
                s_birth = str(extra_sub.get('birthday') or '').strip()
                s_debut = str(extra_sub.get('career_start_year') or '').strip()
                s_height = cls._parse_integer(extra_sub.get('height'))
                s_country = str(extra_sub.get('birthplace_code') or extra_sub.get('nationality') or '').strip()
                s_info_url = f"https://theporndb.net/performers/{p_id_raw}" if p_id_raw else p_rec.info_url
                s_aliases = performer_data.get('aliases') or []

                raw_measurements = str(extra_sub.get('measurements') or '').strip()
                b_val, w_val, h_val, c_val = cls._parse_body_size(raw_measurements)
                bust_val = b_val
                waist_sz = w_val
                hip_sz = h_val
                cup_sz = str(extra_sub.get('cupsize') or c_val or '').strip().upper()
                body_str = raw_measurements

                all_photos = []
                for pst in (performer_data.get('posters') or []):
                    if isinstance(pst, dict) and pst.get('url'):
                        all_photos.append(pst['url'])
                primary_photo = performer_data.get('image') or performer_data.get('face') or ''
                if primary_photo and primary_photo not in all_photos:
                    all_photos.insert(0, primary_photo)

            if not s_gender:
                s_gender = str(source_data.get('gender') or (p_rec.gender if 'gender' not in manual_data else '') or '').strip().lower()
            gender_norm = 'male' if s_gender in ['m', 'male', '남', '남성'] else 'female'
            m_by = re.search(r'\b(19\d{2}|20\d{2})\b', s_birth)
            s_by = int(m_by.group(1)) if m_by else None

            if not s_country:
                s_country = str(source_data.get('country') or (p_rec.country if 'country' not in manual_data else '') or '').strip()

            new_source = {
                'name_org': s_name,
                'name_ko': p_rec.name_ko,
                'name_en': s_name,
                'birth': s_birth,
                'height': s_height,
                'body_size': body_str,
                'bra_size': cup_sz,
                'debut': s_debut,
                'agency': p_rec.agency or '',
                'country': s_country,
                'info_url': s_info_url,
                'source': site_used,
                'gender': gender_norm,
                'updated_at': datetime.now().isoformat(timespec='seconds')
            }

            # 수동 수정 보호 항목을 제외한 물리 컬럼 반영
            if 'name_en' not in manual_data: p_rec.name_en = s_name
            if 'name_org' not in manual_data: p_rec.name_org = s_name
            if 'gender' not in manual_data: p_rec.gender = gender_norm
            if 'birth' not in manual_data and 'birth_date' not in manual_data:
                p_rec.birth_date = s_birth
                p_rec.birth_year = s_by
            if 'height' not in manual_data: p_rec.height = s_height
            if 'debut' not in manual_data and 'debut_date' not in manual_data: p_rec.debut_date = s_debut
            if 'body_size' not in manual_data:
                p_rec.body_size = body_str
                p_rec.bust = bust_val
                p_rec.waist = waist_sz
                p_rec.hip = hip_sz
            if 'cup_size' not in manual_data and 'bra_size' not in manual_data: p_rec.cup_size = cup_sz
            if 'country' not in manual_data: p_rec.country = s_country
            if 'info_url' not in manual_data: p_rec.info_url = s_info_url

            if new_idx and not p_rec.person_idx:
                p_rec.person_idx = new_idx

            alias_set = set(p_rec.aliases or [])
            alias_set.update(s_aliases)
            if s_name: alias_set.add(s_name)
            p_rec.aliases = list(alias_set)
            p_rec.other_names = ", ".join(p_rec.aliases)

            media_d = cls.person_media(p_rec)
            if primary_photo and not cls.is_local_server_url(primary_photo):
                media_d['official']['site_img_url'] = primary_photo
                p_rec.site_img_url = primary_photo
            if all_photos:
                clean_all_photos = [u for u in all_photos if u and not cls.is_local_server_url(u)]
                media_d['official']['site_img_urls'] = clean_all_photos
                p_rec.photo_urls = clean_all_photos
            p_rec.media_src = media_d

            # 현재 설정된 배우 이미지 소스 모드 확인
            pfx = 'western' if target_domain == 'WESTERN' else 'jav_censored'
            actor_img_mode = P.ModelSetting.get(f"{pfx}_actor_image_mode") or ('site' if target_domain == 'WESTERN' else 'gds')
            rewrite = P.ModelSetting.get_bool('jav_censored_image_server_rewrite')

            custom = media_d['custom']
            active_custom = custom.get('custom_user_google_fileid') if actor_img_mode == 'gdrive' else custom.get('local_img_path') if actor_img_mode == 'image_server' else False
            if primary_photo and not active_custom and actor_img_mode in ('gdrive', 'image_server'):
                from support_site import SiteAvBase
                actor = {'name_org': p_rec.name_org, 'name_en': p_rec.name_en,
                         'name_ko': p_rec.name_ko, 'actor_idx': p_rec.person_idx,
                         'thumb': primary_photo, 'media_src': media_d}
                SiteAvBase.save_western_actor_image(actor, skip_existing=skip_existing_image)
                if SiteAvBase.image_storage_error(actor):
                    logger.warning('[MetaDB Person] 이미지 처리 실패, 인물 정보는 저장: %s', SiteAvBase.image_storage_error(actor))
                p_rec.media_src = actor.get('media_src', media_d)
                cls.sync_person_media_columns(p_rec)
            p_rec.thumb_url = cls.resolve_person_active_thumb(p_rec)
            p_rec.has_photo = bool(p_rec.thumb_url)

            raw_extra.pop('source_data', None)
            raw_extra.pop('manual_data', None)
            p_rec.source_data = new_source
            p_rec.manual_data = manual_data
            p_rec.extra_info = raw_extra
            cls.rename_person_custom_asset(p_rec, old_image_name)
            p_rec.thumb_url = cls.resolve_person_active_thumb(p_rec)
            p_rec.has_photo = bool(p_rec.thumb_url)

            p_rec.updated_time = datetime.now()
            sess.commit()

            logger.info(f"[MetaDB Person Refresh] 서양 인물 최신 정보 갱신 완료: {p_rec.person_idx} ({s_name}, 출처: {site_used.upper()})")
            detailed_data = cls.get_person_detailed_info(p_rec.id, domain='WESTERN')
            return True, f"[{site_used.upper()}] 최신 정보가 성공적으로 반영되었습니다.", detailed_data

        except Exception as exc:
            sess.rollback()
            logger.error(f"[MetaDB Person Refresh] 오류: {exc}")
            logger.error(traceback.format_exc())
            return False, str(exc), None
        finally:
            sess.remove()


    @classmethod
    def verify_and_sync_person_works(cls, person_identifier, domain='JAV'):
        """현재 인물의 등록된 출연작들만 타겟으로 실제 소장 작품의 출연 여부를 검증하고 오매칭을 자동 정제"""
        from sqlalchemy.orm import selectinload
        cls.ensure_db_ready()
        sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess:
            return False, "인물 세션 획득 실패", {}

        try:
            target_str = str(person_identifier).strip()
            p_rec = None
            if target_str.isdigit():
                p_rec = sess.query(MetaPerson).filter_by(id=int(target_str)).first()
            if not p_rec and target_str:
                p_rec = sess.query(MetaPerson).filter(
                    MetaPerson.domain == domain,
                    or_(MetaPerson.person_idx == target_str, MetaPerson.name_org == target_str, MetaPerson.name_ko == target_str)
                ).first()
            if not p_rec and target_str:
                p_rec = sess.query(MetaPerson).filter(
                    or_(MetaPerson.person_idx == target_str, MetaPerson.name_org == target_str, MetaPerson.name_ko == target_str)
                ).first()

            if not p_rec:
                return False, "인물 정보를 찾을 수 없습니다.", {}

            p_works = copy.deepcopy(p_rec.works if isinstance(p_rec.works, dict) else {})

            # 인물 측의 모든 식별자 및 이름(원문, 한글, 영문, 별칭 전체) 집합 구성
            person_identifiers = set()
            if p_rec.person_idx:
                raw_idx = p_rec.person_idx.strip().upper()
                person_identifiers.add(raw_idx)
                num_only = re.sub(r'^[A-Za-z]+', '', raw_idx)
                if num_only:
                    person_identifiers.add(num_only)

            person_names = set()
            for n_val in [p_rec.name_org, p_rec.name_ko, p_rec.name_en]:
                if n_val and str(n_val).strip():
                    person_names.add(str(n_val).strip().lower())

            for al in (p_rec.aliases or []):
                if al and str(al).strip():
                    person_names.add(str(al).strip().lower())

            if p_rec.other_names:
                for onm in re.split(r'[,/]', p_rec.other_names):
                    clean_onm = str(onm).strip().lower()
                    if clean_onm:
                        person_names.add(clean_onm)

            verified_works = {}
            total_verified = 0
            total_removed = 0

            for cat_k, c_list in p_works.items():
                if not isinstance(c_list, list) or not c_list:
                    continue

                std_cat = 'WESTERN' if cat_k.upper() in ['WEST', 'WESTERN'] else ('JAV_UNCEN' if cat_k.upper() in ['JAV_UNCEN', 'UNCENSORED'] else 'JAV_CEN')
                if std_cat not in DOMAIN_MAP:
                    continue

                item_engine = cls._engines.get('postgres') if cls._is_postgres else cls._engines.get(DOMAIN_MAP[std_cat][0])
                if not item_engine:
                    continue

                work_session = sessionmaker(bind=item_engine, autocommit=False, autoflush=False, expire_on_commit=False)()
                try:
                    verified_works[std_cat] = []
                    for c_item in c_list:
                        raw_c = c_item.get('code') if isinstance(c_item, dict) else str(c_item)
                        if not raw_c:
                            continue

                        m_item = work_session.query(MetaItem).options(
                            lazyload('*'),
                            selectinload(MetaItem.person_maps).lazyload(MetaItemPersonMap.person),
                        ).filter(
                            MetaItem.category == std_cat,
                            or_(
                                MetaItem.code == raw_c,
                                MetaItem.ui_code == raw_c,
                                MetaItem.originaltitle == raw_c,
                                func.lower(MetaItem.code) == raw_c.lower(),
                                func.lower(MetaItem.ui_code) == raw_c.lower()
                            )
                        ).first()

                        if m_item:
                            is_actor_present = False

                            # 관계 테이블(MetaItemPersonMap) 매핑 확인
                            if hasattr(m_item, 'person_maps') and m_item.person_maps:
                                for pm in m_item.person_maps:
                                    if pm.person_id == p_rec.id:
                                        is_actor_present = True
                                        break

                            # 작품의 모든 배우 저장소 수집
                            movie_actors = []
                            for src in [
                                (m_item.extra_info or {}).get('_actors'),
                                (m_item.extra_info or {}).get('actor_cache'),
                                (m_item.extra_info or {}).get('actors'),
                                (m_item.original or {}).get('actor')
                            ]:
                                if isinstance(src, list):
                                    movie_actors.extend(src)

                            # 작품 출연진에 해당 인물이 포함되어 있는지 다각도 교차 대조
                            if not is_actor_present and movie_actors:
                                for a in movie_actors:
                                    if not a:
                                        continue

                                    if isinstance(a, str):
                                        if a.strip().lower() in person_names:
                                            is_actor_present = True
                                            break
                                    elif isinstance(a, dict):
                                        act_idx = str(a.get('actor_idx') or a.get('person_idx') or '').strip().upper()
                                        if act_idx and act_idx in person_identifiers:
                                            is_actor_present = True
                                            break
                                        act_num = re.sub(r'^[A-Za-z]+', '', act_idx)
                                        if act_num and act_num in person_identifiers:
                                            is_actor_present = True
                                            break

                                        act_names = [
                                            a.get('name_org'),
                                            a.get('name_ko'),
                                            a.get('name_en'),
                                            a.get('name'),
                                            a.get('originalname')
                                        ]
                                        for n_cand in act_names:
                                            if n_cand and str(n_cand).strip().lower() in person_names:
                                                is_actor_present = True
                                                break
                                        if is_actor_present:
                                            break

                            # 작품에 실제 출연한 것이 확인된 경우에만 최신 메타로 유지
                            if is_actor_present:
                                y_val = str(m_item.year) if (m_item.year and m_item.year != 1900) else (str(m_item.premiered)[:4] if m_item.premiered else '')
                                verified_entry = {
                                    'code': m_item.code,
                                    'ui_code': m_item.ui_code or m_item.originaltitle or m_item.code,
                                    'title': m_item.title or m_item.originaltitle or '',
                                    'year': y_val
                                }
                                verified_works[std_cat].append(verified_entry)
                                total_verified += 1
                            else:
                                total_removed += 1
                                logger.info(f"[MetaDB WorkVerify] 오매칭 작품 배제: [{m_item.code}]의 출연진에 [{p_rec.name_ko or p_rec.name_org}] 부재 확인 (인물 works에서 제거)")
                        else:
                            fallback_ui = c_item.get('ui_code') if isinstance(c_item, dict) else raw_c
                            fallback_title = c_item.get('title') if isinstance(c_item, dict) else ''
                            fallback_year = c_item.get('year') if isinstance(c_item, dict) else ''
                            verified_works[std_cat].append({
                                'code': raw_c,
                                'ui_code': fallback_ui or raw_c,
                                'title': fallback_title,
                                'year': fallback_year
                            })
                            total_verified += 1
                finally:
                    work_session.close()

            p_rec.works = verified_works
            p_rec.works_count = total_verified
            sess.commit()

            msg = f"출연작 검증 완료: 정상 유지 {total_verified}편, 오매칭 제거 {total_removed}편"
            logger.info(f"[MetaDB WorkVerify] 인물 [{p_rec.name_ko or p_rec.name_org}] {msg}")
            return True, msg, verified_works

        except Exception as e:
            sess.rollback()
            logger.error(f"[MetaDB] verify_and_sync_person_works 오류: {e}")
            return False, str(e), {}
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
                p_media = cls.person_media(p)
                raw_local = str(p.local_path or p_media['official'].get('local_img_path') or p_media['custom'].get('local_img_path') or '').strip()
                if str(p.domain or '').upper() in ('JAV', 'WESTERN'):
                    extra_data.pop('local_img_url', None)
                elif raw_local:
                    resolved_url = cls.format_actor_thumb_url(raw_local, domain=p.domain)
                    p_media['official']['local_img_url'] = resolved_url
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

                # 캐싱된 물리 thumb_url 컬럼을 우선 사용
                active_thumb = p.thumb_url or cls.resolve_person_active_thumb(p)

                results.append({
                    'id': p.id,
                    'domain': p.domain,
                    'name_org': p.name_org,
                    'name_ko': p.name_ko or '',
                    'name_en': p.name_en or '',
                    'other_names': p.other_names or '',
                    'aliases': p.aliases or [],
                    'gender': p.gender or 'female',
                    'birth_date': p.birth_date or '',
                    'birth_year': p.birth_year,
                    'debut_date': p.debut_date or '',
                    'height': p.height,
                    'bust': p.bust,
                    'waist': p.waist,
                    'hip': p.hip,
                    'body_size': p.body_size or '',
                    'cup_size': p.cup_size or '',
                    'blood_type': p.blood_type or '',
                    'agency': p.agency or '',
                    'country': p.country or '',
                    'info_url': p.info_url or '',
                    'thumb': active_thumb,
                    'has_photo': bool(p.has_photo),
                    'person_idx': p.person_idx or '',
                    'person_type': p.person_type or 'actor',
                    'local_path': p.local_path or '',
                    'gdrive_fileid': p.gdrive_fileid or '',
                    'custom_gdrive_fileid': p.custom_gdrive_fileid or '',
                    'photo_urls': p.photo_urls or [],
                    'merged_count': p.merged_count or 1,
                    'alt_indices': p.alt_indices or [],
                    'merged_details': p.merged_details or [],
                    'media_src': p_media,
                    'works': p_works,
                    'works_detailed': works_detailed,
                    'works_count': p.works_count if p.works_count is not None else (sum(len(v) for v in p_works.values()) if isinstance(p_works, dict) else 0),
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
            search_site = str(params.get('search_site', 'all')).strip().lower()

            base_query = sess.query(MetaPerson)
            if search_domain != 'ALL':
                base_query = base_query.filter(func.upper(MetaPerson.domain) == search_domain)

            if search_domain == 'WESTERN' and search_site not in ['all', '']:
                if search_site == 'stashdb':
                    base_query = base_query.filter(or_(
                        MetaPerson.person_idx.ilike('PS%'),
                        cast(MetaPerson.extra_info, Text).ilike('%stashdb%')
                    ))
                elif search_site == 'tpdb':
                    base_query = base_query.filter(or_(
                        MetaPerson.person_idx.ilike('PP%'),
                        MetaPerson.person_idx.ilike('PT%'),
                        cast(MetaPerson.extra_info, Text).ilike('%theporndb%'),
                        cast(MetaPerson.extra_info, Text).ilike('%tpdb%')
                    ))

            if search_status == 'no_photo':
                base_query = base_query.filter(or_(MetaPerson.has_photo == False, MetaPerson.has_photo == None))
            elif search_status == 'has_photo':
                base_query = base_query.filter(MetaPerson.has_photo == True)
            elif search_status == 'merged_only':
                base_query = base_query.filter(MetaPerson.merged_count > 1)
            elif search_status == 'has_works':
                base_query = base_query.filter(MetaPerson.works_count > 0)

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

            effective_person_name = func.coalesce(func.nullif(MetaPerson.name_ko, ''), MetaPerson.name_org)

            if search_order == 'match' or (search_word and search_order == 'desc'):
                query = query.order_by(
                    match_priority.asc(),
                    effective_person_name.asc(),
                    MetaPerson.id.desc()
                )
            elif search_order == 'works_desc':
                query = query.order_by(MetaPerson.works_count.desc(), MetaPerson.id.desc())
            elif search_order == 'asc':
                query = query.order_by(MetaPerson.id.asc())
            elif search_order == 'name_asc':
                query = query.order_by(effective_person_name.asc())
            elif search_order == 'name_desc':
                query = query.order_by(effective_person_name.desc())
            elif search_order == 'id_asc':
                query = query.order_by(MetaPerson.id.asc())
            elif search_order == 'id_desc':
                query = query.order_by(MetaPerson.id.desc())
            else:
                query = query.order_by(MetaPerson.id.desc())

            # 기존 DB의 누락 테이블·컬럼은 정규 마이그레이션에서만 보정한다.
            try:
                count = query.count()
            except Exception as e_p_tbl:
                logger.error(f"[MetaDB Person WebList] 스키마 조회 실패: {e_p_tbl}")
                sess.rollback()
                return {'success': False, 'paging': None, 'list': [], 'msg': '메타 DB 마이그레이션이 필요합니다.'}

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
                source_data = copy.deepcopy(p.source_data or {})
                manual_data = copy.deepcopy(p.manual_data or {})

                p_media = cls.person_media(p)
                raw_local = str(p.local_path or p_media['official'].get('local_img_path') or p_media['custom'].get('local_img_path') or '').strip()
                if str(p.domain or '').upper() in ('JAV', 'WESTERN'):
                    extra_data.pop('local_img_url', None)
                elif raw_local:
                    resolved_url = cls.format_actor_thumb_url(raw_local, domain=p.domain)
                    p_media['official']['local_img_url'] = resolved_url
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

                active_thumb = p.thumb_url or cls.resolve_person_active_thumb(p)

                item_list.append({
                    'schema': {
                        'db_schema_name': cls.META_DB_SCHEMA_NAME,
                        'db_schema_version': cls.META_DB_SCHEMA_VERSION,
                        'json_format_version': 1
                    },
                    'id': p.id,
                    'domain': p.domain,
                    'name_org': p.name_org,
                    'name_ko': p.name_ko or '',
                    'name_en': p.name_en or '',
                    'other_names': p.other_names or '',
                    'aliases': p.aliases or [],
                    'gender': p.gender or 'female',
                    'birth_date': p.birth_date or '',
                    'birth_year': p.birth_year,
                    'debut_date': p.debut_date or '',
                    'height': p.height,
                    'bust': p.bust,
                    'waist': p.waist,
                    'hip': p.hip,
                    'body_size': p.body_size or '',
                    'cup_size': p.cup_size or '',
                    'blood_type': p.blood_type or '',
                    'agency': p.agency or '',
                    'country': p.country or '',
                    'thumb': active_thumb,
                    'has_photo': bool(p.has_photo),
                    'person_idx': p.person_idx or '',
                    'person_type': p.person_type or 'actor',
                    'media_src': p_media,
                    'works': p_works,
                    'works_detailed': works_detailed,
                    'works_count': p.works_count if p.works_count is not None else (sum(len(v) for v in p_works.values()) if isinstance(p_works, dict) else 0),
                    'extra_info': extra_data,
                    'merged_count': p.merged_count or 1,
                    'alt_indices': p.alt_indices or [],
                    'merged_details': p.merged_details or [],
                    'source_data': source_data,
                    'manual_data': manual_data
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
            master_image_server_url = (P.ModelSetting.get(img_srv_key) or P.ModelSetting.get("jav_censored_image_server_url") or "").rstrip('/')

            return {
                'success': True,
                'paging': paging,
                'list': item_list,
                'meta_db_use_ff_proxy': P.ModelSetting.get_bool("meta_db_use_ff_proxy"),
                'meta_db_ddns': F.SystemModelSetting.get('ddns') or '',
                'image_server_url': master_image_server_url
            }

        except Exception as e:
            logger.error(traceback.format_exc())
            return {'success': False, 'paging': None, 'list': []}
        finally:
            sess.remove()

    @classmethod
    def person_save(cls, person_dict):
        allowed, guard_msg = cls._schema_write_guard('PERSON', '인물 수동 편집')
        if not allowed:
            return False, guard_msg
        cls.ensure_db_ready()
        sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess:
            return False, "세션 생성 실패"

        try:
            pid = person_dict.get('id') if isinstance(person_dict, dict) else None
            p_rec = sess.query(MetaPerson).filter_by(id=pid).first() if pid else None

            if not p_rec and person_dict.get('person_idx'):
                p_rec = sess.query(MetaPerson).filter_by(domain=person_dict.get('domain', 'JAV'), person_idx=str(person_dict.get('person_idx')).strip()).first()

            if not p_rec:
                target_name = person_dict.get('name_org') or person_dict.get('name_ko') or '미상'
                logger.warning(f"[MetaDB Person Save] DB 미등록 인물 수동 추가 시도 차단: '{target_name}'")
                return False, "크롤링 DB에 존재하지 않는 인물의 신규 수동 등록은 허용되지 않습니다."

            old_image_name = cls.person_image_name(p_rec)
            previous_primary_url = cls.resolve_person_active_thumb(p_rec)
            p_rec.domain = person_dict.get('domain', 'JAV')
            existing_extra, existing_source, existing_manual = cls.normalize_person_extra(p_rec.extra_info, person=p_rec)
            source_data = cls.build_person_source_data(p_rec)

            submitted_names = {
                'name_org': str(person_dict.get('name_org') or '').strip(),
                'name_ko': str(person_dict.get('name_ko') or '').strip(),
                'name_en': str(person_dict.get('name_en') or '').strip(),
            }
            for field, value in submitted_names.items():
                source_value = str(source_data.get(field) or '').strip()
                if value != source_value:
                    existing_manual[field] = value
                elif field in existing_manual:
                    # 사용자가 공식값으로 되돌려 저장한 경우 수동 보호를 해제한다.
                    existing_manual.pop(field, None)

            p_rec.name_org = submitted_names['name_org']
            p_rec.name_ko = submitted_names['name_ko']
            p_rec.name_en = submitted_names['name_en']
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
            if p_rec.other_names != str(source_data.get('other_names') or ''):
                existing_manual['other_names'] = p_rec.other_names
            else:
                existing_manual.pop('other_names', None)

            # media_src 분리 저장
            p_media = cls.normalize_person_media(person_dict['media_src'], domain=p_rec.domain) if person_dict.get('media_src') else cls.person_media(p_rec)
            raw_local = str(person_dict.get('local_img_path') or p_media['official'].get('local_img_path') or '').strip()
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

            if (P.ModelSetting.get(f'{module_pfx}_actor_image_mode') or 'site') in ('image_server', 'gdrive') and primary_target_url.startswith('http') and primary_target_url != previous_primary_url:
                from support_site import SiteAvBase
                if p_rec.domain == 'WESTERN':
                    act_dummy = {
                        'thumb': primary_target_url,
                        'name_org': p_rec.name_org,
                        'name_en': p_rec.name_en,
                        'actor_idx': p_rec.person_idx,
                        'media_src': p_media
                    }
                    SiteAvBase.save_western_actor_image(act_dummy)
                    if SiteAvBase.image_storage_error(act_dummy):
                        logger.warning('[MetaDB Person] 이미지 처리 실패, 인물 정보는 저장: %s', SiteAvBase.image_storage_error(act_dummy))
                        SiteAvBase.sanitize_incomplete_images(act_dummy)
                        primary_target_url = p_rec.thumb_url or ''
                    if act_dummy.get('media_src'):
                        p_media = cls.normalize_person_media(act_dummy['media_src'], domain=p_rec.domain)
                        clean_local = p_media['official']['local_img_path']
                        primary_target_url = act_dummy.get('thumb') or primary_target_url
                else:
                    act_dummy = {
                        'thumb': primary_target_url,
                        'name_org': p_rec.name_org,
                        'name_ko': p_rec.name_ko,
                        'actor_idx': p_rec.person_idx,
                        'media_src': p_media
                    }
                    SiteAvBase.save_actor_image(act_dummy)
                    if SiteAvBase.image_storage_error(act_dummy):
                        logger.warning('[MetaDB Person] 이미지 처리 실패, 인물 정보는 저장: %s', SiteAvBase.image_storage_error(act_dummy))
                        SiteAvBase.sanitize_incomplete_images(act_dummy)
                        primary_target_url = p_rec.thumb_url or ''
                    if act_dummy.get('media_src'):
                        p_media = cls.normalize_person_media(act_dummy['media_src'], domain=p_rec.domain)
                        clean_local = p_media['official']['local_img_path']
                        primary_target_url = act_dummy.get('thumb') or primary_target_url

            p_media['official'].update({
                'local_img_path': clean_local,
                'google_fileid': str(person_dict.get('google_fileid') or p_media['official'].get('google_fileid') or '').strip(),
                'site_img_url': str(person_dict.get('site_img_url') or p_media['official'].get('site_img_url') or '').strip(),
                'site_img_urls': site_urls_list
            })
            custom_google_id = str(
                person_dict.get('custom_user_google_fileid')
                or p_media['custom'].get('custom_user_google_fileid')
                or ''
            ).strip()
            if custom_google_id:
                p_media['custom']['custom_user_google_fileid'] = custom_google_id
            p_rec.media_src = p_media

            # extra_info 순수 프로필 분리 저장 및 기존 병합/예외 목록 보존
            h_raw = person_dict.get('height')
            m_h = re.search(r'\d+', str(h_raw)) if h_raw is not None else None
            h_val = int(m_h.group()) if (m_h and int(m_h.group()) > 0) else None

            if 'gender' in person_dict: p_rec.gender = str(person_dict['gender'] or 'female')
            p_rec.height = h_val
            p_rec.agency = str(person_dict.get('agency') or '').strip()
            p_rec.birth_date = str(person_dict.get('birth') or '').strip()
            m_by = re.search(r'\b(19\d{2}|20\d{2})\b', p_rec.birth_date)
            p_rec.birth_year = int(m_by.group(1)) if m_by else None

            p_rec.debut_date = str(person_dict.get('debut') or '').strip()
            p_rec.blood_type = str(person_dict.get('blood') or '').strip().upper().replace('형', '')
            p_rec.body_size = str(person_dict.get('body_size') or '').strip()

            raw_bra = str(person_dict.get('bra_size') or '').strip().upper()
            clean_cup = re.sub(r'[^A-Z]', '', raw_bra)
            p_rec.cup_size = clean_cup or None

            p_rec.info_url = str(person_dict.get('info_url') or '').strip()
            p_rec.local_path = clean_local
            p_rec.gdrive_fileid = p_media['official'].get('google_fileid') or ''
            p_rec.custom_gdrive_fileid = custom_google_id or p_rec.custom_gdrive_fileid
            p_rec.site_img_url = p_media['official'].get('site_img_url') or ''
            p_rec.photo_urls = site_urls_list
            cls.sync_person_media_columns(p_rec)

            # 활성 썸네일 캐시 및 사진 보유 여부 갱신
            final_active_thumb = primary_target_url or cls.resolve_person_active_thumb(p_rec)
            p_rec.thumb_url = final_active_thumb
            p_rec.has_photo = bool(final_active_thumb and str(final_active_thumb).strip())

            submitted_profile = {
                'gender': p_rec.gender,
                'birth': p_rec.birth_date,
                'height': p_rec.height,
                'body_size': p_rec.body_size,
                'bra_size': raw_bra,
                'debut': p_rec.debut_date,
                'info_url': p_rec.info_url,
                'agency': p_rec.agency,
                'blood': p_rec.blood_type,
                'hobby': str(person_dict.get('hobby') or '').strip(),
                'specialty': str(person_dict.get('specialty', existing_extra.get('specialty', source_data.get('specialty', ''))) or '').strip(),
            }
            for field, value in submitted_profile.items():
                source_value = source_data.get(field)
                if str(value or '') != str(source_value or ''):
                    existing_manual[field] = value
                elif field in existing_manual:
                    existing_manual.pop(field, None)
            existing_extra.update({
                'source_origin': str(existing_extra.get('source_origin') or 'web').strip()
            })
            p_rec.source_data = source_data
            p_rec.manual_data = existing_manual
            p_rec.extra_info = existing_extra
            cls.rebuild_effective_person_fields(p_rec)
            cls.rename_person_custom_asset(p_rec, old_image_name)
            p_rec.thumb_url = cls.resolve_person_active_thumb(p_rec) or p_rec.thumb_url
            p_rec.has_photo = bool(p_rec.thumb_url)

            sess.commit()
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
    def person_reset_user_data(cls, person_id, reset_type):
        allowed, guard_msg = cls._schema_write_guard('PERSON', '인물 사용자 데이터 초기화')
        if not allowed:
            return False, guard_msg
        cls.ensure_db_ready()
        sess, _, _ = cls.get_session_and_domain('PERSON')
        if not sess:
            return False, "인물 DB 세션 생성 실패"
        try:
            person = sess.query(MetaPerson).filter_by(id=int(person_id)).first()
            if not person:
                return False, "초기화할 인물을 찾을 수 없습니다."
            reset_type = str(reset_type or '').strip().lower()
            if reset_type not in ('text', 'image', 'all'):
                return False, "지원하지 않는 초기화 유형입니다."

            media = cls.person_media(person)
            has_text_data = bool(person.manual_data)
            has_image_data = bool(
                media['custom'].get('local_img_path') or
                media['custom'].get('custom_user_google_fileid')
            )
            if not ((reset_type in ('text', 'all') and has_text_data) or
                    (reset_type in ('image', 'all') and has_image_data)):
                return True, "초기화할 사용자 데이터가 없어 변경하지 않았습니다."

            if reset_type in ('image', 'all'):
                from .util_metadata import MetaUtil
                deleted, delete_msg = MetaUtil.delete_person_custom_image(person)
                if not deleted:
                    sess.rollback()
                    return False, delete_msg
                media = cls.person_media(person)
                media['custom'] = {'custom_user_google_fileid': '', 'local_img_path': ''}
                person.media_src = media

            if reset_type in ('text', 'all'):
                extra, source, _ = cls.normalize_person_extra(person.extra_info, person=person)
                person.manual_data = {}
                person.source_data = source
                person.extra_info = extra
                cls.rebuild_effective_person_fields(person)

            person.thumb_url = cls.resolve_person_active_thumb(person)
            person.has_photo = bool(person.thumb_url)
            sess.commit()
            logger.info(f"[MetaDB PersonReset] 사용자 데이터 초기화 완료: {person.person_idx} ({reset_type})")
            return True, "사용자 데이터 초기화가 완료되었습니다."
        except (ValueError, TypeError) as exc:
            sess.rollback()
            return False, f"잘못된 인물 ID입니다: {exc}"
        except Exception as exc:
            sess.rollback()
            logger.error(f"[MetaDB PersonReset] 초기화 실패 ({person_id}, {reset_type}): {exc}", exc_info=True)
            return False, str(exc)
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

            from sqlalchemy.orm import selectinload
            item = sess.query(MetaItem).options(
                lazyload('*'), selectinload(MetaItem.fingerprints),
            ).filter_by(code=code, category=std_cat).first()
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

                from sqlalchemy.orm import joinedload

                query = sess.query(MetaItem).options(
                    lazyload('*'),
                    joinedload(MetaItem.person_maps).lazyload(MetaItemPersonMap.person),
                ).filter(MetaItem.category == std_cat)
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
    def _annotate_registered_list_actors(cls, rows):
        """Resolve this page's actors in batches against the person master DB."""
        actors = []
        for row in rows:
            if row.get('category') not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
                continue
            domain = cls._person_domain_from_item_category(row['category'])
            for actor in (row.get('json_data') or {}).get('actor') or []:
                if isinstance(actor, dict):
                    actor.update(is_registered=False, db_person_id=None)
                    actors.append((domain, actor))
        if not actors:
            return
        registry, _, _ = cls.get_session_and_domain('PERSON')
        if registry is None:
            return
        # Separate read session: PostgreSQL may share the list's scoped session.
        session = sessionmaker(bind=registry.get_bind())()
        def clean(value):
            return str(value or '').strip()
        identifiers, names = {}, {}
        try:
            for domain in {entry[0] for entry in actors}:
                entries = [actor for actor_domain, actor in actors if actor_domain == domain]
                tokens = {clean(actor.get(key)) for actor in entries
                          for key in ('actor_idx', 'person_idx', 'name_org', 'name_ko', 'name_en', 'name')}
                tokens.discard('')
                identity_tokens = {clean(actor.get('actor_idx') or actor.get('person_idx')) for actor in entries}
                token_list = sorted(tokens)
                for start in range(0, len(token_list), 100):
                    chunk = token_list[start:start + 100]
                    # JSON string boundaries make alternate-ID matching exact on SQLite and PostgreSQL.
                    alternate_conditions = []
                    for token in chunk:
                        if token not in identity_tokens:
                            continue
                        quoted = json.dumps(token, ensure_ascii=False).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
                        alternate_conditions.append(cast(MetaPerson.alt_indices, Text).like('%' + quoted + '%', escape='\\'))
                    people = session.query(MetaPerson.id, MetaPerson.person_idx, MetaPerson.name_org,
                        MetaPerson.name_ko, MetaPerson.name_en, MetaPerson.alt_indices).filter(
                        MetaPerson.domain == domain, or_(MetaPerson.person_idx.in_(chunk),
                        MetaPerson.name_org.in_(chunk), MetaPerson.name_ko.in_(chunk),
                        MetaPerson.name_en.in_(chunk), *alternate_conditions)).all()
                    for person in people:
                        for value in [person.person_idx] + list(person.alt_indices or []):
                            if clean(value):
                                identifiers.setdefault((domain, clean(value)), set()).add(person.id)
                        for value in (person.name_org, person.name_ko, person.name_en):
                            if clean(value):
                                names.setdefault((domain, clean(value)), set()).add(person.id)
            for domain, actor in actors:
                identity = clean(actor.get('actor_idx') or actor.get('person_idx'))
                matches = identifiers.get((domain, identity), set())
                if not matches:
                    matches = set().union(*(names.get((domain, clean(actor.get(key))), set())
                        for key in ('name_org', 'name_ko', 'name_en', 'name')))
                # Ambiguous names/identifiers must not open an arbitrary person.
                if len(matches) == 1:
                    actor.update(is_registered=True, db_person_id=next(iter(matches)))
        except Exception as exc:
            logger.debug(f'[MetaDB WebList] 인물 등록 여부 확인 실패: {exc}')
        finally:
            session.close()

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

        target_cat_upper = str(target_cat).strip().upper()
        is_all_category = (target_cat_upper == 'AV_ALL')

        sess = None
        list_sessions = []
        domain = None
        std_cat = target_cat_upper

        if not is_all_category:
            sess, domain, std_cat = cls.get_session_and_domain(target_cat_upper)
            if not sess:
                logger.error(f"[MetaDB WebList] 세션 획득 실패: target_cat='{target_cat}'")
                return {'success': False, 'paging': None, 'list': []}
        elif cls._is_postgres:
            sess = cls._sessions.get('postgres')
            domain = 'postgres'
            std_cat = 'AV_ALL'
            if sess is None:
                logger.error("[MetaDB WebList] PostgreSQL 세션 획득 실패: AV_ALL")
                return {'success': False, 'paging': None, 'list': [], 'msg': '메타 DB 연결을 확인하세요.'}

        try:
            try: page_size = int(params.get('page_size', 10))
            except: page_size = 10
            if page_size < 1: page_size = 10

            search_word = str(params.get('search_word', '')).strip()
            search_site = str(params.get('search_site', 'all')).strip()
            search_order = str(params.get('search_order', 'desc')).strip()
            search_status = str(params.get('search_status', 'all')).strip()

            av_categories = ['JAV_CEN', 'JAV_UNCEN', 'WESTERN']

            # 공통 필터 조건 생성 헬퍼 함수
            def build_common_filters(cat_condition):
                conds = [cat_condition]
                if search_site and search_site.lower() not in ['all', '']:
                    conds.append(func.lower(MetaItem.site) == search_site.lower())

                if search_status == 'no_poster':
                    conds.append(or_(
                        MetaItem.poster_url == '',
                        MetaItem.poster_url == None,
                        MetaItem.poster_url.ilike('%_pl.jpg'),
                        MetaItem.poster_url.ilike('%_pl.png'),
                        MetaItem.poster_url.ilike('%_pl.jpeg'),
                        MetaItem.poster_url.ilike('%_pl.webp')
                    ))
                elif search_status == 'no_plot':
                    conds.append(or_(MetaItem.plot == '', MetaItem.plot == None))
                elif search_status == 'no_trailer':
                    conds.append(or_(
                        MetaItem.extra_info == None,
                        ~cast(MetaItem.extra_info, Text).ilike('%trailer_video%')
                    ))
                elif search_status == 'no_actor':
                    conds.append(and_(
                        ~MetaItem.person_maps.any(),
                        or_(
                            MetaItem.extra_info == None,
                            cast(MetaItem.extra_info, Text) == '{}',
                            and_(
                                ~cast(MetaItem.extra_info, Text).ilike('%name_org%'),
                                ~cast(MetaItem.extra_info, Text).ilike('%name_ko%')
                            )
                        )
                    ))
                elif search_status == 'complete':
                    conds.append(and_(
                        MetaItem.poster_url != '',
                        MetaItem.poster_url != None,
                        MetaItem.plot != '',
                        MetaItem.plot != None
                    ))

                if search_word:
                    search_like = f"%{search_word.replace('-', '%')}%"
                    conds.append(or_(
                        MetaItem.originaltitle.ilike(search_like),
                        MetaItem.ui_code.ilike(search_like),
                        MetaItem.code.ilike(search_like),
                        MetaItem.title.ilike(f'%{search_word}%'),
                        MetaItem.studio.ilike(f'%{search_word}%'),
                        MetaItem.director.ilike(f'%{search_word}%')
                    ))
                return conds

            # SQLite 환경에서 ALL(통합) 조회 시 3개 분산 DB 병합 처리
            if is_all_category and not cls._is_postgres:
                total_count = 0
                all_candidate_items = []
                max_fetch_count = page * page_size

                for c_cat in av_categories:
                    c_sess, _, c_std = cls.get_session_and_domain(c_cat)
                    if not c_sess:
                        continue
                    # Keep ORM relationships readable until page serialization completes.
                    list_sessions.append(c_sess)
                    c_conds = build_common_filters(MetaItem.category == c_std)
                    c_cnt = c_sess.query(func.count(MetaItem.id)).filter(and_(*c_conds)).scalar() or 0
                    total_count += c_cnt

                    if c_cnt > 0:
                        c_query = c_sess.query(MetaItem).filter(and_(*c_conds)).options(
                            lazyload(MetaItem.media_files),
                            lazyload(MetaItem.person_maps),
                            lazyload(MetaItem.tag_maps),
                            lazyload(MetaItem.fingerprints)
                        )
                        if search_order == 'asc':
                            c_query = c_query.order_by(MetaItem.created_time.asc())
                        elif search_order == 'title_asc':
                            c_query = c_query.order_by(MetaItem.title.asc())
                        elif search_order == 'title_desc':
                            c_query = c_query.order_by(MetaItem.title.desc())
                        else:
                            c_query = c_query.order_by(MetaItem.created_time.desc())

                        all_candidate_items.extend(c_query.limit(max_fetch_count).all())

                # 메모리 통합 정렬 및 페이지 슬라이싱
                if search_order == 'asc':
                    all_candidate_items.sort(key=lambda x: x.created_time or datetime.min)
                elif search_order == 'title_asc':
                    all_candidate_items.sort(key=lambda x: (x.title or '').lower())
                elif search_order == 'title_desc':
                    all_candidate_items.sort(key=lambda x: (x.title or '').lower(), reverse=True)
                else:
                    all_candidate_items.sort(key=lambda x: x.created_time or datetime.min, reverse=True)

                count = total_count
                items = all_candidate_items[(page - 1) * page_size : page * page_size]

            else:
                # PostgreSQL 환경 또는 단일 카테고리 일반 조회
                if is_all_category:
                    filter_conditions = build_common_filters(MetaItem.category.in_(av_categories))
                else:
                    filter_conditions = build_common_filters(MetaItem.category == std_cat)

                try:
                    count = sess.query(func.count(MetaItem.id)).filter(and_(*filter_conditions)).scalar() or 0
                except Exception as e_tbl_chk:
                    sess.rollback()
                    logger.error(f"[MetaDB WebList] 스키마 조회 실패: {e_tbl_chk}")
                    return {'success': False, 'paging': None, 'list': [], 'msg': '메타 DB 마이그레이션이 필요합니다.'}

                if count == 0:
                    return {'success': True, 'paging': None, 'list': []}

                query = sess.query(MetaItem).filter(and_(*filter_conditions)).options(
                    lazyload(MetaItem.media_files),
                    lazyload(MetaItem.person_maps),
                    lazyload(MetaItem.tag_maps),
                    lazyload(MetaItem.fingerprints)
                )

                if search_order == 'asc':
                    query = query.order_by(MetaItem.created_time.asc())
                elif search_order == 'title_asc':
                    query = query.order_by(MetaItem.title.asc())
                elif search_order == 'title_desc':
                    query = query.order_by(MetaItem.title.desc())
                else:
                    query = query.order_by(MetaItem.created_time.desc())

                items = query.offset((page - 1) * page_size).limit(page_size).all()

            if count == 0:
                return {'success': True, 'paging': None, 'list': []}

            total_page = math.ceil(count / page_size) if count > 0 else 1

            if page > total_page and total_page > 0:
                page = total_page

            start_page = ((page - 1) // 10) * 10 + 1
            end_page = min(start_page + 9, total_page)

            url_mapping_str = P.ModelSetting.get("meta_db_image_url_mapping") or ""
            mappings = []
            if url_mapping_str:
                for line in url_mapping_str.split('\n'):
                    if '|' in line:
                        p = line.split('|', 1)
                        if p[0].strip() and p[1].strip(): mappings.append((p[0].strip(), p[1].strip()))

            item_list = []
            for item in items:
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

            cls._annotate_registered_list_actors(item_list)

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
                'meta_db_ddns': F.SystemModelSetting.get('ddns') or '',
                'image_server_url': master_image_server_url.rstrip('/')
            }

        except Exception as e:
            logger.error(f"[MetaDB WebList Item] 에러 ({target_cat}): {e}")
            logger.error(traceback.format_exc())
            return {'success': False, 'paging': None, 'list': []}
        finally:
            for list_session in list_sessions:
                list_session.remove()
            if sess is not None:
                sess.remove()


    @classmethod
    def get_delete_plan(cls, targets):
        """Build a reviewable inventory; input selects DB records, never arbitrary files."""
        import hashlib
        if isinstance(targets, str):
            targets = json.loads(targets)
        if not isinstance(targets, list) or not targets or len(targets) > 1000:
            raise ValueError('삭제할 항목을 1~1000개 선택하세요.')
        cls.ensure_db_ready()
        records, files, seen = [], {}, set()
        for target in targets:
            person = target.get('kind') == 'person'
            category = 'PERSON' if person else str(target.get('category') or '').upper()
            if not person and category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
                raise ValueError('AV 카테고리를 지정해야 합니다.')
            identifier = str(int(target['id'])) if person else str(target.get('code') or '').strip()
            key = (category, identifier)
            if not identifier or key in seen:
                continue
            seen.add(key)
            sess, _, _ = cls.get_session_and_domain(category)
            if sess is None:
                raise RuntimeError('DB 세션 생성 실패: ' + category)
            try:
                model = MetaPerson if person else MetaItem
                query = sess.query(model).options(lazyload('*'))
                record = query.filter_by(id=int(identifier)).first() if person else query.filter_by(code=identifier, category=category).first()
                if record is None:
                    raise ValueError('삭제할 DB 항목이 없습니다: ' + identifier)
                if person and str(record.domain).upper() not in ('JAV', 'WESTERN'):
                    raise ValueError('AV 인물만 삭제할 수 있습니다.')
                descriptor = dict(kind='person' if person else 'item', category=category, id=record.id,
                    code=identifier, title=(record.name_ko or record.name_org) if person else (record.originaltitle or record.title),
                    updated=str(record.updated_time or ''))
                records.append(descriptor)
                for asset in MetaUtil.collect_delete_assets(record, person=person):
                    if asset['key'] not in files:
                        asset['owners'] = []
                        files[asset['key']] = asset
                    files[asset['key']]['owners'].append(identifier)
            finally:
                sess.remove()
        # Shared assets still needed by unselected records must remain available.
        item_codes = {r['code'] for r in records if r['kind'] == 'item'}
        person_ids = {r['id'] for r in records if r['kind'] == 'person'}
        for asset in files.values():
            fid, relative = asset['file_id'], asset['relative_path']
            if item_codes:
                for category in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
                    sess, _, _ = cls.get_session_and_domain(category)
                    if sess is None:
                        raise RuntimeError('공유 파일 참조 확인 실패: ' + category)
                    try:
                        condition = MetaMedia.gdrive_fileid == fid if fid else and_(
                            MetaMedia.relative_path == relative, or_(MetaMedia.gdrive_fileid == '', MetaMedia.gdrive_fileid.is_(None)))
                        shared = sess.query(MetaMedia.id).join(MetaItem).filter(
                            MetaItem.category == category, ~MetaItem.code.in_(item_codes), condition).first() if fid or relative else None
                        if shared:
                            asset['retained'] = '다른 영상 DB 항목에서 사용하는 공유 파일'
                            break
                    finally:
                        sess.remove()
            if person_ids:
                sess, _, _ = cls.get_session_and_domain('PERSON')
                if sess is None:
                    raise RuntimeError('공유 인물 이미지 참조 확인 실패')
                try:
                    if fid:
                        condition = or_(MetaPerson.official_cached_gdrive_fileid == fid, MetaPerson.custom_gdrive_fileid == fid)
                    else:
                        condition = and_(MetaPerson.domain == ('WESTERN' if asset['category'] == 'WESTERN' else 'JAV'),
                            or_(MetaPerson.official_cached_local_path == relative, MetaPerson.custom_local_path == relative))
                    if (fid or relative) and sess.query(MetaPerson.id).filter(~MetaPerson.id.in_(person_ids), condition).first():
                        asset['retained'] = '다른 인물 DB 항목에서 사용하는 공유 파일'
                finally:
                    sess.remove()
        entries = sorted(files.values(), key=lambda a: a['key'])
        records.sort(key=lambda r: (r['category'], r['code']))
        signature = hashlib.sha256(json.dumps([records, entries], sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        return dict(records=records, files=entries, token=signature)

    @classmethod
    def confirm_delete_plan(cls, targets, mode, token):
        if mode not in ('db', 'all'):
            return {'ret':'error', 'msg':'삭제 범위를 선택하세요.'}
        plan = cls.get_delete_plan(targets)
        if not token or token != plan['token']:
            return {'ret':'error', 'msg':'DB 또는 저장소 설정이 변경되었습니다. 삭제 목록을 다시 확인하세요.'}
        for category in {r['category'] for r in plan['records']}:
            allowed, message = cls._schema_write_guard(category, '메타데이터 삭제')
            if not allowed:
                return {'ret':'error', 'msg':message}
        if mode == 'all':
            blocked = [f for f in plan['files'] if f['blocked'] and not f['retained']]
            if blocked:
                return {'ret':'error', 'msg':'삭제할 파일 경로/저장소 설정을 먼저 확인하세요.', 'files':blocked}
            errors = []
            try:
                for asset in plan['files']:
                    try:
                        MetaUtil.delete_owned_asset(asset)
                    except Exception as exc:
                        logger.exception('[MetaDB Delete] 파일 삭제 실패: %s', asset['path'] or asset['file_id'])
                        errors.append(dict(path=asset['path'] or asset['file_id'], error=str(exc)))
            finally:
                from .util_preview import MetaPreviewUtil
                MetaPreviewUtil.clear_dir_cache()
            if errors:
                return {'ret':'error', 'msg':'일부 파일을 삭제하지 못해 DB는 유지했습니다. 삭제된 파일은 되돌릴 수 없습니다.', 'files':errors}
        count = 0
        for category in sorted({r['category'] for r in plan['records']}):
            sess, _, _ = cls.get_session_and_domain(category)
            try:
                model = MetaPerson if category == 'PERSON' else MetaItem
                for descriptor in [r for r in plan['records'] if r['category'] == category]:
                    record = sess.query(model).options(lazyload('*')).filter_by(id=descriptor['id']).with_for_update(of=model).first()
                    if record is None or str(record.updated_time or '') != descriptor['updated']:
                        raise RuntimeError('삭제 도중 DB 항목이 변경되었습니다. 목록을 다시 확인하세요.')
                    sess.delete(record)
                sess.commit()
                count += sum(r['category'] == category for r in plan['records'])
            except Exception:
                sess.rollback()
                logger.exception('[MetaDB Delete] DB 삭제 실패 (%s)', category)
                return {'ret':'error', 'msg':'DB 삭제 실패. 완료 %d건. 목록을 새로 고친 뒤 재시도하세요.' % count}
            finally:
                sess.remove()
        logger.info('[MetaDB Delete] 선택 삭제 완료: mode=%s records=%d files=%d', mode, count, len(plan['files']))
        return {'ret':'success', 'msg':'%d건의 DB 데이터%s 삭제했습니다.' % (count, '와 관련 미디어를' if mode == 'all' else '만')}

    @classmethod
    def delete_record(cls, code, category=None):
        cls.ensure_db_ready()
        sess, domain, std_cat = cls.get_session_and_domain(category)
        if not sess:
            return False
        try:
            from sqlalchemy.orm import selectinload
            item = sess.query(MetaItem).options(
                selectinload(MetaItem.media_files),
                selectinload(MetaItem.person_maps),
                selectinload(MetaItem.tag_maps),
                selectinload(MetaItem.fingerprints),
            ).filter_by(code=code).first()
            if not item:
                logger.warning(f"[MetaDB Delete] [{std_cat}] 삭제 대상 메타데이터 없음: {code}")
                return False

            title_log = item.originaltitle or item.title or code

            if P.ModelSetting.get_bool("meta_db_delete_user_images"):
                MetaUtil.delete_item_user_images(item)

            from .util_preview import MetaPreviewUtil
            if MetaPreviewUtil.is_delete_video_on_meta_delete_enabled(item.category):
                MetaPreviewUtil.delete_video_assets(item.extra_info, category=item.category)

            sess.delete(item)
            sess.commit()
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
        cat_upper = str(category or '').strip().upper()
        if cat_upper == 'AV_ALL':
            # 전체 AV 통합 모드에서는 세부 카테고리를 순회하며 일괄 삭제
            total_deleted = 0
            for c_cat in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN']:
                succ, cnt = cls.delete_records(codes, category=c_cat)
                if succ:
                    total_deleted += cnt
            return True, total_deleted

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

            from .util_preview import MetaPreviewUtil
            delete_videos = MetaPreviewUtil.is_delete_video_on_meta_delete_enabled(std_cat)
            from sqlalchemy.orm import selectinload
            items = sess.query(MetaItem).options(
                selectinload(MetaItem.media_files),
                selectinload(MetaItem.person_maps),
                selectinload(MetaItem.tag_maps),
                selectinload(MetaItem.fingerprints),
            ).filter(MetaItem.category == std_cat, MetaItem.code.in_(code_list)).all()
            deleted_count = 0

            for item in items:
                if delete_user_imgs:
                    MetaUtil.delete_item_user_images(item)

                if delete_videos:
                    MetaPreviewUtil.delete_video_assets(item.extra_info, category=item.category)

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
            from .util_preview import MetaPreviewUtil
            if MetaPreviewUtil.is_delete_video_on_meta_delete_enabled(std_cat):
                all_items = (
                    sess.query(MetaItem).options(lazyload('*'))
                    .filter_by(category=std_cat)
                    .outerjoin(MetaItem.person_maps)
                    .order_by(MetaItemPersonMap.sort_order)
                    .all()
                )
                for it in all_items:
                    MetaPreviewUtil.delete_video_assets(it.extra_info, category=std_cat)

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

    def _get_batch_snapshot(self, batch_type='item'):
        """Read worker state without replacing the web process's local job state."""
        batch_key = 'person' if str(batch_type).lower() == 'person' else 'item'
        with self._batch_log_lock:
            status = dict(self.batch_status[batch_key])
            logs = list(self.batch_log_history[batch_key])
        try:
            shared_cache = F.get_cache(f"{P.package_name}_batch_job_{batch_key}")
            raw_status = shared_cache.get('status')
            raw_logs = shared_cache.get('logs')
            if raw_status:
                cached_status = json.loads(raw_status)
                if isinstance(cached_status, dict):
                    status = cached_status
            if raw_logs:
                cached_logs = json.loads(raw_logs)
                if isinstance(cached_logs, list):
                    logs = cached_logs
        except Exception as exc:
            logger.debug(f"[MetaDB Batch] 공유 상태 조회 실패 ({batch_key}): {exc}")
        # The full resume queue belongs in persistent state, not every UI update.
        status.pop('target_items', None)
        return status, logs[-300:]

    def _broadcast_batch_log(self, log_line, event_type="log", is_updated=False, batch_type='item'):
        """트랙(item/person)별 실시간 SSE 구독자들에게 로그 및 상태를 전송하고 링 버퍼에 보관."""
        batch_key = 'person' if str(batch_type).lower() == 'person' else 'item'
        timestamp_str = datetime.now().strftime('%H:%M:%S')
        formatted_entry = f"[{timestamp_str}] {log_line}"

        with self._batch_log_lock:
            history = self.batch_log_history[batch_key]
            history.append(formatted_entry)
            if len(history) > 300:
                history.pop(0)

            public_status = {key: value for key, value in self.batch_status[batch_key].items()
                             if key != 'target_items'}
            history_snapshot = list(history)
            subscribers = list(self.batch_log_subscribers[batch_key])

        # Emit once at the worker, independent of SSE subscribers/cache availability.
        logger.debug('[MetaDB Batch] [%s/%s] [%s] %s', batch_key,
                     public_status.get('mode') or 'unknown', event_type, log_line)
        # Celery 프로세스와 웹 프로세스 간 상태 공유를 위해 트랙별 캐시 동기화
        try:
            shared_cache = F.get_cache(f"{P.package_name}_batch_job_{batch_key}")
            shared_cache.set('status', json.dumps(public_status, ensure_ascii=False))
            shared_cache.set('logs', json.dumps(history_snapshot, ensure_ascii=False))
        except Exception:
            pass

        dead_subs = []
        for sub_queue in subscribers:
            try:
                payload = json.dumps({
                    'type': event_type,
                    'message': formatted_entry,
                    'status': public_status,
                    'is_updated': is_updated
                }, ensure_ascii=False)
                sub_queue.put(f"data: {payload}\n\n")
            except Exception:
                dead_subs.append(sub_queue)

        with self._batch_log_lock:
            for dead in dead_subs:
                if dead in self.batch_log_subscribers[batch_key]:
                    self.batch_log_subscribers[batch_key].remove(dead)

    def _save_persistent_batch_state(self, batch_type='item'):
        """트랙별 진행 상태와 큐를 디스크에 독립 보존."""
        batch_key = 'person' if str(batch_type).lower() == 'person' else 'item'
        state_file = self.batch_state_files.get(batch_key)
        if not state_file:
            return

        try:
            os.makedirs(os.path.dirname(state_file), exist_ok=True)
            with self._batch_log_lock:
                logs_copy = list(self.batch_log_history[batch_key][-150:])
            save_payload = {
                'status_data': self.batch_status[batch_key],
                'logs': logs_copy
            }
            tmp_path = state_file + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(save_payload, f, ensure_ascii=False)
            if os.path.exists(tmp_path):
                shutil.move(tmp_path, state_file)
        except Exception as e:
            logger.debug(f"[{self.name}] [{batch_key}] 배치 상태 파일 저장 예외: {e}")

    def _load_persistent_batch_state(self):
        """플러그인 로드 시 트랙별로 보관된 배치 작업 상태 복원 및 재시작 중단 보정."""
        for batch_key in ['item', 'person']:
            state_file = self.batch_state_files.get(batch_key)
            if not state_file or not os.path.exists(state_file):
                continue

            try:
                with open(state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    status = data.get('status_data', {})
                    logs = data.get('logs', [])

                    if status.get('is_running'):
                        status['is_running'] = False
                        status['stop_flag'] = False
                        curr_idx = status.get('resume_index', 0)
                        tot = status.get('total', 0)
                        if tot > 0 and curr_idx < tot:
                            status['can_resume'] = True
                            status['status'] = f"FF 재시작으로 중단됨 (#{curr_idx + 1}번부터 재개 가능)"
                            logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] [시스템] FF 재시작 감지 ➔ 작업이 일시 중단되었습니다 (#{curr_idx + 1}/{tot} 재개 가능)")
                        else:
                            status['can_resume'] = False
                            status['status'] = '완료'

                    self.batch_status[batch_key].update(status)
                    with self._batch_log_lock:
                        self.batch_log_history[batch_key] = logs[-300:]

                    self._save_persistent_batch_state(batch_key)
                    try:
                        shared_cache = F.get_cache(f"{P.package_name}_batch_job_{batch_key}")
                        shared_cache.set('status', json.dumps(self.batch_status[batch_key], ensure_ascii=False))
                        shared_cache.set('logs', json.dumps(self.batch_log_history[batch_key], ensure_ascii=False))
                        shared_cache.set('stop_flag', 'false')
                    except Exception:
                        pass

                    logger.debug(f"[{self.name}] [{batch_key}] 영구 보관된 배치 작업 상태 복원 완료: {self.batch_status[batch_key].get('status')}")
            except Exception as e:
                logger.error(f"[{self.name}] [{batch_key}] 배치 상태 파일 복원 예외: {e}")


    @staticmethod
    def _format_seconds(sec_val):
        """초 단위 숫자를 시/분/초 문자열(HH:MM:SS 또는 MM:SS)로 변환."""
        total_sec = max(0, int(sec_val or 0))
        m, s = divmod(total_sec, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"


    def _run_works_sync_batch(self, job_status, batch_opts, batch_key):
        """소장 작품(MetaItem) 전수 스캔 및 인물(MetaPerson) 출연작 연계/통계 일괄 갱신 엔진."""
        from .util_runtime import cooperate
        clean_orphans = batch_opts.get('works_sync_clean_orphan', True)
        shared_cache = F.get_cache(f'{P.package_name}_batch_job_{batch_key}')
        shared_cache.set('stop_flag', 'false')
        def should_stop():
            return job_status.get('stop_flag') or shared_cache.get('stop_flag') in ('true', b'true')
        job_status.update({
            'is_running': True,
            'status': '작품 전수 스캔 중...',
            'mode': 'works_sync',
            'current': 0,
            'total': 0,
            'success': 0,
            'fail': 0,
            'skipped': 0,
            'stop_flag': False,
            'can_resume': False,
            'elapsed_str': '00:00',
            'eta_str': '계산 중...'
        })
        self._broadcast_batch_log(
            "배치 작업 시작 설정 ➔ 모드: works_sync, 서브 옵션: 오매칭 출연작 자동 정제: "
            + ("ON" if clean_orphans else "OFF"),
            batch_type=batch_key
        )
        self._broadcast_batch_log("소장 작품(JAV 및 Western) 전수 스캔 및 메모리 매핑 구축 시작...", batch_type=batch_key)

        scanned_works_by_idx = {}
        scanned_works_by_name = {}

        for cat_scan in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN']:
            scan_eng = self._engines.get('postgres') if self._is_postgres else self._engines.get(DOMAIN_MAP[cat_scan][0])
            if not scan_eng:
                continue

            scan_sess = sessionmaker(bind=scan_eng, autocommit=False, autoflush=False, expire_on_commit=False)()
            try:
                items_data = scan_sess.query(
                    MetaItem.code, MetaItem.ui_code, MetaItem.title,
                    MetaItem.originaltitle, MetaItem.year, MetaItem.premiered,
                    MetaItem.extra_info, MetaItem.original
                ).filter(MetaItem.category == cat_scan).yield_per(500)

                for m_code, m_ui_code, m_title, m_orig_title, m_year, m_prem, m_extra, m_orig in items_data:
                    cooperate()
                    if should_stop():
                        job_status.update(is_running=False, status='중단됨')
                        self._broadcast_batch_log('사용자 요청으로 작품 스캔이 중단되었습니다.', batch_type=batch_key)
                        return
                    y_val = str(m_year) if (m_year and m_year != 1900) else (str(m_prem)[:4] if m_prem else '')
                    work_entry = {
                        'code': m_code,
                        'ui_code': m_ui_code or m_orig_title or m_code,
                        'title': m_title or m_orig_title or '',
                        'year': y_val
                    }

                    movie_actors = []
                    for src_list in [
                        (m_extra or {}).get('_actors'),
                        (m_extra or {}).get('actor_cache'),
                        (m_extra or {}).get('actors'),
                        (m_orig or {}).get('actor')
                    ]:
                        if isinstance(src_list, list):
                            movie_actors.extend(src_list)

                    for act in movie_actors:
                        if not act:
                            continue

                        act_idx = (act.get('actor_idx') or act.get('person_idx') or '').strip().upper() if isinstance(act, dict) else ''
                        act_org = str((act.get('name_org') or act.get('name') or '') if isinstance(act, dict) else act).strip().lower()
                        act_ko = (act.get('name_ko') or '').strip().lower() if isinstance(act, dict) else ''

                        def append_scanned(mapping, key, entry):
                            if not key:
                                return
                            if key not in mapping:
                                mapping[key] = {}
                            if cat_scan not in mapping[key]:
                                mapping[key][cat_scan] = []
                            if not any(x['code'] == entry['code'] for x in mapping[key][cat_scan]):
                                mapping[key][cat_scan].append(entry)

                        if act_idx:
                            append_scanned(scanned_works_by_idx, act_idx, work_entry)
                            num_part = re.sub(r'^[A-Za-z]+', '', act_idx)
                            if num_part:
                                append_scanned(scanned_works_by_idx, num_part, work_entry)
                        if act_org:
                            append_scanned(scanned_works_by_name, act_org, work_entry)
                        if act_ko:
                            append_scanned(scanned_works_by_name, act_ko, work_entry)
            finally:
                scan_sess.close()

        self._broadcast_batch_log("작품 스캔 완료 ➔ 인물 DB 전수 대조 및 물리 통계 일괄 반영 시작...", batch_type=batch_key)

        p_sess, _, _ = self.get_session_and_domain('PERSON')
        if not p_sess:
            job_status['fail'] = 1
            job_status['is_running'] = False
            job_status['status'] = '인물 DB 세션 획득 실패'
            self._broadcast_batch_log("인물 DB 세션 획득에 실패했습니다.", event_type="error", batch_type=batch_key)
            return

        try:
            person_query = p_sess.query(MetaPerson)
            total_len = person_query.count()
            def person_pages():
                last_id = 0
                while True:
                    page = person_query.filter(MetaPerson.id > last_id).order_by(MetaPerson.id).limit(500).all()
                    if not page:
                        return
                    last_id = page[-1].id
                    cooperate()
                    yield from page
                    p_sess.flush()
                    p_sess.expunge_all()
            all_persons = person_pages()
            job_status['total'] = total_len
            healed_persons_count = 0
            t_works_start = time.time()

            for p_idx_num, p_item in enumerate(all_persons, 1):
                cooperate()
                if should_stop():
                    p_sess.commit()
                    job_status['is_running'] = False
                    job_status['status'] = '중단됨'
                    self._broadcast_batch_log("사용자 요청으로 출연작 동기화가 중단되었습니다.", event_type="warning", batch_type=batch_key)
                    return

                job_status['current'] = p_idx_num
                job_status['current_code'] = p_item.name_ko or p_item.name_org or p_item.person_idx

                # 이번 세션 기준 실시간 소요 시간 및 ETA 연산
                elapsed_sec = time.time() - t_works_start
                remain_items = total_len - p_idx_num
                job_status['elapsed_str'] = self._format_seconds(elapsed_sec)
                if p_idx_num > 0 and elapsed_sec > 0:
                    sec_per_item = elapsed_sec / p_idx_num
                    job_status['eta_str'] = self._format_seconds(sec_per_item * remain_items)

                matched_works = {}
                cand_keys_idx = [p_item.person_idx.upper()] if p_item.person_idx else []
                for alt_id in (p_item.alt_indices or []):
                    if alt_id and alt_id.upper() not in cand_keys_idx:
                        cand_keys_idx.append(alt_id.upper())

                for c_k in cand_keys_idx:
                    if c_k in scanned_works_by_idx:
                        for cat_k, w_list in scanned_works_by_idx[c_k].items():
                            if cat_k not in matched_works:
                                matched_works[cat_k] = []
                            for w in w_list:
                                if not any(x['code'] == w['code'] for x in matched_works[cat_k]):
                                    matched_works[cat_k].append(w)

                cand_keys_name = []
                for n_val in [p_item.name_org, p_item.name_ko, p_item.name_en]:
                    if n_val and n_val.strip().lower() not in cand_keys_name:
                        cand_keys_name.append(n_val.strip().lower())
                for al in (p_item.aliases or []):
                    if al and al.strip().lower() not in cand_keys_name:
                        cand_keys_name.append(al.strip().lower())

                for c_n in cand_keys_name:
                    if c_n in scanned_works_by_name:
                        for cat_k, w_list in scanned_works_by_name[c_n].items():
                            if cat_k not in matched_works:
                                matched_works[cat_k] = []
                            for w in w_list:
                                if not any(x['code'] == w['code'] for x in matched_works[cat_k]):
                                    matched_works[cat_k].append(w)

                old_works = p_item.works if isinstance(p_item.works, dict) else {}
                if clean_orphans:
                    new_works = matched_works
                else:
                    new_works = copy.deepcopy(old_works)
                    for cat_k, w_list in matched_works.items():
                        if cat_k not in new_works:
                            new_works[cat_k] = []
                        for w in w_list:
                            if not any(x['code'] == w['code'] for x in new_works[cat_k]):
                                new_works[cat_k].append(w)

                calc_works_count = sum(len(w_list) for w_list in new_works.values() if isinstance(w_list, list))

                is_changed = (p_item.works_count != calc_works_count) or (p_item.works != new_works)
                if is_changed:
                    p_item.works = new_works
                    p_item.works_count = calc_works_count
                    healed_persons_count += 1

                if p_idx_num % 500 == 0:
                    p_sess.commit()
                    self._broadcast_batch_log(f'출연작 통계 처리: {p_idx_num:,}/{total_len:,}', batch_type=batch_key)
                    self._save_persistent_batch_state(batch_type=batch_key)

            p_sess.commit()
            job_status['success'] = healed_persons_count
            job_status['is_running'] = False
            job_status['status'] = '완료'
            job_status['eta_str'] = '00:00'
            job_status['elapsed_str'] = self._format_seconds(time.time() - t_works_start)
            self._broadcast_batch_log(
                f"출연작 전수 동기화 완료: 전체 {total_len}명 중 {healed_persons_count}명 연계 및 통계 갱신 (소요: {job_status['elapsed_str']})",
                is_updated=True,
                batch_type=batch_key
            )
            self._save_persistent_batch_state(batch_type=batch_key)
        except Exception as e_wsync:
            p_sess.rollback()
            job_status['fail'] = 1
            job_status['is_running'] = False
            job_status['status'] = '실패'
            logger.error(f"[MetaDB Batch WorksSync] 예외 발생: {e_wsync}")
            self._broadcast_batch_log(f"출연작 동기화 실패: {e_wsync}", event_type="error", batch_type=batch_key)
            self._save_persistent_batch_state(batch_type=batch_key)
        finally:
            p_sess.remove()


    def run_batch_refresh_worker(self, target_scope, target_codes=None, search_params=None, refresh_mode='missing', delay=2.0, action='restart', batch_type='item', batch_options=None):
        from .util_runtime import batch_database_limits
        try:
            with batch_database_limits(logger):
                return self._run_batch_refresh_worker(target_scope, target_codes, search_params,
                    refresh_mode, delay, action, batch_type, batch_options)
        except Exception as exc:
            batch_key = 'person' if (str(batch_type).lower() == 'person' or refresh_mode in ('person_image_sync', 'works_sync')) else 'item'
            status = self.batch_status[batch_key]
            status.update(is_running=False, status=f'오류: {exc}',
                          can_resume=bool(status.get('target_items')))
            logger.exception('[MetaDB Batch] [%s/%s] 배치 실행 중단', batch_key, refresh_mode)
            self._broadcast_batch_log(f'배치 작업 중단: {exc}', event_type='error', batch_type=batch_key)
            self._save_persistent_batch_state(batch_key)
            raise
        finally:
            # One best-effort checkpoint after the batch, including stop/failure paths.
            self.checkpoint_wal()

    def _run_batch_refresh_worker(self, target_scope, target_codes=None, search_params=None, refresh_mode='missing', delay=2.0, action='restart', batch_type='item', batch_options=None):
        """선택 항목 또는 하단 검색 조건을 대상으로 배치 작업을 실행하며, item/person 트랙별로 독립 격리 실행."""
        from .util_runtime import cooperate
        batch_key = 'person' if (str(batch_type).lower() == 'person' or refresh_mode in ['person_image_sync', 'works_sync']) else 'item'
        job_status = self.batch_status[batch_key]
        batch_opts = dict(batch_options or {})
        if refresh_mode in ('media_sync', 'media_reprocess'):
            # These modes share the modal pipeline; legacy storage-only switches do not apply.
            batch_opts = {key: value for key, value in batch_opts.items() if not key.startswith('media_sync_')}

        option_labels = {
            'person_skip_existing': '기존 인물 이미지 처리 생략',
            'western_protect_manual': '수동 편집 데이터 보호',
            'western_skip_existing': '기존 Western 이미지 처리 생략',
            'works_sync_clean_orphan': '오매칭 출연작 자동 정제',
            'missing_include_media': '이미지/미디어 동기화 포함',
            'in_place_skip_media': '이미지/미디어 처리 생략',
            'auto_search_under_95': '95점 미만 항목만 대상'
        }
        configured_options = [
            f"{label}: {'ON' if bool(batch_opts.get(key)) else 'OFF'}"
            for key, label in option_labels.items()
            if key in batch_opts
        ]
        if refresh_mode in ('media_sync', 'media_reprocess'):
            configured_options.append('기존 일반 미디어 재사용: ' + ('ON' if refresh_mode == 'media_sync' else 'OFF'))
            configured_options.append('사용자 이미지 보호: ON')
            configured_options.append('기존 프리뷰 보호: ON')
        option_summary = ', '.join(configured_options) if configured_options else '없음'
        def broadcast_start_config():
            self._broadcast_batch_log(
                f"배치 작업 시작 설정 ➔ 모드: {refresh_mode}, 딜레이: {delay:g}초, 서브 옵션: {option_summary}",
                batch_type=batch_key
            )

        # works_sync 모드는 전용 단일 패스 스캔 엔진으로 즉시 분기 실행
        if refresh_mode == 'works_sync':
            self._run_works_sync_batch(job_status, batch_opts, batch_key)
            return

        if refresh_mode == 'person_image_sync':
            allowed, guard_msg = self._schema_write_guard('PERSON', '인물 GDrive 동기화')
            if not allowed:
                job_status.update({'is_running': False, 'status': guard_msg})
                logger.warning(f"[MetaDB Batch Person] {guard_msg}")
                return

        from .util_metadata import MetaUtil
        from .util_preview import MetaPreviewUtil

        search_params = search_params or {}
        is_resume = (action == 'resume' and job_status.get('can_resume') and job_status.get('target_items'))

        if is_resume:
            target_items = job_status['target_items']
            start_idx = job_status.get('resume_index', 0)
            job_status.update({
                'is_running': True,
                'status': '작업 재개 중...',
                'mode': refresh_mode,
                'stop_flag': False,
                'can_resume': False
            })
            self._broadcast_batch_log(f"배치 작업 재개 ➔ #{start_idx + 1}번 항목부터 이어서 진행합니다. (총 {len(target_items)}건)", batch_type=batch_key)
            broadcast_start_config()
        else:
            job_status.update({
                'is_running': True,
                'status': '대상 항목 수집 중...',
                'mode': refresh_mode,
                'total': 0,
                'current': 0,
                'success': 0,
                'fail': 0,
                'skipped': 0,
                'current_code': '',
                'stop_flag': False,
                'can_resume': False,
                'resume_index': 0,
                'target_items': [],
                'elapsed_str': '00:00',
                'eta_str': '계산 중...'
            })
            with self._batch_log_lock:
                self.batch_log_history[batch_key].clear()

            self._broadcast_batch_log(f"배치 작업 세션 초기화 완료 ➔ 대상 수집 및 준비를 시작합니다 (모드: {refresh_mode})", batch_type=batch_key)
            broadcast_start_config()
            target_items = []

            # 체크박스로 선택된 항목 대상 처리
            if target_scope in ['selected', 'auto'] and target_codes:
                clean_codes = target_codes if isinstance(target_codes, list) else json.loads(target_codes)
                if clean_codes:
                    selected_items = []
                    for raw_item in clean_codes:
                        if isinstance(raw_item, dict):
                            raw_code = str(raw_item.get('code') or raw_item.get('ui_code') or '').strip()
                            raw_ui_code = str(raw_item.get('ui_code') or raw_code).strip()
                            raw_category = str(raw_item.get('category') or '').strip().upper()
                            if raw_code:
                                selected_items.append({
                                    'code': raw_code,
                                    'ui_code': raw_ui_code,
                                    'category': raw_category
                                })
                        else:
                            raw_code = str(raw_item).strip()
                            if raw_code:
                                selected_items.append({'code': raw_code, 'ui_code': raw_code, 'category': ''})

                    clean_codes = [item['code'] for item in selected_items]
                    self._broadcast_batch_log(f"체크박스 선택 항목 {len(clean_codes)}건을 대상으로 선정합니다.", batch_type=batch_key)
                    selected_by_code = {}
                    legacy_items = [item for item in selected_items if not item['category']]

                    for item in selected_items:
                        if item['category'] in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN']:
                            selected_by_code[(item['code'].lower(), item['category'])] = {
                                'code': item['code'],
                                'ui_code': item['ui_code'],
                                'category': item['category']
                            }

                    for cat in (['JAV_CEN', 'JAV_UNCEN', 'WESTERN'] if legacy_items else []):
                        sess, _, std_cat = self.get_session_and_domain(cat)
                        if not sess: continue
                        try:
                            query_codes = [
                                item['code'] for item in selected_items
                                if not item['category'] or item['category'] == std_cat
                            ]
                            rows = sess.query(MetaItem.code, MetaItem.ui_code, MetaItem.category).filter(
                                MetaItem.category == std_cat,
                                or_(MetaItem.code.in_(query_codes), MetaItem.ui_code.in_(query_codes))
                            ).all()
                            for r in rows:
                                selected_by_code.setdefault((str(r.code).lower(), r.category), {
                                    'code': r.code, 'ui_code': r.ui_code or r.code, 'category': r.category
                                })
                                if r.ui_code:
                                    selected_by_code.setdefault((str(r.ui_code).lower(), r.category), {
                                        'code': r.code, 'ui_code': r.ui_code, 'category': r.category
                                    })
                        finally:
                            sess.remove()
                    target_items = [
                        selected_by_code.get((item['code'].lower(), item['category'])) or
                        next(
                            (value for (lookup_code, _lookup_cat), value in selected_by_code.items()
                             if lookup_code == item['code'].lower()),
                            None
                        )
                        for item in selected_items
                        if selected_by_code.get((item['code'].lower(), item['category'])) or
                        any(lookup_code == item['code'].lower() for lookup_code, _ in selected_by_code)
                    ]

            # 하단 검색/필터 조건 대상 처리
            if not target_items and target_scope in ['search_filter', 'auto']:
                if batch_key == 'person':
                    p_sess, _, _ = self.get_session_and_domain('PERSON')
                    if p_sess:
                        try:
                            s_domain = str(search_params.get('search_domain') or 'ALL').upper()
                            s_status = str(search_params.get('search_status') or 'all')
                            s_word = str(search_params.get('search_word') or '').strip()
                            s_order = str(search_params.get('search_order') or 'desc').strip()

                            q_person = p_sess.query(MetaPerson)
                            if refresh_mode == 'western_person_refresh':
                                q_person = q_person.filter(func.upper(MetaPerson.domain) == 'WESTERN')
                            elif s_domain != 'ALL':
                                q_person = q_person.filter(func.upper(MetaPerson.domain) == s_domain)

                            if s_status == 'no_photo':
                                q_person = q_person.filter(or_(MetaPerson.has_photo == False, MetaPerson.has_photo == None))
                            elif s_status == 'has_photo':
                                q_person = q_person.filter(MetaPerson.has_photo == True)
                            elif s_status == 'has_works':
                                q_person = q_person.filter(MetaPerson.works_count > 0)
                            elif s_status == 'merged_only':
                                q_person = q_person.filter(MetaPerson.merged_count > 1)

                            if s_word:
                                s_like = f"%{s_word}%"
                                q_person = q_person.filter(or_(
                                    MetaPerson.name_org.ilike(s_like),
                                    MetaPerson.name_ko.ilike(s_like),
                                    MetaPerson.name_en.ilike(s_like),
                                    MetaPerson.person_idx.ilike(s_like)
                                ))

                            effective_person_name = func.coalesce(
                                func.nullif(MetaPerson.name_ko, ''),
                                MetaPerson.name_org
                            )
                            if s_order == 'name_asc':
                                q_person = q_person.order_by(effective_person_name.asc(), MetaPerson.id.asc())
                            elif s_order == 'name_desc':
                                q_person = q_person.order_by(effective_person_name.desc(), MetaPerson.id.desc())
                            elif s_order == 'works_desc':
                                q_person = q_person.order_by(MetaPerson.works_count.desc(), MetaPerson.id.desc())
                            elif s_order == 'desc':
                                q_person = q_person.order_by(MetaPerson.id.desc())
                            elif s_order == 'id_desc':
                                q_person = q_person.order_by(MetaPerson.id.desc())
                            else:
                                q_person = q_person.order_by(MetaPerson.id.asc())

                            for p in q_person.with_entities(MetaPerson.id, MetaPerson.person_idx, MetaPerson.name_org, MetaPerson.name_ko, MetaPerson.domain).yield_per(500):
                                if len(target_items) % 500 == 0:
                                    cooperate()
                                target_items.append({
                                    'id': p.id,
                                    'person_idx': p.person_idx or str(p.id),
                                    'name_org': p.name_org,
                                    'name_ko': p.name_ko or p.name_org,
                                    'domain': p.domain or 'JAV',
                                    'is_person': True
                                })
                        finally:
                            p_sess.remove()
                else:
                    search_cat = str(search_params.get('search_category') or 'AV_ALL').strip().upper()
                    search_cat_aliases = {
                        'ALL': 'AV_ALL',
                        'JAV_CENSORED': 'JAV_CEN',
                        'JAV_UNCENSORED': 'JAV_UNCEN',
                        'WEST': 'WESTERN',
                    }
                    search_cat = search_cat_aliases.get(search_cat, search_cat)
                    if search_cat not in ['AV_ALL', 'JAV_CEN', 'JAV_UNCEN', 'WESTERN']:
                        self._broadcast_batch_log(
                            f"유효하지 않은 검색 카테고리 '{search_cat}'입니다. 실제 레코드 category를 확인할 수 없어 대상 수집을 중단합니다.",
                            event_type="warning",
                            batch_type=batch_key
                        )
                        categories = []
                    else:
                        categories = ['JAV_CEN', 'JAV_UNCEN', 'WESTERN'] if search_cat == 'AV_ALL' else [search_cat]

                    s_site = str(search_params.get('search_site') or 'all').strip()
                    s_status = str(search_params.get('search_status') or 'all').strip()
                    s_word = str(search_params.get('search_word') or '').strip()
                    s_order = str(search_params.get('search_order') or 'desc').strip()

                    self._broadcast_batch_log(f"하단 검색 조건 대조 수집 시작 ➔ 카테고리: {search_cat}, 상태: {s_status}, 사이트: {s_site}, 검색어: '{s_word}'", batch_type=batch_key)

                    # 검색 및 상태 필터 조건 조립 헬퍼
                    def build_filter_conditions(cat_condition):
                        cond_list = [cat_condition]
                        if s_site and s_site.lower() not in ['all', '']:
                            cond_list.append(func.lower(MetaItem.site) == s_site.lower())

                        if s_status == 'no_actor':
                            cond_list.append(and_(
                                ~MetaItem.person_maps.any(),
                                or_(
                                    MetaItem.extra_info == None,
                                    cast(MetaItem.extra_info, Text) == '{}',
                                    and_(
                                        ~cast(MetaItem.extra_info, Text).ilike('%name_org%'),
                                        ~cast(MetaItem.extra_info, Text).ilike('%name_ko%')
                                    )
                                )
                            ))
                        elif s_status == 'no_plot':
                            cond_list.append(or_(MetaItem.plot == '', MetaItem.plot == None))
                        elif s_status == 'no_trailer':
                            cond_list.append(or_(
                                MetaItem.extra_info == None,
                                ~cast(MetaItem.extra_info, Text).ilike('%trailer_video%')
                            ))
                        elif s_status == 'no_poster':
                            cond_list.append(or_(
                                MetaItem.poster_url == '',
                                MetaItem.poster_url == None,
                                MetaItem.poster_url.ilike('%_pl.jpg'),
                                MetaItem.poster_url.ilike('%_pl.png'),
                                MetaItem.poster_url.ilike('%_pl.jpeg'),
                                MetaItem.poster_url.ilike('%_pl.webp')
                            ))
                        elif s_status == 'complete':
                            cond_list.append(and_(
                                MetaItem.poster_url != '',
                                MetaItem.poster_url != None,
                                MetaItem.plot != '',
                                MetaItem.plot != None
                            ))

                        if s_word:
                            s_like = f"%{s_word.replace('-', '%')}%"
                            cond_list.append(or_(
                                MetaItem.originaltitle.ilike(s_like),
                                MetaItem.ui_code.ilike(s_like),
                                MetaItem.code.ilike(s_like),
                                MetaItem.title.ilike(f'%{s_word}%'),
                                MetaItem.studio.ilike(f'%{s_word}%')
                            ))
                        return cond_list

                    if self._is_postgres:
                        # PostgreSQL: 단일 통합 DB이므로 전체 대상 카테고리에 대해 단일 쿼리 및 DB 정렬 실행
                        sess, _, _ = self.get_session_and_domain('JAV_CEN')
                        if sess:
                            try:
                                cat_condition = MetaItem.category.in_(categories) if len(categories) > 1 else (MetaItem.category == categories[0])
                                conds = build_filter_conditions(cat_condition)
                                q = sess.query(MetaItem.code, MetaItem.ui_code, MetaItem.category).filter(and_(*conds))
                                if s_order == 'asc':
                                    q = q.order_by(MetaItem.created_time.asc(), MetaItem.id.asc())
                                elif s_order == 'title_asc':
                                    q = q.order_by(MetaItem.title.asc(), MetaItem.id.asc())
                                elif s_order == 'title_desc':
                                    q = q.order_by(MetaItem.title.desc(), MetaItem.id.desc())
                                else:
                                    q = q.order_by(MetaItem.created_time.desc(), MetaItem.id.desc())

                                for r in q.yield_per(500):
                                    target_items.append({'code': r.code, 'ui_code': r.ui_code or r.code, 'category': r.category})
                                    if len(target_items) % 500 == 0:
                                        cooperate()
                            finally:
                                sess.remove()
                    else:
                        # SQLite: 도메인별 분산 DB 파일에서 수집 후 웹 목록과 동일하게 메모리 통합 정렬 수행
                        temp_collected = []
                        for cat in categories:
                            sess, _, std_cat = self.get_session_and_domain(cat)
                            if not sess: continue
                            try:
                                conds = build_filter_conditions(MetaItem.category == std_cat)
                                q = sess.query(
                                    MetaItem.code,
                                    MetaItem.ui_code,
                                    MetaItem.category,
                                    MetaItem.title,
                                    MetaItem.created_time,
                                    MetaItem.id
                                ).filter(and_(*conds))

                                for r in q.yield_per(500):
                                    if len(temp_collected) % 500 == 0:
                                        cooperate()
                                    temp_collected.append({
                                        'code': r.code,
                                        'ui_code': r.ui_code or r.code,
                                        'category': r.category,
                                        'title': r.title or '',
                                        'created_time': r.created_time or datetime.min,
                                        'id': r.id or 0
                                    })
                            finally:
                                sess.remove()

                        # 웹 목록 정렬 기준과 일치하도록 전체 카테고리 통합 정렬
                        if s_order == 'asc':
                            temp_collected.sort(key=lambda x: (x['created_time'], x['id']))
                        elif s_order == 'title_asc':
                            temp_collected.sort(key=lambda x: (x['title'].lower(), x['id']))
                        elif s_order == 'title_desc':
                            temp_collected.sort(key=lambda x: (x['title'].lower(), x['id']), reverse=True)
                        else:
                            temp_collected.sort(key=lambda x: (x['created_time'], x['id']), reverse=True)

                        for it in temp_collected:
                            target_items.append({'code': it['code'], 'ui_code': it['ui_code'], 'category': it['category']})

            start_idx = 0
            job_status['target_items'] = target_items

        total_len = len(target_items)
        job_status['total'] = total_len

        if total_len == 0:
            job_status.update({'is_running': False, 'status': '완료 (대상 없음)'})
            self._broadcast_batch_log("처리할 대상 항목이 없습니다. 하단 목록에서 선택하거나 필터 조건을 확인하세요.", batch_type=batch_key)
            self._save_persistent_batch_state(batch_type=batch_key)
            return

        memory_person_indices = {}
        if refresh_mode == 'local_sync':
            from .util_metadata import PersonMemoryIndex
            domains_needed = set('WESTERN' if it['category'] == 'WESTERN' else 'JAV' for it in target_items)
            for dom in domains_needed:
                self._broadcast_batch_log(f"인물 DB 메모리 색인 테이블 구축 시작 ({dom})...", batch_type=batch_key)
                memory_person_indices[dom] = PersonMemoryIndex(domain=dom)
                self._broadcast_batch_log(f"인물 색인 적재 완료 ({dom}: 총 {len(memory_person_indices[dom].by_idx):,}명 색인화)", batch_type=batch_key)

        effective_delay = max(0, delay)
        if refresh_mode in ('local_sync', 'person_image_sync'):
            delay_desc = '없음(사이트 검색·정보 조회 없음)'
        else:
            delay_desc = f'{effective_delay}초(사이트 검색·정보 조회 시에만)'

        job_status['status'] = f'작업 진행 중 ({total_len}건)'
        self._broadcast_batch_log(f"작업 준비 완료 ➔ 총 {total_len}건 (적용 딜레이: {delay_desc})", batch_type=batch_key)
        self._save_persistent_batch_state(batch_type=batch_key)
        t_start = time.time()
        session_start_idx = start_idx

        # 배치 실행 세션 동안 폴더 단위 1회 일괄 적재를 위해 MetaPreviewUtil 캐시 초기화
        MetaPreviewUtil.clear_dir_cache()

        for idx in range(start_idx, total_len):
            cooperate()
            stop_requested = job_status.get('stop_flag')
            try:
                shared_cache = F.get_cache(f"{P.package_name}_batch_job_{batch_key}")
                if shared_cache.get('stop_flag') == 'true':
                    stop_requested = True
            except Exception:
                pass

            if stop_requested:
                job_status['resume_index'] = idx
                job_status['can_resume'] = True
                job_status['is_running'] = False
                job_status['status'] = f'중단됨 (#{idx + 1}번부터 재개 가능)'
                job_status['elapsed_str'] = self._format_seconds(time.time() - t_start)
                self._broadcast_batch_log(f"작업이 일시 정지되었습니다 ➔ 총 {total_len}건 중 {idx}건 완료 (#{idx + 1}번부터 재개 가능, 소요: {job_status['elapsed_str']})", event_type="warning", batch_type=batch_key)
                try:
                    shared_cache = F.get_cache(f"{P.package_name}_batch_job_{batch_key}")
                    shared_cache.set('stop_flag', 'false')
                except Exception:
                    pass

                self._save_persistent_batch_state(batch_type=batch_key)
                return

            item = target_items[idx]
            site_activity = {}
            job_status['current'] = idx + 1

            # 이번 세션 시작 시점 기준 소요 시간 및 ETA 실시간 연산
            elapsed_sec = time.time() - t_start
            session_processed = (idx + 1) - session_start_idx
            remain_items = total_len - (idx + 1)

            job_status['elapsed_str'] = self._format_seconds(elapsed_sec)
            if session_processed > 0 and elapsed_sec > 0:
                sec_per_item = elapsed_sec / session_processed
                job_status['eta_str'] = self._format_seconds(sec_per_item * remain_items)

            if batch_key == 'person':
                p_id = item.get('id')
                p_name = item.get('name_ko') or item.get('name_org') or '배우'
                p_domain = item.get('domain') or 'JAV'
                job_status['current_code'] = p_name

                if refresh_mode == 'western_person_refresh':
                    if p_domain != 'WESTERN':
                        job_status['skipped'] += 1
                        continue

                    protect_manual = batch_opts.get('western_protect_manual', True)
                    skip_existing_image = batch_opts.get('western_skip_existing', True)
                    succ, msg, p_data = self.refresh_person_from_site(
                        p_id,
                        domain='WESTERN',
                        protect_manual=protect_manual,
                        skip_existing_image=skip_existing_image,
                        use_dir_cache=True,
                        site_activity=site_activity
                    )

                    if succ:
                        job_status['success'] += 1
                        log_line = f"[{idx + 1}/{total_len}] [WESTERN] '{p_name}': 프로필 갱신 완료 ({msg})"
                        self._broadcast_batch_log(log_line, is_updated=True, batch_type=batch_key)
                    else:
                        job_status['fail'] += 1
                        log_line = f"[{idx + 1}/{total_len}] [WESTERN] '{p_name}': 갱신 실패 ({msg})"
                        self._broadcast_batch_log(log_line, event_type="warning", is_updated=False, batch_type=batch_key)

                    if idx + 1 < total_len and not job_status.get('stop_flag'):
                        MetaUtil.wait_after_batch_site_request(site_activity, effective_delay)
                    continue

                res = self.sync_person_image_to_storage(domain=p_domain, person_id=p_id,
                    skip_existing=batch_opts.get('person_skip_existing', True), use_dir_cache=True)
                if res.get('ret') != 'success':
                    job_status['fail'] += 1
                    self._broadcast_batch_log(f"[{idx + 1}/{total_len}] [{p_domain}] '{p_name}': {res.get('msg')}",
                                              event_type='warning', batch_type=batch_key)
                else:
                    updated = bool(res.get('is_updated'))
                    job_status['success' if updated else 'skipped'] += 1
                    self._broadcast_batch_log(f"[{idx + 1}/{total_len}] [{p_domain}] '{p_name}': {res.get('msg')}",
                                              is_updated=updated, batch_type=batch_key)

            else:
                # 영상 메타(item) 배치 처리 분기
                code = item['code']
                ui_code = item['ui_code']
                cat = item['category']
                job_status['current_code'] = ui_code

                if cat not in DOMAIN_MAP and cat not in ('ALL', 'AV_ALL'):
                    invalid_cat = cat
                    resolved_categories = []
                    for candidate_cat in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN']:
                        candidate_sess, _, candidate_std_cat = self.get_session_and_domain(candidate_cat)
                        if not candidate_sess:
                            continue
                        try:
                            exists = candidate_sess.query(MetaItem.id).filter(
                                MetaItem.category == candidate_std_cat,
                                or_(MetaItem.code == code, MetaItem.ui_code == code,
                                    MetaItem.code == ui_code, MetaItem.ui_code == ui_code)
                            ).first()
                            if exists:
                                resolved_categories.append(candidate_std_cat)
                        finally:
                            candidate_sess.remove()

                    if len(resolved_categories) == 1:
                        cat = resolved_categories[0]
                        item['category'] = cat
                        self._broadcast_batch_log(
                            f"[{idx + 1}/{total_len}] [{ui_code}] 잘못된 category '{invalid_cat}'를 실제 DB category '{cat}'로 복원합니다.",
                            event_type="warning",
                            batch_type=batch_key
                        )
                    else:
                        job_status['fail'] += 1
                        reason = "대상 DB를 찾을 수 없음" if not resolved_categories else "복수 DB에 존재하여 자동 선택 불가"
                        self._broadcast_batch_log(
                            f"[{idx + 1}/{total_len}] [{ui_code}] 유효하지 않은 DB category '{cat}' ({reason})",
                            event_type="error",
                            batch_type=batch_key
                        )
                        continue

                if not self._ensure_meta_db_writable(cat):
                    job_status['fail'] += 1
                    self._broadcast_batch_log(
                        f"[{idx + 1}/{total_len}] [{cat}] '{ui_code}': 메타 DB 마이그레이션 완료 전이라 건너뜁니다.",
                        event_type="warning",
                        batch_type=batch_key
                    )
                    continue

                mod_name = 'western' if cat == 'WESTERN' else ('jav_uncensored' if cat == 'JAV_UNCEN' else 'jav_censored')
                target_mod = P.get_module(mod_name)

                if not target_mod:
                    job_status['fail'] += 1
                    self._broadcast_batch_log(f"[{idx + 1}/{total_len}] [{ui_code}] {mod_name} 모듈 탐색 실패", event_type="error", batch_type=batch_key)
                    continue

                try:
                    res = None

                    if refresh_mode == 'local_sync':
                        target_dom = 'WESTERN' if cat == 'WESTERN' else 'JAV'
                        idx_engine = memory_person_indices.get(target_dom)
                        res = MetaUtil.sync_local_data(target_mod, code, cat, memory_index=idx_engine)

                    elif refresh_mode in ('media_sync', 'media_reprocess'):
                        res = MetaUtil.refresh_image_only(
                            target_mod, code, cat, site_activity=site_activity,
                            storage_action='replace' if refresh_mode == 'media_reprocess' else 'reuse'
                        )
                        if res and res.get('media_warning'):
                            res['detail_msg'] = '일부 미디어 실패: ' + str(res['media_warning'])

                    elif refresh_mode == 'missing':
                        res = MetaUtil.heal_metadata(target_mod, code, cat, site_activity=site_activity)

                        if batch_opts.get('missing_include_media') and MetaPreviewUtil.is_trailer_download_enabled(cat):
                            cached_item = self.get_metadata(code, category=cat) or {}
                            if not (cached_item.get('extra_info') or {}).get('trailer_video'):
                                raw_tr = None
                                for ex in (cached_item.get('original', {}).get('extras') or []):
                                    if isinstance(ex, dict) and ex.get('content_type') == 'trailer' and ex.get('content_url'):
                                        raw_tr = ex['content_url']
                                        break
                                if raw_tr:
                                    MetaPreviewUtil.process_trailer_download_workflow(code, raw_tr, category=cat, force=False, use_dir_cache=True)

                    elif refresh_mode == 'in_place':
                        skip_media = batch_opts.get('in_place_skip_media', False)
                        if skip_media:
                            res = MetaUtil.heal_metadata(target_mod, code, cat, site_activity=site_activity)
                        else:
                            res = MetaUtil.refresh_in_place(target_mod, code, cat, site_activity=site_activity)

                    elif refresh_mode == 'auto_search':
                        cached_item = self.get_metadata(code, category=cat) or {}
                        under_95_only = batch_opts.get('auto_search_under_95', True)
                        current_rating = cached_item.get('rating') or 0

                        if under_95_only and cached_item.get('site') in ['dmm', 'mgstage'] and current_rating >= 4.5:
                            res = {'ret': 'success', 'is_updated': False, 'title_log': ui_code, 'detail_msg': '고득점 유지'}
                        else:
                            res = MetaUtil.refresh_auto_search(target_mod, code, cat, site_activity=site_activity)

                    if res:
                        title_display = res.get('title_log') or ui_code
                        detail_str = res.get('detail_msg', '')
                        is_updated = res.get('is_updated', False)

                        if res.get('ret') == 'success':
                            if is_updated:
                                job_status['success'] += 1
                                detail_suffix = f" ({detail_str})" if detail_str else ""
                                log_line = f"[{idx + 1}/{total_len}] [{cat}] '{title_display}': 처리 완료{detail_suffix}"
                                self._broadcast_batch_log(log_line, is_updated=True, batch_type=batch_key)
                            else:
                                job_status['skipped'] += 1
                                log_line = f"[{idx + 1}/{total_len}] [{cat}] '{title_display}': 변경 없음"
                                self._broadcast_batch_log(log_line, is_updated=False, batch_type=batch_key)
                        else:
                            job_status['fail'] += 1
                            log_line = f"[{idx + 1}/{total_len}] [{cat}] '{title_display}': 실패 ({res.get('msg')})"
                            self._broadcast_batch_log(log_line, event_type="warning", is_updated=False, batch_type=batch_key)

                except Exception as e_proc:
                    job_status['fail'] += 1
                    self._broadcast_batch_log(f"[{idx + 1}/{total_len}] [{ui_code}] 오류: {str(e_proc)}", event_type="error", batch_type=batch_key)

            if idx + 1 < total_len and not job_status.get('stop_flag'):
                MetaUtil.wait_after_batch_site_request(site_activity, effective_delay)

            if (idx + 1) % 25 == 0 or effective_delay >= 1.0:
                job_status['resume_index'] = idx + 1
                self._save_persistent_batch_state(batch_type=batch_key)

        elapsed = time.time() - t_start
        job_status.update({
            'is_running': False,
            'can_resume': False,
            'resume_index': 0,
            'target_items': [],
            'status': '완료',
            'elapsed_str': self._format_seconds(elapsed),
            'eta_str': '00:00'
        })
        self._broadcast_batch_log(f"전체 배치 작업 완료 ➔ 성공: {job_status['success']}건, 실패: {job_status['fail']}건, 건너뜀: {job_status['skipped']}건 (소요시간: {job_status['elapsed_str']})", batch_type=batch_key)
        self._save_persistent_batch_state(batch_type=batch_key)
        MetaPreviewUtil.clear_dir_cache()


    @classmethod
    def checkpoint_wal(cls):
        """One best-effort WAL truncation at bulk-job boundaries; never wait for readers."""
        if cls._is_postgres:
            return True
        from urllib.parse import quote
        success = True
        # Do not initialize engines or run their connect hooks for maintenance.
        # Those hooks set a 30s busy timeout before we could lower it.
        for domain, engine in list(cls._engines.items()):
            database = engine.url.database
            if not database or database == ':memory:':
                continue
            try:
                uri = 'file:' + quote(os.path.abspath(database), safe='/') + '?mode=rw'
                connection = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
                try:
                    busy, pages, completed = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                finally:
                    connection.close()
                if busy:
                    success = False
                    logger.debug('[MetaDB WAL] %s: 사용 중인 연결이 있어 TRUNCATE 생략 (pages=%s, checkpointed=%s)',
                                 domain, pages, completed)
                else:
                    logger.debug('[MetaDB WAL] %s: TRUNCATE 완료', domain)
            except sqlite3.Error as exc:
                success = False
                logger.warning('[MetaDB WAL] %s: 체크포인트 생략 (%s)', domain, exc)
        return success

    @classmethod
    def vacuum_db(cls):
        cls.ensure_db_ready()
        t_start = time.time()
        engine_label = "PostgreSQL VACUUM ANALYZE" if cls._is_postgres else "SQLite VACUUM & WAL TRUNCATE"
        logger.info(f"[MetaDB Vacuum] 데이터베이스 최적화 시작 ➔ {engine_label}")

        try:
            if cls._is_postgres:
                vacuum_tables = (
                    'meta_item',
                    'meta_person',
                    'meta_tag',
                    'meta_media',
                    'meta_fingerprint',
                    'meta_item_person_map',
                    'meta_item_tag_map',
                )
                with cls._engines['postgres'].connect().execution_options(
                    isolation_level='AUTOCOMMIT'
                ) as conn:
                    for table_name in vacuum_tables:
                        conn.execute(text(f'VACUUM ANALYZE {table_name}'))
            else:
                for dom, eng in cls._engines.items():
                    with eng.connect().execution_options(
                        isolation_level='AUTOCOMMIT'
                    ) as conn:
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

            if cat not in DOMAIN_MAP and cat not in ('ALL', 'AV_ALL'):
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

        if not all_targets:
            return False, '가져올 수 있는 DB 또는 JSON 파일이 없습니다.'

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
                    if 'meta_item' in tables and t_cat != 'PERSON':
                        if t_cat == 'ALL':
                            c.execute('SELECT count(*) FROM meta_item')
                        elif t_cat == 'AV_ALL':
                            c.execute("SELECT count(*) FROM meta_item WHERE category IN ('JAV_CEN','JAV_UNCEN','WESTERN')")
                        else:
                            c.execute('SELECT count(*) FROM meta_item WHERE category=?', (t_cat,))
                        total_units += c.fetchone()[0]
                    if 'meta_person' in tables:
                        c.execute('SELECT count(*) FROM meta_person')
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
                    from .util_migration import MetadataCopier
                    source_engine, source_session, copier = None, None, None
                    target_sessions = {}
                    try:
                        source_engine = create_engine(f'sqlite:///{t_val}', poolclass=NullPool)
                        cls._validate_copy_source(source_engine)
                        source_session = sessionmaker(bind=source_engine)()
                        def destination(category):
                            session, _, _ = cls.get_session_and_domain(category)
                            if session is None or not cls._ensure_meta_db_writable(category):
                                raise ValueError(f'대상 DB에 쓸 수 없습니다: {category}')
                            target_sessions[id(session)] = session
                            return session
                        person_target = destination('PERSON')
                        categories = None if t_cat == 'ALL' else ['JAV_CEN', 'JAV_UNCEN', 'WESTERN'] if t_cat == 'AV_ALL' else [] if t_cat == 'PERSON' else [t_cat]
                        local_status = {'stop_flag': False}
                        def report(message):
                            if progress_status is not None:
                                local_status['stop_flag'] = progress_status.get('stop_flag', False)
                                progress_status.update(current=current_idx + local_status.get('current', 0),
                                    total=total_units, current_code=local_status.get('current_code', ''),
                                    inserted=insert_count + local_status.get('inserted', 0),
                                    updated=update_count + local_status.get('updated', 0),
                                    skipped=skip_count + local_status.get('skipped', 0),
                                    fail=fail_count + local_status.get('fail', 0))
                            logger.info(f'[MetaDB Import] {message}')
                        copier = MetadataCopier([source_session], destination, person_target,
                            mode='missing' if mode == 'missing' else 'merge', status=local_status,
                            categories=categories, log=report,
                            should_stop=lambda: bool(progress_status and progress_status.get('stop_flag')))
                        if not copier.run() and not local_status.get('stop_flag'):
                            logger.warning('[MetaDB Import] 일부 데이터 가져오기에 실패했습니다.')
                        current_idx += local_status.get('current', 0)
                        insert_count += local_status.get('inserted', 0)
                        update_count += local_status.get('updated', 0)
                        skip_count += local_status.get('skipped', 0)
                        fail_count += local_status.get('fail', 0)
                        logger.info('[MetaDB Import] DB 파일 처리 완료')
                    finally:
                        if copier:
                            copier.close()
                        if source_session:
                            source_session.close()
                        if source_engine:
                            source_engine.dispose()
                        for target in target_sessions.values():
                            target.remove()

                elif t_type == 'json_dir':
                    if t_cat in ('ALL', 'AV_ALL', 'PERSON'):
                        raise ValueError('ALL/AV_ALL/PERSON 가져오기는 DB 백업 파일을 지정하세요.')
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
            return not fail_count and not (progress_status and progress_status.get('stop_flag')), final_msg
        except Exception as e:
            logger.error(f"[MetaDB Import] 오류: {e}")
            return False, str(e)

    @classmethod
    def export_database(cls, category_mode="all", target_category=None, is_sanitized=False):
        """Export physical rows, including inactive storage assets and standalone people."""
        from .util_migration import MetadataCopier
        import tempfile
        cls.ensure_db_ready()
        sources, engine, target, copier = [], None, None, None
        filepath, completed = None, False
        try:
            target_dir = P.ModelSetting.get('meta_db_sqlite_dir') or os.path.join(path_data, 'db', 'meta_db')
            os.makedirs(target_dir, exist_ok=True)
            suffix = 'clean' if is_sanitized else 'full'
            handle, filepath = tempfile.mkstemp(prefix=f'metadata_{datetime.now():%Y%m%d_%H%M%S}_', suffix=f'_{suffix}.db', dir=target_dir)
            os.close(handle)
            engine = create_engine(f'sqlite:///{filepath}', poolclass=NullPool,
                                   json_serializer=lambda value: json.dumps(value, ensure_ascii=False))
            Base.metadata.create_all(bind=engine)
            target = sessionmaker(bind=engine)()
            categories = None if category_mode == 'all' else [target_category or 'JAV_CEN']
            selected = list(DOMAIN_MAP) if categories is None else categories + ['PERSON']
            seen = set()
            for category in selected:
                session, _, _ = cls.get_session_and_domain(category)
                if session is None or id(session.get_bind()) in seen:
                    continue
                seen.add(id(session.get_bind()))
                cls._validate_copy_source(session.get_bind())
                sources.append(session)
            if not sources:
                raise ValueError('내보낼 소스 DB가 없습니다.')
            status = {}
            copier = MetadataCopier(sources, lambda category: target, target, mode='clean',
                status=status, categories=categories, sanitized=is_sanitized,
                log=lambda message: logger.info(f'[MetaDB Export] {message}'))
            if not copier.run():
                raise ValueError(f"전체 백업 생성 실패: {status['fail']}건")
            cls._stamp_copy_targets([engine])
            count = target.query(MetaItem.id).count()
            completed = True
            return True, filepath, count
        except Exception as exc:
            logger.error(f'[MetaDB Export] {exc}')
            return False, str(exc), 0
        finally:
            if copier:
                copier.close()
            if target:
                target.close()
            if engine:
                engine.dispose()
            for source in sources:
                source.remove()
            if filepath and not completed and os.path.exists(filepath):
                os.remove(filepath)


    @classmethod
    def get_transfer_engine_version(cls, engine_type):
        """복제 대상 엔진(sqlite / postgres)의 스키마 버전을 안전하게 조회."""
        cls.ensure_db_ready()
        eng = None
        should_dispose = False
        try:
            if engine_type == "postgres":
                eng = cls._engines.get('postgres')
                if not eng:
                    pg_user = P.ModelSetting.get("meta_db_pg_user") or "metadata"
                    pg_pass = P.ModelSetting.get("meta_db_pg_pass") or ""
                    pg_name = P.ModelSetting.get("meta_db_pg_name") or "metadata"
                    pg_conn_type = P.ModelSetting.get("meta_db_pg_conn_type") or "tcp"
                    if pg_conn_type == "socket":
                        socket_dir = (P.ModelSetting.get("meta_db_pg_socket_dir") or "/var/run/postgresql").strip()
                        eng = create_engine(f"postgresql+psycopg2://{pg_user}:{pg_pass}@/{pg_name}?client_encoding=utf8", poolclass=NullPool, connect_args={"host": socket_dir})
                    else:
                        pg_host = P.ModelSetting.get("meta_db_pg_host") or "postgres"
                        pg_port = P.ModelSetting.get("meta_db_pg_port") or "5432"
                        eng = create_engine(f"postgresql+psycopg2://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_name}?client_encoding=utf8", poolclass=NullPool)
                    should_dispose = True

                if eng:
                    with eng.connect() as conn:
                        exists = conn.execute(text(
                            "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='meta_schema_version'"
                        )).fetchone()
                        if exists:
                            row = conn.execute(text("SELECT schema_version, migration_state FROM meta_schema_version WHERE schema_name = 'meta_db'")).fetchone()
                            if row and row[0] is not None and row[1] == 'complete':
                                return int(row[0])
            else:
                db_dir = P.ModelSetting.get("meta_db_sqlite_dir") or os.path.join(path_data, 'db', 'meta_db')
                versions = []
                for check_file in sorted(set(value[1] for value in DOMAIN_MAP.values())):
                    full_p = os.path.join(db_dir, check_file)
                    if not os.path.exists(full_p):
                        continue
                    temp_eng = create_engine(f"sqlite:///{full_p}", poolclass=NullPool)
                    try:
                        with temp_eng.connect() as conn:
                            exists = conn.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta_schema_version'")).first()
                            if not exists:
                                versions.append(0)
                                continue
                            row = conn.execute(text("SELECT schema_version, migration_state FROM meta_schema_version WHERE schema_name='meta_db'")).first()
                            versions.append(int(row[0]) if row and row[1] == 'complete' else 0)
                    finally:
                        temp_eng.dispose()
                return versions[0] if versions and len(set(versions)) == 1 else 0
        except Exception as e:
            logger.debug(f"[MetaDB Transfer] 스키마 버전 확인 중 예외 ({engine_type}): {e}")
        finally:
            if should_dispose and eng:
                try: eng.dispose()
                except Exception: pass
        return 0

    @classmethod
    def _validate_copy_source(cls, engine):
        from sqlalchemy import inspect
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if 'meta_schema_version' not in tables:
            raise ValueError('소스 DB의 스키마 버전이 없습니다. 마이그레이션 후 진행하세요.')
        with engine.connect() as connection:
            version = connection.execute(text("SELECT schema_version, migration_state FROM meta_schema_version WHERE schema_name='meta_db'")).first()
        if not version or version[0] != cls.META_DB_SCHEMA_VERSION or version[1] != 'complete':
            raise ValueError('소스 DB가 현재 스키마의 완료 상태가 아닙니다.')
        for model in (MetaItem, MetaPerson, MetaTag, MetaMedia, MetaFingerprint, MetaItemPersonMap, MetaItemTagMap):
            table = model.__table__
            columns = {c['name'] for c in inspector.get_columns(table.name)} if table.name in tables else set()
            missing = set(table.columns.keys()) - columns
            if missing:
                raise ValueError(f"소스/대상 스키마 준비가 필요합니다: {table.name}: {', '.join(sorted(missing))}")

    @classmethod
    def _stamp_copy_targets(cls, engines):
        for engine in engines:
            with engine.begin() as connection:
                table = MetaSchemaVersion.__table__
                row = connection.execute(table.select().where(table.c.schema_name == cls.META_DB_SCHEMA_NAME)).first()
                values = dict(schema_version=cls.META_DB_SCHEMA_VERSION, migration_state='complete',
                    completed_at=datetime.now(), updated_at=datetime.now())
                if row:
                    connection.execute(table.update().where(table.c.schema_name == cls.META_DB_SCHEMA_NAME).values(**values))
                else:
                    connection.execute(table.insert().values(schema_name=cls.META_DB_SCHEMA_NAME, **values))

    @classmethod
    def check_transfer_version_compatibility(cls, src_type, tgt_type, mode='merge'):
        source = cls.get_transfer_engine_version(src_type)
        if source != cls.META_DB_SCHEMA_VERSION:
            return False, f"소스 DB(v{source})는 현재 스키마 v{cls.META_DB_SCHEMA_VERSION}로 마이그레이션을 완료해야 합니다."
        if mode != 'clean':
            target = cls.get_transfer_engine_version(tgt_type)
            if target != cls.META_DB_SCHEMA_VERSION:
                return False, f"대상 DB(v{target})는 현재 스키마로 준비하거나 clean 정책을 사용해야 합니다."
        return True, ''


    @classmethod
    def transfer_database(cls, source_type, target_type, mode="merge", progress_status=None):
        cls.ensure_db_ready()
        t_start = time.time()
        logger.info(f"[MetaDB Transfer] DB 데이터 복제 시작: {source_type.upper()} ➔ {target_type.upper()} (정책: {mode})")

        if source_type == target_type or mode not in ('clean', 'merge', 'missing'):
            return False, '유효하지 않은 복제 방향 또는 정책입니다.'
        allowed, msg = cls.check_transfer_version_compatibility(source_type, target_type, mode)
        if not allowed:
            return False, msg

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

            for source in src_sessions:
                cls._validate_copy_source(source.get_bind())

            # 목적지 엔진 준비 및 clean 모드 시 타겟 테이블 완전 DROP 처리
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

                if mode == "clean":
                    logger.info("[MetaDB Transfer] 타겟 PostgreSQL 데이터베이스 전체 테이블 삭제(DROP) 시작...")
                    with pg_tgt_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                        tables = conn.execute(text(
                            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'"
                        )).fetchall()
                        for (t_name,) in tables:
                            conn.execute(text(f'DROP TABLE IF EXISTS "{t_name}" CASCADE;'))

                if mode != 'clean':
                    cls._validate_copy_source(pg_tgt_engine)
                Base.metadata.create_all(bind=pg_tgt_engine)

                if mode == "clean":
                    with pg_tgt_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                        conn.execute(text(
                            "INSERT INTO meta_schema_version (schema_name, schema_version, migration_state, completed_at, updated_at) "
                            "VALUES ('meta_db', :ver, 'copying', :now, :now) "
                            "ON CONFLICT (schema_name) DO UPDATE SET schema_version = :ver, migration_state = 'copying', completed_at = :now, updated_at = :now"
                        ), {'ver': cls.META_DB_SCHEMA_VERSION, 'now': datetime.now()})

                pg_tgt_session = sessionmaker(bind=pg_tgt_engine)()
            else:
                db_dir = P.ModelSetting.get("meta_db_sqlite_dir") or os.path.join(path_data, 'db', 'meta_db')
                os.makedirs(db_dir, exist_ok=True)
                for dom in set(v[0] for v in DOMAIN_MAP.values()):
                    db_filename = next(v[1] for v in DOMAIN_MAP.values() if v[0] == dom)
                    full_target_db_path = os.path.join(db_dir, db_filename)
                    json_dump_utf8 = lambda obj: json.dumps(obj, ensure_ascii=False)
                    eng = create_engine(f"sqlite:///{full_target_db_path}", poolclass=NullPool, json_serializer=json_dump_utf8)

                    if mode == "clean":
                        logger.info(f"[MetaDB Transfer] 타겟 SQLite ({dom}) 전체 테이블 삭제(DROP) 시작...")
                        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                            conn.execute(text("PRAGMA foreign_keys = OFF;"))
                            tables = conn.execute(text(
                                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';"
                            )).fetchall()
                            for (t_name,) in tables:
                                conn.execute(text(f'DROP TABLE IF EXISTS "{t_name}";'))

                    if mode != 'clean' and os.path.exists(full_target_db_path) and os.path.getsize(full_target_db_path):
                        cls._validate_copy_source(eng)
                    Base.metadata.create_all(bind=eng)

                    if mode == "clean":
                        with eng.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                            conn.execute(text(
                                "INSERT OR REPLACE INTO meta_schema_version (schema_name, schema_version, migration_state, completed_at, updated_at) "
                                "VALUES ('meta_db', :ver, 'copying', :now, :now)"
                            ), {'ver': cls.META_DB_SCHEMA_VERSION, 'now': datetime.now()})

                    tgt_engines[dom] = eng
                    tgt_sessions[dom] = sessionmaker(bind=eng)()

            from .util_migration import MetadataCopier
            status = progress_status if progress_status is not None else {}
            def target_for_category(category):
                if target_type == 'postgres':
                    return pg_tgt_session
                domain = DOMAIN_MAP.get(category, DOMAIN_MAP['JAV_CEN'])[0]
                return tgt_sessions.get(domain)
            person_target = pg_tgt_session if target_type == 'postgres' else tgt_sessions['person']
            copier = MetadataCopier(src_sessions, target_for_category, person_target,
                mode=mode, status=status, log=lambda message: logger.info(f'[MetaDB Transfer] {message}'))
            try:
                copied = copier.run()
            finally:
                copier.close()
            if not copied:
                return False, f"복제 중단 또는 일부 항목 실패 (실패: {status['fail']}, 제외: {status['skipped']})"
            cls._stamp_copy_targets([pg_tgt_engine] if target_type == 'postgres' else list(tgt_engines.values()))
            total_count = status['total']
            inserted_count, updated_count = status['inserted'], status['updated']
            skipped_count, fail_count = status['skipped'], status['fail']

            # 4. PostgreSQL 대상 복제 시 자동 증가 시퀀스(Serial Sequence) 최대 ID 값으로 자동 재정렬
            if target_type == "postgres" and pg_tgt_engine:
                logger.debug("[MetaDB Transfer] PostgreSQL 자동 증가 시퀀스 정렬 실행...")
                try:
                    with pg_tgt_engine.connect() as conn:
                        seq_tables = ['meta_item', 'meta_person', 'meta_tag', 'meta_media', 'meta_fingerprint', 'meta_item_person_map', 'meta_item_tag_map']
                        for tbl in seq_tables:
                            try:
                                conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{tbl}', 'id'), COALESCE((SELECT MAX(id) FROM {tbl}), 1));"))
                            except Exception:
                                pass
                        conn.commit()
                except Exception as e_seq:
                    logger.debug(f"[MetaDB Transfer] 시퀀스 정렬 예외: {e_seq}")

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
            self._load_persistent_batch_state()
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
                    self._start_maintenance_job('actor_sync')
                elif latest_ver > last_synced:
                    logger.info(f"[MetaDB] 시작 시 새 배포 DB 파일 감지 ({last_synced} ➔ {latest_ver}). 자동 동기화를 진행합니다...")
                    self._start_maintenance_job('actor_sync')

            # logger.debug(f"[{self.name}] Universal Metadata DB Infrastructure Loaded.")
        except Exception as e:
            logger.error(f"[{self.name}] plugin_load 에러: {e}")

    def plugin_load_celery(self):
        """Celery 워커 프로세스 구동 시 DB 엔진 초기화"""
        self.init_engines()
        self._load_persistent_batch_state()

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

            if req_command == 'db_batch_refresh_status' or command == 'db_batch_refresh_status':
                return self.process_command('db_batch_refresh_status', req.form.get('batch_type') or 'item', '', '', req)

            if normalized_cat == 'PERSON':
                is_list_query = (req_command in ['web_list', 'list', 'person_web_list'] or not req_command)
                if is_list_query:
                    default_domain = req.form.get('search_domain', 'ALL') or 'ALL'
                    return jsonify(self.person_web_list(req, default_domain=default_domain))

                if req_command in ['person_search', 'person_get_detailed', 'person_get_works', 'person_verify_works', 'person_save', 'person_delete', 'person_clear', 'person_sync_jav_actors', 'person_version_status', 'person_sub_set_master', 'person_sub_split', 'db_vacuum']:
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
            if command == 'db_delete_preview':
                return jsonify({'ret':'success', 'data':self.get_delete_plan(arg1)})
            elif command == 'db_delete_confirm':
                return jsonify(self.confirm_delete_plan(arg1, arg2, arg3))
            elif command == 'person_search':
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

            elif command == 'person_verify_works':
                person_id = arg1 or ''
                domain = arg2 or 'JAV'
                success, msg, verified_works = self.verify_and_sync_person_works(person_id, domain=domain)
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg, 'works_detailed': verified_works})

            elif command == 'person_refresh_from_site':
                person_id = arg1 or ''
                domain = arg2 or 'WESTERN'
                success, msg, data = self.refresh_person_from_site(person_id, domain=domain)
                return jsonify({
                    'ret': 'success' if success else 'warning',
                    'msg': msg,
                    'data': data
                })

            elif command == 'person_save':
                p_data = json.loads(arg1) if arg1 else {}
                success, msg = self.person_save(p_data)
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == 'person_reset_user_data':
                reset_data = json.loads(arg2) if arg2 else {}
                reset_type = reset_data.get('reset_type') if isinstance(reset_data, dict) else arg2
                success, msg = self.person_reset_user_data(arg1, reset_type)
                return jsonify({'ret': 'success' if success else 'error', 'msg': msg})

            elif command == 'person_crop_save':
                allowed, guard_msg = self._schema_write_guard('PERSON', '사용자 인물 이미지 변경')
                if not allowed:
                    return jsonify({'ret': 'error', 'msg': guard_msg})
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

                success, result_msg, updated_person_obj = MetaUtil.save_user_cropped_person_image(
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
                    p_media = self.normalize_person_media(p_media, domain=updated_person_obj.domain)
                    raw_local = str(p_media['official'].get('local_img_path') or p_media['custom'].get('local_img_path') or '').strip()
                    if str(updated_person_obj.domain or '').upper() in ('JAV', 'WESTERN'):
                        extra_data.pop('local_img_url', None)
                    elif raw_local:
                        resolved_url = self.format_actor_thumb_url(raw_local, domain=updated_person_obj.domain)
                        p_media['official']['local_img_url'] = resolved_url
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

            elif command in ["crop_save", "db_crop_save"]:
                code = arg1
                crop_data = arg2
                upload_payload = arg3
                pl_base64, p_base64 = None, None

                if upload_payload:
                    try:
                        p_json = json.loads(upload_payload)
                        if isinstance(p_json, dict):
                            if p_json.get('type') == 'p': p_base64 = p_json.get('data')
                            elif p_json.get('type') == 'pl': pl_base64 = p_json.get('data')
                    except Exception:
                        pl_base64 = upload_payload

                target_cat = self.find_item_category(code, hint_category=req.form.get('category'))
                success, result_msg = MetaUtil.save_user_cropped_poster(
                    code, crop_data, pl_image_base64_data=pl_base64, p_image_base64_data=p_base64, category=target_cat
                )
                return jsonify({'ret': 'success' if success else 'error', 'msg': result_msg, 'new_url': result_msg if success else None})

            elif command == "crop_url_download":
                target_url = (arg1 or '').strip()
                code = (arg2 or '').strip()
                target_cat = (arg3 or getattr(self, 'category', 'JAV_CEN')).strip().upper()
                source_type = req.form.get('source_type') or 'pl'

                if not target_url:
                    return jsonify({'ret': 'error', 'msg': '다운로드할 이미지 URL이 없습니다.'})

                from support_site import SiteAvBase
                im = SiteAvBase.imopen(target_url)
                if im is None:
                    return jsonify({'ret': 'error', 'msg': '원격 서버에서 이미지를 다운로드하지 못했습니다.'})

                tmp_dir = os.path.join(path_data, 'tmp')
                os.makedirs(tmp_dir, exist_ok=True)
                clean_code = re.sub(r'[^\w-]', '_', code or 'temp')
                temp_filename = f"temp_crop_{int(time.time())}_{clean_code}_{source_type}.jpg"
                temp_filepath = os.path.join(tmp_dir, temp_filename)

                try:
                    MetaUtil.save_normalized_jpeg(im, temp_filepath)
                    im.close()
                except Exception as e_save_tmp:
                    return jsonify({'ret': 'error', 'msg': f'임시 파일 저장 실패: {e_save_tmp}'})

                ddns_host = F.SystemModelSetting.get('ddns') or ''
                is_uncen = (target_cat == 'JAV_UNCEN')
                route_path = 'jav_image_un' if is_uncen else 'jav_image'
                display_url = f"{ddns_host}/metadata/normal/{route_path}?site=system&path={quote(temp_filepath)}"

                logger.info(f"[MetaDB] 크롭용 URL 이미지 임시 다운로드 완료: {temp_filepath}")
                return jsonify({
                    'ret': 'success',
                    'filepath': temp_filepath,
                    'display_url': display_url,
                    'url': target_url
                })

            elif command == "crop_direct_save":
                code = arg1
                source_val = arg2
                target_type = arg3 or 'pl'
                target_cat = self.find_item_category(code, hint_category=req.form.get('category'))
                image_base64_data = req.form.get('data') or ''

                success, result_msg = MetaUtil.save_direct_user_image(
                    code,
                    source_val,
                    image_type=target_type,
                    category=target_cat,
                    image_base64_data=image_base64_data
                )
                return jsonify({'ret': 'success' if success else 'error', 'msg': result_msg, 'new_url': result_msg if success else None})

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
                    sub_list = copy.deepcopy(p_rec.merged_details or [])
                    target_sub = next((s for s in sub_list if s.get('actor_id') == target_sub_id), None)

                    if not target_sub:
                        target_sub = {
                            'actor_id': target_sub_id,
                            'name_org': p_rec.name_org,
                            'name_ko': p_rec.name_ko,
                            'name_en': p_rec.name_en,
                            'site_img_url': p_rec.site_img_url or '',
                            'local_img_path': self.normalize_person_media(
                                p_rec.media_src, domain=p_rec.domain
                            )['official'].get('local_img_path', ''),
                            'google_fileid': self.normalize_person_media(
                                p_rec.media_src, domain=p_rec.domain
                            )['official'].get('google_fileid', '')
                        }

                    # 주 레코드 컬럼 및 미디어 스왑
                    p_rec.person_idx = target_sub_id
                    if target_sub.get('name_org'): p_rec.name_org = target_sub['name_org']
                    if target_sub.get('name_ko'): p_rec.name_ko = target_sub['name_ko']
                    if target_sub.get('name_en'): p_rec.name_en = target_sub['name_en']

                    sub_birth = str(target_sub.get('birth') or '').strip()
                    if sub_birth:
                        p_rec.birth_date = sub_birth
                        m_by = re.search(r'\b(19\d{2}|20\d{2})\b', sub_birth)
                        p_rec.birth_year = int(m_by.group(1)) if m_by else None

                    sub_h = self._parse_integer(target_sub.get('height'))
                    if sub_h:
                        p_rec.height = sub_h

                    sub_body = str(target_sub.get('body_size') or '').strip()
                    if sub_body:
                        p_rec.body_size = sub_body
                        b_val, w_val, h_val, c_val = self._parse_body_size(sub_body)
                        p_rec.bust = b_val or p_rec.bust
                        p_rec.waist = w_val or p_rec.waist
                        p_rec.hip = h_val or p_rec.hip
                        if c_val:
                            p_rec.cup_size = c_val

                    raw_bra = str(target_sub.get('bra_size') or target_sub.get('cup_size') or '').strip().upper()
                    clean_bra = re.sub(r'[^A-Z]', '', raw_bra)
                    if clean_bra:
                        p_rec.cup_size = clean_bra

                    if target_sub.get('debut'): p_rec.debut_date = str(target_sub['debut']).strip()
                    if target_sub.get('agency'): p_rec.agency = str(target_sub['agency']).strip()
                    if target_sub.get('blood'): p_rec.blood_type = str(target_sub['blood']).strip().upper().replace('형', '')
                    if target_sub.get('info_url'): p_rec.info_url = str(target_sub['info_url']).strip()

                    # 미디어 물리 컬럼 스왑
                    if target_sub.get('local_img_path'): p_rec.local_path = target_sub['local_img_path']
                    if target_sub.get('google_fileid'): p_rec.gdrive_fileid = target_sub['google_fileid']
                    if target_sub.get('site_img_url'): p_rec.site_img_url = target_sub['site_img_url']

                    media_dict = self.normalize_person_media(p_rec.media_src, domain=p_rec.domain)
                    if target_sub.get('local_img_path'): media_dict['official']['local_img_path'] = target_sub['local_img_path']
                    if target_sub.get('google_fileid'): media_dict['official']['google_fileid'] = target_sub['google_fileid']
                    if target_sub.get('site_img_url'): media_dict['official']['site_img_url'] = target_sub['site_img_url']
                    p_rec.media_src = media_dict

                    final_thumb = self.resolve_person_active_thumb(p_rec)
                    p_rec.thumb = final_thumb
                    p_rec.thumb_url = final_thumb
                    p_rec.has_photo = bool(final_thumb and str(final_thumb).strip())

                    source_data = copy.deepcopy(p_rec.source_data or {})
                    for spec_key in self.PERSON_SOURCE_FIELDS:
                        if target_sub.get(spec_key) is not None:
                            source_data[spec_key] = target_sub[spec_key]
                    p_rec.source_data = source_data
                    self.rebuild_effective_person_fields(p_rec)
                    extra_dict = copy.deepcopy(p_rec.extra_info or {})

                    alt_list = [x for x in (p_rec.alt_indices or []) if x != target_sub_id]
                    alt_list.insert(0, target_sub_id)
                    if old_idx and old_idx not in alt_list:
                        alt_list.append(old_idx)
                    p_rec.alt_indices = alt_list
                    extra_dict.pop('alt_actor_indices', None)
                    extra_dict.pop('merged_sub_actors', None)
                    p_rec.extra_info = extra_dict
                    self.sync_person_media_columns(p_rec)
                    sess.commit()

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
                    sub_list = copy.deepcopy(p_rec.merged_details or [])
                    target_sub = next((s for s in sub_list if s.get('actor_id') == target_sub_id), None)
                    if not target_sub:
                        return jsonify({'ret': 'error', 'msg': f'서브 인물 {target_sub_id} 정보를 찾을 수 없습니다.'})
                    if target_sub_id == p_rec.person_idx:
                        return jsonify({'ret': 'error', 'msg': '대표 인물은 먼저 다른 인물로 대표를 변경한 후 분리하세요.'})

                    # 마스터 레코드에서 서브 인물 제외 및 재병합 방지 등록
                    p_rec.merged_details = [s for s in sub_list if s.get('actor_id') != target_sub_id]
                    alt_list = (p_rec.alt_indices or [])
                    p_rec.alt_indices = [x for x in alt_list if x != target_sub_id]

                    split_ex = set(extra_dict.get('split_exclusions', []))
                    split_ex.add(target_sub_id)
                    extra_dict['split_exclusions'] = list(split_ex)
                    p_rec.extra_info = extra_dict

                    # 마스터 레코드 병합 카운트 및 인덱스 갱신
                    p_rec.alt_indices = list(p_rec.alt_indices or [])
                    p_rec.merged_details = list(p_rec.merged_details or [])
                    p_rec.merged_count = len(p_rec.alt_indices or []) if len(p_rec.alt_indices or []) > 1 else 1

                    # 서브 인물 데이터로부터 물리 스펙 파싱
                    sub_birth = str(target_sub.get('birth') or '').strip()
                    m_by = re.search(r'\b(19\d{2}|20\d{2})\b', sub_birth)
                    sub_by = int(m_by.group(1)) if m_by else None

                    sub_h = self._parse_integer(target_sub.get('height'))
                    sub_body = str(target_sub.get('body_size') or '').strip()
                    b_val, w_val, h_val, c_val = self._parse_body_size(sub_body)

                    raw_bra = str(target_sub.get('bra_size') or target_sub.get('cup_size') or '').strip().upper()
                    clean_bra = re.sub(r'[^A-Z]', '', raw_bra)
                    sub_cup = clean_bra or c_val or None

                    sub_local = str(target_sub.get('local_img_path') or '').strip()
                    sub_google = str(target_sub.get('google_fileid') or '').strip()
                    sub_site = str(target_sub.get('site_img_url') or '').strip()
                    sub_photos = [sub_site] if sub_site else []

                    # 보존되어 있던 원본 데이터로 독립 MetaPerson 신규 생성 (물리 컬럼 완벽 초기화)
                    new_standalone = MetaPerson(
                        domain=p_rec.domain,
                        name_org=target_sub.get('name_org') or p_rec.name_org,
                        name_ko=target_sub.get('name_ko') or p_rec.name_ko,
                        name_en=target_sub.get('name_en') or p_rec.name_en,
                        person_idx=target_sub_id,
                        person_type="actor",
                        gender=p_rec.gender or "female",
                        birth_date=sub_birth,
                        birth_year=sub_by,
                        debut_date=str(target_sub.get('debut') or '').strip(),
                        height=sub_h,
                        bust=b_val,
                        waist=w_val,
                        hip=h_val,
                        body_size=sub_body,
                        cup_size=sub_cup,
                        blood_type=str(target_sub.get('blood') or '').strip().upper().replace('형', ''),
                        agency=str(target_sub.get('agency') or '').strip(),
                        country=p_rec.country or ("미국" if p_rec.domain == "WESTERN" else "일본"),
                        info_url=str(target_sub.get('info_url') or '').strip(),
                        local_path=sub_local,
                        gdrive_fileid=sub_google,
                        custom_gdrive_fileid=None,
                        custom_local_path=None,
                        site_img_url=sub_site,
                        photo_urls=sub_photos,
                        merged_count=1,
                        alt_indices=[target_sub_id],
                        merged_details=[target_sub],
                        works_count=0,
                        source_data={
                            'name_org': target_sub.get('name_org') or p_rec.name_org,
                            'name_ko': target_sub.get('name_ko') or p_rec.name_ko,
                            'name_en': target_sub.get('name_en') or p_rec.name_en,
                            'birth': sub_birth,
                            'height': sub_h,
                            'body_size': sub_body,
                            'bra_size': sub_cup,
                            'debut': str(target_sub.get('debut') or '').strip(),
                            'agency': str(target_sub.get('agency') or '').strip(),
                            'blood': str(target_sub.get('blood') or '').strip(),
                            'info_url': str(target_sub.get('info_url') or '').strip()
                        },
                        manual_data={},
                        media_src={
                            'official': {
                                'local_img_path': sub_local,
                                'google_fileid': sub_google,
                                'site_img_url': sub_site,
                                'site_img_urls': sub_photos
                            },
                            'custom': {
                                'custom_user_google_fileid': '',
                                'remote_img_path': '',
                                'local_img_path': ''
                            }
                        },
                        works={},
                        extra_info={
                            'split_exclusions': [p_rec.person_idx]
                        }
                    )
                    final_sub_thumb = self.resolve_person_active_thumb(new_standalone)
                    new_standalone.thumb = final_sub_thumb
                    new_standalone.thumb_url = final_sub_thumb
                    new_standalone.has_photo = bool(final_sub_thumb and str(final_sub_thumb).strip())

                    sess.add(new_standalone)
                    sess.commit()

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

            # 모달 폼 기반 메타데이터 직접 수정 저장
            elif command == 'db_edit_save':
                code = arg1
                raw_json_str = arg2
                if not raw_json_str:
                    return jsonify({'ret': 'error', 'msg': '수정할 데이터가 없습니다.'})
                try:
                    new_json = json.loads(raw_json_str)
                    target_cat = new_json.get('category') or self.find_item_category(code, hint_category=req.form.get('category'))
                    success = self.save_metadata(target_cat, new_json)
                    return jsonify({'ret': 'success' if success else 'error', 'msg': 'DB에 성공적으로 저장되었습니다.' if success else '저장 실패'})
                except Exception as e:
                    return jsonify({'ret': 'error', 'msg': str(e)})

            # 단일 레코드 삭제
            elif command == 'db_delete':
                code = arg1
                target_cat = self.find_item_category(code, hint_category=req.form.get('category') or arg2)
                success = self.delete_record(code, category=target_cat)
                return jsonify({'ret': 'success' if success else 'error', 'msg': '삭제되었습니다.' if success else '삭제 실패'})

            # 카테고리 DB 전체 초기화
            elif command == 'db_clear':
                raw_cat = (arg1 or req.form.get('category') or getattr(self, 'category', 'JAV_CEN')).strip().upper()
                if raw_cat == 'AV_ALL':
                    total_cleared = 0
                    all_success = True
                    for av_cat in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN']:
                        succ, count = self.clear_db(av_cat)
                        if succ:
                            total_cleared += count
                        else:
                            all_success = False
                    return jsonify({
                        'ret': 'success' if all_success else 'warning',
                        'msg': f'AV 전체 {total_cleared}건의 데이터가 초기화되었습니다.' if all_success else '일부 카테고리 초기화 실패'
                    })
                else:
                    success, count = self.clear_db(raw_cat)
                    return jsonify({
                        'ret': 'success' if success else 'error',
                        'msg': f"['{raw_cat}'] {count}건의 데이터가 초기화되었습니다." if success else '초기화 실패'
                    })

            # 통합 배치 갱신 작업 제어 명령 (item / person 분기)
            elif command == 'db_batch_refresh_start':
                post_params = {}
                if arg3 and isinstance(arg3, str) and arg3.strip().startswith('{'):
                    try:
                        post_params = json.loads(arg3)
                    except Exception:
                        pass

                refresh_mode = post_params.get('mode') or req.form.get('mode') or arg2 or 'missing'
                batch_type = post_params.get('batch_type') or req.form.get('batch_type') or ('person' if refresh_mode == 'person_image_sync' else 'item')
                batch_key = 'person' if str(batch_type).lower() == 'person' else 'item'

                current_status, _ = self._get_batch_snapshot(batch_key)
                if current_status.get('is_running'):
                    target_label = "인물" if batch_key == 'person' else "영상 메타"
                    return jsonify({'ret': 'warning', 'msg': f'이미 진행 중인 {target_label} 배치 작업이 있습니다.'})

                action = post_params.get('action') or req.form.get('action') or arg1 or 'restart'
                target_scope = post_params.get('target_scope') or req.form.get('target_scope') or 'auto'
                delay_val = float(post_params.get('delay') or req.form.get('delay') or (0.5 if batch_key == 'person' else 2.0))
                target_codes_raw = post_params.get('codes') or req.form.get('codes')
                batch_options = post_params.get('batch_options') or req.form.get('batch_options') or {}
                if isinstance(batch_options, str):
                    try:
                        batch_options = json.loads(batch_options)
                    except Exception:
                        batch_options = {}

                search_params = {
                    'search_category': post_params.get('search_category') or req.form.get('search_category') or 'AV_ALL',
                    'search_domain': post_params.get('search_domain') or req.form.get('search_domain') or 'ALL',
                    'search_site': post_params.get('search_site') or req.form.get('search_site') or 'all',
                    'search_status': post_params.get('search_status') or req.form.get('search_status') or 'all',
                    'search_word': post_params.get('search_word') or req.form.get('search_word') or '',
                    'search_order': post_params.get('search_order') or req.form.get('search_order') or 'desc'
                }

                use_celery = False
                try:
                    # FF stores this platform option in config, not SystemModelSetting.
                    use_celery = bool(celery and F.config.get('use_celery', False))
                except Exception:
                    use_celery = False

                if use_celery:
                    logger.info(f"[{self.name}] Celery 워커로 배치 작업 위임 실행 (트랙: {batch_key}, 모드: {refresh_mode})")
                    task_meta_db_batch_refresh.apply_async(
                        args=[target_scope, target_codes_raw, search_params, refresh_mode, delay_val, action],
                        kwargs={'batch_type': batch_key, 'batch_options': batch_options}
                    )
                else:
                    logger.info(f"[{self.name}] 백그라운드 스레드로 배치 작업 시작 (트랙: {batch_key}, 모드: {refresh_mode})")
                    t = threading.Thread(
                        target=self.run_batch_refresh_worker,
                        args=(target_scope, target_codes_raw, search_params, refresh_mode, delay_val, action),
                        kwargs={'batch_type': batch_key, 'batch_options': batch_options},
                        daemon=True
                    )
                    t.start()

                target_label = "인물" if batch_key == 'person' else "영상 메타"
                start_msg = f"{target_label} 중단 지점부터 작업을 재개합니다." if action == 'resume' else f"{target_label} 배치 작업을 시작했습니다."
                if use_celery:
                    start_msg += " (Celery 워커 처리)"
                return jsonify({'ret': 'success', 'msg': start_msg})

            elif command == 'db_batch_refresh_stop':
                batch_type = req.form.get('batch_type') or arg1 or 'item'
                batch_key = 'person' if str(batch_type).lower() == 'person' else 'item'
                self.batch_status[batch_key]['stop_flag'] = True
                try:
                    shared_cache = F.get_cache(f"{P.package_name}_batch_job_{batch_key}")
                    shared_cache.set('stop_flag', 'true')
                except Exception:
                    pass
                target_label = "인물" if batch_key == 'person' else "영상 메타"
                return jsonify({'ret': 'success', 'msg': f'{target_label} 배치 작업 중단을 요청했습니다.'})

            # 배치 작업 실시간 상태 및 로그 조회 명령 (트랙별 분기)
            elif command in ['db_batch_refresh_status', 'batch_status']:
                batch_type = req.form.get('batch_type') or arg1 or 'item'
                batch_key = 'person' if str(batch_type).lower() == 'person' else 'item'
                status_to_return, logs_to_return = self._get_batch_snapshot(batch_key)
                return jsonify({
                    'ret': 'success',
                    'data': status_to_return,
                    'logs': logs_to_return
                })

            elif command == 'db_batch_refresh_reset':
                batch_type = req.form.get('batch_type') or arg1 or 'item'
                batch_key = 'person' if str(batch_type).lower() == 'person' else 'item'

                default_mode = 'person_image_sync' if batch_key == 'person' else 'missing'
                default_category = 'PERSON' if batch_key == 'person' else 'AV_ALL'

                self.batch_status[batch_key].update({
                    'is_running': False,
                    'status': '대기 중',
                    'mode': default_mode,
                    'category': default_category,
                    'total': 0,
                    'current': 0,
                    'success': 0,
                    'fail': 0,
                    'skipped': 0,
                    'current_code': '',
                    'stop_flag': False,
                    'can_resume': False,
                    'resume_index': 0,
                    'target_items': []
                })
                with self._batch_log_lock:
                    self.batch_log_history[batch_key].clear()

                try:
                    target_file = self.batch_state_files[batch_key]
                    if os.path.exists(target_file):
                        os.remove(target_file)
                except Exception:
                    pass

                target_label = "인물" if batch_key == 'person' else "영상 메타"
                self._broadcast_batch_log(f"{target_label} 배치 작업 상태 및 큐가 초기화되었습니다.", batch_type=batch_key)
                return jsonify({'ret': 'success', 'msg': f'{target_label} 배치 작업 상태가 초기화되었습니다.'})

            elif command in ['info_batch', 'batch_info']:
                try:
                    options = json.loads(arg3) if arg3 else {}
                except (TypeError, ValueError):
                    return jsonify({'ret': 'error', 'error_code': 'invalid_request', 'msg': 'arg3 JSON 형식 오류'}), 400
                return self._metadata_batch_response(arg1 or 'JAV_CEN', arg2, options)

            elif command in ['db_sync_media', 'person_sync_image']:
                try:
                    options = json.loads(arg3) if arg3 else {}
                except (TypeError, ValueError):
                    return jsonify({'ret': 'error', 'error_code': 'invalid_request', 'msg': 'arg3 JSON 형식 오류'}), 400
                operation = 'media_sync' if command == 'db_sync_media' else 'person_image_sync'
                params = {'code': arg1, 'category': arg2, 'options': options} if operation == 'media_sync' else {
                    'person_idx': arg1, 'domain': arg2, 'options': options}
                return self._storage_sync_response(operation, params)

            elif command == 'db_refresh_missing_in_place':
                code = arg1
                target_cat = self.find_item_category(code, hint_category=arg2 or req.form.get('category'))
                mod_name = 'western' if target_cat == 'WESTERN' else ('jav_uncensored' if target_cat == 'JAV_UNCEN' else 'jav_censored')
                target_mod = P.get_module(mod_name)
                if not target_mod:
                    return jsonify({'ret': 'error', 'msg': f'[{mod_name}] 모듈을 찾을 수 없습니다.'})
                return jsonify(MetaUtil.heal_metadata(target_mod, code, target_cat))

            elif command == 'db_refresh_image_only':
                code = arg1
                body = req.get_json(silent=True) if req.is_json else {}
                media_path = req.values.get('media_path')
                if media_path is None and isinstance(body, dict):
                    media_path = body.get('media_path')
                target_cat = self.find_item_category(code, hint_category=arg2 or req.form.get('category'))
                mod_name = 'western' if target_cat == 'WESTERN' else ('jav_uncensored' if target_cat == 'JAV_UNCEN' else 'jav_censored')
                target_mod = P.get_module(mod_name)
                if not target_mod:
                    return jsonify({'ret': 'error', 'msg': f'[{mod_name}] 모듈을 찾을 수 없습니다.'})
                return jsonify(MetaUtil.refresh_image_only(target_mod, code, target_cat,
                                                          storage_action=arg3 or None, media_path=media_path))

            elif command == 'db_refresh_in_place':
                code = arg1
                target_cat = self.find_item_category(code, hint_category=arg2 or req.form.get('category'))
                mod_name = 'western' if target_cat == 'WESTERN' else ('jav_uncensored' if target_cat == 'JAV_UNCEN' else 'jav_censored')
                target_mod = P.get_module(mod_name)
                if not target_mod:
                    return jsonify({'ret': 'error', 'msg': f'[{mod_name}] 모듈을 찾을 수 없습니다.'})
                return jsonify(MetaUtil.refresh_in_place(target_mod, code, target_cat))

            elif command == 'db_refresh_auto_search':
                code = arg1
                target_cat = self.find_item_category(code, hint_category=arg2 or req.form.get('category'))
                mod_name = 'western' if target_cat == 'WESTERN' else ('jav_uncensored' if target_cat == 'JAV_UNCEN' else 'jav_censored')
                target_mod = P.get_module(mod_name)
                if not target_mod:
                    return jsonify({'ret': 'error', 'msg': f'[{mod_name}] 모듈을 찾을 수 없습니다.'})
                return jsonify(MetaUtil.refresh_auto_search(target_mod, code, target_cat))

            elif command == 'person_sync_jav_actors':
                success, msg = self._start_maintenance_job('actor_sync')
                return jsonify({'ret': 'success' if success else 'warning', 'msg': msg, 'data': self._get_maintenance_status()})

            elif command == 'meta_db_maintenance_status':
                return jsonify({'ret': 'success', 'data': self._get_maintenance_status()})

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

            elif command == 'meta_db_schema_status':
                statuses = {}
                for category in ['JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'PERSON', 'MOVIE', 'KTV', 'FTV']:
                    status = self.get_meta_schema_status(category)
                    statuses[status.get('physical_name') or category] = status
                return jsonify({
                    'ret': 'success',
                    'schema_name': self.META_DB_SCHEMA_NAME,
                    'target_version': self.META_DB_SCHEMA_VERSION,
                    'data': statuses,
                    'migration': self.meta_migration_status,
                    'postgres_tools': {
                        'sql_backup_available': bool(self._engines.get('postgres'))
                        if self._is_postgres else True
                    }
                })

            elif command == 'meta_db_schema_migrate':
                success, msg = self.orchestrator.start_upgrade()
                return jsonify({'ret': 'success' if success else 'warning', 'msg': msg})

            elif command == 'meta_db_schema_migrate_stop':
                success, msg = self.orchestrator.request_stop()
                return jsonify({'ret': 'success' if success else 'warning', 'msg': msg})

            elif command == 'meta_db_schema_backups':
                backup_root = os.path.join(path_data, 'db', 'meta_db_backups')
                backups = []
                allowed_files = (
                    {'meta_db_postgres.sqlite3'}
                    if self._is_postgres else
                    {filename for _, filename in DOMAIN_MAP.values()}
                )
                if os.path.isdir(backup_root):
                    for name in sorted(os.listdir(backup_root), reverse=True):
                        root = os.path.join(backup_root, name)
                        if not os.path.isdir(root):
                            continue
                        for filename in sorted(os.listdir(root)):
                            file_path = os.path.join(root, filename)
                            if filename in allowed_files and os.path.isfile(file_path):
                                backups.append({
                                    'label': f'{name} / {filename}',
                                    'path': os.path.realpath(file_path),
                                    'filename': filename
                                })
                return jsonify({'ret': 'success', 'data': backups})

            elif command == 'meta_db_schema_backup':
                success, msg = self.maintenance_orchestrator.start_backup()
                return jsonify({'ret': 'success' if success else 'warning', 'msg': msg, 'data': self._get_maintenance_status()})

            elif command == 'meta_db_schema_restore':
                success, msg = self.restore_meta_db(arg1, arg2)
                return jsonify({'ret': 'success' if success else 'warning', 'msg': msg, 'data': self._get_maintenance_status()})

            elif command == 'db_transfer_status':
                return jsonify({'ret': 'success', 'data': self.transfer_status})

            elif command == 'db_import_status':
                return jsonify({'ret': 'success', 'data': self.import_status})

            elif command == 'db_import_stop':
                self.import_status['stop_flag'] = True
                return jsonify({'ret': 'success', 'msg': '임포트 중단을 요청했습니다.'})

            # code, ui_code, originaltitle 3개 필드 대조 및 카테고리 전방위 폴백 탐색
            elif command == 'get_meta_by_code':
                from sqlalchemy.orm import selectinload
                detail_options = (
                    selectinload(MetaItem.media_files),
                    selectinload(MetaItem.person_maps),
                    selectinload(MetaItem.tag_maps),
                    selectinload(MetaItem.fingerprints),
                )
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
                    if raw_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
                        break
                    if c_cand not in categories_to_try:
                        categories_to_try.append(c_cand)

                found_item = None
                found_session = None

                match_targets = {target_code.lower()}
                try:
                    from support_site import SiteAvBase
                    raw_cid = target_code
                    if len(target_code) >= 3 and target_code[0] in ['C', 'E', 'W']:
                        raw_cid = target_code[2:].lstrip('_')

                    parsed_ui, _, _ = SiteAvBase._parse_ui_code(raw_cid)
                    if parsed_ui:
                        match_targets.add(parsed_ui.lower())
                        match_targets.add(parsed_ui.replace('-', '').lower())
                except Exception as e_parse:
                    logger.debug(f"[MetaDB get_meta_by_code] 코드 파싱 예외: {e_parse}")

                for try_cat in categories_to_try:
                    sess, _, std_cat = self.get_session_and_domain(try_cat)
                    if not sess:
                        continue
                    try:
                        # code, ui_code, originaltitle 필드와 100% 완전 일치하는 레코드만 엄격하게 조회
                        item = sess.query(MetaItem).options(*detail_options).filter(
                            MetaItem.category == std_cat, MetaItem.code == target_code).first()
                        if item is None and not (std_cat in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') and self._is_av_internal_code(target_code)):
                            item = sess.query(MetaItem).options(*detail_options).filter(
                            MetaItem.category == std_cat,
                            or_(
                                func.lower(MetaItem.code).in_(match_targets),
                                func.lower(MetaItem.ui_code).in_(match_targets),
                                func.lower(MetaItem.originaltitle).in_(match_targets)
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
                    from sqlalchemy.orm import selectinload
                    item = sess.query(MetaItem).options(
                        lazyload('*'),
                        selectinload(MetaItem.media_files),
                    ).filter_by(code=code).first()
                    if not item:
                        return jsonify({'ret': 'error', 'msg': '작품을 찾을 수 없습니다.'})

                    extra = dict(item.extra_info or {})
                    from .util_preview import MetaPreviewUtil
                    video_type = arg3 or req.form.get('video_type') or None
                    previous_videos = copy.deepcopy(extra)
                    success, del_msg = MetaPreviewUtil.delete_video_assets(extra, category=std_cat, video_type=video_type)
                    if not success:
                        return jsonify({'ret': 'error', 'msg': del_msg})
                    for kind, key in (('trailer', 'trailer_video'), ('preview', 'preview_clip')):
                        deleted = previous_videos.get(key)
                        if deleted and key not in extra:
                            for media in list(item.media_files):
                                if media.media_type == kind and bool(media.gdrive_fileid) == (deleted.get('storage_type') == 'gdrive'):
                                    item.media_files.remove(media)
                    item.extra_info = extra
                    self.sync_item_video_assets(item)
                    sess.commit()
                    logger.debug(f"[MetaPreview] [{std_cat}] {code} {del_msg}")
                    return jsonify({'ret': 'success', 'msg': f'{del_msg}되었습니다.'})
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

                is_compat, v_msg = self.check_transfer_version_compatibility(src_type, tgt_type, mode)
                if not is_compat:
                    return jsonify({'ret': 'warning', 'msg': v_msg})

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
                success, filepath_or_msg, count = self.export_database(category_mode=cat_mode, is_sanitized=is_clean)
                if success:
                    filename = os.path.basename(filepath_or_msg)
                    msg = f"Export 완료: {filepath_or_msg} (총 {count:,}건)"
                    return jsonify({'ret': 'success', 'filename': filename, 'filepath': filepath_or_msg, 'msg': msg})
                else:
                    return jsonify({'ret': 'error', 'msg': f'Export 실패: {filepath_or_msg}'})

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
        if sub == "batch_stream":
            return self.process_api("batch_stream", req)
        if sub == "meta_schema_stream":
            return self.process_api("meta_schema_stream", req)

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
            # SSE 실시간 로그 스트리밍 엔드포인트 (type=item / type=person 채널 분기)
            if sub == "batch_stream":
                batch_type = req.args.get('type') or req.values.get('type') or 'item'
                batch_key = 'person' if str(batch_type).lower() == 'person' else 'item'

                import queue
                sub_queue = queue.Queue()
                with self._batch_log_lock:
                    self.batch_log_subscribers[batch_key].append(sub_queue)

                def sse_event_stream():
                    try:
                        yield f": {' ' * 2048}\n\n"

                        status, logs = self._get_batch_snapshot(batch_key)
                        last_snapshot = json.dumps([status, logs], ensure_ascii=False)
                        last_success = status.get('success', 0)
                        yield "data: " + json.dumps({
                            'type': 'init', 'batch_type': batch_key,
                            'logs': logs, 'status': status
                        }, ensure_ascii=False) + "\n\n"

                        last_ping = time.monotonic()
                        while True:
                            try:
                                msg = sub_queue.get(timeout=1)
                                yield msg
                            except queue.Empty:
                                # A Celery worker cannot write to this process's queue.
                                status, logs = self._get_batch_snapshot(batch_key)
                                snapshot = json.dumps([status, logs], ensure_ascii=False)
                                if snapshot != last_snapshot:
                                    yield "data: " + json.dumps({
                                        'type': 'sync', 'batch_type': batch_key,
                                        'logs': logs, 'status': status,
                                        'is_updated': status.get('success', 0) > last_success
                                    }, ensure_ascii=False) + "\n\n"
                                    last_snapshot = snapshot
                                    last_success = status.get('success', 0)
                                elif time.monotonic() - last_ping >= 15:
                                    yield ": ping\n\n"
                                    last_ping = time.monotonic()
                    finally:
                        with self._batch_log_lock:
                            if sub_queue in self.batch_log_subscribers[batch_key]:
                                self.batch_log_subscribers[batch_key].remove(sub_queue)

                return Response(sse_event_stream(), mimetype="text/event-stream", headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no',
                    'Connection': 'keep-alive'
                })

            if sub == "meta_schema_stream":
                import queue
                sub_queue = queue.Queue()
                with self._meta_schema_log_lock:
                    self.meta_schema_log_subscribers.append(sub_queue)

                def schema_sse_event_stream():
                    try:
                        yield f": {' ' * 2048}\n\n"
                        init_payload = json.dumps({
                            'type': 'init',
                            'status': self.meta_migration_status
                        }, ensure_ascii=False)
                        yield f"data: {init_payload}\n\n"
                        while True:
                            try:
                                yield sub_queue.get(timeout=15)
                            except queue.Empty:
                                yield ": ping\n\n"
                    except GeneratorExit:
                        with self._meta_schema_log_lock:
                            if sub_queue in self.meta_schema_log_subscribers:
                                self.meta_schema_log_subscribers.remove(sub_queue)

                return Response(schema_sse_event_stream(), mimetype="text/event-stream", headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no',
                    'Connection': 'keep-alive'
                })

            # 대량 메타데이터 일괄 조회 API (info_batch / batch_info)
            if sub in ["info_batch", "batch_info"]:
                body_json = req.get_json(silent=True) if req.is_json else {}
                if not isinstance(body_json, dict):
                    body_json = {}

                category = body_json.get('category', body_json.get('cat',
                    req.values.get('category', req.values.get('cat', 'JAV_CEN'))))

                raw_codes = body_json.get('codes', body_json.get('code',
                    req.values.get('codes', req.values.get('code'))))

                if not raw_codes:
                    return jsonify({'ret': 'error', 'msg': '조회할 코드 목록(codes)이 전달되지 않았습니다.'}), 400

                options = body_json.get("options")
                return self._metadata_batch_response(category, raw_codes, options)

            if sub in ['media_sync', 'person_image_sync']:
                params = req.get_json(silent=True) if req.is_json else dict(req.values)
                if isinstance(params, dict) and isinstance(params.get('options'), str):
                    try:
                        params['options'] = json.loads(params['options'])
                    except ValueError:
                        return jsonify({'ret': 'error', 'error_code': 'invalid_request', 'msg': 'options JSON 형식 오류'}), 400
                return self._storage_sync_response(sub, params)

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
                    return meta_module.process_command("delete_preview_clip", code, cat, params.get("video_type") or req.values.get("video_type", ""), req)

        except Exception as e:
            logger.exception('[MetaDB API] 요청 처리 실패: sub=%s', sub)
            return jsonify({'ret': 'exception', 'msg': '메타 DB API 요청 처리에 실패했습니다.'}), 500
