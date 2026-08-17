# app/api/routes/backups.py
"""백업 파일 목록과 다운로드 라우트."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from ...domain.errors import ValidationError
from ...infrastructure.db.repositories import CommentRepository
from ...infrastructure.timeutils import isoformat_local
from ...services.backup import BackupService
from ..deps import AppContext, get_context
from ..schemas import (
    BackupExportIn,
    BackupExportOut,
    BackupItemOut,
    BackupListOut,
)

router = APIRouter(prefix="/api/backups", tags=["backups"])


def _service(context: AppContext) -> BackupService:
    return BackupService(
        database=context.database,
        backup_dir=context.settings.backup_path,
        tz_name=context.settings.timezone,
    )


@router.get("", response_model=BackupListOut)
async def list_backups(context: AppContext = Depends(get_context)) -> BackupListOut:
    """생성된 백업 파일 목록을 최신순으로 돌려준다."""
    items = _service(context).list_backups()
    return BackupListOut(
        items=[
            BackupItemOut(
                name=item.name,
                size=item.size,
                created_at=isoformat_local(
                    item.created_at, tz_name=context.settings.timezone
                ),
            )
            for item in items
        ]
    )


@router.post("/export", response_model=BackupExportOut)
async def export_backup(
    payload: BackupExportIn, context: AppContext = Depends(get_context)
) -> BackupExportOut:
    """선택한 댓글을 JSON 과 CSV 로 내려받을 수 있게 저장한다.

    삭제와 무관한 순수 내보내기다. 삭제 작업이 만드는 백업과 같은 형식이지만,
    실제로 지우지 않고 기록만 남기고 싶을 때 쓴다.
    """
    settings = context.settings
    if (payload.comment_ids is None) == (payload.filter is None):
        raise ValidationError("comment_ids 와 filter 중 정확히 하나만 지정해야 합니다.")

    if payload.filter is not None:
        try:
            criteria = payload.filter.to_domain(tz_name=settings.timezone)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        async with context.database.session() as session:
            comment_ids = await CommentRepository(session).ids_for(criteria)
    else:
        comment_ids = list(dict.fromkeys(payload.comment_ids or ()))

    if not comment_ids:
        raise ValidationError("내보낼 댓글이 없습니다. 조건을 확인하세요.")

    result = await _service(context).export(comment_ids, label=payload.label)
    return BackupExportOut(
        json_file=result.json_path.name,
        csv_file=result.csv_path.name,
        count=result.count,
        created_at=isoformat_local(result.created_at, tz_name=settings.timezone),
    )


@router.get("/{name}")
async def download_backup(name: str, context: AppContext = Depends(get_context)) -> FileResponse:
    """백업 파일을 내려받는다.

    경로 탈출 차단과 존재 여부 확인은 :meth:`BackupService.resolve_backup` 이 담당한다.
    """
    path = _service(context).resolve_backup(name)
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")
