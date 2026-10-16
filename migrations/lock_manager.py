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
    def acquire(cls, engine, dialect: str, worker_id=None):
        if not worker_id:
            raise ValueError('A unique worker token is required')
        try:
            with engine.begin() as conn:
                conn.execute(text('''CREATE TABLE IF NOT EXISTS meta_db_job_lock (
                    lock_name VARCHAR(100) PRIMARY KEY,
                    owner_token VARCHAR(500) NOT NULL,
                    expires_at FLOAT NOT NULL
                )'''))
                conn.execute(text("INSERT INTO meta_db_job_lock (lock_name, owner_token, expires_at) "
                                  "VALUES ('migration', '', 0) ON CONFLICT (lock_name) DO NOTHING"))
                row = conn.execute(text("SELECT owner_token FROM meta_db_job_lock WHERE lock_name='migration'")).fetchone()
                previous = row[0]
                dead = cls._dead_local_owner(previous)
                result = conn.execute(text("UPDATE meta_db_job_lock SET owner_token=:owner, expires_at=:until "
                                           "WHERE lock_name='migration' AND owner_token=:previous "
                                           "AND (owner_token='' OR expires_at<=:now OR :dead=1)"),
                                      {'owner': worker_id, 'previous': previous, 'until': time.time() + cls.LOCK_TIMEOUT_SECONDS,
                                       'now': time.time(), 'dead': int(dead)})
                return result.rowcount == 1
        except Exception as exc:
            logger.error(f'[MetaDB Lock] acquire failed: {exc}')
            return False

    @classmethod
    def is_locked(cls, engine):
        from sqlalchemy import inspect
        if 'meta_db_job_lock' not in inspect(engine).get_table_names():
            return False
        with engine.connect() as conn:
            row = conn.execute(text("SELECT owner_token, expires_at FROM meta_db_job_lock WHERE lock_name='migration'")).fetchone()
            return bool(row and row[0] and row[1] > time.time() and not cls._dead_local_owner(row[0]))

    @classmethod
    def renew(cls, engine, worker_id):
        with engine.begin() as conn:
            result = conn.execute(text("UPDATE meta_db_job_lock SET expires_at=:until "
                                       "WHERE lock_name='migration' AND owner_token=:owner"),
                                  {'until': time.time() + cls.LOCK_TIMEOUT_SECONDS, 'owner': worker_id})
            return result.rowcount == 1

    @classmethod
    def release(cls, engine, dialect: str, worker_id=None):
        if not worker_id:
            return False
        try:
            with engine.begin() as conn:
                result = conn.execute(text("UPDATE meta_db_job_lock SET owner_token='', expires_at=0 "
                                           "WHERE lock_name='migration' AND owner_token=:owner"), {'owner': worker_id})
                return result.rowcount == 1
        except Exception as exc:
            logger.error(f'[MetaDB Lock] release failed: {exc}')
            return False
