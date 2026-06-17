from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import csv
import json

from gbfr_save import GBFRSaveData, UnitRecord
from gbid_tools import HASHISH_ID_TYPES
from item_db import ItemDatabase
from unit_labeler import manager_for_record, contextual_unit_name, UnitLabelIndex
from unit_meta import unit_name

# Save-unit families from Community's save unit research notes. The editor uses these
# as a map/coverage layer; exact field semantics still stay conservative.
MANAGER_FIELD_NOTES: Dict[str, Dict[int, str]] = {
    "UserDataManager": {
        1101: "user data scalar", 1102: "user data scalar", 1103: "user data scalar",
        1104: "user data scalar", 1105: "user data scalar", 1106: "user data scalar",
        1107: "user data scalar", 1108: "user data scalar", 1109: "user data scalar",
        1110: "user data scalar", 1111: "user data scalar", 1112: "user data scalar",
        1113: "user data scalar", 1114: "user data scalar", 1151: "user data scalar",
        1201: "user data scalar", 1202: "user data scalar", 1203: "user data scalar",
        1204: "user data scalar", 1205: "user data scalar", 1206: "user data scalar", 1207: "user data scalar",
    },
    "Character": {
        1301: "character/player GBID hash", 1302: "character state", 1303: "character state", 1304: "character state",
        1305: "character state", 1307: "character state", 1308: "character level", 1309: "character state",
        1310: "character state", 1311: "character state", 1312: "character state", 1313: "character state",
        1314: "character state", 1315: "character state", 1316: "character state", 1317: "character state",
        1318: "character state", 1321: "character state", 1322: "character state", 1402: "character param/state",
        1403: "character param/state", 1404: "character param/state", 1501: "character progression/state",
        1502: "character progression/state", 1503: "character progression/state", 1601: "character unlock/progression state",
        1602: "character unlock/progression state", 1603: "character unlock/progression state", 1604: "character unlock/progression state",
        1605: "character unlock/progression state", 1606: "character unlock/progression state", 1607: "character unlock/progression state",
    },
    "Character Loadout": {
        3001: "loadout slot/state", 3002: "loadout slot/state", 3003: "loadout party/loadout state", 3004: "loadout party/loadout state",
        3101: "character mastery/page state", 3102: "character mastery/page state", 3103: "character mastery/page state",
        3104: "character mastery/page state", 3105: "character mastery/page state", 3106: "character mastery/page state",
        3107: "character mastery/page state", 3108: "character mastery/page state", 3401: "loadout detail state", 3402: "loadout detail state",
    },
    "Weapon": {
        2801: "max/slot metadata", 2802: "weapon slot/index", 2803: "weapon GBID hash", 2804: "weapon XP/progress",
        2805: "weapon state candidate", 2806: "weapon state candidate", 2807: "weapon state candidate", 2813: "weapon extra state",
        2814: "weapon state candidate", 2815: "weapon flags", 2816: "imbued stone/item hash", 7401: "weapon unlock/tracking",
        7402: "weapon unlock/tracking", 7403: "weapon unlock/tracking",
    },
    "Sigil": {
        2701: "max/slot metadata", 2702: "sigil slot id", 2703: "sigil/gem GBID hash", 2704: "primary skill level",
        2706: "equipped-by character hash", 2707: "sigil flags/lock bit candidate", 2708: "sigil extra state",
        8001: "sigil extra loop field", 8002: "sigil extra loop field",
    },
    "Item": {
        1801: "item category field", 1802: "item category field", 1803: "item category field", 1804: "item category field",
        1805: "item category field", 1806: "item category field", 1807: "item category field", 1901: "extra item hash candidate",
        1902: "extra item serial/index candidate", 1903: "extra item quantity/type candidate", 1904: "extra item flags candidate",
        2001: "item bucket metadata", 2002: "bucket item hash candidate", 2003: "bucket item serial/index candidate",
        2004: "bucket item quantity/type candidate", 2101: "inventory metadata", 2102: "inventory item hash",
        2103: "inventory item serial/index candidate", 2104: "inventory item flags candidate", 2105: "inventory quantity/type candidate",
        6801: "item junk/curio/journal state candidate",
    },
    "Ability": {3903: "ability/action hash", 3904: "ability flags/equip state"},
    "Scenario": {4201: "scenario progress", 4202: "scenario id/progress"},
    "Quest": {k: "quest state/progression" for k in list(range(2501, 2523)) + [2530, 2550, 2551, 2552, 2553, 2554, 2555, 2560, 2561, 2562, 2563, 2570, 2571, 2572, 2573, 2574, 2575, 2576, 2577, 2580, 2581, 2582, 2583]},
    "Party": {2201: "party member field", 2202: "party member field", 2203: "party member field", 2301: "party preset field", 2302: "party preset field", 2401: "party state", 2402: "party state", 3003: "party loadout field", 3004: "party loadout field"},
    "Options": {4315: "difficulty option", 4316: "assist mode option", 4317: "auto-save option", 4318: "dialogue autoplay option", 4319: "screen shake option"},
    "Filter/Sort": {3601: "filter/sort state", 3701: "filter/sort state", 3801: "filter/sort loop", 3802: "filter/sort loop"},
    "Cycle Trade": {4001: "trade/exchange loop", 4002: "trade/exchange loop", 4003: "trade/exchange loop", 4004: "trade/exchange metadata", 4101: "trade/exchange loop", 4102: "trade/exchange loop", 4103: "trade/exchange loop", 4104: "trade/exchange loop", 4105: "trade/exchange loop", 4106: "trade/exchange loop", 4107: "trade/exchange loop"},
    "Tutorial": {5901: "tutorial flag", 5902: "tutorial flag", 6001: "tutorial flag", 6002: "tutorial flag"},
    "Island": {6101: "island state", 6102: "island state"},
    "Shop": {6901: "shop state", 6902: "shop state"},
    "Gacha": {7001: "transmute/gacha state", 7002: "transmute/gacha state", 7003: "transmute/gacha state"},
    "Communication": {6201: "communication state", 6202: "communication state", 6301: "communication loop", 6302: "communication loop", 7501: "communication loop", 7502: "communication loop", 7503: "communication loop", 7504: "communication loop", 7701: "communication row", 7702: "communication row", 7703: "communication row"},
    "UI": {4502: "ui/network state", 4503: "ui/network state", 4504: "ui/network state", 4505: "ui/network state", 4506: "ui/network state", 4507: "ui/network state", 4601: "ui/userdata shared state", 4602: "ui/userdata shared state", 4603: "ui/userdata shared state", 4604: "ui/userdata shared state", 4605: "ui/userdata shared state", 4606: "ui/userdata shared state", 4607: "ui/userdata shared state", 4701: "ui/network state", 4702: "ui/network state", 4703: "ui/network state", 4704: "ui/network state", 4705: "ui/network state", 4706: "ui/network state", 4707: "ui/network state", 4708: "ui/network state", 4801: "ui/network loop", 4802: "ui/network loop", 4803: "ui/network loop", 4804: "ui/network loop", 4901: "ui/network state", 5001: "ui/network state", 5002: "ui/network state", 5003: "ui/network state", 7101: "unlock ui row", 7102: "unlock ui row", 7103: "unlock ui row", 7201: "ui misc", 7202: "ui misc", 7203: "ui misc", 7204: "ui misc", 7301: "information dialog loop", 7302: "information dialog loop", 7303: "information dialog state", 7304: "information dialog state", 7305: "information dialog state", 7306: "information dialog state", 7307: "information dialog state", 7308: "information dialog state", 7309: "information dialog state", 7310: "information dialog state", 7351: "information dialog loop", 7352: "information dialog loop"},
    "Archive": {7901: "archive/codex state", 7902: "archive/codex state", 8101: "word list state", 8102: "word list state", 8201: "main story archive state", 8202: "main story archive state", 8301: "bgm archive state", 8302: "bgm archive state", 8401: "character picture book", 8402: "character picture book", 8501: "enemy picture book", 8502: "enemy picture book", 8601: "pendulum picture book", 8602: "pendulum picture book", 8701: "tips state", 8702: "tips state", 8801: "command list state", 8802: "command list state"},
    "Playlog": {8901: "playlog stat", 8902: "playlog stat", 8903: "playlog stat", 8904: "playlog stat", 8905: "playlog stat", 8906: "playlog stat", 8907: "playlog stat", 8908: "playlog stat", 8909: "playlog stat", 8910: "playlog stat", 8911: "playlog stat", 8912: "playlog stat", 8913: "playlog stat", 8914: "playlog stat"},
    "Fate Episode": {3501: "fate episode id/state", 3502: "fate episode flags/progress"},
    "Random": {7601: "random seed/state"},
}


