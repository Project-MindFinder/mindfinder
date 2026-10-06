"""SQLAlchemy ORM 모델 정의.

매칭 정밀도: 연·월 단위 (입원 연월 ~ 퇴원 연월).
기간 겹침 판정을 빠르게 하기 위해 ym(연*12+월) 정수 컬럼을 함께 저장한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.db.base import Base


class User(Base):
    """디스코드 사용자."""

    __tablename__ = "users"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    admissions: Mapped[list["Admission"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    match_preference: Mapped[Optional["MatchPreference"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class Hospital(Base):
    """정규화된 병원."""

    __tablename__ = "hospitals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    normalized_name: Mapped[str] = mapped_column(
        String(200), unique=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    aliases: Mapped[list["HospitalAlias"]] = relationship(
        back_populates="hospital", cascade="all, delete-orphan"
    )
    admissions: Mapped[list["Admission"]] = relationship(back_populates="hospital")


class HospitalAlias(Base):
    """병원 별칭 (자유 입력 정규화용)."""

    __tablename__ = "hospital_aliases"
    __table_args__ = (UniqueConstraint("hospital_id", "alias", name="uq_hospital_alias"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hospital_id: Mapped[int] = mapped_column(
        ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    hospital: Mapped["Hospital"] = relationship(back_populates="aliases")


class Admission(Base):
    """입원 이력 (자기 신고 + 인증 상태)."""

    __tablename__ = "admissions"
    __table_args__ = (
        CheckConstraint("admit_month BETWEEN 1 AND 12", name="ck_admit_month"),
        CheckConstraint(
            "discharge_month IS NULL OR discharge_month BETWEEN 1 AND 12",
            name="ck_discharge_month",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.discord_id", ondelete="CASCADE"), nullable=False, index=True
    )
    hospital_id: Mapped[int] = mapped_column(
        ForeignKey("hospitals.id"), nullable=False, index=True
    )
    admit_year: Mapped[int] = mapped_column(Integer, nullable=False)
    admit_month: Mapped[int] = mapped_column(Integer, nullable=False)
    discharge_year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    discharge_month: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_ongoing: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 기간 겹침 판정용: year*12 + (month-1)
    admit_ym: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    discharge_ym: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # self_reported | verified | rejected
    verification: Mapped[str] = mapped_column(
        String(20), default="self_reported", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="admissions")
    hospital: Mapped["Hospital"] = relationship(back_populates="admissions")

    # --- 도메인 헬퍼 ---

    def period_text(self) -> str:
        """사람이 읽기 쉬운 기간 문자열."""
        start = f"{self.admit_year}년 {self.admit_month}월"
        if self.is_ongoing or self.discharge_year is None:
            return f"{start} ~ 현재 입원 중"
        return f"{start} ~ {self.discharge_year}년 {self.discharge_month}월"

    def overlaps(self, other: "Admission") -> bool:
        """다른 입원 이력과 기간이 겹치는지 (연·월 단위, 양끝 포함)."""
        self_end = self.discharge_ym if self.discharge_ym is not None else float("inf")
        other_end = other.discharge_ym if other.discharge_ym is not None else float("inf")
        return self.admit_ym <= other_end and other.admit_ym <= self_end


class VerificationRequest(Base):
    """서류 심사 요청."""

    __tablename__ = "verification_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_verification_status"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.discord_id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    proof_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    proof_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reviewer_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MatchPreference(Base):
    """매칭 희망 설정."""

    __tablename__ = "match_preferences"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.discord_id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="match_preference")


class MatchRequest(Base):
    """매칭 신청/수락/거절."""

    __tablename__ = "match_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected', 'cancelled')",
            name="ck_match_status",
        ),
        CheckConstraint("requester_id <> target_id", name="ck_match_distinct"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    requester_id: Mapped[int] = mapped_column(
        ForeignKey("users.discord_id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_id: Mapped[int] = mapped_column(
        ForeignKey("users.discord_id", ondelete="CASCADE"), nullable=False, index=True
    )
    admission_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admissions.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    responded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class MatchChannel(Base):
    """매칭 성사 시 생성된 비공개 채널.

    1:1 뿐 아니라 여러 명이 하나의 채널을 공유할 수 있다.
    봇은 연락처(전화번호/SNS)를 저장하지 않고, 채널 접근 권한만 관리한다.
    """

    __tablename__ = "match_channels"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'closed')", name="ck_match_channel_status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 디스코드 텍스트 채널 ID
    discord_channel_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 같은 병원 기준으로 채널을 묶기 위한 참조 (병원 삭제 시 NULL)
    hospital_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("hospitals.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # open | closed
    status: Mapped[str] = mapped_column(
        String(20), default="open", nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    members: Mapped[list["MatchChannelMember"]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class MatchChannelMember(Base):
    """매칭 비공개 채널 참여자."""

    __tablename__ = "match_channel_members"
    __table_args__ = (
        UniqueConstraint(
            "match_channel_id", "user_id", name="uq_match_channel_member"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_channel_id: Mapped[int] = mapped_column(
        ForeignKey("match_channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    joined_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )

    channel: Mapped["MatchChannel"] = relationship(back_populates="members")


class AuditLog(Base):
    """감사 로그 (누가 무엇을 했는지)."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
