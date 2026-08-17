# app/api/schemas.py
"""API 요청/응답 스키마와 도메인 객체 변환기.

`docs/api-contract.md` 가 규격 문서이고 이 파일이 그 구현이다. 둘이 어긋나면
화면이 조용히 깨지므로 항상 같이 수정한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator

from ..domain.enums import CommentStatus, SpamLevel
from ..domain.models import CommentFilter
from ..infrastructure.timeutils import isoformat_local, parse_user_datetime

# 한 번에 돌려줄 수 있는 목록 크기 상한. 브라우저와 서버 모두를 보호한다.
MAX_PAGE_SIZE = 500


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------
class ErrorBody(BaseModel):
    """오류 응답 본문."""

    type: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody


class OkResponse(BaseModel):
    ok: bool = True


# ---------------------------------------------------------------------------
# 게시글
# ---------------------------------------------------------------------------
class TargetOut(BaseModel):
    entry_id: int
    url: str
    title: Optional[str] = None
    enabled: bool = True
    comment_count: int = 0
    source: str = "manual"
    last_collected_at: Optional[str] = None


class TargetListOut(BaseModel):
    items: list[TargetOut]
    total: int


class TargetCreateIn(BaseModel):
    url_or_id: str = Field(min_length=1, max_length=512)


class TargetPatchIn(BaseModel):
    enabled: bool


class CollectIn(BaseModel):
    entry_ids: list[int] = Field(default_factory=list)
    since: Optional[str] = None


class JobCreatedOut(BaseModel):
    job_id: int
    total: int = 0


# ---------------------------------------------------------------------------
# 댓글
# ---------------------------------------------------------------------------
class CommentFilterIn(BaseModel):
    """목록 조회와 일괄 선택이 공유하는 필터 조건."""

    entry_ids: list[int] = Field(default_factory=list)
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    nickname: Optional[str] = None
    content: Optional[str] = None
    levels: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=lambda: [CommentStatus.ACTIVE.value])
    min_score: Optional[int] = None

    @field_validator("levels", "statuses", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """쉼표로 이어진 문자열도 목록으로 받아들인다."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("entry_ids", mode="before")
    @classmethod
    def _split_int_csv(cls, value: Any) -> Any:
        """게시글 번호도 ``723,722`` 형태의 문자열로 받을 수 있게 한다.

        화면은 목록 조회(GET 질의 문자열)와 전체 선택(POST 본문)에 같은 필터 객체를
        쓴다. 두 경로가 같은 표현을 받아들여야 조건이 갈라지지 않는다.
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def to_domain(self, *, tz_name: str) -> CommentFilter:
        """도메인 필터로 변환한다.

        Raises:
            ValueError: 날짜 형식이 잘못된 경우.
        """
        levels = tuple(SpamLevel(value) for value in self.levels)
        statuses = tuple(CommentStatus(value) for value in self.statuses) or (
            CommentStatus.ACTIVE,
        )
        return CommentFilter(
            entry_ids=tuple(self.entry_ids),
            written_from=parse_user_datetime(self.date_from, tz_name=tz_name),
            written_to=parse_user_datetime(self.date_to, tz_name=tz_name),
            nickname_query=(self.nickname or "").strip() or None,
            content_query=(self.content or "").strip() or None,
            spam_levels=levels,
            statuses=statuses,
            min_spam_score=self.min_score,
        )


class CommentOut(BaseModel):
    comment_id: int
    entry_id: int
    nickname: str
    content: str
    written_at: Optional[str] = None
    is_secret: bool = False
    is_reply: bool = False
    is_admin: bool = False
    is_admin_deleted: bool = False
    spam_score: int = 0
    spam_level: str = SpamLevel.NORMAL.value
    spam_reasons: list[str] = Field(default_factory=list)
    whitelisted: bool = False
    status: str = CommentStatus.ACTIVE.value
    deleted_at: Optional[str] = None

    @property
    def protected(self) -> bool:
        return self.whitelisted or self.is_admin


class CommentListOut(BaseModel):
    items: list[CommentOut]
    total: int
    page: int
    size: int
    summary: dict[str, int]


class SelectIdsIn(BaseModel):
    filter: CommentFilterIn


class SelectIdsOut(BaseModel):
    ids: list[int]
    # ids 와 같은 순서의 스팸 등급. 화면이 선택 항목의 등급을 알아야 체크를 해제했을 때
    # 등급별 내역을 다시 조회하지 않고 정확히 다시 셀 수 있다.
    levels: list[str] = Field(default_factory=list)
    count: int
    whitelisted_excluded: int


class RescoreIn(BaseModel):
    entry_ids: list[int] = Field(default_factory=list)


class RescoreOut(BaseModel):
    updated: int


# ---------------------------------------------------------------------------
# 작업
# ---------------------------------------------------------------------------
class DeleteJobIn(BaseModel):
    comment_ids: Optional[list[int]] = None
    filter: Optional[CommentFilterIn] = None
    dry_run: Optional[bool] = None
    rps: Optional[float] = Field(default=None, gt=0, le=100)
    concurrency: Optional[int] = Field(default=None, ge=1, le=32)
    verify_after: bool = True
    # 스팸으로 분류되지 않은 댓글까지 지울지 여부. 기본값은 거부이며 화면에서
    # 사용자가 내역을 확인하고 명시적으로 켜야 한다.
    allow_normal: bool = False


class DeleteJobOut(BaseModel):
    """삭제 작업 생성 결과.

    ``backup`` 은 ``{"json": 경로, "csv": 경로, "count": 건수}`` 형태다.
    필드명 ``json`` 이 BaseModel 메서드와 겹치지 않도록 모델 대신 딕셔너리로 둔다.
    """

    job_id: int
    total: int
    backup: Optional[dict[str, Any]] = None
    dry_run: bool


class JobOut(BaseModel):
    id: int
    type: str
    status: str
    total: int
    done: int
    succeeded: int
    failed: int
    skipped: int
    percent: float
    message: str
    backup_path: Optional[str] = None
    error: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class JobListOut(BaseModel):
    items: list[JobOut]
    total: int
    page: int
    size: int


class JobDetailOut(BaseModel):
    job: JobOut
    counts: dict[str, int]


class JobItemOut(BaseModel):
    comment_id: int
    status: str
    attempts: int
    http_status: Optional[int] = None
    message: str
    updated_at: Optional[str] = None


class JobItemListOut(BaseModel):
    items: list[JobItemOut]
    total: int
    page: int
    size: int


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
class AuthOut(BaseModel):
    state: str
    message: str
    cookie_names: list[str] = Field(default_factory=list)
    checked_at: Optional[str] = None
    can_delete: bool = False


class SettingsOut(BaseModel):
    blog_url: str
    auth: AuthOut
    runtime: dict[str, Any]
    paths: dict[str, str]
    version: str


class CookieIn(BaseModel):
    raw: str = Field(min_length=1)


class TestDeleteIn(BaseModel):
    comment_id: int = Field(gt=0)


class TestDeleteOut(BaseModel):
    comment_id: int
    success: bool
    http_status: Optional[int] = None
    message: str


class RulesOut(BaseModel):
    yaml: str


class RulesIn(BaseModel):
    yaml: str


class RulesSavedOut(BaseModel):
    ok: bool = True
    rule_count: int


# ---------------------------------------------------------------------------
# 통계와 백업
# ---------------------------------------------------------------------------
class OverviewOut(BaseModel):
    totals: dict[str, int]
    targets: list[dict[str, Any]]
    histogram: list[dict[str, Any]]
    top_nicknames: list[dict[str, Any]]
    running_jobs: int


class BackupItemOut(BaseModel):
    name: str
    size: int
    created_at: Optional[str] = None


class BackupListOut(BaseModel):
    items: list[BackupItemOut]


class BackupExportIn(BaseModel):
    """삭제와 무관하게 백업만 내려받을 때 쓰는 요청."""

    comment_ids: Optional[list[int]] = None
    filter: Optional[CommentFilterIn] = None
    label: str = "export"


class BackupExportOut(BaseModel):
    json_file: str
    csv_file: str
    count: int
    created_at: Optional[str] = None


# ---------------------------------------------------------------------------
# 변환기
# ---------------------------------------------------------------------------
def target_to_out(row: Any, *, tz_name: str) -> TargetOut:
    return TargetOut(
        entry_id=row.entry_id,
        url=row.url,
        title=row.title,
        enabled=row.enabled,
        comment_count=row.comment_count,
        source=row.source,
        last_collected_at=isoformat_local(row.last_collected_at, tz_name=tz_name),
    )


def comment_to_out(row: Any, *, tz_name: str) -> CommentOut:
    return CommentOut(
        comment_id=row.comment_id,
        entry_id=row.entry_id,
        nickname=row.nickname,
        content=row.content,
        written_at=isoformat_local(row.written_at, tz_name=tz_name),
        is_secret=row.is_secret,
        is_reply=row.is_reply,
        is_admin=row.is_admin,
        is_admin_deleted=row.is_admin_deleted,
        spam_score=row.spam_score,
        spam_level=row.spam_level,
        spam_reasons=list(row.spam_reasons or []),
        whitelisted=row.whitelisted,
        status=row.status,
        deleted_at=isoformat_local(row.deleted_at, tz_name=tz_name),
    )


def job_to_out(row: Any, *, tz_name: str) -> JobOut:
    percent = round(min(row.done / row.total, 1.0) * 100, 2) if row.total else 0.0
    return JobOut(
        id=row.id,
        type=row.type,
        status=row.status,
        total=row.total,
        done=row.done,
        succeeded=row.succeeded,
        failed=row.failed,
        skipped=row.skipped,
        percent=percent,
        message=row.message,
        backup_path=row.backup_path,
        error=row.error,
        params=dict(row.params or {}),
        created_at=isoformat_local(row.created_at, tz_name=tz_name),
        started_at=isoformat_local(row.started_at, tz_name=tz_name),
        finished_at=isoformat_local(row.finished_at, tz_name=tz_name),
    )


def job_item_to_out(row: Any, *, tz_name: str) -> JobItemOut:
    return JobItemOut(
        comment_id=row.comment_id,
        status=row.status,
        attempts=row.attempts,
        http_status=row.http_status,
        message=row.message,
        updated_at=isoformat_local(row.updated_at, tz_name=tz_name),
    )


def auth_to_out(diagnosis: Any, *, tz_name: str) -> AuthOut:
    return AuthOut(
        state=diagnosis.state.value,
        message=diagnosis.message,
        cookie_names=list(diagnosis.cookie_names),
        checked_at=isoformat_local(diagnosis.checked_at, tz_name=tz_name),
        can_delete=diagnosis.can_delete,
    )


def progress_to_dict(progress: Any, *, tz_name: str) -> dict[str, Any]:
    """SSE 로 내보낼 진행률 페이로드."""
    return {
        "job_id": progress.job_id,
        "status": progress.status,
        "total": progress.total,
        "done": progress.done,
        "succeeded": progress.succeeded,
        "failed": progress.failed,
        "skipped": progress.skipped,
        "percent": progress.percent,
        "message": progress.message,
        "updated_at": isoformat_local(progress.updated_at, tz_name=tz_name),
    }


def stats_row_to_dict(row: dict[str, Any], *, tz_name: str, title: Optional[str]) -> dict[str, Any]:
    """게시글별 집계 행을 응답 형태로 바꾼다."""
    payload = dict(row)
    payload["title"] = title
    for key in ("first_written_at", "last_written_at"):
        value = payload.get(key)
        payload[key] = isoformat_local(value, tz_name=tz_name) if isinstance(value, datetime) else None
    return payload
