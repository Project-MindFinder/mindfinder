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
    # 세션이 닫힌 뒤에도 adm.hospital.name 을 안전하게 쓸 수 있도록 즉시 로딩한다.
    # (async SQLAlchemy 에서 지연 로딩은 DetachedInstanceError/MissingGreenlet 을 유발)
    hospital: Mapped["Hospital"] = relationship(
        back_populates="admissions", lazy="selectin"
    )

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
    """서류 심사 요청.

    - 수동 심사: 사용자가 서류를 제출하면 관리자가 승인/거절.
    - 자동 검증: 병원 진위확인 API(예: 서울대병원)로 문서 진위·정신과 여부를
      확인해 자동 승인한다. 이때 auto_verified=True 로 표시하고 진위확인
      결과(문서번호, 상태, 메시지)를 함께 저장한다.
    """

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
    # 인증 방식: document(서류) | knowledge(지식기반) | other(기타·관리자 수동)
    method: Mapped[str] = mapped_column(
        String(20), default="document", nullable=False
    )
    proof_channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    proof_message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reviewer_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- 수동 심사용 (사용자는 자료만 올리고, 관리자가 병원/기간을 확정) ---
    # 승인 시 이력(admission)을 생성할 때 쓰는 임시 병원명/병동
    pending_hospital_name: Mapped[Optional[str]] = mapped_column(
        String(200), nullable=True
    )
    pending_ward: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    # --- 자동 진위확인 (병원 진위확인 API) ---
    # 관리자 수동 승인 없이 자동 승인되었는지 여부
    auto_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # 진위확인에 사용한 문서확인번호 (중복 검증 방지용, 유니크)
    document_no: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    # 정신건강의학과 입원으로 확인되었는지 여부
    is_psychiatric: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # 진위확인 상태 (valid | invalid | unknown)
    truth_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # 진위확인 응답 메시지 (사이트 alert 문구)
    truth_message: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    # 파싱한 진료과 요약 (예: '소아정신과, 소아응급실')
    department_summary: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)

    # --- 증빙 서류 지문 (원본 PDF 미보관, 개인정보 보호) ---
    # 개인정보를 제거한 정규화 텍스트의 SHA-256 해시 (동일 서류 판정용)
    proof_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )
    # 유사 서류 판정용 MinHash 토큰(문자 n-gram) JSON 문자열
    proof_shingles: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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


class ManagedRole(Base):
    """봇이 자동 생성/관리하는 역할 매핑.

    접근 제어에 쓰이는 역할(인증됨/서류인증/병원별)을 이름으로 자동 생성하고,
    생성된 디스코드 역할 ID 를 길드별로 기록해 재사용한다.
    """

    __tablename__ = "managed_roles"
    __table_args__ = (UniqueConstraint("guild_id", "key", name="uq_managed_role_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    # 논리 식별자: 'tier1' | 'tier2' | 'hospital:<hospital_id>'
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class ManagedChannel(Base):
    """봇이 자동 생성/관리하는 채널·카테고리 매핑.

    kind: 'category' | 'channel'
    key: 논리 식별자 (예: 'cat:verify', 'ch:panel', 'cat:hospital', 'ch:hospital:3')
    """

    __tablename__ = "managed_channels"
    __table_args__ = (
        UniqueConstraint("guild_id", "key", name="uq_managed_channel_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    discord_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class KnowledgeVerification(Base):
    """지식기반 인증.

    이미 서류 인증을 마친 사용자(asker)가 자신이 입원했던 시기·병원에
    실제로 있었던 사람만 알 수 있는 질문을 올리고, 응답자(respondent)가
    답하면 asker 가 정답 여부를 판정한다. asker 가 정답으로 확인하면
    관리자 최종 승인을 거쳐 응답자에게 해당 병원 인증을 부여한다.

    status:
      - awaiting_answer    : 질문 등록됨, 응답 대기
      - awaiting_verdict   : 응답 완료, asker 판정 대기
      - asker_confirmed    : asker 가 정답으로 확인, 관리자 승인 대기
      - asker_rejected     : asker 가 오답으로 반려
      - approved           : 관리자 최종 승인 완료
      - rejected           : 관리자 최종 거절
    """

    __tablename__ = "knowledge_verifications"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_answer', 'awaiting_verdict', 'asker_confirmed', "
            "'asker_rejected', 'approved', 'rejected')",
            name="ck_knowledge_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 질문자 (서류 인증자)
    asker_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    # 응답자 (인증을 받으려는 사람)
    respondent_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True
    )
    hospital_id: Mapped[int] = mapped_column(
        ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    admit_ym: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    discharge_ym: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    period_text: Mapped[str] = mapped_column(String(120), nullable=False)

    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(24), default="awaiting_answer", nullable=False, index=True
    )
    reviewer_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # 질문/응답을 주고받은 디스코드 메시지 (재시작 후에도 버튼이 동작하도록 저장)
    channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ConsentRecord(Base):
    """이용자 동의 이력 (개인정보 처리방침·서비스 이용약관).

    인증(입원 이력 등록/서류 제출)을 진행하려면 약관 및 개인정보 처리방침에
    필수 동의해야 한다. 문서 종류별·버전별로 동의 시각을 기록해 두어,
    방침이 개정되어 버전이 올라가면 다시 동의를 받을 수 있도록 한다.

    doc_type: 'privacy' | 'terms'
    """

    __tablename__ = "consent_records"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "doc_type", "doc_version", name="uq_consent_user_doc"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    doc_type: Mapped[str] = mapped_column(String(20), nullable=False)
    doc_version: Mapped[str] = mapped_column(String(20), nullable=False)
    agreed_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )


class HospitalRequest(Base):
    """사용자가 직접 입력한 '병원 추가' 요청 (관리자 승인 후 목록에 반영).

    기본 목록에는 서울대학교병원(62병동)·서울대학교어린이병원(7A병동)만 두고,
    그 외 병원은 사용자가 직접 입력해 요청하면 관리자가 검토 후 승인한다.

    status: 'pending' | 'approved' | 'rejected'
    """

    __tablename__ = "hospital_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_hospital_request_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    ward: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False, index=True
    )
    reviewer_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class HospitalOptIn(Base):
    """사용자가 커뮤니티 패널에서 직접 '선택'한 병원 역할.

    서버 역할(role)을 최소화하기 위해, 병원 역할은 사용자가 원할 때만
    부여하고 선택 상태는 DB 에 저장한다.
    """

    __tablename__ = "hospital_optins"
    __table_args__ = (
        UniqueConstraint("user_id", "hospital_id", name="uq_hospital_optin"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    hospital_id: Mapped[int] = mapped_column(
        ForeignKey("hospitals.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), nullable=False
    )
