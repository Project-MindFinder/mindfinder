"""환경변수 및 봇 설정 로딩 모듈.

.env 파일(또는 실제 환경변수)에서 값을 읽어 하나의 Settings 객체로 관리한다.
초보자도 안전하게 쓸 수 있도록 다음 원칙을 따른다.
- 토큰 등 비밀값은 절대 코드에 하드코딩하지 않고 .env 에서만 읽는다.
- 선택 항목은 합리적인 기본값을 제공한다.
- 필수 항목이 비어 있으면 명확한 한국어 오류 메시지와 함께 즉시 실패한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 프로젝트 루트 (bot/config.py 기준 두 단계 위)
BASE_DIR = Path(__file__).resolve().parent.parent


def _get_int(name: str, default: int | None = None) -> int | None:
    """환경변수를 정수로 읽는다. 비어 있으면 기본값을 반환한다."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"환경변수 {name} 값이 정수가 아닙니다: {raw!r}"
        ) from exc


def _get_bool(name: str, default: bool) -> bool:
    """환경변수를 불리언으로 읽는다. 비어 있으면 기본값을 반환한다."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "y"}


def _get_float(name: str, default: float) -> float:
    """환경변수를 실수로 읽는다. 비어 있으면 기본값을 반환한다."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"환경변수 {name} 값이 실수가 아닙니다: {raw!r}"
        ) from exc


