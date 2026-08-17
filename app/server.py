# app/server.py
"""FastAPI 애플리케이션 팩토리.

기동 순서와 종료 순서를 한 곳에서 통제한다. 특히 종료 시 진행 중인 작업을
일시정지로 표시해야 다음 기동에서 이어서 실행할 수 있다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import __version__
from .api.deps import AppContext
from .api.errors import register_exception_handlers
from .api.routes import backups, comments, jobs, pages, settings as settings_routes, stats
from .api.routes.targets import load_targets_from_file, router as targets_router
from .api.security import AuthMiddleware
from .config.settings import Settings, get_settings
from .infrastructure.logging_setup import get_logger, setup_logging

logger = get_logger(__name__)

# 웹 자원 위치
WEB_DIR = Path(__file__).resolve().parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """애플리케이션 인스턴스를 만든다.

    Args:
        settings: 주입할 설정. 생략하면 `.env` 에서 읽는다. 테스트에서 유용하다.
    """
    resolved = settings or get_settings()
    resolved.ensure_directories()

    setup_logging(
        level=resolved.log_level,
        log_dir=resolved.log_path,
        retention_days=resolved.log_retention_days,
        tz_name=resolved.timezone,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        """기동과 종료 시 자원을 준비하고 정리한다."""
        context: AppContext = application.state.context
        await context.database.create_all()
        await context.job_manager.recover_on_startup()

        added = await load_targets_from_file(context)
        if added:
            logger.info("설정 파일에서 게시글 %d건을 새로 등록했습니다.", added)

        await context.monitor.start()
        logger.info(
            "서비스를 시작했습니다. 버전 %s, 대상 블로그 %s", __version__, resolved.blog_url
        )
        try:
            yield
        finally:
            await context.monitor.stop()
            await context.job_manager.shutdown()
            await context.database.dispose()
            logger.info("서비스를 종료했습니다.")

    application = FastAPI(
        title="티스토리 댓글 정리",
        description="티스토리 블로그의 댓글을 수집, 분류, 백업, 삭제한다.",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    application.state.context = AppContext.create(resolved)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    application.state.templates = templates

    # 세션 미들웨어는 인증 미들웨어보다 먼저 등록해야 request.session 을 쓸 수 있다.
    application.add_middleware(
        SessionMiddleware,
        secret_key=resolved.secret_key,
        max_age=resolved.session_max_age,
        same_site="lax",
        https_only=False,
    )
    application.add_middleware(AuthMiddleware, settings=resolved)

    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    application.include_router(pages.router)
    application.include_router(stats.router)
    application.include_router(targets_router)
    application.include_router(comments.router)
    application.include_router(jobs.router)
    application.include_router(settings_routes.router)
    application.include_router(backups.router)

    register_exception_handlers(application, templates)
    return application
