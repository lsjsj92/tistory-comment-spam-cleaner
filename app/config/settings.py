# app/config/settings.py
"""애플리케이션 설정 단일 소스.

모든 설정 값은 이 모듈의 :class:`Settings` 를 통해서만 접근한다.
코드 어디에서도 호스트, 경로, 동시성 같은 값을 상수로 박아두지 않는다.
`.env.example` 의 키 목록과 :class:`Settings` 필드는 항상 1:1로 대응하며
`tests/test_config_sync.py` 가 이를 기계적으로 검증한다.
"""

from __future__ import annotations

import base64
import os
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 프로젝트 루트: app/config/settings.py 기준 두 단계 위
BASE_DIR = Path(__file__).resolve().parents[2]

ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE_FILE = BASE_DIR / ".env.example"

# .env 에서 설정 키를 구분하기 위한 접두사
ENV_PREFIX = "APP_"


class Settings(BaseSettings):
    """`.env` 로부터 로드되는 정적 설정.

    실행 중 변경되는 값(작업별 RPS, 드라이런 여부 등)은 여기에 두지 않고
    작업 생성 시 파라미터로 전달한다. 그래야 `.env` 가 유일한 기본값 소스로 유지된다.
    """

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        env_prefix=ENV_PREFIX,
        extra="ignore",
        case_sensitive=False,
    )

    # --- 웹 서버 ---------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = Field(default=8765, ge=1, le=65535)
    open_browser: bool = True

    # --- 로깅 -------------------------------------------------------------
    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    log_retention_days: int = Field(default=14, ge=1, le=365)
    timezone: str = "Asia/Seoul"

    # --- 보안 -------------------------------------------------------------
    secret_key: str = ""
    auth_enabled: bool = False
    auth_username: str = "admin"
    auth_password: str = ""
    session_max_age: int = Field(default=43200, ge=60)

    # --- 대상 블로그 -------------------------------------------------------
    blog_url: str = "https://example.tistory.com"
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    # --- 데이터 경로 -------------------------------------------------------
    data_dir: Path = Path("data")
    database_url: str = "sqlite+aiosqlite:///data/comments.db"
    backup_dir: Path = Path("data/backups")
    config_dir: Path = Path("config")

    # --- HTTP 클라이언트 ---------------------------------------------------
    http_timeout: float = Field(default=20.0, gt=0)
    http_max_retries: int = Field(default=5, ge=0, le=20)
    http_backoff_base: float = Field(default=0.5, gt=0)
    http_backoff_max: float = Field(default=30.0, gt=0)

    # --- 댓글 수집 ---------------------------------------------------------
    collect_concurrency: int = Field(default=3, ge=1, le=32)
    collect_rps: float = Field(default=6.0, gt=0, le=100)

    # --- 댓글 삭제 ---------------------------------------------------------
    delete_concurrency: int = Field(default=3, ge=1, le=32)
    delete_rps: float = Field(default=4.0, gt=0, le=100)
    delete_dry_run: bool = True
    circuit_breaker_threshold: int = Field(default=10, ge=1, le=1000)
    backup_before_delete: bool = True

    # --- 주기 모니터링 -----------------------------------------------------
    monitor_enabled: bool = False
    monitor_interval_minutes: int = Field(default=60, ge=1, le=10080)

    # --- 화면 -------------------------------------------------------------
    page_size: int = Field(default=50, ge=10, le=500)

    # ------------------------------------------------------------------
    # 검증 및 정규화
    # ------------------------------------------------------------------
    @field_validator("blog_url")
    @classmethod
    def _normalize_blog_url(cls, value: str) -> str:
        """블로그 주소에서 끝의 슬래시를 제거하고 스킴을 보정한다."""
        value = value.strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if level not in allowed:
            raise ValueError(f"APP_LOG_LEVEL 은 {sorted(allowed)} 중 하나여야 합니다: {value}")
        return level

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - 환경 의존
            raise ValueError(f"알 수 없는 시간대입니다: {value}") from exc
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> "Settings":
        """설정 간 상호 모순을 기동 시점에 차단한다."""
        if self.http_backoff_max < self.http_backoff_base:
            raise ValueError("APP_HTTP_BACKOFF_MAX 는 APP_HTTP_BACKOFF_BASE 이상이어야 합니다.")
        if self.auth_enabled and not self.auth_password:
            raise ValueError(
                "APP_AUTH_ENABLED=true 인 경우 APP_AUTH_PASSWORD 를 반드시 설정해야 합니다."
            )
        if self.host not in {"127.0.0.1", "localhost", "::1"} and not self.auth_enabled:
            raise ValueError(
                "로컬 주소가 아닌 곳에 바인딩하려면 APP_AUTH_ENABLED=true 로 인증을 켜야 합니다. "
                "세션 쿠키가 저장되는 서비스이므로 무인증 외부 노출을 허용하지 않습니다."
            )
        return self

    # ------------------------------------------------------------------
    # 파생 값
    # ------------------------------------------------------------------
    def _resolve(self, path: Path) -> Path:
        """상대 경로는 프로젝트 루트를 기준으로 절대 경로화한다."""
        return path if path.is_absolute() else (BASE_DIR / path)

    @property
    def data_path(self) -> Path:
        return self._resolve(self.data_dir)

    @property
    def backup_path(self) -> Path:
        return self._resolve(self.backup_dir)

    @property
    def config_path(self) -> Path:
        return self._resolve(self.config_dir)

    @property
    def log_path(self) -> Path:
        return self._resolve(self.log_dir)

    @property
    def targets_file(self) -> Path:
        return self.config_path / "targets.yaml"

    @property
    def rules_file(self) -> Path:
        return self.config_path / "rules.yaml"

    @property
    def resolved_database_url(self) -> str:
        """SQLite 상대 경로를 프로젝트 루트 기준 절대 경로로 바꾼다.

        작업 디렉터리가 달라져도 항상 같은 DB 파일을 바라보게 하기 위함이다.
        """
        prefix = "sqlite+aiosqlite:///"
        if not self.database_url.startswith(prefix):
            return self.database_url
        raw = self.database_url[len(prefix) :]
        if raw.startswith(":memory:") or raw.startswith("/"):
            return self.database_url
        return prefix + self._resolve(Path(raw)).as_posix()

    @property
    def database_file(self) -> Optional[Path]:
        """SQLite 파일 경로. 메모리 DB이거나 다른 드라이버면 None."""
        prefix = "sqlite+aiosqlite:///"
        url = self.resolved_database_url
        if not url.startswith(prefix):
            return None
        raw = url[len(prefix) :]
        if raw.startswith(":memory:"):
            return None
        return Path(raw)

    def ensure_directories(self) -> None:
        """기동에 필요한 디렉터리를 만든다. 이미 있으면 아무 일도 하지 않는다."""
        for path in (self.data_path, self.backup_path, self.config_path, self.log_path):
            path.mkdir(parents=True, exist_ok=True)
        db_file = self.database_file
        if db_file is not None:
            db_file.parent.mkdir(parents=True, exist_ok=True)


