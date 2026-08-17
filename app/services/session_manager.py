# app/services/session_manager.py
"""세션 쿠키 보관과 티스토리 클라이언트 생성.

쿠키는 블로그 계정을 조작할 수 있는 자격 증명이므로 암호화해 DB 에 넣고,
화면과 로그에는 이름만 노출한다. 클라이언트는 용도(수집/삭제)에 따라 속도 설정이
다르므로 이 클래스가 매번 새로 만들어 준다.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

from ..config.settings import Settings
from ..domain.enums import AuthState
from ..domain.models import AuthDiagnosis
from ..infrastructure.db.repositories import SettingsRepository
from ..infrastructure.db.session import Database
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.security.crypto import SecretBox
from ..infrastructure.timeutils import utc_now
from ..infrastructure.tistory.auth import (
    deserialize_cookies,
    load_cookies_from_browser,
    parse_cookie_input,
    serialize_cookies,
)
from ..infrastructure.tistory.client import TistoryClient
from ..infrastructure.tistory.ratelimit import BackoffPolicy, CircuitBreaker

logger = get_logger(__name__)

# app_settings 테이블에서 사용하는 키
COOKIE_KEY = "tistory.session_cookies"
DIAGNOSIS_KEY = "tistory.session_diagnosis"


class SessionManager:
    """세션 쿠키의 수명과 클라이언트 생성을 담당한다."""

    def __init__(self, database: Database, settings: Settings) -> None:
        self._database = database
        self._settings = settings
        self._box = SecretBox(settings.secret_key)

    # ------------------------------------------------------------------
    # 쿠키 보관
    # ------------------------------------------------------------------
    async def load_cookies(self) -> dict[str, str]:
        """저장된 쿠키를 복호화해 돌려준다. 없으면 빈 딕셔너리."""
        async with self._database.session() as session:
            stored = await SettingsRepository(session).get(COOKIE_KEY)
        plaintext = self._box.try_decrypt(stored)
        if stored and plaintext is None:
            logger.warning("저장된 쿠키를 복호화하지 못했습니다. APP_SECRET_KEY 변경 여부를 확인하세요.")
        return deserialize_cookies(plaintext)

    async def save_cookies(self, raw: str) -> AuthDiagnosis:
        """사용자가 붙여넣은 문자열에서 쿠키를 추출해 저장하고 즉시 진단한다."""
        cookies = parse_cookie_input(raw)
        await self._store(cookies)
        return await self.diagnose()

    async def save_cookies_from_browser(self) -> AuthDiagnosis:
        """설치된 브라우저에서 쿠키를 읽어 저장하고 즉시 진단한다."""
        cookies = load_cookies_from_browser(self._settings.blog_url)
        await self._store(cookies)
        return await self.diagnose()

    async def clear_cookies(self) -> None:
        """저장된 쿠키와 진단 결과를 지운다."""
        async with self._database.session() as session:
            repo = SettingsRepository(session)
            await repo.delete(COOKIE_KEY)
            await repo.delete(DIAGNOSIS_KEY)
        logger.info("저장된 세션 쿠키를 삭제했습니다.")

    async def _store(self, cookies: dict[str, str]) -> None:
        payload = self._box.encrypt(serialize_cookies(cookies))
        async with self._database.session() as session:
            await SettingsRepository(session).set(COOKIE_KEY, payload, is_secret=True)
        logger.info("세션 쿠키 %d개를 저장했습니다: %s", len(cookies), ", ".join(sorted(cookies)))

    # ------------------------------------------------------------------
    # 진단
    # ------------------------------------------------------------------
    async def diagnose(self) -> AuthDiagnosis:
        """쿠키로 소유자 권한이 유효한지 확인하고 결과를 캐시한다."""
        client = await self.build_client(rps=self._settings.collect_rps, concurrency=1)
        async with client:
            diagnosis = await client.diagnose_session()
        await self._cache_diagnosis(diagnosis)
        return diagnosis

    async def cached_diagnosis(self) -> AuthDiagnosis:
        """마지막 진단 결과. 없으면 아직 진단하지 않은 상태로 돌려준다."""
        async with self._database.session() as session:
            stored = await SettingsRepository(session).get(DIAGNOSIS_KEY)
        if not stored:
            return AuthDiagnosis(
                state=AuthState.UNKNOWN, message="아직 세션을 진단하지 않았습니다."
            )
        try:
            payload = json.loads(stored)
            checked_at_raw = payload.get("checked_at")
            return AuthDiagnosis(
                state=AuthState(payload["state"]),
                message=payload["message"],
                cookie_names=tuple(payload.get("cookie_names", ())),
                checked_at=datetime.fromisoformat(checked_at_raw) if checked_at_raw else None,
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return AuthDiagnosis(
                state=AuthState.UNKNOWN, message="저장된 진단 결과를 해석하지 못했습니다."
            )

    async def _cache_diagnosis(self, diagnosis: AuthDiagnosis) -> None:
        payload = json.dumps(
            {
                "state": diagnosis.state.value,
                "message": diagnosis.message,
                "cookie_names": list(diagnosis.cookie_names),
                "checked_at": (diagnosis.checked_at or utc_now()).isoformat(),
            },
            ensure_ascii=False,
        )
        async with self._database.session() as session:
            await SettingsRepository(session).set(DIAGNOSIS_KEY, payload)

    # ------------------------------------------------------------------
    # 클라이언트 생성
    # ------------------------------------------------------------------
    async def build_client(
        self,
        *,
        rps: float,
        concurrency: int,
        circuit_breaker: Optional[CircuitBreaker] = None,
        with_cookies: bool = True,
    ) -> TistoryClient:
        """설정과 저장된 쿠키로 클라이언트를 만든다.

        Args:
            rps: 초당 요청 상한.
            concurrency: 동시 요청 수.
            circuit_breaker: 연속 실패 차단기. 삭제 작업에서만 전달한다.
            with_cookies: False 면 인증 없이 만든다. 조회 전용 경로에 쓴다.
        """
        cookies = await self.load_cookies() if with_cookies else {}
        return TistoryClient(
            blog_url=self._settings.blog_url,
            user_agent=self._settings.user_agent,
            timeout=self._settings.http_timeout,
            max_retries=self._settings.http_max_retries,
            backoff=BackoffPolicy(
                base=self._settings.http_backoff_base,
                maximum=self._settings.http_backoff_max,
            ),
            rps=rps,
            concurrency=concurrency,
            cookies=cookies,
            circuit_breaker=circuit_breaker,
            tz_name=self._settings.timezone,
        )
