from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional
from urllib.parse import parse_qs, urlparse
import csv
import io
import re
import urllib.request

SAVE_WIZARD_SHEET_URL = "https://docs.google.com/spreadsheets/d/1mGf987Njg3VodeXp8kVwzEgvYSAeMzkHnkGnAj1_RjY/edit?gid=1502701072#gid=1502701072"
SAVE_WIZARD_SHEET_GID = "1502701072"

@dataclass(frozen=True)
class SaveWizardCheat:
    key: str
    name: str
    category: str
    action: str
    target: str = ""
    description: str = ""
    source: str = "Bundled"
    safe: bool = True

    def display_action(self) -> str:
        if self.action == "preset":
            return f"Apply preset: {self.target}"
        if self.action == "max_items":
            return "Patch existing known item quantities"
        if self.action == "max_sigils":
            return "Patch existing known sigil levels/locks"
        if self.action == "max_weapons":
            return "Patch existing known weapon XP/flags"
        if self.action == "max_characters":
            return "Patch existing character levels"
        if self.action == "complete_quests":
            return "Complete all mapped progression groups"
        if self.action == "complete_progression_group":
            return f"Complete mapped progression group: {self.target or 'all'}"
        if self.action == "unlock_titles":
            return "Experimental title/archive unlock candidates"
        if self.action == "add_all_known_v_sigils":
            return "Add all known V/V+ sigils"
        if self.action == "add_all_known_materials":
            return "Safe add missing verified material/item stacks"
        return self.action


# These are not raw Save Wizard address/code patches. They are editor-native
# equivalents built from the same cheat-page intent: safe, named save edits that
# reuse existing empty FlatBuffer slots instead of blind offset writes.
BUILTIN_SAVE_WIZARD_CHEATS: tuple[SaveWizardCheat, ...] = (
    SaveWizardCheat(
        key="sw-max-current-items",
        name="Max Current Known Items / Materials",
        category="Inventory",
        action="max_items",
        description="Sets quantities on known item/material/currency rows already present in the save.",
    ),
    SaveWizardCheat(
        key="sw-add-known-material-cache",
        name="Add Upgrade Material Cache",
        category="Inventory",
        action="preset",
        target="cheat-upgrade-material-cache",
        description="Adds common upgrade materials into empty item slots.",
    ),
    SaveWizardCheat(
        key="sw-max-currency-mastery",
        name="Max Rupies + Mastery Points",
        category="Currency",
        action="preset",
        target="cheat-max-currency-mastery",
        description="Adds high-value Rupie and Mastery Point rows into empty item slots.",
    ),
    SaveWizardCheat(
        key="sw-max-current-sigils",
        name="Max Current Known Sigils + Traits + Lock",
        category="Sigils",
        action="max_sigils",
        description="Sets known sigils already in the save to the current max sigil level, updates linked 120M trait-level rows where present, and normalizes assignment/lock flags.",
    ),
    SaveWizardCheat(
        key="sw-add-basic-v-sigils",
        name="Add Basic V Sigil Set",
        category="Sigils",
        action="preset",
        target="basic-v-sigils",
        description="Adds a small safe starter V-rank sigil set.",
    ),
    SaveWizardCheat(
        key="sw-add-meta-sigils",
        name="Add Meta Sigil Core",
        category="Sigils",
        action="preset",
        target="cheat-meta-sigil-core",
        description="Adds a larger general-purpose combat sigil stack.",
    ),
    SaveWizardCheat(
        key="sw-add-survival-sigils",
        name="Add Survival Sigil Stack",
        category="Sigils",
        action="preset",
        target="cheat-survival-sigil-stack",
        description="Adds defensive and comfort sigils into empty sigil slots.",
    ),
    SaveWizardCheat(
        key="sw-add-all-known-v-sigils",
        name="Add All Known V / V+ Sigils",
        category="Sigils",
        action="add_all_known_v_sigils",
        description="Adds every bundled known V-rank/V+-rank GEEN sigil into empty sigil slots. Skips trait-only skills.",
    ),
    SaveWizardCheat(
        key="sw-max-current-weapons",
        name="Max Current Known Weapons",
        category="Weapons",
        action="max_weapons",
        description="Sets known weapon progress rows already present in the save to the current cheat XP value and flag.",
    ),
    SaveWizardCheat(
        key="sw-max-current-characters",
        name="Max Current Character Levels",
        category="Characters",
        action="max_characters",
        description="Sets existing character slot numeric fields to the current character max value.",
    ),
    SaveWizardCheat(
        key="sw-complete-mapped-progression",
        name="Complete All Mapped Progression",
        category="Progression",
        action="complete_quests",
        target="",
        description="Completes every quest/stage row that can be mapped by the save's real mission-key vectors. Catalog-only rows are skipped safely.",
    ),
    SaveWizardCheat(
        key="sw-complete-main-story",
        name="Complete Main Story",
        category="Progression",
        action="complete_progression_group",
        target="1",
        description="Completes mapped 100000-series Main Quest rows using QuestSystem key/status vectors.",
    ),
    SaveWizardCheat(
        key="sw-complete-side-quests",
        name="Complete Side / Challenge Quests",
        category="Progression",
        action="complete_progression_group",
        target="2",
        description="Completes mapped 200000-series Challenge/Side Quest rows using the 2550/2551/2554/2555 vectors.",
    ),
    SaveWizardCheat(
        key="sw-complete-fate-episodes",
        name="Complete Fate Episodes",
        category="Progression",
        action="complete_progression_group",
        target="3",
        description="Completes mapped 300000-series Fate Episode rows using the 2560/2561 vector.",
    ),
    SaveWizardCheat(
        key="sw-complete-multiplayer-quests",
        name="Complete Multiplayer / Quest Counter",
        category="Progression",
        action="complete_progression_group",
        target="4",
        description="Completes mapped 400000-series multiplayer/quest-counter rows and raises known rank candidates.",
    ),
    SaveWizardCheat(
        key="sw-complete-town-lobby-misc",
        name="Complete Town / Lobby / Misc",
        category="Progression",
        action="complete_progression_group",
        target="5",
        description="Completes mapped 500000-series town/lobby rows that exist in the loaded save vector.",
    ),
    SaveWizardCheat(
        key="sw-unlock-title-archive-candidates",
        name="Unlock Title / Archive Candidates",
        category="Progression",
        action="unlock_titles",
        description="Experimental unlock for archive/title/book/list candidate state fields. Raises values only; does not lower counters.",
    ),
    SaveWizardCheat(
        key="sw-add-captain-weapons",
        name="Add Captain Weapon Pack",
        category="Weapons",
        action="preset",
        target="cheat-captain-weapon-pack",
        description="Adds Captain weapons into empty weapon slots with high progress values.",
    ),
    SaveWizardCheat(
        key="sw-add-all-known-materials",
        name="Safe Add Missing Items / Materials",
        category="Inventory",
        action="add_all_known_materials",
        description="Safely activates missing verified normal item/material rows only when a matching 180x row and known-good template exist. Excludes wallet, relics, curios, unknowns, and reference-only rows.",
    ),
    SaveWizardCheat(
        key="sw-repair-unsafe-material-addall",
        name="Repair Unsafe Add-All Material Rows",
        category="Inventory Safety",
        action="repair_unsafe_material_addall",
        description="Clears likely bad inactive 180x material quantities created by older Add All Materials builds.",
    ),
)


