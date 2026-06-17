# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for a single-file GBFR Relink Editor executable.

Build:
    pyinstaller --clean --noconfirm GBFRRelinkEditor.spec

Output:
    dist/GBFRRelinkEditor.exe
"""
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
PROJECT_ROOT = Path(SPECPATH).resolve()

# The editor uses small bootstrap sys.path additions at runtime so source mode
# can import grouped modules by their legacy short names, e.g. `gbfr_save`.
# PyInstaller analyzes imports before that runtime bootstrap executes, so the
# same folders must be present in Analysis.pathex and the short-name modules
# must be listed as hidden imports for the one-file EXE.
MODULE_SEARCH_PATHS = [
    str(PROJECT_ROOT),
    str(PROJECT_ROOT / 'gbfr_editor' / 'core'),
    str(PROJECT_ROOT / 'gbfr_editor' / 'data'),
    str(PROJECT_ROOT / 'gbfr_editor' / 'research'),
    str(PROJECT_ROOT / 'gbfr_editor' / 'ui'),
    str(PROJECT_ROOT / 'gbfr_editor' / 'cli'),
]

SHORT_NAME_MODULES = [
    'cheat_actions', 'diff_tools', 'gbfr_save', 'hash_tools', 'hashing',
    'entity_prefixes', 'google_sheet_audit', 'item_db', 'item_id_catalog',
    'model_id_catalog', 'phase_id_catalog', 'preset_packs', 'quest_id_catalog',
    'reference_db', 'resource_id_db', 'save_id_catalog', 'save_wizard_cheats',
    'sigil_gem_id_catalog', 'trait_skill_id_catalog',
    'gbid_tools', 'hash_resolver', 'id_audit', 'research_tools', 'save_mapper',
    'unit_labeler', 'unit_meta',
]

# Bundle the lookup databases inside the one-file EXE. At runtime PyInstaller
# extracts these under sys._MEIPASS, and gbfr_editor.paths resolves them there.
added_files = [
    ('gbfr_editor/resources', 'gbfr_editor/resources'),
]

hiddenimports = (
    collect_submodules('PyQt6')
    + collect_submodules('gbfr_editor')
    + SHORT_NAME_MODULES
)


a = Analysis(
    ['app.py'],
    pathex=MODULE_SEARCH_PATHS,
    binaries=[],
    datas=added_files,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='GBFRRelinkEditor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
