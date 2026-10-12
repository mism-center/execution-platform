"""Batch runs execute in a writable copy of the read-only model mount and
collect what they wrote into the output mount (envagent's ``writable_copy``)."""

from __future__ import annotations

import dataclasses
import hashlib
import subprocess
from pathlib import Path

import pytest

from services.appstore_client import AppstoreClient
from services.dal_service import DALService
from services.run_service import (
    _WORKING_COPY_SH,
    MODEL_CODE_MOUNT,
    MODEL_WORK_COPY,
    OUTPUT_MOUNT,
    RunService,
)
from tests.conftest import create_test_run


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _run_script(tmp_path: Path, model_cmd: str) -> tuple[int, Path, Path]:
    """Execute the real wrapper with local paths standing in for the mounts."""
    src = tmp_path / "model"
    (src / "studies" / "sim").mkdir(parents=True)
    (src / "studies" / "sim" / "run.py").write_text("print('hi')\n")
    (src / "studies" / "results.json").write_text('{"old": true}\n')
    (src / "README.md").write_text("readme\n")
    copy = tmp_path / "scratch" / "model"
    out = tmp_path / "output"
    out.mkdir()
    proc = subprocess.run(
        ["sh", "-c", _WORKING_COPY_SH, str(src), str(copy), str(out), "sh", "-c", model_cmd],
        capture_output=True, text=True, timeout=30,
    )
    return proc.returncode, src, out


class TestWorkingCopyScript:
    def test_collects_new_and_changed_files_anywhere_in_the_tree(self, tmp_path: Path) -> None:
        cmd = (
            "mkdir -p studies/viz reports/figs out/composites && "
            "echo a > studies/viz/a.html && echo b > reports/figs/b.html && "
            "echo c > out/composites/c.png && echo '{\"new\": 1}' > studies/results.json && "
            "echo log > .runlog"
        )
        rc, _, out = _run_script(tmp_path, cmd)
        assert rc == 0
        assert sorted(_tree_digest(out)) == [
            ".runlog",
            "out/composites/c.png",
            "reports/figs/b.html",
            "studies/results.json",
            "studies/viz/a.html",
        ]
        assert (out / "studies" / "results.json").read_text() == '{"new": 1}\n'

    def test_unchanged_code_and_caches_are_not_collected(self, tmp_path: Path) -> None:
        cmd = (
            "mkdir -p studies/sim/__pycache__ .git && "
            "echo x > studies/sim/__pycache__/run.cpython-311.pyc && echo y > stray.pyc && "
            "echo z > .git/HEAD && echo r > result.txt"
        )
        rc, _, out = _run_script(tmp_path, cmd)
        assert rc == 0
        assert sorted(_tree_digest(out)) == ["result.txt"]

    def test_source_is_never_modified(self, tmp_path: Path) -> None:
        src = tmp_path / "model"
        rc, src, _ = _run_script(
            tmp_path, "echo changed > studies/results.json && rm README.md && echo n > new.txt"
        )
        assert rc == 0
        assert (src / "studies" / "results.json").read_text() == '{"old": true}\n'
        assert (src / "README.md").exists()
        assert not (src / "new.txt").exists()

    def test_exit_code_passes_through_and_partial_outputs_are_kept(
        self, tmp_path: Path
    ) -> None:
        rc, _, out = _run_script(tmp_path, "echo partial > partial.txt && exit 3")
        assert rc == 3
        assert (out / "partial.txt").read_text() == "partial\n"

    def test_runs_from_the_copy_root(self, tmp_path: Path) -> None:
        rc, _, out = _run_script(tmp_path, "test -f studies/sim/run.py && pwd > where.txt")
        assert rc == 0
        assert (out / "where.txt").read_text().strip().endswith("scratch/model")


class TestBatchLaunch:
    async def test_model_mounted_read_only_and_command_wrapped(
        self, run_service: RunService, dal: DALService, mock_appstore: AppstoreClient
    ) -> None:
        run_id = create_test_run(dal)

        await run_service.create_run(run_id)

        kw = mock_appstore.launch_job.call_args.kwargs
        mounts = {m["mount_path"]: m for m in kw["pvc_mounts"]}
        assert mounts[MODEL_CODE_MOUNT]["read_only"] is True
        assert mounts[MODEL_CODE_MOUNT]["sub_path"] == "mism/models/spike-predictor"
        assert mounts[OUTPUT_MOUNT]["read_only"] is False
        # No in-place model mount and no out/ overlay any more.
        assert "/app" not in mounts and "/app/out" not in mounts

        cmd = kw["command"]
        assert cmd[:3] == ["sh", "-c", _WORKING_COPY_SH]
        assert cmd[3:6] == [MODEL_CODE_MOUNT, MODEL_WORK_COPY, OUTPUT_MOUNT]
        assert cmd[6:8] == ["sh", "-c"]
        assert cmd[8].startswith("run.sh")
        assert "cd " not in cmd[8]

        env = kw["env"]
        assert env["MODEL_PATH"] == MODEL_WORK_COPY
        assert env["OUTPUT_PATH"] == OUTPUT_MOUNT
        assert "PYTHONPATH" not in env

    async def test_model_without_files_runs_image_contents_unwrapped(
        self,
        run_service: RunService,
        dal: DALService,
        mock_appstore: AppstoreClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id = create_test_run(dal)
        real_get = dal.get_resource

        def no_files(resource_id: str):
            r = real_get(resource_id)
            if r is not None and r.resource_type.value == "model":
                return dataclasses.replace(r, location_uri="")
            return r

        monkeypatch.setattr(dal, "get_resource", no_files)
        await run_service.create_run(run_id)

        kw = mock_appstore.launch_job.call_args.kwargs
        assert kw["command"][:2] == ["sh", "-c"] and len(kw["command"]) == 3
        assert MODEL_CODE_MOUNT not in {m["mount_path"] for m in kw["pvc_mounts"]}
        assert "MODEL_PATH" not in kw["env"]
