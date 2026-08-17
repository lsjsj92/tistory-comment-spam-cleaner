# app/infrastructure/db/session.py
"""비동기 DB 엔진과 세션 관리.

SQLite 는 기본 설정으로 동시 쓰기에 취약하다. 삭제 작업이 여러 워커에서 결과를
기록하는 동안 "database is locked" 가 나지 않도록 WAL 모드와 busy_timeout 을 켠다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ...config.settings import Settings
from ..logging_setup import get_logger
from .base import Base

logger = get_logger(__name__)

# 쓰기 잠금 대기 시간(밀리초). 워커 동시성보다 넉넉하게 잡는다.
_BUSY_TIMEOUT_MS = 10_000


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """새 커넥션마다 SQLite 튜닝 옵션을 적용한다."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


class Database:
    """엔진과 세션 팩토리를 묶어 수명을 관리한다."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._engine: AsyncEngine = create_async_engine(
            settings.resolved_database_url,
            echo=False,
            future=True,
            pool_pre_ping=True,
        )
        if self._engine.dialect.name == "sqlite":
            event.listen(self._engine.sync_engine, "connect", _apply_sqlite_pragmas)
        self._session_factory = async_sessionmaker(
            bind=self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    async def create_all(self) -> None:
        """테이블을 생성한다. 이미 있으면 건너뛴다."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("데이터베이스 준비 완료: %s", self._describe_target())

    async def healthcheck(self) -> bool:
        """단순 질의로 연결 상태를 확인한다."""
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # pragma: no cover - 장애 상황
            logger.exception("데이터베이스 상태 확인 실패")
            return False

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """트랜잭션 경계를 갖는 세션 컨텍스트.

        정상 종료 시 커밋하고 예외가 나면 롤백한다.
        """
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """커넥션 풀을 정리한다."""
        await self._engine.dispose()
        logger.info("데이터베이스 커넥션을 정리했습니다.")

    def _describe_target(self) -> str:
        """로그에 남길 대상 설명. 파일 경로가 있으면 경로를 보여준다."""
        db_file = self._settings.database_file
        return str(db_file) if db_file else self._settings.resolved_database_url
