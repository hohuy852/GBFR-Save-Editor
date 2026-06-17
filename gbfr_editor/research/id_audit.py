from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple
import csv

from gbfr_save import GBFRSaveData
from item_db import ItemDatabase
from resource_id_db import ResourceIdDatabase
from unit_meta import unit_name
from hashing import gbfr_hash

EMPTY_HASH = 0x887AE0B0

# Hash-like save fields we currently know/care about.
HASH_FIELD_INFO: Dict[int, Tuple[str, str, str]] = {
    1301: ("Character", "Character ID hash", "PL/NP character or NPC hash"),
    1901: ("Item", "Item bucket hash", "extra/bucket item hash"),
    2002: ("Item", "Item bucket hash", "item bucket hash"),
    2102: ("Item", "Inventory item hash", "inventory item hash"),
    2703: ("Sigil", "Sigil/Gem hash", "main sigil/gem hash"),
    2706: ("Sigil", "Sigil trait/property hash", "secondary sigil trait / property candidate"),
    2803: ("Weapon", "Weapon hash", "main weapon hash"),
    2815: ("Weapon", "Weapon equipped/owner hash", "character owner / equipped-by hash candidate"),
    2816: ("Weapon", "Weapon stone hash", "imbued stone / weapon item hash candidate"),
    3903: ("Ability", "Ability/action hash", "ability/action hash"),
}

# These are intentionally conservative. They produce IDs only when the custom hash
# exactly matches an unknown save value; names remain candidate/reserved unless known.
_CANDIDATE_PREFIXES = [
    ("PL", "Character/Player", range(0, 2601)),
    ("NP", "NPC/Story", range(0, 3001)),
    ("EM", "Enemy/Model", range(0, 9001)),
    ("WP", "Player Weapon Model", range(0, 2601)),
]


def _generate_candidate_map(values: Iterable[int], max_hits: int = 2000) -> Dict[int, List[str]]:
    wanted = {int(v) & 0xFFFFFFFF for v in values if int(v) not in (0, EMPTY_HASH)}
    out: Dict[int, List[str]] = {}
    if not wanted:
        return out
    for prefix, category, nums in _CANDIDATE_PREFIXES:
        for n in nums:
            text_id = f"{prefix}{n:04d}"
            hv = gbfr_hash(text_id)
            if hv in wanted:
                out.setdefault(hv, []).append(f"{text_id} · {category}")
                if sum(len(v) for v in out.values()) >= max_hits:
                    return out
    for n in range(0, 2601):
        for idx in range(0, 21):
            text_id = f"WEP_PL{n:04d}_{idx:02d}"
            hv = gbfr_hash(text_id)
            if hv in wanted:
                out.setdefault(hv, []).append(f"{text_id} · Weapon GBID")
                if sum(len(v) for v in out.values()) >= max_hits:
                    return out
    for prefix, category in [("GEEN", "Sigil/Gem GBID"), ("SKILL", "Trait/Skill GBID")]:
        for a in range(0, 400):
            for b in range(0, 40):
                text_id = f"{prefix}_{a:03d}_{b:02d}"
                hv = gbfr_hash(text_id)
                if hv in wanted:
                    out.setdefault(hv, []).append(f"{text_id} · {category}")
                    if sum(len(v) for v in out.values()) >= max_hits:
                        return out
    return out


def _lookup_resource_hash(resource_db: Optional[ResourceIdDatabase], value: int, manager: str):
    if not resource_db:
        return None
    # ResourceDatabase stores hash IDs as compact hex strings, whose decimal_value
    # matches the uint value if parsed as hex. Prefer relevant hash categories.
    category_hints = {
        "Ability": ["Action Hash", "Buff Hash", "Debuff/Ailment Hash", "Control Type Hash"],
        "Character": ["Model Player", "Model NPC", "Model Enemy"],
    }.get(manager, None)
    if category_hints:
        hit = resource_db.lookup_value(value, category_hints)
        if hit:
            return hit
    return resource_db.lookup_value(value)


