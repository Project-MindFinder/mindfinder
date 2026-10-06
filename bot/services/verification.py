"""인증/진위확인 도메인 서비스.

UI(슬래시 커맨드, 상시 패널)에 의존하지 않는 순수 로직만 담는다.
디스코드 역할 부여나 메시지 전송 같은 부수효과는 호출자(코그)가 처리한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

from bot.utils import certificate as certificate_utils
from bot.utils import document_fingerprint as fingerprint_utils
from bot.utils import hospitals as hospital_utils
from bot.utils.periods import Period, validate_period
from bot.utils.snuh_truth import SnuhTruthClient, TruthStatus

logger = logging.getLogger(__name__)

# 자동 진위확인에 허용할 PDF 최대 크기 (10MB)
MAX_PDF_BYTES = 10 * 1024 * 1024
# 진위확인 자동 검증을 지원하는 병원 키
# snuh: 서울대학교병원(62병동), snuh_child: 서울대학교어린이병원(7A병동)
TRUTH_SUPPORTED_HOSPITALS = {"snuh", "snuh_child"}
# 자동 진위확인을 지원하는 대표 병원명 (그 외 병원은 관리자 수동 심사)
# 서울대학교병원과 서울대학교어린이병원은 서로 다른 병동이지만 동일한 진위확인 API 를 쓴다.
AUTO_VERIFY_HOSPITAL_NAMES = {"서울대학교병원", "서울대학교어린이병원"}

# 인증 상태 표시
VERIFICATION_LABEL = {
    "self_reported": "🟡 자기 신고 (미인증)",
    "other_pending": "🟠 기타 인증 심사 중",
    "verified": "🟢 인증 완료",
    "rejected": "🔴 인증 반려",
}


@dataclass
class AutoVerifyResult:
    """자동 진위확인 결과."""

    approved: bool
    title: str
    color: str  # "green" | "red" | "orange"
    detail: str  # 성공/실패 사유 또는 요약
    document_no: Optional[str] = None
    hospital_name: Optional[str] = None
    departments: Optional[str] = None
    period_text: Optional[str] = None
    truth_message: Optional[str] = None


class VerificationService:
    """입원 이력 인증 및 서울대병원 자동 진위확인 로직."""

    def __init__(self, bot: MindFinderBot) -> None:
        self.bot = bot

    # ---------- 자동/수동 판별 ----------

    def supports_auto_verify(self, hospital_name: str) -> bool:
        """해당 병원이 자동 진위확인을 지원하는지 여부.

        서울대학교병원만 자동, 그 외 병원은 관리자 수동 심사로 처리한다.
        """
        canonical = hospital_utils.resolve_canonical(hospital_name)
        return canonical in AUTO_VERIFY_HOSPITAL_NAMES

    # ---------- 서류 지문 (원본 미보관, 중복 제출 탐지) ----------

    async def check_duplicate_fingerprint(
        self, raw_text: str
    ) -> Optional[str]:
        """서류 텍스트의 지문을 기존 제출 서류와 대조한다.

        원본 PDF 를 저장하지 않고, 개인정보를 제거한 정규화 텍스트의 해시와
        MinHash 토큰만 비교한다.

        Returns:
            중복으로 판정되면 사유 문자열, 아니면 None.
        """
        if not raw_text:
            return None
        fingerprint = fingerprint_utils.fingerprint_text(raw_text)
        if not fingerprint.fingerprint:
            return None
        for stored_hash, stored_shingles in await self.bot.verifications.list_fingerprints():
            if fingerprint_utils.is_similar(
                fingerprint, stored_hash, stored_shingles
            ):
                if stored_hash == fingerprint.fingerprint:
                    return "동일한 서류가 이미 제출되어 있습니다."
                return (
                    "이미 제출된 서류와 매우 유사합니다 (표현만 다른 같은 서류일 수 있습니다)."
                )
        return None

    # ---------- 병원 정규화 ----------

    async def resolve_hospital(self, raw_name: str):
        """입력 병원명을 대표 병원으로 정규화하고, 없으면 새로 만든다."""
        canonical = hospital_utils.resolve_canonical(raw_name)
        normalized = hospital_utils.normalize_name(canonical)

        hospital = await self.bot.hospitals.get_by_normalized(normalized)
        if hospital is None:
            hospital = await self.bot.hospitals.get_by_name(canonical)
        if hospital is None:
            hospital = await self.bot.hospitals.create(canonical, normalized)
            logger.info("새 병원 등록: %s", canonical)
        return hospital

    # ---------- 자기 신고 이력 ----------

    async def register_admission(
        self,
        user_id: int,
        hospital_name: str,
        start_year: int,
        start_month: int,
        end_year: Optional[int],
        end_month: Optional[int],
        is_ongoing: bool,
    ) -> tuple[int, Period, str]:
        """자기 신고 입원 이력을 등록한다.

        Returns:
            (admission_id, period, hospital_name)

        Raises:
            PeriodError: 기간 입력이 유효하지 않을 때.
        """
        period = validate_period(
            start_year, start_month, end_year, end_month, is_ongoing
        )
        hospital = await self.resolve_hospital(hospital_name)
        await self.bot.user_repo.ensure_user(user_id)
        admission_id = await self.bot.admissions.add(
            user_id=user_id,
            hospital_id=hospital.id,
            admit_year=period.start_year,
            admit_month=period.start_month,
            discharge_year=period.end_year,
            discharge_month=period.end_month,
            is_ongoing=period.is_ongoing,
        )
        await self.bot.audit.log(
            "admission.create",
            actor_id=user_id,
            target=f"admission:{admission_id}",
            detail=f"{hospital.name} {period.text()}",
        )
        return admission_id, period, hospital.name

    # ---------- 자동 진위확인 (서울대병원) ----------

    async def auto_verify_pdf(
        self,
        user_id: int,
        filename: str,
        pdf_bytes: bytes,
        admission_id: Optional[int] = None,
    ) -> AutoVerifyResult:
        """서울대병원 입퇴원 사실확인서 PDF 를 자동 진위확인한다.

        ``admission_id`` 가 주어지면 그 이력을 사용하고(기간은 문서에서 추출),
        없으면 문서에서 추출한 병원·기간으로 새 이력을 만든다.
        호출자가 인증 역할 부여만 하면 되도록 결과를 반환한다.
        """
        settings = self.bot.settings

        # 0) 기능 활성화 여부
        if not settings.snuh_truth_enabled:
            return AutoVerifyResult(
                approved=False,
                title="기능 비활성화",
                color="orange",
                detail="자동 진위확인 기능이 비활성화되어 있습니다. 관리자에게 문의해주세요.",
            )

        # 1) 파일 형식 검증
        if not filename.lower().endswith(".pdf"):
            return AutoVerifyResult(
                approved=False,
                title="형식 오류",
                color="orange",
                detail="PDF 파일만 제출할 수 있습니다.",
            )
        if len(pdf_bytes) > MAX_PDF_BYTES:
            return AutoVerifyResult(
                approved=False,
                title="용량 초과",
                color="orange",
                detail="파일이 너무 큽니다. 10MB 이하의 PDF 를 올려주세요.",
            )

        # 2) PDF 파싱
        try:
            cert = certificate_utils.parse_certificate(pdf_bytes)
        except certificate_utils.CertificateParseError as exc:
            return AutoVerifyResult(
                approved=False,
                title="해석 실패",
                color="orange",
                detail=f"서류를 해석할 수 없습니다.\n> {exc}",
            )

        # 3) 지원 병원 여부
        if cert.hospital_key not in TRUTH_SUPPORTED_HOSPITALS:
            return AutoVerifyResult(
                approved=False,
                title="지원하지 않는 병원",
                color="orange",
                detail=(
                    "현재 **서울대학교병원(62병동)·서울대학교어린이병원(7A병동)** "
                    "발급 서류만 자동 진위확인을 지원합니다.\n"
                    "다른 병원 서류는 관리자 심사를 이용해주세요."
                ),
            )

        # 4) 정신건강의학과 입원 여부
        if not cert.is_psychiatric:
            return AutoVerifyResult(
                approved=False,
                title="정신과 이력 아님",
                color="orange",
                detail=(
                    "정신건강의학과(정신과) 입원 이력이 확인되지 않아 인증할 수 없습니다.\n"
                    f"확인된 진료과: {', '.join(cert.departments) or '없음'}"
                ),
            )

        # 5) 문서확인번호 필요
        if not cert.document_no:
            return AutoVerifyResult(
                approved=False,
                title="문서번호 없음",
                color="orange",
                detail="서류에서 문서확인번호(문서번호)를 찾지 못했습니다.",
            )

        # 6) 중복 문서 방지 (문서확인번호)
        existing = await self.bot.verifications.find_by_document_no(cert.document_no)
        if existing is not None:
            return AutoVerifyResult(
                approved=False,
                title="중복 문서",
                color="orange",
                detail="이미 등록된 문서입니다. 동일한 서류로는 중복 인증할 수 없습니다.",
                document_no=cert.document_no,
            )

        # 6-1) 중복 서류 방지 (지문/유사도). 원본은 저장하지 않고 지문만 대조한다.
        duplicate_reason = await self.check_duplicate_fingerprint(cert.raw_text)
        if duplicate_reason is not None:
            return AutoVerifyResult(
                approved=False,
                title="중복 서류",
                color="orange",
                detail=duplicate_reason,
                document_no=cert.document_no,
            )

        # 7) 입퇴원 기간 확인 → 입원 이력 생성
        period = cert.period()
        if period is None:
            return AutoVerifyResult(
                approved=False,
                title="기간 확인 불가",
                color="orange",
                detail="서류에서 입퇴원 기간을 확인할 수 없습니다.",
            )
        start_year, start_month, end_year, end_month = period

        # 서류에서 판별한 병원(키워드)을 대표 병원으로 정규화한다.
        # 서울대학교어린이병원(7A병동)과 서울대학교병원(62병동)은 서로 다른 병동이다.
        hospital = await self.resolve_hospital(
            cert.hospital_name or "서울대학교병원"
        )
        hospital_display = hospital_utils.hospital_display_name(hospital.name)
        await self.bot.user_repo.ensure_user(user_id)
        if admission_id is None:
            # 사용자가 이력을 미리 만들지 않아도 문서에서 기간을 추출해 생성한다.
            admission_id = await self.bot.admissions.add(
                user_id=user_id,
                hospital_id=hospital.id,
                admit_year=start_year,
                admit_month=start_month,
                discharge_year=end_year,
                discharge_month=end_month,
                is_ongoing=False,
            )

        # 8) 서울대병원 진위확인 API 호출
        client = SnuhTruthClient(
            base_url=settings.snuh_truth_base_url,
            timeout=settings.snuh_truth_timeout,
        )
        try:
            result = await client.verify(cert.document_no)
        except Exception as exc:  # noqa: BLE001 - 네트워크/파싱 오류 모두 안내
            logger.warning("진위확인 API 오류: %s", exc)
            await self.bot.admissions.set_verification(admission_id, "rejected")
            return AutoVerifyResult(
                approved=False,
                title="통신 오류",
                color="orange",
                detail=(
                    "진위확인 서버와 통신하지 못했습니다. 잠시 후 다시 시도해주세요.\n"
                    f"> {exc}"
                ),
                document_no=cert.document_no,
            )

        approved = result.status is TruthStatus.VALID
        department_summary = ", ".join(cert.departments) or None

        # 9) 검증 요청 기록 + 이력 상태 반영 (원본 대신 지문만 저장)
        fingerprint = fingerprint_utils.fingerprint_text(cert.raw_text)
        request_id = await self.bot.verifications.create_auto_verified(
            user_id=user_id,
            admission_id=admission_id,
            document_no=cert.document_no,
            is_psychiatric=cert.is_psychiatric,
            truth_status=result.status.value,
            truth_message=result.message,
            department_summary=department_summary,
            approved=approved,
            note=(
                "자동 진위확인 통과"
                if approved
                else f"진위확인 실패: {result.message}"
            ),
            proof_fingerprint=fingerprint.fingerprint,
            proof_shingles=fingerprint_utils.encode_shingles(fingerprint.shingles),
        )
        await self.bot.admissions.set_verification(
            admission_id, "verified" if approved else "rejected"
        )
        await self.bot.audit.log(
            "verification.auto",
            actor_id=user_id,
            target=f"verification:{request_id}",
            detail=f"{cert.document_no} {result.status.value}",
        )

        return AutoVerifyResult(
            approved=approved,
            title="자동 인증 완료" if approved else "자동 인증 실패",
            color="green" if approved else "red",
            detail=(
                f"**병원**: {hospital_display}\n"
                f"**진료과**: {department_summary or '확인 불가'}\n"
                f"**기간**: {start_year}년 {start_month}월 ~ "
                f"{end_year}년 {end_month}월\n"
                f"**문서번호**: `{cert.document_no}`\n"
                f"**발급번호**: {cert.issue_no or '?'}\n"
                f"**진위확인**: {result.message or result.status.value}"
            ),
            document_no=cert.document_no,
            hospital_name=hospital.name,
            departments=department_summary,
            period_text=(
                f"{start_year}년 {start_month}월 ~ {end_year}년 {end_month}월"
            ),
            truth_message=result.message,
        )

    # ---------- 관리자 심사 ----------

    async def submit_proof(self, user_id: int, admission_id: int) -> int:
        """서류 심사 요청을 생성하고 심사 번호를 반환한다."""
        request_id = await self.bot.verifications.create(
            user_id=user_id, admission_id=admission_id, method="document"
        )
        await self.bot.audit.log(
            "verification.request",
            actor_id=user_id,
            target=f"verification:{request_id}",
        )
        return request_id

    async def submit_other_proof(self, user_id: int, admission_id: int) -> int:
        """서류·지식 외 '기타 방법' 인증 요청을 생성한다 (관리자 수동 승인).

        서류 없이 사유(경위)만 제출하면 관리자가 대화로 사실을 확인한 뒤
        승인/거절한다. 접수 시점의 이력은 'other_pending' 으로 표시해
        아직 2단계가 아님을 명확히 한다.
        """
        request_id = await self.bot.verifications.create(
            user_id=user_id, admission_id=admission_id, method="other"
        )
        await self.bot.admissions.set_verification(admission_id, "other_pending")
        await self.bot.audit.log(
            "verification.request_other",
            actor_id=user_id,
            target=f"verification:{request_id}",
        )
        return request_id

    async def submit_document_upload(
        self,
        user_id: int,
        *,
        hospital_name: Optional[str] = None,
        ward: Optional[str] = None,
        method: str = "document",
    ) -> int:
        """사용자가 자료만 업로드한 수동 심사 요청을 생성한다.

        병원/기간은 관리자가 승인 시 확정하므로 이력은 만들지 않는다.
        """
        request_id = await self.bot.verifications.create_pending_manual(
            user_id, hospital_name=hospital_name, ward=ward, method=method
        )
        await self.bot.audit.log(
            "verification.upload",
            actor_id=user_id,
            target=f"verification:{request_id}",
        )
        return request_id

    async def approve_manual(
        self,
        request_id: int,
        reviewer_id: int,
        *,
        hospital_name: str,
        ward: Optional[str],
        start_year: int,
        start_month: int,
        end_year: Optional[int],
        end_month: Optional[int],
        is_ongoing: bool,
    ):
        """관리자가 수동 심사를 승인한다.

        관리자가 입력한 병원/기간으로 이력을 생성하고 검증 완료 처리한다.
        """
        req = await self.bot.verifications.get_by_id(request_id)
        if req is None or req.status != "pending":
            return None
        display = f"{hospital_name} {ward}".strip() if ward else hospital_name
        admission_id, period, resolved_name = await self.register_admission(
            user_id=req.user_id,
            hospital_name=display,
            start_year=start_year,
            start_month=start_month,
            end_year=end_year,
            end_month=end_month,
            is_ongoing=is_ongoing,
        )
        await self.bot.admissions.set_verification(admission_id, "verified")
        await self.bot.verifications.set_admission(request_id, admission_id)
        await self.bot.verifications.resolve(request_id, "approved", reviewer_id)
        await self.bot.audit.log(
            "verification.approve_manual",
            actor_id=reviewer_id,
            target=f"verification:{request_id}",
            detail=f"{resolved_name} {period.text()}",
        )
        req.admission_id = admission_id
        return req

    async def approve(self, request_id: int, reviewer_id: int):
        """대기 중 심사를 승인한다. 성공 시 검증 요청을 반환한다."""
        req = await self.bot.verifications.get_by_id(request_id)
        if req is None or req.status != "pending":
            return None
        await self.bot.verifications.resolve(request_id, "approved", reviewer_id)
        if req.admission_id:
            await self.bot.admissions.set_verification(req.admission_id, "verified")
        await self.bot.audit.log(
            "verification.approve",
            actor_id=reviewer_id,
            target=f"verification:{request_id}",
        )
        return req

    async def reject(self, request_id: int, reviewer_id: int, note: Optional[str]):
        """대기 중 심사를 거절한다. 성공 시 검증 요청을 반환한다."""
        req = await self.bot.verifications.get_by_id(request_id)
        if req is None or req.status != "pending":
            return None
        await self.bot.verifications.resolve(
            request_id, "rejected", reviewer_id, note=note
        )
        if req.admission_id:
            await self.bot.admissions.set_verification(req.admission_id, "rejected")
        await self.bot.audit.log(
            "verification.reject",
            actor_id=reviewer_id,
            target=f"verification:{request_id}",
            detail=note,
        )
        return req
