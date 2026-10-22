"""Cooperative batch execution without changing FlaskFarm's worker configuration."""
import copy
import logging
import re
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar

_BATCH_DB_LIMITS = ContextVar('metadata_batch_db_limits', default=False)


class StorageLookupError(RuntimeError):
    error_code = 'storage_lookup_failed'


class IncompleteStorageError(StorageLookupError):
    error_code = 'storage_asset_incomplete'


class StorageConflict(RuntimeError):
    error_code = 'storage_conflict'


def redact_diagnostic(value):
    """Transform log copies only; never transform media references or journals."""
    try:
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, BaseException):
            return type(value).__name__
        if isinstance(value, dict):
            return {str(key): ('[redacted]' if re.search(
                r'(?i)(api.?key|authorization|cookie|password|passwd|secret|token|signature|credential|private.?key|session.?id)', str(key))
                else redact_diagnostic(item)) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return type(value)(redact_diagnostic(item) for item in value)
        text = str(value)
        text = re.sub(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s<>\'\"]+', '[url]', text)
        text = re.sub(r'(?i)\b(Bearer|Basic)\s+[^\s,;]+', r'\1 [redacted]', text)
        text = re.sub(r'(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+', r'\1[redacted]', text)
        return re.sub(r'(?i)((?:api.?key|authorization|cookie|password|passwd|secret|token|signature|credential|private.?key|session.?id)'
                      r'[\w-]*[\s\'\"]*[:=][\s\'\"]*)[^\s,;\'\"}\]]+', r'\1[redacted]', text)
    except Exception:
        return '[redaction failed]'


class StorageLogger:
    """Sanitize before dispatch, so every handler receives the same safe record."""
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def _write(self, level, message, args, kwargs):
        try:
            if not self.wrapped.isEnabledFor(level):
                return
            exc = kwargs.pop('exc_info', None)
            kwargs.pop('stack_info', None)
            if 'extra' in kwargs:
                kwargs['extra'] = redact_diagnostic(kwargs['extra'])
            if 'Traceback (most recent call last)' in str(message):
                message = '[traceback: %s]' % type(sys.exc_info()[1]).__name__
            if exc:
                info = sys.exc_info() if exc is True else exc
                error = info[1] if isinstance(info, tuple) else exc
                # Exception messages, source lines and chained messages may carry arbitrary secrets.
                kinds = []
                while error is not None and len(kinds) < 8:
                    kinds.append(type(error).__name__)
                    error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
                message = str(message) + ' [exceptions: ' + ' <- '.join(kinds) + ']'
            # Keep string fragments intact until formatting: sanitizing a URL
            # prefix alone can hide the scheme and expose its remaining token.
            safe_args = tuple(arg if isinstance(arg, str) else redact_diagnostic(arg) for arg in args)
            safe_message = str(message)
            if safe_args:
                safe_message = safe_message % safe_args
            safe_message = redact_diagnostic(safe_message)
            kwargs['stacklevel'] = int(kwargs.get('stacklevel', 1)) + 2
            self.wrapped.log(level, safe_message, **kwargs)
        except Exception:
            # Do not fall back to the original message or traceback.
            try:
                self.wrapped.log(level, '[storage diagnostic unavailable]')
            except Exception:
                pass

    def debug(self, message, *args, **kwargs): self._write(logging.DEBUG, message, args, kwargs)
    def info(self, message, *args, **kwargs): self._write(logging.INFO, message, args, kwargs)
    def warning(self, message, *args, **kwargs): self._write(logging.WARNING, message, args, kwargs)
    warn = warning
    def error(self, message, *args, **kwargs): self._write(logging.ERROR, message, args, kwargs)
    def critical(self, message, *args, **kwargs): self._write(logging.CRITICAL, message, args, kwargs)
    fatal = critical
    def log(self, level, message, *args, **kwargs): self._write(level, message, args, kwargs)
    def exception(self, message, *args, **kwargs):
        kwargs['exc_info'] = True
        self._write(logging.ERROR, message, args, kwargs)


def storage_logger(logger):
    return logger if isinstance(logger, StorageLogger) else StorageLogger(logger)


def _gevent_active():
    try:
        from gevent import monkey
        return monkey.is_module_patched('socket')
    except ImportError:
        return False


def _psycopg_wait(connection):
    # Psycopg's documented wait-callback API; no runtime dependency installation.
    from psycopg2 import extensions, OperationalError
    from gevent.socket import wait_read, wait_write
    while True:
        state = connection.poll()
        if state == extensions.POLL_OK:
            return
        if state == extensions.POLL_READ:
            wait_read(connection.fileno())
        elif state == extensions.POLL_WRITE:
            wait_write(connection.fileno())
        else:
            raise OperationalError('Unexpected PostgreSQL poll state: %s' % state)


def configure_postgres_cooperation(logger):
    if not _gevent_active():
        return False
    try:
        from psycopg2 import extensions
    except ImportError:
        return False
    existing = extensions.get_wait_callback()
    if existing is None:
        extensions.set_wait_callback(_psycopg_wait)
        logger.info('[MetaDB Runtime] gevent PostgreSQL cooperative wait callback enabled')
    elif existing is not _psycopg_wait:
        logger.debug('[MetaDB Runtime] Existing PostgreSQL wait callback preserved: %s.%s',
                     getattr(existing, '__module__', 'unknown'), getattr(existing, '__name__', type(existing).__name__))
    # A callback registered by FF or another plugin belongs to that owner.
    return True


def cooperate():
    if _gevent_active():
        from gevent import sleep
        # A positive interval also lets pending I/O/timers run in hot cache loops.
        sleep(0.001)


@contextmanager
def batch_database_limits(logger):
    configure_postgres_cooperation(logger)
    token = _BATCH_DB_LIMITS.set(True)
    try:
        yield
    finally:
        _BATCH_DB_LIMITS.reset(token)


def install_batch_timeouts(engine):
    from sqlalchemy import event

    @event.listens_for(engine, 'begin')
    def set_batch_timeouts(connection):
        if _BATCH_DB_LIMITS.get():
            # Transaction-local: pool reuse, migrations and maintenance keep their policies.
            connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            connection.exec_driver_sql("SET LOCAL statement_timeout = '60s'")


@contextmanager
def batch_stage(logger, stage, code):
    started = time.monotonic()
    logger.debug('[MetaDB Stage] [%s] %s start', code, stage)
    try:
        yield
    except Exception:
        logger.debug('[MetaDB Stage] [%s] %s failed (%.3fs)', code, stage, time.monotonic() - started)
        raise
    else:
        logger.debug('[MetaDB Stage] [%s] %s complete (%.3fs)', code, stage, time.monotonic() - started)


def _record_snapshot(record, relationships):
    def columns(obj):
        return {column.key: copy.deepcopy(getattr(obj, column.key)) for column in obj.__table__.columns}
    return (columns(record), {key: sorted((columns(child) for child in getattr(record, key)),
                                         key=lambda child: child['id']) for key in relationships})


def stage_storage_record(session, record, relationships=()):
    """Release read transactions before network/file work, retaining a conflict snapshot."""
    snapshot = _record_snapshot(record, relationships)
    session.expunge_all()
    session.rollback()
    return snapshot


def commit_storage_record(session, record, snapshot, relationships=()):
    from sqlalchemy.orm import lazyload
    from sqlalchemy import event
    session.info['storage_commit_state'] = 'not_attempted'
    model = type(record)
    # Avoid FOR UPDATE on nullable joined relationships in PostgreSQL.
    current = session.query(model).options(lazyload('*')).filter(model.id == record.id).populate_existing().with_for_update(of=model).first()
    if current is None or _record_snapshot(current, relationships) != snapshot:
        raise StorageConflict('동기화 도중 DB 정보가 변경되었습니다. 최신 정보를 확인한 뒤 다시 실행하세요.')
    session.merge(record)
    session.flush()
    def committed(sess):
        sess.info['storage_commit_state'] = 'committed'
    event.listen(session, 'after_commit', committed, once=True)
    session.info['storage_commit_state'] = 'unknown'
    session.commit()
    session.info['storage_commit_state'] = 'committed'
