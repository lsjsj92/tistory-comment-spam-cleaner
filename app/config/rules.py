# app/config/rules.py
"""`config/rules.yaml` 스키마와 로더.

규칙은 코드 배포 없이 바꿀 수 있어야 하므로 파일로 관리한다. 반대로 잘못된 규칙
하나가 4천 건이 넘는 댓글을 통째로 스팸으로 만들 수 있으므로, 로드 시점에
가능한 모든 오류를 잡아낸다.

- 정규식은 이 모듈에서 미리 컴파일한다. 평가 루프에서 컴파일 오류가 나면
  이미 되돌릴 수 없는 판정이 섞인 뒤이기 때문이다.
- 조건이 하나도 없는 규칙은 모든 댓글에 가중치를 주므로 오류로 막는다.
- 알 수 없는 키는 오류로 막는다. `content_regexp` 같은 오타가 조용히 무시되면
  사용자는 규칙이 동작한다고 믿게 된다.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from ..domain.errors import ConfigurationError
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.timeutils import parse_user_datetime

logger = get_logger(__name__)

# 규칙 파일이 없을 때 사용하는 기본 임계값. 어떤 규칙도 적용하지 않는다.
DEFAULT_SUSPICIOUS_THRESHOLD = 40
DEFAULT_SPAM_THRESHOLD = 70
DEFAULT_RULES_VERSION = 1

# 원자적 저장에 쓰는 임시 파일 접두사와 접미사
_TEMP_PREFIX = ".rules-"
_TEMP_SUFFIX = ".yaml.tmp"

# 저장할 때 강제하는 줄바꿈. Windows 에서 편집해도 파일이 일관되게 유지된다.
_FILE_NEWLINE = "\n"


def _compile_pattern(pattern: str, *, where: str) -> re.Pattern[str]:
    """정규식을 컴파일한다. 실패하면 어디가 잘못됐는지 알려준다.

    Args:
        pattern: 정규식 문자열.
        where: 오류 메시지에 넣을 위치 설명. 예: ``규칙 'link-spam' 의 content_regex``.

    Raises:
        ConfigurationError: 컴파일할 수 없는 패턴인 경우.
    """
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigurationError(
            f"{where} 정규식이 잘못되었습니다: {pattern!r} ({exc})"
        ) from exc


class WrittenBetween(BaseModel):
    """작성 시각 범위 조건. 두 값 모두 한국시간 문자열이다."""

    model_config = ConfigDict(extra="forbid")

    start: Optional[str] = None
    end: Optional[str] = None

    @model_validator(mode="after")
    def _validate_bounds(self) -> "WrittenBetween":
        """최소 한쪽 경계는 있어야 하고, 형식은 지금 검증한다."""
        if self.start is None and self.end is None:
            raise ValueError("written_between 은 start 또는 end 중 하나 이상이 필요합니다.")
        for field_name, value in (("start", self.start), ("end", self.end)):
            if value is None:
                continue
            try:
                parse_user_datetime(value)
            except ValueError as exc:
                raise ValueError(f"written_between.{field_name} 형식이 잘못되었습니다: {exc}") from exc
        return self


class RuleCondition(BaseModel):
    """규칙 1건의 판정 조건. 지정된 항목은 모두 AND 로 결합된다."""

    model_config = ConfigDict(extra="forbid")

    written_between: Optional[WrittenBetween] = None
    nickname_regex: Optional[str] = None
    content_regex: Optional[str] = None
    content_equals: Optional[str] = None
    nickname_equals: Optional[str] = None
    same_nickname_count_gte: Optional[int] = Field(default=None, ge=1)
    comments_per_minute_gte: Optional[int] = Field(default=None, ge=1)
    is_secret: Optional[bool] = None
    is_reply: Optional[bool] = None

    def is_empty(self) -> bool:
        """조건이 하나도 지정되지 않았는지 여부."""
        return all(
            getattr(self, name) is None for name in type(self).model_fields
        )


class Rule(BaseModel):
    """가중치를 가진 판정 규칙 1건."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    description: str = ""
    weight: int = Field(ge=1)
    when: RuleCondition

    # 평가 루프에서 재컴파일하지 않도록 로드 시점에 만들어 둔다.
    _nickname_pattern: Optional[re.Pattern[str]] = PrivateAttr(default=None)
    _content_pattern: Optional[re.Pattern[str]] = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _prepare(self) -> "Rule":
        """조건 유무를 확인하고 정규식을 미리 컴파일한다."""
        if self.when.is_empty():
            raise ValueError(
                f"규칙 '{self.id}' 의 when 이 비어 있습니다. "
                "조건이 없는 규칙은 모든 댓글에 점수를 주므로 허용하지 않습니다."
            )
        if self.when.nickname_regex is not None:
            self._nickname_pattern = _compile_pattern(
                self.when.nickname_regex, where=f"규칙 '{self.id}' 의 nickname_regex"
            )
        if self.when.content_regex is not None:
            self._content_pattern = _compile_pattern(
                self.when.content_regex, where=f"규칙 '{self.id}' 의 content_regex"
            )
        return self

    @property
    def nickname_pattern(self) -> Optional[re.Pattern[str]]:
        """사전 컴파일된 닉네임 정규식. 조건이 없으면 None."""
        return self._nickname_pattern

    @property
    def content_pattern(self) -> Optional[re.Pattern[str]]:
        """사전 컴파일된 본문 정규식. 조건이 없으면 None."""
        return self._content_pattern


