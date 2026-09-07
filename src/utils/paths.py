"""Path validation shared by every entry point that opens an operator-named file.

The ingestion and modelling CLIs both take a `-c/--config` argument and open it. That
argument is an untrusted string whenever the caller is a scheduler, a container command
or an agent rather than a human at a shell, so the path is resolved and checked to sit
inside a known root before anything opens it. A traversal like `../../etc/shadow` is
rejected here rather than at the `open()`.
"""

import tempfile
from pathlib import Path

# The repository root — this file is src/utils/paths.py, so three parents up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Temp dirs are allowed on purpose: pytest builds config fixtures under `tmp_path`, and
# the container writes generated configs to a scratch dir. Both are still bounded roots.
_ALLOWED_ROOTS = (PROJECT_ROOT, Path(tempfile.gettempdir()).resolve())


def safe_path(candidate: Path | str) -> Path:
    """Resolve `candidate` and return it, or raise if it escapes every allowed root.

    Symlinks are followed by `resolve()` before the check, so a link inside the project
    pointing outside it is rejected too.
    """
    resolved = Path(candidate).expanduser().resolve()
    for root in _ALLOWED_ROOTS:
        if resolved == root or root in resolved.parents:
            return resolved
    raise ValueError(
        f"Refusing to open {candidate!r}: resolved to {resolved}, which is outside "
        f"the project root {PROJECT_ROOT}."
    )
