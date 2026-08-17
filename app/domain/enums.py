# app/domain/enums.py
"""도메인 상태 열거형.

DB 컬럼과 API 응답에 그대로 쓰이므로 값 문자열을 바꾸면 마이그레이션이 필요하다.
"""

from __future__ import annotations

from enum import Enum


class CommentStatus(str, Enum):
    """수집된 댓글의 처리 상태."""

    ACTIVE = "active"          # 블로그에 남아 있음
    DELETING = "deleting"      # 삭제 작업이 진행 중
    DELETED = "deleted"        # 삭제 완료
    FAILED = "failed"          # 삭제 시도했으나 실패


class SpamLevel(str, Enum):
    """스팸 점수를 사람이 읽는 등급으로 환산한 값."""

    NORMAL = "normal"
    SUSPICIOUS = "suspicious"
    SPAM = "spam"


class JobType(str, Enum):
    """백그라운드 작업 종류."""

    COLLECT = "collect"
    DELETE = "delete"
    DISCOVER = "discover"


class JobStatus(str, Enum):
    """작업 수명 주기.

    PAUSED 는 서킷 브레이커 개방이나 프로세스 재시작으로 중단된 상태를 뜻하며
    사용자가 재개할 수 있다. CANCELLED 는 사용자가 명시적으로 중단한 상태다.
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        """더 이상 진행하지 않는 종료 상태인지 여부."""
        return self in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}

    @property
    def is_resumable(self) -> bool:
        """이어서 실행할 수 있는 상태인지 여부."""
        return self in {JobStatus.PENDING, JobStatus.PAUSED}


class JobItemStatus(str, Enum):
    """작업 단위 항목의 처리 상태."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"        # 화이트리스트 등으로 제외


class AuthState(str, Enum):
    """세션 쿠키 진단 결과."""

    UNKNOWN = "unknown"        # 아직 진단하지 않음
    MISSING = "missing"        # 쿠키가 등록되지 않음
    ANONYMOUS = "anonymous"    # 쿠키는 있으나 로그인 상태가 아님
    NOT_OWNER = "not_owner"    # 로그인했으나 해당 블로그의 소유자가 아님
    OWNER = "owner"            # 소유자 확인됨
