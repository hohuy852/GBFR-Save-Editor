from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Dict
import csv

from item_db import ItemDatabase, ItemEntry, RAW_ITEM_URL, FALLBACK_ITEM_URL, TRAIT_SKILL_URL, FALLBACK_TRAIT_SKILL_URL


COMMUNITY_ITEM_ID_TARGET_ROWS = 1852  # upstream docs/resources/item_id.csv rows excluding header at the time this tool was mapped

PREFIX_LABELS = {
    "GEEN": "Sigils / Gems",
    "ITEM": "Inventory Items / Materials / Currency",
    "WEP": "Weapons",
    "PL": "Playable Characters",
    "NP": "NPC / Story Characters",
    "SKILL": "Traits / Skills",
}

ITEM_FAMILY_LABELS = {
    "ITEM_13": "Consumables",
    "ITEM_23": "Crewmate Cards",
    "ITEM_25": "Dread Wrightstones",
    "ITEM_26": "Vitality Wrightstones",
    "ITEM_27": "Fortification Wrightstones",
    "ITEM_28": "Sequestration Wrightstones",
    "ITEM_30": "Mirage Munition",
    "ITEM_31": "Boss Materials / Break Parts",
    "ITEM_32": "Late-game / Boss Materials",
    "ITEM_33": "Commemorative / Special Items",
    "ITEM_34": "Glitterstones / Glittercrystals",
    "ITEM_35": "Currency",
    "ITEM_36": "Tickets",
    "ITEM_50": "Fate / Special Items",
    "ITEM_60": "Crab / Wee Pincer",
    "ITEM_70": "Key Items / Color Packs",
    "ITEM_80": "Keys",
}

SOURCE_URLS = [RAW_ITEM_URL, FALLBACK_ITEM_URL, TRAIT_SKILL_URL, FALLBACK_TRAIT_SKILL_URL]


def gbid_prefix(item_id: str) -> str:
    ident = (item_id or "").upper().strip()
    if ident.startswith("WEP_"):
        return "WEP"
    if ident.startswith("ITEM_"):
        parts = ident.split("_")
        return "_".join(parts[:2]) if len(parts) >= 2 else "ITEM"
    if ident.startswith("PL") and ident[2:].isdigit():
        return "PL"
    if ident.startswith("NP") and ident[2:].isdigit():
        return "NP"
    return ident.split("_", 1)[0] if ident else "Other"


def high_level_prefix(item_id: str) -> str:
    p = gbid_prefix(item_id)
    if p.startswith("ITEM_"):
        return "ITEM"
    return p


def build_catalog_summary(db: ItemDatabase) -> Dict[str, object]:
    rows = list(db.by_hash.values())
    categories = Counter(e.category for e in rows)
    high = Counter(high_level_prefix(e.item_id) for e in rows)
    families = Counter(gbid_prefix(e.item_id) for e in rows if high_level_prefix(e.item_id) == "ITEM")
    dummy = sum(1 for e in rows if e.display_name.lower().startswith("reserved / dummy"))
    unnamed = sum(1 for e in rows if e.display_name.lower().startswith("unnamed / reserved"))
    target = COMMUNITY_ITEM_ID_TARGET_ROWS
    coverage = round((len(rows) / target) * 100, 1) if target else 0.0
    return {
        "total": len(rows),
        "target_rows": target,
        "coverage_percent": coverage,
        "categories": dict(sorted(categories.items())),
        "prefixes": dict(sorted(high.items())),
        "item_families": dict(sorted(families.items())),
        "reserved_dummy": dummy,
        "unnamed_reserved": unnamed,
        "source_urls": list(SOURCE_URLS),
    }


def catalog_rows(db: ItemDatabase, text_filter: str = "") -> List[List[object]]:
    q = (text_filter or "").strip().lower()
    rows: List[List[object]] = []
    for e in sorted(db.by_hash.values(), key=lambda x: (high_level_prefix(x.item_id), gbid_prefix(x.item_id), x.item_id, x.hash_hex)):
        prefix = gbid_prefix(e.item_id)
        group = ITEM_FAMILY_LABELS.get(prefix) or PREFIX_LABELS.get(high_level_prefix(e.item_id), high_level_prefix(e.item_id))
        row = [e.category, group, e.display_name, e.item_id, e.hash_hex, e.alias_text]
        hay = " ".join(str(v) for v in row).lower()
        if q and not all(t in hay for t in q.split() if t):
            continue
        rows.append(row)
    return rows


def write_catalog_csv(db: ItemDatabase, path: str | Path, text_filter: str = "") -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["category", "group", "name", "gbid", "hash", "aliases"])
        writer.writerows(catalog_rows(db, text_filter))


def format_catalog_summary(db: ItemDatabase) -> str:
    summary = build_catalog_summary(db)
    lines = [
        "Community Item IDs coverage",
        "========================",
        f"Loaded GBID/hash rows: {summary['total']:,}",
        f"Community item_id.csv target rows: {summary['target_rows']:,}",
        f"Approx coverage vs item_id.csv: {summary['coverage_percent']}%",
        f"Reserved dummy rows: {summary['reserved_dummy']:,}",
        f"Unnamed/reserved rows: {summary['unnamed_reserved']:,}",
        "",
        "Top-level prefixes:",
    ]
    for key, count in sorted(summary["prefixes"].items(), key=lambda kv: (-kv[1], kv[0])):
        label = PREFIX_LABELS.get(key, key)
        lines.append(f"- {key:<8} {count:>5,}  {label}")
    lines.append("")
    lines.append("ITEM_* families:")
    for key, count in sorted(summary["item_families"].items(), key=lambda kv: (kv[0])):
        label = ITEM_FAMILY_LABELS.get(key, key)
        lines.append(f"- {key:<8} {count:>5,}  {label}")
    lines.append("")
    lines.append("Primary sources:")
    for url in SOURCE_URLS:
        lines.append(f"- {url}")
    return "\n".join(lines)
