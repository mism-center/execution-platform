"""Translate resource ``location_uri`` values to paths on the iRODS PVC.

Mirrors model-discovery's ``mismapi.core.file_storage.resolve_location_uri`` so
both services agree on where a resource lives. Accepted shapes, all resolving
to ``datasets/abc-123`` relative to the PVC root:

    irods:///datasets/abc-123          # canonical form (3 slashes = empty host)
    irods://datasets/abc-123           # tolerated; first segment is *not* a host
    /irods/datasets/abc-123            # absolute path that already includes the mount
    /datasets/abc-123                  # plain absolute path — implicit iRODS
    datasets/abc-123                   # plain relative path — implicit iRODS

The relative form is what a K8s ``subPath`` needs; a bare ``.strip("/")`` turns
``irods:///x`` into ``irods:///x``, which is not a path on the claim.
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from urllib.parse import urlsplit


def pvc_subpath(location_uri: str, mount_path: str = "/irods") -> str:
    """Return ``location_uri`` as a path relative to the PVC root.

    ``mount_path`` is where this pod mounts the claim; an absolute URI that
    already includes it has the prefix removed. Raises ``ValueError`` for an
    empty URI, an unsupported scheme, or a path that escapes the claim.
    """
    if not location_uri:
        raise ValueError("Resource has no location_uri")

    parts = urlsplit(location_uri)
    # urlsplit("C:\\foo") reports scheme "c"; treat a drive letter as a plain path.
    is_plain_path = parts.scheme == "" or (len(parts.scheme) == 1 and parts.scheme.isalpha())

    if parts.scheme == "irods":
        # urlsplit("irods://foo/bar") -> netloc="foo", path="/bar"; the netloc is
        # the first path segment, not a host.
        rel = parts.netloc + parts.path
    elif is_plain_path:
        rel = location_uri.replace("\\", "/")
        mount = mount_path.rstrip("/")
        if mount and (rel == mount or rel.startswith(mount + "/")):
            rel = rel[len(mount):]
    else:
        raise ValueError(
            f"Unsupported location_uri scheme '{parts.scheme}'; "
            "expected 'irods://' or a plain path"
        )

    rel = posixpath.normpath(rel.lstrip("/"))
    if rel in ("", ".") or rel == ".." or rel.startswith("../"):
        raise ValueError(f"location_uri {location_uri!r} does not name a path inside the claim")
    return rel


def resolve_on_mount(location_uri: str, mount_path: str) -> Path:
    """Absolute path of ``location_uri`` on this pod's mount of the PVC."""
    return Path(mount_path) / pvc_subpath(location_uri, mount_path)