class Thresholds(BaseModel):
    """점수를 등급으로 바꾸는 경계값."""

    model_config = ConfigDict(extra="forbid")

    suspicious: int = Field(default=DEFAULT_SUSPICIOUS_THRESHOLD, ge=0)
    spam: int = Field(default=DEFAULT_SPAM_THRESHOLD, ge=0)

    @model_validator(mode="after")
    def _validate_order(self) -> "Thresholds":
        if self.spam < self.suspicious:
            raise ValueError(
                f"thresholds.spam({self.spam}) 은 thresholds.suspicious({self.suspicious}) "
                "이상이어야 합니다."
            )
        return self


class Whitelist(BaseModel):
    """점수와 무관하게 삭제 대상에서 제외할 대상.

    블로그 운영자 본인의 댓글은 규칙 엔진이 항상 자동 보호하므로 여기에 적지 않아도 된다.
    """

    model_config = ConfigDict(extra="forbid")

    nicknames: list[str] = Field(default_factory=list)
    nickname_regex: list[str] = Field(default_factory=list)
    comment_ids: list[int] = Field(default_factory=list)

    _nickname_patterns: tuple[re.Pattern[str], ...] = PrivateAttr(default=())

    @model_validator(mode="after")
    def _compile_patterns(self) -> "Whitelist":
        self._nickname_patterns = tuple(
            _compile_pattern(pattern, where=f"whitelist.nickname_regex[{index}] 의")
            for index, pattern in enumerate(self.nickname_regex)
        )
        return self

    @property
    def nickname_patterns(self) -> tuple[re.Pattern[str], ...]:
        """사전 컴파일된 화이트리스트 닉네임 정규식 목록."""
        return self._nickname_patterns


class RulesConfig(BaseModel):
    """`rules.yaml` 전체 구조."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=DEFAULT_RULES_VERSION, ge=1)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    whitelist: Whitelist = Field(default_factory=Whitelist)
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> "RulesConfig":
        """규칙 id 중복을 막는다. 화면에서 근거를 식별하는 키이기 때문이다."""
        seen: set[str] = set()
        duplicated: list[str] = []
        for rule in self.rules:
            if rule.id in seen:
                duplicated.append(rule.id)
            seen.add(rule.id)
        if duplicated:
            raise ValueError(f"규칙 id 가 중복되었습니다: {sorted(set(duplicated))}")
        return self


def default_rules() -> RulesConfig:
    """규칙 파일이 없을 때 쓰는 안전한 기본값. 어떤 댓글에도 점수를 주지 않는다."""
    return RulesConfig()


def parse_rules_yaml(raw_yaml: str) -> RulesConfig:
    """YAML 원문을 검증된 설정 객체로 바꾼다.

    Raises:
        ConfigurationError: YAML 문법, 스키마, 정규식 중 하나라도 잘못된 경우.
    """
    try:
        data: Any = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"규칙 YAML 문법이 잘못되었습니다: {exc}") from exc

    if data is None:
        # 빈 파일은 규칙 없음으로 본다.
        data = {}
    if not isinstance(data, dict):
        raise ConfigurationError(
            f"규칙 파일의 최상위는 매핑이어야 하지만 {type(data).__name__} 입니다."
        )

    try:
        return RulesConfig.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(f"규칙 파일 스키마가 잘못되었습니다: {exc}") from exc


def load_rules(path: Path) -> RulesConfig:
    """규칙 파일을 읽어 검증한다.

    파일이 없으면 규칙이 없는 기본값을 반환한다. 규칙 파일이 사라졌다고 해서
    서비스가 기동조차 못 하면 오히려 사용자가 문제를 고칠 방법이 없어진다.

    Raises:
        ConfigurationError: 파일은 있으나 읽거나 해석할 수 없는 경우.
    """
    if not path.exists():
        logger.warning("규칙 파일이 없어 기본값을 사용합니다: %s", path)
        return default_rules()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"규칙 파일을 읽을 수 없습니다: {path} ({exc})") from exc
    return parse_rules_yaml(raw)


def read_rules_yaml(path: Path) -> str:
    """편집 화면에 보여줄 원문. 파일이 없으면 빈 문자열을 돌려준다."""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"규칙 파일을 읽을 수 없습니다: {path} ({exc})") from exc


def save_rules_yaml(path: Path, raw_yaml: str) -> RulesConfig:
    """원문 YAML 을 검증한 뒤에만 파일에 반영한다.

    검증에 실패하면 기존 파일은 손대지 않는다. 편집 실수로 유일한 규칙 파일이
    깨지면 판정 기준 전체를 잃기 때문이다. 쓰기는 임시 파일 생성 후 교체하는
    방식이라 중간에 프로세스가 죽어도 반쪽짜리 파일이 남지 않는다.

    Returns:
        저장된 내용을 파싱한 설정 객체.

    Raises:
        ConfigurationError: 검증 실패 또는 쓰기 실패.
    """
    config = parse_rules_yaml(raw_yaml)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp_name = tempfile.mkstemp(
            prefix=_TEMP_PREFIX, suffix=_TEMP_SUFFIX, dir=str(path.parent)
        )
    except OSError as exc:
        raise ConfigurationError(f"규칙 파일을 저장할 수 없습니다: {path} ({exc})") from exc

    temp_path = Path(temp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline=_FILE_NEWLINE) as stream:
            stream.write(raw_yaml)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        temp_path.unlink(missing_ok=True)
        raise ConfigurationError(f"규칙 파일을 저장할 수 없습니다: {path} ({exc})") from exc

    logger.info("규칙 파일을 갱신했습니다: %s (규칙 %d개)", path, len(config.rules))
    return config
