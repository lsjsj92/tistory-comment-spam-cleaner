# app/infrastructure/db/repositories.py
"""저장소 계층.

서비스는 SQL 을 직접 쓰지 않고 이 클래스들만 사용한다. 질의 조건이 한 곳에 모여
있어야 "선택한 조건과 실제 삭제 대상이 다르다" 같은 치명적 불일치를 막을 수 있다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import ColumnElement, and_, case, delete, func, not_, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.enums import (
    CommentStatus,
    JobItemStatus,
    JobStatus,
    JobType,
    SpamLevel,
)
from ...domain.models import CommentFilter, ParsedComment, SpamVerdict, TargetSpec
from ..timeutils import utc_now
from .models import AppSetting, AuditLog, Comment, Job, JobItem, Target

# IN 절에 넣을 수 있는 최대 항목 수. SQLite 변수 한도(999)를 넘지 않게 나눠 실행한다.
_SQL_CHUNK = 500


def chunked(items: Sequence[Any], size: int = _SQL_CHUNK) -> Iterable[Sequence[Any]]:
    """시퀀스를 SQL 변수 한도 이하 묶음으로 나눈다."""
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _escape_like(term: str) -> str:
    """LIKE 패턴에서 와일드카드를 문자 그대로 취급하도록 이스케이프한다."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _count_if(condition: ColumnElement[bool]):
    """조건을 만족하는 행의 수. SQLite 버전에 상관없이 동작하도록 CASE 로 쓴다."""
    return func.sum(case((condition, 1), else_=0))


def protected_condition() -> ColumnElement[bool]:
    """삭제할 수 없는 댓글 조건. 운영자 본인 댓글과 화이트리스트가 여기 해당한다."""
    return or_(Comment.whitelisted.is_(True), Comment.is_admin.is_(True))


def selectable_condition() -> ColumnElement[bool]:
    """일괄 삭제 대상이 될 수 있는 댓글 조건.

    보호 대상과 이미 삭제된 댓글을 제외한다. 목록의 "선택 가능 N건" 과 전체 선택이
    이 함수 하나만 쓰도록 해서 두 수치가 어긋나지 않게 한다. 예전에는 같은 규칙이
    SQL 과 파이썬 양쪽에 따로 적혀 있어 삭제된 댓글의 처리가 갈렸다.
    """
    return and_(
        not_(protected_condition()),
        Comment.status != CommentStatus.DELETED.value,
    )


def build_comment_conditions(criteria: CommentFilter) -> list[ColumnElement[bool]]:
    """:class:`CommentFilter` 를 SQLAlchemy 조건 목록으로 변환한다.

    목록 조회와 일괄 선택이 반드시 같은 조건을 쓰도록 단일 함수로 유지한다.
    """
    conditions: list[ColumnElement[bool]] = []

    if criteria.entry_ids:
        conditions.append(Comment.entry_id.in_(criteria.entry_ids))
    if criteria.written_from is not None:
        conditions.append(Comment.written_at >= criteria.written_from)
    if criteria.written_to is not None:
        conditions.append(Comment.written_at <= criteria.written_to)
    if criteria.nickname_query:
        pattern = f"%{_escape_like(criteria.nickname_query)}%"
        conditions.append(Comment.nickname.like(pattern, escape="\\"))
    if criteria.content_query:
        pattern = f"%{_escape_like(criteria.content_query)}%"
        conditions.append(Comment.content.like(pattern, escape="\\"))
    if criteria.spam_levels:
        conditions.append(Comment.spam_level.in_([level.value for level in criteria.spam_levels]))
    if criteria.min_spam_score is not None:
        conditions.append(Comment.spam_score >= criteria.min_spam_score)
    if criteria.statuses:
        conditions.append(Comment.status.in_([status.value for status in criteria.statuses]))
    if criteria.exclude_comment_ids:
        conditions.append(Comment.comment_id.notin_(criteria.exclude_comment_ids))

    return conditions


