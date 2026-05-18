"""Shared config for test scripts. Values are read from environment variables.

Copy scripts/.env.example to scripts/.env and fill in the values, then:
    export $(cat scripts/.env | xargs) && python scripts/test_batch_execution.py
"""

import os


def _require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Required env var '{name}' is not set. "
            "See scripts/.env.example"
        )
    return val


DB_URL        = _require("MISM_DB_URL")
EXEC_API      = _require("MISM_EXEC_API")
MODEL_IMAGE   = _require("MISM_MODEL_IMAGE")
NOTEBOOK_PATH = _require("MISM_NOTEBOOK_PATH")
