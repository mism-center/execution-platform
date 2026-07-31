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

    def test_option_flag_with_user_value(self) -> None:
        # Valued option: emitted only when the caller explicitly supplied it.
        entrypoint = EntryPoint(
            command="tool",
            arguments=(Argument(name="--out", data_type="path", default="/output"),),
        )
        cmd = RunService._render_batch_command(entrypoint, {"--out": "/tmp/x"})
        assert cmd[2] == "tool --out /tmp/x"

    def test_option_default_not_emitted_without_user_override(self) -> None:
        # Argparse-native semantics: if the caller didn't pass --out, we don't
        # bake the default into argv. The script's own argparse fills it in.
        # This is the fix for TD-009 — enables vivarium-chemotaxis composites
        # that gate on len(sys.argv) == 1.
        entrypoint = EntryPoint(
            command="tool",
            arguments=(Argument(name="--out", data_type="path", default="/output"),),
        )
        cmd = RunService._render_batch_command(entrypoint, {})
        assert cmd[2] == "tool"

    def test_bool_option_only_when_user_supplies_true(self) -> None:
        # Bool defaults never reach argv unless the caller explicitly sets
        # them true. False user values are also skipped (argparse presence
        # flags don't have a "false" argv form).
        entrypoint = EntryPoint(
            command="tool",
            arguments=(
                Argument(name="--verbose", data_type="bool", default=False),
                Argument(name="--quiet", data_type="bool", default=True),
            ),
        )
        # No user values → NOTHING emitted, even though --quiet has default=True
        assert RunService._render_batch_command(entrypoint, {})[2] == "tool"
        # Only --verbose supplied True → emitted
        cmd = RunService._render_batch_command(entrypoint, {"--verbose": True})
        assert cmd[2] == "tool --verbose"
        # User explicitly sets --quiet False → still skipped (presence flag)
        cmd = RunService._render_batch_command(entrypoint, {"--quiet": False})
        assert cmd[2] == "tool"

    def test_bool_option_coerces_string_forms(self) -> None:
        # Real-world footgun: JSON `"false"` (string) is a non-empty Python
        # str and therefore truthy. We coerce common string forms before the
        # truthiness check so string "false" doesn't emit the flag.
        entrypoint = EntryPoint(
            command="tool",
            arguments=(Argument(name="--topology", data_type="bool", default=False),),
        )
        # String falsy forms → skipped
        for falsy in ("false", "False", "FALSE", "0", "no", "off", ""):
            cmd = RunService._render_batch_command(entrypoint, {"--topology": falsy})
            assert cmd[2] == "tool", f"expected no --topology for {falsy!r}"
        # String truthy forms → emitted
        for truthy in ("true", "True", "1", "yes", "on"):
            cmd = RunService._render_batch_command(entrypoint, {"--topology": truthy})
            assert cmd[2] == "tool --topology", f"expected --topology for {truthy!r}"

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

    def test_cwd_var_prefixes_cd(self) -> None:
        # When the model has files mounted at /app, we prefix the command
        # with `cd "$MODEL_PATH" &&` so relative paths resolve from /app.
        entrypoint = EntryPoint(command="python chemotaxis/foo.py")
        cmd = RunService._render_batch_command(
            entrypoint, {}, cwd_var="MODEL_PATH"
        )
        assert cmd[2] == 'cd "$MODEL_PATH" && python chemotaxis/foo.py'

    def test_cwd_var_none_skips_cd_prefix(self) -> None:
        # Models without a location_uri (no iRODS files) skip the cd prefix.
        entrypoint = EntryPoint(command="run.sh")
        cmd = RunService._render_batch_command(entrypoint, {}, cwd_var=None)
        assert cmd[2] == "run.sh"


class TestBuildPvcMounts:
    """`_build_pvc_mounts` — model files mounted at /app when location_uri set."""

    def _svc(self):
        # _build_pvc_mounts is an instance method that only reads self via
        # unused attributes here; a bare RunService with mock deps is fine.
        from unittest.mock import MagicMock

        return RunService(dal=MagicMock(), appstore=MagicMock(), settings=MagicMock())

    def test_model_mount_appended_when_location_uri_present(self) -> None:
        mounts = self._svc()._build_pvc_mounts(
            input_paths=[],
            output_uri="out-uuid/v1",
            pvc="irods-pvc",
            model_location_uri="vivarium-test-A/1.0.0",
        )
        # Should be: [output, model]
        model_mount = next(m for m in mounts if m["mount_path"] == "/app")
        assert model_mount["sub_path"] == "vivarium-test-A/1.0.0"
        assert model_mount["read_only"] is False
        assert model_mount["pvc"] == "irods-pvc"

    def test_model_mount_omitted_when_location_uri_none(self) -> None:
        mounts = self._svc()._build_pvc_mounts(
            input_paths=[],
            output_uri="out-uuid/v1",
            pvc="irods-pvc",
            model_location_uri=None,
        )
        assert not any(m["mount_path"] == "/app" for m in mounts)

    def test_leading_slash_stripped_from_sub_path(self) -> None:
        mounts = self._svc()._build_pvc_mounts(
            input_paths=[],
            output_uri="out/v1",
            pvc="irods-pvc",
            model_location_uri="/leading/slash",
        )
        model_mount = next(m for m in mounts if m["mount_path"] == "/app")
        assert model_mount["sub_path"] == "leading/slash"

    def test_overlay_mounts_output_at_app_out(self) -> None:
        # /app/out and /output should share the same sub_path so scripts
        # writing to relative "out/" land in the output resource.
        mounts = self._svc()._build_pvc_mounts(
            input_paths=[],
            output_uri="out-uuid/v1",
            pvc="irods-pvc",
            model_location_uri="model-uuid/1.0.0",
        )
        overlay = next(m for m in mounts if m["mount_path"] == "/app/out")
        output = next(m for m in mounts if m["mount_path"] == "/output")
        assert overlay["sub_path"] == output["sub_path"] == "out-uuid/v1"
        assert overlay["read_only"] is False

    def test_overlay_skipped_when_no_model_mount(self) -> None:
        # No model files → no overlay (no /app to nest under).
        mounts = self._svc()._build_pvc_mounts(
            input_paths=[],
            output_uri="out-uuid/v1",
            pvc="irods-pvc",
            model_location_uri=None,
        )
        assert not any(m["mount_path"] == "/app/out" for m in mounts)

    def test_overlay_opt_out(self) -> None:
        # Caller can disable the overlay when they want /app/out to remain
        # a plain (writable) subdirectory of the model dir on iRODS.
        mounts = self._svc()._build_pvc_mounts(
            input_paths=[],
            output_uri="out-uuid/v1",
            pvc="irods-pvc",
            model_location_uri="model-uuid/1.0.0",
            overlay_output_at_model_out=False,
        )
        assert not any(m["mount_path"] == "/app/out" for m in mounts)
