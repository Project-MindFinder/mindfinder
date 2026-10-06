"""지식기반 인증 도메인 서비스.

이미 서류 인증을 마친 사용자(asker)가 자신이 입원했던 시기·병원에 실제로
있었던 사람만 알 수 있는 질문을 올리고, 응답자(respondent)가 답하면
asker 가 정답 여부를 판정한다. asker 가 정답으로 확인하면 관리자 최종
승인을 거쳐 응답자에게 해당 병원 인증을 부여한다.

역할 부여 등 디스코드 부수효과는 호출자(코그)가 처리한다.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:  # 순환 임포트 방지를 위해 타입 검사 시에만 임포트한다.
    from bot.core import MindFinderBot

from bot.db.models import Admission, KnowledgeVerification

logger = logging.getLogger(__name__)


class KnowledgeService:
    """지식기반 인증 질문/응답/판정/승인 로직."""

    def __init__(self, bot: "MindFinderBot") -> None:
        self.bot = bot

    # ---------- 질문 등록 ----------

    async def ask(
        self,
        asker_id: int,
        admission_id: int,
        respondent_id: int,
        question: str,
    ) -> tuple[Optional[int], str]:
        """질문을 등록한다.

        Returns:
            (request_id, 안내 메시지). 실패 시 request_id 는 None.
        """
        if asker_id == respondent_id:
            return None, "자기 자신에게는 질문을 보낼 수 없습니다."

        question = question.strip()
        if len(question) < 5:
            return None, "질문 내용을 조금 더 구체적으로 입력해주세요."

        # asker 의 해당 입원 이력이 서류 인증(verified) 상태여야 한다.
        admission = await self.bot.admissions.get_by_id(admission_id)
        if admission is None or admission.user_id != asker_id:
            return None, "본인의 입원 이력을 선택해주세요."
        if admission.verification != "verified":
            return None, "서류 인증을 마친 본인 이력에 대해서만 질문할 수 있습니다."

        hospital = await self.bot.hospitals.get_by_id(admission.hospital_id)
        hospital_name = hospital.name if hospital else "병원"

        request_id = await self.bot.knowledge.create(
            asker_id=asker_id,
            respondent_id=respondent_id,
            hospital_id=admission.hospital_id,
            admit_ym=admission.admit_ym,
            discharge_ym=admission.discharge_ym,
            period_text=admission.period_text(),
            question=question,
        )
        await self.bot.audit.log(
            "knowledge.ask",
            actor_id=asker_id,
            target=f"knowledge:{request_id}",
            detail=f"{hospital_name} {admission.period_text()}",
        )
        return request_id, f"{hospital_name} · {admission.period_text()}"

    # ---------- 조회 ----------

    async def list_for_respondent(
        self, respondent_id: int, status: str = "awaiting_answer"
    ) -> Sequence[KnowledgeVerification]:
        return await self.bot.knowledge.list_for_respondent(respondent_id, status)

    async def get(self, request_id: int) -> Optional[KnowledgeVerification]:
        return await self.bot.knowledge.get_by_id(request_id)

    async def list_pending_review(self) -> Sequence[KnowledgeVerification]:
        return await self.bot.knowledge.list_pending_review()

    async def hospital_name(self, hospital_id: int) -> str:
        hospital = await self.bot.hospitals.get_by_id(hospital_id)
        return hospital.name if hospital else "병원"

    # ---------- 응답 ----------

    async def answer(
        self, request_id: int, respondent_id: int, answer: str
    ) -> Optional[KnowledgeVerification]:
        """응답자가 답변을 등록한다 (상태 → awaiting_verdict)."""
        req = await self.bot.knowledge.get_by_id(request_id)
        if req is None or req.respondent_id != respondent_id:
            return None
        if req.status != "awaiting_answer":
            return None
        answer = answer.strip()
        if not answer:
            return None
        await self.bot.knowledge.set_answer(request_id, answer)
        await self.bot.audit.log(
            "knowledge.answer",
            actor_id=respondent_id,
            target=f"knowledge:{request_id}",
        )
        return await self.bot.knowledge.get_by_id(request_id)

    # ---------- 질문자 판정 ----------

    async def verdict(
        self, request_id: int, asker_id: int, correct: bool
    ) -> Optional[KnowledgeVerification]:
        """질문자가 응답의 정답 여부를 판정한다.

        correct=True  → asker_confirmed (관리자 승인 대기)
        correct=False → asker_rejected
        """
        req = await self.bot.knowledge.get_by_id(request_id)
        if req is None or req.asker_id != asker_id:
            return None
        if req.status != "awaiting_verdict":
            return None
        status = "asker_confirmed" if correct else "asker_rejected"
        await self.bot.knowledge.set_status(request_id, status)
        await self.bot.audit.log(
            "knowledge.verdict",
            actor_id=asker_id,
            target=f"knowledge:{request_id}",
            detail=status,
        )
        return await self.bot.knowledge.get_by_id(request_id)

    # ---------- 관리자 최종 승인 ----------

    async def admin_approve(
        self, request_id: int, reviewer_id: int
    ) -> Optional[KnowledgeVerification]:
        req = await self.bot.knowledge.get_by_id(request_id)
        if req is None or req.status != "asker_confirmed":
            return None
        await self.bot.knowledge.set_status(
            request_id, "approved", reviewer_id=reviewer_id
        )
        await self.bot.audit.log(
            "knowledge.approve",
            actor_id=reviewer_id,
            target=f"knowledge:{request_id}",
        )
        return await self.bot.knowledge.get_by_id(request_id)

    async def admin_reject(
        self, request_id: int, reviewer_id: int, note: Optional[str] = None
    ) -> Optional[KnowledgeVerification]:
        req = await self.bot.knowledge.get_by_id(request_id)
        if req is None or req.status != "asker_confirmed":
            return None
        await self.bot.knowledge.set_status(
            request_id, "rejected", reviewer_id=reviewer_id, note=note
        )
        await self.bot.audit.log(
            "knowledge.reject",
            actor_id=reviewer_id,
            target=f"knowledge:{request_id}",
            detail=note,
        )
        return await self.bot.knowledge.get_by_id(request_id)
