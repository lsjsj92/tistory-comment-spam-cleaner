# app/api/routes/targets.py
"""수집 대상 게시글 라우트."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from ...config.targets import load_targets, save_targets
from ...domain.errors import NotFoundError
from ...domain.models import TargetSpec
from ...infrastructure.db.repositories import TargetRepository
from ...infrastructure.logging_setup import get_logger
from ...infrastructure.timeutils import parse_user_datetime
from ...services.discovery import DiscoveryService
from ..deps import AppContext, get_context
from ..schemas import (
    CollectIn,
    JobCreatedOut,
    OkResponse,
    TargetCreateIn,
    TargetListOut,
    TargetPatchIn,
    target_to_out,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/targets", tags=["targets"])


@router.get("", response_model=TargetListOut)
async def list_targets(context: AppContext = Depends(get_context)) -> TargetListOut:
    """등록된 게시글 목록."""
    async with context.database.session() as session:
        rows = await TargetRepository(session).list_all()
    items = [target_to_out(row, tz_name=context.settings.timezone) for row in rows]
    return TargetListOut(items=items, total=len(items))


@router.post("", response_model=dict)
async def add_target(
    payload: TargetCreateIn, context: AppContext = Depends(get_context)
) -> dict:
    """URL 또는 글번호로 게시글을 등록한다. 제목은 가능한 범위에서 가져온다."""
    settings = context.settings
    client = await context.session_manager.build_client(
        rps=settings.collect_rps, concurrency=1, with_cookies=False
    )
    async with client:
        # resolve_entry 가 제목까지 채워서 돌려주므로 별도 조회가 필요 없다.
        spec = await DiscoveryService(
            client=client, blog_url=settings.blog_url
        ).resolve_entry(payload.url_or_id)

    async with context.database.session() as session:
        row = await TargetRepository(session).upsert(spec, source="manual")
        item = target_to_out(row, tz_name=settings.timezone)

    await _sync_targets_file(context)
    return {"item": item.model_dump()}


@router.patch("/{entry_id}", response_model=dict)
async def patch_target(
    entry_id: int, payload: TargetPatchIn, context: AppContext = Depends(get_context)
) -> dict:
    """게시글의 수집 활성 여부를 바꾼다."""
    async with context.database.session() as session:
        repo = TargetRepository(session)
        if not await repo.set_enabled(entry_id, payload.enabled):
            raise NotFoundError(f"게시글 {entry_id} 이(가) 등록되어 있지 않습니다.")
        row = await repo.get(entry_id)
    assert row is not None
    return {"item": target_to_out(row, tz_name=context.settings.timezone).model_dump()}


@router.delete("/{entry_id}", response_model=OkResponse)
async def remove_target(
    entry_id: int, context: AppContext = Depends(get_context)
) -> OkResponse:
    """대상 목록에서 제거한다. 이미 수집한 댓글 기록은 남는다."""
    async with context.database.session() as session:
        if not await TargetRepository(session).remove(entry_id):
            raise NotFoundError(f"게시글 {entry_id} 이(가) 등록되어 있지 않습니다.")
    await _sync_targets_file(context)
    return OkResponse()


@router.post("/discover", response_model=JobCreatedOut)
async def discover_targets(context: AppContext = Depends(get_context)) -> JobCreatedOut:
    """sitemap 으로 블로그 전체 게시글을 찾는 작업을 시작한다."""
    job_id = await context.job_manager.create_discover_job()
    return JobCreatedOut(job_id=job_id, total=1)


@router.post("/collect", response_model=JobCreatedOut)
async def collect_targets(
    payload: CollectIn, context: AppContext = Depends(get_context)
) -> JobCreatedOut:
    """지정한 게시글의 댓글을 수집하는 작업을 시작한다."""
    since = parse_user_datetime(payload.since, tz_name=context.settings.timezone)
    job_id = await context.job_manager.create_collect_job(payload.entry_ids or None, since=since)
    return JobCreatedOut(job_id=job_id, total=len(payload.entry_ids))


async def _sync_targets_file(context: AppContext) -> None:
    """수동으로 등록한 대상을 ``config/targets.yaml`` 에 반영한다.

    sitemap 으로 자동 발견한 게시글은 파일에 남기지 않는다. 파일이 수백 줄로 불어나
    사람이 관리할 수 없게 되기 때문이다.
    """
    settings = context.settings
    async with context.database.session() as session:
        rows = await TargetRepository(session).list_all()
    manual = [
        TargetSpec(entry_id=row.entry_id, url=row.url, title=row.title)
        for row in rows
        if row.source == "manual"
    ]
    try:
        save_targets(settings.targets_file, manual)
    except OSError:
        # 파일 저장 실패가 등록 자체를 취소시킬 이유는 없다. 기록만 남긴다.
        logger.exception("targets.yaml 저장에 실패했습니다: %s", settings.targets_file)


async def load_targets_from_file(context: AppContext) -> int:
    """기동 시 ``config/targets.yaml`` 의 대상을 DB 로 동기화한다."""
    settings = context.settings
    specs = load_targets(settings.targets_file, settings.blog_url)
    if not specs:
        return 0
    async with context.database.session() as session:
        return await TargetRepository(session).upsert_many(specs, source="manual")
