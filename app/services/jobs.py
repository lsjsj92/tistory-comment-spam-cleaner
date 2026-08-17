# app/services/jobs.py
"""백그라운드 작업 오케스트레이션.

수집, 탐색, 삭제를 같은 수명 주기로 다룬다. 작업 상태는 모두 DB 에 있으므로
프로세스가 죽었다 살아나도 중단 지점부터 이어서 실행할 수 있다.

진행률은 메모리 기반 발행/구독 허브로 흘려 SSE 로 화면에 전달한다.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any, Optional

from ..config.settings import Settings
from ..domain.enums import (
    CommentStatus,
    JobItemStatus,
    JobStatus,
    JobType,
    SpamLevel,
)
from ..domain.errors import (
    AuthenticationError,
    CircuitOpenError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from ..domain.models import CommentFilter, JobProgress, TargetSpec
from ..infrastructure.db.repositories import (
    AuditRepository,
    CommentRepository,
    JobRepository,
    TargetRepository,
)
from ..infrastructure.db.session import Database
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.timeutils import utc_now
from ..infrastructure.tistory.ratelimit import CircuitBreaker
from .backup import BackupService
from .collector import CollectorService
from .deleter import DeleteSummary, DeletionService
from .discovery import DiscoveryService
from .session_manager import SessionManager
from .spam_rules import SpamScoringService

logger = get_logger(__name__)

# 구독자 큐가 이 크기를 넘으면 오래된 진행률을 버린다. 느린 브라우저가 서버를 막지 않게 한다.
_SUBSCRIBER_QUEUE_SIZE = 32

# 전체 작업을 구독하는 가상 채널 번호
GLOBAL_CHANNEL = 0

# 종료 시 실행 중인 작업이 스스로 정리될 때까지 기다리는 시간(초)
_SHUTDOWN_GRACE = 5.0

# 작업 항목을 나눠 읽는 크기. 건수 상한 없이 전부 훑기 위한 페이지 단위다.
_ITEM_PAGE_SIZE = 1000


class ProgressHub:
    """작업 진행률 발행/구독 허브."""

    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = {}
        self._latest: dict[int, JobProgress] = {}

    def subscribe(self, job_id: int) -> asyncio.Queue:
        """구독 큐를 만든다. 사용이 끝나면 :meth:`unsubscribe` 로 반드시 정리한다."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        self._subscribers.setdefault(job_id, set()).add(queue)
        return queue

    def unsubscribe(self, job_id: int, queue: asyncio.Queue) -> None:
        listeners = self._subscribers.get(job_id)
        if not listeners:
            return
        listeners.discard(queue)
        if not listeners:
            self._subscribers.pop(job_id, None)

    def latest(self, job_id: int) -> Optional[JobProgress]:
        """마지막 진행 상황. 구독 직후 현재 상태를 즉시 보여주는 데 쓴다."""
        return self._latest.get(job_id)

    async def publish(self, progress: JobProgress) -> None:
        """해당 작업 채널과 전체 채널에 진행률을 뿌린다."""
        self._latest[progress.job_id] = progress
        for channel in (progress.job_id, GLOBAL_CHANNEL):
            for queue in list(self._subscribers.get(channel, ())):
                if queue.full():
                    # 밀린 구독자는 가장 오래된 항목을 버려 최신 상태를 우선한다.
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    queue.put_nowait(progress)

    def forget(self, job_id: int) -> None:
        """종료된 작업의 캐시를 정리한다."""
        self._latest.pop(job_id, None)


