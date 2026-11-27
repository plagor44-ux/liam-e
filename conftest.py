"""Shared fixtures for the offline release and probe deploy suites."""

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def _has_git_ancestor(path: Path) -> bool:
    resolved = path.resolve()
    return any(os.path.lexists(parent / ".git") for parent in (resolved, *resolved.parents))


def _usable_deploy_base(path: Path) -> bool:
    return (
        path.is_dir()
        and os.access(path, os.W_OK | os.X_OK)
        and not _has_git_ancestor(path)
        # Traversal permission alone says nothing about executing the fake CLI.
        and not (os.statvfs(path).f_flag & getattr(os, "ST_NOEXEC", 0))
    )


@pytest.fixture
def deploy_tmp_path(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Path]:
    """Keep the fake home, cache, and staging outside Git ancestry."""
    if getattr(request, "param", False):
        # Selected success tests simulate the host marker in their own pytest directory.
        (tmp_path / ".git").mkdir()
        tmp_path = tmp_path / "beneath-marker"
        tmp_path.mkdir()

    if _usable_deploy_base(tmp_path):
        yield tmp_path
        return

    for base in (Path("/var/tmp"), Path("/dev/shm"), Path.home() / ".cache"):
        if _usable_deploy_base(base):
            with tempfile.TemporaryDirectory(prefix="liam-e-deploy-test.", dir=base) as clean:
                yield Path(clean)
            return
    pytest.skip("the deploy tests need a writable, executable temporary directory with no .git ancestor")
