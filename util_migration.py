# -*- coding: utf-8 -*-
"""공통 메타 DB 마이그레이션, 백업/복원 및 복제 인프라.

도메인별 데이터 변환 규칙은 이 모듈에 넣지 않는다. 상태 파일의 원자적
저장과 SQLite/PostgreSQL 백업·복원·복제를 위한 실행 인프라를 제공한다.
"""

import copy
import json
import tempfile
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable
from types import SimpleNamespace

from sqlalchemy import MetaData, Table, inspect, text, and_, func, select


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
        conn_opts = {}
        if getattr(getattr(engine, 'dialect', None), 'name', '') == 'postgresql':
            conn_opts["isolation_level"] = "REPEATABLE READ"
        with engine.connect().execution_options(**conn_opts) as source:
            with source.begin():
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

    try:
        backup = sqlite3.connect(backup_path)
    except Exception as exc:
        raise RuntimeError(f"논리 백업 파일 열기 실패: {exc}")
    try:
        try:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
        except Exception as exc:
            raise RuntimeError(f"논리 백업 무결성 검사 실패: {exc}")
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
                    if table_name == 'meta_item' and 'actor_search_text' in column_names:
                        from .mod_meta_db import MetaItem
                        extra_val = values.get('extra_info')
                        if isinstance(extra_val, str):
                            try:
                                extra_val = json.loads(extra_val)
                            except Exception:
                                extra_val = {}
                        orig_val = values.get('original')
                        if isinstance(orig_val, str):
                            try:
                                orig_val = json.loads(orig_val)
                            except Exception:
                                orig_val = {}
                        values['actor_search_text'] = MetaItem.build_actor_search_text(
                            extra_val, orig_val)
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


