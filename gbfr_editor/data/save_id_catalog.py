from __future__ import annotations

from dataclasses import dataclass
import csv
from pathlib import Path
from typing import Iterable, List, Optional


@dataclass(frozen=True)
class SaveIdCatalogRow:
    field_id: int
    name: str
    kind: str
    manager: str
    meaning: str
    editor_note: str = ""
    source: str = "Local editor mapping"

    def searchable_text(self) -> str:
        return " ".join([
            str(self.field_id), self.name, self.kind, self.manager,
            self.meaning, self.editor_note, self.source,
        ]).lower()


# Conservative copy of the useful SaveIDType names/comments from GBFRDataTools.
# The editor keeps candidate wording where our save observations do not fully prove
# the field's meaning yet.
SAVE_ID_ROWS: List[SaveIdCatalogRow] = [
    SaveIdCatalogRow(1001, "SAVEDATA_1001", "ushort", "Save System", "Save system/version field"),
    SaveIdCatalogRow(1002, "SAVEDATA_1002", "ushort", "Save System", "Save system/version field"),
    SaveIdCatalogRow(1003, "SAVEDATA_HASHSEED", "uint", "Save System", "Random save hash seed used to select the active hash section"),

    SaveIdCatalogRow(1101, "USERDATA_1101", "ushort", "UserDataManager", "User profile field"),
    SaveIdCatalogRow(1102, "USERDATA_1102", "int", "UserDataManager", "User profile field"),
    SaveIdCatalogRow(1103, "USERDATA_1103", "int", "UserDataManager", "User profile field"),
    SaveIdCatalogRow(1104, "USERDATA_RUPIES", "int", "UserDataManager", "Rupies"),
    SaveIdCatalogRow(1105, "USERDATA_1105", "int", "UserDataManager", "User profile field"),
    SaveIdCatalogRow(1106, "USERDATA_COMMENDATIONS", "int", "UserDataManager", "Number of commendations"),
    SaveIdCatalogRow(1107, "USERDATA_1107", "bool", "UserDataManager", "User profile flag"),
    SaveIdCatalogRow(1108, "USERDATA_ONLINE_STATUS_FLAGS", "ulong", "UserDataManager", "Online status flags"),
    SaveIdCatalogRow(1109, "USERDATA_1109", "int", "UserDataManager", "User profile field"),
    SaveIdCatalogRow(1110, "USERDATA_1110", "int", "UserDataManager", "User profile field"),
    SaveIdCatalogRow(1111, "USERDATA_1111", "int", "UserDataManager", "User profile field"),
    SaveIdCatalogRow(1112, "USERDATA_MASTERY_POINTS", "int", "UserDataManager", "Mastery Points"),
    SaveIdCatalogRow(1113, "USERDATA_1113", "bool", "UserDataManager", "User profile flag"),
    SaveIdCatalogRow(1151, "USERDATA_1151", "uint", "UserDataManager", "User profile field"),

    SaveIdCatalogRow(1201, "CURRENT_LOCATION_STAGE_ID", "int", "Current Location", "Current location / stage id; convert to hex for direct stage file"),
    SaveIdCatalogRow(1202, "CURRENT_LOCATION_1202", "byte", "Current Location", "Current location field"),
    SaveIdCatalogRow(1203, "CURRENT_LOCATION_1203", "uint", "Current Location", "Current location field"),
    SaveIdCatalogRow(1204, "CURRENT_LOCATION_1204", "float[4]", "Current Location", "Current location vector/position candidate"),
    SaveIdCatalogRow(1205, "CURRENT_LOCATION_1205", "float[4]", "Current Location", "Current location vector/position candidate"),
    SaveIdCatalogRow(1206, "PARTY_HEALTH", "int[4]", "Current Location", "Party health values"),
    SaveIdCatalogRow(1207, "CURRENT_LOCATION_1207", "int[4]", "Current Location", "Current location/party field"),

    SaveIdCatalogRow(1301, "CHARACTER_ID_HASH", "uint hash", "CharacterManager", "Character ID hash, e.g. PL0000"),
    SaveIdCatalogRow(1302, "CHARACTER_UNLOCK_ACTIVE_CANDIDATE", "uint", "CharacterManager", "Character unlock/active candidate; observed 0/1 in samples"),
    SaveIdCatalogRow(1303, "CHARACTER_EXP_PROGRESS", "int", "CharacterManager", "Character EXP/progress; should be raised with level 1308"),
    SaveIdCatalogRow(1304, "CHARACTER_1304", "uint", "CharacterManager", "Character state field"),
    SaveIdCatalogRow(1305, "CHARACTER_1305", "uint", "CharacterManager", "Character state field; commonly 1"),
    SaveIdCatalogRow(1307, "CHARACTER_PARTY_STATE_CANDIDATE", "uint", "CharacterManager", "Character state/party candidate"),
    SaveIdCatalogRow(1308, "CHARACTER_LEVEL", "int", "CharacterManager", "Displayed character level; pair with EXP/progress 1303"),
    SaveIdCatalogRow(1309, "CHARACTER_MSP_PROGRESS_CANDIDATE", "int", "CharacterManager", "Character mastery/progression candidate"),
    SaveIdCatalogRow(1310, "CHARACTER_1310", "int", "CharacterManager", "Character progression/stat candidate"),
    SaveIdCatalogRow(1311, "CHARACTER_1311", "int", "CharacterManager", "Character state field"),
    SaveIdCatalogRow(1312, "CHARACTER_1312", "float", "CharacterManager", "Character float value; commonly 8.0"),
    SaveIdCatalogRow(1313, "CHARACTER_1313", "int", "CharacterManager", "Character state field; commonly 5"),
    SaveIdCatalogRow(1314, "CHARACTER_1314", "int", "CharacterManager", "Character state/costume/equipment candidate"),
    SaveIdCatalogRow(1315, "CHARACTER_EMPTY_HASH_STATE", "uint hash", "CharacterManager", "Observed empty-hash-like state value, not displayed level"),
    SaveIdCatalogRow(1316, "CHARACTER_1316", "int", "CharacterManager", "Character state/progression candidate"),
    SaveIdCatalogRow(1317, "CHARACTER_1317", "int", "CharacterManager", "Character state/progression candidate"),
    SaveIdCatalogRow(1318, "CHARACTER_FLAGS_CANDIDATE", "uint", "CharacterManager", "Character flags/progression candidate"),
    SaveIdCatalogRow(1321, "CHARACTER_1321", "uint", "CharacterManager", "Character cumulative/progression counter candidate"),
    SaveIdCatalogRow(1322, "CHARACTER_1322", "int", "CharacterManager", "Character state/progression candidate"),
    SaveIdCatalogRow(1402, "CHARACTER_1402_STATE_INDEX", "uint", "CharacterManager", "Character state/index field; previously mislabeled as level"),
    SaveIdCatalogRow(1403, "CHARACTER_MASTERIES_CANDIDATE", "uint[]", "CharacterManager", "Observed 8-value character mastery/progression array"),
    SaveIdCatalogRow(1404, "CHARACTER_OVERMASTERY_RNG_HASHES", "uint hash[4]", "CharacterManager", "Observed four RNG/overmastery hash slots"),
    SaveIdCatalogRow(1501, "CHARACTER_1501", "uint", "CharacterManager", "Character auxiliary field"),
    SaveIdCatalogRow(1502, "CHARACTER_1502", "uint", "CharacterManager", "Character auxiliary/progression field"),
    SaveIdCatalogRow(1503, "CHARACTER_1503", "int[2]", "CharacterManager", "Character two-value auxiliary/progression field"),

    SaveIdCatalogRow(1701, "EQUIPMENT_TRAIT_ID", "uint hash", "Shared Equipment Trait", "Trait/skill ID attached to sigils, wrightstones, weapons, and other slot objects. Anonymous save-unit notes show FF A5 06 / A506 as the current trait field."),
    SaveIdCatalogRow(1702, "EQUIPMENT_TRAIT_LEVEL", "int", "Shared Equipment Trait", "Trait level/value paired with 1701. Anonymous save-unit notes show FF A6 06 / A606 as current trait level rows."),

    SaveIdCatalogRow(1801, "ITEMDATA_ITEM_ID", "uint hash", "ItemManager", "Item ID hash, e.g. ITEM_01_0000"),
    SaveIdCatalogRow(1802, "ITEMDATA_ITEM_COUNT", "int", "ItemManager", "Item count"),
    SaveIdCatalogRow(1803, "ITEMDATA_ITEM_FLAGS", "uint", "ItemManager", "Item flags"),
    SaveIdCatalogRow(1804, "ITEMDATA_1804", "uint", "ItemManager", "Item data field"),
    SaveIdCatalogRow(1805, "ITEMDATA_1805", "uint", "ItemManager", "Item data field"),
    SaveIdCatalogRow(1806, "ITEMDATA_1806", "uint", "ItemManager", "Item data field"),
    SaveIdCatalogRow(1807, "ITEMDATA_1807", "int", "ItemManager", "Item data field"),
    SaveIdCatalogRow(1901, "ITEMJUNK_CURIO_REWARD_ITEMID", "uint hash", "ItemManager / Curio", "Curio reward item/gem ID hash"),
    SaveIdCatalogRow(1902, "ITEMJUNK_CURIO_REWARD_1902", "int", "ItemManager / Curio", "Curio reward field"),
    SaveIdCatalogRow(1903, "ITEMJUNK_CURIO_REWARD_1903", "uint", "ItemManager / Curio", "Curio reward seed candidate"),
    SaveIdCatalogRow(1904, "ITEMJUNK_CURIO_REWARD_1904", "int", "ItemManager / Curio", "Curio reward level candidate"),
    SaveIdCatalogRow(2001, "ITEMJUNK_SEEDCOUNTER", "uint", "ItemManager / Curio", "Curio seed counter; starts from 1000"),
    SaveIdCatalogRow(2002, "ITEMJUNK_CURIO_IDS", "uint hash", "ItemManager / Curio", "Curio item ID hash, e.g. ITEM_19_0001"),
    SaveIdCatalogRow(2003, "ITEMJUNK_CURIO_ITEM_SEEDS", "uint", "ItemManager / Curio", "Curio item seed"),
    SaveIdCatalogRow(2004, "ITEMJUNK_2004", "int", "ItemManager / Curio", "Curio/item bucket value"),
    SaveIdCatalogRow(2101, "ITEM_UNK_MAX_SLOT_ID", "uint", "ItemManager", "Last/current 210x item/wrightstone slot id; anonymous save-unit notes show FF3508 as Last Count"),
    SaveIdCatalogRow(2102, "ITEM_UNK_ITEM_ID", "uint hash", "ItemManager", "Slot-style item/wrightstone ID hash; anonymous save-unit notes show FF3608 as Current Wrightstone"),
    SaveIdCatalogRow(2103, "ITEM_UNK_SLOT_IDS", "uint", "ItemManager", "Slot id/count for 210x item/wrightstone rows; anonymous save-unit notes show FF3708 as Count"),
    SaveIdCatalogRow(2104, "ITEM_UNK_2104", "bool", "ItemManager", "Item flag / active slot candidate"),
    SaveIdCatalogRow(2105, "ITEM_UNK_FLAGS", "uint", "ItemManager", "GBFRDataTools labels this flags; editor-observed stack edits treat it as quantity/type candidate", "Use Save As and verify for uncommon item families"),

    SaveIdCatalogRow(2201, "PARTY_MEMBER_CHARACTER_HASH", "uint hash", "PartyManager", "Current party character id hash"),
    SaveIdCatalogRow(2202, "PARTY_MEMBER_SLOT_STATE_2202", "unknown", "PartyManager", "Party slot state"),
    SaveIdCatalogRow(2203, "PARTY_MEMBER_SLOT_STATE_2203", "unknown", "PartyManager", "Party slot state"),

    SaveIdCatalogRow(2570, "QUESTSYSTEM_QUEST_IDS", "uint", "QuestSystem", "Quest IDs"),
    SaveIdCatalogRow(2571, "QUESTSYSTEM_QUEST_COMPLETECOUNT", "uint", "QuestSystem", "Quest complete count"),
    SaveIdCatalogRow(2572, "QUESTSYSTEM_QUEST_UNK2", "uint", "QuestSystem", "Quest state field"),
    SaveIdCatalogRow(2573, "QUESTSYSTEM_QUEST_UNK3", "uint", "QuestSystem", "Quest state field"),
    SaveIdCatalogRow(2574, "QUESTSYSTEM_QUEST_FLAGS", "uint", "QuestSystem", "Quest flags"),
    SaveIdCatalogRow(2575, "QUESTSYSTEM_QUEST_UNK5", "bool", "QuestSystem", "Quest state flag"),
    SaveIdCatalogRow(2576, "QUESTSYSTEM_QUEST_UNK6", "bool", "QuestSystem", "Quest state flag"),
    SaveIdCatalogRow(2577, "QUESTSYSTEM_QUEST_UNK7", "bool", "QuestSystem", "Quest state flag"),

    SaveIdCatalogRow(2701, "GEMDATA_MAX_SLOT_ID", "uint", "GemManager", "Last/current sigil slot id; anonymous save-unit notes show FF8D0A as Last Count"),
    SaveIdCatalogRow(2702, "GEMDATA_SLOT_IDS", "uint", "GemManager", "Sigil slot id/count; anonymous save-unit notes show FF8E0A as Count"),
    SaveIdCatalogRow(2703, "GEMDATA_GEM_ID", "uint hash", "GemManager", "Gem/Sigil ID hash, e.g. GEEN_140_00"),
    SaveIdCatalogRow(2704, "GEMDATA_SKILL_1_LEVEL", "int", "GemManager", "Sigil primary skill level; anonymous save-unit notes show FF900A as current sigil level"),
    SaveIdCatalogRow(2706, "GEMDATA_WORN_BY", "uint hash", "GemManager", "Worn-by character hash, e.g. PL0500"),
    SaveIdCatalogRow(2707, "GEMDATA_FLAGS", "uint", "GemManager", "Gem/Sigil flags; bit 0 = locked, bit 1 = unknown"),
    SaveIdCatalogRow(2708, "GEMDATA_2708", "uint", "GemManager", "Sigil extra field"),

    SaveIdCatalogRow(2801, "WEAPONDATA_MAX_SLOT_ID", "uint", "WeaponManager", "Max/current weapon slot id candidate"),
    SaveIdCatalogRow(2802, "WEAPONDATA_SLOT_ID", "uint", "WeaponManager", "Weapon slot id"),
    SaveIdCatalogRow(2803, "WEAPONDATA_WEAPON_ID", "uint hash", "WeaponManager", "Weapon ID hash"),
    SaveIdCatalogRow(2804, "WEAPONDATA_WEAPON_XP", "uint", "WeaponManager", "Weapon XP / progress"),
    SaveIdCatalogRow(2805, "WEAPONDATA_WEAPON_UNCAP_STAGE", "int", "WeaponManager", "Weapon uncap/max-level stage candidate", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(2806, "WEAPONDATA_WEAPON_TRAIT_PLUS_CANDIDATE", "int", "WeaponManager", "Weapon trait + bonus candidate; still needs before/after confirmation", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(2807, "WEAPONDATA_WEAPON_2807", "int", "WeaponManager", "Weapon state/stat field candidate", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(2814, "WEAPONDATA_WEAPON_2814", "uint", "WeaponManager", "Weapon state hash/value candidate", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(2815, "WEAPONDATA_WEAPON_FLAGS", "uint", "WeaponManager", "Weapon owned/flags", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(2816, "WEAPONDATA_WEAPON_STONE_ITEM_ID", "uint hash", "WeaponManager", "Weapon stone/wrightstone item hash candidate", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(2813, "WEAPONDATA_WEAPON_2813", "uint", "WeaponManager", "Weapon state field listed in weapon loop; not editor-confirmed yet", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(7401, "WEAPON_AUX_7401", "unknown", "WeaponManager", "Weapon auxiliary loop field, units 0-511 candidate", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(7402, "WEAPON_AUX_7402", "unknown", "WeaponManager", "Weapon auxiliary loop field, units 0-511 candidate", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(7403, "WEAPON_AUX_7403", "unknown", "WeaponManager", "Weapon auxiliary loop field, units 0-511 candidate", source="Anonymous save-unit reference"),

    SaveIdCatalogRow(3903, "ABILITYDATA_ABILITY_ID", "uint hash", "AbilityManager", "Ability ID hash"),
    SaveIdCatalogRow(3904, "ABILITYDATA_ABILITY_FLAGS", "uint", "AbilityManager", "Ability flags"),

    SaveIdCatalogRow(4302, "OPTION_INVERT_VERTICAL_LOOK", "bool", "Options", "Invert Vertical Look"),
    SaveIdCatalogRow(4303, "OPTION_INVERT_HORIZONTAL_LOOK", "bool", "Options", "Invert Horizontal Look"),
    SaveIdCatalogRow(4305, "OPTION_CAMERA_REPOSITIONING", "bool", "Options", "Camera Repositioning"),
    SaveIdCatalogRow(4307, "OPTION_BATTLE_CAMERA_REPOSITIONING", "bool", "Options", "Battle Camera Repositioning"),
    SaveIdCatalogRow(4310, "OPTION_AUTO_DASH", "bool", "Options", "Auto Dash"),
    SaveIdCatalogRow(4312, "OPTION_AUTO_TARGET_SWITCHING", "bool", "Options", "Auto Target Switching"),
    SaveIdCatalogRow(4313, "OPTION_VIBRATION", "bool", "Options", "Vibration"),
    SaveIdCatalogRow(4315, "OPTION_DIFFICULTY_TYPE", "int", "Options", "Difficulty Type"),
    SaveIdCatalogRow(4316, "OPTION_ASSIST_MODE", "int", "Options", "Assist Mode"),
    SaveIdCatalogRow(4317, "OPTION_AUTO_SAVE", "bool", "Options", "Auto Save"),
    SaveIdCatalogRow(4318, "OPTION_AUTOPLAY_DIALOGUE", "bool", "Options", "Autoplay Dialogue"),
    SaveIdCatalogRow(4319, "OPTION_SCREEN_SHAKE", "bool", "Options", "Screen Shake"),
    SaveIdCatalogRow(4321, "OPTION_GUARD_LOCK_ON_BUTTONS", "bool", "Options", "Control Scheme - Guard/Lock On Buttons"),
    SaveIdCatalogRow(4322, "OPTION_COMM_WHEEL_SELECTION_STICK", "int", "Options", "Communication Wheel Selection Stick"),
    SaveIdCatalogRow(4324, "OPTION_AUTOPLAY_FATE_EPISODE", "bool", "Options", "Autoplay Fate Episode"),
    SaveIdCatalogRow(4326, "OPTION_OVERRIDE_HOLD_SBA_CHAIN_BURST", "bool", "Options", "Override Hold SBA for Chain Bursting"),
    SaveIdCatalogRow(4327, "OPTION_SBA_TARGET_PRIORITY", "int", "Options", "Skybound Art Target Priority"),
    SaveIdCatalogRow(4328, "OPTION_SBA_ACTIVATION", "int", "Options", "Skybound Art Activation"),
    SaveIdCatalogRow(4329, "OPTION_CAMERA_TERRAIN_ADJUSTMENT", "int", "Options", "Camera Terrain Adjustment"),
    SaveIdCatalogRow(4330, "OPTION_QUEST_CUTSCENE_AUTO_SKIP", "bool", "Options", "Quest Cutscene Auto-Skip"),
    SaveIdCatalogRow(4331, "OPTION_LOADING_SCREEN_SKIP", "bool", "Options", "Loading Screen Skip"),
    SaveIdCatalogRow(4332, "OPTION_NAVYRN_GATE", "bool", "Options", "Navyrngate"),
    SaveIdCatalogRow(4333, "OPTION_CAMERA_SENSITIVITY_VERTICAL", "int", "Options", "Camera Sensitivity Vertical"),
    SaveIdCatalogRow(4334, "OPTION_CAMERA_SENSITIVITY_HORIZONTAL", "int", "Options", "Camera Sensitivity Horizontal"),
    SaveIdCatalogRow(4335, "OPTION_AIM_SENSITIVITY_VERTICAL", "int", "Options", "Aim Sensitivity Vertical"),
    SaveIdCatalogRow(4336, "OPTION_AIM_SENSITIVITY_HORIZONTAL", "int", "Options", "Aim Sensitivity Horizontal"),
    SaveIdCatalogRow(4337, "OPTION_CAMERA_SMOOTHING", "bool", "Options", "Camera Smoothing"),
    SaveIdCatalogRow(4338, "OPTION_BATTLE_CAMERA_CORRECTION", "bool", "Options", "Battle Camera Correction"),
    SaveIdCatalogRow(4339, "OPTION_ENABLE_MAP", "bool", "Options", "Enable Map"),
    SaveIdCatalogRow(4340, "OPTION_AUTO_CLOSE_MAP", "bool", "Options", "Auto Close Map"),

    SaveIdCatalogRow(4412, "OPTION_PROFILE_VISIBILITY", "int", "Online / Options", "Network Settings - Profile Visibility"),
    SaveIdCatalogRow(4413, "ONLINE_PLAYER_NAME", "string", "Online / Options", "Co-Op Settings - Edit Profile - Player Name"),
    SaveIdCatalogRow(4420, "OPTION_CONTROL_SCHEME_TYPE", "int", "Online / Options", "Control Scheme - Scheme Type"),
    SaveIdCatalogRow(4421, "OPTION_SKILL_DODGE_BUTTONS", "bool", "Online / Options", "Control Scheme - Skill/Dodge Buttons"),
    SaveIdCatalogRow(4501, "ONLINE_FOLLOWED_STEAM_IDS", "array", "UINetworkManager", "Followed player Steam IDs"),
    SaveIdCatalogRow(4601, "ONLINEPLAYERLIST_CHARA_ID", "int", "UINetworkManager", "Co-Op Settings - Favorite Character"),
    SaveIdCatalogRow(4602, "ONLINEPLAYERLIST_BADGE_ID", "int", "UINetworkManager", "Co-Op Settings - Badge ID"),
    SaveIdCatalogRow(4603, "ONLINEPLAYERLIST_BADGE", "unknown", "UINetworkManager", "Co-Op Settings - Badge"),
    SaveIdCatalogRow(4604, "ONLINEPLAYERLIST_ABOUTME", "string[]", "UINetworkManager", "Co-Op Settings - About Me"),
    SaveIdCatalogRow(4605, "ONLINEPLAYERLIST_AVAILABILITY_START", "int[2]", "UINetworkManager", "Availability Start"),
    SaveIdCatalogRow(4606, "ONLINEPLAYERLIST_AVAILABILITY_END", "int[2]", "UINetworkManager", "Availability End"),
    SaveIdCatalogRow(4703, "PLAYER_PAGE_COMMENDATIONS", "unknown", "UINetworkManager", "Player Page - Commendations"),
    SaveIdCatalogRow(4804, "MOST_USED_CHARACTERS_FOR_QUESTING", "unknown", "UINetworkManager", "Most-used characters for questing"),
    SaveIdCatalogRow(4901, "QUESTS_CLEARED", "unknown", "UINetworkManager", "Quests cleared"),
    SaveIdCatalogRow(7601, "RANDOM_STATE", "unknown", "Random", "cycle::utility::Random state"),
    SaveIdCatalogRow(8901, "PLAYLOGMANAGER_8901", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8902, "PLAYLOGMANAGER_8902", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8903, "PLAYLOGMANAGER_8903", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8904, "PLAYLOGMANAGER_8904", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8905, "PLAYLOGMANAGER_8905", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8906, "PLAYLOGMANAGER_8906", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8907, "PLAYLOGMANAGER_8907", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8908, "PLAYLOGMANAGER_8908", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8909, "PLAYLOGMANAGER_8909", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8912, "PLAYLOGMANAGER_8912", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8913, "PLAYLOGMANAGER_8913", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8910, "PLAYLOGMANAGER_8910", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8911, "PLAYLOGMANAGER_8911", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
    SaveIdCatalogRow(8914, "PLAYLOGMANAGER_8914", "unknown", "PlayLogManager", "Play log field listed in anonymous save-unit reference", source="Anonymous save-unit reference"),
]


def save_id_rows(query: str = "", limit: int = 1000) -> List[SaveIdCatalogRow]:
    terms = [t.lower() for t in (query or "").split() if t.strip()]
    rows = []
    for row in SAVE_ID_ROWS:
        hay = row.searchable_text()
        if all(t in hay for t in terms):
            rows.append(row)
    rows.sort(key=lambda r: (r.manager, r.field_id, r.name))
    return rows[:limit]


def format_save_id_summary() -> str:
    from collections import Counter
    managers = Counter(r.manager for r in SAVE_ID_ROWS)
    kinds = Counter(r.kind for r in SAVE_ID_ROWS)
    lines = [f"Save field ID rows: {len(SAVE_ID_ROWS)}", "", "By manager:"]
    for manager, count in managers.most_common():
        lines.append(f"  {manager:<28} {count}")
    lines.append("\nBy type:")
    for kind, count in kinds.most_common():
        lines.append(f"  {kind:<28} {count}")
    return "\n".join(lines)


def write_save_id_catalog_csv(rows: Iterable[SaveIdCatalogRow], path: str | Path) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["field_id", "name", "kind", "manager", "meaning", "editor_note", "source"])
        writer.writeheader()
        for r in rows:
            writer.writerow({
                "field_id": r.field_id,
                "name": r.name,
                "kind": r.kind,
                "manager": r.manager,
                "meaning": r.meaning,
                "editor_note": r.editor_note,
                "source": r.source,
            })


def save_id_name(field_id: int) -> Optional[str]:
    for row in SAVE_ID_ROWS:
        if row.field_id == int(field_id):
            return row.name
    return None
