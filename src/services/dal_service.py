"""DAL service — wraps mism-registry operations for the execution platform.

Uses the ``Registry`` protocol so the backend (InMemory vs Postgres) is
swappable via settings.  The execution platform consumes Runs and Resources
that were created by the Discovery Gateway in a shared Postgres database.

Session lifecycle: each operation gets a fresh session from the factory,
commits on success, and rolls back on failure.  This prevents a single
failed query from poisoning all subsequent requests.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING, Generator

from mism_registry import (
    ExecutionType,
    InMemoryRegistry,
    Resource,
    ResourceType,
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
    from sqlalchemy.orm import Session, sessionmaker

    from core.settings import Settings

logger = logging.getLogger(__name__)

# Default resource requirements when model.metadata has none.
DEFAULT_RESOURCE_REQUIREMENTS = {
    "cpus": "1",
    "memory": "2Gi",
}


def create_registry(settings: "Settings") -> "Registry | sessionmaker":
    """Factory: returns InMemoryRegistry or a Postgres session factory."""
    if settings.database_url:
        from mism_registry.backends.postgres import create_session_factory

        return create_session_factory(settings.database_url)
    return InMemoryRegistry()


class DALService:
    """High-level operations the execution platform needs from the DAL."""

    def __init__(self, registry_or_factory: "Registry | sessionmaker") -> None:
        if isinstance(registry_or_factory, InMemoryRegistry):
            self._factory = None
            self._in_memory = registry_or_factory
        else:
            self._factory = registry_or_factory
            self._in_memory = None

    @contextmanager
    def _session_scope(self) -> Generator[Registry, None, None]:
        """Yield a registry backed by a fresh session. Commits on success,
        rolls back on failure, closes always."""
        if self._in_memory is not None:
            yield self._in_memory
            return

        from mism_registry.backends.postgres import PostgresRegistry

        session: Session = self._factory()
        try:
            yield PostgresRegistry(session)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_resource(self, resource_id: str) -> Resource | None:
        with self._session_scope() as reg:
            try:
                return reg.get_resource(resource_id)
            except ResourceNotFoundError:
                return None

    def get_run(self, run_id: str) -> Run | None:
        with self._session_scope() as reg:
            try:
                return reg.get_run(run_id)
            except RunNotFoundError:
                return None

    def list_all_runs(self) -> list[Run]:
        with self._session_scope() as reg:
            return find_runs(reg)

    def find_runs_for_model(self, model_id: str) -> list[Run]:
        with self._session_scope() as reg:
            return find_runs(reg, model_id=model_id)

    def mark_running(self, run_id: str, notes: str = "") -> Run:
        with self._session_scope() as reg:
            if notes:
                run = reg.get_run(run_id)
                run.notes = notes
                reg.update_run(run)
            run = start_run(reg, run_id=run_id)
            logger.info(f"Run {run_id} → running")
            return run

    def mark_succeeded(
        self, run_id: str, output_resources: list[Resource] | None = None
    ) -> Run:
        with self._session_scope() as reg:
            run = complete_run(
                reg,
                run_id=run_id,
                output_resources=output_resources or [],
            )
            logger.info(f"Run {run_id} → completed")
            return run

    def mark_failed(self, run_id: str, error: str) -> Run:
        with self._session_scope() as reg:
            run = fail_run(reg, run_id=run_id, error_message=error)
            logger.info(f"Run {run_id} → failed: {error}")
            return run

    def cancel(self, run_id: str) -> Run:
        with self._session_scope() as reg:
            run = cancel_run(reg, run_id=run_id)
            logger.info(f"Run {run_id} → cancelled")
            return run

    def register_dataset(
        self,
        *,
        resource_id: str,
        name: str,
        location_uri: str,
    ) -> Resource:
        """Register a dataset resource (e.g., run output)."""
        with self._session_scope() as reg:
            resource = Resource(
                id=resource_id,
                name=name,
                resource_type=ResourceType.DATASET,
                location_uri=location_uri,
            )
            return reg.register_resource(resource)

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
        with self._session_scope() as reg:
            return register_model(
                reg,
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
        with self._session_scope() as reg:
            run = prepare_run(
                reg,
                model_id=model_id,
                input_resource_ids=input_resource_ids or [],
                triggered_by=triggered_by,
                notes=notes,
            )
            logger.info(f"Run created: {run.id} for model={model_id}")
            return run
