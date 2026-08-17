# app/services/backup.py
"""삭제 전 백업 서비스.

댓글 삭제는 되돌릴 수 없다. 그래서 이 모듈은 "백업이 확실히 만들어졌다" 를
호출자에게 보장하는 것을 유일한 책임으로 삼는다.

- JSON 과 CSV 를 동시에 만든다. JSON 은 기계 판독용, CSV 는 Excel 확인용이다.
- 만들고 끝내지 않고 다시 읽어 건수를 확인한다. 디스크가 가득 찼거나 권한이
  없으면 여기서 걸린다.
- 도중에 실패하면 반쪽짜리 파일을 남기지 않고 지운다. 반쪽 백업이 남으면
  사용자는 백업이 있다고 착각한 채 삭제를 진행하게 된다.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..domain.errors import BackupError, NotFoundError, ValidationError
from ..infrastructure.db.models import Comment
from ..infrastructure.db.repositories import CommentRepository
from ..infrastructure.db.session import Database
from ..infrastructure.logging_setup import get_logger
from ..infrastructure.timeutils import isoformat_local, to_local, utc_now

logger = get_logger(__name__)

# 파일명에 붙는 타임스탬프 형식(한국시간 기준)
FILENAME_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"

# 백업 파일 확장자. 목록과 다운로드 모두 이 목록만 취급한다.
JSON_SUFFIX = ".json"
CSV_SUFFIX = ".csv"
BACKUP_SUFFIXES = (JSON_SUFFIX, CSV_SUFFIX)

# CSV 는 Windows Excel 이 UTF-8 을 인식하도록 BOM 을 붙인다.
CSV_ENCODING = "utf-8-sig"
JSON_ENCODING = "utf-8"
# 줄바꿈은 csv 모듈이 직접 제어해야 하므로 파일 쪽에서는 변환하지 않는다.
CSV_NEWLINE = ""

# 파일명에 쓸 수 있는 문자 외에는 모두 치환한다. 점을 허용하지 않으므로 ".." 이 남지 않는다.
_UNSAFE_LABEL_PATTERN = re.compile(r"[^0-9A-Za-z가-힣_-]+")
DEFAULT_LABEL = "backup"
MAX_LABEL_LENGTH = 64

# 같은 초에 두 번 요청해도 앞선 백업을 덮어쓰지 않도록 붙이는 일련번호의 상한
MAX_FILENAME_ATTEMPTS = 100

# CSV 헤더: (댓글 필드, 화면 표기)
CSV_COLUMNS: tuple[tuple[str, str], ...] = (
    ("comment_id", "댓글번호"),
    ("entry_id", "게시글번호"),
    ("nickname", "닉네임"),
    ("content", "본문"),
    ("written_at", "작성시각"),
    ("written_ts", "작성epoch"),
    ("is_secret", "비밀댓글"),
    ("is_reply", "대댓글"),
    ("is_admin", "운영자작성"),
    ("is_admin_deleted", "관리자삭제본문"),
    ("spam_score", "스팸점수"),
    ("spam_level", "스팸등급"),
    ("spam_reasons", "판정근거"),
    ("whitelisted", "화이트리스트"),
    ("status", "상태"),
    ("collected_at", "수집시각"),
    ("deleted_at", "삭제시각"),
    ("last_error", "마지막오류"),
)

# CSV 의 불리언 표기. Excel 에서 그대로 읽히도록 한글로 쓴다.
_BOOL_TRUE = "예"
_BOOL_FALSE = "아니오"

# 판정 근거를 CSV 한 칸에 넣을 때 쓰는 구분자
_REASON_SEPARATOR = ", "


@dataclass(frozen=True)
class BackupResult:
    """백업 1회의 결과.

    Attributes:
        json_path: 생성된 JSON 파일 경로.
        csv_path: 생성된 CSV 파일 경로.
        count: 백업된 댓글 수.
        created_at: 생성 시각(UTC aware).
    """

    json_path: Path
    csv_path: Path
    count: int
    created_at: datetime


@dataclass(frozen=True)
class BackupInfo:
    """백업 디렉터리에 있는 파일 1건의 요약.

    Attributes:
        name: 파일 이름. 다운로드 요청에 그대로 쓴다.
        size: 바이트 크기.
        created_at: 파일 수정 시각(UTC aware).
    """

    name: str
    size: int
    created_at: datetime


def normalize_label(label: str) -> str:
    """파일명에 쓸 수 있도록 라벨을 정규화한다.

    경로 구분자, 상위 이동, 널 문자 등은 모두 제거한다. 라벨은 사용자 입력이나
    작업 이름에서 오므로 그대로 파일명에 넣으면 백업 디렉터리 밖에 파일을 만들 수 있다.
    """
    cleaned = _UNSAFE_LABEL_PATTERN.sub("_", label.strip())
    cleaned = cleaned.strip("_-")[:MAX_LABEL_LENGTH].strip("_-")
    return cleaned or DEFAULT_LABEL


class BackupService:
    """댓글 백업 파일의 생성, 목록, 조회를 담당한다."""

    def __init__(self, database: Database, backup_dir: Path, tz_name: str) -> None:
        self._database = database
        self._backup_dir = backup_dir
        self._tz_name = tz_name

    @property
    def backup_dir(self) -> Path:
        """백업 파일이 저장되는 디렉터리."""
        return self._backup_dir

    async def export(self, comment_ids: Sequence[int], *, label: str) -> BackupResult:
        """지정한 댓글을 JSON 과 CSV 로 저장한다.

        Args:
            comment_ids: 백업할 댓글 번호 목록.
            label: 파일명 앞에 붙일 이름. 경로 조작 문자는 제거된다.

        Returns:
            생성된 파일 경로와 건수.

        Raises:
            BackupError: 대상이 없거나, 저장에 실패했거나, 검증에 실패한 경우.
        """
        if not comment_ids:
            raise BackupError("백업할 댓글이 없습니다. 삭제를 진행하지 않습니다.")

        rows = await self._fetch(comment_ids)
        if not rows:
            raise BackupError(
                f"백업 대상 댓글을 찾을 수 없습니다. 요청 {len(comment_ids)}건 중 0건 조회."
            )
        missing = len(set(comment_ids)) - len(rows)
        if missing > 0:
            logger.warning("백업 대상 중 %d건은 데이터베이스에 없어 제외되었습니다.", missing)

        created_at = utc_now()
        safe_label = normalize_label(label)
        payload = [self._to_dict(row) for row in rows]

        json_path, csv_path = self._reserve_paths(safe_label, created_at)
        try:
            # 파일 쓰기와 재검증은 동기 작업이다. 5천 건 규모에서 1초 넘게 걸리는데
            # 이벤트 루프에서 직접 돌리면 그동안 진행률 전송과 요청 처리가 모두 멈춘다.
            await asyncio.to_thread(
                self._write_files, json_path, csv_path, payload, safe_label, created_at
            )
        except Exception as exc:
            self._cleanup(json_path, csv_path)
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"백업 파일 생성에 실패했습니다: {exc}") from exc

        logger.info("백업을 생성했습니다: %s, %s (%d건)", json_path.name, csv_path.name, len(payload))
        return BackupResult(
            json_path=json_path, csv_path=csv_path, count=len(payload), created_at=created_at
        )

    def _write_files(
        self,
        json_path: Path,
        csv_path: Path,
        payload: list,
        label: str,
        created_at: datetime,
    ) -> None:
        """두 파일을 쓰고 즉시 다시 읽어 건수를 대조한다. 별도 스레드에서 실행된다."""
        self._write_json(json_path, payload, label=label, created_at=created_at)
        self._write_csv(csv_path, payload)
        self._verify(json_path, csv_path, count=len(payload))

    def list_backups(self) -> list[BackupInfo]:
        """백업 디렉터리의 파일 목록. 최신순으로 정렬한다."""
        if not self._backup_dir.exists():
            return []
        infos: list[BackupInfo] = []
        for path in self._backup_dir.iterdir():
            if not path.is_file() or path.suffix.lower() not in BACKUP_SUFFIXES:
                continue
            stat = path.stat()
            infos.append(
                BackupInfo(
                    name=path.name,
                    size=stat.st_size,
                    created_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                )
            )
        infos.sort(key=lambda info: (info.created_at, info.name), reverse=True)
        return infos

    def resolve_backup(self, name: str) -> Path:
        """다운로드할 백업 파일의 실제 경로를 돌려준다.

        이름은 외부 입력이므로 백업 디렉터리 바로 아래의 백업 파일만 통과시킨다.
        심볼릭 링크를 통한 우회까지 막기 위해 정규화한 경로로 비교한다.

        Raises:
            ValidationError: 이름이 비었거나 백업 디렉터리를 벗어나는 경우.
            NotFoundError: 해당 파일이 없는 경우.
        """
        cleaned = name.strip()
        if not cleaned:
            raise ValidationError("백업 파일 이름이 비어 있습니다.")
        if cleaned != Path(cleaned).name or any(part in cleaned for part in ("/", "\\", "\x00")):
            raise ValidationError(f"허용되지 않는 백업 파일 이름입니다: {name}")
        if Path(cleaned).suffix.lower() not in BACKUP_SUFFIXES:
            raise ValidationError(f"백업 파일 형식이 아닙니다: {name}")

        base = self._backup_dir.resolve()
        candidate = (self._backup_dir / cleaned).resolve()
        if candidate.parent != base:
            raise ValidationError(f"백업 디렉터리를 벗어나는 경로입니다: {name}")
        if not candidate.is_file():
            raise NotFoundError(f"백업 파일이 없습니다: {name}")
        return candidate

    # ------------------------------------------------------------------
    # 내부 구현
    # ------------------------------------------------------------------
    async def _fetch(self, comment_ids: Sequence[int]) -> list[Comment]:
        """백업 대상 댓글을 읽는다."""
        async with self._database.session() as session:
            return await CommentRepository(session).get_many(list(comment_ids))

    def _to_dict(self, row: Comment) -> dict[str, Any]:
        """ORM 행을 직렬화 가능한 사전으로 바꾼다. 시각은 한국시간 ISO 8601 이다."""
        return {
            "comment_id": row.comment_id,
            "entry_id": row.entry_id,
            "nickname": row.nickname,
            "content": row.content,
            "written_at": isoformat_local(row.written_at, tz_name=self._tz_name),
            "written_ts": row.written_ts,
            "is_secret": row.is_secret,
            "is_reply": row.is_reply,
            "is_admin": row.is_admin,
            "is_admin_deleted": row.is_admin_deleted,
            "spam_score": row.spam_score,
            "spam_level": row.spam_level,
            "spam_reasons": list(row.spam_reasons or []),
            "whitelisted": row.whitelisted,
            "status": row.status,
            "collected_at": isoformat_local(row.collected_at, tz_name=self._tz_name),
            "deleted_at": isoformat_local(row.deleted_at, tz_name=self._tz_name),
            "last_error": row.last_error,
        }

    def _reserve_paths(self, label: str, created_at: datetime) -> tuple[Path, Path]:
        """겹치지 않는 JSON/CSV 경로 쌍을 정한다.

        Raises:
            BackupError: 디렉터리를 만들 수 없거나 이름이 계속 겹치는 경우.
        """
        try:
            self._backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupError(f"백업 디렉터리를 만들 수 없습니다: {self._backup_dir} ({exc})") from exc

        stamp = to_local(created_at, tz_name=self._tz_name).strftime(FILENAME_TIMESTAMP_FORMAT)
        for attempt in range(MAX_FILENAME_ATTEMPTS):
            suffix = "" if attempt == 0 else f"_{attempt}"
            stem = f"{label}_{stamp}{suffix}"
            json_path = self._backup_dir / f"{stem}{JSON_SUFFIX}"
            csv_path = self._backup_dir / f"{stem}{CSV_SUFFIX}"
            if not json_path.exists() and not csv_path.exists():
                return (json_path, csv_path)
        raise BackupError(f"백업 파일 이름을 정할 수 없습니다: {label}_{stamp}")

    def _write_json(
        self, path: Path, payload: list[dict[str, Any]], *, label: str, created_at: datetime
    ) -> None:
        """메타와 댓글 전체를 JSON 으로 저장한다."""
        document = {
            "meta": {
                "created_at": isoformat_local(created_at, tz_name=self._tz_name),
                "label": label,
                "count": len(payload),
            },
            "comments": payload,
        }
        with path.open("w", encoding=JSON_ENCODING) as stream:
            json.dump(document, stream, ensure_ascii=False, indent=2)

    def _write_csv(self, path: Path, payload: list[dict[str, Any]]) -> None:
        """사람이 확인할 CSV 를 저장한다. Excel 한글 호환을 위해 BOM 을 포함한다."""
        with path.open("w", encoding=CSV_ENCODING, newline=CSV_NEWLINE) as stream:
            writer = csv.writer(stream)
            writer.writerow([header for _, header in CSV_COLUMNS])
            for item in payload:
                writer.writerow([self._csv_value(item[field]) for field, _ in CSV_COLUMNS])

    @staticmethod
    def _csv_value(value: Any) -> str:
        """CSV 한 칸에 넣을 문자열로 바꾼다."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return _BOOL_TRUE if value else _BOOL_FALSE
        if isinstance(value, (list, tuple)):
            return _REASON_SEPARATOR.join(str(item) for item in value)
        return str(value)

    def _verify(self, json_path: Path, csv_path: Path, *, count: int) -> None:
        """저장한 파일을 다시 읽어 건수가 맞는지 확인한다.

        Raises:
            BackupError: 파일을 읽을 수 없거나 건수가 다른 경우.
        """
        try:
            document = json.loads(json_path.read_text(encoding=JSON_ENCODING))
        except (OSError, ValueError) as exc:
            raise BackupError(f"백업 JSON 을 다시 읽을 수 없습니다: {json_path} ({exc})") from exc

        stored = document.get("comments")
        if not isinstance(stored, list) or len(stored) != count:
            raise BackupError(
                f"백업 JSON 건수가 일치하지 않습니다: 기대 {count}건, 실제 "
                f"{len(stored) if isinstance(stored, list) else '알 수 없음'}건"
            )

        try:
            with csv_path.open("r", encoding=CSV_ENCODING, newline=CSV_NEWLINE) as stream:
                rows = sum(1 for _ in csv.reader(stream))
        except OSError as exc:
            raise BackupError(f"백업 CSV 를 다시 읽을 수 없습니다: {csv_path} ({exc})") from exc
        if rows != count + 1:  # 헤더 1줄 포함
            raise BackupError(f"백업 CSV 건수가 일치하지 않습니다: 기대 {count}건, 실제 {rows - 1}건")

    @staticmethod
    def _cleanup(*paths: Path) -> None:
        """실패한 백업의 잔여 파일을 지운다."""
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 삭제조차 실패하는 드문 경우
                logger.warning("실패한 백업 파일을 정리하지 못했습니다: %s", path)
