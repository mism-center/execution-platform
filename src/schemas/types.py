"""Reusable validated types for request schemas.

Uses ``Annotated`` with Pydantic ``StringConstraints`` to define types that
enforce validation at the schema boundary — before data reaches services.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import StringConstraints

# A non-empty string with leading/trailing whitespace stripped.
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Container image reference — must be non-empty, no leading/trailing whitespace.
# Examples: "docker.io/org/model:v1", "containers.renci.org/mism/vivarium:latest"
ImageRef = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[^\s]+$"),
]

# Data path — must be non-empty, no leading/trailing whitespace.
# Examples: "/mism/datasets/cohort-a/data.csv", "runs/abc/outputs"
DataPath = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]

# Kubernetes resource quantity — e.g. "1", "0.5", "2Gi", "512Mi", "250m"
K8sQuantity = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^\d+(\.\d+)?([EPTGMK]i?|[mkn])?$"),
]
