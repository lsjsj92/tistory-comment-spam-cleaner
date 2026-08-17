# app/api/routes/settings.py
"""설정, 세션 쿠키, 규칙 편집 라우트.

쿠키 값은 어떤 응답에도 포함하지 않는다. 이름과 진단 결과만 돌려준다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ... import __version__
from ...config.rules import load_rules, read_rules_yaml, save_rules_yaml
from ...config.settings import ENV_FILE
from ...domain.enums import CommentStatus
from ...domain.errors import AuthenticationError, ValidationError
from ...infrastructure.db.repositories import AuditRepository, CommentRepository
from ..deps import AppContext, get_context
from ..schemas import (
    AuthOut,
    CookieIn,
    OkResponse,
    RulesIn,
    RulesOut,
    RulesSavedOut,
    SettingsOut,
    TestDeleteIn,
    TestDeleteOut,
    auth_to_out,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# 시험 삭제에 쓰는 최소 동시성. 한 건만 보내므로 1이면 충분하다.
_TEST_CONCURRENCY = 1


@router.get("", response_model=SettingsOut)
async def get_settings_view(context: AppContext = Depends(get_context)) -> SettingsOut:
    """현재 적용 중인 설정과 세션 상태를 돌려준다."""
    settings = context.settings
    diagnosis = await context.session_manager.cached_diagnosis()
    return SettingsOut(
        blog_url=settings.blog_url,
        auth=auth_to_out(diagnosis, tz_name=settings.timezone),
        runtime={
            "collect_concurrency": settings.collect_concurrency,
            "collect_rps": settings.collect_rps,
            "delete_concurrency": settings.delete_concurrency,
            "delete_rps": settings.delete_rps,
            "delete_dry_run": settings.delete_dry_run,
            "circuit_breaker_threshold": settings.circuit_breaker_threshold,
            "backup_before_delete": settings.backup_before_delete,
            "page_size": settings.page_size,
            "timezone": settings.timezone,
            "monitor_enabled": settings.monitor_enabled,
            "monitor_interval_minutes": settings.monitor_interval_minutes,
            "http_timeout": settings.http_timeout,
            "http_max_retries": settings.http_max_retries,
        },
        paths={
            "env_file": str(ENV_FILE),
            "rules_file": str(settings.rules_file),
            "targets_file": str(settings.targets_file),
            "backup_dir": str(settings.backup_path),
            "database": str(settings.database_file or settings.resolved_database_url),
            "log_dir": str(settings.log_path),
        },
        version=__version__,
    )


@router.post("/cookies", response_model=AuthOut)
async def register_cookies(
    payload: CookieIn, context: AppContext = Depends(get_context)
) -> AuthOut:
    """붙여넣은 문자열에서 쿠키를 추출해 저장하고 즉시 진단한다."""
    diagnosis = await context.session_manager.save_cookies(payload.raw)
    return auth_to_out(diagnosis, tz_name=context.settings.timezone)


@router.post("/cookies/browser", response_model=AuthOut)
async def import_browser_cookies(context: AppContext = Depends(get_context)) -> AuthOut:
    """설치된 브라우저에서 쿠키를 직접 읽어온다."""
    diagnosis = await context.session_manager.save_cookies_from_browser()
    return auth_to_out(diagnosis, tz_name=context.settings.timezone)


@router.delete("/cookies", response_model=OkResponse)
async def clear_cookies(context: AppContext = Depends(get_context)) -> OkResponse:
    """저장된 쿠키를 지운다."""
    await context.session_manager.clear_cookies()
    return OkResponse()


@router.post("/diagnose", response_model=AuthOut)
async def diagnose(context: AppContext = Depends(get_context)) -> AuthOut:
    """세션이 아직 유효한지 다시 확인한다."""
    diagnosis = await context.session_manager.diagnose()
    return auth_to_out(diagnosis, tz_name=context.settings.timezone)


@router.post("/test-delete", response_model=TestDeleteOut)
async def test_delete(
    payload: TestDeleteIn, context: AppContext = Depends(get_context)
) -> TestDeleteOut:
    """댓글 1건만 실제로 삭제해 본다.

    대량 실행 전에 응답 계약과 권한을 확인하기 위한 절차다. 드라이런이 아니라
    진짜로 지우므로, 일괄 삭제와 똑같은 보호 장치를 여기에도 적용한다. 번호를
    한 글자 잘못 입력해 운영자 댓글이 사라지는 사고를 막기 위함이다.
    """
    settings = context.settings
    diagnosis = await context.session_manager.cached_diagnosis()
    if not diagnosis.can_delete:
        raise AuthenticationError(
            "소유자 세션이 확인되지 않았습니다. 쿠키를 등록하고 진단을 먼저 통과하세요."
        )

    async with context.database.session() as session:
        rows = await CommentRepository(session).get_many([payload.comment_id])
    target = rows[0] if rows else None

    if target is not None:
        if target.whitelisted or target.is_admin:
            raise ValidationError(
                f"댓글 {payload.comment_id} 은(는) 보호 대상입니다. "
                f"작성자 '{target.nickname or '이름 없음'}'. 다른 번호를 확인하세요."
            )
        if target.status == CommentStatus.DELETED.value:
            raise ValidationError(f"댓글 {payload.comment_id} 은(는) 이미 삭제되었습니다.")

    client = await context.session_manager.build_client(
        rps=settings.delete_rps, concurrency=_TEST_CONCURRENCY
    )
    async with client:
        outcome = await client.delete_comment(payload.comment_id, dry_run=False)

    # 시험 삭제도 되돌릴 수 없는 조작이다. 상태와 감사 기록을 남겨야 이후 일괄
    # 삭제가 같은 댓글에 다시 요청을 보내지 않는다.
    async with context.database.session() as session:
        if target is not None:
            await CommentRepository(session).mark_status(
                [payload.comment_id],
                CommentStatus.DELETED if outcome.success else CommentStatus.FAILED,
                error=None if outcome.success else outcome.message[:500],
            )
        await AuditRepository(session).log(
            "test_delete",
            target=f"comment:{payload.comment_id}",
            detail={
                "success": outcome.success,
                "http_status": outcome.http_status,
                "message": outcome.message,
                "known_comment": target is not None,
            },
        )

    return TestDeleteOut(
        comment_id=outcome.comment_id,
        success=outcome.success,
        http_status=outcome.http_status,
        message=outcome.message or ("삭제에 성공했습니다." if outcome.success else ""),
    )


@router.get("/rules", response_model=RulesOut)
async def get_rules(context: AppContext = Depends(get_context)) -> RulesOut:
    """규칙 파일 원문을 돌려준다."""
    return RulesOut(yaml=read_rules_yaml(context.settings.rules_file))


@router.put("/rules", response_model=RulesSavedOut)
async def put_rules(
    payload: RulesIn, context: AppContext = Depends(get_context)
) -> RulesSavedOut:
    """규칙 파일을 검증한 뒤 저장한다. 검증에 실패하면 기존 파일은 그대로 둔다."""
    config = save_rules_yaml(context.settings.rules_file, payload.yaml)
    return RulesSavedOut(rule_count=len(config.rules))


@router.post("/rules/reload", response_model=RulesSavedOut)
async def reload_rules(context: AppContext = Depends(get_context)) -> RulesSavedOut:
    """디스크의 규칙 파일을 다시 읽어 유효한지 확인한다."""
    config = load_rules(context.settings.rules_file)
    return RulesSavedOut(rule_count=len(config.rules))
