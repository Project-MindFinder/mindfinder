"""SQLAlchemy 2.0 async 엔진 및 세션 관리 모듈.

- 봇 시작 시 connect() 로 엔진을 만들고 테이블을 생성한다.
- session() 컨텍스트 매니저로 트랜잭션을 다룬다.
- 종료 시 dispose() 로 정리한다.

지원 대상:
- **SQLite**(파일): 로컬 개발/테스트용. ``Path`` 를 넘기면 자동 사용.
- **PostgreSQL**(별도 컨테이너): 운영용. ``DATABASE_URL`` 환경변수
  (예: ``postgresql+asyncpg://user:pw@db:5432/mindfinder``) 를 넘기면 사용.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Union

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
    """SQLAlchemy async 엔진/세션을 감싸는 래퍼.

    ``target`` 은 다음 중 하나:
    - ``Path`` : SQLite 파일 경로 (로컬/테스트)
    - ``str``  : 데이터베이스 URL
      (``sqlite+aiosqlite:///...`` 또는 ``postgresql+asyncpg://...``)
    """

    def __init__(self, target: Union[Path, str]) -> None:
        if isinstance(target, Path):
            self.path: Path | None = target
            self.url = f"sqlite+aiosqlite:///{target}"
            self.is_sqlite = True
        else:
            raw = str(target).strip()
            self.url = raw
            self.is_sqlite = raw.startswith("sqlite")
            self.path = None
            if self.is_sqlite:
                # sqlite URL 에서 경로만 뽑아 디렉터리 생성을 위해 보관한다.
                marker = "sqlite+aiosqlite:///"
                if raw.startswith(marker):
                    self.path = Path(raw[len(marker):])
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise RuntimeError("DB가 연결되지 않았습니다. 먼저 connect() 를 호출하세요.")
        return self._engine

    async def connect(self) -> None:
        """엔진을 생성하고 테이블을 만든다."""
        # SQLite 파일이면 상위 디렉터리를 보장한다.
        if self.is_sqlite and self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

        self._engine = create_async_engine(self.url, echo=False, future=True)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )
        async with self._engine.begin() as conn:
            if self.is_sqlite:
                # SQLite 는 외래키 제약이 기본 비활성이라 켜준다.
                await conn.exec_driver_sql("PRAGMA foreign_keys = ON;")
            await conn.run_sync(Base.metadata.create_all)
            # SQLite 파일에만 필요한 경량 마이그레이션 (신규 Postgres 는 불필요).
            if self.is_sqlite:
                await self._run_light_migrations(conn)
        target_label = str(self.path) if self.path is not None else self.url
        logger.info("데이터베이스 연결 완료: %s", target_label)

    async def _run_light_migrations(self, conn) -> None:  # noqa: ANN001
        """기존 SQLite DB 파일에 누락된 컬럼을 자동으로 추가한다.

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
                # NOT NULL 컬럼은 기존 행을 위해 기본값을 함께 지정한다.
                if not column.nullable:
                    default = column.default
                    if default is not None and getattr(default, "arg", None) is not None:
                        literal = default.arg
                        if isinstance(literal, bool):
                            ddl += f" DEFAULT {1 if literal else 0}"
                        elif isinstance(literal, (int, float)):
                            ddl += f" DEFAULT {literal}"
                        elif isinstance(literal, str):
                            escaped = literal.replace("'", "''")
                            ddl += f" DEFAULT '{escaped}'"
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
