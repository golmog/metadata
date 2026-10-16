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
from .revisions.rev_001_v0_to_v1 import RevisionV0ToV1


class MigrationOrchestrator:
    """스키마 마이그레이션 및 비동기 데이터 백필 통합 관리자."""

    TARGET_VERSION = 1

    def __init__(self, mod_meta_db_instance, maintenance=False):
        self.mod = mod_meta_db_instance
        self.maintenance = maintenance
        self.revisions = [
            RevisionV0ToV1()  # 순차 리비전 목록 등록
        ]
        state_name = 'metadata_maintenance_state.json' if maintenance else 'metadata_migration_orchestrator.json'
        self.state_file = os.path.join(path_data, 'db', state_name)
        self.status = {
            'is_running': False,
            'status': '대기 중',
            'phase': 'idle',
            'current_version': 0,
            'target_version': self.TARGET_VERSION,
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
        if version < self.TARGET_VERSION or state != 'complete':
            return False
        _, domain, _ = self.mod.get_session_and_domain(category or 'JAV_CEN')
        engine = self.mod._engines.get('postgres' if self.mod._is_postgres else domain)
        return engine is not None and not MetaDbLockManager.is_locked(engine)

    @classmethod
    def prepare_engine_schema(cls, target_engine, physical_name: str, base_metadata):
        """
        신규 설치 DB는 v1 스키마로 생성하고, 기존 DB는 ORM 호환에 필요한 누락 컬럼을 확보합니다.
        버전 상태를 감지/등록하며, 실제 데이터 이전은 명시적인 마이그레이션에서 수행합니다.
        """
        from sqlalchemy import inspect
        from ..mod_meta_db import MetaSchemaVersion, MetaSchemaMigrationHistory

        try:
            inspector = inspect(target_engine)
            existing_tables = set(inspector.get_table_names())
            version_table = MetaSchemaVersion.__table__
            version_table.create(bind=target_engine, checkfirst=True)
            history_table = MetaSchemaMigrationHistory.__table__
            history_table.create(bind=target_engine, checkfirst=True)
            dialect = str(target_engine.dialect.name or 'sqlite').lower()

            with target_engine.begin() as conn:
                ops = DdlOperations(conn, dialect)

                # v0 레거시 DB 호환성 방어:
                # 마이그레이션 실행 전에도 ORM의 SELECT 쿼리가 깨지지 않도록 미존재 컬럼을 선제 확보
                if 'meta_media' in existing_tables:
                    media_cols = {
                        'gdrive_fileid': 'VARCHAR(255)',
                        'relative_path': 'VARCHAR(500)',
                        'source': 'VARCHAR(30)',
                        'checksum': 'VARCHAR(128)',
                        'updated_time': 'DATETIME',
                    }
                    for c_name, c_type in media_cols.items():
                        ops.add_column('meta_media', c_name, c_type)

                if 'meta_schema_version' in existing_tables:
                    ops.add_column('meta_schema_version', 'lock_token', 'VARCHAR(100)')
                    ops.add_column('meta_schema_version', 'locked_at', 'DATETIME')
                    ops.add_column('meta_schema_version', 'locked_by', 'VARCHAR(100)')

                if 'meta_person' in existing_tables:
                    person_cols = {
                        'gender': 'VARCHAR(15)',
                        'birth_date': 'VARCHAR(20)',
                        'birth_year': 'INTEGER',
                        'debut_date': 'VARCHAR(20)',
                        'height': 'INTEGER',
                        'bust': 'INTEGER',
                        'waist': 'INTEGER',
                        'hip': 'INTEGER',
                        'body_size': 'VARCHAR(30)',
                        'cup_size': 'VARCHAR(10)',
                        'blood_type': 'VARCHAR(5)',
                        'agency': 'VARCHAR(100)',
                        'country': 'VARCHAR(50)',
                        'info_url': 'VARCHAR(500)',
                        'has_photo': 'BOOLEAN',
                        'thumb_url': 'VARCHAR(500)',
                        'local_path': 'VARCHAR(255)',
                        'gdrive_fileid': 'VARCHAR(255)',
                        'custom_gdrive_fileid': 'VARCHAR(255)',
                        'custom_local_path': 'VARCHAR(255)',
                        'official_cached_gdrive_fileid': 'VARCHAR(255)',
                        'official_cached_local_path': 'VARCHAR(500)',
                        'official_remote_path': 'VARCHAR(500)',
                        'custom_remote_path': 'VARCHAR(500)',
                        'site_img_url': 'VARCHAR(500)',
                        'photo_urls': 'JSON',
                        'merged_count': 'INTEGER',
                        'alt_indices': 'JSON',
                        'merged_details': 'JSON',
                        'works_count': 'INTEGER',
                        'source_data': 'JSON',
                        'manual_data': 'JSON',
                        'created_time': 'DATETIME',
                        'updated_time': 'DATETIME',
                    }
                    for c_name, c_type in person_cols.items():
                        ops.add_column('meta_person', c_name, c_type)

                version_row = conn.execute(
                    text(
                        "SELECT schema_version, migration_state "
                        "FROM meta_schema_version WHERE schema_name = 'meta_db'"
                    )
                ).fetchone()

                user_tables = existing_tables - {'meta_schema_version', 'meta_schema_migration_history', 'meta_db_job_lock'}

                # 신규 설치: 테이블이 전혀 없는 빈 DB인 경우 최신 v1으로 즉시 생성
                if not user_tables:
                    base_metadata.create_all(bind=target_engine)
                    if version_row is None:
                        conn.execute(
                            text(
                                "INSERT INTO meta_schema_version "
                                "(schema_name, schema_version, migration_state, completed_at, updated_at) "
                                "VALUES ('meta_db', :ver, 'complete', :now, :now)"
                            ),
                            {'ver': cls.TARGET_VERSION, 'now': datetime.now()}
                        )
                    else:
                        conn.execute(
                            text(
                                "UPDATE meta_schema_version SET schema_version = :ver, "
                                "migration_state = 'complete', completed_at = :now, updated_at = :now "
                                "WHERE schema_name = 'meta_db'"
                            ),
                            {'ver': cls.TARGET_VERSION, 'now': datetime.now()}
                        )
                    logger.info(f"[MetaDB Schema] 신규 메타 DB v{cls.TARGET_VERSION} 초기화 완료: {physical_name}")

                # 레거시 v0 DB: 기존 테이블은 있으나 버전 기록이 없는 경우
                elif version_row is None:
                    conn.execute(
                        text(
                            "INSERT INTO meta_schema_version "
                            "(schema_name, schema_version, migration_state, updated_at) "
                            "VALUES ('meta_db', 0, 'migration_required', :now)"
                        ),
                        {'now': datetime.now()}
                    )
                    logger.warning(f"[MetaDB Schema] 레거시 v0 메타 DB 감지 (마이그레이션 필요): {physical_name}")

                elif int(version_row[0] or 0) < cls.TARGET_VERSION:
                    logger.warning(
                        f"[MetaDB Schema] 스키마 마이그레이션 필요: {physical_name} "
                        f"(v{version_row[0]} -> v{cls.TARGET_VERSION})"
                    )

        except Exception as exc:
            logger.error(f"[MetaDB Schema] 엔진 스키마 상태 초기화 실패 ({physical_name}): {exc}")

    def start_upgrade(self, daemon: bool = True) -> tuple:
        """v0에서 v1으로의 전체 마이그레이션(Fast DDL + Async Backfill) 트리거."""
        maintenance = self.mod._get_maintenance_status()
        if self.status.get('is_running') or maintenance.get('is_running'):
            return False, "마이그레이션 또는 DB 관리 작업이 이미 진행 중입니다."

        # A restore in another process invalidates the old web process's resume cursor.
        if (maintenance.get('operation') == 'restore' and maintenance.get('phase') == 'complete'
                and maintenance.get('updated_at', 0) > self.status.get('updated_at', 0)):
            self.status['can_resume'] = False
        if not self.status.get('can_resume'):
            self.status['backfill_positions'] = {}
        self.status.update({
            'is_running': True,
            'status': '마이그레이션 준비 중...',
            'phase': 'starting',
            'current': 0,
            'total': 0,
            'current_db': '',
            'last_error': '',
            'stop_flag': False,
            'can_resume': False
        })
        self._save_state()

        thread = threading.Thread(target=self._run_pipeline, daemon=daemon)
        thread.start()
        return True, "메타 DB v0 -> v1 통합 마이그레이션을 시작했습니다."

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
        """Fast DDL -> 락 해제 -> Async Backfill -> 감사 원장 기록 통합 파이프라인."""
        engine_target = self.mod._engines.get('postgres') if self.mod._is_postgres else self.mod._engines.get('jav_cen')
        dialect = 'postgresql' if self.mod._is_postgres else 'sqlite'
        worker_id = MetaDbLockManager.worker_id('migration')

        start_time = datetime.now()
        backup_root = ""
        acquired_lock = False
        heartbeat_stop = threading.Event()
        heartbeat_thread = None

        try:
            # 분산 DB 레벨 전역 락 획득
            acquired_lock = MetaDbLockManager.acquire(engine_target, dialect, worker_id=worker_id)
            if not acquired_lock:
                raise RuntimeError("다른 프로세스에서 마이그레이션이 이미 실행 중입니다 (락 획득 실패).")

            heartbeat_thread = self._start_lock_heartbeat(engine_target, worker_id, heartbeat_stop)

            # 사전 안전 무결성 백업 생성
            backup_root = os.path.join(
                path_data, 'db', 'meta_db_backups',
                start_time.strftime('meta_db_%Y%m%d_%H%M%S')
            )
            os.makedirs(backup_root, exist_ok=True)
            self.status.update({
                'phase': 'backup',
                'status': '사전 무결성 백업 생성 중',
                'backup_path': backup_root
            })
            self._save_state()
            self._execute_backups(backup_root)

            # Fast DDL 단계 실행 (테이블/컬럼/인덱스 생성, 수 초 내 완료)
            self.status.update({
                'phase': 'migrate',
                'status': '스키마 구조 변경 중 (Fast DDL)'
            })
            self._save_state()
            self._execute_fast_ddl(backup_root)

            # DDL 성공 즉시 버전 승격 및 상태를 'backfilling'으로 기록
            self._update_schema_version_record(
                new_version=self.TARGET_VERSION,
                state='backfilling',
                backup_path=backup_root
            )

            # Keep the lease until backfill and final version bookkeeping finish.
            # Async Backfill 단계 실행 (대규모 레코드 정규화)
            self.status.update({
                'phase': 'person',
                'status': '인물 데이터 정규화 백필 중'
            })
            self._save_state()
            self._execute_backfill()

            if self.status.get('stop_flag') or not MetaDbLockManager.renew(engine_target, worker_id):
                raise InterruptedError('마이그레이션 잠금 또는 중단 상태를 확인하세요.')

            # 전체 완료 처리 및 감사 원장 기록
            duration = (datetime.now() - start_time).total_seconds()
            self._update_schema_version_record(
                new_version=self.TARGET_VERSION,
                state='complete',
                backup_path=backup_root
            )
            self._record_history(
                revision_id=self.revisions[0].revision_id,
                from_version=0,
                to_version=self.TARGET_VERSION,
                phase='complete',
                status='success',
                started_at=start_time,
                completed_at=datetime.now(),
                duration_sec=duration,
                backup_path=backup_root
            )

            self.status.update({
                'is_running': False,
                'status': '완료',
                'phase': 'complete',
                'can_resume': False,
                'last_error': ''
            })
            self._save_state()
            logger.info(f"[Migration] v0 -> v1 전체 마이그레이션 성공 완료 (소요시간: {duration:.1f}초)")

        except InterruptedError as exc:
            self.status.update({
                'is_running': False,
                'status': f'중단됨: {exc}',
                'phase': 'stopped',
                'can_resume': True
            })
            self._save_state()
            logger.warning(f"[Migration] 작업이 일시 중단되었습니다: {exc}")

        except Exception as exc:
            self.status.update({
                'is_running': False,
                'status': '실패',
                'phase': 'failed',
                'last_error': str(exc),
                'can_resume': True
            })
            self._save_state()
            logger.error(f"[Migration] 마이그레이션 실패: {exc}", exc_info=True)
            self._record_history(
                revision_id=self.revisions[0].revision_id,
                from_version=0,
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
        targets = self.mod._meta_db_targets()
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
        targets = self.mod._meta_db_targets()
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
        for physical_name, engine, _ in self.mod._meta_db_targets():
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

    def _update_schema_version_record(self, new_version: int, state: str, backup_path: str):
        """meta_schema_version 테이블 레코드 갱신."""
        targets = self.mod._meta_db_targets()
        for _, eng, _ in targets:
            if eng is None:
                continue
            with eng.begin() as conn:
                conn.execute(text("""
                    UPDATE meta_schema_version SET
                        schema_version = :ver,
                        migration_id = :mid,
                        migration_state = :state,
                        completed_at = :now,
                        updated_at = :now,
                        last_error = NULL,
                        backup_path = :bpath
                    WHERE schema_name = 'meta_db'
                """), {
                    'ver': new_version,
                    'mid': self.revisions[0].revision_id,
                    'state': state,
                    'now': datetime.now(),
                    'bpath': backup_path
                })

    def _record_history(self, revision_id, from_version, to_version, phase, status,
                        started_at, completed_at, duration_sec, backup_path, error_message=None):
        """meta_schema_migration_history 감사 원장에 실행 이력 영구 기록."""
        targets = self.mod._meta_db_targets()
        for _, eng, _ in targets:
            if eng is None:
                continue
            try:
                from ..mod_meta_db import MetaSchemaMigrationHistory
                MetaSchemaMigrationHistory.__table__.create(bind=eng, checkfirst=True)
                with eng.begin() as conn:
                    conn.execute(text("""
                        INSERT INTO meta_schema_migration_history
                        (revision_id, from_version, to_version, phase, status, started_at, completed_at, duration_sec, backup_path, error_message)
                        VALUES (:rid, :from_v, :to_v, :phase, :status, :start, :end, :dur, :bpath, :err)
                    """), {
                        'rid': revision_id,
                        'from_v': from_version,
                        'to_v': to_version,
                        'phase': phase,
                        'status': status,
                        'start': started_at,
                        'end': completed_at,
                        'dur': duration_sec,
                        'bpath': backup_path,
                        'err': error_message
                    })
            except Exception as e_hist:
                logger.debug(f"[Migration History] 원장 기록 예외 (무시): {e_hist}")

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
        """저장된 상태 복원."""
        if os.path.exists(self.state_file):
            saved = load_json_state(self.state_file)
            if saved and isinstance(saved, dict):
                if saved.get('is_running') and not self.maintenance:
                    saved['is_running'] = False
                    saved['status'] = "서버 재시작으로 중단됨 (재개 가능)"
                    saved['can_resume'] = True
                self.status.update(saved)
