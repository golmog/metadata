# -*- coding: utf-8 -*-
"""메타 DB 통합 마이그레이션 및 백필 실행 오케스트레이터 패키지."""

import os
import json
import time
import threading
from datetime import datetime
from sqlalchemy import text, inspect
from sqlalchemy.orm import sessionmaker

from ..setup import logger, path_data
from ..util_migration import (
    backup_postgres_database,
    backup_sqlite_database,
    save_json_state,
    load_json_state
)
from .ddl_ops import DdlOperations
from .lock_manager import MetaDbLockManager
from .revisions.rev_002_v1_to_v2 import RevisionV1ToV2


class MigrationOrchestrator:
    """스키마 마이그레이션 및 비동기 데이터 백필 통합 관리자."""

    TARGET_VERSION = 2

    def __init__(self, mod_meta_db_instance, maintenance=False):
        self.mod = mod_meta_db_instance
        self.maintenance = maintenance
        self.revisions = [
            RevisionV1ToV2()
        ]
        state_name = 'metadata_maintenance_state.json' if maintenance else 'metadata_migration_orchestrator.json'
        self.state_file = os.path.join(path_data, 'db', state_name)
        self.status = {
            'is_running': False,
            'status': '대기 중',
            'phase': 'idle',
            'current_version': 0,
            'target_version': self.TARGET_VERSION,
            'revision_id': self.revisions[0].revision_id,
            'current': 0,
            'total': 0,
            'current_db': '',
            'backup_path': '',
            'last_error': '',
            'stop_flag': False,
            'can_resume': False
        }
        self._load_state()

    def get_status(self) -> dict:
        """현재 마이그레이션 및 백필 진행 상태 반환."""
        copy_status = dict(self.status)
        return copy_status

    def is_writable(self, category: str = None) -> bool:
        """현재 DB가 쓰기 작업이 가능한 상태인지 원자적 검증."""
        if self.status.get('is_running'):
            return False
        maintenance = self.mod._get_maintenance_status()
        if maintenance.get('is_running') and maintenance.get('operation') in ('backup', 'restore'):
            return False
        meta_status = self.mod.get_meta_schema_status(category or 'JAV_CEN')
        version = int(meta_status.get('schema_version') or 0)
        state = meta_status.get('migration_state')
        if version != self.TARGET_VERSION or state != 'complete':
            return False
        _, domain, _ = self.mod.get_session_and_domain(category or 'JAV_CEN')
        engine = self.mod._engines.get('postgres' if self.mod._is_postgres else domain)
        return engine is not None and not MetaDbLockManager.is_locked(engine)

    @classmethod
    def prepare_engine_schema(cls, target_engine, physical_name: str, base_metadata):
        """Only bootstrap version discovery on existing DBs; install empty DBs at v2."""
        from ..mod_meta_db import MetaSchemaVersion
        try:
            existing = set(inspect(target_engine).get_table_names())
            MetaSchemaVersion.__table__.create(bind=target_engine, checkfirst=True)
            with target_engine.begin() as conn:
                row = conn.execute(text("SELECT schema_version, migration_state FROM meta_schema_version "
                                        "WHERE schema_name='meta_db'")).first()
                user_tables = existing - {'meta_schema_version', 'meta_schema_migration_history', 'meta_db_job_lock'}
                if not user_tables:
                    if conn.dialect.name == 'sqlite':
                        conn.exec_driver_sql('BEGIN')
                    base_metadata.create_all(bind=conn)
                    RevisionV1ToV2.prepare_auxiliary_schema(conn)
                    RevisionV1ToV2.finalize_schema(conn)
                    values = {'ver': cls.TARGET_VERSION, 'now': datetime.now()}
                    if row:
                        conn.execute(text("UPDATE meta_schema_version SET schema_version=:ver, "
                            "migration_state='complete', completed_at=:now, updated_at=:now "
                            "WHERE schema_name='meta_db'"), values)
                    else:
                        conn.execute(text("INSERT INTO meta_schema_version "
                            "(schema_name,schema_version,migration_state,completed_at,updated_at) "
                            "VALUES ('meta_db',:ver,'complete',:now,:now)"), values)
                    logger.info(f'[MetaDB Schema] 신규 메타 DB v{cls.TARGET_VERSION} 초기화 완료: {physical_name}')
                elif row is None:
                    conn.execute(text("INSERT INTO meta_schema_version "
                        "(schema_name,schema_version,migration_state,updated_at) "
                        "VALUES ('meta_db',0,'migration_required',:now)"), {'now': datetime.now()})
                    logger.error(f'[MetaDB Schema] 지원하지 않는 기존 DB 버전: {physical_name} (v1/complete만 업그레이드 가능)')
                elif row[0] not in (1, cls.TARGET_VERSION):
                    logger.error(f'[MetaDB Schema] 지원하지 않는 기존 DB 버전: {physical_name} (v{row[0]}, v1/complete만 업그레이드 가능)')
                elif row[0] != cls.TARGET_VERSION or row[1] != 'complete':
                    logger.warning(f'[MetaDB Schema] 관리자 마이그레이션 필요: {physical_name} (v{row[0]} -> v2)')
        except Exception as exc:
            logger.error(f'[MetaDB Schema] 엔진 스키마 상태 초기화 실패 ({physical_name}): {exc}')

    def _upgrade_targets(self):
        targets = self.mod._meta_db_targets()
        if not targets:
            raise RuntimeError('사용 가능한 메타 DB가 없습니다.')
        pending = []
        for name, engine, path in targets:
            if engine is None:
                raise RuntimeError(f'메타 DB 연결 없음: {name}')
            with engine.connect() as conn:
                row = conn.execute(text("SELECT schema_version,migration_state,migration_id "
                                        "FROM meta_schema_version WHERE schema_name='meta_db'")).first()
            if row and row[0] == self.TARGET_VERSION and row[1] == 'complete':
                continue
            resumable = (row and row[0] == 1 and row[1] == 'backfilling'
                         and row[2] == self.revisions[0].revision_id
                         and self.status.get('revision_id') == row[2]
                         and self.status.get('backup_complete')
                         and os.path.isdir(self.status.get('backup_path') or ''))
            if not (row and row[0] == 1 and (row[1] == 'complete' or resumable)):
                raise RuntimeError(f'{name}: v1/complete -> v2만 지원합니다. 기존 v0/미확인 상태는 업그레이드할 수 없습니다.')
            pending.append((name, engine, path))
        return pending

    def _index_repair_targets(self):
        """v2/complete 상태이나 SQLite 커버링 인덱스가 누락/불일치한 대상 DB 탐색."""
        targets = self.mod._meta_db_targets()
        if not targets:
            return []
        needs_repair = []
        for name, engine, path in targets:
            if engine is None or engine.dialect.name != 'sqlite':
                continue
            with engine.connect() as conn:
                row = conn.execute(text("SELECT schema_version,migration_state "
                                        "FROM meta_schema_version WHERE schema_name='meta_db'")).first()
                if not row or row[0] != self.TARGET_VERSION or row[1] != 'complete':
                    continue
                if not inspect(conn).has_table('meta_item'):
                    continue
                if not RevisionV1ToV2.is_sqlite_covering_index_valid(conn):
                    needs_repair.append((name, engine, path))
        return needs_repair

    def _targets(self):
        return self._active_targets if hasattr(self, '_active_targets') else self.mod._meta_db_targets()

    def start_upgrade(self, daemon: bool = True) -> tuple:
        """Explicit v1 -> v2 upgrade, or index repair for existing v2 DBs, or a read-only no-op when already complete."""
        maintenance = self.mod._get_maintenance_status()
        if self.status.get('is_running') or maintenance.get('is_running'):
            return False, '마이그레이션 또는 DB 관리 작업이 이미 진행 중입니다.'
        if (maintenance.get('operation') == 'restore' and maintenance.get('phase') == 'complete'
                and maintenance.get('updated_at', 0) > self.status.get('updated_at', 0)):
            self.status.update(can_resume=False, backfill_positions={}, backup_complete=False)
        try:
            pending_upgrade = self._upgrade_targets()
            pending_repair = self._index_repair_targets() if not pending_upgrade else []
        except Exception as exc:
            return False, str(exc)
        if not pending_upgrade and not pending_repair:
            self.status.update(current_version=self.TARGET_VERSION, phase='complete',
                               status='이미 v2 완료 및 인덱스 정상', can_resume=False, last_error='')
            return True, '모든 메타 DB가 이미 v2/complete이며 인덱스가 정상입니다.'

        if pending_upgrade:
            if not self.status.get('can_resume'):
                self.status.update(backfill_positions={}, backup_complete=False, backup_path='')
            self.status.update(is_running=True, status='v1 -> v2 마이그레이션 준비 중...', phase='starting',
                current_version=1, current=0, total=0, current_db='', last_error='', stop_flag=False, can_resume=False)
            self._save_state()
            threading.Thread(target=self._run_pipeline, daemon=daemon).start()
            return True, '메타 DB v1 -> v2 마이그레이션을 시작했습니다.'
        else:
            self.status.update(is_running=True, status='v2 인덱스 정비 준비 중...', phase='starting',
                current_version=self.TARGET_VERSION, current=0, total=len(pending_repair),
                current_db='', last_error='', stop_flag=False, can_resume=False)
            self._save_state()
            threading.Thread(target=self._run_index_repair_pipeline, daemon=daemon).start()
            return True, '메타 DB v2 커버링 인덱스 정비를 시작했습니다.'

    def repair_indexes(self, daemon: bool = True) -> tuple:
        """v2/complete SQLite DB 전용 명시적 커버링 인덱스 정비."""
        maintenance = self.mod._get_maintenance_status()
        if self.status.get('is_running') or maintenance.get('is_running'):
            return False, '마이그레이션 또는 DB 관리 작업이 이미 진행 중입니다.'
        try:
            pending_repair = self._index_repair_targets()
        except Exception as exc:
            return False, str(exc)
        if not pending_repair:
            return True, '정비가 필요한 SQLite v2 커버링 인덱스가 없습니다.'
        self.status.update(is_running=True, status='v2 인덱스 정비 준비 중...', phase='starting',
            current_version=self.TARGET_VERSION, current=0, total=len(pending_repair),
            current_db='', last_error='', stop_flag=False, can_resume=False)
        self._save_state()
        threading.Thread(target=self._run_index_repair_pipeline, daemon=daemon).start()
        return True, '메타 DB v2 커버링 인덱스 정비를 시작했습니다.'

    def request_stop(self) -> tuple:
        """진행 중인 백필 작업 중단 요청."""
        if not self.status.get('is_running'):
            return False, "실행 중인 마이그레이션 작업이 없습니다."
        self.status['stop_flag'] = True
        self.status['status'] = "중단 요청 처리 중..."
        self._save_state()
        return True, "마이그레이션 중단을 요청했습니다."

    def start_backup(self, daemon: bool = True) -> tuple:
        """Manual backups share maintenance execution/status, not migration progress."""
        return self.mod._start_maintenance_job('backup')

    def _run_manual_backup(self):
        """수동 백업 실행 및 실시간 진행률 송출, 감사 원장 기록."""
        engine_target = self.mod._engines.get('postgres') if self.mod._is_postgres else self.mod._engines.get('jav_cen')
        dialect = 'postgresql' if self.mod._is_postgres else 'sqlite'
        worker_id = MetaDbLockManager.worker_id('backup')

        start_time = datetime.now()
        backup_root = ""
        acquired_lock = False
        heartbeat_stop = threading.Event()
        heartbeat_thread = None

        try:
            MetaDbLockManager.initialize(engine_target)
            acquired_lock = MetaDbLockManager.acquire(engine_target, dialect, worker_id=worker_id)
            if not acquired_lock:
                raise RuntimeError("다른 프로세스에서 작업이 진행 중입니다 (락 획득 실패).")

            heartbeat_thread = self._start_lock_heartbeat(engine_target, worker_id, heartbeat_stop)
            backup_root = os.path.join(
                path_data, 'db', 'meta_db_backups',
                start_time.strftime('manual_meta_db_%Y%m%d_%H%M%S')
            )
            os.makedirs(backup_root, exist_ok=True)
            self.status.update({
                'phase': 'backup',
                'status': '수동 백업 생성 중',
                'backup_path': backup_root
            })
            self._save_state()
            self._execute_backups(backup_root)

            duration = (datetime.now() - start_time).total_seconds()
            current_ver = int(self.mod.get_schema_version('JAV_CEN') or 0)
            self._record_history(
                revision_id="manual_backup",
                from_version=current_ver,
                to_version=current_ver,
                phase='backup',
                status='success',
                started_at=start_time,
                completed_at=datetime.now(),
                duration_sec=duration,
                backup_path=backup_root
            )

            self.status.update({
                'is_running': False,
                'status': '백업 완료',
                'phase': 'complete',
                'can_resume': False,
                'last_error': ''
            })
            self._save_state()
            logger.info(f"[Migration] 수동 백업 생성 완료: {backup_root} (소요시간: {duration:.1f}초)")

        except Exception as exc:
            self.status.update({
                'is_running': False,
                'status': '백업 실패',
                'phase': 'failed',
                'last_error': str(exc),
                'can_resume': False
            })
            self._save_state()
            logger.error(f"[Migration] 수동 백업 실패: {exc}", exc_info=True)

        finally:
            heartbeat_stop.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=15)
            if acquired_lock:
                MetaDbLockManager.release(engine_target, dialect, worker_id=worker_id)

    def _run_pipeline(self):
        """Backup -> rev002 DDL -> bounded derived-field backfill -> verified v2."""
        engine_target = self.mod._engines.get('postgres' if self.mod._is_postgres else 'jav_cen')
        dialect = 'postgresql' if self.mod._is_postgres else 'sqlite'
        worker_id = MetaDbLockManager.worker_id('migration')
        start_time = datetime.now()
        backup_root = ''
        acquired_lock = False
        heartbeat_stop = threading.Event()
        heartbeat_thread = None
        self._active_targets = []
        try:
            self._active_targets = self._upgrade_targets()
            if not self._active_targets:
                self.status.update(is_running=False, phase='complete', status='이미 v2 완료', can_resume=False)
                self._save_state()
                return
            MetaDbLockManager.initialize(engine_target)
            acquired_lock = MetaDbLockManager.acquire(engine_target, dialect, worker_id=worker_id)
            if not acquired_lock:
                raise RuntimeError('다른 프로세스에서 마이그레이션이 실행 중입니다 (락 획득 실패).')
            # Revalidate after winning the lease; another process may have completed it.
            self._active_targets = self._upgrade_targets()
            if not self._active_targets:
                self.status.update(is_running=False, phase='complete', status='이미 v2 완료', can_resume=False)
                self._save_state()
                return
            heartbeat_thread = self._start_lock_heartbeat(engine_target, worker_id, heartbeat_stop)
            # A complete v1 row means a fresh upgrade (including restored backups).
            for name, engine, _ in self._active_targets:
                with engine.connect() as conn:
                    state = conn.execute(text("SELECT migration_state FROM meta_schema_version WHERE schema_name='meta_db'")).scalar()
                if state == 'complete':
                    self.status.setdefault('backfill_positions', {}).pop(name, None)
                    self.status['backup_complete'] = False
            if self.status.get('backup_complete') and os.path.isdir(self.status.get('backup_path') or ''):
                backup_root = self.status['backup_path']
            else:
                backup_root = os.path.join(path_data, 'db', 'meta_db_backups', start_time.strftime('meta_db_v2_%Y%m%d_%H%M%S_%f'))
                os.makedirs(backup_root, exist_ok=True)
                self.status.update(phase='backup', status='v1 사전 백업 생성 중', backup_path=backup_root, backup_complete=False)
                self._save_state()
                self._execute_backups(backup_root)
                self.status['backup_complete'] = True
                self._save_state()
            if self.status.get('stop_flag'):
                raise InterruptedError('마이그레이션 중단 요청')
            self.status.update(phase='migrate', status='v2 스키마 준비 중')
            self._save_state()
            # Tag the backed-up attempt before DDL, so a DDL failure can resume
            # with its original backup instead of backing up a partial schema.
            self._update_schema_version_record(1, 'backfilling', backup_root)
            self._execute_fast_ddl(backup_root)
            # Keep version 1 until BOTH data and indexes have been verified.
            self.status.update(phase='backfill', status='배우 검색 텍스트 백필 중')
            self._save_state()
            self._execute_backfill()
            if self.status.get('stop_flag') or not MetaDbLockManager.renew(engine_target, worker_id):
                raise InterruptedError('마이그레이션 잠금 또는 중단 상태를 확인하세요.')
            duration = (datetime.now() - start_time).total_seconds()
            self._update_schema_version_record(self.TARGET_VERSION, 'complete', backup_root, history={
                'revision_id': self.revisions[0].revision_id, 'from_version': 1, 'to_version': 2,
                'phase': 'complete', 'status': 'success', 'started_at': start_time,
                'completed_at': datetime.now(), 'duration_sec': duration, 'backup_path': backup_root})
            self.status.update(is_running=False, status='완료', phase='complete', current_version=2, can_resume=False, last_error='')
            self._save_state()
            logger.info(f'[Migration] v1 -> v2 마이그레이션 완료 ({duration:.1f}초)')
        except InterruptedError as exc:
            self.status.update(is_running=False, status=f'중단됨: {exc}', phase='stopped', can_resume=True)
            self._save_state()
            logger.warning(f'[Migration] 작업 중단: {exc}')
        except Exception as exc:
            self.status.update(is_running=False, status='실패', phase='failed', last_error=str(exc),
                               can_resume=bool(getattr(self, '_active_targets', [])))
            self._save_state()
            logger.error(f'[Migration] v1 -> v2 마이그레이션 실패: {exc}', exc_info=True)
            if self._active_targets:
                self._record_history(revision_id=self.revisions[0].revision_id, from_version=1, to_version=2,
                    phase='failed', status='failed', started_at=start_time, completed_at=datetime.now(),
                    duration_sec=(datetime.now() - start_time).total_seconds(), backup_path=backup_root, error_message=str(exc))
        finally:
            heartbeat_stop.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=15)
            if acquired_lock:
                MetaDbLockManager.release(engine_target, dialect, worker_id=worker_id)

    def _run_index_repair_pipeline(self):
        """v2/complete SQLite DB를 대상으로 커버링 인덱스를 안전하고 멱등적으로 보완."""
        engine_target = self.mod._engines.get('jav_cen') or self.mod._engines.get('western')
        dialect = 'sqlite'
        worker_id = MetaDbLockManager.worker_id('index_repair')
        start_time = datetime.now()
        backup_root = ''
        acquired_lock = False
        heartbeat_stop = threading.Event()
        heartbeat_thread = None
        self._active_targets = []
        try:
            self._active_targets = self._index_repair_targets()
            if not self._active_targets:
                self.status.update(is_running=False, phase='complete', status='인덱스 정상', can_resume=False)
                self._save_state()
                return
            MetaDbLockManager.initialize(engine_target)
            acquired_lock = MetaDbLockManager.acquire(engine_target, dialect, worker_id=worker_id)
            if not acquired_lock:
                raise RuntimeError('다른 프로세스에서 DB 작업이 실행 중입니다 (락 획득 실패).')
            self._active_targets = self._index_repair_targets()
            if not self._active_targets:
                self.status.update(is_running=False, phase='complete', status='인덱스 정상', can_resume=False)
                self._save_state()
                return
            heartbeat_thread = self._start_lock_heartbeat(engine_target, worker_id, heartbeat_stop)

            # 사전 백업 생성
            backup_root = os.path.join(path_data, 'db', 'meta_db_backups',
                                       start_time.strftime('meta_db_index_%Y%m%d_%H%M%S_%f'))
            os.makedirs(backup_root, exist_ok=True)
            self.status.update(phase='backup', status='인덱스 정비 사전 백업 중',
                               backup_path=backup_root, backup_complete=False)
            self._save_state()
            self._execute_backups(backup_root)
            self.status['backup_complete'] = True
            self._save_state()

            if self.status.get('stop_flag'):
                raise InterruptedError('인덱스 정비 중단 요청')

            self.status.update(phase='migrate', status='SQLite 커버링 인덱스 생성 중')
            self._save_state()

            for idx, (p_name, eng, _) in enumerate(self._active_targets, 1):
                if eng is None:
                    continue
                self.status.update(current=idx, total=len(self._active_targets),
                                   status=f'{p_name} 커버링 인덱스 생성 중', current_db=p_name)
                self._save_state()
                with eng.begin() as conn:
                    RevisionV1ToV2.finalize_schema(conn)

            duration = (datetime.now() - start_time).total_seconds()
            for _, eng, _ in self._active_targets:
                try:
                    with eng.begin() as conn:
                        self._record_history(
                            revision_id='rev_002_index_repair',
                            from_version=self.TARGET_VERSION,
                            to_version=self.TARGET_VERSION,
                            phase='complete',
                            status='success',
                            started_at=start_time,
                            completed_at=datetime.now(),
                            duration_sec=duration,
                            backup_path=backup_root,
                            connection=conn
                        )
                except Exception as hist_err:
                    logger.debug(f'[Migration History] 인덱스 정비 원장 기록 실패: {hist_err}')

            self.status.update(is_running=False, status='인덱스 정비 완료', phase='complete',
                               current_version=self.TARGET_VERSION, can_resume=False, last_error='')
            self._save_state()
            logger.info(f'[Migration] SQLite v2 커버링 인덱스 정비 완료 ({duration:.1f}초)')
        except InterruptedError as exc:
            self.status.update(is_running=False, status=f'중단됨: {exc}', phase='stopped', can_resume=False)
            self._save_state()
            logger.warning(f'[Migration] 인덱스 정비 중단: {exc}')
        except Exception as exc:
            self.status.update(is_running=False, status='실패', phase='failed', last_error=str(exc), can_resume=False)
            self._save_state()
            logger.error(f'[Migration] SQLite v2 커버링 인덱스 정비 실패: {exc}', exc_info=True)
            if self._active_targets:
                self._record_history(
                    revision_id='rev_002_index_repair',
                    from_version=self.TARGET_VERSION,
                    to_version=self.TARGET_VERSION,
                    phase='failed',
                    status='failed',
                    started_at=start_time,
                    completed_at=datetime.now(),
                    duration_sec=(datetime.now() - start_time).total_seconds(),
                    backup_path=backup_root,
                    error_message=str(exc)
                )
        finally:
            heartbeat_stop.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=15)
            if acquired_lock:
                MetaDbLockManager.release(engine_target, dialect, worker_id=worker_id)

    def _start_lock_heartbeat(self, engine, worker_id, stop):
        def heartbeat():
            while not stop.wait(MetaDbLockManager.HEARTBEAT_SECONDS):
                try:
                    if not MetaDbLockManager.renew(engine, worker_id):
                        self.status['stop_flag'] = True
                        self.status['last_error'] = 'Migration lease ownership lost'
                        return
                except Exception as exc:
                    logger.error(f'[MetaDB Lock] heartbeat failed: {exc}')
                    self.status['stop_flag'] = True
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        return thread

    def _execute_backups(self, backup_root: str):
        """사전 물리/논리 백업 실행."""
        targets = self._targets()
        self.status['total'] = len(targets)
        self.status['current'] = 0
        last_emit = 0.0
        completed_targets = 0
        total_targets = sum(1 for _, _, src in targets if src and os.path.exists(src))

        def backup_progress(cur, tot, msg, db_name):
            nonlocal last_emit
            if self.status.get('stop_flag'):
                raise InterruptedError('백업 작업이 중단되었습니다.')
            now = time.time()
            if now - last_emit >= 0.25 or cur >= tot:
                last_emit = now
                self.status.update({
                    'current': cur,
                    'total': tot,
                    'status': msg,
                    'current_db': db_name
                })
                if not self.mod._is_postgres:
                    self.status.update(completed_targets=completed_targets, total_targets=total_targets,
                        percent=100 * (completed_targets + (cur / tot if tot else 0)) / max(1, total_targets))
                self._save_state()

        if self.mod._is_postgres:
            logical_path = os.path.join(backup_root, 'meta_db_postgres.sqlite3')
            backup_postgres_database(
                self.mod._engines.get('postgres'),
                logical_path,
                progress_callback=backup_progress
            )
        else:
            for idx, (p_name, eng, src_path) in enumerate(targets, 1):
                if not src_path or not os.path.isfile(src_path):
                    raise RuntimeError(f'{p_name}: SQLite 사전 백업 원본 파일이 없습니다.')
                if src_path and os.path.exists(src_path):
                    dst_path = os.path.join(backup_root, os.path.basename(src_path))
                    backup_sqlite_database(
                        src_path, dst_path,
                        progress_callback=backup_progress,
                        target_name=p_name
                    )
                    completed_targets += 1
                    self.status.update(completed_targets=completed_targets, total_targets=total_targets,
                        percent=100 * completed_targets / max(1, total_targets))
                    self._save_state()

    def _execute_fast_ddl(self, backup_root: str):
        """모든 타겟 DB에 Fast DDL 실행."""
        self.status.pop('percent', None)
        targets = self._targets()
        self.status['total'] = len(targets)
        dialect = 'postgresql' if self.mod._is_postgres else 'sqlite'

        for idx, (p_name, eng, _) in enumerate(targets, 1):
            if eng is None:
                continue

            self.status.update({
                'current': idx,
                'status': f'{p_name} 스키마 DDL 변경 중',
                'current_db': p_name
            })
            self._save_state()

            with eng.begin() as conn:
                ops = DdlOperations(conn, dialect)
                for rev in self.revisions:
                    rev.upgrade_schema(ops, p_name)

    def _execute_backfill(self):
        positions = self.status.setdefault('backfill_positions', {})
        for physical_name, engine, _ in self._targets():
            if engine is None:
                continue
            tables = set(inspect(engine).get_table_names())
            session = sessionmaker(bind=engine, expire_on_commit=False)()
            target_positions = positions.setdefault(physical_name, {})
            def checkpoint(table, last_id):
                target_positions[table] = last_id
                self._save_state()
            def progress(cur, total, msg, target):
                self.status.update(current=cur, total=total, status=msg, current_db=physical_name)
                self._save_state()
            context = {
                'tables': tables, 'positions': target_positions, 'physical_name': physical_name,
                'checkpoint': checkpoint, 'stop_flag_checker': lambda: self.status.get('stop_flag', False)
            }
            try:
                for revision in self.revisions:
                    if revision.has_backfill:
                        revision.run_backfill(session, context, progress_callback=progress)
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    def _update_schema_version_record(self, new_version: int, state: str, backup_path: str, history=None):
        for _, engine, _ in self._targets():
            with engine.begin() as conn:
                if state == 'complete':
                    self.revisions[0].finalize_schema(conn)
                conn.execute(text("UPDATE meta_schema_version SET schema_version=:ver,migration_id=:mid, "
                    "migration_state=:state,completed_at=:completed,updated_at=:now,last_error=NULL,backup_path=:backup "
                    "WHERE schema_name='meta_db'"), {'ver': new_version, 'mid': self.revisions[0].revision_id,
                    'state': state, 'completed': datetime.now() if state == 'complete' else None,
                    'now': datetime.now(), 'backup': backup_path})
                if history:
                    self._record_history(connection=conn, **history)

    def _record_history(self, revision_id, from_version, to_version, phase, status,
                        started_at, completed_at, duration_sec, backup_path, error_message=None, connection=None):
        """INSERT only; schema creation belongs to explicit installation/revision."""
        statement = text("INSERT INTO meta_schema_migration_history "
            "(revision_id,from_version,to_version,phase,status,started_at,completed_at,duration_sec,backup_path,error_message) "
            "VALUES (:rid,:fv,:tv,:phase,:status,:start,:end,:dur,:backup,:err)")
        params = dict(rid=revision_id, fv=from_version, tv=to_version, phase=phase, status=status,
                      start=started_at, end=completed_at, dur=duration_sec, backup=backup_path, err=error_message)
        if connection is not None:
            connection.execute(statement, params)
            return
        for _, engine, _ in self._targets():
            try:
                with engine.begin() as conn:
                    if status == 'failed':
                        row = conn.execute(text("SELECT schema_version,migration_state FROM meta_schema_version WHERE schema_name='meta_db'")).first()
                        if row and row[0] == 2 and row[1] == 'complete':
                            continue
                    conn.execute(statement, params)
            except Exception as exc:
                logger.debug(f'[Migration History] 원장 기록 불가: {exc}')

    def _save_state(self):
        """진행 상태 저장 및 SSE 브로드캐스트."""
        self.status['updated_at'] = time.time()
        save_json_state(self.state_file, self.status)
        if getattr(self, 'maintenance', False):
            self.mod._publish_maintenance_status(self.status)
            return
        payload = json.dumps({'type': 'status', 'status': self.status}, ensure_ascii=False)
        with self.mod._meta_schema_log_lock:
            subscribers = list(self.mod.meta_schema_log_subscribers)
        for sub in subscribers:
            try:
                sub.put_nowait(f"data: {payload}\n\n")
            except Exception:
                pass

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return
        saved = load_json_state(self.state_file)
        if not isinstance(saved, dict):
            return
        if not self.maintenance and (saved.get('revision_id') != self.revisions[0].revision_id
                                     or saved.get('target_version') != self.TARGET_VERSION):
            return
        if saved.get('is_running') and not self.maintenance:
            saved.update(is_running=False, status='서버 재시작으로 중단됨 (재개 가능)', can_resume=True)
        self.status.update(saved)
