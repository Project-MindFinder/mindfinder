"""로깅 설정 모듈.

콘솔과 파일(logs/mindfinder.log)에 동시에 로그를 남긴다.
로그 파일은 Docker 볼륨과 연동해 영속화할 수 있다.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    """루트 로거를 구성하고 로거를 반환한다.

    Args:
        log_dir: 로그 파일을 저장할 디렉터리.
        level: 로그 레벨 문자열(DEBUG, INFO, WARNING, ERROR).

    Returns:
        구성된 루트 로거.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # 중복 핸들러 방지 (재호출 대비)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    # 콘솔 핸들러
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    # 파일 핸들러 (5MB x 5개 로테이션)
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_dir / "mindfinder.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # discord.py 의 과도한 로그 억제
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)

    return root
