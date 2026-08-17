# app/domain/errors.py
"""도메인 예외 계층.

외부 라이브러리 예외를 그대로 상위 계층으로 흘리지 않고 여기서 정의한 타입으로
번역한다. API 계층은 이 타입만 보고 HTTP 상태 코드를 결정한다.
"""

from __future__ import annotations

from typing import Optional

class AppError(Exception):
    """모든 애플리케이션 예외의 최상위 타입."""

    #: API 응답에 사용할 기본 HTTP 상태 코드
    status_code: int = 500

    def __init__(self, message: str, *, detail: Optional[object] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class ConfigurationError(AppError):
    """설정 파일이나 환경 변수가 잘못된 경우."""

    status_code = 500


class AuthenticationError(AppError):
    """세션 쿠키가 없거나 만료되어 소유자 권한을 쓸 수 없는 경우."""

    status_code = 401


class PermissionDeniedError(AppError):
    """로그인은 되어 있으나 해당 자원을 다룰 권한이 없는 경우."""

    status_code = 403


class NotFoundError(AppError):
    """요청한 자원이 존재하지 않는 경우."""

    status_code = 404


class ValidationError(AppError):
    """입력 값이 업무 규칙을 위반한 경우."""

    status_code = 400


class ConflictError(AppError):
    """현재 상태에서 수행할 수 없는 요청인 경우. 예: 실행 중인 작업을 다시 실행."""

    status_code = 409


class TistoryApiError(AppError):
    """티스토리 응답이 예상과 다른 경우.

    Attributes:
        http_status: 티스토리가 돌려준 HTTP 상태 코드.
        retryable: 재시도로 회복될 여지가 있는지 여부.
    """

    status_code = 502

    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        retryable: bool = False,
        detail: Optional[object] = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.http_status = http_status
        self.retryable = retryable


class RateLimitedError(TistoryApiError):
    """티스토리가 요청 속도를 제한한 경우."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None) -> None:
        super().__init__(message, http_status=429, retryable=True)
        self.retry_after = retry_after


class CircuitOpenError(AppError):
    """연속 실패로 서킷 브레이커가 열려 요청을 차단한 경우."""

    status_code = 503


class BackupError(AppError):
    """삭제 전 백업 생성에 실패한 경우. 이 예외가 발생하면 삭제를 진행하지 않는다."""

    status_code = 500
