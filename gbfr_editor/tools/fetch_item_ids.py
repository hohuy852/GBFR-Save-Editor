from __future__ import annotations

import argparse
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
from gbfr_editor.bootstrap import bootstrap_paths
from gbfr_editor.paths import RESOURCE_DIR
bootstrap_paths()
from item_db import ItemDatabase, DEFAULT_ITEM_URL


def main() -> int:
    parser = argparse.ArgumentParser(description="Download Community GBFR item IDs to CSV")
    parser.add_argument("output", nargs="?", default=str(RESOURCE_DIR / "item_ids_downloaded.csv"))
    parser.add_argument("--url", default=DEFAULT_ITEM_URL)
    args = parser.parse_args()
    db = ItemDatabase.download_community(args.url)
    db.save_csv(args.output)
    print(f"Downloaded {len(db)} item rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
