"""DB-atomic deletion intents and explicit same-request post-commit cleanup.

The v1 table stays separate from ORM Base. Old receipts never gain authority;
physical cleanup needs the originating live request proof and confirmed commit.
"""
import base64
import binascii
import hashlib
import re
import json
import os
import threading
import time
import uuid
from datetime import datetime

from sqlalchemy import (Column, DateTime, Index, Integer, MetaData, String, Table,
                        Text, UniqueConstraint, event, func, inspect, select, text)


class DeleteReceiptError(RuntimeError):
    pass


class _DeleteRequest:
    """Ephemeral proof, not persisted or returned to clients; no worker registry."""
    def __init__(self, engine, category, registry, stamp):
        self.engine, self.category, self.registry = engine, category, registry
        self.session = registry() if hasattr(registry, 'registry') else registry
        self.pid, self.stamp = os.getpid(), stamp
        self.nonce = uuid.uuid4().hex
        self.op_id, self.transaction = None, None
        self.digests = {}
        self.committed = self.revoked = self.busy = False
        self.executing_asset = None
        self.manual = False

    @property
    def contract(self):
        return dict(kind='d02f3_same_request_v1', nonce_hash=DeleteReceipts.digest(self.nonce),
                    database_stamp=self.stamp)

    def bind(self):
        self.transaction = self.session.get_transaction()
        def committed(session):
            if session.get_nested_transaction() is None and session.get_transaction() is self.transaction:
                self.committed = True
        def rolled_back(session, previous):
            if previous is self.transaction:
                self.revoked = True
        event.listen(self.session, 'after_commit', committed)
        event.listen(self.session, 'after_soft_rollback', rolled_back)


