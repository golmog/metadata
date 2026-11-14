# -*- coding: utf-8 -*-
"""DB row leases shared by SQLite and PostgreSQL workers."""
import os
import socket
import time
import uuid
from sqlalchemy import text
from ..setup import logger


class MetaDbLockManager:
    LOCK_TIMEOUT_SECONDS = 1800
    HEARTBEAT_SECONDS = 10

    @staticmethod
    def _process_identity(pid):
        try:
            with open('/proc/sys/kernel/random/boot_id') as fp:
                boot = fp.read().strip()
            with open(f'/proc/{pid}/stat') as fp:
                started = fp.read().rsplit(')', 1)[1].split()[19]
            return f'{boot}:{started}'
        except OSError:
            return None

    @classmethod
    def worker_id(cls, label):
        identity = cls._process_identity(os.getpid()) or 'unknown:unknown'
        return f'{socket.gethostname()}|{os.getpid()}|{identity}|{label}|{uuid.uuid4().hex}'

    @classmethod
    def _dead_local_owner(cls, token):
        try:
            host, pid, identity, _, _ = token.split('|', 4)
            if host != socket.gethostname():
                return False
            current = cls._process_identity(int(pid))
            if current:
                return current != identity
            try:
                os.kill(int(pid), 0)
                return False
            except ProcessLookupError:
                return True
            except PermissionError:
                return False
        except (TypeError, ValueError):
            return False

    @classmethod
    def create_schema(cls, conn):
        """Explicit installation/maintenance bootstrap, before acquiring a lease."""
        conn.execute(text('''CREATE TABLE IF NOT EXISTS meta_db_job_lock (
            lock_name VARCHAR(100) PRIMARY KEY,
            owner_token VARCHAR(500) NOT NULL,
            expires_at FLOAT NOT NULL
        )'''))
        for name in ('migration', 'transfer'):
            try:
                conn.execute(text("INSERT INTO meta_db_job_lock (lock_name, owner_token, expires_at) "
                                  "VALUES (:name, '', 0) ON CONFLICT (lock_name) DO NOTHING"), {'name': name})
            except Exception:
                try:
                    conn.execute(text("INSERT OR IGNORE INTO meta_db_job_lock (lock_name, owner_token, expires_at) "
                                      "VALUES (:name, '', 0)"), {'name': name})
                except Exception:
                    pass

    @classmethod
    def initialize(cls, engine):
        with engine.begin() as conn:
            cls.create_schema(conn)

    @classmethod
    def acquire(cls, engine, dialect: str, worker_id=None, lock_name='migration', now=None):
        if not worker_id:
            raise ValueError('A unique worker token is required')
        try:
            now_ts = time.time() if now is None else float(now)
            with engine.begin() as conn:
                cls.create_schema(conn)
                row = conn.execute(
                    text("SELECT owner_token, expires_at FROM meta_db_job_lock WHERE lock_name=:lock_name"),
                    {'lock_name': lock_name}
                ).fetchone()
                if row is None:
                    try:
                        conn.execute(
                            text("INSERT INTO meta_db_job_lock (lock_name, owner_token, expires_at) "
                                 "VALUES (:lock_name, '', 0) ON CONFLICT (lock_name) DO NOTHING"),
                            {'lock_name': lock_name}
                        )
                    except Exception:
                        conn.execute(
                            text("INSERT OR IGNORE INTO meta_db_job_lock (lock_name, owner_token, expires_at) "
                                 "VALUES (:lock_name, '', 0)"),
                            {'lock_name': lock_name}
                        )
                    row = conn.execute(
                        text("SELECT owner_token, expires_at FROM meta_db_job_lock WHERE lock_name=:lock_name"),
                        {'lock_name': lock_name}
                    ).fetchone()
                    if row is None:
                        return False
                previous = row[0]
                prev_expires = float(row[1] or 0)
                dead = cls._dead_local_owner(previous)
                if previous and prev_expires > now_ts and not dead:
                    return False
                result = conn.execute(
                    text("UPDATE meta_db_job_lock SET owner_token=:owner, expires_at=:until "
                         "WHERE lock_name=:lock_name AND owner_token=:previous "
                         "AND (owner_token='' OR expires_at<=:now OR :dead=1)"),
                    {'owner': worker_id, 'previous': previous, 'until': now_ts + cls.LOCK_TIMEOUT_SECONDS,
                     'now': now_ts, 'dead': int(dead), 'lock_name': lock_name}
                )
                return getattr(result, 'rowcount', 0) == 1
        except Exception as exc:
            logger.error(f'[MetaDB Lock] acquire failed: {exc}')
            return False

    @classmethod
    def is_locked(cls, engine, lock_name='migration', now=None):
        from sqlalchemy import inspect
        try:
            if 'meta_db_job_lock' not in inspect(engine).get_table_names():
                return False
            now_ts = time.time() if now is None else float(now)
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT owner_token, expires_at FROM meta_db_job_lock WHERE lock_name=:lock_name"),
                    {'lock_name': lock_name}
                ).fetchone()
                return bool(row and row[0] and float(row[1] or 0) > now_ts and not cls._dead_local_owner(row[0]))
        except Exception:
            return False

    @classmethod
    def renew(cls, engine, worker_id, lock_name='migration', now=None):
        if not worker_id:
            return False
        try:
            now_ts = time.time() if now is None else float(now)
            with engine.begin() as conn:
                result = conn.execute(
                    text("UPDATE meta_db_job_lock SET expires_at=:until "
                         "WHERE lock_name=:lock_name AND owner_token=:owner"),
                    {'until': now_ts + cls.LOCK_TIMEOUT_SECONDS, 'owner': worker_id, 'lock_name': lock_name}
                )
                return getattr(result, 'rowcount', 0) == 1
        except Exception as exc:
            logger.error(f'[MetaDB Lock] renew failed: {exc}')
            return False

    @classmethod
    def release(cls, engine, dialect: str, worker_id=None, lock_name='migration'):
        if not worker_id:
            return False
        try:
            with engine.begin() as conn:
                result = conn.execute(
                    text("UPDATE meta_db_job_lock SET owner_token='', expires_at=0 "
                         "WHERE lock_name=:lock_name AND owner_token=:owner"),
                    {'owner': worker_id, 'lock_name': lock_name}
                )
                return getattr(result, 'rowcount', 0) == 1
        except Exception as exc:
            logger.error(f'[MetaDB Lock] release failed: {exc}')
            return False
