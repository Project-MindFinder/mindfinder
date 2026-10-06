"""데이터 접근 함수 모음 (리포지토리 계층, SQLAlchemy ORM).

ORM 쿼리를 이 파일에 모아두어 코그(cog) 코드에서는 비즈니스 로직에 집중한다.
각 메서드는 자체 세션을 열고 커밋까지 처리한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import delete, func, or_, select, update

from bot.db.database import Database
from bot.db.models import (
    Admission,
    AuditLog,
    ConsentRecord,
    Hospital,
    HospitalAlias,
    HospitalOptIn,
    HospitalRequest,
    KnowledgeVerification,
    ManagedChannel,
    ManagedRole,
    MatchChannel,
    MatchChannelMember,
    MatchPreference,
    MatchRequest,
    User,
    UserDisplayPreference,
    VerificationRequest,
)


def ym(year: int, month: int) -> int:
    """연·월을 비교 가능한 정수로 변환한다. (year*12 + month-1)"""
    return year * 12 + (month - 1)


def _utcnow() -> datetime:
    """시간대 미포함(naive) UTC 현재 시각을 반환한다.

    DB 컬럼이 naive datetime 이므로 tzinfo 를 제거해 저장한다.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


logger = logging.getLogger(__name__)


class UserRepository:
    """users 테이블 접근."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def ensure_user(self, discord_id: int) -> None:
        """사용자가 없으면 생성한다."""
        async with self.db.session() as session:
            user = await session.get(User, discord_id)
            if user is None:
                session.add(User(discord_id=discord_id))
                await session.commit()

    async def exists(self, discord_id: int) -> bool:
        async with self.db.session() as session:
            return await session.get(User, discord_id) is not None


class HospitalRepository:
    """hospitals / hospital_aliases 테이블 접근."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_by_name(self, name: str) -> Optional[Hospital]:
        async with self.db.session() as session:
            result = await session.execute(
                select(Hospital).where(Hospital.name == name)
            )
            return result.scalar_one_or_none()

    async def get_by_normalized(self, normalized: str) -> Optional[Hospital]:
        async with self.db.session() as session:
            result = await session.execute(
                select(Hospital).where(Hospital.normalized_name == normalized)
            )
            return result.scalar_one_or_none()

    async def get_by_alias(self, alias: str) -> Optional[Hospital]:
        async with self.db.session() as session:
            result = await session.execute(
                select(Hospital)
                .join(HospitalAlias, HospitalAlias.hospital_id == Hospital.id)
                .where(HospitalAlias.alias == alias)
            )
            return result.scalar_one_or_none()

    async def get_by_id(self, hospital_id: int) -> Optional[Hospital]:
        async with self.db.session() as session:
            return await session.get(Hospital, hospital_id)

    async def list_all(self) -> Sequence[Hospital]:
        async with self.db.session() as session:
            result = await session.execute(select(Hospital).order_by(Hospital.name))
            return result.scalars().all()

    async def create(
        self,
        name: str,
        normalized_name: str,
        *,
        ward: Optional[str] = None,
        auto_verify: bool = False,
    ) -> Hospital:
        """새 병원을 생성하고 반환한다 (병동·자동검증 플래그 포함)."""
        async with self.db.session() as session:
            hospital = Hospital(
                name=name,
                normalized_name=normalized_name,
                ward=ward,
                auto_verify=auto_verify,
            )
            session.add(hospital)
            await session.commit()
            await session.refresh(hospital)
            return hospital

    async def set_ward(self, hospital_id: int, ward: Optional[str]) -> None:
        """병원의 병동을 저장한다."""
        async with self.db.session() as session:
            hospital = await session.get(Hospital, hospital_id)
            if hospital is not None:
                hospital.ward = ward
                await session.commit()

    async def set_auto_verify(self, hospital_id: int, enabled: bool) -> None:
        """자동 진위확인 지원 여부를 저장한다."""
        async with self.db.session() as session:
            hospital = await session.get(Hospital, hospital_id)
            if hospital is not None:
                hospital.auto_verify = enabled
                await session.commit()

    async def list_auto_verify(self) -> Sequence[Hospital]:
        """자동 진위확인을 지원하는 병원 목록."""
        async with self.db.session() as session:
            result = await session.execute(
                select(Hospital).where(Hospital.auto_verify.is_(True))
            )
            return result.scalars().all()

    async def add_alias(self, hospital_id: int, alias: str) -> None:
        async with self.db.session() as session:
            existing = await session.execute(
                select(HospitalAlias).where(HospitalAlias.alias == alias)
            )
            if existing.scalar_one_or_none() is None:
                session.add(HospitalAlias(hospital_id=hospital_id, alias=alias))
                await session.commit()

    async def delete(self, hospital_id: int) -> None:
        """병원을 삭제한다 (별칭 포함)."""
        async with self.db.session() as session:
            await session.execute(
                delete(HospitalAlias).where(HospitalAlias.hospital_id == hospital_id)
            )
            hospital = await session.get(Hospital, hospital_id)
            if hospital is not None:
                await session.delete(hospital)
            await session.commit()


