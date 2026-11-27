"""Shared fixtures for the offline release and probe deploy suites."""

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


def _has_git_ancestor(path: Path) -> bool:
    resolved = path.resolve()
    return any(os.path.lexists(parent / ".git") for parent in (resolved, *resolved.parents))


@pytest.fixture
def deploy_tmp_path(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[Path]:
    """Keep the fake home, cache, and staging outside Git ancestry."""
    if getattr(request, "param", False):
        # Selected success tests simulate the host marker in their own pytest directory.
        (tmp_path / ".git").mkdir()
        tmp_path = tmp_path / "beneath-marker"
        tmp_path.mkdir()

    if not _has_git_ancestor(tmp_path):
        yield tmp_path
        return

    for base in (Path("/var/tmp"), Path("/dev/shm")):
        if base.is_dir() and os.access(base, os.W_OK | os.X_OK) and not _has_git_ancestor(base):
            with tempfile.TemporaryDirectory(prefix="liam-e-deploy-test.", dir=base) as clean:
                yield Path(clean)
            return
    pytest.skip("the deploy tests need a writable temporary directory with no .git ancestor")
