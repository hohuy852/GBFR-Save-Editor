from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from gbfr_save import GBFRSaveData, UnitRecord
from item_db import ItemDatabase

EMPTY_HASH = 0x887AE0B0

CHARACTER_IDS = {1301, 1302, 1303, 1304, 1305, 1307, 1308, 1309, 1310, 1311, 1312, 1313, 1314, 1315, 1316, 1317, 1318, 1321, 1322, 1402, 1403, 1404, 1501, 1502, 1503, 1601, 1602, 1603, 1604, 1605, 1606, 1607}
CHARACTER_LOADOUT_IDS = {3001, 3002, 3003, 3004, 3101, 3102, 3103, 3104, 3105, 3106, 3107, 3108, 3201, 3301, 3302, 3303, 3304, 3305, 3401, 3402}
WEAPON_IDS = {2801, 2802, 2803, 2804, 2805, 2806, 2807, 2813, 2814, 2815, 2816, 2901, 2902, 2903, 2904, 2907, 7401, 7402, 7403}
SIGIL_IDS = {2701, 2702, 2703, 2704, 2706, 2707, 2708, 8001, 8002}
ITEM_IDS = {1801, 1802, 1803, 1804, 1805, 1806, 1807, 1901, 1902, 1903, 1904, 2001, 2002, 2003, 2004, 2101, 2102, 2103, 2104, 2105, 6801}
ABILITY_IDS = {3903, 3904}
SCENARIO_IDS = {4201, 4202}
QUEST_IDS = set(range(2501, 2523)) | {2530, 2550, 2551, 2552, 2553, 2554, 2555, 2560, 2561, 2562, 2563, 2570, 2571, 2572, 2573, 2574, 2575, 2576, 2577, 2580, 2581, 2582, 2583}
PARTY_IDS = {2201, 2202, 2203, 2301, 2302, 2401, 2402, 3003, 3004}
OPTION_IDS = set(range(4300, 4338)) | {4315, 4316, 4317, 4318, 4319}
FILTER_SORT_IDS = {3601, 3701, 3801, 3802}
CYCLE_TRADE_IDS = {4001, 4002, 4003, 4004, 4101, 4102, 4103, 4104, 4105, 4106, 4107}
TUTORIAL_IDS = {5901, 5902, 6001, 6002}
ISLAND_IDS = {6101, 6102}
SHOP_IDS = {6901, 6902}
GACHA_IDS = {7001, 7002, 7003}
COMMUNICATION_IDS = {6201, 6202, 6301, 6302, 7501, 7502, 7503, 7504, 7701, 7702, 7703}
UI_IDS = set(range(7101, 7104)) | set(range(7301, 7311)) | {4502,4503,4504,4505,4506,4507,4601,4602,4603,4604,4605,4606,4607,4701,4702,4703,4704,4705,4706,4707,4708,4801,4802,4803,4804,4901,5001,5002,5003,7201,7202,7203,7204,7351,7352}
ARCHIVE_IDS = {7901,7902,8101,8102,8201,8202,8301,8302,8401,8402,8501,8502,8601,8602,8701,8702,8801,8802}
PLAYLOG_IDS = set(range(8901, 8915))


@dataclass(frozen=True)
class UnitLabel:
    group: str
    unit_id: int
    name: str
    source: str = ""
    hash_hex: str = ""
    gbid: str = ""
    fields: str = ""

    @property
    def display(self) -> str:
        if self.gbid and self.gbid not in self.name:
            return f"{self.name} ({self.gbid})"
        return self.name


