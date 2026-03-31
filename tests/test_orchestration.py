"""Tests for orchestration models, Compute protocol, and StubCompute."""

from __future__ import annotations

from orchestration.compute import Compute, StartResult, SystemStatus
from orchestration.models import (
    ContainerSpec,
    ResourceLimits,
    SystemSpec,
    VolumeMount,
)
from orchestration.stub import StubCompute
from schemas.enums import PodPhase


class TestSystemSpec:
    def test_full_name(self) -> None:
        system = SystemSpec(app_name="mism-run", containers=[], identifier="abc123")
        assert system.full_name == "mism-run-abc123"

    def test_primary_port_from_container(self) -> None:
        container = ContainerSpec(name="jupyter", image="jupyter:latest", ports=[8888])
        system = SystemSpec(app_name="test", containers=[container])
        assert system.primary_port == 8888

    def test_primary_port_default(self) -> None:
        container = ContainerSpec(name="worker", image="worker:latest")
        system = SystemSpec(app_name="test", containers=[container])
        assert system.primary_port == 8888

    def test_ambassador_prefix(self) -> None:
        system = SystemSpec(
            app_name="vivarium",
            containers=[],
            identifier="abc123",
            username="hpatel",
        )
        assert system.ambassador_prefix == "/private/vivarium/hpatel/abc123/"


class TestContainerSpec:
    def test_defaults(self) -> None:
        c = ContainerSpec(name="test", image="test:latest")
        assert c.env == {}
        assert c.ports == []
        assert c.volumes == []

    def test_with_volumes(self) -> None:
        vol = VolumeMount(
            name="input",
            mount_path="/input",
            pvc_name="irods-data",
            sub_path="datasets/cohort-a",
            read_only=True,
        )
        c = ContainerSpec(name="model", image="model:v1", volumes=[vol])
        assert len(c.volumes) == 1
        assert c.volumes[0].read_only is True


class TestResourceLimits:
    def test_defaults(self) -> None:
        r = ResourceLimits()
        assert r.cpus is None
        assert r.gpus is None

    def test_custom(self) -> None:
        r = ResourceLimits(cpus="2", memory="4Gi", gpus=1)
        assert r.gpus == 1


class TestStubCompute:
    def test_implements_protocol(self) -> None:
        assert isinstance(StubCompute(), Compute)

    def test_start_returns_start_result(self) -> None:
        stub = StubCompute()
        container = ContainerSpec(name="test", image="test:latest", ports=[8080])
        system = SystemSpec(app_name="test", containers=[container], identifier="abc")
        result = stub.start(system)
        assert isinstance(result, StartResult)
        assert result.sid == "abc"
        assert result.name == "test-abc"

    def test_status_returns_system_status(self) -> None:
        stub = StubCompute()
        container = ContainerSpec(name="test", image="test:latest")
        system = SystemSpec(app_name="test", containers=[container], identifier="abc")
        stub.start(system)

        status = stub.status("abc")
        assert isinstance(status, SystemStatus)
        assert status.phase == PodPhase.RUNNING
        assert status.is_ready is True

    def test_status_not_found(self) -> None:
        stub = StubCompute()
        assert stub.status("nonexistent") is None

    def test_delete(self) -> None:
        stub = StubCompute()
        container = ContainerSpec(name="test", image="test:latest")
        system = SystemSpec(app_name="test", containers=[container], identifier="abc")
        stub.start(system)

        stub.delete("abc")
        assert stub.status("abc") is None
