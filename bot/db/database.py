"""SQLAlchemy 2.0 async 엔진 및 세션 관리 모듈.

- 봇 시작 시 connect() 로 엔진을 만들고 테이블을 생성한다.
- session() 컨텍스트 매니저로 트랜잭션을 다룬다.
- 종료 시 dispose() 로 정리한다.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import sqlalchemy
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bot.db.base import Base
# 모델을 import 해야 Base.metadata 에 테이블이 등록된다.
from bot.db import models  # noqa: F401

logger = logging.getLogger(__name__)


class Database:
    """SQLAlchemy async 엔진/세션을 감싸는 래퍼."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("DB가 연결되지 않았습니다. 먼저 connect() 를 호출하세요.")
        return self._engine

    async def connect(self) -> None:
        """엔진을 생성하고 테이블을 만든다."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite+aiosqlite:///{self.path}"
        self._engine = create_async_engine(url, echo=False, future=True)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )
        async with self._engine.begin() as conn:
            # SQLite 는 외래키 제약이 기본 비활성이라 켜준다.
            await conn.exec_driver_sql("PRAGMA foreign_keys = ON;")
            await conn.run_sync(Base.metadata.create_all)
            await self._run_light_migrations(conn)
        logger.info("데이터베이스 연결 완료: %s", self.path)

    async def _run_light_migrations(self, conn) -> None:  # noqa: ANN001
        """기존 DB 파일에 누락된 컬럼을 자동으로 추가한다.

        ``create_all`` 은 이미 존재하는 테이블을 변경하지 않으므로,
        모델에 새 컬럼이 생기면 여기서 ALTER TABLE 로 보강한다.
        """
        existing_tables = set(
            await conn.run_sync(
                lambda sync_conn: sqlalchemy.inspect(sync_conn).get_table_names()
            )
        )
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            rows = await conn.exec_driver_sql(f"PRAGMA table_info({table.name})")
            existing_cols = {row[1] for row in rows.fetchall()}
            for column in table.columns:
                if column.name in existing_cols:
                    continue
                try:
                    col_type = column.type.compile(dialect=conn.dialect)
                except Exception:  # noqa: BLE001
                    col_type = "TEXT"
                ddl = (
                    f"ALTER TABLE {table.name} "
                    f"ADD COLUMN {column.name} {col_type}"
                )
                try:
                    await conn.exec_driver_sql(ddl)
                    logger.info(
                        "DB 마이그레이션: %s.%s 컬럼 추가", table.name, column.name
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "DB 마이그레이션 실패: %s.%s", table.name, column.name
                    )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """트랜잭션 세션 컨텍스트 매니저.

        사용 예:
            async with db.session() as session:
                session.add(obj)
                await session.commit()
        """
        if self._session_factory is None:
            raise RuntimeError("DB가 연결되지 않았습니다. 먼저 connect() 를 호출하세요.")
        session = self._session_factory()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def dispose(self) -> None:
        """엔진을 정리한다."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("데이터베이스 연결 종료")