class TargetRepository:
    """수집 대상 게시글 저장소."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, entry_id: int) -> Optional[Target]:
        return await self._session.get(Target, entry_id)

    async def list_all(self, *, enabled_only: bool = False) -> list[Target]:
        stmt = select(Target).order_by(Target.entry_id.desc())
        if enabled_only:
            stmt = stmt.where(Target.enabled.is_(True))
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def upsert(self, spec: TargetSpec, *, source: str = "manual") -> Target:
        """대상을 추가하거나 제목/주소를 갱신한다. enabled 상태는 보존한다."""
        await self._upsert_rows([spec], source=source)
        await self._session.flush()
        target = await self._session.get(Target, spec.entry_id)
        assert target is not None  # 방금 upsert 했으므로 반드시 존재한다.
        await self._session.refresh(target)
        return target

    async def upsert_many(self, specs: Sequence[TargetSpec], *, source: str = "manual") -> int:
        """여러 대상을 한 번에 등록한다. 새로 추가된 건수를 반환한다."""
        if not specs:
            return 0
        # 같은 요청 안에 중복 entry_id 가 섞여 오면 ON CONFLICT 가 실패하므로 먼저 정리한다.
        unique: dict[int, TargetSpec] = {spec.entry_id: spec for spec in specs}
        deduped = list(unique.values())
        existing = await self._existing_ids(list(unique))
        await self._upsert_rows(deduped, source=source)
        return len([spec for spec in deduped if spec.entry_id not in existing])

    async def _upsert_rows(self, specs: Sequence[TargetSpec], *, source: str) -> None:
        """대상 행을 묶어서 삽입/갱신한다."""
        rows = [
            {
                "entry_id": spec.entry_id,
                "url": spec.url,
                "title": spec.title,
                "enabled": True,
                "source": source,
            }
            for spec in specs
        ]
        for chunk in chunked(rows, size=100):
            stmt = sqlite_insert(Target).values(list(chunk))
            stmt = stmt.on_conflict_do_update(
                index_elements=[Target.entry_id],
                set_={
                    "url": stmt.excluded.url,
                    # 새로 얻은 제목이 없으면 기존 제목을 유지한다.
                    "title": func.coalesce(stmt.excluded.title, Target.title),
                    "updated_at": utc_now(),
                },
            )
            await self._session.execute(stmt)

    async def _existing_ids(self, entry_ids: Sequence[int]) -> set[int]:
        found: set[int] = set()
        for chunk in chunked(entry_ids):
            result = await self._session.execute(
                select(Target.entry_id).where(Target.entry_id.in_(chunk))
            )
            found.update(result.scalars().all())
        return found

    async def set_enabled(self, entry_id: int, enabled: bool) -> bool:
        result = await self._session.execute(
            update(Target)
            .where(Target.entry_id == entry_id)
            .values(enabled=enabled, updated_at=utc_now())
        )
        return result.rowcount > 0

    async def remove(self, entry_id: int) -> bool:
        """대상만 제거한다. 이미 수집된 댓글 기록은 남긴다."""
        result = await self._session.execute(delete(Target).where(Target.entry_id == entry_id))
        return result.rowcount > 0

    async def update_collection_stats(
        self, entry_id: int, *, comment_count: int, collected_at: Optional[datetime] = None
    ) -> None:
        await self._session.execute(
            update(Target)
            .where(Target.entry_id == entry_id)
            .values(
                comment_count=comment_count,
                last_collected_at=collected_at or utc_now(),
                updated_at=utc_now(),
            )
        )


class CommentRepository:
    """댓글 저장소."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_many(self, comments: Sequence[ParsedComment]) -> tuple[int, int]:
        """수집한 댓글을 저장한다.

        이미 있는 댓글의 ``status`` 와 삭제 시각은 덮어쓰지 않는다. 삭제 완료한
        댓글이 재수집으로 되살아나는 것을 막기 위함이다.

        Returns:
            (신규 저장 건수, 갱신 건수)
        """
        if not comments:
            return (0, 0)

        # 같은 배치에 같은 댓글이 두 번 실려 와도(응답이 겹치는 경우) 마지막 것만 남긴다.
        # 중복을 그대로 두면 보고 건수가 실제 저장 행 수보다 부풀고, SQLite 버전에 따라
        # ON CONFLICT 가 같은 행을 두 번 갱신하려다 실패한다.
        deduped = list({comment.comment_id: comment for comment in comments}.values())

        ids = [comment.comment_id for comment in deduped]
        existing = await self._existing_ids(ids)

        now = utc_now()
        rows = [
            {
                "comment_id": comment.comment_id,
                "entry_id": comment.entry_id,
                "nickname": comment.nickname,
                "content": comment.content,
                "written_at": comment.written_at,
                "written_ts": comment.written_ts,
                "is_secret": comment.is_secret,
                "is_reply": comment.is_reply,
                "is_admin": comment.is_admin,
                "is_admin_deleted": comment.is_admin_deleted,
                "collected_at": now,
            }
            for comment in deduped
        ]

        for chunk in chunked(rows, size=100):
            stmt = sqlite_insert(Comment).values(list(chunk))
            excluded = stmt.excluded
            stmt = stmt.on_conflict_do_update(
                index_elements=[Comment.comment_id],
                set_={
                    "entry_id": excluded.entry_id,
                    "nickname": excluded.nickname,
                    "content": excluded.content,
                    "written_at": excluded.written_at,
                    "written_ts": func.coalesce(excluded.written_ts, Comment.written_ts),
                    "is_secret": excluded.is_secret,
                    "is_reply": excluded.is_reply,
                    # 운영자 표식은 한 번 확인되면 유지한다. 티스토리 마크업이 바뀌어
                    # rp_admin 을 놓치는 순간 운영자 댓글 보호가 통째로 풀리기 때문이다.
                    "is_admin": or_(Comment.is_admin, excluded.is_admin),
                    "is_admin_deleted": excluded.is_admin_deleted,
                    "collected_at": excluded.collected_at,
                },
            )
            await self._session.execute(stmt)

        inserted = len([cid for cid in ids if cid not in existing])
        return (inserted, len(ids) - inserted)

    async def _existing_ids(self, comment_ids: Sequence[int]) -> set[int]:
        found: set[int] = set()
        for chunk in chunked(comment_ids):
            result = await self._session.execute(
                select(Comment.comment_id).where(Comment.comment_id.in_(chunk))
            )
            found.update(result.scalars().all())
        return found

    async def count(self, criteria: CommentFilter) -> int:
        stmt = select(func.count()).select_from(Comment).where(*build_comment_conditions(criteria))
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def count_protected(self, criteria: CommentFilter) -> int:
        """조건에 맞으면서 삭제할 수 없는 댓글 수.

        운영자 본인 댓글과 화이트리스트 댓글이 여기에 해당한다. 화면에서
        보호 건수를 따로 알려주기 위해 센다.
        """
        stmt = (
            select(func.count())
            .select_from(Comment)
            .where(*build_comment_conditions(criteria), protected_condition())
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def count_selectable(self, criteria: CommentFilter) -> int:
        """조건에 맞으면서 실제로 일괄 삭제 대상이 될 수 있는 댓글 수.

        화면의 "필터 결과 전체 선택 (N건)" 에 쓰이는 값이라
        :meth:`ids_for` 의 ``selectable_only`` 결과와 반드시 같은 수여야 한다.
        """
        stmt = (
            select(func.count())
            .select_from(Comment)
            .where(*build_comment_conditions(criteria), selectable_condition())
        )
        result = await self._session.execute(stmt)
        return int(result.scalar_one())

    async def list_page(
        self,
        criteria: CommentFilter,
        *,
        offset: int = 0,
        limit: int = 50,
        newest_first: bool = True,
    ) -> list[Comment]:
        order = Comment.written_at.desc() if newest_first else Comment.written_at.asc()
        stmt = (
            select(Comment)
            .where(*build_comment_conditions(criteria))
            .order_by(order, Comment.comment_id.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def selectable_with_levels(
        self, criteria: CommentFilter
    ) -> list[tuple[int, str]]:
        """전체 선택 대상의 (댓글 번호, 스팸 등급) 목록.

        등급을 함께 돌려주는 이유가 있다. 화면이 선택한 항목의 등급을 알고 있어야
        사용자가 체크를 해제했을 때 등급별 내역이 즉시 정확해진다. 등급을 모르면
        필터 조건으로 다시 세는 수밖에 없는데, 그 값은 선택에서 뺀 항목을 반영하지
        못해 "해제했는데 숫자가 그대로" 인 상태가 된다.

        조건은 :meth:`count_selectable` 과 같은 :func:`selectable_condition` 이다.
        """
        stmt = (
            select(Comment.comment_id, Comment.spam_level)
            .where(*build_comment_conditions(criteria), selectable_condition())
            .order_by(Comment.written_at.asc(), Comment.comment_id.asc())
        )
        result = await self._session.execute(stmt)
        return [(int(comment_id), str(level)) for comment_id, level in result.all()]

    async def ids_for(self, criteria: CommentFilter, *, limit: Optional[int] = None) -> list[int]:
        """조건에 맞는 댓글 ID 전체. 백업 대상 지정과 작업 생성에 쓴다."""
        stmt = (
            select(Comment.comment_id)
            .where(*build_comment_conditions(criteria))
            .order_by(Comment.written_at.asc(), Comment.comment_id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [int(value) for value in result.scalars().all()]

    async def get_many(self, comment_ids: Sequence[int]) -> list[Comment]:
        rows: list[Comment] = []
        for chunk in chunked(comment_ids):
            result = await self._session.execute(
                select(Comment)
                .where(Comment.comment_id.in_(chunk))
                .order_by(Comment.written_at.asc())
            )
            rows.extend(result.scalars().all())
        return rows

    async def all_for_scoring(self, entry_ids: Optional[Sequence[int]] = None) -> list[Comment]:
        """규칙 재계산 대상. 삭제 완료 건은 점수를 다시 매길 필요가 없다."""
        stmt = select(Comment).where(Comment.status != CommentStatus.DELETED.value)
        if entry_ids:
            stmt = stmt.where(Comment.entry_id.in_(entry_ids))
        result = await self._session.execute(stmt.order_by(Comment.written_at.asc()))
        return list(result.scalars().all())

    async def apply_verdicts(self, verdicts: dict[int, SpamVerdict]) -> int:
        """규칙 엔진 판정 결과를 반영한다.

        기본키 기준 일괄 UPDATE 를 쓴다. 건별로 UPDATE 를 날리면 SQLAlchemy 가
        매번 세션에 적재된 객체 전체를 동기화하느라 건수의 제곱에 비례해 느려지고,
        5천 건 규모에서 한 트랜잭션이 2분 넘게 쓰기 잠금을 쥐게 된다. 그 사이 다른
        작업의 기록이 잠금 대기로 실패하므로 성능 문제가 아니라 정합성 문제다.

        Returns:
            반영을 시도한 건수.
        """
        if not verdicts:
            return 0

        # 기본키 일괄 UPDATE 는 대상 행이 하나라도 없으면 묶음 전체가 StaleDataError 로
        # 실패한다. 판정을 만든 뒤 저장하기까지 사이에 행이 사라질 수 있으므로,
        # 실제 존재하는 것만 추려서 넘긴다.
        existing = await self._existing_ids(list(verdicts))
        rows = [
            {
                "comment_id": comment_id,
                "spam_score": verdict.score,
                "spam_level": verdict.level.value,
                "spam_reasons": list(verdict.reasons),
                "whitelisted": verdict.whitelisted,
            }
            for comment_id, verdict in verdicts.items()
            if comment_id in existing
        ]
        if not rows:
            return 0
        for chunk in chunked(rows, size=200):
            await self._session.execute(
                update(Comment).execution_options(synchronize_session=False), list(chunk)
            )
        return len(rows)

    async def mark_status(
        self, comment_ids: Sequence[int], status: CommentStatus, *, error: Optional[str] = None
    ) -> int:
        """여러 댓글의 상태를 한 번에 바꾼다."""
        if not comment_ids:
            return 0
        values: dict[str, Any] = {"status": status.value, "last_error": error}
        if status is CommentStatus.DELETED:
            values["deleted_at"] = utc_now()
        changed = 0
        for chunk in chunked(comment_ids):
            result = await self._session.execute(
                update(Comment).where(Comment.comment_id.in_(chunk)).values(**values)
            )
            changed += result.rowcount
        return changed

    async def reset_stale_deleting(self) -> int:
        """중단된 작업이 남긴 ``deleting`` 상태를 정상으로 되돌린다."""
        result = await self._session.execute(
            update(Comment)
            .where(Comment.status == CommentStatus.DELETING.value)
            .values(status=CommentStatus.ACTIVE.value)
        )
        return result.rowcount

    async def stats_by_entry(self) -> list[dict[str, Any]]:
        """게시글별 현황 집계."""
        stmt = (
            select(
                Comment.entry_id,
                func.count().label("total"),
                _count_if(Comment.status == CommentStatus.ACTIVE.value).label("active"),
                _count_if(Comment.status == CommentStatus.DELETED.value).label("deleted"),
                _count_if(Comment.spam_level == SpamLevel.SPAM.value).label("spam"),
                _count_if(Comment.spam_level == SpamLevel.SUSPICIOUS.value).label("suspicious"),
                func.min(Comment.written_at).label("first_written_at"),
                func.max(Comment.written_at).label("last_written_at"),
            )
            .group_by(Comment.entry_id)
            .order_by(func.count().desc())
        )
        result = await self._session.execute(stmt)
        return [dict(row._mapping) for row in result.all()]

    async def totals(self) -> dict[str, int]:
        """대시보드 스탯 타일용 전체 집계."""
        stmt = select(
            func.count().label("total"),
            _count_if(Comment.status == CommentStatus.ACTIVE.value).label("active"),
            _count_if(Comment.status == CommentStatus.DELETED.value).label("deleted"),
            _count_if(Comment.status == CommentStatus.FAILED.value).label("failed"),
            _count_if(Comment.spam_level == SpamLevel.SPAM.value).label("spam"),
            _count_if(Comment.spam_level == SpamLevel.SUSPICIOUS.value).label("suspicious"),
        )
        row = (await self._session.execute(stmt)).one()
        return {key: int(value or 0) for key, value in row._mapping.items()}

    async def hourly_histogram(
        self, *, entry_ids: Optional[Sequence[int]] = None, tz_offset_hours: int = 9
    ) -> list[dict[str, Any]]:
        """시간대별 댓글 유입 분포.

        저장은 UTC 이므로 SQL 안에서 사용자 시간대만큼 이동시킨 뒤 시간 단위로 자른다.
        """
        shifted = func.datetime(Comment.written_at, f"{tz_offset_hours:+d} hours")
        bucket = func.strftime("%Y-%m-%d %H:00", shifted).label("bucket")
        stmt = select(bucket, func.count().label("count")).group_by(bucket).order_by(bucket)
        if entry_ids:
            stmt = stmt.where(Comment.entry_id.in_(entry_ids))
        result = await self._session.execute(stmt)
        return [{"bucket": row.bucket, "count": int(row.count)} for row in result.all()]

    async def nickname_counts(self, entry_ids: Optional[Sequence[int]] = None) -> dict[str, int]:
        """닉네임별 작성 횟수. 규칙 엔진의 반복 작성자 판정에 쓴다."""
        stmt = select(Comment.nickname, func.count().label("count")).group_by(Comment.nickname)
        if entry_ids:
            stmt = stmt.where(Comment.entry_id.in_(entry_ids))
        result = await self._session.execute(stmt)
        return {row.nickname: int(row.count) for row in result.all()}

    async def top_nicknames(self, limit: int = 10) -> list[dict[str, Any]]:
        """작성 횟수 상위 닉네임. 대시보드에서 공격자 식별에 쓴다."""
        stmt = (
            select(Comment.nickname, func.count().label("count"))
            .group_by(Comment.nickname)
            .order_by(func.count().desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return [{"nickname": row.nickname, "count": int(row.count)} for row in result.all()]

    async def delete_by_entry(self, entry_id: int) -> int:
        """로컬 수집 기록만 지운다. 블로그의 실제 댓글에는 영향이 없다."""
        result = await self._session.execute(delete(Comment).where(Comment.entry_id == entry_id))
        return result.rowcount


class JobRepository:
    """작업과 작업 항목 저장소."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, *, job_type: JobType, params: dict[str, Any], total: int = 0, message: str = ""
    ) -> Job:
        job = Job(
            type=job_type.value,
            status=JobStatus.PENDING.value,
            params=params,
            total=total,
            message=message,
        )
        self._session.add(job)
        await self._session.flush()
        return job

    async def get(self, job_id: int) -> Optional[Job]:
        return await self._session.get(Job, job_id)

    async def list_recent(
        self, *, limit: int = 50, offset: int = 0, job_type: Optional[JobType] = None
    ) -> list[Job]:
        stmt = select(Job).order_by(Job.created_at.desc()).offset(offset).limit(limit)
        if job_type is not None:
            stmt = stmt.where(Job.type == job_type.value)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_all(self, *, job_type: Optional[JobType] = None) -> int:
        stmt = select(func.count()).select_from(Job)
        if job_type is not None:
            stmt = stmt.where(Job.type == job_type.value)
        return int((await self._session.execute(stmt)).scalar_one())

    async def list_by_status(self, statuses: Sequence[JobStatus]) -> list[Job]:
        result = await self._session.execute(
            select(Job).where(Job.status.in_([status.value for status in statuses]))
        )
        return list(result.scalars().all())

    async def update_fields(self, job_id: int, **values: Any) -> None:
        if not values:
            return
        values.setdefault("updated_at", utc_now())
        await self._session.execute(update(Job).where(Job.id == job_id).values(**values))

    async def add_items(self, job_id: int, comment_ids: Sequence[int]) -> int:
        """작업 항목을 등록한다. 같은 작업에 중복 등록되지 않는다."""
        if not comment_ids:
            return 0
        added = 0
        for chunk in chunked(comment_ids, size=200):
            stmt = (
                sqlite_insert(JobItem)
                .values(
                    [
                        {
                            "job_id": job_id,
                            "comment_id": comment_id,
                            "status": JobItemStatus.PENDING.value,
                        }
                        for comment_id in chunk
                    ]
                )
                .on_conflict_do_nothing(index_elements=[JobItem.job_id, JobItem.comment_id])
            )
            result = await self._session.execute(stmt)
            added += result.rowcount if result.rowcount and result.rowcount > 0 else 0
        return added

    async def pending_item_ids(self, job_id: int, *, limit: Optional[int] = None) -> list[int]:
        stmt = (
            select(JobItem.comment_id)
            .where(JobItem.job_id == job_id, JobItem.status == JobItemStatus.PENDING.value)
            .order_by(JobItem.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self._session.execute(stmt)
        return [int(value) for value in result.scalars().all()]

    async def update_item(
        self,
        job_id: int,
        comment_id: int,
        *,
        status: JobItemStatus,
        attempts: int = 1,
        http_status: Optional[int] = None,
        message: str = "",
    ) -> None:
        await self._session.execute(
            update(JobItem)
            .where(JobItem.job_id == job_id, JobItem.comment_id == comment_id)
            .values(
                status=status.value,
                attempts=attempts,
                http_status=http_status,
                message=message[:2000],
                updated_at=utc_now(),
            )
        )

    async def item_counts(self, job_id: int) -> dict[str, int]:
        result = await self._session.execute(
            select(JobItem.status, func.count())
            .where(JobItem.job_id == job_id)
            .group_by(JobItem.status)
        )
        counts = {status.value: 0 for status in JobItemStatus}
        for status_value, count in result.all():
            counts[status_value] = int(count)
        return counts

    async def failed_item_ids(self, job_id: int) -> list[int]:
        result = await self._session.execute(
            select(JobItem.comment_id).where(
                JobItem.job_id == job_id, JobItem.status == JobItemStatus.FAILED.value
            )
        )
        return [int(value) for value in result.scalars().all()]

    async def list_items(
        self, job_id: int, *, limit: int = 100, offset: int = 0, status: Optional[JobItemStatus] = None
    ) -> list[JobItem]:
        stmt = (
            select(JobItem)
            .where(JobItem.job_id == job_id)
            .order_by(JobItem.id.asc())
            .offset(offset)
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(JobItem.status == status.value)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


class AuditRepository:
    """되돌릴 수 없는 조작의 감사 기록."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log(self, action: str, *, target: str = "", detail: Optional[dict[str, Any]] = None) -> None:
        self._session.add(AuditLog(action=action, target=target, detail=detail or {}))

    async def list_recent(self, *, limit: int = 100) -> list[AuditLog]:
        result = await self._session.execute(
            select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())


class SettingsRepository:
    """`.env` 로 관리하지 않는 런타임 값 저장소. 현재는 세션 쿠키 계열만 쓴다."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, key: str) -> Optional[str]:
        row = await self._session.get(AppSetting, key)
        return row.value if row else None

    async def set(self, key: str, value: str, *, is_secret: bool = False) -> None:
        stmt = (
            sqlite_insert(AppSetting)
            .values(key=key, value=value, is_secret=is_secret, updated_at=utc_now())
            .on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value": value, "is_secret": is_secret, "updated_at": utc_now()},
            )
        )
        await self._session.execute(stmt)

    async def delete(self, key: str) -> bool:
        result = await self._session.execute(delete(AppSetting).where(AppSetting.key == key))
        return result.rowcount > 0

    async def updated_at(self, key: str) -> Optional[datetime]:
        row = await self._session.get(AppSetting, key)
        return row.updated_at if row else None