@dataclass(frozen=True)
class Settings:
    """봇 실행에 필요한 모든 설정값."""

    # --- 필수 ---
    token: str

    # --- 디스코드 ID (선택) ---
    guild_id: int | None = None
    admin_role_id: int | None = None
    verified_role_id: int | None = None
    verification_channel_id: int | None = None
    crisis_channel_id: int | None = None
    # 매칭 채널을 생성할 카테고리 ID (선택)
    match_category_id: int | None = None
    # 상시 패널(버튼 안내판)을 게시할 채널 ID (선택, 비우면 자동 생성)
    panel_channel_id: int | None = None
    # 패널 채널이 없으면 봇이 자동으로 만들지 여부
    auto_create_panel_channel: bool = True

    # --- 접근 제어용 역할 ID (선택, 비우면 봇이 자동 생성) ---
    # 1단계(인증됨): 자기 신고 이력 등록자. 전체 커뮤니티 열람.
    verified_community_role_id: int | None = None
    # 2단계(서류 인증): 서류/진위확인 통과자. 병원·기간·매칭 채널 이용.
    doc_verified_role_id: int | None = None
    # 병원별 커뮤니티 채널/역할을 묶을 카테고리 ID (선택, 비우면 자동 생성)
    hospital_category_id: int | None = None
    # 용도별 채널/카테고리를 없으면 자동 생성할지 여부
    auto_create_channels: bool = True
    # 봇 시작 시 인증 관련 데이터를 전면 초기화할지 여부.
    # (테스트용. 운영에서는 반드시 false 로 두세요.)
    reset_verifications_on_start: bool = True
    # 병원별로 별도 채널(및 역할)을 만들지 여부
    per_hospital_channels: bool = True
    # 서류 인증이 없는 사용자도 매칭(동료 찾기)을 쓸 수 있게 할지 (기본 false)
    allow_unverified_matching: bool = False

    # --- 약관/개인정보 동의 (선택) ---
    # 인증 시 필수 동의를 받는 문서의 버전. 버전을 올리면 다시 동의를 받는다.
    privacy_policy_version: str = "2026-10-06"
    terms_of_service_version: str = "2026-10-06"
    # 문서 공개 URL (선택). 비워두면 서버 공지/`/약관` 명령으로 안내한다.
    privacy_policy_url: str | None = None
    terms_of_service_url: str | None = None

    # --- 디스코드 특권 인텐트 (선택) ---
    # 개발자 포털에서 활성화하지 않았다면 false 로 두어야 봇이 로그인할 수 있다.
    # - members: 역할 부여/조회 (인증 배지)에 필요
    # - message_content: 위기 대응 키워드 감지에 필요
    enable_members_intent: bool = True
    enable_message_content_intent: bool = True

    # --- 서울대병원 문서 진위확인 API (선택) ---
    # 진위확인 자동 검증 기능 사용 여부
    snuh_truth_enabled: bool = True
    # 진위확인 사이트 기본 URL
    snuh_truth_base_url: str = "https://sdoc.snuh.org"
    # 진위확인 요청 타임아웃(초)
    snuh_truth_timeout: float = 20.0

    # --- 경로 ---
    database_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "mindfinder.db")
    log_dir: Path = field(default_factory=lambda: BASE_DIR / "logs")

    # --- 로깅 ---
    log_level: str = "INFO"

    def ensure_directories(self) -> None:
        """DB 및 로그 디렉터리가 없으면 생성한다."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)


def _resolve_path(raw: str, default: Path) -> Path:
    """경로 문자열을 절대경로로 변환한다. 비어 있으면 기본값을 쓴다."""
    if not raw.strip():
        return default
    path = Path(raw.strip())
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def load_settings(env_file: str | os.PathLike[str] | None = None) -> Settings:
    """환경변수를 읽어 Settings 객체를 생성한다.

    Args:
        env_file: 사용할 .env 파일 경로. None 이면 프로젝트 루트의 .env 를 사용.

    Raises:
        RuntimeError: 필수 환경변수(DISCORD_TOKEN)가 비어 있을 때.
    """
    # override=False: 이미 설정된 실제 환경변수를 .env 가 덮어쓰지 않도록 한다.
    # (Docker 등에서 환경변수로 주입하는 경우를 위함)
    load_dotenv(dotenv_path=env_file or (BASE_DIR / ".env"), override=False)

    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token or token == "여기에_봇_토큰을_입력하세요":
        raise RuntimeError(
            "DISCORD_TOKEN 이 설정되지 않았습니다. "
            "프로젝트 루트에 .env 파일을 만들고 "
            "DISCORD_TOKEN=봇토큰 형식으로 값을 채워주세요. "
            "(.env.example 파일을 복사해서 시작하면 됩니다.)"
        )

    return Settings(
        token=token,
        guild_id=_get_int("GUILD_ID"),
        admin_role_id=_get_int("ADMIN_ROLE_ID"),
        verified_role_id=_get_int("VERIFIED_ROLE_ID"),
        verification_channel_id=_get_int("VERIFICATION_CHANNEL_ID"),
        crisis_channel_id=_get_int("CRISIS_CHANNEL_ID"),
        match_category_id=_get_int("MATCH_CATEGORY_ID"),
        panel_channel_id=_get_int("PANEL_CHANNEL_ID"),
        auto_create_panel_channel=_get_bool("AUTO_CREATE_PANEL_CHANNEL", True),
        verified_community_role_id=_get_int("VERIFIED_COMMUNITY_ROLE_ID"),
        doc_verified_role_id=_get_int("DOC_VERIFIED_ROLE_ID"),
        hospital_category_id=_get_int("HOSPITAL_CATEGORY_ID"),
        auto_create_channels=_get_bool("AUTO_CREATE_CHANNELS", True),
        reset_verifications_on_start=_get_bool(
            "RESET_VERIFICATIONS_ON_START", True
        ),
        per_hospital_channels=_get_bool("PER_HOSPITAL_CHANNELS", True),
        allow_unverified_matching=_get_bool("ALLOW_UNVERIFIED_MATCHING", False),
        privacy_policy_version=(
            os.getenv("PRIVACY_POLICY_VERSION", "").strip() or "2026-10-06"
        ),
        terms_of_service_version=(
            os.getenv("TERMS_OF_SERVICE_VERSION", "").strip() or "2026-10-06"
        ),
        privacy_policy_url=os.getenv("PRIVACY_POLICY_URL", "").strip() or None,
        terms_of_service_url=os.getenv("TERMS_OF_SERVICE_URL", "").strip() or None,
        enable_members_intent=_get_bool("ENABLE_MEMBERS_INTENT", True),
        enable_message_content_intent=_get_bool(
            "ENABLE_MESSAGE_CONTENT_INTENT", True
        ),
        snuh_truth_enabled=_get_bool("SNUH_TRUTH_ENABLED", True),
        snuh_truth_base_url=(
            os.getenv("SNUH_TRUTH_BASE_URL", "").strip()
            or "https://sdoc.snuh.org"
        ),
        snuh_truth_timeout=_get_float("SNUH_TRUTH_TIMEOUT", 20.0),
        database_path=_resolve_path(
            os.getenv("DATABASE_PATH", ""), BASE_DIR / "data" / "mindfinder.db"
        ),
        log_dir=_resolve_path(os.getenv("LOG_DIR", ""), BASE_DIR / "logs"),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
