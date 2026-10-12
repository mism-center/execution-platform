"""Tests for core.storage — location_uri → PVC-relative path, matching
model-discovery's resolve_location_uri contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.storage import pvc_subpath, resolve_on_mount


class TestPvcSubpath:
    @pytest.mark.parametrize(
        "uri",
        [
            "irods:///datasets/abc-123",
            "irods://datasets/abc-123",
            "/irods/datasets/abc-123",
            "/datasets/abc-123",
            "datasets/abc-123",
        ],
    )
    def test_all_accepted_shapes_resolve_to_same_path(self, uri: str) -> None:
        assert pvc_subpath(uri, "/irods") == "datasets/abc-123"

    def test_trailing_slash_dropped(self) -> None:
        assert pvc_subpath("irods:///mism/models/m1/", "/irods") == "mism/models/m1"

    def test_mount_prefix_only_stripped_on_segment_boundary(self) -> None:
        assert pvc_subpath("/irodsfoo/bar", "/irods") == "irodsfoo/bar"

    def test_custom_mount_path(self) -> None:
        assert pvc_subpath("/data/irods/x/y", "/data/irods") == "x/y"

    def test_generated_output_uri_unchanged(self) -> None:
        assert pvc_subpath("3f2a/v1", "/irods") == "3f2a/v1"

    @pytest.mark.parametrize(
        "uri",
        ["", "https://example.org/model", "s3://bucket/key", "docker://img"],
    )
    def test_rejects_empty_and_unsupported_schemes(self, uri: str) -> None:
        with pytest.raises(ValueError):
            pvc_subpath(uri, "/irods")

    @pytest.mark.parametrize(
        "uri",
        ["irods:///../etc", "../outside", "a/../../b", "/irods", "irods:///", "/"],
    )
    def test_rejects_paths_outside_or_at_claim_root(self, uri: str) -> None:
        with pytest.raises(ValueError):
            pvc_subpath(uri, "/irods")

    def test_resolve_on_mount(self) -> None:
        assert resolve_on_mount("irods:///m/1", "/irods") == Path("/irods/m/1")
