from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Any


@dataclass(frozen=True)
class PresetPack:
    key: str
    name: str
    category: str
    description: str
    items: tuple[tuple[str, int], ...] = ()
    sigils: tuple[tuple[str, int, bool], ...] = ()
    weapons: tuple[tuple[str, int], ...] = ()
    notes: str = ""

    @property
    def total_rows(self) -> int:
        return len(self.items) + len(self.sigils) + len(self.weapons)

    def to_batch_text(self) -> str:
        parts: List[str] = []
        if self.items:
            parts.append("# Items / Materials")
            parts.extend(f"{name}, {qty}" for name, qty in self.items)
        if self.sigils:
            if parts:
                parts.append("")
            parts.append("# Sigils / Gems")
            parts.extend(f"{name}, {level} {'locked' if locked else 'unlocked'}" for name, level, locked in self.sigils)
        if self.weapons:
            if parts:
                parts.append("")
            parts.append("# Weapons")
            parts.extend(f"{name}, {xp}" for name, xp in self.weapons)
        return "\n".join(parts)


PRESET_PACKS: Dict[str, PresetPack] = {
    "starter-currencies-materials": PresetPack(
        key="starter-currencies-materials",
        name="Starter Currency + Upgrade Materials",
        category="Items",
        description="Adds common currencies and upgrade materials into empty item slots.",
        items=(
            ("Rupie", 9_999_999),
            ("Mastery Point", 999_999),
            ("Fortitude Crystal (L)", 99),
            ("Standard Refinium", 99),
            ("Quality Refinium", 99),
            ("Exceptional Refinium", 99),
        ),
        notes="Good first test pack because it only touches ItemManager empty slots.",
    ),
    "weapon-upgrade-materials": PresetPack(
        key="weapon-upgrade-materials",
        name="Weapon Upgrade Material Stack",
        category="Items",
        description="Adds common weapon-upgrade material stacks into empty item slots.",
        items=(
            ("Standard Refinium", 99),
            ("Quality Refinium", 99),
            ("Exceptional Refinium", 99),
            ("Fortitude Crystal (M)", 99),
            ("Fortitude Crystal (L)", 99),
        ),
        notes="Uses existing item names from the GBID database; unresolved rows will be skipped with a warning.",
    ),
    "basic-v-sigils": PresetPack(
        key="basic-v-sigils",
        name="Basic V Sigil Set",
        category="Sigils",
        description="Adds a safe general-purpose set of known V-rank sigils into empty sigil slots.",
        sigils=(
            ("Damage Cap V", 15, True),
            ("Critical Hit Rate V", 15, True),
            ("Stamina V", 15, True),
            ("Tyranny V", 15, True),
            ("Quick Cooldown V", 15, True),
            ("Potion Hoarder V", 15, True),
        ),
        notes="This preset now requires every row to resolve to a named sigil before it will apply, so it should not add unknown hashes.",
    ),
    "captain-weapon-basics": PresetPack(
        key="captain-weapon-basics",
        name="Captain Weapon Basics",
        category="Weapons",
        description="Adds common Captain weapon hashes into empty weapon slots.",
        weapons=(
            ("WEP_PL0000_01", 0),
            ("WEP_PL0000_02", 0),
            ("WEP_PL0000_03", 0),
            ("WEP_PL0000_06", 0),
        ),
        notes="Useful for validating weapon add/swap flows. Uses empty WeaponManager slots only.",
    ),

    "cheat-max-currency-mastery": PresetPack(
        key="cheat-max-currency-mastery",
        name="Cheat: Max Currency + Mastery",
        category="Cheats",
        description="Sets direct UserDataManager wallet values for Rupies and Mastery Points.",
        items=(
            ("Rupie", 99_999_999),
            ("Mastery Point", 9_999_999),
        ),
        notes="Safe starter cheat. Rupies/MSP are routed to direct wallet fields 1104/1112 instead of fake item stacks.",
    ),
    "cheat-upgrade-material-cache": PresetPack(
        key="cheat-upgrade-material-cache",
        name="Cheat: Upgrade Material Cache",
        category="Cheats",
        description="Adds a broad stack of common weapon/material upgrade items into empty item slots.",
        items=(
            ("Fortitude Crystal (S)", 999),
            ("Fortitude Crystal (M)", 999),
            ("Fortitude Crystal (L)", 999),
            ("Standard Refinium", 999),
            ("Quality Refinium", 999),
            ("Exceptional Refinium", 999),
            ("Silver Centrum", 999),
            ("Damascus Ingot", 99),
            ("Ambrosia", 99),
        ),
        notes="Good material cheat pack. Uses only names in the bundled GBID database.",
    ),
    "cheat-meta-sigil-core": PresetPack(
        key="cheat-meta-sigil-core",
        name="Cheat: Meta Sigil Core",
        category="Cheats",
        description="Adds a stronger general-purpose combat sigil stack at level 15.",
        sigils=(
            ("Damage Cap V", 15, True),
            ("Damage Cap V", 15, True),
            ("Critical Hit Rate V", 15, True),
            ("Stamina V", 15, True),
            ("Tyranny V", 15, True),
            ("Supplements V", 15, True),
            ("Quick Cooldown V", 15, True),
            ("Cascade V", 15, True),
            ("Linked Together V", 15, True),
            ("Potion Hoarder V", 15, True),
        ),
        notes="Cheat-style starter/meta set. War Elemental and Glass Cannon are trait IDs in our database, so they are intentionally not added as sigil records yet.",
    ),
    "cheat-survival-sigil-stack": PresetPack(
        key="cheat-survival-sigil-stack",
        name="Cheat: Survival Sigil Stack",
        category="Cheats",
        description="Adds defensive/comfort V-rank sigils at level 15.",
        sigils=(
            ("Health V", 15, True),
            ("Aegis V", 15, True),
            ("Autorevive V", 15, True),
            ("GEEN_063_04", 15, True),
            ("Nimble Onslaught V", 15, True),
            ("Potion Hoarder V", 15, True),
        ),
        notes="Comfort cheat pack for safer testing. Requires those sigils to resolve to known GEEN rows before applying.",
    ),
    "cheat-captain-weapon-pack": PresetPack(
        key="cheat-captain-weapon-pack",
        name="Cheat: Captain Weapon Pack",
        category="Cheats",
        description="Adds several Captain weapon hashes into empty weapon slots with high XP/progress.",
        weapons=(
            ("WEP_PL0000_01", 190),
            ("WEP_PL0000_02", 190),
            ("WEP_PL0000_03", 190),
            ("WEP_PL0000_04", 190),
            ("WEP_PL0000_05", 190),
            ("WEP_PL0000_06", 190),
        ),
        notes="Weapon XP/progress meaning is still save-derived, so verify in-game after Save As.",
    ),

    "research-smoke-test": PresetPack(
        key="research-smoke-test",
        name="Small Smoke Test Pack",
        category="Mixed",
        description="Tiny mixed pack for testing add flows without consuming many empty slots.",
        items=(("Rupie", 1000),),
        sigils=(("GEEN_020_04", 15, True),),
        weapons=(("WEP_PL0200_01", 0),),
        notes="Good for quick Save As testing; adds one item, one sigil, and one weapon if slots exist.",
    ),
}


def list_preset_packs() -> list[PresetPack]:
    return sorted(PRESET_PACKS.values(), key=lambda p: (p.category.lower(), p.name.lower()))


def get_preset_pack(key_or_name: str) -> PresetPack:
    q = key_or_name.strip().lower()
    if q in PRESET_PACKS:
        return PRESET_PACKS[q]
    for pack in PRESET_PACKS.values():
        if pack.name.lower() == q:
            return pack
    matches = [p for p in PRESET_PACKS.values() if q in p.key.lower() or q in p.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(f"Unknown preset pack: {key_or_name}")
    raise KeyError("Ambiguous preset pack. Matches: " + ", ".join(p.key for p in matches))


def search_preset_packs(query: str = "") -> list[PresetPack]:
    q = query.strip().lower()
    rows = list_preset_packs()
    if not q:
        return rows
    toks = q.split()
    return [p for p in rows if all(t in " ".join([p.key, p.name, p.category, p.description, p.notes]).lower() for t in toks)]
