# app/api/routes/jobs.py
"""작업 생성, 조회, 제어와 진행률 스트리밍 라우트."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ...domain.enums import JobItemStatus, JobStatus, JobType
from ...domain.errors import NotFoundError, ValidationError
from ...infrastructure.db.repositories import JobRepository
from ...infrastructure.logging_setup import get_logger
from ...services.jobs import GLOBAL_CHANNEL
from ..deps import AppContext, get_context
from ..schemas import (
    MAX_PAGE_SIZE,
    DeleteJobIn,
    DeleteJobOut,
    JobDetailOut,
    JobItemListOut,
    JobListOut,
    OkResponse,
    job_item_to_out,
    job_to_out,
    progress_to_dict,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# SSE 연결 유지용 주석 프레임 전송 주기(초). 프록시가 유휴 연결을 끊는 것을 막는다.
_KEEPALIVE_INTERVAL = 15.0


@router.post("/delete", response_model=DeleteJobOut)
async def create_delete_job(
    payload: DeleteJobIn, context: AppContext = Depends(get_context)
) -> DeleteJobOut:
    """삭제 작업을 만들고 즉시 실행한다.

    대상 확정, 보호 대상 제외, 백업 생성이 모두 끝난 뒤에야 실제 요청이 나간다.
    """
    criteria = None
    if payload.filter is not None:
        try:
            criteria = payload.filter.to_domain(tz_name=context.settings.timezone)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    result = await context.job_manager.create_delete_job(
        comment_ids=payload.comment_ids,
        criteria=criteria,
        dry_run=payload.dry_run,
        rps=payload.rps,
        concurrency=payload.concurrency,
        verify_after=payload.verify_after,
        allow_normal=payload.allow_normal,
    )
    return DeleteJobOut(**result)


@router.get("", response_model=JobListOut)
async def list_jobs(
    context: AppContext = Depends(get_context),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=MAX_PAGE_SIZE),
    type: Optional[str] = Query(default=None),
) -> JobListOut:
    """작업 목록을 최신순으로 돌려준다."""
    job_type = _parse_job_type(type)
    async with context.database.session() as session:
        repo = JobRepository(session)
        rows = await repo.list_recent(limit=size, offset=(page - 1) * size, job_type=job_type)
        total = await repo.count_all(job_type=job_type)
    return JobListOut(
        items=[job_to_out(row, tz_name=context.settings.timezone) for row in rows],
        total=total,
        page=page,
        size=size,
    )


@router.get("/stream")
async def stream_all_jobs(
    request: Request, context: AppContext = Depends(get_context)
) -> StreamingResponse:
    """모든 작업의 진행률을 하나의 스트림으로 흘린다."""
    return _sse_response(request, context, GLOBAL_CHANNEL)


@router.get("/{job_id}", response_model=JobDetailOut)
async def get_job(job_id: int, context: AppContext = Depends(get_context)) -> JobDetailOut:
    """작업 상세와 항목 집계."""
    async with context.database.session() as session:
        repo = JobRepository(session)
        row = await repo.get(job_id)
        if row is None:
            raise NotFoundError(f"작업 {job_id} 을(를) 찾을 수 없습니다.")
        counts = await repo.item_counts(job_id)
    return JobDetailOut(
        job=job_to_out(row, tz_name=context.settings.timezone), counts=counts
    )


@router.get("/{job_id}/items", response_model=JobItemListOut)
async def list_job_items(
    job_id: int,
    context: AppContext = Depends(get_context),
    status: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
) -> JobItemListOut:
    """작업이 처리한 항목 목록. 실패 사유를 확인하는 데 쓴다."""
    item_status = _parse_item_status(status)
    async with context.database.session() as session:
        repo = JobRepository(session)
        if await repo.get(job_id) is None:
            raise NotFoundError(f"작업 {job_id} 을(를) 찾을 수 없습니다.")
        rows = await repo.list_items(
            job_id, limit=size, offset=(page - 1) * size, status=item_status
        )
        counts = await repo.item_counts(job_id)
    total = counts.get(item_status.value, 0) if item_status else sum(counts.values())
    return JobItemListOut(
        items=[job_item_to_out(row, tz_name=context.settings.timezone) for row in rows],
        total=total,
        page=page,
        size=size,
    )


@router.post("/{job_id}/cancel", response_model=OkResponse)
async def cancel_job(job_id: int, context: AppContext = Depends(get_context)) -> OkResponse:
    """진행 중인 작업에 취소를 요청한다."""
    await context.job_manager.cancel(job_id)
    return OkResponse()


@router.post("/{job_id}/resume", response_model=OkResponse)
async def resume_job(job_id: int, context: AppContext = Depends(get_context)) -> OkResponse:
    """일시정지된 삭제 작업을 남은 항목부터 이어서 실행한다."""
    await context.job_manager.resume(job_id)
    return OkResponse()


@router.post("/{job_id}/retry-failed", response_model=DeleteJobOut)
async def retry_failed(
    job_id: int, context: AppContext = Depends(get_context)
) -> DeleteJobOut:
    """실패한 항목만 모아 새 삭제 작업을 만든다."""
    result = await context.job_manager.retry_failed(job_id)
    return DeleteJobOut(**result)


@router.get("/{job_id}/stream")
async def stream_job(
    job_id: int, request: Request, context: AppContext = Depends(get_context)
) -> StreamingResponse:
    """단일 작업의 진행률 SSE 스트림."""
    return _sse_response(request, context, job_id)


def _sse_response(
    request: Request, context: AppContext, channel: int
) -> StreamingResponse:
    """SSE 응답을 만든다. 클라이언트가 끊으면 구독을 정리한다."""
    tz_name = context.settings.timezone

    async def event_source() -> AsyncIterator[bytes]:
        """진행률을 SSE 프레임으로 흘린다.

        다음 항목을 기다리는 일은 별도 태스크로 두고 그 태스크를 기다린다.
        ``wait_for`` 로 제너레이터 자체에 타임아웃을 걸면 취소가 제너레이터 본문으로
        전파되어 구독이 해제되고 스트림이 닫힌다. 연결 유지용 프레임을 보내려다
        오히려 연결을 끊는 셈이 되므로, 대기 태스크는 살려 둔 채 다음 회차에서
        이어서 기다린다.
        """
        stream = context.job_manager.stream(channel)
        iterator = stream.__aiter__()
        waiter: Optional[asyncio.Task] = None
        try:
            while True:
                if await request.is_disconnected():
                    return
                if waiter is None:
                    waiter = asyncio.ensure_future(iterator.__anext__())

                done, _ = await asyncio.wait({waiter}, timeout=_KEEPALIVE_INTERVAL)
                if not done:
                    # 연결 유지용 주석 프레임. 클라이언트는 무시한다.
                    yield b": keepalive\n\n"
                    continue

                finished, waiter = waiter, None
                try:
                    progress = finished.result()
                except StopAsyncIteration:
                    return

                payload = json.dumps(
                    progress_to_dict(progress, tz_name=tz_name), ensure_ascii=False
                )
                event = "done" if _is_terminal(progress.status) else "progress"
                yield f"event: {event}\ndata: {payload}\n\n".encode("utf-8")
                if event == "done" and channel != GLOBAL_CHANNEL:
                    return
        finally:
            if waiter is not None:
                waiter.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await waiter
            with contextlib.suppress(Exception):
                await stream.aclose()

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx 등 역방향 프록시의 버퍼링을 끈다.
            "X-Accel-Buffering": "no",
        },
    )


def _is_terminal(status: str) -> bool:
    """작업이 종료 상태인지 확인한다. 알 수 없는 값은 진행 중으로 본다."""
    try:
        return JobStatus(status).is_terminal
    except ValueError:
        return False


def _parse_job_type(raw: Optional[str]) -> Optional[JobType]:
    if not raw:
        return None
    try:
        return JobType(raw)
    except ValueError as exc:
        raise ValidationError(f"알 수 없는 작업 종류입니다: {raw}") from exc


def _parse_item_status(raw: Optional[str]) -> Optional[JobItemStatus]:
    if not raw:
        return None
    try:
        return JobItemStatus(raw)
    except ValueError as exc:
        raise ValidationError(f"알 수 없는 항목 상태입니다: {raw}") from exc