def generate_secret_key() -> str:
    """Fernet 키로 바로 쓸 수 있는 32바이트 base64 문자열을 만든다."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def ensure_env_file() -> Path:
    """`.env` 가 없으면 `.env.example` 을 복사하고 비어 있는 비밀키를 채운다.

    최초 실행자가 별도 준비 없이 바로 기동할 수 있게 하기 위한 부트스트랩이다.
    이미 값이 채워진 키는 절대 덮어쓰지 않는다.
    """
    if not ENV_FILE.exists():
        if not ENV_EXAMPLE_FILE.exists():
            raise FileNotFoundError(
                f".env 와 .env.example 이 모두 없습니다: {ENV_EXAMPLE_FILE}"
            )
        ENV_FILE.write_text(
            ENV_EXAMPLE_FILE.read_text(encoding="utf-8"), encoding="utf-8"
        )

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    key_name = f"{ENV_PREFIX}SECRET_KEY"
    changed = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{key_name}=") and not stripped[len(key_name) + 1 :].strip():
            lines[index] = f"{key_name}={generate_secret_key()}"
            changed = True
            break
    else:
        if not any(line.strip().startswith(f"{key_name}=") for line in lines):
            lines.append(f"{key_name}={generate_secret_key()}")
            changed = True

    if changed:
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return ENV_FILE


def env_var_names() -> set[str]:
    """`Settings` 필드로부터 환경변수 이름 집합을 만든다. 동기화 검증에 쓰인다."""
    return {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """설정 싱글턴. 프로세스 수명 동안 한 번만 로드한다."""
    settings = Settings()
    if not settings.secret_key:
        # 테스트나 임시 실행처럼 .env 부트스트랩을 거치지 않은 경우의 안전망.
        # 프로세스가 끝나면 사라지므로 저장된 쿠키는 다음 기동에서 복호화되지 않는다.
        settings.secret_key = generate_secret_key()
    return settings


def reset_settings_cache() -> None:
    """테스트에서 환경변수를 바꾼 뒤 설정을 다시 읽기 위한 훅."""
    get_settings.cache_clear()


def current_env_overrides() -> dict[str, str]:
    """현재 프로세스 환경에 직접 지정된 APP_ 변수. 진단 화면 표시에 쓴다."""
    return {k: v for k, v in os.environ.items() if k.startswith(ENV_PREFIX)}