def build_id_audit(save: GBFRSaveData, item_db: ItemDatabase, resource_db: Optional[ResourceIdDatabase] = None, include_empty: bool = False) -> List[Dict[str, object]]:
    grouped: Dict[Tuple[int, int], Dict[str, object]] = {}
    unknown_values: List[int] = []
    for rec in save.records:
        if rec.kind != "uint" or rec.id_type not in HASH_FIELD_INFO:
            continue
        vals = save.get_values(rec, min(rec.value_count, 2048))
        for i, raw in enumerate(vals):
            value = int(raw) & 0xFFFFFFFF
            if value in (0, EMPTY_HASH) and not include_empty:
                continue
            manager, label, note = HASH_FIELD_INFO.get(rec.id_type, ("Other", unit_name(rec.id_type), "hash-like field"))
            key = (rec.id_type, value)
            row = grouped.setdefault(key, {
                "manager": manager,
                "field_id": rec.id_type,
                "field_name": label,
                "hash": f"0x{value:08X}",
                "decimal": value,
                "status": "unresolved",
                "name": "",
                "gbid_or_id": "",
                "source": "",
                "occurrences": 0,
                "units": [],
                "note": note,
            })
            row["occurrences"] = int(row["occurrences"]) + 1
            units = row["units"]
            if isinstance(units, list) and len(units) < 12:
                units.append(f"{rec.unit_id}:{i}")
            if value not in (0, EMPTY_HASH) and not item_db.lookup_hash(value):
                unknown_values.append(value)

    candidates = _generate_candidate_map(unknown_values)
    for row in grouped.values():
        value = int(row["decimal"])
        manager = str(row["manager"])
        if value == 0:
            row.update(status="empty-zero", name="Empty / zero", source="save value")
            continue
        if value == EMPTY_HASH:
            row.update(status="empty-hash", name="Empty hash", source="save value")
            continue
        hit = item_db.lookup_hash(value)
        if hit:
            row.update(status="known-gbid", name=hit.display_name, gbid_or_id=hit.item_id, source="GBID database")
            continue
        res = _lookup_resource_hash(resource_db, value, manager)
        if res:
            row.update(status="known-resource", name=res.name, gbid_or_id=res.id_text, source=res.category)
            continue
        cand = candidates.get(value)
        if cand:
            row.update(status="candidate", name=" / ".join(cand[:3]), gbid_or_id=cand[0].split(" · ", 1)[0], source="generated pattern match")
            continue
        row.update(status="unresolved", name=f"Unknown {value:08X}", source="needs source/sample")

    rows = list(grouped.values())
    for row in rows:
        if isinstance(row.get("units"), list):
            row["units"] = ", ".join(row["units"])  # type: ignore[index]
    return sorted(rows, key=lambda r: (str(r["status"]), str(r["manager"]), int(r["field_id"]), str(r["hash"])))


def id_audit_summary(rows: List[Dict[str, object]]) -> str:
    from collections import Counter
    if not rows:
        return "No hash-like IDs found in the loaded save."
    by_status = Counter(str(r.get("status", "")) for r in rows)
    by_manager = Counter(str(r.get("manager", "")) for r in rows)
    unresolved = by_status.get("unresolved", 0)
    candidate = by_status.get("candidate", 0)
    lines = [
        "ID cleanup audit",
        "",
        f"Unique hash-like IDs: {len(rows):,}",
        f"Resolved GBIDs/resources: {by_status.get('known-gbid', 0) + by_status.get('known-resource', 0):,}",
        f"Generated candidates: {candidate:,}",
        f"Still unresolved: {unresolved:,}",
        "",
        "By status:",
    ]
    for status, count in sorted(by_status.items()):
        lines.append(f"  {status:<16} {count:,}")
    lines.extend(["", "By manager:"])
    for manager, count in sorted(by_manager.items()):
        lines.append(f"  {manager:<16} {count:,}")
    if unresolved:
        lines.extend(["", "Next research target: export unresolved rows, then make one in-game change at a time and compare saves."])
    return "\n".join(lines)


def write_id_audit_csv(rows: List[Dict[str, object]], path: str, unresolved_only: bool = False) -> None:
    fields = ["manager", "field_id", "field_name", "hash", "decimal", "status", "name", "gbid_or_id", "source", "occurrences", "units", "note"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            if unresolved_only and row.get("status") not in {"unresolved", "candidate"}:
                continue
            w.writerow({k: row.get(k, "") for k in fields})
