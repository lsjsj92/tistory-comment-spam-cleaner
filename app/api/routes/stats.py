# app/api/routes/stats.py
"""대시보드 통계 라우트."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from ...domain.enums import JobStatus
from ...infrastructure.db.repositories import (
    CommentRepository,
    JobRepository,
    TargetRepository,
)
from ...infrastructure.timeutils import get_zone
from ..deps import AppContext, get_context
from ..schemas import OverviewOut, stats_row_to_dict

router = APIRouter(prefix="/api/stats", tags=["stats"])

# 대시보드에 보여줄 상위 작성자 수
_TOP_NICKNAME_LIMIT = 10


def _tz_offset_hours(tz_name: str) -> int:
    """현재 시점 기준 시간대 오프셋(시간).

    시간대별 집계를 SQL 안에서 처리하려면 오프셋이 필요하다. 한국은 서머타임이 없어
    항상 9지만, 다른 시간대를 쓰는 사용자를 위해 실제 값을 계산한다.
    """
    now = datetime.now(timezone.utc).astimezone(get_zone(tz_name))
    offset = now.utcoffset()
    return int(offset.total_seconds() // 3600) if offset else 0


@router.get("/overview", response_model=OverviewOut)
async def get_overview(context: AppContext = Depends(get_context)) -> OverviewOut:
    """대시보드 한 화면에 필요한 집계를 한 번에 돌려준다."""
    tz_name = context.settings.timezone
    offset_hours = _tz_offset_hours(tz_name)

    async with context.database.session() as session:
        comments = CommentRepository(session)
        targets = TargetRepository(session)
        jobs = JobRepository(session)

        totals = await comments.totals()
        per_entry = await comments.stats_by_entry()
        histogram = await comments.hourly_histogram(tz_offset_hours=offset_hours)
        top_nicknames = await comments.top_nicknames(limit=_TOP_NICKNAME_LIMIT)
        target_rows = await targets.list_all()
        running = await jobs.list_by_status([JobStatus.RUNNING])

    titles = {row.entry_id: row.title for row in target_rows}
    totals["targets"] = len(target_rows)

    return OverviewOut(
        totals=totals,
        targets=[
            stats_row_to_dict(row, tz_name=tz_name, title=titles.get(row["entry_id"]))
            for row in per_entry
        ],
        histogram=histogram,
        top_nicknames=top_nicknames,
        running_jobs=len(running),
    )
