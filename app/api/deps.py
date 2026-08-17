# app/api/deps.py
"""의존성 주입 컨테이너.

서비스 인스턴스를 한 곳에서 만들어 애플리케이션 수명과 함께 관리한다.
라우트는 여기서 꺼내 쓰기만 하고 직접 생성하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..config.settings import Settings
from ..infrastructure.db.session import Database
from ..services.jobs import JobManager
from ..services.monitor import MonitorService
from ..services.session_manager import SessionManager


@dataclass
class AppContext:
    """애플리케이션 전역에서 공유하는 객체 묶음."""

    settings: Settings
    database: Database
    session_manager: SessionManager
    job_manager: JobManager
    monitor: MonitorService

    @classmethod
    def create(cls, settings: Settings) -> "AppContext":
        """설정만으로 전체 의존성 그래프를 만든다."""
        database = Database(settings)
        session_manager = SessionManager(database, settings)
        job_manager = JobManager(
            database=database, settings=settings, session_manager=session_manager
        )
        monitor = MonitorService(
            database=database, settings=settings, job_manager=job_manager
        )
        return cls(
            settings=settings,
            database=database,
            session_manager=session_manager,
            job_manager=job_manager,
            monitor=monitor,
        )


def get_context(request: Request) -> AppContext:
    """요청에서 애플리케이션 컨텍스트를 꺼낸다."""
    return request.app.state.context


def get_settings_dep(request: Request) -> Settings:
    return get_context(request).settings


def get_database(request: Request) -> Database:
    return get_context(request).database


def get_session_manager(request: Request) -> SessionManager:
    return get_context(request).session_manager


def get_job_manager(request: Request) -> JobManager:
    return get_context(request).job_manager