class DeleteReceipts:
    # Process-local duplicate-click protection, never a lock on metadata writers.
    _active_operations = set()
    _active_lock = threading.Lock()
    attempt_grace_seconds = 300
    metadata = MetaData()
    table = Table(
        'meta_asset_delete_receipt', metadata,
        Column('receipt_id', String(32), primary_key=True),
        Column('op_id', String(32), nullable=False),
        Column('asset_key', String(64), nullable=False),
        Column('category', String(30), nullable=False),
        Column('request_type', String(30), nullable=False),
        Column('format_version', Integer, nullable=False),
        Column('payload', Text, nullable=False),
        Column('payload_digest', String(64), nullable=False),
        Column('state', String(32), nullable=False),
        Column('version', Integer, nullable=False),
        Column('reason', String(80), nullable=False),
        Column('created_at', DateTime, nullable=False),
        Column('updated_at', DateTime, nullable=False),
        UniqueConstraint('op_id', 'asset_key', name='uq_asset_delete_op_asset'),
    )
    Index('ix_asset_delete_op', table.c.op_id)
    Index('ix_asset_delete_state_created', table.c.state, table.c.created_at)

    @classmethod
    def ensure(cls, engine, enabled=True):
        """Serialize and validate DDL in one transaction, touching only our table.

        No cached readiness: restore/clean-copy can replace a DB underneath an
        existing Engine. Failure never enables an older direct-delete fallback.
        """
        if not enabled:
            return False
        with engine.connect() as conn:
            try:
                dialect = conn.dialect.name
                if dialect == 'sqlite':
                    conn.exec_driver_sql('BEGIN IMMEDIATE')
                elif dialect == 'postgresql':
                    deadline = time.monotonic() + 2
                    while not conn.execute(text('SELECT pg_try_advisory_xact_lock(2087451025)')).scalar():
                        if time.monotonic() >= deadline:
                            raise DeleteReceiptError('D02_SCHEMA_LOCK')
                        time.sleep(0.01)
                else:
                    raise DeleteReceiptError('D02_SCHEMA_DIALECT')
                status = conn.execute(text(
                    "SELECT schema_version, migration_state FROM meta_schema_version "
                    "WHERE schema_name='meta_db'" + (' FOR SHARE' if dialect == 'postgresql' else '')
                )).first()
                if not status or status[0] != 1 or status[1] != 'complete':
                    raise DeleteReceiptError('D02_SCHEMA_NOT_READY')
                # Restore/copy can hold the existing maintenance lease while the
                # version row still reads complete. Do not create or renew it.
                if inspect(conn).has_table('meta_db_job_lock'):
                    lease = conn.execute(text(
                        "SELECT owner_token, expires_at FROM meta_db_job_lock "
                        "WHERE lock_name='migration'" + (' FOR SHARE' if dialect == 'postgresql' else '')
                    )).first()
                    if lease and lease[0] and lease[1] > time.time():
                        raise DeleteReceiptError('D02_MAINTENANCE_LOCKED')
                if not inspect(conn).has_table(cls.table.name):
                    cls.table.create(conn)
                cls._validate(conn)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                if isinstance(exc, DeleteReceiptError):
                    raise
                # Driver messages can contain connection details or SQL values.
                raise DeleteReceiptError('D02_SCHEMA_UNAVAILABLE') from None
        return True

    @classmethod
    def _validate(cls, conn):
        inspector = inspect(conn)
        columns = {c['name']: c for c in inspector.get_columns(cls.table.name)}
        for expected in cls.table.columns:
            actual = columns.get(expected.name)
            if actual is None:
                raise DeleteReceiptError('D02_SCHEMA_COLUMN')
            # Compare dialect-normalized SQL types (TEXT must not match VARCHAR).
            if str(actual['type'].compile(dialect=conn.dialect)).upper() != str(expected.type.compile(dialect=conn.dialect)).upper():
                raise DeleteReceiptError('D02_SCHEMA_TYPE')
            if actual['nullable'] != expected.nullable:
                raise DeleteReceiptError('D02_SCHEMA_NULLABLE')
        if inspector.get_pk_constraint(cls.table.name).get('constrained_columns') != ['receipt_id']:
            raise DeleteReceiptError('D02_SCHEMA_PK')
        uniques = {tuple(c['column_names']) for c in inspector.get_unique_constraints(cls.table.name)}
        if ('op_id', 'asset_key') not in uniques:
            raise DeleteReceiptError('D02_SCHEMA_UNIQUE')
        indexes = {c['name']: c for c in inspector.get_indexes(cls.table.name)}
        for name, names in (('ix_asset_delete_op', ['op_id']),
                            ('ix_asset_delete_state_created', ['state', 'created_at'])):
            index = indexes.get(name)
            if not index or index['column_names'] != names or index.get('unique'):
                raise DeleteReceiptError('D02_SCHEMA_INDEX')

    @staticmethod
    def digest(payload):
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    @staticmethod
    def _owner(owner):
        owner = tuple(owner)
        if (len(owner) not in (5, 6) or not isinstance(owner[0], str) or not owner[0]
                or owner[1] not in ('item', 'person') or not isinstance(owner[2], str)
                or not owner[2] or type(owner[3]) is not int or owner[3] <= 0
                or not isinstance(owner[4], str)
                or len(owner) == 6 and not isinstance(owner[5], str)):
            raise ValueError()
        return owner

    @classmethod
    def _selection(cls, owners):
        try:
            selected = {cls._owner(owner) for owner in owners}
            if not selected:
                raise ValueError()
            return selected
        except (TypeError, ValueError):
            raise DeleteReceiptError('D02_OWNER_UNVERIFIED') from None

    @classmethod
    def _related_owners(cls, asset, selected):
        try:
            evidence = asset.get('reference_evidence')
            if not isinstance(evidence, (list, tuple)):
                raise ValueError()
            related = set()
            for ref in evidence:
                ref = cls._owner(ref)
                if len(ref) == 6 and ref[5] == 'external':
                    continue
                if ref[:5] in selected:
                    related.add(ref[:5])
                elif ref in selected:
                    related.add(ref)
            if not related:
                raise ValueError()
            return sorted([list(o) for o in related], key=str)
        except (TypeError, ValueError, IndexError):
            raise DeleteReceiptError('D02_OWNER_UNVERIFIED') from None

    @classmethod
    def selected_owners(cls, asset, owners):
        """5-tuples select owners, 6-tuples select slots; external refs stay evidence."""
        return cls._related_owners(asset, cls._selection(owners))

    @classmethod
    def record(cls, session, category, request_type, assets, owners, request=None):
        """INSERT through the caller's Session; never commit or perform file I/O."""
        if not assets:
            return None
        selected = cls._selection(owners)
        if request is not None and (not isinstance(request, _DeleteRequest) or request.engine is not session.get_bind()
                or request.category != category or request.op_id is not None):
            raise DeleteReceiptError('D02_REQUEST_PROOF')
        op_id = uuid.uuid4().hex
        now = datetime.now()
        unique = {}
        for asset in assets:
            key = str(asset.get('key') or '')
            if not key or (asset.get('blocked') and not asset.get('retained')):
                raise DeleteReceiptError('D02_ASSET_UNVERIFIED')
            related = cls._related_owners(asset, selected)
            key = cls.digest(key)
            body = dict(asset=asset, owners=related, physical_cleanup_enabled=request is not None)
            if request is not None:
                body['cleanup_contract'] = request.contract
                body['manual_retry_contract'] = dict(kind='d02f4_admin_retry_v1', database_stamp=request.stamp)
            payload = json.dumps(body, sort_keys=True, ensure_ascii=False)
            previous = unique.get(key)
            if previous is not None and previous != payload:
                raise DeleteReceiptError('D02_ASSET_AMBIGUOUS')
            unique[key] = payload
        for key, payload in unique.items():
            session.execute(cls.table.insert().values(
                receipt_id=uuid.uuid4().hex, op_id=op_id, asset_key=key,
                category=category, request_type=request_type, format_version=1,
                payload=payload, payload_digest=cls.digest(payload), state='RECORDED',
                version=0, reason='COMMIT_PENDING' if request is not None else 'WRITER_COVERAGE_UNPROVEN',
                created_at=now, updated_at=now))
        if request is not None:
            request.op_id = op_id
            request.digests = {key: cls.digest(payload) for key, payload in unique.items()}
            request.bind()
        return op_id

    @staticmethod
    def _limit(value):
        if isinstance(value, bool) or not (isinstance(value, int) or
                isinstance(value, str) and re.fullmatch(r'[0-9]{1,9}', value)):
            raise DeleteReceiptError('D02_PAGE_LIMIT')
        value = int(value)
        if value <= 0:
            raise DeleteReceiptError('D02_PAGE_LIMIT')
        return min(value, 100)

    @staticmethod
    def _scope(kind, scope):
        pattern = r'[0-9a-f]{32}' if kind in ('read', 'replay') else r'[A-Z][A-Z0-9_]{0,29}'
        if not isinstance(scope, str) or not re.fullmatch(pattern, scope):
            raise DeleteReceiptError('D02_PAGE_SCOPE')

    @staticmethod
    def _cursor(kind, scope, after):
        raw = json.dumps([1, kind, scope, after], separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip('=')

    @classmethod
    def _after(cls, kind, scope, cursor):
        if cursor is None:
            return None
        try:
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 512:
                raise ValueError()
            raw = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            value = json.loads(raw.decode('utf-8'))
            if (not isinstance(value, list) or len(value) != 4 or type(value[0]) is not int or value[:3] != [1, kind, scope]
                    or value[3] is not None and not isinstance(value[3], str)
                    or value[3] is not None and not re.fullmatch(r'[0-9a-f]{32}', value[3])):
                raise ValueError()
            return value[3]
        except (ValueError, TypeError, UnicodeError, binascii.Error):
            raise DeleteReceiptError('D02_PAGE_CURSOR') from None

    @classmethod
    def _page(cls, engine, kind, scope, limit, cursor, states=None):
        cls._scope(kind, scope)
        limit = cls._limit(limit)
        after = cls._after(kind, scope, cursor)
        base = [cls.table.c.op_id == scope] if kind in ('read', 'replay') else [cls.table.c.category == scope]
        conditions = base + ([cls.table.c.state.in_(states)] if states is not None else (
            [cls.table.c.state == 'RECORDED'] if kind in ('discover', 'replay') else []))
        page_conditions = conditions + ([cls.table.c.receipt_id > after] if after else [])
        with engine.connect() as conn:
            items = [dict(row) for row in conn.execute(select(cls.table).where(*page_conditions)
                .order_by(cls.table.c.receipt_id).limit(limit)).mappings()]
            last = items[-1]['receipt_id'] if items else after
            remaining_conditions = conditions + ([cls.table.c.receipt_id > last] if last else [])
            remaining = conn.execute(select(func.count()).select_from(cls.table).where(*remaining_conditions)).scalar()
            pending = conn.execute(select(func.count()).select_from(cls.table).where(
                *base, cls.table.c.state == 'RECORDED')).scalar()
        return dict(items=items, limit=limit, returned=len(items), remaining=remaining,
            pending_remaining=pending, next_cursor=cls._cursor(kind, scope, last) if remaining else None)

    @classmethod
    def read_page(cls, engine, op_id, limit=100, cursor=None):
        """All states for one operation. A cursor is scoped, not an authority token.

        Keyset order is receipt_id, independent of timestamps and state changes.
        Read/discovery are live scans: start a new scan for inserts behind a cursor.
        """
        return cls._page(engine, 'read', op_id, limit, cursor)

    @classmethod
    def read(cls, engine, op_id, limit=100):
        return cls.read_page(engine, op_id, limit)['items']

    @classmethod
    def discover_page(cls, engine, category, limit=100, cursor=None):
        """Only unprocessed work for one category; blocked rows cannot monopolize it."""
        return cls._page(engine, 'discover', category, limit, cursor)

    @classmethod
    def discover(cls, engine, category, limit=100):
        return cls.discover_page(engine, category, limit)['items']

    @classmethod
    def audit_page(cls, engine, category, limit=100, cursor=None):
        """Explicit category status audit, including all old blocked v1 receipts."""
        return cls._page(engine, 'audit', category, limit, cursor)

    @classmethod
    def replay_page(cls, engine, op_id, limit=100, cursor=None):
        """One bounded RECORDED page, CAS updates only; NEVER file I/O.

        next_cursor continues unprocessed rows. If an insert appeared behind the
        cursor, the next cursor explicitly starts another sweep. No automatic loop.
        Counts are observations, not a snapshot or distributed commit guarantee.
        """
        page = cls._page(engine, 'replay', op_id, limit, cursor)
        rows = page['items']
        processed = 0
        with engine.begin() as conn:
            for row in rows:
                state, reason = 'BLOCKED_CONCURRENCY', 'WRITER_COVERAGE_UNPROVEN'
                if row['format_version'] != 1 or cls.digest(row['payload']) != row['payload_digest']:
                    state, reason = 'BLOCKED', 'RECEIPT_UNVERIFIED'
                else:
                    try:
                        payload = json.loads(row['payload'])
                        if payload.get('physical_cleanup_enabled') is True and payload.get('cleanup_contract', {}).get('kind') == 'd02f3_same_request_v1':
                            continue
                        asset = payload['asset']
                        if not isinstance(asset, dict) or not isinstance(payload['owners'], list):
                            raise ValueError()
                        if asset.get('retained'):
                            state, reason = 'RETAINED', 'OTHER_OWNER_REFERENCE'
                        group = asset.get('request_group') or {}
                        if group.get('cross_category') and asset.get('retention_scope') == 'cross_category_pending':
                            state, reason = 'BLOCKED_CONCURRENCY', 'AV_ALL_GROUP_UNCONFIRMED'
                    except (KeyError, TypeError, ValueError, AttributeError):
                        state, reason = 'BLOCKED', 'RECEIPT_UNVERIFIED'
                result = conn.execute(cls.table.update().where(
                    cls.table.c.receipt_id == row['receipt_id'],
                    cls.table.c.state == 'RECORDED', cls.table.c.version == row['version'],
                ).values(state=state, reason=reason, version=row['version'] + 1, updated_at=datetime.now()))
                processed += result.rowcount
        # Return exactly the examined page, never the first status page of the op.
        with engine.connect() as conn:
            items = [dict(row) for row in conn.execute(select(cls.table).where(
                cls.table.c.receipt_id.in_([r['receipt_id'] for r in rows]))
                .order_by(cls.table.c.receipt_id).limit(page['limit'])).mappings()] if rows else []
            pending = conn.execute(select(func.count()).select_from(cls.table).where(
                cls.table.c.op_id == op_id, cls.table.c.state == 'RECORDED')).scalar()
            last = rows[-1]['receipt_id'] if rows else cls._after('replay', op_id, cursor)
            ahead = conn.execute(select(cls.table.c.receipt_id).where(
                cls.table.c.op_id == op_id, cls.table.c.state == 'RECORDED',
                *([cls.table.c.receipt_id > last] if last else [])).limit(1)).first()
        restart = bool(pending and not ahead)
        return dict(items=items, returned=len(items), processed=processed, limit=page['limit'],
            pending_remaining=pending, next_cursor=cls._cursor('replay', op_id, None if restart else last)
                if pending else None, restarted=restart)

    @classmethod
    def replay(cls, engine, op_id, limit=100):
        """Legacy list: the processed page only. Repeating advances pending work."""
        return cls.replay_page(engine, op_id, limit)['items']

    @classmethod
    def database_stamp(cls, engine):
        """Use existing schema state and DB identity; no new incarnation table."""
        with engine.connect() as conn:
            row = conn.execute(text("SELECT schema_version, migration_state, migration_id, completed_at, updated_at "
                                    "FROM meta_schema_version WHERE schema_name='meta_db'")).first()
            if not row or row[0] != 1 or row[1] != 'complete':
                raise DeleteReceiptError('D02_DATABASE_NOT_READY')
            if inspect(conn).has_table('meta_db_job_lock'):
                lock = conn.execute(text("SELECT owner_token, expires_at FROM meta_db_job_lock WHERE lock_name='migration'")).first()
                if lock and lock[0] and lock[1] > time.time():
                    raise DeleteReceiptError('D02_MAINTENANCE_LOCKED')
        location = None
        if engine.dialect.name == 'sqlite' and engine.url.database not in (None, '', ':memory:'):
            st = os.stat(engine.url.database)
            location = (st.st_dev, st.st_ino)
        return cls.digest(json.dumps([str(engine.url), list(row), location], default=str, sort_keys=True))

    @classmethod
    def new_request(cls, engine, category, registry):
        return _DeleteRequest(engine, category, registry, cls.database_stamp(engine))

    @classmethod
    def manual_request(cls, engine, category, registry, op_id):
        """Internal proof for a separately authenticated explicit admin action."""
        cls._scope('replay', op_id)
        request = cls.new_request(engine, category, registry)
        request.manual, request.committed, request.op_id = True, True, op_id
        return request

    @classmethod
    def _manual_payload_valid(cls, row, payload, category, stamp):
        try:
            contract = payload['cleanup_contract']
            asset = payload['asset']
            return (row['format_version'] == 1 and row['category'] == category
                and row['reason'] not in ('WRITER_COVERAGE_UNPROVEN', 'AV_ALL_GROUP_UNCONFIRMED')
                and cls.digest(row['payload']) == row['payload_digest']
                and payload.get('physical_cleanup_enabled') is True
                and contract.get('kind') == 'd02f3_same_request_v1'
                and contract.get('database_stamp') == stamp
                and isinstance(contract.get('nonce_hash'), str)
                and re.fullmatch(r'[0-9a-f]{64}', contract['nonce_hash']) is not None
                and payload.get('manual_retry_contract') == dict(kind='d02f4_admin_retry_v1', database_stamp=stamp)
                and not asset.get('request_group') and not asset.get('retention_scope')
                and cls.digest(asset['key']) == row['asset_key']
                and bool(payload['owners'])
                and cls.selected_owners(asset, payload['owners']) == payload['owners'])
        except (KeyError, TypeError, ValueError, AttributeError, DeleteReceiptError):
            return False

    @classmethod
    def pending_operations(cls, engine, category):
        """Bounded, read-only status. Listing never confers deletion authority."""
        cls._scope('audit', category)
        stamp = cls.database_stamp(engine)
        with engine.connect() as conn:
            if not inspect(conn).has_table(cls.table.name):
                return []
            cls._validate(conn)
            pending = ('RECORDED', 'RETRY_PENDING', 'UNKNOWN_OUTCOME', 'BLOCKED', 'BLOCKED_CONCURRENCY')
            operations = conn.execute(select(cls.table.c.op_id, func.max(cls.table.c.created_at).label('created'))
                .where(cls.table.c.category == category, cls.table.c.state.in_(pending))
                .group_by(cls.table.c.op_id).order_by(func.max(cls.table.c.created_at).desc(), cls.table.c.op_id)
                .limit(20)).mappings().all()
            result = []
            for op in operations:
                states = dict(conn.execute(select(cls.table.c.state, func.count()).where(
                    cls.table.c.category == category, cls.table.c.op_id == op['op_id'])
                    .group_by(cls.table.c.state)).all())
                first = conn.execute(select(cls.table).where(cls.table.c.category == category,
                    cls.table.c.op_id == op['op_id'], cls.table.c.state.in_(pending[:3]))
                    .order_by(cls.table.c.receipt_id).limit(1)).mappings().first()
                try:
                    allowed = bool(first and cls._manual_payload_valid(first, json.loads(first['payload']), category, stamp))
                except (TypeError, ValueError):
                    allowed = False
                result.append(dict(op_id=op['op_id'], label=op['op_id'][:8], category=category,
                    created_at=str(op['created']), cleanup_counts=states, retry_allowed=allowed,
                    note='미완료 파일 재검증 가능' if allowed else '이전 계약·보호 상태 또는 검증 불가: 조회 전용'))
        return result

    @classmethod
    def _request_valid(cls, engine, op_id, request):
        if (not isinstance(request, _DeleteRequest) or request.engine is not engine or request.pid != os.getpid()
                or request.op_id != op_id or not request.committed or request.revoked):
            return False
        if hasattr(request.registry, 'registry') and request.registry() is not request.session:
            return False
        if request.session.in_transaction():
            return False
        return cls.database_stamp(engine) == request.stamp

    @classmethod
    def _cas(cls, engine, row, state, reason):
        with engine.begin() as conn:
            result = conn.execute(cls.table.update().where(
                cls.table.c.receipt_id == row['receipt_id'], cls.table.c.state == row['state'],
                cls.table.c.version == row['version']).values(state=state, reason=reason,
                version=row['version'] + 1, updated_at=datetime.now()))
        return result.rowcount == 1

    @classmethod
    def execute_page(cls, engine, op_id, request, executor, limit=100, cursor=None, retry=False):
        """Sequential external work between short state transactions, one page only.

        A boolean on the originating proof prevents overlapping calls in this
        request. It is not a distributed/writer lock. External TOCTOU is accepted.
        """
        states = ('RECORDED', 'RETRY_PENDING', 'UNKNOWN_OUTCOME') if retry else ('RECORDED',)
        page = cls._page(engine, 'replay', op_id, limit, cursor, states=states)
        rows, processed, deferred = page['items'], 0, 0
        valid = cls._request_valid(engine, op_id, request) if rows else False
        error = '' if valid or not rows else 'REQUEST_PROOF_REQUIRED'
        if valid and request.busy:
            valid, error = False, 'REQUEST_BUSY'
        if valid:
            request.busy = True
            try:
                for row in rows:
                    try:
                        payload = json.loads(row['payload'])
                        if request.manual:
                            if not cls._manual_payload_valid(row, payload, request.category, request.stamp):
                                # Do not upgrade or rewrite old/ambiguous receipts.
                                continue
                            if (row['state'] == 'UNKNOWN_OUTCOME' and row['reason'] == 'DELETE_ATTEMPT_STARTED'
                                    and (datetime.now() - row['updated_at']).total_seconds() < cls.attempt_grace_seconds):
                                deferred += 1
                                continue
                        # Old/copy-injected rows never acquire this request's authority.
                        elif (payload.get('physical_cleanup_enabled') is not True
                                or payload.get('cleanup_contract') != request.contract
                                or row['asset_key'] not in request.digests):
                            continue
                        asset = payload['asset']
                        if (row['format_version'] != 1 or row['category'] != request.category
                                or cls.digest(row['payload']) != row['payload_digest']
                                or not request.manual and row['payload_digest'] != request.digests[row['asset_key']]
                                or cls.digest(asset['key']) != row['asset_key']
                                or cls.selected_owners(asset, payload['owners']) != payload['owners']
                                or asset.get('request_group')):
                            processed += int(cls._cas(engine, row, 'BLOCKED', 'RECEIPT_UNVERIFIED'))
                            continue
                        # Durable uncertainty before the external effect covers a crash
                        # after unlink/DELETE and before its final status write.
                        if not cls._cas(engine, row, 'UNKNOWN_OUTCOME', 'DELETE_ATTEMPT_STARTED'):
                            continue
                        attempt = dict(row, state='UNKNOWN_OUTCOME', version=row['version'] + 1)
                        try:
                            request.executing_asset = asset
                            state, reason = executor(asset, request.category)
                            if state not in ('DELETED', 'RETAINED', 'BLOCKED', 'RETRY_PENDING', 'UNKNOWN_OUTCOME'):
                                state, reason = 'UNKNOWN_OUTCOME', 'INVALID_EXECUTOR_RESULT'
                        except Exception:
                            state, reason = 'UNKNOWN_OUTCOME', 'EXECUTOR_INTERRUPTED'
                        finally:
                            request.executing_asset = None
                        try:
                            cls._cas(engine, attempt, state, reason)
                        except Exception:
                            # The already committed UNKNOWN_OUTCOME survives even if
                            # the result DB write is unavailable. No DB owner rollback.
                            error = 'RESULT_WRITE_UNCERTAIN'
                        processed += 1
                        if error:
                            break
                    except (KeyError, TypeError, ValueError, DeleteReceiptError):
                        if request.manual:
                            continue
                        processed += int(cls._cas(engine, row, 'BLOCKED', 'RECEIPT_UNVERIFIED'))
            finally:
                request.busy = False
        with engine.connect() as conn:
            items = [dict(r) for r in conn.execute(select(cls.table).where(
                cls.table.c.receipt_id.in_([r['receipt_id'] for r in rows]))
                .order_by(cls.table.c.receipt_id).limit(page['limit'])).mappings()] if rows else []
            summary = dict(conn.execute(select(cls.table.c.state, func.count()).where(
                cls.table.c.op_id == op_id).group_by(cls.table.c.state)).all())
            pending = conn.execute(select(func.count()).select_from(cls.table).where(
                cls.table.c.op_id == op_id, cls.table.c.state.in_(states))).scalar()
            last = rows[-1]['receipt_id'] if rows else cls._after('replay', op_id, cursor)
            ahead = conn.execute(select(cls.table.c.receipt_id).where(
                cls.table.c.op_id == op_id, cls.table.c.state.in_(states),
                *([cls.table.c.receipt_id > last] if last else [])).limit(1)).first()
        return dict(items=items, returned=len(items), processed=processed, pending_remaining=pending,
            has_more=bool(ahead), deferred_active=deferred,
            next_cursor=cls._cursor('replay', op_id, last if ahead else None) if pending else None,
            cleanup_counts=summary, cleanup_pending=bool(error or not summary or
                any(n for s, n in summary.items() if s not in ('DELETED', 'RETAINED'))),
            error_code=error or ('COMMITTED_RECEIPT_NOT_FOUND' if not summary else ''))

    @classmethod
    def complete(cls, engine, op_id, request, executor, retry=False):
        key = (os.getpid(), getattr(request, 'stamp', ''), getattr(request, 'category', ''), op_id)
        with cls._active_lock:
            if key in cls._active_operations:
                return dict(processed=0, returned=0, pages=0, cleanup_counts={}, cleanup_pending=True,
                            pending_remaining=None, next_cursor=None, error_code='REQUEST_BUSY', deferred_active=0)
            cls._active_operations.add(key)
        try:
            return cls._complete(engine, op_id, request, executor, retry)
        finally:
            with cls._active_lock:
                cls._active_operations.discard(key)

    @classmethod
    def _complete(cls, engine, op_id, request, executor, retry=False):
        """Consume bounded pages, never retry a new failure in this invocation."""
        cursor, seen, previous_pending = None, set(), None
        result = dict(processed=0, returned=0, pages=0, deferred_active=0, cleanup_counts={},
                      cleanup_pending=True, pending_remaining=None, next_cursor=None, error_code='')
        while True:
            try:
                page = cls.execute_page(engine, op_id, request, executor, cursor=cursor, retry=retry)
            except Exception:
                result['error_code'] = 'POSTCOMMIT_PAGE_FAILED'
                # Preserve completed counts and refresh only aggregates if the DB
                # is reachable. No storage work or receipt payload reload here.
                try:
                    with engine.connect() as conn:
                        result['cleanup_counts'] = dict(conn.execute(select(cls.table.c.state, func.count())
                            .where(cls.table.c.op_id == op_id).group_by(cls.table.c.state)).all())
                    result['pending_remaining'] = sum(result['cleanup_counts'].get(s, 0) for s in
                        (('RECORDED', 'RETRY_PENDING', 'UNKNOWN_OUTCOME') if retry else ('RECORDED',)))
                except Exception:
                    pass
                break
            result.update(cleanup_counts=page['cleanup_counts'], cleanup_pending=page['cleanup_pending'],
                          pending_remaining=page['pending_remaining'], next_cursor=page['next_cursor'],
                          error_code=page['error_code'])
            result['processed'] += page['processed']
            result['returned'] += page['returned']
            result['pages'] += 1
            result['deferred_active'] += page.get('deferred_active', 0)
            if page['error_code']:
                break
            if (retry and not page['has_more']) or (not retry and not page['pending_remaining']):
                result['next_cursor'] = None
                break
            next_cursor = page['next_cursor']
            if (not page['returned'] or (not retry and (not page['processed'] or
                    previous_pending is not None and page['pending_remaining'] >= previous_pending))
                    or next_cursor in seen):
                result['error_code'] = 'PAGE_NO_PROGRESS'
                break
            seen.add(next_cursor)
            previous_pending, cursor = page['pending_remaining'], next_cursor
        if result['error_code']:
            result['cleanup_pending'] = True
        return result
