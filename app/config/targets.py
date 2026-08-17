# app/config/targets.py
"""`targets.yaml` 로더와 스키마.

수집 대상 게시글 목록은 사람이 직접 열어 고칠 수 있어야 하므로 DB 가 아니라
YAML 파일을 원본으로 둔다. 이 모듈은 파일과 도메인 객체 사이의 번역만 담당하고
어떤 게시글을 수집할지에 대한 판단은 하지 않는다.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as SchemaValidationError

from ..domain.errors import ConfigurationError
from ..domain.models import TargetSpec
from ..infrastructure.logging_setup import get_logger

logger = get_logger(__name__)

# 현재 파일 스키마 버전. 구조를 바꾸면 올리고 마이그레이션을 붙인다.
TARGETS_SCHEMA_VERSION = 1

# 저장할 때 파일 맨 위에 남기는 안내. 사람이 직접 편집하는 파일이라 필수다.
FILE_HEADER = """# 수집 대상 게시글 목록
#
# entry: 게시글 번호 (필수). 주소가 https://blog.tistory.com/723 이면 723 이다.
# title: 화면에 표시할 이름 (선택). 비워두면 수집 시 블로그에서 가져온다.
#
# 웹 화면의 "게시글 관리"에서 추가/삭제하면 이 파일이 갱신된다.
# sitemap 전체 스캔으로 찾은 게시글은 이 파일에 남지 않고 데이터베이스에만 등록된다.
"""

# 원자적 쓰기에 쓰는 임시 파일 접미사
TEMP_SUFFIX = ".tmp"


class TargetEntry(BaseModel):
    """대상 파일의 항목 1개."""

    model_config = ConfigDict(extra="forbid")

    entry: int = Field(gt=0)
    title: Optional[str] = None

    @field_validator("title")
    @classmethod
    def _blank_title_is_none(cls, value: Optional[str]) -> Optional[str]:
        """빈 문자열은 "제목 없음"과 같은 뜻이므로 None 으로 통일한다."""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class TargetsConfig(BaseModel):
    """대상 파일 전체 구조."""

    model_config = ConfigDict(extra="forbid")

    version: int = TARGETS_SCHEMA_VERSION
    targets: list[TargetEntry] = Field(default_factory=list)


def load_targets(path: Path, blog_url: str) -> list[TargetSpec]:
    """대상 파일을 읽어 :class:`TargetSpec` 목록으로 바꾼다.

    파일이 없는 것은 오류가 아니다. 최초 실행자는 빈 목록에서 시작해 화면에서
    대상을 추가하면 된다.

    Args:
        path: `targets.yaml` 경로.
        blog_url: 게시글 주소를 만들 때 쓸 블로그 주소.

    Returns:
        entry 오름차순으로 정렬된 대상 목록.

    Raises:
        ConfigurationError: 파일을 읽지 못했거나 YAML 형식/스키마가 잘못된 경우.
    """
    if not path.exists():
        logger.info("대상 파일이 없어 빈 목록으로 시작합니다: %s", path)
        return []

    raw = _read_yaml(path)
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"대상 파일의 최상위는 매핑이어야 합니다: {path}", detail=type(raw).__name__
        )

    config = _validate(raw, path)
    if config.version != TARGETS_SCHEMA_VERSION:
        logger.warning(
            "대상 파일 버전이 %s 입니다. 이 프로그램은 %s 를 기준으로 동작합니다: %s",
            config.version,
            TARGETS_SCHEMA_VERSION,
            path,
        )

    base = blog_url.rstrip("/")
    entries = sorted(config.targets, key=lambda item: item.entry)
    return [
        TargetSpec(entry_id=item.entry, url=f"{base}/{item.entry}", title=item.title)
        for item in entries
    ]


def save_targets(path: Path, specs: Sequence[TargetSpec]) -> None:
    """대상 목록을 파일에 쓴다.

    쓰는 도중 프로그램이 죽어도 반쪽짜리 파일이 남지 않도록 같은 디렉터리에
    임시 파일을 만든 뒤 :func:`os.replace` 로 갈아끼운다.

    Args:
        path: `targets.yaml` 경로.
        specs: 저장할 대상 목록. 중복 entry 는 마지막 값만 남는다.
    """
    unique: dict[int, TargetSpec] = {spec.entry_id: spec for spec in specs}
    ordered = [unique[entry_id] for entry_id in sorted(unique)]
    document = FILE_HEADER + "\n" + _dump_yaml(ordered)

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + TEMP_SUFFIX)
    temp_path.write_text(document, encoding="utf-8")
    os.replace(temp_path, path)
    logger.info("대상 파일을 저장했습니다 (%d건): %s", len(ordered), path)


def _read_yaml(path: Path) -> object:
    """YAML 파일을 파이썬 객체로 읽는다."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigurationError(f"대상 파일을 열 수 없습니다: {path}", detail=str(exc)) from exc
    except yaml.YAMLError as exc:
        raise ConfigurationError(
            f"대상 파일의 YAML 형식이 잘못되었습니다: {path}", detail=str(exc)
        ) from exc


def _validate(raw: dict[str, object], path: Path) -> TargetsConfig:
    """읽어들인 매핑을 스키마로 검증한다."""
    try:
        return TargetsConfig.model_validate(raw)
    except SchemaValidationError as exc:
        raise ConfigurationError(
            f"대상 파일의 항목 형식이 올바르지 않습니다: {path}", detail=exc.errors()
        ) from exc


def _dump_yaml(specs: Sequence[TargetSpec]) -> str:
    """대상 목록을 YAML 본문 문자열로 만든다. 한글 제목을 그대로 저장한다."""
    payload = {
        "version": TARGETS_SCHEMA_VERSION,
        "targets": [_to_mapping(spec) for spec in specs],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _to_mapping(spec: TargetSpec) -> dict[str, object]:
    """저장용 매핑. 제목이 없으면 키 자체를 넣지 않아 파일을 간결하게 유지한다."""
    mapping: dict[str, object] = {"entry": spec.entry_id}
    if spec.title:
        mapping["title"] = spec.title
    return mapping
