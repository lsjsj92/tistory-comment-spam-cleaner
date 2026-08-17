# app/services/deleter.py
"""댓글 삭제 실행 엔진.

되돌릴 수 없는 조작이므로 다음 원칙을 지킨다.

1. 화이트리스트와 운영자 댓글은 어떤 경로로도 삭제 대상에 들어가지 않는다.
2. 워커는 취소 신호와 서킷 브레이커를 매 건마다 확인한다.
3. 결과는 단일 기록자(writer)가 묶어서 저장한다. SQLite 에 동시 쓰기를 몰지 않기 위함이다.
4. 실행 후 실제로 사라졌는지 다시 조회해 확인한다. 응답이 성공이어도 믿지 않는다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Optional

from ..domain.enums import CommentStatus, JobItemStatus
from ..domain.errors import CircuitOpenError, TistoryApiError
from ..domain.models import DeleteOutcome
from ..infrastructure.db.repositories import CommentRepository, JobRepository
from ..infrastructure.db.session import Database
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.tistory.client import TistoryClient, next_comment_cursor

logger = get_logger(__name__)

# 결과를 모아 한 번에 저장할 최대 건수
_FLUSH_BATCH_SIZE = 25

# 배치가 차지 않아도 이 시간이 지나면 저장한다(초). 진행률이 멈춰 보이지 않게 하기 위함이다.
_FLUSH_INTERVAL = 1.0

# 워커 종료를 알리는 표식
_SENTINEL = object()

# 저장 실패 시 재시도 횟수와 간격(초).
# "database is locked" 는 다른 트랜잭션이 끝나면 곧 풀리는 일시적 상태다. 한 번만
# 더 시도해도 진행 중이던 배치를 대부분 회수할 수 있고, 그 배치가 유실되면
# 티스토리에서는 지워졌는데 DB 에는 기록이 없는 댓글이 남는다.
_FLUSH_RETRIES = 2
_FLUSH_RETRY_DELAY = 1.5


@dataclass
class DeleteSummary:
    """삭제 실행 결과 요약."""

    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    # 성공 중에서 "요청 전에 이미 사라져 있던" 건수. succeeded 에 포함된 값이다.
    # 부모 댓글을 지우면 대댓글이 함께 사라지므로 대량 삭제에서는 흔하게 발생한다.
    # 사용자가 "실제로 내가 몇 건을 지웠는가" 를 알 수 있도록 따로 센다.
    already_gone: int = 0
    cancelled: bool = False
    circuit_open: bool = False
    # 결과를 DB 에 기록하지 못해 중단된 상태. 기록 없이 계속 지우는 것보다
    # 멈추는 편이 낫다. 무엇을 지웠는지 모르는 상황이 가장 위험하다.
    aborted: bool = False
    verified_failures: list[int] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def done(self) -> int:
        return self.succeeded + self.failed + self.skipped

    @property
    def should_stop(self) -> bool:
        """워커가 더 진행하면 안 되는 상태인지 여부."""
        return self.circuit_open or self.aborted


@dataclass
class VerifyResult:
    """사후 검증 결과.

    Attributes:
        still_alive: 삭제했다고 기록했으나 재조회에서 여전히 보이는 댓글 번호.
        unverified_entries: 조회 자체가 실패해 확인하지 못한 게시글 번호.
    """

    still_alive: list[int] = field(default_factory=list)
    unverified_entries: list[int] = field(default_factory=list)
    #: 실패로 기록했으나 재조회에서 보이지 않아 실제로는 지워진 것으로 정정한 댓글
    actually_deleted: list[int] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        """남은 것도 없고 확인 못 한 것도 없는 상태인지 여부."""
        return not self.still_alive and not self.unverified_entries


ProgressCallback = Callable[[DeleteSummary], Awaitable[None]]


class DeletionService:
    """댓글 ID 목록을 받아 실제 삭제를 수행한다."""

    def __init__(
        self,
        *,
        client: TistoryClient,
        database: Database,
        concurrency: int,
        dry_run: bool,
    ) -> None:
        self._client = client
        self._database = database
        self._concurrency = max(1, concurrency)
        self._dry_run = dry_run

    async def run(
        self,
        job_id: int,
        comment_ids: Sequence[int],
        *,
        cancel_event: asyncio.Event,
        on_progress: Optional[ProgressCallback] = None,
    ) -> DeleteSummary:
        """작업 항목을 처리한다.

        Args:
            job_id: 결과를 기록할 작업 번호.
            comment_ids: 처리할 댓글 번호 목록. 이미 화이트리스트가 제외된 상태여야 한다.
            cancel_event: 설정되면 남은 항목을 처리하지 않고 중단한다.
            on_progress: 진행 상황을 알리는 콜백.
        """
        summary = DeleteSummary(total=len(comment_ids))
        if not comment_ids:
            return summary

        work_queue: asyncio.Queue = asyncio.Queue()
        result_queue: asyncio.Queue = asyncio.Queue()

        for comment_id in comment_ids:
            work_queue.put_nowait(comment_id)

        writer = asyncio.create_task(
            self._write_results(job_id, result_queue, summary, on_progress),
            name=f"delete-writer-{job_id}",
        )
        workers = [
            asyncio.create_task(
                self._worker(work_queue, result_queue, cancel_event, summary),
                name=f"delete-worker-{job_id}-{index}",
            )
            for index in range(self._concurrency)
        ]

        try:
            await asyncio.gather(*workers)
        finally:
            # 워커가 모두 끝난 뒤에만 기록자를 종료시켜 결과 유실을 막는다.
            await result_queue.put(_SENTINEL)
            await writer

        if cancel_event.is_set():
            summary.cancelled = True
        return summary

    async def _worker(
        self,
        work_queue: asyncio.Queue,
        result_queue: asyncio.Queue,
        cancel_event: asyncio.Event,
        summary: DeleteSummary,
    ) -> None:
        """큐에서 댓글을 하나씩 꺼내 삭제한다."""
        while True:
            try:
                comment_id = work_queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            if cancel_event.is_set() or summary.should_stop:
                # 남은 항목은 pending 으로 두어 재개할 수 있게 한다.
                work_queue.task_done()
                return

            try:
                outcome = await self._client.delete_comment(comment_id, dry_run=self._dry_run)
            except CircuitOpenError as exc:
                summary.circuit_open = True
                summary.error = exc.message
                logger.error("서킷 브레이커로 삭제를 중단합니다: %s", exc.message)
                work_queue.task_done()
                return
            except TistoryApiError as exc:
                outcome = DeleteOutcome(
                    comment_id=comment_id,
                    success=False,
                    http_status=exc.http_status,
                    message=exc.message,
                )
            except Exception as exc:  # noqa: BLE001 - 한 건의 예외로 작업 전체가 죽으면 안 된다.
                logger.exception("댓글 %s 삭제 중 예기치 못한 오류", comment_id)
                outcome = DeleteOutcome(
                    comment_id=comment_id, success=False, message=f"내부 오류: {exc}"
                )

            # 결과는 회로 상태와 무관하게 항상 기록한다. 실패 사유가 남아야
            # 사용자가 원인을 확인하고 재개를 판단할 수 있다.
            await result_queue.put(outcome)
            work_queue.task_done()

            # 연속 실패를 세는 주체는 여기다. 전송 계층이 아니라 "댓글 한 건의 삭제"
            # 단위로 세야 설정의 "연속 실패 N회" 와 의미가 맞고, 재시도 대상이 아닌
            # 권한 오류(세션 만료)도 빠짐없이 잡힌다.
            if await self._record_outcome(outcome, summary):
                return

    async def _record_outcome(self, outcome: DeleteOutcome, summary: DeleteSummary) -> bool:
        """결과를 서킷 브레이커에 반영하고 회로가 열렸는지 알려준다."""
        breaker = self._client.circuit_breaker
        if breaker is None:
            return False
        if outcome.success:
            await breaker.record_success()
            return False
        if await breaker.record_failure(outcome.message):
            summary.circuit_open = True
            summary.error = outcome.message
            logger.error("연속 실패로 삭제를 중단합니다: %s", outcome.message)
            return True
        return False

    async def _write_results(
        self,
        job_id: int,
        result_queue: asyncio.Queue,
        summary: DeleteSummary,
        on_progress: Optional[ProgressCallback],
    ) -> None:
        """결과를 모아 DB 에 반영하는 단일 기록자.

        기록에 실패하면 조용히 죽지 않고 ``summary.aborted`` 를 세워 워커를 멈춘다.
        기록자가 죽은 채 워커가 계속 돌면 티스토리에서는 지워지는데 DB 에는 아무
        흔적이 없는, 되돌릴 수도 추적할 수도 없는 상태가 된다.
        """
        buffer: list[DeleteOutcome] = []

        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        result_queue.get(), timeout=_FLUSH_INTERVAL
                    )
                except asyncio.TimeoutError:
                    if buffer:
                        await self._flush_with_retry(job_id, buffer, summary, on_progress)
                        buffer.clear()
                    continue

                if item is _SENTINEL:
                    if buffer:
                        await self._flush_with_retry(job_id, buffer, summary, on_progress)
                    return

                buffer.append(item)
                if len(buffer) >= _FLUSH_BATCH_SIZE:
                    await self._flush_with_retry(job_id, buffer, summary, on_progress)
                    buffer.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 어떤 저장 실패든 작업을 멈춰야 한다.
            summary.aborted = True
            summary.error = f"결과를 저장하지 못해 삭제를 중단했습니다: {exc}"
            logger.exception(
                "작업 %d 의 결과 기록에 실패했습니다. 남은 항목 처리를 중단합니다.", job_id
            )
            # 아직 큐에 남아 있는 결과는 잃는다. 워커가 곧 멈추므로 그 수는 제한적이며,
            # 해당 항목은 pending 으로 남아 재개 시 다시 처리된다.

    async def _flush_with_retry(
        self,
        job_id: int,
        outcomes: Sequence[DeleteOutcome],
        summary: DeleteSummary,
        on_progress: Optional[ProgressCallback],
    ) -> None:
        """저장을 시도하고, 일시적 실패면 잠시 뒤 한 번 더 시도한다.

        순서가 중요하다. **첫 실패에서 곧바로 중단 신호를 세운 뒤** 재시도한다.
        재시도를 먼저 하면 그 대기 시간 동안 워커가 계속 삭제하고, 결국 실패했을 때
        기록 없이 지워진 댓글이 그만큼 늘어난다. 반대로 먼저 멈춰 두면 재시도가
        성공했을 때 이 배치는 살아남고, 남은 항목은 pending 으로 안전하게 보존된다.

        마지막 시도까지 실패하면 예외를 그대로 올려 기록자가 중단 처리하게 한다.
        """
        for attempt in range(1, _FLUSH_RETRIES + 1):
            try:
                await self._flush(job_id, outcomes, summary, on_progress)
                return
            except Exception as exc:  # noqa: BLE001 - 마지막 시도면 그대로 올린다.
                if attempt >= _FLUSH_RETRIES:
                    raise
                if not summary.aborted:
                    summary.aborted = True
                    summary.error = f"결과를 저장하지 못해 삭제를 중단했습니다: {exc}"
                logger.warning(
                    "결과 저장 실패, 워커를 멈추고 %.1f초 뒤 다시 시도합니다 (%d/%d): %s",
                    _FLUSH_RETRY_DELAY,
                    attempt,
                    _FLUSH_RETRIES,
                    exc,
                )
                await asyncio.sleep(_FLUSH_RETRY_DELAY)

    async def _flush(
        self,
        job_id: int,
        outcomes: Sequence[DeleteOutcome],
        summary: DeleteSummary,
        on_progress: Optional[ProgressCallback],
    ) -> None:
        """버퍼에 쌓인 결과를 한 트랜잭션으로 저장한다."""
        succeeded = [item for item in outcomes if item.success]
        failed = [item for item in outcomes if not item.success]

        async with self._database.session() as session:
            jobs = JobRepository(session)
            comments = CommentRepository(session)

            for outcome in outcomes:
                await jobs.update_item(
                    job_id,
                    outcome.comment_id,
                    status=(
                        JobItemStatus.SUCCEEDED if outcome.success else JobItemStatus.FAILED
                    ),
                    attempts=outcome.attempts,
                    http_status=outcome.http_status,
                    message=outcome.message,
                )

            if succeeded and not self._dry_run:
                # 드라이런에서는 실제로 지운 것이 아니므로 상태를 바꾸지 않는다.
                await comments.mark_status(
                    [item.comment_id for item in succeeded], CommentStatus.DELETED
                )
            if failed:
                # 사유가 다른 실패를 한 덩어리로 묶으면 원인 진단이 어긋난다.
                # 같은 사유끼리만 모아 각자의 메시지를 남긴다.
                by_reason: dict[str, list[int]] = {}
                for item in failed:
                    by_reason.setdefault(item.message[:500], []).append(item.comment_id)
                for reason, ids in by_reason.items():
                    await comments.mark_status(ids, CommentStatus.FAILED, error=reason)

            summary.succeeded += len(succeeded)
            summary.failed += len(failed)
            summary.already_gone += sum(1 for item in succeeded if item.already_gone)
            await jobs.update_fields(
                job_id,
                done=summary.done,
                succeeded=summary.succeeded,
                failed=summary.failed,
            )

        if on_progress is not None:
            await on_progress(summary)

    async def verify(
        self,
        job_id: int,
        entry_ids: Sequence[int],
        deleted_ids: Sequence[int],
        failed_ids: Sequence[int] = (),
    ) -> VerifyResult:
        """기록한 결과가 블로그의 실제 상태와 맞는지 다시 조회해 확인한다.

        두 방향을 모두 본다.

        - 성공으로 기록했는데 아직 보이면: 실패로 되돌린다. 티스토리가 200 을
          돌려줬어도 실제로 지워졌다는 보장은 없기 때문이다.
        - 실패로 기록했는데 보이지 않으면: 실제로는 지워진 것이므로 정정한다.
          응답 판정이 틀리면 전량이 실패로 기록될 수 있는데, 그 경우 재시도는
          이미 없는 댓글에 요청을 반복하게 된다.

        조회에 실패한 게시글은 "확인하지 못했다" 로 따로 보고한다. 그래야
        "확인했더니 깨끗함" 과 구별할 수 있다.
        """
        if self._dry_run or not (deleted_ids or failed_ids):
            return VerifyResult()

        target = set(deleted_ids)
        failed_target = set(failed_ids) - target
        still_alive: set[int] = set()
        seen_alive: set[int] = set()
        unverified: list[int] = []

        for entry_id in entry_ids:
            try:
                live_ids = await self._fetch_live_comment_ids(entry_id)
            except TistoryApiError as exc:
                logger.warning("검증 조회 실패 entry=%s: %s", entry_id, exc.message)
                unverified.append(entry_id)
                continue
            still_alive.update(target & live_ids)
            seen_alive.update(live_ids)

        # 조회에 성공한 게시글이 하나도 없으면 "안 보인다" 는 판단을 신뢰할 수 없다.
        verified_any = len(unverified) < len(entry_ids)
        actually_deleted = (
            sorted(failed_target - seen_alive) if verified_any and failed_target else []
        )
        if actually_deleted:
            logger.info(
                "실패로 기록됐으나 실제로는 지워진 댓글 %d건을 정정합니다.",
                len(actually_deleted),
            )
            reason = "재조회 결과 실제로는 삭제되어 있어 성공으로 정정했습니다."
            async with self._database.session() as session:
                await CommentRepository(session).mark_status(
                    actually_deleted, CommentStatus.DELETED
                )
                jobs = JobRepository(session)
                for comment_id in actually_deleted:
                    await jobs.update_item(
                        job_id,
                        comment_id,
                        status=JobItemStatus.SUCCEEDED,
                        message=reason,
                    )

        if still_alive:
            logger.warning("삭제 후에도 남아 있는 댓글 %d건을 발견했습니다.", len(still_alive))
            remaining = sorted(still_alive)
            reason = "삭제 요청은 성공했으나 재조회에서 여전히 확인되었습니다."
            async with self._database.session() as session:
                await CommentRepository(session).mark_status(
                    remaining, CommentStatus.FAILED, error=reason
                )
                # 작업 항목도 실패로 되돌려야 "실패 항목만 재시도" 가 이들을 찾아낸다.
                jobs = JobRepository(session)
                for comment_id in remaining:
                    await jobs.update_item(
                        job_id,
                        comment_id,
                        status=JobItemStatus.FAILED,
                        message=reason,
                    )
        return VerifyResult(
            still_alive=sorted(still_alive),
            unverified_entries=unverified,
            actually_deleted=actually_deleted,
        )

    async def _fetch_live_comment_ids(self, entry_id: int) -> set[int]:
        """게시글에 현재 남아 있는 댓글 번호 전체를 모은다."""
        live: set[int] = set()
        cursor: Optional[int] = None
        seen_cursors: set[int] = set()

        while True:
            page = await self._client.fetch_comment_page(entry_id, cursor)
            live.update(comment.comment_id for comment in page.comments)
            if not page.has_more:
                break
            # 수집과 같은 커서 규칙을 쓴다. 여기서 한 건이라도 놓치면 아직 살아 있는
            # 댓글을 "사라졌다" 고 판정해 삭제됨으로 표시하게 된다.
            next_cursor = next_comment_cursor(page, cursor)
            if next_cursor is None or next_cursor == cursor or next_cursor in seen_cursors:
                logger.warning("검증 중 커서가 진행하지 않아 중단합니다 entry=%s", entry_id)
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return live
