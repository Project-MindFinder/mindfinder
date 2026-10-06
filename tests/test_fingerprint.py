"""증빙 서류 지문(원본 미보관) 및 유사도 기반 중복 탐지 테스트."""

from __future__ import annotations

import pytest

from bot.db.database import Database
from bot.db.repositories import UserRepository, VerificationRepository
from bot.services.verification import VerificationService
from bot.utils import document_fingerprint as fp

# 개인정보(성명·주민번호·문서번호)가 섞인 서류 텍스트 예시
DOC_TEXT_A = """
입퇴원 사실확인서
환자 성명: 홍길동
주민등록번호: 900101-1234567
진료과: 정신건강의학과
입원기간: 2020.03.02 ~ 2020.06.15
문서확인번호: 123456789
발급번호: 987654
"""

# 같은 서류지만 성명/문서번호 등 개인정보만 다른 경우
DOC_TEXT_B = """
입퇴원 사실확인서
환자 성명: 김철수
주민등록번호: 880202-2345678
진료과: 정신건강의학과
입원기간: 2020.03.02 ~ 2020.06.15
문서확인번호: 555555555
발급번호: 111111
"""

# 전혀 다른 서류
DOC_TEXT_C = """
입퇴원 사실확인서
환자 성명: 이영희
진료과: 정형외과
입원기간: 2019.01.01 ~ 2019.02.01
문서확인번호: 222222222
"""


def test_normalize_strips_personal_info() -> None:
    normalized = fp.normalize_document_text(DOC_TEXT_A)
    # 주민등록번호/긴 숫자열은 제거된다.
    assert "900101" not in normalized
    assert "123456789" not in normalized
    # 진료과 등 구조적 텍스트는 남는다.
    assert "정신건강의학과" in normalized


def test_same_document_same_fingerprint() -> None:
    a = fp.fingerprint_text(DOC_TEXT_A)
    # 공백/개인정보만 다른 동일 서류 → 동일 지문
    a2 = fp.fingerprint_text(DOC_TEXT_A)
    assert a.fingerprint == a2.fingerprint


def test_different_personal_info_same_structure_is_not_duplicate() -> None:
    a = fp.fingerprint_text(DOC_TEXT_A)
    b = fp.fingerprint_text(DOC_TEXT_B)
    # 성명·문서번호만 다른 같은 서식 → 지문은 다르고, 임계값 아래라 중복이 아니다.
    # (같은 서식의 '서로 다른 서류'를 중복으로 오판하지 않도록 임계값을 높게 둔다.)
    assert a.fingerprint != b.fingerprint
    assert a.similarity(b) < fp.DEFAULT_SIMILARITY_THRESHOLD


def test_unrelated_documents_are_not_similar() -> None:
    a = fp.fingerprint_text(DOC_TEXT_A)
    c = fp.fingerprint_text(DOC_TEXT_C)
    assert a.similarity(c) < fp.DEFAULT_SIMILARITY_THRESHOLD


def test_encode_decode_roundtrip() -> None:
    f = fp.fingerprint_text(DOC_TEXT_A)
    encoded = fp.encode_shingles(f.shingles)
    decoded = fp.decode_shingles(encoded)
    assert decoded == f.shingles


def test_is_similar_only_on_identical_text() -> None:
    a = fp.fingerprint_text(DOC_TEXT_A)
    b = fp.fingerprint_text(DOC_TEXT_B)
    # 해시 완전 일치 → 동일 서류
    assert fp.is_similar(a, a.fingerprint, fp.encode_shingles(a.shingles))
    # 같은 서식·개인정보만 다른 서류는 중복으로 보지 않는다.
    assert not fp.is_similar(b, a.fingerprint, fp.encode_shingles(a.shingles))
    # 무관한 서류
    c = fp.fingerprint_text(DOC_TEXT_C)
    assert not fp.is_similar(c, a.fingerprint, fp.encode_shingles(a.shingles))


def test_empty_text_has_no_shingles() -> None:
    f = fp.fingerprint_text("")
    assert f.shingles == frozenset()
    assert f.similarity(fp.fingerprint_text(DOC_TEXT_A)) == 0.0


# --------------------------------------------------------------------------- #
# 리포지토리 연동 (원본 대신 지문만 저장)
# --------------------------------------------------------------------------- #
@pytest.fixture
async def repo(tmp_path):
    database = Database(tmp_path / "fp.db")
    await database.connect()
    try:
        # verification_requests.user_id 는 users 테이블을 참조하므로 먼저 생성한다.
        await UserRepository(database).ensure_user(1)
        yield VerificationRepository(database)
    finally:
        await database.dispose()


async def test_store_and_list_fingerprints(repo: VerificationRepository) -> None:
    request_id = await repo.create(user_id=1, admission_id=None)
    f = fp.fingerprint_text(DOC_TEXT_A)
    await repo.set_fingerprint(
        request_id, f.fingerprint, fp.encode_shingles(f.shingles)
    )
    stored = await repo.list_fingerprints()
    assert len(stored) == 1
    assert stored[0][0] == f.fingerprint
    assert fp.decode_shingles(stored[0][1]) == f.shingles


async def test_list_fingerprints_skips_empty(repo: VerificationRepository) -> None:
    await repo.create(user_id=1, admission_id=None)
    assert await repo.list_fingerprints() == []


class _FakeBot:
    """check_duplicate_fingerprint 에 필요한 최소 봇 대역."""

    def __init__(self, verifications: VerificationRepository) -> None:
        self.verifications = verifications


async def test_service_detects_duplicate_submission(
    repo: VerificationRepository,
) -> None:
    service = VerificationService(_FakeBot(repo))  # type: ignore[arg-type]
    request_id = await repo.create(user_id=1, admission_id=None)
    f = fp.fingerprint_text(DOC_TEXT_A)
    await repo.set_fingerprint(
        request_id, f.fingerprint, fp.encode_shingles(f.shingles)
    )

    # 동일 서류 재제출 → 중복 탐지
    assert await service.check_duplicate_fingerprint(DOC_TEXT_A) is not None
    # 개인정보만 다른 같은 서식(다른 서류) → 중복 아님
    assert await service.check_duplicate_fingerprint(DOC_TEXT_B) is None
    # 무관한 서류 → 중복 아님
    assert await service.check_duplicate_fingerprint(DOC_TEXT_C) is None
