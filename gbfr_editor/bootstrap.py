from __future__ import annotations

import sys
from pathlib import Path

def bootstrap_paths() -> None:
    """Allow the small top-level launchers to find grouped editor modules.

    The project intentionally keeps app.py and cli.py tiny.  The real code lives
    under gbfr_editor/core, data, research, and ui so the project folder stays
    readable while legacy internal imports continue to work.
    """
    package_dir = Path(__file__).resolve().parent
    for rel in ("core", "data", "research", "ui", "cli"):
        path = str(package_dir / rel)
        if path not in sys.path:
            sys.path.insert(0, path)
