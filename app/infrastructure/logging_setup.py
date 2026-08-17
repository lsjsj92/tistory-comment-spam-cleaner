# app/infrastructure/logging_setup.py
"""한국 시간 기준 로깅 설정.

표준 :mod:`logging` 의 기본 포매터는 서버 로컬 시간을 쓰기 때문에 UTC 환경의
컨테이너에서 돌리면 로그 시각이 실제 운영 시각과 어긋난다. 여기서는 설정된
시간대(기본 Asia/Seoul)로 타임스탬프를 강제한다.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Optional

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S %Z"

# 로그가 과도하게 시끄러워지는 서드파티 로거
_NOISY_LOGGERS = ("httpx", "httpcore", "aiosqlite", "multipart", "watchfiles")


class TimezoneFormatter(logging.Formatter):
    """레코드 타임스탬프를 지정한 시간대로 변환하는 포매터."""

    def __init__(self, fmt: str, datefmt: str, tz: ZoneInfo) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        self._tz = tz

    def formatTime(self, record: logging.LogRecord, datefmt: Optional[str] = None) -> str:  # noqa: N802
        # record.created 는 epoch 초다. UTC 로 해석한 뒤 대상 시간대로 변환한다.
        moment = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone(self._tz)
        return moment.strftime(datefmt or self.datefmt or DATE_FORMAT)


def _build_console_handler(formatter: logging.Formatter) -> logging.Handler:
    """표준 출력 핸들러. Windows 콘솔에서도 한글이 깨지지 않게 인코딩을 맞춘다."""
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):  # pragma: no branch - 파이썬 3.7+ 표준
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 리다이렉트된 스트림
            pass
    handler = logging.StreamHandler(stream)
    handler.setFormatter(formatter)
    return handler


def _build_file_handler(
    log_dir: Path, retention_days: int, formatter: logging.Formatter
) -> logging.Handler:
    """일 단위로 회전하는 파일 핸들러."""
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_dir / "app.log",
        when="midnight",
        backupCount=retention_days,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(formatter)
    return handler


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    retention_days: int = 14,
    tz_name: str = "Asia/Seoul",
) -> None:
    """루트 로거를 구성한다. 중복 호출해도 핸들러가 쌓이지 않는다.

    Args:
        level: 루트 로거 레벨 문자열.
        log_dir: 로그 파일을 남길 디렉터리. None 이면 콘솔에만 출력한다.
        retention_days: 회전된 로그 파일 보관 일수.
        tz_name: 타임스탬프에 사용할 IANA 시간대 이름.
    """
    tz = ZoneInfo(tz_name)
    formatter = TimezoneFormatter(LOG_FORMAT, DATE_FORMAT, tz)

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.addHandler(_build_console_handler(formatter))
    if log_dir is not None:
        root.addHandler(_build_file_handler(log_dir, retention_days, formatter))

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # uvicorn 은 자체 핸들러를 붙이므로 루트로 전파만 시키고 중복 출력을 막는다.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    """모듈용 로거를 반환한다."""
    return logging.getLogger(name)
