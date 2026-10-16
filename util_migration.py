# -*- coding: utf-8 -*-
"""공통 메타 DB 마이그레이션 인프라.

도메인별 데이터 변환 규칙은 이 모듈에 넣지 않는다. 상태 파일의 원자적
저장과 SQLite/PostgreSQL 백업처럼 모든 마이그레이션에서 공통으로 쓰는
실행 인프라만 제공한다.
"""

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable

from sqlalchemy import MetaData, Table, inspect, text


def save_json_state(path, state):
    """마이그레이션 상태를 원자적으로 저장한다."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary_path = f"{path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, indent=2, default=str)
    os.replace(temporary_path, path)


def load_json_state(path):
    """손상되었거나 없는 상태 파일은 빈 상태로 처리한다."""
    try:
        with open(path, encoding="utf-8") as fp:
            value = json.load(fp)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


@dataclass(frozen=True)
class MigrationStep:
    """버전 업그레이드의 한 단계. 데이터 변환은 callback이 담당한다."""

    migration_id: str
    phase: str
    label: str
    callback: Callable


class MigrationPlan:
    """버전 구간별 마이그레이션 단계를 등록하는 계획 저장소."""

    def __init__(self, schema_name):
        self.schema_name = schema_name
        self._steps = {}

    def register(self, from_version, to_version, phase, label, callback):
        if to_version <= from_version:
            raise ValueError("마이그레이션 대상 버전은 시작 버전보다 커야 합니다.")
        key = (int(from_version), int(to_version))
        if key in self._steps:
            raise ValueError(f"중복 마이그레이션 단계: {key}")
        self._steps[key] = MigrationStep(
            migration_id=f"{self.schema_name}_v{from_version}_to_v{to_version}",
            phase=phase,
            label=label,
            callback=callback,
        )

    def steps_for(self, current_version, target_version):
        """현재 버전에서 대상 버전까지 순서대로 단계를 반환한다."""
        current = int(current_version)
        target = int(target_version)
        steps = []
        while current < target:
            candidates = [
                (to_version, step)
                for (from_version, to_version), step in self._steps.items()
                if from_version == current
            ]
            if not candidates:
                raise RuntimeError(
                    f"{self.schema_name}: v{current}에서 진행할 마이그레이션이 없습니다."
                )
            next_version, step = min(candidates, key=lambda item: item[0])
            if next_version > target:
                raise RuntimeError(
                    f"{self.schema_name}: v{current}에서 v{target}으로 직접 이동할 단계가 없습니다."
                )
            steps.append(step)
            current = next_version
        return steps


class MigrationRunner:
    """재사용 가능한 마이그레이션 생명주기 실행기.

    DB별 SQL/ORM 처리는 callback에 위임하고, 실행 상태·중단·재개·예외 처리는
    공통으로 관리한다. callback에는 ``context``가 전달되며 context는 현재
    단계와 공통 상태를 제공한다.
    """

    def __init__(self, state, state_path, save_state, logger=None):
        self.state = state
        self.state_path = state_path
        self.save_state = save_state
        self.logger = logger

    def start(self, steps: Iterable[MigrationStep], target, daemon=True):
        if self.state.get("is_running"):
            return False
        self.state.update({
            "is_running": True,
            "status": "시작 중",
            "phase": "starting",
            "current": 0,
            "total": 0,
            "current_db": "",
            "last_error": "",
            "stop_flag": False,
            "can_resume": False,
        })
        self.save_state()
        thread = threading.Thread(
            target=self.run,
            args=(list(steps), target),
            daemon=daemon
        )
        thread.start()
        return True

    def run(self, steps, target):
        try:
            for step in steps:
                if self.state.get("stop_flag"):
                    self.state.update({
                        "is_running": False,
                        "status": "중단됨",
                        "phase": "stopped",
                        "can_resume": True,
                    })
                    self.save_state()
                    return

                self.state.update({
                    "phase": step.phase,
                    "status": step.label,
                    "migration_id": step.migration_id,
                })
                self.save_state()
                context = {
                    "runner": self,
                    "state": self.state,
                    "step": step,
                    "target": target,
                    "now": datetime.now(),
                }
                step.callback(context)
                self.save_state()

            self.state.update({
                "is_running": False,
                "status": "완료",
                "phase": "complete",
                "can_resume": False,
                "last_error": "",
            })
            self.save_state()
        except InterruptedError as exc:
            self.state.update({
                "is_running": False,
                "status": f"중단됨: {exc}",
                "phase": "stopped",
                "can_resume": True,
            })
            self.save_state()
        except Exception as exc:
            self.state.update({
                "is_running": False,
                "status": "실패",
                "phase": "failed",
                "last_error": str(exc),
                "can_resume": True,
            })
            self.save_state()
            if self.logger:
                self.logger.error("마이그레이션 실행 실패: %s", exc, exc_info=True)


def backup_sqlite_database(source_path, backup_path, progress_callback=None, target_name=""):
    """SQLite 데이터베이스를 백업하고 무결성을 확인한다."""
    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(backup_path)
    try:
        def on_backup_progress(status, remaining, total):
            if progress_callback and total > 0:
                current_pages = total - remaining
                progress_callback(current_pages, total, "SQLite 백업 진행 중", target_name)

        source.backup(
            destination,
            pages=500,
            progress=on_backup_progress if progress_callback else None
        )
        if destination.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta_db_job_lock'").fetchone():
            destination.execute("UPDATE meta_db_job_lock SET owner_token='', expires_at=0")
            destination.commit()
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"SQLite 백업 무결성 검사 실패: {result}")
    finally:
        destination.close()
        source.close()


def restore_sqlite_database(backup_path, target_path, progress_callback=None, target_name=""):
    """백업 SQLite 파일을 대상 DB에 복원하고 무결성을 확인한다."""
    if not os.path.isfile(backup_path):
        raise FileNotFoundError(f"SQLite 백업 파일을 찾을 수 없습니다: {backup_path}")
    if os.path.abspath(backup_path) == os.path.abspath(target_path):
        raise ValueError("백업 파일과 대상 DB 경로가 같을 수 없습니다.")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    source = sqlite3.connect(backup_path)
    destination = sqlite3.connect(target_path)
    try:
        integrity = source.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"SQLite 백업 무결성 검사 실패: {integrity}")
        destination.execute("PRAGMA journal_mode=DELETE")

        def on_restore_progress(status, remaining, total):
            if progress_callback and total > 0:
                current_pages = total - remaining
                progress_callback(current_pages, total, "SQLite 복원 진행 중", target_name)

        source.backup(
            destination,
            pages=500,
            progress=on_restore_progress if progress_callback else None
        )
        destination.commit()
        restored = destination.execute("PRAGMA integrity_check").fetchone()
        if not restored or restored[0] != "ok":
            raise RuntimeError(f"SQLite 복원 후 무결성 검사 실패: {restored}")
    finally:
        destination.close()
        source.close()


def backup_postgres_database(engine, backup_path, chunk_size=500, progress_callback=None):
    """외부 pg_dump 없이 PostgreSQL을 로컬 SQLite 논리 백업으로 저장한다."""
    inspector = inspect(engine)
    tables = [name for name in inspector.get_table_names() if name != 'meta_db_job_lock']
    if progress_callback:
        progress_callback(0, len(tables), "PostgreSQL 논리 백업 준비 중", "")

    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    backup = sqlite3.connect(backup_path)
    try:
        backup.execute("PRAGMA journal_mode=DELETE")
        backup.execute("""
            CREATE TABLE IF NOT EXISTS meta_db_backup_schema (
                table_name TEXT PRIMARY KEY, columns_json TEXT NOT NULL
            )
        """)
        backup.execute("""
            CREATE TABLE IF NOT EXISTS meta_db_backup_rows (
                table_name TEXT NOT NULL, row_number INTEGER NOT NULL,
                row_json TEXT NOT NULL,
                PRIMARY KEY (table_name, row_number)
            )
        """)
        with engine.connect() as source:
            for table_index, table_name in enumerate(tables, 1):
                columns = inspector.get_columns(table_name)
                columns_json = json.dumps([
                    {
                        "name": column["name"],
                        "type": str(column["type"]),
                        "nullable": bool(column.get("nullable", True)),
                        "primary_key": bool(column.get("primary_key", False))
                    }
                    for column in columns
                ], ensure_ascii=False)
                backup.execute(
                    "INSERT OR REPLACE INTO meta_db_backup_schema "
                    "(table_name, columns_json) VALUES (?, ?)",
                    (table_name, columns_json)
                )
                escaped_name = table_name.replace('"', '""')
                result = source.execute(text(f'SELECT * FROM "{escaped_name}"'))
                row_number = 0
                while True:
                    rows = result.fetchmany(chunk_size)
                    if not rows:
                        break
                    payload = [
                        (
                            table_name,
                            row_number + offset,
                            json.dumps(dict(row._mapping), ensure_ascii=False, default=str)
                        )
                        for offset, row in enumerate(rows)
                    ]
                    row_number += len(payload)
                    backup.executemany(
                        "INSERT OR REPLACE INTO meta_db_backup_rows "
                        "(table_name, row_number, row_json) VALUES (?, ?, ?)",
                        payload
                    )
                    backup.commit()
                if progress_callback:
                    progress_callback(
                        table_index, len(tables), "PostgreSQL 논리 백업 중", table_name
                    )
        integrity = backup.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"PostgreSQL 논리 백업 무결성 검사 실패: {integrity}")
    finally:
        backup.close()


def restore_postgres_database(engine, backup_path, chunk_size=500, progress_callback=None):
    """SQLite 논리 백업을 기존 PostgreSQL 스키마에 원자적으로 복원한다."""
    if not os.path.isfile(backup_path):
        raise FileNotFoundError(f"PostgreSQL 논리 백업을 찾을 수 없습니다: {backup_path}")

    backup = sqlite3.connect(backup_path)
    try:
        integrity = backup.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"논리 백업 무결성 검사 실패: {integrity}")
        schema_rows = backup.execute(
            "SELECT table_name, columns_json FROM meta_db_backup_schema ORDER BY table_name"
        ).fetchall()
        if not schema_rows:
            raise RuntimeError("논리 백업에 테이블 스키마가 없습니다.")

        inspector = inspect(engine)
        target_tables = set(inspector.get_table_names())
        backup_tables = [row[0] for row in schema_rows]
        missing = sorted(set(backup_tables) - target_tables)
        if missing:
            raise RuntimeError(
                f"PostgreSQL 복원 대상 테이블이 없습니다: {', '.join(missing)}"
            )

        dependencies = {table_name: set() for table_name in backup_tables}
        for table_name in backup_tables:
            for foreign_key in inspector.get_foreign_keys(table_name):
                referred = foreign_key.get("referred_table")
                if referred in dependencies and referred != table_name:
                    dependencies[table_name].add(referred)

        def dependency_order():
            pending = {name: set(values) for name, values in dependencies.items()}
            ordered = []
            while pending:
                ready = sorted(name for name, values in pending.items() if not values)
                if not ready:
                    raise RuntimeError("PostgreSQL 테이블 외래키 순환으로 복원 순서를 결정할 수 없습니다.")
                ordered.extend(ready)
                for name in ready:
                    pending.pop(name)
                for values in pending.values():
                    values.difference_update(ready)
            return ordered

        ordered = dependency_order()
        metadata = MetaData()
        reflected = {
            name: Table(name, metadata, autoload_with=engine)
            for name in backup_tables
        }
        total_rows = backup.execute(
            "SELECT COUNT(*) FROM meta_db_backup_rows"
        ).fetchone()[0]
        if progress_callback:
            progress_callback(0, total_rows, "PostgreSQL 복원 준비 중", "")

        with engine.begin() as connection:
            for table_name in reversed(ordered):
                connection.execute(reflected[table_name].delete())

            current = 0
            for table_name in ordered:
                table = reflected[table_name]
                column_names = {column.name for column in table.columns}
                rows = backup.execute(
                    "SELECT row_json FROM meta_db_backup_rows "
                    "WHERE table_name = ? ORDER BY row_number",
                    (table_name,)
                )
                batch = []
                for (row_json,) in rows:
                    raw = json.loads(row_json)
                    values = {
                        key: value for key, value in raw.items()
                        if key in column_names
                    }
                    batch.append(values)
                    if len(batch) >= chunk_size:
                        connection.execute(table.insert(), batch)
                        current += len(batch)
                        batch = []
                        if progress_callback:
                            progress_callback(current, total_rows, "PostgreSQL 데이터 복원 중", table_name)
                if batch:
                    connection.execute(table.insert(), batch)
                    current += len(batch)
                if progress_callback:
                    progress_callback(current, total_rows, "PostgreSQL 테이블 복원 완료", table_name)

            for table_name in ordered:
                primary_keys = [
                    column for column in reflected[table_name].primary_key.columns
                    if column.name == "id"
                ]
                if len(primary_keys) != 1:
                    continue
                sequence = connection.execute(
                    text("SELECT pg_get_serial_sequence(:table_name, :column_name)"),
                    {"table_name": table_name, "column_name": "id"}
                ).scalar()
                if sequence:
                    escaped_name = table_name.replace('"', '""')
                    connection.execute(
                        text(
                            "SELECT setval(:sequence_name, "
                            "COALESCE((SELECT MAX(\"id\") FROM \"" + escaped_name + "\"), 1), "
                            "(SELECT COUNT(*) > 0 FROM \"" + escaped_name + "\"))"
                        ),
                        {"sequence_name": sequence}
                    )
    finally:
        backup.close()