class AdmissionRepository:
    """admissions 테이블 접근."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def add(
        self,
        user_id: int,
        hospital_id: int,
        admit_year: int,
        admit_month: int,
        discharge_year: Optional[int],
        discharge_month: Optional[int],
        is_ongoing: bool,
        is_community: bool = True,
    ) -> int:
        """입원 이력을 추가하고 id 를 반환한다.

        ``is_community`` 는 병원·기간 커뮤니티 대상 여부다 (정신과 입원만 True).
        """
        async with self.db.session() as session:
            admission = Admission(
                user_id=user_id,
                hospital_id=hospital_id,
                admit_year=admit_year,
                admit_month=admit_month,
                discharge_year=discharge_year,
                discharge_month=discharge_month,
                is_ongoing=is_ongoing,
                admit_ym=ym(admit_year, admit_month),
                discharge_ym=None if is_ongoing else ym(discharge_year, discharge_month),
                verification="self_reported",
                is_community=is_community,
            )
            session.add(admission)
            await session.commit()
            await session.refresh(admission)
            return admission.id

    async def list_by_user(self, user_id: int) -> Sequence[Admission]:
        async with self.db.session() as session:
            result = await session.execute(
                select(Admission)
                .where(Admission.user_id == user_id)
                .order_by(Admission.admit_ym.desc())
            )
            return result.scalars().all()

    async def count_by_hospital(self, hospital_id: int) -> int:
        """특정 병원에 등록된 입원 이력 수."""
        async with self.db.session() as session:
            result = await session.execute(
                select(func.count())
                .select_from(Admission)
                .where(Admission.hospital_id == hospital_id)
            )
            return int(result.scalar_one())

    async def list_verified_hospital_ids(self) -> Sequence[int]:
        """인증(verified)된 입원 이력이 있는 병원 id 집합 (중복 제거).

        '병원 역할 선택(opt-in)' 없이, 서류/지식 인증을 받은 병원의
        채널·역할을 자동으로 확보하기 위해 사용한다.
        """
        async with self.db.session() as session:
            result = await session.execute(
                select(Admission.hospital_id)
                .where(Admission.verification == "verified")
                .distinct()
            )
            return [row[0] for row in result.all()]

    async def delete_by_hospital(self, hospital_id: int) -> int:
        """특정 병원의 입원 이력을 모두 삭제한다 (병원 정리 시).

        Returns: 삭제한 행 수.
        """
        async with self.db.session() as session:
            result = await session.execute(
                delete(Admission).where(Admission.hospital_id == hospital_id)
            )
            await session.commit()
            return result.rowcount or 0

    async def get_by_id(self, admission_id: int) -> Optional[Admission]:
        async with self.db.session() as session:
            return await session.get(Admission, admission_id)

    async def update(
        self,
        admission_id: int,
        hospital_id: int,
        admit_year: int,
        admit_month: int,
        discharge_year: Optional[int],
        discharge_month: Optional[int],
        is_ongoing: bool,
    ) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(Admission)
                .where(Admission.id == admission_id)
                .values(
                    hospital_id=hospital_id,
                    admit_year=admit_year,
                    admit_month=admit_month,
                    discharge_year=discharge_year,
                    discharge_month=discharge_month,
                    is_ongoing=is_ongoing,
                    admit_ym=ym(admit_year, admit_month),
                    discharge_ym=(
                        None if is_ongoing else ym(discharge_year, discharge_month)
                    ),
                )
            )
            await session.commit()

    async def delete(self, admission_id: int) -> None:
        async with self.db.session() as session:
            await session.execute(
                delete(Admission).where(Admission.id == admission_id)
            )
            await session.commit()

    async def set_verification(self, admission_id: int, status: str) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(Admission)
                .where(Admission.id == admission_id)
                .values(verification=status)
            )
            await session.commit()

    async def reset_rejected(self) -> int:
        """반려(rejected)된 이력을 자기 신고 상태로 되돌린다 (재시도 허용).

        Returns: 되돌린 행 수.
        """
        async with self.db.session() as session:
            result = await session.execute(
                update(Admission)
                .where(Admission.verification == "rejected")
                .values(verification="self_reported")
            )
            await session.commit()
            return result.rowcount or 0

    async def find_overlapping(
        self,
        hospital_id: int,
        admit_ym_val: int,
        discharge_ym_val: Optional[int],
        exclude_user_id: Optional[int] = None,
    ) -> Sequence[Admission]:
        """같은 병원에서 기간이 겹치는 다른 사용자의 입원 이력을 찾는다.

        겹침 조건 (양끝 포함):  A.start <= B.end  AND  B.start <= A.end
        입원 중(discharge_ym IS NULL)은 끝이 무한대로 취급한다.
        """
        end = discharge_ym_val if discharge_ym_val is not None else 10**9
        stmt = select(Admission).where(
            Admission.hospital_id == hospital_id,
            Admission.admit_ym <= end,
            or_(
                Admission.discharge_ym.is_(None),
                Admission.discharge_ym >= admit_ym_val,
            ),
        )
        if exclude_user_id is not None:
            stmt = stmt.where(Admission.user_id != exclude_user_id)
        stmt = stmt.order_by(Admission.admit_ym)
        async with self.db.session() as session:
            result = await session.execute(stmt)
            return result.scalars().all()


class VerificationRepository:
    """verification_requests 테이블 접근."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self, user_id: int, admission_id: Optional[int], *, method: str = "document"
    ) -> int:
        async with self.db.session() as session:
            request = VerificationRequest(
                user_id=user_id, admission_id=admission_id, method=method
            )
            session.add(request)
            await session.commit()
            await session.refresh(request)
            return request.id

    async def create_pending_manual(
        self,
        user_id: int,
        *,
        hospital_name: Optional[str] = None,
        ward: Optional[str] = None,
        method: str = "document",
    ) -> int:
        """자료만 제출한 수동 심사 요청을 생성한다 (관리자가 병원/기간 확정)."""
        async with self.db.session() as session:
            request = VerificationRequest(
                user_id=user_id,
                admission_id=None,
                method=method,
                pending_hospital_name=hospital_name,
                pending_ward=ward,
            )
            session.add(request)
            await session.commit()
            await session.refresh(request)
            return request.id

    async def set_admission(self, request_id: int, admission_id: int) -> None:
        """수동 심사 승인 시 생성된 이력을 요청에 연결한다."""
        async with self.db.session() as session:
            await session.execute(
                update(VerificationRequest)
                .where(VerificationRequest.id == request_id)
                .values(admission_id=admission_id)
            )
            await session.commit()

    async def find_by_document_no(
        self, document_no: str
    ) -> Optional[VerificationRequest]:
        """문서확인번호로 기존 검증 요청을 찾는다 (중복 검증 방지)."""
        if not document_no:
            return None
        async with self.db.session() as session:
            result = await session.execute(
                select(VerificationRequest).where(
                    VerificationRequest.document_no == document_no
                )
            )
            return result.scalar_one_or_none()

    async def create_auto_verified(
        self,
        user_id: int,
        admission_id: Optional[int],
        *,
        document_no: str,
        is_psychiatric: bool,
        truth_status: str,
        truth_message: Optional[str],
        department_summary: Optional[str],
        approved: bool,
        note: Optional[str] = None,
        proof_fingerprint: Optional[str] = None,
        proof_shingles: Optional[str] = None,
    ) -> int:
        """진위확인 결과를 담은 검증 요청을 생성한다.

        관리자 수동 심사 없이 자동 판정 결과를 그대로 저장한다.
        서류 원본 대신 지문(fingerprint)과 유사도 데이터(shingles)만 보관한다.
        """
        async with self.db.session() as session:
            request = VerificationRequest(
                user_id=user_id,
                admission_id=admission_id,
                status="approved" if approved else "rejected",
                auto_verified=True,
                document_no=document_no,
                is_psychiatric=is_psychiatric,
                truth_status=truth_status,
                truth_message=truth_message,
                department_summary=department_summary,
                note=note,
                proof_fingerprint=proof_fingerprint,
                proof_shingles=proof_shingles,
                reviewed_at=_utcnow(),
            )
            session.add(request)
            await session.commit()
            await session.refresh(request)
            return request.id

    async def set_fingerprint(
        self,
        request_id: int,
        proof_fingerprint: str,
        proof_shingles: str,
    ) -> None:
        """검증 요청에 서류 지문(원본 미보관)을 기록한다."""
        async with self.db.session() as session:
            await session.execute(
                update(VerificationRequest)
                .where(VerificationRequest.id == request_id)
                .values(
                    proof_fingerprint=proof_fingerprint,
                    proof_shingles=proof_shingles,
                )
            )
            await session.commit()

    async def list_fingerprints(self) -> Sequence[tuple[str, str]]:
        """저장된 (지문, shingles) 목록을 반환한다 (유사 서류 대조용)."""
        async with self.db.session() as session:
            result = await session.execute(
                select(
                    VerificationRequest.proof_fingerprint,
                    VerificationRequest.proof_shingles,
                ).where(VerificationRequest.proof_fingerprint.is_not(None))
            )
            return [
                (row[0], row[1] or "")
                for row in result.all()
                if row[0]
            ]

    async def set_proof_message(
        self, request_id: int, channel_id: int, message_id: int
    ) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(VerificationRequest)
                .where(VerificationRequest.id == request_id)
                .values(proof_channel_id=channel_id, proof_message_id=message_id)
            )
            await session.commit()

    async def get_by_id(self, request_id: int) -> Optional[VerificationRequest]:
        async with self.db.session() as session:
            return await session.get(VerificationRequest, request_id)

    async def delete(self, request_id: int) -> None:
        """검증 요청을 삭제한다 (실패/중복 재시도 초기화용)."""
        async with self.db.session() as session:
            req = await session.get(VerificationRequest, request_id)
            if req is not None:
                await session.delete(req)
            await session.commit()

    async def delete_by_document_no(self, document_no: str) -> None:
        """문서확인번호로 검증 요청을 삭제한다 (실패 재시도 초기화용)."""
        if not document_no:
            return
        async with self.db.session() as session:
            await session.execute(
                delete(VerificationRequest).where(
                    VerificationRequest.document_no == document_no
                )
            )
            await session.commit()

    async def delete_rejected(self) -> int:
        """자동 진위확인이 실패(rejected)한 기록을 삭제한다 (재시도 허용).

        Returns: 삭제한 행 수.
        """
        async with self.db.session() as session:
            result = await session.execute(
                delete(VerificationRequest).where(
                    VerificationRequest.status == "rejected",
                    VerificationRequest.auto_verified.is_(True),
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def list_pending(self) -> Sequence[VerificationRequest]:
        async with self.db.session() as session:
            result = await session.execute(
                select(VerificationRequest)
                .where(
                    VerificationRequest.status == "pending",
                    VerificationRequest.method == "document",
                )
                .order_by(VerificationRequest.created_at)
            )
            return result.scalars().all()

    async def list_pending_by_method(
        self, method: str
    ) -> Sequence[VerificationRequest]:
        """특정 인증 방식의 대기 중 요청 목록 (예: 기타 수동 승인)."""
        async with self.db.session() as session:
            result = await session.execute(
                select(VerificationRequest)
                .where(
                    VerificationRequest.status == "pending",
                    VerificationRequest.method == method,
                )
                .order_by(VerificationRequest.created_at)
            )
            return result.scalars().all()

    async def resolve(
        self,
        request_id: int,
        status: str,
        reviewer_id: int,
        note: Optional[str] = None,
    ) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(VerificationRequest)
                .where(VerificationRequest.id == request_id)
                .values(
                    status=status,
                    reviewer_id=reviewer_id,
                    note=note,
                    reviewed_at=_utcnow(),
                )
            )
            await session.commit()

    async def list_by_user(self, user_id: int) -> Sequence[VerificationRequest]:
        async with self.db.session() as session:
            result = await session.execute(
                select(VerificationRequest)
                .where(VerificationRequest.user_id == user_id)
                .order_by(VerificationRequest.created_at.desc())
            )
            return result.scalars().all()


class MatchPreferenceRepository:
    """match_preferences 테이블 접근."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def set_enabled(self, user_id: int, enabled: bool) -> None:
        async with self.db.session() as session:
            pref = await session.get(MatchPreference, user_id)
            if pref is None:
                session.add(MatchPreference(user_id=user_id, enabled=enabled))
            else:
                pref.enabled = enabled
            await session.commit()

    async def is_enabled(self, user_id: int) -> bool:
        async with self.db.session() as session:
            pref = await session.get(MatchPreference, user_id)
            return bool(pref.enabled) if pref else False


class MatchRequestRepository:
    """match_requests 테이블 접근."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self, requester_id: int, target_id: int, admission_id: Optional[int]
    ) -> int:
        async with self.db.session() as session:
            request = MatchRequest(
                requester_id=requester_id,
                target_id=target_id,
                admission_id=admission_id,
            )
            session.add(request)
            await session.commit()
            await session.refresh(request)
            return request.id

    async def get_by_id(self, request_id: int) -> Optional[MatchRequest]:
        async with self.db.session() as session:
            return await session.get(MatchRequest, request_id)

    async def find_pending_between(
        self, user_a: int, user_b: int
    ) -> Optional[MatchRequest]:
        """두 사용자 사이에 이미 대기 중인 매칭 신청이 있는지 확인한다."""
        async with self.db.session() as session:
            result = await session.execute(
                select(MatchRequest).where(
                    MatchRequest.status == "pending",
                    or_(
                        (MatchRequest.requester_id == user_a)
                        & (MatchRequest.target_id == user_b),
                        (MatchRequest.requester_id == user_b)
                        & (MatchRequest.target_id == user_a),
                    ),
                )
            )
            return result.scalars().first()

    async def resolve(self, request_id: int, status: str) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(MatchRequest)
                .where(MatchRequest.id == request_id)
                .values(status=status, responded_at=_utcnow())
            )
            await session.commit()

    async def list_incoming_pending(self, target_id: int) -> Sequence[MatchRequest]:
        async with self.db.session() as session:
            result = await session.execute(
                select(MatchRequest)
                .where(
                    MatchRequest.target_id == target_id,
                    MatchRequest.status == "pending",
                )
                .order_by(MatchRequest.created_at)
            )
            return result.scalars().all()


