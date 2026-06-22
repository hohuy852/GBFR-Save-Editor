from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Dict, List
import csv
import re

from resource_id_db import ResourceIdDatabase, ResourceEntry

COMMUNITY_QUEST_ID_URL = ""
COMMUNITY_QUEST_ID_CSV_URL = ""

QUEST_GROUPS = {
    "1": "Main Quest / Story",
    "2": "Challenge / Side Quest",
    "3": "Fate Episode",
    "4": "Multiplayer / Quest Counter",
    "5": "Towns / Lobbies",
    "6": "Dummy / Practice",
    "7": "Short Story / Misc",
}


def quest_group(quest_id: str, name: str = "") -> str:
    code = (quest_id or "").strip().upper()
    if not code:
        return "Other / unknown quest"
    if code.startswith("290"):
        return "Challenge / Side Quest - Crustacean chain"
    if code.startswith("30") or code.startswith("301"):
        return "Fate Episode"
    if code.startswith("40"):
        return "Multiplayer / Quest Counter"
    if code.startswith("10"):
        # Keep story/lobby jump rows readable.
        if "grandcypher" in (name or "").lower() or code.endswith("F00") or code.endswith("F01"):
            return "Main Quest / Story - Grandcypher or travel"
        return "Main Quest / Story"
    return QUEST_GROUPS.get(code[0], "Other / unknown quest")


def quest_sort_key(quest_id: str) -> tuple[int, str]:
    text = (quest_id or "").strip().upper()
    try:
        return (int(text, 16 if re.search(r"[A-F]", text) else 10), text)
    except Exception:
        return (10**9, text)


def is_quest_entry(entry: ResourceEntry) -> bool:
    cat = (entry.category or "").strip().lower()
    ident = (entry.id_text or "").strip().upper()
    return cat in {"quest", "quest/stage", "quest stage", "quest ids"} or bool(re.match(r"^[1-7][0-9A-F]{5}$", ident, flags=re.I))


def quest_rows(db: ResourceIdDatabase, text_filter: str = "") -> List[List[object]]:
    q = (text_filter or "").strip().lower()
    rows: List[List[object]] = []
    seen: set[str] = set()
    entries = sorted(db.entries, key=lambda r: quest_sort_key(r.id_text))
    for e in entries:
        if not is_quest_entry(e):
            continue
        qid = (e.id_text or "").strip().upper()
        if not re.match(r"^[1-7][0-9A-F]{5}$", qid, flags=re.I):
            continue
        if qid in seen:
            continue
        seen.add(qid)
        # Quest/stage IDs are save keys. Treat all six-character quest IDs as
        # hexadecimal values even when they contain only digits, because the save
        # vectors store e.g. 100000 as 0x100000, not decimal 100000.
        try:
            dec = int(qid, 16)
        except Exception:
            dec = e.decimal_value
        dec_text = dec if dec is not None else ""
        encoded = f"0x{dec:X}" if dec is not None else ""
        group = quest_group(qid, e.name)
        row = ["Quest/Stage", group, e.name, qid, dec_text, encoded, e.source, e.alias_text]
        hay = " ".join(str(v) for v in row).lower()
        if q and not all(t in hay for t in q.split() if t):
            continue
        rows.append(row)
    return rows


def build_quest_summary(db: ResourceIdDatabase) -> Dict[str, object]:
    rows = quest_rows(db)
    counts = Counter(row[1] for row in rows)
    return {
        "total": len(rows),
        "groups": dict(sorted(counts.items())),
        "source_urls": [COMMUNITY_QUEST_ID_URL, COMMUNITY_QUEST_ID_CSV_URL],
    }


def format_quest_summary(db: ResourceIdDatabase) -> str:
    summary = build_quest_summary(db)
    lines = [
        "Community Quest IDs coverage",
        "=========================",
        f"Quest/stage rows: {summary['total']:,}",
        "",
        "Quest groups:",
    ]
    for key, count in sorted(summary["groups"].items(), key=lambda kv: kv[0]):
        lines.append(f"- {key:<50} {count:>4,}")
    lines.append("")
    lines.append("Primary sources:")
    for url in summary["source_urls"]:
        lines.append(f"- {url}")
    lines.append("")
    lines.append("Notes:")
    lines.append("- The first digit identifies broad quest type: 1 story, 2 side/challenge, 3 Fate, 4 quest counter, 5 town/lobby, 6 practice, 7 misc.")
    lines.append("- Numeric Value is the save-friendly integer interpretation. Hex-like IDs such as 101F00 are also shown as encoded values.")
    return "\n".join(lines)


def write_quest_catalog_csv(db: ResourceIdDatabase, path: str | Path, text_filter: str = "") -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["category", "group", "name", "quest_id", "numeric_value", "encoded_hex", "source", "aliases"])
        writer.writerows(quest_rows(db, text_filter))
