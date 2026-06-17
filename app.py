from __future__ import annotations

from gbfr_editor.bootstrap import bootstrap_paths

bootstrap_paths()

from gbfr_editor.ui.main_window import main


if __name__ == "__main__":
    raise SystemExit(main())
