# app/api/security.py
"""선택적 로그인 보호.

기본값은 로컬 전용이라 인증이 꺼져 있다. ``APP_AUTH_ENABLED=true`` 로 켜면
세션 쿠키 기반 단일 계정 로그인이 활성화된다. 티스토리 세션 쿠키를 보관하는
서비스이므로 로컬 주소가 아닌 곳에 바인딩할 때는 설정 검증 단계에서 인증을 강제한다.
"""

from __future__ import annotations

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, RedirectResponse, Response

from ..config.settings import Settings
from ..infrastructure.security.crypto import constant_time_equals

# 세션에 저장하는 인증 표식 키
SESSION_USER_KEY = "authenticated_user"

# 로그인 없이 접근할 수 있는 경로
_PUBLIC_PATHS = frozenset({"/login", "/logout", "/health"})

# 정적 자원 접두사
_STATIC_PREFIX = "/static"


class AuthMiddleware(BaseHTTPMiddleware):
    """인증이 켜져 있을 때 로그인하지 않은 요청을 차단한다."""

    def __init__(self, app, settings: Settings) -> None:
        super().__init__(app)
        self._settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._settings.auth_enabled:
            return await call_next(request)

        path = request.url.path
        if path in _PUBLIC_PATHS or path.startswith(_STATIC_PREFIX):
            return await call_next(request)

        if request.session.get(SESSION_USER_KEY):
            return await call_next(request)

        if path.startswith("/api"):
            return JSONResponse(
                status_code=401,
                content={
                    "error": {
                        "type": "AuthenticationError",
                        "message": "로그인이 필요합니다.",
                    }
                },
            )
        return RedirectResponse(url="/login", status_code=303)


def verify_credentials(settings: Settings, username: str, password: str) -> bool:
    """설정된 계정 정보와 비교한다. 타이밍 공격을 피하려고 상수 시간 비교를 쓴다."""
    if not settings.auth_enabled:
        return True
    user_ok = constant_time_equals(username, settings.auth_username)
    password_ok = constant_time_equals(password, settings.auth_password)
    return user_ok and password_ok