# Extra labels observed in real saves during the mapper pass.
MANAGER_FIELD_NOTES.setdefault("Save System", {}).update({
    911: "profile/header scalar", 912: "profile/header scalar", 913: "profile/header scalar",
    914: "profile/header scalar", 915: "profile/header scalar", 916: "profile/header scalar",
    917: "profile/header scalar", 918: "profile/header scalar", 919: "profile/header scalar",
    1001: "save system version/flag", 1002: "save system version/flag", 1003: "active hash seed",
})
MANAGER_FIELD_NOTES.setdefault("Record Meta", {}).update({1701: "shared record metadata A", 1702: "shared record metadata B"})
MANAGER_FIELD_NOTES.setdefault("Quest/System", {}).update({2590: "quest/system global state", 2591: "quest/system global state", 2592: "quest/system global state", 2594: "quest/system global state", 2595: "quest/system global state", 2596: "quest/system global state", 2600: "quest/system flag candidate"})
MANAGER_FIELD_NOTES.setdefault("Weapon", {}).update({2901: "weapon global metadata", 2902: "weapon global metadata", 2903: "weapon global metadata", 2904: "weapon global metadata", 2907: "weapon global metadata"})
MANAGER_FIELD_NOTES.setdefault("Character Loadout", {}).update({3201: "character mastery/loadout observed field", 3301: "character mastery/loadout observed field", 3302: "character mastery/loadout observed field", 3303: "character mastery/loadout observed field", 3304: "character mastery/loadout observed field", 3305: "character mastery/loadout observed field"})
MANAGER_FIELD_NOTES.setdefault("Options", {}).update({k: "option/profile setting candidate" for k in range(4300, 4338)})

