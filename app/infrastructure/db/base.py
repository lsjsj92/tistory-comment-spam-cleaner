# app/infrastructure/db/base.py
"""ORM 기반 클래스와 공용 컬럼 타입.

SQLite 는 시간대 정보를 보존하지 못한다. 그래서 저장 직전에 UTC naive 로 바꾸고
읽을 때 다시 UTC aware 로 되돌리는 타입을 정의해, 애플리케이션 코드가 항상
timezone-aware datetime 만 다루도록 강제한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, MetaData, TypeDecorator
from sqlalchemy.orm import DeclarativeBase

# 인덱스와 제약 조건 이름을 일관되게 만들어 이후 마이그레이션을 쉽게 한다.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class UtcDateTime(TypeDecorator):
    """항상 UTC aware datetime 으로 오가는 DateTime 컬럼."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"datetime 이 필요하지만 {type(value)!r} 이 전달되었습니다.")
        if value.tzinfo is None:
            # naive 는 UTC 로 간주한다. 사용자 시간대 해석은 timeutils 에서 끝낸다.
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Any) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    """모든 ORM 모델의 기반 클래스."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
