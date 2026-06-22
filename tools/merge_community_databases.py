#!/usr/bin/env python3
"""Merge public Community GBFR databases into the editor seed/cache CSVs.

Usage from the project root:
    python tools/merge_community_databases.py

The script keeps existing editor rows, pulls current Community sources, de-duplicates
by hash/category/id, tags same-name collisions with [ID1]/[ID2], and writes an
audit CSV into gbfr_editor/resources/database_merge_audit_all.csv.
"""
from __future__ import annotations

import csv
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "gbfr_editor" / "resources"

GBID_SOURCES = [
    "",
    "",
    "",
]
RESOURCE_SOURCES = {
    "model_hash_ids_seed.csv": [
        "",
    ],
    "phase_hash_ids_seed.csv": [
        "",
    ],
    "quest_ids_seed.csv": [
        "",
    ],
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "GBFRRelinkEditor/DatabaseSync"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8-sig", errors="replace")


def read_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(r) for r in csv.DictReader(f)]


def write_rows(path: Path, rows: List[Dict[str, str]], fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def norm_hash(text: str) -> str:
    text = (text or "").strip().upper().replace("0X", "")
    return text if re.fullmatch(r"[0-9A-F]{8}", text) else ""


def clean_name(name: str, ident: str) -> str:
    name = (name or "").strip().strip('"')
    if not name:
        return f"Unnamed / reserved {ident}"
    return name


def parse_gbid_csv(text: str) -> List[Dict[str, str]]:
    out = []
    for row in csv.DictReader(text.splitlines()):
        ident = (row.get("Id") or row.get("id") or row.get("GBID") or "").strip()
        h = norm_hash(row.get("Id Hash") or row.get("hash") or row.get("Hash") or "")
        if not ident or not h:
            continue
        out.append({"id": ident, "name": clean_name(row.get("Name") or row.get("name") or "", ident), "hash": h})
    return out


def merge_by_hash(old: Iterable[Dict[str, str]], new: Iterable[Dict[str, str]]) -> Tuple[List[Dict[str, str]], List[List[str]]]:
    by_hash: Dict[str, Dict[str, str]] = {}
    audit: List[List[str]] = []
    for source, rows in (("old", old), ("new", new)):
        for r in rows:
            h = norm_hash(r.get("hash") or r.get("Id Hash") or r.get("Hash") or "")
            ident = (r.get("id") or r.get("Id") or r.get("GBID") or "").strip()
            name = clean_name(r.get("name") or r.get("Name") or "", ident)
            if not h or not ident:
                continue
            current = by_hash.get(h)
            if current is None:
                by_hash[h] = {"id": ident, "name": name, "hash": h}
                audit.append([source, "add", h, ident, name, ""])
                continue
            if current["id"] == ident and current["name"] == name:
                audit.append([source, "duplicate_exact", h, ident, name, "kept existing"])
                continue
            # Prefer non-placeholder Community/current names, keep alternate in alias field.
            old_name = current["name"]
            if ("Unnamed / reserved" in old_name or old_name.startswith("Unknown")) and name:
                current["name"] = name
                current["id"] = ident or current["id"]
                audit.append([source, "rename_from_reference", h, ident, name, old_name])
            else:
                aliases = current.get("aliases", "")
                more = f"{ident}:{name}"
                if more not in aliases:
                    current["aliases"] = (aliases + "; " + more).strip("; ")
                audit.append([source, "same_hash_alias", h, ident, name, old_name])
    rows = list(by_hash.values())
    name_groups = defaultdict(list)
    for r in rows:
        name_groups[r["name"].lower()].append(r)
    for group in name_groups.values():
        if len(group) > 1:
            for i, r in enumerate(sorted(group, key=lambda x: x["id"]), 1):
                r["name"] = f"{r['name']} [ID{i}]"
    rows.sort(key=lambda r: (r["id"], r["hash"]))
    return rows, audit


def parse_resource_csv(text: str, category: str, source: str) -> List[Dict[str, str]]:
    rows = []
    for r in csv.DictReader(text.splitlines()):
        ident = (r.get("Id") or r.get("id") or r.get("Quest ID") or r.get("Phase ID") or r.get("Value") or "").strip()
        name = (r.get("Name") or r.get("name") or r.get("Title") or r.get("Description") or "").strip()
        h = norm_hash(r.get("Id Hash") or r.get("Hash") or r.get("hash") or "")
        if not ident:
            continue
        rows.append({"category": category, "id": h or ident, "name": clean_name(name, ident), "decimal": "", "source": source, "aliases": ident if h else ""})
    return rows


def parse_model_markdown(text: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    category = "Model"
    for line in text.splitlines():
        mhead = re.match(r"^#+\s+(.+)$", line.strip())
        if mhead:
            category = "Model - " + mhead.group(1).strip()
            continue
        # Community model docs commonly use code + dash/list/table text.
        m = re.search(r"`?([A-Z]{2}\d{4}|[A-Z]{2}\d{3}|[A-Z]{2}_?\d{3,4})`?\s*(?:[-–|:]|\s{2,})\s*([^|`#]+)", line)
        if not m:
            continue
        ident, name = m.group(1).strip(), m.group(2).strip(" -|")
        if ident and name and not name.lower().startswith("id"):
            rows.append({"category": category, "id": ident, "name": name, "decimal": "", "source": "Community model_ids.md", "aliases": ""})
    return rows


def merge_resource_file(seed_name: str, new_rows: List[Dict[str, str]]) -> List[List[str]]:
    path = RES / seed_name
    old = read_rows(path)
    keymap = {}
    audit = []
    for source, rows in (("old", old), ("new", new_rows)):
        for r in rows:
            cat = r.get("category", "")
            ident = r.get("id", "")
            if not ident:
                continue
            key = (cat.lower(), ident.upper())
            if key not in keymap:
                keymap[key] = {"category": cat, "id": ident, "name": r.get("name", ""), "decimal": r.get("decimal", ""), "source": r.get("source", source), "aliases": r.get("aliases", "")}
                audit.append([seed_name, "add", ident, r.get("name", ""), source])
            else:
                cur = keymap[key]
                if len(r.get("name", "")) > len(cur.get("name", "")):
                    cur["aliases"] = (cur.get("aliases", "") + "; " + cur.get("name", "")).strip("; ")
                    cur["name"] = r.get("name", cur.get("name", ""))
                    audit.append([seed_name, "rename", ident, cur["name"], source])
                else:
                    audit.append([seed_name, "duplicate", ident, r.get("name", ""), source])
    rows = sorted(keymap.values(), key=lambda r: (r["category"], r["id"]))
    write_rows(path, rows, ["category", "id", "name", "decimal", "source", "aliases"])
    return audit


def main() -> int:
    all_gbid = []
    audit = [["source", "action", "hash_or_id", "gbid", "name", "notes"]]
    failures = []
    for url in GBID_SOURCES:
        try:
            rows = parse_gbid_csv(fetch(url))
            all_gbid.extend(rows)
            audit.append([url, "downloaded", str(len(rows)), "", "", ""])
        except Exception as exc:
            failures.append(f"{url}: {exc}")
    # Update full GBID/item seed and specialized filtered seeds.
    if all_gbid:
        old = read_rows(RES / "item_ids_seed.csv")
        merged, a = merge_by_hash(old, all_gbid)
        write_rows(RES / "item_ids_seed.csv", merged, ["id", "name", "hash"])
        audit += a
        sigils = [r for r in merged if r["id"].upper().startswith("GEEN_")]
        skills = [r for r in merged if r["id"].upper().startswith("SKILL_")]
        write_rows(RES / "sigil_gem_ids_seed.csv", sigils, ["id", "name", "hash"])
        write_rows(RES / "trait_skill_seed.csv", skills, ["id", "name", "hash"])
    # Update resource seeds.
    for seed_name, urls in RESOURCE_SOURCES.items():
        new_rows = []
        for url in urls:
            try:
                text = fetch(url)
                if seed_name == "model_hash_ids_seed.csv":
                    new_rows.extend(parse_model_markdown(text))
                elif seed_name == "phase_hash_ids_seed.csv":
                    new_rows.extend(parse_resource_csv(text, "Phase", "Community phase_id.csv"))
                elif seed_name == "quest_ids_seed.csv":
                    new_rows.extend(parse_resource_csv(text, "Quest/Stage", "Community quest_id.csv"))
            except Exception as exc:
                failures.append(f"{url}: {exc}")
        if new_rows:
            for row in merge_resource_file(seed_name, new_rows):
                audit.append(row)
    if failures:
        audit.append(["failures", "warning", str(len(failures)), "", "", " | ".join(failures[:10])])
    write_rows(RES / "database_merge_audit_all.csv", [dict(zip(audit[0], row)) for row in audit[1:]], audit[0])
    print("Database merge complete.")
    if failures:
        print("Some sources failed:")
        for f in failures:
            print("-", f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
