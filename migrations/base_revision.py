# -*- coding: utf-8 -*-
"""마이그레이션 리비전 표준 기본 규격 정의 모듈."""

from abc import ABC, abstractmethod
from typing import Callable, Optional
from sqlalchemy.orm import Session
from .ddl_ops import DdlOperations


class BaseRevision(ABC):
    """모든 마이그레이션 리비전이 구현해야 하는 기본 추상 클래스."""

    @property
    @abstractmethod
    def revision_id(self) -> str:
        """리비전 고유 식별자."""
        pass

    @property
    @abstractmethod
    def from_version(self) -> int:
        """시작 스키마 버전."""
        pass

    @property
    @abstractmethod
    def to_version(self) -> int:
        """목표 스키마 버전."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """리비전 작업 요약 설명."""
        pass

    @abstractmethod
    def upgrade_schema(self, ops: DdlOperations, physical_name: str) -> None:
        """
        초고속 스키마 구조 변경(Fast DDL) 단계.
        테이블 생성, 컬럼 추가, 인덱스 생성 등 수 초 내에 끝나는 작업만 수행하며,
        대규모 데이터 루프나 무거운 레코드 변환은 여기에 포함하지 않습니다.
        """
        pass

    @property
    def has_backfill(self) -> bool:
        """스키마 변경 완료 후 백그라운드 데이터 정규화(Backfill) 작업 필요 여부."""
        return False

    def run_backfill(
        self,
        session: Session,
        context: dict,
        progress_callback: Optional[Callable[[int, int, str, str], None]] = None
    ) -> None:
        """
        비동기 데이터 백필(Async Data Backfill) 단계.
        대규모 레코드 순회, JSON 구조 분리, 썸네일 캐싱 등 장기 실행 작업을
        청크 단위로 서비스 중단 없이 수행합니다.
        """
        pass
