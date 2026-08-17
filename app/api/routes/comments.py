# app/api/routes/comments.py
"""댓글 조회와 일괄 선택 라우트.

목록 조회와 "필터 결과 전체 선택" 이 반드시 같은 조건을 쓰도록 두 경로가
동일한 :class:`CommentFilterIn` 을 거쳐 도메인 필터로 변환된다.
"""

from __future__ import annotations

from typing import Optional
from fastapi import APIRouter, Depends, Query

from ...domain.enums import CommentStatus
from ...domain.errors import ValidationError
from ...infrastructure.db.repositories import CommentRepository
from ...services.spam_rules import SpamScoringService
from ..deps import AppContext, get_context
from ..schemas import (
    MAX_PAGE_SIZE,
    CommentFilterIn,
    CommentListOut,
    RescoreIn,
    RescoreOut,
    SelectIdsIn,
    SelectIdsOut,
    comment_to_out,
)

router = APIRouter(prefix="/api/comments", tags=["comments"])


def _parse_int_csv(raw: Optional[str]) -> list[int]:
    """``723,722`` 형태의 질의 문자열을 정수 목록으로 바꾼다."""
    if not raw:
        return []
    values: list[int] = []
    for chunk in raw.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        try:
            values.append(int(piece))
        except ValueError as exc:
            raise ValidationError(f"게시글 번호가 올바르지 않습니다: {piece}") from exc
    return values


def _build_filter(
    entry_ids: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    nickname: Optional[str],
    content: Optional[str],
    levels: Optional[str],
    statuses: Optional[str],
    min_score: Optional[int],
) -> CommentFilterIn:
    """질의 문자열을 필터 모델로 모은다."""
    return CommentFilterIn(
        entry_ids=_parse_int_csv(entry_ids),
        date_from=date_from,
        date_to=date_to,
        nickname=nickname,
        content=content,
        levels=levels or [],
        statuses=statuses or [CommentStatus.ACTIVE.value],
        min_score=min_score,
    )


@router.get("", response_model=CommentListOut)
async def list_comments(
    context: AppContext = Depends(get_context),
    entry_ids: Optional[str] = Query(default=None, description="쉼표로 구분한 게시글 번호"),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
    nickname: Optional[str] = Query(default=None),
    content: Optional[str] = Query(default=None),
    levels: Optional[str] = Query(default=None, description="spam,suspicious,normal"),
    statuses: Optional[str] = Query(default=None, description="active,deleted,failed"),
    min_score: Optional[int] = Query(default=None, ge=0),
    page: int = Query(default=1, ge=1),
    size: Optional[int] = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> CommentListOut:
    """조건에 맞는 댓글을 페이지 단위로 돌려준다."""
    settings = context.settings
    page_size = size or settings.page_size
    payload = _build_filter(
        entry_ids, date_from, date_to, nickname, content, levels, statuses, min_score
    )
    try:
        criteria = payload.to_domain(tz_name=settings.timezone)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    async with context.database.session() as session:
        repo = CommentRepository(session)
        total = await repo.count(criteria)
        protected = await repo.count_protected(criteria)
        selectable = await repo.count_selectable(criteria)
        rows = await repo.list_page(
            criteria, offset=(page - 1) * page_size, limit=page_size
        )

    items = [comment_to_out(row, tz_name=settings.timezone) for row in rows]
    return CommentListOut(
        items=items,
        total=total,
        page=page,
        size=page_size,
        # 필터 조건 전체 기준 수치다. 현재 페이지가 아니라 선택 가능한 총량을 보여준다.
        # selectable 은 전체 선택이 실제로 고르는 건수와 같은 규칙으로 센다.
        # total - protected 로 계산하면 이미 삭제된 댓글이 남아 두 수치가 어긋난다.
        summary={"selectable": selectable, "whitelisted": protected},
    )


@router.post("/select-ids", response_model=SelectIdsOut)
async def select_ids(
    payload: SelectIdsIn, context: AppContext = Depends(get_context)
) -> SelectIdsOut:
    """필터 조건에 맞는 댓글 ID 전체를 돌려준다.

    보호 대상(운영자 댓글, 화이트리스트)은 항상 제외한다. 화면에서 전체 선택을
    눌러도 보호 대상이 절대 선택되지 않도록 서버에서 한 번 더 막는 장치다.
    """
    settings = context.settings
    try:
        criteria = payload.filter.to_domain(tz_name=settings.timezone)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc

    async with context.database.session() as session:
        repo = CommentRepository(session)
        # 삭제 작업이 제외하는 조건을 저장소의 selectable_condition 하나로 적용한다.
        # 목록의 summary.selectable 과 같은 함수를 쓰므로 두 수치가 갈라질 수 없다.
        total = await repo.count(criteria)
        rows = await repo.selectable_with_levels(criteria)

    return SelectIdsOut(
        ids=[comment_id for comment_id, _ in rows],
        levels=[level for _, level in rows],
        count=len(rows),
        whitelisted_excluded=total - len(rows),
    )


@router.post("/rescore", response_model=RescoreOut)
async def rescore(
    payload: RescoreIn, context: AppContext = Depends(get_context)
) -> RescoreOut:
    """현재 규칙 파일로 스팸 점수를 다시 계산한다."""
    service = SpamScoringService(
        database=context.database,
        rules_path=context.settings.rules_file,
        tz_name=context.settings.timezone,
    )
    updated = await service.rescore(payload.entry_ids or None)
    return RescoreOut(updated=updated)
