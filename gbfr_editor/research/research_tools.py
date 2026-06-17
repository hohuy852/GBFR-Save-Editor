from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
import csv
import json

from gbfr_save import GBFRSaveData
from unit_meta import unit_name


def search_values(save: GBFRSaveData, query: str, *, exact: bool = False, limit: int = 500) -> List[Dict[str, Any]]:
    q = query.strip()
    if not q:
        return []
    wanted_num: Optional[float] = None
    wanted_int: Optional[int] = None
    try:
        wanted_int = int(q, 0)
        wanted_num = float(wanted_int)
    except Exception:
        try:
            wanted_num = float(q)
        except Exception:
            wanted_num = None
    q_low = q.lower()
    rows: List[Dict[str, Any]] = []
    for rec in save.records:
        values = save.get_values(rec)
        hits: List[int] = []
        for i, value in enumerate(values):
            ok = False
            if wanted_num is not None and isinstance(value, (int, float, bool)):
                if exact:
                    ok = float(value) == wanted_num
                else:
                    ok = abs(float(value) - wanted_num) < 0.00001
            if not ok and q_low in str(value).lower():
                ok = True
            if ok:
                hits.append(i)
        if hits:
            rows.append({
                "kind": rec.kind,
                "id_type": rec.id_type,
                "id_name": unit_name(rec.id_type),
                "unit_id": rec.unit_id,
                "value_count": rec.value_count,
                "hit_indexes": hits[:32],
                "values_preview": values[:32],
            })
            if len(rows) >= limit:
                break
    return rows


def write_search_csv(rows: List[Dict[str, Any]], output_path: str | Path) -> None:
    with Path(output_path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["kind", "id_type", "id_name", "unit_id", "value_count", "hit_indexes", "values_preview"])
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "hit_indexes": json.dumps(row["hit_indexes"]), "values_preview": json.dumps(row["values_preview"], ensure_ascii=False)})


def format_search_text(rows: List[Dict[str, Any]], *, max_rows: int = 200) -> str:
    lines = ["GBFR value search", ""]
    if not rows:
        lines.append("No matching values found.")
        return "\n".join(lines)
    for row in rows[:max_rows]:
        lines.append(
            f"{row['kind']} id={row['id_type']} {row['id_name']} unit={row['unit_id']} "
            f"count={row['value_count']} hit_indexes={row['hit_indexes']} values={row['values_preview']}"
        )
    if len(rows) > max_rows:
        lines.append(f"... +{len(rows) - max_rows} more rows")
    return "\n".join(lines)


def scan_known_hashes(save: GBFRSaveData, item_db, *, include_unknown: bool = False, limit: int = 5000) -> List[Dict[str, Any]]:
    """Find uint values that match the GBID database or sit in known hash-like save fields."""
    hashish_id_types = {1301, 1901, 2002, 2102, 2703, 2706, 2803, 2816, 3903}
    empty_hashes = {0, 0x887AE0B0}
    rows: List[Dict[str, Any]] = []
    for rec in save.records:
        if rec.kind != "uint":
            continue
        is_hash_field = rec.id_type in hashish_id_types
        values = save.get_values(rec)
        for i, value in enumerate(values):
            ivalue = int(value) & 0xFFFFFFFF
            if ivalue in empty_hashes:
                continue
            entry = item_db.lookup_hash(ivalue) if item_db else None
            if entry or (include_unknown and is_hash_field):
                rows.append({
                    "category": entry.category if entry else "Unknown hash",
                    "name": entry.display_name if entry else f"Unknown 0x{ivalue:08X}",
                    "gbid": entry.item_id if entry else "",
                    "hash": f"{ivalue:08X}",
                    "decimal": ivalue,
                    "kind": rec.kind,
                    "id_type": rec.id_type,
                    "id_name": unit_name(rec.id_type),
                    "unit_id": rec.unit_id,
                    "value_index": i,
                    "record_index": rec.index,
                    "aliases": entry.alias_text if entry else "",
                    "known": bool(entry),
                })
                if len(rows) >= limit:
                    return rows
    return rows


def write_hash_scan_csv(rows: List[Dict[str, Any]], output_path: str | Path) -> None:
    fields = ["category", "name", "gbid", "hash", "decimal", "kind", "id_type", "id_name", "unit_id", "value_index", "record_index", "aliases", "known"]
    with Path(output_path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def format_hash_scan_text(rows: List[Dict[str, Any]], *, max_rows: int = 200) -> str:
    lines = ["GBFR known/unknown hash scan", ""]
    if not rows:
        lines.append("No matching hashes found.")
        return "\n".join(lines)
    known = sum(1 for r in rows if r.get("known"))
    unknown = len(rows) - known
    lines.append(f"Rows: {len(rows)}  known: {known}  unknown hash-like: {unknown}")
    lines.append("")
    for row in rows[:max_rows]:
        lines.append(
            f"{row['category']:<14} {row['hash']} {row['gbid']:<16} {row['name']:<42} "
            f"at {row['kind']} id={row['id_type']} {row['id_name']} unit={row['unit_id']} index={row['value_index']}"
        )
    if len(rows) > max_rows:
        lines.append(f"... +{len(rows) - max_rows} more rows")
    return "\n".join(lines)
