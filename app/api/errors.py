# app/api/errors.py
"""예외를 HTTP 응답으로 번역한다.

라우트에서 try/except 를 반복하지 않도록 도메인 예외 계층을 한 곳에서 처리한다.
API 요청은 JSON 오류를, 화면 요청은 오류 페이지를 돌려준다.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..domain.errors import AppError
from ..infrastructure.logging_setup import get_logger

logger = get_logger(__name__)

# 이 접두사로 시작하는 요청은 JSON 오류를 받는다.
_API_PREFIX = "/api"


def _wants_json(request: Request) -> bool:
    """JSON 응답을 기대하는 요청인지 판단한다."""
    if request.url.path.startswith(_API_PREFIX):
        return True
    accept = request.headers.get("accept", "")
    return "application/json" in accept and "text/html" not in accept


def _json_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"type": error_type, "message": message}},
    )


def register_exception_handlers(app: FastAPI, templates: Jinja2Templates) -> None:
    """애플리케이션에 예외 처리기를 등록한다."""

    def _html_error(request: Request, status_code: int, message: str) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="error.html",
            context={
                "status_code": status_code,
                "message": message,
                "active_page": "",
            },
            status_code=status_code,
        )

    @app.exception_handler(AppError)
    async def handle_domain_error(request: Request, exc: AppError) -> Response:
        """업무 규칙 위반이나 외부 연동 실패."""
        if exc.status_code >= 500:
            logger.error("도메인 오류 %s: %s", type(exc).__name__, exc.message)
        else:
            logger.info("요청 거부 %s: %s", type(exc).__name__, exc.message)
        if _wants_json(request):
            return _json_error(exc.status_code, type(exc).__name__, exc.message)
        return _html_error(request, exc.status_code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        """요청 본문이나 질의 문자열의 형식 오류."""
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', '')}"
            for error in exc.errors()
        )
        message = f"요청 형식이 올바르지 않습니다. {detail}"
        if _wants_json(request):
            return _json_error(422, "RequestValidationError", message)
        return _html_error(request, 422, message)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> Response:
        """404 등 프레임워크가 만든 오류."""
        message = str(exc.detail) if exc.detail else "요청을 처리할 수 없습니다."
        if _wants_json(request):
            return _json_error(exc.status_code, "HTTPException", message)
        return _html_error(request, exc.status_code, message)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> Response:
        """예상하지 못한 오류. 내부 정보를 사용자에게 그대로 노출하지 않는다."""
        logger.exception("처리되지 않은 오류: %s %s", request.method, request.url.path)
        message = "서버 내부 오류가 발생했습니다. 로그를 확인하세요."
        if _wants_json(request):
            return _json_error(500, "InternalServerError", message)
        return _html_error(request, 500, message)
