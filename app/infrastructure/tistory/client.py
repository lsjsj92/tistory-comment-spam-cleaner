# app/infrastructure/tistory/client.py
"""티스토리 저수준 HTTP 클라이언트.

여기서만 외부 통신을 한다. 상위 계층은 이 클래스가 돌려주는 도메인 객체와 예외만
다루므로, 티스토리 규격이 바뀌어도 수정 범위가 이 파일에 갇힌다.

확인된 규격
    - ``POST /comment/view``          : 댓글 조회. ``Referer`` 헤더가 없으면 412.
    - ``POST /comment/delete/{id}``   : 댓글 삭제. 소유자 세션 쿠키 필요, CSRF 토큰 없음.
    - ``GET  /sitemap.xml``           : 전체 게시글 주소 목록.
    - ``GET  /manage/comment``        : 소유자 관리 화면. 로그인 여부 진단에 쓴다.
"""

from __future__ import annotations

import asyncio
import json
from types import TracebackType
from typing import Any, Optional

import httpx

from ...domain.enums import AuthState
from ...domain.errors import (
    AuthenticationError,
    PermissionDeniedError,
    RateLimitedError,
    TistoryApiError,
)
from ...domain.models import AuthDiagnosis, CommentPage, DeleteOutcome
from ..logging_setup import get_logger
from ..timeutils import utc_now
from .parser import CommentHtmlParser
from .ratelimit import BackoffPolicy, CircuitBreaker, TokenBucket

logger = get_logger(__name__)

# 재시도해도 의미가 있는 HTTP 상태 코드
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

# 인증 문제로 판단할 상태 코드
_AUTH_STATUS = frozenset({401, 403})

# 로그인 페이지로 유도되는지 확인할 때 쓰는 경로 조각
_LOGIN_PATH_MARKER = "/auth/login"


def next_comment_cursor(page: CommentPage, current: Optional[int]) -> Optional[int]:
    """댓글 목록의 다음 페이지를 요청할 커서를 계산한다.

    티스토리의 ``ts`` 는 **미만(exclusive)** 이다. 실측으로 확인했다.
    응답이 준 ``ts`` 를 그대로 다음 요청에 넘기면 그 초에 작성된 다른 댓글이
    배치 경계에서 잘려 영원히 조회되지 않는다. 도배는 초당 여러 건이 들어오므로
    이 경계가 자주 발생하고, 실제 블로그 7개 글에서 20건이 이렇게 누락됐다.

    그래서 ``ts + 1`` 로 요청해 경계 초를 포함시킨다. 경계에 걸린 댓글 한 건이
    다시 오지만 저장은 upsert 라 중복이 쌓이지 않는다.

    같은 커서가 반복되면(한 배치가 전부 같은 초인 경우) 전진하지 못하므로
    그때만 ``ts`` 로 낮춘다. 커서는 항상 엄격히 작아지므로 무한 반복은 없다.

    Args:
        page: 방금 받은 페이지.
        current: 이번 요청에 쓴 커서. 첫 요청이면 None.

    Returns:
        다음 요청에 쓸 커서. 더 진행할 수 없으면 None.
    """
    if page.cursor is None:
        return None
    following = page.cursor + 1
    if current is None:
        return following
    if following < current:
        return following
    if page.cursor < current:
        # 경계 초를 포함하면 제자리걸음이 된다. 한 칸 낮춰 반드시 전진시킨다.
        # 그 초의 나머지 댓글은 이번 배치에 이미 들어와 있다.
        return page.cursor
    # 서버가 더 과거로 내려가지 않는다. 같은 값을 돌려주어 호출자가 중단하게 한다.
    return current

# 삭제 응답이 JSON 오류일 때 등장하는 키. 원인이 담긴 message 를 우선한다.
# title 은 "댓글 관련 에러" 처럼 분류만 알려주므로 단독으로는 진단에 쓸모가 없다.
_ERROR_DETAIL_KEYS = ("message", "error")
_ERROR_TITLE_KEY = "title"


