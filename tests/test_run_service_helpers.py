"""Unit tests for RunService's field-extraction / command-rendering helpers.

Covers ``_require_image``, ``_resolve_compute``, and ``_render_batch_command``
in isolation — no DAL, no appstore, no test fixtures. Each helper is a pure
function of its inputs, so we test them against direct Run/Compute/EntryPoint
values.
"""

from __future__ import annotations

import pytest
from mism_registry import Run
from mism_registry.types import Argument, Compute, Container, EntryPoint

from services.dal_service import DEFAULT_RESOURCE_REQUIREMENTS
from services.run_service import RunService


class TestRequireImage:
    """`_require_image` pulls container.image_name off the Run snapshot."""

    def test_returns_image_when_present(self) -> None:
        run = Run(
            model_id="m",
            container=Container(kind="docker", image_name="docker.io/org/model:v1"),
        )
        assert RunService._require_image(run) == "docker.io/org/model:v1"

    def test_raises_when_container_is_none(self) -> None:
        run = Run(model_id="m", container=None)
        with pytest.raises(ValueError, match="container.image_name"):
            RunService._require_image(run)

    def test_raises_when_image_name_is_empty(self) -> None:
        run = Run(model_id="m", container=Container(kind="docker", image_name=""))
        with pytest.raises(ValueError, match="container.image_name"):
            RunService._require_image(run)


class TestResolveCompute:
    """`_resolve_compute` maps Compute → (cpus_str, memory_str) with defaults."""

    def test_none_falls_back_to_defaults(self) -> None:
        cpus, memory = RunService._resolve_compute(None)
        assert cpus == DEFAULT_RESOURCE_REQUIREMENTS["cpus"]
        assert memory == DEFAULT_RESOURCE_REQUIREMENTS["memory"]

    def test_all_fields_populated(self) -> None:
        cpus, memory = RunService._resolve_compute(
            Compute(cpu_cores=4, memory_gb=8.0)
        )
        assert cpus == "4"
        assert memory == "8.0Gi"

    def test_only_cpu_populated_uses_default_memory(self) -> None:
        cpus, memory = RunService._resolve_compute(Compute(cpu_cores=2))
        assert cpus == "2"
        assert memory == DEFAULT_RESOURCE_REQUIREMENTS["memory"]

    def test_only_memory_populated_uses_default_cpu(self) -> None:
        cpus, memory = RunService._resolve_compute(Compute(memory_gb=16.0))
        assert cpus == DEFAULT_RESOURCE_REQUIREMENTS["cpus"]
        assert memory == "16.0Gi"

    def test_fractional_memory_preserved(self) -> None:
        # K8s memory strings accept fractional Gi, don't round-trip through int
        _, memory = RunService._resolve_compute(Compute(memory_gb=1.5))
        assert memory == "1.5Gi"


class TestRenderBatchCommand:
    """`_render_batch_command` renders EntryPoint → ['sh', '-c', '<cli>']."""

    def test_wraps_to_cli_output_in_sh_dash_c(self) -> None:
        entrypoint = EntryPoint(command="run.sh", purpose="smoke test")
        cmd = RunService._render_batch_command(entrypoint, {})
        assert cmd[:2] == ["sh", "-c"]
        assert len(cmd) == 3
        assert cmd[2] == "run.sh"

    def test_positional_arg_appended(self) -> None:
        entrypoint = EntryPoint(
            command="jupyter nbconvert",
            arguments=(Argument(name="notebook", position=1, default="foo.ipynb"),),
        )
        cmd = RunService._render_batch_command(entrypoint, {})
        assert cmd[2] == "jupyter nbconvert foo.ipynb"

    def test_positional_arg_user_override(self) -> None:
        entrypoint = EntryPoint(
            command="jupyter nbconvert",
            arguments=(Argument(name="notebook", position=1, default="foo.ipynb"),),
        )
        cmd = RunService._render_batch_command(entrypoint, {"notebook": "bar.ipynb"})
        assert cmd[2] == "jupyter nbconvert bar.ipynb"

    def test_option_flag_with_value(self) -> None:
        entrypoint = EntryPoint(
            command="tool",
            arguments=(Argument(name="--out", data_type="path", default="/output"),),
        )
        cmd = RunService._render_batch_command(entrypoint, {})
        assert cmd[2] == "tool --out /output"

    def test_bool_option_emitted_only_when_truthy(self) -> None:
        entrypoint = EntryPoint(
            command="tool",
            arguments=(
                Argument(name="--verbose", data_type="bool", default=False),
                Argument(name="--quiet", data_type="bool", default=True),
            ),
        )
        cmd = RunService._render_batch_command(entrypoint, {})
        # --quiet default=True → emitted; --verbose default=False → omitted
        assert "--quiet" in cmd[2]
        assert "--verbose" not in cmd[2]

    def test_shell_metacharacters_quoted(self) -> None:
        # Injection defense: user-supplied values must be shell-quoted so a
        # value like "; rm -rf /" cannot escape the argument context.
        entrypoint = EntryPoint(
            command="echo",
            arguments=(Argument(name="msg", position=1),),
        )
        cmd = RunService._render_batch_command(entrypoint, {"msg": "; rm -rf /"})
        # The dangerous token gets shlex-quoted (typically single-quoted)
        assert "; rm -rf /" not in cmd[2].replace("'; rm -rf /'", "")
        assert "'; rm -rf /'" in cmd[2]

    def test_none_entrypoint_raises(self) -> None:
        with pytest.raises(ValueError, match="no entrypoint"):
            RunService._render_batch_command(None, {})

    def test_placeholder_stripped_from_command_template(self) -> None:
        # EntryPoint.to_cli strips <placeholder> tokens from the base command
        # so positional args fill them cleanly.
        entrypoint = EntryPoint(
            command="cmd <target>",
            arguments=(Argument(name="target", position=1, default="value"),),
        )
        cmd = RunService._render_batch_command(entrypoint, {})
        assert cmd[2] == "cmd value"
