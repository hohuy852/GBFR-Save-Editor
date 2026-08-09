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
from item_db import ItemDatabase, DEFAULT_ITEM_URL, TRAIT_SKILL_URL, RAW_SIGIL_GEM_URL, source_urls_from_text
from item_id_catalog import catalog_rows, format_catalog_summary, write_catalog_csv, COMMUNITY_ITEM_ID_TARGET_ROWS
from sigil_gem_id_catalog import sigil_rows, format_sigil_summary, write_sigil_catalog_csv
from trait_skill_id_catalog import trait_skill_rows, format_trait_skill_summary, write_trait_skill_catalog_csv
from resource_id_db import ResourceIdDatabase, DEFAULT_RESOURCE_URLS
from unit_meta import unit_name
from unit_labeler import UnitLabelIndex
from hashing import gbfr_hash, gbfr_hash_hex
from reference_db import ReferenceDatabase
from cheat_actions import complete_quest_tables_splusplus, unlock_title_archive_candidates, set_character_overmastery_hashes, clear_character_overmastery_hashes, patch_summary, EMPTY_HASH as CHEAT_EMPTY_HASH

APP_TITLE = "GBFR - Sigils / Wrightstones / Mastery Editor"
EMPTY_HASH = 0x887AE0B0
I32_MIN = -2_147_483_648
I32_MAX = 2_147_483_647
SIGIL_MAX_EQUIPPED_PER_OWNER = 13
SIGIL_LEVEL_MAX = I32_MAX
SIGIL_LEVEL_TEST_MAX = SIGIL_LEVEL_MAX
# Wrightstones use 50000-series inventory slots and 140000000-series linked trait lanes.
# 120000000-series rows are sigil/gem trait lanes, not Wrightstones.
SIGIL_TRAIT_UNIT_BASE = 120_000_000
WRIGHTSTONE_TRAIT_UNIT_BASE = 140_000_000
WEAPON_XP_MAX = 999_999_999
CHARACTER_VALUE_MAX = 999_999_999
# In-game equipped sigil rows use 2706 / FF920A as the assigned character hash.
# Equip writes also repair the sigil serial and active flag before assigning the owner.
SIGIL_EQUIP_WRITES_ENABLED = True

# 1607 is the mastery amount/value field. The Save Wizard notes include an
# extreme 0x7FFFFFFF test preset, but that path has been reported to crash
# save/write flows. Keep the GUI on the highest working preset we have seen
# used in codes, and sanitize older edited saves before writing.
MASTERY_1607_SAFE_MAX = I32_MAX  # signed 32-bit max for FF470600 mastery value tests
MASTERY_1607_MORE_VALUE = 0x05F5E0FF  # 99,999,999 / "MORE than normal"

