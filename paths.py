"""Project path helpers.

Resolves the project root by locating ``index.py`` so callers do not need
to rely on ``os.getcwd()`` or hard-coded paths, which differ across
platforms and deployment targets.
"""

from functools import lru_cache
from pathlib import Path


ROOT_MARKER = "index.py"


@lru_cache(maxsize=1)
def project_root() -> Path:
    """Return the directory that contains ``index.py``."""
    for parent in Path(__file__).resolve().parents:
        if (parent / ROOT_MARKER).is_file():
            return parent
    raise RuntimeError(f"project root not found: {ROOT_MARKER} missing above {__file__}")


def resolve(*parts: str | Path) -> Path:
    """Return ``project_root() / parts`` as an absolute path."""
    return project_root().joinpath(*parts)