def google_sheet_csv_url(url: str) -> str:
    """Convert a normal Google Sheets edit link into a CSV export URL."""
    if "docs.google.com/spreadsheets" not in url:
        return url
    parsed = urlparse(url)
    m = re.search(r"/spreadsheets/d/([^/]+)", parsed.path)
    if not m:
        return url
    doc_id = m.group(1)
    query = parse_qs(parsed.query)
    gid = (query.get("gid") or [""])[0]
    if not gid and parsed.fragment.startswith("gid="):
        gid = parsed.fragment.split("=", 1)[1]
    if not gid:
        gid = "0"
    return f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=csv&gid={gid}"


def load_sheet_csv(url: str = SAVE_WIZARD_SHEET_URL, timeout: int = 35) -> str:
    csv_url = google_sheet_csv_url(url)
    req = urllib.request.Request(csv_url, headers={"User-Agent": "GBFR-Relink-Editor/3.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8-sig", errors="replace")


def parse_sheet_cheats(csv_text: str, source: str = "Save Wizard sheet") -> list[SaveWizardCheat]:
    """Best-effort parser for the community Save Wizard tab.

    The sheet layout may change over time, so this parser intentionally accepts
    many possible column names and falls back to row text. Imported rows are
    listed as references; the editor does not blind-apply raw Save Wizard codes.
    """
    f = io.StringIO(csv_text)
    sample = f.read(4096)
    f.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample)
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(f, dialect=dialect)
    if not reader.fieldnames:
        return []
    fields = {str(name).strip().lower(): name for name in reader.fieldnames if name is not None}

    def pick(*names: str) -> Optional[str]:
        for n in names:
            if n in fields:
                return fields[n]
        for key, original in fields.items():
            if any(n in key for n in names):
                return original
        return None

    name_col = pick("name", "cheat", "title", "description")
    category_col = pick("category", "group", "tab", "section")
    code_col = pick("code", "save wizard", "sw", "patch")
    note_col = pick("note", "notes", "comment", "comments")
    rows: list[SaveWizardCheat] = []
    for idx, row in enumerate(reader, 1):
        values = [str(v).strip() for v in row.values() if str(v).strip()]
        if not values:
            continue
        name = str(row.get(name_col, "")).strip() if name_col else ""
        if not name:
            name = values[0]
        if not name or name.startswith("#"):
            continue
        category = str(row.get(category_col, "")).strip() if category_col else "Imported"
        code = str(row.get(code_col, "")).strip() if code_col else ""
        notes = str(row.get(note_col, "")).strip() if note_col else ""
        desc_parts = []
        if notes:
            desc_parts.append(notes)
        if code:
            desc_parts.append("Raw Save Wizard/code text present; import is reference-only until mapped to a safe editor action.")
        key = "sheet-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]
        if not key or key == "sheet-":
            key = f"sheet-row-{idx}"
        rows.append(SaveWizardCheat(
            key=key,
            name=name,
            category=category or "Imported",
            action="reference_only",
            description=" ".join(desc_parts) or "Imported sheet row. Needs mapping before safe apply.",
            source=source,
            safe=False,
        ))
    return rows


def list_builtin_save_wizard_cheats(query: str = "") -> list[SaveWizardCheat]:
    q = query.strip().lower()
    rows = list(BUILTIN_SAVE_WIZARD_CHEATS)
    if not q:
        return rows
    toks = q.split()
    return [c for c in rows if all(t in " ".join([c.key, c.name, c.category, c.action, c.target, c.description]).lower() for t in toks)]


def get_builtin_save_wizard_cheat(key_or_name: str) -> SaveWizardCheat:
    q = key_or_name.strip().lower()
    rows = list(BUILTIN_SAVE_WIZARD_CHEATS)
    for row in rows:
        if q == row.key.lower() or q == row.name.lower():
            return row
    matches = [r for r in rows if q in r.key.lower() or q in r.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(f"Unknown Save Wizard cheat: {key_or_name}")
    raise KeyError("Ambiguous Save Wizard cheat. Matches: " + ", ".join(r.key for r in matches))