class MatchChannelRepository:
    """match_channels / match_channel_members 테이블 접근."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        discord_channel_id: int,
        guild_id: int,
        hospital_id: Optional[int],
        member_ids: Sequence[int],
    ) -> int:
        """새 매칭 채널을 등록하고 id 를 반환한다."""
        async with self.db.session() as session:
            channel = MatchChannel(
                discord_channel_id=discord_channel_id,
                guild_id=guild_id,
                hospital_id=hospital_id,
            )
            session.add(channel)
            await session.flush()
            for user_id in dict.fromkeys(member_ids):  # 중복 제거, 순서 유지
                session.add(
                    MatchChannelMember(match_channel_id=channel.id, user_id=user_id)
                )
            await session.commit()
            await session.refresh(channel)
            return channel.id

    async def get_by_id(self, match_channel_id: int) -> Optional[MatchChannel]:
        async with self.db.session() as session:
            return await session.get(MatchChannel, match_channel_id)

    async def get_by_discord_id(
        self, discord_channel_id: int
    ) -> Optional[MatchChannel]:
        async with self.db.session() as session:
            result = await session.execute(
                select(MatchChannel).where(
                    MatchChannel.discord_channel_id == discord_channel_id
                )
            )
            return result.scalar_one_or_none()

    async def find_open_for_users(
        self, user_ids: Sequence[int], hospital_id: Optional[int] = None
    ) -> Optional[MatchChannel]:
        """주어진 사용자들이 모두 참여 중인 열린 채널을 찾는다.

        여러 명이 한 채널을 공유하는 흐름에서, 이미 함께 있는 채널이 있으면
        새로 만들지 않고 재사용하기 위해 사용한다.
        """
        wanted = list(dict.fromkeys(user_ids))
        if not wanted:
            return None
        async with self.db.session() as session:
            stmt = (
                select(MatchChannel)
                .join(
                    MatchChannelMember,
                    MatchChannelMember.match_channel_id == MatchChannel.id,
                )
                .where(
                    MatchChannel.status == "open",
                    MatchChannelMember.user_id.in_(wanted),
                )
                .group_by(MatchChannel.id)
                .having(
                    func.count(func.distinct(MatchChannelMember.user_id)) == len(wanted)
                )
            )
            if hospital_id is not None:
                stmt = stmt.where(MatchChannel.hospital_id == hospital_id)
            result = await session.execute(stmt)
            return result.scalars().first()

    async def list_open_for_user(self, user_id: int) -> Sequence[MatchChannel]:
        """사용자가 참여 중인 열린 매칭 채널 목록."""
        async with self.db.session() as session:
            result = await session.execute(
                select(MatchChannel)
                .join(
                    MatchChannelMember,
                    MatchChannelMember.match_channel_id == MatchChannel.id,
                )
                .where(
                    MatchChannel.status == "open",
                    MatchChannelMember.user_id == user_id,
                )
                .order_by(MatchChannel.created_at)
            )
            return result.scalars().all()

    async def list_members(self, match_channel_id: int) -> Sequence[int]:
        """채널 참여자 user_id 목록."""
        async with self.db.session() as session:
            result = await session.execute(
                select(MatchChannelMember.user_id).where(
                    MatchChannelMember.match_channel_id == match_channel_id
                )
            )
            return list(result.scalars().all())

    async def add_member(self, match_channel_id: int, user_id: int) -> None:
        """채널에 참여자를 추가한다 (이미 있으면 무시)."""
        async with self.db.session() as session:
            existing = await session.execute(
                select(MatchChannelMember).where(
                    MatchChannelMember.match_channel_id == match_channel_id,
                    MatchChannelMember.user_id == user_id,
                )
            )
            if existing.scalar_one_or_none() is None:
                session.add(
                    MatchChannelMember(
                        match_channel_id=match_channel_id, user_id=user_id
                    )
                )
                await session.commit()

    async def close(self, match_channel_id: int) -> None:
        """채널을 닫힘 상태로 표시한다."""
        async with self.db.session() as session:
            await session.execute(
                update(MatchChannel)
                .where(MatchChannel.id == match_channel_id)
                .values(status="closed", closed_at=_utcnow())
            )
            await session.commit()


class AuditRepository:
    """audit_logs 테이블 접근."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def log(
        self,
        action: str,
        actor_id: Optional[int] = None,
        target: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        async with self.db.session() as session:
            session.add(
                AuditLog(action=action, actor_id=actor_id, target=target, detail=detail)
            )
            await session.commit()