# Confirmed Save Wizard mastery/overmastery pairing:
#   FF460600 -> SaveData field 1606 -> selected mastery/overmastery effect ID
#   FF470600 -> SaveData field 1607 -> paired mastery/overmastery amount/value
# Overmastery is a fixed 40 character/group x 4 lane block.  Each lane has a
# 1606 effect row and a paired 1607 amount row at the same concrete save unit.
MASTERY_EFFECT_FIELD_ID = 1606
MASTERY_VALUE_FIELD_ID = 1607
OVERMASTERY_GROUP_COUNT = 0x28
OVERMASTERY_LANE_COUNT = 4
OVERMASTERY_UNIT_BASE = 10_000_000
OVERMASTERY_UNIT_GROUP_STRIDE = 1000
OVERMASTERY_SAVEWIZARD_ROW_STRIDE = 0x18
OVERMASTERY_SAVEWIZARD_GROUP_STRIDE = 0x60
OVERMASTERY_VALUE_NORMAL = 0x0200
OVERMASTERY_VALUE_MAX = 0x03FF
BASIC_MASTERY_SW_SLOT_COUNT = 0x258
BASIC_MASTERY_SW_ROW_STRIDE = 0x18

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

    def preview(self, rec: UnitRecord, limit: int = 10) -> str:
        if self.save is None:
            return ""
        values = self.save.get_values(rec, limit)
        shown: List[str] = []
        for val in values:
            if isinstance(val, bool):
                shown.append("true" if val else "false")
            elif isinstance(val, float):
                shown.append(f"{val:g}")
            elif isinstance(val, int):
                iv = int(val) & 0xFFFFFFFF
                entry = self.item_db.lookup_hash(iv)
                if entry:
                    shown.append(f"{entry.display_name} ({entry.item_id})")
                else:
                    shown.append(f"0x{iv:08X}")
            else:
                shown.append(str(val))
        return ", ".join(shown)

    def record_at(self, row: int) -> Optional[UnitRecord]:
        if 0 <= row < len(self.filtered):
            return self.filtered[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.filtered)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
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
        self.sigil_model = SimpleRowsModel(["Unit", "Slot", "Sigil", "GBID", "Hash", "Lv", "Trait 1", "Trait 2", "Character", "Flags", "T1 Lv", "T2 Lv", "Char GBID"])
        self.sigil_model.editable_columns = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}
        self.sigil_model.set_data_handler = self.apply_sigil_table_cell_edit
        self.sigil_database_model = SimpleRowsModel(["Status", "Name", "GBID", "Hash", "Grade", "Owned", "Empty Slots", "Action"])
        self.wrightstone_model = SimpleRowsModel(["Slot", "Wrightstone", "GBID", "Hash", "Value", "Trait 1", "T1 Lv", "Trait 2", "T2 Lv", "Trait 3", "T3 Lv", "Flags", "Unit"])
        self.wrightstone_model.editable_columns = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11}
        self.wrightstone_rows_meta: List[Dict[str, Any]] = []
        self.mastery_slot_model = SimpleRowsModel(["Slot / Lane", "Socket", "1606 Effect / Stat", "Row Type", "State / Lane", "1607 Amount", "1601 Slot Key", "1606 Hash", "Save Unit"])
        self.mastery_slot_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_model = SimpleRowsModel(["Row", "Kind", "Current Effect", "Hidden Type", "Value / Amount", "Hidden Hash", "Hidden Unit", "Hidden Pair"])
        self.mastery_overmastery_matrix_model = SimpleRowsModel(["Lane", "Save Unit", "1606 Stat / Effect", "1606 Row", "1607 Amount", "1607 Row", "SW Lane"])
        self.mastery_overmastery_matrix_rows_meta: List[Dict[str, Any]] = []
        # Current rows deliberately keep a few hidden columns in the backing
        # model because older helper code updates by column index. The visible
        # editor only shows Row / Kind / Effect / Value.
        self.mastery_mod_reference_model = SimpleRowsModel(["Effect / Stat", "Effect Hash", "Value Override", "Category", "Notes"])
        self.mastery_mod_code_model = SimpleRowsModel(["Pattern Row", "SW Rel Offset", "Target Effect", "Current Effect", "Current Hash", "Save Unit", "Repeat", "Status"])
        self.mastery_mod_preset_model = SimpleRowsModel(["Preset Range", "Rows", "Effect To Install", "1606 Hash", "Reason / Notes"])
        self.mastery_mod_value_model = SimpleRowsModel(["Value Preset", "Decimal", "Hex", "Risk", "What It Does"])
        self.mastery_mod_reference_model.editable_columns = {2}
        self.mastery_mod_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_reference_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_code_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_preset_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_value_rows_meta: List[Dict[str, Any]] = []
        self.mastery_mod_choices_cache: Optional[List[Dict[str, Any]]] = None
        self.wrightstone_choices_cache: Optional[List[Dict[str, Any]]] = None
        self.weapon_trait_choices_cache: Optional[List[Dict[str, Any]]] = None
        self._wrightstone_trait_grouped_cache: Optional[Dict[int, Dict[int, UnitRecord]]] = None
        self._wrightstone_slot_grouped_cache: Optional[Dict[int, Dict[int, UnitRecord]]] = None
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
        self.save_wizard_model = SimpleRowsModel(["Category", "Reference", "Source", "Status", "Notes", "Key"])
        self.progression_model = SimpleRowsModel(["Section", "Confidence", "Records", "Values", "Non-zero", "Recommended Action"])
        self.progression_edit_model = SimpleRowsModel(["Quest ID", "Name", "Status", "Rank", "Done", "Source"])
        self.progression_edit_model.editable_columns = {2, 3, 4}
        self._progression_catalog_cache: Optional[List[Dict[str, Any]]] = None
        self._progression_catalog_counts_cache: Dict[str, int] = {}
        self._progression_vector_record_cache: Dict[int, Optional[UnitRecord]] = {}
        self._progression_vector_values_cache: Dict[int, List[Any]] = {}
        self._progression_key_index_cache: Dict[tuple[str, int], Dict[int, int]] = {}
        self._progression_controls_loading = False
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
        self._sigil_auto_apply_timer = None
        self._sigil_field_auto_timers: Dict[int, QTimer] = {}
        self._sigil_auto_apply_in_progress = False
        self._item_qty_auto_apply_timer = None
        self._item_qty_auto_apply_in_progress = False
        self._weapon_inline_auto_apply_timer = None
        self._wrightstone_auto_apply_timer = None
        self._general_value_timers: Dict[int, QTimer] = {}
        self._general_values_loading = False
        self._general_party_auto_apply_timer = None
        self._general_party_loading = False


        self._load_ui_settings()
        self._build_ui()
        self._build_menu()
        if hasattr(self, "advanced_checkbox"):
            self.advanced_checkbox.setChecked(bool(self.advanced_mode))
            self._set_advanced_visible(bool(self.advanced_mode), persist=False)
        self.apply_theme()
        self._apply_view_preferences(persist=False)


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
            elif label == "General":
                self.update_edit_hub_summary()
                self.update_status_text()
                self.refresh_general_character_controls()
                self.refresh_general_synced_values()
            elif label == "Progression":
                self.refresh_progression_rows()
            elif label == "Items / Materials":
                self._refresh_current_items_subtab()
            elif label == "Sigils":
                self.refresh_sigil_rows()
            elif label == "Weapons":
                self.refresh_weapon_rows()
            elif label == "Wrightstones":
                self.refresh_wrightstone_rows()
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
            visible_pages = {"Sigils", "Wrightstones", "Mastery"}
            current_label = self._current_page_label()
            if current_label and current_label not in visible_pages:
                self._show_page("Sigils")
        self._update_nav_selection(self._current_page_label() or "Sigils")
        if persist:
            self._save_ui_settings()

    def _add_nav_button(self, layout: QVBoxLayout, label: str, page_builder, advanced: bool = False) -> QPushButton:
        idx = self.stack.addWidget(page_builder())
        self.page_indexes[label] = idx
        nav_icons = {
            "Welcome": "⌂", "Cheats": "⚡", "General": "★", "Progression": "◆",
            "Items / Materials": "▣", "Sigils": "◇", "Weapons": "⚔", "Wrightstones": "◆", "Characters": "◉", "Mastery": "✚",
            "Save Health": "✓", "Settings": "⚙", "About": "ⓘ", "Save Map": "🗺", "ID Cleanup": "⌁", "Unit Map": "◎",
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

    def _configure_overmastery_matrix_table(self) -> None:
        """Keep the selected-character Overmastery matrix readable."""
        table = getattr(self, "mastery_overmastery_matrix_table", None)
        if table is None:
            return
        try:
            table.setWordWrap(False)
            table.setTextElideMode(Qt.TextElideMode.ElideRight)
            table.verticalHeader().setDefaultSectionSize(30)
            table.verticalHeader().setMinimumSectionSize(28)
            header = table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            table.setColumnWidth(0, 60)
            table.setColumnWidth(1, 110)
            table.setColumnWidth(2, 360)
            table.setColumnWidth(3, 185)
            table.setColumnWidth(4, 220)
            table.setColumnWidth(5, 185)
            table.setColumnWidth(6, 110)
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
        try:
            self._sync_settings_controls()
        except Exception:
            pass
        self.statusBar().showMessage("Clean view enabled" if enabled else "Help text visible", 2500)

    def set_compact_mode(self, enabled: bool) -> None:
        self.compact_mode = bool(enabled)
        self._apply_view_preferences()
        self.apply_theme()
        try:
            self._sync_settings_controls()
        except Exception:
            pass
        self.statusBar().showMessage("Compact table rows enabled" if enabled else "Comfortable table rows enabled", 2500)

    def set_auto_fit_tables(self, enabled: bool) -> None:
        self.auto_fit_tables = bool(enabled)
        self._save_ui_settings()
        if enabled:
            for table in self.findChildren(QTableView):
                self._auto_fit_table(table)
        try:
            self._sync_settings_controls()
        except Exception:
            pass
        self.statusBar().showMessage("Auto-fit columns enabled" if enabled else "Auto-fit columns disabled", 2500)

    def set_fast_load_mode(self, enabled: bool) -> None:
        self.fast_load_mode = bool(enabled)
        self._save_ui_settings()
        try:
            self._sync_settings_controls()
        except Exception:
            pass
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
        try:
            self._sync_settings_controls()
        except Exception:
            pass
        self.statusBar().showMessage("Clean view reset", 2500)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        open_action = QAction("Open Save...", self)
        open_action.triggered.connect(self.open_save)
        save_action = QAction("Save", self)
        save_action.triggered.connect(self.save_original)
        save_as_action = QAction("Save As...", self)
        save_as_action.triggered.connect(self.save_as)
        file_menu.addAction(open_action)
        file_menu.addAction(save_action)
        file_menu.addAction(save_as_action)

        view_menu = self.menuBar().addMenu("View")
        appearance_menu = view_menu.addMenu("Appearance / Theme")
        for key, label in self._theme_options():
            action = QAction(label, self)
            action.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            appearance_menu.addAction(action)
        view_menu.addSeparator()

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
        self._add_nav_button(nav_items_layout, "Sigils", self._sigils_page)
        self._add_nav_button(nav_items_layout, "Wrightstones", self._wrightstones_page)
        self._add_nav_button(nav_items_layout, "Mastery", self._mastery_mods_page)
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
        self._show_page("Sigils")

    def _theme_options(self) -> List[tuple[str, str]]:
        return [
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
        ]

    def _set_theme_from_combo(self) -> None:
        combo = getattr(self, "settings_theme_combo", None)
        if combo is None:
            return
        key = combo.currentData()
        if key:
            self.set_theme(str(key))

    def _sync_settings_controls(self) -> None:
        combo = getattr(self, "settings_theme_combo", None)
        if combo is not None:
            combo.blockSignals(True)
            try:
                for idx in range(combo.count()):
                    if combo.itemData(idx) == getattr(self, "current_theme", "modern_dark"):
                        combo.setCurrentIndex(idx)
                        break
            finally:
                combo.blockSignals(False)
        pairs = [
            ("settings_clean_check", "ui_clean_mode"),
            ("settings_compact_check", "compact_mode"),
            ("settings_auto_fit_check", "auto_fit_tables"),
            ("settings_fast_load_check", "fast_load_mode"),
        ]
        for widget_name, attr_name in pairs:
            widget = getattr(self, widget_name, None)
            if widget is not None:
                widget.blockSignals(True)
                try:
                    widget.setChecked(bool(getattr(self, attr_name, False)))
                finally:
                    widget.blockSignals(False)























































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
        if category in {"All Safe Materials", "All Database Matches"}:
            return status in {"Missing · safe add", "Already owned", "Blocked / not safe"}
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
            searchable = " ".join([
                str(entry.display_name), str(entry.item_id), str(entry.category), str(entry.hash_hex), str(entry.alias_text)
            ]).lower()
            if query and query not in searchable:
                continue
            if not query and not self._items_database_category_accepts(status):
                continue
            # When the user types a specific search such as "dragon", show all
            # matching statuses so database rows do not look missing from the
            # editor just because they are already owned or blocked in this save.
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
                "Select a row. Searches show matching database rows even if the loaded save cannot safely add them yet. Only 'Missing · safe add' and 'Already owned' rows can be written."
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



    def _build_character_owner_choices(self) -> List[Dict[str, Any]]:
        """Known character hashes used by GemManager 2706 worn/character assignment."""
        choices: List[Dict[str, Any]] = [{"label": "None / Unassigned", "name": "None / Unassigned", "gbid": "", "hash": EMPTY_HASH}]
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
            return "None / Unassigned"
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
        resolved = self._resolve_hash_from_text(str(row[12] or row[8] or ""))
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
        """Count non-empty sigils that currently point at each character assignment hash.

        This only counts the visible owner reference field. It is used as a
        safety guard because the game breaks above 13 assigned sigils for one
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
        """Validate a sigil owner/character assignment assignment before touching the save.

        Known-good equipped saves confirm field 2706 / FF920A stores the
        character assignment character owner. The guard now only blocks impossible/unsafe
        assignments, such as too many sigils on one character.
        """
        try:
            target_hash = int(target_hash or 0) & 0xFFFFFFFF
        except Exception:
            target_hash = EMPTY_HASH

        if target_hash in (0, EMPTY_HASH):
            return True

        meta = {}
        try:
            if 0 <= int(row_index) < len(getattr(self, "sigil_rows_meta", [])):
                meta = self.sigil_rows_meta[int(row_index)]
        except Exception:
            meta = {}

        if meta and meta.get("is_empty"):
            if show_message:
                QMessageBox.warning(self, "Empty sigil slot", "Pick a real sigil before assigning an character assignment character.")
            return False

        counts = self._sigil_owner_counts_by_hash(skip_meta=meta)
        if counts.get(target_hash, 0) >= SIGIL_MAX_EQUIPPED_PER_OWNER:
            if show_message:
                QMessageBox.warning(
                    self,
                    "Too many assigned sigils",
                    f"The game only tolerates {SIGIL_MAX_EQUIPPED_PER_OWNER} assigned sigils per character. "
                    f"This owner already has {counts.get(target_hash, 0)}. Clear one first."
                )
            return False
        return True

    def _apply_sigil_owner_to_meta(self, meta: Dict[str, Any], owner_hash: int, row_index: int = -1, show_message: bool = True) -> bool:
        """Apply the observed sigil character-assignment pattern.

        Save Wizard / in-game assigned rows use:
        - 2702: sigil serial/key
        - 2703: sigil hash
        - 2704: sigil level
        - 2706: assigned character hash
        - 2707: assignment/inventory flags

        From the uploaded sample:
        - assigned rows use 2706 = character hash and 2707 low bits = 2
        - unassigned locked inventory rows commonly use 2707 low bits = 3
        """
        if not self.save or not meta:
            return False
        try:
            owner_hash = int(owner_hash or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            owner_hash = EMPTY_HASH
        if not self._sigil_owner_assignment_allowed(row_index, owner_hash, show_message=show_message):
            return False

        owner_rec = meta.get("worn_rec")
        if owner_rec is None:
            if show_message:
                QMessageBox.information(self, "Missing assignment field", "This sigil row does not expose the 2706 assigned-character field.")
            return False

        sigil_hash = int(self._record_first_value(meta.get("hash_rec"), 0) or 0) & 0xFFFFFFFF
        if owner_hash not in (0, EMPTY_HASH) and sigil_hash in (0, EMPTY_HASH):
            if show_message:
                QMessageBox.warning(self, "Empty sigil slot", "This row is empty. Add or select a sigil before assigning it to a character.")
            return False

        changed = False
        try:
            serial = int(self._record_first_value(meta.get("slot_rec"), 0) or 0) & 0xFFFFFFFF
        except Exception:
            serial = 0
        if sigil_hash not in (0, EMPTY_HASH) and serial in (0, EMPTY_HASH):
            self._set_sigil_serial(meta)
            changed = True

        old_owner = int(self._record_first_value(owner_rec, EMPTY_HASH) or EMPTY_HASH) & 0xFFFFFFFF
        if old_owner != owner_hash:
            if self._set_record_first_value(owner_rec, owner_hash, "sigil assigned character 2706"):
                changed = True
        else:
            changed = True

        flags_rec = meta.get("flags_rec")
        if flags_rec is not None and sigil_hash not in (0, EMPTY_HASH):
            cur_flags = int(self._record_first_value(flags_rec, 0) or 0)
            assigned = owner_hash not in (0, EMPTY_HASH)
            locked = bool(cur_flags & 1)
            normalized = self._safe_sigil_flags(cur_flags, locked=locked, assigned=assigned)
            if self._set_record_first_value(flags_rec, normalized, "sigil assignment/inventory flags 2707"):
                changed = True

        return changed

    def _sigil_owner_combo_changed(self, index: int) -> None:
        """Mirror/apply the observed 2706 character-assignment pattern.

        The uploaded Save Wizard sample shows assigned rows as 2706=character hash
        and 2707 low bits=2, so dropdown changes now write that mapped pattern.
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
        if self.apply_sigil_table_cell_edit(row_index, 8, edit_value):
            # Keep the detail panel in sync without requiring the Apply button.
            self.update_sigil_detail()
            self.statusBar().showMessage("Auto-applied selected sigil assigned character. Save when ready.", 3500)
        else:
            # Restore the dropdown to the selected row if the unsafe character-assignment write
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
                issues.append(f"Unit {unit_id}: {sigil_name} assigned to unknown owner 0x{owner_i:08X}")
        if show_message:
            if issues:
                QMessageBox.warning(self, "Sigil owner validation", "Unknown character assignment references found:\n\n" + "\n".join(issues[:40]))
            else:
                QMessageBox.information(self, "Sigil owner validation", "No invalid sigil owner references found.")
        return issues

    def _sigils_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 10, 22, 10)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        header = QLabel("Sigils")
        header.setObjectName("pageHeader")
        header.setMaximumHeight(36)
        layout.addWidget(header, 0, Qt.AlignmentFlag.AlignTop)

        help_text = QLabel(
            "Edit current sigils, view reusable empty slots, and add new sigils from the built-in database. Selected sigil fields auto-apply after you change them; Save writes them to disk. "
            "Sigil ID is 2703 / FF8F0A, level is 2704 / FF900A, assigned character is 2706 / FF920A, and the observed active assignment flag is 2707 = 2."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        help_text.setVisible(False)

        self.sigil_count_label = QLabel("Open a save to inspect sigil slots.")
        self.sigil_count_label.setWordWrap(False)
        self.sigil_count_label.setObjectName("subtleText")
        self.sigil_count_label.setMaximumHeight(24)
        layout.addWidget(self.sigil_count_label, 0, Qt.AlignmentFlag.AlignTop)

        self.sigil_tabs = QTabWidget()
        self.sigil_tabs.setObjectName("editorTabs")
        self.sigil_tabs.currentChanged.connect(lambda *_: self._refresh_current_sigil_tab())

        current_tab = QWidget()
        current_layout = QVBoxLayout(current_tab)
        current_layout.setContentsMargins(8, 6, 8, 6)
        current_layout.setSpacing(6)

        self.sigil_filter_edit = QLineEdit()
        self.sigil_filter_edit.setPlaceholderText("Filter current sigils by name, GBID, hash, level, character assignment 2706, flags, or unit id...")
        self._connect_debounced_text_changed(self.sigil_filter_edit, "sigils_filter", self.refresh_sigil_rows, 180)
        self.sigil_filter_edit.setMinimumHeight(32)
        self.sigil_filter_edit.setMaximumHeight(34)
        self.sigil_filter_edit.setFont(QFont("Segoe UI", 10))
        current_layout.addWidget(self.sigil_filter_edit)

        sigil_filter_row = QHBoxLayout()
        self.sigil_show_empty_check = QCheckBox("Show empty slots")
        self.sigil_known_only_check = QCheckBox("Known only")
        self.sigil_unknown_only_check = QCheckBox("Unknown only")
        self.sigil_invalid_owner_only_check = QCheckBox("Invalid owner only")
        self.sigil_show_technical_check = QCheckBox("Technical columns")
        self.sigil_known_only_check.setToolTip("Show only slots whose sigil hash resolves to a known GBID/name.")
        self.sigil_unknown_only_check.setToolTip("Show only non-empty sigil slots whose hash is not in the database yet.")
        self.sigil_invalid_owner_only_check.setToolTip("Show assigned sigils whose owner hash is not one of the known character hashes.")
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
        # No long inline tip label in compact layout.
        sigil_filter_row.addStretch(1)
        current_layout.addLayout(sigil_filter_row, 0)

        self.sigil_table = QTableView()
        self.sigil_table.setModel(self.sigil_model)
        self._table_clean(self.sigil_table, hidden_columns=(0, 3, 4, 10, 11, 12))
        self.sigil_table.verticalHeader().setDefaultSectionSize(21 if getattr(self, "compact_mode", True) else 28)
        self.sigil_table.setFont(QFont("Segoe UI", 9))
        self.sigil_table.setMinimumHeight(190)
        self.sigil_table.setMaximumHeight(190)
        self.sigil_table.setFixedHeight(190)
        self.sigil_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._apply_sigil_column_visibility()
        self.sigil_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.sigil_table.selectionModel().selectionChanged.connect(lambda *_: self.update_sigil_detail())
        self.sigil_table.doubleClicked.connect(lambda _: self.edit_selected_sigil_level())
        current_layout.addWidget(self.sigil_table, 0)

        detail = make_card("Selected Sigil · Inline Editor")
        self._set_compact_detail(detail, max_height=335)
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(16, 12, 16, 12)
        detail_layout.setSpacing(7)
        self.sigil_detail_label = QPlainTextEdit()
        self.sigil_detail_label.setReadOnly(True)
        self.sigil_detail_label.setMinimumHeight(0)
        self.sigil_detail_label.setMaximumHeight(0)
        self.sigil_detail_label.setVisible(False)
        self.sigil_detail_label.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.sigil_detail_label.setPlainText("Select a sigil row, then edit the sigil, level, assigned character 2706, and flags. In-game assigned rows use 2706 = character hash and 2707 = 2.")
        self.sigil_detail_label.setObjectName("detailText")
        self.sigil_detail_label.setFont(QFont("Consolas", 10))
        self.sigil_detail_label.setStyleSheet("QPlainTextEdit#detailText { padding: 10px; }")
        self.sigil_detail_label.setToolTip("Read-only selected sigil summary.")
        detail_layout.addWidget(self.sigil_detail_label)

        sigil_grid = QGridLayout()
        sigil_grid.setHorizontalSpacing(8)
        sigil_grid.setVerticalSpacing(7)
        self.sigil_identity_edit = QLineEdit(); self.sigil_identity_edit.setPlaceholderText("GBID, sigil name, decimal hash, or 0xHASH")
        self.sigil_level_edit = QLineEdit(); self.sigil_level_edit.setPlaceholderText("Level")
        self.sigil_level_edit.setMinimumWidth(170)
        self.sigil_worn_by_combo = QComboBox()
        self.sigil_worn_by_combo.setMinimumHeight(32)
        self.sigil_worn_by_combo.setMinimumWidth(280)
        self.sigil_worn_by_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.sigil_worn_by_combo.setMinimumContentsLength(24)
        self.sigil_worn_by_combo.setToolTip("Sets 2706 / FF920A to a character hash and normalizes 2707 to the in-game active assignment flag 2.")
        for choice in getattr(self, "character_owner_choices", []):
            self.sigil_worn_by_combo.addItem(str(choice.get("label", "")), int(choice.get("hash", EMPTY_HASH)) & 0xFFFFFFFF)
        self.sigil_worn_by_combo.currentIndexChanged.connect(self._sigil_owner_combo_changed)
        self.sigil_worn_by_edit = QLineEdit(); self.sigil_worn_by_edit.setPlaceholderText("Optional raw 2706 hash")
        self.sigil_worn_by_edit.setMinimumHeight(32)
        self.sigil_flags_edit = QLineEdit(); self.sigil_flags_edit.setPlaceholderText("Flags / lock state")
        self.sigil_flags_edit.setMinimumWidth(140)

        trait_choices = self._weapon_trait_choices() if hasattr(self, "_weapon_trait_choices") else []
        self.sigil_trait1_combo = QComboBox()
        self.sigil_trait1_combo.setMinimumWidth(300)
        self.sigil_trait1_combo.setMinimumHeight(32)
        self.sigil_trait1_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.sigil_trait1_level_spin = QSpinBox()
        self.sigil_trait1_level_spin.setRange(0, I32_MAX)
        self.sigil_trait1_level_spin.setValue(15)
        self.sigil_trait1_level_spin.setMinimumWidth(105)
        self.sigil_trait1_level_spin.setMinimumHeight(32)

        self.sigil_trait2_combo = QComboBox()
        self.sigil_trait2_combo.setMinimumWidth(300)
        self.sigil_trait2_combo.setMinimumHeight(32)
        self.sigil_trait2_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.sigil_trait2_level_spin = QSpinBox()
        self.sigil_trait2_level_spin.setRange(0, I32_MAX)
        self.sigil_trait2_level_spin.setValue(15)
        self.sigil_trait2_level_spin.setMinimumWidth(105)
        self.sigil_trait2_level_spin.setMinimumHeight(32)
        self._populate_hash_combo(self.sigil_trait1_combo, trait_choices)
        self._populate_hash_combo(self.sigil_trait2_combo, trait_choices)

        for editor in (self.sigil_identity_edit, self.sigil_level_edit, self.sigil_worn_by_edit, self.sigil_flags_edit):
            editor.setMinimumHeight(32)
            editor.setFont(QFont("Segoe UI", 10))
            editor.returnPressed.connect(lambda *_: self.apply_sigil_inline_edits(show_no_change=False))
        self.sigil_identity_edit.textChanged.connect(lambda *_: self._schedule_sigil_field_auto_apply(2, lambda: self.sigil_identity_edit.text(), "sigil", delay_ms=220))
        self.sigil_identity_edit.editingFinished.connect(lambda *_: self._apply_selected_sigil_column_now(2, self.sigil_identity_edit.text(), "sigil", show_status=False))
        self.sigil_worn_by_edit.textChanged.connect(lambda *_: self._schedule_sigil_field_auto_apply(8, lambda: self.sigil_worn_by_edit.text(), "assigned character", delay_ms=220))
        self.sigil_worn_by_edit.editingFinished.connect(lambda *_: self._apply_selected_sigil_column_now(8, self.sigil_worn_by_edit.text(), "assigned character", show_status=False))
        self.sigil_level_edit.textChanged.connect(lambda *_: self._schedule_sigil_field_auto_apply(5, lambda: self.sigil_level_edit.text(), "level", delay_ms=140))
        self.sigil_flags_edit.textChanged.connect(lambda *_: self._schedule_sigil_field_auto_apply(9, lambda: self.sigil_flags_edit.text(), "flags", delay_ms=140))
        self.sigil_trait1_combo.currentIndexChanged.connect(lambda *_: self._apply_selected_sigil_combo_column_now(6, self.sigil_trait1_combo, "trait 1"))
        self.sigil_trait2_combo.currentIndexChanged.connect(lambda *_: self._apply_selected_sigil_combo_column_now(7, self.sigil_trait2_combo, "trait 2"))
        self.sigil_trait1_level_spin.valueChanged.connect(lambda *_: self._apply_selected_sigil_column_now(10, self.sigil_trait1_level_spin.value(), "trait 1 level"))
        self.sigil_trait2_level_spin.valueChanged.connect(lambda *_: self._apply_selected_sigil_column_now(11, self.sigil_trait2_level_spin.value(), "trait 2 level"))
        sigil_grid.addWidget(QLabel("Sigil / GBID / Hash"), 0, 0)
        sigil_grid.addWidget(self.sigil_identity_edit, 0, 1, 1, 3)
        sigil_grid.addWidget(QLabel("Level"), 1, 0)
        sigil_grid.addWidget(self.sigil_level_edit, 1, 1)
        sigil_grid.addWidget(QLabel("Assigned Character"), 1, 2)
        sigil_grid.addWidget(self.sigil_worn_by_combo, 1, 3)
        sigil_grid.addWidget(QLabel("Trait 1"), 2, 0)
        sigil_grid.addWidget(self.sigil_trait1_combo, 2, 1)
        sigil_grid.addWidget(QLabel("T1 Level"), 2, 2)
        sigil_grid.addWidget(self.sigil_trait1_level_spin, 2, 3)
        sigil_grid.addWidget(QLabel("Trait 2"), 3, 0)
        sigil_grid.addWidget(self.sigil_trait2_combo, 3, 1)
        sigil_grid.addWidget(QLabel("T2 Level"), 3, 2)
        sigil_grid.addWidget(self.sigil_trait2_level_spin, 3, 3)
        sigil_grid.addWidget(QLabel("Raw 2706"), 4, 0)
        sigil_grid.addWidget(self.sigil_worn_by_edit, 4, 1)
        sigil_grid.addWidget(QLabel("Flags"), 4, 2)
        sigil_grid.addWidget(self.sigil_flags_edit, 4, 3)
        sigil_grid.setColumnStretch(1, 2)
        sigil_grid.setColumnStretch(3, 3)
        detail_layout.addLayout(sigil_grid)

        sigil_inline_row = QHBoxLayout()
        for text, slot in [
            ("Apply Now / Resync", self.apply_sigil_inline_edits),
            ("Max + Lock", self.max_selected_sigil),
            ("Clear 2706", self.clear_selected_sigil_worn_by),
            ("Remove / Empty", self.remove_selected_sigil_to_empty_slot),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); sigil_inline_row.addWidget(btn)
        sigil_inline_row.addStretch(1)
        detail_layout.addLayout(sigil_inline_row)
        current_layout.addWidget(detail, 0)

        row = QHBoxLayout()
        for text, slot in [
            ("Add Sigil", self.add_sigil_to_empty_slot),
            ("Remove Selected", self.remove_selected_sigil_to_empty_slot),
            ("Show Empty", self.show_empty_sigils_in_current_table),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); row.addWidget(btn)
        row.addWidget(self._make_more_button("More", [
            ("Change Selected Sigil", self.edit_selected_sigil_hash),
            ("Lock Selected", lambda: self.set_selected_sigil_lock(True)),
            ("Unlock Selected", lambda: self.set_selected_sigil_lock(False)),
            ("Batch Add Sigils From Text", self.batch_add_sigils_to_empty_slots),
            ("Duplicate Sigil to Empty Slot", self.duplicate_selected_sigil_to_empty_slot),
            ("Remove Selected to Empty Slot", self.remove_selected_sigil_to_empty_slot),
            ("Copy Sigil Slot", self.copy_selected_sigil_slot),
            ("Paste Sigil Slot", self.paste_sigil_slot_to_selected),
            ("Swap With Copied Sigil Slot", self.swap_selected_sigil_with_copied),
            ("Max Visible Levels", self.bulk_set_visible_sigil_level),
            ("Max Visible + Lock", self.max_visible_sigils),
            ("Assign to Character 2706", self.edit_selected_sigil_worn_by),
            ("Repair / Sanitize Sigil Slots", self.repair_added_sigil_slots),
            ("Clear Character Assignment", self.clear_selected_sigil_worn_by),
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
        db_help = QLabel("Add sigils into reusable empty 2703/2704 slots. Levels may use signed 32-bit max. Save Wizard pattern: assigned rows use 2706 = character hash and 2707 = 2; unassigned locked inventory rows commonly use 2707 = 3.")
        db_help.setWordWrap(True)
        db_help.setObjectName("helpText")
        db_layout.addWidget(db_help)
        db_tools = QHBoxLayout()
        self.sigil_database_filter_edit = QLineEdit()
        self.sigil_database_filter_edit.setPlaceholderText("Search sigil database by name, GBID, hash, family, V, V+, Damage Cap, Supplementary...")
        self._connect_debounced_text_changed(self.sigil_database_filter_edit, "sigils_database_filter", self.refresh_sigil_database_rows, 220)
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
        self.sigil_database_locked_check = QCheckBox("Lock if unassigned (2707=3)")
        self.sigil_database_locked_check.setChecked(True)
        db_tools.addWidget(self.sigil_database_locked_check)
        self.sigil_database_assign_combo = QComboBox()
        self.sigil_database_assign_combo.addItem("None / Unassigned", EMPTY_HASH)
        for choice in self.character_owner_choices[1:]:
            self.sigil_database_assign_combo.addItem(str(choice.get("label", "")), int(choice.get("hash", EMPTY_HASH)) & 0xFFFFFFFF)
        self.sigil_database_assign_combo.setMinimumWidth(240)
        db_tools.addWidget(QLabel("Assign To"))
        db_tools.addWidget(self.sigil_database_assign_combo, 1)
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
            ("Import Best Templates", self.import_sigil_templates),
            ("Show Empty In Current", self.show_empty_sigils_in_current_table),
        ]:
            btn = QPushButton(text); btn.clicked.connect(slot); db_actions.addWidget(btn)
        db_actions.addStretch(1)
        db_layout.addLayout(db_actions)

        # Empty slots are shown inside Current Sigils via the Show Empty button.

        self.sigil_tabs.addTab(current_tab, "Current Sigils")
        self.sigil_tabs.addTab(database_tab, "Database / Add")
        layout.addWidget(self.sigil_tabs, 1)
        self._install_common_numeric_validators()
        return page





    def _wrightstone_choices(self) -> List[Dict[str, Any]]:
        cached = getattr(self, "wrightstone_choices_cache", None)
        if cached is not None:
            return cached
        choices: List[Dict[str, Any]] = [{"label": "None / Empty", "hash": EMPTY_HASH, "gbid": "", "name": "None / Empty"}]
        try:
            entries = []
            for entry in self.item_db.by_hash.values():
                cat = str(getattr(entry, "category", "") or "").lower()
                gbid = str(getattr(entry, "item_id", "") or "").upper()
                name = str(getattr(entry, "display_name", "") or "").lower()
                if "wrightstone" in cat or "wrightstone" in name or gbid.startswith("ITEM_25_") or gbid.startswith("ITEM_26_") or gbid.startswith("ITEM_27_") or gbid.startswith("ITEM_28_") or gbid.startswith("ITEM_29_"):
                    entries.append(entry)
            def _sort_key(entry):
                return (str(getattr(entry, "category", "")), str(getattr(entry, "item_id", "")), str(getattr(entry, "display_name", "")))
            for entry in sorted(entries, key=_sort_key):
                choices.append({
                    "label": f"{entry.display_name} ({entry.item_id})",
                    "hash": int(entry.hash_value) & 0xFFFFFFFF,
                    "gbid": str(entry.item_id),
                    "name": str(entry.display_name),
                })
        except Exception:
            pass
        self.wrightstone_choices_cache = choices
        return choices

    def _set_hash_combo_current_value(self, combo: QComboBox, current: Optional[int] = None) -> None:
        if combo is None or current is None:
            return
        try:
            target_int = int(current) & 0xFFFFFFFF
        except Exception:
            return
        combo.blockSignals(True)
        try:
            for i in range(combo.count()):
                data = combo.itemData(i)
                if data is not None and int(data) & 0xFFFFFFFF == target_int:
                    combo.setCurrentIndex(i)
                    break
        except Exception:
            pass
        combo.blockSignals(False)

    def _populate_hash_combo(self, combo: QComboBox, choices: List[Dict[str, Any]], current: Optional[int] = None) -> None:
        if combo is None:
            return
        old = combo.currentData()
        target = current if current is not None else old
        # ComboBox rebuilds are surprisingly expensive on large pages; only
        # populate once unless the choice count changes.
        if combo.count() != len(choices):
            combo.blockSignals(True)
            combo.clear()
            for choice in choices:
                combo.addItem(str(choice.get("label")), int(choice.get("hash", EMPTY_HASH)) & 0xFFFFFFFF)
            combo.blockSignals(False)
        self._set_hash_combo_current_value(combo, target)

    def populate_wrightstone_editors(self) -> None:
        if hasattr(self, "wrightstone_hash_combo"):
            self._populate_hash_combo(self.wrightstone_hash_combo, self._wrightstone_choices())
        trait_choices = self._weapon_trait_choices() if hasattr(self, "_weapon_trait_choices") else []
        if hasattr(self, "wrightstone_trait1_combo"):
            self._populate_hash_combo(self.wrightstone_trait1_combo, trait_choices)
        if hasattr(self, "wrightstone_trait2_combo"):
            self._populate_hash_combo(self.wrightstone_trait2_combo, trait_choices)
        if hasattr(self, "wrightstone_trait3_combo"):
            self._populate_hash_combo(self.wrightstone_trait3_combo, trait_choices)

    def _sigil_trait_grouped(self) -> Dict[int, Dict[int, UnitRecord]]:
        if not self.save:
            return {}
        cached = getattr(self, "_sigil_trait_grouped_cache", None)
        if cached is not None:
            return cached
        try:
            cached = self.save.group_by_unit([1701, 1702])
        except Exception:
            cached = {}
        self._sigil_trait_grouped_cache = cached
        return cached

    def _sigil_trait_unit_for_sigil_unit(self, sigil_unit_id: int, lane: int) -> int:
        return SIGIL_TRAIT_UNIT_BASE + (int(sigil_unit_id) - 30000) * 100 + int(lane)

    def _sigil_trait_fields_for_sigil_unit(self, sigil_unit_id: int, lane: int) -> Dict[int, UnitRecord]:
        if not self.save:
            return {}
        try:
            unit_id = self._sigil_trait_unit_for_sigil_unit(int(sigil_unit_id), int(lane))
            return dict(self._sigil_trait_grouped().get(unit_id, {}) or {})
        except Exception:
            return {}

    def _sigil_trait_display(self, trait_hash: Any, trait_level: Any) -> str:
        try:
            h = int(trait_hash or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            h = EMPTY_HASH
        if h in (0, EMPTY_HASH):
            return "—"
        name, _gbid, hx = self.hash_entry_parts(h)
        label = name or hx or f"0x{h:08X}"
        # Strip Roman numeral suffix for cleaner display
        import re
        label = re.sub(r'\s+(I|II|III|IV|V|V\+|VI|VII|VIII|IX|X)\s*$', '', label.strip())
        label = re.sub(r'\s*\[\w+\]\s*$', '', label.strip())
        try:
            lv = int(trait_level or 0)
        except Exception:
            lv = 0
        return f"{label} Lv {lv}" if lv else str(label)

    def _sigil_trait_meta_records_for_unit(self, sigil_unit_id: int) -> Dict[str, Any]:
        try:
            t1 = self._sigil_trait_fields_for_sigil_unit(int(sigil_unit_id), 0)
            t2 = self._sigil_trait_fields_for_sigil_unit(int(sigil_unit_id), 1)
            return {
                "trait1_hash_rec": t1.get(1701),
                "trait1_level_rec": t1.get(1702),
                "trait2_hash_rec": t2.get(1701),
                "trait2_level_rec": t2.get(1702),
                "trait1_unit": self._sigil_trait_unit_for_sigil_unit(int(sigil_unit_id), 0),
                "trait2_unit": self._sigil_trait_unit_for_sigil_unit(int(sigil_unit_id), 1),
            }
        except Exception:
            return {}

    def _wrightstone_trait_grouped(self) -> Dict[int, Dict[int, UnitRecord]]:
        if not self.save:
            return {}
        cached = getattr(self, "_wrightstone_trait_grouped_cache", None)
        if cached is not None:
            return cached
        try:
            cached = self.save.group_by_unit([1701, 1702])
        except Exception:
            cached = {}
        self._wrightstone_trait_grouped_cache = cached
        return cached


    def _wrightstone_slot_fields(self) -> Dict[int, Dict[int, UnitRecord]]:
        if not self.save:
            return {}
        cached = getattr(self, "_wrightstone_slot_grouped_cache", None)
        if cached is not None:
            return cached
        try:
            cached = self.save.group_by_unit([2102, 2103, 2104, 2105])
        except Exception:
            cached = {}
        self._wrightstone_slot_grouped_cache = cached
        return cached

    def _wrightstone_hash_display(self, value: Any, empty_label: str = "") -> Tuple[str, str, str]:
        try:
            h = int(value or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            h = EMPTY_HASH
        if h in (0, EMPTY_HASH):
            return empty_label, "", ""
        return self.hash_entry_parts(h)

    def _wrightstones_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        header = QLabel("Wrightstones")
        header.setObjectName("pageHeader")
        layout.addWidget(header)

        help_text = QLabel(
            "Edit real Wrightstone rows and their up to three linked trait rows. Wrightstone inventory uses 2102-2105 on units 50000-54999; linked traits use 1701/1702 on units 140000000 + slot*100 + lane. Detail controls auto-apply after each change; Save writes them to disk."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        self.wrightstone_status_label = QLabel("Open a save to inspect real Wrightstone slots. Mapping: 2102 stone hash, 2103 value, 2104 active, 2105 flags, 140000000-series linked traits.")
        self.wrightstone_status_label.setWordWrap(True)
        self.wrightstone_status_label.setObjectName("subtleText")
        layout.addWidget(self.wrightstone_status_label)

        tools = QHBoxLayout()
        self.wrightstone_filter_edit = QLineEdit()
        self.wrightstone_filter_edit.setPlaceholderText("Filter wrightstones by name, GBID, hash, trait, slot, or unit...")
        self._connect_debounced_text_changed(self.wrightstone_filter_edit, "wrightstones_filter", self.refresh_wrightstone_rows, 180)
        tools.addWidget(self.wrightstone_filter_edit, 3)

        self.wrightstone_show_empty_check = QCheckBox("Show empty")
        self.wrightstone_show_empty_check.toggled.connect(lambda *_: self.refresh_wrightstone_rows())
        tools.addWidget(self.wrightstone_show_empty_check)

        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_wrightstone_rows)
        tools.addWidget(refresh_btn)
        tools.addStretch(1)
        layout.addLayout(tools)

        self.wrightstone_table = QTableView()
        self.wrightstone_table.setModel(self.wrightstone_model)
        self._table_clean(self.wrightstone_table, hidden_columns=(3, 12))
        self.wrightstone_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.wrightstone_table.selectionModel().selectionChanged.connect(lambda *_: self.update_wrightstone_detail())
        layout.addWidget(self.wrightstone_table, 1)

        detail = make_card("Selected Wrightstone")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setSpacing(10)

        self.wrightstone_detail_label = QLabel("Select a wrightstone row.")
        self.wrightstone_detail_label.setWordWrap(True)
        self.wrightstone_detail_label.setObjectName("subtleText")
        detail_layout.addWidget(self.wrightstone_detail_label)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)

        self.wrightstone_hash_combo = QComboBox()
        self.wrightstone_hash_combo.setMinimumWidth(360)
        self.wrightstone_hash_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.wrightstone_value_spin = QSpinBox()
        self.wrightstone_value_spin.setRange(0, I32_MAX)
        self.wrightstone_value_spin.setMinimumWidth(130)

        self.wrightstone_trait1_combo = QComboBox()
        self.wrightstone_trait1_combo.setMinimumWidth(360)
        self.wrightstone_trait1_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.wrightstone_trait1_level_spin = QSpinBox()
        self.wrightstone_trait1_level_spin.setRange(0, I32_MAX)
        self.wrightstone_trait1_level_spin.setValue(15)
        self.wrightstone_trait1_level_spin.setMinimumWidth(100)

        self.wrightstone_trait2_combo = QComboBox()
        self.wrightstone_trait2_combo.setMinimumWidth(360)
        self.wrightstone_trait2_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.wrightstone_trait2_level_spin = QSpinBox()
        self.wrightstone_trait2_level_spin.setRange(0, I32_MAX)
        self.wrightstone_trait2_level_spin.setValue(15)
        self.wrightstone_trait2_level_spin.setMinimumWidth(100)

        self.wrightstone_trait3_combo = QComboBox()
        self.wrightstone_trait3_combo.setMinimumWidth(360)
        self.wrightstone_trait3_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        self.wrightstone_trait3_level_spin = QSpinBox()
        self.wrightstone_trait3_level_spin.setRange(0, I32_MAX)
        self.wrightstone_trait3_level_spin.setValue(15)
        self.wrightstone_trait3_level_spin.setMinimumWidth(100)

        for combo in (self.wrightstone_hash_combo, self.wrightstone_trait1_combo, self.wrightstone_trait2_combo, self.wrightstone_trait3_combo):
            combo.currentIndexChanged.connect(lambda *_: self._schedule_wrightstone_auto_apply())
        for spin in (self.wrightstone_value_spin, self.wrightstone_trait1_level_spin, self.wrightstone_trait2_level_spin, self.wrightstone_trait3_level_spin):
            spin.valueChanged.connect(lambda *_: self._schedule_wrightstone_auto_apply())

        grid.addWidget(QLabel("Wrightstone"), 0, 0)
        grid.addWidget(self.wrightstone_hash_combo, 0, 1)
        grid.addWidget(QLabel("Value"), 0, 2)
        grid.addWidget(self.wrightstone_value_spin, 0, 3)

        grid.addWidget(QLabel("Trait 1"), 1, 0)
        grid.addWidget(self.wrightstone_trait1_combo, 1, 1)
        grid.addWidget(QLabel("Level"), 1, 2)
        grid.addWidget(self.wrightstone_trait1_level_spin, 1, 3)

        grid.addWidget(QLabel("Trait 2"), 2, 0)
        grid.addWidget(self.wrightstone_trait2_combo, 2, 1)
        grid.addWidget(QLabel("Level"), 2, 2)
        grid.addWidget(self.wrightstone_trait2_level_spin, 2, 3)

        grid.addWidget(QLabel("Trait 3"), 3, 0)
        grid.addWidget(self.wrightstone_trait3_combo, 3, 1)
        grid.addWidget(QLabel("Level"), 3, 2)
        grid.addWidget(self.wrightstone_trait3_level_spin, 3, 3)
        grid.setColumnStretch(1, 3)
        detail_layout.addLayout(grid)

        actions = QHBoxLayout()
        for text, slot in [
            ("Apply Selected / Resync", self.apply_selected_wrightstone_edit),
            ("Clear Selected", self.clear_selected_wrightstone),
            ("Open Weapons", lambda: self._show_page("Weapons")),
        ]:
            btn = QPushButton(text)
            if text == "Apply Selected":
                btn.setProperty("class", "primaryButton")
            btn.clicked.connect(slot)
            actions.addWidget(btn)
        actions.addStretch(1)
        detail_layout.addLayout(actions)

        layout.addWidget(detail)
        return page

    def refresh_wrightstone_rows(self) -> None:
        if not hasattr(self, "wrightstone_model"):
            return
        if not self.save:
            self.wrightstone_model.set_rows([])
            self.wrightstone_rows_meta = []
            if hasattr(self, "wrightstone_status_label"):
                self.wrightstone_status_label.setText("Open a save to inspect wrightstone slots.")
            return

        self._wrightstone_slot_grouped_cache = None
        self._wrightstone_trait_grouped_cache = None
        self.populate_wrightstone_editors()
        grouped = self._wrightstone_slot_fields()
        trait_grouped = self._wrightstone_trait_grouped()
        # Corrected mapping: 140000000-series is 5000 slots x 3 lanes for Wrightstones.
        # 120000000-series is 5100 slots x 2 lanes and matches sigil/gem trait storage.
        rows: List[List[Any]] = []
        meta_rows: List[Dict[str, Any]] = []
        q = getattr(self, "wrightstone_filter_edit", None).text().strip().lower() if hasattr(self, "wrightstone_filter_edit") else ""
        show_empty = bool(getattr(getattr(self, "wrightstone_show_empty_check", None), "isChecked", lambda: False)())

        total = active = empty = hidden_special = 0
        for unit_id, fields in sorted(grouped.items()):
            try:
                unit_int = int(unit_id)
            except Exception:
                continue
            if not (50000 <= unit_int <= 54999):
                continue
            total += 1
            slot = unit_int - 50000
            stone_hash_value = self._record_first_value(fields.get(2102), EMPTY_HASH)
            value_2103 = self._record_first_value(fields.get(2103), 0)
            active_2104 = self._record_first_value(fields.get(2104), False)
            flags_2105 = self._record_first_value(fields.get(2105), 0)

            trait1_fields = dict(trait_grouped.get(WRIGHTSTONE_TRAIT_UNIT_BASE + slot * 100, {}) or {})
            trait2_fields = dict(trait_grouped.get(WRIGHTSTONE_TRAIT_UNIT_BASE + slot * 100 + 1, {}) or {})
            trait3_fields = dict(trait_grouped.get(WRIGHTSTONE_TRAIT_UNIT_BASE + slot * 100 + 2, {}) or {})
            trait1_hash = self._record_first_value(trait1_fields.get(1701), EMPTY_HASH)
            trait1_level = self._record_first_value(trait1_fields.get(1702), 0)
            trait2_hash = self._record_first_value(trait2_fields.get(1701), EMPTY_HASH)
            trait2_level = self._record_first_value(trait2_fields.get(1702), 0)
            trait3_hash = self._record_first_value(trait3_fields.get(1701), EMPTY_HASH)
            trait3_level = self._record_first_value(trait3_fields.get(1702), 0)

            stone_name, stone_gbid, stone_hx = self._wrightstone_hash_display(stone_hash_value, "<Empty wrightstone slot>")
            t1_name, t1_gbid, _ = self._wrightstone_hash_display(trait1_hash, "")
            t2_name, t2_gbid, _ = self._wrightstone_hash_display(trait2_hash, "")
            t3_name, t3_gbid, _ = self._wrightstone_hash_display(trait3_hash, "")

            is_empty = int(stone_hash_value or EMPTY_HASH) & 0xFFFFFFFF in (0, EMPTY_HASH)
            is_real_wrightstone = self._is_wrightstone_hash(stone_hash_value)
            # Trait rows can exist even when the inventory slot is empty, and
            # the 50000-series also contains potions/currency/special items.
            # The Wrightstones page should only show real Wrightstone entries.
            if is_empty:
                empty += 1
                if not show_empty:
                    continue
                trait1_hash = EMPTY_HASH
                trait1_level = 0
                trait2_hash = EMPTY_HASH
                trait2_level = 0
                trait3_hash = EMPTY_HASH
                trait3_level = 0
                t1_name, t1_gbid = "", ""
                t2_name, t2_gbid = "", ""
                t3_name, t3_gbid = "", ""
            elif not is_real_wrightstone:
                hidden_special += 1
                continue
            else:
                active += 1

            row = [
                slot,
                stone_name,
                stone_gbid,
                stone_hx,
                value_2103,
                t1_name if t1_name else "",
                trait1_level,
                t2_name if t2_name else "",
                trait2_level,
                t3_name if t3_name else "",
                trait3_level,
                f"{int(flags_2105)} / active {bool(active_2104)}",
                unit_int,
            ]
            if q and not self._matches_editor_filter(row, q):
                continue
            rows.append(row)
            meta_rows.append({
                "slot": slot,
                "unit_id": unit_int,
                "is_empty": is_empty,
                "stone_rec": fields.get(2102),
                "value_rec": fields.get(2103),
                "active_rec": fields.get(2104),
                "flags_rec": fields.get(2105),
                "trait1_hash_rec": trait1_fields.get(1701),
                "trait1_level_rec": trait1_fields.get(1702),
                "trait2_hash_rec": trait2_fields.get(1701),
                "trait2_level_rec": trait2_fields.get(1702),
                "trait3_hash_rec": trait3_fields.get(1701),
                "trait3_level_rec": trait3_fields.get(1702),
                "trait1_unit": WRIGHTSTONE_TRAIT_UNIT_BASE + slot * 100,
                "trait2_unit": WRIGHTSTONE_TRAIT_UNIT_BASE + slot * 100 + 1,
                "trait3_unit": WRIGHTSTONE_TRAIT_UNIT_BASE + slot * 100 + 2,
            })

        self.wrightstone_rows_meta = meta_rows
        self.wrightstone_model.set_rows(rows)
        if hasattr(self, "wrightstone_status_label"):
            hidden_note = f" · {self.format_value(hidden_special)} non-wrightstone special rows hidden" if hidden_special else ""
            if active == 0 and not show_empty:
                self.wrightstone_status_label.setText(
                    f"Wrightstone slots: 0 active / {self.format_value(total)} total · "
                    f"{self.format_value(empty)} empty{hidden_note}. Enable Show empty to inspect reusable slots."
                )
            else:
                self.wrightstone_status_label.setText(
                    f"Wrightstone slots: {self.format_value(active)} active / {self.format_value(total)} total · "
                    f"{self.format_value(empty)} empty · showing {self.format_value(len(rows))}{hidden_note}"
                )
        if hasattr(self, "wrightstone_table"):
            self._set_table_widths(self.wrightstone_table, {0: 70, 1: 320, 2: 150, 4: 80, 5: 240, 6: 80, 7: 240, 8: 80, 9: 240, 10: 80, 11: 140})
        self.update_wrightstone_detail()

    def _selected_wrightstone_meta(self) -> Optional[Dict[str, Any]]:
        if not hasattr(self, "wrightstone_table"):
            return None
        return self._selected_meta(self.wrightstone_table, self.wrightstone_rows_meta)

    def update_wrightstone_detail(self) -> None:
        self._updating_wrightstone_detail = True
        try:
            meta = self._selected_wrightstone_meta()
            if not hasattr(self, "wrightstone_detail_label"):
                return
            if not meta:
                self.wrightstone_detail_label.setText("Select a wrightstone row.")
                return
            stone_hash = self._record_first_value(meta.get("stone_rec"), EMPTY_HASH)
            value = self._record_first_value(meta.get("value_rec"), 0)
            stone_is_empty = int(stone_hash or EMPTY_HASH) & 0xFFFFFFFF in (0, EMPTY_HASH)
            if stone_is_empty:
                t1 = EMPTY_HASH
                t1_lv = 0
                t2 = EMPTY_HASH
                t2_lv = 0
                t3 = EMPTY_HASH
                t3_lv = 0
            else:
                t1 = self._record_first_value(meta.get("trait1_hash_rec"), EMPTY_HASH)
                t1_lv = self._record_first_value(meta.get("trait1_level_rec"), 0)
                t2 = self._record_first_value(meta.get("trait2_hash_rec"), EMPTY_HASH)
                t2_lv = self._record_first_value(meta.get("trait2_level_rec"), 0)
                t3 = self._record_first_value(meta.get("trait3_hash_rec"), EMPTY_HASH)
                t3_lv = self._record_first_value(meta.get("trait3_level_rec"), 0)
            stone_name, stone_gbid, stone_hx = self._wrightstone_hash_display(stone_hash, "Empty")
            t1_name, t1_gbid, _ = self._wrightstone_hash_display(t1, "None")
            t2_name, t2_gbid, _ = self._wrightstone_hash_display(t2, "None")
            t3_name, t3_gbid, _ = self._wrightstone_hash_display(t3, "None")
            self.wrightstone_detail_label.setText(
                f"Slot {meta.get('slot')} · Unit {meta.get('unit_id')}\n"
                f"Wrightstone: {stone_name} {f'({stone_gbid})' if stone_gbid else ''} {stone_hx}\n"
                f"Trait 1: {t1_name} {f'({t1_gbid})' if t1_gbid else ''} · Level {t1_lv} · Unit {meta.get('trait1_unit')}\n"
                f"Trait 2: {t2_name} {f'({t2_gbid})' if t2_gbid else ''} · Level {t2_lv} · Unit {meta.get('trait2_unit')}\n"
                f"Trait 3: {t3_name} {f'({t3_gbid})' if t3_gbid else ''} · Level {t3_lv} · Unit {meta.get('trait3_unit')}"
            )
            if hasattr(self, "wrightstone_hash_combo"):
                if self.wrightstone_hash_combo.count() == 0:
                    self._populate_hash_combo(self.wrightstone_hash_combo, self._wrightstone_choices(), stone_hash)
                else:
                    self._set_hash_combo_current_value(self.wrightstone_hash_combo, stone_hash)
            if hasattr(self, "wrightstone_value_spin"):
                self.wrightstone_value_spin.blockSignals(True)
                self.wrightstone_value_spin.setValue(max(0, min(I32_MAX, int(value or 0))))
                self.wrightstone_value_spin.blockSignals(False)
            trait_choices = self._weapon_trait_choices() if hasattr(self, "_weapon_trait_choices") else []
            if hasattr(self, "wrightstone_trait1_combo"):
                if self.wrightstone_trait1_combo.count() == 0:
                    self._populate_hash_combo(self.wrightstone_trait1_combo, trait_choices, t1)
                else:
                    self._set_hash_combo_current_value(self.wrightstone_trait1_combo, t1)
            if hasattr(self, "wrightstone_trait2_combo"):
                if self.wrightstone_trait2_combo.count() == 0:
                    self._populate_hash_combo(self.wrightstone_trait2_combo, trait_choices, t2)
                else:
                    self._set_hash_combo_current_value(self.wrightstone_trait2_combo, t2)
            if hasattr(self, "wrightstone_trait3_combo"):
                if self.wrightstone_trait3_combo.count() == 0:
                    self._populate_hash_combo(self.wrightstone_trait3_combo, trait_choices, t3)
                else:
                    self._set_hash_combo_current_value(self.wrightstone_trait3_combo, t3)
            if hasattr(self, "wrightstone_trait1_level_spin"):
                self.wrightstone_trait1_level_spin.blockSignals(True)
                self.wrightstone_trait1_level_spin.setValue(max(0, min(I32_MAX, int(t1_lv or 0))))
                self.wrightstone_trait1_level_spin.blockSignals(False)
            if hasattr(self, "wrightstone_trait2_level_spin"):
                self.wrightstone_trait2_level_spin.blockSignals(True)
                self.wrightstone_trait2_level_spin.setValue(max(0, min(I32_MAX, int(t2_lv or 0))))
                self.wrightstone_trait2_level_spin.blockSignals(False)
            if hasattr(self, "wrightstone_trait3_level_spin"):
                self.wrightstone_trait3_level_spin.blockSignals(True)
                self.wrightstone_trait3_level_spin.setValue(max(0, min(I32_MAX, int(t3_lv or 0))))
                self.wrightstone_trait3_level_spin.blockSignals(False)
        finally:
            self._updating_wrightstone_detail = False

    def _schedule_wrightstone_auto_apply(self) -> None:
        if getattr(self, "_updating_wrightstone_detail", False):
            return
        if bool(getattr(self, "_save_in_progress", False)) or bool(getattr(self, "_load_in_progress", False)):
            return
        if not getattr(self, "save", None) or not hasattr(self, "wrightstone_table"):
            return
        idx = self.wrightstone_table.currentIndex()
        if not idx.isValid() or idx.row() < 0:
            return
        timer = getattr(self, "_wrightstone_auto_apply_timer", None)
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._run_wrightstone_auto_apply)
            self._wrightstone_auto_apply_timer = timer
        timer.start(180)

    def _run_wrightstone_auto_apply(self) -> None:
        if bool(getattr(self, "_save_in_progress", False)) or bool(getattr(self, "_load_in_progress", False)):
            return
        self.apply_selected_wrightstone_edit(auto=True)

    def apply_selected_wrightstone_edit(self, *_args, auto: bool = False) -> None:
        if not self.save:
            if not auto:
                QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        meta = self._selected_wrightstone_meta()
        if not meta:
            if not auto:
                self.statusBar().showMessage("Select a wrightstone row first.", 3000)
            return
        changed = 0
        stone_hash = int(self.wrightstone_hash_combo.currentData()) & 0xFFFFFFFF if hasattr(self, "wrightstone_hash_combo") and self.wrightstone_hash_combo.currentData() is not None else EMPTY_HASH
        if stone_hash not in (0, EMPTY_HASH) and not self._is_wrightstone_hash(stone_hash):
            QMessageBox.warning(self, "Not a wrightstone", "The selected item is not categorized as a Wrightstone.")
            return
        value = int(self.wrightstone_value_spin.value()) if hasattr(self, "wrightstone_value_spin") else 0
        t1 = int(self.wrightstone_trait1_combo.currentData()) & 0xFFFFFFFF if hasattr(self, "wrightstone_trait1_combo") and self.wrightstone_trait1_combo.currentData() is not None else EMPTY_HASH
        t1_lv = int(self.wrightstone_trait1_level_spin.value()) if hasattr(self, "wrightstone_trait1_level_spin") else 0
        t2 = int(self.wrightstone_trait2_combo.currentData()) & 0xFFFFFFFF if hasattr(self, "wrightstone_trait2_combo") and self.wrightstone_trait2_combo.currentData() is not None else EMPTY_HASH
        t2_lv = int(self.wrightstone_trait2_level_spin.value()) if hasattr(self, "wrightstone_trait2_level_spin") else 0
        t3 = int(self.wrightstone_trait3_combo.currentData()) & 0xFFFFFFFF if hasattr(self, "wrightstone_trait3_combo") and self.wrightstone_trait3_combo.currentData() is not None else EMPTY_HASH
        t3_lv = int(self.wrightstone_trait3_level_spin.value()) if hasattr(self, "wrightstone_trait3_level_spin") else 0

        for rec_key, val, label in [
            ("stone_rec", stone_hash, "wrightstone hash 2102 / FF360800"),
            ("value_rec", value, "wrightstone value 2103 / FF370800"),
            ("trait1_hash_rec", t1, "wrightstone trait 1 ID 1701 / FFA50600"),
            ("trait1_level_rec", t1_lv, "wrightstone trait 1 level 1702 / FFA60600"),
            ("trait2_hash_rec", t2, "wrightstone trait 2 ID 1701 / FFA50600"),
            ("trait2_level_rec", t2_lv, "wrightstone trait 2 level 1702 / FFA60600"),
            ("trait3_hash_rec", t3, "wrightstone trait 3 ID 1701 / FFA50600"),
            ("trait3_level_rec", t3_lv, "wrightstone trait 3 level 1702 / FFA60600"),
        ]:
            if meta.get(rec_key) is not None and self._set_record_first_value(meta.get(rec_key), val, label):
                changed += 1
        if stone_hash not in (0, EMPTY_HASH):
            if meta.get("active_rec") is not None and self._set_record_first_value(meta.get("active_rec"), True, "wrightstone active flag 2104"):
                changed += 1
            if meta.get("flags_rec") is not None:
                old_flags = int(self._record_first_value(meta.get("flags_rec"), 0) or 0)
                new_flags = max(1, old_flags)
                if self._set_record_first_value(meta.get("flags_rec"), new_flags, "wrightstone flags 2105"):
                    changed += 1
        self._mark_stale_pages(["Wrightstones", "Weapons", "Save Health"])
        self.refresh_wrightstone_rows()
        self.statusBar().showMessage(f"Wrightstone updated: {changed} field(s) changed. Save As to test.", 6000)

    def clear_selected_wrightstone(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        meta = self._selected_wrightstone_meta()
        if not meta:
            self.statusBar().showMessage("Select a wrightstone row first.", 3000)
            return
        if QMessageBox.question(self, "Clear wrightstone", "Clear selected wrightstone slot and its three linked trait slots?") != QMessageBox.StandardButton.Yes:
            return
        changed = 0
        for rec_key, val, label in [
            ("stone_rec", EMPTY_HASH, "wrightstone hash 2102"),
            ("value_rec", 0, "wrightstone value 2103"),
            ("active_rec", False, "wrightstone active 2104"),
            ("flags_rec", 0, "wrightstone flags 2105"),
            ("trait1_hash_rec", EMPTY_HASH, "trait 1 ID 1701"),
            ("trait1_level_rec", 0, "trait 1 level 1702"),
            ("trait2_hash_rec", EMPTY_HASH, "trait 2 ID 1701"),
            ("trait2_level_rec", 0, "trait 2 level 1702"),
            ("trait3_hash_rec", EMPTY_HASH, "trait 3 ID 1701"),
            ("trait3_level_rec", 0, "trait 3 level 1702"),
        ]:
            if meta.get(rec_key) is not None and self._set_record_first_value(meta.get(rec_key), val, label):
                changed += 1
        self._mark_stale_pages(["Wrightstones", "Weapons", "Save Health"])
        self.refresh_wrightstone_rows()
        prefix = "Auto-applied" if auto else "Applied"
        self.statusBar().showMessage(f"{prefix} wrightstone edit: {changed} field(s) changed. Save when ready.", 5000)






    def _apply_value_preset_to_line_edit(self, combo: QComboBox, edit: QLineEdit) -> None:
        data = combo.currentData()
        if data is None:
            return
        edit.setText(str(data))



    def _mastery_mods_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        header = QLabel("Mastery")
        header.setObjectName("pageHeader")
        layout.addWidget(header)

        help_text = QLabel("1606 / FF460600 = mastery ID. 1607 / FF470600 = paired value. Use Overmastery for the 4 visible slots; use Basic Sweep only when you intentionally want to write the 0x258 basic mastery rows.")
        help_text.setWordWrap(True)
        help_text.setObjectName("helpText")
        layout.addWidget(help_text)

        target_card = make_card("Character / group")
        target_layout = QHBoxLayout(target_card)
        target_layout.setSpacing(10)
        target_layout.addWidget(QLabel("Target"))
        self.mastery_mod_character_combo = QComboBox()
        self.mastery_mod_character_combo.setMinimumWidth(360)
        self.mastery_mod_character_combo.currentIndexChanged.connect(lambda *_: self._on_mastery_mod_character_changed())
        target_layout.addWidget(self.mastery_mod_character_combo, 1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._on_mastery_mod_character_changed)
        target_layout.addWidget(refresh_btn)
        update_names_btn = QPushButton("Update Names")
        update_names_btn.clicked.connect(self.download_mastery_mod_id_search_db)
        target_layout.addWidget(update_names_btn)
        self.mastery_mod_status_label = QLabel("Open a save, then pick a character/group.")
        self.mastery_mod_status_label.setObjectName("subtleText")
        self.mastery_mod_status_label.setWordWrap(True)
        target_layout.addWidget(self.mastery_mod_status_label, 2)
        layout.addWidget(target_card)

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

        over_card = make_card("Overmastery 4-Lane Editor")
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

        value_grid = QGridLayout()
        value_grid.setHorizontalSpacing(10)
        value_grid.setVerticalSpacing(6)
        self.mastery_overmastery_value_edit = QLineEdit(str(OVERMASTERY_VALUE_MAX))
        self.mastery_overmastery_value_edit.setToolTip("0x03FF / 1023 = max / 80%; 0x0200 / 512 = normal max / 20%. Legacy -1 writes raw FFFFFFFF only for old test saves.")
        self.mastery_overmastery_value_edit.setMaximumWidth(110)
        self.mastery_overmastery_value_edit.textChanged.connect(lambda *_: self._schedule_overmastery_auto_apply())
        self.mastery_overmastery_value_preset_combo = QComboBox()
        self.mastery_overmastery_value_preset_combo.setMinimumWidth(160)
        for label, value_text in [
            ("80% / 03FF", str(OVERMASTERY_VALUE_MAX)),
            ("20% / 0200", str(OVERMASTERY_VALUE_NORMAL)),
            ("Zero", "0"),
            ("Legacy Raw FF", "-1"),
        ]:
            self.mastery_overmastery_value_preset_combo.addItem(label, value_text)
        self.mastery_overmastery_value_preset_combo.currentIndexChanged.connect(
            lambda *_: self._apply_value_preset_to_line_edit(self.mastery_overmastery_value_preset_combo, self.mastery_overmastery_value_edit)
        )
        self.mastery_overmastery_write_value_check = QCheckBox("Write 1607")
        self.mastery_overmastery_write_value_check.setChecked(True)
        self.mastery_overmastery_write_value_check.toggled.connect(lambda *_: self._schedule_overmastery_auto_apply())
        self.mastery_overmastery_auto_apply_check = QCheckBox("Auto selected")
        self.mastery_overmastery_auto_apply_check.setChecked(True)
        self.mastery_overmastery_apply_all_auto_check = QCheckBox("Auto all 40")
        self.mastery_overmastery_apply_all_auto_check.setChecked(False)
        value_grid.addWidget(QLabel("Value"), 0, 0)
        value_grid.addWidget(self.mastery_overmastery_value_edit, 0, 1)
        value_grid.addWidget(QLabel("Preset"), 0, 2)
        value_grid.addWidget(self.mastery_overmastery_value_preset_combo, 0, 3)
        value_grid.addWidget(self.mastery_overmastery_write_value_check, 0, 4)
        value_grid.addWidget(self.mastery_overmastery_auto_apply_check, 1, 1, 1, 2)
        value_grid.addWidget(self.mastery_overmastery_apply_all_auto_check, 1, 3, 1, 2)
        over_layout.addLayout(value_grid)

        action_row = QHBoxLayout()
        apply_selected_over_btn = QPushButton("Apply Selected")
        apply_selected_over_btn.clicked.connect(self.apply_overmastery_four_stats_selected)
        apply_all_over_btn = QPushButton("Apply All 40")
        apply_all_over_btn.clicked.connect(self.apply_overmastery_four_stats_all)
        action_row.addWidget(apply_selected_over_btn)
        action_row.addWidget(apply_all_over_btn)
        action_row.addStretch(1)
        over_layout.addLayout(action_row)

        self.mastery_sw_lab_status = QLabel("Ready")
        self.mastery_sw_lab_status.setWordWrap(True)
        self.mastery_sw_lab_status.setObjectName("subtleText")
        over_layout.addWidget(self.mastery_sw_lab_status)

        over_layout_root.addWidget(over_card)

        # ------------------------------------------------------------------
        # Tab 2: basic mastery sweep workflow
        # ------------------------------------------------------------------
        basic_tab = QWidget()
        basic_tab_layout = QVBoxLayout(basic_tab)
        basic_tab_layout.setContentsMargins(12, 12, 12, 12)
        basic_tab_layout.setSpacing(12)

        basic_card = make_card("Basic Masteries Sweep")
        basic_layout = QVBoxLayout(basic_card)
        basic_layout.setSpacing(8)
        basic_note = QLabel("Writes the selected character's basic mastery rows using the newer Save Wizard template: first SlotINFO row as N, count 0x258, stride +0x18. This stays button-based because it can rewrite hundreds of rows.")
        basic_note.setWordWrap(True)
        basic_note.setObjectName("subtleText")
        basic_layout.addWidget(basic_note)

        basic_grid = QGridLayout()
        basic_grid.setHorizontalSpacing(10)
        basic_grid.setVerticalSpacing(6)
        self.mastery_basic_effect_combo = QComboBox()
        self.mastery_basic_effect_combo.setMinimumWidth(520)
        self.mastery_basic_effect_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.mastery_basic_value_edit = QLineEdit(str(OVERMASTERY_VALUE_MAX))
        self.mastery_basic_value_edit.setMaximumWidth(110)
        self.mastery_basic_value_preset_combo = QComboBox()
        self.mastery_basic_value_preset_combo.setMinimumWidth(160)
        for label, value_text in [
            ("80% / 03FF", str(OVERMASTERY_VALUE_MAX)),
            ("20% / 0200", str(OVERMASTERY_VALUE_NORMAL)),
            ("Zero", "0"),
            ("Legacy Raw FF", "-1"),
        ]:
            self.mastery_basic_value_preset_combo.addItem(label, value_text)
        self.mastery_basic_value_preset_combo.currentIndexChanged.connect(
            lambda *_: self._apply_value_preset_to_line_edit(self.mastery_basic_value_preset_combo, self.mastery_basic_value_edit)
        )
        self.mastery_basic_write_effect_check = QCheckBox("Write 1606 IDs")
        self.mastery_basic_write_effect_check.setChecked(True)
        self.mastery_basic_write_value_check = QCheckBox("Write 1607 values")
        self.mastery_basic_write_value_check.setChecked(True)
        basic_grid.addWidget(QLabel("Mastery ID"), 0, 0)
        basic_grid.addWidget(self.mastery_basic_effect_combo, 0, 1, 1, 5)
        basic_grid.addWidget(QLabel("Value"), 1, 0)
        basic_grid.addWidget(self.mastery_basic_value_edit, 1, 1)
        basic_grid.addWidget(QLabel("Preset"), 1, 2)
        basic_grid.addWidget(self.mastery_basic_value_preset_combo, 1, 3)
        basic_grid.addWidget(self.mastery_basic_write_effect_check, 1, 4)
        basic_grid.addWidget(self.mastery_basic_write_value_check, 1, 5)
        basic_apply_btn = QPushButton("Apply Sweep")
        basic_apply_btn.clicked.connect(self.apply_basic_mastery_sweep_selected)
        basic_preview_btn = QPushButton("Preview SlotINFO")
        basic_preview_btn.clicked.connect(self.refresh_basic_mastery_sweep_status)
        basic_grid.addWidget(basic_apply_btn, 2, 3, 1, 2)
        basic_grid.addWidget(basic_preview_btn, 2, 5)
        basic_layout.addLayout(basic_grid)
        self.mastery_basic_status_label = QLabel("Pick a character to preview the first Basic Mastery SlotINFO row.")
        self.mastery_basic_status_label.setWordWrap(True)
        self.mastery_basic_status_label.setObjectName("subtleText")
        basic_layout.addWidget(self.mastery_basic_status_label)
        basic_tab_layout.addWidget(basic_card)
        basic_tab_layout.addStretch(1)

        over_layout_root.addStretch(1)

        # Hidden single-slot controls retained for old helper compatibility only.
        self.mastery_sw_slot_spin = QSpinBox(page)
        self.mastery_sw_slot_spin.setRange(1, 31408)
        self.mastery_sw_slot_spin.setValue(1)
        self.mastery_sw_slot_spin.hide()
        self.mastery_sw_value_spin = QSpinBox(page)
        self.mastery_sw_value_spin.setRange(-1, MASTERY_1607_SAFE_MAX)
        self.mastery_sw_value_spin.setSpecialValueText("Legacy FFFFFFFF")
        self.mastery_sw_value_spin.setValue(OVERMASTERY_VALUE_MAX)
        self.mastery_sw_value_spin.hide()

        # ------------------------------------------------------------------
        # Tab 3: rows / edit
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
        mastery_tabs.addTab(basic_tab, "Basic Sweep")
        mastery_tabs.addTab(rows_tab, "Rows / Edit")
        mastery_tabs.currentChanged.connect(lambda idx: self.refresh_mastery_mod_rows() if idx == 2 else None)
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
        self.mastery_mod_preset_1607_spin.setValue(0x3FF)
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
        self._populate_basic_mastery_effect_combo()
        self._install_common_numeric_validators()
        return page































    def copy_text(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"Copied: {text}", 3000)





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


    def _is_sigil_db_entry(self, entry: Any) -> bool:
        if not entry:
            return False
        cat = str(getattr(entry, "category", "") or "").strip().lower()
        item_id = str(getattr(entry, "item_id", "") or "").strip().upper()
        return cat in {"sigil", "sigil / gem", "sigils", "gem"} or item_id.startswith("GEEN_")

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









    def _known_material_entries(self):
        blocked = {"Sigil", "Weapon", "Character", "Trait / Skill", "Other"}
        rows = []
        for entry in self.item_db.by_hash.values():
            if entry.category in blocked:
                continue
            name_low = entry.display_name.lower()
            if name_low.startswith("unnamed / reserved") or name_low.startswith("reserved /"):
                continue
            allowed_categories = {"Material", "Currency", "Consumable", "Glitterstone", "Wrightstone", "Ticket", "Crewmate Card"}
            wallet = self._wallet_field_for_item_key(entry.display_name, entry.hash_value)
            if wallet is not None:
                continue
            alias_text = str(getattr(entry, "alias_text", "") or "").lower()
            item_id = str(getattr(entry, "item_id", "") or "").upper()
            sheet_treasure = "treasure" in alias_text or item_id.startswith(("ITEM_17_", "ITEM_18_", "ITEM_22_", "ITEM_23_"))
            if entry.category in allowed_categories or sheet_treasure:
                rows.append(entry)
        return sorted(rows, key=lambda e: (e.category, e.item_id, e.display_name))



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
        self._sigil_trait_grouped_cache = None
        grouped = self.save.group_by_unit([2703, 2704, 2706, 2707])
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
            if not self._is_sigil_db_entry(entry):
                continue
            trait_meta = self._sigil_trait_meta_records_for_unit(int(unit_id))
            targets.append({"unit": unit_id, "name": entry.display_name, "level_rec": level_rec, "owner_rec": fields.get(2706), "flags_rec": fields.get(2707), **trait_meta})
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
        The item ID commonly appears as ITEM_19-series in notes. Keep these
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
                writer.writerow(["Unit", "Slot", "Hash", "Level", "Trait 1", "Trait 2", "Assigned Character", "Flags", "Visible Name"])
                for row in rows:
                    writer.writerow([row[0], row[1], row[4], row[5], row[6], row[7], row[8], row[9], row[2]])
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



    def copy_selected_sigil_hash(self) -> None:
        idx = self.sigil_table.currentIndex()
        if idx.isValid() and idx.row() < len(self.sigil_model.rows):
            self.copy_text(str(self.sigil_model.rows[idx.row()][4]).removeprefix("0x"))

    def copy_selected_sigil_gbid(self) -> None:
        idx = self.sigil_table.currentIndex()
        if idx.isValid() and idx.row() < len(self.sigil_model.rows):
            self.copy_text(str(self.sigil_model.rows[idx.row()][3]))






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


    def _install_common_numeric_validators(self) -> None:
        """Apply signed 32-bit/safe-range validators to direct-edit boxes that accept numbers."""
        specs = [
            ("sigil_level_edit", 0, SIGIL_LEVEL_MAX, "Sigil level: 0 to signed 32-bit max."),
            ("sigil_flags_edit", I32_MIN, I32_MAX, "Sigil flags: signed 32-bit range."),
            ("mastery_state_edit", 0, MASTERY_1607_SAFE_MAX, "Mastery state/value: 0 to signed 32-bit max."),
            ("mastery_overmastery_value_edit", -1, MASTERY_1607_SAFE_MAX, "Overmastery value: -1 for FFFFFFFF/80%, otherwise 0 to signed 32-bit max."),
        ]
        for name, minimum, maximum, tip in specs:
            self._set_i32_line_edit_validator(getattr(self, name, None), minimum=minimum, maximum=maximum, tooltip=tip)

    def _set_i32_line_edit_validator(self, editor, *, minimum=I32_MIN, maximum=I32_MAX, tooltip="") -> None:
        if editor is None:
            return
        minimum = max(I32_MIN, int(minimum))
        maximum = min(I32_MAX, int(maximum))
        editor.setValidator(QIntValidator(minimum, maximum, editor))
        if tooltip:
            editor.setToolTip(tooltip)
        else:
            editor.setToolTip(f"Whole number only. Safe range: {minimum:,} to {maximum:,}.")


    def _selected_item_meta(self) -> Optional[Dict[str, Any]]:
        return self._selected_meta(self.item_table, self.item_rows_meta) if hasattr(self, "item_table") else None

    def _selected_sigil_meta(self) -> Optional[Dict[str, Any]]:
        return self._selected_meta(self.sigil_table, self.sigil_rows_meta) if hasattr(self, "sigil_table") else None

    def _weapon_meta_by_unit(self, unit_id: int) -> Optional[Dict[str, Any]]:
        for meta in getattr(self, "weapon_rows_meta", []) or []:
            try:
                if int(meta.get("unit_id", -1)) == int(unit_id):
                    return meta
            except Exception:
                continue
        return None

    def _weapon_meta_display_label(self, meta: Dict[str, Any]) -> str:
        h = self._record_first_value(meta.get("hash_rec"), 0)
        name, gbid, hash_hex = self.hash_entry_parts(h)
        unit = meta.get("unit_id", "?")
        cap = self._record_first_value(meta.get("cap_rec") or meta.get("unk_2805_rec"), 0)
        bonus = self._record_first_value(meta.get("unk_2806_rec"), 0)
        if name and name != "Unknown":
            return f"{unit} · {name} · cap {cap} · trait +{bonus}"
        return f"{unit} · {hash_hex or 'Unknown weapon'} · cap {cap} · trait +{bonus}"




    def _sync_weapon_cap_trait_weapon_combo(self) -> None:
        combo = getattr(self, "weapon_cap_trait_weapon_combo", None)
        if combo is None:
            return
        current_unit = combo.currentData()
        if current_unit is None:
            selected = self._selected_meta(self.weapon_table, self.weapon_rows_meta) if hasattr(self, "weapon_table") else None
            current_unit = selected.get("unit_id") if selected else None
        combo.blockSignals(True)
        try:
            combo.clear()
            for meta in getattr(self, "weapon_rows_meta", []) or []:
                if meta.get("is_empty"):
                    continue
                combo.addItem(self._weapon_meta_display_label(meta), int(meta.get("unit_id", 0)))
            if current_unit is not None:
                for i in range(combo.count()):
                    try:
                        if int(combo.itemData(i)) == int(current_unit):
                            combo.setCurrentIndex(i)
                            break
                    except Exception:
                        pass
        finally:
            combo.blockSignals(False)
        self.update_weapon_cap_trait_controls()

    def _set_weapon_cap_trait_combo_unit(self, unit_id: Any) -> None:
        combo = getattr(self, "weapon_cap_trait_weapon_combo", None)
        if combo is None or unit_id is None:
            return
        try:
            target = int(unit_id)
        except Exception:
            return
        for i in range(combo.count()):
            try:
                if int(combo.itemData(i)) == target:
                    if combo.currentIndex() != i:
                        combo.blockSignals(True)
                        combo.setCurrentIndex(i)
                        combo.blockSignals(False)
                    return
            except Exception:
                continue







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

    def _current_sigil_row_index_for_apply(self) -> int:
        table = getattr(self, "sigil_table", None)
        if table is None:
            return -1
        idx = table.currentIndex()
        if not idx.isValid():
            return -1
        row = int(idx.row())
        if row < 0 or row >= len(getattr(self, "sigil_rows_meta", []) or []):
            return -1
        return row

    def _sigil_auto_value_ready(self, column: int, value: Any) -> bool:
        text_value = str(value or "").strip()
        col = int(column)
        # Do not write numeric 0 just because the user temporarily cleared a box
        # while typing a new number. Also avoid modal validation warnings during
        # live typing; the explicit Apply/Resync path still reports bad values.
        if col in {5, 9, 10, 11}:
            return bool(text_value) and _parse_intish(text_value) is not None
        if col in {2, 3, 4, 6, 7, 8, 12}:
            if text_value.lower() in {"", "none", "clear", "empty", "0", "—", "-"}:
                return True
            return self._resolve_hash_from_text(text_value) is not None
        return True

    def _apply_selected_sigil_column_now(self, column: int, value: Any, label: str = "field", *, show_status: bool = True) -> bool:
        """Apply one selected-sigil editor field immediately.

        This avoids the old whole-form batch path, which could leave the visible
        table/detail stale until the debounce timer or focus change completed.
        """
        if getattr(self, "_updating_sigil_detail", False):
            return False
        if getattr(self, "_sigil_auto_apply_in_progress", False):
            return False
        if bool(getattr(self, "_save_in_progress", False)) or bool(getattr(self, "_load_in_progress", False)):
            return False
        if not getattr(self, "save", None):
            return False
        row_index = self._current_sigil_row_index_for_apply()
        if row_index < 0:
            return False
        if not self._sigil_auto_value_ready(int(column), value):
            return False
        self._sigil_auto_apply_in_progress = True
        try:
            ok = self.apply_sigil_table_cell_edit(row_index, int(column), value)
            if ok:
                try:
                    self.update_sigil_detail()
                except Exception:
                    pass
                if show_status:
                    self.statusBar().showMessage(f"Auto-applied selected sigil {label}. Save when ready.", 3500)
            return bool(ok)
        finally:
            self._sigil_auto_apply_in_progress = False

    def _apply_selected_sigil_combo_column_now(self, column: int, combo: QComboBox, label: str = "trait") -> bool:
        if combo is None:
            return False
        data = combo.currentData()
        value = "" if data in (None, 0, EMPTY_HASH) else f"0x{int(data) & 0xFFFFFFFF:08X}"
        return self._apply_selected_sigil_column_now(column, value, label)

    def _schedule_sigil_field_auto_apply(self, column: int, value_func, label: str, delay_ms: int = 120) -> None:
        if getattr(self, "_updating_sigil_detail", False):
            return
        if getattr(self, "_sigil_auto_apply_in_progress", False):
            return
        if not getattr(self, "save", None):
            return
        row_index = self._current_sigil_row_index_for_apply()
        if row_index < 0:
            return
        timers = getattr(self, "_sigil_field_auto_timers", None)
        if not isinstance(timers, dict):
            self._sigil_field_auto_timers = {}
            timers = self._sigil_field_auto_timers
        timer = timers.get(int(column))
        if timer is None:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timers[int(column)] = timer
        try:
            timer.timeout.disconnect()
        except Exception:
            pass
        timer.timeout.connect(lambda col=int(column), vf=value_func, lab=label: self._apply_selected_sigil_column_now(col, vf(), lab))
        timer.start(max(25, int(delay_ms)))



    def apply_sigil_inline_edits(self, *_args, auto: bool = False, show_no_change: bool = True) -> int:
        if getattr(self, "_updating_sigil_detail", False) and auto:
            return 0
        if not self.save:
            if show_no_change and not auto:
                QMessageBox.information(self, "No save loaded", "Open a save first.")
            return 0
        row = self._selected_row(self.sigil_table, self.sigil_model) if hasattr(self, "sigil_table") else None
        if not row:
            return 0
        self._sigil_auto_apply_in_progress = True
        try:
            changes = 0
            owner_text = self.sigil_worn_by_edit.text() if hasattr(self, "sigil_worn_by_edit") else ""
            if not str(owner_text or "").strip() and hasattr(self, "sigil_worn_by_combo"):
                owner_hash = self.sigil_worn_by_combo.currentData()
                owner_text = "" if int(owner_hash or EMPTY_HASH) in (0, EMPTY_HASH) else f"0x{int(owner_hash) & 0xFFFFFFFF:08X}"
            trait1_value = ""
            trait2_value = ""
            if hasattr(self, "sigil_trait1_combo"):
                data = self.sigil_trait1_combo.currentData()
                trait1_value = "" if data in (None, 0, EMPTY_HASH) else f"0x{int(data) & 0xFFFFFFFF:08X}"
            if hasattr(self, "sigil_trait2_combo"):
                data = self.sigil_trait2_combo.currentData()
                trait2_value = "" if data in (None, 0, EMPTY_HASH) else f"0x{int(data) & 0xFFFFFFFF:08X}"
            requests = [
                (2, self.sigil_identity_edit.text() if hasattr(self, "sigil_identity_edit") else "", str(row[3] or row[4] or row[2] or "")),
                (5, self.sigil_level_edit.text() if hasattr(self, "sigil_level_edit") else "", str(row[5] or "")),
                (6, trait1_value, str(row[6] or "")),
                (10, str(self.sigil_trait1_level_spin.value()) if hasattr(self, "sigil_trait1_level_spin") else "", str(row[10] or "")),
                (7, trait2_value, str(row[7] or "")),
                (11, str(self.sigil_trait2_level_spin.value()) if hasattr(self, "sigil_trait2_level_spin") else "", str(row[11] or "")),
                (8, owner_text, str(row[12] or row[8] or "")),
                (9, self.sigil_flags_edit.text() if hasattr(self, "sigil_flags_edit") else "", str(row[9] or "")),
            ]
            current_row = self.sigil_table.currentIndex().row()
            for column, text, current in requests:
                text = str(text or "").strip()
                if text == str(current or "").strip():
                    continue
                if self.apply_sigil_table_cell_edit(current_row, column, text):
                    changes += 1
                else:
                    return changes
            if changes:
                prefix = "Auto-applied" if auto else "Applied"
                self.statusBar().showMessage(f"{prefix} {changes} sigil field change{'s' if changes != 1 else ''} in memory. Save when ready.", 5000)
                try:
                    self.update_sigil_detail()
                except Exception:
                    pass
            elif show_no_change and not auto:
                self.statusBar().showMessage("No sigil field changes to apply.", 3000)
            return changes
        finally:
            self._sigil_auto_apply_in_progress = False






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




    def update_sigil_detail(self) -> None:
        if not hasattr(self, "sigil_detail_label"):
            return
        self._updating_sigil_detail = True
        row = self._selected_row(self.sigil_table, self.sigil_model) if hasattr(self, "sigil_table") else None
        if not row:
            text = "Select a sigil row. Use inline fields for sigil, level, character assignment character, and lock/flags."
            if hasattr(self.sigil_detail_label, "setPlainText"):
                self.sigil_detail_label.setPlainText(text)
            else:
                self.sigil_detail_label.setText(text)
            for name in ("sigil_identity_edit", "sigil_level_edit", "sigil_worn_by_edit", "sigil_flags_edit"):
                self._clear_line_edit_safely(name)
            self._set_owner_combo_by_hash(EMPTY_HASH)
            if hasattr(self, "sigil_trait1_combo"):
                self._set_hash_combo_current_value(self.sigil_trait1_combo, EMPTY_HASH)
            if hasattr(self, "sigil_trait2_combo"):
                self._set_hash_combo_current_value(self.sigil_trait2_combo, EMPTY_HASH)
            for name in ("sigil_trait1_level_spin", "sigil_trait2_level_spin"):
                spin = getattr(self, name, None)
                if spin is not None:
                    spin.blockSignals(True); spin.setValue(0); spin.blockSignals(False)
            self._updating_sigil_detail = False
            return
        self._raw_hash_editor_text("sigil_identity_edit", row[3], row[4])
        self._set_line_edit_text_safely("sigil_level_edit", row[5])
        self._set_line_edit_text_safely("sigil_worn_by_edit", row[12] if str(row[8] or "").startswith("Unknown") else "")
        self._set_owner_combo_by_hash(self._current_sigil_owner_hash())
        self._set_line_edit_text_safely("sigil_flags_edit", row[9])
        meta = self._selected_sigil_meta() if hasattr(self, "sigil_table") else None
        pair_note = str((meta or {}).get("level_pair_note") or "")
        t1_hash = self._record_first_value((meta or {}).get("trait1_hash_rec"), EMPTY_HASH)
        t2_hash = self._record_first_value((meta or {}).get("trait2_hash_rec"), EMPTY_HASH)
        t1_level = self._record_first_value((meta or {}).get("trait1_level_rec"), 0)
        t2_level = self._record_first_value((meta or {}).get("trait2_level_rec"), 0)
        if hasattr(self, "sigil_trait1_combo"):
            self._set_hash_combo_current_value(self.sigil_trait1_combo, t1_hash)
        if hasattr(self, "sigil_trait2_combo"):
            self._set_hash_combo_current_value(self.sigil_trait2_combo, t2_hash)
        for spin_name, value in (("sigil_trait1_level_spin", t1_level), ("sigil_trait2_level_spin", t2_level)):
            spin = getattr(self, spin_name, None)
            if spin is not None:
                try:
                    ivalue = int(value or 0)
                except Exception:
                    ivalue = 0
                spin.blockSignals(True)
                spin.setValue(max(0, min(I32_MAX, ivalue)))
                spin.blockSignals(False)
        text = (
            f"Sigil/Gem : {format_display_value(row[2], 'Sigil')}\n"
            f"Level     : {format_display_value(row[5], 'Level')}  (2704 / FF900A){pair_note}\n"
            f"Trait 1   : {format_display_value(row[6] or 'None', 'Trait 1')}  (120M lane 0: 1701/1702)\n"
            f"Trait 2   : {format_display_value(row[7] or 'None', 'Trait 2')}  (120M lane 1: 1701/1702)\n"
            f"Assigned Character: {format_display_value(row[8] or 'None / empty', 'Character Assignment')}\n"
            f"Flags     : {format_display_value(row[9], 'Flags')}\n"
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
        self._updating_weapon_detail = True
        try:
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
                    f"Uncap:  {format_display_value(row[5], 'Uncap')}  (2805 / FFF50A00)",
                    f"Trait+: {format_display_value(row[6], 'Trait +')}  (2806; appears as the + weapon-trait level bonus)",
                    f"Stone:  {format_display_value(row[10] or 'none', 'Stone')}",
                    f"Flags:  {format_display_value(row[9], 'Flags')}",
                    "",
                    "Tip: edit a field and it auto-applies after a short pause. Press Enter/leave the field to force a resync.",
                ])
            if hasattr(self.weapon_detail_label, "setPlainText"):
                self.weapon_detail_label.setPlainText(text)
            else:
                self.weapon_detail_label.setText(text)
        finally:
            self._updating_weapon_detail = False
        meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta) if hasattr(self, "weapon_table") else None
        if meta:
            self._set_weapon_cap_trait_combo_unit(meta.get("unit_id"))
        self.update_weapon_cap_trait_controls()

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
            f"- Invalid character assignment references: {len(sigil_owner_issues)}",
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
        return labels.get(str(prefix or "")[:1], f"{prefix}00000-series progression")






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
                group_label = self._progression_group_label_for_prefix(quest_prefix)
            limit_label = "all rows" if max_rows is None else f"first {max_rows:,} matches"
            self.progression_filter_status.setText(
                f"Showing {len(rows):,} rows · section: {section} · {group_label} · {field_label} · {value_mode} · {limit_label}"
            )
        if hasattr(self, "progression_rows_table"):
            self._set_table_widths(self.progression_rows_table, {0: 220, 1: 82, 2: 190, 3: 100, 4: 210, 5: 90, 6: 70, 7: 82, 8: 420})




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
        """Cheap General summary used immediately after opening a save."""
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
                value = int(clean, 16) & 0xFFFFFFFF
                if self.item_db.lookup_hash(value):
                    return value
                if len(clean) == 8:
                    rev = int.from_bytes(value.to_bytes(4, "big"), "little") & 0xFFFFFFFF
                    if self.item_db.lookup_hash(rev):
                        return rev
                return value
            if q.lower().startswith("0x"):
                value = int(q, 16) & 0xFFFFFFFF
                if self.item_db.lookup_hash(value):
                    return value
                rev = int.from_bytes(value.to_bytes(4, "big"), "little") & 0xFFFFFFFF
                if self.item_db.lookup_hash(rev):
                    return rev
                return value
            if q.isdecimal():
                return int(q, 10) & 0xFFFFFFFF
        except Exception:
            pass
        matches = self.item_db.search(q, limit=20)
        # Prefer exact name match, then prefix match (handles "Name [GBID]" format)
        q_lower = q.lower()
        for entry in matches:
            name = entry.name.lower()
            # Exact match
            if q_lower == name:
                return entry.hash_value & 0xFFFFFFFF
        for entry in matches:
            name = entry.name.lower()
            # Prefix match: "Damage Cap V+" matches "damage cap v+ [geen_xxx]"
            if name.startswith(q_lower) or name.startswith(q_lower.replace("+", "")):
                if " iv+" not in name and " iv " not in name.split("[")[0].strip():
                    # Prefer V+ over IV+
                    return entry.hash_value & 0xFFFFFFFF
        for entry in matches:
            name = entry.name.lower()
            if name.startswith(q_lower):
                return entry.hash_value & 0xFFFFFFFF
        if matches:
            return matches[0].hash_value & 0xFFFFFFFF
        # Last resort: many GBFR IDs are custom-XXHash32 strings.
        # Allow direct generated hashes for ID-looking tokens even before the
        # database has a named row for them.
        if q.upper() == q and any(ch == "_" for ch in q) and all(ch.isalnum() or ch == "_" for ch in q):
            return gbfr_hash(q) & 0xFFFFFFFF
        return None

    def _resolve_trait_hash(self, text: str) -> Optional[int]:
        """Resolve trait name preferring SKILL/trait DB entries over sigil (GEEN) entries."""
        q = (text or "").strip()
        if not q:
            return None
        q_lower = q.lower()
        trait_entries = []
        for entry in self.item_db.by_hash.values():
            gbid = str(getattr(entry, "item_id", "") or "").upper()
            cat = str(getattr(entry, "category", "") or "").lower()
            if gbid.startswith("SKILL") or "trait" in cat or "skill" in cat:
                trait_entries.append(entry)
        for e in trait_entries:
            if e.name.lower() == q_lower:
                return e.hash_value & 0xFFFFFFFF
        for e in trait_entries:
            if e.name.lower().startswith(q_lower):
                return e.hash_value & 0xFFFFFFFF
        for e in trait_entries:
            if q_lower in e.name.lower():
                return e.hash_value & 0xFFFFFFFF
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






    def _is_wrightstone_hash(self, item_hash: int) -> bool:
        """True only for real Wrightstone database entries."""
        try:
            h = int(item_hash or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            return False
        if h in (0, EMPTY_HASH):
            return False
        try:
            entry = self.item_db.lookup_hash(h)
        except Exception:
            entry = None
        if not entry:
            return False
        hay = " ".join(str(getattr(entry, name, "") or "") for name in ("item_id", "name", "display_name", "category", "aliases")).lower()
        gbid = str(getattr(entry, "item_id", "") or "").upper()
        return (
            "wrightstone" in hay
            or "whetstone" in hay
            or "wst" in hay
            or gbid.startswith(("ITEM_25_", "ITEM_26_", "ITEM_27_", "ITEM_28_", "ITEM_29_"))
        )


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

    def _known_character_hashes_for_sigil_owner(self) -> set[int]:
        hashes: set[int] = set()
        if not self.save:
            return hashes
        try:
            for rec in self.save.find(id_type=1301):
                value = self._record_first_value(rec, 0)
                if value not in (0, EMPTY_HASH):
                    hashes.add(int(value) & 0xFFFFFFFF)
        except Exception:
            pass
        return hashes

    def _is_valid_sigil_inventory_hash(self, sigil_hash: int) -> bool:
        value = int(sigil_hash) & 0xFFFFFFFF
        if value in (0, EMPTY_HASH):
            return False
        entry = self.item_db.lookup_hash(value) if hasattr(self, "item_db") else None
        if not entry:
            # Unknown GEEN hashes can exist in newer saves, so do not treat
            # unknown active rows as invalid during repair. Add paths still use
            # the database/browser and therefore normally resolve to GEEN rows.
            return True
        item_id = str(getattr(entry, "item_id", "") or "").upper()
        return self._is_sigil_db_entry(entry)

    def _safe_sigil_level(self, value: Any, *, minimum: int = 1) -> int:
        return self._clamp_sigil_level_value(value, minimum=minimum)

    def _safe_sigil_owner_hash(self, owner_hash: Optional[int]) -> int:
        if owner_hash in (None, "", 0, EMPTY_HASH):
            return EMPTY_HASH
        try:
            owner = int(owner_hash) & 0xFFFFFFFF
        except Exception:
            return EMPTY_HASH
        if owner in self._known_character_hashes_for_sigil_owner():
            return owner
        return EMPTY_HASH

    def _safe_sigil_flags(self, current: Any = 0, *, locked: bool = True, assigned: bool = False) -> int:
        try:
            cur = int(current or 0)
        except Exception:
            cur = 0
        # Uploaded Save Wizard sample pattern:
        # - assigned to a character through 2706: low bits are 2
        # - unassigned locked inventory rows: low bits are 3
        # - unassigned normal inventory rows: low bits are 2
        high = cur & ~3
        low = 2 if assigned else (3 if locked else 2)
        return high | low

    def _sigil_meta_assigned_owner(self, meta: Dict[str, Any]) -> bool:
        try:
            owner = int(self._record_first_value(meta.get("worn_rec"), EMPTY_HASH) or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            owner = EMPTY_HASH
        return owner not in (0, EMPTY_HASH)

    def _sanitize_new_sigil_values(self, values: Dict[str, Any], *, locked: bool = True) -> Dict[str, Any]:
        out = dict(values or {})
        if "level_rec" in out:
            out["level_rec"] = self._safe_sigil_level(out.get("level_rec"), minimum=1)
        if "worn_rec" in out:
            # Duplicated or pasted sigils should not inherit an character assignment.
            # Duplicating an equipped row can exceed per-character slot limits
            # and has been crash-prone in-game.
            out["worn_rec"] = EMPTY_HASH
        if "flags_rec" in out:
            out["flags_rec"] = self._safe_sigil_flags(out.get("flags_rec"), locked=locked, assigned=False)
        return out

    def _activate_sigil_slot(
        self,
        slot_meta: Dict[str, Any],
        sigil_hash: int,
        level: int = SIGIL_LEVEL_MAX,
        locked: bool = True,
        owner_hash: Optional[int] = None,
    ) -> bool:
        sigil_hash = int(sigil_hash) & 0xFFFFFFFF
        if not self._is_valid_sigil_inventory_hash(sigil_hash):
            return False
        level = self._safe_sigil_level(level, minimum=1)
        serial = self._set_sigil_serial(slot_meta)
        ok_hash = self._set_record_first_value(slot_meta.get("hash_rec"), sigil_hash, "sigil hash 2703")
        ok_level = self._set_record_first_value(slot_meta.get("level_rec"), level, "sigil level 2704 / FF900A")
        owner = EMPTY_HASH
        if slot_meta.get("worn_rec") is not None:
            owner = self._safe_sigil_owner_hash(owner_hash)
            self._set_record_first_value(slot_meta.get("worn_rec"), owner, "sigil assigned character 2706")
        if slot_meta.get("flags_rec") is not None:
            cur = self._record_first_value(slot_meta.get("flags_rec"), 0)
            assigned = owner not in (0, EMPTY_HASH)
            new_flags = self._safe_sigil_flags(cur, locked=locked, assigned=assigned)
            self._set_record_first_value(slot_meta.get("flags_rec"), new_flags, "sigil flags 2707")
        return bool(ok_hash or ok_level or serial)

    def _assign_existing_sigil_to_character(self, slot_meta: Dict[str, Any], owner_hash: int, locked: bool = True) -> bool:
        """Assign an already-active sigil to a character with proper flags + serial + owner."""
        if not self.save or not slot_meta:
            return False
        owner = self._safe_sigil_owner_hash(owner_hash)
        if owner in (0, EMPTY_HASH):
            return False
        changed = False
        # Set owner 2706
        if slot_meta.get("worn_rec") is not None:
            if self._set_record_first_value(slot_meta["worn_rec"], owner, "sigil owner 2706"):
                changed = True
        # Normalize flags 2707: assigned + locked = 3
        if slot_meta.get("flags_rec") is not None:
            cur = self._record_first_value(slot_meta["flags_rec"], 0)
            new_flags = self._safe_sigil_flags(cur, locked=locked, assigned=True)
            if self._set_record_first_value(slot_meta["flags_rec"], new_flags, "sigil flags 2707"):
                changed = True
        # Ensure serial 2702 exists
        serial = self._set_sigil_serial(slot_meta)
        if serial:
            changed = True
        return changed

    def repair_added_sigil_slots(self, silent: bool = False) -> int:
        """Repair/sanitize sigil inventory rows that can crash the game.

        This conservative pass:
        - assigns unique 2702 serials to active rows missing one
        - fixes duplicate 2702 serials on active rows
        - repairs invalid negative 2704 levels while preserving high signed-32-bit levels
        - normalizes low active/lock bits in 2707
        - clears invalid character assignment hashes in 2706
        - clears half-active empty rows where hash is empty but level/flags/owner remain
        """
        if not self.save:
            if not silent:
                QMessageBox.information(self, "No save loaded", "Open a save first.")
            return 0

        grouped = self.save.group_by_unit([2701, 2702, 2703, 2704, 2706, 2707])
        counter = self._sigil_counter_record()
        used_serials: set[int] = set()
        max_serial = max(0, self._record_first_value(counter, 0) if counter is not None else 0)
        changed = 0
        fixed_serials = fixed_levels = fixed_flags = fixed_owner = cleared_empty = 0

        def next_serial() -> int:
            nonlocal max_serial
            max_serial = int(max_serial) + 1
            while max_serial in used_serials or max_serial <= 0:
                max_serial += 1
            used_serials.add(max_serial)
            return max_serial

        for unit_id, fields in sorted(grouped.items()):
            if int(unit_id) == 0:
                continue
            hash_rec = fields.get(2703)
            slot_rec = fields.get(2702)
            level_rec = self._sigil_level_record_for_new_slot(fields) or fields.get(2704)
            owner_rec = fields.get(2706)
            flags_rec = fields.get(2707)

            sigil_hash = self._record_first_value(hash_rec, 0) & 0xFFFFFFFF if hash_rec else 0
            serial = self._record_first_value(slot_rec, 0) & 0xFFFFFFFF if slot_rec else 0
            level = self._record_first_value(level_rec, 0) if level_rec else 0
            owner = self._record_first_value(owner_rec, EMPTY_HASH) & 0xFFFFFFFF if owner_rec else EMPTY_HASH
            flags = self._record_first_value(flags_rec, 0) if flags_rec else 0

            active = sigil_hash not in (0, EMPTY_HASH)
            if not active:
                # Empty rows should not keep partial active-looking state.
                if level_rec is not None and int(level or 0) != 0:
                    if self._set_record_first_value(level_rec, 0, "clear empty sigil level 2704"):
                        changed += 1; cleared_empty += 1
                if owner_rec is not None and owner not in (0, EMPTY_HASH):
                    if self._set_record_first_value(owner_rec, EMPTY_HASH, "clear empty sigil owner 2706"):
                        changed += 1; cleared_empty += 1
                if flags_rec is not None and int(flags or 0) != 0:
                    if self._set_record_first_value(flags_rec, 0, "clear empty sigil flags 2707"):
                        changed += 1; cleared_empty += 1
                # Leave serials alone on empty rows; some saves use reusable bank
                # slots with historical serials and the hash is what controls visibility.
                continue

            # Active rows need a unique, non-empty serial.
            if slot_rec is not None:
                if serial in (0, EMPTY_HASH) or serial in used_serials:
                    new_serial = next_serial()
                    if self._set_record_first_value(slot_rec, new_serial, "repair sigil serial/key 2702"):
                        changed += 1; fixed_serials += 1
                else:
                    used_serials.add(serial)
                    max_serial = max(max_serial, int(serial))

            # Keep high sigil levels. Only repair obviously invalid negative values.
            if level_rec is not None:
                try:
                    level_i = int(level or 0)
                except Exception:
                    level_i = 1
                if level_i < 1:
                    if self._set_record_first_value(level_rec, 1, "repair sigil level 2704 / FF900A"):
                        changed += 1; fixed_levels += 1

            # Normalize active/lock bits to the uploaded Save Wizard pattern.
            # Assigned rows use low bits 2. Unassigned locked inventory rows keep 3.
            if flags_rec is not None:
                assigned = owner not in (0, EMPTY_HASH)
                locked = bool(int(flags or 0) & 1)
                safe_flags = self._safe_sigil_flags(flags, locked=locked, assigned=assigned)
                if int(flags or 0) != int(safe_flags):
                    if self._set_record_first_value(flags_rec, safe_flags, "normalize sigil flags 2707"):
                        changed += 1; fixed_flags += 1

            # Clear owners that do not match an actual character hash.
            if owner_rec is not None:
                safe_owner = self._safe_sigil_owner_hash(owner)
                if int(owner or 0) != int(safe_owner):
                    if self._set_record_first_value(owner_rec, safe_owner, "clear invalid sigil owner 2706"):
                        changed += 1; fixed_owner += 1

        if counter is not None and max_serial > self._record_first_value(counter, 0):
            if self._set_record_first_value(counter, max_serial, "sigil serial counter 2701"):
                changed += 1; fixed_serials += 1

        if changed:
            if silent:
                self.dirty = True
                self._invalidate_add_browser_indexes()
                self._mark_stale_pages(["Sigils", "Save Health", "Welcome"])
            else:
                self._after_editor_patch(
                    f"Sanitized sigils: {changed} field(s) changed "
                    f"({fixed_serials} serial, {fixed_levels} level, {fixed_flags} flag, {fixed_owner} owner, {cleared_empty} empty-state).",
                    refresh=True,
                )
        if not silent:
            if changed:
                QMessageBox.information(
                    self,
                    "Sigil repair complete",
                    f"Sanitized {changed} sigil field(s). Save As and test this copy in-game."
                )
            else:
                QMessageBox.information(self, "No repair needed", "No unsafe sigil rows were found.")
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
                meta = {
                    "unit_id": unit_id,
                    "slot_rec": fields.get(2702),
                    "hash_rec": hash_rec,
                    "level_rec": level_rec,
                    "worn_rec": fields.get(2706),
                    "flags_rec": fields.get(2707),
                }
                meta.update(self._sigil_trait_meta_records_for_unit(int(unit_id)))
                return meta
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
            "Damage Cap V, 15 locked\nSupplementary DMG V, 15 locked",
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

    BEST_SIGIL_TEMPLATES: Dict[str, List[tuple]] = {
        "Gran / Djeeta (Captain)": [
            ("Captain's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Cooldown V+", 15), ("Combo Booster V+", 15),
        ],
        "Katalina": [
            ("Guardian's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Stout Heart V+", 15), ("Combo Booster V+", 15),
        ],
        "Rackam": [
            ("Helmsman's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Concentrated Fire V+", 15), ("Concentrated Fire V+", 15),
        ],
        "Io": [
            ("Mage's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Charge V+", 15), ("Quick Charge V+", 15),
        ],
        "Eugen": [
            ("Veteran's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Concentrated Fire V+", 15), ("Concentrated Fire V+", 15),
        ],
        "Rosetta": [
            ("Rose's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Cooldown V+", 15), ("Cascade V+", 15),
        ],
        "Lancelot": [
            ("White Wing's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Flight over Fight V+", 15), ("Combo Booster V+", 15),
        ],
        "Vane": [
            ("Hero's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Drain V+", 15), ("Steel Nerve V+", 15),
        ],
        "Percival": [
            ("Lord's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Charge V+", 15), ("Quick Charge V+", 15),
        ],
        "Siegfried": [
            ("Dragonslayer's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Stout Heart V+", 15), ("Combo Booster V+", 15),
        ],
        "Charlotta": [
            ("Holy Knight's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Combo Booster V+", 15), ("Quick Cooldown V+", 15),
        ],
        "Yodarha": [
            ("Swordmaster's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Flight over Fight V+", 15), ("Quick Cooldown V+", 15),
        ],
        "Narmaya": [
            ("Butterfly's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Charge V+", 15), ("Quick Charge V+", 15),
        ],
        "Zeta": [
            ("Crimson's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Combo Booster V+", 15), ("Quick Cooldown V+", 15),
        ],
        "Vaseraga": [
            ("Undying's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Charge V+", 15), ("Quick Charge V+", 15),
        ],
        "Ferry": [
            ("Phantasm's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Cooldown V+", 15), ("Cascade V+", 15),
        ],
        "Ghandagoza": [
            ("Eternal's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Charge V+", 15), ("Quick Charge V+", 15),
        ],
        "Cagliostro": [
            ("Founder's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Cooldown V+", 15), ("Cascade V+", 15),
        ],
        "Seofon (Siete)": [
            ("Sword Sovereign's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Cooldown V+", 15), ("Combo Booster V+", 15),
        ],
        "Tweyen (Song)": [
            ("Radiant Archer's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Concentrated Fire V+", 15), ("Concentrated Fire V+", 15),
        ],
        "Sandalphon": [
            ("Supreme Archangel's Awakening+", 15), ("Damage Cap V+", 15), ("Damage Cap V+", 15),
            ("Damage Cap V+", 15), ("Damage Cap V+", 15), ("War Elemental+", 15),
            ("Supplementary Damage V+", 15), ("Supplementary Damage V+", 15),
            ("Supplementary Damage V+", 15), ("Berserker Echo+", 15),
            ("Quick Cooldown V+", 15), ("Cascade V+", 15),
        ],
    }

    CHARACTER_NAME_ALIASES: Dict[str, str] = {
        "Gran": "Gran", "Djeeta": "Djeeta", "Captain": "Gran",
        "Katalina": "Katalina", "Rackam": "Rackam", "Io": "Io",
        "Eugen": "Eugen", "Rosetta": "Rosetta", "Lancelot": "Lancelot",
        "Vane": "Vane", "Percival": "Percival", "Siegfried": "Siegfried",
        "Charlotta": "Charlotta", "Yodarha": "Yodarha", "Narmaya": "Narmaya",
        "Zeta": "Zeta", "Vaseraga": "Vaseraga", "Ferry": "Ferry",
        "Ghandagoza": "Ghandagoza", "Cagliostro": "Cagliostro",
        "Seofon": "Seofon", "Siete": "Seofon", "Tweyen": "Tweyen", "Song": "Tweyen",
        "Sandalphon": "Sandalphon",
    }

    # Maps sigil name -> (trait1_name, trait2_name) for V+ sigils
    SIGIL_TRAIT_MAP: Dict[str, tuple] = {
        "Captain's Awakening+": ("Captain's Awakening", "Guts"),
        "Guardian's Awakening+": ("Guardian's Awakening", "Guts"),
        "Helmsman's Awakening+": ("Helmsman's Awakening", "Guts"),
        "Mage's Awakening+": ("Mage's Awakening", "Guts"),
        "Veteran's Awakening+": ("Veteran's Awakening", "Guts"),
        "Rose's Awakening+": ("Rose's Awakening", "Guts"),
        "White Wing's Awakening+": ("White Wing's Awakening", "Guts"),
        "Hero's Awakening+": ("Hero's Awakening", "Guts"),
        "Lord's Awakening+": ("Lord's Awakening", "Guts"),
        "Dragonslayer's Awakening+": ("Dragonslayer's Awakening", "Guts"),
        "Holy Knight's Awakening+": ("Holy Knight's Awakening", "Guts"),
        "Swordmaster's Awakening+": ("Swordmaster's Awakening", "Guts"),
        "Butterfly's Awakening+": ("Butterfly's Awakening", "Guts"),
        "Crimson's Awakening+": ("Crimson's Awakening", "Guts"),
        "Undying's Awakening+": ("Undying's Awakening", "Guts"),
        "Phantasm's Awakening+": ("Phantasm's Awakening", "Guts"),
        "Eternal's Awakening+": ("Eternal's Awakening", "Guts"),
        "Founder's Awakening+": ("Founder's Awakening", "Guts"),
        "Sword Sovereign's Awakening+": ("Sword Sovereign's Awakening", "Guts"),
        "Radiant Archer's Awakening+": ("Radiant Archer's Awakening", "Guts"),
        "Supreme Archangel's Awakening+": ("Supreme Archangel's Awakening", "Guts"),
        "Damage Cap V+": ("Damage Cap", "Supplementary Damage"),
        "War Elemental+": ("War Elemental", "Critical Hit Rate"),
        "Supplementary Damage V+": ("Supplementary Damage", "Potion Hoarder"),
        "Berserker Echo+": ("Berserker Echo", "Attack Power"),
        "Quick Cooldown V+": ("Quick Cooldown", "Cascade"),
        "Combo Booster V+": ("Combo Booster", "Improved Dodge"),
        "Stout Heart V+": ("Stout Heart", "Steel Nerve"),
        "Concentrated Fire V+": ("Concentrated Fire", "Critical Hit Rate"),
        "Quick Charge V+": ("Quick Charge", "Concentrated Fire"),
        "Cascade V+": ("Cascade", "Improved Dodge"),
        "Flight over Fight V+": ("Flight over Fight", "Nimble Onslaught"),
        "Drain V+": ("Drain", "Stout Heart"),
        "Steel Nerve V+": ("Steel Nerve", "Quick Cooldown"),
    }

    # Sigils always present in save (story/DLC) - auto-assign to character if unowned
    ALWAYS_OWNED_SIGILS: Dict[str, str] = {
        "Fearless Heart": "Gran", "Fearless Soul+": "Gran", "Ain+": "Gran", "Versalis Heart": "Gran",
        "Guardian's Warpath": "Katalina", "Guardian's Awakening+": "Katalina",
        "Helmsman's Warpath": "Rackam", "Helmsman's Awakening+": "Rackam",
        "Mage's Warpath": "Io", "Mage's Awakening+": "Io",
        "Veteran's Warpath": "Eugen", "Veteran's Awakening+": "Eugen",
        "Rose's Warpath": "Rosetta", "Rose's Awakening+": "Rosetta",
        "Holy Knight's Warpath": "Charlotta", "Holy Knight's Awakening+": "Charlotta",
        "Eternal Rage's Warpath": "Ghandagoza", "Eternal Rage's Awakening+": "Ghandagoza",
        "Phantasm's Warpath": "Ferry", "Phantasm's Awakening+": "Ferry",
        "Butterfly's Warpath": "Narmaya", "Butterfly's Awakening+": "Narmaya",
        "White Dragon's Warpath": "Lancelot", "White Dragon's Awakening+": "Lancelot",
        "Hero's Warpath": "Vane", "Hero's Awakening+": "Vane",
        "Lord's Warpath": "Percival", "Lord's Awakening+": "Percival",
        "Dragonslayer's Warpath": "Siegfried", "Dragonslayer's Awakening+": "Siegfried",
        "Founder's Warpath": "Cagliostro", "Founder's Awakening+": "Cagliostro",
        "Swordmaster's Warpath": "Yodarha", "Swordmaster's Awakening+": "Yodarha",
        "Crimson's Warpath": "Zeta", "Crimson's Awakening+": "Zeta",
        "Ebony's Warpath": "Vaseraga", "Ebony's Awakening+": "Vaseraga",
        "Spirit Edge's Rally": "Seofon", "Spirit Edge's Fury": "Seofon",
        "Spirit Edge's Warpath": "Seofon", "Seven-Star Boundary+": "Seofon",
        "Spirit Edge's Awakening+": "Seofon",
        "Dark Huntress's Volley": "Tweyen", "Dark Huntress's Surge": "Tweyen",
        "Dark Huntress's Warpath": "Tweyen", "Two-Crown Boundary+": "Tweyen",
        "Dark Huntress's Awakening+": "Tweyen",
        "Supreme Primarch's Awe": "Sandalphon", "Supreme Primarch's Nimbus": "Sandalphon",
        "Supreme Primarch's Warpath": "Sandalphon", "Supreme Primarch's Awakening+": "Sandalphon",
    }

    def _find_character_hash(self, name: str) -> Optional[int]:
        alias = self.CHARACTER_NAME_ALIASES.get(name, name)
        for choice in getattr(self, "character_owner_choices", []):
            choice_name = str(choice.get("name", ""))
            if alias.lower() in choice_name.lower() or choice_name.lower() in alias.lower():
                return int(choice.get("hash", EMPTY_HASH)) & 0xFFFFFFFF
        return None

    def import_sigil_templates(self) -> None:
        if not self.save:
            QMessageBox.information(self, "No save loaded", "Open a save first.")
            return
        import json
        json_path = RESOURCE_DIR / "sigil_templates.json"
        if not json_path.exists():
            QMessageBox.warning(self, "Missing", f"Template not found: {json_path}"); return
        try:
            templates_data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as e:
            QMessageBox.warning(self, "JSON Error", str(e)); return
        char_list = [c["nhan_vat"] for c in templates_data]
        dialog = QDialog(self)
        dialog.setWindowTitle("Import Best Sigil Templates")
        dialog.setMinimumWidth(400)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("Select characters to apply best builds:"))
        checks = {}
        for name in char_list:
            cb = QCheckBox(name); cb.setChecked(False); layout.addWidget(cb); checks[name] = cb
        select_all_btn = QPushButton("Select All")
        select_all_btn.clicked.connect(lambda: [cb.setChecked(True) for cb in checks.values()])
        layout.addWidget(select_all_btn)
        btn_layout = QHBoxLayout()
        ok_btn = QPushButton("Apply Templates"); cancel_btn = QPushButton("Cancel")
        btn_layout.addWidget(ok_btn); btn_layout.addWidget(cancel_btn); layout.addLayout(btn_layout)
        def apply():
            sel = [n for n, cb in checks.items() if cb.isChecked()]
            if not sel: QMessageBox.information(dialog, "None selected", "Select at least one character."); return
            dialog.accept()
        ok_btn.clicked.connect(apply); cancel_btn.clicked.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted: return
        selected = [n for n, cb in checks.items() if cb.isChecked()]
        if not selected: return

        # Build existing sigil map and find unassigned always-owned sigils
        existing: Dict[int, set] = {}
        unassigned_always: Dict[str, List[Dict]] = {}  # char_alias -> [slot_meta]
        if self.save:
            grouped = self.save.group_by_unit([2702, 2703, 2704, 2706, 2707])
            for unit_id, fields in grouped.items():
                owner_rec = fields.get(2706)
                hash_rec = fields.get(2703)
                if owner_rec and hash_rec:
                    try:
                        owner = int(self._record_first_value(owner_rec, EMPTY_HASH)) & 0xFFFFFFFF
                        sighash = int(self._record_first_value(hash_rec, 0)) & 0xFFFFFFFF
                    except Exception:
                        continue
                    if owner not in (0, EMPTY_HASH) and sighash not in (0, EMPTY_HASH):
                        existing.setdefault(owner, set()).add(sighash)
                    # Find unassigned always-owned sigils
                    if owner in (0, EMPTY_HASH) and sighash not in (0, EMPTY_HASH):
                        entry = self.item_db.lookup_hash(sighash)
                        if entry:
                            sigil_name = str(getattr(entry, "display_name", "") or "")
                            for always_name, char_alias in self.ALWAYS_OWNED_SIGILS.items():
                                if always_name.lower() in sigil_name.lower():
                                    meta = {
                                        "unit_id": int(unit_id),
                                        "hash_rec": hash_rec,
                                        "worn_rec": owner_rec,
                                        "flags_rec": fields.get(2707),
                                        "slot_rec": fields.get(2702),
                                        "level_rec": fields.get(2704),
                                    }
                                    unassigned_always.setdefault(char_alias, []).append(meta)
                                    break

        results = []
        for char_data in templates_data:
            char_name = char_data["nhan_vat"]
            if char_name not in selected: continue
            sigils = char_data.get("sigils", [])
            owner_hash = self._find_character_hash(char_name)
            if owner_hash is None:
                results.append(f"SKIP {char_name}: character not found in DB"); continue
            owned = existing.get(owner_hash, set())

            # Auto-assign unassigned always-owned sigils (proper flags + serial + owner)
            char_alias = self.CHARACTER_NAME_ALIASES.get(char_name, char_name)
            auto_assigned = 0
            candidates = unassigned_always.get(char_alias, [])
            if candidates:
                do_assign = QMessageBox.question(
                    self, "Auto-Assign Sigils?",
                    f"Found {len(candidates)} unassigned always-owned sigil(s) for {char_name}.\n\nAuto-assign them to this character?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                ) == QMessageBox.StandardButton.Yes
                if do_assign:
                    for meta in candidates:
                        sighash = int(self._record_first_value(meta.get("hash_rec"), 0)) & 0xFFFFFFFF
                        if sighash not in owned:
                            if self._assign_existing_sigil_to_character(meta, owner_hash, locked=True):
                                owned.add(sighash)
                                auto_assigned += 1
            if auto_assigned:
                results.append(f"  Auto-assigned {auto_assigned} always-owned sigils to {char_name}")

            added = skipped = 0
            for s in sigils:
                sigil_name = s["ten_sigil"]
                level = s.get("level_trait_1", 15)
                trait1 = s.get("trait_1", ""); trait2 = s.get("trait_2", "")
                t1_lv = s.get("level_trait_1", 15); t2_lv = s.get("level_trait_2", 15)
                # Skip always-owned sigils (already in save)
                is_always_owned = False
                for always_name in self.ALWAYS_OWNED_SIGILS:
                    if always_name.lower() in sigil_name.lower():
                        is_always_owned = True; break
                if is_always_owned:
                    skipped += 1; continue

                h = self._resolve_hash_from_text(sigil_name)
                if h is None:
                    # Try without +, try common variations
                    for variant in [sigil_name.replace("+", ""), sigil_name.replace("+", " V"), sigil_name]:
                        h = self._resolve_hash_from_text(variant)
                        if h: break
                if h is None: results.append(f"  MISS: {sigil_name}"); continue
                # NOTE: Allow duplicates - same sigil can be in multiple slots
                slot = self._find_empty_sigil_slot()
                if not slot: results.append(f"  FAIL: {sigil_name} - no slot"); break
                if not self._activate_sigil_slot(slot, h, level, locked=True, owner_hash=None):
                    results.append(f"  FAIL: {sigil_name} - activate failed"); break
                # Force-clear owner so sigil is NOT auto-equipped in-game
                if slot.get("worn_rec") is not None:
                    self._set_record_first_value(slot["worn_rec"], EMPTY_HASH, "clear owner 2706")
                if slot.get("flags_rec") is not None:
                    cur = self._record_first_value(slot["flags_rec"], 0)
                    self._set_record_first_value(slot["flags_rec"], (cur & ~3) | 3, "flags -> unassigned locked")
                owned.add(h & 0xFFFFFFFF)
                # Set traits from template JSON data
                sigil_unit_id = int(slot.get("unit_id", 0))
                if sigil_unit_id > 0 and (trait1 or trait2):
                    tf = self._sigil_trait_meta_records_for_unit(sigil_unit_id)
                    for lane, tn, tl in [(0, trait1, t1_lv), (1, trait2, t2_lv)]:
                        if not tn: continue
                        # Resolve trait using SKILL entries only (not GEEN sigils)
                        th = self._resolve_trait_hash(tn)
                        if th and th not in (0, EMPTY_HASH):
                            kr = "trait1_hash_rec" if lane == 0 else "trait2_hash_rec"
                            lk = "trait1_level_rec" if lane == 0 else "trait2_level_rec"
                            r = tf.get(kr)
                            if r is not None and r.value_count >= 1:
                                self._set_record_first_value(r, th, f"trait{lane+1}")
                                lr = tf.get(lk)
                                if lr is not None and lr.value_count >= 1:
                                    self._set_record_first_value(lr, int(tl), f"trait{lane+1} lv")
                owned.add(h & 0xFFFFFFFF)
                added += 1
            msg = f"OK {char_name}: {added} added"
            if skipped: msg += f", {skipped} skipped"
            results.append(msg)
        self._after_editor_patch("Imported best sigil templates."); self.refresh_sigil_rows()
        QMessageBox.information(self, "Import Complete", "\n".join(results) + "\n\nSave As to write changes.")

    def _apply_sigil_traits_from_db(self, entry, slot_meta) -> None:
        """Apply trait 1 & 2 using hardcoded trait map."""
        sigil_unit_id = int(slot_meta.get("unit_id", 0))
        if sigil_unit_id <= 0:
            return
        name = str(getattr(entry, "display_name", "") or "")
        # Look up traits from map
        traits = None
        for key in self.SIGIL_TRAIT_MAP:
            if key in name or name in key:
                traits = self.SIGIL_TRAIT_MAP[key]
                break
        if not traits:
            return
        trait1_name, trait2_name = traits
        trait_fields = self._sigil_trait_meta_records_for_unit(sigil_unit_id)
        for lane, tname in [(0, trait1_name), (1, trait2_name)]:
            if not tname:
                continue
            thash = self._resolve_hash_from_text(tname)
            if thash and thash not in (0, EMPTY_HASH):
                key_rec = "trait1_hash_rec" if lane == 0 else "trait2_hash_rec"
                lv_rec_key = "trait1_level_rec" if lane == 0 else "trait2_level_rec"
                rec = trait_fields.get(key_rec)
                if rec is not None and rec.value_count >= 1:
                    self._set_record_first_value(rec, thash, f"trait{lane+1} {tname}")
                    lv_rec = trait_fields.get(lv_rec_key)
                    if lv_rec is not None and lv_rec.value_count >= 1:
                        self._set_record_first_value(lv_rec, 15, f"trait{lane+1} level")


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
    SIGIL_SLOT_FIELDS = [("hash_rec", "sigil hash"), ("level_rec", "level"), ("trait1_hash_rec", "trait 1"), ("trait1_level_rec", "trait 1 level"), ("trait2_hash_rec", "trait 2"), ("trait2_level_rec", "trait 2 level"), ("worn_rec", "assigned character"), ("flags_rec", "flags")]
    WEAPON_SLOT_FIELDS = [("hash_rec", "weapon hash"), ("xp_rec", "XP"), ("flags_rec", "flags"), ("stone_rec", "stone")]





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
        values = self._sanitize_new_sigil_values(self.sigil_slot_clipboard["values"], locked=True) if was_empty else dict(self.sigil_slot_clipboard["values"])
        patched = self._patch_meta_values(meta, values, self.SIGIL_SLOT_FIELDS)
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
        values = self._sanitize_new_sigil_values(self._meta_values(meta, [k for k, _ in self.SIGIL_SLOT_FIELDS]), locked=True)
        self._patch_meta_values(slot, values, self.SIGIL_SLOT_FIELDS)
        serial = self._set_sigil_serial(slot)
        self._after_editor_patch(f"Duplicated sigil into empty unit {slot.get('unit_id')} / serial {serial}.")








    def _update_visible_sigil_level_flag_row(self, row_index: int, level_value: Any = None, flags_value: Any = None) -> None:
        try:
            if not hasattr(self, "sigil_model"):
                return
            if row_index < 0 or row_index >= len(self.sigil_model.rows):
                return
            if level_value is not None:
                self.sigil_model.rows[row_index][5] = level_value
            if flags_value is not None:
                self.sigil_model.rows[row_index][9] = flags_value
            self._emit_model_row_changed(self.sigil_model, row_index)
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
            flags_value = self._safe_sigil_flags(cur, locked=True, assigned=self._sigil_meta_assigned_owner(meta))
            if self._set_record_first_value(flags, flags_value, "sigil flags 2707"):
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
                flags_value = self._safe_sigil_flags(cur, locked=True, assigned=self._sigil_meta_assigned_owner(meta))
                if self._set_record_first_value(flags, flags_value, "sigil flags 2707"):
                    flags_patched += 1
            if level_changed:
                patched += 1
            self._update_visible_sigil_level_flag_row(row_index, SIGIL_LEVEL_MAX, flags_value)
        self.update_sigil_detail()
        self._after_editor_patch(
            f"Maxed visible sigils instantly: {patched:,} level row(s), {flags_patched:,} flag row(s).",
            refresh=False,
        )






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

        The game tolerates large 2704 / FF900A values without meaningful extra
        effect, so the editor keeps the high-value cheat behavior while preventing
        overflow/wrap.
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
        ok = False
        resolved = None
        parsed = None

        if column in (2, 3, 4):
            resolved = self._resolve_edit_hash(value, "sigil", allow_empty=True)
            if resolved is None:
                return False
            ok = self._set_record_first_value(meta.get("hash_rec"), resolved, "sigil hash 2703 / FF8F0A")
            if ok and resolved in (0, EMPTY_HASH):
                self._set_record_first_value(meta.get("trait1_hash_rec"), EMPTY_HASH, "sigil trait 1 ID 1701")
                self._set_record_first_value(meta.get("trait1_level_rec"), 0, "sigil trait 1 level 1702")
                self._set_record_first_value(meta.get("trait2_hash_rec"), EMPTY_HASH, "sigil trait 2 ID 1701")
                self._set_record_first_value(meta.get("trait2_level_rec"), 0, "sigil trait 2 level 1702")
        elif column == 5:
            parsed = self._parse_edit_int(value, "Sigil level 2704 / FF900A")
            if parsed is None:
                return False
            parsed = self._clamp_sigil_level_value(parsed, minimum=0)
            if hasattr(self, "sigil_level_edit"):
                self._set_line_edit_text_safely("sigil_level_edit", str(parsed))
            ok = self._set_record_first_value(meta.get("level_rec"), parsed, "sigil level 2704 / FF900A")
        elif column == 6:
            resolved = self._resolve_edit_hash(value, "sigil trait 1", allow_empty=True)
            if resolved is None:
                return False
            ok = self._set_record_first_value(meta.get("trait1_hash_rec"), resolved, "sigil trait 1 ID 1701 / FFA50600")
        elif column == 7:
            resolved = self._resolve_edit_hash(value, "sigil trait 2", allow_empty=True)
            if resolved is None:
                return False
            ok = self._set_record_first_value(meta.get("trait2_hash_rec"), resolved, "sigil trait 2 ID 1701 / FFA50600")
        elif column == 8 or column == 12:
            resolved = self._resolve_edit_hash(value, "assigned character", allow_empty=True)
            if resolved is None:
                return False
            ok = self._apply_sigil_owner_to_meta(meta, resolved, row_index=row_index, show_message=True)
        elif column == 9:
            parsed = self._parse_edit_int(value, "Sigil flags")
            if parsed is None:
                return False
            ok = self._set_record_first_value(meta.get("flags_rec"), parsed, "sigil flags 2707")
        elif column == 10:
            parsed = self._parse_edit_int(value, "sigil trait 1 level 1702 / FFA60600")
            if parsed is None:
                return False
            ok = self._set_record_first_value(meta.get("trait1_level_rec"), max(0, min(I32_MAX, int(parsed))), "sigil trait 1 level 1702 / FFA60600")
        elif column == 11:
            parsed = self._parse_edit_int(value, "sigil trait 2 level 1702 / FFA60600")
            if parsed is None:
                return False
            ok = self._set_record_first_value(meta.get("trait2_level_rec"), max(0, min(I32_MAX, int(parsed))), "sigil trait 2 level 1702 / FFA60600")
        else:
            return False

        if ok:
            try:
                t1_hash = self._record_first_value(meta.get("trait1_hash_rec"), EMPTY_HASH)
                t1_lv = self._record_first_value(meta.get("trait1_level_rec"), 0)
                t2_hash = self._record_first_value(meta.get("trait2_hash_rec"), EMPTY_HASH)
                t2_lv = self._record_first_value(meta.get("trait2_level_rec"), 0)
                if column in (2, 3, 4):
                    self._patch_visible_hash_row(self.sigil_model, row_index, 2, 3, 4, resolved)
                    meta["is_empty"] = resolved in (0, EMPTY_HASH)
                    meta["is_known"] = bool(self.item_db.lookup_hash(resolved))
                    if resolved in (0, EMPTY_HASH):
                        self.sigil_model.rows[row_index][6] = "—"
                        self.sigil_model.rows[row_index][7] = "—"
                        self.sigil_model.rows[row_index][10] = ""
                        self.sigil_model.rows[row_index][11] = ""
                elif column == 5:
                    self.sigil_model.rows[row_index][5] = parsed
                elif column in (6, 10):
                    self.sigil_model.rows[row_index][6] = self._sigil_trait_display(t1_hash, self._record_first_value(meta.get("trait1_level_rec"), 0))
                    self.sigil_model.rows[row_index][10] = self._record_first_value(meta.get("trait1_level_rec"), "")
                elif column in (7, 11):
                    self.sigil_model.rows[row_index][7] = self._sigil_trait_display(t2_hash, self._record_first_value(meta.get("trait2_level_rec"), 0))
                    self.sigil_model.rows[row_index][11] = self._record_first_value(meta.get("trait2_level_rec"), "")
                elif column in (8, 12):
                    self.sigil_model.rows[row_index][8] = "" if resolved in (0, EMPTY_HASH) else self._character_owner_name_for_hash(resolved)
                    self.sigil_model.rows[row_index][12] = self._character_owner_gbid_for_hash(resolved)
                elif column == 9:
                    self.sigil_model.rows[row_index][9] = parsed
                self._emit_model_row_changed(self.sigil_model, row_index)
            except Exception:
                pass
            self._mark_stale_pages(["Sigils", "Save Health"])
            self._after_editor_patch("Sigil/gem cell updated in memory.", refresh=False)
        return bool(ok)

    def _selected_weapon_meta(self) -> Optional[Dict[str, Any]]:
        combo = getattr(self, "weapon_cap_trait_weapon_combo", None)
        if combo is not None:
            try:
                unit_id = combo.currentData()
                if unit_id is not None:
                    meta = self._weapon_meta_by_unit(int(unit_id))
                    if meta:
                        return meta
            except Exception:
                pass
        table_meta = self._selected_meta(self.weapon_table, self.weapon_rows_meta) if hasattr(self, "weapon_table") else None
        if table_meta:
            return table_meta
        return None


    def _set_weapon_cap_combo_to_value(self, value: Any) -> None:
        combo = getattr(self, "weapon_cap_combo", None)
        if combo is None:
            return
        try:
            target = int(value)
        except Exception:
            target = 5
        for i in range(combo.count()):
            try:
                if int(combo.itemData(i)) == target:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(i)
                    combo.blockSignals(False)
                    return
            except Exception:
                pass

    def _weapon_trait_choices(self) -> List[Dict[str, Any]]:
        cached = getattr(self, "weapon_trait_choices_cache", None)
        if cached is not None:
            return cached
        choices: List[Dict[str, Any]] = [{"label": "None / Clear", "hash": EMPTY_HASH, "gbid": "", "name": "None / Clear"}]
        try:
            entries = []
            for entry in self.item_db.by_hash.values():
                gbid = str(getattr(entry, "item_id", "") or "").upper()
                cat = str(getattr(entry, "category", "") or "")
                if gbid.startswith("SKILL") or "trait" in cat.lower() or "skill" in cat.lower():
                    entries.append(entry)
            def _sort_key(entry):
                gbid = str(getattr(entry, "item_id", "") or "").upper()
                m = re.search(r"(\d+)", gbid)
                return (int(m.group(1)) if m else 999999, gbid)
            for entry in sorted(entries, key=_sort_key):
                choices.append({
                    "label": f"{entry.display_name} ({entry.item_id})",
                    "hash": int(entry.hash_value) & 0xFFFFFFFF,
                    "gbid": str(entry.item_id),
                    "name": str(entry.display_name),
                })
        except Exception:
            pass
        self.weapon_trait_choices_cache = choices
        return choices

    def refresh_weapon_trait_choices(self) -> None:
        combo = getattr(self, "weapon_trait_combo", None)
        if combo is None:
            return
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        for choice in self._weapon_trait_choices():
            combo.addItem(str(choice.get("label")), int(choice.get("hash", EMPTY_HASH)) & 0xFFFFFFFF)
        if current is not None:
            for i in range(combo.count()):
                try:
                    if int(combo.itemData(i)) == int(current):
                        combo.setCurrentIndex(i)
                        break
                except Exception:
                    pass
        combo.blockSignals(False)

    def _set_weapon_trait_combo_to_hash(self, value: Any) -> None:
        combo = getattr(self, "weapon_trait_combo", None)
        if combo is None:
            return
        try:
            target = int(value or EMPTY_HASH) & 0xFFFFFFFF
        except Exception:
            target = EMPTY_HASH
        for i in range(combo.count()):
            data = combo.itemData(i)
            try:
                if data is not None and int(data) == target:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(i)
                    combo.blockSignals(False)
                    return
            except Exception:
                pass
        if combo.count():
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)

    def _set_weapon_trait_bonus_spin_to_value(self, value: Any) -> None:
        spin = getattr(self, "weapon_trait_bonus_spin", None)
        if spin is None:
            return
        try:
            spin.blockSignals(True)
            spin.setValue(max(0, min(999, int(value or 0))))
        except Exception:
            pass
        finally:
            try:
                spin.blockSignals(False)
            except Exception:
                pass

    def _weapon_meta_saved_values(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "xp": self._record_first_value(meta.get("xp_rec"), 0),
            "cap": self._record_first_value(meta.get("cap_rec") or meta.get("unk_2805_rec"), 0),
            "trait_bonus": self._record_first_value(meta.get("unk_2806_rec"), 0),
            "field_2807": self._record_first_value(meta.get("unk_2807_rec"), 0),
            "field_2814": self._record_first_value(meta.get("unk_2814_rec"), 0),
            "owned_flag": self._record_first_value(meta.get("flags_rec"), 0),
            "stone": self._record_first_value(meta.get("stone_rec"), EMPTY_HASH),
        }

    def _update_weapon_builtin_trait_summary(self, meta: Optional[Dict[str, Any]]) -> None:
        label = getattr(self, "weapon_builtin_trait_summary", None)
        if label is None:
            return
        if not meta:
            label.setText("Select a weapon to inspect its saved uncap/trait-bonus fields.")
            return
        values = self._weapon_meta_saved_values(meta)
        h = self._record_first_value(meta.get("hash_rec"), 0)
        name, gbid, hash_hex = self.hash_entry_parts(h)
        stone_value = values.get("stone")
        stone_name, stone_gbid, stone_hash = self.hash_entry_parts(stone_value)
        try:
            stone_text = "none" if int(stone_value) in (0, EMPTY_HASH) else (stone_name if stone_gbid else stone_hash)
        except Exception:
            stone_text = str(stone_value)
        label.setText(
            f"{format_display_value(name, 'Weapon')} ({gbid or hash_hex})  •  "
            f"XP/progress 2804: {values['xp']}  •  "
            f"Uncap/max-level 2805: {values['cap']}  •  "
            f"Trait + bonus 2806: {values['trait_bonus']}  •  "
            f"2807: {values['field_2807']}  •  "
            f"2814: {values['field_2814']}  •  "
            f"Owned/flag 2815: {values['owned_flag']}  •  "
            f"Stone/Wrightstone 2816: {stone_text}"
        )




    def update_weapon_cap_trait_controls(self) -> None:
        if not hasattr(self, "weapon_cap_trait_status"):
            return
        if hasattr(self, "weapon_trait_combo") and self.weapon_trait_combo.count() == 0:
            self.refresh_weapon_trait_choices()
        meta = self._selected_weapon_meta()
        if not meta:
            self.weapon_cap_trait_status.setText("Select a weapon above or in Current Weapons first.")
            self._update_weapon_builtin_trait_summary(None)
            return
        cap = self._record_first_value(meta.get("cap_rec") or meta.get("unk_2805_rec"), 0)
        bonus = self._record_first_value(meta.get("unk_2806_rec"), 0)
        self._set_weapon_cap_combo_to_value(cap)
        if hasattr(self, "weapon_cap_custom_spin"):
            try:
                self.weapon_cap_custom_spin.blockSignals(True)
                self.weapon_cap_custom_spin.setValue(max(0, min(255, int(cap))))
                self.weapon_cap_custom_spin.blockSignals(False)
            except Exception:
                pass
        self._set_weapon_trait_bonus_spin_to_value(bonus)
        self._update_weapon_builtin_trait_summary(meta)
        trait_hash = self._record_first_value(meta.get("trait_id_rec"), EMPTY_HASH)
        trait_level = self._record_first_value(meta.get("trait_level_rec"), 0)
        self._set_weapon_trait_combo_to_hash(trait_hash)
        if hasattr(self, "weapon_trait_level_spin"):
            try:
                self.weapon_trait_level_spin.blockSignals(True)
                self.weapon_trait_level_spin.setValue(max(0, min(I32_MAX, int(trait_level or 0))))
                self.weapon_trait_level_spin.blockSignals(False)
            except Exception:
                pass
        trait_state = "direct 1701/1702 records found" if meta.get("trait_id_rec") is not None or meta.get("trait_level_rec") is not None else "in-game ATK/HP traits are derived; no direct 1701/1702 rows on this weapon"
        self.weapon_cap_trait_status.setText(f"Selected unit {meta.get('unit_id')}: uncap 2805={cap}, trait + 2806={bonus}. {trait_state}.")
















    def clear_selected_sigil_worn_by(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            return
        row_index = self.sigil_table.currentIndex().row() if hasattr(self, "sigil_table") else -1
        if self._apply_sigil_owner_to_meta(meta, EMPTY_HASH, row_index=row_index, show_message=True):
            self.refresh_sigil_rows()
            self._after_editor_patch("Selected sigil character assignment cleared in memory.", refresh=False)

    def _clear_sigil_meta_to_empty(self, meta: Dict[str, Any]) -> int:
        """Turn a sigil row back into a reusable empty 270x slot.

        A reusable empty sigil slot is hash-empty and level-zero.  We also clear
        the owner/flags and both linked 120M trait lanes so the next Add Sigil
        operation starts from a clean game-compatible row.
        """
        if not self.save or not meta:
            return 0
        patched = 0
        clear_values = [
            ("slot_rec", 0, "sigil serial/key 2702"),
            ("hash_rec", EMPTY_HASH, "sigil hash 2703 / FF8F0A"),
            ("level_rec", 0, "sigil level 2704 / FF900A"),
            ("trait1_hash_rec", EMPTY_HASH, "sigil trait 1 ID 1701 / FFA50600"),
            ("trait1_level_rec", 0, "sigil trait 1 level 1702 / FFA60600"),
            ("trait2_hash_rec", EMPTY_HASH, "sigil trait 2 ID 1701 / FFA50600"),
            ("trait2_level_rec", 0, "sigil trait 2 level 1702 / FFA60600"),
            ("worn_rec", EMPTY_HASH, "sigil assigned character 2706 / FF920A"),
            ("flags_rec", 0, "sigil flags 2707"),
        ]
        for key, value, label in clear_values:
            rec = meta.get(key)
            if rec is None:
                continue
            try:
                current = self._record_first_value(rec, None)
            except Exception:
                current = None
            if current == value:
                continue
            if self._set_record_first_value(rec, value, label):
                patched += 1
        return patched

    def remove_selected_sigil_to_empty_slot(self) -> None:
        if not self.save:
            return
        meta = self._selected_meta(self.sigil_table, self.sigil_rows_meta)
        if not meta:
            QMessageBox.information(self, "No selection", "Select a sigil row in the table first.")
            return
        try:
            sigil_hash = int(self._record_first_value(meta.get("hash_rec"), 0) or 0) & 0xFFFFFFFF
        except Exception:
            sigil_hash = 0
        if sigil_hash in (0, EMPTY_HASH):
            self.statusBar().showMessage("Selected row is already empty.", 4000)
            return
        name, gbid, hx = self.hash_entry_parts(sigil_hash)
        display = name or gbid or hx or "selected sigil"
        unit_id = meta.get("unit_id", "?")
        if QMessageBox.question(
            self, "Remove Sigil",
            f"Remove \"{display}\" at unit {unit_id}?\n\nThis clears this ONE slot back to empty.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        patched = self._clear_sigil_meta_to_empty(meta)
        if patched <= 0:
            self.statusBar().showMessage("No fields changed; row may already be empty.", 5000)
            return
        self.refresh_sigil_rows()
        self.statusBar().showMessage(f"Removed \"{display}\" from unit {unit_id} ({patched} fields cleared).", 5000)

























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
        value = self._prompt_hash("Set Sigil Assigned Character Hash 2706", self._record_first_value(rec, 0))
        row_index = self.sigil_table.currentIndex().row() if hasattr(self, "sigil_table") else -1
        if value is not None and self._apply_sigil_owner_to_meta(meta, value, row_index=row_index, show_message=True):
            self.refresh_sigil_rows()
            self._after_editor_patch("Sigil character assignment updated using 2706 character hash / 2707=2.", refresh=False)












    def open_save(self) -> None:
        if bool(getattr(self, "_save_in_progress", False)):
            QMessageBox.warning(self, "Save still running", "Wait for the current save to finish before opening another save.")
            return
        if bool(getattr(self, "_load_in_progress", False)):
            return
        self._stop_pending_ui_timers_before_save()
        default_dir = os.path.expandvars(r"%LOCALAPPDATA%\GBFR\Saved\SaveGames")
        if not os.path.isdir(default_dir):
            default_dir = ""
        path, _ = QFileDialog.getOpenFileName(self, "Open GBFR save", default_dir, "Save files (*.dat *.sav);;All files (*)")
        if not path:
            return
        self._open_save_path(path)

    def _open_save_path(self, path: str) -> None:
        if bool(getattr(self, "_save_in_progress", False)):
            return
        if bool(getattr(self, "_load_in_progress", False)):
            return
        if not path:
            return
        self._load_in_progress = True
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
            new_save = GBFRSaveData.open(path)
            self.save = new_save
            self.dirty = False
            self._switch_to_safe_page_without_refresh()
            self._finish_loaded_save_ui()
            self.statusBar().showMessage("Save loaded. Open an editor tab to refresh it.", 5000)
        except Exception as exc:
            QMessageBox.critical(self, "Open failed", str(exc))
        finally:
            try:
                QApplication.restoreOverrideCursor()
            except Exception:
                pass
            self._load_in_progress = False

    def _clear_save_bound_ui_models(self) -> None:
        """Clear visible models that contain UnitRecord references."""
        for attr in ("sigil_model", "wrightstone_model", "mastery_slot_model",
                     "mastery_mod_model", "mastery_mod_code_model",
                     "mastery_mod_preset_model", "mastery_mod_value_model"):
            model = getattr(self, attr, None)
            if model is not None and hasattr(model, "set_rows"):
                try:
                    model.set_rows([])
                except Exception:
                    pass
        for attr in ("sigil_rows_meta", "wrightstone_rows_meta",
                     "mastery_slot_rows_meta", "mastery_mod_rows_meta",
                     "mastery_mod_code_rows_meta", "mastery_mod_preset_rows_meta",
                     "mastery_mod_value_rows_meta", "sigil_database_rows_meta"):
            try:
                setattr(self, attr, [])
            except Exception:
                pass
        if hasattr(self, "unit_model"):
            try:
                self.unit_model.set_save(None)
            except Exception:
                pass

    def _finish_loaded_save_ui(self) -> None:
        """Post-load UI update."""
        if not self.save:
            return
        # Clear filters
        for attr in ("sigil_filter_edit",):
            widget = getattr(self, attr, None)
            if widget is not None and hasattr(widget, "clear"):
                try:
                    widget.clear()
                except Exception:
                    pass
        self._mark_all_pages_stale()
        # Set unit model with new save
        try:
            self.unit_model.set_save(self.save)
            self.unit_model.set_item_db(self.item_db)
            self.unit_model.set_resource_db(self.resource_db)
        except Exception:
            pass



    def _switch_to_safe_page_without_refresh(self) -> None:
        """Move to Sigils during save swaps without triggering heavy refreshes."""
        try:
            idx = getattr(self, "page_indexes", {}).get("Sigils")
            if idx is None or not hasattr(self, "stack"):
                return
            old = bool(getattr(self, "_refreshing_page", False))
            self._refreshing_page = True
            try:
                self.stack.blockSignals(True)
                self.stack.setCurrentIndex(idx)
                self.stack.blockSignals(False)
                self._update_nav_selection("Sigils")
            finally:
                try:
                    self.stack.blockSignals(False)
                except Exception:
                    pass
                self._refreshing_page = old
        except Exception:
            pass





    def _ui_thread_is_current(self) -> bool:
        try:
            app = QApplication.instance()
            return bool(app is None or self.thread() == app.thread())
        except Exception:
            return True



    def _stop_pending_ui_timers_before_save(self) -> None:
        if not self._ui_thread_is_current():
            return
        # Stop known auto-apply timers
        for attr in ("_sigil_auto_apply_timer", "_wrightstone_auto_apply_timer",
                     "_item_qty_auto_apply_timer", "_weapon_inline_auto_apply_timer",
                     "_general_party_auto_apply_timer"):
            timer = getattr(self, attr, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:
                    pass
        # Stop filter timers
        for timer in getattr(self, "_filter_timers", {}).values():
            try:
                timer.stop()
            except Exception:
                pass
        # Normalize after stopping
        if not isinstance(getattr(self, "_filter_timers", None), dict):
            self._filter_timers = {}









    def save_original(self) -> None:
        if not self.save:
            return
        path = str(self.save.container.path) if hasattr(self.save, 'container') and self.save.container else ""
        if not path:
            QMessageBox.warning(self, "Cannot Save", "No original path. Use Save As instead.")
            return
        self._run_save_operation(path, "Saved and created backup.", backup_original=True)


    def save_as(self) -> None:
        if not self.save:
            return
        default_dir = os.path.expandvars(r"%LOCALAPPDATA%\GBFR\Saved\SaveGames")
        if not os.path.isdir(default_dir):
            default_dir = ""
        path, _ = QFileDialog.getSaveFileName(self, "Save As", default_dir, "Save files (*.dat);;All files (*)")
        if not path:
            return
        self._run_save_operation(path, f"Saved to: {path}")

    def _get_save_as_path(self) -> Optional[str]:
        default_dir = os.path.expandvars(r"%LOCALAPPDATA%\GBFR\Saved\SaveGames")
        if not os.path.isdir(default_dir):
            default_dir = ""
        path, _ = QFileDialog.getSaveFileName(self, "Save As", default_dir, "Save files (*.dat);;All files (*)")
        return path if path else None

    def _run_save_operation(self, target_path: str, success_msg: str, backup_original: bool = False) -> None:
        if not self.save:
            return
        self._save_in_progress = True
        try:
            self.statusBar().showMessage("Saving...", 0)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._stop_pending_ui_timers_before_save()
                target = Path(target_path)
                if backup_original and target.exists():
                    stamp = time.strftime("%Y%m%d_%H%M%S")
                    backup_path = target.with_name(f"{target.name}.bak_{stamp}")
                    shutil.copy2(target, backup_path)
                self.save.save_as(target_path, update_hash=True)
                self.dirty = False
                self.statusBar().showMessage(success_msg, 5000)
            except Exception as exc:
                QMessageBox.critical(self, "Save failed", str(exc))
        finally:
            QApplication.restoreOverrideCursor()
            self._save_in_progress = False



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
                (0xC4925BD7, "Attack Power Up", "Overmastery"),
                (0x68B39018, "Chain Burst Damage Up", "Overmastery"),
                (0x45C65767, "Critical Rate", "Overmastery"),
                (0x54929589, "Healing Cap Up", "Overmastery"),
                (0x52A207B5, "Health Up", "Overmastery"),
                (0x43B7581D, "Normal Damage Cap Up", "Overmastery"),
                (0x4A4C093D, "SBA Damage Cap Up", "Overmastery"),
                (0x4E42646B, "SBA Damage Up", "Overmastery"),
                (0x9C555433, "Skill Damage Cap Up", "Overmastery"),
                (0x9A97C049, "Skill Damage Up", "Overmastery"),
                (0x6CB38EF3, "Stun Power Up", "Overmastery"),
                (0x7B727910, "Sigil Slot Add / Restore (danger: >13 slots breaks game)", "Layout Safety"),
                (0xD75B92C4, "Attack Power Up (OP right-table variant)", "Overmastery Variant"),
                (0x1890B368, "Chain Burst Damage Up (OP right-table variant)", "Overmastery Variant"),
                (0x6757C645, "Critical Rate (OP right-table variant)", "Overmastery Variant"),
                (0x89959254, "Healing Cap Up (OP right-table variant)", "Overmastery Variant"),
                (0xB507A252, "Health Up (OP right-table variant)", "Overmastery Variant"),
                (0x1D58B743, "Normal Damage Cap Up (OP right-table variant)", "Overmastery Variant"),
                (0x3D094C4A, "SBA Damage Cap Up (OP right-table variant)", "Overmastery Variant"),
                (0x6B64424E, "SBA Damage Up (OP right-table variant)", "Overmastery Variant"),
                (0x3354559C, "Skill Damage Cap Up (OP right-table variant)", "Overmastery Variant"),
                (0x49C0979A, "Skill Damage Up (OP right-table variant)", "Overmastery Variant"),
                (0xF38EB36C, "Stun Power Up (OP right-table variant)", "Overmastery Variant"),
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
            0x68B39018: "Chain Burst Dmg",
            0x45C65767: "Critical Rate",
            0x54929589: "Healing Cap",
            0x52A207B5: "Health",
            0x43B7581D: "Normal Cap",
            0x4A4C093D: "SBA Cap",
            0x4E42646B: "SBA Damage",
            0x9C555433: "Skill Cap",
            0x9A97C049: "Skill Damage",
            0x6CB38EF3: "Stun Power",
            0x7B727910: "Sigil Slot Add (danger)",
            0xD75B92C4: "Attack Power Alt",
            0x1890B368: "Chain Burst Alt",
            0x6757C645: "Crit Rate Alt",
            0x89959254: "Healing Cap Alt",
            0xB507A252: "Health Alt",
            0x1D58B743: "Normal Cap Alt",
            0x3D094C4A: "SBA Cap Alt",
            0x6B64424E: "SBA Dmg Alt",
            0x3354559C: "Skill Cap Alt",
            0x49C0979A: "Skill Dmg Alt",
            0xF38EB36C: "Stun Power Alt",
        }
        preferred = [
            0xC4925BD7,  # Attack Power
            0x43B7581D,  # Normal Damage Cap
            0x9C555433,  # Skill Damage Cap
            0x45C65767,  # Critical Rate
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

    def _populate_basic_mastery_effect_combo(self) -> None:
        combo = getattr(self, "mastery_basic_effect_combo", None)
        if combo is None:
            return
        current = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("Pick Basic Mastery ID", None)
        for choice in self._load_mastery_mod_choices():
            try:
                value = int(choice.get("value", 0)) & 0xFFFFFFFF
            except Exception:
                continue
            combo.addItem(self._mastery_mod_choice_label(choice, show_hash=True), value)
        if current is not None:
            for i in range(combo.count()):
                data = combo.itemData(i)
                if data is not None and int(data) == int(current):
                    combo.setCurrentIndex(i)
                    break
        combo.blockSignals(False)

    def _set_overmastery_combo_to_value(self, combo: QComboBox, value: Optional[int]) -> bool:
        if combo is None:
            return False
        combo.blockSignals(True)
        try:
            target = None if value is None else (int(value) & 0xFFFFFFFF)
            if target is None or target in (0, EMPTY_HASH):
                combo.setCurrentIndex(0 if combo.count() else -1)
                return False
            for i in range(combo.count()):
                data = combo.itemData(i)
                if data is not None and (int(data) & 0xFFFFFFFF) == target:
                    combo.setCurrentIndex(i)
                    return True
            # If the save contains a valid but currently unknown 1606 value, add it
            # so switching characters never leaves the previous character's visible
            # stat in place.
            label = self._mastery_effect_name(target)
            if label.lower().startswith("unknown"):
                label = f"Unknown 0x{target:08X}"
            combo.addItem(label, target)
            combo.setCurrentIndex(combo.count() - 1)
            return True
        finally:
            combo.blockSignals(False)

    def refresh_overmastery_matrix_preview(self) -> None:
        """Show the selected character's confirmed 4-lane 1606/1607 pairing.

        This is display-only. Edits still flow through the four dropdowns/value
        box and the paired write helpers, but this preview makes the Save Wizard
        mapping visible to the user:

            lane unit = 10000000 + character_index * 1000 + lane
            1606 / FF460600 = selected stat/effect ID
            1607 / FF470600 = paired amount/value
        """
        model = getattr(self, "mastery_overmastery_matrix_model", None)
        if model is None:
            return
        rows: List[List[Any]] = []
        metas: List[Dict[str, Any]] = []
        if self.save:
            group_index = self._mastery_current_overmastery_group_index()
        else:
            group_index = None
        if group_index is not None:
            for lane in range(OVERMASTERY_LANE_COUNT):
                unit_id = self._mastery_overmastery_unit_id(group_index, lane)
                effect_rec = self._mastery_overmastery_record(group_index, lane, MASTERY_EFFECT_FIELD_ID)
                value_rec = self._mastery_overmastery_record(group_index, lane, MASTERY_VALUE_FIELD_ID)

                effect_raw = self._record_first_value(effect_rec, EMPTY_HASH) if effect_rec is not None else EMPTY_HASH
                effect_u32 = int(effect_raw or EMPTY_HASH) & 0xFFFFFFFF
                if effect_rec is None:
                    effect_label = "Missing 1606 row"
                elif effect_u32 in (0, EMPTY_HASH):
                    effect_label = "Empty / keep current"
                else:
                    effect_label = f"{self._mastery_effect_name(effect_u32)} · 0x{effect_u32:08X}"

                amount_raw = self._record_first_value(value_rec, None) if value_rec is not None else None
                amount_label = "Missing 1607 row" if value_rec is None else self._mastery_amount_label(amount_raw)

                def row_ref(rec: Optional[UnitRecord], field_id: int) -> str:
                    if rec is None:
                        return f"{field_id}: missing"
                    try:
                        off = int(getattr(rec, "value_data_offset", 0))
                        return f"{field_id} · {rec.kind}[{rec.index}] · 0x{off:X}"
                    except Exception:
                        return f"{field_id} · row"

                rows.append([
                    f"Lane {lane + 1}",
                    unit_id,
                    effect_label,
                    row_ref(effect_rec, MASTERY_EFFECT_FIELD_ID),
                    amount_label,
                    row_ref(value_rec, MASTERY_VALUE_FIELD_ID),
                    f"+0x{lane * OVERMASTERY_SAVEWIZARD_ROW_STRIDE:02X}",
                ])
                metas.append({
                    "group_index": int(group_index),
                    "lane": int(lane),
                    "unit_id": int(unit_id),
                    "effect_rec": effect_rec,
                    "value_rec": value_rec,
                    "effect": effect_u32,
                    "amount": amount_raw,
                })
        self.mastery_overmastery_matrix_rows_meta = metas
        model.set_rows(rows)
        self._configure_overmastery_matrix_table()


    def _clear_overmastery_controls_for_character(self, message: str = "") -> None:
        self._mastery_mod_loading = True
        try:
            for combo in list(getattr(self, "mastery_overmastery_combos", []) or []):
                try:
                    combo.blockSignals(True)
                    combo.setCurrentIndex(0 if combo.count() else -1)
                    combo.blockSignals(False)
                except Exception:
                    pass
            edit = getattr(self, "mastery_overmastery_value_edit", None)
            if edit is not None:
                edit.blockSignals(True)
                edit.setText("1023")
                edit.blockSignals(False)
        finally:
            self._mastery_mod_loading = False
        self.refresh_overmastery_matrix_preview()
        if hasattr(self, "mastery_sw_lab_status"):
            self.mastery_sw_lab_status.setText(message or "No Overmastery four-stat rows found for this character/group.")

    def load_overmastery_controls_for_current_character(self) -> None:
        """Load the four Overmastery picker slots from the selected character.

        Earlier builds refreshed only the raw Rows/Edit table when the character
        changed. If the newly selected character had no overmastery records, the
        four dropdowns kept showing the previous character's stats. This method
        always reloads or clears the controls so the display matches the selected
        character.
        """
        if not hasattr(self, "mastery_overmastery_combos"):
            return
        if not self.save:
            self._clear_overmastery_controls_for_character("Open a save to load Overmastery rows.")
            return
        group_index = self._mastery_current_overmastery_group_index()
        combo = getattr(self, "mastery_mod_character_combo", None)
        char_label = combo.currentText() if combo is not None else "selected character"
        if group_index is None:
            self._clear_overmastery_controls_for_character(f"{char_label}: no four-stat Overmastery group is mapped for this selection.")
            return

        loaded_effects = 0
        loaded_values = []
        missing_lanes = []
        self._mastery_mod_loading = True
        try:
            for lane, combo in enumerate(list(getattr(self, "mastery_overmastery_combos", []) or [])[:4]):
                rec = self._mastery_overmastery_record(group_index, lane, 1606)
                effect_value = self._record_first_value(rec, EMPTY_HASH) if rec is not None else EMPTY_HASH
                if rec is not None and int(effect_value or EMPTY_HASH) & 0xFFFFFFFF not in (0, EMPTY_HASH):
                    if self._set_overmastery_combo_to_value(combo, int(effect_value) & 0xFFFFFFFF):
                        loaded_effects += 1
                else:
                    self._set_overmastery_combo_to_value(combo, None)
                    missing_lanes.append(lane + 1)

                vrec = self._mastery_overmastery_record(group_index, lane, 1607)
                if vrec is not None:
                    value = self._record_first_value(vrec, None)
                    if value is not None:
                        loaded_values.append(int(value))
            edit = getattr(self, "mastery_overmastery_value_edit", None)
            if edit is not None:
                edit.blockSignals(True)
                if loaded_values:
                    first = loaded_values[0]
                    if int(first) == -1 or (int(first) & 0xFFFFFFFF) == 0xFFFFFFFF:
                        edit.setText("-1")
                    else:
                        edit.setText(str(int(first)))
                else:
                    edit.setText("1023")
                edit.blockSignals(False)
        finally:
            self._mastery_mod_loading = False

        self.refresh_overmastery_matrix_preview()
        if hasattr(self, "mastery_sw_lab_status"):
            if loaded_effects == 0 and not loaded_values:
                self.mastery_sw_lab_status.setText(f"{char_label}: no existing Overmastery four-stat rows found; controls cleared.")
            else:
                missing = f" Missing lane(s): {', '.join(map(str, missing_lanes))}." if missing_lanes else ""
                amount = self._mastery_amount_label(loaded_values[0]) if loaded_values else "no 1607 value rows"
                self.mastery_sw_lab_status.setText(f"{char_label}: loaded {loaded_effects}/4 Overmastery stat lane(s). Amount: {amount}.{missing}")

    def _on_mastery_mod_character_changed(self) -> None:
        self.refresh_mastery_mod_rows()
        self.load_overmastery_controls_for_current_character()
        self.refresh_basic_mastery_sweep_status()

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
        self.load_overmastery_controls_for_current_character()
        self.refresh_basic_mastery_sweep_status()

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
            return "Legacy FFFFFFFF sentinel (-1 signed)"
        if s == 0xFFFFFFFF:
            return "Legacy FFFFFFFF sentinel"
        if s == 1023:
            return "1023 / 0x03FF / Max 80%"
        if s == 512:
            return "512 / 0x0200 / Normal 20%"
        if s == 1:
            return "1 (Active)"
        if s == 0:
            return "0 (Off)"
        return self._mastery_amount_label(s)

    def _mastery_amount_label(self, amount: Any) -> str:
        try:
            raw = int(amount or 0)
        except Exception:
            return str(amount or "")
        unsigned = raw & 0xFFFFFFFF
        if raw == -1 or unsigned == 0xFFFFFFFF:
            return "Legacy FFFFFFFF sentinel (-1 signed)"
        if unsigned == 0x03FF:
            return "1023 / 0x03FF / Max 80%"
        if unsigned == 0x0200:
            return "512 / 0x0200 / Normal 20%"
        if unsigned == 0:
            return "0 / Off"
        return f"{raw:,} / 0x{unsigned:08X}"

    def _mastery_mod_optional_amount_display(self, amount: Any) -> str:
        if amount is None:
            return "Keep current"
        try:
            return str(int(amount))
        except Exception:
            return "Keep current"


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
            self._populate_basic_mastery_effect_combo()
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
        all_check = getattr(self, "mastery_overmastery_apply_all_auto_check", None)
        if all_check is not None and all_check.isChecked():
            self.apply_overmastery_four_stats_all(auto=True)
        else:
            self.apply_overmastery_four_stats_selected(auto=True)

    def _selected_overmastery_effect_values(self) -> List[Optional[int]]:
        values: List[Optional[int]] = []
        for combo in list(getattr(self, "mastery_overmastery_combos", []) or []):
            data = combo.currentData()
            values.append(None if data is None else int(data) & 0xFFFFFFFF)
        while len(values) < 4:
            values.append(None)
        return values[:4]

    def _basic_mastery_rows_for_character(self, char_unit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return the selected character's normal/basic 1606/1607 rows.

        This supports the newer community template:
            4ENNNNNN XXXXXXXX
            02580018 00000000

        The first visible Basic Mastery row supplies N.  Overmastery rows are
        excluded because the four explicit NNNNNNNN lines handle those lanes.
        """
        if not self.save:
            return []
        try:
            cu = int(self._mastery_mod_current_character_unit() if char_unit is None else char_unit)
        except Exception:
            cu = 10000
        if cu < 0:
            return []
        try:
            _rows, metas = self._mastery_mod_build_rows_for_character(cu)
        except Exception:
            metas = []
        basic: List[Dict[str, Any]] = []
        for meta in metas or []:
            try:
                unit_id = int(meta.get("unit_id", 0) or 0)
            except Exception:
                continue
            if self._mastery_overmastery_slot_for_unit(unit_id) is not None:
                continue
            # Normal/basic mastery rows for a PL group are char_unit * 10000 + ...
            if unit_id < 100000000 or int(unit_id) // 10000 != cu:
                continue
            if meta.get("mastery_rec") is None and meta.get("state_rec") is None:
                continue
            basic.append(meta)
        basic.sort(key=lambda m: (
            1 if m.get("sw_rel_int") is None else 0,
            int(m.get("sw_rel_int") or 0),
            int(m.get("unit_id") or 0),
        ))
        return basic[:BASIC_MASTERY_SW_SLOT_COUNT]

    def _basic_mastery_sweep_first_relative(self, rows: Optional[List[Dict[str, Any]]] = None) -> Optional[int]:
        rows = rows if rows is not None else self._basic_mastery_rows_for_character()
        for meta in rows or []:
            rel = meta.get("sw_rel_int")
            if rel is not None:
                try:
                    return int(rel)
                except Exception:
                    continue
        return None

    def refresh_basic_mastery_sweep_status(self) -> None:
        label = getattr(self, "mastery_basic_status_label", None)
        if label is None:
            return
        if not self.save:
            label.setText("Open a save to preview the first Basic Mastery SlotINFO row.")
            return
        try:
            char_unit = int(self._mastery_mod_current_character_unit())
        except Exception:
            char_unit = 10000
        if char_unit < 0:
            label.setText("Pick a single character/group. The 0x258 Basic Masteries sweep is not available from the raw all-row scan.")
            return
        rows = self._basic_mastery_rows_for_character(char_unit)
        first_rel = self._basic_mastery_sweep_first_relative(rows)
        char_text = getattr(self, "mastery_mod_character_combo", None).currentText() if hasattr(self, "mastery_mod_character_combo") else f"unit {char_unit}"
        if not rows:
            label.setText(f"{char_text}: no Basic Mastery rows were found for this character/group.")
            return
        if first_rel is None:
            label.setText(f"{char_text}: found {len(rows)} Basic Mastery row(s), but no Save Wizard N offset was resolved. Apply will use parsed rows only.")
            return
        direct_write = 0x28000000 | (int(first_rel) & 0x00FFFFFF)
        repeat_write = 0x4E000000 | (int(first_rel) & 0x00FFFFFF)
        label.setText(
            f"{char_text}: first Basic Mastery SlotINFO N = 0x{first_rel:06X}. "
            f"Single-code form: 0x{direct_write:08X}; repeat-code form: 0x{repeat_write:08X}. "
            f"Apply targets 0x{BASIC_MASTERY_SW_SLOT_COUNT:03X} rows with +0x{BASIC_MASTERY_SW_ROW_STRIDE:02X} stride; parsed rows visible: {len(rows)}."
        )

    def _apply_basic_mastery_sweep(self, char_unit: int, effect_value: Optional[int], value: int, *, write_effect: bool, write_value: bool) -> Dict[str, int]:
        rows = self._basic_mastery_rows_for_character(char_unit)
        first_rel = self._basic_mastery_sweep_first_relative(rows)
        count = BASIC_MASTERY_SW_SLOT_COUNT if first_rel is not None else len(rows)
        value = self._mastery_sw_normalize_write_value(value)
        effect_u32 = None if effect_value is None else int(effect_value) & 0xFFFFFFFF
        stats = {
            "effect_found": 0, "effect_changed": 0, "effect_missing": 0,
            "value_found": 0, "value_changed": 0, "value_missing": 0,
            "checked": int(count), "parsed_rows": len(rows),
        }
        if count <= 0:
            return stats
        meta_by_index = {i: meta for i, meta in enumerate(rows)}
        for i in range(int(count)):
            meta = meta_by_index.get(i, {})
            rel = int(first_rel) + i * BASIC_MASTERY_SW_ROW_STRIDE if first_rel is not None else meta.get("sw_rel_int")
            if write_effect and effect_u32 is not None:
                erec = None
                if rel is not None:
                    try:
                        erec = self._mastery_sw_effect_record_for_relative(int(rel))
                    except Exception:
                        erec = None
                if erec is None:
                    erec = meta.get("mastery_rec")
                exists, changed = self._set_record_first_value_quiet(erec, effect_u32)
                stats["effect_found"] += 1 if exists else 0
                stats["effect_changed"] += 1 if changed else 0
                stats["effect_missing"] += 0 if exists else 1
            if write_value:
                vrec = None
                if rel is not None:
                    try:
                        vrec = self._mastery_sw_value_record_for_relative(int(rel))
                    except Exception:
                        vrec = None
                if vrec is None:
                    vrec = meta.get("state_rec")
                exists, changed = self._set_record_first_value_quiet(vrec, value)
                stats["value_found"] += 1 if exists else 0
                stats["value_changed"] += 1 if changed else 0
                stats["value_missing"] += 0 if exists else 1
        return stats

    def apply_basic_mastery_sweep_selected(self) -> None:
        if not self.save:
            return
        try:
            char_unit = int(self._mastery_mod_current_character_unit())
        except Exception:
            char_unit = 10000
        if char_unit < 0:
            QMessageBox.warning(self, "Basic Masteries sweep", "Pick a single character/group first. The 0x258 Basic Masteries sweep is disabled for the raw all-row scan.")
            return
        write_effect = bool(getattr(self, "mastery_basic_write_effect_check", None) is None or self.mastery_basic_write_effect_check.isChecked())
        write_value = bool(getattr(self, "mastery_basic_write_value_check", None) is None or self.mastery_basic_write_value_check.isChecked())
        if not write_effect and not write_value:
            self.statusBar().showMessage("Nothing selected to write. Enable 1606 IDs and/or 1607 values.", 4500)
            return
        effect_value = None
        if write_effect:
            combo = getattr(self, "mastery_basic_effect_combo", None)
            effect_value = combo.currentData() if combo is not None else None
            if effect_value is None:
                QMessageBox.warning(self, "Basic Masteries sweep", "Pick a Basic Mastery ID before writing 1606 IDs.")
                return
        value_text = getattr(self, "mastery_basic_value_edit", None).text() if hasattr(self, "mastery_basic_value_edit") else str(OVERMASTERY_VALUE_MAX)
        value = self._parse_mastery_u32_text(value_text, OVERMASTERY_VALUE_MAX)
        stats = self._apply_basic_mastery_sweep(char_unit, effect_value, value, write_effect=write_effect, write_value=write_value)
        self._refresh_mastery_after_bulk_write()
        self.refresh_basic_mastery_sweep_status()
        try:
            tabs = getattr(self, "mastery_value_tabs", None)
            if tabs is not None and tabs.currentIndex() == 1:
                self.refresh_mastery_mod_rows()
        except Exception:
            pass
        char_text = getattr(self, "mastery_mod_character_combo", None).currentText() if hasattr(self, "mastery_mod_character_combo") else f"unit {char_unit}"
        parts = []
        if write_effect:
            parts.append(f"1606 IDs {stats['effect_changed']} changed / {stats['effect_found']} found")
        if write_value:
            parts.append(f"1607 values {stats['value_changed']} changed / {stats['value_found']} found")
        shown_value = self._mastery_amount_label(value)
        msg = f"Basic Masteries sweep for {char_text}: " + "; ".join(parts) + f". Value {shown_value}. Checked {stats['checked']} row(s)."
        if getattr(self, "mastery_sw_lab_status", None) is not None:
            self.mastery_sw_lab_status.setText(msg)
        self.statusBar().showMessage(msg + " Save As to test in game.", 9000)

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

    def apply_overmastery_four_stats_selected(self, auto: bool = False) -> None:
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
        self.load_overmastery_controls_for_current_character()
        shown = self._mastery_amount_label(value)
        prefix = "Auto applied selected" if auto else "Selected group applied"
        msg = f"{prefix}: stats {stats['effect_changed']}, values {stats['value_changed']} · {shown}"
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
        value = self._parse_mastery_u32_text(getattr(self, "mastery_overmastery_value_edit", None).text() if hasattr(self, "mastery_overmastery_value_edit") else str(OVERMASTERY_VALUE_MAX), OVERMASTERY_VALUE_MAX)
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
        self.load_overmastery_controls_for_current_character()
        shown = self._mastery_amount_label(value)
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
            self.refresh_overmastery_matrix_preview()
        except Exception:
            pass
        try:
            self.refresh_basic_mastery_sweep_status()
        except Exception:
            pass
        try:
            self.update_status_text_light()
        except Exception:
            pass





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
        # Legacy tests sometimes used FFFFFFFF as a raw sentinel. Prefer
        # 0x03FF for normal max / 80% overmastery rows; keep -1 support so
        # older saves/codes can still be inspected or reproduced.
        if ivalue == -1:
            return 0xFFFFFFFF
        return max(0, min(0xFFFFFFFF, ivalue))


    def _mastery_sw_effect_record_for_relative(self, rel: int) -> Optional[UnitRecord]:
        for candidate in (int(rel), int(rel) - 1, int(rel) + 1):
            rec = self._mastery_mod_record_by_sw_relative(candidate, id_type=1606)
            if rec is not None:
                return rec
        return None









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
            # Pasted code fallback: N is a 28000000-series direct write or 4E000000-style range write.
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
            0x7B727910: "Sigil Slot Add / Restore (danger: >13 slots breaks game)",
            0xC4925BD7: "Attack Power Up",
            0x68B39018: "Chain Burst Damage Up",
            0x45C65767: "Critical Rate",
            0x54929589: "Healing Cap Up",
            0x52A207B5: "Health Up",
            0x43B7581D: "Normal Damage Cap Up",
            0x4A4C093D: "SBA Damage Cap Up",
            0x4E42646B: "SBA Damage Up",
            0x9C555433: "Skill Damage Cap Up",
            0x9A97C049: "Skill Damage Up",
            0x6CB38EF3: "Stun Power Up",
            # Pasted sheet also listed a second/right OP ID table. Keep these named
            # so hashes do not show as unknown if they appear in a save or sheet import.
            0xD75B92C4: "Attack Power Up (OP right-table variant)",
            0x1890B368: "Chain Burst Damage Up (OP right-table variant)",
            0x6757C645: "Critical Rate (OP right-table variant)",
            0x89959254: "Healing Cap Up (OP right-table variant)",
            0xB507A252: "Health Up (OP right-table variant)",
            0x1D58B743: "Normal Damage Cap Up (OP right-table variant)",
            0x3D094C4A: "SBA Damage Cap Up (OP right-table variant)",
            0x6B64424E: "SBA Damage Up (OP right-table variant)",
            0x3354559C: "Skill Damage Cap Up (OP right-table variant)",
            0x49C0979A: "Skill Damage Up (OP right-table variant)",
            0xF38EB36C: "Stun Power Up (OP right-table variant)",
            # Additional MED_EFF labels observed in previous test/hash data.
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
            return "Sigil Slot Add / Layout Safety"
        if h in {0x43B7581D, 0x4A4C093D, 0x9C555433, 0x1D58B743, 0x3D094C4A, 0x3354559C}:
            return "Damage Cap"
        if h in {0xC4925BD7, 0x9A97C049, 0x6CB38EF3, 0x4E42646B, 0x45C65767, 0x68B39018, 0xD75B92C4, 0x1890B368, 0x6757C645, 0x6B64424E, 0x49C0979A, 0xF38EB36C}:
            return "Damage / Offense"
        if h in {0x52A207B5, 0x54929589, 0xB507A252, 0x89959254}:
            return "Survival / Healing"
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
                amount_label = self._mastery_amount_label(state)
                row = [slot + 1, "—", effect_name if not gbid else f"{effect_name} ({gbid})", "Overmastery 4-stat lane", f"Lane {slot + 1}/4", amount_label, "—", self._hash_hex_or_dash(mastery), unit_id]
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
            mode_name = {"effects": "Mastery Effects", "board": "Board Slot Keys", "overmastery": "Overmastery 4-Stat Slots"}.get(mode, mode)
            self.mastery_slot_summary_label.setText(f"{name} · {mode_name}: showing {len(self.mastery_slot_rows_meta)} row(s). 1606 = effect/stat hash, 1607 = amount/state (0200=20%, 03FF=80%), 1601 = slot key/layout.")
        if hasattr(self, "mastery_slot_table"):
            self._set_table_widths(self.mastery_slot_table, {0: 70, 1: 70, 2: 330, 3: 150, 4: 75, 5: 85, 6: 270, 7: 125, 8: 110})
        self.update_mastery_slot_detail()


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
        state_or_amount = self._mastery_amount_label(state) if meta.get("mode") == "overmastery" else self._mastery_state_display(state, mastery)
        label.setText(
            f"Character unit {meta.get('char_unit')} · Slot/Lane {int(meta.get('slot', 0)) + 1}{socket_text}\n"
            f"View: {meta.get('mode')} · Unit: {meta.get('unit_id')}\n"
            f"Effect/Stat: {effect_name}\n"
            f"Group: {self._mastery_effect_category(mastery, slotinfo)} · State/Amount: {state_or_amount}\n"
            f"GBID: {gbid} · 1606 effect/stat hash: {self._hash_hex_or_dash(mastery)}\n"
            f"1601 slot key: {self._mastery_hash_display(slotinfo, 'slotinfo')}"
            f"{missing_text}"
        )













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
        """Refresh the reusable-empty-sigil model.

        The old Empty Slots tab was removed from the UI. Older construction code
        left self.sigil_empty_status/self.sigil_empty_table wrappers pointing at
        Qt objects that could be deleted, so this function must never assume
        those widgets are alive.
        """
        if not hasattr(self, "sigil_empty_model"):
            return

        def _safe_set_empty_status(message: str) -> None:
            widget = getattr(self, "sigil_empty_status", None)
            if widget is None:
                return
            try:
                widget.setText(message)
            except RuntimeError:
                try:
                    delattr(self, "sigil_empty_status")
                except Exception:
                    pass

        def _safe_width_empty_table() -> None:
            table = getattr(self, "sigil_empty_table", None)
            if table is None:
                return
            try:
                self._set_table_widths(table, {0: 110, 1: 100, 2: 90, 3: 160, 4: 100, 5: 280})
            except RuntimeError:
                try:
                    delattr(self, "sigil_empty_table")
                except Exception:
                    pass

        if not self.save:
            self.sigil_empty_model.set_rows([])
            self.sigil_empty_rows_meta = []
            _safe_set_empty_status("Open a save to list reusable empty sigil slots.")
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
            row_meta = {
                "unit_id": unit_id,
                "slot_rec": slot_rec,
                "hash_rec": fields.get(2703),
                "level_rec": level_rec,
                "worn_rec": fields.get(2706),
                "flags_rec": fields.get(2707),
            }
            row_meta.update(self._sigil_trait_meta_records_for_unit(int(unit_id)))
            meta.append(row_meta)
        self.sigil_empty_rows_meta = meta
        self.sigil_empty_model.set_rows(rows)
        _safe_width_empty_table()
        _safe_set_empty_status(f"Empty sigil slots: {self.format_value(len(rows))} reusable slot(s) found.")

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
        owner_hash = self.sigil_database_assign_combo.currentData() if hasattr(self, "sigil_database_assign_combo") else EMPTY_HASH
        result = self._add_sigil_hash_level_to_empty_slot(int(meta.get("hash", 0)) & 0xFFFFFFFF, level=level, locked=locked, owner_hash=owner_hash)
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
        # Normal view uses only contiguous readable columns:
        # Slot, Sigil, Lv, Trait 1, Trait 2, Character, Flags.
        # Technical mode exposes Unit/GBID/hash plus the separate hidden trait levels.
        for col in (0, 3, 4, 10, 11, 12):
            self.sigil_table.setColumnHidden(col, not show_technical)
        try:
            header = self.sigil_table.horizontalHeader()
            header.setStretchLastSection(False)
            header.setMinimumSectionSize(40)
            for col in range(self.sigil_model.columnCount()):
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            for col in (2, 6, 7, 8):
                header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
            self.sigil_table.setColumnWidth(1, 44)     # Slot
            self.sigil_table.setColumnWidth(2, 210)    # Sigil
            self.sigil_table.setColumnWidth(5, 100)    # Level
            self.sigil_table.setColumnWidth(6, 170)    # Trait 1
            self.sigil_table.setColumnWidth(7, 170)    # Trait 2
            self.sigil_table.setColumnWidth(8, 160)    # Character
            self.sigil_table.setColumnWidth(9, 50)     # Flags
            self.sigil_table.setColumnWidth(10, 64)    # T1 Lv technical
            self.sigil_table.setColumnWidth(11, 64)    # T2 Lv technical
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
        self._sigil_trait_grouped_cache = None
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
            trait_meta = self._sigil_trait_meta_records_for_unit(int(unit_id))
            trait1_hash = self._record_first_value(trait_meta.get("trait1_hash_rec"), EMPTY_HASH)
            trait1_level = self._record_first_value(trait_meta.get("trait1_level_rec"), 0)
            trait2_hash = self._record_first_value(trait_meta.get("trait2_hash_rec"), EMPTY_HASH)
            trait2_level = self._record_first_value(trait_meta.get("trait2_level_rec"), 0)
            if is_empty:
                trait1_name = ""
                trait1_level = ""
                trait2_name = ""
                trait2_level = ""
            else:
                trait1_name, _trait1_gbid, _ = self.hash_entry_parts(trait1_hash) if trait1_hash not in (0, EMPTY_HASH) else ("", "", "")
                trait2_name, _trait2_gbid, _ = self.hash_entry_parts(trait2_hash) if trait2_hash not in (0, EMPTY_HASH) else ("", "", "")
                if trait1_hash in (0, EMPTY_HASH):
                    trait1_level = ""
                if trait2_hash in (0, EMPTY_HASH):
                    trait2_level = ""
            row = [
                unit_id,
                self.value1(fields.get(2702), ""),
                s_name,
                s_gbid,
                s_hash,
                self.value1(level_rec, ""),
                self._sigil_trait_display(trait1_hash, trait1_level),
                self._sigil_trait_display(trait2_hash, trait2_level),
                worn_display,
                self.value1(fields.get(2707), ""),
                trait1_level,
                trait2_level,
                worn_gbid,
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
                **trait_meta,
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
            self._set_table_widths(self.sigil_table, {1: 44, 2: 210, 3: 140, 4: 105, 5: 100, 6: 170, 7: 170, 8: 160, 9: 50, 10: 64, 11: 64, 12: 140})
        if hasattr(self, "sigil_database_model"):
            self.refresh_sigil_database_rows()
        self.update_sigil_detail()





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
                "cap_rec": fields.get(2805),
                "unk_2805_rec": fields.get(2805),
                "unk_2806_rec": fields.get(2806),
                "trait_id_rec": fields.get(1701),
                "trait_level_rec": fields.get(1702),
                "fields": fields,
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
            self._set_table_widths(self.weapon_table, {1: 380, 2: 170, 4: 120, 5: 90, 6: 95, 10: 260})
            self._auto_fit_table(self.weapon_table)
        if hasattr(self, "weapon_empty_model"):
            self.refresh_weapon_empty_slot_rows()
        if hasattr(self, "weapon_database_model"):
            self.refresh_weapon_database_rows()
        if hasattr(self, "weapon_trait_combo"):
            self.refresh_weapon_trait_choices()
        if hasattr(self, "weapon_cap_trait_weapon_combo"):
            self._sync_weapon_cap_trait_weapon_combo()
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
        try:
            self._sync_settings_controls()
        except Exception:
            pass
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
