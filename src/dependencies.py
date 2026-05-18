"""FastAPI dependency injection — provides services and DAL singletons."""

from __future__ import annotations

from functools import lru_cache

from core.settings import Settings, get_settings
from services.appstore_client import AppstoreClient
from services.dal_service import DALService, create_registry
from services.run_service import RunService


@lru_cache
def _create_dal(settings: Settings | None = None) -> DALService:
    settings = settings or get_settings()
    registry_or_factory = create_registry(settings)
    return DALService(registry_or_factory)


@lru_cache
def _create_appstore(settings: Settings | None = None) -> AppstoreClient:
    settings = settings or get_settings()
    return AppstoreClient(settings)


def get_dal() -> DALService:
    return _create_dal()


@lru_cache
def get_run_service() -> RunService:
    settings = get_settings()
    return RunService(
        dal=get_dal(),
        appstore=_create_appstore(),
        settings=settings,
    )
