from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Set

from gbfr_save import GBFRSaveData
from hashing import gbfr_hash
from item_db import ItemDatabase

HASH_FIELD_IDS = {1301, 2703, 2803, 2815, 3903, 2102, 1901, 2002}

@dataclass(frozen=True)
class HashCandidate:
    hash_hex: str
    hash_value: int
    text_id: str
    category: str
    confidence: str
    source: str


def _candidate_strings() -> Iterable[tuple[str, str, str]]:
    # Player/NPC/enemy/model hashes seen in character/worn-by/save references.
    for prefix, category, max_n in [
        ("PL", "Character/Player", 2600),
        ("NP", "NPC/Story", 2300),
        ("EM", "Enemy/Model", 9000),
        ("WP", "Player Weapon Model", 2600),
    ]:
        for n in range(max_n + 1):
            yield f"{prefix}{n:04d}", category, "generated model/entity ID"

    # Save weapon GBIDs use WEP_PL####_##. The public DB covers many of them;
    # this fills in future/unused rows when they appear in a save before we have names.
    for n in range(0, 2601):
        for idx in range(0, 21):
            yield f"WEP_PL{n:04d}_{idx:02d}", "Weapon GBID", "generated weapon GBID"

    # Sigil/skill IDs usually use GEEN_###_## or SKILL_###_##.
    for prefix, category in [("GEEN", "Sigil/Gem GBID"), ("SKILL", "Trait/Skill GBID")]:
        for a in range(0, 400):
            for b in range(0, 40):
                yield f"{prefix}_{a:03d}_{b:02d}", category, "generated sigil/skill GBID"


def unknown_hash_values(save: GBFRSaveData, db: ItemDatabase) -> Set[int]:
    out: Set[int] = set()
    for rec in save.records:
        if rec.kind != "uint" or rec.id_type not in HASH_FIELD_IDS:
            continue
        vals = save.get_values(rec, 1)
        if not vals:
            continue
        value = int(vals[0]) & 0xFFFFFFFF
        if value and db.lookup_hash(value) is None:
            out.add(value)
    return out


def resolve_unknown_hashes(save: GBFRSaveData, db: ItemDatabase, limit: int = 5000) -> List[HashCandidate]:
    unknown = unknown_hash_values(save, db)
    rows: List[HashCandidate] = []
    if not unknown:
        return rows
    seen: Set[tuple[int, str]] = set()
    for text_id, category, source in _candidate_strings():
        hv = gbfr_hash(text_id)
        if hv in unknown and (hv, text_id) not in seen:
            seen.add((hv, text_id))
            rows.append(HashCandidate(f"{hv:08X}", hv, text_id, category, "pattern-match", source))
            if len(rows) >= limit:
                break
    return sorted(rows, key=lambda r: (r.category, r.text_id, r.hash_hex))


def format_hash_candidates(rows: List[HashCandidate]) -> str:
    if not rows:
        return "No generated hash candidates matched the save's unknown hash fields."
    lines = ["Generated hash candidates:", ""]
    for r in rows:
        lines.append(f"{r.hash_hex}  {r.text_id:<18} {r.category:<22} {r.confidence}  # {r.source}")
    return "\n".join(lines)


def write_hash_candidates_csv(rows: List[HashCandidate], path: str) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["hash", "decimal", "id", "category", "confidence", "source"])
        for r in rows:
            w.writerow([r.hash_hex, r.hash_value, r.text_id, r.category, r.confidence, r.source])