class UnitLabelIndex:
    def __init__(self) -> None:
        self.labels: Dict[Tuple[str, int], UnitLabel] = {}
        self.character_names: Dict[int, str] = {}

    @classmethod
    def empty(cls) -> "UnitLabelIndex":
        return cls()

    @classmethod
    def from_save(cls, save: Optional[GBFRSaveData], db: Optional[ItemDatabase]) -> "UnitLabelIndex":
        idx = cls()
        if not save:
            return idx
        db = db or ItemDatabase()
        idx._add_characters(save, db)
        idx._add_hash_group(save, db, "Weapon", [2802,2803,2814,2815,2804,2805,2806,2807,2816,2813,1701,1702,7401,7402,7403], 2803, "2803 weapon hash")
        idx._add_hash_group(save, db, "Sigil", [2702,2703,2704,2706,2707,2708,1701,1702,8001,8002], 2703, "2703 sigil/gem hash")
        idx._add_item_groups(save, db)
        idx._add_hash_group(save, db, "Ability", [3903,3904], 3903, "3903 ability hash")
        idx._add_range_fallbacks(save)
        return idx

    def _first_value(self, save: GBFRSaveData, rec: Optional[UnitRecord], default: Any = None) -> Any:
        if not rec or rec.value_count < 1:
            return default
        try:
            return save.get_values(rec, 1)[0]
        except Exception:
            return default

    def _lookup(self, db: ItemDatabase, value: Any) -> tuple[str, str, str]:
        try:
            iv = int(value) & 0xFFFFFFFF
        except Exception:
            return str(value), "", ""
        if iv in (0, EMPTY_HASH):
            return "Empty", "", f"{iv:08X}"
        entry = db.lookup_hash(iv)
        if entry:
            return entry.display_name, entry.item_id, entry.hash_hex
        return f"Unknown 0x{iv:08X}", "", f"{iv:08X}"

    def _present_fields(self, fields: Dict[int, UnitRecord]) -> str:
        return ", ".join(str(k) for k in sorted(fields.keys()))

    def _put(self, label: UnitLabel) -> None:
        key = (label.group.lower(), int(label.unit_id))
        old = self.labels.get(key)
        if old is None or self._score(label) >= self._score(old):
            self.labels[key] = label

    def _score(self, label: UnitLabel) -> int:
        name = label.name or ""
        score = len(name)
        if label.gbid:
            score += 70
        if label.hash_hex:
            score += 10
        if "Unknown" in name:
            score -= 35
        if "Empty" in name:
            score -= 100
        if "unit-id range" in label.source:
            score -= 8
        if "slot fallback" in label.source:
            score -= 12
        return score

    def _add_characters(self, save: GBFRSaveData, db: ItemDatabase) -> None:
        grouped = save.group_by_unit(sorted(CHARACTER_IDS))
        for unit_id, fields in grouped.items():
            rec = fields.get(1301)
            value = self._first_value(save, rec, None)
            if value is None:
                continue
            name, gbid, hx = self._lookup(db, value)
            if name == "Empty":
                continue
            slot = character_slot_name(unit_id)
            if name.startswith("Unknown"):
                display = f"{slot}: {name}" if slot else f"Character: {name}"
            else:
                display = f"{slot}: {name}" if slot else f"Character: {name}"
                if 10000 <= unit_id <= 10039:
                    self.character_names[unit_id - 10000] = name
            self._put(UnitLabel("Character", unit_id, display, "1301 character hash", hx, gbid, self._present_fields(fields)))

    def _add_hash_group(self, save: GBFRSaveData, db: ItemDatabase, group: str, ids: Iterable[int], hash_id: int, source: str) -> None:
        grouped = save.group_by_unit(list(ids))
        for unit_id, fields in grouped.items():
            rec = fields.get(hash_id)
            value = self._first_value(save, rec, None)
            if value is None:
                continue
            name, gbid, hx = self._lookup(db, value)
            slot = contextual_unit_name(group, fields.get(hash_id).id_type if fields.get(hash_id) else hash_id, unit_id, self.character_names)
            if name == "Empty":
                if slot:
                    self._put(UnitLabel(group, unit_id, slot, "empty hash / slot fallback", hx, gbid, self._present_fields(fields)))
                continue
            detail = ""
            if group == "Sigil":
                level = self._first_value(save, fields.get(2704), "")
                if level not in ("", None):
                    detail = f" Lv {level}"
            elif group == "Weapon":
                xp = self._first_value(save, fields.get(2804), "")
                if xp not in ("", None):
                    detail = f" XP {xp}"
            base = f"{group}: {name}{detail}"
            if slot and not slot.startswith(group + " Unit"):
                base = f"{slot}: {name}{detail}"
            self._put(UnitLabel(group, unit_id, base, source, hx, gbid, self._present_fields(fields)))

    def _add_item_groups(self, save: GBFRSaveData, db: ItemDatabase) -> None:
        ids = [2102,2103,2104,2105,1901,1902,1903,1904,2002,2003,2004,1801,1802,1803,1804,1805,1806,1807,2001,2101,6801,1701,1702]
        grouped = save.group_by_unit(ids)
        for unit_id, fields in grouped.items():
            hash_rec = fields.get(2102) or fields.get(1901) or fields.get(2002)
            value = self._first_value(save, hash_rec, None)
            slot = contextual_unit_name("Item", hash_rec.id_type if hash_rec else 0, unit_id, self.character_names)
            if value is None:
                if slot:
                    self._put(UnitLabel("Item", unit_id, slot, "item slot fallback", "", "", self._present_fields(fields)))
                continue
            name, gbid, hx = self._lookup(db, value)
            if name == "Empty":
                if slot:
                    self._put(UnitLabel("Item", unit_id, slot, "empty hash / slot fallback", hx, gbid, self._present_fields(fields)))
                continue
            qty = self._first_value(save, fields.get(2105) or fields.get(1903) or fields.get(2004), "")
            detail = f" x{qty}" if qty not in ("", None) else ""
            base = f"Item: {name}{detail}"
            if slot and not slot.startswith("Item Unit"):
                base = f"{slot}: {name}{detail}"
            self._put(UnitLabel("Item", unit_id, base, "2102/1901/2002 item hash", hx, gbid, self._present_fields(fields)))

    def _add_range_fallbacks(self, save: GBFRSaveData) -> None:
        # Add a fallback row for every unit we can categorize. Specific hash-derived labels
        # above will win, but this makes the Unit Map useful even when the row is still unknown.
        seen: set[tuple[str, int]] = set()
        for rec in save.records:
            group = manager_for_record(rec)
            key = (group, rec.unit_id)
            if key in seen:
                continue
            seen.add(key)
            fallback = contextual_unit_name(group, rec.id_type, rec.unit_id, self.character_names)
            if fallback:
                self._put(UnitLabel(group or "Other", rec.unit_id, fallback, "unit-id range / slot fallback", "", "", str(rec.id_type)))

    def label_for(self, rec: UnitRecord) -> str:
        group = manager_for_record(rec)
        candidates = []
        if group:
            candidates.append((group.lower(), rec.unit_id))
        # 1701/1702 are shared meta fields; only then try slot-derived groups by range.
        # Do not globally match by unit id, because many unrelated managers reuse unit 0/1/etc.
        if rec.id_type in {1701, 1702}:
            for fallback_group in _shared_meta_candidate_groups(rec.unit_id):
                candidates.append((fallback_group.lower(), rec.unit_id))
        for key in candidates:
            label = self.labels.get(key)
            if label:
                return label.display
        return contextual_unit_name(group, rec.id_type, rec.unit_id, self.character_names) or ""

    def rows(self) -> List[List[Any]]:
        order = {"Character": 0, "Weapon": 1, "Sigil": 2, "Item": 3, "Ability": 4, "Quest": 5, "Scenario": 6, "Party": 7, "Character Loadout": 8, "Options": 9, "UI": 10, "Archive": 11, "Shop": 12, "Gacha": 13, "Playlog": 14, "Other": 99}
        out = []
        for label in sorted(self.labels.values(), key=lambda x: (order.get(x.group, 50), x.unit_id, x.name)):
            out.append([label.group, label.unit_id, label.display, label.source, label.hash_hex, label.gbid, label.fields])
        return out


