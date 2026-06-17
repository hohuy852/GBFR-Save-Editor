from __future__ import annotations

from gbfr_editor.bootstrap import bootstrap_paths
from gbfr_editor.paths import RESOURCE_DIR, SETTINGS_PATH
bootstrap_paths()

import sys
import csv
import traceback
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from PyQt6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer, QObject, QThread, pyqtSignal
    from PyQt6.QtGui import QAction, QFont, QTextOption, QIntValidator
    from PyQt6.QtWidgets import (
        QApplication, QFileDialog, QFrame, QGroupBox, QHBoxLayout, QLabel,
        QAbstractItemView, QCheckBox, QComboBox, QDialog, QGridLayout, QHeaderView, QInputDialog, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton, QPlainTextEdit,
        QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTabBar, QTabWidget, QTableView, QVBoxLayout, QWidget
    )
except ImportError as exc:  # pragma: no cover - PyQt6 may not be installed in CI.
    print("PyQt6 is required for the GUI. Install it with: pip install -r requirements.txt")
    raise

from gbfr_save import GBFRSaveData, UnitRecord
from diff_tools import compare_saves, format_compare_text, write_compare_csv, write_compare_json
from item_db import ItemDatabase, DEFAULT_ITEM_URL, TRAIT_SKILL_URL, RAW_SIGIL_GEM_URL, source_urls_from_text
from item_id_catalog import catalog_rows, format_catalog_summary, write_catalog_csv, COMMUNITY_ITEM_ID_TARGET_ROWS
from sigil_gem_id_catalog import sigil_rows, format_sigil_summary, write_sigil_catalog_csv
from trait_skill_id_catalog import trait_skill_rows, format_trait_skill_summary, write_trait_skill_catalog_csv
from model_id_catalog import model_rows, format_model_summary, write_model_catalog_csv
from phase_id_catalog import phase_rows, format_phase_summary, write_phase_catalog_csv
from quest_id_catalog import quest_rows, format_quest_summary, write_quest_catalog_csv
from resource_id_db import ResourceIdDatabase, DEFAULT_RESOURCE_URLS
from unit_meta import unit_name
from unit_labeler import UnitLabelIndex
from gbid_tools import HASHISH_ID_TYPES, build_candidate_records
from research_tools import search_values, format_search_text, write_search_csv, scan_known_hashes, format_hash_scan_text, write_hash_scan_csv
from hashing import gbfr_hash, gbfr_hash_hex
from entity_prefixes import describe_entity_code
from reference_db import ReferenceDatabase
from preset_packs import PresetPack, get_preset_pack, search_preset_packs, list_preset_packs
from save_wizard_cheats import (
    SAVE_WIZARD_SHEET_URL, SaveWizardCheat, get_builtin_save_wizard_cheat,
    list_builtin_save_wizard_cheats, load_sheet_csv, parse_sheet_cheats,
)
from save_mapper import build_save_map, build_unknown_field_report, save_map_summary_text, write_save_map_csv, write_save_map_json
from hash_resolver import resolve_unknown_hashes, format_hash_candidates, write_hash_candidates_csv
from id_audit import build_id_audit, id_audit_summary, write_id_audit_csv
from google_sheet_audit import audit_sheet_sources, audit_summary, write_audit_csv, urls_from_resource_file
from cheat_actions import complete_quest_tables_splusplus, unlock_title_archive_candidates, set_character_overmastery_hashes, clear_character_overmastery_hashes, patch_summary, EMPTY_HASH as CHEAT_EMPTY_HASH

APP_TITLE = "Granblue Fantasy Relink Save Lab"
EMPTY_HASH = 0x887AE0B0
I32_MIN = -2_147_483_648
I32_MAX = 2_147_483_647
SIGIL_MAX_EQUIPPED_PER_OWNER = 13
SIGIL_LEVEL_MAX = I32_MAX
SIGIL_LEVEL_TEST_MAX = SIGIL_LEVEL_MAX
WEAPON_XP_MAX = 999_999_999
CHARACTER_VALUE_MAX = 999_999_999
# The visible 2706 / FF920A-style owner field is not enough by itself for a valid in-game equip.
# Keep equip writes disabled until a known-good before/after equipped save maps the full relation.
SIGIL_EQUIP_WRITES_ENABLED = False

# 1607 is the mastery amount/value field. The Save Wizard notes include an
# extreme 0x7FFFFFFF test preset, but that path has been reported to crash
# save/write flows. Keep the GUI on the highest working preset we have seen
# used in codes, and sanitize older edited saves before writing.
MASTERY_1607_SAFE_MAX = I32_MAX  # signed 32-bit max for FF470600 mastery value tests
MASTERY_1607_MORE_VALUE = 0x05F5E0FF  # 99,999,999 / "MORE than normal"

# Mastery should treat the community sheet's ID/Search column as the
# authoritative 1606 write value.  QMX-style cells are kept only as labels /
# aliases because they are not necessarily the save-backed value.
MASTERY_ID_SEARCH_SHEET_URL = "https://docs.google.com/spreadsheets/d/1mGf987Njg3VodeXp8kVwzEgvYSAeMzkHnkGnAj1_RjY/edit?gid=1539189767#gid=1539189767"
MASTERY_OFFSET_PATTERN_SHEET_URL = "https://docs.google.com/spreadsheets/d/1foZPXTg1osduiYv6ocyNtk4OKbXqvaxODdYSOVFdvy8/edit?gid=1568354475#gid=1568354475"


class _BackgroundSaveWorker(QObject):
    """Write an already-prepared save byte snapshot away from the Qt GUI thread.

    Important: this worker must never touch MainWindow, GBFRSaveData, Qt widgets,
    or live UnitRecord objects. Earlier builds sent a bound self.save.save_as()
    lambda into the worker thread. That could race the UI if the user opened
    another save while the write was still running, and it also let a background
    thread read a QMainWindow-owned attribute. This worker receives plain bytes
    and paths only.
    """

    finished = pyqtSignal(object, object)

    def __init__(self, target_path: str, data: bytes, backup_source: str = "", make_backup: bool = False):
        super().__init__()
        self._target_path = str(target_path)
        self._data = bytes(data)
        self._backup_source = str(backup_source or "")
        self._make_backup = bool(make_backup)

    def run(self) -> None:
        try:
            target = Path(self._target_path)
            target.parent.mkdir(parents=True, exist_ok=True)

            if self._make_backup and self._backup_source:
                src = Path(self._backup_source)
                if src.exists():
                    stamp = time.strftime("%Y%m%d_%H%M%S")
                    backup_path = target.with_name(f"{target.name}.bak_{stamp}")
                    shutil.copy2(src, backup_path)

            fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
            tmp_path = Path(tmp_name)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(self._data)
                    fh.flush()
                os.replace(tmp_path, target)
            except Exception:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
                raise
        except Exception as exc:  # pragma: no cover - depends on user files/storage.
            self.finished.emit(exc, traceback.format_exc())
            return
        self.finished.emit(None, None)


_FORMAT_EMPTY = "—"

_COUNT_HEADERS = {
    "quantity", "xp", "rows", "items", "sigils", "weapons", "records", "units",
    "known hashes", "unknown hashes", "known values", "count", "counts", "value",
}
_ID_HEADERS = {"slot", "unit", "index", "field id", "id", "decimal"}

def _parse_intish(value: Any) -> Optional[int]:
    """Best-effort parser for values we show in the UI. Keeps CSV/export rows raw."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        sign = -1 if text.startswith("-") else 1
        if text[:1] in "+-":
            text = text[1:]
        try:
            if text.lower().startswith("0x"):
                return sign * int(text, 16)
            if text.isdigit():
                return sign * int(text, 10)
        except Exception:
            return None
    return None

def format_hash_value(value: Any) -> str:
    if value in (None, "", _FORMAT_EMPTY):
        return _FORMAT_EMPTY
    ivalue = _parse_intish(value)
    if ivalue is None:
        text = str(value).strip()
        if len(text) == 8 and all(c in "0123456789abcdefABCDEF" for c in text):
            try:
                return f"0x{int(text, 16):08X}"
            except Exception:
                return text
        return text
    if ivalue == 0:
        return _FORMAT_EMPTY
    return f"0x{ivalue & 0xFFFFFFFF:08X}"

def format_display_value(value: Any, header: str = "") -> str:
    """Readable UI formatter: hashes become 0xHEX, counts get commas, blanks become dashes."""
    header_l = (header or "").strip().lower()
    if value is None:
        return _FORMAT_EMPTY
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:,.6g}"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return _FORMAT_EMPTY
        if text.lower() in {"true", "false"}:
            return "Yes" if text.lower() == "true" else "No"
        if text.startswith("Unknown 0x"):
            return "Unknown · " + format_hash_value(text.replace("Unknown ", ""))
        if text.startswith("0x") or (len(text) == 8 and all(c in "0123456789abcdefABCDEF" for c in text) and "hash" in header_l):
            if "hash" in header_l:
                return format_hash_value(text)
        value = text
    if "hash" in header_l:
        return format_hash_value(value)
    ivalue = _parse_intish(value)
    if ivalue is not None:
        # Keep field IDs compact; format user-facing amounts and big unit/slot ids.
        if header_l in {"field id"}:
            return str(ivalue)
        if header_l in _COUNT_HEADERS or header_l in _ID_HEADERS or abs(ivalue) >= 10000:
            return f"{ivalue:,}"
        return str(ivalue)
    return str(value)

def format_raw_and_display(value: Any, header: str = "") -> tuple[str, str]:
    raw = "" if value is None else str(value)
    shown = format_display_value(value, header)
    return raw, shown


class UnitTableModel(QAbstractTableModel):
    headers = ["Type", "Index", "Field ID", "Field Name", "Unit ID", "Unit Name", "Count", "Values"]

    def __init__(self) -> None:
        super().__init__()
        self.save: Optional[GBFRSaveData] = None
        self.item_db = ItemDatabase()
        self.resource_db = ResourceIdDatabase()
        self.records: List[UnitRecord] = []
        self.filtered: List[UnitRecord] = []
        self.filter_text = ""
        self.unit_labels = UnitLabelIndex.empty()

    def set_save(self, save: Optional[GBFRSaveData]) -> None:
        self.beginResetModel()
        self.save = save
        self.records = save.records if save else []
        self.unit_labels = UnitLabelIndex.from_save(save, self.item_db) if save else UnitLabelIndex.empty()
        self.filtered = list(self.records)
        self.endResetModel()

    def set_item_db(self, db: ItemDatabase) -> None:
        self.item_db = db
        if self.save:
            self.unit_labels = UnitLabelIndex.from_save(self.save, self.item_db)
            self.dataChanged.emit(self.index(0, 0), self.index(max(0, self.rowCount() - 1), self.columnCount() - 1))

    def set_resource_db(self, db: ResourceIdDatabase) -> None:
        self.resource_db = db
        if self.save and self.rowCount() > 0:
            self.dataChanged.emit(self.index(0, 0), self.index(max(0, self.rowCount() - 1), self.columnCount() - 1))

    def set_filter(self, text: str) -> None:
        self.filter_text = text.strip().lower()
        self.beginResetModel()
        if not self.filter_text:
            self.filtered = list(self.records)
        else:
            q = self.filter_text
            out: List[UnitRecord] = []
            for rec in self.records:
                name = unit_name(rec.id_type).lower()
                value_preview = self.preview(rec, 32).lower() if self.save else ""
                unit_label = self.unit_labels.label_for(rec).lower()
                if (
                    q in rec.kind.lower()
                    or q in name
                    or q == str(rec.id_type)
                    or q == str(rec.unit_id)
                    or q in f"0x{rec.id_type:x}"
                    or q in f"0x{rec.unit_id:x}"
                    or q in unit_label
                    or q in value_preview
                ):
                    out.append(rec)
            self.filtered = out
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.filtered)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def record_at(self, row: int) -> Optional[UnitRecord]:
        if 0 <= row < len(self.filtered):
            return self.filtered[row]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            return None
        if self.save is None:
            return None
        rec = self.filtered[index.row()]
        col = index.column()
        if col == 0:
            return rec.kind
        if col == 1:
            return format_display_value(rec.index, "Index")
        if col == 2:
            return format_display_value(rec.id_type, "Field ID")
        if col == 3:
            return unit_name(rec.id_type)
        if col == 4:
            return format_display_value(rec.unit_id, "Unit")
        if col == 5:
            return self.unit_labels.label_for(rec)
        if col == 6:
            return format_display_value(rec.value_count, "Count")
        if col == 7:
            return self.preview(rec, 10 if role == Qt.ItemDataRole.DisplayRole else 64)
        return None

    def preview(self, rec: UnitRecord, limit: int = 10) -> str:
        if self.save is None:
            return ""
        values = self.save.get_values(rec, limit)
        shown: List[str] = []
        for val in values:
            if isinstance(val, bool):
                shown.append("true" if val else "false")
            elif rec.kind == "uint" and rec.id_type in HASHISH_ID_TYPES:
                iv = int(val) & 0xFFFFFFFF
                entry = self.item_db.lookup_hash(iv)
                if entry:
                    shown.append(f"{entry.display_name} ({entry.item_id})")
                else:
                    cats = _resource_categories_for_field(rec.id_type)
                    resource = self.resource_db.lookup_value(iv, cats) if cats else self.resource_db.lookup_value(iv)
                    if resource:
                        shown.append(f"{resource.name} ({resource.id_text})")
                    else:
                        shown.append(f"0x{iv:08X}")
            elif isinstance(val, float):
                shown.append(f"{val:.6g}")
            else:
                resource = None
                if isinstance(val, int):
                    cats = _resource_categories_for_field(rec.id_type)
                    if cats:
                        resource = self.resource_db.lookup_value(int(val), cats)
                if resource:
                    shown.append(f"{resource.name} ({resource.id_text})")
                else:
                    shown.append(format_display_value(val, "Value"))
        if rec.value_count > limit:
            shown.append("...")
        return ", ".join(shown)


class SimpleRowsModel(QAbstractTableModel):
    def __init__(self, headers: List[str]) -> None:
        super().__init__()
        self.headers = headers
        self.rows: List[List[Any]] = []
        # Optional per-table inline editing. Most lookup tables stay read-only;
        # editor pages can opt in by setting editable_columns and a handler.
        self.editable_columns: set[int] = set()
        self.set_data_handler = None

    def set_rows(self, rows: List[List[Any]]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.isValid() and index.column() in self.editable_columns:
            return base | Qt.ItemFlag.ItemIsEditable
        return base

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if role != Qt.ItemDataRole.EditRole or not index.isValid() or index.column() not in self.editable_columns:
            return False
        if self.set_data_handler is not None:
            return bool(self.set_data_handler(index.row(), index.column(), value))
        try:
            self.rows[index.row()][index.column()] = value
        except IndexError:
            return False
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole])
        return True

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        try:
            value = self.rows[index.row()][index.column()]
        except IndexError:
            return None
        header = self.headers[index.column()] if index.column() < len(self.headers) else "Value"
        if role == Qt.ItemDataRole.DisplayRole:
            return format_display_value(value, header)
        if role == Qt.ItemDataRole.EditRole:
            return "" if value in (None, _FORMAT_EMPTY) else str(value)
        if role == Qt.ItemDataRole.ToolTipRole:
            row = self.rows[index.row()]
            raw, shown = format_raw_and_display(value, header)
            parts = [f"{header}: {shown}"]
            if raw and raw != shown:
                parts.append(f"Raw: {raw}")
            if index.column() in self.editable_columns:
                parts.append("Editable: double-click this cell or select it and type a new value.")
            # Rows with resolved GBIDs use columns [Name, GBID, Hash] in several pages.
            if "GBID" in self.headers and "Hash" in self.headers:
                try:
                    gbid = row[self.headers.index('GBID')]
                    hval = row[self.headers.index('Hash')]
                    parts.append(f"GBID: {format_display_value(gbid, 'GBID')}")
                    parts.append(f"Hash: {format_hash_value(hval)}")
                except Exception:
                    pass
            parts.append("Use the selected-row panel for safer grouped edits, or jump to the raw save unit when available.")
            return "\n".join(str(x) for x in parts if x not in (None, ""))
        return None


class GbidTableModel(QAbstractTableModel):
    headers = ["Category", "ID", "Name", "Hash", "Decimal", "Aliases"]

    def __init__(self) -> None:
        super().__init__()
        self.db = ItemDatabase()
        self.rows = []

    def set_db(self, db: ItemDatabase) -> None:
        self.db = db
        self.set_filter("")

    def set_filter(self, text: str) -> None:
        self.beginResetModel()
        self.rows = self.db.search(text, limit=10000)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        entry = self.rows[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.tooltip()
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        col = index.column()
        if col == 0:
            return entry.category
        if col == 1:
            return entry.item_id
        if col == 2:
            return entry.display_name
        if col == 3:
            return format_hash_value(entry.hash_hex)
        if col == 4:
            return format_display_value(entry.hash_value & 0xFFFFFFFF, "Decimal")
        if col == 5:
            return entry.alias_text
        return None

    def entry_at(self, row: int):
        if 0 <= row < len(self.rows):
            return self.rows[row]
        return None


class ResourceIdTableModel(QAbstractTableModel):
    headers = ["Category", "ID", "Name", "Decimal", "Source", "Aliases"]

    def __init__(self) -> None:
        super().__init__()
        self.db = ResourceIdDatabase()
        self.rows = []

    def set_db(self, db: ResourceIdDatabase) -> None:
        self.db = db
        self.set_filter("")

    def set_filter(self, text: str) -> None:
        self.beginResetModel()
        self.rows = self.db.search(text, limit=20000)
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        entry = self.rows[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.tooltip()
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        col = index.column()
        if col == 0:
            return entry.category
        if col == 1:
            return entry.id_text
        if col == 2:
            return entry.name
        if col == 3:
            return format_display_value(entry.decimal_value, "Decimal")
        if col == 4:
            return entry.source
        if col == 5:
            return entry.alias_text
        return None

    def entry_at(self, row: int):
        if 0 <= row < len(self.rows):
            return self.rows[row]
        return None


class HashScanTableModel(SimpleRowsModel):
    pass

def make_card(title: str) -> QGroupBox:
    box = QGroupBox(title)
    box.setProperty("class", "card")
    return box




def _debug_log_path() -> Path:
    """Best-effort log path for GUI save/load crashes."""
    try:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "GBFRRelinkEditor"
        base.mkdir(parents=True, exist_ok=True)
        return base / "gbfr_editor_debug.log"
    except Exception:
        return Path.cwd() / "gbfr_editor_debug.log"


def _debug_log(message: str) -> None:
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        _debug_log_path().open("a", encoding="utf-8").write(f"[{ts}] {message}\n")
    except Exception:
        pass


def _install_exception_logging() -> None:
    """Log uncaught GUI exceptions instead of only dumping them to a console."""
    try:
        if getattr(_install_exception_logging, "_installed", False):
            return
        old_hook = sys.excepthook

        def _hook(exc_type, exc_value, exc_tb):
            try:
                _debug_log("UNCAUGHT EXCEPTION:\n" + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
            except Exception:
                pass
            try:
                old_hook(exc_type, exc_value, exc_tb)
            except Exception:
                pass

        sys.excepthook = _hook
        _install_exception_logging._installed = True
        _debug_log("exception logging installed")
    except Exception:
        pass

class MainWindow(QMainWindow):
    def format_value(self, value: Any, header: str = "") -> str:
        """UI-friendly value formatter used by labels/detail cards.

        Kept as a MainWindow method so older/newer page code can call
        self.format_value(...) while the actual formatting logic stays in
        the shared module-level formatter.
        """
        return format_display_value(value, header)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1480, 920)
        self.setMinimumSize(1100, 700)
        self.save: Optional[GBFRSaveData] = None
        self.dirty = False
        # Guard save/write operations from delayed UI timers.  Qt can emit
        # "QObject::killTimer" warnings/crashes when a pending timer is torn
        # down while Save As is running or while a modal save dialog is closing.
        self._save_in_progress = False
        self.item_db = ItemDatabase.load_many([
            RESOURCE_DIR / "item_ids_seed.csv",
            RESOURCE_DIR / "sigil_gem_ids_seed.csv",
            RESOURCE_DIR / "sigil_generated_plus_seed.csv",
            RESOURCE_DIR / "sigil_verified_extra_seed.csv",
            RESOURCE_DIR / "trait_skill_seed.csv",
            RESOURCE_DIR / "character_ids_seed.csv",
            RESOURCE_DIR / "model_hash_ids_seed.csv",
            RESOURCE_DIR / "phase_hash_ids_seed.csv",
            RESOURCE_DIR / "item_ids_downloaded.csv",
            RESOURCE_DIR / "item_ids_sheet_merged.csv",
        ])

        self.material_bank_templates = self._load_material_bank_templates()
        self.character_owner_choices = self._build_character_owner_choices()

        self.resource_db = ResourceIdDatabase.load_many([
            RESOURCE_DIR / "resource_ids_seed.csv",
            RESOURCE_DIR / "model_hash_ids_seed.csv",
            RESOURCE_DIR / "phase_hash_ids_seed.csv",
            RESOURCE_DIR / "quest_ids_seed.csv",
            RESOURCE_DIR / "resource_ids_downloaded.csv",
        ])
        self.reference_db = ReferenceDatabase.load_many([
            RESOURCE_DIR / "reference_notes_seed.csv",
            RESOURCE_DIR / "reference_notes_downloaded.csv",
        ])

        self.unit_model = UnitTableModel()
        self.unit_model.set_item_db(self.item_db)
        self.unit_model.set_resource_db(self.resource_db)
        self.unit_map_model = SimpleRowsModel(["Group", "Slot", "Name", "Source", "Hash", "GBID", "Fields"])
        self.save_finder_model = SimpleRowsModel(["Save", "Type", "Folder", "Modified"])
        self.save_finder_rows_meta: List[Dict[str, Any]] = []
        self.sigil_model = SimpleRowsModel(["Unit", "Slot", "Sigil", "GBID", "Hash", "Lv", "Equipped To", "Equipped GBID", "Lock / Flags"])
        self.sigil_model.editable_columns = {2, 3, 4, 5, 6, 7, 8}
        self.sigil_model.set_data_handler = self.apply_sigil_table_cell_edit
        self.sigil_database_model = SimpleRowsModel(["Status", "Name", "GBID", "Hash", "Grade", "Owned", "Empty Slots", "Action"])
        self.sigil_empty_model = SimpleRowsModel(["Unit", "Slot", "Level", "Owner", "Flags", "Notes"])
        self.weapon_model = SimpleRowsModel(["Slot", "Weapon", "GBID", "Hash", "XP", "2805", "2806", "2807", "2814", "Flags", "Stone"])
        self.weapon_model.editable_columns = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10}
        self.weapon_model.set_data_handler = self.apply_weapon_table_cell_edit
        self.weapon_database_model = SimpleRowsModel(["Status", "Weapon", "GBID", "Hash", "Owner", "Owned", "Empty Slots", "Action"])
        self.weapon_empty_model = SimpleRowsModel(["Unit", "Hash", "XP", "Flags", "Stone", "Notes"])
        self.character_model = SimpleRowsModel(["Slot", "Character", "GBID", "Hash", "Level", "EXP", "MSP?", "Unlock?", "Unit"])
        self.character_model.editable_columns = {1, 2, 3, 4, 5, 6, 7}
        self.character_model.set_data_handler = self.apply_character_table_cell_edit
        self.mastery_slot_model = SimpleRowsModel(["Slot #", "Socket #", "1606 Effect Label", "Row Type", "Active", "1607 Amount / State", "1601 Slot Key Label", "1606 Hash", "Save Unit"])
        self.mastery_slot_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_model = SimpleRowsModel(["Row", "Kind", "Current Effect", "Hidden Type", "Value / Amount", "Hidden Hash", "Hidden Unit", "Hidden Pair"])
        # Current rows deliberately keep a few hidden columns in the backing
        # model because older helper code updates by column index. The visible
        # editor only shows Row / Kind / Effect / Value.
        self.mastery_mod_reference_model = SimpleRowsModel(["Effect / Stat", "Effect Hash", "Value Override", "Category", "Notes"])
        self.mastery_mod_code_model = SimpleRowsModel(["Pattern Row", "SW Rel Offset", "Target Effect", "Current Effect", "Current Hash", "Save Unit", "Repeat", "Status"])
        self.mastery_mod_preset_model = SimpleRowsModel(["Preset Range", "Rows", "Effect To Install", "1606 Hash", "Reason / Notes"])
        self.mastery_mod_value_model = SimpleRowsModel(["Value Preset", "Decimal", "Hex", "Risk", "What It Does"])
        self.mastery_mod_reference_model.editable_columns = {2}
        self.mastery_mod_reference_model.set_data_handler = self.apply_mastery_mod_reference_cell_edit
        self.mastery_mod_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_reference_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_code_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_preset_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_value_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_choices_cache: Optional[List[Dict[str, Any]]] = None
        self._mastery_mod_cache_key: Optional[tuple] = None
        self._mastery_mod_grouped_cache: Optional[Dict[int, Dict[int, UnitRecord]]] = None
        self._mastery_mod_anchor_cache_key: Optional[tuple] = None
        self._mastery_mod_anchor_cache: Optional[int] = None
        self._mastery_mod_anchor_source: str = ""
        self._mastery_mod_anchor_score: int = 0
        self._mastery_mod_anchor_by_field: Dict[int, Optional[int]] = {}
        self._mastery_mod_anchor_source_by_field: Dict[int, str] = {}
        self._mastery_mod_anchor_score_by_field: Dict[int, int] = {}
        self._mastery_mod_anchor_cache_key_by_field: Dict[int, tuple] = {}
        self.mastery_offset_pattern_cache: Optional[List[Dict[str, Any]]] = None
        self._mastery_mod_abs1606_cache_key: Optional[tuple] = None
        self._mastery_mod_abs1606_cache: Dict[int, UnitRecord] = {}
        self.item_model = SimpleRowsModel(["Slot", "Item", "GBID", "Hash", "Index", "Flag", "Qty / Value", "Backing Store"])
        self.item_model.editable_columns = {1, 2, 3, 4, 5, 6}
        self.item_model.set_data_handler = self.apply_item_table_cell_edit
        self.items_database_model = SimpleRowsModel(["Status", "Name", "GBID", "Category", "Hash", "Action"])
        self.items_database_rows_meta: List[Dict[str, Any]] = []
        self.relic_database_model = SimpleRowsModel(["Status", "Name", "GBID", "Hash", "Owned", "Empty Slots", "Action"])
        self.relic_database_rows_meta: List[Dict[str, Any]] = []
        self.gbid_model = GbidTableModel()
        self.gbid_model.set_db(self.item_db)
        self.resource_id_model = ResourceIdTableModel()
        self.resource_id_model.set_db(self.resource_db)
        self.database_model = SimpleRowsModel(["Database", "Category", "Rows", "Known Values", "Notes"])
        self.item_id_catalog_model = SimpleRowsModel(["Category", "Group", "Name", "GBID", "Hash", "Aliases"])
        self.sigil_gem_catalog_model = SimpleRowsModel(["Group", "Name", "GBID", "Hash", "Family", "Grade", "Tier", "Plus", "Variant", "Aliases"])
        self.trait_skill_catalog_model = SimpleRowsModel(["Group", "Name", "Skill ID", "Hash", "Family", "Variant", "Status", "Aliases"])
        self.model_id_catalog_model = SimpleRowsModel(["Category", "Group", "Name", "Model ID", "Hash", "Decimal", "Aliases", "Source"])
        self.phase_id_catalog_model = SimpleRowsModel(["Category", "Group", "Name", "Phase ID", "Entity Code", "Phase Hash", "Entity Hash", "Source", "Aliases"])
        self.quest_id_catalog_model = SimpleRowsModel(["Category", "Group", "Name", "Quest ID", "Numeric Value", "Encoded Hex", "Source", "Aliases"])
        self.reference_model = SimpleRowsModel(["Category", "Topic", "Key", "Value", "Notes", "Source"])
        self.id_audit_model = SimpleRowsModel(["Manager", "Field", "Hash", "Status", "Name", "ID/GBID", "Occurrences", "Units", "Source", "Note"])
        self.candidate_model = SimpleRowsModel(["Category", "Confidence", "Kind", "ID", "Name", "Unit", "Count", "Preview", "Note"])
        self.hash_scan_model = HashScanTableModel(["Category", "Name", "GBID", "Hash", "Kind", "ID", "Unit", "Index", "Known", "Aliases"])
        self.preset_model = SimpleRowsModel(["Category", "Preset", "Rows", "Items", "Sigils", "Weapons", "Description", "Key"])
        self.add_browser_model = SimpleRowsModel(["Action", "Name", "GBID", "Hash", "Type", "Status / Notes"])
        self._add_browser_tab = "Safe Add"
        self._add_browser_tab_buttons: List[QPushButton] = []
        self._add_browser_index_key = None
        self._add_browser_active_material_hashes: set[int] = set()
        self._add_browser_safe_template_hashes: set[int] = set()
        self._add_browser_empty_counts = {"items": 0, "sigils": 0, "weapons": 0}
        self._add_browser_refresh_timer = QTimer(self)
        self._add_browser_refresh_timer.setSingleShot(True)
        self._add_browser_refresh_timer.setInterval(250)
        self._add_browser_refresh_timer.timeout.connect(self.refresh_add_browser_rows)
        self.save_wizard_model = SimpleRowsModel(["Category", "Reference", "Source", "Status", "Notes", "Key"])
        self.progression_model = SimpleRowsModel(["Section", "Confidence", "Records", "Values", "Non-zero", "Recommended Action"])
        self.progression_edit_model = SimpleRowsModel(["Quest ID", "Name", "Status", "Rank", "Done", "Source"])
        self.progression_edit_model.editable_columns = {2, 3, 4}
        self.progression_edit_model.set_data_handler = self.apply_progression_table_cell_edit
        self._progression_catalog_cache: Optional[List[Dict[str, Any]]] = None
        self._progression_catalog_counts_cache: Dict[str, int] = {}
        self._progression_vector_record_cache: Dict[int, Optional[UnitRecord]] = {}
        self._progression_vector_values_cache: Dict[int, List[Any]] = {}
        self._progression_key_index_cache: Dict[tuple[str, int], Dict[int, int]] = {}
        self._progression_controls_loading = False
        self._progression_edit_refresh_timer = QTimer(self)
        self._progression_edit_refresh_timer.setSingleShot(True)
        self._progression_edit_refresh_timer.setInterval(180)
        self._progression_edit_refresh_timer.timeout.connect(self.refresh_progression_editor_rows)
        self.progression_rows_model = SimpleRowsModel(["Section", "Field ID", "Field Name", "Unit ID", "Unit Name", "Record", "Count", "Non-zero", "Values", "Notes"])
        self.save_map_model = SimpleRowsModel(["Manager", "Confidence", "Kind", "Field ID", "Field Name", "Records", "Units", "Known Hashes", "Unknown Hashes", "Sample", "Note"])
        self.hash_scan_rows: List[Dict[str, Any]] = []
        self.item_rows_meta: List[Dict[str, Any]] = []
        self.sigil_rows_meta: List[Dict[str, Any]] = []
        self.sigil_database_rows_meta: List[Dict[str, Any]] = []
        self.sigil_empty_rows_meta: List[Dict[str, Any]] = []
        self.weapon_rows_meta: List[Dict[str, Any]] = []
        self.weapon_database_rows_meta: List[Dict[str, Any]] = []
        self.weapon_empty_rows_meta: List[Dict[str, Any]] = []
        self.character_rows_meta: List[Dict[str, Any]] = []
        self.item_slot_clipboard: Optional[Dict[str, Any]] = None
        self.sigil_slot_clipboard: Optional[Dict[str, Any]] = None
        self.weapon_slot_clipboard: Optional[Dict[str, Any]] = None
        self.character_slot_clipboard: Optional[Dict[str, Any]] = None
        self.overmastery_clipboard: Optional[List[int]] = None
        self.compare_before_path: Optional[str] = None
        self.compare_after_path: Optional[str] = None
        self.save_finder_base_path: str = ""
        self.settings_path = SETTINGS_PATH
        self.current_theme = "modern_dark"
        self.advanced_mode = False
        self.ui_clean_mode = True
        self.compact_mode = False
        # Fast load mode keeps save-open responsive by refreshing only the visible tab first.
        # Heavy research/catalog tables refresh lazily when opened.
        self.fast_load_mode = True
        # Auto-fit requires scanning every visible cell in a table and was the biggest UI delay
        # on large saves. Users can re-enable it from View when needed.
        self.auto_fit_tables = False
        self.nav_widgets: List[QWidget] = []
        self.nav_button_by_label: Dict[str, QPushButton] = {}
        self.advanced_nav_widgets: List[QWidget] = []
        self._stale_page_labels = set()
        self._refreshing_page = False
        self._fast_edit_mode = True
        self._filter_timers: Dict[str, QTimer] = {}
        self._save_in_progress = False
        self._load_in_progress = False
        self._io_guard_depth = 0
        self._save_thread = None
        self._save_worker = None

        self._load_ui_settings()
        self._build_ui()
        self._build_menu()
        if hasattr(self, "advanced_checkbox"):
            self.advanced_checkbox.setChecked(bool(self.advanced_mode))
            self._set_advanced_visible(bool(self.advanced_mode), persist=False)
        self.apply_theme()
        self._apply_view_preferences(persist=False)
        # Populate only the first user-facing table at startup.
        # Catalog/research pages can contain thousands of rows and are refreshed lazily when opened.
        self.refresh_preset_rows()


    def _nav_section(self, text: str, advanced: bool = False) -> QLabel:
        label = QLabel(text.upper())
        label.setObjectName("navSection")
        if advanced:
            self.advanced_nav_widgets.append(label)
        return label

    def _show_page(self, label: str) -> None:
        idx = self.page_indexes.get(label)
        if idx is not None:
            self.stack.setCurrentIndex(idx)

    def _update_nav_selection(self, active_label: str) -> None:
        for label, btn in getattr(self, "nav_button_by_label", {}).items():
            btn.blockSignals(True)
            btn.setChecked(label == active_label)
            btn.blockSignals(False)

    def _current_page_label(self) -> str:
        if not hasattr(self, "stack"):
            return ""
        idx = self.stack.currentIndex()
        for label, page_idx in getattr(self, "page_indexes", {}).items():
            if page_idx == idx:
                return label
        return ""

    def _mark_all_pages_stale(self) -> None:
        self._stale_page_labels = set(getattr(self, "page_indexes", {}).keys())

    def _mark_stale_pages(self, labels: List[str]) -> None:
        if not hasattr(self, "_stale_page_labels"):
            self._stale_page_labels = set()
        self._stale_page_labels.update(str(label) for label in labels)

    def _on_stack_page_changed(self, index: int) -> None:
        if getattr(self, "_refreshing_page", False):
            return
        label = self._current_page_label()
        if label:
            self._update_nav_selection(label)
            self._refresh_page_by_label(label)

    def _refresh_page_by_label(self, label: str, force: bool = False) -> None:
        if not label:
            return
        if bool(getattr(self, "_io_guard_depth", 0)) and not force:
            self._mark_stale_pages([label])
            return
        if not force and label not in getattr(self, "_stale_page_labels", set()):
            return
        self._refreshing_page = True
        try:
            if label == "Welcome":
                self.update_status_text()
            elif label == "Cheats":
                self.refresh_preset_rows()
                self.update_preset_detail()
            elif label == "Progression":
                self.refresh_progression_rows()
            elif label == "Items / Materials":
                self._refresh_current_items_subtab()
            elif label == "Sigils":
                self.refresh_sigil_rows()
            elif label == "Weapons":
                self.refresh_weapon_rows()
            elif label == "Characters":
                self.refresh_character_rows()
            elif label == "Masteries":
                self._populate_mastery_character_combo()
                self.refresh_mastery_slot_rows()
            elif label == "Mastery":
                self._populate_mastery_mod_character_combo()
                tabs = getattr(self, "mastery_value_tabs", None)
                if tabs is not None and tabs.currentIndex() == 1:
                    self.refresh_mastery_mod_rows()
                elif hasattr(self, "mastery_mod_status_label"):
                    self.mastery_mod_status_label.setText("Ready. Pick four Overmastery stats, choose a value, or open Rows / Edit to inspect individual save rows.")
            elif label == "Save Health":
                self.refresh_save_health()
            elif label == "Units":
                self.unit_model.set_save(self.save)
                self.unit_model.set_filter(self.filter_edit.text())
            elif label == "Unit Map":
                self.refresh_unit_map_rows()
            elif label == "Save Map":
                self.refresh_save_map_rows()
            elif label == "ID Cleanup":
                self.refresh_id_audit_rows()
                self.refresh_candidate_rows()
            elif label == "GBID Browser":
                self.refresh_database_rows()
            elif label == "Item ID Catalog":
                self.refresh_item_id_catalog_rows()
            elif label == "Sigil/Gem ID Catalog":
                self.refresh_sigil_gem_catalog_rows()
            elif label == "Trait/Skill ID Catalog":
                self.refresh_trait_skill_catalog_rows()
            elif label == "Model ID Catalog":
                self.refresh_model_id_catalog_rows()
            elif label == "Phase ID Catalog":
                self.refresh_phase_id_catalog_rows()
            elif label == "Quest ID Catalog":
                self.refresh_quest_id_catalog_rows()
            elif label == "Reference Tables":
                self.refresh_reference_rows()
            elif label == "Resource Database":
                self.refresh_database_rows()
            elif label == "Hash Scan":
                if hasattr(self, "hash_scan_text") and not getattr(self, "hash_scan_rows", []):
                    self.hash_scan_text.setPlainText("Run a hash scan to list known GBIDs and unknown hash-like fields in the loaded save.")
            elif label == "Research":
                if hasattr(self, "research_text") and not getattr(self, "value_search_results", []):
                    self.research_text.setPlainText("Load a save, then use value search for exact before/after hunting.")
            self._stale_page_labels.discard(label)
        finally:
            self._refreshing_page = False

    def _set_advanced_visible(self, enabled: bool, persist: bool = True) -> None:
        # Research/lookup tabs are intentionally hidden from the end-user UI.
        self.advanced_mode = False
        for widget in getattr(self, "advanced_nav_widgets", []):
            widget.setVisible(False)
        if hasattr(self, "stack"):
            visible_pages = {"Welcome", "Cheats", "Progression", "Items / Materials", "Sigils", "Weapons", "Characters", "Mastery", "Save Health", "About"}
            current_label = self._current_page_label()
            if current_label and current_label not in visible_pages:
                self._show_page("Welcome")
        self._update_nav_selection(self._current_page_label() or "Welcome")
        if persist:
            self._save_ui_settings()

    def _add_nav_button(self, layout: QVBoxLayout, label: str, page_builder, advanced: bool = False) -> QPushButton:
        idx = self.stack.addWidget(page_builder())
        self.page_indexes[label] = idx
        nav_icons = {
            "Welcome": "⌂", "Cheats": "⚡", "Progression": "◆",
            "Items / Materials": "▣", "Sigils": "◇", "Weapons": "⚔", "Characters": "◉", "Mastery": "✚",
            "Save Health": "✓", "About": "ⓘ", "Save Map": "🗺", "ID Cleanup": "⌁", "Unit Map": "◎",
            "GBID Browser": "#", "Item ID Catalog": "▦", "Sigil/Gem ID Catalog": "◇",
            "Trait/Skill ID Catalog": "✣", "Model ID Catalog": "▧", "Phase ID Catalog": "◌",
            "Quest ID Catalog": "?", "Reference Tables": "≡", "Resource Database": "▤",
            "Entity Prefixes": "<>", "Units": "☷", "Resource IDs": "ID", "Hash Tools": "#",
            "Data Sources": "↧", "Hash Scan": "⌕", "Research": "⌬", "Compare": "⇄", "Raw Tools": "{}",
        }
        icon = nav_icons.get(label, "•")
        btn = QPushButton(f"{icon}  {label}")
        btn.setProperty("class", "navButton")
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(lambda _=False, name=label: self._show_page(name))
        layout.addWidget(btn)
        self.nav_widgets.append(btn)
        self.nav_button_by_label[label] = btn
        if advanced:
            self.advanced_nav_widgets.append(btn)
            btn.setVisible(False)
        return btn

    def _make_more_button(self, title: str, actions: List[tuple[str, Any]]) -> QPushButton:
        btn = QPushButton(title)
        menu = QMenu(btn)
        for text, slot in actions:
            action = QAction(text, self)
            action.triggered.connect(slot)
            menu.addAction(action)
        btn.setMenu(menu)
        return btn

    def _make_filter_button(self, title: str, checkboxes: List[tuple[str, QCheckBox]]) -> QPushButton:
        """Compact checkable filter menu used by the main editor tabs."""
        btn = QPushButton(title)
        menu = QMenu(btn)
        for text, checkbox in checkboxes:
            action = QAction(text, self)
            action.setCheckable(True)
            action.setChecked(checkbox.isChecked())
            action.toggled.connect(checkbox.setChecked)
            checkbox.toggled.connect(action.setChecked)
            menu.addAction(action)
        btn.setMenu(menu)
        return btn

    def _make_action_button(self, text: str, slot: Any, primary: bool = False) -> QPushButton:
        btn = QPushButton(text)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        if primary:
            btn.setProperty("class", "primaryButton")
        btn.clicked.connect(slot)
        return btn

    def _add_action_grid(self, layout: QVBoxLayout, actions: List[tuple[str, Any]], columns: int = 4, primary_first: bool = False) -> None:
        """Add responsive-looking action buttons without long horizontal rows."""
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        for idx, (text, slot) in enumerate(actions):
            btn = self._make_action_button(text, slot, primary=primary_first and idx == 0)
            grid.addWidget(btn, idx // max(1, columns), idx % max(1, columns))
        layout.addLayout(grid)

    def _fit_action_row(self, row: QHBoxLayout) -> None:
        """Keep older horizontal action rows from fighting for screen width."""
        for idx in range(row.count()):
            item = row.itemAt(idx)
            widget = item.widget() if item else None
            if isinstance(widget, QPushButton):
                widget.setCursor(Qt.CursorShape.PointingHandCursor)
                widget.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)

    def _set_compact_detail(self, widget: QWidget, max_height: int = 82) -> None:
        widget.setMaximumHeight(max_height)

    def _table_clean(self, table: QTableView, hidden_columns: tuple[int, ...] = ()) -> None:
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(24 if getattr(self, "compact_mode", True) else 31)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.setTextElideMode(Qt.TextElideMode.ElideRight)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        for col in hidden_columns:
            table.setColumnHidden(col, True)

    def _auto_fit_table(self, table: QTableView, max_width: int = 280) -> None:
        """Resize visible columns after refresh, while capping giant text columns."""
        if not getattr(self, "auto_fit_tables", True):
            return
        try:
            table.resizeColumnsToContents()
            for col in range(table.model().columnCount()):
                if table.isColumnHidden(col):
                    continue
                width = table.columnWidth(col)
                if width > max_width:
                    table.setColumnWidth(col, max_width)
                elif width < 72:
                    table.setColumnWidth(col, 72)
        except Exception:
            pass

    def _set_table_widths(self, table: QTableView, widths: Dict[int, int]) -> None:
        """Set sane readable column widths when expensive auto-fit is disabled."""
        try:
            for col, width in widths.items():
                if not table.isColumnHidden(col):
                    table.setColumnWidth(col, width)
        except Exception:
            pass

    def _configure_mastery_rows_table(self) -> None:
        """Make the Mastery Value Editor's current-row table readable.

        The backing model still carries hidden debug columns for old helper
        functions, but the user-facing table should read like a normal editor:
        Row, Kind, Current Effect, and Value / Amount.
        """
        table = getattr(self, "mastery_mod_table", None)
        if table is None:
            return
        try:
            table.setWordWrap(False)
            table.setTextElideMode(Qt.TextElideMode.ElideRight)
            table.verticalHeader().setDefaultSectionSize(34)
            table.verticalHeader().setMinimumSectionSize(32)
            header = table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            # Hide research columns in the normal editor. Column 3 is a raw
            # category duplicate, and 5-7 are internal hash/unit/offset data.
            for col in (3, 5, 6, 7):
                table.hideColumn(col)
            table.setColumnWidth(0, 70)
            table.setColumnWidth(1, 170)
            table.setColumnWidth(2, 520)
            table.setColumnWidth(4, 250)
            try:
                header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            except Exception:
                pass
        except Exception:
            pass

    def _load_ui_settings(self) -> None:
        try:
            if not self.settings_path.exists():
                return
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
            self.current_theme = str(data.get("theme", self.current_theme))
            self.advanced_mode = False
            self.save_finder_base_path = str(data.get("save_finder_base_path", self.save_finder_base_path))
            self.ui_clean_mode = bool(data.get("clean_mode", self.ui_clean_mode))
            self.compact_mode = bool(data.get("compact_mode", self.compact_mode))
            self.auto_fit_tables = bool(data.get("auto_fit_tables", self.auto_fit_tables))
            self.fast_load_mode = bool(data.get("fast_load_mode", self.fast_load_mode))
        except Exception:
            # Bad settings should never stop the editor from opening.
            pass

    def _save_ui_settings(self) -> None:
        try:
            data = {
                "theme": self.current_theme,
                "advanced_mode": self.advanced_mode,
                "clean_mode": self.ui_clean_mode,
                "compact_mode": self.compact_mode,
                "auto_fit_tables": self.auto_fit_tables,
                "fast_load_mode": self.fast_load_mode,
                "save_finder_base_path": self.save_finder_base_path,
            }
            self.settings_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _apply_view_preferences(self, persist: bool = True) -> None:
        row_height = 24 if getattr(self, "compact_mode", True) else 31
        for table in self.findChildren(QTableView):
            try:
                table.verticalHeader().setDefaultSectionSize(row_height)
            except Exception:
                pass
        for label in self.findChildren(QLabel):
            try:
                if label.objectName() == "helpText":
                    label.setVisible(not getattr(self, "ui_clean_mode", True))
                elif label.objectName() == "navSubtitle":
                    label.setVisible(not getattr(self, "ui_clean_mode", True))
            except Exception:
                pass
        if hasattr(self, "nav"):
            self.nav.setFixedWidth(208 if getattr(self, "ui_clean_mode", True) else 250)
        if hasattr(self, "clean_mode_action"):
            self.clean_mode_action.setChecked(bool(self.ui_clean_mode))
        if hasattr(self, "compact_mode_action"):
            self.compact_mode_action.setChecked(bool(self.compact_mode))
        if hasattr(self, "auto_fit_action"):
            self.auto_fit_action.setChecked(bool(self.auto_fit_tables))
        if hasattr(self, "fast_load_action"):
            self.fast_load_action.setChecked(bool(self.fast_load_mode))
        if persist:
            self._save_ui_settings()

    def set_clean_mode(self, enabled: bool) -> None:
        self.ui_clean_mode = bool(enabled)
        self._apply_view_preferences()
        self.statusBar().showMessage("Clean view enabled" if enabled else "Help text visible", 2500)

    def set_compact_mode(self, enabled: bool) -> None:
        self.compact_mode = bool(enabled)
        self._apply_view_preferences()
        self.apply_theme()
        self.statusBar().showMessage("Compact table rows enabled" if enabled else "Comfortable table rows enabled", 2500)

    def set_auto_fit_tables(self, enabled: bool) -> None:
        self.auto_fit_tables = bool(enabled)
        self._save_ui_settings()
        if enabled:
            for table in self.findChildren(QTableView):
                self._auto_fit_table(table)
        self.statusBar().showMessage("Auto-fit columns enabled" if enabled else "Auto-fit columns disabled", 2500)

    def set_fast_load_mode(self, enabled: bool) -> None:
        self.fast_load_mode = bool(enabled)
        self._save_ui_settings()
        self.statusBar().showMessage("Fast load enabled" if enabled else "Full refresh on load enabled", 2500)

    def reset_clean_view(self) -> None:
        self.ui_clean_mode = True
        self.compact_mode = False
        self.auto_fit_tables = False
        self.fast_load_mode = True
        self.advanced_mode = False
        if hasattr(self, "advanced_checkbox"):
            self.advanced_checkbox.setChecked(False)
        self._set_advanced_visible(False, persist=False)
        self._apply_view_preferences()
        self.statusBar().showMessage("Clean view reset", 2500)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        open_action = QAction("Open Save...", self)
        open_action.triggered.connect(self.open_save)
        save_action = QAction("Save", self)
        save_action.triggered.connect(self.save_original)
        save_as_action = QAction("Save As...", self)
        save_as_action.triggered.connect(self.save_as)
        export_action = QAction("Export JSON Report...", self)
        export_action.triggered.connect(self.export_report)
        compare_action = QAction("Compare Two Saves...", self)
        compare_action.triggered.connect(self.compare_two_saves_dialog)
        import_items_action = QAction("Import Item CSV...", self)
        import_items_action.triggered.connect(self.import_item_csv)
        download_items_action = QAction("Download Community Item/Sigil/Trait IDs", self)
        download_items_action.triggered.connect(self.download_item_ids)
        download_all_community_action = QAction("Download All Community Databases", self)
        download_all_community_action.triggered.connect(self.download_all_community_databases)
        export_db_action = QAction("Export Merged GBID DB...", self)
        export_db_action.triggered.connect(self.export_item_db_csv)
        file_menu.addAction(open_action)
        file_menu.addAction(save_action)
        file_menu.addAction(save_as_action)
        file_menu.addSeparator()
        file_menu.addAction(export_action)
        file_menu.addAction(compare_action)
        file_menu.addSeparator()
        file_menu.addAction(import_items_action)
        file_menu.addAction(download_items_action)
        file_menu.addAction(download_all_community_action)
        file_menu.addAction(export_db_action)

        theme_menu = self.menuBar().addMenu("Theme")
        for key, label in [
            ("modern_dark", "Modern Dark"),
            ("clean_dark", "Clean Dark"),
            ("midnight", "Midnight Blue"),
            ("slate", "Slate Dark"),
            ("light", "Clean Light"),
            ("sakura", "Sakura"),
            ("emerald", "Emerald"),
            ("graphite", "Graphite"),
            ("royal", "Royal Purple"),
            ("cyberpunk", "Cyberpunk Neon"),
            ("dracula", "Dracula"),
            ("ocean", "Deep Ocean"),
            ("forest", "Forest Green"),
            ("amber", "Amber Terminal"),
            ("oled", "OLED Black"),
            ("contrast", "High Contrast"),
        ]:
            action = QAction(label, self)
            action.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            theme_menu.addAction(action)

        view_menu = self.menuBar().addMenu("View")
        self.clean_mode_action = QAction("Clean view (hide page tips)", self)
        self.clean_mode_action.setCheckable(True)
        self.clean_mode_action.setChecked(bool(self.ui_clean_mode))
        self.clean_mode_action.toggled.connect(self.set_clean_mode)
        view_menu.addAction(self.clean_mode_action)

        self.compact_mode_action = QAction("Compact table rows", self)
        self.compact_mode_action.setCheckable(True)
        self.compact_mode_action.setChecked(bool(self.compact_mode))
        self.compact_mode_action.toggled.connect(self.set_compact_mode)
        view_menu.addAction(self.compact_mode_action)

        self.auto_fit_action = QAction("Auto-fit columns after refresh", self)
        self.auto_fit_action.setCheckable(True)
        self.auto_fit_action.setChecked(bool(self.auto_fit_tables))
        self.auto_fit_action.toggled.connect(self.set_auto_fit_tables)
        view_menu.addAction(self.auto_fit_action)

        self.fast_load_action = QAction("Fast load / lazy refresh", self)
        self.fast_load_action.setCheckable(True)
        self.fast_load_action.setChecked(bool(self.fast_load_mode))
        self.fast_load_action.toggled.connect(self.set_fast_load_mode)
        view_menu.addAction(self.fast_load_action)

        self.fast_edit_action = QAction("Fast edit mode (no full refresh per cell)", self)
        self.fast_edit_action.setCheckable(True)
        self.fast_edit_action.setChecked(bool(self._fast_edit_mode))
        self.fast_edit_action.toggled.connect(lambda checked: setattr(self, "_fast_edit_mode", bool(checked)))
        view_menu.addAction(self.fast_edit_action)

        refresh_page_action = QAction("Refresh Current Page", self)
        refresh_page_action.triggered.connect(self.refresh_current_page)
        view_menu.addAction(refresh_page_action)

        view_menu.addSeparator()
        reset_action = QAction("Reset clean view", self)
        reset_action.triggered.connect(self.reset_clean_view)
        view_menu.addAction(reset_action)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)

        self.nav = QFrame()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(250)
        nav_layout = QVBoxLayout(self.nav)
        nav_layout.setContentsMargins(14, 18, 14, 14)
        nav_layout.setSpacing(8)

        title = QLabel("GBFR Editor")
        title.setObjectName("navTitle")
        nav_layout.addWidget(title)

        subtitle = QLabel("Modern save workflow")
        subtitle.setObjectName("navSubtitle")
        nav_layout.addWidget(subtitle)

        self.stack = QStackedWidget()
        self.stack.setObjectName("contentStack")
        self.stack.currentChanged.connect(self._on_stack_page_changed)
        self.page_indexes: Dict[str, int] = {}

        nav_scroll = QScrollArea()
        nav_scroll.setObjectName("navScroll")
        nav_scroll.setWidgetResizable(True)
        nav_scroll.setFrameShape(QFrame.Shape.NoFrame)
        nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        nav_content = QWidget()
        nav_content.setObjectName("navContent")
        nav_items_layout = QVBoxLayout(nav_content)
        nav_items_layout.setContentsMargins(0, 8, 0, 8)
        nav_items_layout.setSpacing(6)
        nav_scroll.setWidget(nav_content)
        nav_layout.addWidget(nav_scroll, 1)

        nav_items_layout.addWidget(self._nav_section("Edit"))
        self._add_nav_button(nav_items_layout, "Welcome", self._overview_page)
        self._add_nav_button(nav_items_layout, "Cheats", self._cheat_preset_hub_page)
        self._add_nav_button(nav_items_layout, "Progression", self._progression_page)
        self._add_nav_button(nav_items_layout, "Items / Materials", self._items_page)
        self._add_nav_button(nav_items_layout, "Sigils", self._sigils_page)
        self._add_nav_button(nav_items_layout, "Weapons", self._weapons_page)
        self._add_nav_button(nav_items_layout, "Characters", self._characters_page)
        self._add_nav_button(nav_items_layout, "Mastery", self._mastery_mods_page)
        self._add_nav_button(nav_items_layout, "Save Health", self._save_health_page)
        self._add_nav_button(nav_items_layout, "About", self._about_page)
        nav_items_layout.addStretch(1)

        self.status_label = QLabel("No save loaded")
        self.status_label.setObjectName("navStatus")
        self.status_label.setWordWrap(True)
        nav_layout.addWidget(self.status_label)

        self._set_advanced_visible(False, persist=False)
        root_layout.addWidget(self.nav)
        root_layout.addWidget(self.stack, 1)
        self.setCentralWidget(root)
        self._install_common_numeric_validators()
        self._show_page("Welcome")

    def _overview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)

        header = QLabel("Welcome")
        header.setObjectName("pageHeader")
        layout.addWidget(header)

        intro = QLabel("Select a base folder and the editor will search every subfolder for saves named GameData or SaveData1.")
        intro.setWordWrap(True)
        intro.setObjectName("helpText")
        layout.addWidget(intro)

        open_card = make_card("Open Save")
        open_layout = QVBoxLayout(open_card)
        open_layout.setSpacing(10)

        top_row = QHBoxLayout()
        open_btn = QPushButton("Open Save File")
        open_btn.clicked.connect(self.open_save)
        choose_btn = QPushButton("Choose Folder")
        choose_btn.clicked.connect(self.choose_save_finder_base_folder)
        scan_btn = QPushButton("Scan Folder")
        scan_btn.clicked.connect(self.scan_save_finder_base_folder)
        for btn in (open_btn, choose_btn, scan_btn):
            top_row.addWidget(btn)
        top_row.addStretch(1)
        open_layout.addLayout(top_row)

        path_row = QHBoxLayout()
        self.save_finder_base_edit = QLineEdit()
        self.save_finder_base_edit.setPlaceholderText("Base folder to scan recursively...")
        self.save_finder_base_edit.setText(str(getattr(self, "save_finder_base_path", "") or ""))
        self.save_finder_base_edit.returnPressed.connect(self.scan_save_finder_base_folder)
        path_row.addWidget(QLabel("Folder"))
        path_row.addWidget(self.save_finder_base_edit, 1)
        open_layout.addLayout(path_row)

        self.save_finder_status = QLabel("Choose a folder to scan. Subfolders are included automatically. Double-click a save row to open it.")
        self.save_finder_status.setObjectName("subtleText")
        self.save_finder_status.setWordWrap(True)
        open_layout.addWidget(self.save_finder_status)

        self.save_finder_table = QTableView()
        self.save_finder_table.setModel(self.save_finder_model)
        self._table_clean(self.save_finder_table)
        self.save_finder_table.setMinimumHeight(420)
        self.save_finder_table.doubleClicked.connect(lambda *_: self.open_selected_found_save())
        open_layout.addWidget(self.save_finder_table, 1)

        action_row = QHBoxLayout()
        open_selected_btn = QPushButton("Open Selected")
        open_selected_btn.clicked.connect(self.open_selected_found_save)
        rescan_btn = QPushButton("Rescan")
        rescan_btn.clicked.connect(self.scan_save_finder_base_folder)
        action_row.addWidget(open_selected_btn)
        action_row.addWidget(rescan_btn)
        action_row.addStretch(1)
        open_layout.addLayout(action_row)

        layout.addWidget(open_card, 1)
        return page


    def choose_save_finder_base_folder(self) -> None:
        start = str(getattr(self, "save_finder_base_path", "") or Path.home())
        folder = QFileDialog.getExistingDirectory(self, "Choose folder to scan for GBFR saves", start)
        if not folder:
            return
        self.save_finder_base_path = folder
        if hasattr(self, "save_finder_base_edit"):
            self.save_finder_base_edit.setText(folder)
        self._save_ui_settings()
        self.scan_save_finder_base_folder()

    def _is_gbfr_save_candidate_name(self, name: str) -> bool:
        n = str(name or "").strip()
        lower = n.lower()
        # PC saves normally appear as GameData. Some copied/resigned/backed-up
        # slot saves use SaveData1. Accept extension/backups too so copied files
        # still show up, but keep the scan focused on real save names.
        return lower.startswith("gamedata") or lower.startswith("savedata1")

    def scan_save_finder_base_folder(self) -> None:
        edit = getattr(self, "save_finder_base_edit", None)
        base_text = edit.text().strip() if edit is not None else str(getattr(self, "save_finder_base_path", "") or "")
        if not base_text:
            self.statusBar().showMessage("Choose a base folder first.", 3500)
            return
        base = Path(base_text).expanduser()
        if not base.exists() or not base.is_dir():
            self.statusBar().showMessage("That base folder does not exist.", 4500)
            return
        self.save_finder_base_path = str(base)
        self._save_ui_settings()

        rows: List[List[Any]] = []
        meta: List[Dict[str, Any]] = []
        checked = 0
        max_hits = 2000
        try:
            for root, dirs, files in os.walk(base):
                # Skip common giant/non-save folders for responsiveness.
                dirs[:] = [
                    d for d in dirs
                    if d.lower() not in {"__pycache__", ".git", "node_modules", "$recycle.bin", "windows", "program files", "program files (x86)"}
                ]
                for filename in files:
                    checked += 1
                    if not self._is_gbfr_save_candidate_name(filename):
                        continue
                    path = Path(root) / filename
                    try:
                        stat = path.stat()
                        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
                        kind = "GameData" if filename.lower().startswith("gamedata") else "SaveData1"
                        rows.append([filename, kind, str(path.parent), modified])
                        meta.append({"path": str(path), "name": filename, "kind": kind, "modified": modified})
                        if len(rows) >= max_hits:
                            break
                    except Exception:
                        continue
                if len(rows) >= max_hits:
                    break
        except Exception as exc:
            QMessageBox.warning(self, "Scan failed", str(exc))
            return

        self.save_finder_rows_meta = meta
        self.save_finder_model.set_rows(rows)
        if hasattr(self, "save_finder_table"):
            self._set_table_widths(self.save_finder_table, {0: 160, 1: 110, 2: 720, 3: 160})
        msg = f"Found {len(rows):,} save candidate(s) under {base} and its subfolders."
        if len(rows) >= max_hits:
            msg += f" Stopped at {max_hits:,} results."
        if hasattr(self, "save_finder_status"):
            self.save_finder_status.setText(msg)
        self.statusBar().showMessage(msg, 5000)

    def open_selected_found_save(self) -> None:
        table = getattr(self, "save_finder_table", None)
        if table is None:
            return
        idx = table.currentIndex()
        if not idx.isValid() or idx.row() >= len(getattr(self, "save_finder_rows_meta", [])):
            self.statusBar().showMessage("Select a save from the list first.", 3000)
            return
        path = self.save_finder_rows_meta[idx.row()].get("path")
        if not path:
            return
        self._open_save_path(str(path))


    def _save_health_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(14)
        header = QLabel("Save Health")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("A clean checklist for whether the opened save is safe to edit, how many reusable empty slots are available, and what still needs research.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        card = make_card("Current save status")
        card_layout = QVBoxLayout(card)
        self.save_health_text = QPlainTextEdit()
        self.save_health_text.setObjectName("summaryBox")
        self.save_health_text.setReadOnly(True)
        self.save_health_text.setPlainText("Open a save to run the health checklist.")
        card_layout.addWidget(self.save_health_text)
        layout.addWidget(card, 1)

        row = QHBoxLayout()
        for text, slot in [
            ("Refresh", self.refresh_save_health),
            ("Open Items", lambda: self._show_page("Items / Materials")),
            ("Open Sigils", lambda: self._show_page("Sigils")),
            ("Open Weapons", lambda: self._show_page("Weapons")),
            ("Save As", self.save_as),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page


    def _about_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(10)

        header = QLabel("About")
        header.setObjectName("pageHeader")
        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("aboutScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(10)

        top = make_card("Granblue Fantasy Relink Save Lab")
        top_layout = QGridLayout(top)
        top_layout.setHorizontalSpacing(18)
        top_layout.setVerticalSpacing(6)

        creator = QLabel("<b>Created by ProtoBuffers</b>")
        creator.setTextFormat(Qt.TextFormat.RichText)
        creator.setWordWrap(True)
        top_layout.addWidget(creator, 0, 0, 1, 2)

        support = QLabel("Supports PC saves and decrypted PS4 saves.")
        support.setWordWrap(True)
        support.setObjectName("subtleText")
        top_layout.addWidget(support, 1, 0, 1, 2)

        sheet = QLabel(
            'Community data sheet: '
            '<a href="https://docs.google.com/spreadsheets/d/1mGf987Njg3VodeXp8kVwzEgvYSAeMzkHnkGnAj1_RjY/edit?gid=0#gid=0">'
            "Granblue Fantasy Relink spreadsheet</a>"
        )
        sheet.setTextFormat(Qt.TextFormat.RichText)
        sheet.setOpenExternalLinks(True)
        sheet.setWordWrap(True)
        top_layout.addWidget(sheet, 2, 0, 1, 2)
        layout.addWidget(top)

        credits = make_card("Credits")
        credits_layout = QVBoxLayout(credits)
        credits_text = QLabel(
            "zeraf3000  •  JJDarklight  •  method_dev  •  hywolfe  •  skiller  •  peepeez  •  "
            "di_ciolla  •  tj0816  •  dvymin  •  ceruleandhm  •  anon devs"
        )
        credits_text.setWordWrap(True)
        credits_layout.addWidget(credits_text)
        layout.addWidget(credits)

        features = make_card("Features")
        features_layout = QGridLayout(features)
        features_layout.setHorizontalSpacing(24)
        features_layout.setVerticalSpacing(6)
        feature_items = [
            "PC / PS4 save support",
            "Welcome save finder",
            "Save Health checks",
            "Cheats dashboard",
            "Progression / unlock editing",
            "Items / Materials editor",
            "Sigil editor + database add",
            "Weapon editor + add all missing",
            "Character editor",
            "Mastery / Overmastery editor",
            "32-bit input safety clamps",
            "Save As workflow",
        ]
        for idx, item in enumerate(feature_items):
            label = QLabel(f"• {item}")
            label.setWordWrap(True)
            features_layout.addWidget(label, idx // 2, idx % 2)
        layout.addWidget(features)

        ps4 = make_card("PS4 Save Help")
        ps4_layout = QVBoxLayout(ps4)
        ps4_text = QLabel(
            'Need to decrypt a PS4 save for free? Join the ProtoBuffers Discord: '
            '<a href="https://discord.gg/protobuffers">https://discord.gg/protobuffers</a>'
        )
        ps4_text.setTextFormat(Qt.TextFormat.RichText)
        ps4_text.setOpenExternalLinks(True)
        ps4_text.setWordWrap(True)
        ps4_layout.addWidget(ps4_text)
        layout.addWidget(ps4)

        layout.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        return page


    def _save_map_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)
        header = QLabel("Save Map")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Manager-level map of the save: field IDs, readable names, known/unknown hash coverage, sample units, and research confidence. This is the main page for mapping unknown data without staring at raw rows.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        top = QHBoxLayout()
        self.save_map_filter_edit = QLineEdit()
        self.save_map_filter_edit.setPlaceholderText("Filter by manager, field ID, field name, confidence, note, sample...")
        self.save_map_filter_edit.textChanged.connect(self.refresh_save_map_rows)
        top.addWidget(self.save_map_filter_edit, 1)
        self.save_map_unknown_check = QCheckBox("Research targets only")
        self.save_map_unknown_check.toggled.connect(self.refresh_save_map_rows)
        top.addWidget(self.save_map_unknown_check)
        layout.addLayout(top)

        self.save_map_table = QTableView()
        self.save_map_table.setModel(self.save_map_model)
        self.save_map_table.setAlternatingRowColors(True)
        self.save_map_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.save_map_table.setSortingEnabled(True)
        layout.addWidget(self.save_map_table, 1)

        detail = make_card("Map Summary")
        detail_layout = QVBoxLayout(detail)
        self.save_map_summary = QPlainTextEdit()
        self.save_map_summary.setObjectName("summaryBox")
        self.save_map_summary.setReadOnly(True)
        self.save_map_summary.setMaximumHeight(170)
        self.save_map_summary.setPlainText("Open a save to build the manager/field map.")
        detail_layout.addWidget(self.save_map_summary)
        layout.addWidget(detail)

        row = QHBoxLayout()
        for text, slot in [
            ("Refresh Map", self.refresh_save_map_rows),
            ("Export Map CSV", lambda: self.export_save_map("csv", False)),
            ("Export Map JSON", lambda: self.export_save_map("json", False)),
            ("Export Research Targets CSV", lambda: self.export_save_map("csv", True)),
            ("Open Raw Units", lambda: self._show_page("Units")),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _id_cleanup_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)
        header = QLabel("ID Cleanup")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Audits every hash-like ID in the loaded save and separates known GBIDs, generated candidates, empty values, and unresolved IDs. Use this page to hunt missing IDs without mixing them into the normal edit tabs.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        top = QHBoxLayout()
        self.id_audit_filter_edit = QLineEdit()
        self.id_audit_filter_edit.setPlaceholderText("Filter by manager, hash, status, name, source...")
        self.id_audit_filter_edit.textChanged.connect(self.refresh_id_audit_rows)
        top.addWidget(self.id_audit_filter_edit, 1)
        self.id_audit_unresolved_check = QCheckBox("Unresolved / candidates only")
        self.id_audit_unresolved_check.toggled.connect(self.refresh_id_audit_rows)
        top.addWidget(self.id_audit_unresolved_check)
        self.id_audit_empty_check = QCheckBox("Include empty hashes")
        self.id_audit_empty_check.toggled.connect(self.refresh_id_audit_rows)
        top.addWidget(self.id_audit_empty_check)
        self.id_audit_hide_ability_check = QCheckBox("Hide ability/action noise")
        self.id_audit_hide_ability_check.setChecked(True)
        self.id_audit_hide_ability_check.toggled.connect(self.refresh_id_audit_rows)
        top.addWidget(self.id_audit_hide_ability_check)
        layout.addLayout(top)

        self.id_audit_table = QTableView()
        self.id_audit_table.setModel(self.id_audit_model)
        self.id_audit_table.setAlternatingRowColors(True)
        self.id_audit_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.id_audit_table.setSortingEnabled(True)
        layout.addWidget(self.id_audit_table, 1)

        detail = make_card("Coverage Summary")
        detail_layout = QVBoxLayout(detail)
        self.id_audit_summary = QPlainTextEdit()
        self.id_audit_summary.setReadOnly(True)
        self.id_audit_summary.setMaximumHeight(150)
        self.id_audit_summary.setPlainText("Open a save to audit hash-like IDs.")
        detail_layout.addWidget(self.id_audit_summary)
        layout.addWidget(detail)

        row = QHBoxLayout()
        for text, slot in [
            ("Refresh Audit", self.refresh_id_audit_rows),
            ("Export All CSV", lambda: self.export_id_audit(False)),
            ("Export Missing/Candidates CSV", lambda: self.export_id_audit(True)),
            ("Open Hash Scan", lambda: self._show_page("Hash Scan")),
            ("Open Data Sources", lambda: self._show_page("Data Sources")),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _editor_hub_page(self) -> QWidget:
        """Player-facing basic editor page.

        This is intentionally less technical than the row/catalog pages. It
        collects the common safe actions in one place, then links out to the
        detailed editors only when the user needs row-level control.
        """
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(28, 24, 28, 24)
        page_layout.setSpacing(12)

        header = QLabel("Basic Editor")
        header.setObjectName("pageHeader")
        page_layout.addWidget(header)
        help_text = QLabel(
            "Start here for normal save editing. Use exact-value buttons for quick changes, "
            "then open the row tabs only when you need to inspect or replace a specific item, sigil, weapon, or character."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        page_layout.addWidget(help_text)

        self.basic_workflow_label = QLabel("Open a save to start. The editor will show safe next steps here.")
        self.basic_workflow_label.setObjectName("subtleText")
        self.basic_workflow_label.setWordWrap(True)
        page_layout.addWidget(self.basic_workflow_label)

        self.edit_hub_summary = QLabel("Open a save to see editable slot counts.")
        self.edit_hub_summary.setObjectName("subtleText")
        self.edit_hub_summary.setWordWrap(True)
        page_layout.addWidget(self.edit_hub_summary)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(12)
        scroll.setWidget(content)
        page_layout.addWidget(scroll, 1)

        top_row = QHBoxLayout()
        file_box = make_card("1. Open / Save safely")
        file_layout = QVBoxLayout(file_box)
        file_layout.addWidget(QLabel("Use Save As for the first edited copy. Backup + Save Original is there only after you trust the edit."))
        self._add_action_grid(file_layout, [
            ("Open Save", self.open_save),
            ("Save As Edited Copy", self.save_as),
            ("Backup + Save Original", self.save_original),
            ("Open Save Health", lambda: self._show_page("Save Health")),
        ], columns=2, primary_first=True)
        top_row.addWidget(file_box, 1)

        exact_box = make_card("2. Exact value quick edits")
        exact_layout = QVBoxLayout(exact_box)
        exact_layout.addWidget(QLabel("Patch known existing rows to a value you choose. These do not create new rows."))
        self._add_action_grid(exact_layout, [
            ("Set Known Items To...", self.cheat_set_known_item_quantities_custom),
            ("Set Sigil Levels To...", self.cheat_set_known_sigil_levels_custom),
            ("Set Weapon XP To...", self.cheat_set_known_weapon_xp_custom),
            ("Set Character Levels To...", self.cheat_set_character_levels_custom),
        ], columns=2)
        top_row.addWidget(exact_box, 1)
        layout.addLayout(top_row)

        middle_row = QHBoxLayout()
        row_edit_box = make_card("3. Row editors")
        row_edit_layout = QVBoxLayout(row_edit_box)
        row_edit_layout.addWidget(QLabel("Open a specific tab when you want to select one row, search/filter, or replace a hash."))
        self._add_action_grid(row_edit_layout, [
            ("Items / Materials", lambda: self._show_page("Items / Materials")),
            ("Sigils", lambda: self._show_page("Sigils")),
            ("Weapons", lambda: self._show_page("Weapons")),
            ("Characters", lambda: self._show_page("Characters")),
            ("Progression", lambda: self._show_page("Progression")),
        ], columns=2)
        middle_row.addWidget(row_edit_box, 1)

        add_box = make_card("4. Add packs / empty-slot tools")
        add_layout = QVBoxLayout(add_box)
        add_layout.addWidget(QLabel("These add into reusable empty slots. They do not resize/rebuild the save yet."))
        self._add_action_grid(add_layout, [
            ("Open Cheats", lambda: self._show_page("Cheats")),
            ("Add Item to Empty Slot", self.add_item_to_empty_slot),
            ("Add Sigil to Empty Slot", self.add_sigil_to_empty_slot),
            ("Add Weapon to Empty Slot", self.add_weapon_to_empty_slot),
            ("Repair Unsafe Inventory Rows", self.repair_unsafe_material_add_all_rows),
            ("Add All Known V/V+ Sigils", self.cheat_add_all_known_v_sigils),
        ], columns=2)
        middle_row.addWidget(add_box, 1)
        layout.addLayout(middle_row)

        bottom_row = QHBoxLayout()
        max_box = make_card("Optional max cheats")
        max_layout = QVBoxLayout(max_box)
        max_layout.addWidget(QLabel("Fast bulk edits for known rows already present in your save."))
        self._add_action_grid(max_layout, [
            ("Max Known Item Quantities", self.cheat_max_known_item_quantities),
            ("Max Sigil Levels + Lock", self.cheat_max_sigil_levels_and_locks),
            ("Max Weapon XP + Flags", self.cheat_max_weapon_xp_and_flags),
            ("Max Character Levels", self.cheat_max_character_levels),
        ], columns=2)
        bottom_row.addWidget(max_box, 1)

        cleanup_box = make_card("Cleanup / research only when needed")
        cleanup_layout = QVBoxLayout(cleanup_box)
        cleanup_layout.addWidget(QLabel("Use these when names are missing or you need to map new IDs."))
        self._add_action_grid(cleanup_layout, [
            ("Show Unknown Sigils", self.show_unknown_sigils),
            ("Copy Unknown Sigil Hashes", self.copy_visible_unknown_sigil_hashes),
            ("Open Sigil Catalog", lambda: self._show_page("Sigil/Gem ID Catalog")),
            ("Open ID Cleanup", lambda: self._show_page("ID Cleanup")),
        ], columns=2)
        bottom_row.addWidget(cleanup_box, 1)
        layout.addLayout(bottom_row)

        self.basic_safety_label = QLabel("Safety status will appear after opening a save.")
        self.basic_safety_label.setObjectName("subtleText")
        self.basic_safety_label.setWordWrap(True)
        layout.addWidget(self.basic_safety_label)
        layout.addStretch(1)
        return page


    def _cheat_preset_hub_page(self) -> QWidget:
        """Button-only cheat dashboard.

        Preset/add-pack tables were removed because the mapped actions already
        exist as buttons. The hidden preset machinery is still available to
        mapped actions, but the user workflow is now grouped buttons only.
        """
        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(28, 24, 28, 24)
        page_layout.setSpacing(10)

        header = QLabel("Cheats")
        header.setObjectName("pageHeader")
        page_layout.addWidget(header)

        summary = QLabel(
            "Button-only cheat dashboard. Actions patch known save fields or existing empty slots; "
            "use Save As for the first edited copy. Preset/add-pack tables were removed."
        )
        summary.setWordWrap(True)
        summary.setObjectName("subtleText")
        page_layout.addWidget(summary)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(12)
        scroll.setWidget(content)
        page_layout.addWidget(scroll, 1)

        def mapped(key: str):
            return lambda _=False, k=key: self.apply_save_wizard_cheat(get_builtin_save_wizard_cheat(k))

        def add_box(title: str, help_text: str, actions: list[tuple[str, Any]], columns: int = 4, primary_first: bool = False) -> None:
            box = make_card(title)
            box_layout = QVBoxLayout(box)
            box_layout.setSpacing(8)
            label = QLabel(help_text)
            label.setWordWrap(True)
            label.setObjectName("subtleText")
            box_layout.addWidget(label)
            self._add_action_grid(box_layout, actions, columns=columns, primary_first=primary_first)
            layout.addWidget(box)

        add_box(
            "Quick max existing rows",
            "Fast cheats for rows already present in the loaded save. These do not add new FlatBuffer rows.",
            [
                ("Max Items / Currency", self.cheat_max_known_item_quantities),
                ("Max Sigils + Lock", self.cheat_max_sigil_levels_and_locks),
                ("Max Weapons 999,999,999", self.cheat_max_weapon_xp_and_flags),
                ("Max Characters 999,999,999", self.cheat_max_character_levels),
            ],
            columns=4,
            primary_first=True,
        )

        add_box(
            "Exact value tools",
            "Use these when you want a typed value instead of a max value. These now skip extra confirmation popups.",
            [
                ("Set Items To...", self.cheat_set_known_item_quantities_custom),
                ("Set Sigil Levels To...", self.cheat_set_known_sigil_levels_custom),
                ("Set Weapon XP To...", self.cheat_set_known_weapon_xp_custom),
                ("Set Character Values To...", self.cheat_set_character_levels_custom),
            ],
            columns=4,
        )

        add_box(
            "Add / repair existing slots",
            "Adds into existing empty verified slots only, or repairs older unsafe add-all edits.",
            [
                ("Safe Add Missing Materials", mapped("sw-add-all-known-materials")),
                ("Add All Known V/V+ Sigils", mapped("sw-add-all-known-v-sigils")),
                ("Add Basic V Sigils", mapped("sw-add-basic-v-sigils")),
                ("Add Meta Sigil Core", mapped("sw-add-meta-sigils")),
                ("Add Survival Sigils", mapped("sw-add-survival-sigils")),
                ("Add Captain Weapons", mapped("sw-add-captain-weapons")),
                ("Repair Unsafe Inventory", mapped("sw-repair-unsafe-material-addall")),
            ],
            columns=3,
        )

        add_box(
            "Mastery / Overmastery lab",
            "Direct Save-Wizard-style mastery actions from the cleaned FF460600/FF470600 mapping. Save As before testing.",
            [
                ("Normal Mastery 512", lambda _=False: self.apply_mastery_sw_normal_value_sweep(0x200)),
                ("Normal Mastery 1016", lambda _=False: self.apply_mastery_sw_normal_value_sweep(0x3F8)),
                ("Normal Mastery 1023", lambda _=False: self.apply_mastery_sw_normal_value_sweep(0x3FF)),
                ("Normal All Attack Power", lambda _=False: self.apply_mastery_sw_normal_effect_sweep(0xC4925BD7, "Attack Power Up")),
                ("Normal All Critical Rate", lambda _=False: self.apply_mastery_sw_normal_effect_sweep(0x6757C645, "Critical Rate")),
                ("Overmastery 20%", lambda _=False: self.apply_mastery_sw_overmastery_value_sweep(0x200)),
                ("Overmastery 80%", lambda _=False: self.apply_mastery_sw_overmastery_value_sweep(0xFFFFFFFF)),
                ("Open Mastery Editor", lambda _=False: self._show_page("Mastery")),
            ],
            columns=4,
        )

        add_box(
            "Progression / unlocks",
            "Mapped progression actions only. The editor skips catalog-only rows and writes rows that exist in the loaded save vectors.",
            [
                ("Complete All Mapped", mapped("sw-complete-mapped-progression")),
                ("Complete Main Story", mapped("sw-complete-main-story")),
                ("Complete Side / Challenge", mapped("sw-complete-side-quests")),
                ("Complete Fate Episodes", mapped("sw-complete-fate-episodes")),
                ("Complete Multiplayer", mapped("sw-complete-multiplayer-quests")),
                ("Complete Town / Lobby", mapped("sw-complete-town-lobby-misc")),
                ("Unlock Title / Archive", mapped("sw-unlock-title-archive-candidates")),
            ],
            columns=3,
        )

        add_box(
            "Open focused editors",
            "Jump straight to the page that has the detailed table/editor for that save area.",
            [
                ("Items / Materials", lambda _=False: self._show_page("Items / Materials")),
                ("Sigils", lambda _=False: self._show_page("Sigils")),
                ("Weapons", lambda _=False: self._show_page("Weapons")),
                ("Characters", lambda _=False: self._show_page("Characters")),
                ("Progression", lambda _=False: self._show_page("Progression")),
                ("Mastery", lambda _=False: self._show_page("Mastery")),
                ("Save Health", lambda _=False: self._show_page("Save Health")),
                ("Save As", self.save_as),
            ],
            columns=4,
        )

        notes = make_card("Notes")
        notes_layout = QVBoxLayout(notes)
        notes_label = QLabel(
            "Sigil equipment writes are still guarded until we map the real equip relation. "
            "Add actions reuse empty existing slots only. Max buttons update the status bar instead of opening completion dialogs."
        )
        notes_label.setWordWrap(True)
        notes_label.setObjectName("subtleText")
        notes_layout.addWidget(notes_label)
        layout.addWidget(notes)

        layout.addStretch(1)
        return page


    def _cheats_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)
        header = QLabel("Cheats")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Curated cheat actions for common edits. Cheat packs still reuse empty existing slots; they do not resize the save yet. Use Save As first and verify in-game.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        quick = make_card("One-click max cheats for the currently loaded save")
        quick_layout = QVBoxLayout(quick)
        quick_layout.addWidget(QLabel("These patch existing known rows already present in the save. They do not add new rows."))
        quick_row = QHBoxLayout()
        for text, slot in [
            ("Max Known Item Quantities", self.cheat_max_known_item_quantities),
            ("Max Sigil Levels + Lock", self.cheat_max_sigil_levels_and_locks),
            ("Max Weapon XP + Flags", self.cheat_max_weapon_xp_and_flags),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        quick_layout.addLayout(quick_row)
        layout.addWidget(quick)

        custom = make_card("Specific value cheats")
        custom_layout = QVBoxLayout(custom)
        custom_layout.addWidget(QLabel("Use these when you want an exact value instead of a max value. They only patch known existing rows already present in the save."))
        custom_row = QHBoxLayout()
        for text, slot in [
            ("Set Known Items To...", self.cheat_set_known_item_quantities_custom),
            ("Set Sigil Levels To...", self.cheat_set_known_sigil_levels_custom),
            ("Set Weapon XP To...", self.cheat_set_known_weapon_xp_custom),
            ("Set Character Levels To...", self.cheat_set_character_levels_custom),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            custom_row.addWidget(btn)
        custom_row.addStretch(1)
        custom_layout.addLayout(custom_row)
        layout.addWidget(custom)

        packs = make_card("Cheat packs that add into empty slots")
        packs_layout = QVBoxLayout(packs)
        for key in [
            "cheat-max-currency-mastery",
            "cheat-upgrade-material-cache",
            "cheat-meta-sigil-core",
            "cheat-survival-sigil-stack",
            "cheat-captain-weapon-pack",
        ]:
            try:
                pack = get_preset_pack(key)
            except Exception:
                continue
            row = QHBoxLayout()
            text = QLabel(f"<b>{pack.name}</b><br><span style='color:#93a4b8'>{pack.description}<br>Rows: {pack.total_rows} · Items {len(pack.items)} · Sigils {len(pack.sigils)} · Weapons {len(pack.weapons)}</span>")
            text.setWordWrap(True)
            row.addWidget(text, 1)
            apply_btn = QPushButton("Apply")
            apply_btn.clicked.connect(lambda _=False, p=pack: self.apply_preset_pack(p))
            row.addWidget(apply_btn)
            copy_btn = QPushButton("Copy Text")
            copy_btn.clicked.connect(lambda _=False, p=pack: QApplication.clipboard().setText(p.to_batch_text()))
            row.addWidget(copy_btn)
            packs_layout.addLayout(row)
        layout.addWidget(packs)

        notes = make_card("Safety notes")
        notes_layout = QVBoxLayout(notes)
        notes_label = QLabel("If a cheat pack needs more empty slots than your save exposes, it will stop before applying. Curated cheat packs require known database matches, so they should not create Unknown hash rows. Manual add/batch tools still allow raw hashes for research.")
        notes_label.setWordWrap(True)
        notes_layout.addWidget(notes_label)
        layout.addWidget(notes)
        layout.addStretch(1)
        return page


    def _save_wizard_cheats_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)
        header = QLabel("Save Wizard Cheat Runner")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "This tab is now action-focused. Preset Packs are reusable add-loadouts; "
            "Save Wizard is for cheat-style save edits and sheet-code research. "
            "Mapped cheats run safe editor-native logic. Imported sheet rows stay reference-only until we map them."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        quick = make_card("Mapped one-click Save Wizard style cheats")
        quick_layout = QVBoxLayout(quick)

        def add_section(title: str, actions: list[tuple[str, str]]):
            label = QLabel(f"<b>{title}</b>")
            label.setObjectName("subtleText")
            quick_layout.addWidget(label)
            row = QHBoxLayout()
            for text, key in actions:
                btn = QPushButton(text)
                btn.clicked.connect(lambda _=False, k=key: self.apply_save_wizard_cheat(get_builtin_save_wizard_cheat(k)))
                row.addWidget(btn)
            row.addStretch(1)
            quick_layout.addLayout(row)

        add_section("Inventory / Currency", [
            ("Max Existing Items", "sw-max-current-items"),
            ("Safe Add Missing Items", "sw-add-all-known-materials"),
            ("Max Rupies + Mastery", "sw-max-currency-mastery"),
            ("Repair Unsafe Inventory", "sw-repair-unsafe-material-addall"),
        ])
        add_section("Sigils", [
            ("Max Existing Sigils + Lock", "sw-max-current-sigils"),
            ("Add All Known V/V+", "sw-add-all-known-v-sigils"),
            ("Add Basic V Set", "sw-add-basic-v-sigils"),
            ("Add Meta Core", "sw-add-meta-sigils"),
        ])
        add_section("Characters / Weapons", [
            ("Max Character Levels", "sw-max-current-characters"),
            ("Max Existing Weapons", "sw-max-current-weapons"),
            ("Add Captain Weapons", "sw-add-captain-weapons"),
        ])
        add_section("Progression", [
            ("Complete All Mapped", "sw-complete-mapped-progression"),
            ("Complete Main Story", "sw-complete-main-story"),
            ("Complete Side Quests", "sw-complete-side-quests"),
            ("Complete Fate Episodes", "sw-complete-fate-episodes"),
            ("Complete Multiplayer", "sw-complete-multiplayer-quests"),
            ("Complete Town/Lobby", "sw-complete-town-lobby-misc"),
            ("Unlock Title / Archive Candidates", "sw-unlock-title-archive-candidates"),
        ])
        custom_label = QLabel("<b>Specific Values</b>")
        custom_label.setObjectName("subtleText")
        quick_layout.addWidget(custom_label)
        custom_row = QHBoxLayout()
        for text, slot in [
            ("Set Known Items To...", self.cheat_set_known_item_quantities_custom),
            ("Set Sigils To...", self.cheat_set_known_sigil_levels_custom),
            ("Set Weapons To...", self.cheat_set_known_weapon_xp_custom),
            ("Set Characters To...", self.cheat_set_character_levels_custom),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            custom_row.addWidget(btn)
        custom_row.addStretch(1)
        quick_layout.addLayout(custom_row)
        layout.addWidget(quick)

        sheet_box = make_card("Imported Save Wizard sheet rows / research")
        sheet_layout = QVBoxLayout(sheet_box)
        sheet_help = QLabel(
            "Load the community sheet here to compare raw Save Wizard entries against our editor actions. "
            "These rows are not applied blindly; they are a to-do list for mapping more safe cheats."
        )
        sheet_help.setWordWrap(True)
        sheet_help.setObjectName("subtleText")
        sheet_layout.addWidget(sheet_help)
        top = QHBoxLayout()
        self.save_wizard_filter_edit = QLineEdit()
        self.save_wizard_filter_edit.setPlaceholderText("Filter imported/reference sheet rows...")
        self.save_wizard_filter_edit.textChanged.connect(lambda _: self.refresh_save_wizard_rows())
        top.addWidget(self.save_wizard_filter_edit, 1)
        load_btn = QPushButton("Load Sheet Tab")
        load_btn.clicked.connect(self.load_save_wizard_sheet_tab)
        top.addWidget(load_btn)
        export_btn = QPushButton("Export Mapped + Imported List")
        export_btn.clicked.connect(self.export_save_wizard_cheats_csv)
        top.addWidget(export_btn)
        sheet_layout.addLayout(top)

        self.save_wizard_table = QTableView()
        self.save_wizard_table.setModel(self.save_wizard_model)
        self._table_clean(self.save_wizard_table, hidden_columns=(5,))
        self.save_wizard_table.selectionModel().selectionChanged.connect(lambda *_: self.update_save_wizard_detail())
        self.save_wizard_table.doubleClicked.connect(lambda _: self.apply_selected_save_wizard_cheat())
        sheet_layout.addWidget(self.save_wizard_table, 1)

        detail = make_card("Selected Sheet Reference")
        detail_layout = QVBoxLayout(detail)
        self.save_wizard_detail_label = QLabel("Load or select an imported sheet row to preview it. Mapped cheat buttons are above.")
        self.save_wizard_detail_label.setWordWrap(True)
        self.save_wizard_detail_label.setObjectName("subtleText")
        detail_layout.addWidget(self.save_wizard_detail_label)
        sheet_layout.addWidget(detail)
        layout.addWidget(sheet_box, 1)

        row = QHBoxLayout()
        for text, slot in [
            ("Open Presets", lambda: self._show_page("Cheats")),
            ("Open Cheats", lambda: self._show_page("Cheats")),
            ("Open Progression", lambda: self._show_page("Progression")),
            ("Save As", self.save_as),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _progression_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        header_row = QHBoxLayout()
        header = QLabel("Progression / Unlocks")
        header.setObjectName("pageHeader")
        header_row.addWidget(header)
        header_row.addStretch(1)
        save_btn = QPushButton("Save As")
        save_btn.setMinimumHeight(36)
        save_btn.clicked.connect(self.save_as)
        header_row.addWidget(save_btn)
        layout.addLayout(header_row)

        help_text = QLabel(
            "Clean quest/progression editor. Pick a top tab, search the visible mapped rows, then complete mapped rows or edit the selected row. "
            "Catalog-only rows are hidden from this page until their save vector is mapped."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        # Hidden backing combo used by existing progression helpers.
        self.progression_quest_group_combo = QComboBox(page)
        self.progression_quest_group_combo.addItem("All Progression", "")
        groups = [
            ("1", "Main Quest"),
            ("2", "Challenges / Side Quests"),
            ("3", "Fate Episodes"),
            ("4", "Multiplayer / Quest Counter"),
            ("5", "Town / Lobby"),
            ("6", "Dummy / Practice"),
            ("7", "Short Story / Misc"),
        ]
        for prefix, label in groups:
            self.progression_quest_group_combo.addItem(label, prefix)
        self.progression_quest_group_combo.currentIndexChanged.connect(lambda *_: self.refresh_progression_editor_rows())
        self.progression_quest_group_combo.setVisible(False)

        self.progression_group_tabs = QTabBar()
        self.progression_group_tabs.setObjectName("progressionTopTabs")
        self.progression_group_tabs.setExpanding(False)
        self.progression_group_tabs.setDrawBase(False)
        # Visible mission tabs only. Town/Lobby and Dummy/Practice stay hidden
        # from the normal workflow because they are not useful user-facing mission groups.
        self.progression_group_tab_prefixes = ["", "1", "2", "3", "4", "7"]
        self.progression_group_tab_base_labels = {
            "": "All",
            "1": "Main Quest",
            "2": "Challenges / Side Quests",
            "3": "Fate Episodes",
            "4": "Multiplayer",
            "7": "Short Story / Misc",
        }
        for prefix in self.progression_group_tab_prefixes:
            self.progression_group_tabs.addTab(self.progression_group_tab_base_labels[prefix])
            self.progression_group_tabs.setTabData(self.progression_group_tabs.count() - 1, prefix)
        self.progression_group_tabs.currentChanged.connect(lambda idx: self.set_progression_group_filter(self.progression_group_tabs.tabData(idx) or ""))
        layout.addWidget(self.progression_group_tabs)

        main_box = make_card("Quest / Progression Rows")
        main_layout = QVBoxLayout(main_box)
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)

        title_row = QHBoxLayout()
        self.progression_editor_title = QLabel("All Progression")
        self.progression_editor_title.setObjectName("sectionHeader")
        title_row.addWidget(self.progression_editor_title)
        title_row.addStretch(1)
        complete_visible = QPushButton("Complete Current Tab")
        complete_visible.setMinimumHeight(36)
        complete_visible.clicked.connect(self.complete_progression_visible_group)
        title_row.addWidget(complete_visible)
        complete_all = QPushButton("Complete All Mapped")
        complete_all.setMinimumHeight(36)
        complete_all.clicked.connect(lambda _=False: self.cheat_complete_progression_group("", "Complete All Mapped Progression"))
        title_row.addWidget(complete_all)
        main_layout.addLayout(title_row)

        self.progression_editor_status = QLabel("Open a save to edit progression.")
        self.progression_editor_status.setObjectName("helpText")
        self.progression_editor_status.setWordWrap(True)
        main_layout.addWidget(self.progression_editor_status)

        tools_row = QHBoxLayout()
        self.progression_editor_search_edit = QLineEdit()
        self.progression_editor_search_edit.setPlaceholderText("Search quest/stage name, ID, status, rank, or mapped state...")
        self.progression_editor_search_edit.textChanged.connect(lambda *_: self.schedule_progression_editor_refresh())
        tools_row.addWidget(self.progression_editor_search_edit, 3)
        self.progression_done_filter_combo = QComboBox()
        self.progression_done_filter_combo.addItems(["All mapped rows", "Completed only", "Incomplete only"])
        self.progression_done_filter_combo.currentTextChanged.connect(lambda *_: self.schedule_progression_editor_refresh())
        tools_row.addWidget(self.progression_done_filter_combo, 0)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setMinimumHeight(36)
        refresh_btn.clicked.connect(self.refresh_progression_rows)
        tools_row.addWidget(refresh_btn)
        main_layout.addLayout(tools_row)

        self.progression_edit_table = QTableView()
        self.progression_edit_table.setModel(self.progression_edit_model)
        self._table_clean(self.progression_edit_table)
        self.progression_edit_table.verticalHeader().setDefaultSectionSize(32)
        self.progression_edit_table.verticalHeader().setMinimumSectionSize(30)
        self.progression_edit_table.setMinimumHeight(470)
        self.progression_edit_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.progression_edit_table.selectionModel().selectionChanged.connect(lambda *_: self.update_progression_edit_controls())
        main_layout.addWidget(self.progression_edit_table, 1)

        selected_box = make_card("Selected Row")
        selected_layout = QGridLayout(selected_box)
        selected_layout.setContentsMargins(14, 18, 14, 14)
        selected_layout.setHorizontalSpacing(10)
        selected_layout.setVerticalSpacing(8)
        self.progression_selected_summary = QLabel("Select a row to edit common progression values.")
        self.progression_selected_summary.setObjectName("helpText")
        self.progression_selected_summary.setWordWrap(True)
        selected_layout.addWidget(self.progression_selected_summary, 0, 0, 1, 8)
        self.progression_status_spin = QSpinBox()
        self.progression_status_spin.setObjectName("progressionStatusSpin")
        self.progression_status_spin.setRange(0, 999)
        self.progression_status_spin.setValue(1)
        self.progression_status_spin.setMinimumHeight(36)
        self.progression_status_spin.setMinimumWidth(120)
        self.progression_rank_spin = QSpinBox()
        self.progression_rank_spin.setObjectName("progressionRankSpin")
        self.progression_rank_spin.setRange(0, 9)
        self.progression_rank_spin.setValue(7)
        self.progression_rank_spin.setMinimumHeight(36)
        self.progression_rank_spin.setMinimumWidth(120)
        self.progression_completed_check = QCheckBox("Completed / Viewed")
        self.progression_completed_check.setChecked(True)
        self.progression_status_spin.valueChanged.connect(lambda *_: self.apply_progression_realtime_from_controls())
        self.progression_rank_spin.valueChanged.connect(lambda *_: self.apply_progression_realtime_from_controls())
        self.progression_completed_check.stateChanged.connect(lambda *_: self.apply_progression_realtime_from_controls())
        apply_selected = QPushButton("Apply Now")
        apply_selected.setMinimumHeight(36)
        apply_selected.clicked.connect(self.apply_progression_selected_edit)
        complete_selected = QPushButton("Complete Selected")
        complete_selected.setMinimumHeight(36)
        complete_selected.clicked.connect(self.complete_progression_selected_row)
        selected_layout.addWidget(QLabel("Status"), 1, 0)
        selected_layout.addWidget(self.progression_status_spin, 1, 1)
        selected_layout.addWidget(QLabel("Rank"), 1, 2)
        selected_layout.addWidget(self.progression_rank_spin, 1, 3)
        selected_layout.addWidget(self.progression_completed_check, 1, 4)
        selected_layout.addWidget(apply_selected, 1, 5)
        selected_layout.addWidget(complete_selected, 1, 6)
        main_layout.addWidget(selected_box, 0)

        layout.addWidget(main_box, 1)

        # Hidden compatibility widgets for old raw helper methods.
        hidden = QWidget(page)
        hidden.hide()
        self.progression_detail_combo = QComboBox(hidden)
        self.progression_detail_combo.addItem("Overview")
        self.progression_detail_text = QPlainTextEdit(hidden)
        self.progression_table = QTableView(hidden)
        self.progression_table.setModel(self.progression_model)
        self.progression_raw_group = make_card("Raw Field Rows")
        self.progression_raw_group.hide()
        self.progression_raw_group.setCheckable(True)
        self.progression_raw_group.setChecked(False)
        self.progression_row_filter_edit = QLineEdit(hidden)
        self.progression_field_filter_combo = QComboBox(hidden)
        self.progression_unit_filter_edit = QLineEdit(hidden)
        self.progression_nonzero_only_check = QCheckBox(hidden)
        self.progression_nonzero_only_check.setChecked(True)
        self.progression_expand_values_check = QCheckBox(hidden)
        self.progression_value_mode_combo = QComboBox(hidden)
        self.progression_value_mode_combo.addItems(["Any values", "Has non-zero", "All zero/empty", "Known/named units", "Unknown/unnamed units"])
        self.progression_max_rows_combo = QComboBox(hidden)
        self.progression_max_rows_combo.addItems(["250 rows", "500 rows", "1000 rows", "2500 rows", "All rows"])
        self.progression_max_rows_combo.setCurrentText("250 rows")
        self.progression_filter_status = QLabel("Raw rows hidden.", hidden)
        self.progression_rows_table = QTableView(hidden)
        self.progression_rows_table.setModel(self.progression_rows_model)

        return page


    def _units_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("All Save Units")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by kind, field ID, unit ID, unit name, or known name...  e.g. Gran, Rukalsa, Damage Cap, 2703")
        self.filter_edit.textChanged.connect(self.unit_model.set_filter)
        layout.addWidget(self.filter_edit)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.unit_table = QTableView()
        self.unit_table.setModel(self.unit_model)
        self._table_clean(self.unit_table, hidden_columns=(0, 1))
        self.unit_table.selectionModel().selectionChanged.connect(self.unit_selected)
        self.unit_table.doubleClicked.connect(lambda _: self.copy_selected_values_to_editor())
        splitter.addWidget(self.unit_table)

        edit_box = make_card("Selected Unit Editor")
        edit_layout = QVBoxLayout(edit_box)
        self.selected_label = QLabel("Select a row to edit existing values.")
        self.selected_label.setWordWrap(True)
        edit_layout.addWidget(self.selected_label)
        self.value_edit = QPlainTextEdit()
        self.value_edit.setPlaceholderText("Comma-separated values. The count must stay the same; this editor does not insert/delete FlatBuffer entries yet.")
        edit_layout.addWidget(self.value_edit, 1)
        btn_row = QHBoxLayout()
        copy_btn = QPushButton("Load Values")
        copy_btn.clicked.connect(self.copy_selected_values_to_editor)
        apply_btn = QPushButton("Apply Values")
        apply_btn.clicked.connect(self.apply_selected_values)
        btn_row.addWidget(copy_btn)
        btn_row.addWidget(apply_btn)
        btn_row.addStretch(1)
        edit_layout.addLayout(btn_row)
        splitter.addWidget(edit_box)
        splitter.setSizes([900, 360])
        layout.addWidget(splitter, 1)
        return page

    def _unit_map_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Named Unit Map")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("This page converts numeric Unit IDs into readable save slots by using manager ranges plus hashes found inside the save: characters, items/materials, sigils, weapons, abilities, quests, and fallback slot names.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.unit_map_filter_edit = QLineEdit()
        self.unit_map_filter_edit.setPlaceholderText("Filter unit labels by group, name, GBID, hash, or unit id...")
        self.unit_map_filter_edit.textChanged.connect(lambda _: self.refresh_unit_map_rows())
        layout.addWidget(self.unit_map_filter_edit)
        self.unit_map_table = QTableView()
        self.unit_map_table.setModel(self.unit_map_model)
        self._table_clean(self.unit_map_table, hidden_columns=(4, 6))
        self.unit_map_table.doubleClicked.connect(lambda _: self.jump_to_unit_map_unit())
        layout.addWidget(self.unit_map_table, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Jump Raw", self.jump_to_unit_map_unit),
            ("Copy Unit Label", self.copy_selected_unit_label),
            ("Export Unit Map CSV", self.export_unit_map_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _preset_packs_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Preset Packs")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Curated add packs for common testing workflows. Presets still reuse empty existing slots only; use Save As first and verify in-game.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.preset_filter_edit = QLineEdit()
        self.preset_filter_edit.setPlaceholderText("Filter presets by category, name, item/sigil/weapon, or description...")
        self.preset_filter_edit.textChanged.connect(lambda _: self.refresh_preset_rows())
        layout.addWidget(self.preset_filter_edit)
        self.preset_table = QTableView()
        self.preset_table.setModel(self.preset_model)
        self._table_clean(self.preset_table, hidden_columns=(7,))
        self.preset_table.selectionModel().selectionChanged.connect(lambda *_: self.update_preset_detail())
        self.preset_table.doubleClicked.connect(lambda _: self.apply_selected_preset_pack())
        layout.addWidget(self.preset_table, 1)
        detail = make_card("Selected Preset")
        detail_layout = QVBoxLayout(detail)
        self.preset_detail_label = QLabel("Select a preset to preview what it will add.")
        self.preset_detail_label.setWordWrap(True)
        self.preset_detail_label.setObjectName("subtleText")
        detail_layout.addWidget(self.preset_detail_label)
        layout.addWidget(detail)
        row = QHBoxLayout()
        for text, slot in [
            ("Apply Preset", self.apply_selected_preset_pack),
            ("Copy Batch Text", self.copy_selected_preset_text),
            ("Export Presets CSV", self.export_preset_packs_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page


    def _add_equip_browser_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Add / Equip Browser")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Safe browser for database entries. Materials are only updated when the save already has an active 180x stack; this avoids the Add All inventory crash. Sigils and weapons reuse real empty slots. Reference-only hashes are clearly marked and cannot be added.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        tab_row = QHBoxLayout()
        self._add_browser_tab_buttons = []
        for label in ["Safe Add", "Items", "Sigils", "Weapons", "Wallet", "Lookup"]:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setMinimumHeight(34)
            btn.clicked.connect(lambda _=False, name=label: self._set_add_browser_tab(name))
            self._add_browser_tab_buttons.append(btn)
            tab_row.addWidget(btn)
        tab_row.addStretch(1)
        layout.addLayout(tab_row)
        self._refresh_add_browser_tab_buttons()

        top = QGridLayout()
        self.add_browser_filter = QLineEdit()
        self.add_browser_filter.setPlaceholderText("Search current tab by name, GBID, hash, or type... e.g. Damage Cap, Centrum, Apocalypse")
        self.add_browser_filter.textChanged.connect(lambda _: self.schedule_add_browser_refresh())
        top.addWidget(QLabel("Search"), 0, 0)
        top.addWidget(self.add_browser_filter, 0, 1, 1, 5)

        self.add_browser_status_filter = QComboBox()
        self.add_browser_status_filter.addItems(["All status", "Ready / Safe", "Missing / Safe Add", "Already Owned", "Has Empty Slot", "Blocked / Not Safe", "Reference Only"])
        self.add_browser_status_filter.currentTextChanged.connect(lambda _: self.schedule_add_browser_refresh())
        self.add_browser_subtype_filter = QComboBox()
        self.add_browser_subtype_filter.currentTextChanged.connect(lambda _: self.schedule_add_browser_refresh())
        self.add_browser_category = QComboBox()
        self.add_browser_category.addItems(["Auto", "Safe For Loaded Save", "Addable Types", "Items / Materials", "Sigils", "Weapons", "Wallet / Profile", "Relics / Curios", "All Database Rows", "Characters", "Traits / Skills", "Reference / Models"])
        self.add_browser_category.currentTextChanged.connect(lambda _: self.schedule_add_browser_refresh())
        top.addWidget(QLabel("Status"), 1, 0)
        top.addWidget(self.add_browser_status_filter, 1, 1)
        top.addWidget(QLabel("Subtype"), 1, 2)
        top.addWidget(self.add_browser_subtype_filter, 1, 3)
        top.addWidget(QLabel("Scope"), 1, 4)
        top.addWidget(self.add_browser_category, 1, 5)
        layout.addLayout(top)
        self._update_add_browser_subtype_filter()

        quick = QHBoxLayout()
        for label, query, tab, subtype in [
            ("Damage Cap", "Damage Cap", "Sigils", "Damage / Power"),
            ("War Elemental", "War Elemental", "Sigils", "Special / Unique"),
            ("Missing Materials", "", "Items", "Missing Safe"),
            ("Wrightstones", "", "Items", "Wrightstones"),
            ("Terminus/Apocalypse", "Apocalypse", "Weapons", "Apocalypse / Terminus"),
            ("Characters", "PL", "Lookup", "Characters"),
            ("Clear", "", "Safe Add", "All subtypes"),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, q=query, t=tab, st=subtype: self._set_add_browser_quick_filter(q, t, st))
            quick.addWidget(btn)
        quick.addStretch(1)
        layout.addLayout(quick)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.add_browser_table = QTableView()
        self.add_browser_table.setModel(self.add_browser_model)
        self._table_clean(self.add_browser_table, hidden_columns=())
        self.add_browser_table.setColumnWidth(0, 130)
        self.add_browser_table.setColumnWidth(1, 260)
        self.add_browser_table.setColumnWidth(2, 160)
        self.add_browser_table.setColumnWidth(3, 120)
        self.add_browser_table.setColumnWidth(4, 150)
        self.add_browser_table.setColumnWidth(5, 280)
        self.add_browser_table.selectionModel().selectionChanged.connect(lambda *_: self.update_add_browser_detail())
        self.add_browser_table.doubleClicked.connect(lambda _: self.add_browser_selected_default())
        splitter.addWidget(self.add_browser_table)

        detail = make_card("Selected Entry · Safe Actions")
        detail_layout = QVBoxLayout(detail)
        self.add_browser_detail_label = QLabel("Select a row. The action panel will explain whether it is safe to update/add for the currently loaded save.")
        self.add_browser_detail_label.setWordWrap(True)
        self.add_browser_detail_label.setObjectName("subtleText")
        detail_layout.addWidget(self.add_browser_detail_label)

        grid = QGridLayout()
        self.add_browser_qty_spin = QSpinBox(); self.add_browser_qty_spin.setRange(1, 99_999_999); self.add_browser_qty_spin.setValue(99)
        self.add_browser_level_spin = QSpinBox(); self.add_browser_level_spin.setRange(0, 99); self.add_browser_level_spin.setValue(15)
        self.add_browser_xp_spin = QSpinBox(); self.add_browser_xp_spin.setRange(0, 2_147_483_647); self.add_browser_xp_spin.setValue(0)
        self.add_browser_locked_check = QCheckBox("Lock new sigil"); self.add_browser_locked_check.setChecked(True)
        self.add_browser_equip_combo = QComboBox(); self.add_browser_equip_combo.addItem("None / Unequipped", EMPTY_HASH)
        grid.addWidget(QLabel("Material / Wallet Quantity"), 0, 0); grid.addWidget(self.add_browser_qty_spin, 0, 1)
        grid.addWidget(QLabel("Sigil Level"), 1, 0); grid.addWidget(self.add_browser_level_spin, 1, 1)
        grid.addWidget(QLabel("Weapon XP"), 2, 0); grid.addWidget(self.add_browser_xp_spin, 2, 1)
        grid.addWidget(QLabel("Equip New Sigil To"), 3, 0); grid.addWidget(self.add_browser_equip_combo, 3, 1)
        grid.addWidget(self.add_browser_locked_check, 4, 0, 1, 2)
        detail_layout.addLayout(grid)

        action_grid = QGridLayout()
        actions = [
            ("Update/Add Item Safely", self.add_browser_selected_as_item),
            ("Add as Sigil", self.add_browser_selected_as_sigil),
            ("Add as Weapon", self.add_browser_selected_as_weapon),
            ("Copy Hash", self.copy_add_browser_selected_hash),
            ("Open Items", lambda: self._show_page("Items / Materials")),
            ("Open Sigils", lambda: self._show_page("Sigils")),
            ("Open Weapons", lambda: self._show_page("Weapons")),
            ("Open Characters", lambda: self._show_page("Characters")),
        ]
        for i, (text, slot) in enumerate(actions):
            btn = QPushButton(text); btn.clicked.connect(slot); action_grid.addWidget(btn, i // 2, i % 2)
        detail_layout.addLayout(action_grid)
        detail_layout.addStretch(1)
        splitter.addWidget(detail)
        splitter.setSizes([980, 420])
        layout.addWidget(splitter, 1)
        self.refresh_add_browser_rows()
        return page

    def _items_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Inventory Items / Materials")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Inventory shows owned save-backed quantities. Database / Missing Items shows verified normal materials that can be safely added or updated without touching relics, curios, wallet-only values, or unknown rows.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.item_count_label = QLabel("Open a save to inspect ItemManager inventory slots.")
        self.item_count_label.setWordWrap(True)
        self.item_count_label.setObjectName("subtleText")
        layout.addWidget(self.item_count_label)
        self.item_empty_hint_label = QLabel("")
        self.item_empty_hint_label.setWordWrap(True)
        self.item_empty_hint_label.setObjectName("helpText")
        self.item_empty_hint_label.setVisible(False)
        layout.addWidget(self.item_empty_hint_label)

        self.items_material_tabs = QTabWidget()
        self.items_material_tabs.setObjectName("editorTabs")
        self.items_material_tabs.currentChanged.connect(lambda _=0: self._refresh_current_items_subtab())

        inventory_tab = QWidget()
        inventory_layout = QVBoxLayout(inventory_tab)
        inventory_layout.setContentsMargins(12, 12, 12, 12)
        inventory_layout.setSpacing(8)

        self.item_filter_edit = QLineEdit()
        self.item_filter_edit.setPlaceholderText("Filter owned items/materials by name, GBID, hash, quantity, or unit id...")
        self.item_filter_edit.textChanged.connect(lambda _: self.refresh_item_rows())
        inventory_layout.addWidget(self.item_filter_edit)

        category_row = QHBoxLayout()
        category_row.addWidget(QLabel("Category"))
        self.item_category_combo = QComboBox()
        self.item_category_combo.addItems([
            "All Safe Quantity Rows",
            "Wallet / Profile",
            "Materials",
            "Consumables",
            "Wrightstones",
            "Glitterstones",
            "Tickets / Badges",
            "Missing / Addable",
            "Unknown Safe Rows",
            "Technical / Relic / Curio",
        ])
        self.item_category_combo.currentTextChanged.connect(lambda _: self.refresh_item_rows())
        category_row.addWidget(self.item_category_combo)
        category_row.addStretch(1)
        inventory_layout.addLayout(category_row)
        item_filter_row = QHBoxLayout()
        self.item_show_empty_check = QCheckBox("Show empty addable slots")
        self.item_known_only_check = QCheckBox("Known hashes only")
        self.item_unknown_only_check = QCheckBox("Unknown hashes only")
        self.item_show_technical_check = QCheckBox("Show technical / non-quantity rows")
        self.item_show_technical_check.setToolTip("Show 190x/200x/210x item-slot, relic/curio, and type/state rows. These are hidden by default because their Quantity column is not a safe stack quantity.")
        self.item_known_only_check.setToolTip("Show only slots whose hash resolves to a known GBID/name.")
        self.item_unknown_only_check.setToolTip("Show only non-empty slots whose hash is not in the database yet.")
        self.item_known_only_check.toggled.connect(lambda checked: self._sync_known_unknown_filter(checked, self.item_unknown_only_check, self.refresh_item_rows))
        self.item_unknown_only_check.toggled.connect(lambda checked: self._sync_known_unknown_filter(checked, self.item_known_only_check, self.refresh_item_rows))
        self.item_show_empty_check.toggled.connect(lambda _=False: self.refresh_item_rows())
        self.item_show_technical_check.toggled.connect(lambda _=False: self.refresh_item_rows())
        item_filter_row.addWidget(self._make_filter_button("Filters", [
            ("Show empty addable slots", self.item_show_empty_check),
            ("Known hashes only", self.item_known_only_check),
            ("Unknown hashes only", self.item_unknown_only_check),
            ("Show technical / non-quantity rows", self.item_show_technical_check),
        ]))
        item_filter_row.addWidget(QLabel("Tip: double-click the Quantity column to edit, or use the selected-item actions below."))
        item_filter_row.addStretch(1)
        inventory_layout.addLayout(item_filter_row)
        self.item_table = QTableView()
        self.item_table.setModel(self.item_model)
        self._table_clean(self.item_table, hidden_columns=(0, 3, 7))
        self.item_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.item_table.selectionModel().selectionChanged.connect(lambda *_: self.update_item_detail())
        inventory_layout.addWidget(self.item_table, 1)
        detail = make_card("Selected Item")
        self._set_compact_detail(detail, max_height=170)
        detail_layout = QVBoxLayout(detail)
        self.item_detail_label = QLabel("Select an item row to view its save-backed fields.")
        self.item_detail_label.setObjectName("selectedItemSummary")
        self.item_detail_label.setWordWrap(True)
        self.item_detail_label.setMinimumHeight(62)
        detail_layout.addWidget(self.item_detail_label)

        # Hidden compatibility widgets for older helper methods.
        self.item_identity_edit = QLineEdit(); self.item_identity_edit.setVisible(False)
        self.item_quantity_edit = QLineEdit(); self.item_quantity_edit.setVisible(False)
        self.item_index_edit = QLineEdit(); self.item_index_edit.setVisible(False)
        self.item_flag_edit = QLineEdit(); self.item_flag_edit.setVisible(False)
        for editor in (self.item_identity_edit, self.item_quantity_edit, self.item_index_edit, self.item_flag_edit):
            editor.returnPressed.connect(self.apply_item_inline_edits)

        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("Qty"))
        self.item_selected_qty_spin = QSpinBox()
        self.item_selected_qty_spin.setRange(0, 99_999_999)
        self.item_selected_qty_spin.setValue(1)
        self.item_selected_qty_spin.setMinimumWidth(130)
        action_row.addWidget(self.item_selected_qty_spin)
        for text, slot in [
            ("Set Quantity", self.set_selected_item_quantity_from_spin),
            ("Max Selected", self.max_selected_item_quantity),
            ("Add Item", self.add_item_to_empty_slot),
            ("Copy Hash", self.copy_selected_item_hash),
            ("Copy GBID", self.copy_selected_item_gbid),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); action_row.addWidget(btn)
        action_row.addStretch(1)
        detail_layout.addLayout(action_row)
        inventory_layout.addWidget(detail)

        row = QHBoxLayout()
        row.addWidget(self._make_more_button("More", [
            ("Edit Quantity", self.edit_selected_item_quantity),
            ("Edit Item / Hash", self.edit_selected_item_hash),
            ("Edit Index / Serial", self.edit_selected_item_index),
            ("Edit Flag / State", self.edit_selected_item_flag),
            ("Show Empty Addable Slots", lambda: (self.item_show_empty_check.setChecked(True), self.refresh_item_rows())),
            ("Explain This Tab", self.explain_items_tab),
            ("Validate Inventory Safety", self.validate_inventory_safety),
            ("Batch Add Items From Text", self.batch_add_items_to_empty_slots),
            ("Duplicate Item to Empty Slot", self.duplicate_selected_item_to_empty_slot),
            ("Copy Item Slot", self.copy_selected_item_slot),
            ("Paste Item Slot", self.paste_item_slot_to_selected),
            ("Swap With Copied Item Slot", self.swap_selected_item_with_copied),
            ("Set Visible Quantities", self.bulk_set_visible_item_quantity),
            ("Max Visible Quantities", self.max_visible_item_quantities),
            ("Jump to Raw Unit", self.jump_to_item_unit),
            ("Copy Hash", self.copy_selected_item_hash),
            ("Copy GBID", self.copy_selected_item_gbid),
            ("Export CSV", self.export_items_csv),
        ]))
        row.addStretch(1)
        inventory_layout.addLayout(row)

        self.items_material_tabs.addTab(inventory_tab, "Inventory")
        self.items_material_tabs.addTab(self._build_items_database_tab(), "Database / Missing Items")
        self.items_material_tabs.addTab(self._build_relic_curio_database_tab(), "Relics / Curios")
        layout.addWidget(self.items_material_tabs, 1)
        return page

    def _build_items_database_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        help_text = QLabel("Shows verified normal item/material database entries missing from this save. This view uses the same safe 180x material-template writer as the working item cheat; relics, curios, wallet values, unknowns, and reference-only rows are not added here.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        controls = QHBoxLayout()
        self.items_database_filter = QLineEdit()
        self.items_database_filter.setPlaceholderText("Search missing materials/items by name, GBID, category, or hash...")
        self.items_database_filter.textChanged.connect(lambda _: self.refresh_items_database_rows())
        controls.addWidget(self.items_database_filter, 2)
        self.items_database_category = QComboBox()
        self.items_database_category.addItems(["Missing Safe Only", "Already Owned", "All Safe Materials", "Blocked / Not Safe"])
        self.items_database_category.currentTextChanged.connect(lambda _: self.refresh_items_database_rows())
        controls.addWidget(self.items_database_category)
        controls.addWidget(QLabel("Qty"))
        self.items_database_qty_spin = QSpinBox()
        self.items_database_qty_spin.setRange(1, 99_999_999)
        self.items_database_qty_spin.setValue(999)
        controls.addWidget(self.items_database_qty_spin)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_items_database_rows)
        controls.addWidget(refresh_btn)
        layout.addLayout(controls)

        self.items_database_summary = QLabel("Open a save to compare the database against inventory.")
        self.items_database_summary.setObjectName("subtleText")
        self.items_database_summary.setWordWrap(True)
        layout.addWidget(self.items_database_summary)

        self.items_database_table = QTableView()
        self.items_database_table.setModel(self.items_database_model)
        self._table_clean(self.items_database_table, hidden_columns=())
        self.items_database_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.items_database_table.selectionModel().selectionChanged.connect(lambda *_: self.update_items_database_detail())
        layout.addWidget(self.items_database_table, 1)

        detail = make_card("Selected Database Item")
        detail_layout = QVBoxLayout(detail)
        self.items_database_detail = QLabel("Select a missing safe item to add it to the loaded save.")
        self.items_database_detail.setObjectName("selectedItemSummary")
        self.items_database_detail.setWordWrap(True)
        self.items_database_detail.setMinimumHeight(70)
        detail_layout.addWidget(self.items_database_detail)
        button_row = QHBoxLayout()
        for text, slot in [
            ("Add / Update Selected", self.add_items_database_selected),
            ("Add Visible Missing", self.add_items_database_visible_missing),
            ("Open Inventory View", lambda: self.items_material_tabs.setCurrentIndex(0) if hasattr(self, "items_material_tabs") else None),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); button_row.addWidget(btn)
        button_row.addStretch(1)
        detail_layout.addLayout(button_row)
        layout.addWidget(detail)
        return tab

    def _build_relic_curio_database_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        help_text = QLabel("Experimental relic/curio database. This does not use normal material 1801/1802 stacks. It writes only into existing empty 190x/200x relic/curio spaces and shows how many matching relics are already present in the loaded save.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        controls = QHBoxLayout()
        self.relic_database_filter = QLineEdit()
        self.relic_database_filter.setPlaceholderText("Search relics/curios by name, GBID, hash...")
        self.relic_database_filter.textChanged.connect(lambda _: self.refresh_relic_database_rows())
        controls.addWidget(self.relic_database_filter, 2)
        self.relic_database_status = QComboBox()
        self.relic_database_status.addItems(["Missing Only", "Already Owned", "All Relics / Curios"])
        self.relic_database_status.currentTextChanged.connect(lambda _: self.refresh_relic_database_rows())
        controls.addWidget(self.relic_database_status)
        controls.addWidget(QLabel("Count / State"))
        self.relic_database_qty_spin = QSpinBox()
        self.relic_database_qty_spin.setRange(1, 9999)
        self.relic_database_qty_spin.setValue(1)
        controls.addWidget(self.relic_database_qty_spin)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_relic_database_rows)
        controls.addWidget(refresh_btn)
        layout.addLayout(controls)

        self.relic_database_summary = QLabel("Open a save to inspect relic/curio slots.")
        self.relic_database_summary.setObjectName("subtleText")
        self.relic_database_summary.setWordWrap(True)
        layout.addWidget(self.relic_database_summary)

        self.relic_database_table = QTableView()
        self.relic_database_table.setModel(self.relic_database_model)
        self._table_clean(self.relic_database_table, hidden_columns=())
        self.relic_database_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.relic_database_table.selectionModel().selectionChanged.connect(lambda *_: self.update_relic_database_detail())
        self.relic_database_table.doubleClicked.connect(lambda *_: self.add_relic_database_selected())
        layout.addWidget(self.relic_database_table, 1)

        detail = make_card("Selected Relic / Curio")
        detail_layout = QVBoxLayout(detail)
        self.relic_database_detail = QLabel("Select a missing relic/curio to write it into an empty existing 190x/200x slot.")
        self.relic_database_detail.setObjectName("selectedItemSummary")
        self.relic_database_detail.setWordWrap(True)
        self.relic_database_detail.setMinimumHeight(70)
        detail_layout.addWidget(self.relic_database_detail)
        row = QHBoxLayout()
        for text, slot in [
            ("Add Selected Relic", self.add_relic_database_selected),
            ("Add Visible Missing", self.add_relic_database_visible_missing),
            ("Open Inventory View", lambda: self.items_material_tabs.setCurrentIndex(0) if hasattr(self, "items_material_tabs") else None),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        detail_layout.addLayout(row)
        layout.addWidget(detail)
        return tab

    def _relic_curio_entries(self):
        rows = []
        for entry in self.item_db.by_hash.values():
            if not self._is_curio_relic_entry(entry):
                continue
            name = str(getattr(entry, "display_name", "") or "").strip()
            gbid = str(getattr(entry, "item_id", "") or "").strip()
            if not name and not gbid:
                continue
            rows.append(entry)
        return sorted(rows, key=lambda e: (str(getattr(e, "item_id", "")), str(getattr(e, "display_name", ""))))

    def _relic_slot_rows(self) -> List[Dict[str, Any]]:
        if not self.save:
            return []
        rows = []
        grouped = self.save.group_by_unit([1901, 1902, 1903, 1904, 2002, 2003, 2004])
        families = [
            ("190x Relic/Curio slot", 1901, 1902, 1903, 1904),
            ("200x Relic/Curio slot", 2002, 2003, 2004, None),
        ]
        for unit_id, fields in sorted(grouped.items()):
            for label, hash_id, index_id, qty_id, flag_id in families:
                if hash_id not in fields or qty_id not in fields:
                    continue
                hrec = fields.get(hash_id)
                qrec = fields.get(qty_id)
                item_hash = int(self._record_first_value(hrec, 0) or 0) & 0xFFFFFFFF
                qty = int(self._record_first_value(qrec, 0) or 0)
                rows.append({
                    "unit_id": unit_id,
                    "label": label,
                    "hash": item_hash,
                    "qty": qty,
                    "hash_rec": hrec,
                    "qty_rec": qrec,
                    "index_rec": fields.get(index_id) if index_id else None,
                    "flag_rec": fields.get(flag_id) if flag_id else None,
                })
                break
        return rows

    def _relic_counts_and_empty(self) -> tuple[Dict[int, int], int]:
        counts: Dict[int, int] = {}
        empty = 0
        for slot in self._relic_slot_rows():
            h = int(slot.get("hash", 0) or 0) & 0xFFFFFFFF
            qty = int(slot.get("qty", 0) or 0)
            if h in (0, EMPTY_HASH):
                if qty == 0:
                    empty += 1
                continue
            counts[h] = counts.get(h, 0) + max(1, qty)
        return counts, empty

    def refresh_relic_database_rows(self) -> None:
        if not hasattr(self, "relic_database_model"):
            return
        if not self.save:
            self.relic_database_rows_meta = []
            self.relic_database_model.set_rows([])
            if hasattr(self, "relic_database_summary"):
                self.relic_database_summary.setText("Open a save to inspect relic/curio slots.")
            return
        counts, empty = self._relic_counts_and_empty()
        query = (self.relic_database_filter.text() if hasattr(self, "relic_database_filter") else "").strip().lower()
        status_filter = self.relic_database_status.currentText() if hasattr(self, "relic_database_status") else "Missing Only"
        rows: List[List[Any]] = []
        metas: List[Dict[str, Any]] = []
        owned_n = missing_n = 0
        for entry in self._relic_curio_entries():
            h = int(entry.hash_value) & 0xFFFFFFFF
            owned = counts.get(h, 0)
            if owned:
                owned_n += 1
                status = "Already owned"
                action = "Already present"
            else:
                missing_n += 1
                status = "Missing"
                action = "Add to empty 190x/200x slot" if empty else "No empty relic slot"
            if status_filter == "Missing Only" and owned:
                continue
            if status_filter == "Already Owned" and not owned:
                continue
            searchable = " ".join([str(entry.display_name), str(entry.item_id), str(entry.hash_hex), str(entry.alias_text)]).lower()
            if query and query not in searchable:
                continue
            rows.append([status, entry.display_name or entry.item_id, entry.item_id, entry.hash_hex, owned, empty, action])
            metas.append({"entry": entry, "owned": owned, "status": status})
            if len(rows) >= 750:
                rows.append(["More results hidden", "Narrow search/filter to show more rows", "—", "—", "—", empty, "Showing first 750 rows"])
                metas.append({"entry": None, "status": "hint"})
                break
        self.relic_database_rows_meta = metas
        self.relic_database_model.set_rows(rows)
        if hasattr(self, "relic_database_table"):
            self._set_table_widths(self.relic_database_table, {0: 150, 1: 320, 2: 160, 3: 110, 4: 80, 5: 90, 6: 230})
        if hasattr(self, "relic_database_summary"):
            self.relic_database_summary.setText(f"Relics/Curios: {owned_n:,} owned types · {missing_n:,} missing database types · {empty:,} empty 190x/200x slots available.")
        self.update_relic_database_detail()

    def _selected_relic_database_meta(self) -> Optional[Dict[str, Any]]:
        if not hasattr(self, "relic_database_table"):
            return None
        idx = self.relic_database_table.currentIndex()
        if not idx.isValid():
            return None
        try:
            return self.relic_database_rows_meta[idx.row()]
        except Exception:
            return None

    def update_relic_database_detail(self) -> None:
        if not hasattr(self, "relic_database_detail"):
            return
        meta = self._selected_relic_database_meta()
        entry = meta.get("entry") if meta else None
        if not entry:
            self._set_detail_text(self.relic_database_detail, "Select a relic/curio. Missing entries can be written only into existing empty 190x/200x relic/curio slots.")
            return
        h = int(entry.hash_value) & 0xFFFFFFFF
        counts, empty = self._relic_counts_and_empty()
        owned = counts.get(h, 0)
        self._set_detail_text(self.relic_database_detail, "  •  ".join([
            f"Name: {entry.display_name or entry.item_id}",
            f"GBID: {entry.item_id}",
            f"Hash: 0x{h:08X}",
            f"Already in save: {owned:,}",
            f"Empty relic/curio slots: {empty:,}",
            "Backing: 190x/200x ItemManager relic/curio rows",
        ]))

    def _find_empty_relic_curio_slot(self) -> Optional[Dict[str, Any]]:
        for slot in self._relic_slot_rows():
            h = int(slot.get("hash", 0) or 0) & 0xFFFFFFFF
            qty = int(slot.get("qty", 0) or 0)
            if h in (0, EMPTY_HASH) and qty == 0:
                return slot
        return None

    def _write_relic_curio_to_empty_slot(self, item_hash: int, qty: int = 1) -> Optional[str]:
        if not self.save:
            return None
        slot = self._find_empty_relic_curio_slot()
        if not slot:
            return None
        item_hash = int(item_hash) & 0xFFFFFFFF
        qty = max(1, int(qty))
        changed = 0
        changed += 1 if self._set_record_first_value(slot.get("hash_rec"), item_hash, "relic/curio hash") else 0
        changed += 1 if self._set_record_first_value(slot.get("qty_rec"), qty, "relic/curio count/state") else 0
        if slot.get("flag_rec") is not None and self._record_first_value(slot.get("flag_rec"), 0) == 0:
            changed += 1 if self._set_record_first_value(slot.get("flag_rec"), 1, "relic/curio flag") else 0
        entry = self.item_db.lookup_hash(item_hash)
        display = f"{entry.display_name} ({entry.item_id})" if entry else f"0x{item_hash:08X}"
        return f"Added {display} -> {slot['label']} unit {slot['unit_id']} ({changed} fields)" if changed else None

    def add_relic_database_selected(self) -> None:
        meta = self._selected_relic_database_meta()
        entry = meta.get("entry") if meta else None
        if not entry:
            QMessageBox.information(self, "No relic selected", "Select a relic/curio database row first.")
            return
        qty = int(self.relic_database_qty_spin.value()) if hasattr(self, "relic_database_qty_spin") else 1
        result = self._write_relic_curio_to_empty_slot(int(entry.hash_value) & 0xFFFFFFFF, qty)
        if not result:
            QMessageBox.information(self, "No empty relic slot", "No empty 190x/200x relic/curio slot was found. This editor will not create new FlatBuffer rows yet.")
            return
        self._after_editor_patch(result)
        self.refresh_relic_database_rows()
        self.refresh_item_rows()

    def add_relic_database_visible_missing(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        candidates = [m.get("entry") for m in getattr(self, "relic_database_rows_meta", []) if m.get("status") == "Missing" and m.get("entry") is not None]
        if not candidates:
            QMessageBox.information(self, "No missing relics visible", "No visible missing relic/curio rows are selected by the current filters.")
            return
        qty = int(self.relic_database_qty_spin.value()) if hasattr(self, "relic_database_qty_spin") else 1
        added = 0
        for entry in candidates:
            if not self._find_empty_relic_curio_slot():
                break
            if self._write_relic_curio_to_empty_slot(int(entry.hash_value) & 0xFFFFFFFF, qty):
                added += 1
        self._after_editor_patch(f"Added {added} visible missing relic/curio rows into empty 190x/200x slots. Save As before testing.")
        self.refresh_relic_database_rows()
        self.refresh_item_rows()

    def _refresh_current_items_subtab(self) -> None:
        if not hasattr(self, "items_material_tabs"):
            return
        text = self.items_material_tabs.tabText(self.items_material_tabs.currentIndex())
        if text.startswith("Database"):
            self.refresh_items_database_rows()
        elif text.startswith("Relics"):
            self.refresh_relic_database_rows()
        else:
            self.refresh_item_rows()

    def _selected_items_database_meta(self) -> Optional[Dict[str, Any]]:
        if not hasattr(self, "items_database_table"):
            return None
        idx = self.items_database_table.currentIndex()
        if not idx.isValid():
            return None
        try:
            return self.items_database_rows_meta[idx.row()]
        except Exception:
            return None

    def _items_database_entry_status(self, entry) -> tuple[str, str]:
        """Return (status, action) for a normal item/material database entry."""
        h = int(entry.hash_value) & 0xFFFFFFFF
        active = h in getattr(self, "_add_browser_active_material_hashes", set())
        templated = h in getattr(self, "_add_browser_safe_template_hashes", set())
        if active:
            return "Already owned", "Update quantity"
        if templated:
            return "Missing · safe add", "Add safely"
        return "Blocked / not safe", "Needs active 1801 row/template"

    def _items_database_category_accepts(self, status: str) -> bool:
        category = self.items_database_category.currentText() if hasattr(self, "items_database_category") else "Missing Safe Only"
        if category == "Missing Safe Only":
            return status == "Missing · safe add"
        if category == "Already Owned":
            return status == "Already owned"
        if category == "All Safe Materials":
            return status in {"Missing · safe add", "Already owned"}
        if category == "Blocked / Not Safe":
            return status == "Blocked / not safe"
        return True

    def refresh_items_database_rows(self) -> None:
        if not hasattr(self, "items_database_model"):
            return
        if not self.save:
            self.items_database_rows_meta = []
            self.items_database_model.set_rows([])
            if hasattr(self, "items_database_summary"):
                self.items_database_summary.setText("Open a save to compare the database against inventory.")
            self.update_items_database_detail()
            return
        self._ensure_add_browser_indexes()
        query = (self.items_database_filter.text() if hasattr(self, "items_database_filter") else "").strip().lower()
        rows: List[List[Any]] = []
        meta: List[Dict[str, Any]] = []
        counts = {"missing": 0, "owned": 0, "blocked": 0}
        shown_limit = 750
        for entry in self._known_material_entries():
            if self._is_curio_relic_entry(entry):
                continue
            wallet = self._wallet_field_for_item_key(entry.display_name, entry.hash_value)
            if wallet is not None:
                continue
            status, action = self._items_database_entry_status(entry)
            if status == "Missing · safe add":
                counts["missing"] += 1
            elif status == "Already owned":
                counts["owned"] += 1
            else:
                counts["blocked"] += 1
            if not self._items_database_category_accepts(status):
                continue
            searchable = " ".join([
                str(entry.display_name), str(entry.item_id), str(entry.category), str(entry.hash_hex), str(entry.alias_text)
            ]).lower()
            if query and query not in searchable:
                continue
            rows.append([status, entry.display_name, entry.item_id, entry.category, entry.hash_hex, action])
            meta.append({"entry": entry, "status": status, "action": action})
            if len(rows) >= shown_limit:
                rows.append(["More results hidden", "Narrow search/filter to show more rows", "—", "—", "—", f"Showing first {shown_limit} rows"])
                meta.append({"entry": None, "status": "hint", "action": "hint"})
                break
        self.items_database_rows_meta = meta
        self.items_database_model.set_rows(rows)
        if hasattr(self, "items_database_table"):
            self._set_table_widths(self.items_database_table, {0: 160, 1: 340, 2: 170, 3: 140, 4: 110, 5: 210})
        if hasattr(self, "items_database_summary"):
            self.items_database_summary.setText(
                f"Database compare: {counts['missing']:,} missing safe addable · {counts['owned']:,} already owned · {counts['blocked']:,} blocked/no template · showing {len([r for r in rows if r and r[0] != 'More results hidden']):,}"
            )
        self.update_items_database_detail()

    def update_items_database_detail(self) -> None:
        if not hasattr(self, "items_database_detail"):
            return
        meta = self._selected_items_database_meta()
        entry = meta.get("entry") if meta else None
        if not entry:
            self._set_detail_text(
                self.items_database_detail,
                "Select a row. Missing safe rows can be added because the save already has the exact inactive 1801 material row and the editor has a verified 1803-1807 template."
            )
            return
        status = meta.get("status", "")
        h = int(entry.hash_value) & 0xFFFFFFFF
        active = h in getattr(self, "_add_browser_active_material_hashes", set())
        templated = h in getattr(self, "_add_browser_safe_template_hashes", set())
        self._set_detail_text(
            self.items_database_detail,
            "  •  ".join([
                f"Name: {entry.display_name}",
                f"GBID: {entry.item_id}",
                f"Hash: 0x{h:08X}",
                f"Category: {entry.category}",
                f"Status: {status}",
                f"Active stack: {'Yes' if active else 'No'}",
                f"Safe template: {'Yes' if templated else 'No'}",
            ])
        )

    def add_items_database_selected(self) -> None:
        meta = self._selected_items_database_meta()
        entry = meta.get("entry") if meta else None
        if not entry:
            QMessageBox.information(self, "No item selected", "Select a database item first.")
            return
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        status = meta.get("status")
        if status not in {"Missing · safe add", "Already owned"}:
            QMessageBox.warning(self, "Not safe to add", "This row has no active material stack or verified safe-add template in this save, so it is blocked.")
            return
        qty = int(self.items_database_qty_spin.value()) if hasattr(self, "items_database_qty_spin") else 999
        result = self._upsert_material_bank_quantity(int(entry.hash_value) & 0xFFFFFFFF, qty, flag=None)
        if not result:
            QMessageBox.information(self, "No safe target", "Could not find a safe 180x stack/template for this item.")
            return
        self._after_editor_patch(f"Database item action: {result}")
        self.refresh_items_database_rows()
        self.refresh_item_rows()

    def add_items_database_visible_missing(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        candidates = [m.get("entry") for m in getattr(self, "items_database_rows_meta", []) if m.get("status") == "Missing · safe add" and m.get("entry") is not None]
        if not candidates:
            QMessageBox.information(self, "No visible missing items", "No visible rows are currently safe missing items. Change the filter to Missing Safe Only or clear the search.")
            return
        qty = int(self.items_database_qty_spin.value()) if hasattr(self, "items_database_qty_spin") else 999
        added = []
        for entry in candidates:
            result = self._upsert_material_bank_quantity(int(entry.hash_value) & 0xFFFFFFFF, qty, flag=None)
            if result:
                added.append(result)
        self._after_editor_patch(f"Safely added {len(added)} visible database items. Save As when ready.")
        self.refresh_items_database_rows()
        self.refresh_item_rows()

    def _inventory_safety_report_lines(self) -> tuple[List[str], bool]:
        """Return inventory validation report lines and whether a crash-risk pattern exists."""
        if not self.save:
            return (["Open a save first."], False)
        grouped = self.save.group_by_unit([1801, 1802, 1803, 1804, 1805, 1806, 1807, 1901, 1902, 1903, 1904, 2002, 2003, 2004, 2102, 2103, 2104, 2105])
        suspicious_qty_only = []
        inactive_catalog = []
        active_materials = 0
        technical_nonzero = []
        duplicate_active_hashes: Dict[int, List[int]] = {}
        zero_active_known = []
        for unit_id, fields in sorted(grouped.items()):
            if fields.get(1801) is not None:
                h = self._record_first_value(fields.get(1801), 0) & 0xFFFFFFFF
                q = int(self._record_first_value(fields.get(1802), 0) or 0)
                state = int(self._record_first_value(fields.get(1803), 0) or 0)
                index = int(self._record_first_value(fields.get(1804), 0) or 0)
                extra = int(self._record_first_value(fields.get(1807), 0) or 0)
                entry = self.item_db.lookup_hash(h) if h else None
                name = f"{entry.display_name} ({entry.item_id})" if entry else f"0x{h:08X}"
                active = bool(h and self._material_bank_slot_is_active(fields))
                if active:
                    active_materials += 1
                    duplicate_active_hashes.setdefault(h, []).append(unit_id)
                    if q <= 0 and entry and entry.category in self.MATERIAL_BANK_CATEGORIES:
                        zero_active_known.append((unit_id, name, q))
                if h and q > 0 and state == 0 and index == 0 and extra == 0:
                    suspicious_qty_only.append((unit_id, name, q))
                if h and q == 0 and (state == 0 and index == 0 and extra == 0) and entry and entry.category in self.MATERIAL_BANK_CATEGORIES:
                    inactive_catalog.append((unit_id, name))
            else:
                qrec = self._first_existing_record(fields, [2105, 1903, 2004])
                q = int(self._record_first_value(qrec, 0) or 0)
                hrec = self._first_non_empty_record(fields, [2102, 1901, 2002])
                h = self._record_first_value(hrec, 0) if hrec else 0
                if q:
                    technical_nonzero.append((unit_id, f"0x{int(h or 0) & 0xFFFFFFFF:08X}", q))
        dupes = [(h, units) for h, units in duplicate_active_hashes.items() if len(units) > 1]
        lines = [
            f"Active material-bank rows: {active_materials:,}",
            f"Inactive known material catalog rows: {len(inactive_catalog):,}",
            f"Suspicious quantity-only 180x rows: {len(suspicious_qty_only):,}",
            f"Duplicate active material hashes: {len(dupes):,}",
            f"Active known rows with zero/negative quantity: {len(zero_active_known):,}",
            f"Technical non-quantity rows with state values: {len(technical_nonzero):,}",
        ]
        risky = bool(suspicious_qty_only or zero_active_known)
        if suspicious_qty_only:
            lines += ["", "Potential crash-risk rows (quantity set but companion state is empty):"]
            for unit_id, name, q in suspicious_qty_only[:12]:
                lines.append(f"- unit {unit_id}: {name} x{q:,}")
            if len(suspicious_qty_only) > 12:
                lines.append(f"...and {len(suspicious_qty_only)-12:,} more")
            lines += ["", "Use More → Repair Unsafe Inventory Rows before saving/testing this copy."]
        if zero_active_known:
            lines += ["", "Active known rows with zero quantity:"]
            for unit_id, name, q in zero_active_known[:12]:
                lines.append(f"- unit {unit_id}: {name} x{q:,}")
        if dupes:
            lines += ["", "Duplicate active hashes to review:"]
            for h, units in dupes[:12]:
                entry = self.item_db.lookup_hash(h)
                name = entry.display_name if entry else f"0x{h:08X}"
                lines.append(f"- {name}: units {', '.join(str(u) for u in units[:6])}")
        if not risky:
            lines += ["", "No known Add-All crash pattern was detected."]
        return lines, risky

    def validate_inventory_safety(self) -> None:
        """Scan inventory rows for the specific unsafe patterns we have seen crash GBFR."""
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        lines, _risky = self._inventory_safety_report_lines()
        QMessageBox.information(self, "Inventory Safety Check", "\n".join(lines))

    def explain_items_tab(self) -> None:
        QMessageBox.information(
            self,
            "What the Items tab shows",
            "The Items tab is not the item-ID database. It only lists active ItemManager inventory slots from the loaded save.\n\n"
            "By default it only shows safe editable quantities: UserData wallet values and ItemManager 1801/1802 material-bank stacks.\n\n"
            "Technical item-slot rows, relic/curio lookup rows, and type/state rows are hidden by default because their quantity-looking value is not a normal stack count. Enable Show technical / non-quantity rows to inspect them without treating them as safe bulk-edit targets.\n\n"
            "It does not show sigils, weapons, characters, quests, titles, raw Save Wizard rows, or the full GBID database. Those are separate pages.\n\n"
            "If this page is empty, the loaded file probably has no active ItemManager item stacks, or the filters are hiding them. Use Add Item to fill an empty slot, enable Show empty addable slots, or load the full SaveData1.dat instead of a raw GameData-only file."
        )

    def _build_character_owner_choices(self) -> List[Dict[str, Any]]:
        """Known character hashes used by GemManager 2706 worn/equipped owner."""
        choices: List[Dict[str, Any]] = [{"label": "None / Unequipped", "name": "None / Unequipped", "gbid": "", "hash": EMPTY_HASH}]
        try:
            entries = []
            for entry in self.item_db.by_hash.values():
                gbid = str(getattr(entry, "item_id", "") or "").upper()
                if gbid.startswith("PL") and len(gbid) >= 6:
                    entries.append(entry)
            def _sort_key(entry):
                gbid = str(entry.item_id).upper()
                try:
                    return int(gbid[2:6])
                except Exception:
                    return 999999
            for entry in sorted(entries, key=_sort_key):
                choices.append({
                    "label": f"{entry.display_name} ({entry.item_id})",
                    "name": entry.display_name,
                    "gbid": entry.item_id,
                    "hash": entry.hash_value & 0xFFFFFFFF,
                })
        except Exception:
            pass
        return choices

    def _character_owner_name_for_hash(self, value: Any) -> str:
        try:
            ivalue = int(value or 0) & 0xFFFFFFFF
        except Exception:
            return str(value or "")
        if ivalue in (0, EMPTY_HASH):
            return "None / Unequipped"
        for choice in getattr(self, "character_owner_choices", []):
            if int(choice.get("hash", 0)) & 0xFFFFFFFF == ivalue:
                return str(choice.get("name") or choice.get("label") or f"0x{ivalue:08X}")
        entry = self.item_db.lookup_hash(ivalue)
        if entry:
            return entry.display_name
        return f"Unknown owner 0x{ivalue:08X}"

    def _character_owner_gbid_for_hash(self, value: Any) -> str:
        try:
            ivalue = int(value or 0) & 0xFFFFFFFF
        except Exception:
            return ""
        if ivalue in (0, EMPTY_HASH):
            return ""
        for choice in getattr(self, "character_owner_choices", []):
            if int(choice.get("hash", 0)) & 0xFFFFFFFF == ivalue:
                return str(choice.get("gbid") or "")
        entry = self.item_db.lookup_hash(ivalue)
        return entry.item_id if entry else ""

    def _current_sigil_owner_hash(self) -> int:
        row = self._selected_row(self.sigil_table, self.sigil_model) if hasattr(self, "sigil_table") else None
        if not row:
            return EMPTY_HASH
        try:
            meta = self._selected_sigil_meta()
            if meta and meta.get("worn_rec") is not None:
                return int(self._record_first_value(meta.get("worn_rec"), EMPTY_HASH) or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            pass
        resolved = self._resolve_hash_from_text(str(row[7] or row[6] or ""))
        return int(resolved if resolved is not None else EMPTY_HASH) & 0xFFFFFFFF

    def _set_owner_combo_by_hash(self, value: Any) -> None:
        combo = getattr(self, "sigil_worn_by_combo", None)
        if combo is None:
            return
        try:
            ivalue = int(value or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            ivalue = EMPTY_HASH
        combo.blockSignals(True)
        found = False
        for idx in range(combo.count()):
            try:
                if int(combo.itemData(idx) or 0) & 0xFFFFFFFF == ivalue:
                    combo.setCurrentIndex(idx)
                    found = True
                    break
            except Exception:
                pass
        if not found:
            raw_text = "" if ivalue in (0, EMPTY_HASH) else f"0x{ivalue:08X}"
            if hasattr(self, "sigil_worn_by_edit"):
                self._set_line_edit_text_safely("sigil_worn_by_edit", raw_text)
            combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _sigil_owner_counts_by_hash(self, skip_meta: Optional[Dict[str, Any]] = None) -> Dict[int, int]:
        """Count non-empty sigils that currently point at each equipped-owner hash.

        This only counts the visible owner reference field. It is used as a
        safety guard because the game breaks above 13 equipped sigils for one
        owner, even after the full equip mapping is implemented.
        """
        counts: Dict[int, int] = {}
        if not self.save:
            return counts
        try:
            skip_unit = int((skip_meta or {}).get("unit_id", -1))
        except Exception:
            skip_unit = -1
        grouped = self.save.group_by_unit([2703, 2706])
        for unit_id, fields in sorted(grouped.items()):
            try:
                if int(unit_id) == skip_unit:
                    continue
                sigil_hash = int(self.value1(fields.get(2703), 0) or 0) & 0xFFFFFFFF
                owner_hash = int(self.value1(fields.get(2706), 0) or 0) & 0xFFFFFFFF
            except Exception:
                continue
            if sigil_hash in (0, EMPTY_HASH) or owner_hash in (0, EMPTY_HASH):
                continue
            counts[owner_hash] = counts.get(owner_hash, 0) + 1
        return counts

    def _sigil_owner_assignment_allowed(self, row_index: int, target_hash: int, show_message: bool = True) -> bool:
        """Validate a sigil owner/equip assignment before touching the save.

        Current builds only know the visible owner reference. The uploaded test
        reports prove that writing this field alone can create a crashy state:
        the game says the sigil is equipped, but it is not actually attached to
        the weapon/character equip list. Non-empty equip writes are therefore
        blocked until a known-good equipped save maps the missing fields.
        """
        try:
            target_hash = int(target_hash or 0) & 0xFFFFFFFF
        except Exception:
            target_hash = EMPTY_HASH

        # Clearing/unequipping is allowed so users can repair bad owner refs
        # created by older builds.
        if target_hash in (0, EMPTY_HASH):
            return True

        if not SIGIL_EQUIP_WRITES_ENABLED:
            if show_message:
                QMessageBox.warning(
                    self,
                    "Sigil equip disabled",
                    "Sigil equip writes are temporarily disabled because the old editor only changed the visible owner field.\n\n"
                    "That can make the game show an Equip option but crash when you click equip/unequip. "
                    "Send the known-good save with sigils equipped to a weapon and I can map the missing weapon/equip-list fields.\n\n"
                    "You can still edit sigil ID, level, lock flags, add sigils to inventory, and use Unequip/Clear Worn By to remove unsafe owner refs."
                )
            return False

        meta = {}
        try:
            if 0 <= int(row_index) < len(getattr(self, "sigil_rows_meta", [])):
                meta = self.sigil_rows_meta[int(row_index)]
        except Exception:
            meta = {}

        counts = self._sigil_owner_counts_by_hash(skip_meta=meta)
        if counts.get(target_hash, 0) >= SIGIL_MAX_EQUIPPED_PER_OWNER:
            if show_message:
                QMessageBox.warning(
                    self,
                    "Too many equipped sigils",
                    f"The game only tolerates {SIGIL_MAX_EQUIPPED_PER_OWNER} equipped sigils per owner. "
                    f"This owner already has {counts.get(target_hash, 0)}. Clear one first."
                )
            return False
        return True

    def _sigil_owner_combo_changed(self, index: int) -> None:
        """Mirror the owner dropdown and only apply safe clear/unequip edits.

        Non-empty equip writes are blocked until the full in-game equipment
        relationship is mapped from a known-good save.
        """
        combo = getattr(self, "sigil_worn_by_combo", None)
        if combo is None:
            return
        value = combo.itemData(index)
        if value is None:
            return
        try:
            owner_hash = int(value or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            owner_hash = EMPTY_HASH
        if hasattr(self, "sigil_worn_by_edit"):
            self._set_line_edit_text_safely("sigil_worn_by_edit", "" if owner_hash in (0, EMPTY_HASH) else f"0x{owner_hash:08X}")

        # During row selection/detail refresh we only want the widgets to mirror
        # the selected row. Real-time apply is only for user-driven dropdown edits.
        if getattr(self, "_updating_sigil_detail", False):
            return
        if not getattr(self, "save", None) or not hasattr(self, "sigil_table"):
            return
        idx = self.sigil_table.currentIndex()
        row_index = idx.row() if idx.isValid() else -1
        if row_index < 0:
            return
        current_owner = self._current_sigil_owner_hash()
        if current_owner == owner_hash:
            return
        edit_value = "" if owner_hash in (0, EMPTY_HASH) else f"0x{owner_hash:08X}"
        if self.apply_sigil_table_cell_edit(row_index, 6, edit_value):
            # Keep the detail panel in sync without requiring the Apply button.
            self.update_sigil_detail()
            self.statusBar().showMessage("Equipped To updated in memory. Save when ready.", 5000)
        else:
            # Restore the dropdown to the selected row if the unsafe equip write
            # was blocked.
            self.update_sigil_detail()

    def validate_sigil_owners(self, show_message: bool = True) -> List[str]:
        issues: List[str] = []
        if not self.save:
            if show_message:
                QMessageBox.information(self, "No save loaded", "Open a save first.")
            return issues
        valid = {int(c.get("hash", 0)) & 0xFFFFFFFF for c in getattr(self, "character_owner_choices", [])}
        valid.update({0, EMPTY_HASH})
        grouped = self.save.group_by_unit([2703, 2706])
        for unit_id, fields in sorted(grouped.items()):
            gem = self.value1(fields.get(2703), 0)
            owner = self.value1(fields.get(2706), 0)
            try:
                gem_i = int(gem or 0) & 0xFFFFFFFF
                owner_i = int(owner or 0) & 0xFFFFFFFF
            except Exception:
                continue
            if gem_i in (0, EMPTY_HASH) or owner_i in (0, EMPTY_HASH):
                continue
            if owner_i not in valid:
                sigil_name, _, _ = self.hash_entry_parts(gem_i)
                issues.append(f"Unit {unit_id}: {sigil_name} equipped to unknown owner 0x{owner_i:08X}")
        if show_message:
            if issues:
                QMessageBox.warning(self, "Sigil owner validation", "Unknown equipped-owner references found:\n\n" + "\n".join(issues[:40]))
            else:
                QMessageBox.information(self, "Sigil owner validation", "No invalid sigil owner references found.")
        return issues

    def _sigils_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        header = QLabel("Sigils")
        header.setObjectName("pageHeader")
        layout.addWidget(header)

        help_text = QLabel(
            "Edit current sigils, view reusable empty slots, and add new sigils from the built-in database. "
            "Sigil ID is 2703 / FF8F0A and level is 2704 / FF900A. Equip writes stay guarded until the full in-game relation is mapped."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        self.sigil_count_label = QLabel("Open a save to inspect sigil slots.")
        self.sigil_count_label.setWordWrap(True)
        self.sigil_count_label.setObjectName("subtleText")
        layout.addWidget(self.sigil_count_label)

        self.sigil_tabs = QTabWidget()
        self.sigil_tabs.setObjectName("editorTabs")
        self.sigil_tabs.currentChanged.connect(lambda *_: self._refresh_current_sigil_tab())

        current_tab = QWidget()
        current_layout = QVBoxLayout(current_tab)
        current_layout.setContentsMargins(12, 12, 12, 12)
        current_layout.setSpacing(10)

        self.sigil_filter_edit = QLineEdit()
        self.sigil_filter_edit.setPlaceholderText("Filter current sigils by name, GBID, hash, level, worn-by, flags, or unit id...")
        self.sigil_filter_edit.textChanged.connect(lambda _: self.refresh_sigil_rows())
        current_layout.addWidget(self.sigil_filter_edit)

        sigil_filter_row = QHBoxLayout()
        self.sigil_show_empty_check = QCheckBox("Show empty slots")
        self.sigil_known_only_check = QCheckBox("Known only")
        self.sigil_unknown_only_check = QCheckBox("Unknown only")
        self.sigil_invalid_owner_only_check = QCheckBox("Invalid owner only")
        self.sigil_show_technical_check = QCheckBox("Technical columns")
        self.sigil_known_only_check.setToolTip("Show only slots whose sigil hash resolves to a known GBID/name.")
        self.sigil_unknown_only_check.setToolTip("Show only non-empty sigil slots whose hash is not in the database yet.")
        self.sigil_invalid_owner_only_check.setToolTip("Show equipped sigils whose owner hash is not one of the known character hashes.")
        self.sigil_show_technical_check.setToolTip("Show Unit, GBID, raw Hash, and Equipped GBID columns. Leave this off for normal editing.")
        self.sigil_known_only_check.toggled.connect(lambda checked: self._sync_known_unknown_filter(checked, self.sigil_unknown_only_check, self.refresh_sigil_rows))
        self.sigil_unknown_only_check.toggled.connect(lambda checked: self._sync_known_unknown_filter(checked, self.sigil_known_only_check, self.refresh_sigil_rows))
        self.sigil_show_technical_check.toggled.connect(lambda _=False: self._apply_sigil_column_visibility())
        self.sigil_invalid_owner_only_check.toggled.connect(lambda _=False: self.refresh_sigil_rows())
        self.sigil_show_empty_check.toggled.connect(lambda _=False: self.refresh_sigil_rows())
        sigil_filter_row.addWidget(self._make_filter_button("Filters", [
            ("Show empty slots", self.sigil_show_empty_check),
            ("Known only", self.sigil_known_only_check),
            ("Unknown only", self.sigil_unknown_only_check),
            ("Invalid owner only", self.sigil_invalid_owner_only_check),
            ("Technical columns", self.sigil_show_technical_check),
        ]))
        for text, slot in [
            ("Show Unknown", self.show_unknown_sigils),
            ("Clear", self.clear_sigil_filters),
            ("Validate Owners", self.validate_sigil_owners),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            sigil_filter_row.addWidget(btn)
        sigil_filter_row.addWidget(QLabel("Tip: add new sigils from the Database tab; empty reusable slots are listed separately."))
        sigil_filter_row.addStretch(1)
        current_layout.addLayout(sigil_filter_row)

        self.sigil_table = QTableView()
        self.sigil_table.setModel(self.sigil_model)
        self._table_clean(self.sigil_table, hidden_columns=(0, 3, 4, 7))
        self.sigil_table.verticalHeader().setDefaultSectionSize(30 if getattr(self, "compact_mode", True) else 36)
        self.sigil_table.setFont(QFont("Segoe UI", 10))
        self._apply_sigil_column_visibility()
        self.sigil_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.sigil_table.selectionModel().selectionChanged.connect(lambda *_: self.update_sigil_detail())
        self.sigil_table.doubleClicked.connect(lambda _: self.edit_selected_sigil_level())
        current_layout.addWidget(self.sigil_table, 1)

        detail = make_card("Selected Sigil · Inline Editor")
        self._set_compact_detail(detail, max_height=218)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setSpacing(5)
        self.sigil_detail_label = QPlainTextEdit()
        self.sigil_detail_label.setReadOnly(True)
        self.sigil_detail_label.setMinimumHeight(0)
        self.sigil_detail_label.setMaximumHeight(0)
        self.sigil_detail_label.setVisible(False)
        self.sigil_detail_label.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.sigil_detail_label.setPlainText("Select a sigil row, then edit the sigil, level, equipped character, and flags here without a dialog.")
        self.sigil_detail_label.setObjectName("detailText")
        self.sigil_detail_label.setFont(QFont("Consolas", 10))
        self.sigil_detail_label.setStyleSheet("QPlainTextEdit#detailText { padding: 10px; }")
        self.sigil_detail_label.setToolTip("Read-only selected sigil summary.")
        detail_layout.addWidget(self.sigil_detail_label)

        sigil_grid = QGridLayout()
        sigil_grid.setHorizontalSpacing(10)
        sigil_grid.setVerticalSpacing(4)
        self.sigil_identity_edit = QLineEdit(); self.sigil_identity_edit.setPlaceholderText("GBID, sigil name, decimal hash, or 0xHASH")
        self.sigil_level_edit = QLineEdit(); self.sigil_level_edit.setPlaceholderText("Level")
        self.sigil_level_edit.setMinimumWidth(140)
        self.sigil_worn_by_combo = QComboBox()
        self.sigil_worn_by_combo.setMinimumHeight(32)
        self.sigil_worn_by_combo.setMinimumWidth(380)
        self.sigil_worn_by_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sigil_worn_by_combo.setMinimumContentsLength(24)
        self.sigil_worn_by_combo.setToolTip("Equip writes are disabled until the full in-game equip relation is mapped. Use Unequip to clear unsafe owner refs.")
        for choice in getattr(self, "character_owner_choices", []):
            self.sigil_worn_by_combo.addItem(str(choice.get("label", "")), int(choice.get("hash", EMPTY_HASH)) & 0xFFFFFFFF)
        self.sigil_worn_by_combo.currentIndexChanged.connect(self._sigil_owner_combo_changed)
        self.sigil_worn_by_edit = QLineEdit(); self.sigil_worn_by_edit.setPlaceholderText("Raw owner hash / GBID fallback for unknown characters")
        self.sigil_worn_by_edit.setMinimumHeight(32)
        self.sigil_flags_edit = QLineEdit(); self.sigil_flags_edit.setPlaceholderText("Flags / lock state")
        self.sigil_flags_edit.setMinimumWidth(180)
        for editor in (self.sigil_identity_edit, self.sigil_level_edit, self.sigil_worn_by_edit, self.sigil_flags_edit):
            editor.setMinimumHeight(32)
            editor.setFont(QFont("Segoe UI", 10))
            editor.returnPressed.connect(self.apply_sigil_inline_edits)
        sigil_grid.addWidget(QLabel("Sigil / GBID / Hash"), 0, 0)
        sigil_grid.addWidget(self.sigil_identity_edit, 0, 1, 1, 3)
        sigil_grid.addWidget(QLabel("Level"), 1, 0)
        sigil_grid.addWidget(self.sigil_level_edit, 1, 1)
        sigil_grid.addWidget(QLabel("Equipped To"), 1, 2)
        sigil_grid.addWidget(self.sigil_worn_by_combo, 1, 3)
        sigil_grid.addWidget(QLabel("Raw owner"), 2, 0)
        sigil_grid.addWidget(self.sigil_worn_by_edit, 2, 1)
        sigil_grid.addWidget(QLabel("Flags"), 2, 2)
        sigil_grid.addWidget(self.sigil_flags_edit, 2, 3)
        sigil_grid.setColumnStretch(1, 2)
        sigil_grid.setColumnStretch(3, 3)
        detail_layout.addLayout(sigil_grid)

        sigil_inline_row = QHBoxLayout()
        for text, slot in [
            ("Apply Changes", self.apply_sigil_inline_edits),
            ("Max + Lock", self.max_selected_sigil),
            ("Unequip", self.clear_selected_sigil_worn_by),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); sigil_inline_row.addWidget(btn)
        sigil_inline_row.addStretch(1)
        detail_layout.addLayout(sigil_inline_row)
        current_layout.addWidget(detail)

        row = QHBoxLayout()
        for text, slot in [
            ("Add Sigil", self.add_sigil_to_empty_slot),
            ("Show Empty Slots", lambda _=False: self._show_sigil_tab(2)),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addWidget(self._make_more_button("More", [
            ("Change Selected Sigil", self.edit_selected_sigil_hash),
            ("Lock Selected", lambda: self.set_selected_sigil_lock(True)),
            ("Unlock Selected", lambda: self.set_selected_sigil_lock(False)),
            ("Batch Add Sigils From Text", self.batch_add_sigils_to_empty_slots),
            ("Duplicate Sigil to Empty Slot", self.duplicate_selected_sigil_to_empty_slot),
            ("Copy Sigil Slot", self.copy_selected_sigil_slot),
            ("Paste Sigil Slot", self.paste_sigil_slot_to_selected),
            ("Swap With Copied Sigil Slot", self.swap_selected_sigil_with_copied),
            ("Max Visible Levels", self.bulk_set_visible_sigil_level),
            ("Max Visible + Lock", self.max_visible_sigils),
            ("Set Worn By", self.edit_selected_sigil_worn_by),
            ("Repair Added Sigil Slots", self.repair_added_sigil_slots),
            ("Unequip / Clear Worn By", self.clear_selected_sigil_worn_by),
            ("Jump to Raw Unit", self.jump_to_sigil_unit),
            ("Copy Hash", self.copy_selected_sigil_hash),
            ("Copy GBID", self.copy_selected_sigil_gbid),
            ("Copy Visible Unknown Hashes", self.copy_visible_unknown_sigil_hashes),
            ("Export Unknown Hashes", self.export_unknown_sigil_hashes_csv),
            ("Explain Unknown Sigils", self.explain_unknown_sigils),
            ("Export CSV", self.export_sigils_csv),
        ]))
        row.addStretch(1)
        current_layout.addLayout(row)

        database_tab = QWidget()
        db_layout = QVBoxLayout(database_tab)
        db_layout.setContentsMargins(12, 12, 12, 12)
        db_layout.setSpacing(10)
        db_help = QLabel("Add new sigils from the built-in sigil database into reusable empty 2703/2704 slots. Database rows are named and filtered; unknown/raw research rows stay out of the normal workflow.")
        db_help.setWordWrap(True)
        db_help.setObjectName("helpText")
        db_layout.addWidget(db_help)
        db_tools = QHBoxLayout()
        self.sigil_database_filter_edit = QLineEdit()
        self.sigil_database_filter_edit.setPlaceholderText("Search sigil database by name, GBID, hash, family, V, V+, Damage Cap, Supplementary...")
        self.sigil_database_filter_edit.textChanged.connect(lambda *_: self.refresh_sigil_database_rows())
        db_tools.addWidget(self.sigil_database_filter_edit, 3)
        self.sigil_database_grade_combo = QComboBox()
        self.sigil_database_grade_combo.addItems(["All sigils", "V / V+ only", "V only", "V+ only", "Missing only", "Owned only"])
        self.sigil_database_grade_combo.currentTextChanged.connect(lambda *_: self.refresh_sigil_database_rows())
        db_tools.addWidget(self.sigil_database_grade_combo, 1)
        self.sigil_database_level_spin = QSpinBox()
        self.sigil_database_level_spin.setRange(1, SIGIL_LEVEL_MAX)
        self.sigil_database_level_spin.setValue(SIGIL_LEVEL_MAX)
        self.sigil_database_level_spin.setMinimumWidth(130)
        db_tools.addWidget(QLabel("Level"))
        db_tools.addWidget(self.sigil_database_level_spin)
        self.sigil_database_locked_check = QCheckBox("Locked")
        self.sigil_database_locked_check.setChecked(True)
        db_tools.addWidget(self.sigil_database_locked_check)
        refresh_db_btn = QPushButton("Refresh")
        refresh_db_btn.clicked.connect(self.refresh_sigil_database_rows)
        db_tools.addWidget(refresh_db_btn)
        db_layout.addLayout(db_tools)

        self.sigil_database_status = QLabel("Open a save to add sigils, or browse the built-in sigil database.")
        self.sigil_database_status.setObjectName("subtleText")
        self.sigil_database_status.setWordWrap(True)
        db_layout.addWidget(self.sigil_database_status)

        self.sigil_database_table = QTableView()
        self.sigil_database_table.setModel(self.sigil_database_model)
        self._table_clean(self.sigil_database_table)
        self.sigil_database_table.setMinimumHeight(420)
        self.sigil_database_table.selectionModel().selectionChanged.connect(lambda *_: self.update_sigil_database_status())
        self.sigil_database_table.doubleClicked.connect(lambda *_: self.add_selected_database_sigil_to_empty_slot())
        db_layout.addWidget(self.sigil_database_table, 1)

        db_actions = QHBoxLayout()
        for text, slot in [
            ("Add Selected", self.add_selected_database_sigil_to_empty_slot),
            ("Add Selected Locked", self.add_selected_database_sigil_locked_to_empty_slot),
            ("Add Selected Unlocked", self.add_selected_database_sigil_unlocked_to_empty_slot),
            ("Batch Add From Text", self.batch_add_sigils_to_empty_slots),
            ("Show Empty Slots", lambda _=False: self._show_sigil_tab(2)),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); db_actions.addWidget(btn)
        db_actions.addStretch(1)
        db_layout.addLayout(db_actions)

        empty_tab = QWidget()
        empty_layout = QVBoxLayout(empty_tab)
        empty_layout.setContentsMargins(12, 12, 12, 12)
        empty_layout.setSpacing(10)
        self.sigil_empty_status = QLabel("Open a save to list reusable empty sigil slots.")
        self.sigil_empty_status.setObjectName("subtleText")
        self.sigil_empty_status.setWordWrap(True)
        empty_layout.addWidget(self.sigil_empty_status)
        self.sigil_empty_table = QTableView()
        self.sigil_empty_table.setModel(self.sigil_empty_model)
        self._table_clean(self.sigil_empty_table)
        self.sigil_empty_table.setMinimumHeight(430)
        empty_layout.addWidget(self.sigil_empty_table, 1)
        empty_actions = QHBoxLayout()
        for text, slot in [
            ("Refresh Empty Slots", self.refresh_sigil_empty_slot_rows),
            ("Open Database", lambda _=False: self._show_sigil_tab(1)),
            ("Show Empty In Current Table", self.show_empty_sigils_in_current_table),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); empty_actions.addWidget(btn)
        empty_actions.addStretch(1)
        empty_layout.addLayout(empty_actions)

        self.sigil_tabs.addTab(current_tab, "Current Sigils")
        self.sigil_tabs.addTab(database_tab, "Database / Add")
        self.sigil_tabs.addTab(empty_tab, "Empty Slots")
        layout.addWidget(self.sigil_tabs, 1)
        self._install_common_numeric_validators()
        return page


    def _weapons_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        header = QLabel("Weapons")
        header.setObjectName("pageHeader")
        layout.addWidget(header)

        help_text = QLabel(
            "Modern weapon slot editor. Edit current weapons, add from the weapon database into reusable empty slots, or inspect empty slots directly. "
            f"Max weapon XP/progress is {WEAPON_XP_MAX:,}."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        self.weapon_count_label = QLabel("Open a save to inspect weapon slots.")
        self.weapon_count_label.setWordWrap(True)
        self.weapon_count_label.setObjectName("subtleText")
        layout.addWidget(self.weapon_count_label)

        self.weapon_tabs = QTabWidget()
        self.weapon_tabs.setObjectName("editorTabs")
        self.weapon_tabs.currentChanged.connect(lambda *_: self._refresh_current_weapon_tab())

        current_tab = QWidget()
        current_layout = QVBoxLayout(current_tab)
        current_layout.setContentsMargins(12, 12, 12, 12)
        current_layout.setSpacing(10)

        self.weapon_filter_edit = QLineEdit()
        self.weapon_filter_edit.setPlaceholderText("Filter current weapons by name, GBID, hash, XP, stone, flags, or unit id...")
        self.weapon_filter_edit.textChanged.connect(lambda _: self.refresh_weapon_rows())
        current_layout.addWidget(self.weapon_filter_edit)

        weapon_filter_row = QHBoxLayout()
        self.weapon_show_empty_check = QCheckBox("Show empty slots")
        self.weapon_known_only_check = QCheckBox("Known only")
        self.weapon_unknown_only_check = QCheckBox("Unknown only")
        self.weapon_known_only_check.setToolTip("Show only slots whose weapon hash resolves to a known GBID/name.")
        self.weapon_unknown_only_check.setToolTip("Show only non-empty weapon slots whose hash is not in the database yet.")
        self.weapon_known_only_check.toggled.connect(lambda checked: self._sync_known_unknown_filter(checked, self.weapon_unknown_only_check, self.refresh_weapon_rows))
        self.weapon_unknown_only_check.toggled.connect(lambda checked: self._sync_known_unknown_filter(checked, self.weapon_known_only_check, self.refresh_weapon_rows))
        self.weapon_show_empty_check.toggled.connect(lambda _=False: self.refresh_weapon_rows())
        weapon_filter_row.addWidget(self._make_filter_button("Filters", [
            ("Show empty slots", self.weapon_show_empty_check),
            ("Known only", self.weapon_known_only_check),
            ("Unknown only", self.weapon_unknown_only_check),
        ]))
        for text, slot in [
            ("Show Empty Slots", lambda _=False: self._show_weapon_tab(2)),
            ("Clear", self.clear_weapon_filters),
            ("Max Visible", self.max_visible_weapons),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            weapon_filter_row.addWidget(btn)
        weapon_filter_row.addWidget(QLabel("Tip: add new weapons from the Database tab; double-click XP to edit."))
        weapon_filter_row.addStretch(1)
        current_layout.addLayout(weapon_filter_row)

        self.weapon_table = QTableView()
        self.weapon_table.setModel(self.weapon_model)
        self._table_clean(self.weapon_table, hidden_columns=(0, 3, 5, 6, 7, 8, 9))
        self.weapon_table.verticalHeader().setDefaultSectionSize(30 if getattr(self, "compact_mode", True) else 36)
        self.weapon_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.weapon_table.selectionModel().selectionChanged.connect(lambda *_: self.update_weapon_detail())
        self.weapon_table.doubleClicked.connect(lambda _: self.edit_selected_weapon_xp())
        current_layout.addWidget(self.weapon_table, 1)

        detail = make_card("Selected Weapon")
        self._set_compact_detail(detail, max_height=226)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setSpacing(6)

        self.weapon_detail_label = QPlainTextEdit()
        self.weapon_detail_label.setReadOnly(True)
        self.weapon_detail_label.setObjectName("summaryBox")
        self.weapon_detail_label.setMinimumHeight(0)
        self.weapon_detail_label.setMaximumHeight(0)
        self.weapon_detail_label.setVisible(False)
        self.weapon_detail_label.setPlainText("Select a weapon row, then edit weapon, XP, imbued stone, and flags here without a dialog.")
        detail_layout.addWidget(self.weapon_detail_label)

        weapon_grid = QGridLayout()
        weapon_grid.setHorizontalSpacing(10)
        weapon_grid.setVerticalSpacing(4)
        self.weapon_identity_edit = QLineEdit(); self.weapon_identity_edit.setPlaceholderText("Weapon GBID/name/hash")
        self.weapon_xp_edit = QLineEdit(); self.weapon_xp_edit.setPlaceholderText("XP / progress")
        self.weapon_stone_edit = QLineEdit(); self.weapon_stone_edit.setPlaceholderText("Stone GBID/name/hash, blank/0 to clear")
        self.weapon_flags_edit = QLineEdit(); self.weapon_flags_edit.setPlaceholderText("Flags")
        for editor in (self.weapon_identity_edit, self.weapon_xp_edit, self.weapon_stone_edit, self.weapon_flags_edit):
            editor.setMinimumHeight(32)
            editor.returnPressed.connect(self.apply_weapon_inline_edits)
        weapon_grid.addWidget(QLabel("Weapon"), 0, 0)
        weapon_grid.addWidget(self.weapon_identity_edit, 0, 1, 1, 3)
        weapon_grid.addWidget(QLabel("XP"), 1, 0)
        weapon_grid.addWidget(self.weapon_xp_edit, 1, 1)
        weapon_grid.addWidget(QLabel("Stone"), 1, 2)
        weapon_grid.addWidget(self.weapon_stone_edit, 1, 3)
        weapon_grid.addWidget(QLabel("Flags"), 2, 0)
        weapon_grid.addWidget(self.weapon_flags_edit, 2, 1)
        weapon_grid.setColumnStretch(1, 2)
        weapon_grid.setColumnStretch(3, 2)
        detail_layout.addLayout(weapon_grid)

        weapon_inline_row = QHBoxLayout()
        for text, slot in [
            ("Apply Changes", self.apply_weapon_inline_edits),
            ("Max Selected", self.max_selected_weapon),
            ("Max All", self.max_all_weapons),
            ("Clear Stone", self.clear_selected_weapon_stone),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); weapon_inline_row.addWidget(btn)
        weapon_inline_row.addStretch(1)
        detail_layout.addLayout(weapon_inline_row)
        current_layout.addWidget(detail)

        row = QHBoxLayout()
        for text, slot in [
            ("Add Weapon", self.add_weapon_to_empty_slot),
            ("Duplicate", self.duplicate_selected_weapon_to_empty_slot),
            ("Open Database", lambda _=False: self._show_weapon_tab(1)),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addWidget(self._make_more_button("More", [
            ("Change Selected Weapon", self.edit_selected_weapon_hash),
            ("Set Stone", self.edit_selected_weapon_stone),
            ("Clear Stone", self.clear_selected_weapon_stone),
            ("Batch Add Weapons From Text", self.batch_add_weapons_to_empty_slots),
            ("Duplicate Weapon to Empty Slot", self.duplicate_selected_weapon_to_empty_slot),
            ("Copy Weapon Slot", self.copy_selected_weapon_slot),
            ("Paste Weapon Slot", self.paste_weapon_slot_to_selected),
            ("Swap With Copied Weapon Slot", self.swap_selected_weapon_with_copied),
            ("Set Visible XP", self.bulk_set_visible_weapon_xp),
            ("Max Visible Weapons", self.max_visible_weapons),
            ("Max All Weapons", self.max_all_weapons),
            ("Set Flags", self.edit_selected_weapon_flags),
            ("Jump to Raw Unit", self.jump_to_weapon_unit),
            ("Copy Hash", self.copy_selected_weapon_hash),
            ("Copy GBID", self.copy_selected_weapon_gbid),
            ("Export CSV", self.export_weapons_csv),
            ("Export Unknown Hashes", self.export_unknown_weapon_hashes_csv),
        ]))
        row.addStretch(1)
        current_layout.addLayout(row)

        database_tab = QWidget()
        db_layout = QVBoxLayout(database_tab)
        db_layout.setContentsMargins(12, 12, 12, 12)
        db_layout.setSpacing(10)
        db_help = QLabel("Add weapons from the built-in database into reusable empty 2803/2804 weapon slots. This does not resize/rebuild the save.")
        db_help.setWordWrap(True)
        db_help.setObjectName("helpText")
        db_layout.addWidget(db_help)

        db_tools = QHBoxLayout()
        self.weapon_database_filter_edit = QLineEdit()
        self.weapon_database_filter_edit.setPlaceholderText("Search weapon database by name, GBID, hash, owner prefix, WEP_PL0000, Eos, Terminus...")
        self.weapon_database_filter_edit.textChanged.connect(lambda *_: self.refresh_weapon_database_rows())
        db_tools.addWidget(self.weapon_database_filter_edit, 3)

        self.weapon_database_filter_combo = QComboBox()
        self.weapon_database_filter_combo.addItems(["All weapons", "Missing only", "Owned only", "Playable WEP_PL only", "NPC / Reserved WEP_NP"])
        self.weapon_database_filter_combo.currentTextChanged.connect(lambda *_: self.refresh_weapon_database_rows())
        db_tools.addWidget(self.weapon_database_filter_combo, 1)

        self.weapon_database_xp_spin = QSpinBox()
        self.weapon_database_xp_spin.setRange(0, WEAPON_XP_MAX)
        self.weapon_database_xp_spin.setValue(WEAPON_XP_MAX)
        self.weapon_database_xp_spin.setMinimumWidth(145)
        db_tools.addWidget(QLabel("XP"))
        db_tools.addWidget(self.weapon_database_xp_spin)

        refresh_db_btn = QPushButton("Refresh")
        refresh_db_btn.clicked.connect(self.refresh_weapon_database_rows)
        db_tools.addWidget(refresh_db_btn)
        db_layout.addLayout(db_tools)

        self.weapon_database_status = QLabel("Open a save to add weapons, or browse the built-in weapon database.")
        self.weapon_database_status.setObjectName("subtleText")
        self.weapon_database_status.setWordWrap(True)
        db_layout.addWidget(self.weapon_database_status)

        self.weapon_database_table = QTableView()
        self.weapon_database_table.setModel(self.weapon_database_model)
        self._table_clean(self.weapon_database_table)
        self.weapon_database_table.setMinimumHeight(420)
        self.weapon_database_table.selectionModel().selectionChanged.connect(lambda *_: self.update_weapon_database_status())
        self.weapon_database_table.doubleClicked.connect(lambda *_: self.add_selected_database_weapon_to_empty_slot())
        db_layout.addWidget(self.weapon_database_table, 1)

        db_actions = QHBoxLayout()
        for text, slot in [
            ("Add Selected", self.add_selected_database_weapon_to_empty_slot),
            ("Add Selected Max XP", self.add_selected_database_weapon_max_to_empty_slot),
            ("Add All Missing", self.add_all_missing_database_weapons_to_empty_slots),
            ("Batch Add From Text", self.batch_add_weapons_to_empty_slots),
            ("Show Empty Slots", lambda _=False: self._show_weapon_tab(2)),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); db_actions.addWidget(btn)
        db_actions.addStretch(1)
        db_layout.addLayout(db_actions)

        empty_tab = QWidget()
        empty_layout = QVBoxLayout(empty_tab)
        empty_layout.setContentsMargins(12, 12, 12, 12)
        empty_layout.setSpacing(10)
        self.weapon_empty_status = QLabel("Open a save to list reusable empty weapon slots.")
        self.weapon_empty_status.setObjectName("subtleText")
        self.weapon_empty_status.setWordWrap(True)
        empty_layout.addWidget(self.weapon_empty_status)
        self.weapon_empty_table = QTableView()
        self.weapon_empty_table.setModel(self.weapon_empty_model)
        self._table_clean(self.weapon_empty_table)
        self.weapon_empty_table.setMinimumHeight(430)
        empty_layout.addWidget(self.weapon_empty_table, 1)
        empty_actions = QHBoxLayout()
        for text, slot in [
            ("Refresh Empty Slots", self.refresh_weapon_empty_slot_rows),
            ("Open Database", lambda _=False: self._show_weapon_tab(1)),
            ("Show Empty In Current Table", self.show_empty_weapons_in_current_table),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); empty_actions.addWidget(btn)
        empty_actions.addStretch(1)
        empty_layout.addLayout(empty_actions)

        self.weapon_tabs.addTab(current_tab, "Current Weapons")
        self.weapon_tabs.addTab(database_tab, "Database / Add")
        self.weapon_tabs.addTab(empty_tab, "Empty Slots")
        layout.addWidget(self.weapon_tabs, 1)
        self._install_common_numeric_validators()
        return page


    def _characters_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Characters Editor")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Character slot view. Common safe edits are changing the character hash, setting level, maxing visible levels, and swapping full character slot fields. Save As first when swapping.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.character_count_label = QLabel("Open a save to inspect character slots.")
        self.character_count_label.setWordWrap(True)
        self.character_count_label.setObjectName("subtleText")
        layout.addWidget(self.character_count_label)
        self.character_filter_edit = QLineEdit()
        self.character_filter_edit.setPlaceholderText("Filter characters by name, GBID, hash, level, slot, or unit id...")
        self.character_filter_edit.textChanged.connect(lambda _: self.refresh_character_rows())
        layout.addWidget(self.character_filter_edit)
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Tip: double-click editable cells for character, level, or state."))
        filter_row.addStretch(1)
        layout.addLayout(filter_row)
        self.character_table = QTableView()
        self.character_table.setModel(self.character_model)
        self._table_clean(self.character_table, hidden_columns=(3, 6))
        self.character_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.character_table.selectionModel().selectionChanged.connect(lambda *_: self.update_character_detail())
        self.character_table.doubleClicked.connect(lambda _: self.update_character_detail())
        layout.addWidget(self.character_table, 1)
        detail = make_card("Selected Character")
        self._set_compact_detail(detail, max_height=272)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setSpacing(10)

        self.character_detail_title = QLabel("Select a character row")
        self.character_detail_title.setObjectName("sectionTitle")
        self.character_detail_title.setWordWrap(True)
        detail_layout.addWidget(self.character_detail_title)

        self.character_detail_label = QLabel("Pick a row above. Level, EXP, unlock, and state controls sync immediately when changed.")
        self.character_detail_label.setWordWrap(True)
        self.character_detail_label.setObjectName("subtleText")
        detail_layout.addWidget(self.character_detail_label)

        control_grid = QGridLayout()
        control_grid.setHorizontalSpacing(12)
        control_grid.setVerticalSpacing(8)
        self.character_level_spin = QSpinBox(); self.character_level_spin.setRange(0, CHARACTER_VALUE_MAX); self.character_level_spin.setButtonSymbols(QSpinBox.ButtonSymbols.PlusMinus)
        self.character_exp_spin = QSpinBox(); self.character_exp_spin.setRange(0, CHARACTER_VALUE_MAX); self.character_exp_spin.setButtonSymbols(QSpinBox.ButtonSymbols.PlusMinus)
        self.character_unlock_spin = QSpinBox(); self.character_unlock_spin.setRange(0, CHARACTER_VALUE_MAX); self.character_unlock_spin.setButtonSymbols(QSpinBox.ButtonSymbols.PlusMinus)
        self.character_state_spin = QSpinBox(); self.character_state_spin.setRange(0, CHARACTER_VALUE_MAX); self.character_state_spin.setButtonSymbols(QSpinBox.ButtonSymbols.PlusMinus)
        for spin in (self.character_level_spin, self.character_exp_spin, self.character_unlock_spin, self.character_state_spin):
            spin.setMinimumHeight(34)
            spin.valueChanged.connect(self.sync_character_detail_controls)
        control_grid.addWidget(QLabel("Level"), 0, 0)
        control_grid.addWidget(self.character_level_spin, 0, 1)
        control_grid.addWidget(QLabel("EXP / Progress"), 0, 2)
        control_grid.addWidget(self.character_exp_spin, 0, 3)
        control_grid.addWidget(QLabel("Unlock / Active"), 1, 0)
        control_grid.addWidget(self.character_unlock_spin, 1, 1)
        control_grid.addWidget(QLabel("State / Flags"), 1, 2)
        control_grid.addWidget(self.character_state_spin, 1, 3)
        detail_layout.addLayout(control_grid)

        character_inline_row = QHBoxLayout()
        for text, slot in [
            ("Max Selected", self.max_selected_character_level),
            ("Max All", self.max_all_character_levels),
            ("Equip Copied Sigil/Gem", self.equip_copied_sigil_to_selected_character),
            ("Change Character", self.edit_selected_character_hash),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); character_inline_row.addWidget(btn)
        character_inline_row.addStretch(1)
        detail_layout.addLayout(character_inline_row)
        layout.addWidget(detail)



        row = QHBoxLayout()
        for text, slot in [
            ("Copy Slot", self.copy_selected_character_slot),
            ("Paste Slot", self.paste_character_slot_to_selected),
            ("Swap With Copied", self.swap_selected_character_with_copied),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addWidget(self._make_more_button("More", [
            ("Max Visible Character Levels", self.max_visible_character_levels),
            ("Max All Character Levels", self.max_all_character_levels),
            ("Copy Character Slot", self.copy_selected_character_slot),
            ("Paste Character Slot", self.paste_character_slot_to_selected),
            ("Swap With Copied Character Slot", self.swap_selected_character_with_copied),
            ("Copy RNG/Overmastery Slots", self.copy_selected_character_overmastery),
            ("Paste RNG/Overmastery Slots", self.paste_selected_character_overmastery),
            ("Equip Copied Sigil/Gem Here", self.equip_copied_sigil_to_selected_character),
            ("Set RNG/Overmastery Slots Raw", self.edit_selected_character_overmastery),
            ("Clear RNG/Overmastery Slots", self.clear_selected_character_overmastery),
            ("Jump to Raw Unit", self.jump_to_character_unit),
            ("Export CSV", self.export_characters_csv),
        ]))
        row.addStretch(1)
        layout.addLayout(row)
        return page


    def _mastery_slots_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Masteries")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Mastery data is split into readable groups. Mastery Effects are the per-slot 1606/1607 effect rows, "
            "Board Slot Keys are the 1601 layout rows, and Overmastery Slots are the four high-impact OP rows. "
            "The OP preset buttons are experimental and are based on the before/after saves plus the Save Wizard Cheats_Mods / Masteries_SlotINFO notes you shared. Use Save As before testing. Sigil equipment is handled on the Sigils page."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        mastery_card = make_card("Mastery Editor")
        mastery_layout = QVBoxLayout(mastery_card)
        mastery_controls = QHBoxLayout()
        self.mastery_character_combo = QComboBox()
        self.mastery_character_combo.setMinimumWidth(260)
        self.mastery_character_combo.currentIndexChanged.connect(lambda *_: self.refresh_mastery_slot_rows())
        self.mastery_mode_combo = QComboBox()
        self.mastery_mode_combo.addItem("Mastery Effects", "effects")
        self.mastery_mode_combo.addItem("Board Slot Keys", "board")
        self.mastery_mode_combo.addItem("Overmastery Slots", "overmastery")
        self.mastery_mode_combo.setMinimumWidth(190)
        self.mastery_mode_combo.currentIndexChanged.connect(lambda *_: self.refresh_mastery_slot_rows())
        self.mastery_slot_filter_edit = QLineEdit()
        self.mastery_slot_filter_edit.setPlaceholderText("Search slot, effect name, GBID, or hash...")
        self._connect_debounced_text_changed(self.mastery_slot_filter_edit, "mastery_slots", self.refresh_mastery_slot_rows, 180)
        mastery_controls.addWidget(QLabel("Character"))
        mastery_controls.addWidget(self.mastery_character_combo)
        mastery_controls.addWidget(QLabel("View"))
        mastery_controls.addWidget(self.mastery_mode_combo)
        mastery_controls.addWidget(self.mastery_slot_filter_edit, 1)
        for text, slot in [
            ("Use Selected Character", self.use_selected_character_for_mastery_slots),
            ("Refresh", self.refresh_mastery_slot_rows),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); mastery_controls.addWidget(btn)
        mastery_layout.addLayout(mastery_controls)
        self.mastery_slot_summary_label = QLabel("Select a character to load mastery rows.")
        self.mastery_slot_summary_label.setObjectName("subtleText")
        mastery_layout.addWidget(self.mastery_slot_summary_label)
        self.mastery_slot_table = QTableView()
        self.mastery_slot_table.setModel(self.mastery_slot_model)
        self._table_clean(self.mastery_slot_table)
        self.mastery_slot_table.setMinimumHeight(390)
        self.mastery_slot_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mastery_slot_table.selectionModel().selectionChanged.connect(lambda *_: self.update_mastery_slot_detail())
        self.mastery_slot_table.doubleClicked.connect(lambda *_: self.apply_mastery_slot_inline_edits())
        mastery_layout.addWidget(self.mastery_slot_table, 1)

        selected_card = make_card("Selected Mastery Row")
        selected_layout = QVBoxLayout(selected_card)
        self.mastery_slot_detail_label = QLabel("Select a mastery row to see its effect, slot key, unit formula, and editable values.")
        self.mastery_slot_detail_label.setWordWrap(True)
        self.mastery_slot_detail_label.setObjectName("subtleText")
        selected_layout.addWidget(self.mastery_slot_detail_label)
        mastery_edit_grid = QGridLayout()
        mastery_edit_grid.setHorizontalSpacing(10)
        mastery_edit_grid.setVerticalSpacing(8)
        self.mastery_slotinfo_edit = QLineEdit(); self.mastery_slotinfo_edit.setPlaceholderText("Board slot key / 1601, e.g. 0x280B6CB0")
        self.mastery_id_edit = QLineEdit(); self.mastery_id_edit.setPlaceholderText("Mastery effect / 1606 name, GBID, or hash")
        self.mastery_state_edit = QLineEdit(); self.mastery_state_edit.setPlaceholderText("Active state / 1607, usually 0 or 1")
        for editor in (self.mastery_slotinfo_edit, self.mastery_id_edit, self.mastery_state_edit):
            editor.setMinimumHeight(30)
            editor.returnPressed.connect(self.apply_mastery_slot_inline_edits)
        mastery_edit_grid.addWidget(QLabel("Slot Key / 1601"), 0, 0)
        mastery_edit_grid.addWidget(self.mastery_slotinfo_edit, 0, 1)
        mastery_edit_grid.addWidget(QLabel("Effect / 1606"), 0, 2)
        mastery_edit_grid.addWidget(self.mastery_id_edit, 0, 3)
        mastery_edit_grid.addWidget(QLabel("State / 1607"), 1, 0)
        mastery_edit_grid.addWidget(self.mastery_state_edit, 1, 1)
        selected_layout.addLayout(mastery_edit_grid)
        mastery_actions = QHBoxLayout()
        for text, slot in [
            ("Apply Row", self.apply_mastery_slot_inline_edits),
            ("Install OP Pattern", self.install_mastery_skiller_op_selected),
            ("Install OP Pattern To All", self.install_mastery_skiller_op_all),
            ("Copy Values", self.copy_selected_mastery_slot_pair),
            ("Export CSV", self.export_mastery_slots_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); mastery_actions.addWidget(btn)
        mastery_actions.addStretch(1)
        selected_layout.addLayout(mastery_actions)
        mastery_layout.addWidget(selected_card)
        layout.addWidget(mastery_card, 1)
        return page


    def _mastery_mods_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        header = QLabel("Mastery")
        header.setObjectName("pageHeader")
        layout.addWidget(header)

        help_text = QLabel("Pick four Overmastery stats and a value. Auto apply can write the setup as soon as you change it.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        self.mastery_mod_tabs = QTabWidget(page)
        self.mastery_mod_tabs.hide()

        mastery_tabs = QTabWidget(page)
        mastery_tabs.setObjectName("editorTabs")
        self.mastery_value_tabs = mastery_tabs

        # ------------------------------------------------------------------
        # Tab 1: overmastery workflow
        # ------------------------------------------------------------------
        over_tab = QWidget()
        over_layout_root = QVBoxLayout(over_tab)
        over_layout_root.setContentsMargins(12, 12, 12, 12)
        over_layout_root.setSpacing(12)

        top_row = QHBoxLayout()
        target_card = make_card("Target")
        target_layout = QVBoxLayout(target_card)
        target_layout.setSpacing(8)
        target_label = QLabel("Target for selected-group edits.")
        target_label.setWordWrap(True)
        target_label.setObjectName("subtleText")
        target_layout.addWidget(target_label)
        self.mastery_mod_character_combo = QComboBox()
        self.mastery_mod_character_combo.setMinimumWidth(360)
        self.mastery_mod_character_combo.currentIndexChanged.connect(lambda *_: self.refresh_mastery_mod_rows())
        target_layout.addWidget(QLabel("Character / group"))
        target_layout.addWidget(self.mastery_mod_character_combo)
        target_btns = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_mastery_mod_rows)
        update_names_btn = QPushButton("Update Names / IDs")
        update_names_btn.clicked.connect(self.download_mastery_mod_id_search_db)
        target_btns.addWidget(refresh_btn)
        target_btns.addWidget(update_names_btn)
        target_btns.addStretch(1)
        target_layout.addLayout(target_btns)
        self.mastery_mod_status_label = QLabel("Open a save, then pick a character/group.")
        self.mastery_mod_status_label.setObjectName("subtleText")
        self.mastery_mod_status_label.setWordWrap(True)
        target_layout.addWidget(self.mastery_mod_status_label)
        top_row.addWidget(target_card, 1)

        over_card = make_card("Overmastery")
        over_layout = QVBoxLayout(over_card)
        over_layout.setSpacing(8)

        self.mastery_overmastery_combos = []
        over_grid = QGridLayout()
        over_grid.setHorizontalSpacing(10)
        over_grid.setVerticalSpacing(6)
        for i in range(4):
            combo = QComboBox()
            combo.setMinimumWidth(420)
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            combo.setMinimumContentsLength(22)
            combo.currentIndexChanged.connect(lambda *_: self._schedule_overmastery_auto_apply())
            self.mastery_overmastery_combos.append(combo)
            over_grid.addWidget(QLabel(f"Stat {i + 1}"), i // 2, (i % 2) * 2)
            over_grid.addWidget(combo, i // 2, (i % 2) * 2 + 1)
        over_layout.addLayout(over_grid)

        value_row = QHBoxLayout()
        self.mastery_overmastery_value_edit = QLineEdit("-1")
        self.mastery_overmastery_value_edit.setToolTip("-1 = 80%, 512 = 20%, 0 = clear")
        self.mastery_overmastery_value_edit.setMaximumWidth(95)
        self.mastery_overmastery_value_edit.textChanged.connect(lambda *_: self._schedule_overmastery_auto_apply())
        self.mastery_overmastery_write_value_check = QCheckBox("Value")
        self.mastery_overmastery_write_value_check.setChecked(True)
        self.mastery_overmastery_write_value_check.toggled.connect(lambda *_: self._schedule_overmastery_auto_apply())
        self.mastery_overmastery_auto_apply_check = QCheckBox("Auto apply to all")
        self.mastery_overmastery_auto_apply_check.setChecked(True)
        value_row.addWidget(QLabel("Value"))
        value_row.addWidget(self.mastery_overmastery_value_edit)
        for label, value_text in [
            ("80%", "-1"),
            ("20%", "512"),
            ("Zero", "0"),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=value_text: self.mastery_overmastery_value_edit.setText(t))
            value_row.addWidget(btn)
        value_row.addWidget(self.mastery_overmastery_write_value_check)
        value_row.addWidget(self.mastery_overmastery_auto_apply_check)
        value_row.addStretch(1)
        over_layout.addLayout(value_row)

        action_grid = QGridLayout()
        action_grid.setHorizontalSpacing(8)
        action_grid.setVerticalSpacing(6)
        apply_all_over_btn = QPushButton("Apply All")
        apply_all_over_btn.clicked.connect(self.apply_overmastery_four_stats_all)
        apply_selected_over_btn = QPushButton("Apply Selected")
        apply_selected_over_btn.clicked.connect(self.apply_overmastery_four_stats_selected)
        stats_only_all_btn = QPushButton("Stats Only")
        stats_only_all_btn.clicked.connect(self.apply_mastery_sw_overmastery_selected_four_stats)
        set_values_btn = QPushButton("Values Only")
        set_values_btn.clicked.connect(
            lambda _=False: self.apply_mastery_sw_overmastery_value_sweep(
                self._parse_mastery_u32_text(self.mastery_overmastery_value_edit.text(), -1)
            )
        )
        action_grid.addWidget(apply_all_over_btn, 0, 0)
        action_grid.addWidget(apply_selected_over_btn, 0, 1)
        action_grid.addWidget(stats_only_all_btn, 0, 2)
        action_grid.addWidget(set_values_btn, 0, 3)
        over_layout.addLayout(action_grid)

        self.mastery_sw_lab_status = QLabel("Ready")
        self.mastery_sw_lab_status.setWordWrap(True)
        self.mastery_sw_lab_status.setObjectName("subtleText")
        over_layout.addWidget(self.mastery_sw_lab_status)

        top_row.addWidget(over_card, 2)
        over_layout_root.addLayout(top_row)

        selected_card = make_card("Selected Row")
        selected_layout = QHBoxLayout(selected_card)
        selected_layout.setSpacing(8)
        selected_layout.addWidget(QLabel("Row"))
        for label, value in [
            ("0", 0),
            ("512", 512),
            ("Max", MASTERY_1607_SAFE_MAX),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, v=value: self.set_mastery_selected_row_value(v))
            selected_layout.addWidget(btn)
        selected_layout.addSpacing(12)
        selected_layout.addWidget(QLabel("Group"))
        for label, value in [
            ("0", 0),
            ("512", 512),
            ("Max", MASTERY_1607_SAFE_MAX),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, v=value: self.set_mastery_current_group_value(v))
            selected_layout.addWidget(btn)
        selected_layout.addStretch(1)
        over_layout_root.addWidget(selected_card)
        over_layout_root.addStretch(1)

        # Hidden single-slot controls retained for old helper compatibility only.
        self.mastery_sw_slot_spin = QSpinBox(page)
        self.mastery_sw_slot_spin.setRange(1, 31408)
        self.mastery_sw_slot_spin.setValue(1)
        self.mastery_sw_slot_spin.hide()
        self.mastery_sw_value_spin = QSpinBox(page)
        self.mastery_sw_value_spin.setRange(-1, MASTERY_1607_SAFE_MAX)
        self.mastery_sw_value_spin.setSpecialValueText("FFFFFFFF / 80%")
        self.mastery_sw_value_spin.setValue(-1)
        self.mastery_sw_value_spin.hide()

        # ------------------------------------------------------------------
        # Tab 2: rows / edit
        # ------------------------------------------------------------------
        rows_tab = QWidget()
        rows_tab_layout = QVBoxLayout(rows_tab)
        rows_tab_layout.setContentsMargins(12, 12, 12, 12)
        rows_tab_layout.setSpacing(12)

        rows_card = make_card("Current Mastery Rows")
        rows_layout = QVBoxLayout(rows_card)
        rows_layout.setSpacing(8)
        row_top = QHBoxLayout()
        self.mastery_mod_target_filter_edit = QLineEdit()
        self.mastery_mod_target_filter_edit.setPlaceholderText("Filter by row, effect name, value, kind...")
        self._connect_debounced_text_changed(self.mastery_mod_target_filter_edit, "mastery_mod_rows", self.refresh_mastery_mod_rows, 180)
        row_top.addWidget(self.mastery_mod_target_filter_edit, 1)
        refresh_rows_btn = QPushButton("Refresh Rows")
        refresh_rows_btn.clicked.connect(self.refresh_mastery_mod_rows)
        row_top.addWidget(refresh_rows_btn)
        rows_layout.addLayout(row_top)
        self.mastery_mod_table = QTableView()
        self.mastery_mod_table.setModel(self.mastery_mod_model)
        self._table_clean(self.mastery_mod_table)
        self.mastery_mod_table.setMinimumHeight(450)
        self.mastery_mod_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.mastery_mod_table.selectionModel().selectionChanged.connect(lambda *_: self.update_mastery_mod_from_selection())
        self.mastery_mod_table.doubleClicked.connect(lambda *_: self.update_mastery_mod_from_selection())
        self._configure_mastery_rows_table()
        rows_layout.addWidget(self.mastery_mod_table, 1)
        self.mastery_mod_selected_detail_label = QLabel("")
        self.mastery_mod_selected_detail_label.setWordWrap(True)
        self.mastery_mod_selected_detail_label.setObjectName("subtleText")
        self.mastery_mod_selected_detail_label.hide()
        rows_tab_layout.addWidget(rows_card, 2)

        edit_card = make_card("Edit Selected Row")
        edit_layout = QVBoxLayout(edit_card)
        edit_layout.setSpacing(8)
        self.mastery_mod_edit_detail_label = QLabel("Select a row above.")
        self.mastery_mod_edit_detail_label.setWordWrap(True)
        self.mastery_mod_edit_detail_label.setObjectName("subtleText")
        edit_layout.addWidget(self.mastery_mod_edit_detail_label)

        self.mastery_mod_target_combo = QComboBox()
        self.mastery_mod_target_combo.setMinimumWidth(620)
        self.mastery_mod_target_combo.currentIndexChanged.connect(lambda *_: self.update_mastery_mod_target_from_combo())
        self.mastery_mod_effect_combo = QComboBox()
        self.mastery_mod_effect_combo.setMinimumWidth(620)
        self.mastery_mod_state_spin = QSpinBox()
        self.mastery_mod_state_spin.setRange(0, MASTERY_1607_SAFE_MAX)
        self.mastery_mod_state_spin.setValue(0x200)
        self.mastery_mod_state_write_check = QCheckBox("Also write value")
        self.mastery_mod_state_write_check.setChecked(True)
        self.mastery_mod_state_write_check.setToolTip("Writes the paired mastery value for the selected row. Leave enabled for most edits.")
        self.mastery_mod_live_check = QCheckBox("Auto apply")
        self.mastery_mod_live_check.setChecked(True)

        edit_grid = QGridLayout()
        edit_grid.setHorizontalSpacing(10)
        edit_grid.setVerticalSpacing(8)
        edit_grid.addWidget(QLabel("Selected row"), 0, 0)
        edit_grid.addWidget(self.mastery_mod_target_combo, 0, 1, 1, 3)
        edit_grid.addWidget(QLabel("Effect"), 1, 0)
        edit_grid.addWidget(self.mastery_mod_effect_combo, 1, 1, 1, 3)
        edit_grid.addWidget(QLabel("Value"), 2, 0)
        edit_grid.addWidget(self.mastery_mod_state_spin, 2, 1)
        edit_grid.addWidget(self.mastery_mod_state_write_check, 2, 2)
        edit_grid.addWidget(self.mastery_mod_live_check, 2, 3)
        value_only_btn = QPushButton("Apply Value Only")
        value_only_btn.clicked.connect(self.apply_mastery_selected_row_value_only)
        effect_value_btn = QPushButton("Apply Effect + Value")
        effect_value_btn.clicked.connect(self.apply_mastery_mod_selected_row_edit)
        reload_btn = QPushButton("Reload Selected")
        reload_btn.clicked.connect(self.update_mastery_mod_target_from_combo)
        edit_grid.addWidget(value_only_btn, 3, 1)
        edit_grid.addWidget(effect_value_btn, 3, 2)
        edit_grid.addWidget(reload_btn, 3, 3)
        edit_layout.addLayout(edit_grid)
        rows_tab_layout.addWidget(edit_card, 1)

        mastery_tabs.addTab(over_tab, "Overmastery")
        mastery_tabs.addTab(rows_tab, "Rows / Edit")
        mastery_tabs.currentChanged.connect(lambda idx: self.refresh_mastery_mod_rows() if idx == 1 else None)
        mastery_tabs.setCurrentIndex(0)
        layout.addWidget(mastery_tabs, 1)

        hidden = QFrame(page)
        hidden.hide()
        self.mastery_mod_reference_status = QLabel(hidden)
        self.mastery_mod_reference_table = QTableView(hidden)
        self.mastery_mod_reference_table.setModel(self.mastery_mod_reference_model)
        self.mastery_mod_value_status = QLabel(hidden)
        self.mastery_mod_value_table = QTableView(hidden)
        self.mastery_mod_value_table.setModel(self.mastery_mod_value_model)
        self.mastery_mod_code_status = QLabel(hidden)
        self.mastery_mod_code_table = QTableView(hidden)
        self.mastery_mod_code_table.setModel(self.mastery_mod_code_model)
        self.mastery_mod_preset_write_1607_check = QCheckBox(hidden)
        self.mastery_mod_preset_write_1607_check.setChecked(True)
        self.mastery_mod_preset_1607_spin = QSpinBox(hidden)
        self.mastery_mod_preset_1607_spin.setRange(0, MASTERY_1607_SAFE_MAX)
        self.mastery_mod_preset_1607_spin.setValue(0x200)
        self.mastery_mod_preset_status = QLabel(hidden)
        self.mastery_mod_preset_table = QTableView(hidden)
        self.mastery_mod_preset_table.setModel(self.mastery_mod_preset_model)
        self.mastery_mod_slot_spin = QSpinBox(hidden)
        self.mastery_mod_slot_spin.setRange(1, 999)
        self.mastery_mod_slot_spin.setValue(1)
        self.mastery_mod_socket_spin = QSpinBox(hidden)
        self.mastery_mod_socket_spin.setRange(1, 3)
        self.mastery_mod_socket_spin.setValue(1)

        self._mastery_mod_loading = False
        for widget in (self.mastery_mod_slot_spin, self.mastery_mod_socket_spin, self.mastery_mod_state_spin):
            widget.valueChanged.connect(lambda *_: self._maybe_live_apply_mastery_mod())
        self.mastery_mod_effect_combo.currentIndexChanged.connect(lambda *_: self._maybe_live_apply_mastery_mod())
        self._populate_mastery_mod_effect_combo()
        self._populate_overmastery_effect_combos()
        self._install_common_numeric_validators()
        return page


    def _gbid_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("GBID / Hash Browser")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Search the loaded item/sigil hash database by ID, name, hex hash, or decimal hash. Double-click a row to copy its hex hash.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.gbid_filter = QLineEdit()
        self.gbid_filter.setPlaceholderText("Search e.g. Damage Cap, GEEN_020, EE732781")
        self.gbid_filter.textChanged.connect(self.gbid_model.set_filter)
        layout.addWidget(self.gbid_filter)
        quick_filter_row = QHBoxLayout()
        for label, text in [
            ("Weapons", "weapon"), ("Sigils", "sigil"), ("Materials", "material"),
            ("Currency", "currency"), ("Characters", "character"), ("Clear", ""),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=text: self.gbid_filter.setText(t))
            quick_filter_row.addWidget(btn)
        quick_filter_row.addStretch(1)
        layout.addLayout(quick_filter_row)
        self.gbid_table = QTableView()
        self.gbid_table.setModel(self.gbid_model)
        self._table_clean(self.gbid_table, hidden_columns=(4, 5))
        self.gbid_table.doubleClicked.connect(lambda _: self.copy_selected_gbid_hash())
        layout.addWidget(self.gbid_table, 1)
        row = QHBoxLayout()
        copy_hex = QPushButton("Copy Hex Hash")
        copy_hex.clicked.connect(self.copy_selected_gbid_hash)
        copy_dec = QPushButton("Copy Decimal Hash")
        copy_dec.clicked.connect(self.copy_selected_gbid_decimal)
        copy_id = QPushButton("Copy ID")
        copy_id.clicked.connect(self.copy_selected_gbid_id)
        add_item_btn = QPushButton("Add as Item")
        add_item_btn.clicked.connect(self.add_selected_gbid_as_item)
        add_sigil_btn = QPushButton("Add as Sigil")
        add_sigil_btn.clicked.connect(self.add_selected_gbid_as_sigil)
        add_weapon_btn = QPushButton("Add as Weapon")
        add_weapon_btn.clicked.connect(self.add_selected_gbid_as_weapon)
        for btn in [copy_hex, copy_dec, copy_id, add_item_btn, add_sigil_btn, add_weapon_btn]:
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _item_id_catalog_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Community Item IDs")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Focused lookup for the Community Item IDs page: sigils/gems, ITEM_* inventory objects, weapons, characters, and trait/skill hashes. "
            "Use Download Full Community Item IDs to pull the complete current CSV on your PC, then search/add from the normal GBID Browser."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.item_id_catalog_summary = QPlainTextEdit()
        self.item_id_catalog_summary.setReadOnly(True)
        self.item_id_catalog_summary.setMaximumHeight(155)
        layout.addWidget(self.item_id_catalog_summary)
        self.item_id_catalog_filter = QLineEdit()
        self.item_id_catalog_filter.setPlaceholderText("Search item_id.csv rows: damage cap, ITEM_31, wrightstone, WEP_PL, color pack...")
        self.item_id_catalog_filter.textChanged.connect(lambda _: self.refresh_item_id_catalog_rows())
        layout.addWidget(self.item_id_catalog_filter)
        quick_row = QHBoxLayout()
        for label, text in [("Sigils", "GEEN"), ("ITEM_*", "ITEM_"), ("Materials", "material"), ("Wrightstones", "wrightstone"), ("Weapons", "WEP_"), ("Clear", "")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=text: self.item_id_catalog_filter.setText(t))
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)
        self.item_id_catalog_table = QTableView()
        self.item_id_catalog_table.setModel(self.item_id_catalog_model)
        self._table_clean(self.item_id_catalog_table, hidden_columns=(5,))
        self.item_id_catalog_table.doubleClicked.connect(lambda _: self.copy_selected_item_id_catalog_hash())
        layout.addWidget(self.item_id_catalog_table, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Refresh", self.refresh_item_id_catalog_rows),
            ("Download Full Community Item IDs", self.download_item_ids),
            ("Copy Hash", self.copy_selected_item_id_catalog_hash),
            ("Copy GBID", self.copy_selected_item_id_catalog_gbid),
            ("Export Catalog CSV", self.export_item_id_catalog_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh_item_id_catalog_rows()
        return page



    def _sigil_gem_id_catalog_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Community Sigil/Gem IDs")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Focused lookup for the Community Sigil/Gem IDs page: GEEN_* sigil inventory IDs, tier variants, plus variants, and their GBFR hashes. "
            "Use this when adding sigils or checking why a sigil appears as unknown. Trait/property SKILL_* IDs live on the Trait/Skill ID Catalog page."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        self.sigil_gem_catalog_summary = QPlainTextEdit()
        self.sigil_gem_catalog_summary.setReadOnly(True)
        self.sigil_gem_catalog_summary.setMaximumHeight(175)
        layout.addWidget(self.sigil_gem_catalog_summary)

        self.sigil_gem_catalog_filter = QLineEdit()
        self.sigil_gem_catalog_filter.setPlaceholderText("Search sigils/gems: Damage Cap, War Elemental, GEEN_020, V+, resistance...")
        self.sigil_gem_catalog_filter.textChanged.connect(lambda _: self.refresh_sigil_gem_catalog_rows())
        layout.addWidget(self.sigil_gem_catalog_filter)

        quick_row = QHBoxLayout()
        for label, text in [
            ("Offense", "offense"),
            ("Utility", "utility"),
            ("Resistance", "resistance"),
            ("V", " tier V"),
            ("V+", "Plus"),
            ("Damage Cap", "Damage Cap"),
            ("Clear", ""),
        ]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=text: self.sigil_gem_catalog_filter.setText(t))
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)

        self.sigil_gem_hide_dummy = QCheckBox("Hide reserved / dummy rows")
        self.sigil_gem_hide_dummy.setChecked(True)
        self.sigil_gem_hide_dummy.toggled.connect(lambda _: self.refresh_sigil_gem_catalog_rows())
        layout.addWidget(self.sigil_gem_hide_dummy)

        self.sigil_gem_catalog_table = QTableView()
        self.sigil_gem_catalog_table.setModel(self.sigil_gem_catalog_model)
        self._table_clean(self.sigil_gem_catalog_table, hidden_columns=(9,))
        self.sigil_gem_catalog_table.doubleClicked.connect(lambda _: self.copy_selected_sigil_gem_catalog_hash())
        layout.addWidget(self.sigil_gem_catalog_table, 1)

        row = QHBoxLayout()
        for text, slot in [
            ("Refresh", self.refresh_sigil_gem_catalog_rows),
            ("Download Full Community IDs", self.download_item_ids),
            ("Copy Hash", self.copy_selected_sigil_gem_catalog_hash),
            ("Copy GBID", self.copy_selected_sigil_gem_catalog_gbid),
            ("Add Selected Sigil", self.add_selected_sigil_gem_catalog_to_empty_slot),
            ("Export Catalog CSV", self.export_sigil_gem_catalog_csv),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh_sigil_gem_catalog_rows()
        return page


    def _trait_skill_id_catalog_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Community Trait/Skill IDs")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Focused lookup for the Community Trait/Skill IDs page: SKILL_* trait/property hashes used by sigil traits, wrightstone properties, overmastery research, and unknown hash cleanup. "
            "These are not the same thing as GEEN_* sigil inventory item IDs."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.trait_skill_catalog_summary = QPlainTextEdit()
        self.trait_skill_catalog_summary.setReadOnly(True)
        self.trait_skill_catalog_summary.setMaximumHeight(160)
        layout.addWidget(self.trait_skill_catalog_summary)
        self.trait_skill_catalog_filter = QLineEdit()
        self.trait_skill_catalog_filter.setPlaceholderText("Search trait/skill IDs: ATK, DMG Cap, War Elemental, SKILL_020, character warpath, unused...")
        self.trait_skill_catalog_filter.textChanged.connect(lambda _: self.refresh_trait_skill_catalog_rows())
        layout.addWidget(self.trait_skill_catalog_filter)
        quick_row = QHBoxLayout()
        for label, text in [("Offense", "offense"), ("Defense", "defense"), ("Resistance", "resistance"), ("Character", "character"), ("Special", "special"), ("DMG Cap", "dmg cap"), ("Clear", "")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=text: self.trait_skill_catalog_filter.setText(t))
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)
        self.trait_skill_hide_unused = QCheckBox("Hide unused / caution rows")
        self.trait_skill_hide_unused.setChecked(True)
        self.trait_skill_hide_unused.toggled.connect(lambda _: self.refresh_trait_skill_catalog_rows())
        layout.addWidget(self.trait_skill_hide_unused)
        self.trait_skill_catalog_table = QTableView()
        self.trait_skill_catalog_table.setModel(self.trait_skill_catalog_model)
        self._table_clean(self.trait_skill_catalog_table, hidden_columns=(7,))
        self.trait_skill_catalog_table.doubleClicked.connect(lambda _: self.copy_selected_trait_skill_catalog_hash())
        layout.addWidget(self.trait_skill_catalog_table, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Refresh", self.refresh_trait_skill_catalog_rows),
            ("Download Full Community IDs", self.download_item_ids),
            ("Copy Hash", self.copy_selected_trait_skill_catalog_hash),
            ("Copy Skill ID", self.copy_selected_trait_skill_catalog_id),
            ("Export Catalog CSV", self.export_trait_skill_catalog_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh_trait_skill_catalog_rows()
        return page


    def _model_id_catalog_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Community Model IDs")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Focused lookup for the Community Model IDs page: player/NPC/enemy model IDs, map objects, player weapon model IDs, and enemy weapon IDs. "
            "The Hash column is generated with the same GBFR hash helper where the ID is hashable, so these rows can help clean up model-like hash fields in saves."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.model_id_catalog_summary = QPlainTextEdit()
        self.model_id_catalog_summary.setReadOnly(True)
        self.model_id_catalog_summary.setMaximumHeight(150)
        layout.addWidget(self.model_id_catalog_summary)
        self.model_id_catalog_filter = QLineEdit()
        self.model_id_catalog_filter.setPlaceholderText("Search model IDs: PL, NP, EM, WP, Bahamut, Lucilius, Rukalsa, cat...")
        self.model_id_catalog_filter.textChanged.connect(lambda _: self.refresh_model_id_catalog_rows())
        layout.addWidget(self.model_id_catalog_filter)
        quick_row = QHBoxLayout()
        for label, text in [("Players", "Model Player"), ("NPCs", "Model NPC"), ("Enemies", "Model Enemy"), ("Player Weapons", "Model Player Weapon"), ("Map Objects", "Model Map"), ("Bahamut", "Bahamut"), ("Clear", "")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=text: self.model_id_catalog_filter.setText(t))
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)
        self.model_id_catalog_table = QTableView()
        self.model_id_catalog_table.setModel(self.model_id_catalog_model)
        self._table_clean(self.model_id_catalog_table, hidden_columns=(7,))
        self.model_id_catalog_table.doubleClicked.connect(lambda _: self.copy_selected_model_id_catalog_hash())
        layout.addWidget(self.model_id_catalog_table, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Refresh", self.refresh_model_id_catalog_rows),
            ("Copy Hash", self.copy_selected_model_id_catalog_hash),
            ("Copy Model ID", self.copy_selected_model_id_catalog_id),
            ("Export Catalog CSV", self.export_model_id_catalog_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh_model_id_catalog_rows()
        return page

    def _phase_id_catalog_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Community Phase IDs")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Focused lookup for the Community Phase IDs page. Phase IDs are p### jump codes such as p720 for Lucilius Arena; "
            "the related ph### entity code is shown beside it for scripts/entity-prefix research. Hash columns are generated locally for save-field cleanup."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.phase_id_catalog_summary = QPlainTextEdit()
        self.phase_id_catalog_summary.setReadOnly(True)
        self.phase_id_catalog_summary.setMaximumHeight(165)
        layout.addWidget(self.phase_id_catalog_summary)
        self.phase_id_catalog_filter = QLineEdit()
        self.phase_id_catalog_filter.setPlaceholderText("Search phase IDs: p720, Lucilius, Grandcypher, Folca, Seedhollow, title screen...")
        self.phase_id_catalog_filter.textChanged.connect(lambda _: self.refresh_phase_id_catalog_rows())
        layout.addWidget(self.phase_id_catalog_filter)
        quick_row = QHBoxLayout()
        for label, text in [("Lucilius", "Lucilius"), ("Grandcypher", "Grandcypher"), ("Folca", "Folca"), ("Seedhollow", "Seedhollow"), ("System/Menu", "System"), ("Unknown", "?"), ("Clear", "")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=text: self.phase_id_catalog_filter.setText(t))
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)
        self.phase_id_catalog_table = QTableView()
        self.phase_id_catalog_table.setModel(self.phase_id_catalog_model)
        self._table_clean(self.phase_id_catalog_table, hidden_columns=(7, 8))
        self.phase_id_catalog_table.doubleClicked.connect(lambda _: self.copy_selected_phase_id_catalog_hash())
        layout.addWidget(self.phase_id_catalog_table, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Refresh", self.refresh_phase_id_catalog_rows),
            ("Copy Phase Hash", self.copy_selected_phase_id_catalog_hash),
            ("Copy Phase ID", self.copy_selected_phase_id_catalog_id),
            ("Copy Entity Code", self.copy_selected_phase_id_catalog_entity_code),
            ("Export Catalog CSV", self.export_phase_id_catalog_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh_phase_id_catalog_rows()
        return page



    def _quest_id_catalog_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Community Quest IDs")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Focused lookup for the Community Quest IDs page. Quest IDs are grouped by story, side/challenge, Fate Episode, quest-counter, town/lobby, practice, and misc ranges. "
            "Numeric Value is useful when matching quest/progression fields in the save map."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.quest_id_catalog_summary = QPlainTextEdit()
        self.quest_id_catalog_summary.setReadOnly(True)
        self.quest_id_catalog_summary.setMaximumHeight(165)
        layout.addWidget(self.quest_id_catalog_summary)
        self.quest_id_catalog_filter = QLineEdit()
        self.quest_id_catalog_filter.setPlaceholderText("Search quest IDs: 407321, Zero, Bahamut, Fate, Grandcypher, Folca, Practice...")
        self.quest_id_catalog_filter.textChanged.connect(lambda _: self.refresh_quest_id_catalog_rows())
        layout.addWidget(self.quest_id_catalog_filter)
        quick_row = QHBoxLayout()
        for label, text in [("Story", "Main Quest"), ("Side", "Challenge"), ("Fate", "Fate"), ("Quest Counter", "Multiplayer"), ("Towns", "Towns"), ("Practice", "Practice"), ("Zero", "Zero"), ("Clear", "")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=text: self.quest_id_catalog_filter.setText(t))
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)
        self.quest_id_catalog_table = QTableView()
        self.quest_id_catalog_table.setModel(self.quest_id_catalog_model)
        self._table_clean(self.quest_id_catalog_table, hidden_columns=(6, 7))
        self.quest_id_catalog_table.doubleClicked.connect(lambda _: self.copy_selected_quest_id_catalog_id())
        layout.addWidget(self.quest_id_catalog_table, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Refresh", self.refresh_quest_id_catalog_rows),
            ("Copy Quest ID", self.copy_selected_quest_id_catalog_id),
            ("Copy Numeric Value", self.copy_selected_quest_id_catalog_numeric),
            ("Export Catalog CSV", self.export_quest_id_catalog_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh_quest_id_catalog_rows()
        return page

    def _reference_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Reference Tables / Rates")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "A readable lookup for rates, mechanics notes, weapon-material references, and source-page coverage. "
            "This is not raw save data; it is here so you can understand what IDs/items are tied to in-game systems."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.reference_filter = QLineEdit()
        self.reference_filter.setPlaceholderText("Search references: curio, grand success, quick quest, weapon materials, emote, transmutation...")
        self.reference_filter.textChanged.connect(lambda _: self.refresh_reference_rows())
        layout.addWidget(self.reference_filter)
        quick_row = QHBoxLayout()
        for label, text in [("Gacha", "gacha"), ("Curio", "curio"), ("Weapon Materials", "weapon materials"), ("Quick Quest", "quick quest"), ("Sigil Synthesis", "synthesis"), ("Clear", "")]:
            btn = QPushButton(label)
            btn.clicked.connect(lambda _=False, t=text: self.reference_filter.setText(t))
            quick_row.addWidget(btn)
        quick_row.addStretch(1)
        layout.addLayout(quick_row)
        self.reference_table = QTableView()
        self.reference_table.setModel(self.reference_model)
        self._table_clean(self.reference_table, hidden_columns=(5,))
        self.reference_table.doubleClicked.connect(lambda _: self.copy_selected_reference_value())
        layout.addWidget(self.reference_table, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Refresh", self.refresh_reference_rows),
            ("Copy Value", self.copy_selected_reference_value),
            ("Copy Notes", self.copy_selected_reference_notes),
            ("Export CSV", self.export_reference_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.refresh_reference_rows()
        return page


    def _database_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Resource Database Coverage")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "A compact view of what the packaged databases currently know. Use this to see whether missing names are GBID/hash rows, non-hash resource IDs, mechanics notes, or save-unit labels."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)
        self.database_filter = QLineEdit()
        self.database_filter.setPlaceholderText("Filter coverage by database/category, e.g. weapon, quest, mechanics, overmastery...")
        self.database_filter.textChanged.connect(lambda _: self.refresh_database_rows())
        layout.addWidget(self.database_filter)
        self.database_table = QTableView()
        self.database_table.setModel(self.database_model)
        self._table_clean(self.database_table)
        layout.addWidget(self.database_table, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Refresh Counts", self.refresh_database_rows),
            ("Show Missing/Unknown Save Hashes", self.show_unknown_hash_scan),
            ("Export Coverage CSV", self.export_database_coverage_csv),
            ("Open Resource IDs", lambda: self._show_page("Resource IDs")),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page


    def _resource_ids_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Resource ID Browser")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Search non-hash public IDs scraped from Community resources: quest IDs, model IDs, phase IDs, buff/debuff IDs, actions, motions, obj IDs, and user attributes. "
            "These are used to make value previews and research output more readable."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.resource_id_filter = QLineEdit()
        self.resource_id_filter.setPlaceholderText("Search category, ID, name, notes...")
        self.resource_id_filter.textChanged.connect(self.resource_id_model.set_filter)
        layout.addWidget(self.resource_id_filter)
        self.resource_id_table = QTableView()
        self.resource_id_table.setModel(self.resource_id_model)
        self.resource_id_table.setSortingEnabled(True)
        self.resource_id_table.doubleClicked.connect(lambda idx: self.copy_resource_id())
        layout.addWidget(self.resource_id_table, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Copy ID", self.copy_resource_id),
            ("Copy Name", self.copy_resource_name),
            ("Export Resource IDs CSV", self.export_resource_ids_csv),
            ("Download Community Resource IDs", self.download_resource_ids),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page


    def _entity_prefixes_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Entity Prefix Decoder")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Decode GBFR asset/entity prefixes from IDs or paths, such as pl0000, wp2200, em1800, ph720, or st101f00. "
            "The prefix database is also merged into Resource IDs so model/phase/stage references are easier to read."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        self.entity_prefix_input = QLineEdit()
        self.entity_prefix_input.setPlaceholderText("Enter code/path: pl0000, wp2200, em1800, ph720, st101f00...")
        self.entity_prefix_input.returnPressed.connect(self.decode_entity_prefix)
        layout.addWidget(self.entity_prefix_input)

        row = QHBoxLayout()
        decode_btn = QPushButton("Decode Prefix")
        decode_btn.clicked.connect(self.decode_entity_prefix)
        copy_btn = QPushButton("Copy Result")
        copy_btn.clicked.connect(self.copy_entity_prefix_result)
        search_btn = QPushButton("Show Prefix Rows in Resource IDs")
        search_btn.clicked.connect(self.show_entity_prefix_resource_rows)
        for btn in [decode_btn, copy_btn, search_btn]:
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.entity_prefix_result = QPlainTextEdit()
        self.entity_prefix_result.setReadOnly(True)
        self.entity_prefix_result.setPlainText(
            "Examples:\n"
            "  pl0000  -> Player body\n"
            "  wp2200  -> Player weapon\n"
            "  em1800  -> Enemy body\n"
            "  ph720   -> Phase\n"
            "  st101f00 -> Stage / room / map"
        )
        layout.addWidget(self.entity_prefix_result, 1)
        return page


    def _hash_tools_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Hash Tools / ID Generator")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel(
            "Compute GBFR's custom XXHash32 for GBID strings. This is useful when the save has a hash but the database is missing a row, or when you want to test likely IDs such as WEP_PL0200_01 or GEEN_020_04."
        )
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        self.hash_input = QLineEdit()
        self.hash_input.setPlaceholderText("Enter one ID/name per line or comma-separated: GEEN_000_00, WEP_PL0200_01")
        self.hash_input.returnPressed.connect(self.compute_hash_tools)
        layout.addWidget(self.hash_input)

        row = QHBoxLayout()
        for text, slot in [
            ("Compute Hash", self.compute_hash_tools),
            ("Copy Results", self.copy_hash_tool_results),
            ("Export Results CSV", self.export_hash_tool_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.hash_results = QPlainTextEdit()
        self.hash_results.setReadOnly(True)
        self.hash_results.setPlainText("Examples:\nGEEN_000_00 -> 95858B63 / Attack Power I\nWEP_PL0200_01 -> 3B2082B6 / Rukalsa")
        layout.addWidget(self.hash_results, 1)
        return page

    def _data_sources_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("GBID Data Sources")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Paste public CSV URLs or normal Google Sheets edit links. The editor converts Google Sheets gid tabs to CSV export URLs and merges any rows with an ID/GBID, name, and 8-digit hash. Extra columns become searchable tooltip aliases.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.sources_text = QPlainTextEdit()
        self.sources_text.setPlaceholderText("One URL per line. Google Sheets links with gid= are supported.")
        default_sources = self.default_source_urls_text()
        self.sources_text.setPlainText(default_sources)
        layout.addWidget(self.sources_text, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Audit Google Sheet Tabs", self.audit_google_sheet_sources),
            ("Download + Merge These Sources", self.download_sources_from_page),
            ("Download Community Item IDs", self.download_item_ids),
            ("Import Local CSV/TSV", self.import_item_csv),
            ("Export Merged DB CSV", self.export_item_db_csv),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.sources_status = QPlainTextEdit()
        self.sources_status.setReadOnly(True)
        self.sources_status.setMaximumHeight(180)
        self.sources_status.setPlainText(f"Packaged seed rows loaded: {len(self.item_db)}\nPackaged Google Sheet tabs: {len(source_urls_from_text(default_sources))}")
        layout.addWidget(self.sources_status)
        return page


    def _hash_scan_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Hash Scan")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Scans the loaded save for uint values that resolve to known GBIDs, plus unknown values in hash-like save fields. This is useful for finding weapons, sigils, materials, currencies, abilities, and still-unknown hashes.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        row = QHBoxLayout()
        known_btn = QPushButton("Scan Known Hashes")
        known_btn.clicked.connect(lambda: self.run_hash_scan(False))
        unknown_btn = QPushButton("Scan + Unknown Hash Fields")
        unknown_btn.clicked.connect(lambda: self.run_hash_scan(True))
        export_btn = QPushButton("Export Hash Scan CSV")
        export_btn.clicked.connect(self.export_hash_scan_csv)
        resolve_btn = QPushButton("Resolve Unknown ID Patterns")
        resolve_btn.clicked.connect(self.resolve_unknown_hash_patterns)
        export_candidates_btn = QPushButton("Export Pattern Matches CSV")
        export_candidates_btn.clicked.connect(self.export_hash_candidate_csv)
        jump_btn = QPushButton("Jump Selected Hash to Raw Unit")
        jump_btn.clicked.connect(self.jump_to_hash_scan_unit)
        for btn in [known_btn, unknown_btn, resolve_btn, export_candidates_btn, jump_btn, export_btn]:
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.hash_scan_table = QTableView()
        self.hash_scan_table.setModel(self.hash_scan_model)
        self.hash_scan_table.doubleClicked.connect(lambda _: self.jump_to_hash_scan_unit())
        layout.addWidget(self.hash_scan_table, 1)
        self.hash_scan_text = QPlainTextEdit()
        self.hash_scan_text.setReadOnly(True)
        self.hash_scan_text.setMaximumHeight(170)
        layout.addWidget(self.hash_scan_text)
        return page

    def _research_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Research / Safe Hunting Tools")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("This page surfaces high-value known and candidate fields. It does not claim unknown fields are safe; use before/after diffs and value search before editing.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        row = QHBoxLayout()
        self.value_search_edit = QLineEdit()
        self.value_search_edit.setPlaceholderText("Search exact value in loaded save, e.g. 999, 0xEE732781, 12345")
        search_btn = QPushButton("Search Values")
        search_btn.clicked.connect(self.search_loaded_values)
        export_btn = QPushButton("Export Value Search CSV")
        export_btn.clicked.connect(self.export_value_search_csv)
        refresh_btn = QPushButton("Refresh Candidates")
        refresh_btn.clicked.connect(self.refresh_candidate_rows)
        row.addWidget(self.value_search_edit, 1)
        row.addWidget(search_btn)
        row.addWidget(export_btn)
        row.addWidget(refresh_btn)
        layout.addLayout(row)
        self.value_search_results: List[Dict[str, Any]] = []
        self.candidate_table = QTableView()
        self.candidate_table.setModel(self.candidate_model)
        self.candidate_table.doubleClicked.connect(lambda _: self.jump_to_candidate_unit())
        layout.addWidget(self.candidate_table, 1)
        self.research_text = QPlainTextEdit()
        self.research_text.setReadOnly(True)
        self.research_text.setMaximumHeight(180)
        layout.addWidget(self.research_text)
        return page


    def _compare_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Compare Saves")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        help_text = QLabel("Load two saves to see exactly which save-unit records changed. This is the fastest way to identify rupies, MSP, item counts, weapon XP, sigil locks, and other unknown fields from before/after samples.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        row = QHBoxLayout()
        before_btn = QPushButton("Choose Before")
        before_btn.clicked.connect(lambda: self.choose_compare_path(True))
        after_btn = QPushButton("Choose After")
        after_btn.clicked.connect(lambda: self.choose_compare_path(False))
        run_btn = QPushButton("Run Compare")
        run_btn.clicked.connect(self.run_compare)
        export_json_btn = QPushButton("Export JSON Diff")
        export_json_btn.clicked.connect(lambda: self.export_compare("json"))
        export_csv_btn = QPushButton("Export CSV Diff")
        export_csv_btn.clicked.connect(lambda: self.export_compare("csv"))
        for btn in [before_btn, after_btn, run_btn, export_json_btn, export_csv_btn]:
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        self.compare_label = QLabel("Before: not selected\nAfter: not selected")
        self.compare_label.setWordWrap(True)
        layout.addWidget(self.compare_label)
        self.compare_text = QPlainTextEdit()
        self.compare_text.setReadOnly(True)
        layout.addWidget(self.compare_text, 1)
        return page

    def _raw_tools_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QLabel("Raw Tools")
        header.setObjectName("pageHeader")
        layout.addWidget(header)
        self.raw_text = QPlainTextEdit()
        self.raw_text.setReadOnly(True)
        layout.addWidget(self.raw_text, 1)
        row = QHBoxLayout()
        for text, slot in [
            ("Export JSON Report", self.export_report),
            ("Import Item CSV", self.import_item_csv),
            ("Download Item IDs", self.download_item_ids),
            ("Download Sheet Sources", self.download_sources_from_page),
            ("Export GBID DB", self.export_item_db_csv),
        ]:
            btn = QPushButton(text)
            btn.clicked.connect(slot)
            row.addWidget(btn)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def copy_text(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"Copied: {text}", 3000)

    def default_source_urls_text(self) -> str:
        # Source website lists are intentionally not shown in the end-user build.
        return ""

    def merge_item_db(self, db: ItemDatabase, label: str = "Imported") -> None:
        before = len(self.item_db)
        self.item_db.merge(db)
        self.unit_model.set_item_db(self.item_db)
        self.gbid_model.set_db(self.item_db)
        self.refresh_item_aware_views()
        self.refresh_unit_map_rows()
        self.refresh_save_map_rows()
        self.refresh_id_audit_rows()
        self.refresh_candidate_rows()
        self.refresh_database_rows()
        self.refresh_item_id_catalog_rows()
        self.refresh_sigil_gem_catalog_rows()
        self.refresh_trait_skill_catalog_rows()
        self.refresh_model_id_catalog_rows()
        self.refresh_phase_id_catalog_rows()
        self.refresh_quest_id_catalog_rows()
        self.refresh_preset_rows()
        self.refresh_save_wizard_rows()
        added = len(self.item_db) - before
        if hasattr(self, "sources_status"):
            self.sources_status.setPlainText(f"{label}: source rows {len(db)} | new merged hashes {added} | total DB rows {len(self.item_db)}")



    def refresh_preset_rows(self) -> None:
        if not hasattr(self, "preset_model"):
            return
        q = self.preset_filter_edit.text().strip() if hasattr(self, "preset_filter_edit") else ""
        rows = []
        for pack in search_preset_packs(q):
            rows.append([
                pack.category,
                pack.name,
                pack.total_rows,
                len(pack.items),
                len(pack.sigils),
                len(pack.weapons),
                pack.description,
                pack.key,
            ])
        self.preset_model.set_rows(rows)
        if hasattr(self, "preset_table"):
            self._auto_fit_table(self.preset_table)
        self.update_preset_detail()

    def current_preset_pack(self) -> Optional[PresetPack]:
        if not hasattr(self, "preset_table"):
            rows = search_preset_packs("")
            return rows[0] if rows else None
        idx = self.preset_table.currentIndex()
        if not idx.isValid():
            return None
        row = self.preset_model.rows[idx.row()] if 0 <= idx.row() < len(self.preset_model.rows) else None
        if not row:
            return None
        try:
            return get_preset_pack(str(row[7]))
        except Exception:
            return None

    def update_preset_detail(self) -> None:
        if not hasattr(self, "preset_detail_label"):
            return
        pack = self.current_preset_pack()
        if not pack:
            self.preset_detail_label.setText("Select a preset to preview what it will add.")
            return
        lines = [
            f"<b>{pack.name}</b>",
            f"Category: {pack.category}",
            f"Rows: {pack.total_rows}  |  Items: {len(pack.items)}  Sigils: {len(pack.sigils)}  Weapons: {len(pack.weapons)}",
            pack.description,
        ]
        if pack.notes:
            lines.append(f"Notes: {pack.notes}")
        if self.save:
            lines.append(f"Available empty slots now: items {self.count_empty_item_slots()}, sigils {self.count_empty_sigil_slots()}, weapons {self.count_empty_weapon_slots()}")
        preview = pack.to_batch_text()
        if preview:
            lines.append("<pre>" + preview.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "</pre>")
        self.preset_detail_label.setText("<br>".join(lines))

    def copy_selected_preset_text(self) -> None:
        pack = self.current_preset_pack()
        if not pack:
            QMessageBox.information(self, "No preset", "Select a preset first.")
            return
        QApplication.clipboard().setText(pack.to_batch_text())
        self.statusBar().showMessage(f"Copied preset batch text: {pack.name}", 3000)

    def export_preset_packs_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export preset packs", "gbfr_preset_packs.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["key", "name", "category", "description", "items", "sigils", "weapons", "notes"])
            for pack in list_preset_packs():
                writer.writerow([
                    pack.key,
                    pack.name,
                    pack.category,
                    pack.description,
                    "\n".join(f"{n}, {q}" for n, q in pack.items),
                    "\n".join(f"{n}, {lvl} {'locked' if lock else 'unlocked'}" for n, lvl, lock in pack.sigils),
                    "\n".join(f"{n}, {xp}" for n, xp in pack.weapons),
                    pack.notes,
                ])
        QMessageBox.information(self, "Exported", f"Exported {len(list_preset_packs())} preset packs.")

    def apply_selected_preset_pack(self) -> None:
        pack = self.current_preset_pack()
        if not pack:
            QMessageBox.information(self, "No preset", "Select a preset first.")
            return
        self.apply_preset_pack(pack)

    def _wallet_field_for_item_key(self, key: str, item_hash: Optional[int] = None) -> Optional[tuple[int, str, int]]:
        """Return direct UserDataManager wallet field for database names that are not real item stacks.

        The game reads top-bar currency/profile values from UserDataManager, not from
        ITEM_* inventory rows. Keeping this routing here prevents cheat/preset packs
        from adding duplicate fake Rupie/MSP stacks that display in the editor but do
        not change the in-game value.
        """
        text = (key or "").strip().lower().replace("_", " ").replace("-", " ")
        labels = {
            "rupie": (1104, "Rupies", 99_999_999),
            "rupies": (1104, "Rupies", 99_999_999),
            "mastery point": (1112, "Mastery Points", 9_999_999),
            "mastery points": (1112, "Mastery Points", 9_999_999),
            "msp": (1112, "Mastery Points", 9_999_999),
            "commendation": (1106, "Commendations", 999),
            "commendations": (1106, "Commendations", 999),
        }
        if text in labels:
            return labels[text]
        entry = self.item_db.lookup_hash(int(item_hash) & 0xFFFFFFFF) if item_hash is not None else None
        if entry:
            name = entry.display_name.strip().lower()
            item_id = entry.item_id.strip().upper()
            if name in labels:
                return labels[name]
            if item_id == "ITEM_35_0000":
                return labels["rupies"]
        return None

    def apply_preset_pack(self, pack: PresetPack) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        errors: List[str] = []
        resolved_items: List[tuple[int, int, str]] = []
        resolved_wallets: List[tuple[int, int, str, int]] = []
        resolved_sigils: List[tuple[int, int, bool, str]] = []
        resolved_weapons: List[tuple[int, int, str]] = []
        # Preset packs are curated, so be strict: do not apply generated or
        # unknown hashes from a preset. Manual Add/Batch tools can still accept
        # raw hashes, but presets should never surprise the user with
        # "Unknown · 0x..." rows.
        for key, qty in pack.items:
            h = self._resolve_hash_from_text(key)
            entry = self.item_db.lookup_hash(h) if h is not None else None
            if h is None or entry is None:
                errors.append(f"Item unresolved or unnamed: {key}")
            else:
                wallet = self._wallet_field_for_item_key(key, h)
                if wallet is not None:
                    field_id, label, cap = wallet
                    resolved_wallets.append((field_id, min(int(qty), int(cap)), label, int(cap)))
                elif entry.category in {"Sigil", "Weapon", "Character", "Trait / Skill"}:
                    errors.append(f"Item category mismatch: {key} resolved as {entry.category}")
                else:
                    resolved_items.append((h, qty, key))
        for key, level, locked in pack.sigils:
            h = self._resolve_hash_from_text(key)
            entry = self.item_db.lookup_hash(h) if h is not None else None
            if h is None or entry is None:
                errors.append(f"Sigil unresolved or unnamed: {key}")
            elif entry.category != "Sigil":
                errors.append(f"Sigil category mismatch: {key} resolved as {entry.category}")
            else:
                resolved_sigils.append((h, level, locked, key))
        for key, xp in pack.weapons:
            h = self._resolve_hash_from_text(key)
            entry = self.item_db.lookup_hash(h) if h is not None else None
            if h is None or entry is None:
                errors.append(f"Weapon unresolved or unnamed: {key}")
            elif entry.category != "Weapon":
                errors.append(f"Weapon category mismatch: {key} resolved as {entry.category}")
            else:
                resolved_weapons.append((h, xp, key))
        if not (resolved_wallets or resolved_items or resolved_sigils or resolved_weapons):
            QMessageBox.warning(self, "Preset unresolved", "None of this preset's rows resolved.\n" + "\n".join(errors[:12]))
            return
        if len(resolved_items) > self.count_empty_item_slots() or len(resolved_sigils) > self.count_empty_sigil_slots() or len(resolved_weapons) > self.count_empty_weapon_slots():
            QMessageBox.warning(
                self,
                "Not enough empty slots",
                "This preset needs more empty reusable slots than the save currently exposes.\n\n"
                f"Needs: {len(resolved_items)} item, {len(resolved_sigils)} sigil, {len(resolved_weapons)} weapon slots.\n"
                f"Available: {self.count_empty_item_slots()} item, {self.count_empty_sigil_slots()} sigil, {self.count_empty_weapon_slots()} weapon slots."
            )
            return
        added: List[str] = []
        for field_id, qty, label, _cap in resolved_wallets:
            rec = self.save.find_first("int", field_id, 0) if self.save else None
            if rec is None:
                errors.append(f"Wallet field missing: {label} ({field_id})")
                continue
            if self._set_record_first_value(rec, int(qty), f"{label} wallet value"):
                added.append(f"Set {label} -> {int(qty):,} (UserDataManager {field_id})")
        for h, qty, _ in resolved_items:
            result = self._add_item_hash_qty_to_empty_slot(h, qty, flag=1)
            if result:
                added.append(result)
        for h, level, locked, _ in resolved_sigils:
            result = self._add_sigil_hash_level_to_empty_slot(h, level, locked)
            if result:
                added.append(result)
        for h, xp, _ in resolved_weapons:
            result = self._add_weapon_hash_xp_to_empty_slot(h, xp)
            if result:
                added.append(result)
        self._after_editor_patch(f"Applied preset {pack.name}: {len(added)} rows added in memory.")





    def refresh_save_wizard_rows(self) -> None:
        if not hasattr(self, "save_wizard_model"):
            return
        q = self.save_wizard_filter_edit.text().strip() if hasattr(self, "save_wizard_filter_edit") else ""
        rows = []
        # Built-in Save Wizard mappings are now shown as action buttons above.
        # The table is reserved for imported/raw sheet references so it no
        # longer duplicates Preset Packs.
        imported = list(getattr(self, "_imported_save_wizard_rows", []))
        if q and imported:
            toks = q.lower().split()
            imported = [r for r in imported if all(t in " ".join([r.key, r.name, r.category, r.description, r.source]).lower() for t in toks)]
        for cheat in imported:
            rows.append([
                cheat.category,
                cheat.name,
                cheat.source,
                "Reference only" if not cheat.safe else "Mapped",
                cheat.description,
                cheat.key,
            ])
        if not rows and not imported and not q:
            rows.append([
                "Sheet",
                "No sheet rows loaded yet",
                "—",
                "Reference only",
                "Use Load Sheet Tab to import the Google Sheet for research. Use the mapped cheat buttons above for actual edits.",
                "__none__",
            ])
        self.save_wizard_model.set_rows(rows)
        if hasattr(self, "save_wizard_table"):
            self._auto_fit_table(self.save_wizard_table)
        self.update_save_wizard_detail()

    def current_save_wizard_cheat(self) -> Optional[SaveWizardCheat]:
        if not hasattr(self, "save_wizard_table"):
            return None
        idx = self.save_wizard_table.currentIndex()
        if not idx.isValid():
            return None
        row = self.save_wizard_model.rows[idx.row()] if 0 <= idx.row() < len(self.save_wizard_model.rows) else None
        if not row:
            return None
        key = str(row[5])
        if key == "__none__":
            return None
        for cheat in getattr(self, "_imported_save_wizard_rows", []):
            if cheat.key == key:
                return cheat
        try:
            return get_builtin_save_wizard_cheat(key)
        except Exception:
            return None

    def update_save_wizard_detail(self) -> None:
        if not hasattr(self, "save_wizard_detail_label"):
            return
        cheat = self.current_save_wizard_cheat()
        if not cheat:
            mapped = len(list_builtin_save_wizard_cheats(""))
            imported = len(getattr(self, "_imported_save_wizard_rows", []))
            self.save_wizard_detail_label.setText(
                f"Mapped editor-native cheats: {mapped}<br>Imported sheet reference rows: {imported}<br><br>"
                "Use the cheat buttons above to patch a save. Use the sheet table to track raw Save Wizard rows we still need to map."
            )
            return
        lines = [
            f"<b>{cheat.name}</b>",
            f"Category: {cheat.category}",
            f"Source: {cheat.source}",
            f"Status: {'mapped safe editor-native action' if cheat.safe else 'reference-only imported row'}",
            cheat.description,
        ]
        if cheat.safe:
            lines.insert(3, f"Action: {cheat.display_action()}")
        else:
            lines.append("This row will not apply until it is mapped to a safe editor-native action.")
        self.save_wizard_detail_label.setText("<br>".join(lines))

    def export_save_wizard_cheats_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Save Wizard cheat list", "gbfr_save_wizard_cheats.csv", "CSV (*.csv)")
        if not path:
            return
        cheats = list_builtin_save_wizard_cheats("") + list(getattr(self, "_imported_save_wizard_rows", []))
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["key", "name", "category", "action", "target", "safe", "description", "source"])
            for c in cheats:
                writer.writerow([c.key, c.name, c.category, c.action, c.target, c.safe, c.description, c.source])
        QMessageBox.information(self, "Exported", f"Exported {len(cheats)} Save Wizard cheat rows.")

    def load_save_wizard_sheet_tab(self) -> None:
        url, ok = QInputDialog.getText(self, "Load Save Wizard sheet tab", "Google Sheets URL:", text=SAVE_WIZARD_SHEET_URL)
        if not ok or not url.strip():
            return
        try:
            text = load_sheet_csv(url.strip())
            rows = parse_sheet_cheats(text, source=url.strip())
        except Exception as exc:
            QMessageBox.warning(self, "Sheet load failed", f"Could not load the sheet tab from this PC.\n\n{exc}\n\nThe bundled editor-native Save Wizard cheats still work offline.")
            return
        self._imported_save_wizard_rows = rows
        self.refresh_save_wizard_rows()
        QMessageBox.information(self, "Sheet loaded", f"Loaded {len(rows)} reference rows from the Save Wizard tab. Imported raw code rows are reference-only until mapped to safe editor actions.")

    def apply_selected_save_wizard_cheat(self) -> None:
        cheat = self.current_save_wizard_cheat()
        if not cheat:
            QMessageBox.information(self, "No cheat selected", "Select a Save Wizard cheat first.")
            return
        self.apply_save_wizard_cheat(cheat)

    def apply_save_wizard_cheat(self, cheat: SaveWizardCheat) -> None:
        if not cheat.safe or cheat.action == "reference_only":
            QMessageBox.information(self, "Reference-only row", "This imported sheet row is listed for research only. I will not blindly apply raw Save Wizard offset/code patches until it is mapped to a safe named editor action.")
            return
        if cheat.action == "preset":
            try:
                self.apply_preset_pack(get_preset_pack(cheat.target))
            except Exception as exc:
                QMessageBox.warning(self, "Preset unavailable", str(exc))
            return
        if cheat.action == "max_items":
            self.cheat_max_known_item_quantities()
            return
        if cheat.action == "max_sigils":
            self.cheat_max_sigil_levels_and_locks()
            return
        if cheat.action == "max_weapons":
            self.cheat_max_weapon_xp_and_flags()
            return
        if cheat.action == "max_characters":
            self.cheat_max_character_levels()
            return
        if cheat.action == "complete_quests":
            self.cheat_complete_progression_group("", cheat.name)
            return
        if cheat.action == "complete_progression_group":
            self.cheat_complete_progression_group(cheat.target, cheat.name)
            return
        if cheat.action == "unlock_titles":
            self.cheat_unlock_title_archive_candidates()
            return
        if cheat.action == "add_all_known_v_sigils":
            self.cheat_add_all_known_v_sigils()
            return
        if cheat.action == "add_all_known_materials":
            self.cheat_add_all_known_materials()
            return
        if cheat.action == "repair_unsafe_material_addall":
            self.repair_unsafe_material_add_all_rows()
            return
        QMessageBox.warning(self, "Unsupported action", f"No apply handler for {cheat.action}")

    def _existing_hashes_for_fields(self, field_ids: List[int]) -> set[int]:
        if not self.save:
            return set()
        hashes: set[int] = set()
        grouped = self.save.group_by_unit(field_ids)
        for fields in grouped.values():
            for fid in field_ids:
                rec = fields.get(fid)
                value = self._record_first_value(rec, 0) if rec is not None else 0
                if value not in (0, EMPTY_HASH):
                    hashes.add(int(value) & 0xFFFFFFFF)
        return hashes

    def _first_non_empty_record(self, fields: Dict[int, UnitRecord], field_ids: Iterable[int]) -> Optional[UnitRecord]:
        """Return the first record whose first value is not an empty/placeholder hash.

        Some saves contain multiple parallel inventory families on the same unit
        (for example 2102/1901/2002), where the first field exists but is empty.
        Older cheat code used `fields.get(2102) or fields.get(1901)`, which
        silently picked the empty record and caused the cheat to patch 0 rows on
        PS4 / early-game saves.
        """
        fallback = None
        for fid in field_ids:
            rec = fields.get(fid)
            if rec is None:
                continue
            if fallback is None:
                fallback = rec
            value = self._record_first_value(rec, 0)
            if value not in (0, EMPTY_HASH):
                return rec
        return fallback

    def _first_existing_record(self, fields: Dict[int, UnitRecord], field_ids: Iterable[int]) -> Optional[UnitRecord]:
        for fid in field_ids:
            rec = fields.get(fid)
            if rec is not None:
                return rec
        return None

    def _known_v_sigil_entries(self):
        rows = []
        for entry in self.item_db.by_hash.values():
            item_id = entry.item_id.upper()
            if entry.category != "Sigil" or not item_id.startswith("GEEN_"):
                continue
            if item_id.endswith("_04") or item_id.endswith("_14") or entry.display_name.endswith(" V") or entry.display_name.endswith(" V+"):
                rows.append(entry)
        return sorted(rows, key=lambda e: (e.item_id, e.display_name))

    def cheat_add_all_known_v_sigils(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        existing = self._existing_hashes_for_fields([2703])
        entries = [e for e in self._known_v_sigil_entries() if (e.hash_value & 0xFFFFFFFF) not in existing]
        if not entries:
            self.statusBar().showMessage("No missing V/V+ sigils found, or all bundled known V/V+ sigils already exist.", 5000)
            return
        empty = self.count_empty_sigil_slots()
        if empty <= 0:
            self.statusBar().showMessage("No reusable empty sigil slots were found.", 5000)
            return
        add_entries = entries[:empty]
        added = []
        for e in add_entries:
            result = self._add_sigil_hash_level_to_empty_slot(e.hash_value, SIGIL_LEVEL_MAX, True)
            if result:
                added.append(result)
        self._after_editor_patch(f"Save Wizard cheat applied: added {len(added)} known V/V+ sigils.")

    def _material_cheat_quantity(self, name: str) -> int:
        low = name.lower()
        if "rupie" in low:
            return 99_999_999
        if "mastery point" in low:
            return 9_999_999
        if "damascus" in low or "ambrosia" in low:
            return 99
        return 999

    def _load_material_bank_templates(self) -> Dict[int, Dict[str, int]]:
        """Load known-good ItemManager 180x activation templates.

        These templates were harvested from a user-provided high-completion PS4
        save where the material bank already contains many valid rows. They let
        the editor activate a matching inactive 1801 row by restoring the
        companion state/index fields instead of only writing quantity.
        """
        templates: Dict[int, Dict[str, int]] = {}
        path = RESOURCE_DIR / "material_bank_templates_seed.csv"
        if not path.exists():
            return templates
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    raw_hash = (row.get("hash_hex") or "").strip()
                    if not raw_hash:
                        continue
                    try:
                        h = int(raw_hash, 16) & 0xFFFFFFFF
                    except ValueError:
                        continue
                    templates[h] = {
                        "source_unit": int(row.get("source_unit") or 0),
                        "template_qty": int(row.get("template_qty") or 0),
                        "state_1803": int(row.get("state_1803") or 0),
                        "index_1804": int(row.get("index_1804") or 0),
                        "value_1805": int(row.get("value_1805") or 0),
                        "value_1806": int(row.get("value_1806") or 0),
                        "extra_1807": int(row.get("extra_1807") or 0),
                    }
        except Exception:
            return templates
        return templates

    def _material_template_for_hash(self, item_hash: int) -> Optional[Dict[str, int]]:
        return getattr(self, "material_bank_templates", {}).get(int(item_hash) & 0xFFFFFFFF)

    MATERIAL_BANK_CATEGORIES = {"Material", "Currency", "Consumable", "Glitterstone", "Ticket", "Wrightstone"}

    def _is_material_bank_entry(self, item_hash: int) -> bool:
        entry = self.item_db.lookup_hash(int(item_hash) & 0xFFFFFFFF) if hasattr(self, "item_db") else None
        return bool(entry and entry.category in self.MATERIAL_BANK_CATEGORIES)

    def _item_meta_has_real_quantity(self, meta: Dict[str, Any]) -> bool:
        # 1802 is the actual material/currency stack count. 2105 is used by
        # wrightstone/item-slot style rows and should not be treated as material quantity.
        return bool(meta and meta.get("quantity_is_real"))

    def _material_bank_slot_is_active(self, fields: Dict[int, UnitRecord]) -> bool:
        """Return True only for material-bank rows that look game-owned/active.

        In uploaded crash samples, bulk inventory edits set 1802 quantities on
        many 180x rows where the game had a valid-looking 1801 hash but zero
        state/index fields. Those rows appear to be locked/unobtained catalog
        entries, not safe inventory stacks. The game can crash when those are
        force-populated. Until the full 1803/1804 state mapping is proven, bulk
        tools only touch rows the game already activated: existing quantity > 0
        or non-zero 1803/1804/1807 state.
        """
        qty = self._record_first_value(fields.get(1802), 0)
        state = self._record_first_value(fields.get(1803), 0)
        index = self._record_first_value(fields.get(1804), 0)
        extra = self._record_first_value(fields.get(1807), 0)
        return int(qty or 0) > 0 or int(state or 0) != 0 or int(index or 0) != 0 or int(extra or 0) != 0

    def _item_meta_is_safe_bulk_quantity_target(self, meta: Dict[str, Any]) -> bool:
        if not self._item_meta_has_real_quantity(meta):
            return False
        if meta.get("wallet_value"):
            return True
        h = self._record_first_value(meta.get("hash_rec"), 0)
        fields = {
            1801: meta.get("hash_rec"),
            1802: meta.get("qty_rec"),
            1803: meta.get("flag_rec"),
            1804: meta.get("index_rec"),
            1805: meta.get("value_1805_rec"),
            1806: meta.get("value_1806_rec"),
            1807: meta.get("extra_rec"),
        }
        if self._material_bank_slot_is_active(fields):
            return True
        # Safe-add templates mean this row is a known 1801 material catalog row
        # that can be activated by restoring 1803-1807 before/while setting 1802.
        try:
            return bool(h and self._material_template_for_hash(int(h) & 0xFFFFFFFF))
        except Exception:
            return False

    def _set_item_meta_quantity_safely(self, meta: Dict[str, Any], quantity: int) -> bool:
        """Set a visible item quantity through the correct backing writer.

        Wallet rows write directly. 180x material rows go through the upsert
        writer so inactive template-backed rows get their companion 1803-1807
        state restored instead of only changing 1802.
        """
        if not meta or not self._item_meta_has_real_quantity(meta):
            return False
        if meta.get("wallet_value"):
            return self._set_record_first_value(meta.get("qty_rec"), max(0, int(quantity)), "wallet quantity")
        h = self._record_first_value(meta.get("hash_rec"), 0)
        if h not in ("", 0, EMPTY_HASH) and self._is_material_bank_entry(int(h) & 0xFFFFFFFF):
            return bool(self._upsert_material_bank_quantity(int(h) & 0xFFFFFFFF, max(1, int(quantity)), flag=None))
        return self._set_record_first_value(meta.get("qty_rec"), max(0, int(quantity)), "item quantity")

    def _unit_record(self, unit_id: int, field_id: int) -> Optional[UnitRecord]:
        """Return the exact save-unit record for a manager field/unit pair.

        This keeps writes routed through the documented Save Unit layout instead
        of whichever similarly-shaped row the UI happened to select first.
        """
        if not self.save:
            return None
        rows = self.save.find(id_type=int(field_id), unit_id=int(unit_id))
        return rows[0] if rows else None

    def _set_unit_first_value(self, unit_id: int, field_id: int, value: Any, label: str) -> bool:
        return self._set_record_first_value(self._unit_record(unit_id, field_id), value, f"{label} ({field_id}/unit {unit_id})")

    def _find_existing_material_bank_slot(self, item_hash: int) -> Optional[Dict[str, Any]]:
        """Find an existing ItemManager material/currency stack by 1801 hash.

        Community's Save Unit list groups material bank rows under ItemManager
        1801/1802/1803/1804/1807. For stackable materials/currency, 1801 is
        the item hash and 1802 is the real quantity. Updating an existing row is
        safer than adding duplicate rows into unrelated 210x item slots.
        """
        if not self.save:
            return None
        wanted = int(item_hash) & 0xFFFFFFFF
        grouped = self.save.group_by_unit([1801, 1802, 1803, 1804, 1805, 1806, 1807])
        for unit_id, fields in sorted(grouped.items()):
            hrec = fields.get(1801)
            qrec = fields.get(1802)
            if not hrec or not qrec:
                continue
            if (self._record_first_value(hrec, 0) & 0xFFFFFFFF) == wanted:
                if not self._material_bank_slot_is_active(fields):
                    continue
                return {
                    "unit_id": unit_id,
                    "label": "180x Material/Currency bank slot",
                    "hash_rec": hrec,
                    "qty_rec": qrec,
                    "flag_rec": fields.get(1803),
                    "index_rec": fields.get(1804),
                    "value_1805_rec": fields.get(1805),
                    "value_1806_rec": fields.get(1806),
                    "extra_rec": fields.get(1807),
                    "quantity_is_real": True,
                }
        return None

    def _find_inactive_material_bank_slot_with_template(self, item_hash: int) -> Optional[Dict[str, Any]]:
        """Find a matching inactive 1801 row that can be safely activated.

        This does not create new FlatBuffer rows and does not place an item hash
        into an unrelated empty slot. It only touches a row where the game's
        save already has that exact 1801 hash, then restores known companion
        1803/1804/etc state from material_bank_templates_seed.csv.
        """
        if not self.save:
            return None
        wanted = int(item_hash) & 0xFFFFFFFF
        template = self._material_template_for_hash(wanted)
        if not template:
            return None
        if int(template.get("state_1803", 0) or 0) == 0 and int(template.get("index_1804", 0) or 0) == 0:
            return None
        grouped = self.save.group_by_unit([1801, 1802, 1803, 1804, 1805, 1806, 1807])
        for unit_id, fields in sorted(grouped.items()):
            hrec = fields.get(1801)
            qrec = fields.get(1802)
            if not hrec or not qrec:
                continue
            if (self._record_first_value(hrec, 0) & 0xFFFFFFFF) != wanted:
                continue
            if self._material_bank_slot_is_active(fields):
                continue
            return {
                "unit_id": unit_id,
                "label": "180x inactive material row activated from template",
                "hash_rec": hrec,
                "qty_rec": qrec,
                "flag_rec": fields.get(1803),
                "index_rec": fields.get(1804),
                "value_1805_rec": fields.get(1805),
                "value_1806_rec": fields.get(1806),
                "extra_rec": fields.get(1807),
                "quantity_is_real": True,
                "template": template,
            }
        return None

    def _write_material_bank_slot(self, slot: Dict[str, Any], item_hash: int, qty: int, flag: Optional[int] = 1) -> int:
        """Write a stackable item to documented ItemManager 180x fields.

        Returns the number of scalar records that actually changed.
        """
        changed = 0
        template = slot.get("template") or {}
        if self._set_record_first_value(slot.get("hash_rec"), int(item_hash) & 0xFFFFFFFF, "ItemManager 1801 item hash"):
            changed += 1
        if self._set_record_first_value(slot.get("qty_rec"), max(0, int(qty)), "ItemManager 1802 quantity"):
            changed += 1

        # When activating an inactive material/catalog row, quantity alone is
        # unsafe. Restore the companion fields from a matching known-good row.
        if template:
            for rec_key, tmpl_key, label in [
                ("flag_rec", "state_1803", "ItemManager 1803 state"),
                ("index_rec", "index_1804", "ItemManager 1804 index/state"),
                ("value_1805_rec", "value_1805", "ItemManager 1805 companion"),
                ("value_1806_rec", "value_1806", "ItemManager 1806 companion"),
                ("extra_rec", "extra_1807", "ItemManager 1807 extra"),
            ]:
                rec = slot.get(rec_key)
                if rec is None:
                    continue
                value = int(template.get(tmpl_key, 0) or 0)
                if value == 0 and tmpl_key in {"state_1803", "index_1804"}:
                    continue
                if self._set_record_first_value(rec, value, label):
                    changed += 1
            return changed

        if flag is not None and slot.get("flag_rec") is not None:
            current = self._record_first_value(slot.get("flag_rec"), 0)
            # Preserve non-zero state unless the caller explicitly asked for a value.
            new_flag = int(flag) if current == 0 or flag is not None else current
            if self._set_record_first_value(slot.get("flag_rec"), new_flag, "ItemManager 1803 state"):
                changed += 1
        return changed

    def _upsert_material_bank_quantity(self, item_hash: int, qty: int, flag: Optional[int] = 1) -> Optional[str]:
        """Update existing 1801/1802 material stack or safely activate a matching 180x row.

        Adding/activating an item should never leave the stack at 0. Direct row
        edits may still set a quantity to 0, but add/cheat paths normalize to at
        least 1 so the game does not see a newly obtained item with no stack.
        """
        if not self.save:
            return None
        qty = max(1, int(qty))
        slot = self._find_existing_material_bank_slot(item_hash)
        action = "Updated"
        if not slot:
            slot = self._find_inactive_material_bank_slot_with_template(item_hash)
            action = "Activated"
        if not slot:
            # Still no safe target: do not invent a new row. Missing materials
            # can only be added when the save already contains the matching
            # inactive 1801 row and we have a known-good companion template.
            return None
        changed = self._write_material_bank_slot(slot, item_hash, qty, flag)
        entry = self.item_db.lookup_hash(int(item_hash) & 0xFFFFFFFF)
        display = f"{entry.display_name} ({entry.item_id})" if entry else f"0x{int(item_hash) & 0xFFFFFFFF:08X}"
        if changed:
            return f"{action} {display} x{int(qty):,} -> 180x unit {slot['unit_id']} ({changed} field{'s' if changed != 1 else ''})"
        return f"No change for {display}; 180x unit {slot['unit_id']} already matched x{int(qty):,}"

    def _known_material_entries(self):
        blocked = {"Sigil", "Weapon", "Character", "Trait / Skill", "Other"}
        rows = []
        for entry in self.item_db.by_hash.values():
            if entry.category in blocked:
                continue
            name_low = entry.display_name.lower()
            if name_low.startswith("unnamed / reserved") or name_low.startswith("reserved /"):
                continue
            allowed_categories = {"Material", "Currency", "Consumable", "Glitterstone", "Wrightstone", "Ticket"}
            wallet = self._wallet_field_for_item_key(entry.display_name, entry.hash_value)
            if wallet is not None:
                continue
            if entry.category in allowed_categories:
                rows.append(entry)
        return sorted(rows, key=lambda e: (e.category, e.item_id, e.display_name))

    def cheat_add_all_known_materials(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        candidates = []
        existing_active = 0
        for entry in self._known_material_entries():
            h = int(entry.hash_value) & 0xFFFFFFFF
            if self._find_existing_material_bank_slot(h):
                existing_active += 1
                continue
            slot = self._find_inactive_material_bank_slot_with_template(h)
            if slot:
                qty = self._material_cheat_quantity(entry.display_name)
                candidates.append((entry, slot, qty))
        if not candidates:
            self.statusBar().showMessage("No missing material rows can be safely activated in this save. Max Existing Items is still safe.", 6000)
            return
        added = []
        for entry, _slot, qty in candidates:
            result = self._upsert_material_bank_quantity(entry.hash_value, qty, flag=None)
            if result:
                added.append(result)
        self._after_editor_patch(f"Safely activated {len(added)} missing material rows in memory.")

    def repair_unsafe_material_add_all_rows(self) -> None:
        """Clear likely bad 1802 quantities created by old Add All Materials builds.

        The crash pattern we confirmed is: 1801 has a catalog hash, but 1803,
        1804, and 1807 are all zero. Old builds filled 1802 anyway, which can
        make the game treat a locked/inactive catalog row as a real inventory
        stack and crash. This repair only clears those suspicious quantities; it
        does not touch rows with non-zero state/index/extra fields.
        """
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        grouped = self.save.group_by_unit([1801, 1802, 1803, 1804, 1807])
        candidates = []
        for unit_id, fields in sorted(grouped.items()):
            hash_rec = fields.get(1801)
            qty_rec = fields.get(1802)
            if not hash_rec or not qty_rec:
                continue
            item_hash = self._record_first_value(hash_rec, 0)
            qty = int(self._record_first_value(qty_rec, 0) or 0)
            state = int(self._record_first_value(fields.get(1803), 0) or 0)
            index = int(self._record_first_value(fields.get(1804), 0) or 0)
            extra = int(self._record_first_value(fields.get(1807), 0) or 0)
            if item_hash not in (0, EMPTY_HASH) and qty > 0 and state == 0 and index == 0 and extra == 0:
                entry = self.item_db.lookup_hash(int(item_hash) & 0xFFFFFFFF) if hasattr(self, "item_db") else None
                name = f"{entry.display_name} ({entry.item_id})" if entry else f"0x{int(item_hash) & 0xFFFFFFFF:08X}"
                candidates.append((unit_id, qty_rec, name, qty))
        if not candidates:
            self.statusBar().showMessage("No likely Add All Materials crash rows were found. Rows with normal state/index data were left untouched.", 6000)
            return
        changed = 0
        for _unit_id, qty_rec, _name, _qty in candidates:
            if self._set_record_first_value(qty_rec, 0, "repair unsafe inactive 1802 quantity"):
                changed += 1
        self._after_editor_patch(f"Repaired {changed} unsafe inactive material quantity rows in memory.")

    def _wallet_quantity_targets(self) -> List[tuple[UnitRecord, str, int]]:
        """UserDataManager wallet/profile values that the game reads directly.

        These are not normal ItemManager stacks, even when the same currency has
        an ITEM_* GBID in the database. Rupies, for example, show as ITEM_35_0000
        in the database, but the actual wallet value is int field 1104.
        """
        if not self.save:
            return []
        rows: List[tuple[UnitRecord, str, int]] = []
        wallet_fields = [
            (1104, "Rupies", 99_999_999),
            (1112, "Mastery Points", 9_999_999),
            (1106, "Commendations", 999),
        ]
        for field_id, label, cap in wallet_fields:
            rec = self.save.find_first("int", field_id, 0)
            if rec is not None:
                rows.append((rec, f"{label} @ UserDataManager {field_id}", cap))
        return rows

    def _known_item_quantity_targets(self) -> List[tuple[UnitRecord, str, int]]:
        if not self.save:
            return []
        # Start with direct wallet/profile values. These are the values the main
        # menu reads for currencies like Rupies; do not rely on ITEM_35_0000
        # material rows for those.
        targets: List[tuple[UnitRecord, str, int]] = list(self._wallet_quantity_targets())

        # Real material/currency/consumable quantities are in the 180x bank:
        #   1801 = item hash, 1802 = stack quantity.
        # Do not bulk-patch 2105 wrightstone/item-slot rows as item quantities.
        grouped = self.save.group_by_unit([1801, 1802, 1803, 1804, 1805, 1806, 1807])
        wallet_names = {"rupie", "rupies", "mastery point", "mastery points"}
        for unit_id, fields in sorted(grouped.items()):
            hash_rec = fields.get(1801)
            qty_rec = fields.get(1802)
            if not hash_rec or not qty_rec:
                continue
            h = self.value1(hash_rec, 0)
            if h in ("", 0, EMPTY_HASH):
                continue
            if not self._material_bank_slot_is_active(fields) and not self._material_template_for_hash(int(h) & 0xFFFFFFFF):
                continue
            entry = self.item_db.lookup_hash(int(h) & 0xFFFFFFFF)
            if not entry or entry.category not in self.MATERIAL_BANK_CATEGORIES:
                continue
            # The wallet row is authoritative for top-bar currency. Avoid
            # showing/patching a duplicate ITEM_35_0000 stack as if it were Rupies.
            if entry.display_name.strip().lower() in wallet_names:
                continue
            targets.append((qty_rec, f"{entry.display_name} @ material unit {unit_id}", unit_id))
        return targets

    def _known_sigil_level_targets(self) -> List[Dict[str, Any]]:
        if not self.save:
            return []
        grouped = self.save.group_by_unit([2703, 2704, 2707])
        targets: List[Dict[str, Any]] = []
        seen_levels = set()
        for unit_id, fields in sorted(grouped.items()):
            hash_rec = self._first_non_empty_record(fields, [2703])
            h = self.value1(hash_rec, 0)
            level_rec = self._sigil_level_record_for_hash_record(hash_rec, fields)
            if not level_rec or h in ("", 0, EMPTY_HASH):
                continue
            if level_rec.key in seen_levels:
                continue
            seen_levels.add(level_rec.key)
            entry = self.item_db.lookup_hash(int(h) & 0xFFFFFFFF)
            if not entry or entry.category != "Sigil":
                continue
            targets.append({"unit": unit_id, "name": entry.display_name, "level_rec": level_rec, "flags_rec": fields.get(2707)})
        return targets

    def _known_weapon_xp_targets(self) -> List[Dict[str, Any]]:
        if not self.save:
            return []
        grouped = self.save.group_by_unit([2803, 2804, 2815])
        targets: List[Dict[str, Any]] = []
        for unit_id, fields in sorted(grouped.items()):
            hash_rec = self._first_non_empty_record(fields, [2803])
            h = self.value1(hash_rec, 0)
            xp_rec = fields.get(2804)
            if not xp_rec or h in ("", 0, EMPTY_HASH):
                continue
            entry = self.item_db.lookup_hash(int(h) & 0xFFFFFFFF)
            if not entry or entry.category != "Weapon":
                continue
            targets.append({"unit": unit_id, "name": entry.display_name, "xp_rec": xp_rec, "flags_rec": fields.get(2815)})
        return targets

    def _character_level_targets(self) -> List[Dict[str, Any]]:
        if not self.save:
            return []
        targets: List[Dict[str, Any]] = []
        for unit_id in range(10000, 10040):
            level_rec = (self.save.find(id_type=1308, unit_id=unit_id) or [None])[0]
            hash_rec = (self.save.find(id_type=1301, unit_id=unit_id) or [None])[0]
            if level_rec is None or hash_rec is None:
                continue
            h = self._record_first_value(hash_rec, 0)
            if h in (0, EMPTY_HASH):
                continue
            entry = self.item_db.lookup_hash(int(h) & 0xFFFFFFFF)
            name = entry.display_name if entry else format_hash_value(h)
            targets.append({"unit": unit_id, "name": name, "level_rec": level_rec, "xp_rec": (self.save.find(id_type=1303, unit_id=unit_id) or [None])[0]})
        return targets

    def _character_min_xp_for_level(self, level: int) -> int:
        """Conservative observed EXP floor for character level edits.

        GBFR stores both displayed level (1308) and a character EXP/progress
        value (1303). Samples show Lv3≈386, Lv9≈2555, Lv18≈26696,
        and Lv100=8,400,000. We raise EXP to a safe floor with those
        anchors so the game is less likely to recompute the edited level away.
        """
        level = max(1, min(100, int(level)))
        anchors = [(1, 0), (3, 386), (9, 2555), (18, 26696), (100, 8_400_000)]
        for lv, xp in anchors:
            if level == lv:
                return xp
        for (lv0, xp0), (lv1, xp1) in zip(anchors, anchors[1:]):
            if lv0 <= level <= lv1:
                t = (level - lv0) / max(1, (lv1 - lv0))
                return int(round(xp0 + (xp1 - xp0) * t))
        return 8_400_000

    def _set_character_level_bundle(self, meta_or_target: Dict[str, Any], level: int) -> int:
        """Set both level field 1308 and matching EXP/progress field 1303."""
        patched = 0
        if self._set_record_first_value(meta_or_target.get("level_rec"), int(level), "character level"):
            patched += 1
        xp_rec = meta_or_target.get("xp_rec")
        if xp_rec is not None:
            current_xp = self._record_first_value(xp_rec, 0)
            target_xp = 8_400_000 if int(level) >= 100 else self._character_min_xp_for_level(int(level))
            try:
                target_xp = max(int(current_xp), int(target_xp))
            except Exception:
                pass
            if self._set_record_first_value(xp_rec, target_xp, "character EXP/progress"):
                patched += 1
        return patched

    def cheat_set_known_item_quantities_custom(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        value, ok = QInputDialog.getInt(self, "Set Known Item Quantities", "Exact quantity/value for all known existing items/materials/currency rows:", 999, 0, 99_999_999)
        if not ok:
            return
        targets = self._known_item_quantity_targets()
        if not targets:
            QMessageBox.information(self, "No known items", "No known item quantity rows were found to patch.")
            return
        patched = 0
        for rec, _, _ in targets:
            if getattr(rec, "id_type", None) == 1802:
                hash_rec = (self.save.find(id_type=1801, unit_id=rec.unit_id) or [None])[0]
                h = self._record_first_value(hash_rec, 0)
                if h not in (0, EMPTY_HASH) and self._upsert_material_bank_quantity(int(h) & 0xFFFFFFFF, int(value), flag=None):
                    patched += 1
            elif self._set_record_first_value(rec, value, "item quantity"):
                patched += 1
        self._after_editor_patch(f"Specific value cheat applied: set {patched} known item quantities to {value:,}.")

    def cheat_set_known_sigil_levels_custom(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        value, ok = QInputDialog.getInt(self, "Set Known Sigil Levels", "Exact sigil level for all known existing sigils:", SIGIL_LEVEL_MAX, 0, SIGIL_LEVEL_TEST_MAX)
        if not ok:
            return
        value = self._clamp_sigil_level_value(value, minimum=0)
        lock = False
        targets = self._known_sigil_level_targets()
        if not targets:
            self.statusBar().showMessage("No known sigil rows were found to patch.", 4000)
            return
        patched = 0
        for t in targets:
            if self._set_record_first_value(t.get("level_rec"), value, "sigil level 2704 / FF900A"):
                patched += 1
            flags_rec = t.get("flags_rec")
            if lock and flags_rec is not None:
                cur = self._record_first_value(flags_rec, 0)
                self._set_record_first_value(flags_rec, cur | 1, "sigil flags")
        self._after_editor_patch(f"Specific value cheat applied: set {patched} known sigil levels to {value:,}.")

    def cheat_set_known_weapon_xp_custom(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        value, ok = QInputDialog.getInt(self, "Set Known Weapon XP", "Exact XP/progress value for all known existing weapons:", WEAPON_XP_MAX, 0, WEAPON_XP_MAX)
        if not ok:
            return
        value = self._clamp_weapon_xp_value(value)
        targets = self._known_weapon_xp_targets()
        if not targets:
            self.statusBar().showMessage("No known weapon rows were found to patch.", 4000)
            return
        patched = 0
        flags_patched = 0
        for t in targets:
            if self._set_record_first_value(t.get("xp_rec"), value, "weapon XP"):
                patched += 1
            flags_rec = t.get("flags_rec")
            if flags_rec is not None:
                cur = self._record_first_value(flags_rec, 0)
                if self._set_record_first_value(flags_rec, int(cur or 0) | 1, "weapon flags"):
                    flags_patched += 1
        try:
            self.refresh_weapon_rows()
        except Exception:
            pass
        self._after_editor_patch(f"Specific value cheat applied: set {patched} known weapon XP/progress rows to {value:,} and enabled {flags_patched} flag rows.", refresh=False)

    def cheat_set_character_levels_custom(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        value, ok = QInputDialog.getInt(self, "Set Character Levels", "Exact level for all visible/known character slots:", CHARACTER_VALUE_MAX, 1, CHARACTER_VALUE_MAX)
        if not ok:
            return
        value = self._clamp_character_value(value, minimum=1)
        targets = self._character_level_targets()
        if not targets:
            self.statusBar().showMessage("No character level rows were found to patch.", 4000)
            return
        patched = 0
        for t in targets:
            if self._set_record_first_value(t.get("level_rec"), value, "character level"):
                patched += 1
            if self._set_record_first_value(t.get("xp_rec"), value, "character EXP/progress"):
                patched += 1
        self._after_editor_patch(f"Specific value cheat applied: updated {patched} character level/EXP fields to {value:,}.")

    def cheat_max_character_levels(self) -> None:
        """Max every known character numeric row in the loaded save."""
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        targets = self._character_level_targets()
        if not targets:
            self.statusBar().showMessage("No character level rows were found to patch.", 4000)
            return
        patched = 0
        for t in targets:
            patched += self._set_character_max_bundle(t)
        self._after_editor_patch(f"Cheat applied: maxed {len(targets)} character slots / {patched} numeric fields to {CHARACTER_VALUE_MAX:,}.")

    def cheat_max_known_item_quantities(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        targets = self._known_item_quantity_targets()
        if not targets:
            self.statusBar().showMessage("No known item/currency quantity rows were found to patch.", 4000)
            return
        patched_targets: List[tuple[UnitRecord, int, str]] = []
        for rec, name, unit_or_cap in targets:
            low = str(name).lower()
            if "rupie" in low:
                cap = 99_999_999
            elif "mastery point" in low:
                cap = 9_999_999
            elif "commendation" in low or "damascus" in low or "ambrosia" in low:
                cap = 999
            else:
                cap = 999
            patched_targets.append((rec, cap, name))
        patched = 0
        for rec, cap, _ in patched_targets:
            if getattr(rec, "id_type", None) == 1802:
                hash_rec = (self.save.find(id_type=1801, unit_id=rec.unit_id) or [None])[0]
                item_hash = self._record_first_value(hash_rec, 0)
                if item_hash not in (0, EMPTY_HASH) and self._upsert_material_bank_quantity(int(item_hash) & 0xFFFFFFFF, int(cap), flag=None):
                    patched += 1
            elif self._set_record_first_value(rec, cap, "wallet/material quantity"):
                patched += 1
        self._after_editor_patch(f"Cheat applied: patched {patched} wallet/material quantities.")

    def cheat_max_sigil_levels_and_locks(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        grouped = self.save.group_by_unit([2702, 2703, 2704, 2707])
        targets: List[Dict[str, Any]] = []
        unknown = 0
        paired_mismatch = 0
        seen_levels = set()
        for unit_id, fields in sorted(grouped.items()):
            hash_rec = self._first_non_empty_record(fields, [2703])
            h = self.value1(hash_rec, 0)
            if not hash_rec or h in ("", 0, EMPTY_HASH):
                continue
            level_rec = self._sigil_level_record_for_hash_record(hash_rec, fields)
            if not level_rec:
                continue
            # Avoid writing the same paired level row twice if the save exposes
            # duplicate/hash mirror rows.
            if level_rec.key in seen_levels:
                continue
            seen_levels.add(level_rec.key)
            entry = self.item_db.lookup_hash(int(h) & 0xFFFFFFFF)
            if not entry or entry.category != "Sigil":
                unknown += 1
                name = f"Unknown 0x{int(h) & 0xFFFFFFFF:08X}"
            else:
                name = entry.display_name
            if int(getattr(hash_rec, "unit_id", unit_id)) != int(getattr(level_rec, "unit_id", unit_id)):
                paired_mismatch += 1
            targets.append({"unit": unit_id, "name": name, "level_rec": level_rec, "flags_rec": fields.get(2707)})
        if not targets:
            self.statusBar().showMessage("No active sigil rows were found to patch.", 4000)
            return
        patched = 0
        for t in targets:
            if self._set_record_first_value(t.get("level_rec"), SIGIL_LEVEL_MAX, "sigil level 2704 / FF900A"):
                patched += 1
            flags_rec = t.get("flags_rec")
            if flags_rec is not None:
                cur = self._record_first_value(flags_rec, 0)
                self._set_record_first_value(flags_rec, cur | 1, "sigil flags 2707")
        self._after_editor_patch(f"Cheat applied: set {patched} active sigil level row(s) to {SIGIL_LEVEL_MAX} through 2704/FF900A bottom-up pairing and locked matching flags.")

    def cheat_max_weapon_xp_and_flags(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        grouped = self.save.group_by_unit([2803, 2804, 2815])
        targets: List[Dict[str, Any]] = []
        for unit_id, fields in sorted(grouped.items()):
            hash_rec = self._first_non_empty_record(fields, [2803])
            h = self.value1(hash_rec, 0)
            xp_rec = fields.get(2804)
            if not xp_rec or h in ("", 0, EMPTY_HASH):
                continue
            entry = self.item_db.lookup_hash(int(h) & 0xFFFFFFFF)
            if not entry or entry.category != "Weapon":
                continue
            targets.append({"unit": unit_id, "name": entry.display_name, "xp_rec": xp_rec, "flags_rec": fields.get(2815)})
        if not targets:
            self.statusBar().showMessage("No known weapon rows were found to patch.", 4000)
            return
        patched = 0
        flags_patched = 0
        for t in targets:
            if self._set_record_first_value(t.get("xp_rec"), WEAPON_XP_MAX, "weapon XP"):
                patched += 1
            flags_rec = t.get("flags_rec")
            if flags_rec is not None:
                cur = self._record_first_value(flags_rec, 0)
                if self._set_record_first_value(flags_rec, int(cur or 0) | 1, "weapon flags"):
                    flags_patched += 1
        try:
            self.refresh_weapon_rows()
        except Exception:
            pass
        self._after_editor_patch(f"Cheat applied: patched {patched} known weapon XP/progress fields to {WEAPON_XP_MAX:,} and enabled {flags_patched} flag rows.", refresh=False)

    def _database_coverage_rows(self) -> List[List[Any]]:
        from collections import Counter
        rows: List[List[Any]] = []
        item_cats = Counter(e.category or "Uncategorized" for e in self.item_db.by_hash.values())
        for cat, count in sorted(item_cats.items(), key=lambda kv: (-kv[1], kv[0].lower())):
            notes = "GBID/hash rows. Used for item/sigil/weapon/character hash lookup and add/change prompts."
            rows.append(["GBID Hash DB", cat, count, count, notes])
        res_cats = Counter(e.category or "Uncategorized" for e in self.resource_db.entries)
        for cat, count in sorted(res_cats.items(), key=lambda kv: (-kv[1], kv[0].lower())):
            known = sum(1 for e in self.resource_db.entries if (e.category or "Uncategorized") == cat and e.decimal_value is not None)
            notes = "Non-hash public IDs and mechanics/reference rows. Used for previews, lookup pages, and research notes."
            rows.append(["Resource ID DB", cat, count, known, notes])
        from collections import Counter as _Counter
        ref_cats = _Counter(e.category or "Uncategorized" for e in self.reference_db.entries)
        for cat, count in sorted(ref_cats.items(), key=lambda kv: (-kv[1], kv[0].lower())):
            rows.append(["Reference Notes", cat, count, count, "Human-readable rate/mechanics/material notes bundled from Community docs for lookup and planning."])
        rows.append(["Save Unit Labels", "Contextual unit names", len(self.unit_model.records), "save-dependent", "Generated from Save Unit ranges plus item/sigil/weapon hashes inside the opened save."])
        return rows

    def refresh_database_rows(self) -> None:
        if not hasattr(self, "database_model"):
            return
        q = self.database_filter.text().strip().lower() if hasattr(self, "database_filter") else ""
        rows = self._database_coverage_rows()
        if q:
            toks = q.split()
            rows = [r for r in rows if all(t in " ".join(str(x).lower() for x in r) for t in toks)]
        self.database_model.set_rows(rows)
        if hasattr(self, "database_table"):
            self._auto_fit_table(self.database_table)


    def refresh_reference_rows(self) -> None:
        if not hasattr(self, "reference_model"):
            return
        q = self.reference_filter.text().strip() if hasattr(self, "reference_filter") else ""
        rows = [[e.category, e.topic, e.key, e.value, e.notes, e.source] for e in self.reference_db.search(q)]
        self.reference_model.set_rows(rows)
        if hasattr(self, "reference_table"):
            self._auto_fit_table(self.reference_table)

    def current_reference_row(self) -> Optional[List[Any]]:
        if not hasattr(self, "reference_table"):
            return None
        idx = self.reference_table.currentIndex()
        if not idx.isValid():
            return None
        row = idx.row()
        if 0 <= row < len(self.reference_model.rows):
            return self.reference_model.rows[row]
        return None

    def copy_selected_reference_value(self) -> None:
        row = self.current_reference_row()
        if row:
            self.copy_text(str(row[3]))

    def copy_selected_reference_notes(self) -> None:
        row = self.current_reference_row()
        if row:
            self.copy_text(" | ".join(str(x) for x in row[:5] if str(x)))

    def export_reference_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export reference notes", "gbfr_reference_notes.csv", "CSV (*.csv)")
        if not path:
            return
        self.reference_db.save_csv(path)
        QMessageBox.information(self, "Exported", f"Exported {len(self.reference_db.entries)} reference rows.")

    def export_database_coverage_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export database coverage", "gbfr_database_coverage.csv", "CSV (*.csv)")
        if not path:
            return
        rows = self._database_coverage_rows()
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Database", "Category", "Rows", "Known Values", "Notes"])
            writer.writerows(rows)
        QMessageBox.information(self, "Exported", f"Exported {len(rows)} coverage rows.")

    def show_unknown_hash_scan(self) -> None:
        if hasattr(self, "advanced_checkbox"):
            self.advanced_checkbox.setChecked(True)
        self._show_page("Hash Scan")
        if self.save:
            self.run_hash_scan(True)

    def _hash_tool_rows(self) -> List[Dict[str, Any]]:
        text = self.hash_input.text() if hasattr(self, "hash_input") else ""
        parts: List[str] = []
        for chunk in text.replace("\n", ",").split(","):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
        rows: List[Dict[str, Any]] = []
        for value in parts:
            hx = gbfr_hash_hex(value)
            dec = gbfr_hash(value)
            entry = self.item_db.lookup_hash(dec)
            rows.append({
                "input": value,
                "hash": hx,
                "decimal": dec,
                "gbid": entry.item_id if entry else "",
                "name": entry.display_name if entry else "",
                "category": entry.category if entry else "",
            })
        return rows

    def compute_hash_tools(self) -> None:
        rows = self._hash_tool_rows()
        if not rows:
            self.hash_results.setPlainText("Enter at least one GBID/string first.")
            return
        lines = ["Input, Hash, Decimal, DB Match"]
        for r in rows:
            match = f"{r['gbid']} / {r['name']}" if r["gbid"] else ""
            lines.append(f"{r['input']}, 0x{r['hash']}, {r['decimal']}, {match}")
        self.hash_results.setPlainText("\n".join(lines))

    def copy_hash_tool_results(self) -> None:
        if hasattr(self, "hash_results"):
            QApplication.clipboard().setText(self.hash_results.toPlainText())
            self.statusBar().showMessage("Copied hash tool results", 3000)

    def export_hash_tool_csv(self) -> None:
        rows = self._hash_tool_rows()
        if not rows:
            QMessageBox.information(self, "No hashes", "Enter at least one GBID/string first.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export hash results", "gbfr_hash_results.csv", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["input", "hash", "decimal", "gbid", "name", "category"])
            writer.writeheader(); writer.writerows(rows)
        QMessageBox.information(self, "Exported", f"Exported {len(rows)} hash rows.")

    def export_item_db_csv(self) -> None:
        default = str(RESOURCE_DIR / "item_ids_merged_export.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export merged GBID DB", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            self.item_db.save_csv(path)
            QMessageBox.information(self, "Exported", f"Merged GBID database written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def current_resource_entry(self):
        if not hasattr(self, "resource_id_table"):
            return None
        idx = self.resource_id_table.currentIndex()
        if not idx.isValid():
            return None
        return self.resource_id_model.entry_at(idx.row())

    def copy_resource_id(self) -> None:
        entry = self.current_resource_entry()
        if not entry:
            return
        QApplication.clipboard().setText(entry.id_text)


    def decode_entity_prefix(self) -> None:
        text = self.entity_prefix_input.text().strip() if hasattr(self, "entity_prefix_input") else ""
        if not text:
            QMessageBox.information(self, "Entity Prefix", "Enter an ID or path first, such as pl0000, wp2200, em1800, ph720, or st101f00.")
            return
        parts = []
        for item in [x.strip() for x in text.replace(",", "\n").splitlines() if x.strip()]:
            parts.append(describe_entity_code(item))
        self.entity_prefix_result.setPlainText("\n\n".join(parts))

    def copy_entity_prefix_result(self) -> None:
        if hasattr(self, "entity_prefix_result"):
            QApplication.clipboard().setText(self.entity_prefix_result.toPlainText())

    def show_entity_prefix_resource_rows(self) -> None:
        # Resource IDs lives in Advanced by default; show it and filter to the merged prefix rows.
        if hasattr(self, "advanced_checkbox"):
            self.advanced_checkbox.setChecked(True)
        if hasattr(self, "resource_id_filter"):
            self.resource_id_filter.setText("Entity Prefix")
        idx = self.page_indexes.get("Resource IDs") if hasattr(self, "page_indexes") else None
        if idx is not None:
            self.stack.setCurrentIndex(idx)


    def copy_resource_name(self) -> None:
        entry = self.current_resource_entry()
        if not entry:
            return
        QApplication.clipboard().setText(entry.name)

    def export_resource_ids_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export resource IDs", "resource_ids_merged.csv", "CSV (*.csv)")
        if not path:
            return
        self.resource_db.save_csv(path)
        QMessageBox.information(self, "Exported", f"Exported {len(self.resource_db.entries)} resource ID rows.")

    def download_resource_ids(self) -> None:
        urls_path = RESOURCE_DIR / "community_resource_urls.txt"
        urls = []
        if urls_path.exists():
            urls = [line.strip() for line in urls_path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")]
        else:
            urls = DEFAULT_RESOURCE_URLS
        try:
            db, errors = ResourceIdDatabase.download_many(urls, timeout=45)
            merged = ResourceIdDatabase.load_many([RESOURCE_DIR / "resource_ids_seed.csv"])
            merged.merge(db)
            out = RESOURCE_DIR / "resource_ids_downloaded.csv"
            merged.save_csv(out)
            self.resource_db = merged
            self.resource_id_model.set_db(self.resource_db)
            self.unit_model.set_resource_db(self.resource_db)
            self.refresh_model_id_catalog_rows()
            self.refresh_all_views(keep_filter=True)
            msg = f"Downloaded/merged {len(merged.entries)} resource ID rows.\nSaved to {out}."
            if errors:
                msg += "\n\nSome sources failed:\n" + "\n".join(errors[:12])
            QMessageBox.information(self, "Resource IDs", msg)
        except Exception as exc:
            QMessageBox.critical(self, "Download failed", str(exc))

    def audit_google_sheet_sources(self) -> None:
        urls = source_urls_from_text(self.sources_text.toPlainText() if hasattr(self, "sources_text") else self.default_source_urls_text())
        if not urls:
            QMessageBox.information(self, "No sources", "No Google Sheet/source URLs are configured.")
            return
        fetch = QMessageBox.question(
            self,
            "Audit Google Sheet tabs",
            "Download and inspect the configured sheet tabs now?\n\nChoose No to only list configured gids without using the network.",
        ) == QMessageBox.StandardButton.Yes
        try:
            rows = audit_sheet_sources(urls, fetch=fetch, timeout=35, dump_dir=None)
            text = audit_summary(rows)
            if hasattr(self, "sources_status"):
                self.sources_status.setPlainText(text)
            self.statusBar().showMessage("Google Sheet source audit complete", 3500)
        except Exception as exc:
            QMessageBox.critical(self, "Sheet audit failed", str(exc))

    def export_google_sheet_audit_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export Google Sheet Audit CSV", "google_sheet_audit.csv", "CSV Files (*.csv)")
        if not path:
            return
        rows = audit_sheet_sources(source_urls_from_text(self.default_source_urls_text()), fetch=False)
        write_audit_csv(rows, path)
        QMessageBox.information(self, "Exported", f"Wrote {path}")


    def download_sources_from_page(self) -> None:
        text = self.sources_text.toPlainText() if hasattr(self, "sources_text") else self.default_source_urls_text()
        urls = source_urls_from_text(text)
        if not urls:
            QMessageBox.information(self, "No sources", "Paste at least one CSV or Google Sheets URL first.")
            return
        try:
            db, errors = ItemDatabase.download_many(urls, timeout=35)
            self.merge_item_db(db, "Downloaded sheet/URL sources")
            out = RESOURCE_DIR / "item_ids_sheet_merged.csv"
            self.item_db.save_csv(out)
            msg = f"Downloaded/merged {len(db)} source rows. Total DB rows: {len(self.item_db)}\nCached to: {out}"
            if errors:
                msg += "\n\nSome sources failed:\n" + "\n".join(errors[:12])
            QMessageBox.information(self, "Sources merged", msg)
        except Exception as exc:
            QMessageBox.critical(self, "Download failed", str(exc))


    def _add_browser_tab_scope(self, tab: Optional[str] = None) -> str:
        tab = tab or getattr(self, "_add_browser_tab", "Safe Add")
        return {
            "Safe Add": "Safe For Loaded Save",
            "Items": "Items / Materials",
            "Sigils": "Sigils",
            "Weapons": "Weapons",
            "Wallet": "Wallet / Profile",
            "Lookup": "All Database Rows",
        }.get(tab, "Safe For Loaded Save")

    def _effective_add_browser_category(self) -> str:
        manual = self.add_browser_category.currentText() if hasattr(self, "add_browser_category") else "Auto"
        if manual and manual != "Auto":
            return manual
        return self._add_browser_tab_scope()

    def _refresh_add_browser_tab_buttons(self) -> None:
        current = getattr(self, "_add_browser_tab", "Safe Add")
        for btn in getattr(self, "_add_browser_tab_buttons", []):
            btn.blockSignals(True)
            btn.setChecked(btn.text() == current)
            btn.blockSignals(False)

    def _set_add_browser_tab(self, tab: str) -> None:
        self._add_browser_tab = tab
        self._refresh_add_browser_tab_buttons()
        if hasattr(self, "add_browser_category"):
            self.add_browser_category.blockSignals(True)
            self.add_browser_category.setCurrentText("Auto")
            self.add_browser_category.blockSignals(False)
        self._update_add_browser_subtype_filter()
        self.schedule_add_browser_refresh()

    def _update_add_browser_subtype_filter(self) -> None:
        if not hasattr(self, "add_browser_subtype_filter"):
            return
        current = self.add_browser_subtype_filter.currentText()
        tab = getattr(self, "_add_browser_tab", "Safe Add")
        if tab in {"Safe Add", "Items", "Wallet"}:
            opts = ["All subtypes", "Missing Safe", "Already Owned", "Materials", "Consumables", "Wrightstones", "Glitterstones", "Tickets / Badges", "Wallet", "Relics / Curios"]
        elif tab == "Sigils":
            opts = ["All subtypes", "Damage / Power", "Defense / HP", "Cooldown / Utility", "Resistance", "Character / Warpath", "Special / Unique", "V+ / Endgame", "Unknown / Dummy"]
        elif tab == "Weapons":
            opts = ["All subtypes", "Apocalypse / Terminus", "Ascension / Awakened", "Base Weapons", "DLC Characters", "Unknown / Empty Name"]
        else:
            opts = ["All subtypes", "Characters", "Traits / Skills", "Models", "Phases", "Quests", "Reference Only"]
        self.add_browser_subtype_filter.blockSignals(True)
        self.add_browser_subtype_filter.clear()
        self.add_browser_subtype_filter.addItems(opts)
        if current in opts:
            self.add_browser_subtype_filter.setCurrentText(current)
        self.add_browser_subtype_filter.blockSignals(False)

    def _set_add_browser_quick_filter(self, query: str, tab_or_category: str, subtype: str = "All subtypes") -> None:
        if tab_or_category in {"Safe Add", "Items", "Sigils", "Weapons", "Wallet", "Lookup"}:
            self._add_browser_tab = tab_or_category
            self._refresh_add_browser_tab_buttons()
            if hasattr(self, "add_browser_category"):
                self.add_browser_category.blockSignals(True)
                self.add_browser_category.setCurrentText("Auto")
                self.add_browser_category.blockSignals(False)
        elif hasattr(self, "add_browser_category"):
            self.add_browser_category.blockSignals(True)
            self.add_browser_category.setCurrentText(tab_or_category)
            self.add_browser_category.blockSignals(False)
        self._update_add_browser_subtype_filter()
        if hasattr(self, "add_browser_subtype_filter") and subtype:
            idx = self.add_browser_subtype_filter.findText(subtype)
            if idx >= 0:
                self.add_browser_subtype_filter.blockSignals(True)
                self.add_browser_subtype_filter.setCurrentIndex(idx)
                self.add_browser_subtype_filter.blockSignals(False)
        if hasattr(self, "add_browser_filter"):
            self.add_browser_filter.blockSignals(True)
            self.add_browser_filter.setText(query)
            self.add_browser_filter.blockSignals(False)
        self.refresh_add_browser_rows()

    def schedule_add_browser_refresh(self) -> None:
        if bool(getattr(self, "_save_in_progress", False)):
            self._mark_stale_pages(["Add / Equip Browser"])
            return
        timer = getattr(self, "_add_browser_refresh_timer", None)
        if timer is not None:
            timer.start()
        else:
            self.refresh_add_browser_rows()

    def _invalidate_add_browser_indexes(self) -> None:
        self._add_browser_index_key = None
        self._add_browser_active_material_hashes = set()
        self._add_browser_safe_template_hashes = set()
        self._add_browser_empty_counts = {"items": 0, "sigils": 0, "weapons": 0}

    def _ensure_add_browser_indexes(self) -> None:
        """Build fast lookup sets for Add / Equip Browser save-safe filtering.

        The browser database is now large after syncing all Community resources.
        Do not scan/group the save once per database row. Build the active
        material/template hash sets once per loaded save and let row filtering
        be O(1) for each database entry.
        """
        key = id(self.save) if self.save else None
        if getattr(self, "_add_browser_index_key", None) == key:
            return
        self._add_browser_index_key = key
        self._add_browser_active_material_hashes = set()
        self._add_browser_safe_template_hashes = set()
        self._add_browser_empty_counts = {"items": 0, "sigils": 0, "weapons": 0}
        if not self.save:
            return
        try:
            grouped = self.save.group_by_unit([1801, 1802, 1803, 1804, 1805, 1806, 1807])
            templates = getattr(self, "material_bank_templates", {}) or {}
            for _unit_id, fields in grouped.items():
                hrec = fields.get(1801)
                qrec = fields.get(1802)
                if not hrec or not qrec:
                    continue
                try:
                    item_hash = int(self._record_first_value(hrec, 0)) & 0xFFFFFFFF
                except Exception:
                    continue
                if not item_hash or item_hash == EMPTY_HASH:
                    continue
                if self._material_bank_slot_is_active(fields):
                    self._add_browser_active_material_hashes.add(item_hash)
                tmpl = templates.get(item_hash)
                if tmpl and (int(tmpl.get("state_1803", 0) or 0) != 0 or int(tmpl.get("index_1804", 0) or 0) != 0):
                    self._add_browser_safe_template_hashes.add(item_hash)
        except Exception:
            self._add_browser_active_material_hashes = set()
            self._add_browser_safe_template_hashes = set()
        try:
            self._add_browser_empty_counts = {
                "items": self.count_empty_item_slots(),
                "sigils": self.count_empty_sigil_slots(),
                "weapons": self.count_empty_weapon_slots(),
            }
        except Exception:
            self._add_browser_empty_counts = {"items": 0, "sigils": 0, "weapons": 0}

    def _is_curio_relic_entry(self, entry) -> bool:
        """Return True for relic/curio rows that must not be written as normal inventory.

        Community's Save Unit notes place Curio/ItemJunk data under ItemManager
        1901-1904 and 2001-2004, not normal stackable 1801/1802 rows.
        The item ID commonly appears as ITEM_19_xxxx in notes. Keep these
        separated so the Add/Equip browser does not put them into material or
        generic item slots by mistake.
        """
        cid = (getattr(entry, "item_id", "") or "").upper()
        cat = (getattr(entry, "category", "") or "").lower()
        name = (getattr(entry, "display_name", "") or "").lower()
        aliases = (getattr(entry, "alias_text", "") or "").lower()
        return (
            cid.startswith("ITEM_19")
            or "curio" in cat
            or "relic" in cat
            or "itemjunk" in cat
            or "curio" in name
            or "relic" in name
            or "curio" in aliases
            or "relic" in aliases
        )

    def _add_browser_use_as(self, entry) -> str:
        cid = (getattr(entry, "item_id", "") or "").upper()
        cat = (getattr(entry, "category", "") or "").lower()
        if self._is_curio_relic_entry(entry):
            return "Relic / Curio Lookup"
        if cid.startswith("GEEN_") or "sigil" in cat or "gem" in cat:
            return "Sigil"
        if cid.startswith("WEP_") or cat == "weapon":
            return "Weapon"
        if cid.startswith("ITEM_") or any(w in cat for w in ["material", "currency", "item", "treasure", "consumable", "ticket", "wrightstone", "glitterstone", "crew"]):
            return "Item / Material"
        if cid.startswith("PL") or cat == "character":
            return "Character Lookup"
        if cid.startswith("SKILL_") or "skill" in cat or "trait" in cat:
            return "Trait Lookup"
        return "Reference Only"

    def _add_browser_wallet_target(self, entry):
        """Return wallet/profile routing for database rows such as Rupies/MSP.

        Those values are not normal ItemManager stacks. Showing them in the
        browser is useful, but writing them through 180x/210x inventory rows
        either does nothing in-game or creates bad duplicate inventory data.
        """
        return self._wallet_field_for_item_key(getattr(entry, "display_name", ""), getattr(entry, "hash_value", None))

    def _add_browser_has_active_material_stack(self, entry) -> bool:
        if not self.save:
            return False
        self._ensure_add_browser_indexes()
        try:
            return (int(entry.hash_value) & 0xFFFFFFFF) in self._add_browser_active_material_hashes
        except Exception:
            return False

    def _add_browser_has_safe_material_template(self, entry) -> bool:
        if not self.save:
            return False
        self._ensure_add_browser_indexes()
        try:
            h = int(entry.hash_value) & 0xFFFFFFFF
            return h in self._add_browser_active_material_hashes or h in self._add_browser_safe_template_hashes
        except Exception:
            return False

    def _add_browser_status_for_entry(self, entry) -> str:
        use_as = self._add_browser_use_as(entry)
        name = (getattr(entry, "display_name", "") or "").lower()
        if name.startswith("unnamed / reserved") or name.startswith("reserved /"):
            return "Reserved/unknown row; hidden from safe mode."
        wallet = self._add_browser_wallet_target(entry)
        if wallet is not None:
            field_id, label, cap = wallet
            return f"Wallet/profile value: writes UserDataManager {field_id} ({label}), cap {cap:,}."
        if use_as == "Relic / Curio Lookup":
            return "Special ItemJunk/Curio data: uses 1901-1904/2001-2004, not normal 1801/1802 material quantity. Add/edit is disabled until the full curio row contract is verified."
        if use_as == "Item / Material":
            if not self.save:
                return "Load a save to check whether this material already has an active 180x stack."
            if self._is_material_bank_entry(entry.hash_value):
                if self._add_browser_has_active_material_stack(entry):
                    return "Safe: existing active 1801/1802 material stack found; quantity update only."
                if self._add_browser_has_safe_material_template(entry):
                    return "Safe add: matching inactive 1801 row found and known-good 1803-1807 template is available."
                return "Not safe to add yet: no active stack or verified 180x template for this save."
            return "Experimental: non-material ItemManager slot; only use on a backup."
        if use_as == "Sigil":
            
            self._ensure_add_browser_indexes()
            return f"Safe if an empty 270x sigil slot exists. Empty slots: {self._add_browser_empty_counts.get('sigils', 0) if self.save else 0}."
        if use_as == "Weapon":
            
            self._ensure_add_browser_indexes()
            return f"Safe if an empty weapon slot exists. Empty slots: {self._add_browser_empty_counts.get('weapons', 0) if self.save else 0}."
        if use_as == "Relic / Curio Lookup":
            return "Reference only for now: curio/relic rows are stored under ItemJunk 1901-1904/2001-2004, not normal materials."
        if use_as == "Trait Lookup":
            return "Reference only: traits are not inventory sigils by themselves."
        if use_as == "Character Lookup":
            return "Reference only: useful for worn-by/equipment ownership hashes."
        return "Reference hash; not directly addable to inventory."

    def _add_browser_status_kind(self, entry) -> str:
        use_as = self._add_browser_use_as(entry)
        if self._add_browser_wallet_target(entry) is not None:
            return "ready"
        if use_as == "Relic / Curio Lookup":
            return "reference"
        if use_as == "Item / Material":
            if self.save and self._is_material_bank_entry(entry.hash_value):
                if self._add_browser_has_active_material_stack(entry):
                    return "owned"
                if self._add_browser_has_safe_material_template(entry):
                    return "missing"
                return "blocked"
            return "blocked"
        if use_as in {"Sigil", "Weapon"}:
            return "ready" if self.save else "blocked"
        if use_as in {"Character Lookup", "Trait Lookup", "Reference Only"}:
            return "reference"
        return "blocked"

    def _add_browser_status_filter_match(self, entry) -> bool:
        status = self.add_browser_status_filter.currentText() if hasattr(self, "add_browser_status_filter") else "All status"
        if status == "All status":
            return True
        kind = self._add_browser_status_kind(entry)
        if status == "Ready / Safe":
            return kind in {"ready", "missing", "owned"}
        if status == "Missing / Safe Add":
            return kind == "missing"
        if status == "Already Owned":
            return kind == "owned"
        if status == "Has Empty Slot":
            return self._add_browser_use_as(entry) in {"Sigil", "Weapon"} and bool(self.save)
        if status == "Blocked / Not Safe":
            return kind == "blocked"
        if status == "Reference Only":
            return kind == "reference"
        return True

    def _add_browser_subtype_match(self, entry) -> bool:
        subtype = self.add_browser_subtype_filter.currentText() if hasattr(self, "add_browser_subtype_filter") else "All subtypes"
        if subtype in ("", "All subtypes"):
            return True
        cid = (getattr(entry, "item_id", "") or "").upper()
        cat = (getattr(entry, "category", "") or "").lower()
        name = (getattr(entry, "display_name", "") or "").lower()
        aliases = (getattr(entry, "alias_text", "") or "").lower()
        blob = " ".join([cid.lower(), cat, name, aliases])
        kind = self._add_browser_status_kind(entry)
        use_as = self._add_browser_use_as(entry)
        if subtype == "Missing Safe":
            return kind == "missing"
        if subtype == "Already Owned":
            return kind == "owned"
        if subtype == "Materials":
            return cid.startswith("ITEM_") and not self._is_curio_relic_entry(entry) and self._add_browser_wallet_target(entry) is None
        if subtype == "Consumables":
            return cid.startswith("ITEM_13") or any(x in name for x in ["potion", "ambrosia"])
        if subtype == "Wrightstones":
            return cid.startswith(("ITEM_25", "ITEM_26", "ITEM_27", "ITEM_28")) or "wrightstone" in name
        if subtype == "Glitterstones":
            return cid.startswith("ITEM_34") or "glitter" in name
        if subtype == "Tickets / Badges":
            return any(x in blob for x in ["ticket", "badge", "dalia", "voucher", "card"])
        if subtype == "Wallet":
            return self._add_browser_wallet_target(entry) is not None
        if subtype == "Relics / Curios":
            return self._is_curio_relic_entry(entry)
        if subtype == "Damage / Power":
            return any(x in name for x in ["attack", "damage", "cap", "tyranny", "stamina", "crit", "critical", "exploiter", "enmity"])
        if subtype == "Defense / HP":
            return any(x in name for x in ["health", "aegis", "garrison", "defense", "guard", "stout", "guts"])
        if subtype == "Cooldown / Utility":
            return any(x in name for x in ["cooldown", "cascade", "uplift", "potion", "nimble", "dodge", "quick"])
        if subtype == "Resistance":
            return "resistance" in name or "resist" in name
        if subtype == "Character / Warpath":
            return "warpath" in name or "awakening" in name or "'s" in name
        if subtype == "Special / Unique":
            return any(x in name for x in ["war elemental", "flight over fight", "glass cannon", "berserker", "alpha", "beta", "gamma", "ain", "boundary"])
        if subtype == "V+ / Endgame":
            return "v+" in name or cid.endswith("_24") or cid.endswith("_94")
        if subtype == "Unknown / Dummy":
            return not name.strip() or "dummy" in name or "unknown" in name or "reserved" in name
        if subtype == "Apocalypse / Terminus":
            return any(x in name for x in ["apocalypse", "bahamut", "celestial", "star", "gateway", "false"])
        if subtype == "Ascension / Awakened":
            return any(x in name for x in ["awaken", "ascension", "omega", "coda", "star key", "azure", "dominant", "purifier", "exalted"])
        if subtype == "Base Weapons":
            return use_as == "Weapon" and not any(x in name for x in ["apocalypse", "bahamut", "omega", "coda", "celestial"])
        if subtype == "DLC Characters":
            return any(x in cid for x in ["PL2100", "PL2200", "PL2300"])
        if subtype == "Unknown / Empty Name":
            return not name.strip() or name in {'""', "unknown"} or "unnamed" in name
        if subtype == "Characters":
            return use_as == "Character Lookup"
        if subtype == "Traits / Skills":
            return use_as == "Trait Lookup" or cid.startswith("SKILL_") or cid.startswith("GEEN_")
        if subtype == "Models":
            return "model" in cat or "model" in cid.lower()
        if subtype == "Phases":
            return "phase" in cat or "phase" in cid.lower()
        if subtype == "Quests":
            return "quest" in cat or "quest" in cid.lower()
        if subtype == "Reference Only":
            return use_as in {"Reference Only", "Character Lookup", "Trait Lookup", "Relic / Curio Lookup"}
        return True

    def _add_browser_category_match(self, entry, category: str) -> bool:
        if category in ("", "All Database Rows", "All"):
            return True
        use_as = self._add_browser_use_as(entry)
        cat = (getattr(entry, "category", "") or "").lower()
        if category in ("Addable Only", "Addable Types"):
            return use_as in {"Item / Material", "Sigil", "Weapon"}
        if category == "Safe For Loaded Save":
            name = (getattr(entry, "display_name", "") or "").lower()
            if name.startswith("unnamed / reserved") or name.startswith("reserved /"):
                return False
            if self._add_browser_wallet_target(entry) is not None:
                return True
            if use_as in {"Sigil", "Weapon"}:
                return True
            if use_as == "Item / Material":
                return bool(self.save and self._add_browser_has_safe_material_template(entry))
            return False
        if category == "Wallet / Profile":
            return self._add_browser_wallet_target(entry) is not None
        if category == "Relics / Curios":
            return use_as == "Relic / Curio Lookup"
        if category == "Sigils":
            return use_as == "Sigil"
        if category == "Items / Materials":
            return use_as == "Item / Material"
        if category == "Weapons":
            return use_as == "Weapon"
        if category == "Characters":
            return use_as == "Character Lookup"
        if category == "Traits / Skills":
            return use_as == "Trait Lookup"
        if category == "Reference / Models":
            return use_as == "Reference Only" or any(w in cat for w in ["model", "phase", "enemy", "object"])
        return True

    def refresh_add_browser_rows(self) -> None:
        if not hasattr(self, "add_browser_model"):
            return
        query = (self.add_browser_filter.text() if hasattr(self, "add_browser_filter") else "").strip()
        category = self._effective_add_browser_category()
        self._ensure_add_browser_indexes()

        # Large all-database views are search-first so opening the tab is instant.
        if not query and category in {"All Database Rows", "Reference / Models", "Traits / Skills"}:
            self.add_browser_model.set_rows([[
                "Search first",
                "Type a name, GBID, or hash to load this large database view",
                "—",
                "—",
                category,
                "The merged Community database is large; this view is intentionally lazy for speed.",
            ]])
            self.refresh_add_browser_character_combo()
            self.update_add_browser_detail()
            return

        # Keep the table responsive. Users can narrow with search/filters.
        hard_limit = 750 if query else 500
        search_limit = 5000 if query else 2500
        entries = self.item_db.search(query, limit=search_limit)
        rows = []
        skipped = 0
        for entry in entries:
            if not self._add_browser_category_match(entry, category):
                continue
            if not self._add_browser_status_filter_match(entry):
                continue
            if not self._add_browser_subtype_match(entry):
                continue
            rows.append([
                self._add_browser_use_as(entry),
                entry.display_name,
                entry.item_id,
                entry.hash_hex,
                entry.category,
                self._add_browser_status_for_entry(entry),
            ])
            if len(rows) >= hard_limit:
                break
        # If the search returned more than we are displaying, show a clear hint.
        if len(rows) >= hard_limit:
            rows.append([
                "More results hidden",
                "Narrow the search to show more exact matches",
                "—",
                "—",
                category,
                f"Showing first {hard_limit} matching rows for speed.",
            ])
        self.add_browser_model.set_rows(rows)
        if hasattr(self, "add_browser_table"):
            self._set_table_widths(self.add_browser_table, {0: 120, 1: 300, 2: 170, 3: 120, 4: 150, 5: 360})
        self.refresh_add_browser_character_combo()
        self.update_add_browser_detail()
        if hasattr(self, "statusBar"):
            tab = getattr(self, "_add_browser_tab", "Safe Add")
            self.statusBar().showMessage(f"Add / Equip Browser: {tab} · {category} · showing {len(rows):,} rows", 1800)

    def refresh_add_browser_character_combo(self) -> None:
        if not hasattr(self, "add_browser_equip_combo"):
            return
        # Character list does not change per browser filter; build it once.
        choices = getattr(self, "_add_browser_character_choices", None)
        if choices is None:
            choices = []
            for entry in self.item_db.search("character", limit=1000):
                cid = (entry.item_id or "").upper()
                if cid.startswith("PL") or entry.category.lower() == "character":
                    choices.append((f"{entry.display_name} ({entry.item_id})", entry.hash_value & 0xFFFFFFFF))
            self._add_browser_character_choices = choices
        current = self.add_browser_equip_combo.currentData()
        self.add_browser_equip_combo.blockSignals(True)
        self.add_browser_equip_combo.clear()
        self.add_browser_equip_combo.addItem("None / Unequipped", EMPTY_HASH)
        for label, value in choices:
            self.add_browser_equip_combo.addItem(label, value)
        idx = self.add_browser_equip_combo.findData(current)
        if idx >= 0:
            self.add_browser_equip_combo.setCurrentIndex(idx)
        self.add_browser_equip_combo.blockSignals(False)

    def selected_add_browser_entry(self):
        if not hasattr(self, "add_browser_table"):
            return None
        idx = self.add_browser_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.add_browser_model.rows):
            return None
        row = self.add_browser_model.rows[idx.row()]
        return self.item_db.by_id.get(str(row[2]).upper()) or self._entry_from_hash_text(row[3])

    def _entry_from_hash_text(self, text: Any):
        try:
            return self.item_db.lookup_hash(int(str(text).replace("0x", ""), 16))
        except Exception:
            return None

    def update_add_browser_detail(self) -> None:
        if not hasattr(self, "add_browser_detail_label"):
            return
        entry = self.selected_add_browser_entry()
        if not entry:
            empty = ""
            if self.save:
                self._ensure_add_browser_indexes()
                counts = self._add_browser_empty_counts
                empty = f"<br><br><b>Empty reusable slots</b><br>Items/materials: {counts.get('items', 0)}<br>Sigils/gems: {counts.get('sigils', 0)}<br>Weapons: {counts.get('weapons', 0)}"
            self.add_browser_detail_label.setText("Select a row to see whether it can be added to the save." + empty)
            return
        use_as = self._add_browser_use_as(entry)
        status = self._add_browser_status_for_entry(entry)
        aliases = entry.alias_text or "—"
        self._ensure_add_browser_indexes()
        counts = self._add_browser_empty_counts
        self.add_browser_detail_label.setText(
            f"<b>{entry.display_name}</b><br>"
            f"Use as: <b>{use_as}</b><br>"
            f"GBID: {entry.item_id}<br>"
            f"Database category: {entry.category}<br>"
            f"Hash: 0x{entry.hash_hex}<br>"
            f"Aliases/source notes: {aliases}<br><br>"
            f"{status}<br><br>"
            f"<b>Empty reusable slots</b><br>"
            f"Items/materials: {counts.get('items', 0) if self.save else 0}<br>"
            f"Sigils/gems: {counts.get('sigils', 0) if self.save else 0}<br>"
            f"Weapons: {counts.get('weapons', 0) if self.save else 0}"
        )

    def add_browser_selected_default(self) -> None:
        entry = self.selected_add_browser_entry()
        if not entry:
            return
        use_as = self._add_browser_use_as(entry)
        if use_as == "Sigil":
            self.add_browser_selected_as_sigil()
        elif use_as == "Weapon":
            self.add_browser_selected_as_weapon()
        elif use_as == "Item / Material":
            self.add_browser_selected_as_item()
        else:
            QMessageBox.information(self, "Reference-only row", f"{entry.display_name} is a {use_as} row. It is useful for lookup, but it is not an inventory item/sigil/weapon that can be added directly.")

    def add_browser_selected_as_item(self) -> None:
        entry = self.selected_add_browser_entry()
        if not entry:
            QMessageBox.information(self, "No entry selected", "Select a database entry first.")
            return
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save before applying browser actions.")
            return

        # Wallet/profile values such as Rupies/MSP are direct UserDataManager
        # fields, not inventory stacks. Route them explicitly.
        wallet = self._add_browser_wallet_target(entry)
        if wallet is not None:
            field_id, label, cap = wallet
            qty = min(int(self.add_browser_qty_spin.value()), int(cap))
            rec = self.save.find_first("int", int(field_id), 0) if self.save else None
            if rec is None:
                QMessageBox.information(self, "Wallet field missing", f"Could not find UserDataManager field {field_id} for {label} in this save.")
                return
            if self._set_record_first_value(rec, qty, f"{label} wallet value"):
                self._after_editor_patch(f"Set {label} -> {qty:,}.")
                QMessageBox.information(self, "Wallet updated", f"Set {label} to {qty:,} in memory. Use Save As and verify in-game.")
            else:
                QMessageBox.information(self, "No change", f"{label} already matched {qty:,}.")
            return

        if self._is_curio_relic_entry(entry):
            QMessageBox.warning(
                self,
                "Relic/curio add blocked",
                f"{entry.display_name} appears to be a relic/curio ItemJunk row.\n\n"
                "Curios/relics are not normal 1801/1802 material stacks. They use ItemManager/ItemJunk fields 1901-1904 and 2001-2004, so this editor blocks writing them through the item/material path for now."
            )
            return

        if self._add_browser_use_as(entry) != "Item / Material":
            QMessageBox.information(self, "Wrong add type", f"{entry.display_name} is marked as {self._add_browser_use_as(entry)}, not an item/material or wallet value.")
            return

        if self._is_material_bank_entry(entry.hash_value) and not self._add_browser_has_safe_material_template(entry):
            QMessageBox.warning(
                self,
                "Unsafe material add blocked",
                f"{entry.display_name} does not have an active 180x material stack or verified template in this save.\n\n"
                "The old Add All path crashed the game by force-filling inactive material catalog rows. "
                "This build only updates active stacks or activates matching inactive 1801 rows when a known-good 1803-1807 template exists."
            )
            return

        result = self._add_item_hash_qty_to_empty_slot(entry.hash_value, self.add_browser_qty_spin.value(), 1)
        if result:
            self._after_editor_patch(f"Browser item action: {result} in memory.")
            QMessageBox.information(self, "Item/material updated", f"Updated in memory:\n{result}\n\nUse Save As first and verify in-game.")
        else:
            QMessageBox.information(self, "No safe target", "Could not find a safe active material stack or reusable item slot for this entry.")

    def add_browser_selected_as_sigil(self) -> None:
        entry = self.selected_add_browser_entry()
        if not entry:
            QMessageBox.information(self, "No entry selected", "Select a database entry first.")
            return
        if self._add_browser_use_as(entry) != "Sigil":
            QMessageBox.information(self, "Wrong add type", f"{entry.display_name} is marked as {self._add_browser_use_as(entry)}, not a sigil. GEEN_* rows are addable sigils; SKILL_/trait rows are lookup-only.")
            return
        owner_hash = self.add_browser_equip_combo.currentData() if hasattr(self, "add_browser_equip_combo") else EMPTY_HASH
        result = self._add_sigil_hash_level_to_empty_slot(
            entry.hash_value,
            self.add_browser_level_spin.value(),
            self.add_browser_locked_check.isChecked(),
            owner_hash=owner_hash,
        )
        if not result:
            QMessageBox.information(self, "No empty sigil slot", "Could not find an empty sigil slot to reuse.")
            return
        self._after_editor_patch(f"Added {result} in memory.")

    def _last_added_sigil_unit_from_result(self, result: str) -> Optional[int]:
        import re
        m = re.search(r"unit\s+(\d+)", result or "")
        return int(m.group(1)) if m else None

    def _find_last_matching_sigil_slot(self, sigil_hash: int) -> Optional[Dict[str, Any]]:
        if not self.save:
            return None
        grouped = self.save.group_by_unit([2703, 2706])
        found = None
        for unit_id, fields in sorted(grouped.items()):
            h = fields.get(2703)
            if h and self._record_first_value(h, 0) == (sigil_hash & 0xFFFFFFFF):
                found = {"unit_id": unit_id, "hash_rec": h, "worn_rec": fields.get(2706)}
        return found

    def add_browser_selected_as_weapon(self) -> None:
        entry = self.selected_add_browser_entry()
        if not entry:
            QMessageBox.information(self, "No entry selected", "Select a database entry first.")
            return
        if self._add_browser_use_as(entry) != "Weapon":
            QMessageBox.information(self, "Wrong add type", f"{entry.display_name} is marked as {self._add_browser_use_as(entry)}, not a weapon.")
            return
        result = self._add_weapon_hash_xp_to_empty_slot(entry.hash_value, self.add_browser_xp_spin.value(), None)
        if result:
            self._after_editor_patch(f"Added {result} in memory.")
            QMessageBox.information(self, "Weapon added", f"Added in memory:\n{result}\n\nUse Save As first and verify in-game.")
        else:
            QMessageBox.information(self, "No empty weapon slot", "Could not find an empty weapon slot to reuse.")

    def copy_add_browser_selected_hash(self) -> None:
        entry = self.selected_add_browser_entry()
        if entry:
            self.copy_text(f"0x{entry.hash_hex}")

    def selected_gbid_entry(self):
        idx = self.gbid_table.currentIndex()
        if not idx.isValid():
            return None
        return self.gbid_model.entry_at(idx.row())

    def copy_selected_gbid_hash(self) -> None:
        entry = self.selected_gbid_entry()
        if entry:
            self.copy_text(entry.hash_hex)

    def copy_selected_gbid_decimal(self) -> None:
        entry = self.selected_gbid_entry()
        if entry:
            self.copy_text(str(entry.hash_value))

    def copy_selected_gbid_id(self) -> None:
        entry = self.selected_gbid_entry()
        if entry:
            self.copy_text(entry.item_id)



    def add_selected_gbid_as_item(self) -> None:
        entry = self.selected_gbid_entry()
        if not entry:
            QMessageBox.information(self, "No GBID selected", "Select a GBID row first.")
            return
        self._add_item_hash_to_empty_slot(entry.hash_value, entry.display_name, entry.item_id)

    def add_selected_gbid_as_sigil(self) -> None:
        entry = self.selected_gbid_entry()
        if not entry:
            QMessageBox.information(self, "No GBID selected", "Select a GBID row first.")
            return
        self._add_sigil_hash_to_empty_slot(entry.hash_value, entry.display_name, entry.item_id)

    def add_selected_gbid_as_weapon(self) -> None:
        entry = self.selected_gbid_entry()
        if not entry:
            QMessageBox.information(self, "No GBID selected", "Select a GBID row first.")
            return
        self._add_weapon_hash_to_empty_slot(entry.hash_value, entry.display_name, entry.item_id)

    def find_unit_row(self, kind: str, id_type: int, unit_id: int) -> Optional[int]:
        for i, rec in enumerate(self.unit_model.filtered):
            if rec.kind == kind and rec.id_type == id_type and rec.unit_id == unit_id:
                return i
        return None

    def jump_to_record(self, kind: str, id_type: int, unit_id: int) -> None:
        if not self.save:
            return
        self.stack.setCurrentIndex(self.page_indexes.get("Units", 1))
        self.filter_edit.setText(str(id_type))
        self.unit_model.set_filter(self.filter_edit.text())
        row = self.find_unit_row(kind, id_type, unit_id)
        if row is None:
            self.filter_edit.setText(f"{id_type}")
            row = self.find_unit_row(kind, id_type, unit_id)
        if row is not None:
            index = self.unit_model.index(row, 0)
            self.unit_table.setCurrentIndex(index)
            self.unit_table.scrollTo(index)
            self.unit_selected()
            self.copy_selected_values_to_editor()

    def jump_to_item_unit(self) -> None:
        if not self.save:
            return
        idx = self.item_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.item_model.rows):
            return
        slot_value = self.item_model.rows[idx.row()][0]
        if isinstance(slot_value, str) and slot_value.startswith("wallet:"):
            field_id = int(slot_value.split(":", 1)[1])
            recs = self.save.find(id_type=field_id, unit_id=0)
            if recs:
                self.jump_to_record(recs[0].kind, recs[0].id_type, recs[0].unit_id)
                return
        unit_id = int(slot_value)
        for id_type in [1801, 1802, 2102, 1901, 2002, 2103, 2105]:
            recs = self.save.find(id_type=id_type, unit_id=unit_id)
            if recs:
                self.jump_to_record(recs[0].kind, recs[0].id_type, recs[0].unit_id)
                return

    def jump_to_sigil_unit(self) -> None:
        if not self.save:
            return
        idx = self.sigil_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.sigil_model.rows):
            return
        unit_id = int(self.sigil_model.rows[idx.row()][0])
        recs = self.save.find(id_type=2703, unit_id=unit_id) or self.save.find(id_type=2704, unit_id=unit_id)
        if recs:
            self.jump_to_record(recs[0].kind, recs[0].id_type, recs[0].unit_id)

    def jump_to_weapon_unit(self) -> None:
        if not self.save:
            return
        idx = self.weapon_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.weapon_model.rows):
            return
        unit_id = int(self.weapon_model.rows[idx.row()][0])
        recs = self.save.find(id_type=2803, unit_id=unit_id) or self.save.find(id_type=2804, unit_id=unit_id)
        if recs:
            self.jump_to_record(recs[0].kind, recs[0].id_type, recs[0].unit_id)

    def jump_to_candidate_unit(self) -> None:
        if not self.save:
            return
        idx = self.candidate_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.candidate_model.rows):
            return
        row = self.candidate_model.rows[idx.row()]
        kind = str(row[2])
        id_type = int(row[3])
        unit_id = int(row[5])
        self.jump_to_record(kind, id_type, unit_id)

    def refresh_unit_map_rows(self) -> None:
        if not self.save:
            self.unit_map_model.set_rows([])
            return
        rows = self.unit_model.unit_labels.rows()
        q = getattr(self, "unit_map_filter_edit", None).text().strip().lower() if hasattr(self, "unit_map_filter_edit") else ""
        if q:
            rows = [row for row in rows if q in " ".join(str(x).lower() for x in row)]
        self.unit_map_model.set_rows(rows)
        if hasattr(self, "unit_map_table"):
            self._auto_fit_table(self.unit_map_table)

    def jump_to_unit_map_unit(self) -> None:
        if not self.save:
            return
        idx = self.unit_map_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.unit_map_model.rows):
            return
        row = self.unit_map_model.rows[idx.row()]
        group = str(row[0]).lower()
        unit_id = int(row[1])
        preferred = {
            "weapon": [2803, 2804, 2815],
            "sigil": [2703, 2704, 2707],
            "item": [2102, 1901, 2002, 2105],
            "character": [1301, 1302, 1315],
            "ability": [3903, 3904],
            "quest": [2570, 2571, 2574, 2501],
            "scenario": [4202, 4201],
            "party": [2201, 2202, 2301],
        }.get(group, [])
        for id_type in preferred:
            recs = self.save.find(id_type=id_type, unit_id=unit_id)
            if recs:
                self.jump_to_record(recs[0].kind, recs[0].id_type, recs[0].unit_id)
                return
        for rec in self.save.records:
            if rec.unit_id == unit_id:
                self.jump_to_record(rec.kind, rec.id_type, rec.unit_id)
                return

    def copy_selected_unit_label(self) -> None:
        idx = self.unit_map_table.currentIndex()
        if idx.isValid() and idx.row() < len(self.unit_map_model.rows):
            self.copy_text(str(self.unit_map_model.rows[idx.row()][2]))

    def export_unit_map_csv(self) -> None:
        default = str((self.save.container.path if self.save else Path("unit_map")).with_suffix(".unit_map.csv"))
        self.export_rows_csv(self.unit_map_model, default)

    def search_loaded_values(self) -> None:
        if not self.save:
            return
        query = self.value_search_edit.text().strip()
        self.value_search_results = search_values(self.save, query, exact=True, limit=1000)
        self.research_text.setPlainText(format_search_text(self.value_search_results, max_rows=120))

    def export_value_search_csv(self) -> None:
        if not self.value_search_results:
            QMessageBox.information(self, "Value search", "Run a value search first.")
            return
        default = str((self.save.container.path if self.save else Path("value_search")).with_suffix(".value_search.csv"))
        path, _ = QFileDialog.getSaveFileName(self, "Export value search CSV", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            write_search_csv(self.value_search_results, path)
            QMessageBox.information(self, "Exported", f"Value search written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))




    def resolve_unknown_hash_patterns(self) -> None:
        if not self.save:
            return
        self.hash_candidate_rows = resolve_unknown_hashes(self.save, self.item_db, limit=5000)
        self.hash_scan_text.setPlainText(format_hash_candidates(self.hash_candidate_rows))

    def export_hash_candidate_csv(self) -> None:
        if not self.save:
            return
        rows = getattr(self, "hash_candidate_rows", [])
        if not rows:
            rows = resolve_unknown_hashes(self.save, self.item_db, limit=5000)
            self.hash_candidate_rows = rows
        if not rows:
            QMessageBox.information(self, "No candidate IDs", "No generated ID-pattern matches were found for the remaining unknown hash fields.")
            return
        default = str((self.save.container.path if self.save else Path("hash_candidates")).with_suffix(".hash_candidates.csv"))
        path, _ = QFileDialog.getSaveFileName(self, "Export generated hash candidates", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            write_hash_candidates_csv(rows, path)
            QMessageBox.information(self, "Exported", f"Generated hash candidates written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def run_hash_scan(self, include_unknown: bool = False) -> None:
        if not self.save:
            return
        self.hash_scan_rows = scan_known_hashes(self.save, self.item_db, include_unknown=include_unknown, limit=12000)
        table_rows = []
        for row in self.hash_scan_rows:
            table_rows.append([
                row.get("category", ""),
                row.get("name", ""),
                row.get("gbid", ""),
                row.get("hash", ""),
                row.get("kind", ""),
                row.get("id_type", ""),
                row.get("unit_id", ""),
                row.get("value_index", ""),
                "yes" if row.get("known") else "no",
                row.get("aliases", ""),
            ])
        self.hash_scan_model.set_rows(table_rows)
        self.hash_scan_text.setPlainText(format_hash_scan_text(self.hash_scan_rows, max_rows=80))

    def export_hash_scan_csv(self) -> None:
        if not self.hash_scan_rows:
            QMessageBox.information(self, "Hash scan", "Run a hash scan first.")
            return
        default = str((self.save.container.path if self.save else Path("hash_scan")).with_suffix(".hash_scan.csv"))
        path, _ = QFileDialog.getSaveFileName(self, "Export hash scan CSV", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            write_hash_scan_csv(self.hash_scan_rows, path)
            QMessageBox.information(self, "Exported", f"Hash scan written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def jump_to_hash_scan_unit(self) -> None:
        if not self.save:
            return
        idx = self.hash_scan_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.hash_scan_rows):
            return
        row = self.hash_scan_rows[idx.row()]
        self.jump_to_record(str(row["kind"]), int(row["id_type"]), int(row["unit_id"]))

    def export_rows_csv(self, model: SimpleRowsModel, default_name: str) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export CSV", default_name, "CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(model.headers)
                writer.writerows(model.rows)
            QMessageBox.information(self, "Exported", f"CSV written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def export_items_csv(self) -> None:
        default = str((self.save.container.path if self.save else Path("items")).with_suffix(".items.csv"))
        self.export_rows_csv(self.item_model, default)

    def export_sigils_csv(self) -> None:
        default = str((self.save.container.path if self.save else Path("sigils")).with_suffix(".sigils.csv"))
        self.export_rows_csv(self.sigil_model, default)

    def show_unknown_sigils(self) -> None:
        if hasattr(self, "sigil_filter_edit"):
            self.sigil_filter_edit.setText("")
        if hasattr(self, "sigil_show_empty_check"):
            self.sigil_show_empty_check.setChecked(False)
        if hasattr(self, "sigil_known_only_check"):
            self.sigil_known_only_check.setChecked(False)
        if hasattr(self, "sigil_unknown_only_check"):
            self.sigil_unknown_only_check.setChecked(True)
        self.refresh_sigil_rows()

    def clear_sigil_filters(self) -> None:
        if hasattr(self, "sigil_filter_edit"):
            self.sigil_filter_edit.setText("")
        for name in ("sigil_show_empty_check", "sigil_known_only_check", "sigil_unknown_only_check"):
            box = getattr(self, name, None)
            if box is not None:
                box.setChecked(False)
        self.refresh_sigil_rows()

    def _visible_unknown_sigil_rows(self) -> List[List[Any]]:
        rows = getattr(self.sigil_model, "rows", []) if hasattr(self, "sigil_model") else []
        return [row for row in rows if len(row) > 4 and str(row[2]).startswith("Unknown 0x")]

    def copy_visible_unknown_sigil_hashes(self) -> None:
        hashes = []
        for row in self._visible_unknown_sigil_rows():
            h = str(row[4]).removeprefix("0x")
            if h and h not in hashes:
                hashes.append(h)
        if not hashes:
            QMessageBox.information(self, "Unknown sigils", "No unknown sigil hashes are visible right now. Use Show Unknown first if needed.")
            return
        self.copy_text("\n".join(hashes))
        self.statusBar().showMessage(f"Copied {len(hashes)} unknown sigil hashes", 3000)

    def export_unknown_sigil_hashes_csv(self) -> None:
        if not self.save:
            return
        rows = self._visible_unknown_sigil_rows()
        if not rows:
            QMessageBox.information(self, "Unknown sigils", "No unknown sigil hashes are visible right now. Use Show Unknown first if needed.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export unknown sigil hashes", str(self.save.container.path.with_suffix(".unknown_sigil_hashes.csv")), "CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Unit", "Slot", "Hash", "Level", "Worn By", "Flags", "Visible Name"])
                for row in rows:
                    writer.writerow([row[0], row[1], row[4], row[5], row[6], row[8], row[2]])
            QMessageBox.information(self, "Exported", f"Unknown sigil hashes written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def explain_unknown_sigils(self) -> None:
        QMessageBox.information(
            self,
            "Why sigils can still show Unknown",
            "A sigil row shows Unknown when the 32-bit hash in the save is not in the local GBID database yet. "
            "That does not mean the slot is broken. It usually means the row is a V+, character-specific, DLC, generated, or still-unmapped variant.\n\n"
            "Use Show Unknown to isolate them, then Copy Visible Unknown Hashes or Export Unknown Hashes. "
            "Those hashes are what we use to add names safely without touching your save data."
        )

    def copy_selected_item_hash(self) -> None:
        idx = self.item_table.currentIndex()
        if idx.isValid() and idx.row() < len(self.item_model.rows):
            self.copy_text(str(self.item_model.rows[idx.row()][3]).removeprefix("0x"))

    def copy_selected_item_gbid(self) -> None:
        idx = self.item_table.currentIndex()
        if idx.isValid() and idx.row() < len(self.item_model.rows):
            self.copy_text(str(self.item_model.rows[idx.row()][2]))

    def copy_selected_sigil_hash(self) -> None:
        idx = self.sigil_table.currentIndex()
        if idx.isValid() and idx.row() < len(self.sigil_model.rows):
            self.copy_text(str(self.sigil_model.rows[idx.row()][4]).removeprefix("0x"))

    def copy_selected_sigil_gbid(self) -> None:
        idx = self.sigil_table.currentIndex()
        if idx.isValid() and idx.row() < len(self.sigil_model.rows):
            self.copy_text(str(self.sigil_model.rows[idx.row()][3]))

    def export_weapons_csv(self) -> None:
        default = str((self.save.container.path if self.save else Path("weapons")).with_suffix(".weapons.csv"))
        self.export_rows_csv(self.weapon_model, default)

    def export_unknown_weapon_hashes_csv(self) -> None:
        if not self.save:
            return
        rows = [r for r in self.weapon_model.rows if str(r[1]).startswith("Unknown 0x") or str(r[10]).startswith("0x")]
        if not rows:
            QMessageBox.information(self, "Unknown weapons", "No unknown weapon/stone hashes are visible on the Weapons page.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export unknown weapon hashes", str(self.save.container.path.with_suffix(".unknown_weapon_hashes.csv")), "CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.weapon_model.headers)
                writer.writerows(rows)
            QMessageBox.information(self, "Exported", f"Unknown weapon hashes written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def copy_selected_weapon_hash(self) -> None:
        idx = self.weapon_table.currentIndex()
        if idx.isValid() and idx.row() < len(self.weapon_model.rows):
            self.copy_text(str(self.weapon_model.rows[idx.row()][3]).removeprefix("0x"))

    def copy_selected_weapon_gbid(self) -> None:
        idx = self.weapon_table.currentIndex()
        if idx.isValid() and idx.row() < len(self.weapon_model.rows):
            self.copy_text(str(self.weapon_model.rows[idx.row()][2]))


    def _selected_row(self, table: QTableView, model: SimpleRowsModel) -> Optional[List[Any]]:
        idx = table.currentIndex()
        if not idx.isValid() or idx.row() >= len(model.rows):
            return None
        return model.rows[idx.row()]

    def _set_detail_text(self, widget: Any, text: str) -> None:
        if hasattr(widget, "setPlainText"):
            widget.setPlainText(text)
        elif hasattr(widget, "setText"):
            widget.setText(text)

    def update_item_detail(self) -> None:
        if not hasattr(self, "item_detail_label"):
            return
        row = self._selected_row(self.item_table, self.item_model) if hasattr(self, "item_table") else None
        if not row:
            self._set_detail_text(self.item_detail_label, "Select an item row to view its save-backed fields. Quantity edits are available from the table or action buttons below.")
            self._sync_selected_item_quantity_spin(None, None)
            self._set_item_inline_fields(None)
            return
        meta = self._selected_item_meta() or {}
        row_type = "Wallet/Profile value" if meta.get("wallet_value") else ("Safe material quantity" if self._item_meta_has_real_quantity(meta) else "Technical / non-quantity row")
        writable = "Yes" if self._item_meta_has_real_quantity(meta) or meta.get("wallet_value") else "No - shown for reference only"
        self._set_detail_text(
            self.item_detail_label,
            "  •  ".join([
                f"Item: {format_display_value(row[1], 'Item')}",
                f"GBID: {format_display_value(row[2], 'GBID')}",
                f"Hash: {format_hash_value(row[3])}",
                f"Qty: {format_display_value(row[6], 'Quantity')}",
                f"Slot: {format_display_value(row[0], 'Slot')}",
                f"Index: {format_display_value(row[4], 'Index')}",
                f"Flag: {format_display_value(row[5], 'Flag')}",
                f"Type: {row_type}",
                f"Editable: {writable}",
            ])
        )
        self._sync_selected_item_quantity_spin(row, meta)
        self._set_item_inline_fields(row)

    def _sync_selected_item_quantity_spin(self, row: Optional[List[Any]], meta: Optional[Dict[str, Any]] = None) -> None:
        if not hasattr(self, "item_selected_qty_spin"):
            return
        spin = self.item_selected_qty_spin
        spin.blockSignals(True)
        try:
            editable = bool(meta and (meta.get("wallet_value") or self._item_meta_has_real_quantity(meta)))
            spin.setEnabled(editable)
            if row is None or not editable:
                spin.setValue(0)
                return
            value = self._record_first_value(meta.get("qty_rec"), row[6] if len(row) > 6 else 0)
            try:
                spin.setValue(max(0, min(int(value or 0), spin.maximum())))
            except Exception:
                spin.setValue(0)
        finally:
            spin.blockSignals(False)

    def set_selected_item_quantity_from_spin(self) -> None:
        if not self.save or not hasattr(self, "item_table") or not hasattr(self, "item_selected_qty_spin"):
            return
        idx = self.item_table.currentIndex()
        meta = self._selected_item_meta()
        if not idx.isValid() or not meta:
            self.statusBar().showMessage("Select an item first.", 2500)
            return
        if not (meta.get("wallet_value") or self._item_meta_has_real_quantity(meta)):
            self.statusBar().showMessage("Selected row is technical/reference data, not a quantity field.", 4000)
            return
        value = int(self.item_selected_qty_spin.value())
        if self.apply_item_table_cell_edit(idx.row(), 6, value):
            self.update_item_detail()

    def _set_item_inline_fields(self, row: Optional[List[Any]]) -> None:
        """Keep the selected item editor synchronized without firing save edits."""
        if not hasattr(self, "item_identity_edit"):
            return
        editors = [
            self.item_identity_edit,
            self.item_quantity_edit,
            self.item_index_edit,
            self.item_flag_edit,
        ]
        for editor in editors:
            editor.blockSignals(True)
        try:
            if row is None:
                for editor in editors:
                    editor.clear()
                return
            identity = str(row[2] or row[3] or row[1] or "")
            is_empty = identity.startswith("<Empty")
            self.item_identity_edit.setText("" if is_empty else identity)
            self.item_quantity_edit.setText("1" if is_empty else ("" if row[6] in (None, "") else str(row[6])))
            self.item_index_edit.setText("0" if is_empty and row[4] in (None, "", 0) else ("" if row[4] in (None, "") else str(row[4])))
            self.item_flag_edit.setText("1" if is_empty else ("" if row[5] in (None, "") else str(row[5])))
        finally:
            for editor in editors:
                editor.blockSignals(False)

    def _clamp_i32_value(self, value: Any, *, minimum: int = I32_MIN, maximum: int = I32_MAX, label: str = "value") -> Optional[int]:
        """Clamp a typed editor value into the requested signed 32-bit-safe range."""
        try:
            ivalue = int(str(value).replace(",", "").strip(), 0)
        except Exception:
            return None
        minimum = max(I32_MIN, int(minimum))
        maximum = min(I32_MAX, int(maximum))
        if ivalue < minimum:
            try:
                self.statusBar().showMessage(f"{label} clamped to {minimum:,}.", 3500)
            except Exception:
                pass
            return minimum
        if ivalue > maximum:
            try:
                self.statusBar().showMessage(f"{label} clamped to {maximum:,}.", 3500)
            except Exception:
                pass
            return maximum
        return ivalue

    def _set_i32_line_edit_validator(self, editor: Optional[QLineEdit], *, minimum: int = I32_MIN, maximum: int = I32_MAX, tooltip: str = "") -> None:
        if editor is None:
            return
        minimum = max(I32_MIN, int(minimum))
        maximum = min(I32_MAX, int(maximum))
        editor.setValidator(QIntValidator(minimum, maximum, editor))
        if tooltip:
            editor.setToolTip(tooltip)
        else:
            editor.setToolTip(f"Whole number only. Safe range: {minimum:,} to {maximum:,}.")

    def _install_common_numeric_validators(self) -> None:
        """Apply signed 32-bit/safe-range validators to direct-edit boxes that accept numbers."""
        specs = [
            ("item_quantity_edit", 0, 99_999_999, "Item quantity/value: 0 to 99,999,999."),
            ("item_index_edit", I32_MIN, I32_MAX, "Item index/serial: signed 32-bit range."),
            ("item_flag_edit", I32_MIN, I32_MAX, "Item flag/state: signed 32-bit range."),
            ("sigil_level_edit", 0, SIGIL_LEVEL_MAX, "Sigil level: 0 to signed 32-bit max."),
            ("sigil_flags_edit", I32_MIN, I32_MAX, "Sigil flags: signed 32-bit range."),
            ("weapon_xp_edit", 0, WEAPON_XP_MAX, f"Weapon XP/progress: 0 to {WEAPON_XP_MAX:,}."),
            ("weapon_flags_edit", I32_MIN, I32_MAX, "Weapon flags: signed 32-bit range."),
            ("mastery_state_edit", 0, MASTERY_1607_SAFE_MAX, "Mastery state/value: 0 to signed 32-bit max."),
            ("mastery_overmastery_value_edit", -1, MASTERY_1607_SAFE_MAX, "Overmastery value: -1 for FFFFFFFF/80%, otherwise 0 to signed 32-bit max."),
        ]
        for name, minimum, maximum, tip in specs:
            self._set_i32_line_edit_validator(getattr(self, name, None), minimum=minimum, maximum=maximum, tooltip=tip)

    def _parse_editor_int(self, text: Any, label: str, minimum: int = I32_MIN, maximum: int = I32_MAX) -> Optional[int]:
        raw = str(text or "").strip().replace(",", "")
        if not raw:
            QMessageBox.warning(self, "Missing value", f"Enter a {label} value first.")
            return None
        value = self._clamp_i32_value(raw, minimum=minimum, maximum=maximum, label=label)
        if value is None:
            QMessageBox.warning(self, "Invalid value", f"{label} must be a whole number. You can use decimal or 0xHEX.")
            return None
        return value

    def _selected_item_meta(self) -> Optional[Dict[str, Any]]:
        return self._selected_meta(self.item_table, self.item_rows_meta) if hasattr(self, "item_table") else None

    def _selected_sigil_meta(self) -> Optional[Dict[str, Any]]:
        return self._selected_meta(self.sigil_table, self.sigil_rows_meta) if hasattr(self, "sigil_table") else None

    def _selected_weapon_meta(self) -> Optional[Dict[str, Any]]:
        return self._selected_meta(self.weapon_table, self.weapon_rows_meta) if hasattr(self, "weapon_table") else None

    def _selected_character_meta(self) -> Optional[Dict[str, Any]]:
        return self._selected_meta(self.character_table, self.character_rows_meta) if hasattr(self, "character_table") else None

    def _apply_item_text_to_field(self, meta: Dict[str, Any], column: int, value: Any) -> bool:
        """Patch one editable inventory/wallet field from table/direct-editor text."""
        if not self.save:
            return False
        text = str(value or "").strip()
        if meta.get("wallet_value"):
            if column != 6:
                QMessageBox.information(self, "Wallet field", "This row is a direct UserDataManager wallet value. Edit the Quantity column only.")
                return False
            parsed = self._parse_editor_int(text, "wallet quantity", 0, 99_999_999)
            return parsed is not None and self._set_record_first_value(meta.get("qty_rec"), parsed, "wallet quantity")
        if meta.get("is_empty") and column not in (1, 2, 3):
            QMessageBox.information(self, "Set item first", "Empty slots need an item GBID/name/hash before quantity, index, or flag edits are applied.")
            return False
        if column in (1, 2, 3):
            if not text:
                QMessageBox.warning(self, "Missing item", "Enter a GBID, item name, decimal hash, or 0xHASH.")
                return False
            resolved = self._resolve_hash_from_text(text)
            if resolved is None:
                QMessageBox.warning(self, "Hash not found", "Could not resolve that item. Paste a GBID, item name, decimal hash, or 8-digit hex hash.")
                return False
            return self._set_record_first_value(meta.get("hash_rec"), resolved, "item hash")
        if column == 4:
            parsed = self._parse_editor_int(text, "index/serial")
            return parsed is not None and self._set_record_first_value(meta.get("index_rec"), parsed, "item index/serial")
        if column == 5:
            parsed = self._parse_editor_int(text, "flag/state")
            return parsed is not None and self._set_record_first_value(meta.get("flag_rec"), parsed, "item flag/state")
        if column == 6:
            if not self._item_meta_has_real_quantity(meta):
                QMessageBox.information(self, "Not a quantity field", "This row does not expose a real stack quantity. Regular materials/currency use the 1802 Quantity field; wrightstone/item-slot rows use this column as type/state data and are left unchanged.")
                return False
            parsed = self._parse_editor_int(text, "quantity", 0, 99_999_999)
            return parsed is not None and self._set_record_first_value(meta.get("qty_rec"), parsed, "item quantity")
        return False

    def apply_item_table_cell_edit(self, row: int, column: int, value: Any) -> bool:
        if row < 0 or row >= len(self.item_rows_meta):
            return False
        meta = self.item_rows_meta[row]
        if self._apply_item_text_to_field(meta, column, value):
            try:
                if column in (1, 2, 3):
                    h = self._record_first_value(meta.get("hash_rec"), 0)
                    self._patch_visible_hash_row(self.item_model, row, 1, 2, 3, int(h or 0))
                    meta["is_empty"] = h in (0, EMPTY_HASH)
                    meta["is_known"] = bool(self.item_db.lookup_hash(int(h or 0)))
                elif column == 4:
                    self.item_model.rows[row][4] = self._record_first_value(meta.get("index_rec"), "")
                elif column == 5:
                    self.item_model.rows[row][5] = self._record_first_value(meta.get("flag_rec"), "")
                elif column == 6:
                    self.item_model.rows[row][6] = self._record_first_value(meta.get("qty_rec"), "")
                self._emit_model_row_changed(self.item_model, row)
            except Exception:
                pass
            self._after_editor_patch("Item cell updated in memory. Save when ready.")
            return True
        return False

    def apply_item_inline_edits(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        meta = self._selected_item_meta()
        if not meta:
            return
        changes = 0
        current = self._selected_row(self.item_table, self.item_model) if hasattr(self, "item_table") else None
        if meta.get("wallet_value"):
            requested = [(6, self.item_quantity_edit.text() if hasattr(self, "item_quantity_edit") else "")]
        else:
            requested = [
                (1, self.item_identity_edit.text() if hasattr(self, "item_identity_edit") else ""),
                (6, self.item_quantity_edit.text() if hasattr(self, "item_quantity_edit") else ""),
                (4, self.item_index_edit.text() if hasattr(self, "item_index_edit") else ""),
                (5, self.item_flag_edit.text() if hasattr(self, "item_flag_edit") else ""),
            ]
        if meta.get("is_empty") and not str(requested[0][1] or "").strip():
            self.item_identity_edit.setFocus()
            self.statusBar().showMessage("Empty slots need an item GBID/name/hash before quantity or flag changes are applied.", 4000)
            return
        # Only patch fields that differ from what is currently shown. This lets users edit one field
        # without accidentally rewriting the rest of the row.
        current_by_col = {1: "", 4: "", 5: "", 6: ""}
        if current:
            def cell_text(v: Any) -> str:
                return "" if v is None or v == "" else str(v)
            current_by_col = {1: str(current[2] or current[3] or current[1] or ""), 4: cell_text(current[4]), 5: cell_text(current[5]), 6: cell_text(current[6])}
        for column, text in requested:
            text = str(text or "").strip()
            if column == 1 and not text and meta.get("is_empty"):
                continue
            if text == current_by_col.get(column, ""):
                continue
            if self._apply_item_text_to_field(meta, column, text):
                changes += 1
            else:
                return
        if changes:
            self._after_editor_patch(f"Applied {changes} item field change{'s' if changes != 1 else ''} in memory. Save when ready.")
        else:
            self.statusBar().showMessage("No item field changes to apply.", 3000)

    def _text_for_raw_value(self, value: Any) -> str:
        if value in (None, "", _FORMAT_EMPTY):
            return ""
        return str(value)

    def _set_line_edit_text_safely(self, name: str, value: Any) -> None:
        editor = getattr(self, name, None)
        if editor is None:
            return
        editor.blockSignals(True)
        editor.setText(self._text_for_raw_value(value))
        editor.blockSignals(False)

    def _clear_line_edit_safely(self, name: str) -> None:
        editor = getattr(self, name, None)
        if editor is None:
            return
        editor.blockSignals(True)
        editor.clear()
        editor.blockSignals(False)

    def _raw_hash_editor_text(self, name: str, gbid: Any, fallback_hash: Any) -> None:
        text = str(gbid or "").strip()
        if not text:
            text = str(fallback_hash or "").strip()
        self._set_line_edit_text_safely(name, text)

    def apply_sigil_inline_edits(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        row = self._selected_row(self.sigil_table, self.sigil_model) if hasattr(self, "sigil_table") else None
        if not row:
            return
        changes = 0
        owner_text = self.sigil_worn_by_edit.text() if hasattr(self, "sigil_worn_by_edit") else ""
        if not str(owner_text or "").strip() and hasattr(self, "sigil_worn_by_combo"):
            owner_hash = self.sigil_worn_by_combo.currentData()
            owner_text = "" if int(owner_hash or EMPTY_HASH) in (0, EMPTY_HASH) else f"0x{int(owner_hash) & 0xFFFFFFFF:08X}"
        requests = [
            (2, self.sigil_identity_edit.text() if hasattr(self, "sigil_identity_edit") else "", str(row[3] or row[4] or row[2] or "")),
            (5, self.sigil_level_edit.text() if hasattr(self, "sigil_level_edit") else "", str(row[5] or "")),
            (6, owner_text, str(row[7] or row[6] or "")),
            (8, self.sigil_flags_edit.text() if hasattr(self, "sigil_flags_edit") else "", str(row[8] or "")),
        ]
        current_row = self.sigil_table.currentIndex().row()
        for column, text, current in requests:
            text = str(text or "").strip()
            if text == str(current or "").strip():
                continue
            if self.apply_sigil_table_cell_edit(current_row, column, text):
                changes += 1
            else:
                return
        if changes:
            self.statusBar().showMessage(f"Applied {changes} sigil field change{'s' if changes != 1 else ''} in memory. Save when ready.", 5000)
        else:
            self.statusBar().showMessage("No sigil field changes to apply.", 3000)

    def apply_weapon_inline_edits(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        row = self._selected_row(self.weapon_table, self.weapon_model) if hasattr(self, "weapon_table") else None
        if not row:
            return
        changes = 0
        requests = [
            (1, self.weapon_identity_edit.text() if hasattr(self, "weapon_identity_edit") else "", str(row[2] or row[3] or row[1] or "")),
            (4, self.weapon_xp_edit.text() if hasattr(self, "weapon_xp_edit") else "", str(row[4] or "")),
            (10, self.weapon_stone_edit.text() if hasattr(self, "weapon_stone_edit") else "", str(row[10] or "")),
            (9, self.weapon_flags_edit.text() if hasattr(self, "weapon_flags_edit") else "", str(row[9] or "")),
        ]
        current_row = self.weapon_table.currentIndex().row()
        for column, text, current in requests:
            text = str(text or "").strip()
            if text == str(current or "").strip():
                continue
            if self.apply_weapon_table_cell_edit(current_row, column, text):
                changes += 1
            else:
                return
        if changes:
            self.statusBar().showMessage(f"Applied {changes} weapon field change{'s' if changes != 1 else ''} in memory. Save when ready.", 5000)
        else:
            self.statusBar().showMessage("No weapon field changes to apply.", 3000)

    def apply_character_inline_edits(self) -> None:
        """Compatibility hook for older buttons/hotkeys; current character controls sync live."""
        self.sync_character_detail_controls()

    def _set_character_spin_safely(self, name: str, value: Any) -> None:
        spin = getattr(self, name, None)
        if spin is None:
            return
        try:
            ivalue = int(value)
        except Exception:
            ivalue = 0
        old_block = spin.blockSignals(True)
        try:
            ivalue = max(spin.minimum(), min(spin.maximum(), ivalue))
            spin.setValue(ivalue)
        finally:
            spin.blockSignals(old_block)

    def _clamp_character_value(self, value: Any, *, minimum: int = 0) -> int:
        try:
            ivalue = int(str(value).replace(",", "").strip())
        except Exception:
            ivalue = minimum
        return max(int(minimum), min(CHARACTER_VALUE_MAX, ivalue))

    def _set_character_max_bundle(self, meta_or_target: Dict[str, Any]) -> int:
        """Set every known editable numeric character field to the editor max."""
        patched = 0
        for key, label in [
            ("level_rec", "character level"),
            ("xp_rec", "character EXP/progress"),
            ("msp_rec", "character MSP/progression"),
            ("unlock_rec", "character unlock/active"),
            ("state_rec", "character state/flags"),
        ]:
            rec = meta_or_target.get(key)
            if rec is not None and self._set_record_first_value(rec, CHARACTER_VALUE_MAX, label):
                patched += 1
        return patched

    def sync_character_detail_controls(self) -> None:
        """Patch selected character fields as soon as the selected card controls change."""
        if getattr(self, "_updating_character_detail", False):
            return
        if not self.save or not hasattr(self, "character_table"):
            return
        current_row = self.character_table.currentIndex().row()
        if current_row < 0 or current_row >= len(getattr(self, "character_rows_meta", [])):
            return
        meta = self.character_rows_meta[current_row]
        if meta.get("is_empty"):
            return
        changed = 0
        try:
            level = int(self.character_level_spin.value()) if hasattr(self, "character_level_spin") else None
            exp = int(self.character_exp_spin.value()) if hasattr(self, "character_exp_spin") else None
            unlock = int(self.character_unlock_spin.value()) if hasattr(self, "character_unlock_spin") else None
            state = int(self.character_state_spin.value()) if hasattr(self, "character_state_spin") else None
        except Exception:
            return
        if level is not None:
            level = self._clamp_character_value(level, minimum=0)
        if exp is not None:
            exp = self._clamp_character_value(exp, minimum=0)
        if unlock is not None:
            unlock = self._clamp_character_value(unlock, minimum=0)
        if state is not None:
            state = self._clamp_character_value(state, minimum=0)
        if level is not None and level != self._record_first_value(meta.get("level_rec"), level):
            if self._set_record_first_value(meta.get("level_rec"), level, "character level"):
                changed += 1
        if exp is not None and exp != self._record_first_value(meta.get("xp_rec"), exp):
            if self._set_record_first_value(meta.get("xp_rec"), exp, "character EXP/progress"):
                changed += 1
        if unlock is not None and unlock != self._record_first_value(meta.get("unlock_rec"), unlock):
            if self._set_record_first_value(meta.get("unlock_rec"), unlock, "character unlock/active candidate"):
                changed += 1
        if state is not None and state != self._record_first_value(meta.get("state_rec"), state):
            if self._set_record_first_value(meta.get("state_rec"), state, "character state/flags"):
                changed += 1
        if not changed:
            return
        try:
            row = self.character_model.rows[current_row]
            row[4] = self._record_first_value(meta.get("level_rec"), row[4])
            row[5] = self._record_first_value(meta.get("xp_rec"), row[5])
            row[7] = self._record_first_value(meta.get("unlock_rec"), row[7])
            self._emit_model_row_changed(self.character_model, current_row)
        except Exception:
            pass
        self._after_editor_patch(f"Synced {changed} selected character field{'s' if changed != 1 else ''} in memory.")

    def update_sigil_detail(self) -> None:
        if not hasattr(self, "sigil_detail_label"):
            return
        self._updating_sigil_detail = True
        row = self._selected_row(self.sigil_table, self.sigil_model) if hasattr(self, "sigil_table") else None
        if not row:
            text = "Select a sigil row. Use inline fields for sigil, level, equipped character, and lock/flags."
            if hasattr(self.sigil_detail_label, "setPlainText"):
                self.sigil_detail_label.setPlainText(text)
            else:
                self.sigil_detail_label.setText(text)
            for name in ("sigil_identity_edit", "sigil_level_edit", "sigil_worn_by_edit", "sigil_flags_edit"):
                self._clear_line_edit_safely(name)
            self._set_owner_combo_by_hash(EMPTY_HASH)
            self._updating_sigil_detail = False
            return
        self._raw_hash_editor_text("sigil_identity_edit", row[3], row[4])
        self._set_line_edit_text_safely("sigil_level_edit", row[5])
        self._set_line_edit_text_safely("sigil_worn_by_edit", row[7] if str(row[6] or "").startswith("Unknown owner") else "")
        self._set_owner_combo_by_hash(self._current_sigil_owner_hash())
        self._set_line_edit_text_safely("sigil_flags_edit", row[8])
        meta = self._selected_sigil_meta() if hasattr(self, "sigil_table") else None
        pair_note = str((meta or {}).get("level_pair_note") or "")
        text = (
            f"Sigil/Gem : {format_display_value(row[2], 'Sigil')}\n"
            f"Level     : {format_display_value(row[5], 'Level')}  (2704 / FF900A){pair_note}\n"
            f"Equipped  : {format_display_value(row[6] or 'None / Unequipped', 'Worn By')}\n"
            f"Flags     : {format_display_value(row[8], 'Flags')}\n"
            f"Slot      : {format_display_value(row[1], 'Slot')}\n"
            f"GBID      : {format_display_value(row[3], 'GBID')}\n"
            f"Hash      : {format_hash_value(row[4])}  (2703 / FF8F0A)\n"
            f"Unit      : {format_display_value(row[0], 'Unit')}"
        )
        if hasattr(self.sigil_detail_label, "setPlainText"):
            self.sigil_detail_label.setPlainText(text)
        else:
            self.sigil_detail_label.setText(text)
        self._updating_sigil_detail = False

    def update_weapon_detail(self) -> None:
        if not hasattr(self, "weapon_detail_label"):
            return
        row = self._selected_row(self.weapon_table, self.weapon_model) if hasattr(self, "weapon_table") else None
        if not row:
            text = "Select a weapon row. Inline fields can edit weapon, XP, stone, and flags."
            for name in ("weapon_identity_edit", "weapon_xp_edit", "weapon_stone_edit", "weapon_flags_edit"):
                self._clear_line_edit_safely(name)
        else:
            self._raw_hash_editor_text("weapon_identity_edit", row[2], row[3])
            self._set_line_edit_text_safely("weapon_xp_edit", row[4])
            self._set_line_edit_text_safely("weapon_stone_edit", row[10])
            self._set_line_edit_text_safely("weapon_flags_edit", row[9])
            text = "\n".join([
                f"Weapon: {format_display_value(row[1], 'Weapon')}",
                f"GBID:   {format_display_value(row[2], 'GBID')}",
                f"Hash:   {format_hash_value(row[3])}",
                f"Slot:   {format_display_value(row[0], 'Slot')}",
                f"XP:     {format_display_value(row[4], 'XP')}",
                f"Stone:  {format_display_value(row[10] or 'none', 'Stone')}",
                f"Flags:  {format_display_value(row[9], 'Flags')}",
                "",
                "Tip: edit the inline fields, press Enter, or click Apply Changes. Technical columns stay available in More / table editing.",
            ])
        if hasattr(self.weapon_detail_label, "setPlainText"):
            self.weapon_detail_label.setPlainText(text)
        else:
            self.weapon_detail_label.setText(text)

    def update_character_detail(self) -> None:
        if not hasattr(self, "character_detail_label"):
            return
        self._updating_character_detail = True
        try:
            row = self._selected_row(self.character_table, self.character_model) if hasattr(self, "character_table") else None
            meta = self._selected_meta(self.character_table, self.character_rows_meta) if hasattr(self, "character_table") else None
            if not row or not meta:
                if hasattr(self, "character_detail_title"):
                    self.character_detail_title.setText("Select a character row")
                self.character_detail_label.setText("Pick a row above. Level, EXP, unlock, and state controls sync immediately when changed.")
                for name in ("character_level_spin", "character_exp_spin", "character_unlock_spin", "character_state_spin"):
                    self._set_character_spin_safely(name, 0)
                return
            level = self._record_first_value(meta.get("level_rec"), row[4])
            exp = self._record_first_value(meta.get("xp_rec"), row[5])
            unlock = self._record_first_value(meta.get("unlock_rec"), row[7])
            state = self._record_first_value(meta.get("state_rec"), 0)
            if hasattr(self, "character_detail_title"):
                self.character_detail_title.setText(str(format_display_value(row[1], "Character")))
            self.character_detail_label.setText(
                f"Slot: {format_display_value(row[0], 'Slot')}    Unit: {format_display_value(row[8], 'Unit')}\n"
                f"GBID: {format_display_value(row[2], 'GBID')}    Hash: {format_hash_value(row[3])}\n"
                f"Level: {format_display_value(level, 'Level')}    EXP: {format_display_value(exp, 'EXP')}\n"
                f"Unlock/Active: {format_display_value(unlock, 'Unlock')}    State/Flags: {format_display_value(state, 'State')}\n"
                "Changes below sync immediately in memory. Save or Save As when ready."
            )
            self._set_character_spin_safely("character_level_spin", level)
            self._set_character_spin_safely("character_exp_spin", exp)
            self._set_character_spin_safely("character_unlock_spin", unlock)
            self._set_character_spin_safely("character_state_spin", state)
        finally:
            self._updating_character_detail = False

    def refresh_save_health(self) -> None:
        if not hasattr(self, "save_health_text"):
            return
        if not self.save:
            self.save_health_text.setPlainText("Open a save to run the health checklist.")
            return
        s = self.save.summary()
        item_empty = self.count_empty_item_slots()
        sigil_empty = self.count_empty_sigil_slots()
        weapon_empty = self.count_empty_weapon_slots()
        known_rows = scan_known_hashes(self.save, self.item_db, include_unknown=False, limit=100000)
        unknown_rows = scan_known_hashes(self.save, self.item_db, include_unknown=True, limit=100000)
        unknown_hash_like = sum(1 for r in unknown_rows if not r.get("known"))
        lines = [
            "GBFR Save Health Checklist",
            "",
            f"File: {s['path']}",
            f"Container: {s['mode']}",
            f"Active hash valid: {'YES' if s.get('active_hash_ok') else 'NO / unavailable'}",
            f"Dirty in editor: {'YES - save or Save As when ready' if self.dirty else 'no'}",
            "",
            "Reusable empty slots",
            f"- Item/material slots: {item_empty}",
            f"- Sigil/gem slots: {sigil_empty}",
            f"- Weapon slots: {weapon_empty}",
            "",
            "Inventory safety",
        ]
        inv_lines, inv_risky = self._inventory_safety_report_lines()
        lines.extend(f"- {line}" if line and not line.startswith("-") else line for line in inv_lines[:8])
        sigil_owner_issues = self.validate_sigil_owners(show_message=False)
        lines.extend([
            f"- Overall inventory risk: {'CHECK WARNINGS BEFORE SAVING' if inv_risky else 'no known crash pattern detected'}",
            "",
            "Sigil / gem equipment safety",
            f"- Invalid equipped-owner references: {len(sigil_owner_issues)}",
            f"- Overall sigil owner risk: {'CHECK WARNINGS BEFORE SAVING' if sigil_owner_issues else 'no invalid owner references detected'}",
            "",
            "Resolved data",
            f"- Loaded GBID/hash rows: {len(self.item_db)}",
            f"- Loaded resource ID rows: {len(self.resource_db.entries)}",
            f"- Known hashes found in this save: {len(known_rows)}",
            f"- Unknown hash-like fields still worth researching: {unknown_hash_like}",
            "",
            "Recommended workflow",
            "1. Use Save As for the first test after any add/swap operation.",
            "2. Add/batch-add only reuses empty slots; it does not resize FlatBuffers yet.",
            "3. If an item appears wrong in-game, compare before/after saves and send the pair back for field confirmation.",
            "4. Use Unknown Hash Scan when names are missing; importing newer GBID CSVs may resolve them.",
        ])
        if not s.get('active_hash_ok'):
            lines += ["", "Warning", "- The active save hash is not valid before editing. Make a backup and avoid overwriting the original until we inspect it."]
        self.save_health_text.setPlainText("\n".join(lines))


    def set_progression_group_filter(self, prefix: str) -> None:
        prefix = str(prefix or "")
        if hasattr(self, "progression_quest_group_combo"):
            for i in range(self.progression_quest_group_combo.count()):
                if str(self.progression_quest_group_combo.itemData(i) or "") == prefix:
                    if self.progression_quest_group_combo.currentIndex() != i:
                        self.progression_quest_group_combo.blockSignals(True)
                        self.progression_quest_group_combo.setCurrentIndex(i)
                        self.progression_quest_group_combo.blockSignals(False)
                    break
        if hasattr(self, "progression_group_tabs"):
            for i in range(self.progression_group_tabs.count()):
                if str(self.progression_group_tabs.tabData(i) or "") == prefix:
                    if self.progression_group_tabs.currentIndex() != i:
                        self.progression_group_tabs.blockSignals(True)
                        self.progression_group_tabs.setCurrentIndex(i)
                        self.progression_group_tabs.blockSignals(False)
                    break
        self.refresh_progression_editor_rows()

    def _quest_progression_fields(self) -> Dict[str, List[int]]:
        return {
            "status": [2511, 2512, 2551, 2561, 2571, 2581],
            "rank": [2574],
            "complete": [2520, 2554, 2555, 2575, 2576, 2577],
        }

    def _progression_vector_plan_for_prefix(self, prefix: str) -> Dict[str, List[int]]:
        """Best-known vector mapping for the clean progression editor.

        GBFR stores many quest/progression states as packed vectors under unit 0,
        not as one save record per quest ID. The UI shows Community quest IDs and maps
        each visible catalog row to the same ordinal inside the best-known vector
        for that broad group. Unknown/unmapped groups remain visible but read-only.
        """
        pfx = str(prefix or "")[:1]
        if pfx == "1":
            return {"keys": [2510], "status": [2511, 2522], "rank": [], "complete": [2520]}
        if pfx == "2":
            return {"keys": [2550], "status": [2551], "rank": [], "complete": [2554, 2555]}
        if pfx == "3":
            return {"keys": [2560], "status": [2561], "rank": [], "complete": []}
        if pfx == "4":
            return {"keys": [2570], "status": [2571], "rank": [2574], "complete": [2575, 2576, 2577]}
        if pfx in {"5", "7"}:
            return {"keys": [2580], "status": [2581], "rank": [], "complete": []}
        return {"keys": [], "status": [], "rank": [], "complete": []}

    def schedule_progression_editor_refresh(self) -> None:
        """Debounce expensive catalog filtering while the user types."""
        if bool(getattr(self, "_save_in_progress", False)) or bool(getattr(self, "_load_in_progress", False)):
            self._mark_stale_pages(["Progression"])
            return
        timer = getattr(self, "_progression_edit_refresh_timer", None)
        if timer is not None:
            timer.start()
        else:
            self.refresh_progression_editor_rows()

    def _invalidate_progression_caches(self, *, catalog: bool = False, vectors: bool = True) -> None:
        if catalog:
            self._progression_catalog_cache = None
            self._progression_catalog_counts_cache = {}
        if vectors:
            self._progression_vector_record_cache = {}
            self._progression_vector_values_cache = {}
            self._progression_key_index_cache = {}

    def _quest_catalog_entries(self) -> List[Dict[str, Any]]:
        """Return cached normalized quest/stage catalog rows for the user-facing editor."""
        cached = getattr(self, "_progression_catalog_cache", None)
        if cached is not None:
            return cached
        try:
            rows = quest_rows(self.resource_db)
        except Exception:
            rows = []
        entries: List[Dict[str, Any]] = []
        counts: Dict[str, int] = {"": 0}
        for row in rows:
            try:
                qid = str(row[3]).strip().upper()
                if not qid:
                    continue
                pfx = qid[:1]
                save_key = self._quest_save_key_from_id(qid)
                entry = {
                    "quest_id": qid,
                    "group": str(row[1]),
                    "name": str(row[2] or qid),
                    "numeric_value": save_key if save_key is not None else (row[4] if len(row) > 4 else ""),
                    "source": str(row[6]) if len(row) > 6 else "Catalog",
                    "prefix": pfx,
                    "save_key": save_key,
                }
                entries.append(entry)
                counts[""] = counts.get("", 0) + 1
                counts[pfx] = counts.get(pfx, 0) + 1
            except Exception:
                continue
        self._progression_catalog_cache = entries
        self._progression_catalog_counts_cache = counts
        return entries

    def _quest_save_key_from_id(self, quest_id: Any) -> Optional[int]:
        """Return the save-vector key for a Community quest/stage ID.

        Mission/progression vectors do not use the catalog row ordinal. They store
        an explicit key list, where six-character IDs such as ``100000`` are packed
        as hexadecimal integers (0x100000), even when the text looks decimal. The
        previous reader used row order, which made mission status drift badly.
        """
        text = str(quest_id or "").strip().upper().replace("0X", "")
        if not text:
            return None
        try:
            if len(text) == 6 and text[:1] in {"1", "2", "3", "4", "5", "6", "7"}:
                return int(text, 16)
            if any(c in "ABCDEF" for c in text):
                return int(text, 16)
            return int(text, 10)
        except Exception:
            return None

    def _progression_key_index_map(self, prefix: str) -> Dict[int, int]:
        pfx = str(prefix or "")[:1]
        cache = getattr(self, "_progression_key_index_cache", None)
        cache_key = (pfx, id(self.save))
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        plan = self._progression_vector_plan_for_prefix(pfx)
        out: Dict[int, int] = {}
        for fid in plan.get("keys", []):
            for idx, value in enumerate(self._progression_values_for_field(fid)):
                try:
                    key = int(value) & 0xFFFFFFFF
                except Exception:
                    continue
                if key and key not in out:
                    out[key] = idx
        if cache is not None:
            cache[cache_key] = out
        return out

    def _progression_index_for_catalog_entry(self, entry: Dict[str, Any]) -> int:
        key = entry.get("save_key")
        if key is None:
            key = self._quest_save_key_from_id(entry.get("quest_id"))
        try:
            key_int = int(key) & 0xFFFFFFFF
        except Exception:
            return -1
        return self._progression_key_index_map(str(entry.get("prefix", ""))).get(key_int, -1)

    def _progression_record_for_field(self, field_id: int) -> Optional[UnitRecord]:
        if not self.save:
            return None
        fid = int(field_id)
        cache = getattr(self, "_progression_vector_record_cache", None)
        if cache is not None and fid in cache:
            return cache[fid]
        rec: Optional[UnitRecord] = None
        try:
            recs = self.save.find(id_type=fid, unit_id=0)
            if recs:
                rec = recs[0]
            else:
                recs = self.save.find(id_type=fid)
                rec = recs[0] if recs else None
        except Exception:
            rec = None
        if cache is not None:
            cache[fid] = rec
        return rec

    def _progression_values_for_field(self, field_id: int) -> List[Any]:
        fid = int(field_id)
        vals_cache = getattr(self, "_progression_vector_values_cache", None)
        if vals_cache is not None and fid in vals_cache:
            return vals_cache[fid]
        rec = self._progression_record_for_field(fid)
        vals: List[Any] = []
        if rec is not None and self.save:
            try:
                vals = list(self.save.get_values(rec))
            except Exception:
                vals = []
        if vals_cache is not None:
            vals_cache[fid] = vals
        return vals

    def _progression_value_at(self, fields: List[int], index: int, default: Any = _FORMAT_EMPTY) -> Any:
        if not self.save or index is None or index < 0:
            return default
        for fid in fields:
            vals = self._progression_values_for_field(fid)
            if index < len(vals):
                return vals[index]
        return default

    def _set_progression_value_at(self, fields: List[int], index: int, value: Any, *, only_raise: bool = False) -> int:
        if not self.save or index is None or index < 0:
            return 0
        changed = 0
        for fid in fields:
            rec = self._progression_record_for_field(fid)
            if not rec:
                continue
            vals = list(self._progression_values_for_field(fid))
            if index >= len(vals):
                continue
            old = vals[index]
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
                vals[index] = new
                self.save.set_values(rec, vals)
                if hasattr(self, "_progression_vector_values_cache"):
                    self._progression_vector_values_cache[int(fid)] = vals
                changed += 1
        return changed

    def _progression_display_values_for_meta(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        index = int(meta.get("progression_index", -1))
        plan = self._progression_vector_plan_for_prefix(str(meta.get("prefix", "")))
        status = self._progression_value_at(plan.get("status", []), index, _FORMAT_EMPTY)
        rank = self._progression_value_at(plan.get("rank", []), index, _FORMAT_EMPTY)
        complete_raw = self._progression_value_at(plan.get("complete", []), index, _FORMAT_EMPTY)
        if complete_raw == _FORMAT_EMPTY:
            done = bool(_parse_intish(status) or _parse_intish(rank)) if status != _FORMAT_EMPTY or rank != _FORMAT_EMPTY else False
        else:
            done = bool(complete_raw)
        writable_targets = [fid for name, vals in plan.items() if name != "keys" for fid in vals]
        writable = index >= 0 and any(self._progression_record_for_field(fid) is not None for fid in writable_targets)
        return {"status": status, "rank": rank, "done": done, "writable": writable}

    def refresh_progression_editor_rows(self) -> None:
        if not hasattr(self, "progression_edit_model"):
            return
        if not self.save:
            self.progression_edit_model.set_rows([])
            if hasattr(self, "progression_editor_status"):
                self.progression_editor_status.setText("Open a save to edit progression.")
            if hasattr(self, "progression_editor_title"):
                self.progression_editor_title.setText("Progression Editor")
            if hasattr(self, "progression_group_tabs"):
                base_labels = getattr(self, "progression_group_tab_base_labels", {}) or {}
                for i in range(self.progression_group_tabs.count()):
                    pfx = str(self.progression_group_tabs.tabData(i) or "")
                    self.progression_group_tabs.setTabText(i, f"{base_labels.get(pfx, self.progression_group_tabs.tabText(i).split(' (')[0])} (0)")
            return

        prefix = self._progression_selected_quest_prefix()
        search_text = self.progression_editor_search_edit.text().strip().lower() if hasattr(self, "progression_editor_search_edit") else ""
        filter_mode = self.progression_done_filter_combo.currentText() if hasattr(self, "progression_done_filter_combo") else "All mapped rows"
        all_catalog = self._quest_catalog_entries()

        # Build mapped/editable metadata first. Catalog-only rows are hidden from
        # this user-facing page; raw catalog/research content belongs in hidden
        # research tools, not the normal Progression workflow.
        mapped_entries: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
        mapped_counts: Dict[str, int] = {"": 0}
        for e in all_catalog:
            idx = self._progression_index_for_catalog_entry(e)
            row_meta = dict(e)
            row_meta["progression_index"] = idx
            vals = self._progression_display_values_for_meta(row_meta)
            if not vals.get("writable"):
                continue
            pfx = str(e.get("prefix", ""))
            mapped_counts[""] = mapped_counts.get("", 0) + 1
            mapped_counts[pfx] = mapped_counts.get(pfx, 0) + 1
            mapped_entries.append((row_meta, vals))

        selected_entries = [(m, v) for m, v in mapped_entries if not prefix or str(m.get("prefix", "")) == str(prefix)]

        rows: List[List[Any]] = []
        meta: List[Dict[str, Any]] = []
        max_rows = 250 if not search_text else 500
        matched_count = 0
        for row_meta, vals in selected_entries:
            status = vals["status"]
            rank = vals["rank"]
            done_bool = bool(vals["done"])
            if filter_mode == "Completed only" and not done_bool:
                continue
            if filter_mode == "Incomplete only" and done_bool:
                continue
            haystack = f"{row_meta.get('quest_id')} {row_meta.get('group')} {row_meta.get('name')} {status} {rank} {done_bool} mapped".lower()
            if search_text and not all(term in haystack for term in search_text.replace(',', ' ').split() if term):
                continue
            matched_count += 1
            if len(rows) >= max_rows:
                continue
            rows.append([
                row_meta.get("quest_id", ""),
                row_meta.get("name", ""),
                status,
                rank,
                "Yes" if done_bool else "No",
                "Mapped",
            ])
            meta.append(row_meta)

        self.progression_edit_rows_meta = meta
        self.progression_edit_model.set_rows(rows)
        selected_group = "All Progression"
        if hasattr(self, "progression_quest_group_combo"):
            selected_group = self.progression_quest_group_combo.currentText().replace(" · ", " — ")
        if hasattr(self, "progression_editor_title"):
            self.progression_editor_title.setText(selected_group)
        if hasattr(self, "progression_editor_status"):
            shown = len(rows)
            extra = " after filters" if search_text or filter_mode != "All mapped rows" else ""
            cap_note = f" · showing first {shown:,} of {matched_count:,}" if matched_count > shown else f" · showing {shown:,}"
            self.progression_editor_status.setText(
                f"{matched_count:,} mapped/editable progression row(s){extra}{cap_note}. "
                "Catalog-only rows are hidden. Status, Rank, and Done update instantly."
            )
        if hasattr(self, "progression_group_tabs"):
            base_labels = getattr(self, "progression_group_tab_base_labels", {}) or {}
            for i in range(self.progression_group_tabs.count()):
                try:
                    pfx = str(self.progression_group_tabs.tabData(i) or "")
                    base = base_labels.get(pfx, self.progression_group_tabs.tabText(i).split(" (")[0])
                    count = mapped_counts.get(pfx, 0)
                    self.progression_group_tabs.setTabText(i, f"{base} ({count:,})")
                except Exception:
                    pass
        if hasattr(self, "progression_edit_table"):
            self._set_table_widths(self.progression_edit_table, {0: 110, 1: 520, 2: 115, 3: 90, 4: 80, 5: 120})
        self.update_progression_edit_controls()
        if hasattr(self, "progression_raw_group") and self.progression_raw_group.isChecked():
            self.refresh_progression_raw_rows()

    def _progression_group_name_for_unit(self, unit_id: int) -> str:
        text = str(unit_id)
        names = {
            "1": "Main Quest",
            "2": "Challenge / Side Quest",
            "3": "Fate Episode",
            "4": "Multiplayer / Quest Counter",
            "5": "Town / Lobby",
            "6": "Dummy / Practice",
            "7": "Short Story / Misc",
        }
        return names.get(text[:1], "Quest / Progression")

    def update_progression_edit_controls(self) -> None:
        if not hasattr(self, "progression_edit_table") or not hasattr(self, "progression_edit_rows_meta"):
            return
        idx = self.progression_edit_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.progression_edit_rows_meta):
            if hasattr(self, "progression_selected_summary"):
                self.progression_selected_summary.setText("Select a row to edit common progression values.")
            return
        meta = self.progression_edit_rows_meta[idx.row()]
        vals = self._progression_display_values_for_meta(meta)
        status = _parse_intish(vals.get("status"))
        rank = _parse_intish(vals.get("rank"))
        self._progression_controls_loading = True
        try:
            if hasattr(self, "progression_status_spin"):
                self.progression_status_spin.blockSignals(True)
                self.progression_status_spin.setEnabled(vals.get("writable", False))
                self.progression_status_spin.setValue(max(0, min(999, status if status is not None else 1)))
                self.progression_status_spin.blockSignals(False)
            if hasattr(self, "progression_rank_spin"):
                self.progression_rank_spin.blockSignals(True)
                # Rank only exists for the mapped multiplayer/quest-counter vector right now.
                self.progression_rank_spin.setEnabled(bool(self._progression_vector_plan_for_prefix(meta.get("prefix", "")).get("rank")))
                self.progression_rank_spin.setValue(max(0, min(9, rank if rank is not None else 7)))
                self.progression_rank_spin.blockSignals(False)
            if hasattr(self, "progression_completed_check"):
                self.progression_completed_check.blockSignals(True)
                self.progression_completed_check.setEnabled(vals.get("writable", False))
                self.progression_completed_check.setChecked(bool(vals.get("done")))
                self.progression_completed_check.blockSignals(False)
        finally:
            self._progression_controls_loading = False
        if hasattr(self, "progression_selected_summary"):
            source = "Mapped/save-editable" if vals.get("writable") else "Catalog only/not mapped yet"
            self.progression_selected_summary.setText(
                f"{meta.get('name')} · ID {meta.get('quest_id')} · index {meta.get('progression_index')} · {source}"
            )

    def _progression_bool_from_text(self, value: Any) -> bool:
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "done", "complete", "completed", "viewed", "checked"}:
            return True
        if text in {"0", "false", "no", "n", "none", "incomplete", "not done", "unchecked", ""}:
            return False
        return bool(_parse_intish(value))

    def _mark_progression_dirty_light(self, message: str) -> None:
        self.dirty = True
        self.update_status_text_light()
        self._mark_all_pages_stale()
        self._stale_page_labels.discard("Progression")
        self.statusBar().showMessage(message, 3500)

    def _refresh_progression_model_row(self, row: int) -> None:
        if not hasattr(self, "progression_edit_model") or not hasattr(self, "progression_edit_rows_meta"):
            return
        if row < 0 or row >= len(self.progression_edit_rows_meta) or row >= len(self.progression_edit_model.rows):
            return
        meta = self.progression_edit_rows_meta[row]
        vals = self._progression_display_values_for_meta(meta)
        self.progression_edit_model.rows[row] = [
            meta.get("quest_id", ""),
            meta.get("name", ""),
            vals.get("status", _FORMAT_EMPTY),
            vals.get("rank", _FORMAT_EMPTY),
            "Yes" if bool(vals.get("done")) else "No",
            "Mapped" if bool(vals.get("writable")) else "Catalog only",
        ]
        try:
            left = self.progression_edit_model.index(row, 0)
            right = self.progression_edit_model.index(row, self.progression_edit_model.columnCount() - 1)
            self.progression_edit_model.dataChanged.emit(left, right, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole])
        except Exception:
            pass

    def apply_progression_table_cell_edit(self, row: int, column: int, value: Any) -> bool:
        if not self.save or not hasattr(self, "progression_edit_rows_meta"):
            return False
        if row < 0 or row >= len(self.progression_edit_rows_meta):
            return False
        meta = self.progression_edit_rows_meta[row]
        vals = self._progression_display_values_for_meta(meta)
        if not vals.get("writable"):
            self.statusBar().showMessage("That progression row is catalog-only until its save vector is mapped.", 3500)
            return False
        index = int(meta.get("progression_index", -1))
        plan = self._progression_vector_plan_for_prefix(str(meta.get("prefix", "")))
        changed = 0
        if column == 2:
            parsed = self._clamp_i32_value(value, minimum=0, maximum=999, label="progression status")
            if parsed is None:
                return False
            changed = self._set_progression_value_at(plan.get("status", []), index, int(parsed), only_raise=False)
        elif column == 3:
            parsed = self._clamp_i32_value(value, minimum=0, maximum=9, label="progression rank")
            if parsed is None:
                return False
            changed = self._set_progression_value_at(plan.get("rank", []), index, int(parsed), only_raise=False)
        elif column == 4:
            changed = self._set_progression_value_at(plan.get("complete", []), index, bool(self._progression_bool_from_text(value)), only_raise=False)
        else:
            return False
        if changed:
            self._refresh_progression_model_row(row)
            self._mark_progression_dirty_light(f"Updated {meta.get('quest_id')} instantly. Save when ready.")
            # Keep the selected-row controls in sync with direct table edits.
            if hasattr(self, "progression_edit_table") and self.progression_edit_table.currentIndex().row() == row:
                self.update_progression_edit_controls()
        return bool(changed)

    def apply_progression_realtime_from_controls(self) -> None:
        if getattr(self, "_progression_controls_loading", False) or not self.save:
            return
        if not hasattr(self, "progression_edit_table") or not hasattr(self, "progression_edit_rows_meta"):
            return
        idx = self.progression_edit_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.progression_edit_rows_meta):
            return
        row = idx.row()
        meta = self.progression_edit_rows_meta[row]
        if not self._progression_display_values_for_meta(meta).get("writable"):
            return
        changed = self._set_progression_meta_values(
            meta,
            status_value=int(self.progression_status_spin.value()),
            rank_value=int(self.progression_rank_spin.value()),
            completed=bool(self.progression_completed_check.isChecked()),
            only_raise=False,
        )
        if changed:
            self._refresh_progression_model_row(row)
            self._mark_progression_dirty_light(f"Updated {meta.get('quest_id')} instantly. Save when ready.")

    def _set_progression_meta_values(self, meta: Dict[str, Any], *, status_value: int = 1, rank_value: int = 7, completed: bool = True, only_raise: bool = True) -> int:
        if not self.save:
            return 0
        index = int(meta.get("progression_index", -1))
        plan = self._progression_vector_plan_for_prefix(str(meta.get("prefix", "")))
        changed = 0
        status_value = max(0, min(999, int(status_value)))
        rank_value = max(0, min(9, int(rank_value)))
        changed += self._set_progression_value_at(plan.get("status", []), index, status_value, only_raise=only_raise)
        changed += self._set_progression_value_at(plan.get("rank", []), index, rank_value, only_raise=only_raise)
        changed += self._set_progression_value_at(plan.get("complete", []), index, bool(completed), only_raise=only_raise)
        return changed

    def _progression_mapped_metas_for_prefix(self, prefix: str = "") -> List[Dict[str, Any]]:
        """Return save-editable quest/stage catalog metas for a prefix.

        This is the shared backend for both the Progression editor and the
        mapped cheats. It intentionally uses the real key vectors such as
        2550/2551 for side quests instead of assuming catalog row order.
        """
        if not self.save:
            return []
        pfx = str(prefix or "")[:1]
        metas: List[Dict[str, Any]] = []
        for entry in self._quest_catalog_entries():
            if pfx and str(entry.get("prefix", "")) != pfx:
                continue
            meta = dict(entry)
            meta["progression_index"] = self._progression_index_for_catalog_entry(entry)
            vals = self._progression_display_values_for_meta(meta)
            if vals.get("writable"):
                metas.append(meta)
        return metas

    def _progression_group_label_for_prefix(self, prefix: str = "") -> str:
        labels = {
            "": "All mapped progression",
            "1": "Main Story",
            "2": "Side / Challenge Quests",
            "3": "Fate Episodes",
            "4": "Multiplayer / Quest Counter",
            "5": "Towns / Lobbies",
            "6": "Dummy / Practice",
            "7": "Short Story / Misc",
        }
        return labels.get(str(prefix or "")[:1], f"{prefix}xxxxx progression")

    def cheat_complete_progression_group(self, prefix: str = "", label: str = "") -> None:
        """Complete mapped progression rows by quest ID group.

        This replaces the old broad QuestSystem cheat. It uses the same mapping
        as the Progression tab, so a row is patched only when its catalog quest
        key is found inside the save's packed mission key vector. For example,
        side quests use 2550 as the key vector and 2551/2554/2555 as status/
        completion vectors.
        """
        if not self.save:
            QMessageBox.information(self, "No save", "Open a save first.")
            return
        self._invalidate_progression_caches(catalog=False, vectors=True)
        pfx = str(prefix or "")[:1]
        group_label = label or self._progression_group_label_for_prefix(pfx)
        metas = self._progression_mapped_metas_for_prefix(pfx)
        if not metas:
            QMessageBox.information(
                self,
                "No mapped progression rows",
                f"No editable mapped rows were found for {self._progression_group_label_for_prefix(pfx)} in this save. "
                "Catalog-only rows are skipped to avoid writing the wrong mission index.",
            )
            return
        preview = "\n".join(f"- {m.get('quest_id')} · {m.get('name')}" for m in metas[:18])
        if len(metas) > 18:
            preview += f"\n...and {len(metas) - 18} more"
        msg = (
            f"Complete {len(metas):,} mapped row(s) for {self._progression_group_label_for_prefix(pfx)}?\n\n"
            f"{preview}\n\n"
            "This uses the same mission-key mapping as the Progression tab and skips catalog-only rows. "
            "Use Save As and test the edited copy in-game."
        )
        if QMessageBox.question(self, "Confirm progression cheat", msg) != QMessageBox.StandardButton.Yes:
            return
        changed_values = 0
        changed_rows = 0
        for meta in metas:
            changed = self._set_progression_meta_values(meta, status_value=1, rank_value=7, completed=True, only_raise=True)
            if changed:
                changed_values += changed
                changed_rows += 1
        if changed_values:
            self._after_editor_patch(
                f"{group_label}: completed {changed_rows:,} mapped row(s), changed {changed_values:,} value(s).",
                refresh=False,
            )
            if hasattr(self, "progression_edit_model"):
                self.refresh_progression_editor_rows()
        else:
            QMessageBox.information(self, "Already complete", f"{self._progression_group_label_for_prefix(pfx)} already looked complete for all mapped rows.")

    def _set_progression_unit_values(self, unit_id: int, *, status_value: int = 1, rank_value: int = 7, completed: bool = True, only_raise: bool = True) -> int:
        """Legacy unit-id editor kept for older quick actions.

        Current quest UI uses packed-vector metadata and calls _set_progression_meta_values.
        """
        if not self.save:
            return 0
        changed = 0
        fields = self._quest_progression_fields()
        targets = []
        for fid in fields["status"]:
            targets.extend((rec, status_value) for rec in self.save.find(id_type=fid, unit_id=unit_id))
        for fid in fields["rank"]:
            targets.extend((rec, rank_value) for rec in self.save.find(id_type=fid, unit_id=unit_id))
        for fid in fields["complete"]:
            targets.extend((rec, completed) for rec in self.save.find(id_type=fid, unit_id=unit_id))
        for rec, value in targets:
            vals = self.save.get_values(rec)
            new_vals = []
            rec_changed = False
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
                    rec_changed = True
                new_vals.append(new)
            if rec_changed:
                self.save.set_values(rec, new_vals)
                changed += 1
        return changed

    def apply_progression_selected_edit(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save", "Open a save first.")
            return
        if not hasattr(self, "progression_edit_table") or not hasattr(self, "progression_edit_rows_meta"):
            return
        idx = self.progression_edit_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.progression_edit_rows_meta):
            QMessageBox.information(self, "No row selected", "Select a progression row first.")
            return
        meta = self.progression_edit_rows_meta[idx.row()]
        if not self._progression_display_values_for_meta(meta).get("writable"):
            QMessageBox.information(self, "Catalog only", "This catalog row is visible for reference, but its save vector is not mapped yet.")
            return
        changed = self._set_progression_meta_values(
            meta,
            status_value=int(self.progression_status_spin.value()),
            rank_value=int(self.progression_rank_spin.value()),
            completed=bool(self.progression_completed_check.isChecked()),
            only_raise=False,
        )
        if changed:
            self._refresh_progression_model_row(idx.row())
            self._mark_progression_dirty_light(f"Progression row {meta.get('quest_id')} updated across {changed} value(s). Save when ready.")
        self.update_progression_edit_controls()

    def complete_progression_selected_row(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save", "Open a save first.")
            return
        if not hasattr(self, "progression_edit_table") or not hasattr(self, "progression_edit_rows_meta"):
            return
        idx = self.progression_edit_table.currentIndex()
        if not idx.isValid() or idx.row() >= len(self.progression_edit_rows_meta):
            QMessageBox.information(self, "No row selected", "Select a progression row first.")
            return
        meta = self.progression_edit_rows_meta[idx.row()]
        if not self._progression_display_values_for_meta(meta).get("writable"):
            QMessageBox.information(self, "Catalog only", "This catalog row is not mapped to a known save vector yet.")
            return
        changed = self._set_progression_meta_values(meta, status_value=1, rank_value=7, completed=True, only_raise=True)
        if changed:
            self._refresh_progression_model_row(idx.row())
            self._mark_progression_dirty_light(f"Completed {meta.get('quest_id')} across {changed} value(s). Save when ready.")
            self.update_progression_edit_controls()
        else:
            self.statusBar().showMessage("Selected progression row was already complete or had no matching values.", 4000)

    def complete_progression_visible_group(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save", "Open a save first.")
            return
        label = self.progression_quest_group_combo.currentText() if hasattr(self, "progression_quest_group_combo") else "current group"
        visible_meta = [m for m in (getattr(self, "progression_edit_rows_meta", []) or []) if self._progression_display_values_for_meta(m).get("writable")]
        if not visible_meta:
            QMessageBox.information(self, "No mapped rows", "No visible rows in this group are mapped to known save vectors yet.")
            return
        if QMessageBox.question(
            self,
            "Complete current progression group",
            f"Patch {len(visible_meta):,} currently visible mapped row(s) in {label}?\n\nUse Save As and test in-game after this experimental edit.",
        ) != QMessageBox.StandardButton.Yes:
            return
        changed = 0
        for meta in visible_meta:
            changed += self._set_progression_meta_values(meta, status_value=1, rank_value=7, completed=True, only_raise=True)
        if changed:
            self._after_editor_patch(f"Completed {label}: changed {changed:,} value(s) in memory. Save when ready.")
        else:
            QMessageBox.information(self, "No changes", "No progression values needed to change.")
        self.refresh_progression_editor_rows()

    def _progression_sections(self) -> Dict[str, Dict[str, Any]]:
        return {
            "Quest completion / ranks": {
                "fields": [2511, 2512, 2551, 2561, 2571, 2581, 2574, 2554, 2555, 2575, 2576, 2577],
                "confidence": "Experimental",
                "notes": [
                    "These are QuestSystem status/result/rank candidate arrays.",
                    "The bulk patch raises known values and rank candidates, but this is not a full per-quest checklist yet.",
                    "Use the row table below to inspect the exact rows and values found in this save.",
                    "Next step is pairing these arrays with the Quest ID catalog so each quest can show Completed / Rank / Clears.",
                ],
            },
            "Titles / archive unlocks": {
                "fields": [7302, 7352, 7902, 8102, 8202, 8302, 8402, 8502, 8602, 8702, 8802],
                "confidence": "Experimental",
                "notes": [
                    "These are archive/book/title/list candidate state arrays.",
                    "The unlock action raises candidate state values without lowering existing counters.",
                    "Exact title challenge names still need before/after samples or a final table map.",
                ],
            },
            "Character levels": {
                "fields": [1308],
                "confidence": "Stable for level rows",
                "notes": [
                    "Character field 1308 is used by the game for character level values. Field 1402 appears to be a separate character state/index value, not the displayed level.",
                    "Use Characters or Cheats to set exact levels or max all known character rows.",
                    "The row table below shows the character unit/slot, current level value, and raw record key.",
                ],
            },
            "Overmastery / RNG slots": {
                "fields": [1404],
                "confidence": "Research",
                "notes": [
                    "Observed character field 1404 stores four RNG/overmastery hash slots.",
                    "Copy/paste and raw-set are useful now; readable HP/ATK/DEF/Cap dropdowns need more mapping.",
                    "Enable Expand values to inspect each of the four raw overmastery hash slots separately.",
                ],
            },
        }

    def _progression_selected_section(self) -> str:
        if hasattr(self, "progression_detail_combo"):
            return self.progression_detail_combo.currentText()
        return "Overview"

    def _progression_fields_for_section(self, section: str) -> List[int]:
        sections = self._progression_sections()
        if section == "Overview":
            fields: List[int] = []
            for data in sections.values():
                fields.extend(data.get("fields", []))
            return sorted(set(fields))
        if section == "Known Quest ID catalog":
            return []
        return list(sections.get(section, sections["Quest completion / ranks"]).get("fields", []))

    def _format_progression_values(self, rec: UnitRecord, values: Optional[List[Any]] = None, limit: int = 18) -> str:
        if not self.save:
            return ""
        if values is None:
            values = self.save.get_values(rec)
        shown: List[str] = []
        for idx, val in enumerate(values[:limit]):
            if rec.id_type in {1404} and isinstance(val, int) and val not in (0, EMPTY_HASH):
                shown.append(f"[{idx}] {format_hash_value(val)}")
            else:
                shown.append(f"[{idx}] {format_display_value(val, 'Value')}")
        if len(values) > limit:
            shown.append("...")
        return "; ".join(shown)

    def _progression_field_note(self, fid: int) -> str:
        if fid in {2511, 2512, 2551, 2561, 2571, 2581, 2574, 2554, 2555, 2575, 2576, 2577}:
            return "QuestSystem candidate row"
        if fid in {7302, 7352, 7902, 8102, 8202, 8302, 8402, 8502, 8602, 8702, 8802}:
            return "Title/archive candidate row"
        if fid == 1308:
            return "Character level row"
        if fid == 1402:
            return "Character 1402 state/index row"
        if fid == 1404:
            return "Overmastery/RNG hash slots"
        return "Mapped progression row"

    def _progression_limit_from_ui(self) -> Optional[int]:
        text = "500 rows"
        if hasattr(self, "progression_max_rows_combo"):
            text = self.progression_max_rows_combo.currentText()
        if text.startswith("All"):
            return None
        try:
            return int(text.split()[0])
        except Exception:
            return 500

    def _progression_selected_field_filter(self) -> Optional[int]:
        if not hasattr(self, "progression_field_filter_combo"):
            return None
        text = self.progression_field_filter_combo.currentText().strip()
        if not text or text.startswith("All"):
            return None
        try:
            return int(text.split()[0])
        except Exception:
            return None

    def _progression_selected_quest_prefix(self) -> str:
        if not hasattr(self, "progression_quest_group_combo"):
            return ""
        data = self.progression_quest_group_combo.currentData()
        return str(data or "")

    def _refresh_progression_field_filter_choices(self, section: str) -> None:
        if not hasattr(self, "progression_field_filter_combo"):
            return
        combo = self.progression_field_filter_combo
        current = combo.currentText()
        fields = self._progression_fields_for_section(section)
        labels = ["All fields"]
        for fid in fields:
            try:
                count = len(self.save.find(id_type=fid)) if self.save else 0
            except Exception:
                count = 0
            labels.append(f"{fid} · {unit_name(fid)} ({count})")
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(labels)
        if current in labels:
            combo.setCurrentText(current)
        else:
            combo.setCurrentText("All fields")
        combo.blockSignals(False)

    def _progression_row_matches_filters(
        self,
        row: List[Any],
        search_text: str,
        unit_filter: str,
        value_mode: str,
        quest_prefix: str = "",
    ) -> bool:
        if search_text:
            haystack = " ".join(str(x).lower() for x in row)
            terms = [t for t in search_text.lower().replace(",", " ").split() if t]
            if not all(t in haystack for t in terms):
                return False
        if quest_prefix:
            unit_id_text = str(row[3]).strip()
            if not unit_id_text.startswith(str(quest_prefix)):
                return False
        if unit_filter:
            unit_hay = f"{row[3]} {row[4]}".lower()
            terms = [t for t in unit_filter.lower().replace(",", " ").split() if t]
            if not all(t in unit_hay for t in terms):
                return False
        try:
            nonzero = int(row[7])
        except Exception:
            nonzero = 0
        unit_name_text = str(row[4]).strip()
        if value_mode == "Has non-zero" and nonzero <= 0:
            return False
        if value_mode == "All zero/empty" and nonzero > 0:
            return False
        if value_mode == "Known/named units" and not unit_name_text:
            return False
        if value_mode == "Unknown/unnamed units" and unit_name_text:
            return False
        return True

    def _build_progression_raw_rows(
        self,
        section: str,
        expand_values: bool = False,
        nonzero_only: bool = False,
        field_filter: Optional[int] = None,
        search_text: str = "",
        unit_filter: str = "",
        value_mode: str = "Any values",
        max_rows: Optional[int] = None,
        quest_prefix: str = "",
    ) -> List[List[Any]]:
        if not self.save:
            return []
        fields = self._progression_fields_for_section(section)
        if field_filter is not None:
            fields = [fid for fid in fields if fid == field_filter]
        if not fields:
            return []
        rows: List[List[Any]] = []
        for fid in fields:
            for rec in self.save.find(id_type=fid):
                try:
                    values = self.save.get_values(rec)
                except Exception:
                    values = []
                nonzero = sum(1 for v in values if bool(v))
                if nonzero_only and not nonzero:
                    continue
                unit_label = self.unit_model.unit_labels.label_for(rec) if hasattr(self, "unit_model") else ""
                field_name = unit_name(rec.id_type)
                note = self._progression_field_note(fid)
                if expand_values:
                    for value_index, value in enumerate(values):
                        if nonzero_only and not bool(value):
                            continue
                        value_text = format_hash_value(value) if fid == 1404 and isinstance(value, int) else format_display_value(value, "Value")
                        row = [
                            section,
                            fid,
                            field_name,
                            rec.unit_id,
                            unit_label,
                            f"{rec.kind}:{rec.index}[{value_index}]",
                            1,
                            1 if bool(value) else 0,
                            value_text,
                            note,
                        ]
                        if self._progression_row_matches_filters(row, search_text, unit_filter, value_mode, quest_prefix):
                            rows.append(row)
                else:
                    row = [
                        section,
                        fid,
                        field_name,
                        rec.unit_id,
                        unit_label,
                        f"{rec.kind}:{rec.index}",
                        rec.value_count,
                        nonzero,
                        self._format_progression_values(rec, values),
                        note,
                    ]
                    if self._progression_row_matches_filters(row, search_text, unit_filter, value_mode, quest_prefix):
                        rows.append(row)
                if max_rows is not None and len(rows) >= max_rows:
                    return rows[:max_rows]
        return rows

    def refresh_progression_raw_rows(self) -> None:
        if not hasattr(self, "progression_rows_model"):
            return
        if hasattr(self, "progression_raw_group") and not self.progression_raw_group.isChecked():
            self.progression_rows_model.set_rows([])
            if hasattr(self, "progression_filter_status"):
                self.progression_filter_status.setText("Raw rows hidden. Open Advanced raw field research to inspect them.")
            return
        section = self._progression_selected_section()
        self._refresh_progression_field_filter_choices(section)
        expand = bool(getattr(self, "progression_expand_values_check", None) and self.progression_expand_values_check.isChecked())
        nonzero = bool(getattr(self, "progression_nonzero_only_check", None) and self.progression_nonzero_only_check.isChecked())
        field_filter = self._progression_selected_field_filter()
        search_text = self.progression_row_filter_edit.text().strip() if hasattr(self, "progression_row_filter_edit") else ""
        unit_filter = self.progression_unit_filter_edit.text().strip() if hasattr(self, "progression_unit_filter_edit") else ""
        value_mode = self.progression_value_mode_combo.currentText() if hasattr(self, "progression_value_mode_combo") else "Any values"
        quest_prefix = self._progression_selected_quest_prefix()
        max_rows = self._progression_limit_from_ui()
        rows = self._build_progression_raw_rows(
            section,
            expand_values=expand,
            nonzero_only=nonzero,
            field_filter=field_filter,
            search_text=search_text,
            unit_filter=unit_filter,
            value_mode=value_mode,
            max_rows=max_rows,
            quest_prefix=quest_prefix,
        )
        self.progression_rows_model.set_rows(rows)
        if hasattr(self, "progression_filter_status"):
            field_label = f"field {field_filter}" if field_filter is not None else "all fields"
            group_label = "all groups"
            if quest_prefix:
                group_label = f"{quest_prefix}xxxxx"
            limit_label = "all rows" if max_rows is None else f"first {max_rows:,} matches"
            self.progression_filter_status.setText(
                f"Showing {len(rows):,} rows · section: {section} · {group_label} · {field_label} · {value_mode} · {limit_label}"
            )
        if hasattr(self, "progression_rows_table"):
            self._set_table_widths(self.progression_rows_table, {0: 220, 1: 82, 2: 190, 3: 100, 4: 210, 5: 90, 6: 70, 7: 82, 8: 420})

    def clear_progression_filters(self) -> None:
        if hasattr(self, "progression_row_filter_edit"):
            self.progression_row_filter_edit.clear()
        if hasattr(self, "progression_unit_filter_edit"):
            self.progression_unit_filter_edit.clear()
        if hasattr(self, "progression_field_filter_combo"):
            self.progression_field_filter_combo.setCurrentText("All fields")
        if hasattr(self, "progression_value_mode_combo"):
            self.progression_value_mode_combo.setCurrentText("Any values")
        if hasattr(self, "progression_nonzero_only_check"):
            self.progression_nonzero_only_check.setChecked(True)
        if hasattr(self, "progression_expand_values_check"):
            self.progression_expand_values_check.setChecked(False)
        if hasattr(self, "progression_max_rows_combo"):
            self.progression_max_rows_combo.setCurrentText("250 rows")
        if hasattr(self, "progression_quest_group_combo"):
            self.progression_quest_group_combo.setCurrentIndex(0)
        self.refresh_progression_raw_rows()

    def copy_progression_rows(self) -> None:
        if not hasattr(self, "progression_rows_model"):
            return
        headers = self.progression_rows_model.headers
        rows = self.progression_rows_model.rows
        lines = ["	".join(headers)]
        for row in rows:
            lines.append("	".join(str(x) for x in row))
        QApplication.clipboard().setText("\n".join(lines))
        QMessageBox.information(self, "Copied", f"Copied {len(rows)} progression rows to the clipboard.")

    def export_progression_rows_csv(self) -> None:
        if not hasattr(self, "progression_rows_model"):
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export progression rows CSV", "progression_rows.csv", "CSV files (*.csv);;All files (*.*)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(self.progression_rows_model.headers)
                writer.writerows(self.progression_rows_model.rows)
            QMessageBox.information(self, "Exported", f"Exported {len(self.progression_rows_model.rows)} rows to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def _progression_field_summary(self, field_ids: List[int]) -> Dict[str, Any]:
        summary = {"records": 0, "values": 0, "nonzero": 0, "samples": []}
        if not self.save:
            return summary
        for fid in field_ids:
            for rec in self.save.find(id_type=fid):
                all_vals = self.save.get_values(rec)
                summary["records"] += 1
                summary["values"] += len(all_vals)
                summary["nonzero"] += sum(1 for v in all_vals if bool(v))
                if len(summary["samples"]) < 8:
                    shown = ", ".join(format_display_value(v) for v in all_vals[:10])
                    if len(all_vals) > 10:
                        shown += ", ..."
                    summary["samples"].append(f"field {fid} / unit {rec.unit_id} / {rec.value_count} values: [{shown}]")
        return summary

    def _progression_detail_for(self, section: str) -> str:
        if not self.save:
            return "Open a save to inspect progression and unlock fields."
        sections = self._progression_sections()
        if section == "Known Quest ID catalog":
            rows = quest_rows(self.resource_db, "")[:12]
            lines = [
                "Quest ID Catalog",
                "Confidence: reference catalog only",
                "",
                "Known Quest IDs are loaded from the bundled Community quest_id-style catalog.",
                "Open the Quest ID Catalog research tab to search/export the full list.",
                "The raw progression row table is blank for this catalog view because these are reference IDs, not save rows.",
                "",
                "Sample rows:",
            ]
            for row in rows:
                try:
                    lines.append(f"- {row[3]} / {row[1]} / {row[2]}")
                except Exception:
                    lines.append(f"- {row}")
            return "\n".join(lines)
        if section == "Overview":
            parts = []
            total_raw_rows = 0
            for title in ["Quest completion / ranks", "Titles / archive unlocks", "Character levels", "Overmastery / RNG slots"]:
                data = sections[title]
                st = self._progression_field_summary(data["fields"])
                total_raw_rows += st["records"]
                parts.append(f"{title}: {st['records']:,} records / {st['values']:,} values / {st['nonzero']:,} non-zero ({data['confidence']})")
            return "\n".join([
                "Progression overview",
                "",
                *parts,
                "",
                f"Rows available in the table below: {total_raw_rows:,} grouped records. Enable Expand values to inspect individual value indexes.",
                "",
                "Use the dropdown for samples and risk notes. This page is intentionally transparent: stable areas are labeled stable, and quest/title systems remain marked experimental until verified per quest/title.",
            ])
        data = sections.get(section, sections["Quest completion / ranks"])
        st = self._progression_field_summary(data["fields"])
        raw_rows = self._build_progression_raw_rows(section, expand_values=False, nonzero_only=False)
        lines = [
            section,
            f"Confidence: {data['confidence']}",
            f"Fields: {', '.join(str(x) for x in data['fields'])}",
            f"Records found: {st['records']:,}",
            f"Total values: {st['values']:,}",
            f"Non-zero values: {st['nonzero']:,}",
            f"Rows displayed below: {len(raw_rows):,} grouped save records",
            "",
            "Notes:",
        ]
        lines.extend(f"- {note}" for note in data["notes"])
        lines.append("")
        lines.append("Observed samples:")
        if st["samples"]:
            lines.extend(f"- {sample}" for sample in st["samples"])
        else:
            lines.append("- No matching fields found in this loaded save.")
        return "\n".join(lines)

    def refresh_progression_detail(self) -> None:
        if not hasattr(self, "progression_detail_text"):
            return
        section = self.progression_detail_combo.currentText() if hasattr(self, "progression_detail_combo") else "Overview"
        self.progression_detail_text.setPlainText(self._progression_detail_for(section))
        self.refresh_progression_raw_rows()

    def refresh_progression_rows(self) -> None:
        if not hasattr(self, "progression_model"):
            return
        if hasattr(self, "_invalidate_progression_caches"):
            self._invalidate_progression_caches(catalog=False, vectors=True)
        if not self.save:
            self.progression_model.set_rows([])
            if hasattr(self, "progression_rows_model"):
                self.progression_rows_model.set_rows([])
            if hasattr(self, "progression_detail_text"):
                self.progression_detail_text.setPlainText("Open a save to inspect progression and unlock fields.")
            return
        rows = []
        mappings = [
            ("Quest completion / ranks", [2511, 2512, 2551, 2561, 2571, 2581, 2574, 2554, 2555, 2575, 2576, 2577], "Experimental", "Inspect first; use Save As before testing quest patch"),
            ("Titles / archive unlocks", [7302, 7352, 7902, 8102, 8202, 8302, 8402, 8502, 8602, 8702, 8802], "Experimental", "Unlock candidates only; exact titles still need mapping"),
            ("Character levels", [1308], "Stable", "Use Characters page for normal level editing"),
            ("Overmastery / RNG slots", [1404], "Research", "Raw hash slots; copy/paste only until traits are mapped"),
        ]
        for section, fields, confidence, action in mappings:
            st = self._progression_field_summary(fields)
            rows.append([section, confidence, st["records"], st["values"], st["nonzero"], action])
        self.progression_model.set_rows(rows)
        if hasattr(self, "progression_table"):
            self._set_table_widths(self.progression_table, {0: 240, 1: 110, 2: 90, 3: 90, 4: 90, 5: 420})
        self.refresh_progression_editor_rows()
        self.refresh_progression_detail()

    def cheat_complete_quest_tables(self) -> None:
        """Compatibility wrapper for the old all-quest cheat button.

        Older builds called a broad raw-array patch that raised every known
        QuestSystem status/rank field by field ID. That could touch values that
        were not currently visible/mapped by the quest catalog. The cheat now
        reuses the same key-vector mapping as the Progression editor so group
        cheats such as All Side Quests Complete patch the correct vector index.
        """
        self.cheat_complete_progression_group("", "Complete All Mapped Progression")

    def cheat_unlock_title_archive_candidates(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save", "Open a save first.")
            return
        if QMessageBox.question(
            self,
            "Experimental title/archive unlock",
            "This will raise title/archive/book/list candidate state fields to at least 1. Exact title challenge mapping is still being verified. Continue?",
        ) != QMessageBox.StandardButton.Yes:
            return
        results = unlock_title_archive_candidates(self.save)
        self.dirty = True
        self._after_editor_patch(patch_summary(results))

    def update_edit_hub_summary(self) -> None:
        if not hasattr(self, "edit_hub_summary"):
            return
        if not self.save:
            self.edit_hub_summary.setText("Open a save to see editable slot counts.")
            if hasattr(self, "basic_workflow_label"):
                self.basic_workflow_label.setText("Workflow: Open Save → make one small edit → Save As edited copy → test in game.")
            if hasattr(self, "basic_safety_label"):
                self.basic_safety_label.setText("No save loaded. Nothing has been modified.")
            return
        try:
            s = self.save.summary()
        except Exception:
            s = {"path": "loaded save", "active_hash_ok": False, "mode": "unknown"}
        item_empty = self.count_empty_item_slots()
        sigil_empty = self.count_empty_sigil_slots()
        weapon_empty = self.count_empty_weapon_slots()
        known_items = len(self._known_item_quantity_targets())
        known_sigils = len(self._known_sigil_level_targets())
        known_weapons = len(self._known_weapon_xp_targets())
        known_characters = len(self._character_level_targets())
        unknown_sigils = sum(
            1 for row in getattr(self.sigil_model, "rows", [])
            if len(row) > 2 and str(row[2]).startswith("Unknown")
        )
        self.edit_hub_summary.setText(
            "Editable known rows: "
            f"{known_items:,} item/material quantities · {known_sigils:,} sigil levels · "
            f"{known_weapons:,} weapon XP rows · {known_characters:,} character levels.\n"
            "Reusable empty slots: "
            f"{item_empty:,} item/material · {sigil_empty:,} sigil · {weapon_empty:,} weapon."
        )
        if hasattr(self, "basic_workflow_label"):
            if unknown_sigils:
                next_step = f"{unknown_sigils:,} visible sigil rows are still unknown. Use Show Unknown Sigils, copy/export them, then map the names."
            elif known_items or known_sigils or known_weapons or known_characters:
                next_step = "Ready for edits: use exact-value buttons for safe bulk changes, or open a row editor for one specific slot."
            else:
                next_step = "This save exposes few known rows. Use row tabs with Show Empty or ID Cleanup to see what can be mapped next."
            self.basic_workflow_label.setText(
                f"Loaded: {Path(str(s.get('path', 'save'))).name} · mode: {s.get('mode', 'unknown')} · "
                f"hash ok: {s.get('active_hash_ok')} · state: {'modified' if self.dirty else 'clean'}.\n{next_step}"
            )
        if hasattr(self, "basic_safety_label"):
            safety = "Use Save As for the first edited copy. "
            if not s.get("active_hash_ok"):
                safety += "Warning: the active hash check is not currently OK; run Save Health before testing."
            elif self.dirty:
                safety += "You have unsaved changes in memory. Save As before closing or testing."
            else:
                safety += "Hash check is OK and no unsaved changes are pending."
            self.basic_safety_label.setText(safety)


    def _selected_meta(self, table: QTableView, meta_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        idx = table.currentIndex()
        if not idx.isValid() or idx.row() >= len(meta_rows):
            try:
                self.statusBar().showMessage("No row selected for this action.", 2500)
            except Exception:
                pass
            return None
        return meta_rows[idx.row()]

    def _record_first_value(self, rec: Optional[UnitRecord], default: int = 0) -> int:
        if not rec or not self.save or rec.value_count < 1:
            return default
        try:
            if hasattr(self.save, "get_first_value"):
                return int(self.save.get_first_value(rec, default))
            return int(self.save.get_values(rec, 1)[0])
        except Exception:
            return default

    def _set_record_first_value(self, rec: Optional[UnitRecord], value: Any, label: str) -> bool:
        if not self.save:
            return False
        if rec is None:
            QMessageBox.information(self, "Missing field", f"This save row does not have a patchable {label} record.")
            return False
        if rec.value_count < 1:
            QMessageBox.information(self, "Empty field", f"The selected {label} record has no values to edit.")
            return False
        try:
            old_value = self.save.get_first_value(rec, None) if hasattr(self.save, "get_first_value") else self.save.get_values(rec, 1)[0]
        except Exception:
            QMessageBox.warning(self, "Read failed", f"Could not read the current {label} value.")
            return False
        try:
            new_value = int(value)
        except Exception:
            new_value = value
        if old_value == new_value:
            return False
        try:
            if hasattr(self.save, "set_first_value"):
                self.save.set_first_value(rec, new_value)
            else:
                values = self.save.get_values(rec)
                if not values:
                    return False
                values[0] = new_value
                self.save.set_values(rec, values)
        except Exception as exc:
            QMessageBox.warning(self, "Patch failed", f"Could not patch {label}: {exc}")
            return False
        self.dirty = True
        return True

    def _after_editor_patch(self, message: str = "Value updated in memory. Save when ready.", refresh: bool = False) -> None:
        """Mark the save dirty without rebuilding every heavy table after each small edit.

        Earlier builds called refresh_all_views() after every cell/inline edit. On large saves
        that rebuilt inventory, sigil, weapon, character, add-browser, and research tables for
        one changed scalar value, which made editing feel frozen. Fast edit mode keeps the
        edited model row live and only refreshes when explicitly requested or after bulk tools.
        """
        self.dirty = True
        self._invalidate_add_browser_indexes()
        self.update_status_text_light()
        if refresh or not getattr(self, "_fast_edit_mode", True):
            self.refresh_all_views(keep_filter=True)
        else:
            self._mark_all_pages_stale()
            label = self._current_page_label()
            self._stale_page_labels.discard(label)
            for detail in (
                "update_item_detail", "update_sigil_detail",
                "update_weapon_detail", "update_character_detail",
            ):
                if hasattr(self, detail):
                    try:
                        getattr(self, detail)()
                    except Exception:
                        pass
        self.statusBar().showMessage(message + "  Use Refresh Current Page if you need a full rebuild.", 5000)

    def update_edit_hub_summary_light(self) -> None:
        """Cheap Basic Editor summary used immediately after opening a save."""
        if not self.save:
            return
        try:
            known_items = len(getattr(self, "item_rows_meta", []) or [])
            known_sigils = len(getattr(self, "sigil_rows_meta", []) or [])
            known_weapons = len(getattr(self, "weapon_rows_meta", []) or [])
            known_chars = len(getattr(self, "character_rows_meta", []) or [])
            text = (
                f"Loaded: {Path(getattr(self.save.container, 'path', '')).name} · mode: {getattr(self.save.container, 'mode', 'save')} · "
                f"hash check deferred · state: {'modified' if self.dirty else 'clean'}.\n"
                "Ready for edits. Exact counts refresh when you open each tab; Save Health performs the full hash check."
            )
            if hasattr(self, "edit_hub_summary"):
                self.edit_hub_summary.setText(text)
            if hasattr(self, "basic_safety_label"):
                self.basic_safety_label.setText("Use Save As for the first edited copy. Hash check is deferred until Save Health or full refresh.")
        except Exception:
            pass

    def update_status_text_light(self) -> None:
        """Cheap dirty/status update used while typing/editing cells and after saves.

        Do not call self.save.summary() here. summary() verifies the active
        GBFR hash, which scans a large slice of the save. Calling it during the
        save-click path made Windows show the editor as Not Responding even after
        the bytes were already written. Full hash validation still happens from
        update_status_text(), Save Health, or explicit refreshes.
        """
        if not self.save:
            return
        try:
            dirty = "modified" if self.dirty else "clean"
            container = getattr(self.save, "container", None)
            path = getattr(container, "path", "")
            mode = getattr(container, "mode", "save")
            hash_ok = getattr(self, "_last_hash_ok", "not rechecked")
            self.status_label.setText(f"{Path(path).name}\n{mode}\n{dirty}\nhash ok: {hash_ok}")
        except Exception:
            pass

    def refresh_current_page(self) -> None:
        if not self.save:
            return
        self._refresh_page_by_label(self._current_page_label(), force=True)
        self.statusBar().showMessage("Current page refreshed.", 3000)

    def _connect_debounced_text_changed(self, edit: QLineEdit, key: str, callback, delay_ms: int = 220) -> None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(callback)

        # Older builds accidentally let this attribute become a list.  Save
        # clicks then crashed while trying to call .values() on that list.
        # Keep the storage normalized any time a new debounce timer is added.
        timers_obj = getattr(self, "_filter_timers", None)
        if not isinstance(timers_obj, dict):
            self._filter_timers = {}
        self._filter_timers[key] = timer

        edit.textChanged.connect(lambda *_: None if (bool(getattr(self, "_save_in_progress", False)) or bool(getattr(self, "_load_in_progress", False))) else timer.start(delay_ms))

    def _resolve_hash_from_text(self, text: str) -> Optional[int]:
        q = (text or "").strip()
        if q.lower().startswith("unknown 0x"):
            q = q.split()[-1]
        if not q:
            return None
        entry = self.item_db.by_id.get(q.upper())
        if entry:
            return entry.hash_value & 0xFFFFFFFF
        clean = q.removeprefix("0x").removeprefix("0X")
        try:
            if all(c in "0123456789abcdefABCDEF" for c in clean) and 1 <= len(clean) <= 8 and not clean.isdecimal():
                return int(clean, 16) & 0xFFFFFFFF
            if q.lower().startswith("0x"):
                return int(q, 16) & 0xFFFFFFFF
            if q.isdecimal():
                return int(q, 10) & 0xFFFFFFFF
        except Exception:
            pass
        matches = self.item_db.search(q, limit=10)
        # Prefer exact name/category-id match, then the first search hit.
        for entry in matches:
            if q.lower() in {entry.name.lower(), entry.display_name.lower(), entry.item_id.lower()}:
                return entry.hash_value & 0xFFFFFFFF
        if matches:
            return matches[0].hash_value & 0xFFFFFFFF
        # Last resort: many GBFR IDs are custom-XXHash32 strings.
        # Allow direct generated hashes for ID-looking tokens even before the
        # database has a named row for them.
        if q.upper() == q and any(ch == "_" for ch in q) and all(ch.isalnum() or ch == "_" for ch in q):
            return gbfr_hash(q) & 0xFFFFFFFF
        return None

    def _prompt_hash(self, title: str, current: int = 0) -> Optional[int]:
        current_text = f"0x{int(current) & 0xFFFFFFFF:08X}" if current else ""
        text, ok = QInputDialog.getText(
            self,
            title,
            "Enter GBID, name, decimal hash, or 8-digit hex hash:\n"
            "Examples: WEP_PL0000_06, Damage Cap V, 0xEE732781",
            text=current_text,
        )
        if not ok:
            return None
        resolved = self._resolve_hash_from_text(text)
        if resolved is None:
            QMessageBox.warning(self, "Hash not found", "Could not resolve that GBID/name/hash. Import more GBID data or paste an 8-digit hash.")
            return None
        return resolved

    def _find_empty_material_bank_slot(self) -> Optional[Dict[str, Any]]:
        # Disabled intentionally. GBFR's 180x material bank appears to include
        # locked/unobtained catalog rows, not generic append slots. Reusing them
        # caused inventory crashes in test saves. We can still update existing
        # active stacks safely, but inserting new material stacks needs a fuller
        # 1803/1804 state-map first.
        return None

    def _item_slot_counter_record(self) -> Optional[UnitRecord]:
        """Return the global 2101/FF3508 item-slot last-count row, if present."""
        if not self.save:
            return None
        rows = [rec for rec in self.save.find(id_type=2101) if getattr(rec, "value_count", 0) >= 1]
        if not rows:
            return None
        # In examples this is often unit 4; prefer low/unit-zero style rows,
        # then fall back to first file-order record.
        rows = sorted(rows, key=lambda rec: (0 if int(getattr(rec, "unit_id", 0)) in (0, 4) else 1, int(getattr(rec, "value_data_offset", 0))))
        return rows[0]

    def _current_item_slot_counter(self) -> int:
        return self._record_first_value(self._item_slot_counter_record(), 0)

    def _next_item_slot_serial(self) -> int:
        """Return the next 2103/FF3708 slot id for wrightstone/item-slot rows."""
        if not self.save:
            return 1
        max_serial = 0
        grouped = self.save.group_by_unit([2103])
        for fields in grouped.values():
            rec = fields.get(2103)
            if not rec:
                continue
            try:
                value = int(self._record_first_value(rec, 0)) & 0xFFFFFFFF
            except Exception:
                continue
            if value not in (0, EMPTY_HASH):
                max_serial = max(max_serial, value)
        return max(max_serial, self._current_item_slot_counter()) + 1

    def _set_item_slot_serial(self, slot_meta: Dict[str, Any], serial: Optional[int] = None) -> int:
        """Patch 2103 and advance 2101 for slot-style items such as wrightstones."""
        serial = int(serial if serial is not None else self._next_item_slot_serial()) & 0xFFFFFFFF
        if serial <= 0:
            serial = 1
        self._set_record_first_value(slot_meta.get("index_rec"), serial, "item/wrightstone slot id 2103 / FF3708")
        counter = self._item_slot_counter_record()
        if counter is not None and serial > self._record_first_value(counter, 0):
            self._set_record_first_value(counter, serial, "item/wrightstone last count 2101 / FF3508")
        return serial

    def _is_wrightstone_hash(self, item_hash: int) -> bool:
        try:
            entry = self.item_db.lookup_hash(int(item_hash) & 0xFFFFFFFF)
        except Exception:
            entry = None
        if not entry:
            return False
        hay = " ".join(str(getattr(entry, name, "") or "") for name in ("item_id", "name", "display_name", "category", "aliases")).lower()
        return "wrightstone" in hay or "whetstone" in hay or "wst" in hay

    def _find_empty_item_slot(self) -> Optional[Dict[str, Any]]:
        if not self.save:
            return None
        grouped = self.save.group_by_unit([2102, 2103, 2104, 2105, 1901, 1902, 1903, 1904, 2002, 2003, 2004])
        # Item-slot rows are mostly wrightstones/slot-style entries. Materials/currency live in 1801/1802.
        # 210x wrightstone examples use 2102=ID, 2103=slot id/count and 2101=last count.
        # Do not require/write 2105 for those rows; it is not the game-read quantity for stones.
        families = [
            ("210x Wrightstone/item slot", "210x", 2102, 2103, 2104, 2105),
            ("190x ItemManager bucket slot", "190x", 1901, 1902, 1904, 1903),
            ("200x ItemManager bucket slot", "200x", 2002, 2003, None, 2004),
        ]
        for label, slot_family, hash_id, index_id, flag_id, qty_id in families:
            for unit_id, fields in sorted(grouped.items()):
                hash_rec = fields.get(hash_id)
                index_rec = fields.get(index_id) if index_id else None
                qty_rec = fields.get(qty_id) if qty_id else None
                if not hash_rec:
                    continue
                cur_hash = self._record_first_value(hash_rec, 0)
                if slot_family == "210x":
                    if not index_rec:
                        continue
                    cur_index = self._record_first_value(index_rec, 0)
                    if (cur_hash in (0, EMPTY_HASH)) and cur_index in (0, EMPTY_HASH):
                        return {
                            "unit_id": unit_id,
                            "label": label,
                            "slot_family": slot_family,
                            "hash_rec": hash_rec,
                            "index_rec": index_rec,
                            "flag_rec": fields.get(flag_id) if flag_id else None,
                            "qty_rec": qty_rec,
                            "quantity_is_real": False,
                        }
                    continue
                if not qty_rec:
                    continue
                cur_qty = self._record_first_value(qty_rec, 0)
                if (cur_hash in (0, EMPTY_HASH)) and cur_qty == 0:
                    return {
                        "unit_id": unit_id,
                        "label": label,
                        "slot_family": slot_family,
                        "hash_rec": hash_rec,
                        "index_rec": index_rec,
                        "flag_rec": fields.get(flag_id) if flag_id else None,
                        "qty_rec": qty_rec,
                        "quantity_is_real": False,
                    }
        return None

    def count_empty_item_slots(self) -> int:
        if not self.save:
            return 0
        count = 0
        grouped = self.save.group_by_unit([1801, 1802, 2102, 2103, 2105, 1901, 1903, 2002, 2004])
        for fields in grouped.values():
            # Do not advertise empty 180x material-bank rows as reusable yet.
            # Some inactive/locked material catalog rows crash when force-filled.
            if 1801 in fields and 1802 in fields:
                continue
            h = fields.get(2102); serial = fields.get(2103)
            if h and serial and self._record_first_value(h, 0) in (0, EMPTY_HASH) and self._record_first_value(serial, 0) in (0, EMPTY_HASH):
                count += 1
                continue
            for hash_id, qty_id in [(1901, 1903), (2002, 2004)]:
                h = fields.get(hash_id)
                q = fields.get(qty_id)
                if h and q and self._record_first_value(h, 0) in (0, EMPTY_HASH) and self._record_first_value(q, 0) == 0:
                    count += 1
                    break
        return count

    def _add_item_hash_to_empty_slot(self, item_hash: int, name: str = "", gbid: str = "") -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        slot = self._find_empty_material_bank_slot() if self._is_material_bank_entry(item_hash) else self._find_empty_item_slot()
        if not slot:
            QMessageBox.information(self, "No empty slot found", "I could not find an empty reusable slot for that item type. Materials/currency need an empty 180x bank slot; wrightstone/item-style rows need an empty ItemManager slot. This build does not insert new FlatBuffer records yet.")
            return
        entry = self.item_db.lookup_hash(item_hash)
        display = f"{entry.display_name} ({entry.item_id})" if entry else (f"{name} ({gbid})" if name or gbid else f"0x{item_hash:08X}")
        qty, ok = QInputDialog.getInt(self, "Add Item Quantity", f"Quantity for {display}:", 1, 1, 99_999_999)
        if not ok:
            return
        msg = (
            f"Patch empty {slot['label']} unit {slot['unit_id']} with:\n\n"
            f"Item: {display}\nQuantity: {qty}\n\n"
            "This reuses an existing empty slot and updates the active save hash when you save. Continue?"
        )
        if QMessageBox.question(self, "Confirm Add Item", msg) != QMessageBox.StandardButton.Yes:
            return
        ok_hash = self._set_record_first_value(slot.get("hash_rec"), item_hash, "item hash")
        ok_qty = self._set_record_first_value(slot.get("qty_rec"), qty, "item quantity")
        flag_rec = slot.get("flag_rec")
        if flag_rec is not None and self._record_first_value(flag_rec, 0) == 0:
            self._set_record_first_value(flag_rec, 1, "item flag")
        if ok_hash and ok_qty:
            self._after_editor_patch(f"Added {display} x{qty} to unit {slot['unit_id']} in memory.")
            QMessageBox.information(self, "Item added in memory", "Item was written to an empty existing slot. Use Save As first for testing, then verify in-game.")

    def add_item_to_empty_slot(self) -> None:
        item_hash = self._prompt_hash("Add Item to Empty Slot", 0)
        if item_hash is None:
            return
        self._add_item_hash_to_empty_slot(item_hash)

    def add_item_from_inline_editor(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        if not hasattr(self, "item_identity_edit"):
            self.add_item_to_empty_slot()
            return
        text = self.item_identity_edit.text().strip()
        if not text:
            self.item_identity_edit.setFocus()
            self.statusBar().showMessage("Enter a GBID, name, decimal hash, or 0xHASH, then click Add From Fields.", 4000)
            return
        item_hash = self._resolve_hash_from_text(text)
        if item_hash is None:
            QMessageBox.warning(self, "Hash not found", "Could not resolve that item. Paste a GBID, item name, decimal hash, or 8-digit hex hash.")
            return
        qty_text = self.item_quantity_edit.text().strip() if hasattr(self, "item_quantity_edit") else ""
        flag_text = self.item_flag_edit.text().strip() if hasattr(self, "item_flag_edit") else ""
        qty = self._parse_editor_int(qty_text or "1", "quantity", 1, 99_999_999)
        if qty is None:
            return
        flag = None
        if flag_text:
            flag = self._parse_editor_int(flag_text, "flag/state")
            if flag is None:
                return
        result = self._add_item_hash_qty_to_empty_slot(item_hash, qty, flag if flag is not None else 1)
        if result:
            self._after_editor_patch(f"Added {result}.")
        else:
            QMessageBox.information(self, "No empty slot", "No empty ItemManager slot was found. Enable Show empty addable slots to inspect reusable slots.")



    def _add_item_hash_qty_to_empty_slot(self, item_hash: int, qty: int, flag: Optional[int] = 1) -> Optional[str]:
        if not self.save:
            return None
        item_hash = int(item_hash) & 0xFFFFFFFF

        # Stackable materials/currency/consumables must be written through the
        # documented ItemManager 180x bank: 1801=item hash, 1802=quantity.
        # Older builds could fall through to 210x rows, which changed metadata
        # but not the quantity the game reads.
        if self._is_material_bank_entry(item_hash):
            return self._upsert_material_bank_quantity(item_hash, int(qty), flag)

        # Non-material inventory rows are still reused in-place. 2105/1903/2004
        # are treated as type/state/count candidates, not real material count.
        slot = self._find_empty_item_slot()
        if not slot:
            return None
        entry = self.item_db.lookup_hash(item_hash)
        display = f"{entry.display_name} ({entry.item_id})" if entry else f"0x{item_hash:08X}"
        changed = 0
        if self._set_record_first_value(slot.get("hash_rec"), item_hash, "ItemManager item/wrightstone hash"):
            changed += 1
        if slot.get("slot_family") == "210x":
            before_serial = self._record_first_value(slot.get("index_rec"), 0)
            serial = self._set_item_slot_serial(slot)
            if before_serial != serial:
                changed += 1
            flag_rec = slot.get("flag_rec")
            if flag_rec is not None:
                # 2104 is usually bool/active.  Set it to active when present,
                # but do not write 2105 as a fake quantity for wrightstones.
                if self._set_record_first_value(flag_rec, True if getattr(flag_rec, "kind", "") == "bool" else 1, "ItemManager item/wrightstone active flag 2104"):
                    changed += 1
            return f"Added {display} -> {slot['label']} unit {slot['unit_id']} / serial {serial} ({changed} fields)" if changed else f"No change for {display}; slot already matched"
        if self._set_record_first_value(slot.get("qty_rec"), int(qty), "ItemManager type/state/count candidate"):
            changed += 1
        flag_rec = slot.get("flag_rec")
        if flag_rec is not None:
            new_flag = flag if flag is not None else 1
            if self._record_first_value(flag_rec, 0) == 0 or flag is not None:
                if self._set_record_first_value(flag_rec, int(new_flag), "ItemManager flag/state"):
                    changed += 1
        if changed:
            return f"Added {display} value {int(qty):,} -> {slot['label']} unit {slot['unit_id']} ({changed} fields)"
        return f"No change for {display}; slot already matched"


    def _parse_batch_item_lines(self, text: str) -> tuple[List[tuple[str, int]], List[str]]:
        rows: List[tuple[str, int]] = []
        errors: List[str] = []
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            line = line.replace("	", ",")
            # Accept: GBID, qty | GBID x99 | GBID = 99. Names with spaces work if the quantity is after comma/x/=.
            m = None
            for pat in [r"^(.+?)\s*[,=]\s*(-?\d+)\s*$", r"^(.+?)\s+[xX]\s*(-?\d+)\s*$", r"^(.+?)\s+x(-?\d+)\s*$"]:
                m = __import__('re').match(pat, line)
                if m:
                    break
            if m:
                key = m.group(1).strip()
                qty = int(m.group(2))
            else:
                key, qty = line, 1
            if qty < 0:
                errors.append(f"Line {line_no}: quantity cannot be negative")
                continue
            rows.append((key, qty))
        return rows, errors

    def batch_add_items_to_empty_slots(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        template = "Rupie, 999999\nStandard Refinium, 99\nFortitude Crystal (L), 99"
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Batch Add Items",
            "Enter one item per line as: GBID/name/hash, quantity\nThis reuses existing empty ItemManager slots only.",
            template,
        )
        if not ok:
            return
        rows, errors = self._parse_batch_item_lines(text)
        if not rows and errors:
            QMessageBox.warning(self, "Nothing to add", "\n".join(errors[:12]))
            return
        resolved: List[tuple[int, int, str]] = []
        for key, qty in rows:
            h = self._resolve_hash_from_text(key)
            if h is None:
                errors.append(f"Could not resolve: {key}")
                continue
            resolved.append((h, qty, key))
        empty_count = self.count_empty_item_slots()
        if not resolved:
            QMessageBox.warning(self, "No valid items", "No valid items were resolved.\n" + "\n".join(errors[:12]))
            return
        if len(resolved) > empty_count:
            QMessageBox.warning(self, "Not enough empty slots", f"Resolved {len(resolved)} items, but only {empty_count} empty item slots are available. Add fewer items or show empty slots to inspect them.")
            return
        preview_lines = []
        for h, qty, key in resolved[:25]:
            entry = self.item_db.lookup_hash(h)
            name = f"{entry.display_name} ({entry.item_id})" if entry else f"{key} -> 0x{h:08X}"
            preview_lines.append(f"- {name} x{qty}")
        msg = "Add these items into empty existing slots?\n\n" + "\n".join(preview_lines)
        if len(resolved) > 25:
            msg += f"\n...and {len(resolved)-25} more"
        if errors:
            msg += "\n\nSkipped lines:\n" + "\n".join(errors[:8])
        if QMessageBox.question(self, "Confirm Batch Add", msg) != QMessageBox.StandardButton.Yes:
            return
        added: List[str] = []
        for h, qty, _key in resolved:
            result = self._add_item_hash_qty_to_empty_slot(h, qty, flag=1)
            if result:
                added.append(result)
            else:
                break
        self._after_editor_patch(f"Batch added {len(added)} item rows in memory.")
        QMessageBox.information(self, "Batch add complete", f"Added {len(added)} item rows in memory. Use Save As first and verify in-game.")


    def _records_for_id_type_sorted(self, id_type: int) -> List[UnitRecord]:
        """Return records for one SaveData field in stable save-file order."""
        if not self.save:
            return []
        rows = [rec for rec in self.save.find(id_type=int(id_type)) if getattr(rec, "value_count", 0) >= 1]
        return sorted(rows, key=lambda rec: (int(getattr(rec, "value_data_offset", 0)), int(getattr(rec, "table_offset", 0)), int(getattr(rec, "index", 0))))

    def _sigil_level_pair_map_bottom_up(self) -> Dict[str, UnitRecord]:
        """Map each 2703 sigil ID row to its matching 2704 level row.

        Community Save Wizard notes identify sigil level as FF900A0000
        (field 2704).  The sigil ID and level are stored in different field
        sections, so editing by only the visual row/unit can miss the live
        level.  The notes say to match them bottom-up: the last sigil ID row
        corresponds to the last level row, the next-to-last to the next-to-last,
        and so on.  We keep unit-based rows for display, but level writes use
        this pair when possible.
        """
        if not self.save:
            return {}
        key = (id(self.save), len(getattr(self.save, "records", []) or []))
        if getattr(self, "_sigil_level_pair_cache_key", None) == key:
            return getattr(self, "_sigil_level_pair_cache", {}) or {}
        id_rows = self._records_for_id_type_sorted(2703)
        level_rows = self._records_for_id_type_sorted(2704)
        if not id_rows or not level_rows:
            self._sigil_level_pair_cache_key = key
            self._sigil_level_pair_cache = {}
            return {}
        out: Dict[str, UnitRecord] = {}
        limit = min(len(id_rows), len(level_rows))
        for i in range(limit):
            out[id_rows[-1 - i].key] = level_rows[-1 - i]
        self._sigil_level_pair_cache_key = key
        self._sigil_level_pair_cache = out
        return out

    def _sigil_level_record_for_hash_record(self, hash_rec: Optional[UnitRecord], fields: Optional[Dict[int, UnitRecord]] = None) -> Optional[UnitRecord]:
        """Resolve the live 2704/FF900A level row for an existing 2703 sigil row.

        Existing sigil level edits follow the community note: the 2703 ID
        section and 2704 level section are matched bottom-up when the unit IDs
        do not line up.
        """
        if hash_rec is None:
            return fields.get(2704) if fields else None
        paired = self._sigil_level_pair_map_bottom_up().get(hash_rec.key)
        if paired is not None:
            return paired
        return fields.get(2704) if fields else None

    def _sigil_level_record_for_new_slot(self, fields: Optional[Dict[int, UnitRecord]]) -> Optional[UnitRecord]:
        """Return the same-unit 2704/FF900A row used when activating an empty sigil slot.

        The bottom-up pairing is correct for matching already-existing ID rows
        to their level rows, but it is dangerous when adding into an empty row:
        it can select another slot's level record.  Community add examples show
        2702/2703/2704 sharing the same unit for the newly created sigil, while
        2701 is the global last-count record.
        """
        return fields.get(2704) if fields else None

    def _sigil_level_pair_note(self, hash_rec: Optional[UnitRecord], level_rec: Optional[UnitRecord]) -> str:
        if hash_rec is None or level_rec is None:
            return ""
        try:
            if int(hash_rec.unit_id) != int(level_rec.unit_id):
                return f" · level paired bottom-up from unit {int(level_rec.unit_id)}"
        except Exception:
            return ""
        return ""

    def _sigil_counter_record(self) -> Optional[UnitRecord]:
        """Return the global 2701/FF8D0A sigil last-count row, if present.

        Some saves store this under unit 4 instead of unit 0, matching the
        community add example.  Older builds only looked at unit 0, so newly
        added sigils could receive 2702/2703/2704 but leave the real last-count
        unchanged and then be ignored by the game.
        """
        if not self.save:
            return None
        rows = [rec for rec in self.save.find(id_type=2701) if getattr(rec, "value_count", 0) >= 1]
        if not rows:
            return None
        rows = sorted(rows, key=lambda rec: (0 if int(getattr(rec, "unit_id", 0)) in (0, 4) else 1, int(getattr(rec, "value_data_offset", 0))))
        return rows[0]

    def _current_sigil_counter(self) -> int:
        return self._record_first_value(self._sigil_counter_record(), 0)

    def _next_sigil_serial(self) -> int:
        """Return the next non-zero 2702 Sigil/Gem serial.

        Earlier builds wrote only 2703/2704 into empty 270x rows and left
        2702 as zero. Those rows show in our parser but the game can ignore
        them because 2702 behaves like the per-sigil inventory serial/key.
        Use the highest existing serial/counter + 1 to make newly-added sigils
        look like game-created inventory rows.
        """
        if not self.save:
            return 1
        max_serial = 0
        grouped = self.save.group_by_unit([2702])
        for fields in grouped.values():
            rec = fields.get(2702)
            if not rec:
                continue
            try:
                value = int(self._record_first_value(rec, 0)) & 0xFFFFFFFF
            except Exception:
                continue
            if value not in (0, EMPTY_HASH):
                max_serial = max(max_serial, value)
        return max(max_serial, self._current_sigil_counter()) + 1

    def _set_sigil_serial(self, slot_meta: Dict[str, Any], serial: Optional[int] = None) -> int:
        serial = int(serial if serial is not None else self._next_sigil_serial()) & 0xFFFFFFFF
        if serial <= 0:
            serial = 1
        self._set_record_first_value(slot_meta.get("slot_rec"), serial, "sigil serial/key 2702")
        counter = self._sigil_counter_record()
        if counter is not None and serial > self._record_first_value(counter, 0):
            self._set_record_first_value(counter, serial, "sigil serial counter 2701")
        return serial

    def _activate_sigil_slot(
        self,
        slot_meta: Dict[str, Any],
        sigil_hash: int,
        level: int = SIGIL_LEVEL_MAX,
        locked: bool = True,
        owner_hash: Optional[int] = None,
    ) -> bool:
        serial = self._set_sigil_serial(slot_meta)
        ok_hash = self._set_record_first_value(slot_meta.get("hash_rec"), int(sigil_hash) & 0xFFFFFFFF, "sigil hash 2703")
        ok_level = self._set_record_first_value(slot_meta.get("level_rec"), max(1, int(level)), "sigil level 2704 / FF900A")
        if slot_meta.get("worn_rec") is not None:
            owner = EMPTY_HASH if owner_hash in (None, 0) else int(owner_hash) & 0xFFFFFFFF
            self._set_record_first_value(slot_meta.get("worn_rec"), owner, "sigil owner 2706")
        if slot_meta.get("flags_rec") is not None:
            cur = self._record_first_value(slot_meta.get("flags_rec"), 0)
            # Preserve unknown high bits, but ensure a newly-added sigil has an
            # active/visible flag. Existing game-created locked rows commonly
            # use 3, unlocked/normal rows use 2, and older editor rows used 1.
            new_flags = (cur | 3) if locked else ((cur | 2) & ~1)
            self._set_record_first_value(slot_meta.get("flags_rec"), new_flags, "sigil flags 2707")
        return bool(ok_hash or ok_level or serial)

    def repair_added_sigil_slots(self, silent: bool = False) -> int:
        """Assign valid 2702 serials to non-empty sigil rows left at zero.

        Older builds could add sigils that were visible in the editor but not
        in-game because 2702 stayed zero and the 2701 counter was not advanced.
        This repair is conservative: it only touches rows that already have a
        real 2703 sigil hash and a missing/empty 2702 serial.
        """
        if not self.save:
            if not silent:
                QMessageBox.information(self, "No save loaded", "Open a save first.")
            return 0
        grouped = self.save.group_by_unit([2701, 2702, 2703, 2704, 2707])
        changed = 0
        for unit_id, fields in sorted(grouped.items()):
            if unit_id == 0:
                continue
            hash_rec = fields.get(2703)
            slot_rec = fields.get(2702)
            if not hash_rec or not slot_rec:
                continue
            sigil_hash = self._record_first_value(hash_rec, 0) & 0xFFFFFFFF
            serial = self._record_first_value(slot_rec, 0) & 0xFFFFFFFF
            if sigil_hash in (0, EMPTY_HASH) or serial not in (0, EMPTY_HASH):
                continue
            meta = {"unit_id": unit_id, "slot_rec": slot_rec}
            self._set_sigil_serial(meta)
            # Rows added by the older editor often had flag=1. Make them look
            # like normal visible inventory rows while preserving locked bit.
            flags_rec = fields.get(2707)
            if flags_rec is not None:
                cur = self._record_first_value(flags_rec, 0)
                if cur in (0, 1):
                    self._set_record_first_value(flags_rec, cur | 2, "sigil flags 2707")
            changed += 1
        if changed:
            if silent:
                # Pre-save repairs must not rebuild heavy UI pages while the save
                # operation is in progress. Older builds refreshed every tab here,
                # which could make Save/Save As look like a crash on large files,
                # especially with the Mastery page open. Mark the affected
                # views stale and let the normal page refresh happen later.
                self.dirty = True
                self._invalidate_add_browser_indexes()
                self._mark_stale_pages(["Sigils", "Save Health", "Welcome"])
            else:
                self._after_editor_patch(f"Repaired {changed} added sigil slot serial(s).", refresh=True)
        if not silent:
            if changed:
                QMessageBox.information(self, "Sigil repair complete", f"Assigned valid 2702 serials to {changed} existing added sigil row(s). Save As and test in-game.")
            else:
                QMessageBox.information(self, "No repair needed", "No non-empty sigil rows with missing 2702 serials were found.")
        return changed

    def _find_empty_sigil_slot(self) -> Optional[Dict[str, Any]]:
        if not self.save:
            return None
        grouped = self.save.group_by_unit([2702, 2703, 2704, 2706, 2707])
        for unit_id, fields in sorted(grouped.items()):
            hash_rec = fields.get(2703)
            level_rec = self._sigil_level_record_for_new_slot(fields)
            if not hash_rec or not level_rec:
                continue
            cur_hash = self._record_first_value(hash_rec, 0)
            cur_level = self._record_first_value(level_rec, 0)
            if cur_hash in (0, EMPTY_HASH) and cur_level == 0:
                return {
                    "unit_id": unit_id,
                    "slot_rec": fields.get(2702),
                    "hash_rec": hash_rec,
                    "level_rec": level_rec,
                    "worn_rec": fields.get(2706),
                    "flags_rec": fields.get(2707),
                }
        return None

    def count_empty_sigil_slots(self) -> int:
        if not self.save:
            return 0
        grouped = self.save.group_by_unit([2703, 2704])
        count = 0
        for fields in grouped.values():
            h = fields.get(2703)
            lvl = self._sigil_level_record_for_new_slot(fields)
            if h and lvl and self._record_first_value(h, 0) in (0, EMPTY_HASH) and self._record_first_value(lvl, 0) == 0:
                count += 1
        return count

    def _add_sigil_hash_to_empty_slot(self, sigil_hash: int, name: str = "", gbid: str = "") -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        slot = self._find_empty_sigil_slot()
        if not slot:
            QMessageBox.information(self, "No empty slot found", "I could not find an empty Gem/Sigil slot to reuse. This build does not insert new FlatBuffer records yet.")
            return
        entry = self.item_db.lookup_hash(sigil_hash)
        display = f"{entry.display_name} ({entry.item_id})" if entry else (f"{name} ({gbid})" if name or gbid else f"0x{sigil_hash:08X}")
        level, ok = QInputDialog.getInt(self, "Add Sigil Level", f"Level for {display}:", SIGIL_LEVEL_MAX, 1, SIGIL_LEVEL_TEST_MAX)
        if not ok:
            return
        level = self._clamp_sigil_level_value(level, minimum=1)
        lock = QMessageBox.question(self, "Lock Sigil?", "Mark the new sigil as locked?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        if self._activate_sigil_slot(slot, sigil_hash, level, lock, owner_hash=EMPTY_HASH):
            serial = self._record_first_value(slot.get("slot_rec"), 0)
            self._after_editor_patch(f"Added {display} level {level} to sigil unit {slot['unit_id']} / serial {serial} in memory.")

    def add_sigil_to_empty_slot(self) -> None:
        sigil_hash = self._prompt_hash("Add Sigil to Empty Slot", 0)
        if sigil_hash is None:
            return
        self._add_sigil_hash_to_empty_slot(sigil_hash)

    def _add_sigil_hash_level_to_empty_slot(self, sigil_hash: int, level: int = SIGIL_LEVEL_MAX, locked: bool = True, owner_hash: Optional[int] = None) -> Optional[str]:
        if not self.save:
            return None
        level = self._clamp_sigil_level_value(level, minimum=1)
        slot = self._find_empty_sigil_slot()
        if not slot:
            return None
        if not self._activate_sigil_slot(slot, sigil_hash, level, locked, owner_hash=owner_hash):
            return None
        entry = self.item_db.lookup_hash(sigil_hash)
        serial = self._record_first_value(slot.get("slot_rec"), 0)
        return f"{entry.display_name if entry else f'0x{sigil_hash:08X}'} Lv {max(1, int(level))} -> unit {slot['unit_id']} / serial {serial}"

    def _parse_batch_sigil_lines(self, text: str) -> tuple[List[tuple[str, int, bool]], List[str]]:
        import re
        rows: List[tuple[str, int, bool]] = []
        errors: List[str] = []
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            locked = not any(tok in line.lower() for tok in [" unlocked", ",unlock", ", unlocked", " unlock"])
            clean = re.sub(r"(?i)\b(lock|locked|unlock|unlocked)\b", "", line).strip(" ,")
            level = 15
            m = re.search(r"(?i)\b(?:lv|level)\s*(\d+)\b", clean)
            if m:
                level = int(m.group(1))
                clean = (clean[:m.start()] + clean[m.end():]).strip(" ,")
            else:
                m = re.match(r"^(.+?)\s*[,=]\s*(\d+)\s*$", clean)
                if m:
                    clean = m.group(1).strip()
                    level = int(m.group(2))
            if not clean:
                errors.append(f"Line {line_no}: missing sigil name/GBID")
                continue
            if level < 0:
                errors.append(f"Line {line_no}: level cannot be negative")
                continue
            rows.append((clean, level, locked))
        return rows, errors

    def batch_add_sigils_to_empty_slots(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Batch Add Sigils",
            "One sigil per line. Examples:\nDamage Cap V, 15 locked\nGEEN_020_04 lv15 unlock\nSupplementary DMG V, 15",
            "Damage Cap V, 255 locked\nSupplementary DMG V, 255 locked",
        )
        if not ok:
            return
        rows, errors = self._parse_batch_sigil_lines(text)
        if not rows:
            QMessageBox.information(self, "No rows", "No sigil rows were parsed." + ("\n" + "\n".join(errors[:8]) if errors else ""))
            return
        resolved: List[tuple[int, int, bool, str]] = []
        for key, level, locked in rows:
            h = self._resolve_hash_from_text(key)
            if h is None:
                errors.append(f"Could not resolve sigil: {key}")
            else:
                resolved.append((h, level, locked, key))
        if not resolved:
            QMessageBox.warning(self, "Nothing resolved", "No sigil hashes resolved.\n" + "\n".join(errors[:12]))
            return
        preview_lines = []
        for h, level, locked, key in resolved[:25]:
            entry = self.item_db.lookup_hash(h)
            name = f"{entry.display_name} ({entry.item_id})" if entry else f"{key} -> 0x{h:08X}"
            preview_lines.append(f"- {name} Lv {level} {'locked' if locked else 'unlocked'}")
        msg = "Add these sigils into empty existing slots?\n\n" + "\n".join(preview_lines)
        if len(resolved) > 25:
            msg += f"\n...and {len(resolved)-25} more"
        if errors:
            msg += "\n\nSkipped lines:\n" + "\n".join(errors[:8])
        if QMessageBox.question(self, "Confirm Batch Add Sigils", msg) != QMessageBox.StandardButton.Yes:
            return
        added: List[str] = []
        for h, level, locked, _key in resolved:
            result = self._add_sigil_hash_level_to_empty_slot(h, level, locked)
            if result:
                added.append(result)
            else:
                break
        self._after_editor_patch(f"Batch added {len(added)} sigil rows in memory.")
        QMessageBox.information(self, "Batch add complete", f"Added {len(added)} sigil rows in memory. Use Save As first and verify in-game.")

    def _find_empty_weapon_slot(self) -> Optional[Dict[str, Any]]:
        if not self.save:
            return None
        grouped = self.save.group_by_unit([2803, 2804, 2805, 2806, 2807, 2814, 2815, 2816])
        for unit_id, fields in sorted(grouped.items()):
            hash_rec = fields.get(2803)
            xp_rec = fields.get(2804)
            if not hash_rec or not xp_rec:
                continue
            cur_hash = self._record_first_value(hash_rec, 0)
            cur_xp = self._record_first_value(xp_rec, 0)
            if cur_hash in (0, EMPTY_HASH) and cur_xp == 0:
                return {
                    "unit_id": unit_id,
                    "hash_rec": hash_rec,
                    "xp_rec": xp_rec,
                    "flags_rec": fields.get(2815),
                    "stone_rec": fields.get(2816),
                }
        return None

    def count_empty_weapon_slots(self) -> int:
        if not self.save:
            return 0
        grouped = self.save.group_by_unit([2803, 2804])
        count = 0
        for fields in grouped.values():
            h = fields.get(2803)
            xp = fields.get(2804)
            if h and xp and self._record_first_value(h, 0) in (0, EMPTY_HASH) and self._record_first_value(xp, 0) == 0:
                count += 1
        return count

    def _add_weapon_hash_to_empty_slot(self, weapon_hash: int, name: str = "", gbid: str = "") -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        slot = self._find_empty_weapon_slot()
        if not slot:
            QMessageBox.information(self, "No empty slot found", "I could not find an empty WeaponManager slot to reuse. This build does not insert new FlatBuffer records yet.")
            return
        entry = self.item_db.lookup_hash(weapon_hash)
        display = f"{entry.display_name} ({entry.item_id})" if entry else (f"{name} ({gbid})" if name or gbid else f"0x{weapon_hash:08X}")
        xp, ok = QInputDialog.getInt(self, "Add Weapon XP", f"XP/progress for {display}:", WEAPON_XP_MAX, 0, WEAPON_XP_MAX)
        if not ok:
            return
        xp = self._clamp_weapon_xp_value(xp)
        ok_hash = self._set_record_first_value(slot.get("hash_rec"), weapon_hash, "weapon hash")
        ok_xp = self._set_record_first_value(slot.get("xp_rec"), xp, "weapon XP")
        if slot.get("flags_rec") is not None and self._record_first_value(slot.get("flags_rec"), 0) == 0:
            self._set_record_first_value(slot.get("flags_rec"), 1, "weapon flags")
        if slot.get("stone_rec") is not None and self._record_first_value(slot.get("stone_rec"), 0) == 0:
            self._set_record_first_value(slot.get("stone_rec"), EMPTY_HASH, "weapon stone hash")
        if ok_hash and ok_xp:
            self._after_editor_patch(f"Added {display} to weapon unit {slot['unit_id']} with XP {xp:,}.")

    def add_weapon_to_empty_slot(self) -> None:
        weapon_hash = self._prompt_hash("Add Weapon to Empty Slot", 0)
        if weapon_hash is None:
            return
        self._add_weapon_hash_to_empty_slot(weapon_hash)

    def _add_weapon_hash_xp_to_empty_slot(self, weapon_hash: int, xp: int = 0, flags: Optional[int] = None) -> Optional[str]:
        if not self.save:
            return None
        xp = self._clamp_weapon_xp_value(xp)
        slot = self._find_empty_weapon_slot()
        if not slot:
            return None
        ok_hash = self._set_record_first_value(slot.get("hash_rec"), weapon_hash, "weapon hash")
        ok_xp = self._set_record_first_value(slot.get("xp_rec"), xp, "weapon XP")
        if slot.get("flags_rec") is not None:
            cur = self._record_first_value(slot.get("flags_rec"), 0)
            self._set_record_first_value(slot.get("flags_rec"), 1 if flags is None and cur == 0 else (flags if flags is not None else cur), "weapon flags")
        if slot.get("stone_rec") is not None and self._record_first_value(slot.get("stone_rec"), 0) == 0:
            self._set_record_first_value(slot.get("stone_rec"), EMPTY_HASH, "weapon stone hash")
        if ok_hash and ok_xp:
            entry = self.item_db.lookup_hash(weapon_hash)
            return f"{entry.display_name if entry else f'0x{weapon_hash:08X}'} XP {xp} -> unit {slot['unit_id']}"
        return None

    def _parse_batch_weapon_lines(self, text: str) -> tuple[List[tuple[str, int]], List[str]]:
        import re
        rows: List[tuple[str, int]] = []
        errors: List[str] = []
        for line_no, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            xp = 0
            m = re.search(r"(?i)\b(?:xp|level|lv)\s*(\d+)\b", line)
            if m:
                xp = int(m.group(1))
                line = (line[:m.start()] + line[m.end():]).strip(" ,")
            else:
                m = re.match(r"^(.+?)\s*[,=]\s*(\d+)\s*$", line)
                if m:
                    line = m.group(1).strip()
                    xp = int(m.group(2))
            if not line:
                errors.append(f"Line {line_no}: missing weapon name/GBID")
                continue
            if xp < 0:
                errors.append(f"Line {line_no}: XP cannot be negative")
                continue
            xp = self._clamp_weapon_xp_value(xp)
            rows.append((line, xp))
        return rows, errors

    def batch_add_weapons_to_empty_slots(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Batch Add Weapons",
            "One weapon per line. Examples:\nRukalsa, 999999999\nWEP_PL0200_01 xp999999999\nSword of Eos, 999999999",
            "Rukalsa, 999999999\nSword of Eos, 999999999",
        )
        if not ok:
            return
        rows, errors = self._parse_batch_weapon_lines(text)
        if not rows:
            QMessageBox.information(self, "No rows", "No weapon rows were parsed." + ("\n" + "\n".join(errors[:8]) if errors else ""))
            return
        resolved: List[tuple[int, int, str]] = []
        for key, xp in rows:
            h = self._resolve_hash_from_text(key)
            if h is None:
                errors.append(f"Could not resolve weapon: {key}")
            else:
                resolved.append((h, xp, key))
        if not resolved:
            QMessageBox.warning(self, "Nothing resolved", "No weapon hashes resolved.\n" + "\n".join(errors[:12]))
            return
        added: List[str] = []
        for h, xp, _key in resolved:
            result = self._add_weapon_hash_xp_to_empty_slot(h, xp)
            if result:
                added.append(result)
            else:
                break
        self._after_editor_patch(f"Batch added {len(added)} weapon rows in memory.")

    def _matches_editor_filter(self, row: List[Any], q: str) -> bool:
        """Tokenized AND filter for editor tables."""
        q = (q or "").strip().lower()
        if not q:
            return True
        hay = " ".join(str(x).lower() for x in row)
        return all(token in hay for token in q.split())

    def _sync_known_unknown_filter(self, checked: bool, opposite_box: QCheckBox, refresh_func) -> None:
        """Keep Known Hashes Only and Unknown Hashes Only mutually exclusive."""
        if checked and opposite_box.isChecked():
            opposite_box.blockSignals(True)
            opposite_box.setChecked(False)
            opposite_box.blockSignals(False)
        refresh_func()

    def _meta_values(self, meta: Dict[str, Any], keys: List[str]) -> Dict[str, Any]:
        return {key: self._record_first_value(meta.get(key), 0) for key in keys if meta.get(key) is not None}

    def _patch_meta_values(self, meta: Dict[str, Any], values: Dict[str, Any], key_labels: List[tuple[str, str]]) -> int:
        if not self.save:
            return 0
        patched = 0
        for key, _label in key_labels:
            if key not in values:
                continue
            rec = meta.get(key)
            if rec is None:
                continue
            if rec.value_count < 1:
                continue
            try:
                if hasattr(self.save, "set_first_value"):
                    self.save.set_first_value(rec, values[key])
                else:
                    cur_values = self.save.get_values(rec)
                    if not cur_values:
                        continue
                    cur_values[0] = values[key]
                    self.save.set_values(rec, cur_values)
            except Exception:
                continue
            patched += 1
            self.dirty = True
        return patched

    def _meta_by_unit(self, metas: List[Dict[str, Any]], unit_id: int) -> Optional[Dict[str, Any]]:
        for meta in metas:
            if int(meta.get("unit_id", -1)) == int(unit_id):
                return meta
        return None

    def _confirm_slot_action(self, title: str, message: str) -> bool:
        return QMessageBox.question(
            self, title, message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes

    ITEM_SLOT_FIELDS = [("hash_rec", "item hash"), ("qty_rec", "quantity"), ("flag_rec", "flag")]
    SIGIL_SLOT_FIELDS = [("hash_rec", "sigil hash"), ("level_rec", "level"), ("worn_rec", "worn-by"), ("flags_rec", "flags")]
    WEAPON_SLOT_FIELDS = [("hash_rec", "weapon hash"), ("xp_rec", "XP"), ("flags_rec", "flags"), ("stone_rec", "stone")]

    def copy_selected_item_slot(self) -> None:
        meta = self._selected_meta(self.item_table, self.item_rows_meta)
        if not meta:
            return
        keys = [k for k, _ in self.ITEM_SLOT_FIELDS]
        self.item_slot_clipboard = {"unit_id": meta.get("unit_id"), "values": self._meta_values(meta, keys)}
        self.statusBar().showMessage(f"Copied item slot {meta.get('unit_id')}.", 4000)

    def paste_item_slot_to_selected(self) -> None:
        meta = self._selected_meta(self.item_table, self.item_rows_meta)
        if not meta or not self.item_slot_clipboard:
            QMessageBox.information(self, "No copied item", "Copy an item slot first.")
            return
        if not self._confirm_slot_action("Paste Item Slot", f"Paste copied item data into unit {meta.get('unit_id')}? This overwrites the selected slot in memory."):
            return
        patched = self._patch_meta_values(meta, self.item_slot_clipboard["values"], self.ITEM_SLOT_FIELDS)
        self._after_editor_patch(f"Pasted copied item slot into unit {meta.get('unit_id')} ({patched} fields).")

    def swap_selected_item_with_copied(self) -> None:
        meta = self._selected_meta(self.item_table, self.item_rows_meta)
        if not meta or not self.item_slot_clipboard:
            QMessageBox.information(self, "No copied item", "Copy an item slot first.")
            return
        other = self._meta_by_unit(self.item_rows_meta, self.item_slot_clipboard.get("unit_id"))
        if not other:
            QMessageBox.information(self, "Copied slot hidden", "The copied item slot is not currently visible. Clear filters or show empty slots, then try again.")
            return
        if other is meta:
            QMessageBox.information(self, "Same slot", "Pick a different selected item slot to swap with.")
            return
        if not self._confirm_slot_action("Swap Item Slots", f"Swap item data between units {other.get('unit_id')} and {meta.get('unit_id')}?"):
            return
        keys = [k for k, _ in self.ITEM_SLOT_FIELDS]
        a = self._meta_values(other, keys)
        b = self._meta_values(meta, keys)
        self._patch_meta_values(other, b, self.ITEM_SLOT_FIELDS)
        self._patch_meta_values(meta, a, self.ITEM_SLOT_FIELDS)
        self.item_slot_clipboard = None
        self._after_editor_patch("Swapped item slots in memory.")

    def duplicate_selected_item_to_empty_slot(self) -> None:
        meta = self._selected_meta(self.item_table, self.item_rows_meta)
        if not meta:
            return
        slot = self._find_empty_item_slot()
        if not slot:
            QMessageBox.information(self, "No empty item slot", "No reusable empty item slot was found.")
            return
        if not self._confirm_slot_action("Duplicate Item", f"Duplicate selected item into empty unit {slot.get('unit_id')}?"):
            return
        values = self._meta_values(meta, [k for k, _ in self.ITEM_SLOT_FIELDS])
        self._patch_meta_values(slot, values, self.ITEM_SLOT_FIELDS)
        self._after_editor_patch(f"Duplicated item into empty unit {slot.get('unit_id')}.")

    def copy_selected_sigil_slot(self) -> None:
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            return
        keys = [k for k, _ in self.SIGIL_SLOT_FIELDS]
        self.sigil_slot_clipboard = {"unit_id": meta.get("unit_id"), "values": self._meta_values(meta, keys)}
        self.statusBar().showMessage(f"Copied sigil slot {meta.get('unit_id')}.", 4000)

    def paste_sigil_slot_to_selected(self) -> None:
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta or not self.sigil_slot_clipboard:
            QMessageBox.information(self, "No copied sigil", "Copy a sigil slot first.")
            return
        if not self._confirm_slot_action("Paste Sigil Slot", f"Paste copied sigil data into unit {meta.get('unit_id')}? This overwrites the selected slot in memory."):
            return
        was_empty = bool(meta.get("is_empty"))
        patched = self._patch_meta_values(meta, self.sigil_slot_clipboard["values"], self.SIGIL_SLOT_FIELDS)
        if was_empty:
            serial = self._set_sigil_serial(meta)
            self._after_editor_patch(f"Pasted copied sigil slot into empty unit {meta.get('unit_id')} / serial {serial} ({patched} fields).")
        else:
            self._after_editor_patch(f"Pasted copied sigil slot into unit {meta.get('unit_id')} ({patched} fields).")

    def swap_selected_sigil_with_copied(self) -> None:
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta or not self.sigil_slot_clipboard:
            QMessageBox.information(self, "No copied sigil", "Copy a sigil slot first.")
            return
        other = self._meta_by_unit(self.sigil_rows_meta, self.sigil_slot_clipboard.get("unit_id"))
        if not other:
            QMessageBox.information(self, "Copied slot hidden", "The copied sigil slot is not currently visible. Clear filters or show empty slots, then try again.")
            return
        if other is meta:
            QMessageBox.information(self, "Same slot", "Pick a different selected sigil slot to swap with.")
            return
        if not self._confirm_slot_action("Swap Sigil Slots", f"Swap sigil data between units {other.get('unit_id')} and {meta.get('unit_id')}?"):
            return
        keys = [k for k, _ in self.SIGIL_SLOT_FIELDS]
        a = self._meta_values(other, keys)
        b = self._meta_values(meta, keys)
        self._patch_meta_values(other, b, self.SIGIL_SLOT_FIELDS)
        self._patch_meta_values(meta, a, self.SIGIL_SLOT_FIELDS)
        self.sigil_slot_clipboard = None
        self._after_editor_patch("Swapped sigil slots in memory.")

    def duplicate_selected_sigil_to_empty_slot(self) -> None:
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            return
        slot = self._find_empty_sigil_slot()
        if not slot:
            QMessageBox.information(self, "No empty sigil slot", "No reusable empty sigil slot was found.")
            return
        if not self._confirm_slot_action("Duplicate Sigil", f"Duplicate selected sigil into empty unit {slot.get('unit_id')}?"):
            return
        values = self._meta_values(meta, [k for k, _ in self.SIGIL_SLOT_FIELDS])
        self._patch_meta_values(slot, values, self.SIGIL_SLOT_FIELDS)
        serial = self._set_sigil_serial(slot)
        self._after_editor_patch(f"Duplicated sigil into empty unit {slot.get('unit_id')} / serial {serial}.")

    def copy_selected_weapon_slot(self) -> None:
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta:
            return
        keys = [k for k, _ in self.WEAPON_SLOT_FIELDS]
        self.weapon_slot_clipboard = {"unit_id": meta.get("unit_id"), "values": self._meta_values(meta, keys)}
        self.statusBar().showMessage(f"Copied weapon slot {meta.get('unit_id')}.", 4000)

    def paste_weapon_slot_to_selected(self) -> None:
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta or not self.weapon_slot_clipboard:
            QMessageBox.information(self, "No copied weapon", "Copy a weapon slot first.")
            return
        if not self._confirm_slot_action("Paste Weapon Slot", f"Paste copied weapon data into unit {meta.get('unit_id')}? This overwrites the selected slot in memory."):
            return
        patched = self._patch_meta_values(meta, self.weapon_slot_clipboard["values"], self.WEAPON_SLOT_FIELDS)
        self._after_editor_patch(f"Pasted copied weapon slot into unit {meta.get('unit_id')} ({patched} fields).")

    def swap_selected_weapon_with_copied(self) -> None:
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta or not self.weapon_slot_clipboard:
            QMessageBox.information(self, "No copied weapon", "Copy a weapon slot first.")
            return
        other = self._meta_by_unit(self.weapon_rows_meta, self.weapon_slot_clipboard.get("unit_id"))
        if not other:
            QMessageBox.information(self, "Copied slot hidden", "The copied weapon slot is not currently visible. Clear filters or show empty slots, then try again.")
            return
        if other is meta:
            QMessageBox.information(self, "Same slot", "Pick a different selected weapon slot to swap with.")
            return
        if not self._confirm_slot_action("Swap Weapon Slots", f"Swap weapon data between units {other.get('unit_id')} and {meta.get('unit_id')}?"):
            return
        keys = [k for k, _ in self.WEAPON_SLOT_FIELDS]
        a = self._meta_values(other, keys)
        b = self._meta_values(meta, keys)
        self._patch_meta_values(other, b, self.WEAPON_SLOT_FIELDS)
        self._patch_meta_values(meta, a, self.WEAPON_SLOT_FIELDS)
        self.weapon_slot_clipboard = None
        self._after_editor_patch("Swapped weapon slots in memory.")

    def duplicate_selected_weapon_to_empty_slot(self) -> None:
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta:
            return
        slot = self._find_empty_weapon_slot()
        if not slot:
            QMessageBox.information(self, "No empty weapon slot", "No reusable empty weapon slot was found.")
            return
        if not self._confirm_slot_action("Duplicate Weapon", f"Duplicate selected weapon into empty unit {slot.get('unit_id')}?"):
            return
        values = self._meta_values(meta, [k for k, _ in self.WEAPON_SLOT_FIELDS])
        self._patch_meta_values(slot, values, self.WEAPON_SLOT_FIELDS)
        self._after_editor_patch(f"Duplicated weapon into empty unit {slot.get('unit_id')}.")

    def _selected_item_cap(self, meta: Dict[str, Any]) -> int:
        h = self._record_first_value(meta.get("hash_rec"), 0)
        entry = self.item_db.lookup_hash(h)
        if not entry:
            return 999
        return self._material_cheat_quantity(entry.display_name)

    def max_selected_item_quantity(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.item_table, self.item_rows_meta)
        if not meta:
            return
        cap = self._selected_item_cap(meta)
        if not self._item_meta_has_real_quantity(meta):
            QMessageBox.information(self, "Not a material quantity", "This selected row does not expose a real material/currency stack quantity.")
            return
        if not self._item_meta_is_safe_bulk_quantity_target(meta):
            QMessageBox.warning(self, "Unsafe material row", "This row is not an active material stack and does not have a verified safe-add template, so the editor will not max it automatically.")
            return
        if self._set_item_meta_quantity_safely(meta, cap):
            self._after_editor_patch(f"Selected item quantity set to {cap:,}.")

    def max_visible_item_quantities(self) -> None:
        if not self.save:
            return
        targets = [m for m in self.item_rows_meta if m.get("qty_rec") and not m.get("is_empty") and self._item_meta_is_safe_bulk_quantity_target(m)]
        if not targets:
            QMessageBox.information(self, "No visible items", "No visible item quantity fields are patchable.")
            return
        patched = 0
        for meta in targets:
            if self._set_item_meta_quantity_safely(meta, self._selected_item_cap(meta)):
                patched += 1
        self._after_editor_patch(f"Maxed {patched} visible item/material quantities.")

    def _update_visible_sigil_level_flag_row(self, row_index: int, level_value: Any = None, flags_value: Any = None) -> None:
        try:
            if not hasattr(self, "sigil_model"):
                return
            if row_index < 0 or row_index >= len(self.sigil_model.rows):
                return
            if level_value is not None:
                self.sigil_model.rows[row_index][5] = level_value
            if flags_value is not None:
                self.sigil_model.rows[row_index][8] = flags_value
            self._emit_model_row_changed(self.sigil_model, row_index)
        except Exception:
            pass

    def _refresh_sigil_auxiliary_views_light(self) -> None:
        try:
            if hasattr(self, "sigil_empty_model"):
                self.refresh_sigil_empty_slot_rows()
        except Exception:
            pass
        try:
            if hasattr(self, "sigil_database_model"):
                self.refresh_sigil_database_rows()
        except Exception:
            pass

    def max_selected_sigil(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            return
        row_index = self.sigil_table.currentIndex().row() if hasattr(self, "sigil_table") else -1
        changed = False
        flags_value = None
        if self._set_record_first_value(meta.get("level_rec"), SIGIL_LEVEL_MAX, "sigil level 2704 / FF900A"):
            changed = True
        flags = meta.get("flags_rec")
        if flags is not None:
            cur = self._record_first_value(flags, 0)
            flags_value = int(cur or 0) | 1
            if self._set_record_first_value(flags, flags_value, "sigil flags"):
                changed = True
        if changed:
            self._update_visible_sigil_level_flag_row(row_index, SIGIL_LEVEL_MAX, flags_value)
            self.update_sigil_detail()
            self._after_editor_patch(f"Selected sigil set to level {SIGIL_LEVEL_MAX:,} and locked.", refresh=False)

    def max_visible_sigils(self) -> None:
        if not self.save:
            return
        targets = [(i, m) for i, m in enumerate(self.sigil_rows_meta) if m.get("level_rec") and not m.get("is_empty")]
        if not targets:
            self.statusBar().showMessage("No visible sigil level fields are patchable.", 3500)
            return
        patched = 0
        flags_patched = 0
        for row_index, meta in targets:
            level_changed = self._set_record_first_value(meta.get("level_rec"), SIGIL_LEVEL_MAX, "sigil level 2704 / FF900A")
            flags_value = None
            flags = meta.get("flags_rec")
            if flags is not None:
                cur = self._record_first_value(flags, 0)
                flags_value = int(cur or 0) | 1
                if self._set_record_first_value(flags, flags_value, "sigil flags"):
                    flags_patched += 1
            if level_changed:
                patched += 1
            self._update_visible_sigil_level_flag_row(row_index, SIGIL_LEVEL_MAX, flags_value)
        self.update_sigil_detail()
        self._after_editor_patch(
            f"Maxed visible sigils instantly: {patched:,} level row(s), {flags_patched:,} flag row(s).",
            refresh=False,
        )

    def max_selected_weapon(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta:
            return
        if self._set_record_first_value(meta.get("xp_rec"), WEAPON_XP_MAX, "weapon XP"):
            flags = meta.get("flags_rec")
            if flags is not None:
                cur = self._record_first_value(flags, 0)
                self._set_record_first_value(flags, int(cur or 0) | 1, "weapon flags")
            try:
                row_index = self.weapon_table.currentIndex().row()
                if 0 <= row_index < len(self.weapon_model.rows):
                    self.weapon_model.rows[row_index][4] = WEAPON_XP_MAX
                    self._emit_model_row_changed(self.weapon_model, row_index)
            except Exception:
                pass
            self._after_editor_patch(f"Selected weapon XP/progress set to {WEAPON_XP_MAX:,} and flags enabled.")

    def max_visible_weapons(self) -> None:
        if not self.save:
            return
        targets = [m for m in self.weapon_rows_meta if m.get("xp_rec") and not m.get("is_empty")]
        if not targets:
            self.statusBar().showMessage("No visible weapon XP fields are patchable.", 4000)
            return
        for meta in targets:
            self._set_record_first_value(meta.get("xp_rec"), WEAPON_XP_MAX, "weapon XP")
            flags = meta.get("flags_rec")
            if flags is not None:
                cur = self._record_first_value(flags, 0)
                self._set_record_first_value(flags, int(cur or 0) | 1, "weapon flags")
        try:
            self.refresh_weapon_rows()
        except Exception:
            pass
        self._after_editor_patch(f"Maxed {len(targets)} visible weapons to {WEAPON_XP_MAX:,}.", refresh=False)

    def _all_weapon_level_targets(self) -> List[Dict[str, Any]]:
        if not self.save:
            return []
        grouped = self.save.group_by_unit([2803, 2804, 2815])
        targets: List[Dict[str, Any]] = []
        for unit_id, fields in sorted(grouped.items()):
            weapon_hash = self.value1(fields.get(2803), 0)
            if weapon_hash in ("", 0, EMPTY_HASH):
                continue
            xp_rec = fields.get(2804)
            if xp_rec is None:
                continue
            targets.append({
                "unit_id": unit_id,
                "hash_rec": fields.get(2803),
                "xp_rec": xp_rec,
                "flags_rec": fields.get(2815),
            })
        return targets

    def max_all_weapons(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        targets = self._all_weapon_level_targets()
        if not targets:
            self.statusBar().showMessage("No active weapon XP fields were found to max.", 4000)
            return
        patched = 0
        flags_patched = 0
        for meta in targets:
            if self._set_record_first_value(meta.get("xp_rec"), WEAPON_XP_MAX, "weapon XP"):
                patched += 1
            flags = meta.get("flags_rec")
            if flags is not None:
                cur = self._record_first_value(flags, 0)
                if self._set_record_first_value(flags, int(cur or 0) | 1, "weapon flags"):
                    flags_patched += 1
        try:
            self.refresh_weapon_rows()
        except Exception:
            pass
        self._after_editor_patch(f"Maxed all active weapons to {WEAPON_XP_MAX:,}: {patched} XP fields and {flags_patched} flag fields updated.")


    def _parse_edit_int(self, value: Any, label: str, allow_empty_hash: bool = False, *, minimum: int = I32_MIN, maximum: int = I32_MAX) -> Optional[int]:
        text = str(value).strip()
        if allow_empty_hash and text.lower() in {"", "none", "clear", "empty", "0", "—", "-"}:
            return EMPTY_HASH
        parsed = _parse_intish(text)
        if parsed is not None:
            clamped = self._clamp_i32_value(parsed, minimum=minimum, maximum=maximum, label=label)
            return clamped
        QMessageBox.warning(self, "Invalid value", f"{label} must be a decimal number or 0xHEX value.")
        return None

    def _resolve_edit_hash(self, value: Any, label: str, allow_empty: bool = False) -> Optional[int]:
        text = str(value).strip()
        if allow_empty and text.lower() in {"", "none", "clear", "empty", "0", "—", "-"}:
            return EMPTY_HASH
        resolved = self._resolve_hash_from_text(text)
        if resolved is None:
            QMessageBox.warning(self, "Could not resolve", f"Could not resolve {label}: {text}\nUse a GBID, known name, decimal hash, or 0xHEX hash.")
            return None
        return resolved

    def _emit_model_row_changed(self, model: SimpleRowsModel, row_index: int) -> None:
        try:
            if 0 <= row_index < model.rowCount():
                model.dataChanged.emit(model.index(row_index, 0), model.index(row_index, model.columnCount() - 1), [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole])
        except Exception:
            pass

    def _patch_visible_hash_row(self, model: SimpleRowsModel, row_index: int, name_col: int, gbid_col: int, hash_col: int, value: int) -> None:
        try:
            entry = self.item_db.lookup_hash(value)
            if entry:
                model.rows[row_index][name_col] = entry.display_name
                model.rows[row_index][gbid_col] = entry.item_id
                model.rows[row_index][hash_col] = f"0x{value & 0xFFFFFFFF:08X}"
            elif value in (0, EMPTY_HASH):
                model.rows[row_index][name_col] = "<Empty>"
                model.rows[row_index][gbid_col] = ""
                model.rows[row_index][hash_col] = ""
            else:
                model.rows[row_index][name_col] = f"Unknown 0x{value & 0xFFFFFFFF:08X}"
                model.rows[row_index][gbid_col] = ""
                model.rows[row_index][hash_col] = f"0x{value & 0xFFFFFFFF:08X}"
        except Exception:
            pass

    def _clamp_sigil_level_value(self, value: Any, *, minimum: int = 0) -> int:
        """Clamp sigil level writes to the signed 32-bit max.

        The UI can accept pasted values. This keeps 2704 / FF900A writes inside
        the largest signed 32-bit value and automatically snaps anything higher
        down to 2,147,483,647 instead of failing or wrapping.
        """
        try:
            ivalue = int(str(value).replace(",", "").strip())
        except Exception:
            ivalue = minimum
        if ivalue < int(minimum):
            return int(minimum)
        if ivalue > SIGIL_LEVEL_TEST_MAX:
            return SIGIL_LEVEL_TEST_MAX
        return ivalue

    def apply_sigil_table_cell_edit(self, row_index: int, column: int, value: Any) -> bool:
        if not self.save or row_index >= len(self.sigil_rows_meta):
            return False
        meta = self.sigil_rows_meta[row_index]
        if column in (2, 3, 4):
            resolved = self._resolve_edit_hash(value, "sigil", allow_empty=True)
            if resolved is None:
                return False
            ok = self._set_record_first_value(meta.get("hash_rec"), resolved, "sigil hash")
        elif column == 5:
            parsed = self._parse_edit_int(value, "Sigil level 2704 / FF900A")
            if parsed is None:
                return False
            parsed = self._clamp_sigil_level_value(parsed, minimum=0)
            if hasattr(self, "sigil_level_edit"):
                self._set_line_edit_text_safely("sigil_level_edit", str(parsed))
            ok = self._set_record_first_value(meta.get("level_rec"), parsed, "sigil level 2704 / FF900A")
        elif column in (6, 7):
            resolved = self._resolve_edit_hash(value, "worn-by character", allow_empty=True)
            if resolved is None:
                return False
            if not self._sigil_owner_assignment_allowed(row_index, resolved, show_message=True):
                return False
            ok = self._set_record_first_value(meta.get("worn_rec"), resolved, "sigil worn-by hash")
        elif column == 8:
            parsed = self._parse_edit_int(value, "Sigil flags")
            if parsed is None:
                return False
            ok = self._set_record_first_value(meta.get("flags_rec"), parsed, "sigil flags")
        else:
            return False
        if ok:
            try:
                if column in (2, 3, 4):
                    self._patch_visible_hash_row(self.sigil_model, row_index, 2, 3, 4, resolved)
                    meta["is_empty"] = resolved in (0, EMPTY_HASH)
                    meta["is_known"] = bool(self.item_db.lookup_hash(resolved))
                elif column == 5:
                    self.sigil_model.rows[row_index][5] = parsed
                elif column in (6, 7):
                    self.sigil_model.rows[row_index][6] = "" if resolved in (0, EMPTY_HASH) else self._character_owner_name_for_hash(resolved)
                    self.sigil_model.rows[row_index][7] = self._character_owner_gbid_for_hash(resolved)
                elif column == 8:
                    self.sigil_model.rows[row_index][8] = parsed
                self._emit_model_row_changed(self.sigil_model, row_index)
            except Exception:
                pass
            self._after_editor_patch("Sigil/gem cell updated in memory.")
        return bool(ok)

    def _clamp_weapon_xp_value(self, value: Any) -> int:
        """Clamp weapon XP/progress to the editor's safe max."""
        try:
            ivalue = int(str(value).replace(",", "").strip())
        except Exception:
            ivalue = 0
        return max(0, min(WEAPON_XP_MAX, ivalue))

    def apply_weapon_table_cell_edit(self, row_index: int, column: int, value: Any) -> bool:
        if not self.save or row_index >= len(self.weapon_rows_meta):
            return False
        meta = self.weapon_rows_meta[row_index]
        field_map = {4: ("xp_rec", "weapon XP"), 5: ("unk_2805_rec", "weapon field 2805"), 6: ("unk_2806_rec", "weapon field 2806"), 7: ("unk_2807_rec", "weapon field 2807"), 8: ("unk_2814_rec", "weapon field 2814"), 9: ("flags_rec", "weapon flags")}
        if column in (1, 2, 3):
            resolved = self._resolve_edit_hash(value, "weapon", allow_empty=True)
            if resolved is None:
                return False
            ok = self._set_record_first_value(meta.get("hash_rec"), resolved, "weapon hash")
        elif column == 10:
            resolved = self._resolve_edit_hash(value, "weapon stone", allow_empty=True)
            if resolved is None:
                return False
            ok = self._set_record_first_value(meta.get("stone_rec"), resolved, "weapon stone hash")
        elif column in field_map:
            parsed = self._parse_edit_int(value, field_map[column][1])
            if parsed is None:
                return False
            if column == 4:
                parsed = self._clamp_weapon_xp_value(parsed)
                if hasattr(self, "weapon_xp_edit"):
                    self._set_line_edit_text_safely("weapon_xp_edit", str(parsed))
            ok = self._set_record_first_value(meta.get(field_map[column][0]), parsed, field_map[column][1])
        else:
            return False
        if ok:
            try:
                if column in (1, 2, 3):
                    self._patch_visible_hash_row(self.weapon_model, row_index, 1, 2, 3, resolved)
                    meta["is_empty"] = resolved in (0, EMPTY_HASH)
                    meta["is_known"] = bool(self.item_db.lookup_hash(resolved))
                elif column == 10:
                    entry = self.item_db.lookup_hash(resolved)
                    self.weapon_model.rows[row_index][10] = entry.display_name if entry else ("" if resolved in (0, EMPTY_HASH) else f"Unknown 0x{resolved & 0xFFFFFFFF:08X}")
                elif column in field_map:
                    self.weapon_model.rows[row_index][column] = parsed
                self._emit_model_row_changed(self.weapon_model, row_index)
            except Exception:
                pass
            self._after_editor_patch("Weapon cell updated in memory.")
        return bool(ok)

    def apply_character_table_cell_edit(self, row_index: int, column: int, value: Any) -> bool:
        if not self.save or row_index >= len(self.character_rows_meta):
            return False
        meta = self.character_rows_meta[row_index]
        if column in (1, 2, 3):
            resolved = self._resolve_edit_hash(value, "character", allow_empty=True)
            if resolved is None:
                return False
            ok = self._set_record_first_value(meta.get("hash_rec"), resolved, "character hash")
        elif column == 4:
            parsed = self._parse_edit_int(value, "Character level")
            if parsed is None:
                return False
            parsed = self._clamp_character_value(parsed, minimum=0)
            ok = self._set_record_first_value(meta.get("level_rec"), parsed, "character level")
        elif column == 5:
            parsed = self._parse_edit_int(value, "Character EXP/progress")
            if parsed is None:
                return False
            parsed = self._clamp_character_value(parsed, minimum=0)
            ok = self._set_record_first_value(meta.get("xp_rec"), parsed, "character EXP/progress")
        elif column == 6:
            parsed = self._parse_edit_int(value, "Character MSP/progression candidate")
            if parsed is None:
                return False
            parsed = self._clamp_character_value(parsed, minimum=0)
            ok = self._set_record_first_value(meta.get("msp_rec"), parsed, "character MSP/progression candidate")
        elif column == 7:
            parsed = self._parse_edit_int(value, "Character unlock/active candidate")
            if parsed is None:
                return False
            parsed = self._clamp_character_value(parsed, minimum=0)
            ok = self._set_record_first_value(meta.get("unlock_rec"), parsed, "character unlock/active candidate")
        else:
            return False
        if ok:
            try:
                if column in (1, 2, 3):
                    self._patch_visible_hash_row(self.character_model, row_index, 1, 2, 3, resolved)
                    meta["is_empty"] = resolved in (0, EMPTY_HASH)
                    meta["is_known"] = bool(self.item_db.lookup_hash(resolved))
                elif column == 4:
                    self.character_model.rows[row_index][4] = parsed
                    if meta.get("xp_rec") is not None:
                        self.character_model.rows[row_index][5] = self._record_first_value(meta.get("xp_rec"), self.character_model.rows[row_index][5])
                elif column in (5, 6, 7):
                    self.character_model.rows[row_index][column] = parsed
                self._emit_model_row_changed(self.character_model, row_index)
            except Exception:
                pass
            self._after_editor_patch("Character cell updated in memory.")
        return bool(ok)

    def _character_hash_from_meta(self, meta: Dict[str, Any]) -> Optional[int]:
        value = self._record_first_value(meta.get("hash_rec"), 0)
        if value in (0, EMPTY_HASH):
            return None
        return value & 0xFFFFFFFF

    def equip_visible_sigils_to_character(self) -> None:
        """Bulk-equip every currently visible non-empty sigil to one owner.

        This is intentionally scoped to visible rows so testers can combine it
        with Known/Unknown/Invalid filters before writing a lot of owner refs.
        Empty/addable slots are skipped.
        """
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        choices = list(getattr(self, "character_owner_choices", []) or [])
        if not choices:
            QMessageBox.information(self, "No character list", "No character owner list is loaded yet.")
            return
        labels = [str(c.get("label") or c.get("name") or f"0x{int(c.get('hash', EMPTY_HASH)) & 0xFFFFFFFF:08X}") for c in choices]
        current_label = "None / Unequipped"
        try:
            owner_hash = self._current_sigil_owner_hash()
            current_label = self._character_owner_name_for_hash(owner_hash)
        except Exception:
            pass
        default_index = labels.index(current_label) if current_label in labels else 0
        label, ok = QInputDialog.getItem(
            self,
            "Batch Install Sigils",
            "Equip every currently visible non-empty sigil to:",
            labels,
            default_index,
            False,
        )
        if not ok or not label:
            return
        choice = choices[labels.index(label)]
        try:
            target_hash = int(choice.get("hash", EMPTY_HASH) or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            target_hash = EMPTY_HASH
        visible_metas = [m for m in getattr(self, "sigil_rows_meta", []) if not m.get("is_empty")]
        if not visible_metas:
            QMessageBox.information(self, "No visible sigils", "There are no visible non-empty sigil rows to equip.")
            return
        if not self._sigil_owner_assignment_allowed(0, target_hash, show_message=True):
            return
        if target_hash not in (0, EMPTY_HASH):
            counts = self._sigil_owner_counts_by_hash()
            remaining = max(0, SIGIL_MAX_EQUIPPED_PER_OWNER - counts.get(target_hash, 0))
            if len(visible_metas) > remaining:
                QMessageBox.warning(
                    self,
                    "Bulk equip capped",
                    f"Only {remaining} more sigil(s) can be equipped to this owner before reaching the safe cap of {SIGIL_MAX_EQUIPPED_PER_OWNER}. "
                    "Reduce the visible rows or clear existing equipped refs first."
                )
                return
        preview_name = self._character_owner_name_for_hash(target_hash)
        confirm = QMessageBox.question(
            self,
            "Confirm bulk equip",
            f"Equip {len(visible_metas)} currently visible sigil row(s) to {preview_name}?\n\n"
            "This only changes the loaded save in memory. Use Save/Save As when ready.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        changed = 0
        skipped = 0
        for meta in visible_metas:
            worn_rec = meta.get("worn_rec")
            if worn_rec is None:
                skipped += 1
                continue
            try:
                current = int(self._record_first_value(worn_rec, EMPTY_HASH) or EMPTY_HASH) & 0xFFFFFFFF
            except Exception:
                current = EMPTY_HASH
            if current == target_hash:
                continue
            if self._set_record_first_value(worn_rec, target_hash, "sigil worn-by hash"):
                changed += 1
            else:
                skipped += 1
        self.refresh_sigil_rows()
        self._after_editor_patch(f"Equipped {changed} visible sigil row(s) to {preview_name}. Skipped {skipped}.", refresh=False)

    def equip_selected_sigil_to_selected_character(self) -> None:
        if not self.save:
            return
        sigil_meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not sigil_meta:
            return
        char_meta = self._selected_meta(self.character_table, self.character_rows_meta) if hasattr(self, "character_table") else None
        if not char_meta:
            QMessageBox.information(self, "Select a character", "Select a character on the Characters page first, then return here and click Equip to Selected Character.")
            return
        char_hash = self._character_hash_from_meta(char_meta)
        if char_hash is None:
            QMessageBox.information(self, "Empty character", "The selected character slot does not have a valid character hash.")
            return
        row_index = self.sigil_table.currentIndex().row() if hasattr(self, "sigil_table") else -1
        if not self._sigil_owner_assignment_allowed(row_index, char_hash, show_message=True):
            return
        if self._set_record_first_value(sigil_meta.get("worn_rec"), char_hash, "sigil worn-by hash"):
            self._after_editor_patch(f"Equipped selected sigil to character slot {char_meta.get('slot')} in memory.")

    def equip_copied_sigil_to_selected_character(self) -> None:
        if not self.save:
            return
        if not self.sigil_slot_clipboard:
            QMessageBox.information(self, "No copied sigil", "Copy a sigil slot first from the Sigils page.")
            return
        char_meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not char_meta:
            return
        sigil_meta = self._meta_by_unit(self.sigil_rows_meta, self.sigil_slot_clipboard.get("unit_id"))
        if not sigil_meta:
            QMessageBox.information(self, "Copied sigil hidden", "The copied sigil slot is not currently visible. Clear sigil filters or copy it again.")
            return
        char_hash = self._character_hash_from_meta(char_meta)
        if char_hash is None:
            QMessageBox.information(self, "Empty character", "The selected character slot does not have a valid character hash.")
            return
        row_index = -1
        try:
            for idx, meta in enumerate(getattr(self, "sigil_rows_meta", [])):
                if meta is sigil_meta or meta.get("unit_id") == sigil_meta.get("unit_id"):
                    row_index = idx
                    break
        except Exception:
            pass
        if not self._sigil_owner_assignment_allowed(row_index, char_hash, show_message=True):
            return
        if self._set_record_first_value(sigil_meta.get("worn_rec"), char_hash, "sigil worn-by hash"):
            self._after_editor_patch(f"Equipped copied sigil to character slot {char_meta.get('slot')} in memory.")

    def clear_selected_sigil_worn_by(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            return
        if self._set_record_first_value(meta.get("worn_rec"), EMPTY_HASH, "sigil worn-by hash"):
            self._after_editor_patch("Selected sigil unequipped in memory.")

    def edit_selected_character_hash(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        value = self._prompt_hash("Change Selected Character", self._record_first_value(meta.get("hash_rec"), 0))
        if value is not None and self._set_record_first_value(meta.get("hash_rec"), value, "character hash"):
            self._after_editor_patch("Character hash updated in memory.")

    def edit_selected_character_level(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        cur = self._record_first_value(meta.get("level_rec"), 1)
        value, ok = QInputDialog.getInt(self, "Set Character Level", "Character level:", cur, 1, CHARACTER_VALUE_MAX)
        if ok:
            value = self._clamp_character_value(value, minimum=1)
            patched = 0
            if self._set_record_first_value(meta.get("level_rec"), value, "character level"):
                patched += 1
            if self._set_record_first_value(meta.get("xp_rec"), value, "character EXP/progress"):
                patched += 1
            if patched:
                self._after_editor_patch("Character level and EXP/progress updated in memory.")

    def max_selected_character_level(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        patched = self._set_character_max_bundle(meta)
        if patched:
            self._after_editor_patch(f"Selected character maxed to {CHARACTER_VALUE_MAX:,} ({patched} fields).")

    def max_visible_character_levels(self) -> None:
        if not self.save:
            return
        targets = [m for m in self.character_rows_meta if m.get("level_rec") and not m.get("is_empty")]
        if not targets:
            self.statusBar().showMessage("No visible character level fields are patchable.", 4000)
            return
        patched = 0
        for meta in targets:
            patched += self._set_character_max_bundle(meta)
        try:
            self.refresh_character_rows()
        except Exception:
            pass
        self._after_editor_patch(f"Maxed {len(targets)} visible characters to {CHARACTER_VALUE_MAX:,} ({patched} fields).", refresh=False)

    def _all_character_level_targets(self) -> List[Dict[str, Any]]:
        if not self.save:
            return []
        grouped = self.save.group_by_unit(self.CHARACTER_FIELD_IDS)
        targets: List[Dict[str, Any]] = []
        for unit_id, fields in sorted(grouped.items()):
            if not (10000 <= int(unit_id) <= 10039):
                continue
            char_hash = self.value1(fields.get(1301), 0)
            if char_hash in ("", 0, EMPTY_HASH):
                continue
            level_rec = fields.get(1308)
            if level_rec is None:
                continue
            targets.append({
                "unit_id": unit_id,
                "hash_rec": fields.get(1301),
                "level_rec": level_rec,
                "xp_rec": fields.get(1303),
                "msp_rec": fields.get(1309),
                "unlock_rec": fields.get(1302),
                "state_rec": fields.get(1315),
                "fields": fields,
            })
        return targets

    def max_all_character_levels(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        targets = self._all_character_level_targets()
        if not targets:
            self.statusBar().showMessage("No active character level fields were found to max.", 4000)
            return
        patched = 0
        for meta in targets:
            patched += self._set_character_max_bundle(meta)
        try:
            self.refresh_character_rows()
        except Exception:
            pass
        self._after_editor_patch(f"Maxed all active characters to {CHARACTER_VALUE_MAX:,}: {len(targets)} character slots, {patched} numeric fields updated.")

    def _character_slot_values(self, meta: Dict[str, Any]) -> Dict[int, Any]:
        values: Dict[int, Any] = {}
        for fid, rec in meta.get("fields", {}).items():
            if fid not in self.CHARACTER_FIELD_IDS:
                continue
            if rec is not None and rec.value_count:
                vals = self.save.get_values(rec, 1) if self.save else []
                if vals:
                    values[fid] = vals[0]
        return values

    def _patch_character_slot_values(self, meta: Dict[str, Any], values: Dict[int, Any]) -> int:
        if not self.save:
            return 0
        patched = 0
        fields = meta.get("fields", {})
        for fid, value in values.items():
            rec = fields.get(fid)
            if rec is None:
                continue
            if rec.value_count < 1:
                continue
            try:
                if hasattr(self.save, "set_first_value"):
                    self.save.set_first_value(rec, value)
                else:
                    vals = self.save.get_values(rec)
                    if not vals:
                        continue
                    vals[0] = value
                    self.save.set_values(rec, vals)
            except Exception:
                continue
            self.dirty = True
            patched += 1
        return patched

    def copy_selected_character_slot(self) -> None:
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        self.character_slot_clipboard = {"unit_id": meta.get("unit_id"), "values": self._character_slot_values(meta)}
        self.statusBar().showMessage(f"Copied character slot {meta.get('slot')}.", 4000)

    def paste_character_slot_to_selected(self) -> None:
        if not self.character_slot_clipboard:
            QMessageBox.information(self, "No copied character", "Copy a character slot first.")
            return
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        if not self._confirm_slot_action("Paste Character Slot", f"Paste copied character slot data into slot {meta.get('slot')}? This overwrites character fields only, not item/sigil/weapon data."):
            return
        patched = self._patch_character_slot_values(meta, self.character_slot_clipboard["values"])
        self._after_editor_patch(f"Pasted {patched} character fields.")

    def swap_selected_character_with_copied(self) -> None:
        if not self.character_slot_clipboard:
            QMessageBox.information(self, "No copied character", "Copy a character slot first.")
            return
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        other = self._meta_by_unit(self.character_rows_meta, int(self.character_slot_clipboard.get("unit_id", -1)))
        if not other:
            QMessageBox.information(self, "Copied slot not visible", "The copied character slot is not currently visible. Clear filters or copy it again.")
            return
        if other is meta:
            QMessageBox.information(self, "Same slot", "Select a different character slot to swap with.")
            return
        if not self._confirm_slot_action("Swap Character Slots", f"Swap character fields between slots {other.get('slot')} and {meta.get('slot')}? Save As first and verify in-game."):
            return
        a = self._character_slot_values(meta)
        b = self._character_slot_values(other)
        self._patch_character_slot_values(other, a)
        self._patch_character_slot_values(meta, b)
        self.character_slot_clipboard = None
        self._after_editor_patch("Swapped character slots in memory.")

    def _selected_character_overmastery_values(self) -> Optional[List[int]]:
        if not self.save:
            return None
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return None
        fields = meta.get("fields", {})
        rec = fields.get(1404)
        if rec is None:
            QMessageBox.information(self, "Missing overmastery field", "This character slot does not have field 1404 mapped.")
            return None
        values = self.save.get_values(rec)
        if len(values) != 4:
            QMessageBox.information(self, "Unexpected field size", f"Field 1404 has {len(values)} values; expected 4.")
            return None
        return [int(v) & 0xFFFFFFFF for v in values]

    def copy_selected_character_overmastery(self) -> None:
        values = self._selected_character_overmastery_values()
        if values is None:
            return
        self.overmastery_clipboard = values
        pretty = ", ".join(f"0x{v:08X}" for v in values)
        self.statusBar().showMessage(f"Copied RNG/overmastery slots: {pretty}", 5000)

    def paste_selected_character_overmastery(self) -> None:
        if not self.save:
            return
        if not self.overmastery_clipboard:
            QMessageBox.information(self, "No copied RNG stats", "Copy RNG/overmastery slots from a character first.")
            return
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        if QMessageBox.question(self, "Paste RNG/Overmastery", "Paste the copied four RNG/overmastery hash slots into the selected character? Save As first and verify in-game.") != QMessageBox.StandardButton.Yes:
            return
        result = set_character_overmastery_hashes(self.save, int(meta.get("unit_id")), self.overmastery_clipboard)
        if result.changed_values:
            self.dirty = True
        self._after_editor_patch(patch_summary([result]))

    def edit_selected_character_overmastery(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        current = self._selected_character_overmastery_values()
        if current is None:
            return
        current_text = ", ".join(f"0x{v:08X}" for v in current)
        text, ok = QInputDialog.getText(
            self,
            "Set RNG/Overmastery Slots",
            "Enter exactly 4 hashes/GBIDs/names separated by commas. This is an advanced raw field 1404 edit until bonus names are fully mapped:",
            text=current_text,
        )
        if not ok:
            return
        parts = [x.strip() for x in text.replace("\n", ",").split(",") if x.strip()]
        if len(parts) != 4:
            QMessageBox.warning(self, "Need four values", "Enter exactly 4 values for the four RNG/overmastery slots.")
            return
        hashes: List[int] = []
        for part in parts:
            value = self._resolve_hash_from_text(part)
            if value is None:
                QMessageBox.warning(self, "Could not resolve value", f"Could not resolve: {part}")
                return
            hashes.append(value)
        result = set_character_overmastery_hashes(self.save, int(meta.get("unit_id")), hashes)
        if result.changed_values:
            self.dirty = True
        self._after_editor_patch(patch_summary([result]))

    def clear_selected_character_overmastery(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        if QMessageBox.question(self, "Clear RNG/Overmastery", "Clear the selected character's four RNG/overmastery hash slots to empty?") != QMessageBox.StandardButton.Yes:
            return
        result = clear_character_overmastery_hashes(self.save, int(meta.get("unit_id")))
        if result.changed_values:
            self.dirty = True
        self._after_editor_patch(patch_summary([result]))

    def jump_to_character_unit(self) -> None:
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        self._show_page("Units")
        self.filter_edit.setText(str(meta.get("unit_id")))
        self.unit_model.set_filter(str(meta.get("unit_id")))

    def export_characters_csv(self) -> None:
        self._export_simple_rows("characters", self.character_model.headers, self.character_model.rows)

    def edit_selected_item_quantity(self) -> None:
        if not self.save or not hasattr(self, "item_table"):
            return
        idx = self.item_table.currentIndex()
        meta = self._selected_item_meta()
        if not idx.isValid() or not meta:
            return
        if not (meta.get("wallet_value") or self._item_meta_has_real_quantity(meta)):
            QMessageBox.information(self, "Not a quantity field", "This selected row is technical/reference data and does not expose a safe quantity value.")
            return
        current = self._record_first_value(meta.get("qty_rec"), 0)
        max_value = 99_999_999
        value, ok = QInputDialog.getInt(self, "Edit Quantity", "Quantity / value:", int(current or 0), 0, max_value)
        if not ok:
            return
        if self.apply_item_table_cell_edit(idx.row(), 6, value):
            self.update_item_detail()


    def bulk_set_visible_item_quantity(self) -> None:
        if not self.save:
            return
        value, ok = QInputDialog.getInt(self, "Set Visible Item Quantities", "Set quantity/value on all currently visible item rows:", 999, 0, 99_999_999)
        if not ok:
            return
        count = sum(1 for m in self.item_rows_meta if m.get("qty_rec") and self._item_meta_has_real_quantity(m))
        if count == 0:
            QMessageBox.information(self, "No quantity fields", "No visible rows have a patchable quantity/value field.")
            return
        reply = QMessageBox.question(self, "Bulk edit", f"Patch {count} visible item quantity fields to {value}?")
        if reply != QMessageBox.StandardButton.Yes:
            return
        patched = 0
        for meta in self.item_rows_meta:
            if self._item_meta_has_real_quantity(meta) and self._item_meta_is_safe_bulk_quantity_target(meta):
                if self._set_item_meta_quantity_safely(meta, value):
                    patched += 1
        self._after_editor_patch(f"Patched {patched} visible item quantity fields.")

    def edit_selected_item_hash(self) -> None:
        if not self.save or not hasattr(self, "item_table"):
            return
        idx = self.item_table.currentIndex()
        meta = self._selected_item_meta()
        if not idx.isValid() or not meta:
            return
        current = self._record_first_value(meta.get("hash_rec"), 0)
        item_hash = self._prompt_hash("Edit Item / GBID / Hash", int(current or 0))
        if item_hash is None:
            return
        if self.apply_item_table_cell_edit(idx.row(), 1, f"0x{item_hash:08X}"):
            self.update_item_detail()

    def edit_selected_item_index(self) -> None:
        if not self.save or not hasattr(self, "item_table"):
            return
        idx = self.item_table.currentIndex()
        meta = self._selected_item_meta()
        if not idx.isValid() or not meta:
            return
        rec = meta.get("index_rec")
        if rec is None:
            QMessageBox.information(self, "No index field", "This row does not expose an index/serial field.")
            return
        current = self._record_first_value(rec, 0)
        value, ok = QInputDialog.getInt(self, "Edit Index / Serial", "Index / serial value:", int(current or 0), -2_147_483_648, 2_147_483_647)
        if not ok:
            return
        if self.apply_item_table_cell_edit(idx.row(), 4, value):
            self.update_item_detail()

    def edit_selected_item_flag(self) -> None:
        if not self.save or not hasattr(self, "item_table"):
            return
        idx = self.item_table.currentIndex()
        meta = self._selected_item_meta()
        if not idx.isValid() or not meta:
            return
        rec = meta.get("flag_rec")
        if rec is None:
            QMessageBox.information(self, "No flag field", "This row does not expose a flag/state field.")
            return
        current = self._record_first_value(rec, 0)
        value, ok = QInputDialog.getInt(self, "Edit Flag / State", "Flag / state value:", int(current or 0), -2_147_483_648, 2_147_483_647)
        if not ok:
            return
        if self.apply_item_table_cell_edit(idx.row(), 5, value):
            self.update_item_detail()

    def edit_selected_sigil_level(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            return
        row_index = self.sigil_table.currentIndex().row() if hasattr(self, "sigil_table") else -1
        rec = meta.get("level_rec")
        cur = self._record_first_value(rec, 1)
        value, ok = QInputDialog.getInt(self, "Set Sigil Level", "Primary skill level:", cur, 0, SIGIL_LEVEL_TEST_MAX)
        if ok:
            value = self._clamp_sigil_level_value(value, minimum=0)
            if self._set_record_first_value(rec, value, "sigil level 2704 / FF900A"):
                self._update_visible_sigil_level_flag_row(row_index, value, None)
                self.update_sigil_detail()
                self._after_editor_patch("Sigil level updated in memory.", refresh=False)

    def bulk_set_visible_sigil_level(self) -> None:
        if not self.save:
            return
        value, ok = QInputDialog.getInt(self, "Set Visible Sigil Levels", "Set primary skill level on visible sigils:", SIGIL_LEVEL_MAX, 0, SIGIL_LEVEL_TEST_MAX)
        if not ok:
            return
        value = self._clamp_sigil_level_value(value, minimum=0)
        targets = [(i, m) for i, m in enumerate(self.sigil_rows_meta) if m.get("level_rec") and not m.get("is_empty")]
        if not targets:
            self.statusBar().showMessage("No visible sigil rows have a patchable level field.", 3500)
            return
        patched = 0
        for row_index, meta in targets:
            if self._set_record_first_value(meta.get("level_rec"), value, "sigil level 2704 / FF900A"):
                patched += 1
            self._update_visible_sigil_level_flag_row(row_index, value, None)
        self.update_sigil_detail()
        self._after_editor_patch(f"Patched {patched:,} visible sigil level row(s) to {value:,}.", refresh=False)

    def set_selected_sigil_lock(self, locked: bool) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            return
        rec = meta.get("flags_rec")
        cur = self._record_first_value(rec, 0)
        value = (cur | 1) if locked else (cur & ~1)
        if self._set_record_first_value(rec, value, "sigil flags"):
            self._after_editor_patch("Sigil locked." if locked else "Sigil unlocked.")

    def edit_selected_sigil_hash(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            return
        rec = meta.get("hash_rec")
        value = self._prompt_hash("Change Selected Sigil", self._record_first_value(rec, 0))
        if value is not None and self._set_record_first_value(rec, value, "sigil hash"):
            self._after_editor_patch("Sigil hash updated in memory.")

    def edit_selected_sigil_worn_by(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            return
        rec = meta.get("worn_rec")
        value = self._prompt_hash("Set Sigil Worn-By Character Hash", self._record_first_value(rec, 0))
        row_index = self.sigil_table.currentIndex().row() if hasattr(self, "sigil_table") else -1
        if value is not None and self._sigil_owner_assignment_allowed(row_index, value, show_message=True) and self._set_record_first_value(rec, value, "sigil worn-by hash"):
            self._after_editor_patch("Sigil worn-by hash updated in memory.")

    def edit_selected_weapon_xp(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta:
            return
        rec = meta.get("xp_rec")
        cur = self._record_first_value(rec, 0)
        value, ok = QInputDialog.getInt(self, "Set Weapon XP", "Weapon XP/progress field:", cur, 0, WEAPON_XP_MAX)
        if ok:
            value = self._clamp_weapon_xp_value(value)
            if self._set_record_first_value(rec, value, "weapon XP"):
                self._after_editor_patch("Weapon XP updated in memory.")

    def bulk_set_visible_weapon_xp(self) -> None:
        if not self.save:
            return
        value, ok = QInputDialog.getInt(self, "Set Visible Weapon XP", "Set XP/progress on visible weapons:", WEAPON_XP_MAX, 0, WEAPON_XP_MAX)
        if not ok:
            return
        value = self._clamp_weapon_xp_value(value)
        targets = [m for m in self.weapon_rows_meta if m.get("xp_rec") and not m.get("is_empty")]
        if not targets:
            self.statusBar().showMessage("No visible weapon rows have a patchable XP field.", 4000)
            return
        patched = 0
        for meta in targets:
            if self._set_record_first_value(meta.get("xp_rec"), value, "weapon XP"):
                patched += 1
        try:
            self.refresh_weapon_rows()
        except Exception:
            pass
        self._after_editor_patch(f"Patched {patched} visible weapon XP fields to {value:,}.", refresh=False)

    def edit_selected_weapon_hash(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta:
            return
        rec = meta.get("hash_rec")
        value = self._prompt_hash("Change Selected Weapon", self._record_first_value(rec, 0))
        if value is not None and self._set_record_first_value(rec, value, "weapon hash"):
            self._after_editor_patch("Weapon hash updated in memory.")

    def edit_selected_weapon_flags(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta:
            return
        rec = meta.get("flags_rec")
        cur = self._record_first_value(rec, 0)
        value, ok = QInputDialog.getInt(self, "Set Weapon Flags", "Weapon flags/state value:", cur, -2_147_483_648, 2_147_483_647)
        if ok and self._set_record_first_value(rec, value, "weapon flags"):
            self._after_editor_patch("Weapon flags updated in memory.")

    def edit_selected_weapon_stone(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta:
            return
        rec = meta.get("stone_rec")
        value = self._prompt_hash("Set Weapon Stone Hash", self._record_first_value(rec, 0))
        if value is not None and self._set_record_first_value(rec, value, "weapon stone hash"):
            self._after_editor_patch("Weapon stone hash updated in memory.")

    def clear_selected_weapon_stone(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta)
        if not meta:
            return
        rec = meta.get("stone_rec")
        if self._set_record_first_value(rec, EMPTY_HASH, "weapon stone hash"):
            self._after_editor_patch("Weapon stone hash cleared in memory.")

    def choose_compare_path(self, before: bool) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose before save" if before else "Choose after save", "", "GBFR saves (*.dat GameData*);;All files (*)")
        if not path:
            return
        if before:
            self.compare_before_path = path
        else:
            self.compare_after_path = path
        self.update_compare_label()

    def update_compare_label(self) -> None:
        before = self.compare_before_path or "not selected"
        after = self.compare_after_path or "not selected"
        self.compare_label.setText(f"Before: {before}\nAfter: {after}")

    def compare_two_saves_dialog(self) -> None:
        self.stack.setCurrentWidget(self.compare_text.parent())
        self.choose_compare_path(True)
        if self.compare_before_path:
            self.choose_compare_path(False)
        if self.compare_before_path and self.compare_after_path:
            self.run_compare()

    def run_compare(self) -> None:
        if not self.compare_before_path or not self.compare_after_path:
            QMessageBox.information(self, "Compare", "Choose both a before save and an after save first.")
            return
        try:
            data = compare_saves(self.compare_before_path, self.compare_after_path, limit=300)
            self.compare_text.setPlainText(format_compare_text(data, max_rows=300))
        except Exception as exc:
            QMessageBox.critical(self, "Compare failed", str(exc))

    def export_compare(self, kind: str) -> None:
        if not self.compare_before_path or not self.compare_after_path:
            QMessageBox.information(self, "Compare", "Choose both a before save and an after save first.")
            return
        suffix = ".diff.json" if kind == "json" else ".diff.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export diff", str(Path(self.compare_after_path).with_name(Path(self.compare_after_path).name + suffix)), "JSON (*.json);;CSV (*.csv);;All files (*)")
        if not path:
            return
        try:
            if kind == "json":
                write_compare_json(self.compare_before_path, self.compare_after_path, path, limit=None)
            else:
                write_compare_csv(self.compare_before_path, self.compare_after_path, path)
            QMessageBox.information(self, "Exported", f"Diff written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def open_save(self) -> None:
        if bool(getattr(self, "_save_in_progress", False)):
            QMessageBox.warning(self, "Save still running", "Wait for the current save to finish before opening another save.")
            return
        if bool(getattr(self, "_load_in_progress", False)):
            return
        self._stop_pending_ui_timers_before_save()
        path, _ = QFileDialog.getOpenFileName(self, "Open GBFR save", "", "GBFR SaveData (GameData* SaveData*);;All files (*)")
        if not path:
            return
        self._open_save_path(path)

    def _open_save_path(self, path: str) -> None:
        if bool(getattr(self, "_save_in_progress", False)):
            QMessageBox.warning(self, "Save still running", "Wait for the current save to finish before opening another save.")
            return
        if bool(getattr(self, "_load_in_progress", False)):
            return
        if not path:
            return
        self._load_in_progress = True
        self._io_guard_depth = int(getattr(self, "_io_guard_depth", 0)) + 1
        _debug_log(f"open_save: begin path={path!r}")
        try:
            self.statusBar().showMessage("Opening save...", 0)
        except Exception:
            pass
        try:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        except Exception:
            pass
        try:
            self._stop_pending_ui_timers_before_save()
            self._clear_save_bound_ui_models()
            self._invalidate_save_bound_caches(clear_models=False)
            new_save = GBFRSaveData.open(path)
            _debug_log(f"open_save: parsed records={len(getattr(new_save, 'records', []))} size={len(getattr(new_save, '_file_bytes', b''))}")

            self.save = new_save
            self.dirty = False
            self._last_hash_ok = "not checked"

            self._switch_to_safe_page_without_refresh()
            self._finish_loaded_save_ui(keep_filter=False)
            self.statusBar().showMessage("Save loaded. Open an editor tab to refresh it.", 5000)
            _debug_log("open_save: complete")
        except Exception as exc:
            _debug_log("open_save: FAILED\n" + traceback.format_exc())
            QMessageBox.critical(self, "Open failed", f"{exc}\n\nDebug log:\n{_debug_log_path()}")
        finally:
            try:
                QApplication.restoreOverrideCursor()
            except Exception:
                pass
            self._io_guard_depth = max(0, int(getattr(self, "_io_guard_depth", 1)) - 1)
            self._load_in_progress = False


    def _switch_to_safe_page_without_refresh(self) -> None:
        """Move to Welcome during save swaps without triggering heavy refreshes."""
        try:
            idx = getattr(self, "page_indexes", {}).get("Welcome")
            if idx is None or not hasattr(self, "stack"):
                return
            old = bool(getattr(self, "_refreshing_page", False))
            self._refreshing_page = True
            try:
                self.stack.blockSignals(True)
                self.stack.setCurrentIndex(idx)
                self.stack.blockSignals(False)
                self._update_nav_selection("Welcome")
            finally:
                try:
                    self.stack.blockSignals(False)
                except Exception:
                    pass
                self._refreshing_page = old
        except Exception:
            pass

    def _clear_save_bound_ui_models(self) -> None:
        """Clear visible models and metadata that contain UnitRecord references."""
        for attr in (
            "item_model", "sigil_model", "weapon_model", "character_model",
            "mastery_slot_model", "mastery_mod_model", "mastery_mod_code_model",
            "mastery_mod_preset_model", "mastery_mod_value_model",
            "progression_model", "progression_edit_model", "progression_rows_model",
            "save_map_model", "unit_map_model", "hash_scan_model",
        ):
            model = getattr(self, attr, None)
            if model is not None and hasattr(model, "set_rows"):
                try:
                    model.set_rows([])
                except Exception:
                    pass
        for attr in (
            "item_rows_meta", "sigil_rows_meta", "weapon_rows_meta", "character_rows_meta",
            "mastery_slot_rows_meta", "mastery_mod_rows_meta", "mastery_mod_code_rows_meta",
            "mastery_mod_preset_rows_meta", "mastery_mod_value_rows_meta",
            "items_database_rows_meta", "relic_database_rows_meta", "hash_scan_rows",
        ):
            try:
                setattr(self, attr, [])
            except Exception:
                pass
        for attr in ("item_slot_clipboard", "sigil_slot_clipboard", "weapon_slot_clipboard", "character_slot_clipboard", "overmastery_clipboard"):
            try:
                setattr(self, attr, None)
            except Exception:
                pass

    def _invalidate_save_bound_caches(self, clear_models: bool = True) -> None:
        self._invalidate_add_browser_indexes()
        self._invalidate_mastery_mod_caches(clear_models=clear_models)
        if hasattr(self, "_invalidate_progression_caches"):
            try:
                self._invalidate_progression_caches(catalog=False, vectors=True)
            except Exception:
                pass
        try:
            if hasattr(self, "unit_model"):
                self.unit_model.set_save(None)
        except Exception:
            pass

    def _finish_loaded_save_ui(self, keep_filter: bool = False) -> None:
        """Cheap post-load UI update; avoids hash scans and heavy Mastery refreshes."""
        if not self.save:
            return
        if not keep_filter:
            for attr in ("filter_edit", "item_filter_edit", "sigil_filter_edit", "weapon_filter_edit", "character_filter_edit"):
                widget = getattr(self, attr, None)
                if widget is not None and hasattr(widget, "clear"):
                    try:
                        widget.clear()
                    except Exception:
                        pass
        self._mark_all_pages_stale()
        try:
            self.update_edit_hub_summary_light()
        except Exception:
            try:
                self.update_edit_hub_summary()
            except Exception:
                pass
        try:
            self.update_status_text_light()
        except Exception:
            pass
        # Welcome is now active; mark it as current enough without forcing a
        # summary/hash scan. Other pages remain stale and refresh on demand.
        try:
            self._stale_page_labels.discard("Welcome")
        except Exception:
            pass

    def _invalidate_mastery_mod_caches(self, clear_models: bool = False) -> None:
        """Drop Mastery caches/metadata that contain UnitRecord references.

        UnitRecord objects are only valid for the currently loaded GBFRSaveData
        instance. Keeping old Mastery row metadata after loading another
        save can make later edits/save repairs touch the wrong bytearray.
        """
        self._mastery_mod_cache_key = None
        self._mastery_mod_grouped_cache = None
        self._mastery_mod_anchor_cache_key = None
        self._mastery_mod_anchor_cache = None
        self._mastery_mod_anchor_source = ""
        self._mastery_mod_anchor_score = 0
        self._mastery_mod_anchor_by_field = {}
        self._mastery_mod_anchor_source_by_field = {}
        self._mastery_mod_anchor_score_by_field = {}
        self._mastery_mod_anchor_cache_key_by_field = {}
        self._mastery_mod_abs1606_cache_key = None
        self._mastery_mod_abs1606_cache = {}
        self._mastery_mod_abs1607_cache_key = None
        self._mastery_mod_abs1607_cache = {}
        self.mastery_mod_rows_meta = []
        self.mastery_mod_code_rows_meta = []
        if clear_models:
            for attr in (
                "mastery_mod_model",
                "mastery_mod_code_model",
                "mastery_mod_preset_model",
                "mastery_mod_value_model",
            ):
                model = getattr(self, attr, None)
                if model is not None and hasattr(model, "set_rows"):
                    try:
                        model.set_rows([])
                    except Exception:
                        pass

    def _ui_thread_is_current(self) -> bool:
        try:
            app = QApplication.instance()
            return bool(app is None or self.thread() == app.thread())
        except Exception:
            return True

    def _safe_stop_timer(self, timer: Any) -> None:
        """Stop a QTimer only from the GUI thread.

        The reported save crash prints Qt's "Timers cannot be stopped from
        another thread" warning.  Save/Save As now drains delayed refresh timers
        on the main thread before writing and never touches timers from a
        non-GUI context.
        """
        if timer is None:
            return
        try:
            if not self._ui_thread_is_current():
                return
            app = QApplication.instance()
            if app is not None and hasattr(timer, "thread") and timer.thread() != app.thread():
                return
            if hasattr(timer, "isActive") and timer.isActive():
                timer.stop()
        except RuntimeError:
            # Timer was already destroyed by Qt; ignore during shutdown/save.
            return
        except Exception:
            return

    def _iter_pending_ui_timers(self) -> List[Any]:
        timers: List[Any] = []

        for attr in ("_add_browser_refresh_timer", "_progression_edit_refresh_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                timers.append(timer)

        timers_obj = getattr(self, "_filter_timers", None)
        if isinstance(timers_obj, dict):
            timers.extend(list(timers_obj.values()))
        elif timers_obj is None:
            pass
        else:
            try:
                timers.extend(list(timers_obj))
            except TypeError:
                timers.append(timers_obj)

        # Deduplicate without assuming QTimer is hashable in every wrapper state.
        result: List[Any] = []
        seen: set[int] = set()
        for timer in timers:
            ident = id(timer)
            if ident in seen:
                continue
            seen.add(ident)
            result.append(timer)
        return result

    def _stop_pending_ui_timers_before_save(self) -> None:
        if not self._ui_thread_is_current():
            return
        for timer in self._iter_pending_ui_timers():
            self._safe_stop_timer(timer)

        # Normalize after stopping so the next filter hookup cannot inherit the
        # older list-shaped attribute that caused the save-click AttributeError.
        if not isinstance(getattr(self, "_filter_timers", None), dict):
            self._filter_timers = {}

    def _set_save_busy_ui(self, busy: bool) -> None:
        """Prevent edits/open-load actions while a byte snapshot is being saved."""
        enabled = not bool(busy)
        for widget in (self.centralWidget(), self.menuBar()):
            if widget is not None:
                try:
                    widget.setEnabled(enabled)
                except Exception:
                    pass

    def _prepare_save_snapshot_for_write(self) -> tuple[bytes, str]:
        """Apply pre-save repairs/hash on the GUI thread, then freeze bytes.

        The worker thread writes this immutable bytes object only. It does not
        call self.save.save_as() or read MainWindow/self.save from the background.
        """
        if not self.save:
            raise RuntimeError("No save is loaded.")
        clamped_mastery = self._sanitize_mastery_values_before_save()
        self.repair_added_sigil_slots(silent=True)
        try:
            self.save.update_active_hash()
        except Exception:
            # Some raw/research files may not have the normal hash table. Keep
            # writing possible, matching the old save_as(update_hash=True) path
            # which tolerated a missing active hash by returning None.
            pass
        note = f" Clamped {clamped_mastery} unsafe 1607 value(s)." if clamped_mastery else ""
        return bytes(getattr(self.save, "_file_bytes")), note

    def _run_save_operation(self, target_path: str | Path, success_message: str, *, backup_original: bool = False) -> None:
        """Prepare and write a save snapshot with no background Qt object access.

        The QThread writer prevented UI blocking, but on the user's machine the
        app still crashed after "Saving in background...".  This path removes the
        thread entirely: the GUI thread freezes only long enough to write a small
        bytearray atomically, while all delayed UI refreshes are stopped first.
        """
        if not self.save:
            return
        if bool(getattr(self, "_save_in_progress", False)) or bool(getattr(self, "_load_in_progress", False)):
            self.statusBar().showMessage("Save/load already running. Please wait.", 5000)
            return

        target = Path(target_path)
        backup_source = str(self.save.container.path) if backup_original else ""
        self._save_in_progress = True
        self._io_guard_depth = int(getattr(self, "_io_guard_depth", 0)) + 1
        _debug_log(f"save: begin target={str(target)!r} backup={backup_original}")
        try:
            self._set_save_busy_ui(True)
            self._stop_pending_ui_timers_before_save()
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        except Exception:
            pass
        try:
            self.statusBar().showMessage("Preparing safe save snapshot...", 0)
        except Exception:
            pass

        try:
            data, note = self._prepare_save_snapshot_for_write()
            _debug_log(f"save: snapshot bytes={len(data)} note={note!r}")
            self.statusBar().showMessage("Writing save file...", 0)
            self._write_save_snapshot_sync(target, data, backup_source=backup_source, make_backup=backup_original)
            _debug_log("save: write complete")
            self._finish_successful_save_ui(success_message + note)
        except Exception as exc:
            _debug_log("save: FAILED\n" + traceback.format_exc())
            log_path = self._write_save_failure_log(exc, traceback.format_exc()) or str(_debug_log_path())
            QMessageBox.critical(self, "Save failed", f"{exc}\n\nDebug log:\n{log_path}")
        finally:
            self._save_in_progress = False
            self._io_guard_depth = max(0, int(getattr(self, "_io_guard_depth", 1)) - 1)
            self._set_save_busy_ui(False)
            try:
                QApplication.restoreOverrideCursor()
            except Exception:
                pass

    def _write_save_snapshot_sync(self, target: Path, data: bytes, *, backup_source: str = "", make_backup: bool = False) -> None:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if make_backup and backup_source:
            src = Path(backup_source)
            if src.exists():
                stamp = time.strftime("%Y%m%d_%H%M%S")
                backup_path = target.with_name(f"{target.name}.bak_{stamp}")
                shutil.copy2(src, backup_path)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
            os.replace(tmp_path, target)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
            raise

    def _finish_background_save(self, exc: object, tb_text: object, success_message: str, save_id: Optional[int] = None) -> None:
        try:
            QApplication.restoreOverrideCursor()
        except Exception:
            pass
        self._save_in_progress = False
        self._set_save_busy_ui(False)
        if exc is not None:
            log_path = self._write_save_failure_log(exc, str(tb_text or ""))
            extra = f"\n\nA crash log was written to:\n{log_path}" if log_path else ""
            QMessageBox.critical(self, "Save failed", f"{exc}{extra}")
            return
        # Do not mark a newly loaded save clean if an old background save somehow
        # finishes after the active save object changed. open_save blocks this, but
        # the guard keeps the state correct during edge-case event ordering.
        if save_id is not None and self.save is not None and id(self.save) != int(save_id):
            self.statusBar().showMessage("Previous save write finished. Current loaded save was not changed.", 7000)
            return
        self._finish_successful_save_ui(success_message)

    def _finish_successful_save_ui(self, message: str) -> None:
        """Keep Save/Save As responsive after writing.

        A full refresh can rebuild large mastery/inventory tables immediately after
        the file write. That is unnecessary and has caused apparent save-time
        crashes/freezes on large saves. The in-memory data is already current, so
        only light status text is updated and the heavier pages are marked stale.
        """
        self.dirty = False
        # The write path refreshes the active hash, so keep the cheap status
        # display truthful without immediately rescanning the whole save.
        self._last_hash_ok = True
        self._mark_all_pages_stale()
        try:
            self.update_status_text_light()
        except Exception:
            try:
                self.update_status_text()
            except Exception:
                pass
        self.statusBar().showMessage(message, 7000)

    def _write_save_failure_log(self, exc: Exception, tb_text: str = "") -> str:
        try:
            base = self.save.container.path.parent if self.save else Path.cwd()
            path = base / "gbfr_editor_save_error.log"
            text = tb_text or traceback.format_exc()
            if not text.strip():
                text = repr(exc)
            path.write_text(text, encoding="utf-8")
            return str(path)
        except Exception:
            return ""

    def _sanitize_mastery_values_before_save(self) -> int:
        """Clamp already-written unsafe 1607 mastery values before save.

        This now reads/writes only value[0].  The earlier implementation pulled
        the whole vector for every 1607 record; if an experimental Mastery
        mapping hit a large vector, Save/Save As could appear to crash before the
        file write even started.
        """
        if not self.save:
            return 0
        changed = 0
        for rec in list(getattr(self.save, "records", [])):
            if int(getattr(rec, "id_type", -1)) != 1607 or getattr(rec, "value_count", 0) < 1:
                continue
            try:
                current = int(self.save.get_first_value(rec, 0) if hasattr(self.save, "get_first_value") else self.save.get_values(rec, 1)[0])
            except Exception:
                continue
            if current > MASTERY_1607_SAFE_MAX:
                try:
                    if hasattr(self.save, "set_first_value"):
                        self.save.set_first_value(rec, MASTERY_1607_SAFE_MAX)
                    else:
                        values = self.save.get_values(rec, 1)
                        if not values:
                            continue
                        # Old fallback for compatibility; normal builds now use set_first_value.
                        full = self.save.get_values(rec)
                        full[0] = MASTERY_1607_SAFE_MAX
                        self.save.set_values(rec, full)
                    changed += 1
                except Exception:
                    continue
        if changed:
            self.dirty = True
        return changed

    def save_original(self) -> None:
        if not self.save:
            return
        self._run_save_operation(
            self.save.container.path,
            "Saved over original and created a timestamped .bak file.",
            backup_original=True,
        )

    def _get_save_as_path(self) -> str:
        if not self.save:
            return ""
        default = self.save.container.path.with_name(self.save.container.path.name + ".edited")
        try:
            dialog = QFileDialog(self, "Save As")
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptSave)
            dialog.setFileMode(QFileDialog.FileMode.AnyFile)
            dialog.setNameFilter("All files (*)")
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            dialog.setDirectory(str(default.parent))
            dialog.selectFile(default.name)
            result = dialog.exec()
            try:
                accepted = int(result) == int(QDialog.DialogCode.Accepted)
            except Exception:
                accepted = result == QDialog.DialogCode.Accepted
            if not accepted:
                return ""
            files = dialog.selectedFiles()
            return files[0] if files else ""
        except Exception:
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Save As",
                str(default),
                "All files (*)",
                options=QFileDialog.Option.DontUseNativeDialog,
            )
            return path or ""

    def save_as(self) -> None:
        if not self.save:
            return
        path = self._get_save_as_path()
        if not path:
            return
        self._run_save_operation(
            path,
            f"Saved to: {path}",
            backup_original=False,
        )

    def export_report(self) -> None:
        if not self.save:
            return
        default = str(self.save.container.path.with_suffix(self.save.container.path.suffix + ".report.json"))
        path, _ = QFileDialog.getSaveFileName(self, "Export JSON Report", default, "JSON (*.json);;All files (*)")
        if not path:
            return
        try:
            self.save.export_report(path, limit_values=32)
            QMessageBox.information(self, "Exported", f"Report written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def import_item_csv(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import item CSV", "", "CSV/TSV (*.csv *.tsv *.txt);;All files (*)")
        if not path:
            return
        try:
            db = ItemDatabase.load_csv(path)
            self.merge_item_db(db, "Imported local CSV")
            QMessageBox.information(self, "Imported", f"Imported {len(db)} source rows. Total item DB rows: {len(self.item_db)}")
        except Exception as exc:
            QMessageBox.critical(self, "Import failed", str(exc))

    def refresh_item_id_catalog_rows(self) -> None:
        if not hasattr(self, "item_id_catalog_model"):
            return
        text = self.item_id_catalog_filter.text() if hasattr(self, "item_id_catalog_filter") else ""
        rows = catalog_rows(self.item_db, text)
        self.item_id_catalog_model.set_rows(rows[:2000])
        if hasattr(self, "item_id_catalog_summary"):
            summary = format_catalog_summary(self.item_db)
            if text:
                summary += f"\n\nCurrent filter: {text!r} | visible rows: {len(rows):,}"
            self.item_id_catalog_summary.setPlainText(summary)
        if hasattr(self, "item_id_catalog_table"):
            self._auto_fit_table(self.item_id_catalog_table)

    def selected_item_id_catalog_row(self):
        if not hasattr(self, "item_id_catalog_table"):
            return None
        idx = self.item_id_catalog_table.currentIndex()
        if not idx.isValid():
            return None
        try:
            return self.item_id_catalog_model.rows[idx.row()]
        except Exception:
            return None

    def copy_selected_item_id_catalog_hash(self) -> None:
        row = self.selected_item_id_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select an Item ID row first.")
            return
        self.copy_text(str(row[4]))

    def copy_selected_item_id_catalog_gbid(self) -> None:
        row = self.selected_item_id_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select an Item ID row first.")
            return
        self.copy_text(str(row[3]))

    def export_item_id_catalog_csv(self) -> None:
        default = str(Path.home() / "gbfr_community_item_id_catalog.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export Community Item ID Catalog", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        text = self.item_id_catalog_filter.text() if hasattr(self, "item_id_catalog_filter") else ""
        try:
            write_catalog_csv(self.item_db, path, text)
            QMessageBox.information(self, "Exported", f"Item ID catalog written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))


    def refresh_sigil_gem_catalog_rows(self) -> None:
        if not hasattr(self, "sigil_gem_catalog_model"):
            return
        text = self.sigil_gem_catalog_filter.text() if hasattr(self, "sigil_gem_catalog_filter") else ""
        hide_dummy = bool(getattr(getattr(self, "sigil_gem_hide_dummy", None), "isChecked", lambda: True)())
        rows = sigil_rows(self.item_db, text, hide_dummy=hide_dummy)
        self.sigil_gem_catalog_model.set_rows(rows[:3000])
        if hasattr(self, "sigil_gem_catalog_summary"):
            summary = format_sigil_summary(self.item_db)
            if text or hide_dummy:
                summary += f"\n\nCurrent filter: {text!r} | hide dummy: {hide_dummy} | visible rows: {len(rows):,}"
            self.sigil_gem_catalog_summary.setPlainText(summary)
        if hasattr(self, "sigil_gem_catalog_table"):
            self._auto_fit_table(self.sigil_gem_catalog_table)

    def toggle_sigil_gem_hide_dummy(self) -> None:
        if hasattr(self, "sigil_gem_hide_dummy"):
            self.sigil_gem_hide_dummy.setChecked(not self.sigil_gem_hide_dummy.isChecked())

    def selected_sigil_gem_catalog_row(self):
        if not hasattr(self, "sigil_gem_catalog_table"):
            return None
        idx = self.sigil_gem_catalog_table.currentIndex()
        if not idx.isValid():
            return None
        try:
            return self.sigil_gem_catalog_model.rows[idx.row()]
        except Exception:
            return None

    def copy_selected_sigil_gem_catalog_hash(self) -> None:
        row = self.selected_sigil_gem_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Sigil/Gem ID row first.")
            return
        self.copy_text(str(row[3]))

    def copy_selected_sigil_gem_catalog_gbid(self) -> None:
        row = self.selected_sigil_gem_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Sigil/Gem ID row first.")
            return
        self.copy_text(str(row[2]))

    def add_selected_sigil_gem_catalog_to_empty_slot(self) -> None:
        row = self.selected_sigil_gem_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Sigil/Gem ID row first.")
            return
        gbid = str(row[2])
        if not gbid.upper().startswith("GEEN_"):
            QMessageBox.warning(self, "Not a sigil", "The selected row is not a GEEN sigil row.")
            return
        value = self._resolve_hash_from_text(gbid)
        if value is None:
            QMessageBox.warning(self, "Hash not found", f"Could not resolve {gbid}.")
            return
        result = self._add_sigil_hash_level_to_empty_slot(value, level=SIGIL_LEVEL_MAX, locked=True)
        if not result:
            QMessageBox.warning(self, "No empty sigil slot", "No reusable empty sigil slot was found in this save.")
            return
        self.dirty = True
        self.refresh_item_aware_views()
        QMessageBox.information(self, "Sigil added", result)

    def export_sigil_gem_catalog_csv(self) -> None:
        default = str(Path.home() / "gbfr_community_sigil_gem_catalog.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export Community Sigil/Gem ID Catalog", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        text = self.sigil_gem_catalog_filter.text() if hasattr(self, "sigil_gem_catalog_filter") else ""
        hide_dummy = bool(getattr(getattr(self, "sigil_gem_hide_dummy", None), "isChecked", lambda: True)())
        try:
            write_sigil_catalog_csv(self.item_db, path, text, hide_dummy=hide_dummy)
            QMessageBox.information(self, "Exported", f"Sigil/Gem catalog written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))


    def refresh_trait_skill_catalog_rows(self) -> None:
        if not hasattr(self, "trait_skill_catalog_model"):
            return
        text = self.trait_skill_catalog_filter.text() if hasattr(self, "trait_skill_catalog_filter") else ""
        hide_unused = bool(getattr(getattr(self, "trait_skill_hide_unused", None), "isChecked", lambda: True)())
        rows = trait_skill_rows(self.item_db, text, hide_unused=hide_unused)
        self.trait_skill_catalog_model.set_rows(rows[:3000])
        if hasattr(self, "trait_skill_catalog_summary"):
            summary = format_trait_skill_summary(self.item_db)
            if text or hide_unused:
                summary += f"\n\nCurrent filter: {text!r} | hide unused: {hide_unused} | visible rows: {len(rows):,}"
            self.trait_skill_catalog_summary.setPlainText(summary)
        if hasattr(self, "trait_skill_catalog_table"):
            self._auto_fit_table(self.trait_skill_catalog_table)

    def selected_trait_skill_catalog_row(self):
        if not hasattr(self, "trait_skill_catalog_table"):
            return None
        idx = self.trait_skill_catalog_table.currentIndex()
        if not idx.isValid():
            return None
        try:
            return self.trait_skill_catalog_model.rows[idx.row()]
        except Exception:
            return None

    def copy_selected_trait_skill_catalog_hash(self) -> None:
        row = self.selected_trait_skill_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Trait/Skill ID row first.")
            return
        self.copy_text(str(row[3]))

    def copy_selected_trait_skill_catalog_id(self) -> None:
        row = self.selected_trait_skill_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Trait/Skill ID row first.")
            return
        self.copy_text(str(row[2]))

    def export_trait_skill_catalog_csv(self) -> None:
        default = str(Path.home() / "gbfr_community_trait_skill_catalog.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export Community Trait/Skill ID Catalog", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        text = self.trait_skill_catalog_filter.text() if hasattr(self, "trait_skill_catalog_filter") else ""
        hide_unused = bool(getattr(getattr(self, "trait_skill_hide_unused", None), "isChecked", lambda: True)())
        try:
            write_trait_skill_catalog_csv(self.item_db, path, text, hide_unused=hide_unused)
            QMessageBox.information(self, "Exported", f"Trait/Skill catalog written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))


    def refresh_model_id_catalog_rows(self) -> None:
        if not hasattr(self, "model_id_catalog_model"):
            return
        text = self.model_id_catalog_filter.text() if hasattr(self, "model_id_catalog_filter") else ""
        rows = model_rows(self.resource_db, text)
        self.model_id_catalog_model.set_rows(rows[:3000])
        if hasattr(self, "model_id_catalog_summary"):
            summary = format_model_summary(self.resource_db)
            if text:
                summary += f"\n\nCurrent filter: {text!r} | visible rows: {len(rows):,}"
            self.model_id_catalog_summary.setPlainText(summary)
        if hasattr(self, "model_id_catalog_table"):
            self._auto_fit_table(self.model_id_catalog_table)

    def selected_model_id_catalog_row(self):
        if not hasattr(self, "model_id_catalog_table"):
            return None
        idx = self.model_id_catalog_table.currentIndex()
        if not idx.isValid():
            return None
        try:
            return self.model_id_catalog_model.rows[idx.row()]
        except Exception:
            return None

    def copy_selected_model_id_catalog_hash(self) -> None:
        row = self.selected_model_id_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Model ID row first.")
            return
        if not row[4]:
            QMessageBox.information(self, "No hash", "This row does not have a generated GBFR hash value.")
            return
        self.copy_text(str(row[4]))

    def copy_selected_model_id_catalog_id(self) -> None:
        row = self.selected_model_id_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Model ID row first.")
            return
        self.copy_text(str(row[3]))

    def export_model_id_catalog_csv(self) -> None:
        default = str(Path.home() / "gbfr_community_model_id_catalog.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export Community Model ID Catalog", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        text = self.model_id_catalog_filter.text() if hasattr(self, "model_id_catalog_filter") else ""
        try:
            write_model_catalog_csv(self.resource_db, path, text)
            QMessageBox.information(self, "Exported", f"Model ID catalog written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def refresh_phase_id_catalog_rows(self) -> None:
        if not hasattr(self, "phase_id_catalog_model"):
            return
        text = self.phase_id_catalog_filter.text() if hasattr(self, "phase_id_catalog_filter") else ""
        rows = phase_rows(self.resource_db, text)
        self.phase_id_catalog_model.set_rows(rows[:3000])
        if hasattr(self, "phase_id_catalog_summary"):
            summary = format_phase_summary(self.resource_db)
            if text:
                summary += f"\n\nCurrent filter: {text!r} | visible rows: {len(rows):,}"
            self.phase_id_catalog_summary.setPlainText(summary)
        if hasattr(self, "phase_id_catalog_table"):
            self._auto_fit_table(self.phase_id_catalog_table)

    def selected_phase_id_catalog_row(self):
        if not hasattr(self, "phase_id_catalog_table"):
            return None
        idx = self.phase_id_catalog_table.currentIndex()
        if not idx.isValid():
            return None
        try:
            return self.phase_id_catalog_model.rows[idx.row()]
        except Exception:
            return None

    def copy_selected_phase_id_catalog_hash(self) -> None:
        row = self.selected_phase_id_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Phase ID row first.")
            return
        if not row[5]:
            QMessageBox.information(self, "No hash", "This row does not have a generated phase hash value.")
            return
        self.copy_text(str(row[5]))

    def copy_selected_phase_id_catalog_id(self) -> None:
        row = self.selected_phase_id_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Phase ID row first.")
            return
        self.copy_text(str(row[3]))

    def copy_selected_phase_id_catalog_entity_code(self) -> None:
        row = self.selected_phase_id_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Phase ID row first.")
            return
        self.copy_text(str(row[4]))

    def export_phase_id_catalog_csv(self) -> None:
        default = str(Path.home() / "gbfr_community_phase_id_catalog.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export Community Phase ID Catalog", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        text = self.phase_id_catalog_filter.text() if hasattr(self, "phase_id_catalog_filter") else ""
        try:
            write_phase_catalog_csv(self.resource_db, path, text)
            QMessageBox.information(self, "Exported", f"Phase ID catalog written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))


    def refresh_quest_id_catalog_rows(self) -> None:
        if not hasattr(self, "quest_id_catalog_model"):
            return
        text = self.quest_id_catalog_filter.text() if hasattr(self, "quest_id_catalog_filter") else ""
        rows = quest_rows(self.resource_db, text)
        self.quest_id_catalog_model.set_rows(rows[:5000])
        if hasattr(self, "quest_id_catalog_summary"):
            summary = format_quest_summary(self.resource_db)
            if text:
                summary += f"\n\nCurrent filter: {text!r} | visible rows: {len(rows):,}"
            self.quest_id_catalog_summary.setPlainText(summary)
        if hasattr(self, "quest_id_catalog_table"):
            self._auto_fit_table(self.quest_id_catalog_table)

    def selected_quest_id_catalog_row(self):
        if not hasattr(self, "quest_id_catalog_table"):
            return None
        idx = self.quest_id_catalog_table.currentIndex()
        if not idx.isValid():
            return None
        try:
            return self.quest_id_catalog_model.rows[idx.row()]
        except Exception:
            return None

    def copy_selected_quest_id_catalog_id(self) -> None:
        row = self.selected_quest_id_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Quest ID row first.")
            return
        self.copy_text(str(row[3]))

    def copy_selected_quest_id_catalog_numeric(self) -> None:
        row = self.selected_quest_id_catalog_row()
        if not row:
            QMessageBox.information(self, "No row selected", "Select a Quest ID row first.")
            return
        self.copy_text(str(row[4]))

    def export_quest_id_catalog_csv(self) -> None:
        default = str(Path.home() / "gbfr_community_quest_id_catalog.csv")
        path, _ = QFileDialog.getSaveFileName(self, "Export Community Quest ID Catalog", default, "CSV (*.csv);;All files (*)")
        if not path:
            return
        text = self.quest_id_catalog_filter.text() if hasattr(self, "quest_id_catalog_filter") else ""
        try:
            write_quest_catalog_csv(self.resource_db, path, text)
            QMessageBox.information(self, "Exported", f"Quest ID catalog written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))


    def download_all_community_databases(self) -> None:
        """Refresh all public Community ID databases used by lookup pages.

        This keeps the bundled seed data intact and writes downloaded caches beside
        the EXE/resources folder, so offline builds still work while users can pull
        current item, model, phase, quest, sigil, and trait/skill IDs.
        """
        messages = []
        errors = []
        try:
            db, item_errors = ItemDatabase.download_many([DEFAULT_ITEM_URL, RAW_SIGIL_GEM_URL, TRAIT_SKILL_URL], timeout=45)
            if len(db):
                self.merge_item_db(db, "Downloaded Community item/sigil/trait IDs")
                out = RESOURCE_DIR / "item_ids_downloaded.csv"
                self.item_db.save_csv(out)
                messages.append(f"GBID DB: {len(self.item_db):,} merged rows saved to {out.name}")
            errors.extend(item_errors)
        except Exception as exc:
            errors.append(f"GBID download failed: {exc}")

        try:
            resource_db, resource_errors = ResourceIdDatabase.download_many(DEFAULT_RESOURCE_URLS, timeout=60)
            merged = ResourceIdDatabase.load_many([RESOURCE_DIR / "resource_ids_seed.csv"])
            merged.merge(resource_db)
            out = RESOURCE_DIR / "resource_ids_downloaded.csv"
            merged.save_csv(out)
            self.resource_db = merged
            self.resource_id_model.set_db(self.resource_db)
            self.unit_model.set_resource_db(self.resource_db)
            self.refresh_model_id_catalog_rows()
            self.refresh_phase_id_catalog_rows()
            self.refresh_quest_id_catalog_rows()
            messages.append(f"Resource DB: {len(self.resource_db.entries):,} merged rows saved to {out.name}")
            errors.extend(resource_errors)
        except Exception as exc:
            errors.append(f"Resource DB download failed: {exc}")

        self.refresh_all_views(keep_filter=True)
        msg = "Community database refresh complete."
        if messages:
            msg += "\n\n" + "\n".join(messages)
        if errors:
            msg += "\n\nSome sources failed or were skipped:\n" + "\n".join(errors[:14])
        QMessageBox.information(self, "Community Databases", msg)

    def download_item_ids(self) -> None:
        try:
            db, errors = ItemDatabase.download_many([DEFAULT_ITEM_URL, RAW_SIGIL_GEM_URL, TRAIT_SKILL_URL], timeout=35)
            if errors and not len(db):
                raise RuntimeError("; ".join(errors))
            self.merge_item_db(db, "Downloaded Community IDs")
            out = RESOURCE_DIR / "item_ids_downloaded.csv"
            self.item_db.save_csv(out)
            QMessageBox.information(self, "Downloaded", f"Downloaded {len(db)} source rows. Cached merged DB to:\n{out}" + ("\n\nSome sources failed:\n" + "\n".join(errors[:8]) if errors else ""))
        except Exception as exc:
            QMessageBox.critical(self, "Download failed", f"Could not download/parse item IDs.\n\n{exc}")

    def update_hash_now(self) -> None:
        if not self.save:
            return
        idx = self.save.update_active_hash()
        self.dirty = True
        self.refresh_all_views()
        QMessageBox.information(self, "Hash", f"Updated active hash index: {idx}" if idx is not None else "No active hash seed found.")

    def current_record(self) -> Optional[UnitRecord]:
        if not self.save:
            return None
        idx = self.unit_table.currentIndex()
        if not idx.isValid():
            return None
        return self.unit_model.record_at(idx.row())

    def unit_selected(self) -> None:
        rec = self.current_record()
        if not rec or not self.save:
            self.selected_label.setText("Select a row to edit existing values.")
            return
        unit_label = self.unit_model.unit_labels.label_for(rec)
        label_part = f" / {unit_label}" if unit_label else ""
        self.selected_label.setText(
            f"{rec.kind}:{rec.index}  ID {rec.id_type} / {unit_name(rec.id_type)}  Unit {rec.unit_id}{label_part}  Count {rec.value_count}"
        )

    def copy_selected_values_to_editor(self) -> None:
        rec = self.current_record()
        if not rec or not self.save:
            return
        values = self.save.get_values(rec)
        self.value_edit.setPlainText(", ".join("1" if v is True else "0" if v is False else str(v) for v in values))

    def apply_selected_values(self) -> None:
        rec = self.current_record()
        if not rec or not self.save:
            return
        try:
            values = self.save.parse_user_values(rec, self.value_edit.toPlainText())
            if rec.id_type == 1003:
                reply = QMessageBox.question(self, "Hash seed", "This is the save hash seed record. Editing it can invalidate the active hash slot. Apply anyway?")
                if reply != QMessageBox.StandardButton.Yes:
                    return
            self.save.set_values(rec, values)
            self.dirty = True
            self.refresh_model_id_catalog_rows()
            self.refresh_all_views(keep_filter=True)
            QMessageBox.information(self, "Applied", "Values updated in memory. Save when ready.")
        except Exception as exc:
            QMessageBox.critical(self, "Apply failed", str(exc))

    def refresh_all_views(self, keep_filter: bool = False) -> None:
        """Refresh UI after loading/editing.

        Fast mode refreshes the visible page immediately and marks everything else stale.
        This avoids rebuilding thousands of research/catalog/table rows every time a save opens.
        """
        if not self.save:
            return
        if not keep_filter and hasattr(self, "filter_edit"):
            self.filter_edit.clear()
        if getattr(self, "fast_load_mode", True):
            self._mark_all_pages_stale()
            # Keep load cheap: do not hash-scan or rebuild heavy pages here.
            try:
                self.update_edit_hub_summary_light()
            except Exception:
                self.update_edit_hub_summary()
            self.update_status_text_light()
            label = self._current_page_label()
            if label in {"Welcome"}:
                self._stale_page_labels.discard(label)
            else:
                self._refresh_page_by_label(label, force=True)
            self.statusBar().showMessage("Loaded quickly. Other tabs refresh when opened.", 3500)
            return
        # Full refresh mode for debugging/research.
        self.unit_model.set_save(self.save)
        self.unit_model.set_filter(self.filter_edit.text())
        self.refresh_item_aware_views()
        self.refresh_unit_map_rows()
        self.refresh_save_map_rows()
        self.refresh_id_audit_rows()
        self.refresh_candidate_rows()
        self.refresh_database_rows()
        self.refresh_item_id_catalog_rows()
        self.refresh_sigil_gem_catalog_rows()
        self.refresh_trait_skill_catalog_rows()
        self.refresh_model_id_catalog_rows()
        self.refresh_phase_id_catalog_rows()
        self.refresh_quest_id_catalog_rows()
        self.refresh_preset_rows()
        self.refresh_progression_rows()
        self.refresh_save_health()
        self.hash_scan_rows = []
        self.hash_scan_model.set_rows([])
        if hasattr(self, "hash_scan_text"):
            self.hash_scan_text.setPlainText("Run a hash scan to list known GBIDs and unknown hash-like fields in the loaded save.")
        self.update_status_text()

    def refresh_item_aware_views(self) -> None:
        if getattr(self, "fast_load_mode", True):
            self._mark_all_pages_stale()
            label = self._current_page_label()
            if label == "Sigils":
                self.refresh_sigil_rows()
                self.update_sigil_detail()
            elif label == "Weapons":
                self.refresh_weapon_rows()
                self.update_weapon_detail()
            elif label == "Characters":
                self.refresh_character_rows()
                self.update_character_detail()
            elif label == "Mastery":
                self._populate_mastery_mod_character_combo()
                self.refresh_mastery_mod_rows()
            elif label == "Items / Materials":
                self._refresh_current_items_subtab()
            self.update_edit_hub_summary()
            self.update_status_text()
            return
        self.refresh_sigil_rows()
        self.refresh_weapon_rows()
        self.refresh_character_rows()
        self.refresh_item_rows()
        if hasattr(self, "items_database_table"):
            self.refresh_items_database_rows()
        self.update_item_detail()
        self.update_sigil_detail()
        self.update_weapon_detail()
        self.update_character_detail()
        self.update_edit_hub_summary()


    def refresh_id_audit_rows(self) -> None:
        if not hasattr(self, "id_audit_model"):
            return
        if not self.save:
            self.id_audit_model.set_rows([])
            if hasattr(self, "id_audit_summary"):
                self.id_audit_summary.setPlainText("Open a save to audit hash-like IDs.")
            return
        try:
            include_empty = bool(getattr(getattr(self, "id_audit_empty_check", None), "isChecked", lambda: False)())
            unresolved_only = bool(getattr(getattr(self, "id_audit_unresolved_check", None), "isChecked", lambda: False)())
            hide_ability = bool(getattr(getattr(self, "id_audit_hide_ability_check", None), "isChecked", lambda: False)())
            audit = build_id_audit(self.save, self.item_db, self.resource_db, include_empty=include_empty)
            q = getattr(getattr(self, "id_audit_filter_edit", None), "text", lambda: "")().strip().lower()
            rows: List[List[Any]] = []
            for r in audit:
                if hide_ability and r.get("manager") == "Ability":
                    continue
                if unresolved_only and r.get("status") not in {"unresolved", "candidate"}:
                    continue
                row = [
                    r.get("manager", ""),
                    r.get("field_id", ""),
                    r.get("hash", ""),
                    r.get("status", ""),
                    r.get("name", ""),
                    r.get("gbid_or_id", ""),
                    r.get("occurrences", ""),
                    r.get("units", ""),
                    r.get("source", ""),
                    r.get("note", ""),
                ]
                hay = " ".join(str(x) for x in row).lower()
                if q and q not in hay:
                    continue
                rows.append(row)
            self.id_audit_model.set_rows(rows)
            if hasattr(self, "id_audit_summary"):
                self.id_audit_summary.setPlainText(id_audit_summary(audit))
            if hasattr(self, "id_audit_table"):
                self._auto_fit_table(self.id_audit_table)
        except Exception as exc:
            if hasattr(self, "id_audit_summary"):
                self.id_audit_summary.setPlainText(f"ID audit failed: {exc}")

    def export_id_audit(self, unresolved_only: bool) -> None:
        if not self.save:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export ID audit", "gbfr_id_audit.csv", "CSV Files (*.csv)")
        if not path:
            return
        audit = build_id_audit(self.save, self.item_db, self.resource_db, include_empty=bool(getattr(getattr(self, "id_audit_empty_check", None), "isChecked", lambda: False)()))
        write_id_audit_csv(audit, path, unresolved_only=unresolved_only)
        QMessageBox.information(self, "Exported", f"Exported ID audit to {path}")

    def refresh_save_map_rows(self) -> None:
        if not hasattr(self, "save_map_model"):
            return
        if not self.save:
            self.save_map_model.set_rows([])
            if hasattr(self, "save_map_summary"):
                self.save_map_summary.setPlainText("Open a save to build the manager/field map.")
            return
        try:
            unknown_only = bool(getattr(getattr(self, "save_map_unknown_check", None), "isChecked", lambda: False)())
            data_rows = build_unknown_field_report(self.save, self.item_db) if unknown_only else build_save_map(self.save, self.item_db)
            q = getattr(getattr(self, "save_map_filter_edit", None), "text", lambda: "")().strip().lower()
            rows: List[List[Any]] = []
            for r in data_rows:
                row = [
                    r.get("manager", ""),
                    r.get("confidence", ""),
                    r.get("kind", ""),
                    r.get("field_id", ""),
                    r.get("field_name", ""),
                    r.get("records", ""),
                    r.get("unit_span", ""),
                    r.get("known_hashes", ""),
                    r.get("unknown_hashes", ""),
                    r.get("sample_units", ""),
                    r.get("note", ""),
                ]
                hay = " ".join(str(x) for x in row).lower() + " " + str(r.get("sample_values", "")).lower()
                if q and not all(term in hay for term in q.split()):
                    continue
                rows.append(row)
            self.save_map_model.set_rows(rows)
            if hasattr(self, "save_map_table"):
                self._auto_fit_table(self.save_map_table)
            if hasattr(self, "save_map_summary"):
                self.save_map_summary.setPlainText(save_map_summary_text(self.save, self.item_db))
        except Exception as exc:
            self.save_map_model.set_rows([])
            if hasattr(self, "save_map_summary"):
                self.save_map_summary.setPlainText(f"Save map failed: {exc}")

    def export_save_map(self, kind: str = "csv", unknown_only: bool = False) -> None:
        if not self.save:
            QMessageBox.information(self, "No save", "Open a save first.")
            return
        suffix = ".research_targets" if unknown_only else ".save_map"
        suffix += ".json" if kind == "json" else ".csv"
        default = str(self.save.container.path.with_name(self.save.container.path.name + suffix))
        filter_text = "JSON (*.json);;All files (*)" if kind == "json" else "CSV (*.csv);;All files (*)"
        path, _ = QFileDialog.getSaveFileName(self, "Export Save Map", default, filter_text)
        if not path:
            return
        try:
            if kind == "json":
                write_save_map_json(self.save, self.item_db, path, unknown_only=unknown_only)
            else:
                write_save_map_csv(self.save, self.item_db, path, unknown_only=unknown_only)
            QMessageBox.information(self, "Exported", f"Save map written to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", str(exc))

    def refresh_candidate_rows(self) -> None:
        if not self.save:
            self.candidate_model.set_rows([])
            return
        rows = []
        for cand in build_candidate_records(self.save, self.item_db, limit=3000):
            rows.append([cand.category, cand.confidence, cand.kind, cand.id_type, cand.id_name, cand.unit_id, cand.value_count, cand.preview, cand.note])
        self.candidate_model.set_rows(rows)
        if hasattr(self, "candidate_table"):
            self._auto_fit_table(self.candidate_table)

    def value1(self, rec: Optional[UnitRecord], default: Any = "") -> Any:
        if not rec or not self.save or rec.value_count < 1:
            return default
        return self.save.get_values(rec, 1)[0]

    def hash_text(self, value: Any) -> str:
        try:
            ivalue = int(value)
        except Exception:
            return str(value)
        return self.item_db.lookup_text(ivalue)

    def hash_entry_parts(self, value: Any) -> tuple[str, str, str]:
        """Return display name, source GBID, and 8-digit hash for known GBFR hashes."""
        try:
            ivalue = int(value) & 0xFFFFFFFF
        except Exception:
            return str(value), "", ""
        entry = self.item_db.lookup_hash(ivalue)
        if entry:
            name = entry.name or "Unnamed / reserved"
            return name, entry.item_id, entry.hash_hex
        return f"Unknown 0x{ivalue:08X}", "", f"0x{ivalue:08X}"

    CHARACTER_FIELD_IDS = [1301, 1302, 1303, 1304, 1305, 1307, 1308, 1309, 1310, 1311, 1312, 1313, 1314, 1315, 1316, 1317, 1318, 1321, 1322, 1402, 1403, 1404, 1501, 1502, 1503, 1601, 1602, 1603, 1604, 1605, 1606, 1607]

    def refresh_character_rows(self) -> None:
        if not self.save:
            self.character_model.set_rows([])
            self.character_rows_meta = []
            if hasattr(self, "mastery_slot_model"):
                self.mastery_slot_model.set_rows([])
                self.mastery_slot_rows_meta = []
            if hasattr(self, "character_count_label"):
                self.character_count_label.setText("Open a save to inspect character slots.")
            return
        grouped = self.save.group_by_unit(self.CHARACTER_FIELD_IDS)
        rows: List[List[Any]] = []
        meta_rows: List[Dict[str, Any]] = []
        total_slots = active_slots = known_slots = unknown_slots = empty_slots = 0
        q = getattr(self, "character_filter_edit", None).text().strip().lower() if hasattr(self, "character_filter_edit") else ""
        for unit_id, fields in sorted(grouped.items()):
            if not (10000 <= int(unit_id) <= 10039):
                continue
            total_slots += 1
            char_hash = self.value1(fields.get(1301), 0)
            is_empty = char_hash in ("", 0, 0x887AE0B0)
            if is_empty:
                empty_slots += 1
            else:
                active_slots += 1
            name, gbid, hx = ("<Empty character slot>", "", "") if is_empty else self.hash_entry_parts(char_hash)
            if is_empty:
                pass
            elif gbid:
                known_slots += 1
            else:
                unknown_slots += 1
            slot = int(unit_id) - 10000
            level = self.value1(fields.get(1308), "")
            xp = self.value1(fields.get(1303), "")
            msp = self.value1(fields.get(1309), "")
            unlock = self.value1(fields.get(1302), "")
            row = [slot, name, gbid, hx, level, xp, msp, unlock, unit_id]
            if q and not self._matches_editor_filter(row, q):
                continue
            rows.append(row)
            meta_rows.append({
                "unit_id": unit_id,
                "slot": slot,
                "is_empty": is_empty,
                "is_known": bool(gbid),
                "hash_rec": fields.get(1301),
                "level_rec": fields.get(1308),
                "xp_rec": fields.get(1303),
                "msp_rec": fields.get(1309),
                "unlock_rec": fields.get(1302),
                "state_rec": fields.get(1315),
                "fields": fields,
            })
        self.character_rows_meta = meta_rows
        self.character_model.set_rows(rows)
        if hasattr(self, "character_count_label"):
            self.character_count_label.setText(
                f"Character slots: {self.format_value(active_slots)} active / {self.format_value(total_slots)} total · "
                f"{self.format_value(known_slots)} known · {self.format_value(unknown_slots)} unknown · "
                f"{self.format_value(empty_slots)} empty · showing {self.format_value(len(rows))}"
            )
        if hasattr(self, "character_table"):
            self._set_table_widths(self.character_table, {0: 80, 1: 260, 2: 165, 4: 80, 5: 120, 6: 100, 7: 95})
        self.update_character_detail()
        if hasattr(self, "mastery_character_combo"):
            self._populate_mastery_character_combo()
            self.refresh_mastery_slot_rows()


    def _clean_mastery_sheet_key(self, key: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(key or "").strip().lower())

    def _mastery_row_get(self, row: Dict[str, Any], *preferred: str) -> str:
        if not row:
            return ""
        normalized = {self._clean_mastery_sheet_key(k): k for k in row.keys()}
        for name in preferred:
            key = self._clean_mastery_sheet_key(name)
            if key in normalized:
                return str(row.get(normalized[key], "") or "").strip()
        for key, original in normalized.items():
            if any(self._clean_mastery_sheet_key(name) in key for name in preferred):
                return str(row.get(original, "") or "").strip()
        return ""

    def _parse_mastery_id_search_value(self, text: Any) -> Optional[int]:
        """Parse the save-backed mastery 1606 value.

        The community sheet has had both QMX-style labels and an ID/Search
        column.  ID/Search is the value that should be written to the save.  QMX
        is intentionally not interpreted here unless there is no better source.
        """
        raw = str(text or "").strip().strip("`\"'")
        if not raw:
            return None
        compact = raw.replace(" ", "").replace("-", "")
        # Prefer a literal 8-hex token anywhere in the cell.
        m = re.search(r"(?:0x)?([0-9A-Fa-f]{8})", compact)
        if m:
            return int(m.group(1), 16) & 0xFFFFFFFF
        # Decimal hashes are valid, but tiny row numbers / rank numbers are not.
        raw_digits = raw.replace(",", "")
        if raw_digits.isdecimal():
            value = int(raw_digits, 10)
            if value >= 0x10000:
                return value & 0xFFFFFFFF
            return None
        # GBID/SKILL/MED_EFF-looking IDs may need hashing or DB lookup.
        if any(ch == "_" for ch in raw) or raw.upper().startswith(("SKILL", "MED", "OM", "ABILITY")):
            try:
                value = self._resolve_hash_from_text(raw)
                return None if value is None else int(value) & 0xFFFFFFFF
            except Exception:
                return None
        return None

    def _mastery_mod_choice_from_row(self, row: Dict[str, Any], source: str) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        id_search_text = self._mastery_row_get(row, "ID/Search", "ID Search", "id_search", "Search", "Search ID", "1606", "Effect Hash", "Hash", "ID Hash", "GBID")
        qmx_text = self._mastery_row_get(row, "QMX", "QMX ID", "QMX/Search", "QMX Search")
        value = self._parse_mastery_id_search_value(id_search_text)
        value_source = "ID/Search"
        if value is None:
            # Last-resort compatibility for older cached CSVs that only had QMX.
            value = self._parse_mastery_id_search_value(qmx_text)
            value_source = "QMX fallback"
        if value is None:
            return None
        value &= 0xFFFFFFFF
        if value in (0, EMPTY_HASH, 0xFF460600, 0x280B6CB0):
            return None
        name = self._mastery_row_get(row, "Name", "Effect", "Stat", "Title", "Description", "Skill", "Trait")
        if not name:
            # If the ID/Search cell is a readable ID rather than just hex, use it
            # as the name.  Otherwise QMX is only a display alias.
            name = id_search_text if id_search_text and not re.fullmatch(r"(?:0x)?[0-9A-Fa-f]{8}", id_search_text.strip()) else qmx_text
        if not name:
            name = f"Mastery 0x{value:08X}"
        category = self._mastery_row_get(row, "Category", "Group", "Type", "Tab", "Section") or "Mastery / ID Search"
        notes = self._mastery_row_get(row, "Notes", "Note", "Comment", "Comments")
        note_bits = [f"Source: {source}", f"Value column: {value_source}"]
        if qmx_text:
            note_bits.append(f"QMX alias: {qmx_text}")
        if notes:
            note_bits.append(notes)
        return {
            "value": value,
            "name": str(name).strip(),
            "category": str(category).strip(),
            "notes": "; ".join(note_bits),
            "label": f"{str(name).strip()} · 0x{value:08X}",
        }

    def _load_mastery_mod_choices_from_csv(self, path: Path, source: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not path.exists():
            return out
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    return out
                # Native normalized files use Hash/Name/Category/Notes.  Raw
                # sheet exports may use ID/Search and QMX columns.
                for row in reader:
                    if any(self._clean_mastery_sheet_key(k) in {"idsearch", "qmx"} for k in row.keys()):
                        choice = self._mastery_mod_choice_from_row(row, source)
                    else:
                        text = str(row.get("Hash") or row.get("hash") or "").strip()
                        if not text:
                            continue
                        value = self._parse_mastery_id_search_value(text)
                        if value is None:
                            continue
                        value &= 0xFFFFFFFF
                        if value in (0, EMPTY_HASH, 0xFF460600, 0x280B6CB0):
                            continue
                        name = str(row.get("Name") or row.get("name") or f"0x{value:08X}").strip()
                        cat = str(row.get("Category") or row.get("category") or "Mastery Mod").strip()
                        notes = str(row.get("Notes") or row.get("notes") or "").strip()
                        choice = {"value": value, "name": name, "category": cat, "notes": notes or source, "label": f"{name} · 0x{value:08X}"}
                    if choice:
                        out.append(choice)
        except Exception:
            return out
        return out

    def _load_mastery_mod_choices(self) -> List[Dict[str, Any]]:
        if getattr(self, "mastery_mod_choices_cache", None) is not None:
            return self.mastery_mod_choices_cache or []
        choices: List[Dict[str, Any]] = []
        # Prefer downloaded/normalized ID/Search rows if the user has pulled the
        # sheet.  Keep the packaged seed as a fallback and for test-only values
        # that may not appear on the current sheet.
        for path, source in [
            (RESOURCE_DIR / "mastery_mod_ids_downloaded.csv", "Downloaded Mastery ID/Search sheet gid 1539189767"),
            (RESOURCE_DIR / "mastery_mod_ids_sheet_raw.csv", "Raw Mastery ID/Search sheet gid 1539189767"),
            (RESOURCE_DIR / "mastery_mod_ids_seed.csv", "Packaged fallback seed"),
        ]:
            choices.extend(self._load_mastery_mod_choices_from_csv(path, source))
        if not choices:
            fallback = [
                (0x45C65767, "Critical Rate", "Overmastery"),
                (0xC4925BD7, "Attack Power Up", "Overmastery"),
                (0x43B7581D, "Normal Damage Cap Up", "Overmastery"),
                (0x9C555433, "Skill Damage Cap Up", "Overmastery"),
                (0x9A97C049, "Skill Damage Up", "Overmastery"),
                (0x52A207B5, "Health Up", "Overmastery"),
                (0x6CB38EF3, "Stun Power Up", "Overmastery"),
                (0x4A4C093D, "SBA Damage Cap Up", "Overmastery"),
                (0x4E42646B, "SBA Damage Up", "Overmastery"),
            ]
            choices = [{"value": v, "name": n, "category": c, "notes": "Built-in fallback label", "label": f"{n} · 0x{v:08X}"} for v, n, c in fallback]
        # De-dupe by 1606 value while preserving the first source.  Downloaded
        # ID/Search rows are loaded before the old seed so they win.
        seen = set(); deduped = []
        for choice in choices:
            value = int(choice.get("value", 0)) & 0xFFFFFFFF
            if value in seen:
                continue
            seen.add(value); deduped.append(choice)
        self.mastery_mod_choices_cache = deduped
        return deduped

    def _mastery_mod_choice_label(self, choice: Dict[str, Any], *, show_hash: bool = False) -> str:
        """Return a clean user-facing mastery effect label.

        The ID/Search hash is still stored as itemData and can be searched in
        the database, but the normal editor should not force raw hex into every
        dropdown. Unknown rows keep the hash because there is no readable name.
        """
        try:
            value = int(choice.get("value", 0)) & 0xFFFFFFFF
        except Exception:
            value = 0
        name = str(choice.get("name") or choice.get("label") or "").strip()
        name = re.sub(r"\s*[·-]\s*0x[0-9A-Fa-f]{8}\s*$", "", name).strip()
        if not name:
            name = f"Unknown 0x{value:08X}" if value else "Unknown"
        if show_hash or name.lower().startswith("unknown"):
            return f"{name} · 0x{value:08X}" if value else name
        return name

    def _populate_mastery_mod_effect_combo(self) -> None:
        combo = getattr(self, "mastery_mod_effect_combo", None)
        if combo is None:
            return
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Custom / keep current", None)
        for choice in self._load_mastery_mod_choices():
            combo.addItem(self._mastery_mod_choice_label(choice), int(choice.get("value", 0)))
        if current is not None:
            for i in range(combo.count()):
                if combo.itemData(i) == current:
                    combo.setCurrentIndex(i); break
        combo.blockSignals(False)

    def _populate_overmastery_effect_combos(self) -> None:
        combos = list(getattr(self, "mastery_overmastery_combos", []) or [])
        if not combos:
            return
        choices = self._load_mastery_mod_choices()
        short_names = {
            0xC4925BD7: "Attack Power",
            0x45C65767: "Critical Rate",
            0x43B7581D: "Normal Cap",
            0x9C555433: "Skill Cap",
            0x4A4C093D: "SBA Cap",
            0x4E42646B: "SBA Damage",
            0x68B39018: "Chain Burst",
            0x6CB38EF3: "Stun Power",
            0x52A207B5: "Health",
            0x54929589: "Healing Cap",
            0x9A97C049: "Skill Damage",
        }
        preferred = [
            0xC4925BD7,  # Attack Power
            0x45C65767,  # Critical Rate
            0x43B7581D,  # Normal Cap
            0x9C555433,  # Skill Cap
        ]
        for idx, combo in enumerate(combos):
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("Keep", None)
            added = set()
            for value in preferred:
                combo.addItem(short_names.get(value, f"0x{value:08X}"), int(value) & 0xFFFFFFFF)
                added.add(int(value) & 0xFFFFFFFF)
            for choice in choices:
                try:
                    value = int(choice.get("value", 0)) & 0xFFFFFFFF
                    if value in added:
                        continue
                    label = short_names.get(value)
                    if not label:
                        label = self._mastery_mod_choice_label(choice)
                        label = re.sub(r"\s+Up$", "", str(label)).strip()
                        label = label.replace("Damage Cap", "Cap")
                        label = label.replace("Damage", "Dmg")
                    combo.addItem(label, value)
                    added.add(value)
                except Exception:
                    continue
            target = current if current is not None else preferred[idx] if idx < len(preferred) else None
            if target is not None:
                for i in range(combo.count()):
                    data = combo.itemData(i)
                    if data is not None and int(data) == int(target):
                        combo.setCurrentIndex(i)
                        break
            combo.blockSignals(False)

    def _populate_mastery_mod_character_combo(self) -> None:
        combo = getattr(self, "mastery_mod_character_combo", None)
        if combo is None:
            return
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for choice in self._mastery_character_choices():
            combo.addItem(choice["label"], int(choice["unit"]))
        target = int(current) if current is not None else 10000
        for i in range(combo.count()):
            if int(combo.itemData(i)) == target:
                combo.setCurrentIndex(i); break
        combo.blockSignals(False)

    def _mastery_mod_current_character_unit(self) -> int:
        combo = getattr(self, "mastery_mod_character_combo", None)
        if combo is not None and combo.currentData() is not None:
            return int(combo.currentData())
        return 10000

    def _mastery_mod_state_label(self, state: Any, mastery_value: Any = 0) -> str:
        try:
            s = int(state or 0)
        except Exception:
            return str(state or "")
        try:
            h = int(mastery_value or 0) & 0xFFFFFFFF
        except Exception:
            h = 0
        if h in (0, EMPTY_HASH):
            return "Empty"
        if s == -1:
            return "FFFFFFFF / 80% (-1 signed)"
        if s == 0xFFFFFFFF:
            return "FFFFFFFF / 80%"
        if s == 1023:
            return "1023 (Max)"
        if s == 1:
            return "1 (Active)"
        if s == 0:
            return "0 (Off)"
        return str(s)

    def _mastery_mod_optional_amount_display(self, amount: Any) -> str:
        if amount is None:
            return "Keep current"
        try:
            return str(int(amount))
        except Exception:
            return "Keep current"

    def _parse_mastery_mod_optional_amount(self, value: Any) -> Optional[int]:
        text = str(value or "").strip().replace(",", "")
        if not text or text.lower() in {"keep", "keep current", "current", "same", "none", "skip", "-", "—"}:
            return None
        amount = int(text, 0)
        return max(0, min(2147483647, amount))

    def _mastery_mod_recommended_segments(self) -> List[Dict[str, Any]]:
        """Recommended 600-row OP spread from the testing notes.

        These are ordinal rows within the selected character's editable 1606 mastery rows.
        The editor writes 1606 only; it does not create rows and it does not change sigils.
        """
        return [
            {"start": 1, "end": 100, "value": 0xC4925BD7, "name": "Attack Power Up", "note": "Raw attack base for the build."},
            {"start": 101, "end": 200, "value": 0x4A4C093D, "name": "SBA Damage Cap Up", "note": "Raises SBA cap without over-stacking raw attack."},
            {"start": 201, "end": 400, "value": 0x43B7581D, "name": "Normal Damage Cap Up", "note": "Main normal/heavy attack damage cap range."},
            {"start": 401, "end": 550, "value": 0x9C555433, "name": "Skill Damage Cap Up", "note": "Skill damage cap; testing notes preferred about 150 slots."},
            {"start": 551, "end": 575, "value": 0x6CB38EF3, "name": "Stun Power Up", "note": "Small utility block."},
            {"start": 576, "end": 600, "value": 0x52A207B5, "name": "Health Up", "note": "Small survivability block."},
        ]

    def refresh_mastery_mod_preset_rows(self) -> None:
        model = getattr(self, "mastery_mod_preset_model", None)
        if model is None:
            return
        rows: List[List[Any]] = []
        metas: List[Dict[str, Any]] = []
        available = len(getattr(self, "mastery_mod_rows_meta", []) or [])
        for seg in self._mastery_mod_recommended_segments():
            start = int(seg["start"]); end = int(seg["end"])
            count = max(0, min(end, available) - start + 1) if available else (end - start + 1)
            total = end - start + 1
            value = int(seg["value"]) & 0xFFFFFFFF
            rows.append([
                f"{start}-{end}",
                f"{count}/{total}" if available else str(total),
                str(seg["name"]),
                f"0x{value:08X}",
                str(seg.get("note", "")),
            ])
            metas.append(dict(seg))
        self.mastery_mod_preset_rows_meta = metas
        model.set_rows(rows)
        table = getattr(self, "mastery_mod_preset_table", None)
        if table is not None:
            self._set_table_widths(table, {0: 110, 1: 90, 2: 260, 3: 130, 4: 600})
        label = getattr(self, "mastery_mod_preset_status", None)
        if label is not None:
            if available:
                name = self.mastery_mod_character_combo.currentText() if hasattr(self, "mastery_mod_character_combo") else "selected character"
                label.setText(f"{name}: {available} editable OP/mastery rows found. Pattern writes existing rows only.")
            else:
                label.setText("No editable mastery rows found yet. Load a save and pick a character.")

    def _mastery_mod_recommended_write_1607_enabled(self) -> bool:
        widget = getattr(self, "mastery_mod_preset_write_1607_check", None)
        if widget is None:
            return True
        try:
            return bool(widget.isChecked())
        except Exception:
            return True

    def _mastery_mod_recommended_1607_value(self) -> int:
        widget = getattr(self, "mastery_mod_preset_1607_spin", None)
        if widget is None:
            return 1023
        try:
            return max(0, min(MASTERY_1607_SAFE_MAX, int(widget.value())))
        except Exception:
            return 1023

    def _apply_mastery_mod_recommended_preset_to_unit(self, char_unit: int) -> Dict[str, int]:
        if not self.save or int(char_unit) < 0:
            return {"changed": 0, "changed_1606": 0, "changed_1607": 0, "checked": 0, "missing": 0}
        # Build rows directly from cached grouped records so installing all characters
        # does not rebuild the whole page for every character.
        rows, metas = self._mastery_mod_build_rows_for_character(int(char_unit))
        sorted_metas = sorted(metas, key=lambda m: (int(m.get("row_number", int(m.get("slot", 0)) + 1)), int(m.get("unit_id", 0))))
        changed_1606 = 0
        changed_1607 = 0
        checked = 0
        missing = 0
        write_1607 = self._mastery_mod_recommended_write_1607_enabled()
        state_value = self._mastery_mod_recommended_1607_value()
        for seg in self._mastery_mod_recommended_segments():
            value = int(seg["value"]) & 0xFFFFFFFF
            for ordinal in range(int(seg["start"]), int(seg["end"]) + 1):
                idx = ordinal - 1
                if idx >= len(sorted_metas):
                    missing += 1
                    continue
                meta = sorted_metas[idx]
                rec = meta.get("mastery_rec")
                if rec is None:
                    missing += 1
                    continue
                checked += 1
                _, did_change = self._set_record_first_value_quiet(rec, value)
                changed_1606 += 1 if did_change else 0
                if write_1607:
                    state_rec = meta.get("state_rec")
                    if state_rec is not None:
                        _, did_state_change = self._set_record_first_value_quiet(state_rec, state_value)
                        changed_1607 += 1 if did_state_change else 0
        return {
            "changed": changed_1606 + changed_1607,
            "changed_1606": changed_1606,
            "changed_1607": changed_1607,
            "checked": checked,
            "missing": missing,
        }

    def install_mastery_mod_recommended_preset_selected(self) -> None:
        if not self.save:
            return
        char_unit = self._mastery_mod_current_character_unit()
        stats = self._apply_mastery_mod_recommended_preset_to_unit(char_unit)
        self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
        self.refresh_mastery_mod_rows()
        name = self.mastery_mod_character_combo.currentText() if hasattr(self, "mastery_mod_character_combo") else f"unit {char_unit}"
        write_note = f"1606 changed {stats.get('changed_1606', 0)}, 1607 changed {stats.get('changed_1607', 0)}" if self._mastery_mod_recommended_write_1607_enabled() else f"1606 changed {stats.get('changed_1606', stats['changed'])}; 1607 kept unchanged"
        self.statusBar().showMessage(
            f"Recommended pattern installed for {name}: {stats['changed']} total changed ({write_note}), {stats['checked']} checked, {stats['missing']} missing rows.",
            8000,
        )

    def install_mastery_mod_recommended_preset_all(self) -> None:
        if not self.save:
            return
        total_changed = total_changed_1606 = total_changed_1607 = total_checked = total_missing = groups = 0
        for choice in self._mastery_character_choices():
            try:
                char_unit = int(choice.get("unit"))
            except Exception:
                continue
            stats = self._apply_mastery_mod_recommended_preset_to_unit(char_unit)
            if stats["checked"] or stats["changed"]:
                groups += 1
                total_changed += stats["changed"]
                total_changed_1606 += stats.get("changed_1606", 0)
                total_changed_1607 += stats.get("changed_1607", 0)
                total_checked += stats["checked"]
                total_missing += stats["missing"]
        self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
        self.refresh_mastery_mod_rows()
        write_note = f"1606 changed {total_changed_1606}, 1607 changed {total_changed_1607}" if self._mastery_mod_recommended_write_1607_enabled() else f"1606 changed {total_changed_1606}; 1607 kept unchanged"
        self.statusBar().showMessage(
            f"Recommended pattern installed to {groups} character/mastery groups: {total_changed} total changed ({write_note}), {total_checked} checked, {total_missing} missing rows.",
            9000,
        )

    def _mastery_value_presets(self) -> List[Dict[str, Any]]:
        """Known 1607 amount presets from the Save Wizard all-masteries notes.

        FF470600 in the quick codes maps to SaveData field 1607. These values
        are separate from 1606 effect/stat IDs.
        """
        return [
            {
                "name": "Normal max",
                "value": 0x00000200,
                "risk": "Safer",
                "note": "Save Wizard 'All Masteries Maxed Normal' value. Good first test.",
            },
            {
                "name": "OP test max",
                "value": 1023,
                "risk": "OP / tested",
                "note": "Observed in the Method/Nail after-save. Good pairing with the recommended pattern.",
            },
            {
                "name": "More than normal",
                "value": 0x05F5E0FF,
                "risk": "Risky",
                "note": "Save Wizard 'MORE than normal' value. May cap, glitch damage, or behave oddly.",
            },
            {
                "name": "WTF maybe",
                "value": MASTERY_1607_SAFE_MAX,
                "risk": "Blocked",
                "disabled": True,
                "note": "Blocked in this editor because it can crash save/write paths. Use 'More than normal' instead.",
            },
        ]

    def _mastery_value_records_for_unit(self, char_unit: int) -> List[UnitRecord]:
        """Return paired 1607 amount records for one character/mastery group.

        Community codes show that FF460600 (1606 effect IDs) and FF470600
        (1607 values/amounts) are separate sections.  The reliable pairing is
        the same Save Wizard relative offset in both sections, not necessarily
        the same Unit ID.  Older builds collected 1607 rows by unit math, which
        could make effect slots change while the visible level/amount stayed
        unchanged in game.
        """
        if not self.save:
            return []
        rows: List[UnitRecord] = []
        seen = set()
        try:
            _, metas = self._mastery_mod_build_rows_for_character(int(char_unit))
        except Exception:
            metas = []
        for meta in metas:
            rec = meta.get("state_rec")
            if rec is None or rec.value_count < 1:
                continue
            key = (rec.kind, rec.index, rec.id_type, rec.unit_id, rec.value_data_offset)
            if key in seen:
                continue
            seen.add(key)
            rows.append(rec)
        rows.sort(key=lambda r: int(self._mastery_mod_record_abs(r) or 0))
        return rows

    def _mastery_value_summary_for_unit(self, char_unit: int) -> str:
        if not self.save:
            return "No save loaded"
        counts: Dict[int, int] = {}
        records = self._mastery_value_records_for_unit(char_unit)
        for rec in records:
            try:
                value = int(self.save.get_values(rec, 1)[0])
            except Exception:
                continue
            counts[value] = counts.get(value, 0) + 1
        if not records:
            return "0 editable 1607 records"
        common_parts = []
        for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]:
            common_parts.append(f"{value:,}: {count}")
        common = ", ".join(common_parts)
        return f"{len(records)} editable 1607 records. Current values: {common}"

    def refresh_mastery_value_rows(self) -> None:
        model = getattr(self, "mastery_mod_value_model", None)
        if model is None:
            return
        rows: List[List[Any]] = []
        metas: List[Dict[str, Any]] = []
        for preset in self._mastery_value_presets():
            value = int(preset["value"])
            blocked = bool(preset.get("disabled"))
            rows.append([
                preset["name"],
                f"{value:,}" + ("  (not written)" if blocked else ""),
                f"0x{value:08X}",
                preset["risk"],
                preset["note"],
            ])
            metas.append(dict(preset))
        self.mastery_mod_value_rows_meta = metas
        model.set_rows(rows)
        table = getattr(self, "mastery_mod_value_table", None)
        if table is not None:
            self._set_table_widths(table, {0: 190, 1: 150, 2: 130, 3: 120, 4: 720})
            if rows and not table.currentIndex().isValid():
                table.selectRow(0)
        label = getattr(self, "mastery_mod_value_status", None)
        if label is not None:
            if self.save:
                char_unit = self._mastery_mod_current_character_unit()
                name = self.mastery_mod_character_combo.currentText() if hasattr(self, "mastery_mod_character_combo") else f"unit {char_unit}"
                label.setText(f"{name}: {self._mastery_value_summary_for_unit(char_unit)}")
            else:
                label.setText("Open a save to see current 1607 value counts.")

    def _selected_mastery_value_preset(self) -> Optional[Dict[str, Any]]:
        table = getattr(self, "mastery_mod_value_table", None)
        if table is None:
            return None
        idx = table.currentIndex()
        if idx.isValid() and idx.row() < len(getattr(self, "mastery_mod_value_rows_meta", [])):
            return self.mastery_mod_value_rows_meta[idx.row()]
        metas = getattr(self, "mastery_mod_value_rows_meta", []) or []
        return metas[0] if metas else None

    def _apply_mastery_value_preset_to_unit(self, char_unit: int, value: int) -> Dict[str, int]:
        if not self.save:
            return {"changed": 0, "checked": 0, "missing": 0}
        changed = 0
        checked = 0
        for rec in self._mastery_value_records_for_unit(int(char_unit)):
            checked += 1
            _, did_change = self._set_record_first_value_quiet(rec, int(value))
            changed += 1 if did_change else 0
        return {"changed": changed, "checked": checked, "missing": 0 if checked else 1}

    def apply_mastery_value_preset_selected_character(self) -> None:
        if not self.save:
            return
        preset = self._selected_mastery_value_preset()
        if not preset:
            self.statusBar().showMessage("Pick a 1607 preset first.", 3500)
            return
        if preset.get("disabled"):
            self.statusBar().showMessage("WTF maybe / 0x7FFFFFFF is blocked because it can crash saving. Use More than normal instead.", 9000)
            return
        char_unit = self._mastery_mod_current_character_unit()
        value = int(preset["value"])
        stats = self._apply_mastery_value_preset_to_unit(char_unit, value)
        self._mark_stale_pages(["Mastery", "Save Health"])
        if hasattr(self, "mastery_mod_value_status"):
            try:
                self.mastery_mod_value_status.setText(self._mastery_value_summary_for_unit(char_unit))
            except Exception:
                pass
        name = self.mastery_mod_character_combo.currentText() if hasattr(self, "mastery_mod_character_combo") else f"unit {char_unit}"
        self.statusBar().showMessage(
            f"Applied 1607 {preset['name']} ({value:,} / 0x{value:08X}) to {name}: {stats['changed']} changed, {stats['checked']} checked.",
            7500,
        )

    def apply_mastery_value_preset_all_characters(self) -> None:
        if not self.save:
            return
        preset = self._selected_mastery_value_preset()
        if not preset:
            self.statusBar().showMessage("Pick a 1607 preset first.", 3500)
            return
        if preset.get("disabled"):
            self.statusBar().showMessage("WTF maybe / 0x7FFFFFFF is blocked because it can crash saving. Use More than normal instead.", 9000)
            return
        value = int(preset["value"])
        total_changed = total_checked = groups = 0
        for choice in self._mastery_character_choices():
            try:
                char_unit = int(choice.get("unit"))
            except Exception:
                continue
            stats = self._apply_mastery_value_preset_to_unit(char_unit, value)
            if stats["checked"]:
                groups += 1
                total_changed += stats["changed"]
                total_checked += stats["checked"]
        self._mark_stale_pages(["Mastery", "Save Health"])
        if hasattr(self, "mastery_mod_value_status"):
            self.mastery_mod_value_status.setText(
                f"Applied to {groups} groups. Current page marked stale; use Refresh Current Page when needed."
            )
        self.statusBar().showMessage(
            f"Applied 1607 {preset['name']} ({value:,} / 0x{value:08X}) to {groups} groups: {total_changed} changed, {total_checked} checked.",
            9000,
        )

    def refresh_mastery_mod_reference_rows(self) -> None:
        model = getattr(self, "mastery_mod_reference_model", None)
        if model is None:
            return
        rows: List[List[Any]] = []
        metas: List[Dict[str, Any]] = []
        for choice in self._load_mastery_mod_choices():
            value = int(choice.get("value", 0)) & 0xFFFFFFFF
            # The sheet/code evidence identifies the 1606 effect hashes. It does
            # not prove that every effect should force the same 1607 value, so
            # reference rows now default to preserving the current save value.
            amount = None
            rows.append([
                str(choice.get("name", "")),
                f"0x{value:08X}",
                self._mastery_mod_optional_amount_display(amount),
                str(choice.get("category", "Mastery Mod")),
                str(choice.get("notes", "")),
            ])
            metas.append({
                "name": str(choice.get("name", "")),
                "value": value,
                "amount": amount,
                "category": str(choice.get("category", "Mastery Mod")),
                "notes": str(choice.get("notes", "")),
            })
        self.mastery_mod_reference_rows_meta = metas
        model.set_rows(rows)
        table = getattr(self, "mastery_mod_reference_table", None)
        if table is not None:
            self._set_table_widths(table, {0: 280, 1: 135, 2: 190, 3: 150, 4: 560})

    def download_mastery_mod_id_search_db(self) -> None:
        """Download gid 1539189767 and normalize ID/Search values for Mastery."""
        try:
            text = load_sheet_csv(MASTERY_ID_SEARCH_SHEET_URL, timeout=35)
            raw_path = RESOURCE_DIR / "mastery_mod_ids_sheet_raw.csv"
            raw_path.write_text(text, encoding="utf-8")
            reader = csv.DictReader(text.splitlines())
            choices: List[Dict[str, Any]] = []
            for row in reader:
                choice = self._mastery_mod_choice_from_row(row, "Downloaded Mastery ID/Search sheet gid 1539189767")
                if choice:
                    choices.append(choice)
            if not choices:
                QMessageBox.warning(
                    self,
                    "No mastery rows found",
                    "Downloaded the sheet, but I could not find any usable ID/Search mastery values. The raw CSV was still cached for inspection.",
                )
                return
            out_path = RESOURCE_DIR / "mastery_mod_ids_downloaded.csv"
            with out_path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["Hash", "Name", "Category", "Notes", "Source"])
                writer.writeheader()
                seen = set()
                for choice in choices:
                    value = int(choice.get("value", 0)) & 0xFFFFFFFF
                    if value in seen:
                        continue
                    seen.add(value)
                    writer.writerow({
                        "Hash": f"{value:08X}",
                        "Name": str(choice.get("name", "")),
                        "Category": str(choice.get("category", "Mastery / ID Search")),
                        "Notes": str(choice.get("notes", "")),
                        "Source": "Google Sheet gid 1539189767 ID/Search",
                    })
            self.mastery_mod_choices_cache = None
            self._populate_mastery_mod_effect_combo()
            self._populate_overmastery_effect_combos()
            self.refresh_mastery_mod_reference_rows()
            self.refresh_mastery_mod_preset_rows()
            self.statusBar().showMessage(f"Downloaded {len(choices)} Mastery ID/Search row(s) and cached {out_path.name}.", 6500)
            QMessageBox.information(
                self,
                "Mastery ID/Search DB updated",
                f"Downloaded and normalized {len(choices)} ID/Search mastery row(s).\n\nSaved normalized cache:\n{out_path}\n\nQMX cells are kept only as aliases/notes; ID/Search is used for 1606 writes.",
            )
        except Exception as exc:
            QMessageBox.critical(self, "Mastery DB download failed", str(exc))

    def _selected_mastery_mod_reference_meta(self) -> Optional[Dict[str, Any]]:
        table = getattr(self, "mastery_mod_reference_table", None)
        if table is None:
            return None
        idx = table.currentIndex()
        if idx.isValid() and idx.row() < len(getattr(self, "mastery_mod_reference_rows_meta", [])):
            return self.mastery_mod_reference_rows_meta[idx.row()]
        return None

    def apply_mastery_mod_reference_cell_edit(self, row: int, column: int, value: Any) -> bool:
        if column != 2:
            return False
        try:
            amount = self._parse_mastery_mod_optional_amount(value)
        except Exception:
            self.statusBar().showMessage("Optional 1607 must be blank/Keep current or a number such as 1023.", 3500)
            return False
        try:
            self.mastery_mod_reference_model.rows[row][column] = self._mastery_mod_optional_amount_display(amount)
            self.mastery_mod_reference_rows_meta[row]["amount"] = amount
        except Exception:
            return False
        idx = self.mastery_mod_reference_model.index(row, column)
        self.mastery_mod_reference_model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole])
        if getattr(self, "mastery_mod_reference_table", None) is not None and self.mastery_mod_reference_table.currentIndex().row() == row:
            self.use_selected_mastery_mod_reference_effect(apply=False)
            if amount is not None and getattr(self, "mastery_mod_live_check", None) is not None and self.mastery_mod_live_check.isChecked():
                self.apply_mastery_mod_selected_row_edit(silent=True)
        return True

    def use_selected_mastery_mod_reference_effect(self, apply: bool = False) -> None:
        meta = self._selected_mastery_mod_reference_meta()
        if not meta:
            return
        self._mastery_mod_loading = True
        try:
            value = int(meta.get("value", 0)) & 0xFFFFFFFF
            amount = meta.get("amount", None)
            combo = getattr(self, "mastery_mod_effect_combo", None)
            if combo is not None:
                for i in range(combo.count()):
                    data = combo.itemData(i)
                    if data is not None and int(data) == value:
                        combo.setCurrentIndex(i)
                        break
            write_check = getattr(self, "mastery_mod_state_write_check", None)
            if amount is not None:
                if write_check is not None:
                    write_check.setChecked(True)
                if hasattr(self, "mastery_mod_state_spin"):
                    self.mastery_mod_state_spin.setValue(max(0, min(self.mastery_mod_state_spin.maximum(), int(amount))))
                amount_text = str(int(amount))
            else:
                amount_text = "Use value box / current checkbox setting"
            if hasattr(self, "mastery_mod_reference_status"):
                self.mastery_mod_reference_status.setText(
                    f"Selected {meta.get('name')} · writes 1606 0x{value:08X} · 1607 {amount_text}."
                )
        finally:
            self._mastery_mod_loading = False
        if apply:
            self.apply_mastery_mod_selected_row_edit(silent=False)

    def set_mastery_selected_row_value(self, value: int) -> None:
        """Directly set only the paired mastery value for the selected row."""
        try:
            self.mastery_mod_state_spin.setValue(int(value))
        except Exception:
            pass
        self.apply_mastery_selected_row_value_only()

    def apply_mastery_selected_row_value_only(self) -> None:
        """Write only FF470600 / field 1607 for the selected concrete row."""
        if not self.save:
            return
        meta = self._selected_mastery_mod_target_meta()
        if not meta:
            self.statusBar().showMessage("Pick an existing mastery row first.", 4500)
            return
        state_rec = meta.get("state_rec")
        target_unit = int(meta.get("unit_id") or 0)
        value = int(getattr(self, "mastery_mod_state_spin").value()) if hasattr(self, "mastery_mod_state_spin") else 1023
        exists, did_change = self._set_record_first_value_quiet(state_rec, value)
        if exists:
            self._update_mastery_mod_visible_row_after_write(target_unit, None, value)
            if did_change:
                self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
                self.statusBar().showMessage(f"Updated selected mastery value to {value:,} / 0x{value:08X}. Save As to test in game.", 4500)
            else:
                self.statusBar().showMessage(f"Selected mastery value is already {value:,} / 0x{value:08X}.", 3500)
        else:
            self.statusBar().showMessage("Selected row does not have an editable value/1607 record. Try another row.", 5500)

    def set_mastery_current_group_value(self, value: int) -> None:
        """Set all value/1607 rows for the currently selected character/group."""
        if not self.save:
            return
        char_unit = self._mastery_mod_current_character_unit()
        value = int(value)
        if int(char_unit) < 0:
            # The all-rows view is intentionally concrete: only write rows that
            # are currently visible/filtered, not every unknown section in the save.
            changed = checked = 0
            for meta in getattr(self, "mastery_mod_rows_meta", []) or []:
                rec = meta.get("state_rec")
                if rec is None:
                    continue
                checked += 1
                _, did_change = self._set_record_first_value_quiet(rec, value)
                changed += 1 if did_change else 0
            label = "visible rows"
            stats = {"changed": changed, "checked": checked}
        else:
            stats = self._apply_mastery_value_preset_to_unit(char_unit, value)
            label = self.mastery_mod_character_combo.currentText() if hasattr(self, "mastery_mod_character_combo") else f"unit {char_unit}"
        self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
        self.refresh_mastery_mod_rows()
        self.statusBar().showMessage(
            f"Set mastery values to {value:,} / 0x{value:08X} for {label}: {stats['changed']} changed, {stats['checked']} checked. Save As to test in game.",
            7500,
        )


    def apply_selected_mastery_mod_reference_effect(self) -> None:
        self.use_selected_mastery_mod_reference_effect(apply=True)

    def _mastery_mod_build_rows_for_character(self, char_unit: int) -> tuple[List[List[Any]], List[Dict[str, Any]]]:
        """Build visible 1606/1607 rows for Mastery.

        The editor now models this the same way the working Save Wizard notes do:
        1606 / FF460600 is the mastery/effect ID section and 1607 / FF470600 is
        the separate level/amount section.  A 1606 row's value row is found by
        using the same relative offset against the FF470600 anchor.  Visible
        slot/socket numbers are only labels; they are not used to pair values.
        """
        grouped = self._mastery_mod_grouped_fast()
        rows: List[List[Any]] = []
        metas: List[Dict[str, Any]] = []
        all_groups = int(char_unit) < 0

        def rec_key(rec: Optional[UnitRecord]) -> Optional[tuple]:
            if rec is None:
                return None
            return (rec.kind, rec.index, rec.id_type, rec.unit_id, rec.value_data_offset)

        # Last-resort pairing when an exact FF460600/FF470600 anchor cannot be
        # resolved: the two sections usually preserve the same row order.  This
        # keeps value edits functional on saves whose marker scan is ambiguous.
        ordinal_value_by_effect_key: Dict[tuple, UnitRecord] = {}
        try:
            effect_recs = []
            value_recs = []
            for fields in grouped.values():
                if fields.get(1606) is not None:
                    effect_recs.append(fields.get(1606))
                if fields.get(1607) is not None:
                    value_recs.append(fields.get(1607))
            effect_recs = sorted(effect_recs, key=lambda r: int(self._mastery_mod_record_abs(r) or 0x7FFFFFFF))
            value_recs = sorted(value_recs, key=lambda r: int(self._mastery_mod_record_abs(r) or 0x7FFFFFFF))
            for erec, vrec in zip(effect_recs, value_recs):
                k = rec_key(erec)
                if k is not None:
                    ordinal_value_by_effect_key[k] = vrec
        except Exception:
            ordinal_value_by_effect_key = {}

        def rec_abs_for_uid(uid: int) -> int:
            fields = grouped.get(uid, {})
            rec = fields.get(1606) or fields.get(1607)
            return int(self._mastery_mod_record_abs(rec) or 0x7FFFFFFF)

        if all_groups:
            unit_ids = sorted(
                (int(uid) for uid, fields in grouped.items() if any(fid in fields for fid in (1606, 1607))),
                key=lambda uid: (rec_abs_for_uid(int(uid)), int(uid)),
            )
        else:
            cu = int(char_unit)
            group_index = cu - 10000
            over_units: List[int] = []
            if 0 <= group_index < 40:
                for lane in range(4):
                    uid = self._mastery_overmastery_unit_id(group_index, lane)
                    fields = grouped.get(uid, {})
                    if fields.get(1606) is not None or fields.get(1607) is not None:
                        over_units.append(int(uid))
            # Use 1606 rows as the main visible normal mastery rows.  1607 rows
            # are attached by matching Save Wizard relative offset below.  The
            # four overmastery lanes are 8-digit units and must be included
            # explicitly; they are not char_unit * 10000 rows.
            normal_units = sorted(
                (
                    int(uid) for uid, fields in grouped.items()
                    if int(uid) // 10000 == cu and int(uid) >= 100000000 and fields.get(1606) is not None
                ),
                key=lambda uid: (rec_abs_for_uid(int(uid)), int(uid)),
            )
            # If a save/group somehow only exposes 1607 normal rows, show them
            # too so value editing is still possible instead of presenting an
            # empty page.
            if not normal_units:
                normal_units = sorted(
                    (
                        int(uid) for uid, fields in grouped.items()
                        if int(uid) // 10000 == cu and int(uid) >= 100000000 and fields.get(1607) is not None
                    ),
                    key=lambda uid: (rec_abs_for_uid(int(uid)), int(uid)),
                )
            unit_ids = over_units + normal_units

        used_value_keys = set()
        for ordinal, unit_id in enumerate(unit_ids, start=1):
            recs = grouped.get(unit_id, {})
            mastery_rec = recs.get(1606)
            fallback_state_rec = recs.get(1607)

            rel_int = self._mastery_mod_sw_relative_int_for_record(mastery_rec or fallback_state_rec)
            state_rec = self._mastery_mod_record_by_sw_relative(rel_int, id_type=1607) if rel_int is not None else None
            pair_source = "offset" if state_rec is not None else "missing"
            if state_rec is None and mastery_rec is not None:
                state_rec = ordinal_value_by_effect_key.get(rec_key(mastery_rec))
                if state_rec is not None:
                    pair_source = "order"
            if state_rec is None:
                state_rec = fallback_state_rec
                pair_source = "unit" if state_rec is not None else "missing"
            if state_rec is not None:
                used_value_keys.add((state_rec.kind, state_rec.index, state_rec.id_type, state_rec.unit_id, state_rec.value_data_offset))

            over_unit = self._mastery_overmastery_slot_for_unit(unit_id)
            if over_unit is not None:
                over_group, over_idx = over_unit
                derived_char = 10000 + int(over_group)
                base_char = derived_char if all_groups else int(char_unit)
                legacy_slot = int(over_idx)
                legacy_socket = 0
            else:
                derived_char = int(unit_id) // 10000 if int(unit_id) >= 100000000 else int(char_unit)
                base_char = derived_char if all_groups else int(char_unit)
                rem = int(unit_id) - int(base_char) * 10000 if base_char >= 0 else int(unit_id)
                legacy_slot = rem // 10 if rem >= 0 else rem
                legacy_socket = rem % 10 if rem >= 0 else None

            mastery = self._record_first_value(mastery_rec, 0)
            state = self._record_first_value(state_rec, 0)
            slotinfo = 0
            try:
                board_unit = int(base_char) * 1000 + int(legacy_slot) if base_char >= 0 and legacy_slot >= 0 and over_unit is None else 0
                board_recs = grouped.get(board_unit, {}) if board_unit else {}
                slotinfo = self._record_first_value(board_recs.get(1601), 0)
            except Exception:
                board_unit = 0
                board_recs = {}

            sw_rel = f"0x{rel_int:06X}" if rel_int is not None else self._mastery_mod_sw_relative_for_record(mastery_rec or state_rec)
            if over_unit is not None:
                over_group, over_idx = over_unit
                section_display = f"Overmastery Stat {over_idx + 1}"
                row_type = f"Overmastery group {over_group + 1}"
            else:
                section_display = "Mastery / Collection"
                row_type = self._mastery_effect_category(mastery, slotinfo)
            if all_groups:
                row_type = f"Group {derived_char} · {row_type}"
            if pair_source == "missing":
                value_label = "No paired value row"
            else:
                value_label = self._mastery_mod_state_label(state, mastery)
                if pair_source == "offset":
                    value_label += " · paired"
                elif pair_source == "order":
                    value_label += " · paired by order"

            name = self._mastery_effect_name(mastery)
            if (int(mastery or 0) & 0xFFFFFFFF) == EMPTY_HASH:
                name = "None / blank slot (B0E07A88)"

            rows.append([
                ordinal,
                section_display,
                name,
                row_type,
                value_label,
                self._hash_hex_or_dash(mastery),
                unit_id,
                sw_rel if sw_rel is not None else "—",
            ])
            metas.append({
                "char_unit": int(derived_char), "unit_id": int(unit_id), "board_unit": int(board_unit or 0),
                "slot": int(ordinal - 1), "socket": int(legacy_socket) if legacy_socket is not None else None,
                "row_number": int(ordinal), "section": section_display, "row_type": row_type,
                "mastery": mastery, "state": state, "slotinfo": slotinfo,
                "sw_rel": sw_rel, "sw_rel_int": rel_int, "pair_source": pair_source, "all_groups": all_groups,
                "mastery_rec": mastery_rec, "state_rec": state_rec, "state_unit_id": int(getattr(state_rec, "unit_id", 0) or 0),
                "slotinfo_rec": board_recs.get(1601) if 'board_recs' in locals() else None,
            })

        # In the all-groups/debug view, append value-only rows that were not paired
        # to a visible 1606 effect row so they can still be inspected/edited.
        if all_groups:
            extra_value_rows = []
            for uid, fields in grouped.items():
                rec = fields.get(1607)
                if rec is None:
                    continue
                key = (rec.kind, rec.index, rec.id_type, rec.unit_id, rec.value_data_offset)
                if key in used_value_keys:
                    continue
                extra_value_rows.append((int(self._mastery_mod_record_abs(rec) or 0x7FFFFFFF), int(uid), rec))
            for _, uid, rec in sorted(extra_value_rows):
                ordinal = len(rows) + 1
                state = self._record_first_value(rec, 0)
                rel_int = self._mastery_mod_sw_relative_int_for_record(rec)
                sw_rel = f"0x{rel_int:06X}" if rel_int is not None else None
                rows.append([ordinal, "Value only", "—", f"Group {uid // 10000} · 1607 only", self._mastery_mod_state_label(state, 1), "—", uid, sw_rel or "—"])
                metas.append({
                    "char_unit": int(uid) // 10000, "unit_id": int(uid), "board_unit": 0,
                    "slot": int(ordinal - 1), "socket": None, "row_number": int(ordinal),
                    "section": "Value only", "row_type": "1607 only",
                    "mastery": 0, "state": state, "slotinfo": 0, "sw_rel": sw_rel, "sw_rel_int": rel_int,
                    "pair_source": "value-only", "all_groups": all_groups,
                    "mastery_rec": None, "state_rec": rec, "state_unit_id": int(uid), "slotinfo_rec": None,
                })
        return rows, metas

    def refresh_mastery_mod_rows(self) -> None:
        if not hasattr(self, "mastery_mod_model"):
            return
        if bool(getattr(self, "_io_guard_depth", 0)):
            self._mark_stale_pages(["Mastery"])
            return
        if not self.save:
            self.mastery_mod_model.set_rows([])
            self.mastery_mod_rows_meta = []
            return
        char_unit = self._mastery_mod_current_character_unit()
        rows, metas = self._mastery_mod_build_rows_for_character(char_unit)

        # Keep the UI responsive but do not silently lose the raw map: filtering
        # by hash/unit/offset lets the user jump to rows past the display cap.
        q = ""
        try:
            q = str(getattr(self, "mastery_mod_target_filter_edit", None).text()).strip().lower()
        except Exception:
            q = ""
        if q:
            filtered_rows = []
            filtered_metas = []
            for row, meta in zip(rows, metas):
                extra = [meta.get("sw_rel", ""), self._hash_hex_or_dash(meta.get("mastery", 0)), str(meta.get("unit_id", ""))]
                if self._matches_editor_filter(list(row) + extra, q):
                    filtered_rows.append(row)
                    filtered_metas.append(meta)
            rows, metas = filtered_rows, filtered_metas

        display_cap = 6000 if int(char_unit) < 0 else 2500
        self.mastery_mod_rows_meta = metas[:display_cap]
        self.mastery_mod_model.set_rows(rows[:display_cap])
        if hasattr(self, "mastery_mod_selected_detail_label") and not rows:
            self.mastery_mod_selected_detail_label.setText("No readable mastery rows were found for this character/group. Try All Rows or another character, then send the debug log if it is still empty.")
        preferred_unit = None
        try:
            preferred_unit = int(getattr(self, "mastery_mod_target_combo", None).currentData())
        except Exception:
            preferred_unit = None
        self._refresh_mastery_mod_target_combo(preferred_unit)
        if hasattr(self, "mastery_mod_table"):
            self._configure_mastery_rows_table()
        if hasattr(self, "mastery_mod_status_label"):
            name = getattr(self, 'mastery_mod_character_combo', None).currentText() if hasattr(self, 'mastery_mod_character_combo') else 'selected group'
            blank = sum(1 for m in metas if (int(m.get("mastery") or 0) & 0xFFFFFFFF) == EMPTY_HASH)
            with_1607 = sum(1 for m in metas if m.get("state_rec") is not None)
            paired = sum(1 for m in metas if m.get("pair_source") in {"offset", "order"})
            cap_note = f" · showing first {len(self.mastery_mod_rows_meta)}/{len(metas)}" if len(metas) > len(self.mastery_mod_rows_meta) else ""
            self.mastery_mod_status_label.setText(
                f"{name}: {len(metas)} editable row(s), {with_1607} with values ({paired} paired by offset), {blank} empty slot(s){cap_note}."
            )

    def _selected_mastery_mod_meta(self) -> Optional[Dict[str, Any]]:
        table = getattr(self, "mastery_mod_table", None)
        if table is None:
            return None
        idx = table.currentIndex()
        if idx.isValid() and idx.row() < len(getattr(self, "mastery_mod_rows_meta", [])):
            return self.mastery_mod_rows_meta[idx.row()]
        return None

    def _mastery_mod_meta_by_unit(self, unit_id: int) -> Optional[Dict[str, Any]]:
        try:
            target = int(unit_id)
        except Exception:
            return None
        for meta in getattr(self, "mastery_mod_rows_meta", []) or []:
            try:
                if int(meta.get("unit_id") or 0) == target:
                    return meta
            except Exception:
                continue
        return None

    def _refresh_mastery_mod_target_combo(self, preferred_unit: Optional[int] = None) -> None:
        combo = getattr(self, "mastery_mod_target_combo", None)
        if combo is None:
            return
        current = combo.currentData()
        target = int(preferred_unit) if preferred_unit is not None else (int(current) if current is not None else None)
        combo.blockSignals(True)
        combo.clear()
        for meta in getattr(self, "mastery_mod_rows_meta", []) or []:
            try:
                unit_id = int(meta.get("unit_id") or 0)
                row_no = int(meta.get("row_number", meta.get("slot", 0) + 1))
                section = str(meta.get("section") or "Mastery row")
                mastery = int(meta.get("mastery", 0) or 0) & 0xFFFFFFFF
                state = meta.get("state", 0)
                name = self._mastery_effect_name(mastery)
                label = f"Row {row_no} · {section} — {name} — Value {self._mastery_mod_state_label(state, mastery)}"
                combo.addItem(label, unit_id)
            except Exception:
                continue
        if combo.count():
            chosen = -1
            if target is not None:
                for i in range(combo.count()):
                    try:
                        if int(combo.itemData(i)) == int(target):
                            chosen = i
                            break
                    except Exception:
                        pass
            combo.setCurrentIndex(chosen if chosen >= 0 else 0)
        combo.blockSignals(False)

    def _mastery_mod_selected_detail_text(self, meta: Dict[str, Any]) -> str:
        try:
            row_no = int(meta.get("row_number", meta.get("slot", 0) + 1))
        except Exception:
            row_no = 0
        try:
            mastery = int(self._record_first_value(meta.get("mastery_rec"), meta.get("mastery", 0)) or 0) & 0xFFFFFFFF
        except Exception:
            mastery = int(meta.get("mastery", 0) or 0) & 0xFFFFFFFF
        try:
            state = int(self._record_first_value(meta.get("state_rec"), meta.get("state", 0)) or 0)
        except Exception:
            state = int(meta.get("state", 0) or 0)
        effect_name = self._mastery_effect_name(mastery)
        value_label = self._mastery_mod_state_label(state, mastery)
        section = str(meta.get("section") or "Mastery row")
        pair = str(meta.get("pair_source") or "unknown")
        pair_note = "paired value row found" if pair in {"offset", "order", "unit"} else "no paired value row found"
        return (
            f"Selected Row {row_no} · {section}\n"
            f"Effect: {effect_name}\n"
            f"Value / Amount: {value_label}\n"
            f"Status: {pair_note}"
        )

    def update_mastery_mod_target_from_combo(self) -> None:
        if bool(getattr(self, "_mastery_mod_loading", False)):
            return
        combo = getattr(self, "mastery_mod_target_combo", None)
        if combo is None or combo.currentData() is None:
            return
        meta = self._mastery_mod_meta_by_unit(int(combo.currentData()))
        if not meta:
            return
        self._mastery_mod_loading = True
        try:
            if hasattr(self, "mastery_mod_slot_spin"):
                self.mastery_mod_slot_spin.setValue(int(meta.get("slot", 0)) + 1)
            if hasattr(self, "mastery_mod_socket_spin"):
                self.mastery_mod_socket_spin.setValue(int(meta.get("socket", 0)) + 1)
            if hasattr(self, "mastery_mod_state_spin"):
                state = int(self._record_first_value(meta.get("state_rec"), meta.get("state", 0)) or 0)
                # For modding, inactive/current-1 rows usually need a real 1607
                # value to apply in-game. Keep larger existing values, but offer
                # 1023 by default for blank/inactive rows.
                suggested_state = 1023 if state <= 1 else state
                self.mastery_mod_state_spin.setValue(max(0, min(self.mastery_mod_state_spin.maximum(), suggested_state)))
            value = int(self._record_first_value(meta.get("mastery_rec"), meta.get("mastery", 0)) or 0) & 0xFFFFFFFF
            combo_effect = getattr(self, "mastery_mod_effect_combo", None)
            if combo_effect is not None:
                found = False
                for i in range(combo_effect.count()):
                    data = combo_effect.itemData(i)
                    if data is not None and int(data) == value:
                        combo_effect.setCurrentIndex(i)
                        found = True
                        break
                if not found:
                    combo_effect.setCurrentIndex(0)
            detail_text = self._mastery_mod_selected_detail_text(meta)
            if hasattr(self, "mastery_mod_selected_detail_label"):
                self.mastery_mod_selected_detail_label.setText(detail_text)
            if hasattr(self, "mastery_mod_edit_detail_label"):
                self.mastery_mod_edit_detail_label.setText(detail_text)
            if hasattr(self, "mastery_mod_status_label"):
                self.mastery_mod_status_label.setText(f"Targeting Row #{int(meta.get('row_number', meta.get('slot', 0) + 1))} · {meta.get('section', 'Mastery row')}.")
        finally:
            self._mastery_mod_loading = False

    def update_mastery_mod_from_selection(self) -> None:
        meta = self._selected_mastery_mod_meta()
        if not meta:
            return
        self._mastery_mod_loading = True
        try:
            if hasattr(self, "mastery_mod_slot_spin"):
                self.mastery_mod_slot_spin.setValue(int(meta.get("slot", 0)) + 1)
            if hasattr(self, "mastery_mod_socket_spin"):
                self.mastery_mod_socket_spin.setValue(int(meta.get("socket", 0)) + 1)
            if hasattr(self, "mastery_mod_state_spin"):
                state = int(self._record_first_value(meta.get("state_rec"), meta.get("state", 0)) or 0)
                # For modding, inactive/current-1 rows usually need a real 1607
                # value to apply in-game. Keep larger existing values, but offer
                # 1023 by default for blank/inactive rows.
                suggested_state = 1023 if state <= 1 else state
                self.mastery_mod_state_spin.setValue(max(0, min(self.mastery_mod_state_spin.maximum(), suggested_state)))
            value = int(self._record_first_value(meta.get("mastery_rec"), meta.get("mastery", 0)) or 0) & 0xFFFFFFFF
            combo = getattr(self, "mastery_mod_effect_combo", None)
            if combo is not None:
                found = False
                for i in range(combo.count()):
                    data = combo.itemData(i)
                    if data is not None and int(data) == value:
                        combo.setCurrentIndex(i); found = True; break
                if not found:
                    combo.setCurrentIndex(0)
            if hasattr(self, "mastery_mod_target_combo"):
                target_unit = int(meta.get("unit_id") or 0)
                for i in range(self.mastery_mod_target_combo.count()):
                    try:
                        if int(self.mastery_mod_target_combo.itemData(i)) == target_unit:
                            self.mastery_mod_target_combo.setCurrentIndex(i)
                            break
                    except Exception:
                        pass
            detail_text = self._mastery_mod_selected_detail_text(meta)
            if hasattr(self, "mastery_mod_selected_detail_label"):
                self.mastery_mod_selected_detail_label.setText(detail_text)
            if hasattr(self, "mastery_mod_edit_detail_label"):
                self.mastery_mod_edit_detail_label.setText(detail_text)
            if hasattr(self, "mastery_mod_status_label"):
                self.mastery_mod_status_label.setText(f"Selected Row #{int(meta.get('row_number', meta.get('slot', 0) + 1))} · {meta.get('section', 'Mastery row')} · Value: {self._mastery_mod_state_label(meta.get('state', 0), value)}.")
        finally:
            self._mastery_mod_loading = False

    def _maybe_live_apply_mastery_mod(self) -> None:
        if bool(getattr(self, "_mastery_mod_loading", False)):
            return
        check = getattr(self, "mastery_mod_live_check", None)
        if check is not None and check.isChecked():
            self.apply_mastery_mod_selected_row_edit(silent=True)

    def _mastery_mod_save_cache_key(self) -> tuple:
        if not self.save:
            return (None, 0, 0, 0)
        try:
            return (id(self.save), len(getattr(self.save, "records", []) or []), int(self.save.container.payload_offset), int(self.save.container.payload_size))
        except Exception:
            return (id(self.save), len(getattr(self.save, "records", []) or []), 0, 0)

    def _mastery_mod_grouped_fast(self) -> Dict[int, Dict[int, UnitRecord]]:
        key = self._mastery_mod_save_cache_key()
        if self._mastery_mod_cache_key != key or self._mastery_mod_grouped_cache is None:
            if not self.save:
                self._mastery_mod_grouped_cache = {}
            else:
                self._mastery_mod_grouped_cache = self.save.group_by_unit([1601, 1606, 1607])
            self._mastery_mod_cache_key = key
        return self._mastery_mod_grouped_cache or {}

    def _mastery_mod_1606_abs_index(self) -> Dict[int, UnitRecord]:
        key = self._mastery_mod_save_cache_key()
        if self._mastery_mod_abs1606_cache_key != key:
            index: Dict[int, UnitRecord] = {}
            if self.save:
                try:
                    base = int(self.save.container.payload_offset)
                    for rec in self.save.records:
                        if rec.id_type == 1606 and rec.kind in {"uint", "int"}:
                            index[base + int(rec.value_data_offset)] = rec
                except Exception:
                    index = {}
            self._mastery_mod_abs1606_cache = index
            self._mastery_mod_abs1606_cache_key = key
        return self._mastery_mod_abs1606_cache or {}

    def _mastery_mod_1607_abs_index(self) -> Dict[int, UnitRecord]:
        key = self._mastery_mod_save_cache_key()
        if getattr(self, "_mastery_mod_abs1607_cache_key", None) != key:
            index: Dict[int, UnitRecord] = {}
            if self.save:
                try:
                    base = int(self.save.container.payload_offset)
                    for rec in self.save.records:
                        if rec.id_type == 1607 and rec.kind in {"uint", "int"}:
                            index[base + int(rec.value_data_offset)] = rec
                except Exception:
                    index = {}
            self._mastery_mod_abs1607_cache = index
            self._mastery_mod_abs1607_cache_key = key
        return getattr(self, "_mastery_mod_abs1607_cache", {}) or {}

    def _on_mastery_mod_tab_changed(self, index: int) -> None:
        # The redesigned Mastery page has only two visible tabs. Keep the
        # expensive offset table lazy until the Advanced / Database tab is opened.
        tab = getattr(self, "mastery_mod_tabs", None)
        label = tab.tabText(index) if tab is not None and 0 <= index < tab.count() else ""
        if "Advanced" in label or "Database" in label or "SW Offset" in label:
            self.refresh_mastery_value_rows()
            self.refresh_mastery_mod_code_rows()

    def _set_record_first_value_quiet(self, rec: Optional[UnitRecord], value: Any) -> tuple[bool, bool]:
        """Set only value[0] without opening message boxes.

        Mastery edits thousands of single-scalar 1606/1607 rows.  Do not
        read or rewrite entire vectors here; that is what made Save/Save As hang
        after the Mastery tab was added.
        """
        if not self.save or rec is None or rec.value_count < 1:
            return False, False
        try:
            old_value = self.save.get_first_value(rec, None) if hasattr(self.save, "get_first_value") else self.save.get_values(rec, 1)[0]
        except Exception:
            return False, False
        new_value = int(value)
        if old_value == new_value:
            return True, False
        if int(new_value) == 0xFFFFFFFF and int(old_value) == -1:
            return True, False
        try:
            if hasattr(self.save, "set_first_value"):
                self.save.set_first_value(rec, new_value)
            else:
                values = self.save.get_values(rec)
                if not values:
                    return True, False
                values[0] = new_value
                self.save.set_values(rec, values)
        except Exception:
            return True, False
        self.dirty = True
        return True, True

    def _mastery_mod_abs_index_for_field(self, id_type: int) -> Dict[int, UnitRecord]:
        try:
            fid = int(id_type)
        except Exception:
            fid = 1606
        if fid == 1607:
            return self._mastery_mod_1607_abs_index()
        return self._mastery_mod_1606_abs_index()

    def _parse_mastery_sw_address_operand(self, value: Any) -> Optional[int]:
        """Parse Save Wizard N operands into a relative offset.

        Forms handled:
        - 280B8078 / 4E0B8078 -> 0B8078
        - decimal 671842424, which is 0x280B8078 -> 0B8078
        - raw 0B8078 / 0x0B8078 -> 0B8078
        """
        text = str(value or "").strip().replace(",", "")
        if not text:
            return None
        if re.fullmatch(r"\d{7,10}", text):
            try:
                raw = int(text, 10)
            except Exception:
                raw = -1
            if raw >= 0:
                op = (raw >> 24) & 0xFF
                if op in (0x28, 0x4E):
                    return raw & 0x00FFFFFF
                if raw <= 0x00FFFFFF:
                    return raw
        for m in re.finditer(r"(?i)(?:0x)?([0-9A-F]{8})\b", text):
            raw = int(m.group(1), 16)
            op = (raw >> 24) & 0xFF
            if op in (0x28, 0x4E):
                return raw & 0x00FFFFFF
        m = re.search(r"(?i)(?:0x)?([0-9A-F]{1,7})\b", text)
        if m:
            return int(m.group(1), 16)
        return None

    def _parse_mastery_repeat_stride(self, row: Dict[str, Any]) -> tuple[int, int]:
        """Parse Save Wizard 4E repeat metadata: 02580018 = count 0x258, stride 0x18."""
        if not row:
            return 1, 0
        count_text = self._mastery_offset_row_pick(row, "Count", "Rows", "Slots", "Repeat", "Quantity")
        stride_text = self._mastery_offset_row_pick(row, "Stride", "Step", "Increment")
        count: Optional[int] = None
        stride: Optional[int] = None
        try:
            if count_text:
                count = int(str(count_text).strip().replace(",", ""), 0)
        except Exception:
            count = None
        try:
            if stride_text:
                stride = int(str(stride_text).strip().replace(",", ""), 0)
        except Exception:
            stride = None
        joined = " | ".join(str(v or "") for v in row.values())
        # Prefer a word/context near the 4E follow-up line, but accept common raw
        # count/stride tokens when they appear in a sheet row.
        m = re.search(r"(?i)\b([0-9A-F]{4})([0-9A-F]{4})\b", joined)
        if m and (count is None or stride is None):
            c = int(m.group(1), 16)
            st = int(m.group(2), 16)
            if 0 < c <= 50000 and 0 < st <= 0x1000:
                count = c if count is None else count
                stride = st if stride is None else stride
        if count is None or count <= 0:
            count = 1
        if stride is None or stride < 0:
            stride = 0
        return min(int(count), 50000), int(stride)

    def _mastery_mod_anchor_base_abs(self, id_type: int = 1606) -> Optional[int]:
        """Return the Save Wizard relative-offset base for 1606 or 1607 rows.

        1606 rows use FF460600 and 1607 rows use FF470600. The same N relative
        offset must be applied against each section's own anchor.
        """
        if not self.save:
            return None
        try:
            fid = int(id_type)
        except Exception:
            fid = 1606
        if fid not in (1606, 1607):
            fid = 1606
        key = self._mastery_mod_save_cache_key()
        if (getattr(self, "_mastery_mod_anchor_cache_key_by_field", {}) or {}).get(fid) == key:
            return (getattr(self, "_mastery_mod_anchor_by_field", {}) or {}).get(fid)

        result: Optional[int] = None
        source = ""
        score = 0
        try:
            entries = self._mastery_method_pattern_entries()
            index = self._mastery_mod_abs_index_for_field(fid)
            start = int(self.save.container.payload_offset)
            end = start + int(self.save.container.payload_size)
            data = self.save._file_bytes

            def score_base(base: int) -> int:
                if base < start or base >= end:
                    return -1
                hits = 0
                for entry in entries:
                    try:
                        rel = int(entry.get("rel", -1))
                    except Exception:
                        continue
                    if rel >= 0 and int(base) + rel in index:
                        hits += 1
                return hits

            if fid == 1607:
                marker_specs = [
                    (bytes.fromhex("FF470600"), 1, "marker FF470600 + 1 / 92-adjusted"),
                    (bytes.fromhex("FF470600"), 0, "marker FF470600 raw"),
                    (bytes.fromhex("47060000"), 0, "marker 47060000"),
                    (bytes.fromhex("470600"), 0, "marker 470600"),
                ]
            else:
                marker_specs = [
                    (bytes.fromhex("FF460600"), 1, "marker FF460600 + 1 / 92-adjusted"),
                    (bytes.fromhex("FF460600"), 0, "marker FF460600 raw"),
                    (bytes.fromhex("46060000"), 0, "marker 46060000"),
                    (bytes.fromhex("460600"), 0, "marker 460600"),
                ]

            candidates: List[tuple[int, str]] = []
            for marker, delta, label in marker_specs:
                pos = data.find(marker, start, end)
                while pos >= 0:
                    candidates.append((pos + delta, label))
                    if len(candidates) > 96:
                        break
                    pos = data.find(marker, pos + 1, end)
                if len(candidates) > 96:
                    break

            counts: Dict[int, int] = {}
            rels = []
            for entry in entries:
                try:
                    rel = int(entry.get("rel", -1))
                except Exception:
                    rel = -1
                if rel >= 0:
                    rels.append(rel)
            rels = list(dict.fromkeys(rels))
            for abs_pos in list(index.keys()):
                for rel in rels:
                    base = int(abs_pos) - int(rel)
                    if start <= base < end:
                        counts[base] = counts.get(base, 0) + 1
            if counts:
                inferred_base, inferred_score = max(counts.items(), key=lambda kv: kv[1])
                candidates.append((inferred_base, f"inferred from parsed {fid} rows ({inferred_score} hit(s))"))

            best: Optional[tuple[int, str, int]] = None
            seen = set()
            for base, label in candidates:
                if base in seen:
                    continue
                seen.add(base)
                s = score_base(base)
                if s < 0:
                    continue
                if best is None or s > best[2]:
                    best = (int(base), label, int(s))
            if best is not None:
                result, source, score = best
        except Exception:
            result = None
            source = "anchor scan failed"
            score = 0

        if not hasattr(self, "_mastery_mod_anchor_by_field"):
            self._mastery_mod_anchor_by_field = {}
            self._mastery_mod_anchor_source_by_field = {}
            self._mastery_mod_anchor_score_by_field = {}
            self._mastery_mod_anchor_cache_key_by_field = {}
        self._mastery_mod_anchor_by_field[fid] = result
        self._mastery_mod_anchor_source_by_field[fid] = source
        self._mastery_mod_anchor_score_by_field[fid] = score
        self._mastery_mod_anchor_cache_key_by_field[fid] = key
        if fid == 1606:
            self._mastery_mod_anchor_cache_key = key
            self._mastery_mod_anchor_cache = result
            self._mastery_mod_anchor_source = source
            self._mastery_mod_anchor_score = score
        return result

    def _mastery_mod_anchor_status(self) -> str:
        base6 = self._mastery_mod_anchor_base_abs(1606)
        base7 = self._mastery_mod_anchor_base_abs(1607)
        if base6 is None:
            return "No FF460600 SW anchor resolved. Current save rows can still be edited by unit, but exact offset rows will stay unresolved."
        srcs = getattr(self, "_mastery_mod_anchor_source_by_field", {}) or {}
        scores = getattr(self, "_mastery_mod_anchor_score_by_field", {}) or {}
        text = f"1606 anchor 0x{base6:X} ({srcs.get(1606, 'resolved')}; {int(scores.get(1606, 0) or 0)} ID row(s))"
        if base7 is None:
            return text + "; no FF470600 value anchor resolved."
        return text + f"; 1607 anchor 0x{base7:X} ({srcs.get(1607, 'resolved')}; {int(scores.get(1607, 0) or 0)} value row(s))."

    def _mastery_mod_record_abs(self, rec: Optional[UnitRecord]) -> Optional[int]:
        if not self.save or rec is None:
            return None
        try:
            return int(self.save.container.payload_offset) + int(rec.value_data_offset)
        except Exception:
            return None

    def _mastery_mod_sw_relative_for_record(self, rec: Optional[UnitRecord]) -> Optional[str]:
        base = self._mastery_mod_anchor_base_abs(getattr(rec, "id_type", 1606))
        abs_pos = self._mastery_mod_record_abs(rec)
        if base is None or abs_pos is None:
            return None
        rel = int(abs_pos) - int(base)
        if rel < 0:
            return None
        return f"0x{rel:06X}"

    def _mastery_mod_sw_relative_int_for_record(self, rec: Optional[UnitRecord]) -> Optional[int]:
        """Return the numeric Save Wizard relative offset for a 1606/1607 record."""
        if rec is None:
            return None
        base = self._mastery_mod_anchor_base_abs(getattr(rec, "id_type", 1606))
        abs_pos = self._mastery_mod_record_abs(rec)
        if base is None or abs_pos is None:
            return None
        rel = int(abs_pos) - int(base)
        return rel if rel >= 0 else None

    def _mastery_mod_record_by_abs(self, abs_pos: int, id_type: int = 1606) -> Optional[UnitRecord]:
        if not self.save:
            return None
        try:
            if int(id_type) == 1607:
                return self._mastery_mod_1607_abs_index().get(int(abs_pos))
            return self._mastery_mod_1606_abs_index().get(int(abs_pos))
        except Exception:
            return None

    def _mastery_mod_record_by_sw_relative(self, rel_offset: int, id_type: int = 1606) -> Optional[UnitRecord]:
        base = self._mastery_mod_anchor_base_abs(id_type)
        if base is None:
            return None
        return self._mastery_mod_record_by_abs(int(base) + int(rel_offset), id_type=id_type)

    def _mastery_overmastery_unit_id(self, group_index: int, slot_index: int) -> int:
        """Return the concrete save unit for one four-stat overmastery lane.

        The before/after saves confirm that the four selectable overmastery
        stats are not generic slot/socket rows.  They are concrete 1606/1607
        units:

            10000000 + character_index * 1000 + lane_index

        Example: Gran/group 0 lanes are 10000000, 10000001, 10000002,
        10000003.  Vaseraga/group 39 lanes are 10039000..10039003.
        """
        return 10000000 + int(group_index) * 1000 + int(slot_index)

    def _mastery_overmastery_slot_for_unit(self, unit_id: int) -> Optional[tuple[int, int]]:
        """Return (character_index, lane_index) for a concrete overmastery unit."""
        try:
            uid = int(unit_id)
        except Exception:
            return None
        if not (10000000 <= uid <= 10039003):
            return None
        rem = uid - 10000000
        group = rem // 1000
        lane = rem % 1000
        if 0 <= group < 40 and 0 <= lane < 4:
            return int(group), int(lane)
        return None

    def _mastery_find_first_any(self, id_type: int, unit_id: int) -> Optional[UnitRecord]:
        """Find the first scalar row for an id/unit regardless of signedness."""
        if not self.save:
            return None
        preferred = ("uint", "int") if int(id_type) == 1606 else ("int", "uint")
        for kind in preferred:
            rec = self.save.find_first(kind, int(id_type), int(unit_id))
            if rec is not None:
                return rec
        try:
            rows = [rec for rec in self.save.records if int(rec.id_type) == int(id_type) and int(rec.unit_id) == int(unit_id)]
            rows.sort(key=lambda r: int(getattr(r, "value_data_offset", 0)))
            return rows[0] if rows else None
        except Exception:
            return None

    def _mastery_overmastery_base_rel_candidates(self) -> List[int]:
        """Relative bases for the four-stat overmastery block.

        Community code pattern:
            92000000 000B8079
            93000000 00000EE8
            4E000000 XXXXXXXX
            00280060 00000000

        The effective relative start is 0x0B8079 + 0x0EE8.  Older notes/sheets
        sometimes show the paired direct address as 280B8078 because of the 92
        pointer adjustment, so try the exact value and nearby off-by-one bases.
        """
        # The concrete before/after saves show the first four-stat lane at
        # first FF460600 marker + 1 + 0x0B8078.  Older notes included a 930
        # adjustment, but applying 0x0B8079 + 0x0EE8 lands in the wrong section.
        base = 0x0B8078
        return [base, base - 0x18, base + 0x18, base + 1, base - 1]

    def _mastery_overmastery_rel_for(self, group_index: int, slot_index: int, candidate_index: int = 0) -> int:
        bases = self._mastery_overmastery_base_rel_candidates()
        base = bases[max(0, min(int(candidate_index), len(bases) - 1))]
        return int(base) - int(group_index) * 0x60 - int(slot_index) * 0x18

    def _mastery_overmastery_record(self, group_index: int, slot_index: int, id_type: int) -> Optional[UnitRecord]:
        # Prefer the real save unit mapping confirmed by the before/after sample.
        unit_id = self._mastery_overmastery_unit_id(group_index, slot_index)
        rec = self._mastery_find_first_any(id_type, unit_id)
        if rec is not None:
            return rec
        # Fallback for any future save variant where the unit id formula changes
        # but the Save Wizard relative layout is still present.
        for ci, _base in enumerate(self._mastery_overmastery_base_rel_candidates()):
            rec = self._mastery_mod_record_by_sw_relative(self._mastery_overmastery_rel_for(group_index, slot_index, ci), id_type=id_type)
            if rec is not None:
                return rec
        return None

    def _mastery_overmastery_slot_for_rel(self, rel_offset: Optional[int]) -> Optional[tuple[int, int]]:
        if rel_offset is None:
            return None
        try:
            rel = int(rel_offset)
        except Exception:
            return None
        for base in self._mastery_overmastery_base_rel_candidates():
            diff = int(base) - rel
            if diff < 0:
                continue
            group = diff // 0x60
            rem = diff % 0x60
            if 0 <= group < 0x28 and rem in (0x00, 0x18, 0x30, 0x48):
                return int(group), int(rem // 0x18)
        return None

    def _mastery_current_overmastery_group_index(self) -> Optional[int]:
        char_unit = self._mastery_mod_current_character_unit()
        if int(char_unit) < 0:
            return None
        # Normal PL groups are 10000 + character index. Extra groups can still
        # be edited from the Current Rows table, but the four-stat community
        # code is a fixed 40-group block keyed by this index.
        idx = int(char_unit) - 10000
        if 0 <= idx < 0x28:
            return idx
        return None

    def _parse_mastery_u32_text(self, text: Any, default: int = 0) -> int:
        raw = str(text or "").strip().replace(",", "")
        if not raw:
            raw = str(default)
        try:
            parsed = int(raw, 0)
        except Exception:
            m = re.search(r"(?i)(?:0x)?([0-9A-F]{1,8})\b", raw)
            parsed = int(m.group(1), 16) if m else int(default)
        # Keep the manual text box signed-32 safe. Use -1 when you explicitly
        # want the known FFFFFFFF / 80% sentinel; the dedicated 80% button still
        # writes the raw FFFFFFFF value directly.
        if parsed == -1:
            return 0xFFFFFFFF
        clamped = self._clamp_i32_value(parsed, minimum=0, maximum=I32_MAX, label="overmastery value")
        return 0 if clamped is None else int(clamped)

    def _schedule_overmastery_auto_apply(self) -> None:
        if getattr(self, "_mastery_mod_loading", False):
            return
        check = getattr(self, "mastery_overmastery_auto_apply_check", None)
        if check is None or not check.isChecked():
            return
        if not self.save:
            return
        try:
            effects = self._selected_overmastery_effect_values()
            if any(v is None for v in effects[:4]):
                return
        except Exception:
            return
        timer = getattr(self, "_overmastery_auto_apply_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._run_overmastery_auto_apply)
            self._overmastery_auto_apply_timer = timer
        timer.start(450)

    def _run_overmastery_auto_apply(self) -> None:
        check = getattr(self, "mastery_overmastery_auto_apply_check", None)
        if check is None or not check.isChecked() or not self.save:
            return
        self.apply_overmastery_four_stats_all(auto=True)

    def _selected_overmastery_effect_values(self) -> List[Optional[int]]:
        values: List[Optional[int]] = []
        for combo in list(getattr(self, "mastery_overmastery_combos", []) or []):
            data = combo.currentData()
            values.append(None if data is None else int(data) & 0xFFFFFFFF)
        while len(values) < 4:
            values.append(None)
        return values[:4]

    def _apply_overmastery_four_stats_to_group(self, group_index: int, effects: List[Optional[int]], value: int, write_value: bool = True) -> Dict[str, int]:
        stats = {"effect_changed": 0, "value_changed": 0, "effect_found": 0, "value_found": 0}
        for slot_index, effect_value in enumerate(effects[:4]):
            if effect_value is not None:
                rec = self._mastery_overmastery_record(group_index, slot_index, 1606)
                exists, changed = self._set_record_first_value_quiet(rec, int(effect_value) & 0xFFFFFFFF)
                stats["effect_found"] += 1 if exists else 0
                stats["effect_changed"] += 1 if changed else 0
            if write_value:
                vrec = self._mastery_overmastery_record(group_index, slot_index, 1607)
                exists, changed = self._set_record_first_value_quiet(vrec, int(value) & 0xFFFFFFFF)
                stats["value_found"] += 1 if exists else 0
                stats["value_changed"] += 1 if changed else 0
        return stats

    def apply_overmastery_four_stats_selected(self) -> None:
        if not self.save:
            return
        group_index = self._mastery_current_overmastery_group_index()
        if group_index is None:
            QMessageBox.warning(self, "Overmastery group not found", "Pick a normal character/group first. The four-stat overmastery code targets the 40 PL groups, not the raw all-row scan.")
            return
        effects = self._selected_overmastery_effect_values()
        value = self._parse_mastery_u32_text(getattr(self, "mastery_overmastery_value_edit", None).text() if hasattr(self, "mastery_overmastery_value_edit") else "1023", 1023)
        write_value = bool(getattr(self, "mastery_overmastery_write_value_check", None) is None or self.mastery_overmastery_write_value_check.isChecked())
        stats = self._apply_overmastery_four_stats_to_group(group_index, effects, value, write_value=write_value)
        self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
        self.refresh_mastery_mod_rows()
        shown = "80%" if int(value) == 0xFFFFFFFF else ("20%" if int(value) == 512 else str(int(value)))
        msg = f"Selected group applied: stats {stats['effect_changed']}, values {stats['value_changed']} · {shown}"
        if hasattr(self, "mastery_sw_lab_status"):
            self.mastery_sw_lab_status.setText(msg)
        self.statusBar().showMessage(msg + ". Save As to test.", 5000)

    def apply_overmastery_four_stats_all(self, auto: bool = False) -> None:
        if not self.save:
            return
        effects = self._selected_overmastery_effect_values()
        if any(v is None for v in effects[:4]):
            self.statusBar().showMessage("Pick four Overmastery stats first.", 3500)
            return
        value = self._parse_mastery_u32_text(getattr(self, "mastery_overmastery_value_edit", None).text() if hasattr(self, "mastery_overmastery_value_edit") else "-1", -1)
        write_value = bool(getattr(self, "mastery_overmastery_write_value_check", None) is None or self.mastery_overmastery_write_value_check.isChecked())
        total = {"effect_changed": 0, "value_changed": 0, "effect_found": 0, "value_found": 0}
        for group_index in range(0x28):
            stats = self._apply_overmastery_four_stats_to_group(group_index, effects, value, write_value=write_value)
            for key in total:
                total[key] += int(stats.get(key, 0) or 0)
        self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
        try:
            tabs = getattr(self, "mastery_value_tabs", None)
            if tabs is not None and tabs.currentIndex() == 1:
                self.refresh_mastery_mod_rows()
        except Exception:
            pass
        shown = "80%" if int(value) == 0xFFFFFFFF else ("20%" if int(value) == 512 else str(int(value)))
        prefix = "Auto applied" if auto else "Applied"
        msg = f"{prefix}: stats {total['effect_changed']}, values {total['value_changed']} · {shown}"
        if hasattr(self, "mastery_sw_lab_status"):
            self.mastery_sw_lab_status.setText(msg)
        self.statusBar().showMessage(msg + ". Save As to test.", 5500)

    def _refresh_mastery_after_bulk_write(self) -> None:
        self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
        try:
            tabs = getattr(self, "mastery_value_tabs", None)
            # Rows / Edit is tab 2 in the cleaned layout. If it is open, refresh
            # immediately so the table visibly changes after button presses.
            if tabs is not None and tabs.currentIndex() == 1:
                self.refresh_mastery_mod_rows()
        except Exception:
            pass
        try:
            self.update_status_text_light()
        except Exception:
            pass

    def _apply_mastery_value_to_all_visible_groups(self, value: int) -> Dict[str, int]:
        total = {"found": 0, "changed": 0, "checked": 0, "missing": 0}
        try:
            choices = self._mastery_character_choices()
        except Exception:
            choices = []
        for choice in choices:
            try:
                char_unit = int(choice.get("unit"))
            except Exception:
                continue
            stats = self._apply_mastery_value_preset_to_unit(char_unit, int(value))
            total["found"] += int(stats.get("checked", 0) or 0)
            total["checked"] += int(stats.get("checked", 0) or 0)
            total["changed"] += int(stats.get("changed", 0) or 0)
            total["missing"] += int(stats.get("missing", 0) or 0)
        return total

    def _apply_mastery_effect_to_all_visible_groups(self, effect_value: int) -> Dict[str, int]:
        total = {"found": 0, "changed": 0, "checked": 0, "missing": 0}
        try:
            choices = self._mastery_character_choices()
        except Exception:
            choices = []
        for choice in choices:
            try:
                char_unit = int(choice.get("unit"))
            except Exception:
                continue
            try:
                _rows, metas = self._mastery_mod_build_rows_for_character(char_unit)
            except Exception:
                metas = []
            for meta in metas:
                # Do not let the normal all-effect test overwrite concrete
                # overmastery stat lanes; those are handled by the four-stat picker.
                try:
                    if self._mastery_overmastery_slot_for_unit(int(meta.get("unit_id", 0))) is not None:
                        continue
                except Exception:
                    pass
                rec = meta.get("mastery_rec")
                if rec is None:
                    total["missing"] += 1
                    continue
                total["checked"] += 1
                exists, did_change = self._set_record_first_value_quiet(rec, int(effect_value) & 0xFFFFFFFF)
                total["found"] += 1 if exists else 0
                total["changed"] += 1 if did_change else 0
        return total

    def _apply_overmastery_concrete_value_sweep(self, value: int, label: str = "Overmastery value sweep") -> Dict[str, int]:
        if not self.save:
            return {"found": 0, "changed": 0, "checked": 0, "missing": 0}
        value = self._mastery_sw_normalize_write_value(value)
        found = changed = missing = checked = 0
        for group_index in range(0x28):
            for lane in range(4):
                checked += 1
                rec = self._mastery_overmastery_record(group_index, lane, 1607)
                exists, did_change = self._set_record_first_value_quiet(rec, value)
                if exists:
                    found += 1
                    changed += 1 if did_change else 0
                else:
                    missing += 1
        self._refresh_mastery_after_bulk_write()
        shown = "FFFFFFFF / 80%" if int(value) == 0xFFFFFFFF else f"{int(value):,} / 0x{int(value):08X}"
        msg = f"{label}: wrote {shown}. {changed} changed, {found} found, {missing} missing out of {checked} overmastery value slot(s)."
        if hasattr(self, "mastery_sw_lab_status"):
            self.mastery_sw_lab_status.setText(msg)
        self.statusBar().showMessage(msg + " Save As to test in game.", 9000)
        return {"found": found, "changed": changed, "checked": checked, "missing": missing}

    def _apply_overmastery_concrete_effect_sweep(self, effects: List[int], label: str = "Overmastery stat sweep") -> Dict[str, int]:
        if not self.save:
            return {"found": 0, "changed": 0, "checked": 0, "missing": 0}
        clean_values = [int(v) & 0xFFFFFFFF for v in (effects or [])]
        if not clean_values:
            return {"found": 0, "changed": 0, "checked": 0, "missing": 0}
        found = changed = missing = checked = 0
        for group_index in range(0x28):
            for lane in range(4):
                checked += 1
                value = clean_values[lane % len(clean_values)]
                rec = self._mastery_overmastery_record(group_index, lane, 1606)
                exists, did_change = self._set_record_first_value_quiet(rec, value)
                if exists:
                    found += 1
                    changed += 1 if did_change else 0
                else:
                    missing += 1
        self._refresh_mastery_after_bulk_write()
        msg = f"{label}: {changed} changed, {found} found, {missing} missing out of {checked} overmastery stat slot(s)."
        if hasattr(self, "mastery_sw_lab_status"):
            self.mastery_sw_lab_status.setText(msg)
        self.statusBar().showMessage(msg + " Save As to test in game.", 9000)
        return {"found": found, "changed": changed, "checked": checked, "missing": missing}

    def _mastery_sw_value_record_for_relative(self, rel: int) -> Optional[UnitRecord]:
        """Resolve one FF470600/1607 record by Save-Wizard-style relative offset.

        Community notes use 92000000 000B8079 and +0x18 slot spacing. Older
        offsets can be off by one because Type 8 sets the pointer after the
        searched bytes, so try exact and nearby relatives.
        """
        for candidate in (int(rel), int(rel) - 1, int(rel) + 1):
            rec = self._mastery_mod_record_by_sw_relative(candidate, id_type=1607)
            if rec is not None:
                return rec
        return None

    def _mastery_sw_normalize_write_value(self, value: Any) -> int:
        try:
            ivalue = int(value)
        except Exception:
            ivalue = 0
        # The pinned overmastery code uses FFFFFFFF as the 80% value. The save
        # writer converts that to -1 for signed int rows while preserving the raw
        # FF FF FF FF bytes.
        if ivalue == -1:
            return 0xFFFFFFFF
        return max(0, min(0xFFFFFFFF, ivalue))

    def _apply_mastery_sw_value_sweep(self, start_rel: int, count: int, step: int, value: int, label: str) -> Dict[str, int]:
        if not self.save:
            return {"found": 0, "changed": 0, "checked": 0, "missing": 0}
        value = self._mastery_sw_normalize_write_value(value)
        found = changed = missing = 0
        for i in range(max(0, int(count))):
            rel = int(start_rel) + i * int(step)
            rec = self._mastery_sw_value_record_for_relative(rel)
            exists, did_change = self._set_record_first_value_quiet(rec, value)
            if exists:
                found += 1
                changed += 1 if did_change else 0
            else:
                missing += 1

        # Reliable fallback: if the Save-Wizard relative scan did not resolve in
        # this save, use the already-working parsed row pairing from Rows / Edit.
        fallback_note = ""
        if found == 0 and int(start_rel) == 0x11 and int(count) == 0x7AB:
            fallback = self._apply_mastery_value_to_all_visible_groups(value)
            found = int(fallback.get("found", 0))
            changed = int(fallback.get("changed", 0))
            missing = int(fallback.get("missing", 0))
            fallback_note = " Used parsed-row fallback."

        self._refresh_mastery_after_bulk_write()
        shown = "FFFFFFFF / 80%" if int(value) == 0xFFFFFFFF else f"{int(value):,} / 0x{int(value):08X}"
        msg = (
            f"{label}: wrote {shown}. "
            f"{changed} changed, {found} found, {missing} missing out of {int(count)} slot(s).{fallback_note}"
        )
        if hasattr(self, "mastery_sw_lab_status"):
            self.mastery_sw_lab_status.setText(msg)
        self.statusBar().showMessage(msg + "  Save As to test in game.", 9000)
        return {"found": found, "changed": changed, "checked": int(count), "missing": missing}

    def _mastery_sw_effect_record_for_relative(self, rel: int) -> Optional[UnitRecord]:
        for candidate in (int(rel), int(rel) - 1, int(rel) + 1):
            rec = self._mastery_mod_record_by_sw_relative(candidate, id_type=1606)
            if rec is not None:
                return rec
        return None

    def _apply_mastery_sw_effect_sweep(self, start_rel: int, count: int, step: int, values: List[int], label: str) -> Dict[str, int]:
        if not self.save:
            return {"found": 0, "changed": 0, "checked": 0, "missing": 0}
        clean_values = [int(v) & 0xFFFFFFFF for v in (values or [])]
        if not clean_values:
            return {"found": 0, "changed": 0, "checked": 0, "missing": 0}
        found = changed = missing = 0
        for i in range(max(0, int(count))):
            rel = int(start_rel) + i * int(step)
            value = clean_values[i % len(clean_values)]
            rec = self._mastery_sw_effect_record_for_relative(rel)
            exists, did_change = self._set_record_first_value_quiet(rec, value)
            if exists:
                found += 1
                changed += 1 if did_change else 0
            else:
                missing += 1

        fallback_note = ""
        if found == 0 and int(start_rel) == 0x11 and int(count) == 0x7AB and len(clean_values) == 1:
            fallback = self._apply_mastery_effect_to_all_visible_groups(clean_values[0])
            found = int(fallback.get("found", 0))
            changed = int(fallback.get("changed", 0))
            missing = int(fallback.get("missing", 0))
            fallback_note = " Used parsed-row fallback."

        self._refresh_mastery_after_bulk_write()
        msg = f"{label}: {changed} changed, {found} found, {missing} missing out of {int(count)} effect slot(s).{fallback_note}"
        if hasattr(self, "mastery_sw_lab_status"):
            self.mastery_sw_lab_status.setText(msg)
        self.statusBar().showMessage(msg + "  Save As to test in game.", 9000)
        return {"found": found, "changed": changed, "checked": int(count), "missing": missing}

    def apply_mastery_sw_normal_effect_sweep(self, effect_value: int, label: str = "selected effect") -> None:
        """Turn every normal FF460600 mastery effect row into one effect.

        Debug pattern from the notes:
            80010005 FF460600
            00000000 00000000
            4E000011 XXXXXXXX
            7AB00018 00000000
        """
        self._apply_mastery_sw_effect_sweep(
            start_rel=0x11,
            count=0x7AB,
            step=0x18,
            values=[int(effect_value)],
            label=f"SW normal FF460600 effect sweep ({label})",
        )

    def apply_mastery_sw_overmastery_value_sweep(self, value: int) -> None:
        """Set all concrete overmastery value lanes.

        The reliable mapping is 40 groups * 4 lanes:
            unit = 10000000 + group_index * 1000 + lane_index

        This is safer than relying only on Save-Wizard relative offsets, which
        can shift between save variants.
        """
        self._apply_overmastery_concrete_value_sweep(int(value), label="Overmastery FF470600 value sweep")

    def apply_mastery_sw_overmastery_selected_four_stats(self) -> None:
        """Write the selected four Overmastery stat IDs to every concrete group."""
        effects = self._selected_overmastery_effect_values()
        if not effects or len(effects) < 4 or any(v is None for v in effects[:4]):
            self.statusBar().showMessage("Pick four Overmastery stats first.", 5000)
            return
        self._apply_overmastery_concrete_effect_sweep(
            [int(v) & 0xFFFFFFFF for v in effects[:4]],
            label="Overmastery FF460600 selected 4-stat sweep",
        )

    def apply_mastery_sw_normal_value_sweep(self, value: int) -> None:
        """Apply the FF470600 normal mastery value sweep from the sheet.

        Pattern:
            80010005 FF470600
            00000000 00000000
            4E000011 VVVVVVVV
            7AB00018 00000000
        """
        self._apply_mastery_sw_value_sweep(
            start_rel=0x11,
            count=0x7AB,
            step=0x18,
            value=int(value),
            label="SW normal FF470600 sweep",
        )

    def apply_mastery_sw_actual_stat_240_sweep(self, value: Optional[int] = None) -> None:
        """Apply the later 240-slot actual-stat block from the Discord notes.

        Pattern:
            92000000 000B8079
            93000000 00001668
            4E000000 VVVVVVVV
            00F00018 00000000

        Effective start relative is 0x0B8079 + 0x1668. 0xF0 = 240 rows.
        """
        if value is None or isinstance(value, bool):
            try:
                value = int(getattr(self, "mastery_sw_value_spin").value())
            except Exception:
                value = 0x3F8
        self._apply_mastery_sw_value_sweep(
            start_rel=0x0B8079 + 0x1668,
            count=0xF0,
            step=0x18,
            value=int(value),
            label="SW 240 actual-stat FF470600 sweep",
        )

    def apply_mastery_sw_single_slot_test(self) -> None:
        """Write one +0x18 slot from the community individual slot test."""
        if not self.save:
            return
        try:
            slot = int(getattr(self, "mastery_sw_slot_spin").value())
        except Exception:
            slot = 1
        try:
            value = int(getattr(self, "mastery_sw_value_spin").value())
        except Exception:
            value = 0
        slot = max(1, min(31408, int(slot)))
        value = self._mastery_sw_normalize_write_value(value)
        # The individual test says Slot 1 = +0x000000, Slot 2 = +0x18, etc.
        # The pointer setup in the note uses 92000000 000B8079.
        rel = 0x0B8079 + (slot - 1) * 0x18
        rec = self._mastery_sw_value_record_for_relative(rel)
        exists, did_change = self._set_record_first_value_quiet(rec, value)
        if exists:
            self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
            shown = "FFFFFFFF / 80%" if int(value) == 0xFFFFFFFF else f"{int(value):,} / 0x{int(value):08X}"
            msg = f"SW slot test wrote slot {slot} rel 0x{rel:06X} to {shown} ({'changed' if did_change else 'already set'})."
        else:
            msg = f"SW slot test could not resolve slot {slot} rel 0x{rel:06X} to an FF470600/1607 row in this save."
        if hasattr(self, "mastery_sw_lab_status"):
            self.mastery_sw_lab_status.setText(msg)
        self.statusBar().showMessage(msg, 9000)

    def apply_mastery_normal_value_sweep(self) -> None:
        """Compatibility wrapper for the older 0x03F8 community sweep."""
        self.apply_mastery_sw_normal_value_sweep(0x03F8)

    def _parse_mastery_offset_value(self, value: Any) -> Optional[int]:
        text = str(value or "").strip()
        if not text:
            return None
        text = text.replace(",", "")
        m = re.search(r"0x([0-9A-Fa-f]{1,8})", text)
        if m:
            return int(m.group(1), 16)
        m = re.search(r"\b([0-9A-Fa-f]{5,8})\b", text)
        if m:
            return int(m.group(1), 16)
        try:
            return int(text, 0)
        except Exception:
            return None

    def _mastery_offset_row_pick(self, row: Dict[str, Any], *needles: str) -> str:
        if not row:
            return ""
        keys = list(row.keys())
        normalized = {self._clean_mastery_sheet_key(k): k for k in keys if k is not None}
        for needle in needles:
            nk = self._clean_mastery_sheet_key(needle)
            if nk in normalized:
                value = row.get(normalized[nk], "")
                if value is not None and str(value).strip():
                    return str(value).strip()
        needle_keys = [self._clean_mastery_sheet_key(n) for n in needles]
        for key in keys:
            nk = self._clean_mastery_sheet_key(key)
            # Do not let a single-letter needle such as "N" match unrelated
            # columns like Name/Notes. Exact matches were already handled above.
            if any(len(nkey) >= 2 and nkey in nk for nkey in needle_keys):
                value = row.get(key, "")
                if value is not None and str(value).strip():
                    return str(value).strip()
        return ""

    def _mastery_method_pattern_entry_from_row(self, row: Dict[str, Any], source: str, row_index: int) -> Optional[Dict[str, Any]]:
        if not row:
            return None
        # Prefer true N / slot-info / address columns.  Do not treat ID/Search as
        # an offset; that column is the 1606 value/name source on the mastery DB.
        rel_text = self._mastery_offset_row_pick(
            row,
            "N", "Masteries_SlotINFO", "Masteries SlotINFO", "Mastery SlotINFO", "SlotINFO",
            "Slot ID", "Slot Code", "SW Code", "Save Wizard", "Code", "Locator",
            "SW Offset", "Rel Offset", "Relative Offset", "Offset", "Address", "Addr", "Code Offset",
        )
        rel = self._parse_mastery_sw_address_operand(rel_text)
        if rel is None:
            rel = self._parse_mastery_offset_value(rel_text)
        if rel is None:
            # Pasted code fallback: N is a 28xxxxxx direct write or 4Exxxxxx range write.
            joined = " | ".join(str(v or "") for v in row.values())
            m = re.search(r"(?i)\b((?:28|4E)[0-9A-F]{6})\b", joined)
            if m:
                rel = int(m.group(1), 16) & 0x00FFFFFF
            else:
                m = re.search(r"(?i)(?:offset|address|addr|rel|slotinfo|slot id|\bN\b)\D{0,24}(?:0x)?([0-9A-F]{5,7})\b", joined)
                if m:
                    rel = int(m.group(1), 16)
        if rel is None:
            return None
        # Effect/write value: ID/Search wins.  QMX remains a label/alias only.
        value_text = self._mastery_row_get(row, "ID/Search", "ID Search", "1606", "Effect Hash", "Hash", "Write Value", "Value", "New Value", "Target Value", "GBID")
        value = self._parse_mastery_id_search_value(value_text)
        if value is None:
            # Some offset sheets put the write value in a code/value cell.
            value_text = self._mastery_offset_row_pick(row, "Write", "Value", "New", "Target", "1606")
            value = self._parse_mastery_id_search_value(value_text)
        if value is None:
            return None
        value &= 0xFFFFFFFF
        if value in (0, EMPTY_HASH, 0xFF460600, 0x280B6CB0):
            return None
        label = self._mastery_row_get(row, "Label", "Name", "Effect", "Stat", "Description", "Notes") or f"Sheet row {row_index}"
        count, stride = self._parse_mastery_repeat_stride(row)
        return {"label": str(label).strip(), "rel": int(rel), "value": value, "source": source, "count": int(count), "stride": int(stride)}

    def _load_mastery_offset_pattern_from_csv(self, path: Path, source: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        if not path.exists():
            return out
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                if not reader.fieldnames:
                    return out
                for idx, row in enumerate(reader, start=2):
                    # Native normalized cache.
                    if {"label", "rel", "value"}.issubset({str(k).strip().lower() for k in row.keys()}):
                        rel = self._parse_mastery_offset_value(row.get("rel") or row.get("Rel"))
                        value = self._parse_mastery_id_search_value(row.get("value") or row.get("Value"))
                        if rel is None or value is None:
                            continue
                        count = self._parse_mastery_offset_value(row.get("count") or row.get("Count")) or 1
                        stride = self._parse_mastery_offset_value(row.get("stride") or row.get("Stride")) or 0
                        out.append({
                            "label": str(row.get("label") or row.get("Label") or f"Pattern row {idx}").strip(),
                            "rel": int(rel),
                            "value": int(value) & 0xFFFFFFFF,
                            "source": source,
                            "count": int(count),
                            "stride": int(stride),
                        })
                        continue
                    entry = self._mastery_method_pattern_entry_from_row(row, source, idx)
                    if entry:
                        count = int(entry.get("count", 1) or 1)
                        stride = int(entry.get("stride", 0) or 0)
                        if count > 1 and stride > 0:
                            for i in range(count):
                                expanded = dict(entry)
                                expanded["label"] = f"{entry.get('label', f'Pattern row {idx}')} #{i + 1}"
                                expanded["rel"] = int(entry["rel"]) + i * stride
                                expanded["count"] = 1
                                expanded["stride"] = 0
                                out.append(expanded)
                        else:
                            out.append(entry)
        except Exception:
            return []
        # De-dupe by relative offset, later duplicate rows are usually notes.
        seen = set(); deduped: List[Dict[str, Any]] = []
        for entry in out:
            rel = int(entry.get("rel", -1))
            if rel < 0 or rel in seen:
                continue
            seen.add(rel); deduped.append(entry)
        return deduped

    def _download_and_cache_mastery_offset_pattern_db(self, url: str) -> int:
        text = load_sheet_csv(url)
        raw_path = RESOURCE_DIR / "mastery_offset_pattern_sheet_raw.csv"
        raw_path.write_text(text, encoding="utf-8-sig")
        rows = self._load_mastery_offset_pattern_from_csv(raw_path, f"Downloaded offset sheet {url}")
        norm_path = RESOURCE_DIR / "mastery_offset_pattern_downloaded.csv"
        with norm_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["label", "rel", "value", "count", "stride", "source"])
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    "label": str(row.get("label", "")),
                    "rel": f"0x{int(row.get('rel', 0)):06X}",
                    "value": f"0x{int(row.get('value', 0)) & 0xFFFFFFFF:08X}",
                    "count": int(row.get("count", 1) or 1),
                    "stride": f"0x{int(row.get('stride', 0) or 0):X}",
                    "source": str(row.get("source", "")),
                })
        self.mastery_offset_pattern_cache = None
        self._invalidate_mastery_mod_caches(clear_models=False)
        return len(rows)

    def download_mastery_offset_pattern_db(self) -> None:
        url, ok = QInputDialog.getText(
            self,
            "Download Mastery Offset Pattern DB",
            "Google Sheets URL:",
            text=MASTERY_OFFSET_PATTERN_SHEET_URL,
        )
        if not ok or not url.strip():
            return
        try:
            count = self._download_and_cache_mastery_offset_pattern_db(url.strip())
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Offset DB download failed",
                f"Could not download/parse the mastery offset pattern sheet from this PC.\n\n{exc}",
            )
            return
        self.refresh_mastery_mod_code_rows()
        QMessageBox.information(self, "Offset DB downloaded", f"Loaded {count} offset pattern row(s).")

    def _mastery_method_pattern_entries(self) -> List[Dict[str, Any]]:
        """Working Method/Skiller Save Wizard pattern, translated to 1606 writes.

        Downloaded offset-pattern rows are preferred.  If no downloaded sheet is
        available, the bundled fallback pattern remains available for testing.
        Values are normal little-endian uints by the save parser, so entries use
        readable integer/hash values such as 0x45C65767.
        """
        if getattr(self, "mastery_offset_pattern_cache", None) is not None:
            return self.mastery_offset_pattern_cache or []
        entries: List[Dict[str, Any]] = []
        for path, source in [
            (RESOURCE_DIR / "mastery_offset_pattern_downloaded.csv", "Downloaded Mastery Offset Pattern sheet gid 1568354475"),
            (RESOURCE_DIR / "mastery_offset_pattern_sheet_raw.csv", "Raw Mastery Offset Pattern sheet gid 1568354475"),
        ]:
            entries.extend(self._load_mastery_offset_pattern_from_csv(path, source))
        if entries:
            self.mastery_offset_pattern_cache = entries
            return entries

        entries = []
        def add(label: str, rel: int, value: int) -> None:
            entries.append({"label": label, "rel": int(rel), "value": int(value) & 0xFFFFFFFF, "source": "Bundled fallback pattern"})
        for label, rel, value in [
            ("Direct overmastery 1", 0x0B7AD8, 0x45C65767),
            ("Direct overmastery 2", 0x0B7AC0, 0x6CB38EF3),
            ("Direct overmastery 3", 0x0B7AA8, 0x6CB38EF3),
            ("Direct overmastery 4", 0x0B7A90, 0x6CB38EF3),
        ]:
            add(label, rel, value)
        for label, start, count, stride, value in [
            ("Range A · Stun Power Up", 0x07E910, 0x0096, 0x18, 0x6CB38EF3),
            ("Range B · Normal Damage Cap Up", 0x07F720, 0x0096, 0x18, 0x43B7581D),
            ("Range C · Skill Damage Cap Up", 0x080530, 0x0096, 0x18, 0x9C555433),
            ("Range D · SBA Damage Cap Up", 0x081340, 0x0078, 0x18, 0x4A4C093D),
            ("Tail A · SBA Damage Up", 0x081E80, 0x0006, 0x18, 0x4E42646B),
            ("Tail B · Normal Damage Cap Up", 0x081F10, 0x0008, 0x18, 0x43B7581D),
            ("Tail C · Skill Damage Cap Up", 0x081FD0, 0x000A, 0x18, 0x9C555433),
            ("Tail D · SBA Damage Cap Up", 0x0820C0, 0x0003, 0x18, 0x4A4C093D),
        ]:
            for i in range(int(count)):
                add(f"{label} #{i + 1}", int(start) + i * int(stride), value)
        for label, rel, value in [
            ("Final direct · SBA Damage Up", 0x082108, 0x4E42646B),
            ("Final direct · Code value 0x7B727910", 0x082120, 0x7B727910),
            ("Final direct · Code value 0x7B727910", 0x082138, 0x7B727910),
        ]:
            add(label, rel, value)
        self.mastery_offset_pattern_cache = entries
        return entries

    def refresh_mastery_mod_code_rows(self) -> None:
        model = getattr(self, "mastery_mod_code_model", None)
        if model is None:
            return
        rows: List[List[Any]] = []
        metas: List[Dict[str, Any]] = []
        base = self._mastery_mod_anchor_base_abs()
        if not self.save or base is None:
            self.mastery_mod_code_rows_meta = []
            model.set_rows([])
            if hasattr(self, "mastery_mod_code_status"):
                self.mastery_mod_code_status.setText(self._mastery_mod_anchor_status())
            return
        resolved = 0
        already = 0
        for entry in self._mastery_method_pattern_entries():
            rel = int(entry["rel"])
            value = int(entry["value"]) & 0xFFFFFFFF
            rec = self._mastery_mod_record_by_sw_relative(rel)
            current = None
            unit_id = "—"
            status = "No 1606 row at offset"
            if rec is not None:
                resolved += 1
                unit_id = int(rec.unit_id)
                try:
                    current = int(self.save.get_values(rec, 1)[0]) & 0xFFFFFFFF
                except Exception:
                    current = None
                if current == value:
                    already += 1
                    status = "Already matches"
                else:
                    status = "Ready to write"
            repeat_text = "—"
            try:
                c = int(entry.get("count", 1) or 1)
                st = int(entry.get("stride", 0) or 0)
                if c > 1 and st > 0:
                    repeat_text = f"{c} @ +0x{st:X}"
            except Exception:
                pass
            rows.append([
                entry["label"],
                f"0x{rel:06X}",
                self._mastery_effect_name(value),
                self._mastery_effect_name(current) if current is not None else "—",
                self._hash_hex_or_dash(current) if current is not None else "—",
                unit_id,
                repeat_text,
                status,
            ])
            metas.append({"rel": rel, "value": value, "record": rec, "current": current, "unit_id": unit_id, "label": entry["label"], "count": entry.get("count", 1), "stride": entry.get("stride", 0)})
        self.mastery_mod_code_rows_meta = metas
        model.set_rows(rows)
        if hasattr(self, "mastery_mod_code_table"):
            self._set_table_widths(self.mastery_mod_code_table, {0: 270, 1: 120, 2: 260, 3: 260, 4: 130, 5: 120, 6: 120, 7: 160})
        if hasattr(self, "mastery_mod_code_status"):
            self.mastery_mod_code_status.setText(f"{self._mastery_mod_anchor_status()} Resolved {resolved}/{len(metas)} pattern rows; {already} already match.")

    def _selected_mastery_mod_code_meta(self) -> Optional[Dict[str, Any]]:
        table = getattr(self, "mastery_mod_code_table", None)
        if table is None:
            return None
        idx = table.currentIndex()
        if idx.isValid() and idx.row() < len(getattr(self, "mastery_mod_code_rows_meta", [])):
            return self.mastery_mod_code_rows_meta[idx.row()]
        return None

    def apply_selected_mastery_mod_reference_to_code_row(self) -> None:
        ref = self._selected_mastery_mod_reference_meta()
        code = self._selected_mastery_mod_code_meta()
        if not self.save or not ref or not code:
            self.statusBar().showMessage("Select one Known Effect row and one Method Pattern row first.", 4500)
            return
        rec = code.get("record")
        if rec is None:
            self.statusBar().showMessage("Selected pattern offset does not resolve to a 1606 record in this save.", 4500)
            return
        value = int(ref.get("value", 0)) & 0xFFFFFFFF
        exists, did_change = self._set_record_first_value_quiet(rec, value)
        self.refresh_mastery_mod_rows()
        self._mark_stale_pages(["Mastery", "Save Health"])
        rel = int(code.get("rel", 0))
        if did_change:
            self.statusBar().showMessage(f"Wrote {ref.get('name')} to SW offset 0x{rel:06X} / unit {rec.unit_id}.", 4500)
        elif exists:
            self.statusBar().showMessage(f"SW offset 0x{rel:06X} already matches {ref.get('name')}.", 4500)

    def install_mastery_method_pattern_selected(self) -> None:
        if not self.save:
            return
        changed_1606 = 0
        changed_1607 = 0
        resolved = 0
        missing = 0
        write_1607 = self._mastery_mod_recommended_write_1607_enabled()
        state_value = self._mastery_mod_recommended_1607_value()
        for entry in self._mastery_method_pattern_entries():
            rec = self._mastery_mod_record_by_sw_relative(int(entry["rel"]))
            if rec is None:
                missing += 1
                continue
            resolved += 1
            _, did_change = self._set_record_first_value_quiet(rec, int(entry["value"]) & 0xFFFFFFFF)
            changed_1606 += 1 if did_change else 0
            if write_1607:
                state_rec = self._mastery_mod_record_by_sw_relative(int(entry["rel"]), id_type=1607)
                if state_rec is None:
                    state_rec = self.save.find_first("int", 1607, int(rec.unit_id)) or self.save.find_first("uint", 1607, int(rec.unit_id))
                if state_rec is not None:
                    _, did_state_change = self._set_record_first_value_quiet(state_rec, state_value)
                    changed_1607 += 1 if did_state_change else 0
        self.refresh_mastery_mod_rows()
        self._mark_stale_pages(["Mastery", "Save Health"])
        changed_total = changed_1606 + changed_1607
        write_note = f"1606 changed {changed_1606}, 1607 changed {changed_1607}" if write_1607 else f"1606 changed {changed_1606}; 1607 kept unchanged"
        if changed_total:
            self._after_editor_patch(f"Installed exact SW mastery pattern ({write_note}, {resolved} resolved, {missing} missing).")
        else:
            self.statusBar().showMessage(f"SW pattern checked: {resolved} resolved, {missing} missing, no value changes needed. {write_note}.", 7000)

    def _mastery_mod_effect_unit_candidates(self, char_unit: int, slot_zero: int, socket_zero: int) -> List[int]:
        """Return possible 1606/1607 unit ids for a user-selected slot/socket.

        The normal SaveDataBinary layout uses zero-based slot/socket units:
        char_unit * 10000 + slot * 10 + socket.  Some sheet/code notes describe
        the same fields using one-based wording, so the editor now tries both
        interpretations before reporting that a target cannot be edited.
        """
        cu = int(char_unit)
        s0 = max(0, int(slot_zero))
        k0 = max(0, int(socket_zero))
        candidates = []
        if 10000 <= cu < 10040 and 0 <= k0 < 4:
            # Four-stat overmastery lanes are concrete 8-digit units.
            candidates.append(self._mastery_overmastery_unit_id(cu - 10000, k0))
            candidates.append(self._mastery_overmastery_unit_id(cu - 10000, s0 if 0 <= s0 < 4 else k0))
        candidates += [
            # Normal mastery/collection rows: 10000 -> 100000000, slot * 10 + socket.
            cu * 10000 + s0 * 10 + k0,
            cu * 10000 + (s0 + 1) * 10 + k0,
            cu * 10000 + s0 * 10 + (k0 + 1),
            cu * 10000 + (s0 + 1) * 10 + (k0 + 1),
            # Alternate board/container-style rows seen in the hash/code notes.
            cu * 1000 + s0 * 1000 + k0,
            cu * 1000 + (s0 + 1) * 1000 + k0,
            cu * 1000 + s0 * 1000 + (k0 + 1),
            cu * 1000 + (s0 + 1) * 1000 + (k0 + 1),
        ]
        out: List[int] = []
        for uid in candidates:
            if uid not in out:
                out.append(uid)
        return out

    def _mastery_mod_find_target_records(self, char_unit: int, slot_zero: int, socket_zero: int) -> tuple[Optional[UnitRecord], Optional[UnitRecord], int]:
        if not self.save:
            return None, None, 0
        # First try the row metadata from the visible Current Save Rows table.
        # This protects manual writes when the displayed row is based on parsed
        # save data but the typed slot/socket came from the UI controls.
        for meta in getattr(self, "mastery_mod_rows_meta", []) or []:
            try:
                if int(meta.get("char_unit")) == int(char_unit) and int(meta.get("slot")) == int(slot_zero) and int(meta.get("socket")) == int(socket_zero):
                    effect_rec = meta.get("mastery_rec")
                    state_rec = meta.get("state_rec")
                    uid = int(meta.get("unit_id") or 0)
                    if effect_rec is not None or state_rec is not None:
                        return effect_rec, state_rec, uid
            except Exception:
                continue
        # Then try exact and one-based-compatible unit id formulas.
        for uid in self._mastery_mod_effect_unit_candidates(char_unit, slot_zero, socket_zero):
            effect_rec = self.save.find_first("uint", 1606, uid) or self.save.find_first("int", 1606, uid)
            state_rec = self.save.find_first("int", 1607, uid) or self.save.find_first("uint", 1607, uid)
            if effect_rec is not None or state_rec is not None:
                return effect_rec, state_rec, uid
        # Final safe fallback: if the user picked a concrete Existing Save Row,
        # use that row instead of failing with a formula-only target.
        combo = getattr(self, "mastery_mod_target_combo", None)
        if combo is not None and combo.currentData() is not None:
            meta = self._mastery_mod_meta_by_unit(int(combo.currentData()))
            if meta is not None:
                effect_rec = meta.get("mastery_rec")
                state_rec = meta.get("state_rec")
                uid = int(meta.get("unit_id") or 0)
                if effect_rec is not None or state_rec is not None:
                    return effect_rec, state_rec, uid
        return None, None, self._mastery_mod_effect_unit_candidates(char_unit, slot_zero, socket_zero)[0]

    def _update_mastery_mod_visible_row_after_write(self, target_unit: int, effect_value: Optional[int], state_value: Optional[int]) -> None:
        """Update the selected Mastery row without rebuilding all tables."""
        rows = getattr(getattr(self, "mastery_mod_model", None), "rows", None)
        if rows is None:
            return
        for row_index, meta in enumerate(getattr(self, "mastery_mod_rows_meta", []) or []):
            try:
                if int(meta.get("unit_id") or 0) != int(target_unit):
                    continue
                if effect_value is not None:
                    meta["mastery"] = int(effect_value) & 0xFFFFFFFF
                    if row_index < len(rows):
                        rows[row_index][2] = self._mastery_effect_name(effect_value)
                        rows[row_index][3] = self._mastery_effect_category(effect_value, meta.get("slotinfo", 0))
                        rows[row_index][5] = self._hash_hex_or_dash(effect_value)
                if state_value is not None:
                    meta["state"] = int(state_value)
                    if row_index < len(rows):
                        rows[row_index][4] = self._mastery_mod_state_label(state_value, meta.get("mastery", effect_value or 0))
                model = getattr(self, "mastery_mod_model", None)
                if model is not None and row_index < len(rows):
                    left = model.index(row_index, 0)
                    right = model.index(row_index, max(0, model.columnCount() - 1))
                    model.dataChanged.emit(left, right, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole])
                combo = getattr(self, "mastery_mod_target_combo", None)
                if combo is not None:
                    for i in range(combo.count()):
                        try:
                            if int(combo.itemData(i)) == int(target_unit):
                                row_no = int(meta.get("row_number", meta.get("slot", 0) + 1))
                                section = str(meta.get("section") or "Mastery row")
                                mastery = int(meta.get("mastery", 0) or 0) & 0xFFFFFFFF
                                state = meta.get("state", 0)
                                combo.setItemText(i, f"Row {row_no} · {section} — {self._mastery_effect_name(mastery)} — Value {self._mastery_mod_state_label(state, mastery)}")
                                break
                        except Exception:
                            pass
                break
            except Exception:
                continue

    def _selected_mastery_mod_target_meta(self) -> Optional[Dict[str, Any]]:
        """Return the concrete save row selected in the one-row editor.

        This is intentionally separate from _selected_mastery_mod_meta(), which
        depends on the raw table selection.  The redesigned page makes the row
        dropdown the primary safe target so manual edits do not depend on the
        older slot/socket formulas.
        """
        combo = getattr(self, "mastery_mod_target_combo", None)
        if combo is not None and combo.currentData() is not None:
            try:
                meta = self._mastery_mod_meta_by_unit(int(combo.currentData()))
                if meta is not None:
                    return meta
            except Exception:
                pass
        return self._selected_mastery_mod_meta()

    def apply_mastery_mod_selected_row_edit(self, silent: bool = False) -> None:
        """Write the selected effect/value to the concrete selected save row.

        This is the safe path used by the redesigned Mastery page.  It
        writes the UnitRecord objects already resolved by refresh_mastery_mod_rows()
        instead of deriving a unit from slot/socket.  That avoids the confusion
        around N offsets, 28/4E opcodes, one-based wording, and the raw all-row
        scan synthetic group.
        """
        if not self.save:
            return
        meta = self._selected_mastery_mod_target_meta()
        if not meta:
            if not silent:
                self.statusBar().showMessage("Pick an existing mastery row first.", 4500)
            return
        effect_value = getattr(self, "mastery_mod_effect_combo", None).currentData() if hasattr(self, "mastery_mod_effect_combo") else None
        state_value = int(getattr(self, "mastery_mod_state_spin").value()) if hasattr(self, "mastery_mod_state_spin") else 1023
        write_state = bool(getattr(self, "mastery_mod_state_write_check", None) is not None and self.mastery_mod_state_write_check.isChecked())
        effect_rec = meta.get("mastery_rec")
        state_rec = meta.get("state_rec")
        target_unit = int(meta.get("unit_id") or 0)

        # Some saves expose the same row through the parsed table but do not
        # retain the record object in older metadata. Fall back to direct lookup
        # by the selected concrete unit before giving up.
        if effect_rec is None and target_unit:
            effect_rec = self._mastery_find_first_any(1606, target_unit)
        if state_rec is None and target_unit:
            state_rec = self._mastery_find_first_any(1607, target_unit)

        changed = 0
        found = 0
        effect_found = False
        state_found = False
        effect_written: Optional[int] = None
        state_written: Optional[int] = None
        if effect_value is not None:
            effect_written = int(effect_value) & 0xFFFFFFFF
            exists, did_change = self._set_record_first_value_quiet(effect_rec, effect_written)
            effect_found = bool(exists)
            found += 1 if exists else 0
            changed += 1 if did_change else 0
        if write_state:
            state_written = state_value
            exists, did_change = self._set_record_first_value_quiet(state_rec, state_value)
            state_found = bool(exists)
            found += 1 if exists else 0
            changed += 1 if did_change else 0

        if found:
            self._update_mastery_mod_visible_row_after_write(target_unit, effect_written if effect_found else None, state_written if state_found else None)
            detail_meta = self._mastery_mod_meta_by_unit(target_unit) or meta
            detail_text = self._mastery_mod_selected_detail_text(detail_meta)
            if hasattr(self, "mastery_mod_selected_detail_label"):
                self.mastery_mod_selected_detail_label.setText(detail_text)
            if hasattr(self, "mastery_mod_edit_detail_label"):
                self.mastery_mod_edit_detail_label.setText(detail_text)
            if changed:
                self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
                row_text = f"Row {int(meta.get('row_number', meta.get('slot', 0) + 1))} · {meta.get('section', 'Mastery row')}"
                self.statusBar().showMessage(
                    f"Updated selected mastery row: {row_text}. Effect {'written' if effect_found else 'missing'}; value {'written' if state_found and write_state else 'kept'}.",
                    3500,
                )
            elif not silent:
                self.statusBar().showMessage(
                    f"Selected mastery row already matches. Effect {'found' if effect_found else 'missing'}; value {'found' if state_found and write_state else 'kept'}.",
                    3500,
                )
        elif not silent:
            self.statusBar().showMessage(
                f"Selected row did not have editable effect/value records. Unit: {target_unit}. Try another row.",
                6500,
            )

    def apply_mastery_mod_manual_edit(self, silent: bool = False) -> None:
        if not self.save:
            return
        char_unit = self._mastery_mod_current_character_unit()
        # In the raw all-row scan the slot/socket formula is not reliable.
        # Force manual writes to use the concrete row selected in the dropdown/table.
        if int(char_unit) < 0:
            meta = self._selected_mastery_mod_meta() or self._mastery_mod_meta_by_unit(int(getattr(self, "mastery_mod_target_combo").currentData() or 0))
            if meta is not None:
                char_unit = int(meta.get("char_unit", -1))
        slot = int(getattr(self, "mastery_mod_slot_spin").value()) - 1
        socket = int(getattr(self, "mastery_mod_socket_spin").value()) - 1
        effect_value = getattr(self, "mastery_mod_effect_combo").currentData()
        state_value = int(getattr(self, "mastery_mod_state_spin").value())
        effect_rec, state_rec, target_unit = self._mastery_mod_find_target_records(char_unit, slot, socket)

        write_state = bool(getattr(self, "mastery_mod_state_write_check", None) is not None and self.mastery_mod_state_write_check.isChecked())
        changed = 0
        found = 0
        if effect_value is not None:
            exists, did_change = self._set_record_first_value_quiet(effect_rec, int(effect_value) & 0xFFFFFFFF)
            found += 1 if exists else 0
            changed += 1 if did_change else 0
        if write_state:
            exists, did_change = self._set_record_first_value_quiet(state_rec, state_value)
            found += 1 if exists else 0
            changed += 1 if did_change else 0

        if changed:
            self._update_mastery_mod_visible_row_after_write(target_unit, int(effect_value) & 0xFFFFFFFF if effect_value is not None else None, state_value if write_state else None)
            self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
            self.statusBar().showMessage(
                f"Updated mastery row: unit {target_unit}. Value {'written' if write_state else 'kept unchanged'}.",
                3000,
            )
        elif found:
            if not silent:
                self.statusBar().showMessage(
                    f"That mastery row already matches the selected effect{' and value' if write_state else ''}. Target unit: {target_unit}.",
                    4500,
                )
        elif not silent:
            self.statusBar().showMessage(
                f"No editable mastery row found for Slot {slot + 1} / Socket {socket + 1}. Pick an existing row from the dropdown, then write again.",
                6500,
            )

    def apply_mastery_mod_sigil_slot_restore(self) -> None:
        if not hasattr(self, "mastery_mod_effect_combo"):
            return
        self._mastery_mod_loading = True
        try:
            for i in range(self.mastery_mod_effect_combo.count()):
                data = self.mastery_mod_effect_combo.itemData(i)
                if data is not None and int(data) == 0x7B727910:
                    self.mastery_mod_effect_combo.setCurrentIndex(i); break
            self.mastery_mod_state_spin.setValue(1)
            self.mastery_mod_slot_key_check.setChecked(True)
            self.mastery_mod_slot_key_edit.setText("0x280B6CB0")
        finally:
            self._mastery_mod_loading = False
        self.apply_mastery_mod_manual_edit()

    def _apply_13_sigil_slot_restore_to_character_unit(self, char_unit: int) -> int:
        """Install the observed sigil-slot restore effect into up to 13 logical mastery sockets.

        Max equipped sigil slots are treated as 13. The save exposes these as
        existing 1606/1607 effect rows plus the matching 1601 board slot key;
        this does not create or resize records.
        """
        if not self.save:
            return 0
        changed = 0
        max_slots = 13
        for idx in range(max_slots):
            slot = idx // 3
            socket = idx % 3
            board_unit = int(char_unit) * 1000 + slot
            changed += 1 if self._set_record_first_value(self.save.find_first("uint", 1601, board_unit), 0x280B6CB0, "Sigil Slot Key 1601") else 0
            changed += 1 if self._set_record_first_value(self._mastery_effect_record(char_unit, slot, socket), 0x7B727910, "Sigil Slot Restore 1606") else 0
            changed += 1 if self._set_record_first_value(self._mastery_state_record(char_unit, slot, socket), 1, "Sigil Slot Restore State 1607") else 0
        return changed

    def install_mastery_mod_13_sigil_slots_selected(self) -> None:
        if not self.save:
            return
        char_unit = self._mastery_mod_current_character_unit()
        changed = self._apply_13_sigil_slot_restore_to_character_unit(char_unit)
        self.refresh_mastery_mod_rows()
        self._mark_stale_pages(["Mastery", "Characters", "Save Health"])
        if changed:
            self._after_editor_patch(f"Installed up to 13 sigil equip-slot restores for the selected character ({changed} value(s)).")
        else:
            self.statusBar().showMessage("No editable sigil-slot restore records were found for the selected character.", 4500)

    def install_mastery_mod_op_selected(self) -> None:
        self.install_mastery_mod_recommended_preset_selected()

    def _mastery_character_choices(self) -> List[Dict[str, Any]]:
        """Return selectable character mastery groups.

        CharacterManager 160x rows use the character unit id, not the simple PL index:
        PL0000 -> unit 10000, PL0100 -> unit 10001, etc. Normal mastery rows
        use char_unit * 10000 + slot * 10 + socket. The four overmastery stat
        lanes use unit 10000000 + character_index * 1000 + lane.
        """
        choices: List[Dict[str, Any]] = []
        try:
            entries = []
            for entry in self.item_db.by_hash.values():
                gbid = str(getattr(entry, "item_id", "") or "").upper()
                if gbid.startswith("PL") and len(gbid) >= 6:
                    try:
                        idx = int(gbid[2:4])
                    except Exception:
                        continue
                    char_unit = 10000 + idx
                    entries.append((char_unit, str(getattr(entry, "display_name", "") or gbid), gbid))
            seen = set()
            for char_unit, name, gbid in sorted(entries, key=lambda x: x[0]):
                if char_unit in seen or char_unit > 10099:
                    continue
                seen.add(char_unit)
                choices.append({"unit": char_unit, "label": f"{name} ({gbid})", "gbid": gbid, "name": name})
        except Exception:
            pass
        # Include any extra mastery groups present in the save but not backed by a PL entry.
        if self.save:
            present_units = set()
            for rec in self.save.records:
                uid = int(rec.unit_id)
                if rec.id_type in {1601, 1602, 1605} and 10000000 <= uid < 11000000:
                    present_units.add(uid // 1000)
                elif rec.id_type in {1606, 1607} and 10000000 <= uid <= 10039003:
                    over = self._mastery_overmastery_slot_for_unit(uid)
                    if over is not None:
                        present_units.add(10000 + int(over[0]))
                elif rec.id_type in {1606, 1607} and 100000000 <= uid < 110000000:
                    present_units.add(uid // 10000)
            known_units = {int(c.get("unit", -1)) for c in choices}
            for char_unit in sorted(present_units - known_units):
                choices.append({"unit": char_unit, "label": f"Extra Mastery Group {char_unit}", "gbid": f"UNIT_{char_unit}", "name": f"Mastery Group {char_unit}"})
        if not choices:
            choices = [
                {"unit": 10000, "label": "Gran (PL0000)", "gbid": "PL0000", "name": "Gran"},
                {"unit": 10001, "label": "Djeeta (PL0100)", "gbid": "PL0100", "name": "Djeeta"},
            ]
        # Diagnostic synthetic group.  Community notes confirm that weapon
        # collections, overmasteries, and normal masteries are all FF4606/FF4706
        # collection rows.  This view is for finding raw offsets when a row does
        # not belong cleanly to the visible character/socket formula.
        if not any(int(c.get("unit", 0)) < 0 for c in choices):
            choices.append({"unit": -1, "label": "All FF4606/FF4706 Rows (raw scan)", "gbid": "ALL_FF4606", "name": "All Mastery/Collection Rows"})
        return choices

    def _populate_mastery_character_combo(self) -> None:
        combo = getattr(self, "mastery_character_combo", None)
        if combo is None:
            return
        current = combo.currentData()
        choices = self._mastery_character_choices()
        combo.blockSignals(True)
        combo.clear()
        for choice in choices:
            combo.addItem(choice["label"], int(choice["unit"]))
        target = int(current) if current is not None else 10000
        if 0 <= target < 10000:
            target = 10000 + target
        for i in range(combo.count()):
            if int(combo.itemData(i)) == target:
                combo.setCurrentIndex(i)
                break
        combo.blockSignals(False)

    def _hash_hex_or_dash(self, value: Any) -> str:
        try:
            ivalue = int(value or 0) & 0xFFFFFFFF
        except Exception:
            return str(value or "")
        if ivalue in (0, EMPTY_HASH):
            return "—"
        return f"0x{ivalue:08X}"

    def _mastery_hash_entry(self, value: Any):
        try:
            ivalue = int(value or 0) & 0xFFFFFFFF
        except Exception:
            return None
        if ivalue in (0, EMPTY_HASH):
            return None
        return self.item_db.lookup_hash(ivalue)

    def _mastery_special_name(self, value: Any, role: str = "mastery") -> Optional[str]:
        try:
            ivalue = int(value or 0) & 0xFFFFFFFF
        except Exception:
            return None
        if role == "slotinfo" and ivalue == 0x280B6CB0:
            return "Normal Sigil Slot Key (reported)"
        if role == "slotinfo" and ivalue == 0xFF460600:
            return "Save Wizard search marker / mastery anchor"
        observed = {
            0x7B727910: "Sigil Slot Restore / Add Equip Slot",
            0x45C65767: "Critical Rate",
            0x6757C645: "Critical Rate (endian-swapped note)",
            0x43B7581D: "Normal Damage Cap Up",
            0x9C555433: "Skill Damage Cap Up",
            0x4A4C093D: "SBA Damage Cap Up",
            0x6CB38EF3: "Stun Power Up",
            0x4E42646B: "SBA Damage Up",
            0xC4925BD7: "Attack Power Up",
            0x52A207B5: "Health Up",
            0x9A97C049: "Skill Damage Up",
            0x54929589: "Recovery Cap Up",
            0x68B39018: "Link/Burst Damage Up",
            0xCB63BE55: "Attack Power Up Variant 2",
            0xDCBD8423: "Attack Power Up Variant 3",
            0x59DCE1E8: "Attack Power Up Variant 4",
            0xF203BB15: "Attack Power Up Variant 5",
            0x57BBC478: "Health Up Variant 2",
            0x5A51F0CB: "Health Up Variant 3",
            0x9C6375CF: "Health Up Variant 4",
            0xF004E9F2: "Health Up Variant 5",
            0xC4B86ED7: "Critical Rate Variant 2",
            0xCEB0DBD2: "Critical Rate Variant 3",
            0xA3545CA1: "Stun Power Up Variant 2",
            0x59FBB7D8: "Stun Power Up Variant 3",
        }
        if role == "mastery" and ivalue in observed:
            return observed[ivalue]
        return None

    def _mastery_hash_display(self, value: Any, role: str = "mastery") -> str:
        try:
            ivalue = int(value or 0) & 0xFFFFFFFF
        except Exception:
            return str(value or "")
        if ivalue in (0, EMPTY_HASH):
            return "Empty"
        special = self._mastery_special_name(ivalue, role)
        if special:
            return f"{special} ({self._hash_hex_or_dash(ivalue)})"
        entry = self.item_db.lookup_hash(ivalue)
        if entry:
            return f"{entry.display_name} ({entry.item_id})"
        return f"Unknown {self._hash_hex_or_dash(ivalue)}"

    def _mastery_effect_name(self, mastery_value: Any) -> str:
        try:
            ivalue = int(mastery_value or 0) & 0xFFFFFFFF
        except Exception:
            return str(mastery_value or "")
        if ivalue in (0, EMPTY_HASH):
            return "Empty slot"
        special = self._mastery_special_name(ivalue, "mastery")
        if special:
            return special
        entry = self._mastery_hash_entry(ivalue)
        if entry:
            return entry.display_name
        return f"Unknown {self._hash_hex_or_dash(ivalue)}"

    def _mastery_effect_gbid(self, mastery_value: Any) -> str:
        entry = self._mastery_hash_entry(mastery_value)
        return entry.item_id if entry else ""

    def _mastery_effect_category(self, mastery_value: Any, slotinfo_value: Any = 0) -> str:
        try:
            h = int(mastery_value or 0) & 0xFFFFFFFF
            slot_h = int(slotinfo_value or 0) & 0xFFFFFFFF
        except Exception:
            return "Unknown"
        if h in (0, EMPTY_HASH):
            return "Empty"
        if h == 0x7B727910 or slot_h == 0x280B6CB0:
            return "Sigil Slot / Layout"
        if h in {0x43B7581D, 0x4A4C093D, 0x9C555433}:
            return "Damage Cap"
        if h in {0xC4925BD7, 0x9A97C049, 0x6CB38EF3, 0x4E42646B, 0x45C65767}:
            return "Damage / Offense"
        if h == 0x52A207B5:
            return "Survival"
        if self._mastery_special_name(h, "mastery"):
            return "Observed / Test Save"
        entry = self._mastery_hash_entry(h)
        name = (entry.display_name if entry else "").lower()
        gbid = (entry.item_id if entry else "").upper()
        text = f"{name} {gbid}"
        if "cap" in text:
            return "Damage Cap"
        if "attack" in text or "damage" in text or "tyranny" in text or "stamina" in text or "enmity" in text:
            return "Damage"
        if "health" in text or "aegis" in text or "guts" in text or "defense" in text:
            return "Survival"
        if "sigil" in text or gbid.startswith("GEEN"):
            return "Trait / Sigil Effect"
        if gbid.startswith("SKILL"):
            return "Skill / Trait"
        return entry.category if entry else "Unknown"

    def _mastery_state_display(self, state: Any, mastery_value: Any = 0) -> str:
        try:
            s = int(state or 0)
        except Exception:
            return str(state or "")
        try:
            h = int(mastery_value or 0) & 0xFFFFFFFF
        except Exception:
            h = 0
        if h in (0, EMPTY_HASH):
            return "Empty"
        if s == 0:
            return "Inactive"
        if s == 1:
            return "Active"
        return f"State {s}"

    def _mastery_slot_units_for_character(self, char_unit: int) -> List[int]:
        if not self.save:
            return []
        cu = int(char_unit)
        units = set()
        for rec in self.save.records:
            uid = int(rec.unit_id)
            if cu < 0:
                if rec.id_type in {1601, 1602, 1605, 1606, 1607}:
                    units.add(uid)
            elif rec.id_type in {1601, 1602, 1605} and uid // 1000 == cu:
                units.add(uid)
            elif rec.id_type in {1606, 1607} and uid // 10000 == cu:
                units.add(uid)
        return sorted(units)

    def _mastery_current_mode(self) -> str:
        combo = getattr(self, "mastery_mode_combo", None)
        return str(combo.currentData() or "effects") if combo is not None else "effects"

    def refresh_mastery_slot_rows(self) -> None:
        if not hasattr(self, "mastery_slot_model"):
            return
        if not self.save:
            self.mastery_slot_model.set_rows([])
            self.mastery_slot_rows_meta = []
            return
        combo = getattr(self, "mastery_character_combo", None)
        char_unit = int(combo.currentData()) if combo is not None and combo.currentData() is not None else 10000
        if 0 <= char_unit < 10000:
            char_unit = 10000 + char_unit
        mode = self._mastery_current_mode()
        q = getattr(self, "mastery_slot_filter_edit", None).text().strip().lower() if hasattr(self, "mastery_slot_filter_edit") else ""
        rows: List[List[Any]] = []
        metas: List[Dict[str, Any]] = []
        grouped = self.save.group_by_unit([1601, 1602, 1605, 1606, 1607])

        def maybe_add(row: List[Any], meta: Dict[str, Any], extra: List[Any]) -> None:
            if q and not self._matches_editor_filter(row + extra, q):
                return
            rows.append(row)
            metas.append(meta)

        if mode == "board":
            unit_ids = sorted(uid for uid, fields in grouped.items() if ((char_unit < 0 and int(uid) < 100000000) or (int(uid) // 1000 == char_unit and int(uid) < 100000000)) and any(fid in fields for fid in (1601, 1602, 1605)))
            for unit_id in unit_ids:
                recs = grouped.get(unit_id, {})
                derived_char = int(unit_id) // 1000 if char_unit < 0 else char_unit
                slot = int(unit_id) - int(derived_char) * 1000
                slotinfo = self._record_first_value(recs.get(1601), 0)
                v1602 = self._record_first_value(recs.get(1602), 0)
                v1605 = self._record_first_value(recs.get(1605), 0)
                slot_key_text = self._mastery_hash_display(slotinfo, "slotinfo")
                row = [slot, "—", slot_key_text, "Board Slot Key", "—", f"1602={v1602} / 1605={self._hash_hex_or_dash(v1605)}", slot_key_text, self._hash_hex_or_dash(v1605), unit_id]
                meta = {"mode": mode, "unit_id": unit_id, "slot": slot, "socket": None, "char_unit": derived_char, "fields": recs, "slotinfo": slotinfo, "mastery": v1605, "state": v1602, "slotinfo_rec": recs.get(1601), "mastery_rec": None, "state_rec": None, "v1602_rec": recs.get(1602), "v1605_rec": recs.get(1605)}
                maybe_add(row, meta, [self._hash_hex_or_dash(slotinfo), self._hash_hex_or_dash(v1605)])
        elif mode == "overmastery":
            unit_ids = sorted(uid for uid, fields in grouped.items() if ((char_unit < 0 and int(uid) < 100000000) or (int(uid) // 1000 == char_unit and int(uid) < 100000000)) and any(fid in fields for fid in (1606, 1607)))
            for unit_id in unit_ids:
                recs = grouped.get(unit_id, {})
                derived_char = int(unit_id) // 1000 if char_unit < 0 else char_unit
                slot = int(unit_id) - int(derived_char) * 1000
                mastery = self._record_first_value(recs.get(1606), 0)
                state = self._record_first_value(recs.get(1607), 0)
                effect_name = self._mastery_effect_name(mastery)
                gbid = self._mastery_effect_gbid(mastery)
                row = [slot + 1, "—", effect_name if not gbid else f"{effect_name} ({gbid})", "Overmastery", "Yes" if self._mastery_state_display(state, mastery) == "Active" else self._mastery_state_display(state, mastery), self._mastery_mod_state_label(state, mastery), "—", self._hash_hex_or_dash(mastery), unit_id]
                meta = {"mode": mode, "unit_id": unit_id, "slot": slot, "socket": None, "char_unit": derived_char, "fields": recs, "slotinfo": 0, "mastery": mastery, "state": state, "slotinfo_rec": None, "mastery_rec": recs.get(1606), "state_rec": recs.get(1607)}
                maybe_add(row, meta, [gbid, self._hash_hex_or_dash(mastery)])
        else:
            unit_ids = sorted(uid for uid, fields in grouped.items() if ((char_unit < 0 and int(uid) >= 100000000) or (int(uid) // 10000 == char_unit and int(uid) >= 100000000)) and any(fid in fields for fid in (1606, 1607)))
            for unit_id in unit_ids:
                recs = grouped.get(unit_id, {})
                derived_char = int(unit_id) // 10000 if char_unit < 0 else char_unit
                rem = int(unit_id) - int(derived_char) * 10000
                slot = rem // 10
                socket = rem % 10
                # Do not skip socket/raw-suffix values above 2.  Discord/Save Wizard
                # notes show large jumps where the last digit is not always a
                # normal 0-2 socket; those rows can still be valid collection
                # or weapon mastery rows.
                mastery = self._record_first_value(recs.get(1606), 0)
                state = self._record_first_value(recs.get(1607), 0)
                board_unit = int(derived_char) * 1000 + slot
                board_recs = grouped.get(board_unit, {})
                slotinfo = self._record_first_value(board_recs.get(1601), 0)
                gbid = self._mastery_effect_gbid(mastery)
                effect_name = self._mastery_effect_name(mastery)
                type_text = self._mastery_effect_category(mastery, slotinfo)
                state_text = self._mastery_state_display(state, mastery)
                slot_key_text = self._mastery_hash_display(slotinfo, "slotinfo")
                socket_display = socket + 1 if 0 <= int(socket) <= 2 else f"raw {socket}"
                row = [slot + 1, socket_display, effect_name if not gbid else f"{effect_name} ({gbid})", type_text, "Yes" if state_text == "Active" else ("No" if state_text == "Inactive" else state_text), self._mastery_mod_state_label(state, mastery), slot_key_text, self._hash_hex_or_dash(mastery), unit_id]
                meta = {"mode": mode, "unit_id": unit_id, "board_unit": board_unit, "slot": slot, "socket": socket, "char_unit": derived_char, "fields": recs, "board_fields": board_recs, "slotinfo": slotinfo, "mastery": mastery, "state": state, "slotinfo_rec": board_recs.get(1601), "mastery_rec": recs.get(1606), "state_rec": recs.get(1607)}
                maybe_add(row, meta, [gbid, self._hash_hex_or_dash(mastery), self._hash_hex_or_dash(slotinfo)])

        self.mastery_slot_rows_meta = metas
        self.mastery_slot_model.set_rows(rows[:800])
        self.mastery_slot_rows_meta = self.mastery_slot_rows_meta[:800]
        if hasattr(self, "mastery_slot_summary_label"):
            name = self.mastery_character_combo.currentText() if hasattr(self, "mastery_character_combo") else f"unit {char_unit}"
            mode_name = {"effects": "Mastery Effects", "board": "Board Slot Keys", "overmastery": "Overmastery Slots"}.get(mode, mode)
            self.mastery_slot_summary_label.setText(f"{name} · {mode_name}: showing {len(self.mastery_slot_rows_meta)} row(s). 1606 = effect label/hash, 1607 = amount/state, 1601 = slot key/layout.")
        if hasattr(self, "mastery_slot_table"):
            self._set_table_widths(self.mastery_slot_table, {0: 70, 1: 70, 2: 330, 3: 150, 4: 75, 5: 85, 6: 270, 7: 125, 8: 110})
        self.update_mastery_slot_detail()

    def _selected_mastery_slot_meta(self) -> Optional[Dict[str, Any]]:
        table = getattr(self, "mastery_slot_table", None)
        if table is None:
            return None
        idx = table.currentIndex()
        if not idx.isValid() or idx.row() >= len(getattr(self, "mastery_slot_rows_meta", [])):
            QMessageBox.information(self, "No mastery row selected", "Select a mastery row first.")
            return None
        return self.mastery_slot_rows_meta[idx.row()]

    def update_mastery_slot_detail(self) -> None:
        label = getattr(self, "mastery_slot_detail_label", None)
        if label is None:
            return
        table = getattr(self, "mastery_slot_table", None)
        idx = table.currentIndex() if table is not None else QModelIndex()
        if not idx.isValid() or idx.row() >= len(getattr(self, "mastery_slot_rows_meta", [])):
            label.setText("Select a mastery row to see its readable effect name, unit formula, slot key, and raw hashes.")
            return
        meta = self.mastery_slot_rows_meta[idx.row()]
        slotinfo = self._record_first_value(meta.get("slotinfo_rec"), meta.get("slotinfo", 0))
        mastery = self._record_first_value(meta.get("mastery_rec"), meta.get("mastery", 0))
        state = self._record_first_value(meta.get("state_rec"), meta.get("state", 0))
        if hasattr(self, "mastery_slotinfo_edit"):
            self.mastery_slotinfo_edit.setText("" if slotinfo in (0, EMPTY_HASH) else f"0x{slotinfo & 0xFFFFFFFF:08X}")
        if hasattr(self, "mastery_id_edit"):
            self.mastery_id_edit.setText("" if mastery in (0, EMPTY_HASH) else f"0x{mastery & 0xFFFFFFFF:08X}")
        if hasattr(self, "mastery_state_edit"):
            self.mastery_state_edit.setText(str(state))
        missing = []
        if meta.get("slotinfo_rec") is None:
            missing.append("1601 slot key")
        if meta.get("mastery_rec") is None:
            missing.append("1606 effect")
        if meta.get("state_rec") is None:
            missing.append("1607 state")
        missing_text = f"\nMissing editable records in this view: {', '.join(missing)}" if missing else ""
        effect_name = self._mastery_effect_name(mastery)
        gbid = self._mastery_effect_gbid(mastery) or "unmapped"
        socket_text = "" if meta.get("socket") is None else f" · Socket {int(meta.get('socket')) + 1}"
        label.setText(
            f"Character unit {meta.get('char_unit')} · Slot {int(meta.get('slot', 0)) + 1}{socket_text}\n"
            f"View: {meta.get('mode')} · Unit: {meta.get('unit_id')}\n"
            f"Effect: {effect_name}\n"
            f"Group: {self._mastery_effect_category(mastery, slotinfo)} · Active: {self._mastery_state_display(state, mastery)}\n"
            f"GBID: {gbid} · 1606 effect hash: {self._hash_hex_or_dash(mastery)}\n"
            f"1601 slot key: {self._mastery_hash_display(slotinfo, 'slotinfo')}"
            f"{missing_text}"
        )

    def apply_mastery_slot_inline_edits(self) -> None:
        if not self.save:
            return
        meta = self._selected_mastery_slot_meta()
        if not meta:
            return
        changed = 0
        slotinfo_text = getattr(self, "mastery_slotinfo_edit", None).text().strip() if hasattr(self, "mastery_slotinfo_edit") else ""
        mastery_text = getattr(self, "mastery_id_edit", None).text().strip() if hasattr(self, "mastery_id_edit") else ""
        state_text = getattr(self, "mastery_state_edit", None).text().strip() if hasattr(self, "mastery_state_edit") else ""
        if slotinfo_text and meta.get("slotinfo_rec") is not None:
            value = self._resolve_hash_from_text(slotinfo_text)
            if value is None:
                QMessageBox.warning(self, "Invalid Slot Key", f"Could not resolve Slot Key value: {slotinfo_text}")
                return
            changed += 1 if self._set_record_first_value(meta.get("slotinfo_rec"), value, "Mastery Slot Key 1601") else 0
        if mastery_text and meta.get("mastery_rec") is not None:
            value = self._resolve_hash_from_text(mastery_text)
            if value is None:
                QMessageBox.warning(self, "Invalid Mastery Effect", f"Could not resolve Mastery Effect value: {mastery_text}")
                return
            changed += 1 if self._set_record_first_value(meta.get("mastery_rec"), value, "Mastery Effect 1606") else 0
        if state_text and meta.get("state_rec") is not None:
            state_value = self._clamp_i32_value(state_text, minimum=0, maximum=I32_MAX, label="Mastery State 1607")
            if state_value is None:
                QMessageBox.warning(self, "Invalid State", f"Could not parse State 1607 value: {state_text}")
                return
            changed += 1 if self._set_record_first_value(meta.get("state_rec"), int(state_value), "Mastery State 1607") else 0
        self.refresh_mastery_slot_rows()
        if changed:
            self._after_editor_patch(f"Updated {changed} mastery value(s) in memory.")
        else:
            self.statusBar().showMessage("No editable mastery values changed for this row/view.", 3500)

    def install_mastery_slot_test_pair(self) -> None:
        if not self.save:
            return
        meta = self._selected_mastery_slot_meta()
        if not meta:
            return
        changed = 0
        # The reported test pair is split across the board slot row and the selected effect row.
        # Board slot key: unit = character_unit * 1000 + slot, id 1601.
        # Effect: unit = character_unit * 10000 + slot * 10 + socket, ids 1606/1607.
        slotinfo_rec = meta.get("slotinfo_rec")
        mastery_rec = meta.get("mastery_rec")
        state_rec = meta.get("state_rec")
        if slotinfo_rec is None:
            board_unit = int(meta.get("char_unit", 10000)) * 1000 + int(meta.get("slot", 0))
            slotinfo_rec = self.save.find_first("uint", 1601, board_unit)
        if mastery_rec is None:
            effect_unit = int(meta.get("char_unit", 10000)) * 10000 + int(meta.get("slot", 0)) * 10 + int(meta.get("socket") or 0)
            mastery_rec = self.save.find_first("uint", 1606, effect_unit)
            state_rec = self.save.find_first("int", 1607, effect_unit)
        changed += 1 if self._set_record_first_value(slotinfo_rec, 0x280B6CB0, "Mastery Slot Key 1601") else 0
        changed += 1 if self._set_record_first_value(mastery_rec, 0x7B727910, "Mastery Effect 1606") else 0
        changed += 1 if self._set_record_first_value(state_rec, 1, "Mastery State 1607") else 0
        self.refresh_mastery_slot_rows()
        if changed:
            self._after_editor_patch(f"Installed reported mastery pair on slot {int(meta.get('slot', 0)) + 1} ({changed} values).")
        else:
            self.statusBar().showMessage("Could not find editable 1601/1606/1607 records for the selected mastery row.", 4500)

    def _mastery_effect_record(self, char_unit: int, slot: int, socket: int):
        if not self.save:
            return None
        unit = int(char_unit) * 10000 + int(slot) * 10 + int(socket)
        return self.save.find_first("uint", 1606, unit)

    def _mastery_state_record(self, char_unit: int, slot: int, socket: int):
        if not self.save:
            return None
        unit = int(char_unit) * 10000 + int(slot) * 10 + int(socket)
        return self.save.find_first("int", 1607, unit) or self.save.find_first("uint", 1607, unit)

    def _mastery_over_record(self, char_unit: int, slot: int):
        if not self.save:
            return None
        unit = int(char_unit) * 1000 + int(slot)
        return self.save.find_first("uint", 1606, unit)

    def _mastery_over_state_record(self, char_unit: int, slot: int):
        if not self.save:
            return None
        unit = int(char_unit) * 1000 + int(slot)
        return self.save.find_first("int", 1607, unit) or self.save.find_first("uint", 1607, unit)

    def _apply_skiller_op_to_character_unit(self, char_unit: int) -> int:
        """Apply the OP mastery pattern from the shared Cheats_Mods sheet/test saves.

        The current mapped save model exposes the editable mastery/effect value at
        field 1606 and the active/value amount at field 1607. The uploaded
        before/after saves showed only 1606 changing for the broad OP pattern,
        while the Save Wizard sheet maps the human-readable IDs used here.

        This only patches existing scalar records. It does not create rows,
        resize arrays, or rebuild the FlatBuffer.
        """
        if not self.save:
            return 0
        changed = 0
        max_value = 1023  # 0x03FF; sheet notes this as the max OP amount.

        # Four OP overmastery slots seen in the working save/code notes.
        # Slot 1 = Critical Rate, Slot 2 = Normal Damage Cap Up,
        # Slot 3 = SBA Damage Cap Up, Slot 4 = Skill Damage Cap Up.
        over_values = [0x45C65767, 0x43B7581D, 0x4A4C093D, 0x9C555433]
        for slot, value in enumerate(over_values):
            changed += 1 if self._set_record_first_value(self._mastery_over_record(char_unit, slot), value, "OP overmastery 1606") else 0
            changed += 1 if self._set_record_first_value(self._mastery_over_state_record(char_unit, slot), max_value, "OP overmastery state 1607") else 0

        # Exact effect layout observed in Final_change.sav and aligned with the
        # Cheats_Mods Masteries_SlotINFO ranges. We address the parsed records
        # by logical slot/socket rather than raw Save Wizard byte offsets.
        # This OP pattern intentionally does not touch sigil equipment/slot data.
        pattern_ranges = [
            (0, 99, 0x9A97C049, max_value),      # Skill Damage Up
            (100, 299, 0xC4925BD7, max_value),  # Attack Power Up
            (300, 349, 0x6CB38EF3, max_value),  # Stun Power Up
            (350, 399, 0x4E42646B, max_value),  # SBA Damage Up
            (400, 499, 0x45C65767, max_value),  # Critical Rate
            (500, 599, 0x52A207B5, max_value),  # Health Up
        ]
        for start_index, end_index, value, state_value in pattern_ranges:
            for idx in range(int(start_index), int(end_index) + 1):
                slot = idx // 3
                socket = idx % 3
                rec = self._mastery_effect_record(char_unit, slot, socket)
                if rec is not None:
                    changed += 1 if self._set_record_first_value(rec, value, "OP mastery effect 1606") else 0
                state_rec = self._mastery_state_record(char_unit, slot, socket)
                if state_rec is not None:
                    changed += 1 if self._set_record_first_value(state_rec, state_value, "OP mastery effect state 1607") else 0
        return changed

    def install_mastery_skiller_op_selected(self) -> None:
        if not self.save:
            return
        combo = getattr(self, "mastery_character_combo", None)
        char_unit = int(combo.currentData()) if combo is not None and combo.currentData() is not None else 10000
        changed = self._apply_skiller_op_to_character_unit(char_unit)
        self.refresh_mastery_slot_rows()
        if changed:
            self._after_editor_patch(f"Installed experimental OP mastery pattern on {combo.currentText() if combo else 'selected character'} ({changed} values).")
        else:
            self.statusBar().showMessage("No editable mastery records were found for the selected character.", 5000)

    def install_mastery_skiller_op_all(self) -> None:
        if not self.save:
            return
        if QMessageBox.question(self, "Install OP preset to all characters", "Install the experimental OP mastery pattern to every detected mastery character group?\n\nUse Save As before testing this in game.") != QMessageBox.StandardButton.Yes:
            return
        changed = 0
        for choice in self._mastery_character_choices():
            try:
                changed += self._apply_skiller_op_to_character_unit(int(choice.get("unit", 0)))
            except Exception:
                continue
        self.refresh_mastery_slot_rows()
        if changed:
            self._after_editor_patch(f"Installed experimental OP mastery pattern across detected characters ({changed} values).")
        else:
            self.statusBar().showMessage("No editable mastery records were found for detected characters.", 5000)

    def copy_selected_mastery_slot_pair(self) -> None:
        meta = self._selected_mastery_slot_meta()
        if not meta:
            return
        slotinfo = self._record_first_value(meta.get("slotinfo_rec"), meta.get("slotinfo", 0))
        mastery = self._record_first_value(meta.get("mastery_rec"), meta.get("mastery", 0))
        state = self._record_first_value(meta.get("state_rec"), meta.get("state", 0))
        socket = meta.get("socket")
        socket_text = "" if socket is None else f" Socket {int(socket) + 1}"
        text = f"CharacterUnit {meta.get('char_unit')} Slot {int(meta.get('slot', 0)) + 1}{socket_text} Unit {meta.get('unit_id')}: 1601=0x{slotinfo & 0xFFFFFFFF:08X}, 1606=0x{mastery & 0xFFFFFFFF:08X}, 1607={state}"
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage("Copied mastery values to clipboard.", 3500)

    def use_selected_character_for_mastery_slots(self) -> None:
        meta = self._selected_meta(self.character_table, self.character_rows_meta)
        if not meta:
            return
        slot = int(meta.get("slot", 0))
        target_unit = 10000 + slot
        combo = getattr(self, "mastery_character_combo", None)
        if combo is None:
            return
        for i in range(combo.count()):
            if int(combo.itemData(i)) == target_unit:
                combo.setCurrentIndex(i)
                self.refresh_mastery_slot_rows()
                return
        QMessageBox.information(self, "No matching mastery group", f"No Mastery group was found for character unit {target_unit}.")

    def export_mastery_slots_csv(self) -> None:
        self._export_simple_rows("mastery_slots", self.mastery_slot_model.headers, self.mastery_slot_model.rows)

    def _show_sigil_tab(self, index: int) -> None:
        tabs = getattr(self, "sigil_tabs", None)
        if tabs is None:
            return
        try:
            tabs.setCurrentIndex(int(index))
            self._refresh_current_sigil_tab()
        except Exception:
            pass

    def _refresh_current_sigil_tab(self) -> None:
        tabs = getattr(self, "sigil_tabs", None)
        idx = tabs.currentIndex() if tabs is not None else 0
        if idx == 0:
            self.refresh_sigil_rows()
        elif idx == 1:
            self.refresh_sigil_database_rows()
        elif idx == 2:
            self.refresh_sigil_empty_slot_rows()

    def show_empty_sigils_in_current_table(self) -> None:
        if hasattr(self, "sigil_show_empty_check"):
            self.sigil_show_empty_check.setChecked(True)
        self._show_sigil_tab(0)
        self.refresh_sigil_rows()

    def _sigil_existing_hash_counts(self) -> Dict[int, int]:
        counts: Dict[int, int] = {}
        if not self.save:
            return counts
        try:
            grouped = self.save.group_by_unit([2703])
        except Exception:
            return counts
        for fields in grouped.values():
            value = self._record_first_value(fields.get(2703), 0)
            try:
                h = int(value or 0) & 0xFFFFFFFF
            except Exception:
                continue
            if h in (0, EMPTY_HASH):
                continue
            counts[h] = counts.get(h, 0) + 1
        return counts

    def _sigil_grade_label(self, entry: Any) -> str:
        name = str(getattr(entry, "display_name", "") or "")
        gbid = str(getattr(entry, "item_id", "") or "").upper()
        if name.endswith(" V+") or gbid.endswith("_14"):
            return "V+"
        if name.endswith(" V") or gbid.endswith("_04"):
            return "V"
        if name.endswith(" IV") or gbid.endswith("_03"):
            return "IV"
        if name.endswith(" III") or gbid.endswith("_02"):
            return "III"
        if name.endswith(" II") or gbid.endswith("_01"):
            return "II"
        if name.endswith(" I") or gbid.endswith("_00"):
            return "I"
        return ""

    def _sigil_database_entries(self) -> List[Any]:
        entries = []
        seen: set[int] = set()
        for entry in getattr(self.item_db, "by_hash", {}).values():
            try:
                cat = str(getattr(entry, "category", "") or "").strip().lower()
                gbid = str(getattr(entry, "item_id", "") or "").strip().upper()
                name = str(getattr(entry, "display_name", "") or "").strip()
                # Seed/import data categorizes these as "Sigil / Gem". The page is
                # now user-facing "Sigils", so accept both plus the GEEN_* GBID
                # family as a robust fallback.
                is_sigil = cat in {"sigil", "sigil / gem", "sigils", "gem"} or gbid.startswith("GEEN_")
                if not is_sigil:
                    continue
                h = int(getattr(entry, "hash_value", 0)) & 0xFFFFFFFF
                if h in seen or h in (0, EMPTY_HASH):
                    continue
                seen.add(h)
                entries.append(entry)
            except Exception:
                continue
        entries.sort(key=lambda e: (str(getattr(e, "display_name", "")).lower(), str(getattr(e, "item_id", "")).lower()))
        return entries

    def refresh_sigil_database_rows(self) -> None:
        if not hasattr(self, "sigil_database_model"):
            return
        q = self.sigil_database_filter_edit.text().strip().lower() if hasattr(self, "sigil_database_filter_edit") else ""
        mode = self.sigil_database_grade_combo.currentText() if hasattr(self, "sigil_database_grade_combo") else "All sigils"
        owned_counts = self._sigil_existing_hash_counts()
        empty_slots = self.count_empty_sigil_slots() if self.save else 0
        rows: List[List[Any]] = []
        meta: List[Dict[str, Any]] = []
        for entry in self._sigil_database_entries():
            h = int(getattr(entry, "hash_value", 0)) & 0xFFFFFFFF
            name = str(getattr(entry, "display_name", "") or "")
            gbid = str(getattr(entry, "item_id", "") or "")
            grade = self._sigil_grade_label(entry)
            owned = owned_counts.get(h, 0)
            status = "Owned" if owned else "Missing"
            haystack = f"{name} {gbid} 0x{h:08X} {grade} {status}".lower()
            if q and not all(tok in haystack for tok in q.replace(",", " ").split() if tok):
                continue
            if mode == "V / V+ only" and grade not in {"V", "V+"}:
                continue
            if mode == "V only" and grade != "V":
                continue
            if mode == "V+ only" and grade != "V+":
                continue
            if mode == "Missing only" and owned:
                continue
            if mode == "Owned only" and not owned:
                continue
            rows.append([
                status,
                name,
                gbid,
                f"0x{h:08X}",
                grade,
                owned,
                empty_slots,
                "Double-click or Add Selected",
            ])
            meta.append({"hash": h, "name": name, "gbid": gbid, "grade": grade, "owned": owned})
        self.sigil_database_rows_meta = meta
        self.sigil_database_model.set_rows(rows)
        if hasattr(self, "sigil_database_table"):
            self._set_table_widths(self.sigil_database_table, {0: 90, 1: 360, 2: 170, 3: 120, 4: 70, 5: 70, 6: 90, 7: 180})
        if hasattr(self, "sigil_database_status"):
            total_known = len(self._sigil_database_entries())
            if total_known == 0:
                self.sigil_database_status.setText("Database: 0 sigils loaded. Check sigil seed CSV files or downloaded sheet merge data.")
            else:
                self.sigil_database_status.setText(
                    f"Database: {self.format_value(len(rows))} visible sigil(s) · "
                    f"{self.format_value(total_known)} known total · "
                    f"{self.format_value(empty_slots)} reusable empty slot(s)."
                )

    def refresh_sigil_empty_slot_rows(self) -> None:
        if not hasattr(self, "sigil_empty_model"):
            return
        if not self.save:
            self.sigil_empty_model.set_rows([])
            self.sigil_empty_rows_meta = []
            if hasattr(self, "sigil_empty_status"):
                self.sigil_empty_status.setText("Open a save to list reusable empty sigil slots.")
            return
        rows: List[List[Any]] = []
        meta: List[Dict[str, Any]] = []
        try:
            grouped = self.save.group_by_unit([2702, 2703, 2704, 2706, 2707])
        except Exception:
            grouped = {}
        for unit_id, fields in sorted(grouped.items()):
            gem = self.value1(fields.get(2703), 0)
            is_empty = gem in ("", 0, EMPTY_HASH)
            if not is_empty:
                continue
            slot_rec = fields.get(2702)
            level_rec = self._sigil_level_record_for_hash_record(fields.get(2703), fields)
            owner_value = self.value1(fields.get(2706), 0)
            flag_value = self.value1(fields.get(2707), "")
            rows.append([
                unit_id,
                self.value1(slot_rec, ""),
                self.value1(level_rec, ""),
                "" if owner_value in ("", 0, EMPTY_HASH) else self.format_value(owner_value),
                flag_value,
                "Reusable empty sigil slot",
            ])
            meta.append({
                "unit_id": unit_id,
                "slot_rec": slot_rec,
                "hash_rec": fields.get(2703),
                "level_rec": level_rec,
                "worn_rec": fields.get(2706),
                "flags_rec": fields.get(2707),
            })
        self.sigil_empty_rows_meta = meta
        self.sigil_empty_model.set_rows(rows)
        if hasattr(self, "sigil_empty_table"):
            self._set_table_widths(self.sigil_empty_table, {0: 110, 1: 100, 2: 90, 3: 160, 4: 100, 5: 280})
        if hasattr(self, "sigil_empty_status"):
            self.sigil_empty_status.setText(f"Empty sigil slots: {self.format_value(len(rows))} reusable slot(s) found.")

    def update_sigil_database_status(self) -> None:
        if not hasattr(self, "sigil_database_status"):
            return
        meta = self._selected_sigil_database_meta(show_status=False)
        empty_slots = self.count_empty_sigil_slots() if self.save else 0
        if not meta:
            self.sigil_database_status.setText(f"Select a sigil to add. Empty slots available: {self.format_value(empty_slots)}.")
            return
        self.sigil_database_status.setText(
            f"Selected: {meta.get('name')} ({meta.get('gbid')}) · {format_hash_value(meta.get('hash'))} · "
            f"owned {self.format_value(meta.get('owned', 0))} · empty slots {self.format_value(empty_slots)}."
        )

    def _selected_sigil_database_meta(self, show_status: bool = True) -> Optional[Dict[str, Any]]:
        table = getattr(self, "sigil_database_table", None)
        rows_meta = getattr(self, "sigil_database_rows_meta", [])
        if table is None:
            return None
        idx = table.currentIndex()
        if not idx.isValid() or idx.row() >= len(rows_meta):
            if show_status:
                self.statusBar().showMessage("Select a sigil from the database first.", 2500)
            return None
        return rows_meta[idx.row()]

    def add_selected_database_sigil_to_empty_slot(self) -> None:
        meta = self._selected_sigil_database_meta()
        if not meta:
            return
        if not self.save:
            self.statusBar().showMessage("Open a save before adding sigils.", 3000)
            return
        level = int(self.sigil_database_level_spin.value()) if hasattr(self, "sigil_database_level_spin") else SIGIL_LEVEL_MAX
        locked = bool(self.sigil_database_locked_check.isChecked()) if hasattr(self, "sigil_database_locked_check") else True
        result = self._add_sigil_hash_level_to_empty_slot(int(meta.get("hash", 0)) & 0xFFFFFFFF, level=level, locked=locked)
        if not result:
            self.statusBar().showMessage("No reusable empty sigil slot was available, or the slot could not be activated.", 5000)
            return
        self._after_editor_patch(f"Added sigil from database: {result}", refresh=False)
        self.refresh_sigil_rows()
        self.refresh_sigil_database_rows()
        self.refresh_sigil_empty_slot_rows()

    def add_selected_database_sigil_locked_to_empty_slot(self) -> None:
        if hasattr(self, "sigil_database_locked_check"):
            self.sigil_database_locked_check.setChecked(True)
        self.add_selected_database_sigil_to_empty_slot()

    def add_selected_database_sigil_unlocked_to_empty_slot(self) -> None:
        if hasattr(self, "sigil_database_locked_check"):
            self.sigil_database_locked_check.setChecked(False)
        self.add_selected_database_sigil_to_empty_slot()

    def _apply_sigil_column_visibility(self) -> None:
        if not hasattr(self, "sigil_table"):
            return
        show_technical = bool(getattr(getattr(self, "sigil_show_technical_check", None), "isChecked", lambda: False)())
        # Normal view keeps the useful columns visible: slot, name, level, equipped-to, flags.
        # Technical view exposes Unit/GBID/hash values for reverse-engineering and manual patching.
        for col in (0, 3, 4, 7):
            self.sigil_table.setColumnHidden(col, not show_technical)
        try:
            self.sigil_table.setColumnWidth(1, 70)    # Slot
            self.sigil_table.setColumnWidth(2, 360)   # Sigil
            self.sigil_table.setColumnWidth(5, 80)    # Level
            self.sigil_table.setColumnWidth(6, 340)   # Equipped To
            self.sigil_table.setColumnWidth(8, 150)   # Flags
        except Exception:
            pass

    def refresh_sigil_rows(self) -> None:
        if not self.save:
            self.sigil_model.set_rows([])
            self.sigil_rows_meta = []
            if hasattr(self, "sigil_empty_model"):
                self.sigil_empty_model.set_rows([])
                self.sigil_empty_rows_meta = []
            if hasattr(self, "sigil_count_label"):
                self.sigil_count_label.setText("Open a save to inspect sigil slots.")
            if hasattr(self, "sigil_database_status"):
                self.refresh_sigil_database_rows()
            return
        grouped = self.save.group_by_unit([2702, 2703, 2704, 2706, 2707])
        show_empty = bool(getattr(getattr(self, "sigil_show_empty_check", None), "isChecked", lambda: False)())
        known_only = bool(getattr(getattr(self, "sigil_known_only_check", None), "isChecked", lambda: False)())
        unknown_only = bool(getattr(getattr(self, "sigil_unknown_only_check", None), "isChecked", lambda: False)())
        invalid_owner_only = bool(getattr(getattr(self, "sigil_invalid_owner_only_check", None), "isChecked", lambda: False)())
        valid_owner_hashes = {int(c.get("hash", 0)) & 0xFFFFFFFF for c in getattr(self, "character_owner_choices", [])}
        valid_owner_hashes.update({0, EMPTY_HASH})
        invalid_owner_count = 0
        rows: List[List[Any]] = []
        meta_rows: List[Dict[str, Any]] = []
        total_slots = active_slots = known_slots = unknown_slots = empty_slots = 0
        q = getattr(self, "sigil_filter_edit", None).text().strip().lower() if hasattr(self, "sigil_filter_edit") else ""
        for unit_id, fields in sorted(grouped.items()):
            total_slots += 1
            gem = self.value1(fields.get(2703), 0)
            is_empty = gem in ("", 0, 0x887AE0B0)
            if is_empty:
                empty_slots += 1
            else:
                active_slots += 1
            if is_empty and not show_empty:
                continue
            s_name, s_gbid, s_hash = ("<Empty sigil slot>", "", "") if is_empty else self.hash_entry_parts(gem)
            is_known = bool(s_gbid)
            if is_empty:
                pass
            elif is_known:
                known_slots += 1
            else:
                unknown_slots += 1
            if known_only and (is_empty or not is_known):
                continue
            if unknown_only and (is_empty or is_known):
                continue
            worn_value = self.value1(fields.get(2706), 0)
            try:
                worn_int = int(worn_value or 0) & 0xFFFFFFFF
            except Exception:
                worn_int = 0
            invalid_owner = (not is_empty) and worn_int not in valid_owner_hashes
            if invalid_owner:
                invalid_owner_count += 1
            if invalid_owner_only and not invalid_owner:
                continue
            worn_display = "" if worn_int in (0, EMPTY_HASH) else self._character_owner_name_for_hash(worn_int)
            worn_gbid = self._character_owner_gbid_for_hash(worn_int)
            worn_hash = "" if worn_int in (0, EMPTY_HASH) else f"0x{worn_int:08X}"
            hash_rec = fields.get(2703)
            level_rec = self._sigil_level_record_for_hash_record(hash_rec, fields)
            row = [
                unit_id,
                self.value1(fields.get(2702), ""),
                s_name,
                s_gbid,
                s_hash,
                self.value1(level_rec, ""),
                worn_display,
                worn_gbid,
                self.value1(fields.get(2707), ""),
            ]
            if not self._matches_editor_filter(row, q):
                continue
            rows.append(row)
            meta_rows.append({
                "unit_id": unit_id,
                "is_empty": is_empty,
                "is_known": is_known,
                "slot_rec": fields.get(2702),
                "hash_rec": hash_rec,
                "level_rec": level_rec,
                "level_pair_note": self._sigil_level_pair_note(hash_rec, level_rec),
                "worn_rec": fields.get(2706),
                "flags_rec": fields.get(2707),
                "invalid_owner": invalid_owner,
            })
        self.sigil_rows_meta = meta_rows
        self.sigil_model.set_rows(rows)
        if hasattr(self, "sigil_count_label"):
            hint = ""
            if unknown_slots:
                hint = " · use Show Unknown to isolate unmapped hashes"
            if invalid_owner_count:
                hint += f" · {self.format_value(invalid_owner_count)} invalid owner reference(s)"
            self.sigil_count_label.setText(
                f"Sigil slots: {self.format_value(active_slots)} active / {self.format_value(total_slots)} total · "
                f"{self.format_value(known_slots)} known · {self.format_value(unknown_slots)} unknown · "
                f"{self.format_value(empty_slots)} empty addable · showing {self.format_value(len(rows))}{hint}"
            )
        if hasattr(self, "sigil_table"):
            self._apply_sigil_column_visibility()
            self._set_table_widths(self.sigil_table, {1: 74, 2: 360, 3: 170, 4: 120, 5: 64, 6: 230, 7: 170, 8: 115})
        if hasattr(self, "sigil_empty_model"):
            self.refresh_sigil_empty_slot_rows()
        if hasattr(self, "sigil_database_model"):
            self.refresh_sigil_database_rows()
        self.update_sigil_detail()

    def clear_weapon_filters(self) -> None:
        for name in ("weapon_filter_edit", "weapon_database_filter_edit"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.clear()
        for name in ("weapon_known_only_check", "weapon_unknown_only_check", "weapon_show_empty_check"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setChecked(False)
        combo = getattr(self, "weapon_database_filter_combo", None)
        if combo is not None:
            combo.setCurrentIndex(0)
        self.refresh_weapon_rows()
        if hasattr(self, "weapon_database_model"):
            self.refresh_weapon_database_rows()

    def _show_weapon_tab(self, index: int) -> None:
        tabs = getattr(self, "weapon_tabs", None)
        if tabs is None:
            return
        try:
            tabs.setCurrentIndex(int(index))
            self._refresh_current_weapon_tab()
        except Exception:
            pass

    def _refresh_current_weapon_tab(self) -> None:
        tabs = getattr(self, "weapon_tabs", None)
        idx = tabs.currentIndex() if tabs is not None else 0
        if idx == 0:
            self.refresh_weapon_rows()
        elif idx == 1:
            self.refresh_weapon_database_rows()
        elif idx == 2:
            self.refresh_weapon_empty_slot_rows()

    def show_empty_weapons_in_current_table(self) -> None:
        if hasattr(self, "weapon_show_empty_check"):
            self.weapon_show_empty_check.setChecked(True)
        self._show_weapon_tab(0)
        self.refresh_weapon_rows()

    def _weapon_existing_hash_counts(self) -> Dict[int, int]:
        counts: Dict[int, int] = {}
        if not self.save:
            return counts
        try:
            grouped = self.save.group_by_unit([2803])
        except Exception:
            return counts
        for fields in grouped.values():
            value = self._record_first_value(fields.get(2803), 0)
            try:
                h = int(value or 0) & 0xFFFFFFFF
            except Exception:
                continue
            if h in (0, EMPTY_HASH):
                continue
            counts[h] = counts.get(h, 0) + 1
        return counts

    def _weapon_database_entries(self) -> List[Any]:
        entries = []
        seen: set[int] = set()
        for entry in getattr(self.item_db, "by_hash", {}).values():
            try:
                cat = str(getattr(entry, "category", "") or "").strip().lower()
                gbid = str(getattr(entry, "item_id", "") or "").strip().upper()
                if cat != "weapon" and not gbid.startswith("WEP_"):
                    continue
                h = int(getattr(entry, "hash_value", 0)) & 0xFFFFFFFF
                if h in seen or h in (0, EMPTY_HASH):
                    continue
                seen.add(h)
                entries.append(entry)
            except Exception:
                continue
        entries.sort(key=lambda e: (str(getattr(e, "item_id", "")).upper(), str(getattr(e, "display_name", "")).lower()))
        return entries

    def _weapon_owner_label(self, gbid: str) -> str:
        gbid = str(gbid or "").upper()
        m = re.match(r"^WEP_([A-Z]+)(\d{4})_", gbid)
        if not m:
            return ""
        prefix, slot = m.groups()
        if prefix == "PL":
            return f"Playable {slot}"
        if prefix == "NP":
            return f"NPC / Reserved {slot}"
        return f"{prefix} {slot}"

    def refresh_weapon_database_rows(self) -> None:
        if not hasattr(self, "weapon_database_model"):
            return
        q = self.weapon_database_filter_edit.text().strip().lower() if hasattr(self, "weapon_database_filter_edit") else ""
        mode = self.weapon_database_filter_combo.currentText() if hasattr(self, "weapon_database_filter_combo") else "All weapons"
        owned_counts = self._weapon_existing_hash_counts()
        empty_slots = self.count_empty_weapon_slots() if self.save else 0
        entries = self._weapon_database_entries()
        rows: List[List[Any]] = []
        meta: List[Dict[str, Any]] = []
        for entry in entries:
            h = int(getattr(entry, "hash_value", 0)) & 0xFFFFFFFF
            name = str(getattr(entry, "display_name", "") or "")
            gbid = str(getattr(entry, "item_id", "") or "")
            owned = owned_counts.get(h, 0)
            status = "Owned" if owned else "Missing"
            owner = self._weapon_owner_label(gbid)
            haystack = f"{name} {gbid} 0x{h:08X} {owner} {status}".lower()
            if q and not all(tok in haystack for tok in q.replace(",", " ").split() if tok):
                continue
            if mode == "Missing only" and owned:
                continue
            if mode == "Owned only" and not owned:
                continue
            if mode == "Playable WEP_PL only" and not gbid.upper().startswith("WEP_PL"):
                continue
            if mode == "NPC / Reserved WEP_NP" and not gbid.upper().startswith("WEP_NP"):
                continue
            rows.append([
                status,
                name,
                gbid,
                f"0x{h:08X}",
                owner,
                owned,
                empty_slots,
                "Double-click or Add Selected",
            ])
            meta.append({"hash": h, "name": name, "gbid": gbid, "owner": owner, "owned": owned})
        self.weapon_database_rows_meta = meta
        self.weapon_database_model.set_rows(rows)
        if hasattr(self, "weapon_database_table"):
            self._set_table_widths(self.weapon_database_table, {0: 90, 1: 360, 2: 170, 3: 120, 4: 160, 5: 70, 6: 90, 7: 180})
        if hasattr(self, "weapon_database_status"):
            if not entries:
                self.weapon_database_status.setText("Database: 0 weapons loaded. Check weapon seed/database data.")
            else:
                self.weapon_database_status.setText(
                    f"Database: {self.format_value(len(rows))} visible weapon(s) · "
                    f"{self.format_value(len(entries))} known total · "
                    f"{self.format_value(empty_slots)} reusable empty slot(s)."
                )

    def refresh_weapon_empty_slot_rows(self) -> None:
        if not hasattr(self, "weapon_empty_model"):
            return
        if not self.save:
            self.weapon_empty_model.set_rows([])
            self.weapon_empty_rows_meta = []
            if hasattr(self, "weapon_empty_status"):
                self.weapon_empty_status.setText("Open a save to list reusable empty weapon slots.")
            return
        rows: List[List[Any]] = []
        meta: List[Dict[str, Any]] = []
        try:
            grouped = self.save.group_by_unit([2803, 2804, 2815, 2816])
        except Exception:
            grouped = {}
        for unit_id, fields in sorted(grouped.items()):
            hash_rec = fields.get(2803)
            xp_rec = fields.get(2804)
            if not hash_rec or not xp_rec:
                continue
            h = self._record_first_value(hash_rec, 0)
            xp = self._record_first_value(xp_rec, 0)
            if h not in (0, EMPTY_HASH) or xp != 0:
                continue
            flags_value = self.value1(fields.get(2815), "")
            stone_value = self.value1(fields.get(2816), "")
            rows.append([
                unit_id,
                self.format_value(h),
                xp,
                flags_value,
                stone_value,
                "Reusable empty weapon slot",
            ])
            meta.append({
                "unit_id": unit_id,
                "hash_rec": hash_rec,
                "xp_rec": xp_rec,
                "flags_rec": fields.get(2815),
                "stone_rec": fields.get(2816),
            })
        self.weapon_empty_rows_meta = meta
        self.weapon_empty_model.set_rows(rows)
        if hasattr(self, "weapon_empty_table"):
            self._set_table_widths(self.weapon_empty_table, {0: 110, 1: 150, 2: 110, 3: 110, 4: 150, 5: 280})
        if hasattr(self, "weapon_empty_status"):
            self.weapon_empty_status.setText(f"Empty weapon slots: {self.format_value(len(rows))} reusable slot(s) found.")

    def update_weapon_database_status(self) -> None:
        if not hasattr(self, "weapon_database_status"):
            return
        meta = self._selected_weapon_database_meta(show_status=False)
        empty_slots = self.count_empty_weapon_slots() if self.save else 0
        if not meta:
            self.weapon_database_status.setText(f"Select a weapon to add. Empty slots available: {self.format_value(empty_slots)}.")
            return
        self.weapon_database_status.setText(
            f"Selected: {meta.get('name')} ({meta.get('gbid')}) · {format_hash_value(meta.get('hash'))} · "
            f"owned {self.format_value(meta.get('owned', 0))} · empty slots {self.format_value(empty_slots)}."
        )

    def _selected_weapon_database_meta(self, show_status: bool = True) -> Optional[Dict[str, Any]]:
        table = getattr(self, "weapon_database_table", None)
        rows_meta = getattr(self, "weapon_database_rows_meta", [])
        if table is None:
            return None
        idx = table.currentIndex()
        if not idx.isValid() or idx.row() >= len(rows_meta):
            if show_status:
                self.statusBar().showMessage("Select a weapon from the database first.", 2500)
            return None
        return rows_meta[idx.row()]

    def add_selected_database_weapon_to_empty_slot(self) -> None:
        meta = self._selected_weapon_database_meta()
        if not meta:
            return
        if not self.save:
            self.statusBar().showMessage("Open a save before adding weapons.", 3000)
            return
        xp = int(self.weapon_database_xp_spin.value()) if hasattr(self, "weapon_database_xp_spin") else WEAPON_XP_MAX
        result = self._add_weapon_hash_xp_to_empty_slot(int(meta.get("hash", 0)) & 0xFFFFFFFF, xp=xp)
        if not result:
            self.statusBar().showMessage("No reusable empty weapon slot was available, or the slot could not be activated.", 5000)
            return
        self._after_editor_patch(f"Added weapon from database: {result}", refresh=False)
        self.refresh_weapon_rows()
        self.refresh_weapon_database_rows()
        self.refresh_weapon_empty_slot_rows()

    def add_selected_database_weapon_max_to_empty_slot(self) -> None:
        if hasattr(self, "weapon_database_xp_spin"):
            self.weapon_database_xp_spin.setValue(WEAPON_XP_MAX)
        self.add_selected_database_weapon_to_empty_slot()

    def add_all_missing_database_weapons_to_empty_slots(self) -> None:
        """Add every currently visible missing database weapon into reusable empty slots.

        Safety rule: NPC/reserved WEP_NP rows are skipped unless the database
        filter is explicitly set to "NPC / Reserved WEP_NP".
        """
        if not self.save:
            self.statusBar().showMessage("Open a save before adding weapons.", 3000)
            return
        if hasattr(self, "weapon_database_model"):
            self.refresh_weapon_database_rows()
        rows_meta = list(getattr(self, "weapon_database_rows_meta", []) or [])
        mode = self.weapon_database_filter_combo.currentText() if hasattr(self, "weapon_database_filter_combo") else "All weapons"
        empty_slots = self.count_empty_weapon_slots()
        if empty_slots <= 0:
            self.statusBar().showMessage("No reusable empty weapon slots are available.", 5000)
            return
        candidates: List[Dict[str, Any]] = []
        skipped_reserved = 0
        for meta in rows_meta:
            try:
                if int(meta.get("owned", 0) or 0) > 0:
                    continue
                gbid = str(meta.get("gbid", "") or "").upper()
                if gbid.startswith("WEP_NP") and mode != "NPC / Reserved WEP_NP":
                    skipped_reserved += 1
                    continue
                candidates.append(meta)
            except Exception:
                continue
        if not candidates:
            extra = " Reserved/NPC rows are skipped unless that filter is selected." if skipped_reserved else ""
            self.statusBar().showMessage("No visible missing weapons to add." + extra, 5000)
            return
        xp = int(self.weapon_database_xp_spin.value()) if hasattr(self, "weapon_database_xp_spin") else WEAPON_XP_MAX
        xp = self._clamp_weapon_xp_value(xp)
        added: List[str] = []
        for meta in candidates[:empty_slots]:
            result = self._add_weapon_hash_xp_to_empty_slot(int(meta.get("hash", 0)) & 0xFFFFFFFF, xp=xp)
            if result:
                added.append(result)
            else:
                break
        remaining = max(0, len(candidates) - len(added))
        msg = f"Added {len(added):,} missing weapon(s) at XP {xp:,}."
        if remaining:
            msg += f" {remaining:,} still missing because there were not enough empty slots."
        if skipped_reserved:
            msg += f" Skipped {skipped_reserved:,} NPC/reserved row(s)."
        self._after_editor_patch(msg, refresh=False)
        self.refresh_weapon_rows()
        self.refresh_weapon_database_rows()
        self.refresh_weapon_empty_slot_rows()

    def refresh_weapon_rows(self) -> None:
        if not self.save:
            self.weapon_model.set_rows([])
            self.weapon_rows_meta = []
            if hasattr(self, "weapon_empty_model"):
                self.weapon_empty_model.set_rows([])
                self.weapon_empty_rows_meta = []
            if hasattr(self, "weapon_count_label"):
                self.weapon_count_label.setText("Open a save to inspect weapon slots.")
            if hasattr(self, "weapon_database_model"):
                self.refresh_weapon_database_rows()
            return
        ids = [2803, 2804, 2805, 2806, 2807, 2814, 2815, 2816]
        grouped = self.save.group_by_unit(ids)
        show_empty = bool(getattr(getattr(self, "weapon_show_empty_check", None), "isChecked", lambda: False)())
        known_only = bool(getattr(getattr(self, "weapon_known_only_check", None), "isChecked", lambda: False)())
        unknown_only = bool(getattr(getattr(self, "weapon_unknown_only_check", None), "isChecked", lambda: False)())
        rows: List[List[Any]] = []
        meta_rows: List[Dict[str, Any]] = []
        total_slots = active_slots = known_slots = unknown_slots = empty_slots = 0
        q = getattr(self, "weapon_filter_edit", None).text().strip().lower() if hasattr(self, "weapon_filter_edit") else ""
        for unit_id, fields in sorted(grouped.items()):
            total_slots += 1
            wid = self.value1(fields.get(2803), 0)
            is_empty = wid in ("", 0, 0x887AE0B0)
            if is_empty:
                empty_slots += 1
            else:
                active_slots += 1
            if is_empty and not show_empty:
                continue
            w_name, w_gbid, w_hash = ("<Empty weapon slot>", "", "") if is_empty else self.hash_entry_parts(wid)
            is_known = bool(w_gbid)
            if is_empty:
                pass
            elif is_known:
                known_slots += 1
            else:
                unknown_slots += 1
            if known_only and (is_empty or not is_known):
                continue
            if unknown_only and (is_empty or is_known):
                continue
            stone_value = self.value1(fields.get(2816), 0)
            stone_name, stone_gbid, stone_hash = self.hash_entry_parts(stone_value)
            try:
                stone_display = "" if int(stone_value) in (0, 0x887AE0B0) else (stone_name if stone_gbid else stone_hash)
            except Exception:
                stone_display = str(stone_value)
            row = [
                unit_id,
                w_name,
                w_gbid,
                w_hash,
                self.value1(fields.get(2804), ""),
                self.value1(fields.get(2805), ""),
                self.value1(fields.get(2806), ""),
                self.value1(fields.get(2807), ""),
                self.value1(fields.get(2814), ""),
                self.value1(fields.get(2815), ""),
                stone_display,
            ]
            if not self._matches_editor_filter(row, q):
                continue
            rows.append(row)
            meta_rows.append({
                "unit_id": unit_id,
                "is_empty": is_empty,
                "is_known": is_known,
                "hash_rec": fields.get(2803),
                "xp_rec": fields.get(2804),
                "unk_2805_rec": fields.get(2805),
                "unk_2806_rec": fields.get(2806),
                "unk_2807_rec": fields.get(2807),
                "unk_2814_rec": fields.get(2814),
                "flags_rec": fields.get(2815),
                "stone_rec": fields.get(2816),
            })
        self.weapon_rows_meta = meta_rows
        self.weapon_model.set_rows(rows)
        if hasattr(self, "weapon_count_label"):
            self.weapon_count_label.setText(
                f"Weapon slots: {self.format_value(active_slots)} active / {self.format_value(total_slots)} total · "
                f"{self.format_value(known_slots)} known · {self.format_value(unknown_slots)} unknown · "
                f"{self.format_value(empty_slots)} empty addable · showing {self.format_value(len(rows))}"
            )
        if hasattr(self, "weapon_table"):
            self._set_table_widths(self.weapon_table, {1: 380, 2: 170, 4: 120, 10: 260})
            self._auto_fit_table(self.weapon_table)
        if hasattr(self, "weapon_empty_model"):
            self.refresh_weapon_empty_slot_rows()
        if hasattr(self, "weapon_database_model"):
            self.refresh_weapon_database_rows()
        self.update_weapon_detail()

    def _current_item_category_filter(self) -> str:
        combo = getattr(self, "item_category_combo", None)
        if combo is None:
            return "All Safe Quantity Rows"
        return combo.currentText() or "All Safe Quantity Rows"

    def _classify_item_row_for_filter(self, entry: Any, family: str, is_empty: bool, inactive_known_material: bool, quantity_is_real: bool) -> str:
        if not quantity_is_real:
            return "Technical / Relic / Curio"
        if inactive_known_material or is_empty:
            return "Missing / Addable"
        if not entry:
            return "Unknown Safe Rows"
        cat = (getattr(entry, "category", "") or "").strip().lower()
        name = (getattr(entry, "display_name", "") or "").strip().lower()
        gbid = (getattr(entry, "item_id", "") or "").strip().lower()
        if cat in {"currency"} or name in {"rupie", "rupies", "mastery point", "mastery points"}:
            return "Wallet / Profile"
        if "wrightstone" in name or "whetstone" in name or "wst" in gbid:
            return "Wrightstones"
        if "glitter" in cat or "glitter" in name:
            return "Glitterstones"
        if cat in {"ticket"} or "badge" in name or "ticket" in name:
            return "Tickets / Badges"
        if cat in {"consumable"}:
            return "Consumables"
        if cat in {"material", "currency"}:
            return "Materials"
        if cat in {c.lower() for c in self.MATERIAL_BANK_CATEGORIES}:
            return "Materials"
        return "Unknown Safe Rows"

    def _item_category_filter_accepts(self, row_category: str) -> bool:
        selected = self._current_item_category_filter()
        if selected in ("", "All Safe Quantity Rows"):
            return row_category != "Technical / Relic / Curio"
        if selected == "Technical / Relic / Curio":
            return row_category == selected
        return row_category == selected

    def refresh_item_rows(self) -> None:
        if not self.save:
            self.item_model.set_rows([])
            self.item_rows_meta = []
            if hasattr(self, "item_count_label"):
                self.item_count_label.setText("Open a save to inspect item/material inventory slots.")
            if hasattr(self, "item_empty_hint_label"):
                self.item_empty_hint_label.setVisible(False)
            return
        grouped = self.save.group_by_unit([1801, 1802, 1803, 1804, 2102, 2103, 2104, 2105, 1901, 1902, 1903, 1904, 2002, 2003, 2004])
        show_empty = bool(getattr(getattr(self, "item_show_empty_check", None), "isChecked", lambda: False)())
        show_technical = bool(getattr(getattr(self, "item_show_technical_check", None), "isChecked", lambda: False)())
        known_only = bool(getattr(getattr(self, "item_known_only_check", None), "isChecked", lambda: False)())
        unknown_only = bool(getattr(getattr(self, "item_unknown_only_check", None), "isChecked", lambda: False)())
        rows: List[List[Any]] = []
        meta_rows: List[Dict[str, Any]] = []
        total_slots = active_slots = empty_slots = known_slots = unknown_slots = filtered_out = 0
        material_bank_rows = item_slot_rows = 0
        q = getattr(self, "item_filter_edit", None).text().strip().lower() if hasattr(self, "item_filter_edit") else ""

        # Direct UserDataManager wallet/profile values. These are what the main
        # menu reads for top-bar currency. Show these before ItemManager rows so
        # filtering "rupie" reveals the real editable value, not only an
        # ITEM_35_0000 database/reference stack.
        for field_id, label, gbid, hash_hex, cap in [
            (1104, "Rupies", "USERDATA_RUPIES", "", 99_999_999),
            (1112, "Mastery Points", "USERDATA_MASTERY_POINTS", "", 9_999_999),
            (1106, "Commendations", "USERDATA_COMMENDATIONS", "", 999),
        ]:
            rec = self.save.find_first("int", field_id, 0)
            if rec is None:
                continue
            qty_value = self._record_first_value(rec, 0)
            row_category = "Wallet / Profile"
            row = [f"wallet:{field_id}", label, gbid, hash_hex, field_id, "UserData", qty_value, "UserDataManager wallet/profile value"]
            total_slots += 1
            active_slots += 1
            known_slots += 1
            if known_only is False and unknown_only is True:
                filtered_out += 1
                continue
            if self._item_category_filter_accepts(row_category) and self._matches_editor_filter(row, q):
                rows.append(row)
                meta_rows.append({
                    "unit_id": 0,
                    "field_id": field_id,
                    "is_empty": False,
                    "is_known": True,
                    "hash_rec": None,
                    "index_rec": None,
                    "flag_rec": None,
                    "qty_rec": rec,
                    "quantity_is_real": True,
                    "wallet_value": True,
                    "row_category": row_category,
                    "family": "UserDataManager wallet/profile value",
                })
            else:
                filtered_out += 1

        for unit_id, fields in sorted(grouped.items()):
            # 180x = real material/currency/consumable bank. 1802 is the count the game reads.
            material_slot_active = False
            if fields.get(1801) is not None:
                family = "180x Material/Currency bank row"
                hash_rec = fields.get(1801)
                qty_rec = fields.get(1802)
                flag_rec = fields.get(1803)
                index_rec = fields.get(1804)
                quantity_is_real = True
                material_slot_active = self._material_bank_slot_is_active(fields)
                material_bank_rows += 1
            else:
                family = "Technical item-slot/relic/curio row (not a safe quantity)"
                if not show_technical:
                    continue
                hash_rec = self._first_non_empty_record(fields, [2102, 1901, 2002])
                index_rec = self._first_existing_record(fields, [2103, 1902, 2003])
                flag_rec = self._first_existing_record(fields, [2104, 1904])
                qty_rec = self._first_existing_record(fields, [2105, 1903, 2004])
                quantity_is_real = False
                item_slot_rows += 1

            item_hash = self.value1(hash_rec, 0)
            index_or_serial = self.value1(index_rec, "")
            flag = self.value1(flag_rec, "")
            qty_or_type = self.value1(qty_rec, 0)
            is_empty = item_hash in ("", 0, EMPTY_HASH) and qty_or_type in ("", 0)
            inactive_known_material = False
            total_slots += 1

            if quantity_is_real and fields.get(1801) is not None and not material_slot_active:
                # Many saves contain catalog rows with a real 1801 hash but no
                # owned stack yet. Showing those as normal quantity rows made
                # the page look like broken inventory (known item, quantity 0).
                # Hide them by default; Show empty addable slots exposes them
                # for safe-template activation/debugging.
                inactive_known_material = True
                is_empty = True

            if is_empty:
                empty_slots += 1
            else:
                active_slots += 1
            if is_empty and not show_empty:
                continue
            if is_empty:
                if inactive_known_material and item_hash not in ("", 0, EMPTY_HASH):
                    i_name, i_gbid, i_hash = self.hash_entry_parts(item_hash)
                    try:
                        entry = self.item_db.lookup_hash(int(item_hash) & 0xFFFFFFFF)
                    except Exception:
                        entry = None
                    family = "Missing/addable 180x material row"
                else:
                    i_name, i_gbid, i_hash = "<Empty material/item slot>", "", ""
                    entry = None
            else:
                i_name, i_gbid, i_hash = self.hash_entry_parts(item_hash)
                try:
                    entry = self.item_db.lookup_hash(int(item_hash) & 0xFFFFFFFF)
                except Exception:
                    entry = None

            # Normal view is intentionally conservative. Show only rows that
            # are backed by a real quantity field and resolve to known normal
            # inventory categories. Wallet/profile currencies are shown above
            # from UserDataManager and duplicate ITEM_35_* stacks are hidden
            # unless the technical view is explicitly enabled.
            if quantity_is_real and not show_technical and not is_empty:
                if not entry or entry.category not in self.MATERIAL_BANK_CATEGORIES:
                    filtered_out += 1
                    continue
                if entry.display_name.strip().lower() in {"rupie", "rupies", "mastery point", "mastery points"}:
                    filtered_out += 1
                    continue

            is_known = bool(i_gbid)
            if is_known:
                known_slots += 1
            elif not is_empty:
                unknown_slots += 1
            if known_only and (is_empty or not is_known):
                filtered_out += 1
                continue
            if unknown_only and (is_empty or is_known):
                filtered_out += 1
                continue
            if quantity_is_real:
                qty_display = "missing / addable" if inactive_known_material else qty_or_type
            else:
                qty_display = f"not editable: type/state {qty_or_type}"
            row_category = self._classify_item_row_for_filter(entry, family, is_empty, inactive_known_material, quantity_is_real)
            row = [unit_id, i_name, i_gbid, i_hash, index_or_serial, flag, qty_display, family]
            if not self._item_category_filter_accepts(row_category):
                filtered_out += 1
                continue
            if not self._matches_editor_filter(row, q):
                filtered_out += 1
                continue
            rows.append(row)
            meta_rows.append({
                "unit_id": unit_id,
                "is_empty": is_empty,
                "is_known": is_known,
                "hash_rec": hash_rec,
                "index_rec": index_rec,
                "flag_rec": flag_rec,
                "qty_rec": qty_rec,
                "value_1805_rec": fields.get(1805),
                "value_1806_rec": fields.get(1806),
                "extra_rec": fields.get(1807),
                "quantity_is_real": quantity_is_real,
                "inactive_known_material": inactive_known_material,
                "row_category": row_category,
                "family": family,
            })
        self.item_rows_meta = meta_rows
        self.item_model.set_rows(rows)
        if hasattr(self, "item_count_label"):
            self.item_count_label.setText(
                f"Safe inventory rows: {self.format_value(active_slots)} active / {self.format_value(total_slots)} scanned · "
                f"{self.format_value(material_bank_rows)} material-bank · {self.format_value(item_slot_rows)} technical hidden/shown · "
                f"{self.format_value(known_slots)} known · {self.format_value(unknown_slots)} unknown · "
                f"{self.format_value(empty_slots)} empty · showing {self.format_value(len(rows))}"
            )
        if hasattr(self, "item_empty_hint_label"):
            if not rows:
                if active_slots == 0:
                    msg = (
                        "No active item/material rows were found in this loaded file. "
                        "Materials/currency normally use 1801/1802 rows. Technical type/state rows are hidden unless enabled under Filters."
                    )
                else:
                    msg = (
                        f"There are {self.format_value(active_slots)} active item/material rows, but the current search/filter settings are hiding them. "
                        "Clear the search text or turn off Known/Unknown-only filters."
                    )
                self.item_empty_hint_label.setText(msg)
                self.item_empty_hint_label.setVisible(True)
            else:
                self.item_empty_hint_label.setVisible(False)
        if hasattr(self, "item_table"):
            self._set_table_widths(self.item_table, {1: 360, 2: 170, 4: 90, 5: 90, 6: 130, 7: 260})
            self._auto_fit_table(self.item_table)
        self.update_item_detail()

    def update_status_text(self) -> None:
        if not self.save:
            self.status_label.setText("No save loaded")
            return
        s = self.save.summary()
        try:
            self._last_hash_ok = s.get("active_hash_ok")
        except Exception:
            pass
        dirty = "modified" if self.dirty else "clean"
        self.status_label.setText(f"{Path(s['path']).name}\n{s['mode']}\n{dirty}\nhash ok: {s['active_hash_ok']}")
        if hasattr(self, "overview_text"):
            self.overview_text.setPlainText(self.format_summary(s))
        if hasattr(self, "next_steps_text"):
            active_sigils = sum(1 for row in getattr(self.sigil_model, "rows", []) if row and not str(row[2]).startswith("<Empty"))
            unknown_sigils = sum(1 for row in getattr(self.sigil_model, "rows", []) if len(row) > 2 and str(row[2]).startswith("Unknown 0x"))
            active_items = sum(1 for row in getattr(self.item_model, "rows", []) if row and not str(row[1]).startswith("<Empty"))
            active_weapons = sum(1 for row in getattr(self.weapon_model, "rows", []) if row and not str(row[1]).startswith("<Empty"))
            tips = [
                f"Loaded: {Path(s['path']).name} · hash ok: {s['active_hash_ok']} · state: {'modified' if self.dirty else 'clean'}",
                f"Visible now: {active_items:,} item/material rows, {active_sigils:,} sigil rows, {active_weapons:,} weapon rows.",
            ]
            if unknown_sigils:
                tips.append(f"There are {unknown_sigils:,} visible unknown sigil hashes. Open Sigils and press Show Unknown, then export/copy the hashes for mapping.")
            else:
                tips.append("Next useful pass: use Cheats for bulk edits, or edit a row directly from Items/Sigils/Weapons/Characters.")
            tips.append("Always use Save As for the first edited copy until the game verifies it.")
            self.next_steps_text.setText("\n".join(tips))
        if hasattr(self, "raw_text"):
            self.raw_text.setPlainText(self.format_summary(s))
        if self._current_page_label() == "Save Health":
            self.refresh_save_health()
        if hasattr(self, "research_text") and self._current_page_label() == "Research" and not getattr(self, "value_search_results", []):
            self.research_text.setPlainText("Load a save, then use value search for exact before/after hunting. Candidate rows above are grouped from known/likely save IDs.")

    def format_summary(self, s: Dict[str, Any]) -> str:
        lines = [
            f"File: {s['path']}",
            f"Mode: {s['mode']}",
            f"File size: {s['size']:,} bytes",
            f"Payload: offset {s['payload_offset']} / size {s['payload_size']:,}",
            f"VersionMaybe: {s['version_maybe']}",
            f"Hash seed: {s['hash_seed']}",
            f"Active hash index: {s['active_hash_index']}",
            f"Active hash OK: {s['active_hash_ok']}",
            f"Item DB rows loaded: {len(self.item_db)}",
            f"Research candidate rows: {self.candidate_model.rowCount() if hasattr(self, 'candidate_model') else 0}",
            "",
            "Record counts:",
        ]
        for kind, count in s["record_counts"].items():
            lines.append(f"  {kind:<7} {count:,}")
        lines.extend(["", "Detected editor data:"])
        try:
            lines.append(f"  Items visible:      {len(getattr(self.item_model, 'rows', [])):,}")
            lines.append(f"  Sigils visible:     {len(getattr(self.sigil_model, 'rows', [])):,}")
            lines.append(f"  Weapons visible:    {len(getattr(self.weapon_model, 'rows', [])):,}")
            lines.append(f"  Characters visible: {len(getattr(self.character_model, 'rows', [])):,}")
        except Exception:
            pass
        lines.extend([
            "",
            "Recommended safe workflow:",
            "  1. Use Save As for the first edited copy.",
            "  2. Use Cheats for bulk edits, or the specific tabs for one row at a time.",
            "  3. Use Save Health after large edits before testing in game.",
            "  4. Export unknown sigil/weapon hashes when names are missing; that helps fill the database without guessing.",
        ])
        if s.get("header"):
            lines.extend(["", "Wrapper header:"])
            for k, v in s["header"].items():
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.dirty:
            reply = QMessageBox.question(self, "Unsaved changes", "You have unsaved changes. Close anyway?")
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._save_ui_settings()
        event.accept()

    def set_theme(self, theme_name: str) -> None:
        self.current_theme = theme_name
        self.apply_theme()
        self._save_ui_settings()
        self.statusBar().showMessage(f"Theme changed to {theme_name}", 3000)

    def apply_theme(self) -> None:
        themes = {
            # Modern Dark is the new default. Clean Dark intentionally shares the same visual language
            # so existing saved settings still receive the modernized UI pass.
            "modern_dark": dict(bg="#0b1018", panel="#0f1723", card="#151e2b", card2="#1b2636", text="#eef5ff", muted="#9fb2c8", border="#243247", button="#1e2d42", hover="#2a3e5b", accent="#61a8ff", accent2="#8b5cf6", header="#111b2a", nav="#0a0f17", danger="#ff5f72", good="#40d39d"),
            "clean_dark": dict(bg="#0b1018", panel="#0f1723", card="#151e2b", card2="#1b2636", text="#eef5ff", muted="#9fb2c8", border="#243247", button="#1e2d42", hover="#2a3e5b", accent="#61a8ff", accent2="#8b5cf6", header="#111b2a", nav="#0a0f17", danger="#ff5f72", good="#40d39d"),
            "midnight": dict(bg="#08111e", panel="#0d1a2d", card="#13243a", card2="#1b3150", text="#edf6ff", muted="#a8bfd9", border="#27466c", button="#193153", hover="#254873", accent="#5aa9ff", accent2="#8a7dff", header="#102137", nav="#08111e", danger="#ff6677", good="#46d99b"),
            "slate": dict(bg="#0d1117", panel="#111827", card="#161f2e", card2="#202b3b", text="#f1f5f9", muted="#a9b4c4", border="#2d3a4d", button="#1f2937", hover="#334155", accent="#7aa2f7", accent2="#a78bfa", header="#172033", nav="#0f141d", danger="#fb7185", good="#34d399"),
            "light": dict(bg="#f6f8fb", panel="#eef3f9", card="#ffffff", card2="#f4f7fb", text="#111827", muted="#5b6677", border="#d9e1ec", button="#edf4ff", hover="#dcecff", accent="#2563eb", accent2="#7c3aed", header="#f3f7fd", nav="#f7faff", danger="#dc2626", good="#059669"),
            "sakura": dict(bg="#17111a", panel="#241827", card="#2d1f33", card2="#382643", text="#fff2fb", muted="#d8bfd4", border="#674873", button="#4b315a", hover="#624278", accent="#f472b6", accent2="#c084fc", header="#40284c", nav="#1b1220", danger="#fb7185", good="#5eead4"),
            "emerald": dict(bg="#081511", panel="#10221d", card="#173228", card2="#1f4236", text="#eafff7", muted="#a9d2c3", border="#2f6252", button="#214f42", hover="#2c6856", accent="#34d399", accent2="#22d3ee", header="#18362e", nav="#0b1714", danger="#fb7185", good="#40d39d"),
            "graphite": dict(bg="#101214", panel="#171a1f", card="#1f242b", card2="#282e37", text="#f3f5f7", muted="#aab2bd", border="#363f4c", button="#29313b", hover="#354150", accent="#8ab4ff", accent2="#c4b5fd", header="#1f2630", nav="#111417", danger="#fb7185", good="#34d399"),
            "royal": dict(bg="#120f1d", panel="#1b1730", card="#241f3e", card2="#302952", text="#f5f0ff", muted="#c5b8e9", border="#51467d", button="#332b60", hover="#44397b", accent="#a78bfa", accent2="#60a5fa", header="#2e2750", nav="#181429", danger="#fb7185", good="#5eead4"),
            "cyberpunk": dict(bg="#090812", panel="#111020", card="#19152c", card2="#241b3f", text="#f7f3ff", muted="#b7a7d9", border="#3a2e5e", button="#281f4d", hover="#3b2d73", accent="#22d3ee", accent2="#f472b6", header="#181331", nav="#080711", danger="#ff4d88", good="#39ffb6"),
            "dracula": dict(bg="#1e1f29", panel="#282a36", card="#2f3142", card2="#383a4e", text="#f8f8f2", muted="#bdc1d6", border="#51546e", button="#3b3f55", hover="#44475a", accent="#bd93f9", accent2="#ff79c6", header="#303241", nav="#1c1d26", danger="#ff5555", good="#50fa7b"),
            "ocean": dict(bg="#06141f", panel="#0b2233", card="#12344a", card2="#194862", text="#e9f8ff", muted="#a7cfe0", border="#245d7a", button="#1b4b68", hover="#25688f", accent="#38bdf8", accent2="#2dd4bf", header="#11354d", nav="#071722", danger="#fb7185", good="#34d399"),
            "forest": dict(bg="#07130c", panel="#0e2015", card="#172d20", card2="#1f3a2b", text="#effff4", muted="#acd1b8", border="#31563e", button="#244a34", hover="#2f6143", accent="#86efac", accent2="#facc15", header="#183421", nav="#08150d", danger="#f87171", good="#22c55e"),
            "amber": dict(bg="#120d05", panel="#1d1509", card="#2a1e0e", card2="#382811", text="#fff7e6", muted="#d6bd8f", border="#60471e", button="#4a3517", hover="#65491e", accent="#f59e0b", accent2="#f97316", header="#33240f", nav="#130e06", danger="#ef4444", good="#84cc16"),
            "oled": dict(bg="#000000", panel="#05070a", card="#090d12", card2="#101820", text="#f7fbff", muted="#9ca3af", border="#202a35", button="#101820", hover="#182433", accent="#00e5ff", accent2="#8b5cf6", header="#070b10", nav="#000000", danger="#ff4d6d", good="#00ffa3"),
            "contrast": dict(bg="#000000", panel="#050505", card="#000000", card2="#111111", text="#ffffff", muted="#eeeeee", border="#ffffff", button="#111111", hover="#333333", accent="#ffd400", accent2="#ffd400", header="#111111", nav="#000000", danger="#ff4040", good="#00ff99"),
        }
        c = themes.get(getattr(self, "current_theme", "modern_dark"), themes["modern_dark"])
        row_pad = 6 if getattr(self, "compact_mode", True) else 9
        button_pad = "8px 12px" if getattr(self, "compact_mode", True) else "11px 14px"
        nav_width = 208 if getattr(self, "ui_clean_mode", True) else 250
        self.setStyleSheet(f"""
            QMainWindow {{ background: {c['bg']}; color: {c['text']}; }}
            QWidget {{ background: {c['bg']}; color: {c['text']}; font-size: 13px; }}
            QLabel {{ background: transparent; color: {c['text']}; }}
            #contentStack {{ background: {c['bg']}; }}
            #navScroll, #navContent {{ background: transparent; border: 0; }}
            #nav {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 {c['nav']}, stop:1 {c['panel']});
                border-right: 1px solid {c['border']}; min-width: {nav_width}px; max-width: {nav_width}px;
            }}
            #navTitle {{ font-size: 23px; font-weight: 900; color: {c['text']}; background: transparent; letter-spacing: .2px; padding: 2px 2px 0 2px; }}
            #navSubtitle {{ color: {c['accent']}; font-size: 11px; font-weight: 800; background: transparent; padding: 0 2px 8px 3px; }}
            #navStatus {{ color: {c['muted']}; background: {c['card']}; border: 1px solid {c['border']}; border-radius: 14px; padding: 10px; }}
            #subtleText, QLabel#subtleText, #helpText, QLabel#helpText {{ color: {c['muted']}; background: transparent; line-height: 135%; }}
            #navSection {{ color: {c['muted']}; font-size: 10px; font-weight: 900; padding: 14px 0 4px 4px; letter-spacing: 1.2px; background: transparent; }}
            #pageHeader {{ font-size: 30px; font-weight: 900; margin: 4px 0 4px 0; background: transparent; color: {c['text']}; letter-spacing: -.4px; }}
            QPushButton {{
                background: {c['button']}; color: {c['text']}; border: 1px solid {c['border']};
                border-radius: 12px; padding: {button_pad}; font-weight: 700;
            }}
            QPushButton:hover {{ background: {c['hover']}; border-color: {c['accent']}; }}
            QPushButton:pressed {{ background: {c['accent']}; color: #07111f; }}
            QPushButton[class="primaryButton"] {{ background: {c['accent']}; color: #07111f; border-color: transparent; }}
            QPushButton[class="primaryButton"]:hover {{ background: {c['accent2']}; color: #07111f; }}
            QPushButton:disabled {{ background: {c['card']}; color: {c['muted']}; border-color: {c['border']}; }}
            QPushButton[class="navButton"] {{
                text-align: left; font-weight: 800; margin: 1px 0; padding: 10px 12px;
                border-radius: 14px; border: 1px solid transparent; background: transparent; color: {c['muted']};
            }}
            QPushButton[class="navButton"]:hover {{ background: {c['card']}; color: {c['text']}; border-color: {c['border']}; }}
            QPushButton[class="navButton"]:checked {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {c['accent']}, stop:1 {c['accent2']});
                color: #07111f; border-color: transparent;
            }}
            QCheckBox {{ color: {c['muted']}; spacing: 8px; padding: 7px 2px; background: transparent; }}
            QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 6px; border: 1px solid {c['border']}; background: {c['card']}; }}
            QCheckBox::indicator:checked {{ background: {c['accent']}; border-color: {c['accent']}; }}
            QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
                background: {c['card']}; border: 1px solid {c['border']}; border-radius: 12px;
                padding: 9px 11px; color: {c['text']}; selection-background-color: {c['accent']}; selection-color: #07111f;
            }}
            QPlainTextEdit#summaryBox {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 14px; padding: 10px; font-family: Consolas, 'Cascadia Mono', monospace; }}
            QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {c['accent']}; background: {c['card2']}; color: {c['text']}; }}
            QComboBox::drop-down {{ border: 0; width: 26px; }}
            QComboBox QAbstractItemView {{ background: {c['panel']}; color: {c['text']}; selection-background-color: {c['accent']}; border: 1px solid {c['border']}; outline: 0; }}
            QTableView {{
                background: {c['card']}; alternate-background-color: {c['card2']}; gridline-color: transparent;
                selection-background-color: {c['accent']}; selection-color: #07111f;
                border: 1px solid {c['border']}; border-radius: 16px; outline: 0;
            }}
            QTableView:focus {{ border-color: {c['accent']}; }}
            QTableView::item {{ padding: {row_pad}px 8px; border: 0; }}
            QTableView::item:hover {{ background: {c['hover']}; }}
            QTableView::item:selected {{ background: {c['accent']}; color: #07111f; }}
            QHeaderView {{ background: transparent; }}
            QHeaderView::section {{
                background: {c['header']}; color: {c['text']}; border: 0; border-right: 1px solid {c['border']};
                padding: 8px 10px; font-weight: 900;
            }}
            QHeaderView::section:first {{ border-top-left-radius: 14px; }}
            QTabWidget#editorTabs {{ background: transparent; border: 0; }}
            QTabWidget#editorTabs::pane {{ background: {c['card']}; border: 1px solid {c['border']}; border-radius: 14px; top: -1px; }}
            QTabWidget#editorTabs QTabBar::tab {{
                background: {c['button']}; color: {c['muted']}; border: 1px solid {c['border']};
                border-bottom: 0; padding: 9px 16px; margin-right: 4px; border-top-left-radius: 10px; border-top-right-radius: 10px;
                font-weight: 800; min-width: 128px;
            }}
            QTabWidget#editorTabs QTabBar::tab:selected {{ background: {c['card2']}; color: {c['text']}; border-color: {c['accent']}; }}
            QTabWidget#editorTabs QTabBar::tab:hover {{ background: {c['hover']}; color: {c['text']}; }}
            QTabWidget#masteryValueTabs {{ background: transparent; border: 0; }}
            QTabWidget#masteryValueTabs::pane {{ background: transparent; border: 0; margin: 0px; top: -1px; }}
            QTabWidget#masteryValueTabs QTabBar {{ background: transparent; border: 0; }}
            QTabWidget#masteryValueTabs QTabBar::tab {{
                background: {c['button']}; color: {c['text']}; border: 1px solid {c['border']};
                border-bottom: 0; padding: 10px 20px; margin-right: 6px;
                border-top-left-radius: 12px; border-top-right-radius: 12px;
                font-size: 13px; font-weight: 900; min-width: 140px;
            }}
            QTabWidget#masteryValueTabs QTabBar::tab:selected {{
                background: {c['card2']}; color: {c['accent']}; border-color: {c['accent']};
            }}
            QTabWidget#masteryValueTabs QTabBar::tab:hover {{ background: {c['hover']}; color: {c['text']}; }}
            QTabBar#progressionTopTabs {{
                background: transparent; border: 0; min-height: 38px; qproperty-drawBase: 0;
            }}
            QTabBar#progressionTopTabs::tab {{
                background: {c['button']}; color: {c['text']}; border: 1px solid {c['border']};
                padding: 10px 16px; margin-right: 6px; border-radius: 11px;
                font-size: 12px; font-weight: 900; min-width: 110px;
            }}
            QTabBar#progressionTopTabs::tab:selected {{
                background: {c['accent']}; color: #07111f; border-color: {c['accent']};
            }}
            QTabBar#progressionTopTabs::tab:hover {{
                background: {c['hover']}; color: {c['text']}; border-color: {c['accent']};
            }}
            QTableView QLineEdit, QTableView QSpinBox {{
                background: {c['panel']}; color: {c['text']}; border: 2px solid {c['accent']};
                border-radius: 8px; padding: 2px 6px; min-height: 24px;
                selection-background-color: {c['accent']}; selection-color: #07111f;
            }}
            QTableView QSpinBox::up-button, QTableView QSpinBox::down-button {{
                background: {c['button']}; border: 0; width: 18px;
            }}
            QLabel#selectedItemSummary {{
                background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 14px;
                padding: 12px; color: {c['text']}; line-height: 145%;
            }}
            QGroupBox {{
                background: {c['card']}; border: 1px solid {c['border']}; border-radius: 18px;
                margin-top: 20px; padding: 20px 16px 16px 16px; font-weight: 900;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; left: 16px; top: 0px; padding: 3px 10px;
                background: {c['card']}; color: {c['accent']}; border-radius: 10px;
            }}
            QScrollArea {{ background: transparent; border: 0; }}
            QSplitter::handle {{ background: {c['border']}; border-radius: 2px; }}
            QMenuBar {{ background: {c['panel']}; color: {c['text']}; border-bottom: 1px solid {c['border']}; padding: 2px; }}
            QMenuBar::item {{ background: transparent; padding: 6px 10px; border-radius: 8px; }}
            QMenuBar::item:selected {{ background: {c['hover']}; }}
            QMenu {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['border']}; border-radius: 10px; padding: 6px; }}
            QMenu::item {{ padding: 7px 24px 7px 10px; border-radius: 8px; }}
            QMenu::item:selected {{ background: {c['hover']}; }}
            QStatusBar {{ background: {c['panel']}; color: {c['muted']}; border-top: 1px solid {c['border']}; }}
            QToolTip {{ background: {c['card']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 7px; border-radius: 8px; }}
            QScrollBar:vertical {{ background: transparent; width: 12px; margin: 3px; }}
            QScrollBar::handle:vertical {{ background: {c['border']}; min-height: 32px; border-radius: 6px; }}
            QScrollBar::handle:vertical:hover {{ background: {c['accent']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 3px; }}
            QScrollBar::handle:horizontal {{ background: {c['border']}; min-width: 32px; border-radius: 6px; }}
            QScrollBar::handle:horizontal:hover {{ background: {c['accent']}; }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
        """)
        app = QApplication.instance()
        if app is not None:
            app.setFont(QFont("Segoe UI", 10))

def _resource_categories_for_field(id_type: int) -> List[str]:
    if id_type in {2570, 2571, 2572, 2573, 2574, 2575, 2576, 2577, 2580, 2581, 2582, 2583, 2501, 2502, 2503, 2504, 2505, 2506, 2507, 2508, 2509, 2510, 2511, 2512, 2513, 2514, 2515, 2516, 2517, 2518, 2519, 2520, 2521, 2522, 2530, 2550, 2551, 2552, 2553, 2554, 2555, 2560, 2561, 2562, 2563}:
        return ["Quest"]
    if id_type in {4201, 4202}:
        return ["Quest", "Phase"]
    if id_type in {3903, 3904}:
        return ["Action", "Buff", "Debuff/Ailment"]
    return []

def main() -> int:
    _install_exception_logging()
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