class JobManager:
    """작업 생성, 실행, 취소, 재개를 담당한다."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        session_manager: SessionManager,
    ) -> None:
        self._database = database
        self._settings = settings
        self._sessions = session_manager
        self._hub = ProgressHub()
        self._tasks: dict[int, asyncio.Task] = {}
        self._cancels: dict[int, asyncio.Event] = {}
        self._breakers: dict[int, CircuitBreaker] = {}
        # 예외로 죽은 작업을 실패 상태로 마무리하는 태스크. 종료 시 함께 기다린다.
        self._finalizers: set = set()
        # 작업 생성은 "실행 중인지 확인" 과 "작업 레코드 생성" 사이에 백업 생성 같은
        # 긴 작업이 끼어 있다. 그 틈에 두 번째 요청이 검사를 통과하지 못하게 직렬화한다.
        self._creation_lock = asyncio.Lock()

    @property
    def hub(self) -> ProgressHub:
        return self._hub

    # ------------------------------------------------------------------
    # 수명 주기
    # ------------------------------------------------------------------
    async def recover_on_startup(self) -> int:
        """비정상 종료로 남은 상태를 정리한다.

        실행 중이던 작업은 일시정지로 바꿔 사용자가 재개할 수 있게 하고,
        ``deleting`` 으로 멈춰 있는 댓글은 활성으로 되돌린다.
        """
        async with self._database.session() as session:
            jobs = JobRepository(session)
            stuck = await jobs.list_by_status([JobStatus.RUNNING])
            for job in stuck:
                await jobs.update_fields(
                    job.id,
                    status=JobStatus.PAUSED.value,
                    message="서버가 재시작되어 일시정지되었습니다. 재개할 수 있습니다.",
                )
            reverted = await CommentRepository(session).reset_stale_deleting()
        if stuck or reverted:
            logger.info("복구: 작업 %d건 일시정지, 댓글 상태 %d건 정정", len(stuck), reverted)
        return len(stuck)

    async def shutdown(self) -> None:
        """진행 중인 작업에 취소를 알리고 정리될 때까지 잠시 기다린다."""
        for event in self._cancels.values():
            event.set()
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            logger.info("작업 %d건 종료를 기다리는 중입니다.", len(tasks))
            done, pending = await asyncio.wait(tasks, timeout=_SHUTDOWN_GRACE)
            for task in pending:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*pending, return_exceptions=True)

        # 실패 마무리 태스크가 아직 돌고 있으면 기다린다. 놓치면 작업이 실행 중인
        # 상태로 남아 다음 기동까지 새 작업을 막는다.
        finalizers = [task for task in self._finalizers if not task.done()]
        if finalizers:
            await asyncio.wait(finalizers, timeout=_SHUTDOWN_GRACE)

        async with self._database.session() as session:
            jobs = JobRepository(session)
            for job in await jobs.list_by_status([JobStatus.RUNNING]):
                await jobs.update_fields(
                    job.id,
                    status=JobStatus.PAUSED.value,
                    message="서버 종료로 일시정지되었습니다.",
                )

    # ------------------------------------------------------------------
    # 조회
    # ------------------------------------------------------------------
    async def get_progress(self, job_id: int) -> JobProgress:
        """DB 기준 최신 진행 상황."""
        async with self._database.session() as session:
            job = await JobRepository(session).get(job_id)
            if job is None:
                raise NotFoundError(f"작업 {job_id} 을(를) 찾을 수 없습니다.")
            return _to_progress(job)

    async def stream(self, job_id: int) -> AsyncIterator[JobProgress]:
        """SSE 용 진행률 스트림.

        구독 직후 현재 상태를 한 번 내보내고, 이후 갱신을 계속 흘린다.
        작업이 종료 상태가 되면 스트림을 닫는다.
        """
        queue = self._hub.subscribe(job_id)
        try:
            if job_id != GLOBAL_CHANNEL:
                yield await self.get_progress(job_id)
            while True:
                progress = await queue.get()
                yield progress
                if job_id != GLOBAL_CHANNEL and JobStatus(progress.status).is_terminal:
                    return
        finally:
            self._hub.unsubscribe(job_id, queue)

    # ------------------------------------------------------------------
    # 작업 생성
    # ------------------------------------------------------------------
    async def create_discover_job(self) -> int:
        """sitemap 으로 전체 게시글을 찾는 작업."""
        await self._ensure_no_running(JobType.DISCOVER)
        async with self._database.session() as session:
            job = await JobRepository(session).create(
                job_type=JobType.DISCOVER, params={}, total=1, message="게시글 목록을 조회합니다."
            )
            job_id = job.id
        self._launch(job_id, self._run_discover(job_id))
        return job_id

    async def create_collect_job(
        self, entry_ids: Optional[Sequence[int]], *, since: Optional[datetime] = None
    ) -> int:
        """지정한 게시글의 댓글을 수집하는 작업."""
        await self._ensure_no_running(JobType.COLLECT)
        async with self._database.session() as session:
            targets = TargetRepository(session)
            if entry_ids:
                resolved = list(dict.fromkeys(int(entry_id) for entry_id in entry_ids))
            else:
                resolved = [item.entry_id for item in await targets.list_all(enabled_only=True)]
            if not resolved:
                raise ValidationError("수집할 게시글이 없습니다. 먼저 게시글을 등록하세요.")
            job = await JobRepository(session).create(
                job_type=JobType.COLLECT,
                params={
                    "entry_ids": resolved,
                    "since": since.isoformat() if since else None,
                },
                total=len(resolved),
                message="댓글을 수집합니다.",
            )
            job_id = job.id
        self._launch(job_id, self._run_collect(job_id, resolved, since))
        return job_id

    async def create_delete_job(
        self,
        *,
        comment_ids: Optional[Sequence[int]] = None,
        criteria: Optional[CommentFilter] = None,
        dry_run: Optional[bool] = None,
        rps: Optional[float] = None,
        concurrency: Optional[int] = None,
        verify_after: bool = True,
        allow_normal: bool = False,
        retry_of: Optional[int] = None,
    ) -> dict[str, Any]:
        """댓글 삭제 작업을 만들고 즉시 실행한다.

        대상 확정, 보호 대상 제외, 백업 생성까지 마친 뒤에야 작업을 시작한다.

        Args:
            allow_normal: 스팸으로 분류되지 않은 댓글까지 지울지 여부. 기본값은
                거부이며, 사용자가 화면에서 명시적으로 확인해야 True 가 된다.

        Returns:
            ``job_id``, ``total``, ``backup``, ``dry_run`` 을 담은 딕셔너리.
        """
        if (comment_ids is None) == (criteria is None):
            raise ValidationError("comment_ids 와 filter 중 정확히 하나만 지정해야 합니다.")

        # 확인부터 작업 레코드 생성까지를 한 덩어리로 묶는다. 그 사이에 백업 생성이
        # 들어가므로, 잠그지 않으면 동시 요청 두 개가 모두 검사를 통과한다.
        async with self._creation_lock:
            return await self._create_delete_job_locked(
                comment_ids=comment_ids,
                criteria=criteria,
                dry_run=dry_run,
                rps=rps,
                concurrency=concurrency,
                verify_after=verify_after,
                allow_normal=allow_normal,
                retry_of=retry_of,
            )

    async def _create_delete_job_locked(
        self,
        *,
        comment_ids: Optional[Sequence[int]],
        criteria: Optional[CommentFilter],
        dry_run: Optional[bool],
        rps: Optional[float],
        concurrency: Optional[int],
        verify_after: bool,
        allow_normal: bool,
        retry_of: Optional[int] = None,
    ) -> dict[str, Any]:
        """생성 잠금 안에서 실행되는 본체."""
        effective_dry_run = self._settings.delete_dry_run if dry_run is None else dry_run
        await self._ensure_no_running(JobType.DELETE)

        if not effective_dry_run:
            diagnosis = await self._sessions.cached_diagnosis()
            if not diagnosis.can_delete:
                raise AuthenticationError(
                    "소유자 세션이 확인되지 않아 삭제를 시작할 수 없습니다. "
                    "설정 화면에서 쿠키를 등록하고 진단을 통과하세요."
                )

        if criteria is not None and criteria.is_empty():
            raise ValidationError(
                "조건을 하나도 지정하지 않으면 수집된 모든 댓글이 대상이 됩니다. "
                "게시글, 기간, 스팸 등급 중 최소 하나는 지정하세요."
            )

        targets, entry_ids, normal_ids = await self._resolve_delete_targets(
            comment_ids, criteria
        )
        if not targets:
            raise ValidationError("삭제 대상이 없습니다. 조건을 확인하세요.")

        if normal_ids and not allow_normal:
            # 스팸으로 확정되지 않은 댓글이 섞였다. 도배 정리 중 진짜 독자 댓글이
            # 딸려 들어가는 사고가 가장 흔하므로, 한 번 더 확인받고 진행한다.
            preview = ", ".join(str(value) for value in normal_ids[:5])
            raise ValidationError(
                f"삭제 대상에 스팸으로 분류되지 않은 댓글 {len(normal_ids)}건이 섞여 있습니다. "
                f"(예: {preview}) 정상 댓글이 맞는지 확인한 뒤, 그래도 삭제하려면 "
                "정상 등급 포함 옵션을 켜고 다시 실행하세요."
            )

        backup_info: Optional[dict[str, Any]] = None
        if not effective_dry_run and not self._settings.backup_before_delete:
            logger.warning(
                "APP_BACKUP_BEFORE_DELETE 가 꺼져 있어 백업 없이 %d건을 삭제합니다.", len(targets)
            )
        if self._settings.backup_before_delete and not effective_dry_run:
            backup = await BackupService(
                database=self._database,
                backup_dir=self._settings.backup_path,
                tz_name=self._settings.timezone,
            ).export(targets, label="delete")
            backup_info = {
                "json": str(backup.json_path),
                "csv": str(backup.csv_path),
                "count": backup.count,
            }

        params = {
            "dry_run": effective_dry_run,
            "rps": rps or self._settings.delete_rps,
            "concurrency": concurrency or self._settings.delete_concurrency,
            "verify_after": verify_after,
            "entry_ids": entry_ids,
            "allow_normal": allow_normal,
        }

        async with self._database.session() as session:
            jobs = JobRepository(session)
            job = await jobs.create(
                job_type=JobType.DELETE,
                params=params,
                total=len(targets),
                message="드라이런 실행" if effective_dry_run else "삭제를 준비합니다.",
            )
            job_id = job.id
            await jobs.add_items(job_id, targets)
            if backup_info:
                await jobs.update_fields(job_id, backup_path=backup_info["json"])
            await AuditRepository(session).log(
                "delete_job_created",
                target=f"job:{job_id}",
                detail={
                    "count": len(targets),
                    "dry_run": effective_dry_run,
                    "entry_ids": entry_ids,
                    "backup": backup_info,
                    # 스팸 외 등급까지 승인한 작업인지 기록에 남긴다. 되돌릴 수 없는
                    # 조작이므로 "누가 무엇을 승인했는지" 가 감사 로그에 있어야 한다.
                    "allow_normal": allow_normal,
                    "normal_included": len(normal_ids),
                    "retry_of": retry_of,
                },
            )

        self._launch(job_id, self._run_delete(job_id, params))
        return {
            "job_id": job_id,
            "total": len(targets),
            "backup": backup_info,
            "dry_run": effective_dry_run,
        }

    async def _resolve_delete_targets(
        self, comment_ids: Optional[Sequence[int]], criteria: Optional[CommentFilter]
    ) -> tuple[list[int], list[int], list[int]]:
        """삭제 대상 ID 를 확정한다. 보호 대상은 여기서 전부 걸러낸다.

        Returns:
            (삭제할 댓글 번호 목록, 관련 게시글 번호 목록,
             그중 스팸으로 분류되지 않은 댓글 번호 목록)
        """
        async with self._database.session() as session:
            repo = CommentRepository(session)
            if criteria is not None:
                candidate_ids = await repo.ids_for(criteria)
            else:
                candidate_ids = list(dict.fromkeys(int(value) for value in comment_ids or ()))

            rows = await repo.get_many(candidate_ids)

        allowed: list[int] = []
        entry_ids: set[int] = set()
        normal: list[int] = []
        for row in rows:
            # 운영자 본인 댓글과 화이트리스트는 어떤 경로로도 삭제하지 않는다.
            if row.whitelisted or row.is_admin:
                continue
            if row.status == CommentStatus.DELETED.value:
                continue
            allowed.append(row.comment_id)
            entry_ids.add(row.entry_id)
            # 스팸으로 확정된 것 외에는 모두 확인 대상이다. 의심 등급도 사람이
            # 한 번 봐야 한다. 임계값을 조정하면 정상이 의심으로 옮겨갈 뿐이므로,
            # "정상만" 막으면 그 순간 보호가 조용히 풀린다.
            if row.spam_level != SpamLevel.SPAM.value:
                normal.append(row.comment_id)
        return allowed, sorted(entry_ids), normal

    # ------------------------------------------------------------------
    # 작업 제어
    # ------------------------------------------------------------------
    async def cancel(self, job_id: int) -> None:
        """진행 중인 작업에 취소를 요청한다."""
        event = self._cancels.get(job_id)
        if event is None:
            raise ConflictError(f"작업 {job_id} 은(는) 실행 중이 아닙니다.")
        event.set()
        logger.info("작업 %d 취소를 요청했습니다.", job_id)

    async def resume(self, job_id: int) -> int:
        """일시정지된 삭제 작업을 남은 항목부터 이어서 실행한다.

        새 작업 생성과 같은 잠금을 쓴다. 재개와 신규 삭제가 동시에 통과하면
        설정한 초당 요청 수의 두 배가 티스토리로 나가고 같은 댓글에 중복 요청이 간다.
        """
        async with self._creation_lock:
            async with self._database.session() as session:
                job = await JobRepository(session).get(job_id)
                if job is None:
                    raise NotFoundError(f"작업 {job_id} 을(를) 찾을 수 없습니다.")
                if not JobStatus(job.status).is_resumable:
                    raise ConflictError(
                        f"작업 {job_id} 은(는) {job.status} 상태라 재개할 수 없습니다."
                    )
                job_type = JobType(job.type)
                params = dict(job.params)

            if job_type is not JobType.DELETE:
                raise ConflictError("삭제 작업만 재개할 수 있습니다.")
            if job_id in self._tasks and not self._tasks[job_id].done():
                raise ConflictError(f"작업 {job_id} 이(가) 이미 실행 중입니다.")
            await self._ensure_no_running(JobType.DELETE)

            # 상태를 먼저 RUNNING 으로 올려 두어야, 잠금을 놓은 직후 들어온 다른
            # 요청이 이 작업을 보고 물러난다. 태스크는 그 뒤에 띄운다.
            await self._mark_running(job_id, "남은 항목부터 이어서 삭제합니다.")
            self._launch(job_id, self._run_delete(job_id, params))
        return job_id

    async def retry_failed(self, job_id: int) -> dict[str, Any]:
        """실패한 항목만 모아 새 삭제 작업을 만든다."""
        async with self._database.session() as session:
            job = await JobRepository(session).get(job_id)
            if job is None:
                raise NotFoundError(f"작업 {job_id} 을(를) 찾을 수 없습니다.")
            failed_ids = await JobRepository(session).failed_item_ids(job_id)
        if not failed_ids:
            raise ValidationError("재시도할 실패 항목이 없습니다.")

        params = dict(job.params)
        return await self.create_delete_job(
            comment_ids=failed_ids,
            dry_run=bool(params.get("dry_run", False)),
            rps=params.get("rps"),
            concurrency=params.get("concurrency"),
            verify_after=bool(params.get("verify_after", True)),
            # 원래 작업에서 이미 확인받은 범위다. 재시도할 때 같은 확인을 다시
            # 요구하면 실패 항목을 영영 처리할 수 없게 된다.
            allow_normal=bool(params.get("allow_normal", False)),
            retry_of=job_id,
        )

    # ------------------------------------------------------------------
    # 실행 본체
    # ------------------------------------------------------------------
    def _launch(self, job_id: int, coro) -> None:
        """작업 코루틴을 태스크로 띄우고 취소 신호를 준비한다."""
        self._cancels[job_id] = asyncio.Event()
        task = asyncio.create_task(coro, name=f"job-{job_id}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda finished: self._on_task_done(job_id, finished))

    def _on_task_done(self, job_id: int, task: asyncio.Task) -> None:
        """작업 태스크가 끝났을 때의 뒷정리.

        태스크가 예외로 죽으면 아무도 결과를 읽지 않아 로그에 흔적이 남지 않고,
        DB 의 작업은 영원히 실행 중으로 남아 이후 모든 작업을 막는다. 여기서
        반드시 확인하고 실패 상태로 마무리한다.
        """
        self._cleanup(job_id, task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        logger.error("작업 %d 이(가) 처리되지 않은 예외로 종료했습니다.", job_id, exc_info=error)
        # 콜백은 동기 컨텍스트라 여기서 await 할 수 없다. 마무리를 별도 태스크로 넘기되
        # 추적 집합에 담아 종료 시 함께 기다린다. 그러지 않으면 루프가 먼저 멈출 때
        # 실패 기록이 유실되고 작업이 실행 중인 채로 남는다.
        finalizer = asyncio.create_task(
            self._finish(
                job_id,
                JobStatus.FAILED,
                message="작업이 예기치 못한 오류로 종료되었습니다.",
                error=str(error),
            ),
            name=f"job-{job_id}-finalize",
        )
        self._finalizers.add(finalizer)
        finalizer.add_done_callback(self._finalizers.discard)

    def _cleanup(self, job_id: int, task: Optional[asyncio.Task] = None) -> None:
        """작업 등록 정보를 지운다.

        같은 번호로 새 태스크가 이미 등록되었을 수 있으므로, 자기 자신일 때만 지운다.
        """
        if task is not None and self._tasks.get(job_id) is not task:
            return
        self._tasks.pop(job_id, None)
        self._cancels.pop(job_id, None)
        self._breakers.pop(job_id, None)
        self._hub.forget(job_id)

    async def _ensure_no_running(self, job_type: JobType) -> None:
        """같은 종류의 작업이 동시에 두 개 돌지 않게 막는다.

        동시에 실행하면 설정한 초당 요청 수를 넘겨 티스토리에 부담을 준다.
        아직 시작 전인 ``pending`` 도 함께 본다. 생성 직후 실행 전 상태를 놓치면
        같은 대상이 두 작업에 들어갈 수 있다.
        """
        async with self._database.session() as session:
            running = await JobRepository(session).list_by_status(
                [JobStatus.RUNNING, JobStatus.PENDING]
            )
        if any(job.type == job_type.value for job in running):
            raise ConflictError(
                f"이미 실행 중인 {job_type.value} 작업이 있습니다. 완료 후 다시 시도하세요."
            )

    async def _mark_running(self, job_id: int, message: str) -> None:
        async with self._database.session() as session:
            await JobRepository(session).update_fields(
                job_id,
                status=JobStatus.RUNNING.value,
                started_at=utc_now(),
                message=message,
                error=None,
            )
        await self._publish(job_id)

    async def _finish(
        self, job_id: int, status: JobStatus, *, message: str, error: Optional[str] = None
    ) -> None:
        async with self._database.session() as session:
            await JobRepository(session).update_fields(
                job_id,
                status=status.value,
                finished_at=utc_now() if status.is_terminal else None,
                message=message,
                error=error,
            )
        await self._publish(job_id)

    async def _publish(self, job_id: int) -> None:
        """DB 상태를 읽어 구독자에게 알린다."""
        async with self._database.session() as session:
            job = await JobRepository(session).get(job_id)
        if job is None:
            return
        progress = _to_progress(job)
        await self._hub.publish(progress)
        if JobStatus(progress.status).is_terminal:
            # 종료 상태를 알린 뒤 캐시를 비운다. `_cleanup` 이 먼저 실행되는 경로
            # (예외로 죽은 작업)에서도 잔여물이 남지 않게 하기 위함이다.
            self._hub.forget(job.id)

    async def _run_discover(self, job_id: int) -> None:
        try:
            # 상태 표시와 클라이언트 생성도 실패할 수 있다. try 밖에 두면 그 예외가
            # 아무 데도 기록되지 않고 작업이 영원히 실행 중으로 남는다.
            await self._mark_running(job_id, "sitemap 을 조회하는 중입니다.")
            client = await self._sessions.build_client(
                rps=self._settings.collect_rps, concurrency=1, with_cookies=False
            )
            async with client:
                specs = await DiscoveryService(
                    client=client, blog_url=self._settings.blog_url
                ).discover_from_sitemap()
            async with self._database.session() as session:
                added = await TargetRepository(session).upsert_many(specs, source="sitemap")
                await JobRepository(session).update_fields(
                    job_id, done=1, succeeded=1, total=1
                )
            await self._finish(
                job_id,
                JobStatus.COMPLETED,
                message=f"게시글 {len(specs)}건을 확인했고 그중 {added}건을 새로 등록했습니다.",
            )
        except Exception as exc:  # noqa: BLE001 - 작업 실패를 상태로 남긴다.
            logger.exception("게시글 탐색 작업 실패 job=%s", job_id)
            await self._finish(
                job_id, JobStatus.FAILED, message="게시글 탐색에 실패했습니다.", error=str(exc)
            )

    async def _run_collect(
        self, job_id: int, entry_ids: Sequence[int], since: Optional[datetime]
    ) -> None:
        cancel_event = self._cancels[job_id]
        done = 0
        succeeded = 0
        failed = 0

        async def on_entry_done(result) -> None:
            nonlocal done, succeeded, failed
            done += 1
            if result.succeeded:
                succeeded += 1
            else:
                failed += 1
            async with self._database.session() as session:
                await JobRepository(session).update_fields(
                    job_id, done=done, succeeded=succeeded, failed=failed
                )
            await self._publish(job_id)

        try:
            await self._mark_running(job_id, "댓글을 수집하는 중입니다.")
            client = await self._sessions.build_client(
                rps=self._settings.collect_rps,
                concurrency=self._settings.collect_concurrency,
                with_cookies=True,
            )
            async with client:
                client.bind_stop_event(cancel_event)
                await self._fill_missing_titles(client, entry_ids)
                collector = CollectorService(
                    client=client, database=self._database, tz_name=self._settings.timezone
                )
                results = await collector.collect_many(
                    entry_ids,
                    since=since,
                    concurrency=self._settings.collect_concurrency,
                    on_entry_done=on_entry_done,
                    cancel_event=cancel_event,
                )

            fetched = sum(result.fetched for result in results)
            # 재채점은 항상 전체를 대상으로 한다. "같은 닉네임 반복", "분당 폭주" 규칙은
            # 전체 집계를 봐야 정확하므로 게시글을 한정하면 점수가 달라진다.
            updated = await SpamScoringService(
                database=self._database,
                rules_path=self._settings.rules_file,
                tz_name=self._settings.timezone,
            ).rescore(None)

            if cancel_event.is_set():
                await self._finish(
                    job_id, JobStatus.CANCELLED, message=f"사용자 취소. {fetched}건까지 저장했습니다."
                )
                return
            await self._finish(
                job_id,
                JobStatus.COMPLETED,
                message=f"댓글 {fetched}건을 수집하고 {updated}건의 스팸 점수를 갱신했습니다.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("수집 작업 실패 job=%s", job_id)
            await self._finish(
                job_id, JobStatus.FAILED, message="댓글 수집에 실패했습니다.", error=str(exc)
            )

    async def _run_delete(self, job_id: int, params: dict[str, Any]) -> None:
        dry_run = bool(params.get("dry_run", True))
        cancel_event = self._cancels[job_id]

        try:
            await self._mark_running(
                job_id,
                "드라이런을 실행하는 중입니다." if dry_run else "댓글을 삭제하는 중입니다.",
            )

            breaker = CircuitBreaker(
                self._settings.circuit_breaker_threshold, name=f"delete-{job_id}"
            )
            self._breakers[job_id] = breaker

            concurrency = int(params.get("concurrency") or self._settings.delete_concurrency)
            rps = float(params.get("rps") or self._settings.delete_rps)

            allow_normal = bool(params.get("allow_normal", False))
            pending, skipped = await self._filter_protected(job_id, allow_normal=allow_normal)

            if not pending:
                # 조기 종료도 최종 집계를 거쳐야 한다. 그러지 않으면 항목은
                # 건너뜀으로 남는데 작업 요약만 옛 수치인 채 "완료" 가 된다.
                await self._finalize_delete(
                    job_id,
                    DeleteSummary(),
                    dry_run=dry_run,
                    note="",
                    skipped=skipped,
                )
                return

            async def on_progress(summary: DeleteSummary) -> None:
                await self._publish(job_id)

            # 클라이언트 생성을 try 안에서 하고 with 로 감싸야, 어느 경로로 빠져나가도
            # 커넥션 풀이 닫힌다.
            client = await self._sessions.build_client(
                rps=rps, concurrency=concurrency, circuit_breaker=breaker
            )
            async with client:
                # 재시도 대기 중에도 취소가 즉시 먹히도록 신호를 연결한다.
                client.bind_stop_event(cancel_event)
                service = DeletionService(
                    client=client,
                    database=self._database,
                    concurrency=concurrency,
                    dry_run=dry_run,
                )
                summary = await service.run(
                    job_id, pending, cancel_event=cancel_event, on_progress=on_progress
                )

                verified_note = ""
                # 성공 건수가 0이어도 검증한다. 응답 판정이 틀려 전부 실패로 기록됐지만
                # 실제로는 지워졌을 수 있고, 그 사실을 아는 방법은 재조회뿐이다.
                if params.get("verify_after", True) and not dry_run:
                    verified_note = await self._verify_after_delete(
                        job_id, service, params, summary
                    )

            await self._finalize_delete(
                job_id, summary, dry_run=dry_run, note=verified_note, skipped=skipped
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("삭제 작업 실패 job=%s", job_id)
            await self._finish(
                job_id, JobStatus.FAILED, message="삭제 작업이 중단되었습니다.", error=str(exc)
            )

    async def _verify_after_delete(
        self,
        job_id: int,
        service: DeletionService,
        params: dict,
        summary: DeleteSummary,
    ) -> str:
        """삭제 후 재조회로 실제 상태를 확인하고 사람이 읽을 요약을 돌려준다.

        검증은 부가 절차다. 여기서 실패해도 이미 기록된 삭제 결과를 잃어서는 안 되므로
        예외를 밖으로 내보내지 않는다. 예외가 새어 나가면 작업이 통째로 ``failed`` 가
        되어, 남은 항목을 재개할 수 있다는 사실이 사용자에게 전달되지 않는다.

        서킷이 열린 상태에서는 조회 자체가 차단되므로 시도하지 않고 그 사실만 남긴다.
        """
        if summary.circuit_open:
            return (
                " 연속 실패로 중단되어 사후 검증은 건너뛰었습니다."
                " 원인을 해결한 뒤 재개하면 남은 항목과 함께 다시 확인합니다."
            )

        entry_ids = [int(value) for value in params.get("entry_ids", [])]
        deleted_ids = await self._item_ids(job_id, JobItemStatus.SUCCEEDED)
        failed_ids = await self._item_ids(job_id, JobItemStatus.FAILED)
        try:
            result = await service.verify(job_id, entry_ids, deleted_ids, failed_ids)
        except CircuitOpenError as exc:
            logger.warning("서킷이 열려 사후 검증을 건너뜁니다 job=%s: %s", job_id, exc)
            return " 요청이 차단되어 사후 검증을 하지 못했습니다."
        except Exception as exc:  # noqa: BLE001 - 검증 실패가 삭제 결과를 지워서는 안 된다.
            logger.exception("사후 검증 실패 job=%s", job_id)
            return f" 사후 검증에 실패했습니다: {exc}"

        note = ""
        if result.actually_deleted:
            note += (
                f" 실패로 기록됐던 {len(result.actually_deleted)}건은"
                " 재조회 결과 이미 삭제되어 있어 성공으로 정정했습니다."
            )
        if result.still_alive:
            note += (
                f" 재조회에서 {len(result.still_alive)}건이 남아 있어 실패로 표시했습니다."
            )
        if result.unverified_entries:
            note += (
                f" 게시글 {len(result.unverified_entries)}개는 조회에 실패해"
                " 확인하지 못했습니다."
            )
        return note

    async def _fill_missing_titles(self, client, entry_ids: Sequence[int]) -> None:
        """제목이 비어 있는 대상의 제목을 채운다.

        설정 파일로 등록한 게시글은 번호만 있어 화면에 "게시글 723" 으로만 보인다.
        수집할 때 한 번씩만 가져오면 이후로는 제목으로 구분할 수 있다.
        실패해도 수집 자체를 막지 않는다.
        """
        async with self._database.session() as session:
            rows = await TargetRepository(session).list_all()
        missing = [row for row in rows if row.entry_id in set(entry_ids) and not row.title]
        if not missing:
            return

        discovery = DiscoveryService(client=client, blog_url=self._settings.blog_url)
        resolved: list[TargetSpec] = []
        for row in missing:
            title = await discovery.fetch_title(row.entry_id)
            if title:
                resolved.append(TargetSpec(entry_id=row.entry_id, url=row.url, title=title))

        if resolved:
            async with self._database.session() as session:
                repo = TargetRepository(session)
                for spec in resolved:
                    await repo.upsert(spec, source="manual")
            logger.info("게시글 제목 %d건을 채웠습니다.", len(resolved))

    async def _filter_protected(
        self, job_id: int, *, allow_normal: bool = False
    ) -> tuple[list[int], int]:
        """실행 직전에 보호 대상을 한 번 더 걸러낸다.

        작업을 만든 뒤 재개하기까지 사이에 규칙 파일이 바뀌어 화이트리스트가 늘어날 수
        있고, 재분류로 등급이 바뀌었을 수도 있으며, 다른 작업이 먼저 지웠을 수도 있다.
        대상 확정 시점의 판단만 믿으면 그 변화가 무시되므로 실행 시점에 다시 확인한다.

        Args:
            allow_normal: 작업 생성 시 사용자가 스팸 외 등급까지 승인했는지 여부.
                승인하지 않았다면 그사이 등급이 내려간 댓글도 건너뛴다.

        Returns:
            (처리할 댓글 번호 목록, 건너뛴 건수)
        """
        async with self._database.session() as session:
            jobs = JobRepository(session)
            pending = await jobs.pending_item_ids(job_id)
            if not pending:
                return ([], 0)

            rows = await CommentRepository(session).get_many(pending)
            by_id = {row.comment_id: row for row in rows}

            allowed: list[int] = []
            for comment_id in pending:
                row = by_id.get(comment_id)
                if row is None:
                    reason = "댓글 기록을 찾을 수 없습니다."
                elif row.whitelisted or row.is_admin:
                    reason = "보호 대상이라 건너뛰었습니다."
                elif row.status == CommentStatus.DELETED.value:
                    reason = "이미 삭제된 댓글입니다."
                elif not allow_normal and row.spam_level != SpamLevel.SPAM.value:
                    # 승인받지 않은 등급이다. 일시정지 중 재분류로 스팸에서 내려온
                    # 댓글이 아무 확인 없이 지워지는 것을 막는다.
                    reason = "스팸으로 분류되지 않아 건너뛰었습니다. 확인 후 다시 실행하세요."
                else:
                    allowed.append(comment_id)
                    continue
                await jobs.update_item(
                    job_id, comment_id, status=JobItemStatus.SKIPPED, message=reason
                )

            skipped = len(pending) - len(allowed)
            if skipped:
                await jobs.update_fields(job_id, skipped=skipped)
                logger.info("작업 %d: 보호 대상 등 %d건을 건너뜁니다.", job_id, skipped)
            return (allowed, skipped)

    async def _item_ids(self, job_id: int, status: JobItemStatus) -> list[int]:
        """작업 항목 중 지정한 상태의 댓글 번호 전체.

        한 번에 다 읽지 않고 페이지로 나눈다. 상한을 걸어두면 그보다 많을 때
        조용히 잘려 검증이 불완전해진다.
        """
        collected: list[int] = []
        offset = 0
        async with self._database.session() as session:
            repo = JobRepository(session)
            while True:
                items = await repo.list_items(
                    job_id, limit=_ITEM_PAGE_SIZE, offset=offset, status=status
                )
                if not items:
                    return collected
                collected.extend(item.comment_id for item in items)
                offset += len(items)

    async def _finalize_delete(
        self,
        job_id: int,
        summary: DeleteSummary,
        *,
        dry_run: bool,
        note: str,
        skipped: int = 0,
    ) -> None:
        """삭제 종료 상태와 감사 로그를 남긴다.

        집계는 이번 실행분(``summary``)이 아니라 작업 항목 전체(``item_counts``)를 쓴다.
        재개로 여러 번 나눠 처리하면 실행분만으로는 최종 결과가 실제와 달라진다.
        """
        async with self._database.session() as session:
            jobs = JobRepository(session)
            counts = await jobs.item_counts(job_id)
            succeeded = counts.get(JobItemStatus.SUCCEEDED.value, 0)
            failed = counts.get(JobItemStatus.FAILED.value, 0)
            # 건너뜀은 `_filter_protected` 가 이미 커밋했으므로 집계가 항상 정확하다.
            # 인자로 받은 값을 대체로 쓰지 않는다. 둘이 어긋나는 날 조용히 덮어써서
            # 오히려 문제를 감추기 때문이다.
            skipped_total = counts.get(JobItemStatus.SKIPPED.value, 0)
            remaining = counts.get(JobItemStatus.PENDING.value, 0)

            await jobs.update_fields(
                job_id,
                done=succeeded + failed + skipped_total,
                succeeded=succeeded,
                failed=failed,
                skipped=skipped_total,
            )
            await AuditRepository(session).log(
                "delete_job_finished",
                target=f"job:{job_id}",
                detail={
                    "dry_run": dry_run,
                    "succeeded": succeeded,
                    "failed": failed,
                    "skipped": skipped_total,
                    "remaining": remaining,
                    "cancelled": summary.cancelled,
                    "circuit_open": summary.circuit_open,
                },
            )

        base = (
            f"성공 {succeeded}건, 실패 {failed}건"
            + (f", 건너뜀 {skipped_total}건" if skipped_total else "")
            + (f", 남은 항목 {remaining}건" if remaining else "")
            # 부모 댓글을 지우면 대댓글이 함께 사라진다. 그 건수를 알려주지 않으면
            # "요청도 안 보냈는데 왜 성공인가" 를 사용자가 이해할 수 없다.
            + (
                f" (이번 실행에서 {summary.already_gone}건은 이미 삭제되어 있었습니다)"
                if summary.already_gone
                else ""
            )
            + note
        )

        if summary.aborted:
            await self._finish(
                job_id,
                JobStatus.PAUSED,
                message=f"결과를 저장하지 못해 중단했습니다. {base}",
                error=summary.error,
            )
        elif summary.circuit_open:
            await self._finish(
                job_id,
                JobStatus.PAUSED,
                message=f"연속 실패로 자동 중단했습니다. {base}",
                error=summary.error,
            )
        elif summary.cancelled:
            await self._finish(job_id, JobStatus.CANCELLED, message=f"사용자 취소. {base}")
        elif remaining:
            await self._finish(
                job_id, JobStatus.PAUSED, message=f"일부 항목이 남아 일시정지했습니다. {base}"
            )
        else:
            prefix = "드라이런 완료" if dry_run else "삭제 완료"
            await self._finish(job_id, JobStatus.COMPLETED, message=f"{prefix}. {base}")


def _to_progress(job) -> JobProgress:
    """ORM Job 을 진행률 DTO 로 바꾼다."""
    return JobProgress(
        job_id=job.id,
        status=job.status,
        total=job.total,
        done=job.done,
        succeeded=job.succeeded,
        failed=job.failed,
        skipped=job.skipped,
        message=job.message,
        updated_at=job.updated_at,
    )
