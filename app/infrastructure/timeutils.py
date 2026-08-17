# app/infrastructure/timeutils.py
"""시간대 변환 유틸리티.

저장은 UTC, 표시와 입력 해석은 사용자 시간대(기본 한국)로 통일한다. 이 규칙이
한 군데라도 깨지면 "8월 8일 20시 이후" 같은 조건이 조용히 아홉 시간 어긋난다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo
from typing import Optional, Union

# 티스토리 댓글 목록 HTML의 날짜 표기 형식 (분 단위까지)
TISTORY_DATE_FORMAT = "%Y.%m.%d %H:%M"

# 화면과 사용자 입력에서 쓰는 형식들. 앞에서부터 순서대로 시도한다.
_INPUT_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d",
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y.%m.%d",
)


@lru_cache(maxsize=8)
def get_zone(tz_name: str) -> ZoneInfo:
    """시간대 객체를 캐시해서 반환한다."""
    return ZoneInfo(tz_name)


def utc_now() -> datetime:
    """현재 시각을 timezone-aware UTC 로 반환한다."""
    return datetime.now(timezone.utc)


def to_utc(moment: datetime, *, tz_name: str = "Asia/Seoul") -> datetime:
    """naive datetime 은 사용자 시간대로 해석하고, aware 는 UTC 로 변환한다."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=get_zone(tz_name))
    return moment.astimezone(timezone.utc)


def to_local(moment: datetime, *, tz_name: str = "Asia/Seoul") -> datetime:
    """UTC(또는 naive UTC) 시각을 사용자 시간대로 변환한다."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(get_zone(tz_name))


def parse_tistory_datetime(text: str, *, tz_name: str = "Asia/Seoul") -> datetime:
    """``2026.08.08 23:40`` 형태의 댓글 표시 시각을 UTC datetime 으로 바꾼다.

    Raises:
        ValueError: 알려진 형식 중 어느 것과도 맞지 않는 경우.
    """
    cleaned = text.strip()
    naive = datetime.strptime(cleaned, TISTORY_DATE_FORMAT)
    return naive.replace(tzinfo=get_zone(tz_name)).astimezone(timezone.utc)


def parse_user_datetime(text: Optional[str], *, tz_name: str = "Asia/Seoul") -> Optional[datetime]:
    """사용자가 입력한 날짜 문자열을 UTC datetime 으로 바꾼다.

    빈 문자열이나 None 은 조건 없음을 뜻하므로 None 을 돌려준다.

    Raises:
        ValueError: 지원하지 않는 형식인 경우.
    """
    if text is None:
        return None
    cleaned = text.strip().replace("Z", "")
    if not cleaned:
        return None
    for fmt in _INPUT_FORMATS:
        try:
            naive = datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=get_zone(tz_name)).astimezone(timezone.utc)
    raise ValueError(f"날짜 형식을 해석할 수 없습니다: {text}")


def from_epoch(seconds: Union[int, str]) -> datetime:
    """epoch 초를 UTC datetime 으로 바꾼다."""
    return datetime.fromtimestamp(int(seconds), tz=timezone.utc)


def to_epoch(moment: datetime, *, tz_name: str = "Asia/Seoul") -> int:
    """datetime 을 epoch 초로 바꾼다. naive 는 사용자 시간대로 해석한다."""
    return int(to_utc(moment, tz_name=tz_name).timestamp())


def format_local(
    moment: Optional[datetime], *, tz_name: str = "Asia/Seoul", fmt: str = "%Y-%m-%d %H:%M:%S"
) -> str:
    """화면 표시용 문자열. None 이면 빈 문자열을 반환한다."""
    if moment is None:
        return ""
    return to_local(moment, tz_name=tz_name).strftime(fmt)


def isoformat_local(moment: Optional[datetime], *, tz_name: str = "Asia/Seoul") -> Optional[str]:
    """API 응답용 ISO 8601 문자열. 시간대 오프셋을 포함한다."""
    if moment is None:
        return None
    return to_local(moment, tz_name=tz_name).isoformat()
