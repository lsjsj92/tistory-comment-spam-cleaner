# app/services/collector.py
"""댓글 수집 서비스.

게시글 하나는 커서 페이징이라 순차로만 읽을 수 있고, 게시글끼리는 서로 독립이라
병렬로 읽을 수 있다. 이 서비스는 그 두 가지 성질을 그대로 코드 구조로 옮긴다.

수집 도중 중단되어도 이미 읽은 페이지는 남아야 하므로 페이지 단위로 저장한다.
게시글 하나가 실패해도 나머지 수집을 막지 않도록 실패는 결과 객체로 돌려준다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import datetime
from typing import Optional, Union

from ..domain.enums import CommentStatus
from ..domain.errors import ValidationError
from ..domain.models import CollectResult, CommentFilter, CommentPage, ParsedComment
from ..infrastructure.db.repositories import CommentRepository, TargetRepository
from ..infrastructure.db.session import Database
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.timeutils import from_epoch
from ..infrastructure.tistory.client import TistoryClient, next_comment_cursor

logger = get_logger(__name__)

# 진행률 보고 콜백 형식
PageCallback = Callable[[int, int], Awaitable[None]]
EntryCallback = Callable[[CollectResult], Awaitable[None]]

# 게시글별 저장 건수를 셀 때 쓰는 상태 조건. 비워두면 삭제 완료분까지 모두 센다.
ALL_STATUSES: tuple[CommentStatus, ...] = ()

# 취소로 아예 시작하지 못한 게시글의 사유. 실패와 구분하기 위해 문구를 고정한다.
CANCELLED_MESSAGE = "사용자 취소로 건너뜀"


class CollectorService:
    """티스토리 댓글을 읽어 데이터베이스에 적재한다."""

    def __init__(self, client: TistoryClient, database: Database, tz_name: str) -> None:
        self._client = client
        self._database = database
        # 목록 HTML 의 시각 해석은 클라이언트의 파서가 이미 이 시간대로 처리한다.
        # 서비스는 저장/표시 규약을 확인할 수 있도록 값만 보관한다.
        self._tz_name = tz_name

    @property
    def tz_name(self) -> str:
        """이 서비스가 전제하는 사용자 시간대 이름."""
        return self._tz_name

    # ------------------------------------------------------------------
    # 게시글 1건 수집
    # ------------------------------------------------------------------
    async def collect_entry(
        self,
        entry_id: int,
        *,
        since: Optional[datetime] = None,
        max_pages: Optional[int] = None,
        on_page: Optional[PageCallback] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> CollectResult:
        """게시글 하나의 댓글을 커서 페이징으로 끝까지 수집한다.

        Args:
            entry_id: 게시글 번호.
            since: 이 시각(UTC aware)보다 과거로 내려가면 조기 종료한다.
                경계에 걸친 배치는 통째로 저장하므로 누락이 생기지 않는다.
            max_pages: 읽을 최대 페이지 수. 시험 수집에 쓴다.
            on_page: 페이지를 저장할 때마다 (페이지 번호, 누적 건수) 로 호출한다.
            cancel_event: 설정되면 다음 페이지를 요청하지 않고 멈춘다.
                이미 저장한 페이지는 그대로 유지한다.

        Returns:
            수집 요약. 실패해도 예외를 올리지 않고 ``error`` 가 채워진 결과를 돌려준다.
        """
        state = _EntryProgress(entry_id)
        try:
            await self._run_pages(
                state,
                since=since,
                max_pages=max_pages,
                on_page=on_page,
                cancel_event=cancel_event,
            )
            await self._update_target_stats(entry_id)
        except Exception as exc:
            if _is_cancelled(cancel_event):
                # 취소가 재시도 대기를 끊으면 마지막 서버 오류가 그대로 올라온다.
                # 그것을 실패로 기록하면 사용자가 취소만 눌렀는데 실패 건수가 늘고
                # 스택트레이스가 쌓인다. 취소는 취소로 보고한다.
                logger.info("게시글 %s 수집을 사용자 취소로 중단했습니다.", entry_id)
                return state.to_result(stopped_early=True, error=CANCELLED_MESSAGE)
            logger.exception("게시글 %s 수집이 실패했습니다.", entry_id)
            return state.to_result(error=str(exc) or exc.__class__.__name__)

        logger.info(
            "게시글 %s 수집 완료: %d건 (신규 %d, 갱신 %d, %d페이지, 조기종료=%s)",
            entry_id,
            state.fetched,
            state.inserted,
            state.updated,
            state.pages,
            state.stopped_early,
        )
        return state.to_result()

    async def _run_pages(
        self,
        state: _EntryProgress,
        *,
        since: Optional[datetime],
        max_pages: Optional[int],
        on_page: Optional[PageCallback],
        cancel_event: Optional[asyncio.Event],
    ) -> None:
        """커서를 따라가며 페이지를 읽고 저장한다."""
        cursor: Optional[int] = None
        seen_cursors: set[int] = set()

        while True:
            if _is_cancelled(cancel_event):
                logger.info(
                    "게시글 %s 수집이 취소되었습니다. %d페이지까지 저장했습니다.",
                    state.entry_id,
                    state.pages,
                )
                state.stopped_early = True
                return

            page = await self._client.fetch_comment_page(state.entry_id, cursor)
            state.pages += 1
            comments = self._with_second_precision(page)
            await self._store(state, comments)
            await _notify_page(on_page, state.pages, state.fetched)

            if _batch_reaches(comments, since):
                state.stopped_early = True
                return
            if not page.has_more:
                return
            if max_pages is not None and state.pages >= max_pages:
                state.stopped_early = True
                return

            # ts 는 미만(exclusive) 이라 그대로 넘기면 같은 초의 댓글이 경계에서 잘린다.
            # 규칙은 next_comment_cursor 한 곳에만 둔다. 삭제 검증도 같은 함수를 쓴다.
            next_cursor = next_comment_cursor(page, cursor)
            if next_cursor is None or next_cursor == cursor or next_cursor in seen_cursors:
                # 같은 커서를 다시 받으면 서버가 끝을 알려주지 못하는 상태다.
                # 여기서 멈추지 않으면 같은 페이지를 영원히 반복해서 읽는다.
                logger.warning(
                    "게시글 %s 의 커서가 진행하지 않아 수집을 중단합니다 (커서=%s, %d페이지).",
                    state.entry_id,
                    next_cursor,
                    state.pages,
                )
                state.stopped_early = True
                return

            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _with_second_precision(self, page: CommentPage) -> list[ParsedComment]:
        """배치에서 가장 오래된 댓글의 시각을 초 단위로 보정한다.

        목록 HTML 은 분 단위까지만 준다. 응답의 ``ts`` 는 그 배치에서 가장 오래된
        댓글(``firstCommentId``)의 초 단위 epoch 이므로 그 한 건만 정확해진다.
        나머지는 보정할 근거가 없어 분 단위 그대로 둔다.
        """
        comments = list(page.comments)
        if page.cursor is None or page.first_comment_id is None:
            return comments

        for index, comment in enumerate(comments):
            if comment.comment_id != page.first_comment_id:
                continue
            comments[index] = replace(
                comment, written_ts=page.cursor, written_at=from_epoch(page.cursor)
            )
            break
        else:
            logger.debug(
                "커서가 가리키는 댓글 %s 이(가) 배치에 없어 시각 보정을 건너뜁니다.",
                page.first_comment_id,
            )
        return comments

    async def _store(self, state: _EntryProgress, comments: Sequence[ParsedComment]) -> None:
        """한 페이지를 즉시 저장한다. 중간에 끊겨도 여기까지는 남는다."""
        if not comments:
            return
        async with self._database.session() as session:
            inserted, updated = await CommentRepository(session).upsert_many(comments)
        state.fetched += len(comments)
        state.inserted += inserted
        state.updated += updated

    async def _update_target_stats(self, entry_id: int) -> None:
        """대상 목록에 저장된 총 댓글 수와 수집 시각을 반영한다."""
        async with self._database.session() as session:
            total = await CommentRepository(session).count(
                CommentFilter(entry_ids=(entry_id,), statuses=ALL_STATUSES)
            )
            await TargetRepository(session).update_collection_stats(
                entry_id, comment_count=total
            )

    # ------------------------------------------------------------------
    # 여러 게시글 수집
    # ------------------------------------------------------------------
    async def collect_many(
        self,
        entry_ids: Sequence[int],
        *,
        since: Optional[datetime] = None,
        concurrency: int,
        on_entry_done: Optional[EntryCallback] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> list[CollectResult]:
        """여러 게시글을 병렬로 수집한다.

        게시글 안에서는 커서가 직전 응답에 의존하므로 순차로 읽고, 게시글 사이만
        세마포어로 동시성을 제한한다.

        Args:
            entry_ids: 수집할 게시글 번호 목록.
            since: 각 게시글의 조기 종료 기준 시각(UTC aware).
            concurrency: 동시에 처리할 게시글 수.
            on_entry_done: 게시글 하나가 끝날 때마다 결과와 함께 호출한다.
            cancel_event: 설정되면 아직 시작하지 않은 게시글은 건너뛰고
                진행 중인 게시글은 현재 페이지까지만 마무리한다.

        Returns:
            게시글마다 하나씩, 입력과 같은 개수의 결과 목록. 각 결과는
            ``entry_id`` 로 식별한다.

        Raises:
            ValidationError: ``concurrency`` 가 1 미만인 경우.
        """
        if concurrency < 1:
            raise ValidationError("동시 수집 게시글 수는 1 이상이어야 합니다.")
        if not entry_ids:
            return []

        semaphore = asyncio.Semaphore(concurrency)

        async def run(entry_id: int) -> CollectResult:
            async with semaphore:
                if _is_cancelled(cancel_event):
                    logger.info("취소되어 게시글 %s 수집을 시작하지 않습니다.", entry_id)
                    result = _cancelled_result(entry_id)
                else:
                    result = await self.collect_entry(
                        entry_id, since=since, cancel_event=cancel_event
                    )
            await _notify_entry(on_entry_done, result)
            return result

        outcomes = await asyncio.gather(
            *(run(entry_id) for entry_id in entry_ids), return_exceptions=True
        )
        return [
            _as_result(entry_id, outcome)
            for entry_id, outcome in zip(entry_ids, outcomes)
        ]


class _EntryProgress:
    """게시글 1건 수집 중 누적되는 값. 실패해도 진행분을 보고하기 위해 따로 둔다."""

    __slots__ = ("entry_id", "fetched", "inserted", "updated", "pages", "stopped_early")

    def __init__(self, entry_id: int) -> None:
        self.entry_id = entry_id
        self.fetched = 0
        self.inserted = 0
        self.updated = 0
        self.pages = 0
        self.stopped_early = False

    def to_result(
        self, *, error: Optional[str] = None, stopped_early: Optional[bool] = None
    ) -> CollectResult:
        return CollectResult(
            entry_id=self.entry_id,
            fetched=self.fetched,
            inserted=self.inserted,
            updated=self.updated,
            pages=self.pages,
            stopped_early=self.stopped_early if stopped_early is None else stopped_early,
            error=error,
        )


def _is_cancelled(cancel_event: Optional[asyncio.Event]) -> bool:
    """취소 신호가 켜졌는지 여부."""
    return cancel_event is not None and cancel_event.is_set()


def _cancelled_result(entry_id: int) -> CollectResult:
    """취소로 아예 시작하지 못한 게시글의 결과."""
    return CollectResult(
        entry_id=entry_id,
        fetched=0,
        inserted=0,
        updated=0,
        pages=0,
        stopped_early=True,
        error=CANCELLED_MESSAGE,
    )


def _batch_reaches(comments: Sequence[ParsedComment], since: Optional[datetime]) -> bool:
    """배치의 가장 오래된 댓글이 기준 시각보다 과거인지 여부."""
    if since is None or not comments:
        return False
    return min(comment.written_at for comment in comments) < since


async def _notify_page(callback: Optional[PageCallback], page_index: int, total: int) -> None:
    """진행률 콜백을 호출한다. 콜백 실패가 수집을 멈추게 두지 않는다."""
    if callback is None:
        return
    try:
        await callback(page_index, total)
    except Exception:
        logger.warning("페이지 진행률 콜백에서 예외가 발생했습니다.", exc_info=True)


async def _notify_entry(callback: Optional[EntryCallback], result: CollectResult) -> None:
    """게시글 완료 콜백을 호출한다. 콜백 실패가 다른 게시글에 번지지 않게 한다."""
    if callback is None:
        return
    try:
        await callback(result)
    except Exception:
        logger.warning("게시글 완료 콜백에서 예외가 발생했습니다.", exc_info=True)


def _as_result(entry_id: int, outcome: Union[CollectResult, BaseException]) -> CollectResult:
    """gather 가 돌려준 값을 결과 객체로 통일한다."""
    if isinstance(outcome, CollectResult):
        return outcome
    logger.error("게시글 %s 처리 중 예외가 격리되었습니다: %s", entry_id, outcome)
    return CollectResult(
        entry_id=entry_id,
        fetched=0,
        inserted=0,
        updated=0,
        pages=0,
        error=str(outcome) or outcome.__class__.__name__,
    )
