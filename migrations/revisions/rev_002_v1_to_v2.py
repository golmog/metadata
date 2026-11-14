# -*- coding: utf-8 -*-
"""v1 -> v2: explicit schema preparation and a JSON-derived actor search field."""
from sqlalchemy import inspect, select, func, text
from ..base_revision import BaseRevision


class RevisionV1ToV2(BaseRevision):
    revision_id = 'rev_002_v1_to_v2'
    from_version = 1
    to_version = 2
    description = 'v2 정식 스키마 및 작품 JSON 기반 배우 검색 텍스트'
    has_backfill = True
    BATCH_SIZE = 100

    # Former boot-time compatibility DDL. Values are deliberately left untouched.
    MEDIA_COLUMNS = {
        'gdrive_fileid': 'VARCHAR(255)', 'relative_path': 'VARCHAR(500)',
        'source': 'VARCHAR(30)', 'checksum': 'VARCHAR(128)', 'updated_time': 'DATETIME',
    }
    PERSON_COLUMNS = {
        'gender': 'VARCHAR(15)', 'birth_date': 'VARCHAR(20)', 'birth_year': 'INTEGER',
        'debut_date': 'VARCHAR(20)', 'height': 'INTEGER', 'bust': 'INTEGER',
        'waist': 'INTEGER', 'hip': 'INTEGER', 'body_size': 'VARCHAR(30)',
        'cup_size': 'VARCHAR(10)', 'blood_type': 'VARCHAR(5)', 'agency': 'VARCHAR(100)',
        'country': 'VARCHAR(50)', 'info_url': 'VARCHAR(500)', 'has_photo': 'BOOLEAN',
        'thumb_url': 'VARCHAR(500)', 'local_path': 'VARCHAR(255)',
        'gdrive_fileid': 'VARCHAR(255)', 'custom_gdrive_fileid': 'VARCHAR(255)',
        'custom_local_path': 'VARCHAR(255)', 'official_cached_gdrive_fileid': 'VARCHAR(255)',
        'official_cached_local_path': 'VARCHAR(500)', 'official_remote_path': 'VARCHAR(500)',
        'custom_remote_path': 'VARCHAR(500)', 'site_img_url': 'VARCHAR(500)',
        'photo_urls': 'JSON', 'merged_count': 'INTEGER', 'alt_indices': 'JSON',
        'merged_details': 'JSON', 'works_count': 'INTEGER', 'source_data': 'JSON',
        'manual_data': 'JSON', 'created_time': 'DATETIME', 'updated_time': 'DATETIME',
    }

    @staticmethod
    def prepare_auxiliary_schema(conn):
        from ...mod_meta_db import MetaSchemaMigrationHistory
        from ...asset_delete import DeleteReceipts
        from ..lock_manager import MetaDbLockManager
        MetaSchemaMigrationHistory.__table__.create(conn, checkfirst=True)
        MetaDbLockManager.create_schema(conn)
        DeleteReceipts.table.create(conn, checkfirst=True)
        # Existing tables may predate the indexes; never replace receipt records.
        for index in DeleteReceipts.table.indexes:
            index.create(conn, checkfirst=True)
        DeleteReceipts._validate(conn)

    def upgrade_schema(self, ops, physical_name):
        for table, columns in (
            ('meta_media', self.MEDIA_COLUMNS), ('meta_person', self.PERSON_COLUMNS),
            ('meta_schema_version', {'lock_token': 'VARCHAR(100)', 'locked_at': 'DATETIME',
                                     'locked_by': 'VARCHAR(100)'}),
            ('meta_item', {'actor_search_text': 'TEXT'}),
        ):
            if ops.has_table(table):
                for name, column_type in columns.items():
                    ops.add_column(table, name, column_type)
        self.prepare_auxiliary_schema(ops.connection)

    def run_backfill(self, session, context, progress_callback=None):
        from ...mod_meta_db import MetaItem
        if 'meta_item' not in context['tables']:
            return
        table = MetaItem.__table__
        last_id = int(context['positions'].get('actor_search_text', 0))
        total = session.execute(select(func.count()).select_from(table)).scalar()
        current = session.execute(select(func.count()).select_from(table).where(table.c.id <= last_id)).scalar()
        while True:
            if context['stop_flag_checker']():
                raise InterruptedError('배우 검색 백필 중단')
            rows = session.execute(select(table.c.id, table.c.extra_info, table.c.original)
                                   .where(table.c.id > last_id).order_by(table.c.id)
                                   .limit(self.BATCH_SIZE)).all()
            if not rows:
                break
            # Raw UPDATE avoids MetaItem.updated_time's Python onupdate default.
            session.execute(text('UPDATE meta_item SET actor_search_text=:names WHERE id=:pk'), [
                {'pk': row.id, 'names': MetaItem.build_actor_search_text(row.extra_info, row.original)}
                for row in rows])
            session.commit()
            last_id = rows[-1].id
            current += len(rows)
            context['checkpoint']('actor_search_text', last_id)
            if progress_callback:
                progress_callback(current, total, '배우 검색 텍스트 백필 중', context['physical_name'])

    INDEX_NAME_SQLITE = 'ix_meta_item_actor_search_cover'

    @classmethod
    def is_sqlite_covering_index_valid(cls, conn) -> bool:
        """Check whether the single covering composite index strictly exists and is valid on SQLite."""
        if conn.dialect.name != 'sqlite':
            return True
        if not inspect(conn).has_table('meta_item'):
            return True
        idx_list = conn.execute(text("PRAGMA index_list('meta_item')")).fetchall()
        target = next((r for r in idx_list if r[1] == cls.INDEX_NAME_SQLITE), None)
        if not target:
            return False
        if len(target) >= 5 and target[4]:
            return False
        xinfo = conn.execute(text(f"PRAGMA index_xinfo('{cls.INDEX_NAME_SQLITE}')")).fetchall()
        key_cols = [r for r in xinfo if len(r) >= 6 and r[5] == 1]
        expected = [
            ('category', 0),
            ('created_time', 1),
            ('actor_search_text', 0),
        ]
        actual = [(c[2], c[3]) for c in key_cols]
        return actual == expected

    @classmethod
    def finalize_schema(cls, conn):
        """Failure must leave the version incomplete, including extension permissions."""
        if inspect(conn).has_table('meta_item'):
            if conn.execute(text('SELECT COUNT(*) FROM meta_item WHERE actor_search_text IS NULL')).scalar():
                raise RuntimeError('배우 검색 텍스트 백필이 완료되지 않았습니다.')
            if conn.dialect.name == 'postgresql':
                try:
                    conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
                    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_meta_item_actor_search_trgm '
                                      'ON meta_item USING gin (actor_search_text gin_trgm_ops)'))
                    valid = conn.execute(text("SELECT i.indisvalid AND i.indisready "
                        "AND am.amname='gin' AND a.attname='actor_search_text' "
                        "AND opc.opcname='gin_trgm_ops' AND i.indnatts=1 "
                        "AND i.indpred IS NULL AND i.indexprs IS NULL "
                        "FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid "
                        "JOIN pg_class tbl ON tbl.oid=i.indrelid "
                        "JOIN pg_am am ON idx.relam=am.oid "
                        "JOIN pg_attribute a ON a.attrelid=tbl.oid AND a.attnum=i.indkey[0] "
                        "JOIN pg_opclass opc ON opc.oid=i.indclass[0] "
                        "WHERE idx.oid=to_regclass('ix_meta_item_actor_search_trgm') "
                        "AND tbl.oid=to_regclass('meta_item')")).scalar()
                    if not valid:
                        raise RuntimeError('GIN 인덱스 정의/상태 불일치')
                except Exception as exc:
                    raise RuntimeError('v2 pg_trgm 확장/GIN 인덱스 준비 실패; DB 권한과 인덱스를 확인하세요.') from exc
            elif conn.dialect.name == 'sqlite':
                try:
                    idx_list = conn.execute(text("PRAGMA index_list('meta_item')")).fetchall()
                    existing_entry = next((r for r in idx_list if r[1] == cls.INDEX_NAME_SQLITE), None)
                    if existing_entry:
                        if len(existing_entry) >= 5 and existing_entry[4]:
                            raise RuntimeError(f'{cls.INDEX_NAME_SQLITE} 인덱스가 부분 인덱스(partial)로 정의되어 있습니다.')
                        xinfo = conn.execute(text(f"PRAGMA index_xinfo('{cls.INDEX_NAME_SQLITE}')")).fetchall()
                        key_cols = [r for r in xinfo if len(r) >= 6 and r[5] == 1]
                        expected = [('category', 0), ('created_time', 1), ('actor_search_text', 0)]
                        actual = [(c[2], c[3]) for c in key_cols]
                        if actual != expected:
                            raise RuntimeError(f'{cls.INDEX_NAME_SQLITE} 인덱스 정의 불일치: 기대={expected}, 실제={actual}')
                    else:
                        conn.execute(text(
                            f'CREATE INDEX {cls.INDEX_NAME_SQLITE} '
                            'ON meta_item (category, created_time DESC, actor_search_text)'
                        ))
                        if not cls.is_sqlite_covering_index_valid(conn):
                            raise RuntimeError(f'{cls.INDEX_NAME_SQLITE} 인덱스 생성 후 유효성 검증 실패')
                except Exception as exc:
                    raise RuntimeError(f'v2 SQLite 배우 검색 커버링 인덱스 준비 실패: {exc}') from exc
