from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Dict, List
import csv
import re

from resource_id_db import ResourceIdDatabase, ResourceEntry
from hashing import gbfr_hash_hex

COMMUNITY_PHASE_ID_URL = ""

PHASE_GROUPS = [
    (re.compile(r"^P1", re.I), "Tempeal / early story"),
    (re.compile(r"^P2", re.I), "Grandcypher / Avia"),
    (re.compile(r"^P3", re.I), "Leautagne Island"),
    (re.compile(r"^P4", re.I), "Dahli Island"),
    (re.compile(r"^P5", re.I), "Phondam Isles / Vulkan Bolla"),
    (re.compile(r"^P6", re.I), "Seedhollow / Angra Mainyu"),
    (re.compile(r"^P7", re.I), "Pillar of Vayoi / Versa / Lucilius"),
    (re.compile(r"^P8", re.I), "Grandcypher story/battle variants"),
    (re.compile(r"^P9", re.I), "Sundappled Grove"),
    (re.compile(r"^PA", re.I), "Unknown A-series phases"),
    (re.compile(r"^PB", re.I), "Clouds / Id / misc story"),
    (re.compile(r"^PC", re.I), "Folca town"),
    (re.compile(r"^PD", re.I), "Seedhollow town"),
    (re.compile(r"^PE", re.I), "Grandcypher / travel / Rolan ship"),
    (re.compile(r"^PF", re.I), "System / menus / title / credits"),
]


def phase_group(phase_id: str, name: str = "") -> str:
    code = (phase_id or "").strip().upper()
    for pattern, label in PHASE_GROUPS:
        if pattern.match(code):
            return label
    return "Other / unknown phase"


def phase_entity_code(phase_id: str) -> str:
    code = (phase_id or "").strip()
    if not code:
        return ""
    if code.lower().startswith("ph"):
        return "ph" + code[2:]
    if code.lower().startswith("p"):
        return "ph" + code[1:]
    return code


def is_phase_entry(entry: ResourceEntry) -> bool:
    cat = (entry.category or "").strip().lower()
    ident = (entry.id_text or "").strip()
    return cat == "phase" or bool(re.match(r"^p[0-9a-f]{3}$", ident, flags=re.I))


def phase_rows(db: ResourceIdDatabase, text_filter: str = "") -> List[List[object]]:
    q = (text_filter or "").strip().lower()
    rows: List[List[object]] = []
    seen: set[str] = set()
    entries = sorted(db.entries, key=lambda r: ((r.id_text or "").upper(), r.name))
    for e in entries:
        if not is_phase_entry(e):
            continue
        phase_id = (e.id_text or "").strip()
        if not re.match(r"^p[0-9a-f]{3}$", phase_id, flags=re.I):
            continue
        key = phase_id.upper()
        if key in seen:
            continue
        seen.add(key)
        entity_code = phase_entity_code(phase_id)
        # Keep exact-case hash because the Community list uses lower-case p### phase IDs.
        phase_hash = gbfr_hash_hex(phase_id)
        entity_hash = gbfr_hash_hex(entity_code) if entity_code and entity_code.lower() != phase_id.lower() else ""
        group = phase_group(phase_id, e.name)
        row = ["Phase", group, e.name, phase_id, entity_code, phase_hash, entity_hash, e.source, e.alias_text]
        hay = " ".join(str(v) for v in row).lower()
        if q and not all(t in hay for t in q.split() if t):
            continue
        rows.append(row)
    return rows


def build_phase_summary(db: ResourceIdDatabase) -> Dict[str, object]:
    rows = phase_rows(db)
    counts = Counter(row[1] for row in rows)
    unknown_named = sum(1 for row in rows if str(row[2]).strip() in {"?", "(?)"} or str(row[2]).strip().startswith("?"))
    return {
        "total": len(rows),
        "groups": dict(sorted(counts.items())),
        "unknown_named": unknown_named,
        "source_urls": [COMMUNITY_PHASE_ID_URL],
    }


def format_phase_summary(db: ResourceIdDatabase) -> str:
    summary = build_phase_summary(db)
    lines = [
        "Community Phase IDs coverage",
        "=========================",
        f"Phase rows: {summary['total']:,}",
        f"Rows still marked unknown/questionable by source: {summary['unknown_named']:,}",
        "",
        "Phase groups:",
    ]
    for key, count in sorted(summary["groups"].items(), key=lambda kv: kv[0]):
        lines.append(f"- {key:<44} {count:>4,}")
    lines.append("")
    lines.append("Primary source:")
    for url in summary["source_urls"]:
        lines.append(f"- {url}")
    lines.append("")
    lines.append("Notes:")
    lines.append("- Phase ID is the p### code from the Community phase jump list.")
    lines.append("- Entity Code is the related ph### scripting-style prefix form used by entity-prefix docs.")
    lines.append("- Hash columns are generated locally and are mainly for resolving hash-like save fields or research scans.")
    return "\n".join(lines)


def write_phase_catalog_csv(db: ResourceIdDatabase, path: str | Path, text_filter: str = "") -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["category", "group", "name", "phase_id", "entity_code", "phase_hash", "entity_hash", "source", "aliases"])
        writer.writerows(phase_rows(db, text_filter))