EXPECTED_BY_FIELD: Dict[int, List[Tuple[str, str]]] = defaultdict(list)
for _manager, _fields in MANAGER_FIELD_NOTES.items():
    for _field, _note in _fields.items():
        EXPECTED_BY_FIELD[_field].append((_manager, _note))


def expected_note_for(group: str, id_type: int) -> str:
    if group in MANAGER_FIELD_NOTES and id_type in MANAGER_FIELD_NOTES[group]:
        return MANAGER_FIELD_NOTES[group][id_type]
    entries = EXPECTED_BY_FIELD.get(id_type, [])
    if entries:
        return "; ".join(f"{g}: {n}" for g, n in entries[:3])
    return "not mapped yet"


def field_confidence(group: str, id_type: int, kind: str) -> str:
    if id_type in HASHISH_ID_TYPES:
        return "hash-field"
    if group in MANAGER_FIELD_NOTES and id_type in MANAGER_FIELD_NOTES[group]:
        note = MANAGER_FIELD_NOTES[group][id_type]
        if "candidate" in note or "unknown" in unit_name(id_type).lower():
            return "candidate"
        return "documented"
    if EXPECTED_BY_FIELD.get(id_type):
        return "cross-listed"
    if group == "Other" or unit_name(id_type).startswith("UNKNOWN_"):
        return "unknown"
    return "fallback"


def _sample_values(save: GBFRSaveData, rec: UnitRecord, limit: int = 4) -> str:
    try:
        vals = save.get_values(rec, limit)
    except Exception:
        return ""
    text = ", ".join(str(v) for v in vals)
    if rec.value_count > limit:
        text += ", ..."
    return text


def _hash_stats(save: GBFRSaveData, item_db: ItemDatabase, records: List[UnitRecord]) -> Tuple[int, int, int]:
    known = unknown = empty = 0
    for rec in records:
        if rec.kind != "uint" or rec.id_type not in HASHISH_ID_TYPES:
            continue
        for value in save.get_values(rec):
            try:
                iv = int(value) & 0xFFFFFFFF
            except Exception:
                continue
            if iv in (0, 0x887AE0B0):
                empty += 1
            elif item_db.lookup_hash(iv):
                known += 1
            else:
                unknown += 1
    return known, unknown, empty


