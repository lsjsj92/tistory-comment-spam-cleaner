# app/infrastructure/db/models.py
"""SQLAlchemy ORM 매핑.

도메인 dataclass 와 분리해 두어, 저장 스키마가 바뀌어도 업무 규칙 코드가 영향을
받지 않게 한다. 시각 컬럼은 모두 :class:`UtcDateTime` 을 쓴다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ...domain.enums import (
    CommentStatus,
    JobItemStatus,
    JobStatus,
    JobType,
    SpamLevel,
)
from .base import Base, UtcDateTime


class Target(Base):
    """수집 대상 게시글."""

    __tablename__ = "targets"

    entry_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(512))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    comment_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_collected_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Comment(Base):
    """수집된 댓글 1건."""

    __tablename__ = "comments"
    __table_args__ = (
        Index("ix_comments_entry_written", "entry_id", "written_at"),
        Index("ix_comments_status_score", "status", "spam_score"),
        Index("ix_comments_nickname", "nickname"),
    )

    comment_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    entry_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    nickname: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    written_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    written_ts: Mapped[Optional[int]] = mapped_column(Integer)

    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 블로그 운영자가 직접 쓴 댓글. 규칙 엔진이 항상 화이트리스트로 처리한다.
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_admin_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    spam_score: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spam_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default=SpamLevel.NORMAL.value
    )
    spam_reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    whitelisted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CommentStatus.ACTIVE.value
    )
    collected_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    deleted_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    last_error: Mapped[Optional[str]] = mapped_column(Text)


class Job(Base):
    """백그라운드 작업."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False, default=JobType.COLLECT.value)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JobStatus.PENDING.value
    )

    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    backup_path: Mapped[Optional[str]] = mapped_column(String(1024))
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    finished_at: Mapped[Optional[datetime]] = mapped_column(UtcDateTime)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    items: Mapped[list["JobItem"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="noload"
    )


class JobItem(Base):
    """작업이 처리하는 개별 항목. 재개와 감사의 단위다."""

    __tablename__ = "job_items"
    __table_args__ = (
        Index("ix_job_items_job_status", "job_id", "status"),
        Index("uq_job_items_job_comment", "job_id", "comment_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
    )
    comment_id: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=JobItemStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    http_status: Mapped[Optional[int]] = mapped_column(Integer)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="items")


class AuditLog(Base):
    """되돌릴 수 없는 조작의 기록."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now()
    )


class AppSetting(Base):
    """런타임에 저장해야 하는 값. 현재는 세션 쿠키와 그 진단 결과뿐이다.

    `.env` 로 관리하는 설정은 여기에 넣지 않는다. 그래야 `.env` 가 유일한
    기본값 소스로 유지된다.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )
