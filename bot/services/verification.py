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

    async def supports_auto_verify(self, hospital) -> bool:
        """해당 병원이 자동 진위확인을 지원하는지 여부 (DB 플래그 기반).

        병원명 하드코딩 대신 DB(hospitals.auto_verify)로 판단한다.
        """
        # 병원 객체가 오면 그대로, 문자열이면 DB에서 조회한다.
        if isinstance(hospital, str):
            found = await self.bot.hospitals.get_by_name(hospital)
            if found is None:
                found = await self.bot.hospitals.get_by_normalized(
                    hospital_utils.normalize_name(hospital)
                )
            hospital = found
        if hospital is None:
            return False
        return bool(getattr(hospital, "auto_verify", False))

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
        # 주의: 같은 병원의 동일 서식 서류는 개인·기간이 달라도 서식 문구 때문에
        # 유사도가 높게 나온다. 따라서 '표현만 다른 같은 서류' 판정은 하지 않고,
        # 정규화 텍스트가 **완전히 동일한**(동일 지문) 경우만 중복으로 막는다.
        for stored_hash, _stored_shingles in await self.bot.verifications.list_fingerprints():
            if stored_hash == fingerprint.fingerprint:
                return "동일한 서류가 이미 제출되어 있습니다."
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
        hospital_id: Optional[int] = None,
    ) -> AutoVerifyResult:
        """서울대병원 입퇴원 사실확인서 PDF 를 자동 진위확인한다.

        ``hospital_id``(사용자가 선택한 병원)가 주어지면 **그 병원**으로 이력을
        만들고(기간은 문서에서 추출), 없으면 문서에서 판별한 병원을 사용한다.
        ``admission_id`` 가 주어지면 그 이력을 사용한다.
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
        #    승인(approved)된 문서만 중복으로 막는다. 실패(rejected)한 문서는
        #    재시도할 수 있도록 기존 기록을 지우고 진행한다.
        existing = await self.bot.verifications.find_by_document_no(cert.document_no)
        if existing is not None:
            if existing.status == "approved":
                if existing.user_id == user_id:
                    detail = (
                        "이미 **본인이** 이 문서로 인증을 완료했습니다. "
                        "같은 서류로는 다시 인증할 수 없습니다."
                    )
                else:
                    detail = (
                        "이미 등록된 문서입니다(다른 사용자). 동일한 서류로는 "
                        "중복 인증할 수 없습니다."
                    )
                return AutoVerifyResult(
                    approved=False,
                    title="중복 문서",
                    color="orange",
                    detail=detail,
                    document_no=cert.document_no,
                )
            # 실패했던 기록은 초기화하고 재시도한다.
            await self.bot.verifications.delete(existing.id)

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
        #    문서에 여러 건의 입원 이력이 있으면 모두 등록한다 (다중 이력).
        entries = list(cert.admissions)
        if not entries:
            # 입원 기간 행을 복원하지 못하면 대표 기간을 단일 이력으로 사용한다.
            period = cert.period()
            if period is None:
                return AutoVerifyResult(
                    approved=False,
                    title="기간 확인 불가",
                    color="orange",
                    detail="서류에서 입퇴원 기간을 확인할 수 없습니다.",
                )
            entries = [
                certificate_utils.AdmissionEntry(
                    admit_year=period[0],
                    admit_month=period[1],
                    discharge_year=period[2],
                    discharge_month=period[3],
                )
            ]
        period = entries[0].period()
        start_year, start_month, end_year, end_month = period
        # 문서에 여러 건의 입원 이력이 있으면 모두 사람이 읽기 쉬운 문자열로 만든다.
        period_texts = [entry.text() for entry in entries]

        # 사용자가 선택한 병원(hospital_id)이 있으면 그 병원을 우선한다.
        # 없으면 서류에서 판별한 병원(키워드)을 대표 병원으로 정규화한다.
        # 서울대학교어린이병원(7A병동)과 서울대학교병원(62병동)은 서로 다른 병동이다.
        if hospital_id is not None:
            hospital = await self.bot.hospitals.get_by_id(hospital_id)
        else:
            hospital = None
        if hospital is None:
            hospital = await self.resolve_hospital(
                cert.hospital_name or "서울대학교병원"
            )
        hospital_display = hospital_utils.hospital_display_name(hospital.name)
        # 7A병동(어린이병원)은 **소아정신과 입원만** 인정한다 (병동 정보 기반).
        is_child_hospital = (hospital.ward or "") == "7A병동"
        if is_child_hospital:
            child_psych = [
                e for e in entries if e.is_community_eligible(child_hospital=True)
            ]
            if not child_psych:
                return AutoVerifyResult(
                    approved=False,
                    title="소아정신과 이력 아님",
                    color="orange",
                    detail=(
                        "**서울대학교어린이병원(7A병동)** 은 **소아정신과** 입원 "
                        "이력만 인증할 수 있습니다.\n"
                        f"확인된 진료과: {', '.join(cert.departments) or '없음'}"
                    ),
                    document_no=cert.document_no,
                )
            # 소아정신과 이력만 등록·커뮤니티 대상으로 삼는다.
            entries = child_psych
            period = entries[0].period()
            start_year, start_month, end_year, end_month = period
            period_texts = [entry.text() for entry in entries]
        await self.bot.user_repo.ensure_user(user_id)
        # 검증할 이력 id 목록. 여러 건이면 **각각 별도 이력**으로 처리한다.
        if admission_id is not None:
            admission_ids: list[int] = [admission_id]
        else:
            # 사용자가 이력을 미리 만들지 않아도 문서에서 기간을 추출해 생성한다.
            # 정신과가 아닌 입원(예: 응급실 단순 관찰)은 커뮤니티 대상에서 제외한다.
            admission_ids = []
            for entry in entries:
                is_community = bool(getattr(entry, "is_community_eligible", True))
                new_id = await self.bot.admissions.add(
                    user_id=user_id,
                    hospital_id=hospital.id,
                    admit_year=entry.admit_year,
                    admit_month=entry.admit_month,
                    discharge_year=entry.discharge_year,
                    discharge_month=entry.discharge_month,
                    is_ongoing=False,
                    is_community=is_community,
                )
                admission_ids.append(new_id)
            admission_id = admission_ids[0]
            if len(admission_ids) > 1:
                logger.info(
                    "다중 입원 이력 등록: user=%s hospital=%s 건수=%d ids=%s",
                    user_id,
                    hospital.name,
                    len(admission_ids),
                    admission_ids,
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
            # 실패 시 이력을 반려하지 않고 재시도할 수 있도록 초기화한다.
            await self._reset_failed_attempt(admission_id, cert.document_no)
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
        # 진단용 로그: 파싱 결과와 진위확인 응답을 기록한다.
        logger.info(
            "자동진위 파싱: doc=%s hospital=%s dept=%s psych=%s period=%s years=%s md=%s",
            cert.document_no,
            cert.hospital_name,
            department_summary,
            cert.is_psychiatric,
            period,
            cert.years,
            cert.month_days,
        )
        logger.info(
            "자동진위 결과: status=%s message=%s",
            result.status.value,
            result.message,
        )

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
        # 문서에서 추출한 모든 입원 이력을 각각 검증 상태로 반영한다.
        for adm_id in admission_ids:
            await self.bot.admissions.set_verification(
                adm_id, "verified" if approved else "rejected"
            )
        if not approved:
            # 실패한 시도는 재시도할 수 있도록 요청/이력을 초기화한다.
            await self._reset_failed_attempt(admission_id, cert.document_no)
        await self.bot.audit.log(
            "verification.auto",
            actor_id=user_id,
            target=f"verification:{request_id}",
            detail=f"{cert.document_no} {result.status.value}",
        )

        periods_display = (
            " / ".join(period_texts)
            if period_texts
            else f"{start_year}년 {start_month}월 ~ {end_year}년 {end_month}월"
        )
        return AutoVerifyResult(
            approved=approved,
            title="자동 인증 완료" if approved else "자동 인증 실패",
            color="green" if approved else "red",
            detail=(
                f"**병원**: {hospital_display}\n"
                f"**진료과**: {department_summary or '확인 불가'}\n"
                f"**기간**: {periods_display}\n"
                f"**문서번호**: `{cert.document_no}`\n"
                f"**발급번호**: {cert.issue_no or '?'}\n"
                f"**진위확인**: {result.message or result.status.value}"
            ),
            document_no=cert.document_no,
            hospital_name=hospital.name,
            departments=department_summary,
            period_text=periods_display,
            truth_message=result.message,
        )

    async def _reset_failed_attempt(
        self, admission_id: Optional[int], document_no: Optional[str]
    ) -> None:
        """자동 진위확인 실패 시 재시도할 수 있도록 흔적을 초기화한다.

        - 이력이 'rejected' 로 남지 않도록 자기 신고 상태로 되돌린다.
        - 문서확인번호에 묶인 실패 기록(unique)을 삭제한다.
        """
        if admission_id is not None:
            try:
                await self.bot.admissions.set_verification(
                    admission_id, "self_reported"
                )
            except Exception:  # noqa: BLE001
                logger.exception("실패 이력 초기화 실패: admission=%s", admission_id)
        if document_no:
            try:
                await self.bot.verifications.delete_by_document_no(document_no)
            except Exception:  # noqa: BLE001
                logger.exception("실패 검증 기록 초기화 실패: doc=%s", document_no)

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