def build_save_map(save: GBFRSaveData, item_db: Optional[ItemDatabase] = None, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Build a manager/field map of a loaded save.

    Rows are grouped by manager, scalar kind, and save-unit field id. This is the
    most useful high-level view when mapping unknown fields from before/after
    saves without reading every raw scalar row.
    """
    item_db = item_db or ItemDatabase()
    grouped: Dict[Tuple[str, str, int], List[UnitRecord]] = defaultdict(list)
    for rec in save.records:
        grouped[(manager_for_record(rec), rec.kind, rec.id_type)].append(rec)

    labels = UnitLabelIndex.from_save(save, item_db)
    rows: List[Dict[str, Any]] = []
    for (manager, kind, id_type), records in grouped.items():
        units = sorted({r.unit_id for r in records})
        value_counts = [r.value_count for r in records]
        known, unknown, empty = _hash_stats(save, item_db, records)
        sample_rec = records[0]
        label_samples: List[str] = []
        for rec in records[:5]:
            label = labels.label_for(rec) or contextual_unit_name(manager, rec.id_type, rec.unit_id, labels.character_names)
            if label and label not in label_samples:
                label_samples.append(label)
        unit_span = ""
        if units:
            unit_span = str(units[0]) if len(units) == 1 else f"{units[0]}–{units[-1]}"
        rows.append({
            "manager": manager,
            "kind": kind,
            "field_id": id_type,
            "field_name": unit_name(id_type),
            "confidence": field_confidence(manager, id_type, kind),
            "records": len(records),
            "unit_span": unit_span,
            "first_unit": units[0] if units else "",
            "last_unit": units[-1] if units else "",
            "unique_units": len(units),
            "value_count_min": min(value_counts) if value_counts else 0,
            "value_count_max": max(value_counts) if value_counts else 0,
            "known_hashes": known,
            "unknown_hashes": unknown,
            "empty_hashes": empty,
            "sample_units": "; ".join(label_samples),
            "sample_values": _sample_values(save, sample_rec),
            "note": expected_note_for(manager, id_type),
        })
    order = {"Character": 0, "Character Loadout": 1, "Weapon": 2, "Sigil": 3, "Item": 4, "Ability": 5, "Quest": 6, "Scenario": 7, "Party": 8, "Options": 9, "UI": 10, "Archive": 11, "Shop": 12, "Gacha": 13, "Playlog": 14, "Other": 99}
    rows.sort(key=lambda r: (order.get(str(r["manager"]), 80), str(r["manager"]), int(r["field_id"]), str(r["kind"])))
    return rows if limit is None else rows[:limit]


def build_unknown_field_report(save: GBFRSaveData, item_db: Optional[ItemDatabase] = None, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    rows = [r for r in build_save_map(save, item_db) if r["confidence"] in {"unknown", "candidate", "fallback"} or int(r.get("unknown_hashes", 0)) > 0]
    return rows if limit is None else rows[:limit]


def save_map_summary_text(save: GBFRSaveData, item_db: Optional[ItemDatabase] = None) -> str:
    rows = build_save_map(save, item_db)
    by_manager = Counter(r["manager"] for r in rows)
    by_conf = Counter(r["confidence"] for r in rows)
    unknown_hashes = sum(int(r.get("unknown_hashes", 0)) for r in rows)
    known_hashes = sum(int(r.get("known_hashes", 0)) for r in rows)
    lines = [
        "Save Map Summary",
        "",
        f"Field groups mapped: {len(rows)}",
        f"Known hash values found: {known_hashes}",
        f"Unknown hash-like values found: {unknown_hashes}",
        "",
        "Managers",
    ]
    for manager, count in by_manager.most_common():
        lines.append(f"- {manager}: {count} field groups")
    lines += ["", "Confidence"]
    for conf, count in by_conf.most_common():
        lines.append(f"- {conf}: {count}")
    lines += ["", "Next research targets"]
    for row in build_unknown_field_report(save, item_db, limit=12):
        lines.append(f"- {row['manager']} / {row['field_id']} {row['field_name']} ({row['confidence']}): {row['note']}")
    return "\n".join(lines)


def write_save_map_csv(save: GBFRSaveData, item_db: Optional[ItemDatabase], path: str | Path, *, unknown_only: bool = False) -> None:
    rows = build_unknown_field_report(save, item_db) if unknown_only else build_save_map(save, item_db)
    headers = ["manager", "kind", "field_id", "field_name", "confidence", "records", "unit_span", "unique_units", "value_count_min", "value_count_max", "known_hashes", "unknown_hashes", "empty_hashes", "sample_units", "sample_values", "note"]
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_save_map_json(save: GBFRSaveData, item_db: Optional[ItemDatabase], path: str | Path, *, unknown_only: bool = False) -> None:
    rows = build_unknown_field_report(save, item_db) if unknown_only else build_save_map(save, item_db)
    payload = {"summary": save.summary(), "rows": rows}
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
