"""Create a private staging directory for a disk deploy, outside any Git tree."""

import os
import sys
import tempfile
from pathlib import Path


def main(prefix: str) -> None:
    override = os.environ.get("TMPDIR")
    try:
        if override:
            base = Path(override).resolve()
        else:
            cache = os.environ.get("XDG_CACHE_HOME")
            base = ((Path(cache) if cache else Path.home() / ".cache") / "liam-e").resolve()

        # The CLI checks for the presence of a .git entry, even if Git cannot open it.
        if any(os.path.lexists(parent / ".git") for parent in (base, *base.parents)):
            sys.exit("deploy: a .git sits in a parent directory of the temporary directory. "
                     "Set TMPDIR to a directory with no .git above it. Nothing was deployed.")

        if not override:
            base.mkdir(mode=0o700, parents=True, exist_ok=True)
        print(tempfile.mkdtemp(prefix=f"{prefix}.", dir=base))
    except (OSError, RuntimeError):
        sys.exit("deploy: could not create private staging. Set TMPDIR to a writable directory "
                 "with no .git above it. Nothing was deployed.")


if __name__ == "__main__":
    main(sys.argv[1])