class MetadataCopier:
    PAGE_SIZE = 500
    # Bound relation memory independently of the 500-row commit/savepoint page.
    RELATION_PREFETCH_SIZE = 96

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
        # Reuse only mappings from committed tag pages; never cache a row that
        # could be rolled back by an item-level savepoint.
        self._committed_tag_ids = {}
        self.status.update(current=0, total=0, inserted=0, updated=0, skipped=0, fail=0, ambiguous=0,
                           conflicts=0, target_newer_skipped=0, is_partial=False)

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

    @staticmethod
    def _rows_by_item(session, table, item_ids):
        """Read an item's actual relation rows without rebuilding metadata."""
        if not item_ids:
            return {}
        result = {}
        for row in session.execute(select(table).where(table.c.item_id.in_(item_ids))).mappings():
            result.setdefault(row['item_id'], []).append(dict(row))
        return result

    def _prefetch_item_batch(self, origin, source, rows):
        """Bounded read-only snapshot for a copy microbatch.

        A single failed prefetch falls back to the existing per-item code path;
        commits and error isolation remain on the original 500-item boundary.
        """
        groups = {}
        for row in rows:
            target = self.target(row['category'])
            group = groups.setdefault(id(target), dict(session=target, codes=set(), rows=[]))
            group['codes'].add(row['code'])
            group['rows'].append(row)

        eligible_source_ids = []
        for group in groups.values():
            target = group['session']
            existing = target.execute(select(self.Item).where(
                self.Item.c.code.in_(group['codes']))).mappings().all()
            group['items'] = {row['code']: dict(row) for row in existing}
            active_ids = set()
            for row in group['rows']:
                old = group['items'].get(row['code'])
                if old and self.mode == 'missing':
                    continue
                src_dt = self._parse_dt(row.get('updated_time'))
                dst_dt = self._parse_dt(old.get('updated_time')) if old else None
                if old and src_dt and dst_dt and dst_dt > src_dt:
                    continue
                eligible_source_ids.append(row['id'])
                if old:
                    active_ids.add(old['id'])
            group['children'] = {
                table.name: self._rows_by_item(target, table, active_ids)
                for table in (self.Media, self.Fingerprint, self.TagMap, self.PersonMap)
            }

        source_children = {
            table.name: self._rows_by_item(source, table, eligible_source_ids)
            for table in (self.Media, self.Fingerprint, self.TagMap, self.PersonMap)
        }
        person_ids = {
            child['person_id'] for group in groups.values()
            for children in group['children'][self.PersonMap.name].values()
            for child in children
        }
        # The committed person mapping is authoritative. Never infer a person
        # from a title or other metadata while copying relationships.
        for children in source_children[self.PersonMap.name].values():
            for child in children:
                found, pid = self.mapped(origin, 'person', child['person_id'], self.person_target)
                if not found:
                    possible = {r[0] for r in self.index.execute(
                        'SELECT DISTINCT new_id FROM mapping WHERE kind=? AND old_id=? AND dest=? AND new_id IS NOT NULL',
                        ('person', child['person_id'], id(self.person_target)))}
                    pid = next(iter(possible)) if len(possible) == 1 else None
                if pid is not None:
                    person_ids.add(pid)
        people = {}
        if person_ids:
            columns = (self.Person.c.id, self.Person.c.domain, self.Person.c.person_idx,
                       self.Person.c.name_org, self.Person.c.name_ko)
            for row in self.person_target.execute(select(*columns).where(
                    self.Person.c.id.in_(person_ids))).mappings():
                people[row['id']] = dict(row)
        return dict(source=source_children, targets=groups, people=people)

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
    def _has_changed_values(existing, incoming):
        """Avoid redundant UPDATEs without skipping child relationship copying."""
        return any(existing.get(key) != value for key, value in incoming.items())

    @staticmethod
    def _parse_dt(val):
        if isinstance(val, datetime):
            return val
        if val:
            try:
                return datetime.fromisoformat(str(val))
            except Exception:
                pass
        return None

    @staticmethod
    def _actor_matches_override(actor, override_entry, domain=None):
        if not actor or not override_entry:
            return False
        if isinstance(actor, str):
            a_name_org = actor.strip()
            a_idx = ''
            a_name_ko = ''
        elif isinstance(actor, dict):
            a_name_org = (actor.get('name_org') or actor.get('name') or '').strip()
            a_idx = str(actor.get('actor_idx') or actor.get('person_idx') or '').strip()
            a_name_ko = (actor.get('name_ko') or '').strip()
        else:
            return False

        o_idx = str(override_entry.get('actor_idx') or override_entry.get('person_idx') or '').strip()
        o_name_org = str(override_entry.get('name_org') or override_entry.get('name') or '').strip()
        o_name_ko = str(override_entry.get('name_ko') or '').strip()
        o_domain = str(override_entry.get('domain') or '').strip().upper()

        if domain and o_domain and str(domain).strip().upper() != o_domain:
            return False

        if a_idx and o_idx:
            return a_idx.lower() == o_idx.lower()

        if o_name_org and a_name_org:
            if o_name_org == a_name_org:
                return True

        if not o_name_org and o_name_ko and a_name_ko and o_name_ko == a_name_ko:
            return True

        return False

    @classmethod
    def merge_actor_manual_overrides(cls, src_ovr, dst_ovr):
        if not isinstance(src_ovr, dict):
            src_ovr = {}
        if not isinstance(dst_ovr, dict):
            dst_ovr = {}

        src_inc = list(src_ovr.get('included') or [])
        src_exc = list(src_ovr.get('excluded') or [])
        dst_inc = list(dst_ovr.get('included') or [])
        dst_exc = list(dst_ovr.get('excluded') or [])

        merged_exc = list(dst_exc)
        for item in src_exc:
            if not any(cls._actor_matches_override(item, e) for e in merged_exc):
                merged_exc.append(copy.deepcopy(item))

        merged_inc = []
        for item in dst_inc:
            if not any(cls._actor_matches_override(item, e) for e in merged_exc):
                merged_inc.append(copy.deepcopy(item))
        for item in src_inc:
            if not any(cls._actor_matches_override(item, e) for e in merged_exc):
                if not any(cls._actor_matches_override(item, inc) for inc in merged_inc):
                    merged_inc.append(copy.deepcopy(item))

        res = {}
        if merged_inc:
            res['included'] = merged_inc
        if merged_exc:
            res['excluded'] = merged_exc
        return res

    @classmethod
    def _is_person_excluded(cls, person_row, excluded_list):
        if not excluded_list or not person_row:
            return False
        p_dict = {
            'actor_idx': person_row.get('person_idx'),
            'person_idx': person_row.get('person_idx'),
            'name_org': person_row.get('name_org'),
            'name_ko': person_row.get('name_ko'),
            'domain': person_row.get('domain'),
        }
        for exc in excluded_list:
            if cls._actor_matches_override(p_dict, exc, domain=person_row.get('domain')):
                return True
        return False

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
        # 4. Custom image protection and conflict resolution
        custom = ('custom_gdrive_fileid', 'custom_local_path', 'custom_remote_path')
        src_has_custom = any(bool(source.get(k)) for k in custom)
        dst_has_custom = any(bool(target.get(k)) for k in custom)
        if dst_has_custom and not src_has_custom:
            for k in custom:
                values[k] = target.get(k)
        elif src_has_custom and not dst_has_custom:
            for k in custom:
                values[k] = source.get(k)
        elif src_has_custom and dst_has_custom:
            is_identical = all(source.get(k) == target.get(k) for k in custom)
            if not is_identical:
                # Both claim different valid custom images -> retain destination manual image, flag conflict
                self.status['conflicts'] = self.status.get('conflicts', 0) + 1
                self.log(f"인물 커스텀 이미지 상이 (충돌 보류): {source.get('name_org')} "
                         f"(목적지={target.get('custom_gdrive_fileid') or target.get('custom_local_path')}, "
                         f"소스={source.get('custom_gdrive_fileid') or source.get('custom_local_path')})")
                for k in custom:
                    values[k] = target.get(k)
            else:
                for k in custom:
                    values[k] = target.get(k)

        for key in ('official_cached_gdrive_fileid', 'official_cached_local_path', 'official_remote_path'):
            if not values.get(key) and target.get(key):
                values[key] = target[key]

        # Consistently maintain works_count
        src_wc = int(source.get('works_count') or 0)
        dst_wc = int(target.get('works_count') or 0)
        values['works_count'] = max(src_wc, dst_wc)
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
                                    src_dt = self._parse_dt(row.get('updated_time'))
                                    dst_dt = self._parse_dt(old.get('updated_time'))
                                    if src_dt and dst_dt and dst_dt > src_dt:
                                        action = 'skipped'
                                        self.status['target_newer_skipped'] = self.status.get('target_newer_skipped', 0) + 1
                                        self.log(f"인물 목적지 최신 보존 (건너뜀): {row.get('name_ko') or row['name_org']} (소스={src_dt}, 목적지={dst_dt})")
                                    else:
                                        values = self.merge_person(values, old)
                                        if self._has_changed_values(old, values):
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
                committed_page = []
                for row in rows:
                    if self.stopped():
                        break
                    category = 'WESTERN' if str(row['domain']).upper() in ('WESTERN', 'WEST') else str(row['domain']).upper()
                    if category not in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN', 'MOVIE', 'KTV', 'FTV'):
                        category = 'JAV_CEN'
                    target = self.target(category)
                    try:
                        with target.begin_nested():
                            mapped_id = self.copy_tag(source, row['id'], target)
                        committed_page.append(((id(source), row['id'], id(target)), mapped_id))
                    except Exception as exc:
                        self.status['fail'] += 1
                        self.log(f"태그 복제 실패: {row['name_org']} ({exc})")
                for target in self.targets.values():
                    target.commit()
                # Only populate the fast lookup after all target commits succeed.
                self._committed_tag_ids.update(committed_page)

    def copy_tag(self, source, old_id, target):
        row = source.execute(select(self.Tag).where(self.Tag.c.id == old_id)).mappings().one_or_none()
        if row is None:
            raise ValueError(f'Missing source tag {old_id}')
        condition = and_(self.Tag.c.domain == row['domain'], self.Tag.c.name_org == row['name_org'], self.Tag.c.tag_type == row['tag_type'])
        old = target.execute(select(self.Tag).where(condition)).mappings().one_or_none()
        if old:
            if self.mode == 'merge':
                values = self.values(row)
                if self._has_changed_values(old, values):
                    target.execute(self.Tag.update().where(self.Tag.c.id == old['id']).values(**values))
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

    def copy_item(self, origin, source, row, prefetched=None):
        target = self.target(row['category'])
        group = prefetched['targets'][id(target)] if prefetched is not None else None
        old = (group['items'].get(row['code']) if group is not None else
               target.execute(select(self.Item).where(self.Item.c.code == row['code'])).mappings().one_or_none())

        def source_children(table):
            if prefetched is not None:
                return prefetched['source'][table.name].get(row['id'], ())
            return source.execute(select(table).where(table.c.item_id == row['id'])).mappings()

        def destination_children(table, item_id):
            if prefetched is not None:
                return group['children'][table.name].get(item_id, ())
            return target.execute(select(table).where(table.c.item_id == item_id)).mappings()

        def person_row(person_id):
            if prefetched is not None and person_id in prefetched['people']:
                return prefetched['people'][person_id]
            return self.person_target.execute(select(self.Person).where(
                self.Person.c.id == person_id)).mappings().one_or_none()
        if old and old['category'] != row['category']:
            raise ValueError('동일 코드가 다른 카테고리에 존재합니다')
        if old and self.mode == 'clean':
            raise ValueError('소스에 중복 작품 코드가 있습니다')

        # 'code' is the unique item identifier. Different codes may share the
        # same display-only ui_code and must be copied as independent items.

        if old and self.mode == 'missing':
            self.remember(origin, 'item', row['id'], target, old['id'])
            return 'skipped'

        # 1. Check if destination is newer than source
        src_dt = self._parse_dt(row.get('updated_time'))
        dst_dt = self._parse_dt(old.get('updated_time')) if old else None
        target_is_newer = bool(old and src_dt and dst_dt and dst_dt > src_dt)
        if target_is_newer:
            self.remember(origin, 'item', row['id'], target, old['id'])
            self.status['target_newer_skipped'] = self.status.get('target_newer_skipped', 0) + 1
            # run() counts the returned 'skipped' action; never count twice.
            self.log(f"작품 목적지 최신 보존 (건너뜀): {row['code']} (소스={src_dt}, 목적지={dst_dt})")
            return 'skipped'

        values = self.clean_item(row) if self.sanitized else self.values(row)
        if row['category'] in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN'):
            values['extra_info'] = copy.deepcopy(values.get('extra_info') or {})
            values['extra_info'].pop('source_video_path', None)
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
                    extra['manual_data'] = {**(extra.get('manual_data') or {}), **copy.deepcopy(manual)}

                # 2. Protect and merge _actor_manual_overrides
                src_overrides = extra.get('_actor_manual_overrides') or {}
                dst_overrides = old_extra.get('_actor_manual_overrides') or {}
                if src_overrides or dst_overrides:
                    extra['_actor_manual_overrides'] = self.merge_actor_manual_overrides(src_overrides, dst_overrides)

                # A missing source cache must not erase a recorded destination video.
                for key in ('preview_clip', 'trailer_video'):
                    if key not in extra and key in old_extra:
                        extra[key] = copy.deepcopy(old_extra[key])
                values['extra_info'] = extra
                from .mod_meta_db import MetaItem
                values['actor_search_text'] = MetaItem.build_actor_search_text(values.get('extra_info'), values.get('original'))
                if self._has_changed_values(old, values):
                    target.execute(self.Item.update().where(self.Item.c.id == target_id).values(**values))
            else:
                from .mod_meta_db import MetaItem
                values['actor_search_text'] = MetaItem.build_actor_search_text(values.get('extra_info'), values.get('original'))
                target_id = target.execute(self.Item.insert().values(parent_id=None, **values)).inserted_primary_key[0]
            from .util_metadata import MetaUtil
            def user_asset(media):
                return MetaUtil.is_user_media(media) if row.get('category') in ('JAV_CEN', 'JAV_UNCEN', 'WESTERN') else bool(media.get('is_user'))
            source_media = [dict(m) for m in source_children(self.Media)]
            existing_media = [dict(m) for m in destination_children(self.Media, target_id)] if old else []
            protected = [m for m in existing_media if user_asset(m)]
            for media in protected:
                if not media.get('is_user'):
                    target.execute(self.Media.update().where(self.Media.c.id == media['id']).values(is_user=True, source='user'))
                    media.update(is_user=True, source='user')
            # Compare only the recorded media slots. Repeated merges should not
            # DELETE+INSERT an unchanged row (or allocate a new media ID).
            def media_slot(media):
                return (media['media_type'], bool(media.get('gdrive_fileid')),
                        media.get('sort_order'))

            source_slots = {media_slot(m) for m in source_media}
            available_official = {}
            for media in existing_media:
                if media not in protected:
                    available_official.setdefault(media_slot(media), []).append(media)
            matched_official = set()
            media_to_insert = []
            if not self.sanitized:
                for media in source_media:
                    if user_asset(media) and any(
                            m['media_type'] == media['media_type'] and
                            bool(m.get('gdrive_fileid')) == bool(media.get('gdrive_fileid')) and
                            (media['media_type'] != 'fanart' or m.get('sort_order') == media.get('sort_order'))
                            for m in protected):
                        continue
                    candidate = self.values(media)
                    candidate['item_id'] = target_id
                    if user_asset(media):
                        candidate.update(is_user=True, source='user')
                    if any(m['url'] == media['url'] and m['media_type'] == media['media_type']
                           for m in protected):
                        continue
                    # An official slot may be updated in place; a user slot
                    # must never overwrite a destination official row.
                    available = available_official.get(media_slot(media), ())
                    if not user_asset(media) and available:
                        previous = available.pop(0)
                        matched_official.add(previous['id'])
                        if self._has_changed_values(previous, candidate):
                            target.execute(self.Media.update().where(
                                self.Media.c.id == previous['id']).values(**candidate))
                    else:
                        media_to_insert.append(candidate)
            # Preserve different storage kinds and unmatched destination slots.
            # Remove only obsolete officials in a source-supplied slot.
            media_to_delete = [media['id'] for media in existing_media
                if media not in protected and media_slot(media) in source_slots
                and media['id'] not in matched_official]
            if media_to_delete:
                target.execute(self.Media.delete().where(self.Media.c.id.in_(media_to_delete)))
            if media_to_insert:
                target.execute(self.Media.insert(), media_to_insert)
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
                # The main row has already been reconciled to `values` above.
                # A protected poster/landscape only needs a second UPDATE when
                # that reconciliation would actually replace the protected URL.
                if updates and self._has_changed_values(values, updates):
                    target.execute(self.Item.update().where(self.Item.c.id == target_id).values(**updates))

            # Fingerprint & TagMap handling
            source_fingerprints = list(source_children(self.Fingerprint))
            source_tag_maps = list(source_children(self.TagMap))

            # 1. Fingerprint handling
            if old and not source_fingerprints:
                self.log(f"지문 보존 (소스 0건, 목적지 유지): {row['code']}")
            elif not old:
                incoming_fingerprints = []
                for child in source_fingerprints:
                    values_child = self.values(child)
                    values_child['item_id'] = target_id
                    incoming_fingerprints.append(values_child)
                if incoming_fingerprints:
                    target.execute(self.Fingerprint.insert(), incoming_fingerprints)
            else:
                existing_fps = list(destination_children(self.Fingerprint, target_id))
                existing_by_algo_hash = {(fp['algorithm'], fp['hash_value']): fp for fp in existing_fps}
                existing_official_by_algo = {fp['algorithm']: fp for fp in existing_fps if fp.get('source') != 'user'}

                incoming_fingerprints = []
                for child in source_fingerprints:
                    algo = child['algorithm']
                    hash_val = child['hash_value']
                    is_user = (child.get('source') == 'user')

                    if (algo, hash_val) in existing_by_algo_hash:
                        # Identical algorithm and hash -> idempotent, already in destination
                        continue

                    if not is_user:
                        if algo in existing_official_by_algo:
                            # Conflict: destination already has a different official fingerprint for this algorithm!
                            dst_fp = existing_official_by_algo[algo]
                            self.status['conflicts'] = self.status.get('conflicts', 0) + 1
                            self.log(f"공식 지문 상이 (충돌 보류): {row['code']} algo={algo} (목적지={dst_fp['hash_value']}, 소스={hash_val})")
                            # Retain destination value, do NOT overwrite or unconditionally union
                            continue
                        else:
                            # Destination has no official fingerprint for this algorithm -> safe to add
                            values_child = self.values(child)
                            values_child['item_id'] = target_id
                            incoming_fingerprints.append(values_child)
                            existing_by_algo_hash[(algo, hash_val)] = values_child
                            existing_official_by_algo[algo] = values_child
                    else:
                        # User fingerprint incoming -> add alongside if not duplicate
                        values_child = self.values(child)
                        values_child['item_id'] = target_id
                        incoming_fingerprints.append(values_child)
                        existing_by_algo_hash[(algo, hash_val)] = values_child
                if incoming_fingerprints:
                    target.execute(self.Fingerprint.insert(), incoming_fingerprints)

            # 2. TagMap handling
            if old and not source_tag_maps:
                # This is an expected preservation case, not an item-level error.
                # Summarize at page boundaries instead of flooding the log.
                self.status['tag_relations_preserved'] = self.status.get('tag_relations_preserved', 0) + 1
            else:
                existing_tag_ids = {m['tag_id'] for m in destination_children(self.TagMap, target_id)} if old else set()
                incoming_tags = []
                for child in source_tag_maps:
                    tag_key = (id(source), child['tag_id'], id(target))
                    mapped_tag_id = self._committed_tag_ids.get(tag_key)
                    if mapped_tag_id is None:
                        mapped_tag_id = self.copy_tag(source, child['tag_id'], target)
                    if mapped_tag_id not in existing_tag_ids:
                        values_child = self.values(child)
                        values_child['item_id'] = target_id
                        values_child['tag_id'] = mapped_tag_id
                        incoming_tags.append(values_child)
                        existing_tag_ids.add(mapped_tag_id)
                if incoming_tags:
                    target.execute(self.TagMap.insert(), incoming_tags)

            # 3. Person relationship protection
            source_person_maps = list(source_children(self.PersonMap))
            existing_person_maps = list(destination_children(self.PersonMap, target_id)) if old else []

            overrides = (values.get('extra_info') or {}).get('_actor_manual_overrides') or {}
            excluded_actors = list(overrides.get('excluded') or [])

            if old and not source_person_maps and existing_person_maps:
                # Source has 0 relations while destination has relations -> keep destination unless explicit tombstone
                for e_map in existing_person_maps:
                    p_row = person_row(e_map['person_id'])
                    if p_row and self._is_person_excluded(p_row, excluded_actors):
                        target.execute(self.PersonMap.delete().where(self.PersonMap.c.id == e_map['id']))
                self.log(f"인물 관계 보존 (소스 0건, 목적지 {len(existing_person_maps)}건 유지): {row['code']}")
            else:
                mapped_incoming = []
                for child in source_person_maps:
                    found, person_id = self.mapped(origin, 'person', child['person_id'], self.person_target)
                    if not found:
                        candidates = {r[0] for r in self.index.execute(
                            'SELECT DISTINCT new_id FROM mapping WHERE kind=? AND old_id=? AND dest=? AND new_id IS NOT NULL',
                            ('person', child['person_id'], id(self.person_target)))}
                        person_id = next(iter(candidates)) if len(candidates) == 1 else None
                    if person_id is None:
                        self.log(f"인물 연결 제외: {row['code']} / source person {child['person_id']}")
                        continue
                    mapped_incoming.append((person_id, child))

                if old:
                    # Remove relations that match explicit exclusion tombstones
                    for e_map in existing_person_maps:
                        p_row = person_row(e_map['person_id'])
                        if p_row and self._is_person_excluded(p_row, excluded_actors):
                            target.execute(self.PersonMap.delete().where(self.PersonMap.c.id == e_map['id']))

                    if not mapped_incoming and existing_person_maps:
                        self.log(f"인물 관계 보존 (소스 매핑 불가, 목적지 관계 유지): {row['code']}")

                # The original rows were preloaded before this item's savepoint.
                # Excluded tombstones must not block fresh authorized relations.
                existing_pids = ({m['person_id'] for m in target.execute(
                    select(self.PersonMap).where(self.PersonMap.c.item_id == target_id)).mappings()}
                    if old and excluded_actors else {m['person_id'] for m in existing_person_maps})
                incoming_people = []
                for person_id, child in mapped_incoming:
                    if person_id in existing_pids:
                        continue
                    p_row = person_row(person_id)
                    if p_row and self._is_person_excluded(p_row, excluded_actors):
                        continue
                    values_child = self.values(child)
                    values_child['item_id'] = target_id
                    values_child['person_id'] = person_id
                    incoming_people.append(values_child)
                    existing_pids.add(person_id)
                if incoming_people:
                    target.execute(self.PersonMap.insert(), incoming_people)
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
                for offset in range(0, len(rows), self.RELATION_PREFETCH_SIZE):
                    if self.stopped():
                        break
                    batch = rows[offset:offset + self.RELATION_PREFETCH_SIZE]
                    try:
                        prefetched = self._prefetch_item_batch(origin, source, batch)
                        self.status['prefetched_batches'] = self.status.get('prefetched_batches', 0) + 1
                    except Exception as exc:
                        # Keep historical per-item failure isolation when read-ahead
                        # is unavailable. Do not skip copying a whole batch.
                        prefetched = None
                        self.status['prefetch_fallbacks'] = self.status.get('prefetch_fallbacks', 0) + 1
                        self.log(f"관계 일괄조회 실패, 개별 조회로 복귀: {type(exc).__name__}: {exc}")
                    for row in batch:
                        if self.stopped():
                            break
                        self.status['current'] += 1
                        self.status['current_code'] = row['code']
                        try:
                            action = self.copy_item(origin, source, row, prefetched=prefetched)
                            self.status[action] += 1
                        except Exception as exc:
                            self.status['fail'] += 1
                            self.log(f"작품 복제 실패: {row['code']} ({exc})")
                for target in self.targets.values():
                    target.commit()
                self.index.commit()
                self.log(f"DB 처리: {self.status['current']:,}/{self.status['total']:,} | 신규 {self.status['inserted']:,}, 갱신 {self.status['updated']:,}, 건너뜀 {self.status['skipped']:,}, 실패 {self.status['fail']:,}, 태그관계유지 {self.status.get('tag_relations_preserved', 0):,}")
        for origin, old_id, parent_id, destination, new_id in self.index.execute('SELECT * FROM parent'):
            target = self.targets[destination]
            found, mapped_parent = self.mapped(origin, 'item', parent_id, target)
            if found and mapped_parent:
                target.execute(self.Item.update().where(self.Item.c.id == new_id).values(parent_id=mapped_parent))
            else:
                self.log(f'부모 연결 제외: source item {old_id} / parent {parent_id}')
        for target in self.targets.values():
            target.commit()
        is_success = not self.stopped() and not self.status['fail']
        if not is_success and (self.status.get('inserted', 0) > 0 or self.status.get('updated', 0) > 0):
            self.status['is_partial'] = True
        return is_success