def _shared_meta_candidate_groups(unit_id: int) -> List[str]:
    groups: List[str] = []
    if 10000 <= unit_id <= 10039 or unit_id >= 1010000:
        groups.append("Character")
    if 40000 <= unit_id <= 40511:
        groups.append("Weapon")
    if 30000 <= unit_id <= 35100:
        groups.append("Sigil")
    if 50000 <= unit_id <= 56000:
        groups.append("Item")
    if 20000 <= unit_id <= 20600 or 10100 <= unit_id <= 10299:
        groups.append("Character Loadout")
    return groups


def manager_for_record(rec: UnitRecord) -> str:
    return manager_for_id_type(rec.id_type, rec.unit_id)


def manager_for_id_type(id_type: int, unit_id: int = 0) -> str:
    # Some CharacterManager field IDs are reused in loadout/table ranges. Prefer
    # the unit-id range first for those documented loop sections.
    if (20000 <= unit_id <= 20600 or 10100 <= unit_id <= 10299 or 1010000 <= unit_id <= 1029999) and (id_type in CHARACTER_IDS or id_type in CHARACTER_LOADOUT_IDS or id_type in {1701, 1702}):
        return "Character Loadout"
    if id_type in CHARACTER_IDS or (10000 <= unit_id <= 10039 and id_type in {1701,1702}):
        return "Character"
    if id_type in WEAPON_IDS or (40000 <= unit_id <= 40511 and id_type in {1701,1702}):
        return "Weapon"
    if id_type in SIGIL_IDS or (30000 <= unit_id <= 35100 and id_type in {1701,1702}):
        return "Sigil"
    if id_type in ITEM_IDS or (50000 <= unit_id <= 56000 and id_type in {1701,1702}):
        return "Item"
    if id_type in ABILITY_IDS:
        return "Ability"
    if id_type in SCENARIO_IDS:
        return "Scenario"
    if id_type in QUEST_IDS:
        return "Quest"
    if id_type in PARTY_IDS:
        return "Party"
    if id_type in CHARACTER_LOADOUT_IDS or (20000 <= unit_id <= 20600) or (10100 <= unit_id <= 10299) or (1010000 <= unit_id <= 1029999):
        return "Character Loadout"
    if id_type in OPTION_IDS:
        return "Options"
    if id_type in FILTER_SORT_IDS:
        return "Filter/Sort"
    if id_type in CYCLE_TRADE_IDS:
        return "Cycle Trade"
    if id_type in TUTORIAL_IDS:
        return "Tutorial"
    if id_type in ISLAND_IDS:
        return "Island"
    if id_type in SHOP_IDS:
        return "Shop"
    if id_type in GACHA_IDS:
        return "Gacha"
    if id_type in COMMUNICATION_IDS:
        return "Communication"
    if id_type in UI_IDS:
        return "UI"
    if id_type in ARCHIVE_IDS:
        return "Archive"
    if id_type in PLAYLOG_IDS:
        return "Playlog"
    if id_type in {911, 912, 913, 914, 915, 916, 917, 918, 919, 1001, 1002, 1003}:
        return "Save System"
    if id_type in {1701, 1702}:
        return "Record Meta"
    if id_type in {2590, 2591, 2592, 2594, 2595, 2596, 2600}:
        return "Quest/System"
    if 10000 <= unit_id <= 10039:
        return "Character"
    if 40000 <= unit_id <= 40511:
        return "Weapon"
    if 30000 <= unit_id <= 35100:
        return "Sigil"
    if 50000 <= unit_id <= 56000:
        return "Item"
    return "Other"


