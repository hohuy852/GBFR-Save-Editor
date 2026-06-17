from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
import csv
import json

from gbfr_save import GBFRSaveData, UnitRecord
from unit_meta import unit_name

RecordKey = Tuple[str, int, int]


@dataclass(frozen=True)
class ChangedRecord:
    kind: str
    id_type: int
    id_name: str
    unit_id: int
    before: List[Any]
    after: List[Any]
    value_count: int
    changed_indexes: List[int]
    delta_preview: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.id_type}:{self.unit_id}"


def _map_records(save: GBFRSaveData) -> Dict[RecordKey, Tuple[UnitRecord, List[Any]]]:
    return {(r.kind, r.id_type, r.unit_id): (r, save.get_values(r)) for r in save.records}


def compare_saves(before_path: str | Path, after_path: str | Path, *, limit: Optional[int] = None) -> Dict[str, Any]:
    before = GBFRSaveData.open(before_path)
    after = GBFRSaveData.open(after_path)
    a = _map_records(before)
    b = _map_records(after)
    keys = sorted(set(a) | set(b), key=lambda k: (k[0], k[1], k[2]))

    changed: List[ChangedRecord] = []
    added: List[RecordKey] = []
    removed: List[RecordKey] = []
    same = 0

    for key in keys:
        if key not in a:
            added.append(key)
            continue
        if key not in b:
            removed.append(key)
            continue
        rec_a, vals_a = a[key]
        _rec_b, vals_b = b[key]
        if vals_a == vals_b:
            same += 1
            continue
        kind, id_type, unit_id = key
        indexes = [i for i, (left, right) in enumerate(zip(vals_a, vals_b)) if left != right]
        if len(vals_a) != len(vals_b):
            indexes.extend(range(min(len(vals_a), len(vals_b)), max(len(vals_a), len(vals_b))))
        changed.append(ChangedRecord(kind, id_type, unit_name(id_type), unit_id, vals_a, vals_b, rec_a.value_count, indexes, _delta_preview(vals_a, vals_b, indexes)))

    grouped: Dict[str, int] = {}
    for row in changed:
        group_key = f"{row.kind}:{row.id_type}:{row.id_name}"
        grouped[group_key] = grouped.get(group_key, 0) + 1

    changed_out = changed if limit is None else changed[:limit]
    return {
        "before_summary": before.summary(),
        "after_summary": after.summary(),
        "same_count": same,
        "changed_count": len(changed),
        "added_count": len(added),
        "removed_count": len(removed),
        "changed_by_id": dict(sorted(grouped.items(), key=lambda kv: kv[1], reverse=True)),
        "changed": [
            {
                "kind": r.kind,
                "id_type": r.id_type,
                "id_name": r.id_name,
                "unit_id": r.unit_id,
                "value_count": r.value_count,
                "changed_indexes": r.changed_indexes,
                "delta_preview": r.delta_preview,
                "before": r.before,
                "after": r.after,
            }
            for r in changed_out
        ],
        "truncated": limit is not None and len(changed) > limit,
    }


def write_compare_json(before_path: str | Path, after_path: str | Path, output_path: str | Path, *, limit: Optional[int] = None) -> None:
    data = compare_saves(before_path, after_path, limit=limit)
    Path(output_path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_compare_csv(before_path: str | Path, after_path: str | Path, output_path: str | Path) -> None:
    data = compare_saves(before_path, after_path, limit=None)
    with Path(output_path).open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["kind", "id_type", "id_name", "unit_id", "value_count", "changed_indexes", "delta_preview", "before", "after"])
        for row in data["changed"]:
            writer.writerow([
                row["kind"],
                row["id_type"],
                row["id_name"],
                row["unit_id"],
                row["value_count"],
                json.dumps(row["before"], ensure_ascii=False),
                json.dumps(row["after"], ensure_ascii=False),
            ])


def _delta_preview(before: List[Any], after: List[Any], indexes: List[int], limit: int = 8) -> str:
    parts: List[str] = []
    for i in indexes[:limit]:
        if i >= len(before) or i >= len(after):
            parts.append(f"[{i}] length-change")
            continue
        left, right = before[i], after[i]
        if isinstance(left, (int, float)) and isinstance(right, (int, float)) and not isinstance(left, bool) and not isinstance(right, bool):
            parts.append(f"[{i}] {left!r}->{right!r} delta={right-left!r}")
        else:
            parts.append(f"[{i}] {left!r}->{right!r}")
    if len(indexes) > limit:
        parts.append(f"+{len(indexes) - limit} more changed indexes")
    return "; ".join(parts)


def _preview_values(values: List[Any], limit: int = 12) -> str:
    shown = values[:limit]
    suffix = "" if len(values) <= limit else f", ... +{len(values) - limit} more"
    return "[" + ", ".join(repr(v) for v in shown) + suffix + "]"


def format_compare_text(data: Dict[str, Any], *, max_groups: int = 50, max_rows: int = 200) -> str:
    before = data["before_summary"]
    after = data["after_summary"]
    lines = [
        "Granblue Fantasy: Relink save compare",
        "",
        f"Before: {before['path']}",
        f"After:  {after['path']}",
        "",
        f"Before mode/hash: {before['mode']} / active hash ok={before['active_hash_ok']}",
        f"After mode/hash:  {after['mode']} / active hash ok={after['active_hash_ok']}",
        f"Same records:     {data['same_count']:,}",
        f"Changed records:  {data['changed_count']:,}",
        f"Added records:    {data['added_count']:,}",
        f"Removed records:  {data['removed_count']:,}",
        "",
        "Top changed save-unit groups:",
    ]
    groups = list(data["changed_by_id"].items())[:max_groups]
    if not groups:
        lines.append("  No changed scalar records.")
    else:
        for name, count in groups:
            lines.append(f"  {count:>6,}  {name}")
    lines.extend(["", "Changed row preview:"])
    for row in data["changed"][:max_rows]:
        before_vals = _preview_values(row["before"])
        after_vals = _preview_values(row["after"])
        lines.append(
            f"  {row['kind']} id={row['id_type']} {row['id_name']} unit={row['unit_id']} "
            f"count={row['value_count']} changed={row.get('changed_indexes', [])[:12]} "
            f"delta={row.get('delta_preview', '')} :: {before_vals} -> {after_vals}"
        )
    if data.get("truncated"):
        lines.append("  ...diff preview truncated; export JSON/CSV for all rows.")
    return "\n".join(lines)
