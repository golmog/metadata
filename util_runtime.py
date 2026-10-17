"""Cooperative batch execution without changing FlaskFarm's worker configuration."""
import copy
import time
from contextlib import contextmanager
from contextvars import ContextVar

_BATCH_DB_LIMITS = ContextVar('metadata_batch_db_limits', default=False)


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
    model = type(record)
    # Avoid FOR UPDATE on nullable joined relationships in PostgreSQL.
    current = session.query(model).options(lazyload('*')).filter(model.id == record.id).populate_existing().with_for_update(of=model).first()
    if current is None or _record_snapshot(current, relationships) != snapshot:
        raise RuntimeError('동기화 도중 DB 정보가 변경되었습니다. 최신 정보를 확인한 뒤 다시 실행하세요.')
    session.merge(record)
    session.commit()