def character_slot_name(unit_id: int) -> str:
    if 10000 <= unit_id <= 10039:
        return f"Character Slot {unit_id - 10000:02d}"
    return ""


def _character_name_from_slot(slot: int, character_names: Optional[Dict[int, str]]) -> str:
    if not character_names:
        return f"Character {slot:02d}"
    return character_names.get(slot, f"Character {slot:02d}")


def contextual_unit_name(group: str, id_type: int, unit_id: int, character_names: Optional[Dict[int, str]] = None) -> str:
    group = group or manager_for_id_type(id_type, unit_id)
    if group == "Character":
        if 10000 <= unit_id <= 10039:
            return character_slot_name(unit_id)
        return f"Character Derived Unit {unit_id}"

    if group == "Character Loadout":
        if 20000 <= unit_id <= 20600:
            n = unit_id - 20000
            char_slot, loadout = divmod(n, 15)
            return f"{_character_name_from_slot(char_slot, character_names)} Loadout {loadout + 1:02d}"
        if 10100 <= unit_id <= 10299:
            return f"Character Mastery/Page Unit {unit_id}"
        if 1010000 <= unit_id <= 1029999:
            return f"Character Loadout Detail {unit_id}"
        return f"Character Loadout Unit {unit_id}"

    if group == "Weapon":
        if 40000 <= unit_id <= 40511:
            return f"Weapon Inventory Slot {unit_id - 40000}"
        if 0 <= unit_id <= 511:
            return f"Weapon Tracking Slot {unit_id}"
        return f"Weapon Unit {unit_id}"

    if group == "Sigil":
        if 30000 <= unit_id <= 35099:
            return f"Sigil Inventory Slot {unit_id - 30000}"
        if 0 <= unit_id <= 899:
            return f"Sigil Extra Slot {unit_id}"
        return f"Sigil Unit {unit_id}"

    if group == "Item":
        if 50000 <= unit_id <= 56000:
            return f"Inventory Item Slot {unit_id - 50000}"
        if 0 <= unit_id <= 299 and id_type in {1801,1802,1803,1804,1805,1806,1807}:
            return f"Item Category Slot {unit_id}"
        if unit_id >= 0 and unit_id % 100 <= 5 and unit_id <= 99999:
            return f"Item Bucket {unit_id // 100} Field {unit_id % 100}"
        return f"Item Unit {unit_id}"

    if group == "Ability":
        return f"Ability Slot {unit_id}"
    if group == "Scenario":
        return f"Scenario Flag/Progress Slot {unit_id}"
    if group == "Quest":
        return f"Quest Progress Row {unit_id}"
    if group == "Party":
        if 103000 <= unit_id <= 103003:
            return f"Party Member Slot {unit_id - 103000 + 1}"
        if 104000 <= unit_id <= 104003:
            return f"Party Loadout Member Slot {unit_id - 104000 + 1}"
        if 10500 <= unit_id <= 10599:
            return f"Party Preset Row {unit_id - 10500}"
        return f"Party Row {unit_id}"
    if group == "Save System":
        return "Save System / Header"
    if group == "Record Meta":
        return f"Shared Record Metadata Row {unit_id}"
    if group == "Quest/System":
        return f"Quest/System Global Row {unit_id}"
    if group == "Options":
        return "Game Options"
    if group == "Filter/Sort":
        return f"Filter/Sort Row {unit_id}"
    if group == "Cycle Trade":
        return f"Trade/Exchange Row {unit_id}"
    if group == "Tutorial":
        return f"Tutorial Flag Row {unit_id}"
    if group == "Island":
        return f"Island State Row {unit_id}"
    if group == "Shop":
        return f"Shop State Row {unit_id}"
    if group == "Gacha":
        return f"Transmute/Gacha Row {unit_id}"
    if group == "Communication":
        return f"Communication Row {unit_id}"
    if group == "UI":
        return f"UI / Network Row {unit_id}"
    if group == "Archive":
        return f"Archive / Codex Row {unit_id}"
    if group == "Playlog":
        return f"Playlog Row {unit_id}"
    if unit_id not in (0, None):
        return f"{group} Unit {unit_id}"
    return ""


def fallback_unit_name(id_type: int, unit_id: int) -> str:
    return contextual_unit_name(manager_for_id_type(id_type, unit_id), id_type, unit_id)
