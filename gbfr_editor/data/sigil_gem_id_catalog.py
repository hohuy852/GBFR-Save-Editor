from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Dict, List
import csv
import re

from item_db import ItemDatabase, ItemEntry

COMMUNITY_SIGIL_GEM_ID_URL = ""
COMMUNITY_SIGIL_GEM_ID_CSV_URL = ""
COMMUNITY_SIGIL_GEM_TARGET_ROWS = 1350  # upstream docs/resources/sigil_id.csv rows excluding header at the time this catalog was merged

_TIER_BY_DIGIT = {
    "0": "I",
    "1": "II",
    "2": "III",
    "3": "IV",
    "4": "V",
}

_OFFENSE_WORDS = {
    "attack", "critical", "stun", "enmity", "stamina", "charged", "throw", "exploiter",
    "finisher", "fire", "damage cap", "booster", "tyranny", "assassin", "garrison",
    "skilled assault", "life on the line", "quick charge", "lucky charge", "injury", "break",
}
_DEFENSE_WORDS = {
    "health", "stout", "guts", "aegis", "steel nerves", "defense", "firm stance", "natural defenses",
}
_UTILITY_WORDS = {
    "linked", "cooldown", "cascade", "uplift", "potion", "low profile", "provoke", "learner",
    "rupie", "guard", "dodge", "healing", "regen", "drain", "autorevive", "nimble", "window",
    "sigil booster", "supplement", "precise",
}


def is_sigil_entry(entry: ItemEntry) -> bool:
    return (entry.item_id or "").upper().startswith("GEEN_")


def parse_sigil_id(sigil_id: str) -> tuple[str, str, str, str, str]:
    """Return (family, grade_code, tier, plus_variant, variant_label)."""
    ident = (sigil_id or "").strip().upper()
    m = re.match(r"^GEEN_(\d{3})_(\d{2})$", ident)
    if not m:
        return "", "", "", "", "Unknown format"
    family, grade = m.groups()
    tier = _TIER_BY_DIGIT.get(grade[-1], grade[-1])
    g = int(grade)
    if g < 10:
        plus = "Base"
        variant = f"Base tier {tier}"
    elif 10 <= g < 20:
        plus = "+ A"
        variant = f"Plus variant A / tier {tier}"
    elif 20 <= g < 30:
        plus = "+ B"
        variant = f"Plus variant B / tier {tier}"
    else:
        plus = "Special"
        variant = f"Special grade {grade}"
    return family, grade, tier, plus, variant


def sigil_group(entry: ItemEntry) -> str:
    name = (entry.display_name or "").lower()
    if name.startswith("reserved / dummy") or name.startswith("dummy") or "dummy" in name:
        return "Reserved / Dummy"
    if "resistance" in name:
        return "Resistance"
    if any(word in name for word in _OFFENSE_WORDS):
        return "Offense / Damage"
    if any(word in name for word in _DEFENSE_WORDS):
        return "Defense / Survival"
    if any(word in name for word in _UTILITY_WORDS):
        return "Utility / Support"
    if "+" in entry.display_name and "v+" in name:
        return "High-rank / Plus"
    return "Other Sigils / Gems"


def sigil_rows(db: ItemDatabase, text_filter: str = "", hide_dummy: bool = False) -> List[List[object]]:
    q = (text_filter or "").strip().lower()
    rows: List[List[object]] = []
    entries = [e for e in db.by_hash.values() if is_sigil_entry(e)]
    entries.sort(key=lambda e: (parse_sigil_id(e.item_id)[0], parse_sigil_id(e.item_id)[1], e.display_name, e.item_id))
    for e in entries:
        family, grade, tier, plus, variant = parse_sigil_id(e.item_id)
        group = sigil_group(e)
        is_dummy = group == "Reserved / Dummy"
        if hide_dummy and is_dummy:
            continue
        row = [group, e.display_name, e.item_id, e.hash_hex, family, grade, tier, plus, variant, e.alias_text]
        hay = " ".join(str(v) for v in row).lower()
        if q and not all(t in hay for t in q.split() if t):
            continue
        rows.append(row)
    return rows


def build_sigil_summary(db: ItemDatabase) -> Dict[str, object]:
    rows = sigil_rows(db)
    no_dummy = sigil_rows(db, hide_dummy=True)
    groups = Counter(row[0] for row in rows)
    pluses = Counter(row[7] for row in rows)
    families = Counter(row[4] for row in rows if row[4])
    target = COMMUNITY_SIGIL_GEM_TARGET_ROWS
    coverage = round((len(rows) / target) * 100, 1) if target else 0.0
    return {
        "total": len(rows),
        "target_rows": target,
        "coverage_percent": coverage,
        "real_or_named": len(no_dummy),
        "reserved_dummy": len(rows) - len(no_dummy),
        "groups": dict(sorted(groups.items())),
        "plus_variants": dict(sorted(pluses.items())),
        "families": len(families),
        "source_urls": [COMMUNITY_SIGIL_GEM_ID_URL, COMMUNITY_SIGIL_GEM_ID_CSV_URL],
    }


def format_sigil_summary(db: ItemDatabase) -> str:
    summary = build_sigil_summary(db)
    lines = [
        "Community Sigil/Gem IDs coverage",
        "==============================",
        f"Loaded GEEN sigil/gem rows: {summary['total']:,}",
        f"Community sigil_id.csv target rows: {summary['target_rows']:,}",
        f"Approx coverage vs sigil_id.csv: {summary['coverage_percent']}%",
        f"Named/non-dummy rows: {summary['real_or_named']:,}",
        f"Reserved/dummy rows: {summary['reserved_dummy']:,}",
        f"GEEN families covered: {summary['families']:,}",
        "",
        "Groups:",
    ]
    for key, count in sorted(summary["groups"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {key:<24} {count:>5,}")
    lines.append("")
    lines.append("Variants:")
    for key, count in sorted(summary["plus_variants"].items(), key=lambda kv: kv[0]):
        lines.append(f"- {key:<10} {count:>5,}")
    lines.append("")
    lines.append("Primary sources:")
    for url in summary["source_urls"]:
        lines.append(f"- {url}")
    lines.append("")
    lines.append("Notes:")
    lines.append("- GEEN_###_00..04 are base I-V rows.")
    lines.append("- GEEN_###_10..14 and GEEN_###_20..24 are two plus/trait-variant ranges used by the game.")
    lines.append("- Use Download Full Community IDs from Item ID Catalog or GBID Browser to pull the complete current sigil_id.csv on your PC.")
    return "\n".join(lines)


def write_sigil_catalog_csv(db: ItemDatabase, path: str | Path, text_filter: str = "", hide_dummy: bool = False) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["group", "name", "gbid", "hash", "family", "grade", "tier", "plus_variant", "variant_label", "aliases"])
        writer.writerows(sigil_rows(db, text_filter, hide_dummy=hide_dummy))
