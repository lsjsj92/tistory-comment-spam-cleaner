# app/infrastructure/tistory/ratelimit.py
"""요청 속도 제어와 장애 차단 장치.

대상은 남의 서비스다. 초당 요청 수를 스스로 묶고, 연속 실패가 쌓이면 즉시 멈추는
것이 예의이자 계정 보호책이다. 여기서는 그 두 가지를 순수 asyncio 로 구현한다.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Optional

from ...domain.errors import CircuitOpenError
from ..logging_setup import get_logger

logger = get_logger(__name__)


class TokenBucket:
    """초당 발급량이 정해진 토큰 버킷.

    ``capacity`` 만큼의 순간 버스트를 허용하되 장기 평균은 ``rate`` 를 넘지 않는다.
    :meth:`acquire` 는 토큰이 없으면 필요한 시간만큼 대기한다.
    """

    def __init__(self, rate: float, *, capacity: Optional[float] = None) -> None:
        if rate <= 0:
            raise ValueError("rate 는 0보다 커야 합니다.")
        self._rate = rate
        self._capacity = capacity if capacity is not None else max(rate, 1.0)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    async def acquire(self, amount: float = 1.0) -> None:
        """토큰 ``amount`` 개를 얻을 때까지 대기한다."""
        if amount > self._capacity:
            raise ValueError("요청량이 버킷 용량보다 큽니다.")
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= amount:
                    self._tokens -= amount
                    return
                deficit = amount - self._tokens
                wait_for = deficit / self._rate
            await asyncio.sleep(wait_for)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed <= 0:
            return
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now


@dataclass
class BackoffPolicy:
    """지수 백오프 대기 시간 계산기.

    지터를 섞어 여러 워커가 같은 순간에 재시도해 부하가 겹치는 것을 막는다.
    """

    base: float = 0.5
    maximum: float = 30.0
    factor: float = 2.0

    def delay_for(self, attempt: int, *, retry_after: Optional[float] = None) -> float:
        """``attempt`` 번째 재시도의 대기 시간(초).

        Args:
            attempt: 1부터 시작하는 재시도 회차.
            retry_after: 서버가 알려준 대기 시간. 있으면 이 값을 우선한다.
        """
        if retry_after is not None and retry_after > 0:
            return min(retry_after, self.maximum)
        exponential = self.base * (self.factor ** max(attempt - 1, 0))
        capped = min(exponential, self.maximum)
        # full jitter: 0 에서 capped 사이 균등 분포
        return random.uniform(0, capped)


class CircuitBreaker:
    """연속 실패가 임계치에 도달하면 요청을 차단하는 스위치.

    티스토리가 요청을 거부하기 시작했는데도 수천 건을 계속 두드리면 계정이 위험해진다.
    임계치를 넘으면 열린 상태가 되고, 사용자가 원인을 해결한 뒤 :meth:`reset` 으로 닫는다.
    """

    def __init__(self, threshold: int, *, name: str = "default") -> None:
        if threshold < 1:
            raise ValueError("threshold 는 1 이상이어야 합니다.")
        self._threshold = threshold
        self._name = name
        self._consecutive_failures = 0
        self._open = False
        self._last_reason = ""
        self._lock = asyncio.Lock()

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    @property
    def last_reason(self) -> str:
        return self._last_reason

    @property
    def threshold(self) -> int:
        return self._threshold

    async def ensure_closed(self) -> None:
        """열려 있으면 예외를 던진다. 요청 직전에 호출한다."""
        if self._open:
            raise CircuitOpenError(
                f"연속 실패 {self._consecutive_failures}회로 작업을 중단했습니다. "
                f"마지막 원인: {self._last_reason or '알 수 없음'}"
            )

    async def record_success(self) -> None:
        async with self._lock:
            self._consecutive_failures = 0

    async def record_failure(self, reason: str = "") -> bool:
        """실패를 기록하고 회로가 열렸는지 여부를 돌려준다."""
        async with self._lock:
            self._consecutive_failures += 1
            self._last_reason = reason
            if self._consecutive_failures >= self._threshold and not self._open:
                self._open = True
                logger.error(
                    "서킷 브레이커 개방 [%s]: 연속 실패 %d회, 원인=%s",
                    self._name,
                    self._consecutive_failures,
                    reason,
                )
            return self._open

    async def reset(self) -> None:
        """회로를 닫고 실패 카운터를 초기화한다."""
        async with self._lock:
            self._open = False
            self._consecutive_failures = 0
            self._last_reason = ""
