from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence

from gbfr_save import GBFRSaveData, UnitRecord

EMPTY_HASH = 0x887AE0B0


@dataclass
class PatchResult:
    label: str
    changed_records: int
    changed_values: int
    note: str = ""


def _find_first(save: GBFRSaveData, id_type: int, kind: Optional[str] = None, unit_id: Optional[int] = None) -> Optional[UnitRecord]:
    rows = save.find(kind=kind, id_type=id_type, unit_id=unit_id)
    return rows[0] if rows else None


def _set_all(save: GBFRSaveData, rec: Optional[UnitRecord], value: Any, *, only_raise: bool = False) -> PatchResult:
    if rec is None:
        return PatchResult("missing", 0, 0, "field missing")
    vals = save.get_values(rec)
    changed = 0
    out = []
    for old in vals:
        new = value
        if only_raise:
            try:
                if isinstance(old, bool):
                    new = bool(old) or bool(value)
                else:
                    new = max(int(old), int(value))
            except Exception:
                new = value
        if old != new:
            changed += 1
        out.append(new)
    if changed:
        save.set_values(rec, out)
    return PatchResult(f"{rec.kind}:{rec.id_type}", 1 if changed else 0, changed)


def _set_parallel(save: GBFRSaveData, field_ids: Iterable[int], value: Any, *, only_raise: bool = False) -> List[PatchResult]:
    results: List[PatchResult] = []
    for field_id in field_ids:
        for rec in save.find(id_type=field_id):
            results.append(_set_all(save, rec, value, only_raise=only_raise))
    return results


def complete_quest_tables_splusplus(save: GBFRSaveData) -> List[PatchResult]:
    """Experimental quest completion patch.

    This intentionally edits only existing scalar arrays. The mapping is based on
    SaveUnit QuestSystem rows: id arrays are left alone, status/result arrays are
    raised to complete-looking values, and rank rows are raised to 7.
    """
    results: List[PatchResult] = []
    # Parallel status/result rows beside the quest-id lists.
    results += _set_parallel(save, [2511, 2512, 2551, 2561, 2571, 2581], 1, only_raise=True)
    # Rank/result-title candidate row. In observed saves this row is already 7 for many quests.
    results += _set_parallel(save, [2574], 7, only_raise=True)
    # Boolean clear/completed/viewed candidates under QuestSystem. Raise False -> True only.
    results += _set_parallel(save, [2520, 2554, 2555, 2575, 2576, 2577], True, only_raise=True)
    return [r for r in results if r.changed_values]


def unlock_title_archive_candidates(save: GBFRSaveData) -> List[PatchResult]:
    """Experimental title/archive unlock patch.

    The exact in-game title/challenge manager is not fully named yet. These are
    the known archive/UI/book/list state rows from SaveUnit IDs. Values are raised
    to at least 1, so existing higher counters are not downgraded.
    """
    title_archive_fields = [
        7302, 7352,   # UI information/dialog candidate state rows
        7902,         # archive/codex state
        8102,         # word list state
        8202,         # main-story archive state
        8302,         # BGM archive state
        8402,         # character picture book state/counter
        8502,         # enemy picture book state
        8602,         # pendulum picture book state
        8702,         # tips state
        8802,         # command list state
    ]
    results = _set_parallel(save, title_archive_fields, 1, only_raise=True)
    return [r for r in results if r.changed_values]


def set_character_overmastery_hashes(save: GBFRSaveData, unit_id: int, hashes: Sequence[int]) -> PatchResult:
    """Set the four observed overmastery/RNG bonus hash slots for a character.

    Field 1404 has four uint values on character units in observed saves. Names
    are still research-mapped, so callers should treat this as an advanced/raw edit.
    """
    if len(hashes) != 4:
        raise ValueError("Overmastery hash edit expects exactly 4 values.")
    rec = _find_first(save, 1404, kind="uint", unit_id=unit_id)
    if rec is None:
        return PatchResult("overmastery 1404", 0, 0, "field 1404 missing for selected character")
    old = save.get_values(rec)
    if len(old) != 4:
        return PatchResult("overmastery 1404", 0, 0, f"expected 4 values, found {len(old)}")
    new = [int(v) & 0xFFFFFFFF for v in hashes]
    changed = sum(1 for a, b in zip(old, new) if int(a) != int(b))
    if changed:
        save.set_values(rec, new)
    return PatchResult("overmastery 1404", 1 if changed else 0, changed)


def clear_character_overmastery_hashes(save: GBFRSaveData, unit_id: int) -> PatchResult:
    return set_character_overmastery_hashes(save, unit_id, [EMPTY_HASH, EMPTY_HASH, EMPTY_HASH, EMPTY_HASH])


def patch_summary(results: Iterable[PatchResult]) -> str:
    rows = [r for r in results if r.changed_records or r.changed_values]
    if not rows:
        return "No patchable values changed."
    changed_values = sum(r.changed_values for r in rows)
    changed_records = sum(r.changed_records for r in rows)
    labels = ", ".join(r.label for r in rows[:8])
    suffix = "..." if len(rows) > 8 else ""
    return f"Patched {changed_values:,} values across {changed_records:,} records ({labels}{suffix})."
