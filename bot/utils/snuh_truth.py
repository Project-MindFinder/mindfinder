"""서울대학교병원 문서 진위확인 클라이언트.

서울대학교병원 의무기록 온라인 발급 사이트(sdoc.snuh.org)의
'진위확인' 기능을 이용해, 발급된 문서(입퇴원 사실확인서 등)가
인터넷으로 정상 발급된 문서인지(=위조가 아닌지) 검증한다.

동작 흐름:
    1) GET /certi/form.do?certiCd=O3   → 세션 쿠키(dpv*) 확보
    2) GET /truth/online.do            → CSRF 토큰(_csrf) 추출
    3) POST /truth/online.do           → 문서확인번호(searchIssueNo) 제출
    4) GET /truth/checkResult.do       → 결과 alert 메시지 파싱

중요: sdoc.snuh.org 는 WAF 가 Python HTTP 클라이언트(aiohttp/httpx)의
TLS 지문을 차단(HTTP 403)한다. 따라서 실제 브라우저 TLS 지문을 흉내내는
curl_cffi 를 사용한다. curl_cffi 는 동기 라이브러리이므로 async 코드에서는
asyncio.to_thread 로 감싸 호출한다.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://sdoc.snuh.org"
# 증명서 종류 코드: O3 = 입퇴원 사실 확인서
CERTI_CODE_ADMISSION = "O3"

_CSRF_RE = re.compile(r'name="_csrf"\s+value="([^"]+)"')
_TRUTH_FORM_RE = re.compile(r'<form id="truthVo".*?</form>', re.S)
_ALERT_RE = re.compile(r"alert\(\s*'([^']*)'\s*\)")


class TruthStatus(str, Enum):
    """진위확인 결과 상태."""

    VALID = "valid"       # 정상 발급 문서 (진본)
    INVALID = "invalid"   # 인터넷 발급 문서가 아님 (위조 의심)
    UNKNOWN = "unknown"   # 응답을 해석하지 못함


@dataclass
class TruthResult:
    """진위확인 결과."""

    status: TruthStatus
    message: str
    document_no: str
    certificate_code: str = CERTI_CODE_ADMISSION

    @property
    def is_valid(self) -> bool:
        return self.status is TruthStatus.VALID


class SnuhTruthError(RuntimeError):
    """진위확인 중 통신/설정 오류."""


def _classify(message: str) -> TruthStatus:
    """결과 alert 메시지를 상태로 분류한다."""
    text = message.replace(" ", "")
    if "정상" in text and "발급" in text:
        return TruthStatus.VALID
    if "아닙니다" in text or "아니" in text or "위조" in text or "조회" in text:
        return TruthStatus.INVALID
    return TruthStatus.UNKNOWN


class SnuhTruthClient:
    """서울대학교병원 진위확인 클라이언트.

    Args:
        base_url: 사이트 기본 URL.
        timeout: 요청 타임아웃(초).
        impersonate: curl_cffi 가 흉내낼 브라우저 (TLS 지문).
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 20.0,
        impersonate: str = "firefox",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.impersonate = impersonate

    # ---------- 내부 (동기) ----------

    def _verify_sync(
        self, document_no: str, certificate_code: str = CERTI_CODE_ADMISSION
    ) -> TruthResult:
        try:
            from curl_cffi import requests
        except ImportError as exc:  # pragma: no cover - 의존성 미설치 시
            raise SnuhTruthError(
                "curl_cffi 가 설치되어 있지 않습니다. requirements 를 설치해주세요."
            ) from exc

        document_no = document_no.strip()
        if not document_no:
            raise SnuhTruthError("문서확인번호가 비어 있습니다.")

        form_url = f"{self.base_url}/truth/online.do"
        session = requests.Session(impersonate=self.impersonate)

        try:
            # 1) 세션 쿠키 확보 (실패해도 진행)
            try:
                session.get(
                    f"{self.base_url}/certi/form.do?certiCd={certificate_code}",
                    timeout=self.timeout,
                )
            except Exception:  # noqa: BLE001 - 쿠키 확보 실패는 치명적이지 않음
                logger.debug("certi/form.do 요청 실패 (무시하고 진행)")

            # 2) CSRF 토큰 추출
            page = session.get(form_url, timeout=self.timeout).text
            form_match = _TRUTH_FORM_RE.search(page)
            form_block = form_match.group(0) if form_match else page
            csrf_match = _CSRF_RE.search(form_block)
            token = csrf_match.group(1) if csrf_match else ""

            # 3) 문서확인번호 제출
            session.post(
                form_url,
                data={
                    "searchCertiCd": certificate_code,
                    "searchIssueNo": document_no,
                    "_csrf": token,
                },
                headers={"Referer": form_url, "Origin": self.base_url},
                timeout=self.timeout,
                allow_redirects=False,
            )

            # 4) 결과 조회
            result_page = session.get(
                f"{self.base_url}/truth/checkResult.do", timeout=self.timeout
            ).text
        except SnuhTruthError:
            raise
        except Exception as exc:  # noqa: BLE001 - 네트워크/파싱 오류 포괄
            raise SnuhTruthError(f"서울대병원 진위확인 요청 실패: {exc}") from exc

        alert_match = _ALERT_RE.search(result_page)
        message = alert_match.group(1).strip() if alert_match else ""
        if not message:
            logger.warning("진위확인 응답에서 메시지를 찾지 못했습니다.")
            return TruthResult(
                status=TruthStatus.UNKNOWN,
                message="응답을 해석할 수 없습니다.",
                document_no=document_no,
                certificate_code=certificate_code,
            )

        return TruthResult(
            status=_classify(message),
            message=message,
            document_no=document_no,
            certificate_code=certificate_code,
        )

    # ---------- 공개 (비동기) ----------

    async def verify(
        self, document_no: str, certificate_code: str = CERTI_CODE_ADMISSION
    ) -> TruthResult:
        """문서확인번호로 진위를 확인한다 (동기 로직을 스레드에서 실행)."""
        return await asyncio.to_thread(
            self._verify_sync, document_no, certificate_code
        )


async def verify_document(
    document_no: str,
    certificate_code: str = CERTI_CODE_ADMISSION,
    base_url: str = DEFAULT_BASE_URL,
) -> TruthResult:
    """편의 함수: 문서확인번호를 서울대병원 진위확인 API 로 검증한다."""
    client = SnuhTruthClient(base_url=base_url)
    return await client.verify(document_no, certificate_code)
