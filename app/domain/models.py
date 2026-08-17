# app/domain/models.py
"""순수 도메인 엔티티.

외부 라이브러리에 의존하지 않는 dataclass 로만 구성한다. ORM 모델이나 API 스키마와
분리해 두어야 저장소나 표현 형식이 바뀌어도 업무 규칙이 흔들리지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .enums import AuthState, CommentStatus, SpamLevel


@dataclass(frozen=True)
class ParsedComment:
    """티스토리 응답 HTML에서 추출한 댓글 1건.

    Attributes:
        comment_id: 티스토리 댓글 고유 번호. 삭제 API의 키가 된다.
        entry_id: 댓글이 달린 게시글 번호.
        nickname: 작성자 표시 이름.
        content: 댓글 본문. 관리자가 내용을 지운 경우 안내 문구가 들어 있다.
        written_at: 작성 시각(UTC). 목록 HTML은 분 단위까지만 제공한다.
        written_ts: 페이징 커서로 얻은 초 단위 epoch. 없으면 None.
        is_secret: 비밀 댓글 여부.
        is_reply: 대댓글 여부.
        is_admin: 블로그 운영자가 작성한 댓글인지 여부. 실수로 지우지 않도록 보호한다.
        is_admin_deleted: 운영정책 위반으로 본문이 관리자 삭제된 상태인지 여부.
    """

    comment_id: int
    entry_id: int
    nickname: str
    content: str
    written_at: datetime
    written_ts: Optional[int] = None
    is_secret: bool = False
    is_reply: bool = False
    is_admin: bool = False
    is_admin_deleted: bool = False


@dataclass(frozen=True)
class CommentPage:
    """`POST /comment/view` 한 번의 응답.

    Attributes:
        comments: 이 배치에 포함된 댓글. 시간 오름차순이다.
        cursor: 다음 요청에 넘길 `ts` 값. 배치에서 가장 오래된 댓글의 epoch 초.
        has_more: 더 과거의 댓글이 남아 있는지 여부.
        first_comment_id: 배치에서 가장 오래된 댓글의 번호.
        raw_count: 티스토리가 보고한 건수. 파싱 결과와 다르면 파서 결함 신호다.
    """

    comments: tuple[ParsedComment, ...]
    cursor: Optional[int]
    has_more: bool
    first_comment_id: Optional[int] = None
    raw_count: int = 0


@dataclass(frozen=True)
class SpamVerdict:
    """규칙 엔진이 댓글 1건에 내린 판정.

    Attributes:
        score: 적중한 규칙 가중치의 합. 0 이상.
        level: 임계값과 비교해 산출한 등급.
        reasons: 적중한 규칙 ID 목록. 화면에서 근거로 보여준다.
        whitelisted: 화이트리스트에 걸려 삭제 대상에서 제외해야 하는지 여부.
    """

    score: int
    level: SpamLevel
    reasons: tuple[str, ...] = ()
    whitelisted: bool = False


@dataclass(frozen=True)
class TargetSpec:
    """수집 대상 게시글 정의.

    Attributes:
        entry_id: 게시글 번호.
        url: 게시글 전체 주소.
        title: 게시글 제목. 알 수 없으면 None.
    """

    entry_id: int
    url: str
    title: Optional[str] = None


@dataclass(frozen=True)
class CollectResult:
    """게시글 1건에 대한 수집 결과 요약."""

    entry_id: int
    fetched: int
    inserted: int
    updated: int
    pages: int
    stopped_early: bool = False
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class DeleteOutcome:
    """댓글 1건 삭제 시도의 결과.

    Attributes:
        comment_id: 대상 댓글 번호.
        success: 삭제 성공 여부.
        http_status: 티스토리가 돌려준 HTTP 상태 코드.
        message: 실패 사유 또는 응답 요약.
        attempts: 총 시도 횟수(최초 시도 포함).
        dry_run: 실제 요청을 보내지 않은 시뮬레이션이었는지 여부.
        already_gone: 이번 요청으로 지운 것이 아니라 이미 사라져 있었는지 여부.
            삭제는 멱등 연산이라 이 경우도 성공이지만, 사용자에게는 구분해서 알려야
            "몇 건을 실제로 지웠는가" 를 정확히 이해할 수 있다.
    """

    comment_id: int
    success: bool
    http_status: Optional[int] = None
    message: str = ""
    attempts: int = 1
    dry_run: bool = False
    already_gone: bool = False


@dataclass
class JobProgress:
    """작업 진행 상황 스냅샷. SSE 로 그대로 전달한다."""

    job_id: int
    status: str
    total: int = 0
    done: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    message: str = ""
    updated_at: Optional[datetime] = None

    @property
    def percent(self) -> float:
        """0.0 ~ 100.0 진행률. total 이 0이면 0을 반환한다."""
        if self.total <= 0:
            return 0.0
        return round(min(self.done / self.total, 1.0) * 100, 2)


@dataclass(frozen=True)
class AuthDiagnosis:
    """세션 쿠키 진단 결과.

    Attributes:
        state: 판정된 인증 상태.
        message: 사용자에게 보여줄 설명.
        cookie_names: 등록된 쿠키 이름 목록. 값은 절대 포함하지 않는다.
        checked_at: 진단 시각(UTC).
    """

    state: AuthState
    message: str
    cookie_names: tuple[str, ...] = ()
    checked_at: Optional[datetime] = None

    @property
    def can_delete(self) -> bool:
        """삭제 작업을 시작해도 되는 상태인지 여부."""
        return self.state is AuthState.OWNER


@dataclass
class CommentFilter:
    """댓글 목록 조회 및 일괄 선택에 쓰는 필터 조건.

    선택을 ID 목록이 아니라 이 조건으로도 전달할 수 있어야 수천 건 선택 시에도
    요청 크기가 일정하게 유지된다.
    """

    entry_ids: tuple[int, ...] = ()
    written_from: Optional[datetime] = None      # UTC
    written_to: Optional[datetime] = None        # UTC
    nickname_query: Optional[str] = None
    content_query: Optional[str] = None
    spam_levels: tuple[SpamLevel, ...] = ()
    statuses: tuple[CommentStatus, ...] = (CommentStatus.ACTIVE,)
    min_spam_score: Optional[int] = None
    exclude_comment_ids: tuple[int, ...] = field(default=())

    def is_empty(self) -> bool:
        """아무 조건도 지정되지 않았는지 여부. 전체 선택 경고에 쓴다."""
        return not any(
            (
                self.entry_ids,
                self.written_from,
                self.written_to,
                self.nickname_query,
                self.content_query,
                self.spam_levels,
                self.min_spam_score is not None,
            )
        )
