# app/api/routes/pages.py
"""화면 라우트.

데이터는 브라우저가 API 로 다시 가져온다. 여기서는 첫 화면을 그리는 데 필요한
최소한의 문맥(현재 메뉴, 세션 상태, 기본값)만 넘긴다.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ... import __version__
from ...infrastructure.logging_setup import get_logger
from ..deps import AppContext, get_context
from ..security import SESSION_USER_KEY, verify_credentials

logger = get_logger(__name__)

router = APIRouter(tags=["pages"])

# 사이드바 활성 항목 식별자
PAGE_DASHBOARD = "dashboard"
PAGE_TARGETS = "targets"
PAGE_COMMENTS = "comments"
PAGE_JOBS = "jobs"
PAGE_SETTINGS = "settings"


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


async def _base_context(request: Request, context: AppContext, active_page: str) -> dict[str, Any]:
    """모든 화면이 공유하는 템플릿 문맥."""
    diagnosis = await context.session_manager.cached_diagnosis()
    settings = context.settings
    return {
        "active_page": active_page,
        "auth_state": diagnosis.state.value,
        "auth_message": diagnosis.message,
        "auth_can_delete": diagnosis.can_delete,
        "auth_enabled": settings.auth_enabled,
        "blog_url": settings.blog_url,
        "page_size": settings.page_size,
        "timezone": settings.timezone,
        "delete_dry_run_default": settings.delete_dry_run,
        "app_version": __version__,
    }


@router.get("/health", include_in_schema=False)
async def health(context: AppContext = Depends(get_context)) -> dict[str, Any]:
    """상태 확인 엔드포인트. 배포 환경의 헬스체크에 쓴다."""
    healthy = await context.database.healthcheck()
    return {"status": "ok" if healthy else "degraded", "version": __version__}


@router.get("/", response_class=Response)
async def dashboard_page(
    request: Request, context: AppContext = Depends(get_context)
) -> Response:
    return _templates(request).TemplateResponse(
        request=request,
        name="dashboard.html",
        context=await _base_context(request, context, PAGE_DASHBOARD),
    )


@router.get("/targets", response_class=Response)
async def targets_page(
    request: Request, context: AppContext = Depends(get_context)
) -> Response:
    return _templates(request).TemplateResponse(
        request=request,
        name="targets.html",
        context=await _base_context(request, context, PAGE_TARGETS),
    )


@router.get("/comments", response_class=Response)
async def comments_page(
    request: Request, context: AppContext = Depends(get_context)
) -> Response:
    return _templates(request).TemplateResponse(
        request=request,
        name="comments.html",
        context=await _base_context(request, context, PAGE_COMMENTS),
    )


@router.get("/jobs", response_class=Response)
async def jobs_page(request: Request, context: AppContext = Depends(get_context)) -> Response:
    return _templates(request).TemplateResponse(
        request=request,
        name="jobs.html",
        context=await _base_context(request, context, PAGE_JOBS),
    )


@router.get("/settings", response_class=Response)
async def settings_page(
    request: Request, context: AppContext = Depends(get_context)
) -> Response:
    return _templates(request).TemplateResponse(
        request=request,
        name="settings.html",
        context=await _base_context(request, context, PAGE_SETTINGS),
    )


@router.get("/login", response_class=Response)
async def login_page(request: Request, context: AppContext = Depends(get_context)) -> Response:
    """인증이 꺼져 있으면 로그인 화면이 필요 없으므로 대시보드로 보낸다."""
    if not context.settings.auth_enabled:
        return RedirectResponse(url="/", status_code=303)
    return _templates(request).TemplateResponse(
        request=request,
        name="login.html",
        context={"active_page": "", "error": None, "app_version": __version__},
    )


@router.post("/login", response_class=Response)
async def login_submit(
    request: Request,
    username: str = Form(default=""),
    password: str = Form(default=""),
    context: AppContext = Depends(get_context),
) -> Response:
    """단일 계정 로그인. 실패 사유는 구체적으로 알려주지 않는다."""
    if not context.settings.auth_enabled:
        return RedirectResponse(url="/", status_code=303)

    if verify_credentials(context.settings, username, password):
        request.session[SESSION_USER_KEY] = username or context.settings.auth_username
        return RedirectResponse(url="/", status_code=303)

    logger.warning("로그인 실패: 사용자 %s", username or "(빈 값)")
    return _templates(request).TemplateResponse(
        request=request,
        name="login.html",
        context={
            "active_page": "",
            "error": "아이디 또는 비밀번호가 올바르지 않습니다.",
            "app_version": __version__,
        },
        status_code=401,
    )


@router.post("/logout", response_class=Response)
async def logout(request: Request) -> Response:
    """세션을 비우고 로그인 화면으로 보낸다."""
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
