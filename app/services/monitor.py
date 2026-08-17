# app/services/monitor.py
"""주기적 재수집 모니터.

한 번 정리했다고 끝이 아니다. 같은 공격이 재발하면 다시 수천 건이 쌓이므로,
활성화된 게시글을 일정 주기로 다시 수집하고 새로 들어온 스팸을 알린다.
삭제는 절대 자동으로 하지 않는다. 판단은 사람이 한다.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from ..config.settings import Settings
from ..domain.enums import CommentStatus, JobStatus, SpamLevel
from ..domain.models import CommentFilter
from ..infrastructure.db.repositories import AuditRepository, CommentRepository
from ..infrastructure.db.session import Database
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.timeutils import format_local, utc_now
from .jobs import JobManager

logger = get_logger(__name__)

# 기동 직후 바로 돌지 않고 잠시 기다린다. 초기화가 끝나기 전에 트래픽을 만들지 않기 위함이다.
_INITIAL_DELAY = 30.0

# 수집 작업이 이미 돌고 있어 건너뛴 경우 다음 시도까지의 짧은 대기(초)
_RETRY_DELAY = 60.0


class MonitorService:
    """설정한 주기로 수집 작업을 예약하는 백그라운드 루프."""

    def __init__(
        self, *, database: Database, settings: Settings, job_manager: JobManager
    ) -> None:
        self._database = database
        self._settings = settings
        self._jobs = job_manager
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """모니터를 시작한다. 설정이 꺼져 있으면 아무 일도 하지 않는다."""
        if not self._settings.monitor_enabled:
            logger.info("주기 모니터링이 비활성화되어 있습니다.")
            return
        if self.is_running:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="monitor-loop")
        logger.info(
            "주기 모니터링을 시작합니다. 주기 %d분", self._settings.monitor_interval_minutes
        )

    async def stop(self) -> None:
        """모니터를 멈춘다."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        """주기마다 한 번씩 재수집을 시도한다."""
        interval = self._settings.monitor_interval_minutes * 60
        await self._sleep_or_stop(_INITIAL_DELAY)

        while not self._stop.is_set():
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 한 번 실패해도 루프는 계속 돈다.
                logger.exception("모니터링 주기 실행 중 오류가 발생했습니다.")
                await self._sleep_or_stop(_RETRY_DELAY)
                continue
            await self._sleep_or_stop(interval)

    async def _run_cycle(self) -> None:
        """활성 게시글을 재수집하고 새로 늘어난 스팸 건수를 기록한다."""
        before = await self._count_spam()
        try:
            job_id = await self._jobs.create_collect_job(None)
        except Exception as exc:  # noqa: BLE001 - 다른 작업이 실행 중이면 다음 주기로 미룬다.
            logger.info("모니터링 수집을 건너뜁니다: %s", exc)
            return

        await self._wait_for_job(job_id)
        after = await self._count_spam()
        increased = after - before
        if increased <= 0:
            logger.info("모니터링 결과 새로운 스팸 댓글이 없습니다.")
            return

        message = (
            f"{format_local(utc_now(), tz_name=self._settings.timezone)} 기준 "
            f"새로운 스팸 의심 댓글 {increased}건이 확인되었습니다."
        )
        logger.warning(message)
        async with self._database.session() as session:
            await AuditRepository(session).log(
                "monitor_detected_spam",
                target="monitor",
                detail={"increased": increased, "total_spam": after, "job_id": job_id},
            )

    async def _count_spam(self) -> int:
        """현재 남아 있는 스팸/의심 댓글 수."""
        async with self._database.session() as session:
            return await CommentRepository(session).count(
                CommentFilter(
                    statuses=(CommentStatus.ACTIVE,),
                    spam_levels=(SpamLevel.SPAM, SpamLevel.SUSPICIOUS),
                )
            )

    async def _wait_for_job(self, job_id: int) -> None:
        """수집 작업이 끝날 때까지 진행률 스트림을 따라간다.

        중간에 빠져나갈 때 제너레이터를 명시적으로 닫는다. 그러지 않으면 구독이
        가비지 컬렉션 시점까지 허브에 남는다.
        """
        stream = self._jobs.stream(job_id)
        try:
            async for progress in stream:
                if self._stop.is_set():
                    return
                if JobStatus(progress.status).is_terminal:
                    return
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    async def _sleep_or_stop(self, seconds: float) -> None:
        """중지 신호가 오면 즉시 깨어나는 대기."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