class ManagedRoleRepository:
    """managed_roles 테이블 접근 (봇이 관리하는 역할 매핑)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, guild_id: int, key: str) -> Optional[ManagedRole]:
        async with self.db.session() as session:
            result = await session.execute(
                select(ManagedRole).where(
                    ManagedRole.guild_id == guild_id, ManagedRole.key == key
                )
            )
            return result.scalar_one_or_none()

    async def list_for_guild(self, guild_id: int) -> Sequence[ManagedRole]:
        """길드의 모든 관리 역할 매핑 (이름 변경 마이그레이션용)."""
        async with self.db.session() as session:
            result = await session.execute(
                select(ManagedRole).where(ManagedRole.guild_id == guild_id)
            )
            return result.scalars().all()

    async def list_all(self) -> Sequence[ManagedRole]:
        """모든 길드의 관리 역할 매핑."""
        async with self.db.session() as session:
            result = await session.execute(select(ManagedRole))
            return result.scalars().all()

    async def upsert(
        self, guild_id: int, key: str, discord_id: int, name: str
    ) -> None:
        async with self.db.session() as session:
            result = await session.execute(
                select(ManagedRole).where(
                    ManagedRole.guild_id == guild_id, ManagedRole.key == key
                )
            )
            role = result.scalar_one_or_none()
            if role is None:
                session.add(
                    ManagedRole(
                        guild_id=guild_id,
                        key=key,
                        discord_id=discord_id,
                        name=name,
                    )
                )
            else:
                role.discord_id = discord_id
                role.name = name
            await session.commit()


class ManagedChannelRepository:
    """managed_channels 테이블 접근 (봇이 관리하는 채널/카테고리 매핑)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, guild_id: int, key: str) -> Optional[ManagedChannel]:
        async with self.db.session() as session:
            result = await session.execute(
                select(ManagedChannel).where(
                    ManagedChannel.guild_id == guild_id, ManagedChannel.key == key
                )
            )
            return result.scalar_one_or_none()

    async def list_for_guild(self, guild_id: int) -> Sequence[ManagedChannel]:
        """길드의 봇 관리 채널/카테고리 매핑 전체 (이름/위치 재정비용)."""
        async with self.db.session() as session:
            result = await session.execute(
                select(ManagedChannel).where(ManagedChannel.guild_id == guild_id)
            )
            return result.scalars().all()

    async def upsert(
        self,
        guild_id: int,
        kind: str,
        key: str,
        discord_id: int,
        name: str,
    ) -> None:
        async with self.db.session() as session:
            result = await session.execute(
                select(ManagedChannel).where(
                    ManagedChannel.guild_id == guild_id, ManagedChannel.key == key
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                session.add(
                    ManagedChannel(
                        guild_id=guild_id,
                        kind=kind,
                        key=key,
                        discord_id=discord_id,
                        name=name,
                    )
                )
            else:
                row.kind = kind
                row.discord_id = discord_id
                row.name = name
            await session.commit()


class KnowledgeVerificationRepository:
    """knowledge_verifications 테이블 접근 (지식기반 인증)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        asker_id: int,
        respondent_id: int,
        hospital_id: int,
        admit_ym: int,
        discharge_ym: Optional[int],
        period_text: str,
        question: str,
    ) -> int:
        async with self.db.session() as session:
            row = KnowledgeVerification(
                asker_id=asker_id,
                respondent_id=respondent_id,
                hospital_id=hospital_id,
                admit_ym=admit_ym,
                discharge_ym=discharge_ym,
                period_text=period_text,
                question=question,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    async def get_by_id(self, request_id: int) -> Optional[KnowledgeVerification]:
        async with self.db.session() as session:
            return await session.get(KnowledgeVerification, request_id)

    async def set_message(
        self, request_id: int, channel_id: int, message_id: int
    ) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(KnowledgeVerification)
                .where(KnowledgeVerification.id == request_id)
                .values(channel_id=channel_id, message_id=message_id)
            )
            await session.commit()

    async def set_answer(self, request_id: int, answer: str) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(KnowledgeVerification)
                .where(KnowledgeVerification.id == request_id)
                .values(
                    answer=answer,
                    status="awaiting_verdict",
                    answered_at=_utcnow(),
                )
            )
            await session.commit()

    async def set_status(
        self,
        request_id: int,
        status: str,
        *,
        reviewer_id: Optional[int] = None,
        note: Optional[str] = None,
    ) -> None:
        async with self.db.session() as session:
            values: dict = {"status": status, "resolved_at": _utcnow()}
            if reviewer_id is not None:
                values["reviewer_id"] = reviewer_id
            if note is not None:
                values["note"] = note
            await session.execute(
                update(KnowledgeVerification)
                .where(KnowledgeVerification.id == request_id)
                .values(**values)
            )
            await session.commit()

    async def list_for_respondent(
        self, respondent_id: int, status: str = "awaiting_answer"
    ) -> Sequence[KnowledgeVerification]:
        async with self.db.session() as session:
            result = await session.execute(
                select(KnowledgeVerification)
                .where(
                    KnowledgeVerification.respondent_id == respondent_id,
                    KnowledgeVerification.status == status,
                )
                .order_by(KnowledgeVerification.created_at.desc())
            )
            return result.scalars().all()

    async def list_by_status(
        self, status: str
    ) -> Sequence[KnowledgeVerification]:
        """상태별 지식기반 인증 요청 목록 (재시작 후 영구 뷰 재등록용)."""
        async with self.db.session() as session:
            result = await session.execute(
                select(KnowledgeVerification)
                .where(KnowledgeVerification.status == status)
                .order_by(KnowledgeVerification.created_at)
            )
            return result.scalars().all()

    async def list_for_asker(
        self, asker_id: int, status: Optional[str] = None
    ) -> Sequence[KnowledgeVerification]:
        async with self.db.session() as session:
            stmt = select(KnowledgeVerification).where(
                KnowledgeVerification.asker_id == asker_id
            )
            if status is not None:
                stmt = stmt.where(KnowledgeVerification.status == status)
            result = await session.execute(
                stmt.order_by(KnowledgeVerification.created_at.desc())
            )
            return result.scalars().all()

    async def list_pending_review(self) -> Sequence[KnowledgeVerification]:
        """관리자 최종 승인 대기 목록 (asker 가 정답 확인한 건)."""
        async with self.db.session() as session:
            result = await session.execute(
                select(KnowledgeVerification)
                .where(KnowledgeVerification.status == "asker_confirmed")
                .order_by(KnowledgeVerification.resolved_at)
            )
            return result.scalars().all()

    async def list_question_pool(
        self, hospital_id: int, start_ym: int, end_ym: int
    ) -> Sequence[KnowledgeVerification]:
        """특정 병원·기간과 겹치는 '질문 풀'.

        asker(서류 인증자)가 올린 질문 중 입원 기간이 겹치는 것을 모은다.
        """
        async with self.db.session() as session:
            result = await session.execute(
                select(KnowledgeVerification)
                .where(
                    KnowledgeVerification.hospital_id == hospital_id,
                    KnowledgeVerification.admit_ym <= end_ym,
                    or_(
                        KnowledgeVerification.discharge_ym.is_(None),
                        KnowledgeVerification.discharge_ym >= start_ym,
                    ),
                )
                .order_by(KnowledgeVerification.created_at.desc())
            )
            return result.scalars().all()


class ConsentRepository:
    """consent_records 테이블 접근 (개인정보 처리방침·서비스 이용약관 동의)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self, user_id: int, doc_type: str, doc_version: str
    ) -> None:
        """동의를 기록한다 (같은 문서·버전이 이미 있으면 무시)."""
        async with self.db.session() as session:
            existing = await session.execute(
                select(ConsentRecord).where(
                    ConsentRecord.user_id == user_id,
                    ConsentRecord.doc_type == doc_type,
                    ConsentRecord.doc_version == doc_version,
                )
            )
            if existing.scalars().first() is not None:
                return
            session.add(
                ConsentRecord(
                    user_id=user_id,
                    doc_type=doc_type,
                    doc_version=doc_version,
                )
            )
            await session.commit()

    async def has_consent(
        self, user_id: int, doc_type: str, doc_version: str
    ) -> bool:
        """특정 문서·버전에 동의한 적이 있는지 확인한다."""
        async with self.db.session() as session:
            result = await session.execute(
                select(ConsentRecord.id).where(
                    ConsentRecord.user_id == user_id,
                    ConsentRecord.doc_type == doc_type,
                    ConsentRecord.doc_version == doc_version,
                )
            )
            return result.first() is not None

    async def list_for_user(self, user_id: int) -> Sequence[ConsentRecord]:
        """사용자의 동의 이력을 최신순으로 반환한다."""
        async with self.db.session() as session:
            result = await session.execute(
                select(ConsentRecord)
                .where(ConsentRecord.user_id == user_id)
                .order_by(ConsentRecord.agreed_at.desc())
            )
            return result.scalars().all()


class HospitalRequestRepository:
    """hospital_requests 테이블 접근 (사용자 '병원 추가' 요청)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def create(
        self,
        *,
        user_id: int,
        name: str,
        ward: Optional[str] = None,
        reason: Optional[str] = None,
    ) -> int:
        async with self.db.session() as session:
            row = HospitalRequest(
                user_id=user_id, name=name, ward=ward, reason=reason
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id

    async def get_by_id(self, request_id: int) -> Optional[HospitalRequest]:
        async with self.db.session() as session:
            return await session.get(HospitalRequest, request_id)

    async def list_by_status(self, status: str) -> Sequence[HospitalRequest]:
        async with self.db.session() as session:
            result = await session.execute(
                select(HospitalRequest)
                .where(HospitalRequest.status == status)
                .order_by(HospitalRequest.created_at.asc())
            )
            return list(result.scalars().all())

    async def set_status(
        self, request_id: int, status: str, *, reviewer_id: Optional[int] = None
    ) -> None:
        async with self.db.session() as session:
            await session.execute(
                update(HospitalRequest)
                .where(HospitalRequest.id == request_id)
                .values(
                    status=status,
                    reviewer_id=reviewer_id,
                    resolved_at=_utcnow(),
                )
            )
            await session.commit()


class HospitalOptInRepository:
    """hospital_optins 테이블 접근 (사용자가 선택한 병원 역할)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def set_optin(
        self, user_id: int, hospital_id: int, enabled: bool
    ) -> None:
        async with self.db.session() as session:
            result = await session.execute(
                select(HospitalOptIn).where(
                    HospitalOptIn.user_id == user_id,
                    HospitalOptIn.hospital_id == hospital_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                session.add(
                    HospitalOptIn(
                        user_id=user_id,
                        hospital_id=hospital_id,
                        enabled=enabled,
                    )
                )
            else:
                row.enabled = enabled
            await session.commit()

    async def is_opted_in(self, user_id: int, hospital_id: int) -> bool:
        async with self.db.session() as session:
            result = await session.execute(
                select(HospitalOptIn).where(
                    HospitalOptIn.user_id == user_id,
                    HospitalOptIn.hospital_id == hospital_id,
                    HospitalOptIn.enabled.is_(True),
                )
            )
            return result.scalar_one_or_none() is not None

    async def list_for_user(self, user_id: int) -> Sequence[int]:
        """사용자가 선택한(enabled) 병원 id 목록."""
        async with self.db.session() as session:
            result = await session.execute(
                select(HospitalOptIn.hospital_id).where(
                    HospitalOptIn.user_id == user_id,
                    HospitalOptIn.enabled.is_(True),
                )
            )
            return [row[0] for row in result.all()]

    async def list_all_enabled(self) -> Sequence[int]:
        """어떤 사용자든 선택한(enabled) 병원 id 집합 (중복 제거)."""
        async with self.db.session() as session:
            result = await session.execute(
                select(HospitalOptIn.hospital_id)
                .where(HospitalOptIn.enabled.is_(True))
                .distinct()
            )
            return [row[0] for row in result.all()]


class UserDisplayPreferenceRepository:
    """user_display_preferences 테이블 접근 (노출 범위·매칭 표시명)."""

    # 허용 범위: none(비공개) | hospital(병원만) | period(병원+기간)
    VALID_SCOPES = ("none", "hospital", "period")
    DEFAULT_SCOPE = "hospital"
    # 매칭 표시명 방식: masked(기본 마스킹) | real(실명) | nickname(병원 닉네임)
    VALID_NAME_MODES = ("masked", "real", "nickname")
    DEFAULT_NAME_MODE = "masked"

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get_scope(self, user_id: int) -> str:
        """사용자가 선택한 노출 범위 (없으면 기본값)."""
        async with self.db.session() as session:
            row = await session.get(UserDisplayPreference, user_id)
            return row.scope if row is not None else self.DEFAULT_SCOPE

    async def set_scope(self, user_id: int, scope: str) -> None:
        """노출 범위를 저장한다 (허용값이 아니면 무시)."""
        if scope not in self.VALID_SCOPES:
            return
        async with self.db.session() as session:
            row = await session.get(UserDisplayPreference, user_id)
            if row is None:
                session.add(UserDisplayPreference(user_id=user_id, scope=scope))
            else:
                row.scope = scope
            await session.commit()

    async def get_match_name(self, user_id: int) -> tuple[str, Optional[str]]:
        """매칭 표시명 설정 (mode, nickname) 을 반환한다."""
        async with self.db.session() as session:
            row = await session.get(UserDisplayPreference, user_id)
            if row is None:
                return self.DEFAULT_NAME_MODE, None
            return row.match_name_mode, row.hospital_nickname

    async def set_match_name(
        self, user_id: int, mode: str, nickname: Optional[str] = None
    ) -> None:
        """매칭 표시명 설정을 저장한다 (허용값이 아니면 무시)."""
        if mode not in self.VALID_NAME_MODES:
            return
        async with self.db.session() as session:
            row = await session.get(UserDisplayPreference, user_id)
            if row is None:
                row = UserDisplayPreference(user_id=user_id, match_name_mode=mode)
                session.add(row)
            else:
                row.match_name_mode = mode
            if nickname is not None:
                row.hospital_nickname = nickname[:50] or None
            await session.commit()
