from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent

# In PyInstaller one-file mode, bundled resources are extracted to sys._MEIPASS.
# In normal source/dev mode, they live beside the package.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    BUNDLE_DIR = Path(sys._MEIPASS)
else:
    BUNDLE_DIR = PROJECT_DIR

RESOURCE_DIR = BUNDLE_DIR / "gbfr_editor" / "resources"

# Keep settings writable next to the EXE in frozen builds. Bundled temp dirs are
# read-only/disposable, so user settings should not be stored under _MEIPASS.
if getattr(sys, "frozen", False):
    SETTINGS_PATH = Path(sys.executable).resolve().parent / "settings.json"
else:
    SETTINGS_PATH = PROJECT_DIR / "settings.json"
