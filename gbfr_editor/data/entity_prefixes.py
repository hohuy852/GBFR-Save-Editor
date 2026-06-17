from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EntityPrefix:
    group: str
    prefix: str
    meaning: str
    notes: str = ""


ENTITY_PREFIXES: dict[str, EntityPrefix] = {
    # Model/entity prefixes from Community's Entity Prefixes page.
    "ba": EntityPrefix("Model", "ba", "Room animated object", "Animated room object models."),
    "bg": EntityPrefix("Model", "bg", "Room static model prop", "Static room props; can be passthrough or non-passthrough."),
    "bh": EntityPrefix("Model", "bh", "Room breakable/obstacle object", "Breakables, obstacles, and similar room objects."),
    "ef": EntityPrefix("Model", "ef", "Effect", "Effect model/entity prefix."),
    "em": EntityPrefix("Model", "em", "Enemy body", "Enemy model/entity prefix."),
    "et": EntityPrefix("Model", "et", "3D model viewer / miscellaneous entity", "Miscellaneous model-viewer entities and other objects."),
    "fe": EntityPrefix("Model", "fe", "Enemy face/head", "Enemy face/head model prefix."),
    "fn": EntityPrefix("Model", "fn", "NPC face/head", "NPC face/head model prefix."),
    "fp": EntityPrefix("Model", "fp", "Player face/head", "Player face/head model prefix."),
    "it": EntityPrefix("Model", "it", "Miscellaneous/random model", "Miscellaneous item/random models."),
    "np": EntityPrefix("Model", "np", "NPC body", "NPC body model prefix."),
    "pl": EntityPrefix("Model", "pl", "Player body", "Player body model prefix."),
    "tr": EntityPrefix("Model", "tr", "Landscape/scenery model", "Map walls, cliffs, landscape, and scenery models."),
    "we": EntityPrefix("Model", "we", "Enemy weapon", "Enemy weapon model prefix."),
    "wn": EntityPrefix("Model", "wn", "NPC weapon", "NPC weapon model prefix."),
    "wp": EntityPrefix("Model", "wp", "Player weapon", "Player weapon model prefix."),
    # Scripting prefixes from the same page.
    "ph": EntityPrefix("Scripting", "ph", "Phase", "Controls what stages to load."),
    "st": EntityPrefix("Scripting", "st", "Stage/room/map", "Stages, rooms, and maps."),
}


def normalize_entity_code(text: str) -> str:
    """Return a compact lower-case asset/entity ID stem from a path or raw code."""
    raw = (text or "").strip().replace("\\", "/")
    raw = raw.rsplit("/", 1)[-1]
    raw = raw.split(".", 1)[0]
    return raw.lower().strip()


def prefix_for_code(text: str) -> str:
    stem = normalize_entity_code(text)
    # Most IDs are two-letter prefixes like pl0000/wp2200/ph720/st101f00.
    m = re.match(r"^([a-z]{2})(?=[0-9_a-z-]*$)", stem)
    if m:
        return m.group(1)
    return stem[:2]


def lookup_entity_prefix(text: str) -> Optional[EntityPrefix]:
    return ENTITY_PREFIXES.get(prefix_for_code(text))


def describe_entity_code(text: str) -> str:
    stem = normalize_entity_code(text)
    prefix = prefix_for_code(text)
    info = ENTITY_PREFIXES.get(prefix)
    if not info:
        return f"{text}: unknown prefix '{prefix}'. No packaged entity-prefix description yet."
    lines = [
        f"Input: {text}",
        f"Stem: {stem}",
        f"Prefix: {info.prefix}",
        f"Group: {info.group}",
        f"Meaning: {info.meaning}",
    ]
    if info.notes:
        lines.append(f"Notes: {info.notes}")
    return "\n".join(lines)


def entity_prefix_rows() -> list[dict[str, str]]:
    rows = []
    for p in sorted(ENTITY_PREFIXES.values(), key=lambda x: (x.group, x.prefix)):
        rows.append({"category": f"Entity Prefix - {p.group}", "id": p.prefix, "name": p.meaning, "notes": p.notes})
    return rows