class TistoryClient:
    """블로그 1개를 대상으로 하는 비동기 클라이언트.

    수집용과 삭제용을 각각 다른 속도 설정으로 만들어 쓰는 것을 전제로 한다.
    """

    def __init__(
        self,
        *,
        blog_url: str,
        user_agent: str,
        timeout: float,
        max_retries: int,
        backoff: BackoffPolicy,
        rps: float,
        concurrency: int,
        cookies: Optional[dict[str, str]] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        tz_name: str = "Asia/Seoul",
    ) -> None:
        self._blog_url = blog_url.rstrip("/")
        self._max_retries = max_retries
        self._backoff = backoff
        self._bucket = TokenBucket(rps)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._breaker = circuit_breaker
        self._parser = CommentHtmlParser(tz_name=tz_name)
        # 사용자가 등록한 쿠키만 따로 기억한다. 응답의 Set-Cookie 가 섞이면
        # "쿠키가 등록되어 있는가" 판정이 무의미해지기 때문이다.
        self._supplied_cookies: dict[str, str] = dict(cookies or {})
        # 재시도 대기를 중간에 끊기 위한 신호. 없으면 백오프를 끝까지 기다린다.
        self._stop_event: Optional[asyncio.Event] = None
        self._client = httpx.AsyncClient(
            base_url=self._blog_url,
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
                "Origin": self._blog_url,
            },
            cookies=cookies or {},
            limits=httpx.Limits(
                max_connections=concurrency + 2,
                max_keepalive_connections=concurrency,
            ),
        )

    # ------------------------------------------------------------------
    # 수명 주기
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "TistoryClient":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def blog_url(self) -> str:
        return self._blog_url

    @property
    def circuit_breaker(self) -> Optional[CircuitBreaker]:
        return self._breaker

    def bind_stop_event(self, event: asyncio.Event) -> None:
        """취소 신호를 연결한다.

        기본 설정에서 재시도는 최대 5회, 대기는 최대 30초다. 취소를 눌러도 대기가
        끝날 때까지 멈추지 않으면 사용자는 수십 초 동안 아무 반응을 못 본다.
        """
        self._stop_event = event

    def has_cookies(self) -> bool:
        """사용자가 직접 등록한 세션 쿠키가 있는지 여부."""
        return bool(self._supplied_cookies)

    @property
    def cookie_names(self) -> tuple[str, ...]:
        """등록된 쿠키 이름. 값은 노출하지 않는다."""
        return tuple(sorted(self._supplied_cookies))

    # ------------------------------------------------------------------
    # 댓글 조회
    # ------------------------------------------------------------------
    async def fetch_comment_page(self, entry_id: int, cursor: Optional[int] = None) -> CommentPage:
        """게시글의 댓글 한 페이지를 가져온다.

        Args:
            entry_id: 게시글 번호.
            cursor: 이전 응답의 ``ts``. None 이면 최신 배치부터 시작한다.

        Raises:
            TistoryApiError: 응답이 JSON 이 아니거나 오류 구조인 경우.
        """
        data: dict[str, Any] = {"id": str(entry_id)}
        if cursor is not None:
            data["ts"] = str(cursor)

        response = await self._request(
            "POST",
            "/comment/view",
            data=data,
            # Referer 가 없으면 티스토리가 412 로 거부한다.
            headers={
                "Referer": f"{self._blog_url}/{entry_id}",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

        payload = self._parse_json(response)
        body = payload.get("data")
        if not isinstance(body, dict):
            raise TistoryApiError(
                f"댓글 조회 응답 형식이 예상과 다릅니다 (entry={entry_id}).",
                http_status=response.status_code,
                detail=payload,
            )
        return self._parser.parse_page(body, entry_id)

    # ------------------------------------------------------------------
    # 댓글 삭제
    # ------------------------------------------------------------------
    async def delete_comment(self, comment_id: int, *, dry_run: bool = False) -> DeleteOutcome:
        """댓글 1건을 삭제한다.

        Args:
            comment_id: 삭제할 댓글 번호.
            dry_run: True 면 실제 요청을 보내지 않고 성공한 것처럼 결과만 만든다.

        Returns:
            성공 여부와 사유가 담긴 :class:`DeleteOutcome`.
        """
        if dry_run:
            return DeleteOutcome(
                comment_id=comment_id,
                success=True,
                message="드라이런: 실제 요청을 보내지 않았습니다.",
                dry_run=True,
            )

        if not self.has_cookies():
            raise AuthenticationError(
                "세션 쿠키가 없어 삭제할 수 없습니다. 설정 화면에서 쿠키를 등록하세요."
            )

        payload = {
            "commentId": str(comment_id),
            "password": "",
            "migPassword": "",
            "mode": "delete",
            "guestbookWrittenPage": "-1",
        }
        try:
            response = await self._request(
                "POST",
                f"/comment/delete/{comment_id}",
                data=payload,
                headers={"Referer": f"{self._blog_url}/comment/manage/{comment_id}"},
            )
        except TistoryApiError as exc:
            return DeleteOutcome(
                comment_id=comment_id,
                success=False,
                http_status=exc.http_status,
                message=exc.message,
                attempts=self._max_retries + 1,
            )

        return self._interpret_delete_response(comment_id, response)

    def _interpret_delete_response(
        self, comment_id: int, response: httpx.Response
    ) -> DeleteOutcome:
        """삭제 응답을 성공/실패로 판정한다.

        폼 전송 방식이라 성공 시 리다이렉트가 올 수도, 빈 200 이 올 수도 있다.
        실패는 일관되게 JSON 오류 구조로 돌아오므로 그것을 기준으로 판단한다.
        """
        status = response.status_code

        if status in _AUTH_STATUS:
            return DeleteOutcome(
                comment_id=comment_id,
                success=False,
                http_status=status,
                message="권한이 없습니다. 세션이 만료되었을 수 있습니다.",
            )

        if 300 <= status < 400:
            location = response.headers.get("location", "")
            if _LOGIN_PATH_MARKER in location:
                return DeleteOutcome(
                    comment_id=comment_id,
                    success=False,
                    http_status=status,
                    message="로그인 페이지로 이동했습니다. 세션이 만료되었습니다.",
                )
            # 삭제 후 원래 글로 돌려보내는 정상 흐름
            return DeleteOutcome(comment_id=comment_id, success=True, http_status=status)

        error_message = _extract_error_message(response)
        # 204 는 본문 없는 성공이다. 200 만 성공으로 보면 실제로 지워진 댓글을
        # 실패로 기록하고 같은 대상에 요청을 반복하게 된다.
        if 200 <= status < 300 and error_message is None:
            return DeleteOutcome(comment_id=comment_id, success=True, http_status=status)

        # 대상이 이미 없다는 응답. 삭제는 멱등 연산이므로 목표 상태에 도달한 것으로 본다.
        # 티스토리는 부모 댓글을 지우면 그 아래 대댓글을 함께 지운다. 그래서 대댓글을
        # 뒤이어 지우려 하면 412 와 함께 이 응답이 온다. 실패로 세면 정상 동작이
        # 연속 실패로 집계되어 서킷 브레이커가 열리고 남은 작업이 통째로 멈춘다.
        # 권한 오류(_AUTH_STATUS)와 로그인 리다이렉트는 위에서 이미 걸러졌으므로
        # 세션 만료를 "이미 지워짐" 으로 오판할 여지는 없다.
        if error_message and _looks_already_gone(error_message):
            return DeleteOutcome(
                comment_id=comment_id,
                success=True,
                already_gone=True,
                http_status=status,
                message=f"이미 삭제되어 있습니다. 티스토리 응답: {error_message}",
            )

        return DeleteOutcome(
            comment_id=comment_id,
            success=False,
            http_status=status,
            message=error_message or f"예상하지 못한 응답입니다 (HTTP {status}).",
        )

    # ------------------------------------------------------------------
    # 부가 조회
    # ------------------------------------------------------------------
    async def fetch_sitemap(self) -> str:
        """sitemap.xml 원문을 가져온다."""
        response = await self._request("GET", "/sitemap.xml")
        if response.status_code != 200:
            raise TistoryApiError(
                "sitemap.xml 을 가져오지 못했습니다.", http_status=response.status_code
            )
        return response.text

    async def fetch_text(self, path: str) -> Optional[str]:
        """블로그 내 임의 경로의 텍스트 응답을 가져온다.

        중첩 sitemap 추적처럼 미리 정해두지 않은 경로가 필요할 때 쓴다.
        속도 제한과 재시도 정책은 다른 요청과 동일하게 적용된다.

        Returns:
            200 응답의 본문. 그 외 상태 코드면 None.
        """
        response = await self._request("GET", path)
        return response.text if response.status_code == 200 else None

    async def fetch_entry_html(self, entry_id: int) -> str:
        """게시글 본문 HTML. 제목을 얻는 데 쓴다."""
        response = await self._request("GET", f"/{entry_id}")
        if response.status_code != 200:
            raise TistoryApiError(
                f"게시글 {entry_id} 을(를) 가져오지 못했습니다.", http_status=response.status_code
            )
        return response.text

    async def diagnose_session(self) -> AuthDiagnosis:
        """등록된 쿠키로 소유자 권한이 유효한지 확인한다.

        관리 화면에 접근해 로그인 페이지로 튕기는지 여부로 판정한다. 이 요청은
        읽기 전용이라 블로그 상태를 바꾸지 않는다.
        """
        names = self.cookie_names
        if not names:
            return AuthDiagnosis(
                state=AuthState.MISSING,
                message="등록된 쿠키가 없습니다. 설정 화면에서 쿠키를 붙여넣으세요.",
                checked_at=utc_now(),
            )

        try:
            response = await self._request("GET", "/manage/comment")
        except TistoryApiError as exc:
            return AuthDiagnosis(
                state=AuthState.UNKNOWN,
                message=f"진단 요청이 실패했습니다: {exc.message}",
                cookie_names=names,
                checked_at=utc_now(),
            )

        status = response.status_code
        location = response.headers.get("location", "")

        if status == 200:
            return AuthDiagnosis(
                state=AuthState.OWNER,
                message="소유자 권한이 확인되었습니다. 삭제 작업을 실행할 수 있습니다.",
                cookie_names=names,
                checked_at=utc_now(),
            )
        if 300 <= status < 400 and _LOGIN_PATH_MARKER in location:
            return AuthDiagnosis(
                state=AuthState.ANONYMOUS,
                message="로그인 상태가 아닙니다. 브라우저에서 로그인한 뒤 쿠키를 다시 복사하세요.",
                cookie_names=names,
                checked_at=utc_now(),
            )
        if status in _AUTH_STATUS:
            return AuthDiagnosis(
                state=AuthState.NOT_OWNER,
                message="로그인은 되어 있으나 이 블로그의 관리 권한이 없습니다.",
                cookie_names=names,
                checked_at=utc_now(),
            )
        return AuthDiagnosis(
            state=AuthState.UNKNOWN,
            message=f"진단 결과를 판단할 수 없습니다 (HTTP {status}).",
            cookie_names=names,
            checked_at=utc_now(),
        )

    # ------------------------------------------------------------------
    # 내부 요청 처리
    # ------------------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> httpx.Response:
        """속도 제한, 동시성 제한, 재시도를 적용해 요청을 보낸다.

        서킷 브레이커는 여기서 열림 여부만 확인한다. 실패를 세는 것은 호출자의 몫이다.
        전송 계층에서 세면 한 건의 재시도 5회가 실패 5회로 잡혀 "연속 실패 N회" 라는
        설명과 어긋나고, 재시도 대상이 아닌 권한 오류(403)는 아예 세지 못한다.

        Raises:
            TistoryApiError: 재시도를 모두 소진했거나 재시도 대상이 아닌 오류인 경우.
            CircuitOpenError: 서킷 브레이커가 열려 있는 경우.
        """
        last_error: Optional[TistoryApiError] = None

        for attempt in range(self._max_retries + 1):
            if self._breaker is not None:
                await self._breaker.ensure_closed()

            await self._bucket.acquire()
            async with self._semaphore:
                try:
                    response = await self._client.request(
                        method, path, data=data, headers=headers
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = TistoryApiError(
                        f"네트워크 오류: {exc}", retryable=True, detail=str(exc)
                    )
                else:
                    if response.status_code not in _RETRYABLE_STATUS:
                        return response
                    last_error = self._error_for_status(response)

            if attempt >= self._max_retries:
                break

            retry_after = last_error.retry_after if isinstance(last_error, RateLimitedError) else None
            delay = self._backoff.delay_for(attempt + 1, retry_after=retry_after)
            logger.warning(
                "요청 재시도 %s %s (%d/%d) %.2f초 후: %s",
                method,
                path,
                attempt + 1,
                self._max_retries,
                delay,
                last_error.message if last_error else "",
            )
            if await self._sleep_or_stop(delay):
                # 취소 신호가 오면 남은 재시도를 포기한다.
                break

        raise last_error or TistoryApiError(f"{method} {path} 요청에 실패했습니다.")

    async def _sleep_or_stop(self, delay: float) -> bool:
        """재시도 대기. 취소 신호가 오면 즉시 깨어나 True 를 돌려준다."""
        if self._stop_event is None:
            await asyncio.sleep(delay)
            return False
        if self._stop_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return False
        return True

    def _error_for_status(self, response: httpx.Response) -> TistoryApiError:
        """재시도 대상 상태 코드를 예외 객체로 바꾼다."""
        if response.status_code == 429:
            raw = response.headers.get("retry-after")
            retry_after: Optional[float]
            try:
                retry_after = float(raw) if raw else None
            except ValueError:
                retry_after = None
            return RateLimitedError("티스토리가 요청 속도를 제한했습니다.", retry_after=retry_after)
        return TistoryApiError(
            f"서버 오류 응답 (HTTP {response.status_code}).",
            http_status=response.status_code,
            retryable=True,
        )

    def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        """응답 본문을 JSON 으로 해석한다."""
        if response.status_code in _AUTH_STATUS:
            raise PermissionDeniedError("해당 자원에 접근할 권한이 없습니다.")
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise TistoryApiError(
                f"JSON 응답을 기대했으나 해석하지 못했습니다 (HTTP {response.status_code}).",
                http_status=response.status_code,
                detail=response.text[:500],
            ) from exc
        if not isinstance(payload, dict):
            raise TistoryApiError(
                "JSON 객체 응답을 기대했습니다.", http_status=response.status_code, detail=payload
            )
        message = payload.get("message")
        if response.status_code != 200 and message:
            raise TistoryApiError(
                str(message), http_status=response.status_code, detail=payload
            )
        return payload


# 대상이 이미 없다는 뜻으로 확인된 티스토리 응답 문구.
# 넓게 잡으면 진짜 실패를 성공으로 오판하므로 실제로 관측한 문구만 넣는다.
# 오판이 생기더라도 사후 검증(DeletionService.verify)이 재조회로 되돌린다.
_ALREADY_GONE_MARKERS = (
    # 부모 댓글이 지워져 대댓글이 속한 대화가 사라진 경우. 실측으로 확인했다.
    "conversation 정보를 찾을 수 없습니다",
    # 댓글 자체가 이미 없는 경우
    "댓글 정보를 찾을 수 없습니다",
    "존재하지 않는 댓글",
)


def _looks_already_gone(message: str) -> bool:
    """삭제 대상이 이미 없다는 응답인지 판별한다."""
    lowered = message.lower()
    return any(marker in lowered for marker in _ALREADY_GONE_MARKERS)


def _extract_error_message(response: httpx.Response) -> Optional[str]:
    """응답 본문이 JSON 오류 구조면 사람이 읽을 메시지를 뽑는다.

    티스토리는 ``{"title": "댓글 관련 에러", "message": "Conversation 정보를 찾을 수 없습니다."}``
    형태로 응답한다. 실패 원인은 ``message`` 에 있으므로 그것을 본문으로 삼고,
    분류를 알려주는 ``title`` 은 앞에 덧붙인다.
    """
    content_type = response.headers.get("content-type", "")
    if "json" not in content_type.lower():
        return None
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    detail = next(
        (
            text
            for key in _ERROR_DETAIL_KEYS
            if (text := _as_error_text(payload.get(key)))
        ),
        None,
    )
    title = payload.get(_ERROR_TITLE_KEY)
    title = title.strip() if isinstance(title, str) and title.strip() else None

    if detail and title and detail != title:
        return f"{title}: {detail}"
    return detail or title


def _as_error_text(value: Any) -> Optional[str]:
    """오류 필드에서 사람이 읽을 문자열을 뽑는다.

    ``{"error": "메시지"}`` 뿐 아니라 ``{"error": {"message": "메시지"}}`` 처럼
    한 겹 감싼 형태도 실패로 인식해야 성공 오판을 막을 수 있다.
    """
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        for key in _ERROR_DETAIL_KEYS + (_ERROR_TITLE_KEY,):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
        # 키 이름은 달라도 내용이 있으면 오류로 본다.
        return str(value) if value else None
    return None
