# -*- coding: utf-8 -*-
"""SQLite와 PostgreSQL 간 방언 차이를 흡수하는 엔진 독립 DDL 연산 계층."""

import traceback
from sqlalchemy import text, inspect
from ..setup import logger


class DdlOperations:
    """엔진 독립적 DDL 실행 인터페이스."""

    def __init__(self, connection, dialect_name: str):
        self.connection = connection
        self.dialect = str(dialect_name or 'sqlite').lower()

    def has_table(self, table_name: str) -> bool:
        """테이블 존재 여부 확인."""
        try:
            inspector = inspect(self.connection)
            return bool(table_name in inspector.get_table_names())
        except Exception as exc:
            logger.debug(f"[DdlOps] has_table 확인 예외 ({table_name}): {exc}")
            return False

    def has_column(self, table_name: str, column_name: str) -> bool:
        """테이블 내 특정 컬럼 존재 여부 확인."""
        try:
            inspector = inspect(self.connection)
            if not self.has_table(table_name):
                return False
            columns = [c['name'].lower() for c in inspector.get_columns(table_name)]
            return bool(column_name.lower() in columns)
        except Exception as exc:
            logger.debug(f"[DdlOps] has_column 확인 예외 ({table_name}.{column_name}): {exc}")
            return False

    def add_column(
        self,
        table_name: str,
        column_name: str,
        column_type: str,
        nullable: bool = True,
        server_default: str = None
    ) -> bool:
        """테이블에 신규 컬럼을 안전하게 추가 (이미 존재할 경우 건너뜀)."""
        if self.has_column(table_name, column_name):
            return False

        # 방언별 타입 정규화 매핑
        norm_type = str(column_type).strip()
        if self.dialect == 'postgresql':
            if norm_type.upper().startswith('DATETIME'):
                norm_type = 'TIMESTAMP'
        else:
            if norm_type.upper().startswith('TIMESTAMP'):
                norm_type = 'DATETIME'
            elif norm_type.upper() == 'BOOLEAN':
                norm_type = 'INTEGER'

        default_clause = f" DEFAULT {server_default}" if server_default is not None else ""
        null_clause = " NULL" if nullable else " NOT NULL"

        statement = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {norm_type}{default_clause}{null_clause}"
        try:
            self.connection.execute(text(statement))
            logger.debug(f"[DdlOps] 컬럼 추가 성공: {table_name}.{column_name} ({norm_type})")
            return True
        except Exception as exc:
            logger.error(f"[DdlOps] 컬럼 추가 실패 ({table_name}.{column_name}): {exc}")
            raise

    def create_index(
        self,
        index_name: str,
        table_name: str,
        columns: list,
        unique: bool = False
    ) -> bool:
        """복합/단일 인덱스를 안전하게 생성."""
        cols_str = ", ".join(columns)
        unique_clause = "UNIQUE " if unique else ""
        statement = f"CREATE {unique_clause}INDEX IF NOT EXISTS {index_name} ON {table_name} ({cols_str})"
        try:
            self.connection.execute(text(statement))
            logger.debug(f"[DdlOps] 인덱스 생성 성공: {index_name} ON {table_name}({cols_str})")
            return True
        except Exception as exc:
            logger.error(f"[DdlOps] 인덱스 생성 실패 ({index_name}): {exc}")
            raise

    def drop_index(self, index_name: str, table_name: str = None) -> bool:
        """인덱스를 안전하게 삭제."""
        statement = f"DROP INDEX IF EXISTS {index_name}"
        try:
            self.connection.execute(text(statement))
            logger.debug(f"[DdlOps] 인덱스 삭제 성공: {index_name}")
            return True
        except Exception as exc:
            logger.error(f"[DdlOps] 인덱스 삭제 실패 ({index_name}): {exc}")
            raise

    def execute_raw(self, sql_statement: str, params: dict = None):
        """방언 특화 로직 처리를 위한 원시 SQL 안전 실행."""
        try:
            return self.connection.execute(text(sql_statement), params or {})
        except Exception as exc:
            logger.error(f"[DdlOps] 원시 SQL 실행 실패: {exc}\nSQL: {sql_statement}")
            raise
