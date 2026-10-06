"""서울대병원 진위확인 클라이언트 테스트 (네트워크 mock)."""

from __future__ import annotations

import pytest

from bot.utils import snuh_truth
from bot.utils.snuh_truth import (
    SnuhTruthClient,
    SnuhTruthError,
    TruthStatus,
    _classify,
)

FORM_HTML = """
<html><body>
<form id="truthVo" action="/truth/online.do" method="post">
  <select name="searchCertiCd"><option value="O3"></option></select>
  <input name="searchIssueNo" value="" />
  <div><input type="hidden" name="_csrf" value="test-token-123" /></div>
</form>
</body></html>
"""


def _result_html(message: str) -> str:
    return (
        "<html><head><script type=\"text/javascript\">"
        f"alert('{message}');"
        "location.replace('/truth/online.do');"
        "</script></head><body></body></html>"
    )


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class FakeSession:
    """curl_cffi.requests.Session 대역."""

    last_post_data: dict | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[tuple[str, str]] = []

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append(("GET", url))
        if url.endswith("/truth/online.do"):
            return FakeResponse(FORM_HTML)
        if url.endswith("/truth/checkResult.do"):
            return FakeResponse(self._result)
        return FakeResponse("<html></html>")

    def post(self, url: str, data=None, **kwargs) -> FakeResponse:
        self.calls.append(("POST", url))
        FakeSession.last_post_data = data
        return FakeResponse("", status_code=302)

    _result: str = _result_html("정상적으로 인터넷으로 발급된 문서 입니다.")


def _install_session(monkeypatch, result_html: str) -> None:
    class _Session(FakeSession):
        _result = result_html

    monkeypatch.setattr(
        "curl_cffi.requests.Session", _Session, raising=False
    )


def test_classify_valid() -> None:
    assert _classify("정상적으로 인터넷으로 발급된 문서 입니다.") is TruthStatus.VALID


def test_classify_invalid() -> None:
    assert _classify("인터넷으로 발급된 문서가 아닙니다.") is TruthStatus.INVALID


def test_classify_unknown() -> None:
    assert _classify("알 수 없는 메시지") is TruthStatus.UNKNOWN


async def test_verify_valid_document(monkeypatch) -> None:
    _install_session(monkeypatch, _result_html("정상적으로 인터넷으로 발급된 문서 입니다."))
    client = SnuhTruthClient()
    result = await client.verify("H26O31004506124556")

    assert result.status is TruthStatus.VALID
    assert result.is_valid is True
    assert result.document_no == "H26O31004506124556"
    # CSRF 토큰과 문서확인번호가 올바르게 전송되었는지 확인
    assert FakeSession.last_post_data is not None
    assert FakeSession.last_post_data["searchIssueNo"] == "H26O31004506124556"
    assert FakeSession.last_post_data["_csrf"] == "test-token-123"
    assert FakeSession.last_post_data["searchCertiCd"] == "O3"


async def test_verify_forged_document(monkeypatch) -> None:
    _install_session(monkeypatch, _result_html("인터넷으로 발급된 문서가 아닙니다."))
    client = SnuhTruthClient()
    result = await client.verify("1132147")

    assert result.status is TruthStatus.INVALID
    assert result.is_valid is False


async def test_verify_empty_document_no() -> None:
    client = SnuhTruthClient()
    with pytest.raises(SnuhTruthError):
        await client.verify("   ")


async def test_verify_network_error(monkeypatch) -> None:
    class BoomSession(FakeSession):
        def get(self, url: str, **kwargs):
            raise OSError("network down")

    monkeypatch.setattr("curl_cffi.requests.Session", BoomSession, raising=False)
    client = SnuhTruthClient()
    with pytest.raises(SnuhTruthError):
        await client.verify("H26O31004506124556")
