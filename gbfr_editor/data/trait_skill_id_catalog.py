from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Dict, List
import csv
import re

from item_db import ItemDatabase, ItemEntry

COMMUNITY_TRAIT_SKILL_ID_URL = ""
COMMUNITY_TRAIT_SKILL_ID_CSV_URL = ""
COMMUNITY_TRAIT_SKILL_TARGET_ROWS = 219  # upstream docs/resources/skill_id.csv rows excluding header at the time this page was mapped
COMMUNITY_TRAIT_SKILL_UNIQUE_HASH_TARGET = 217  # same source collapsed by unique hash/ID lookup aliases

_UNUSED_WORDS = {"unused", "no functionality", "debug", "test"}
_RESIST_WORDS = {"resistance", "resist"}
_OFFENSE_WORDS = {
    "atk", "attack", "critical", "stun", "enmity", "stamina", "charged", "throw", "dmg", "damage",
    "cap", "booster", "tyranny", "assassin", "garrison", "skilled assault", "life on the line",
    "quick charge", "lucky charge", "injury", "break", "supplement", "supplements", "catastrophe",
    "berserker", "war elemental", "alpha", "beta", "gamma", "glass cannon", "flight over fight", "less is more",
}
_DEFENSE_WORDS = {
    "hp", "def", "defense", "stout", "guts", "aegis", "steel nerves", "firm stance", "natural defenses",
    "stronghold", "auto potion", "greater aegis", "improved guard", "guard payback", "improved dodge",
    "dodge payback", "nimble defense", "precise resilience", "autorevive", "regen", "drain",
}
_UTILITY_WORDS = {
    "linked", "cooldown", "cascade", "uplift", "potion", "low profile", "provoke", "learner", "rupie",
    "healing", "sigil booster", "path to mastery", "head start", "untouchable", "roll of the die",
    "potent greens", "crabby", "crabvestment", "seven net", "quick", "precise wrath",
}
_CHARACTER_WORDS = {
    "warpath", "boundary", "dragon", "guardian", "helmsman", "mage", "veteran", "rose", "phantasm",
    "hero", "lord", "dragonslayer", "holy knight", "swordmaster", "butterfly", "eternal rage", "founder",
    "versalis", "crimson", "ebony", "spirit edge", "huntress", "primarch", "fearless", "conviction",
    "navigation", "aspiration", "insight", "blooming", "concord", "oath", "creed", "procession",
    "dominance", "luster", "prowess", "grace", "mettle", "strategy", "foundation", "clout", "presence",
}
_SPECIAL_WORDS = {
    "war elemental", "path to mastery", "catastrophe", "roll of the die", "alpha", "beta", "gamma",
    "berserker echo", "spartan echo", "super ultimate", "crabmiration", "crabvestment", "seven net",
}


def is_trait_skill_entry(entry: ItemEntry) -> bool:
    return (entry.item_id or "").upper().startswith("SKILL_")


def parse_skill_id(skill_id: str) -> tuple[str, str]:
    ident = (skill_id or "").strip().upper()
    m = re.match(r"^SKILL_(\d{3})_(\d{2})$", ident)
    if not m:
        return "", ""
    return m.group(1), m.group(2)


def skill_group(entry: ItemEntry) -> str:
    name = (entry.display_name or "").lower()
    ident = (entry.item_id or "").upper()
    if any(word in name for word in _UNUSED_WORDS):
        return "Unused / Debug"
    if any(word in name for word in _SPECIAL_WORDS):
        return "Special / Rare Traits"
    if any(word in name for word in _CHARACTER_WORDS) or (ident.startswith("SKILL_") and _skill_number(ident) >= 114 and _skill_number(ident) <= 172):
        return "Character-Specific Traits"
    if any(word in name for word in _RESIST_WORDS):
        return "Resistance Traits"
    if any(word in name for word in _OFFENSE_WORDS):
        return "Offense / Damage Traits"
    if any(word in name for word in _DEFENSE_WORDS):
        return "Defense / Survival Traits"
    if any(word in name for word in _UTILITY_WORDS):
        return "Utility / Support Traits"
    return "Other Traits / Skills"


def _skill_number(skill_id: str) -> int:
    fam, _ = parse_skill_id(skill_id)
    try:
        return int(fam)
    except Exception:
        return -1


