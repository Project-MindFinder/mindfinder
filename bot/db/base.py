"""SQLAlchemy 선언적 베이스 클래스.

모든 ORM 모델은 이 Base 를 상속한다.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """모든 ORM 모델의 공통 베이스."""
