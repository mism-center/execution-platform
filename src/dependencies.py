"""FastAPI dependency injection — provides services, DAL, and compute singletons."""

from __future__ import annotations

from functools import lru_cache

from core.settings import Settings, get_settings
from orchestration.compute import Compute
from services.dal_service import DALService, create_registry
from services.run_service import RunService
from services.vivarium_service import VivariumService


@lru_cache
def _create_compute(settings: Settings | None = None) -> Compute:
    settings = settings or get_settings()
    if settings.stub_compute:
        from orchestration.stub import StubCompute

        return StubCompute()
    from orchestration.kube import KubernetesCompute

    return KubernetesCompute(namespace=settings.namespace)


@lru_cache
def _create_dal(settings: Settings | None = None) -> DALService:
    settings = settings or get_settings()
    registry = create_registry(settings)
    return DALService(registry)


def get_compute() -> Compute:
    return _create_compute()


def get_dal() -> DALService:
    return _create_dal()


@lru_cache
def get_run_service() -> RunService:
    settings = get_settings()
    return RunService(dal=get_dal(), compute=get_compute(), settings=settings)


@lru_cache
def get_vivarium_service() -> VivariumService:
    settings = get_settings()
    return VivariumService(compute=get_compute(), settings=settings)
