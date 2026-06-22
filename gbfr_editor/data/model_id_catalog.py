from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List
import csv
import re

from resource_id_db import ResourceIdDatabase, ResourceEntry
from hashing import gbfr_hash, gbfr_hash_hex

COMMUNITY_MODEL_ID_URL = ""

MODEL_CATEGORIES = {
    "Model Player",
    "Model NPC",
    "Model Enemy",
    "Model Map Animated",
    "Model Breakable Prop",
    "Model Player Weapon",
    "Enemy Weapon",
    "Model Extra",
}

CATEGORY_LABELS = {
    "Model Player": "Player bodies",
    "Model NPC": "NPC bodies",
    "Model Enemy": "Enemy bodies / parts",
    "Model Map Animated": "Animated map objects",
    "Model Breakable Prop": "Breakable props",
    "Model Player Weapon": "Player weapon models",
    "Enemy Weapon": "Enemy weapon models",
    "Model Extra": "Extra / misc objects",
}

# Deduplicate the old generic Model PL rows when the cleaner Model Player row exists.
GENERIC_MODEL_UPGRADE = {
    "PL": "Model Player",
    "NP": "Model NPC",
    "EM": "Model Enemy",
    "BA": "Model Map Animated",
    "BH": "Model Breakable Prop",
    "WP": "Model Player Weapon",
    "WE": "Enemy Weapon",
}


def normalize_model_category(entry: ResourceEntry) -> str:
    cat = (entry.category or "").strip()
    if cat in MODEL_CATEGORIES:
        return cat
    ident = (entry.id_text or "").strip().upper()
    if cat == "Model":
        m = re.match(r"^([A-Z]{2})", ident)
        if m:
            return GENERIC_MODEL_UPGRADE.get(m.group(1), cat)
    return cat


def is_model_entry(entry: ResourceEntry) -> bool:
    cat = normalize_model_category(entry)
    if cat in MODEL_CATEGORIES:
        return True
    ident = (entry.id_text or "").strip().upper()
    return bool(re.match(r"^(PL|NP|EM|BA|BH|WP)[0-9A-F]{4}$", ident))


def model_rows(db: ResourceIdDatabase, text_filter: str = "") -> List[List[object]]:
    q = (text_filter or "").strip().lower()
    seen: set[tuple[str, str]] = set()
    rows: List[List[object]] = []
    for e in sorted(db.entries, key=lambda r: (normalize_model_category(r), r.id_upper, r.name)):
        if not is_model_entry(e):
            continue
        category = normalize_model_category(e)
        model_id = (e.id_text or "").strip().upper()
        # Enemy weapon rows on Community are numeric IDs; keep them searchable but do not generate fake WE#### IDs.
        hash_hex = gbfr_hash_hex(model_id) if re.match(r"^[A-Z]{2,3}[0-9A-F]{4}$", model_id) else ""
        key = (category.lower(), model_id)
        if key in seen:
            continue
        seen.add(key)
        group = CATEGORY_LABELS.get(category, category)
        aliases = e.alias_text
        row = [category, group, e.name, model_id, hash_hex, e.decimal_value if e.decimal_value is not None else "", aliases, e.source]
        hay = " ".join(str(v) for v in row).lower()
        if q and not all(t in hay for t in q.split() if t):
            continue
        rows.append(row)
    return rows


def build_model_summary(db: ResourceIdDatabase) -> Dict[str, object]:
    rows = model_rows(db)
    counts = Counter(row[0] for row in rows)
    hashed = sum(1 for row in rows if row[4])
    return {
        "total": len(rows),
        "hashed": hashed,
        "categories": dict(sorted(counts.items())),
        "source_urls": [COMMUNITY_MODEL_ID_URL],
    }


def format_model_summary(db: ResourceIdDatabase) -> str:
    summary = build_model_summary(db)
    lines = [
        "Community Model IDs coverage",
        "=========================",
        f"Model/resource rows: {summary['total']:,}",
        f"Rows with generated GBFR hash: {summary['hashed']:,}",
        "",
        "Model groups:",
    ]
    for key, count in sorted(summary["categories"].items(), key=lambda kv: (kv[0])):
        lines.append(f"- {key:<24} {count:>5,}  {CATEGORY_LABELS.get(key, key)}")
    lines.append("")
    lines.append("Primary source:")
    for url in summary["source_urls"]:
        lines.append(f"- {url}")
    return "\n".join(lines)


def write_model_catalog_csv(db: ResourceIdDatabase, path: str | Path, text_filter: str = "") -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["category", "group", "name", "model_id", "gbfr_hash", "decimal", "aliases", "source"])
        writer.writerows(model_rows(db, text_filter))
