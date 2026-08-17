# app/__main__.py
"""진입점.

``python -m app`` 로 실행한다. `.env` 가 없으면 예시 파일에서 만들고
비밀키를 채운 뒤 서버를 띄운다. 사용자가 준비할 것은 파이썬과 의존성 설치뿐이다.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from typing import Optional

import uvicorn

from . import __version__
from .config.settings import ENV_FILE, ensure_env_file, get_settings, reset_settings_cache
from .domain.errors import ConfigurationError

# 서버가 뜰 시간을 준 뒤 브라우저를 여는 지연(초)
_BROWSER_DELAY = 1.5


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """명령행 인자를 해석한다. 지정하지 않으면 `.env` 값을 그대로 쓴다."""
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="티스토리 댓글 정리 서비스를 실행한다.",
    )
    parser.add_argument("--host", default=None, help="바인딩 주소 (기본값은 .env 의 APP_HOST)")
    parser.add_argument("--port", type=int, default=None, help="포트 (기본값은 .env 의 APP_PORT)")
    parser.add_argument(
        "--no-browser", action="store_true", help="기동 후 브라우저를 열지 않는다"
    )
    parser.add_argument(
        "--reload", action="store_true", help="코드 변경 시 자동 재시작 (개발용)"
    )
    parser.add_argument("--version", action="version", version=f"tistory-comment-spam-cleaner {__version__}")
    return parser.parse_args(argv)


def _open_browser_later(url: str) -> None:
    """서버가 준비될 즈음 기본 브라우저를 연다. 실패해도 무시한다."""

    def _open() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 브라우저가 없는 환경도 정상 동작해야 한다.
            pass

    timer = threading.Timer(_BROWSER_DELAY, _open)
    timer.daemon = True
    timer.start()


def main(argv: Optional[list[str]] = None) -> int:
    """서버를 기동한다. 종료 코드를 반환한다."""
    args = _parse_args(argv)

    ensure_env_file()
    reset_settings_cache()
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - 설정 오류는 사용자에게 그대로 알려준다.
        print(f"설정을 읽지 못했습니다: {exc}", file=sys.stderr)
        print(f"확인할 파일: {ENV_FILE}", file=sys.stderr)
        return 1

    if settings.blog_url.rstrip("/").endswith("example.tistory.com"):
        print(
            "APP_BLOG_URL 이 예시 값 그대로입니다. "
            f"{ENV_FILE} 를 열어 본인 블로그 주소로 바꾼 뒤 다시 실행하세요.",
            file=sys.stderr,
        )
        return 1

    host = args.host or settings.host
    port = args.port or settings.port
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{display_host}:{port}"

    print(f"티스토리 댓글 정리 {__version__}")
    print(f"  대상 블로그: {settings.blog_url}")
    print(f"  주소: {url}")
    print(f"  설정 파일: {ENV_FILE}")
    print("  종료하려면 Ctrl+C 를 누르세요.")

    if settings.open_browser and not args.no_browser and not args.reload:
        _open_browser_later(url)

    try:
        uvicorn.run(
            "app.server:create_app",
            factory=True,
            host=host,
            port=port,
            reload=args.reload,
            log_config=None,
            access_log=False,
        )
    except ConfigurationError as exc:
        print(f"기동 실패: {exc.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover - 사용자 종료
        print("\n종료합니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
