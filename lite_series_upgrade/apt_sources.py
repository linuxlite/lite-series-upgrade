"""Helpers for dealing with apt sources files.

The upgrade utility needs to rewrite entries in ``/etc/apt/sources.list`` and
the accompanying ``sources.list.d`` snippets when switching the release
codename.  Historically this created timestamped ``.bak`` files which clutter
the directory and occasionally confused downstream tooling.  The new policy is
to avoid creating backups for those particular files while keeping the safety
net for every other path the upgrade touches.
"""

from __future__ import annotations

import os
from pathlib import Path

APT_DIR = Path("/etc/apt")
APT_SOURCES_LIST = APT_DIR / "sources.list"
APT_SOURCES_LIST_D = APT_DIR / "sources.list.d"


def _normalise(path: Path) -> Path:
    """Return an absolute variant of *path* without requiring it to exist."""

    try:
        return path.resolve(strict=False)
    except Exception:  # pragma: no cover - extremely defensive
        return path


def should_backup_apt_source(path: Path) -> bool:
    """Return ``False`` when a path refers to an apt sources list entry.

    Only the canonical ``/etc/apt/sources.list`` file and the files located in
    ``/etc/apt/sources.list.d`` are covered.  Everything else should still be
    backed up before modifications.
    """

    normalised = _normalise(path)
    if normalised == APT_SOURCES_LIST:
        return False

    try:
        if normalised.is_relative_to(APT_SOURCES_LIST_D):  # type: ignore[attr-defined]
            return False
    except AttributeError:  # pragma: no cover - Python < 3.9 fallback
        dir_str = str(APT_SOURCES_LIST_D)
        path_str = str(normalised)
        if path_str == dir_str or path_str.startswith(dir_str + os.sep):
            return False

    return True


__all__ = ["should_backup_apt_source"]