def skill_status(entry: ItemEntry) -> str:
    name = (entry.display_name or "").lower()
    if any(word in name for word in _UNUSED_WORDS):
        return "Unused / caution"
    if "unused" in name or "no functionality" in name:
        return "Unused / caution"
    return "Known"


def trait_skill_rows(db: ItemDatabase, text_filter: str = "", hide_unused: bool = False) -> List[List[object]]:
    q = (text_filter or "").strip().lower()
    rows: List[List[object]] = []
    entries = [e for e in db.by_hash.values() if is_trait_skill_entry(e)]
    # Keep duplicate IDs/hashes out of the UI unless they have different names; stable order by skill number.
    seen = set()
    unique: List[ItemEntry] = []
    for e in entries:
        key = (e.item_id.upper(), e.display_name, e.hash_hex)
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    unique.sort(key=lambda e: (_skill_number(e.item_id), parse_skill_id(e.item_id)[1], e.display_name, e.hash_hex))
    for e in unique:
        family, variant = parse_skill_id(e.item_id)
        group = skill_group(e)
        status = skill_status(e)
        if hide_unused and status.startswith("Unused"):
            continue
        row = [group, e.display_name, e.item_id, e.hash_hex, family, variant, status, e.alias_text]
        hay = " ".join(str(v) for v in row).lower()
        if q and not all(t in hay for t in q.split() if t):
            continue
        rows.append(row)
    return rows


def build_trait_skill_summary(db: ItemDatabase) -> Dict[str, object]:
    rows = trait_skill_rows(db)
    active = trait_skill_rows(db, hide_unused=True)
    groups = Counter(row[0] for row in rows)
    statuses = Counter(row[6] for row in rows)
    target = COMMUNITY_TRAIT_SKILL_UNIQUE_HASH_TARGET
    coverage = round((len(rows) / target) * 100, 1) if target else 0.0
    return {
        "total": len(rows),
        "target_rows": COMMUNITY_TRAIT_SKILL_TARGET_ROWS,
        "unique_hash_target": target,
        "coverage_percent": coverage,
        "known_or_active": len(active),
        "unused_or_caution": len(rows) - len(active),
        "groups": dict(sorted(groups.items())),
        "statuses": dict(sorted(statuses.items())),
        "source_urls": [COMMUNITY_TRAIT_SKILL_ID_URL, COMMUNITY_TRAIT_SKILL_ID_CSV_URL],
    }


def format_trait_skill_summary(db: ItemDatabase) -> str:
    summary = build_trait_skill_summary(db)
    lines = [
        "Community Trait/Skill IDs coverage",
        "=================================",
        f"Loaded SKILL rows: {summary['total']:,}",
        f"Community skill_id.csv source rows: {summary['target_rows']:,}",
        f"Unique hash/lookup target rows: {summary['unique_hash_target']:,}",
        f"Approx coverage vs unique lookup rows: {summary['coverage_percent']}%",
        f"Known/non-unused rows: {summary['known_or_active']:,}",
        f"Unused/caution rows: {summary['unused_or_caution']:,}",
        "",
        "Groups:",
    ]
    for key, count in sorted(summary["groups"].items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"- {key:<34} {count:>5,}")
    lines.append("")
    lines.append("Statuses:")
    for key, count in sorted(summary["statuses"].items(), key=lambda kv: kv[0]):
        lines.append(f"- {key:<18} {count:>5,}")
    lines.append("")
    lines.append("Primary sources:")
    for url in summary["source_urls"]:
        lines.append(f"- {url}")
    lines.append("")
    lines.append("Notes:")
    lines.append("- SKILL_* rows are trait/property IDs, not inventory sigil item IDs.")
    lines.append("- Use this catalog for trait lookup, overmastery research, wrightstone/sigil property mapping, and unknown hash cleanup.")
    lines.append("- Unused/caution rows are kept visible for research but hidden by default in the focused UI.")
    return "\n".join(lines)


def write_trait_skill_catalog_csv(db: ItemDatabase, path: str | Path, text_filter: str = "", hide_unused: bool = False) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["group", "name", "skill_id", "hash", "family", "variant", "status", "aliases"])
        writer.writerows(trait_skill_rows(db, text_filter, hide_unused=hide_unused))
