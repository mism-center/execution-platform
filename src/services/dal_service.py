"""DAL service — wraps mism-registry operations for the execution platform.

Uses the ``Registry`` protocol so the backend (InMemory vs Postgres) is
swappable via settings.  The execution platform consumes Runs and Resources
that were created by the Discovery Gateway in a shared Postgres database.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mism_registry import (
    ExecutionType,
    InMemoryRegistry,
    Resource,
    Run,
    cancel_run,
    complete_run,
    fail_run,
    find_runs,
    prepare_run,
    register_model,
    start_run,
)
from mism_registry.errors import ResourceNotFoundError, RunNotFoundError
from mism_registry.protocol import Registry

if TYPE_CHECKING:
    from core.settings import Settings

logger = logging.getLogger(__name__)

# Default resource requirements when model.metadata has none.
DEFAULT_RESOURCE_REQUIREMENTS = {
    "cpus": "1",
    "memory": "2Gi",
}


def create_registry(settings: Settings) -> Registry:
    """Factory: returns Postgres or InMemory registry based on config."""
    if settings.database_url:
        from mism_registry.backends.postgres import create_registry as pg_factory

        registry, _session = pg_factory(settings.database_url)
        return registry
    return InMemoryRegistry()


class DALService:
    """High-level operations the execution platform needs from the DAL."""

    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def get_resource(self, resource_id: str) -> Resource | None:
        try:
            return self._registry.get_resource(resource_id)
        except ResourceNotFoundError:
            return None

    def get_run(self, run_id: str) -> Run | None:
        try:
            return self._registry.get_run(run_id)
        except RunNotFoundError:
            return None

    def list_all_runs(self) -> list[Run]:
        return find_runs(self._registry)

    def find_runs_for_model(self, model_id: str) -> list[Run]:
        return find_runs(self._registry, model_id=model_id)

    def mark_running(self, run_id: str) -> Run:
        run = start_run(self._registry, run_id=run_id)
        logger.info(f"Run {run_id} → running")
        return run

    def mark_succeeded(
        self, run_id: str, output_resources: list[Resource] | None = None
    ) -> Run:
        run = complete_run(
            self._registry,
            run_id=run_id,
            output_resources=output_resources or [],
        )
        logger.info(f"Run {run_id} → completed")
        return run

    def mark_failed(self, run_id: str, error: str) -> Run:
        run = fail_run(self._registry, run_id=run_id, error_message=error)
        logger.info(f"Run {run_id} → failed: {error}")
        return run

    def cancel(self, run_id: str) -> Run:
        run = cancel_run(self._registry, run_id=run_id)
        logger.info(f"Run {run_id} → cancelled")
        return run

    def register_model(
        self,
        *,
        name: str,
        location_uri: str,
        execution_type: ExecutionType = ExecutionType.DOCKER,
        execution_ref: str = "",
        metadata: dict | None = None,
    ) -> Resource:
        """Register a model resource. Used for test setup — in production,
        the Discovery Gateway handles registration."""
        return register_model(
            self._registry,
            name=name,
            location_uri=location_uri,
            execution_type=execution_type,
            execution_ref=execution_ref,
            metadata=metadata or {},
        )

    def create_run(
        self,
        *,
        model_id: str,
        input_resource_ids: list[str] | None = None,
        triggered_by: str = "api",
        notes: str = "",
    ) -> Run:
        """Create a Run record. Used for test setup — in production,
        the Discovery Gateway calls prepare_run."""
        run = prepare_run(
            self._registry,
            model_id=model_id,
            input_resource_ids=input_resource_ids or [],
            triggered_by=triggered_by,
            notes=notes,
        )
        logger.info(f"Run created: {run.id} for model={model_id}")
        return run
