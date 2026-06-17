from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from gbfr_editor.bootstrap import bootstrap_paths
from gbfr_editor.paths import RESOURCE_DIR

bootstrap_paths()

from item_db import ItemDatabase, DEFAULT_ITEM_URL, source_urls_from_text


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch/merge GBFR Relink GBID CSV sources")
    ap.add_argument("output", nargs="?", default=str(RESOURCE_DIR / "item_ids_downloaded_all.csv"))
    ap.add_argument("--urls-file", default=str(RESOURCE_DIR / "google_sheet_tabs.txt"))
    ap.add_argument("--community", action="store_true", default=True)
    args = ap.parse_args()

    urls = []
    if args.community:
        urls.append(DEFAULT_ITEM_URL)
    url_file = Path(args.urls_file)
    if url_file.exists():
        urls.extend(source_urls_from_text(url_file.read_text(encoding="utf-8")))
    db, errors = ItemDatabase.download_many(urls)
    seed = RESOURCE_DIR / "item_ids_seed.csv"
    if seed.exists():
        base = ItemDatabase.load_csv(seed)
        base.merge(db)
        db = base
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    db.save_csv(out)
    print(f"Wrote {len(db)} merged rows to {out}")
    if errors:
        print("Failures:")
        for e in errors:
            print("-", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
