"""MindFinder 봇 진입점.

실행:
    python -m bot.main
"""

from __future__ import annotations

import logging

from bot.config import load_settings
from bot.core import MindFinderBot
from bot.logging_config import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    """설정을 로드하고 봇을 실행한다."""
    try:
        settings = load_settings()
    except RuntimeError as exc:
        # 설정 오류는 로깅 전이므로 콘솔에 직접 안내
        print(f"[설정 오류] {exc}")
        raise SystemExit(1) from exc

    settings.ensure_directories()
    setup_logging(settings.log_dir, settings.log_level)

    logger.info("MindFinder 봇을 시작합니다.")
    bot = MindFinderBot(settings)

    try:
        bot.run(settings.token, log_handler=None)
    except KeyboardInterrupt:
        logger.info("사용자 요청으로 종료합니다.")


if __name__ == "__main__":
    main()
