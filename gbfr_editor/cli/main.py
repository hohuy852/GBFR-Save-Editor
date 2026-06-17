from __future__ import annotations

from gbfr_editor.bootstrap import bootstrap_paths
from gbfr_editor.paths import RESOURCE_DIR
bootstrap_paths()

import argparse
import json
from pathlib import Path

from gbfr_save import GBFRSaveData
from unit_meta import unit_name
from unit_labeler import UnitLabelIndex
from diff_tools import compare_saves, format_compare_text, write_compare_csv, write_compare_json
from item_db import ItemDatabase, DEFAULT_ITEM_URL, TRAIT_SKILL_URL, RAW_SIGIL_GEM_URL, source_urls_from_text
from item_id_catalog import format_catalog_summary, write_catalog_csv, catalog_rows, COMMUNITY_ITEM_ID_TARGET_ROWS
from sigil_gem_id_catalog import format_sigil_summary, write_sigil_catalog_csv, sigil_rows
from trait_skill_id_catalog import format_trait_skill_summary, write_trait_skill_catalog_csv, trait_skill_rows
from model_id_catalog import format_model_summary, write_model_catalog_csv, model_rows
from phase_id_catalog import format_phase_summary, write_phase_catalog_csv, phase_rows
from quest_id_catalog import format_quest_summary, write_quest_catalog_csv, quest_rows
from save_id_catalog import format_save_id_summary, save_id_rows, write_save_id_catalog_csv
from google_sheet_audit import audit_sheet_sources, audit_summary, write_audit_csv, urls_from_resource_file
from resource_id_db import ResourceIdDatabase, DEFAULT_RESOURCE_URLS
from gbid_tools import build_candidate_records
from research_tools import search_values, format_search_text, write_search_csv, scan_known_hashes, format_hash_scan_text, write_hash_scan_csv
from hashing import gbfr_hash_hex, gbfr_hash
from entity_prefixes import describe_entity_code
from reference_db import ReferenceDatabase
from preset_packs import get_preset_pack, search_preset_packs, list_preset_packs
from save_mapper import build_save_map, build_unknown_field_report, write_save_map_csv, write_save_map_json, save_map_summary_text
from hash_resolver import resolve_unknown_hashes, format_hash_candidates, write_hash_candidates_csv
from id_audit import build_id_audit, id_audit_summary, write_id_audit_csv
from save_wizard_cheats import (
    SAVE_WIZARD_SHEET_URL, SaveWizardCheat, get_builtin_save_wizard_cheat,
    list_builtin_save_wizard_cheats, load_sheet_csv, parse_sheet_cheats,
)
from cheat_actions import (
    complete_quest_tables_splusplus, unlock_title_archive_candidates,
    set_character_overmastery_hashes, clear_character_overmastery_hashes, patch_summary,
)


def cmd_info(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    print(json.dumps(save.summary(), indent=2))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    save.export_report(args.output, limit_values=args.limit_values)
    print(f"Wrote {args.output}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    labels = UnitLabelIndex.from_save(save, default_item_db(getattr(args, "item_csv", None)))
    for rec in save.records:
        if args.id_type is not None and rec.id_type != args.id_type:
            continue
        if args.kind is not None and rec.kind != args.kind:
            continue
        if args.unit_id is not None and rec.unit_id != args.unit_id:
            continue
        vals = save.get_values(rec, args.limit_values)
        suffix = " ..." if rec.value_count > args.limit_values else ""
        unit_label = labels.label_for(rec)
        label_part = f" label={unit_label!r}" if unit_label else ""
        print(f"{rec.key:<14} {rec.kind:<6} id={rec.id_type:<5} {unit_name(rec.id_type):<38} unit={rec.unit_id:<7}{label_part:<54} count={rec.value_count:<4} values={vals}{suffix}")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    rec = save.find_first(args.kind, args.id_type, args.unit_id)
    if rec is None:
        raise SystemExit(f"No record found for kind={args.kind} id={args.id_type} unit={args.unit_id}")
    values = save.parse_user_values(rec, args.values)
    save.set_values(rec, values)
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".edited"))
    save.save_as(out, update_hash=True)
    print(f"Patched {rec.key} / {unit_name(rec.id_type)} and wrote {out}")
    return 0



def cmd_search_value(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    rows = search_values(save, args.query, exact=True, limit=args.limit)
    if args.csv:
        write_search_csv(rows, args.csv)
        print(f"Wrote value search CSV to {args.csv}")
    else:
        print(format_search_text(rows, max_rows=args.limit))
    return 0


def cmd_candidates(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = ItemDatabase.load_many(args.item_csv or [])
    rows = build_candidate_records(save, db, limit=args.limit)
    for row in rows:
        print(f"{row.category:<22} {row.confidence:<12} {row.kind:<6} id={row.id_type:<5} {row.id_name:<42} unit={row.unit_id:<8} count={row.value_count:<4} values={row.preview}  # {row.note}")
    return 0


def cmd_gbid(args: argparse.Namespace) -> int:
    db = default_item_db(args.item_csv)
    rows = db.search(args.query, limit=args.limit)
    for entry in rows:
        print(f"{entry.item_id:<16} {entry.hash_hex:<10} {entry.hash_value:<12} {entry.name}")
    return 0




def cmd_hash(args: argparse.Namespace) -> int:
    db = default_item_db(args.item_csv)
    for text in args.text:
        hx = gbfr_hash_hex(text)
        dec = gbfr_hash(text)
        hit = db.lookup_hash(dec)
        suffix = f"  -> DB match: {hit.item_id} {hit.display_name}" if hit else ""
        print(f"{text:<32} {hx:<10} {dec:<12}{suffix}")
    return 0


def cmd_google_sheet_audit(args: argparse.Namespace) -> int:
    urls = []
    if args.url:
        urls.extend(args.url)
    if args.urls_file:
        for file_name in args.urls_file:
            urls.extend(urls_from_resource_file(file_name))
    if not urls:
        urls = urls_from_resource_file()
    rows = audit_sheet_sources(urls, fetch=not args.offline, timeout=args.timeout, dump_dir=args.dump_dir)
    if args.csv:
        write_audit_csv(rows, args.csv)
        print(f"Wrote Google Sheet audit CSV to {args.csv}")
    print(audit_summary(rows))
    return 0


def cmd_db_stats(args: argparse.Namespace) -> int:
    import collections
    item_db = default_item_db(args.item_csv)
    res_db = default_resource_db(args.resource_csv)
    ref_db = default_reference_db(None)
    item_cats = collections.Counter(e.category for e in item_db.by_hash.values())
    res_cats = collections.Counter(e.category for e in res_db.entries)
    ref_cats = collections.Counter(e.category for e in ref_db.entries)
    print(f"GBID/hash rows: {len(item_db)}")
    for k, v in item_cats.most_common():
        print(f"  {k:<28} {v}")
    print(f"\nResource ID rows: {len(res_db.entries)}")
    for k, v in res_cats.most_common():
        print(f"  {k:<28} {v}")
    print(f"\nReference rows: {len(ref_db.entries)}")
    for k, v in ref_cats.most_common():
        print(f"  {k:<28} {v}")
    from save_id_catalog import SAVE_ID_ROWS
    save_id_cats = collections.Counter(e.manager for e in SAVE_ID_ROWS)
    print(f"\nSave field ID labels: {len(SAVE_ID_ROWS)}")
    for k, v in save_id_cats.most_common():
        print(f"  {k:<28} {v}")
    return 0


def cmd_entity_prefix(args: argparse.Namespace) -> int:
    for text in args.code:
        print(describe_entity_code(text))
        print()
    return 0



def cmd_resolve_unknown_hashes(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    rows = resolve_unknown_hashes(save, db, limit=args.limit)
    if args.csv:
        write_hash_candidates_csv(rows, args.csv)
        print(f"Wrote generated hash candidates CSV to {args.csv}")
    else:
        print(format_hash_candidates(rows))
    return 0


def cmd_save_map(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    if args.summary:
        print(save_map_summary_text(save, db))
        return 0
    rows = build_unknown_field_report(save, db, limit=args.limit) if args.unknown_only else build_save_map(save, db, limit=args.limit)
    if args.json:
        write_save_map_json(save, db, args.json, unknown_only=args.unknown_only)
        print(f"Wrote save map JSON to {args.json}")
        return 0
    if args.csv:
        write_save_map_csv(save, db, args.csv, unknown_only=args.unknown_only)
        print(f"Wrote save map CSV to {args.csv}")
        return 0
    for row in rows:
        print(
            f"{row['manager']:<22} {row['confidence']:<12} {row['kind']:<6} "
            f"id={row['field_id']:<5} {row['field_name']:<42} "
            f"records={row['records']:<5} units={row['unit_span']:<13} "
            f"known={row['known_hashes']:<4} unknown={row['unknown_hashes']:<4} # {row['note']}"
        )
    return 0


def cmd_unknown_save_fields(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    rows = build_unknown_field_report(save, db, limit=args.limit)
    if args.csv:
        write_save_map_csv(save, db, args.csv, unknown_only=True)
        print(f"Wrote unknown/research target CSV to {args.csv}")
        return 0
    for row in rows:
        print(
            f"{row['manager']:<22} {row['confidence']:<12} {row['kind']:<6} "
            f"id={row['field_id']:<5} {row['field_name']:<42} "
            f"records={row['records']:<5} units={row['unit_span']:<13} "
            f"unknown_hashes={row['unknown_hashes']:<4} sample={row['sample_values']} # {row['note']}"
        )
    return 0


def default_item_db(paths=None) -> ItemDatabase:
    if paths:
        return ItemDatabase.load_many(paths)
    return ItemDatabase.load_many([
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



def default_resource_db(paths=None) -> ResourceIdDatabase:
    if paths:
        return ResourceIdDatabase.load_many(paths)
    return ResourceIdDatabase.load_many([
        RESOURCE_DIR / "resource_ids_seed.csv",
        RESOURCE_DIR / "quest_ids_seed.csv",
        RESOURCE_DIR / "resource_ids_downloaded.csv",
    ])


def default_reference_db(paths=None) -> ReferenceDatabase:
    if paths:
        return ReferenceDatabase.load_many(paths)
    return ReferenceDatabase.load_many([
        RESOURCE_DIR / "reference_notes_seed.csv",
        RESOURCE_DIR / "reference_notes_downloaded.csv",
    ])


def cmd_reference_notes(args: argparse.Namespace) -> int:
    db = default_reference_db(args.csv)
    rows = db.search(args.query or "", limit=args.limit)
    if args.out_csv:
        import csv
        with open(args.out_csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["category", "topic", "key", "value", "notes", "source"])
            writer.writeheader()
            for e in rows:
                writer.writerow({"category": e.category, "topic": e.topic, "key": e.key, "value": e.value, "notes": e.notes, "source": e.source})
        print(f"Wrote {len(rows)} reference rows to {args.out_csv}")
    else:
        for e in rows:
            print(f"{e.category:<28} {e.topic:<28} {e.key:<28} {e.value}  # {e.notes}")
    return 0


def cmd_resource_ids(args: argparse.Namespace) -> int:
    db = default_resource_db(args.csv)
    rows = db.search(args.query or "", limit=args.limit)
    for e in rows:
        dec = "" if e.decimal_value is None else str(e.decimal_value)
        print(f"{e.category:<22} {e.id_text:<12} {dec:<10} {e.name}")
    return 0


def cmd_download_resource_ids(args: argparse.Namespace) -> int:
    urls = []
    for url in args.url or []:
        urls.append(url)
    for file_path in args.urls_file or []:
        urls.extend([line.strip() for line in Path(file_path).read_text(encoding="utf-8").splitlines() if line.strip() and not line.strip().startswith("#")])
    if args.community or not urls:
        urls.extend(DEFAULT_RESOURCE_URLS)
    db, errors = ResourceIdDatabase.download_many(urls, timeout=args.timeout)
    if args.merge_seed:
        seed = default_resource_db(None)
        seed.merge(db)
        db = seed
    db.save_csv(args.output)
    print(f"Wrote {len(db.entries)} resource ID rows to {args.output}")
    if errors:
        print("Some sources failed:")
        for err in errors:
            print("- " + err)
    return 0

def cmd_item_id_catalog(args: argparse.Namespace) -> int:
    db = default_item_db(args.item_csv)
    if args.download:
        downloaded, errors = ItemDatabase.download_many([DEFAULT_ITEM_URL, RAW_SIGIL_GEM_URL, TRAIT_SKILL_URL], timeout=args.timeout)
        db.merge(downloaded)
        if args.cache:
            db.save_csv(RESOURCE_DIR / "item_ids_downloaded.csv")
        if errors:
            print("Some sources failed:")
            for err in errors:
                print("- " + err)
    if args.csv:
        write_catalog_csv(db, args.csv, args.query or "")
        print(f"Wrote item ID catalog CSV to {args.csv}")
        return 0
    if args.summary or not args.query:
        print(format_catalog_summary(db))
    if args.query:
        rows = catalog_rows(db, args.query)[:args.limit]
        for category, group, name, gbid, h, aliases in rows:
            print(f"{category:<16} {group:<32} {gbid:<18} 0x{h:<8} {name}")
        if not rows:
            print("No matching Item ID catalog rows.")
    return 0


def cmd_sigil_gem_id_catalog(args: argparse.Namespace) -> int:
    db = default_item_db(args.item_csv)
    if args.download:
        downloaded, errors = ItemDatabase.download_many([RAW_SIGIL_GEM_URL], timeout=args.timeout)
        db.merge(downloaded)
        if args.cache:
            db.save_csv(RESOURCE_DIR / "item_ids_downloaded.csv")
        if errors:
            print("Some sources failed:")
            for err in errors:
                print("- " + err)
    if args.csv:
        write_sigil_catalog_csv(db, args.csv, args.query or "", hide_dummy=args.hide_dummy)
        print(f"Wrote sigil/gem ID catalog CSV to {args.csv}")
        return 0
    if args.summary or not args.query:
        print(format_sigil_summary(db))
    if args.query:
        rows = sigil_rows(db, args.query, hide_dummy=args.hide_dummy)[:args.limit]
        for group, name, gbid, h, family, grade, tier, plus, variant, aliases in rows:
            print(f"{group:<22} {gbid:<14} 0x{h:<8} tier={tier:<3} plus={plus:<6} {name}")
        if not rows:
            print("No matching Sigil/Gem ID catalog rows.")
    return 0




def cmd_trait_skill_id_catalog(args: argparse.Namespace) -> int:
    db = default_item_db(args.item_csv)
    if args.download:
        downloaded, errors = ItemDatabase.download_many([TRAIT_SKILL_URL], timeout=args.timeout)
        db.merge(downloaded)
        if args.cache:
            db.save_csv(RESOURCE_DIR / "item_ids_downloaded.csv")
        if errors:
            print("Some sources failed:")
            for err in errors:
                print("- " + err)
    if args.csv:
        write_trait_skill_catalog_csv(db, args.csv, args.query or "", hide_unused=args.hide_unused)
        print(f"Wrote trait/skill ID catalog CSV to {args.csv}")
        return 0
    if args.summary or not args.query:
        print(format_trait_skill_summary(db))
    if args.query:
        rows = trait_skill_rows(db, args.query, hide_unused=args.hide_unused)[:args.limit]
        for group, name, skill_id, h, family, variant, status, aliases in rows:
            print(f"{group:<32} {skill_id:<14} 0x{h:<8} family={family:<3} variant={variant:<2} {status:<16} {name}")
        if not rows:
            print("No matching Trait/Skill ID catalog rows.")
    return 0


def cmd_model_id_catalog(args: argparse.Namespace) -> int:
    db = default_resource_db(args.resource_csv)
    if args.csv:
        write_model_catalog_csv(db, args.csv, args.query or "")
        print(f"Wrote model ID catalog CSV to {args.csv}")
        return 0
    if args.summary or not args.query:
        print(format_model_summary(db))
    if args.query:
        rows = model_rows(db, args.query)[:args.limit]
        for category, group, name, model_id, h, dec, aliases, source in rows:
            hash_text = f"0x{h}" if h else ""
            dec_text = "" if dec in (None, "") else str(dec)
            print(f"{category:<24} {model_id:<10} {hash_text:<12} {dec_text:<8} {name}")
        if not rows:
            print("No matching Model ID catalog rows.")
    return 0


def cmd_phase_id_catalog(args: argparse.Namespace) -> int:
    db = default_resource_db(args.resource_csv)
    if args.csv:
        write_phase_catalog_csv(db, args.csv, args.query or "")
        print(f"Wrote phase ID catalog CSV to {args.csv}")
        return 0
    if args.summary or not args.query:
        print(format_phase_summary(db))
    if args.query:
        rows = phase_rows(db, args.query)[:args.limit]
        for category, group, name, phase_id, entity_code, phase_hash, entity_hash, source, aliases in rows:
            ent_hash_text = f" entity=0x{entity_hash}" if entity_hash else ""
            print(f"{phase_id:<5} {entity_code:<6} 0x{phase_hash:<8}{ent_hash_text:<22} {group:<42} {name}")
        if not rows:
            print("No matching Phase ID catalog rows.")
    return 0


def cmd_quest_id_catalog(args: argparse.Namespace) -> int:
    db = default_resource_db(args.resource_csv)
    if args.csv:
        write_quest_catalog_csv(db, args.csv, args.query or "")
        print(f"Wrote quest ID catalog CSV to {args.csv}")
        return 0
    if args.summary or not args.query:
        print(format_quest_summary(db))
    if args.query:
        rows = quest_rows(db, args.query)[:args.limit]
        for category, group, name, quest_id, dec, encoded_hex, source, aliases in rows:
            print(f"{quest_id:<7} {str(dec):<10} {encoded_hex:<10} {group:<44} {name}")
        if not rows:
            print("No matching Quest ID catalog rows.")
    return 0


def cmd_save_id_catalog(args: argparse.Namespace) -> int:
    rows = save_id_rows(args.query or "", limit=args.limit)
    if args.csv:
        write_save_id_catalog_csv(rows, args.csv)
        print(f"Wrote save field ID catalog CSV to {args.csv}")
        return 0
    if args.summary or not args.query:
        print(format_save_id_summary())
    if args.query:
        for r in rows:
            note = f"  # {r.editor_note}" if r.editor_note else ""
            print(f"{r.field_id:<5} {r.manager:<24} {r.kind:<12} {r.name:<42} {r.meaning}{note}")
        if not rows:
            print("No matching save field ID rows.")
    return 0

def cmd_download_gbids(args: argparse.Namespace) -> int:
    urls = []
    if args.community:
        urls.extend([DEFAULT_ITEM_URL, RAW_SIGIL_GEM_URL, TRAIT_SKILL_URL])
    for url in args.url or []:
        urls.append(url)
    for file_path in args.urls_file or []:
        urls.extend(source_urls_from_text(Path(file_path).read_text(encoding="utf-8")))
    if not urls:
        urls.extend([DEFAULT_ITEM_URL, RAW_SIGIL_GEM_URL, TRAIT_SKILL_URL])
    db, errors = ItemDatabase.download_many(urls, timeout=args.timeout)
    if args.merge_seed:
        db2 = default_item_db(None)
        db2.merge(db)
        db = db2
    db.save_csv(args.output)
    print(f"Wrote {len(db)} GBID rows to {args.output}")
    if errors:
        print("Some sources failed:")
        for err in errors:
            print("- " + err)
    return 0


def _entry_parts(db: ItemDatabase, value) -> tuple[str, str, str, str]:
    try:
        iv = int(value) & 0xFFFFFFFF
    except Exception:
        return str(value), "", "", ""
    entry = db.lookup_hash(iv)
    if entry:
        return entry.display_name, entry.item_id, entry.hash_hex, entry.alias_text
    return f"Unknown 0x{iv:08X}", "", f"{iv:08X}", ""


def cmd_unit_map(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    labels = UnitLabelIndex.from_save(save, db)
    rows = []
    for group, unit, name, source, hash_hex, gbid, fields in labels.rows():
        row = {"group": group, "unit": unit, "label": name, "source": source, "hash": hash_hex, "gbid": gbid, "fields": fields}
        if args.filter and args.filter.lower() not in " ".join(str(v).lower() for v in row.values()):
            continue
        rows.append(row)
    if args.csv:
        import csv
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["group", "unit", "label", "source", "hash", "gbid", "fields"])
            writer.writeheader(); writer.writerows(rows)
        print(f"Wrote unit map CSV to {args.csv}")
    else:
        for r in rows[:args.limit]:
            print(f"{r['group']:<18} unit={r['unit']:<7} {r['label']:<60} {r['gbid']:<16} {r['hash']}")
        if len(rows) > args.limit:
            print(f"... +{len(rows)-args.limit} more")
    return 0


def cmd_items(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    grouped = save.group_by_unit([2102, 2103, 2104, 2105, 1901, 1902, 1903, 1904, 2002, 2003, 2004])
    rows = []
    for unit_id, fields in sorted(grouped.items()):
        def val(id_type, default=""):
            rec = fields.get(id_type)
            return save.get_values(rec, 1)[0] if rec and rec.value_count else default
        item_hash = val(2102, val(1901, val(2002, 0)))
        index_or_serial = val(2103, val(1902, val(2003, "")))
        flag = val(2104, "")
        qty_or_type = val(2105, val(1903, val(2004, "")))
        if item_hash in ("", 0, 0x887AE0B0) and index_or_serial in ("", 0) and qty_or_type in ("", 0):
            continue
        name, gbid, hx, aliases = _entry_parts(db, item_hash)
        row = {"unit": unit_id, "name": name, "gbid": gbid, "hash": hx, "index_or_serial": index_or_serial, "flag": flag, "qty_or_type": qty_or_type, "aliases": aliases}
        if args.filter and args.filter.lower() not in " ".join(str(v).lower() for v in row.values()):
            continue
        rows.append(row)
    if args.csv:
        import csv
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            fields = ["unit", "name", "gbid", "hash", "index_or_serial", "flag", "qty_or_type", "aliases"]
            writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        print(f"Wrote items CSV to {args.csv}")
    else:
        for r in rows[:args.limit]:
            print(f"unit={r['unit']:<7} {r['hash']:<10} {r['gbid']:<16} {r['name']:<45} qty/type={r['qty_or_type']} flag={r['flag']}")
        if len(rows) > args.limit:
            print(f"... +{len(rows)-args.limit} more")
    return 0


def cmd_sigils(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    grouped = save.group_by_unit([2702, 2703, 2704, 2706, 2707])
    rows = []
    for unit_id, fields in sorted(grouped.items()):
        def val(id_type, default=""):
            rec = fields.get(id_type)
            return save.get_values(rec, 1)[0] if rec and rec.value_count else default
        gem = val(2703, 0)
        if gem in ("", 0, 0x887AE0B0):
            continue
        name, gbid, hx, aliases = _entry_parts(db, gem)
        worn_name, worn_gbid, worn_hash, _ = _entry_parts(db, val(2706, 0))
        row = {"unit": unit_id, "slot": val(2702, ""), "name": name, "gbid": gbid, "hash": hx, "skill_level": val(2704, ""), "worn_by": worn_name, "worn_gbid": worn_gbid, "flags": val(2707, ""), "aliases": aliases}
        if args.filter and args.filter.lower() not in " ".join(str(v).lower() for v in row.values()):
            continue
        rows.append(row)
    if args.csv:
        import csv
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            fields = ["unit", "slot", "name", "gbid", "hash", "skill_level", "worn_by", "worn_gbid", "flags", "aliases"]
            writer = csv.DictWriter(f, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
        print(f"Wrote sigils CSV to {args.csv}")
    else:
        for r in rows[:args.limit]:
            print(f"unit={r['unit']:<7} {r['hash']:<10} {r['gbid']:<16} {r['name']:<45} lv={r['skill_level']} flags={r['flags']}")
        if len(rows) > args.limit:
            print(f"... +{len(rows)-args.limit} more")
    return 0


def cmd_hash_scan(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    rows = scan_known_hashes(save, db, include_unknown=args.unknown, limit=args.limit)
    if args.csv:
        write_hash_scan_csv(rows, args.csv)
        print(f"Wrote hash scan CSV to {args.csv}")
    else:
        print(format_hash_scan_text(rows, max_rows=args.limit))
    return 0


def cmd_weapons(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    grouped = save.group_by_unit([2803, 2804, 2805, 2806, 2807, 2814, 2815, 2816])
    rows = []
    for unit_id, fields in sorted(grouped.items()):
        def val(id_type, default=""):
            rec = fields.get(id_type)
            return save.get_values(rec, 1)[0] if rec and rec.value_count else default
        wid = val(2803, 0)
        if wid in ("", 0, 0x887AE0B0):
            continue
        entry = db.lookup_hash(int(wid))
        stone = val(2816, 0)
        stone_entry = db.lookup_hash(int(stone)) if stone not in ("", 0, 0x887AE0B0) else None
        rows.append({
            "unit": unit_id,
            "name": entry.display_name if entry else f"Unknown 0x{int(wid)&0xFFFFFFFF:08X}",
            "gbid": entry.item_id if entry else "",
            "hash": f"{int(wid)&0xFFFFFFFF:08X}",
            "xp": val(2804, ""),
            "2805": val(2805, ""),
            "2806": val(2806, ""),
            "2807": val(2807, ""),
            "2814": val(2814, ""),
            "flags": val(2815, ""),
            "stone": stone_entry.display_name if stone_entry else (f"0x{int(stone)&0xFFFFFFFF:08X}" if stone else ""),
            "aliases": entry.alias_text if entry else "",
        })
    if args.filter:
        q = args.filter.lower()
        rows = [r for r in rows if q in " ".join(str(v).lower() for v in r.values())]
    if args.csv:
        import csv
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["unit","name","gbid","hash","xp","2805","2806","2807","2814","flags","stone","aliases"])
            writer.writeheader(); writer.writerows(rows)
        print(f"Wrote weapons CSV to {args.csv}")
    else:
        for r in rows[:args.limit]:
            print(f"unit={r['unit']:<6} {r['hash']} {r['gbid']:<16} {r['name']:<42} xp={r['xp']} flags={r['flags']} stone={r['stone']}")
        if len(rows) > args.limit:
            print(f"... +{len(rows)-args.limit} more")
    return 0



def _resolve_hash_arg(db: ItemDatabase, text: str) -> int:
    q = str(text).strip()
    entry = db.by_id.get(q.upper())
    if entry:
        return entry.hash_value & 0xFFFFFFFF
    clean = q.removeprefix("0x").removeprefix("0X")
    try:
        if q.lower().startswith("0x"):
            return int(q, 16) & 0xFFFFFFFF
        if clean and all(c in "0123456789abcdefABCDEF" for c in clean) and not clean.isdecimal():
            return int(clean, 16) & 0xFFFFFFFF
        if q.isdecimal():
            return int(q, 10) & 0xFFFFFFFF
    except Exception:
        pass
    hits = db.search(q, limit=5)
    if hits:
        return hits[0].hash_value & 0xFFFFFFFF
    if q.upper() == q and any(ch == "_" for ch in q) and all(ch.isalnum() or ch == "_" for ch in q):
        return gbfr_hash(q) & 0xFFFFFFFF
    raise SystemExit(f"Could not resolve GBID/name/hash: {text}")




def _resolve_known_preset_hash(db: ItemDatabase, text: str, expected: str) -> int:
    """Resolve a preset row, but require a named DB hit and category match.

    expected is one of: item, sigil, weapon. This intentionally rejects
    generated fallback hashes so curated presets cannot write Unknown rows.
    """
    h = _resolve_hash_arg(db, text)
    hit = db.lookup_hash(h)
    if hit is None:
        raise SystemExit(f"Preset row resolved only as unknown hash, skipped for safety: {text} -> 0x{h:08X}")
    if expected == "sigil" and hit.category != "Sigil / Gem":
        raise SystemExit(f"Preset sigil category mismatch: {text} -> {hit.display_name} ({hit.category})")
    if expected == "weapon" and hit.category != "Weapon":
        raise SystemExit(f"Preset weapon category mismatch: {text} -> {hit.display_name} ({hit.category})")
    if expected == "item" and hit.category in {"Sigil / Gem", "Weapon", "Character", "Trait / Skill"}:
        raise SystemExit(f"Preset item category mismatch: {text} -> {hit.display_name} ({hit.category})")
    return h & 0xFFFFFFFF

def _set_first(save: GBFRSaveData, rec, value, label: str) -> bool:
    if rec is None:
        print(f"No {label} field exists for that unit; skipped.")
        return False
    values = save.get_values(rec)
    if not values:
        print(f"{label} field exists but has no values; skipped.")
        return False
    values[0] = value
    save.set_values(rec, values)
    print(f"Set {label}: {value}")
    return True


def _write_edited(save: GBFRSaveData, args: argparse.Namespace) -> int:
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".edited"))
    save.save_as(out, update_hash=True)
    print(f"Wrote {out}")
    return 0


def cmd_edit_item(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    changed = False
    if args.hash is not None:
        rec = (save.find(id_type=2102, unit_id=args.unit) or save.find(id_type=1901, unit_id=args.unit) or save.find(id_type=2002, unit_id=args.unit) or [None])[0]
        changed |= _set_first(save, rec, _resolve_hash_arg(db, args.hash), "item hash")
    if args.quantity is not None:
        rec = (save.find(id_type=2105, unit_id=args.unit) or save.find(id_type=1903, unit_id=args.unit) or save.find(id_type=2004, unit_id=args.unit) or [None])[0]
        changed |= _set_first(save, rec, args.quantity, "item quantity/value")
    if args.flag is not None:
        rec = (save.find(id_type=2104, unit_id=args.unit) or save.find(id_type=1904, unit_id=args.unit) or [None])[0]
        changed |= _set_first(save, rec, args.flag, "item flag")
    if not changed:
        raise SystemExit("No item edits were applied.")
    return _write_edited(save, args)


def _first_empty_item_slot(save: GBFRSaveData):
    grouped = save.group_by_unit([2102, 2103, 2104, 2105, 1901, 1902, 1903, 1904, 2002, 2003, 2004])
    families = [
        ("210x ItemManager slot", 2102, 2103, 2104, 2105),
        ("190x ItemManager bucket slot", 1901, 1902, 1904, 1903),
        ("200x ItemManager bucket slot", 2002, 2003, None, 2004),
    ]
    def first_value(rec, default=0):
        if rec is None or rec.value_count < 1:
            return default
        return int(save.get_values(rec, 1)[0])
    for label, hash_id, index_id, flag_id, qty_id in families:
        for unit_id, fields in sorted(grouped.items()):
            h = fields.get(hash_id)
            q = fields.get(qty_id) if qty_id else None
            if not h or not q:
                continue
            if first_value(h) in (0, 0x887AE0B0) and first_value(q) == 0:
                return {
                    "label": label,
                    "unit": unit_id,
                    "hash_rec": h,
                    "qty_rec": q,
                    "index_rec": fields.get(index_id) if index_id else None,
                    "flag_rec": fields.get(flag_id) if flag_id else None,
                }
    return None


def cmd_add_item(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    slot = _first_empty_item_slot(save)
    if not slot:
        raise SystemExit("No empty ItemManager slot found. This editor does not insert new FlatBuffer records yet.")
    item_hash = _resolve_hash_arg(db, args.item)
    _set_first(save, slot["hash_rec"], item_hash, f"item hash in {slot['label']} unit {slot['unit']}")
    _set_first(save, slot["qty_rec"], args.quantity, "item quantity/value")
    if args.flag is not None:
        _set_first(save, slot.get("flag_rec"), args.flag, "item flag")
    elif slot.get("flag_rec") is not None:
        cur = int(save.get_values(slot["flag_rec"], 1)[0])
        if cur == 0:
            _set_first(save, slot.get("flag_rec"), 1, "item flag")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".added-item"))
    save.save_as(out, update_hash=True)
    hit = db.lookup_hash(item_hash)
    name = f"{hit.display_name} ({hit.item_id})" if hit else f"0x{item_hash:08X}"
    print(f"Added {name} x{args.quantity} to empty {slot['label']} unit {slot['unit']} and wrote {out}")
    return 0


def _first_empty_sigil_slot(save: GBFRSaveData):
    grouped = save.group_by_unit([2702, 2703, 2704, 2706, 2707])
    def first_value(rec, default=0):
        if rec is None or rec.value_count < 1:
            return default
        return int(save.get_values(rec, 1)[0])
    for unit_id, fields in sorted(grouped.items()):
        h = fields.get(2703)
        lvl = fields.get(2704)
        if h and lvl and first_value(h) in (0, 0x887AE0B0) and first_value(lvl) == 0:
            return {
                "unit": unit_id,
                "hash_rec": h,
                "level_rec": lvl,
                "worn_rec": fields.get(2706),
                "flags_rec": fields.get(2707),
            }
    return None


def cmd_add_sigil(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    slot = _first_empty_sigil_slot(save)
    if not slot:
        raise SystemExit("No empty Gem/Sigil slot found. This editor does not insert new FlatBuffer records yet.")
    sigil_hash = _resolve_hash_arg(db, args.sigil)
    _set_first(save, slot["hash_rec"], sigil_hash, f"sigil hash in unit {slot['unit']}")
    _set_first(save, slot["level_rec"], args.level, "sigil level")
    if slot.get("worn_rec") is not None:
        cur = int(save.get_values(slot["worn_rec"], 1)[0])
        if cur == 0:
            _set_first(save, slot.get("worn_rec"), 0x887AE0B0, "sigil worn-by hash")
    if args.lock or args.unlock:
        rec = slot.get("flags_rec")
        cur = int(save.get_values(rec, 1)[0]) if rec else 0
        _set_first(save, rec, (cur | 1) if args.lock else (cur & ~1), "sigil flags")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".added-sigil"))
    save.save_as(out, update_hash=True)
    hit = db.lookup_hash(sigil_hash)
    name = f"{hit.display_name} ({hit.item_id})" if hit else f"0x{sigil_hash:08X}"
    print(f"Added {name} level {args.level} to empty sigil unit {slot['unit']} and wrote {out}")
    return 0


def _first_empty_weapon_slot(save: GBFRSaveData):
    grouped = save.group_by_unit([2803, 2804, 2815, 2816])
    def first_value(rec, default=0):
        if rec is None or rec.value_count < 1:
            return default
        return int(save.get_values(rec, 1)[0])
    for unit_id, fields in sorted(grouped.items()):
        h = fields.get(2803)
        xp = fields.get(2804)
        if h and xp and first_value(h) in (0, 0x887AE0B0) and first_value(xp) == 0:
            return {
                "unit": unit_id,
                "hash_rec": h,
                "xp_rec": xp,
                "flags_rec": fields.get(2815),
                "stone_rec": fields.get(2816),
            }
    return None


def cmd_add_weapon(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    slot = _first_empty_weapon_slot(save)
    if not slot:
        raise SystemExit("No empty WeaponManager slot found. This editor does not insert new FlatBuffer records yet.")
    weapon_hash = _resolve_hash_arg(db, args.weapon)
    _set_first(save, slot["hash_rec"], weapon_hash, f"weapon hash in unit {slot['unit']}")
    _set_first(save, slot["xp_rec"], args.xp, "weapon XP/progress")
    if args.flags is not None:
        _set_first(save, slot.get("flags_rec"), args.flags, "weapon flags")
    elif slot.get("flags_rec") is not None:
        cur = int(save.get_values(slot["flags_rec"], 1)[0])
        if cur == 0:
            _set_first(save, slot.get("flags_rec"), 1, "weapon flags")
    if slot.get("stone_rec") is not None:
        cur = int(save.get_values(slot["stone_rec"], 1)[0])
        if cur == 0:
            _set_first(save, slot.get("stone_rec"), 0x887AE0B0, "weapon stone hash")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".added-weapon"))
    save.save_as(out, update_hash=True)
    hit = db.lookup_hash(weapon_hash)
    name = f"{hit.display_name} ({hit.item_id})" if hit else f"0x{weapon_hash:08X}"
    print(f"Added {name} to empty weapon unit {slot['unit']} and wrote {out}")
    return 0


def _parse_item_batch_file(path: str) -> list[tuple[str, int]]:
    import re
    rows: list[tuple[str, int]] = []
    for line_no, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.replace("\t", ",")
        m = None
        for pat in [r"^(.+?)\s*[,=]\s*(-?\d+)\s*$", r"^(.+?)\s+[xX]\s*(-?\d+)\s*$", r"^(.+?)\s+x(-?\d+)\s*$"]:
            m = re.match(pat, line)
            if m:
                break
        if m:
            key, qty = m.group(1).strip(), int(m.group(2))
        else:
            key, qty = line, 1
        if qty < 0:
            raise SystemExit(f"Line {line_no}: negative quantities are not allowed")
        rows.append((key, qty))
    return rows

def cmd_batch_add_items(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    rows = _parse_item_batch_file(args.list_file)
    if not rows:
        raise SystemExit("No item rows found in list file.")
    added = 0
    for key, qty in rows:
        slot = _first_empty_item_slot(save)
        if not slot:
            raise SystemExit(f"No empty ItemManager slot found after adding {added} rows. Wrote nothing; rerun with fewer items or inspect empty slots.")
        item_hash = _resolve_hash_arg(db, key)
        _set_first(save, slot["hash_rec"], item_hash, f"item hash in {slot['label']} unit {slot['unit']}")
        _set_first(save, slot["qty_rec"], qty, "item quantity/value")
        if slot.get("flag_rec") is not None and int(save.get_values(slot["flag_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("flag_rec"), 1, "item flag")
        hit = db.lookup_hash(item_hash)
        name = f"{hit.display_name} ({hit.item_id})" if hit else f"0x{item_hash:08X}"
        print(f"Added {name} x{qty} to unit {slot['unit']}")
        added += 1
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".batch-added-items"))
    save.save_as(out, update_hash=True)
    print(f"Wrote {out}")
    return 0


def _parse_sigil_batch_file(path: str) -> list[tuple[str, int, bool]]:
    import re
    rows: list[tuple[str, int, bool]] = []
    for line_no, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
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
            raise SystemExit(f"Line {line_no}: missing sigil name/GBID")
        if level < 0:
            raise SystemExit(f"Line {line_no}: negative levels are not allowed")
        rows.append((clean, level, locked))
    return rows


def cmd_batch_add_sigils(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    rows = _parse_sigil_batch_file(args.list_file)
    if not rows:
        raise SystemExit("No sigil rows found in list file.")
    added = 0
    for key, level, locked in rows:
        slot = _first_empty_sigil_slot(save)
        if not slot:
            raise SystemExit(f"No empty Gem/Sigil slot found after adding {added} rows. Wrote nothing; rerun with fewer sigils or inspect empty slots.")
        sigil_hash = _resolve_hash_arg(db, key)
        _set_first(save, slot["hash_rec"], sigil_hash, f"sigil hash in unit {slot['unit']}")
        _set_first(save, slot["level_rec"], level, "sigil level")
        if slot.get("worn_rec") is not None and int(save.get_values(slot["worn_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("worn_rec"), 0x887AE0B0, "sigil worn-by hash")
        rec = slot.get("flags_rec")
        if rec is not None:
            cur = int(save.get_values(rec, 1)[0])
            _set_first(save, rec, (cur | 1) if locked else (cur & ~1), "sigil flags")
        hit = db.lookup_hash(sigil_hash)
        name = f"{hit.display_name} ({hit.item_id})" if hit else f"0x{sigil_hash:08X}"
        print(f"Added {name} Lv {level} {'locked' if locked else 'unlocked'} to unit {slot['unit']}")
        added += 1
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".batch-added-sigils"))
    save.save_as(out, update_hash=True)
    print(f"Wrote {out}")
    return 0


def _parse_weapon_batch_file(path: str) -> list[tuple[str, int]]:
    import re
    rows: list[tuple[str, int]] = []
    for line_no, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
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
            raise SystemExit(f"Line {line_no}: missing weapon name/GBID")
        if xp < 0:
            raise SystemExit(f"Line {line_no}: negative XP is not allowed")
        rows.append((line, xp))
    return rows


def cmd_batch_add_weapons(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    rows = _parse_weapon_batch_file(args.list_file)
    if not rows:
        raise SystemExit("No weapon rows found in list file.")
    added = 0
    for key, xp in rows:
        slot = _first_empty_weapon_slot(save)
        if not slot:
            raise SystemExit(f"No empty WeaponManager slot found after adding {added} rows. Wrote nothing; rerun with fewer weapons or inspect empty slots.")
        weapon_hash = _resolve_hash_arg(db, key)
        _set_first(save, slot["hash_rec"], weapon_hash, f"weapon hash in unit {slot['unit']}")
        _set_first(save, slot["xp_rec"], xp, "weapon XP/progress")
        if slot.get("flags_rec") is not None and int(save.get_values(slot["flags_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("flags_rec"), 1, "weapon flags")
        if slot.get("stone_rec") is not None and int(save.get_values(slot["stone_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("stone_rec"), 0x887AE0B0, "weapon stone hash")
        hit = db.lookup_hash(weapon_hash)
        name = f"{hit.display_name} ({hit.item_id})" if hit else f"0x{weapon_hash:08X}"
        print(f"Added {name} XP {xp} to unit {slot['unit']}")
        added += 1
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".batch-added-weapons"))
    save.save_as(out, update_hash=True)
    print(f"Wrote {out}")
    return 0

def cmd_edit_sigil(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    changed = False
    if args.hash is not None:
        changed |= _set_first(save, (save.find(id_type=2703, unit_id=args.unit) or [None])[0], _resolve_hash_arg(db, args.hash), "sigil hash")
    if args.level is not None:
        changed |= _set_first(save, (save.find(id_type=2704, unit_id=args.unit) or [None])[0], args.level, "sigil level")
    if args.worn_by is not None:
        changed |= _set_first(save, (save.find(id_type=2706, unit_id=args.unit) or [None])[0], _resolve_hash_arg(db, args.worn_by), "sigil worn-by hash")
    if args.lock or args.unlock:
        rec = (save.find(id_type=2707, unit_id=args.unit) or [None])[0]
        cur = int(save.get_values(rec, 1)[0]) if rec else 0
        value = (cur | 1) if args.lock else (cur & ~1)
        changed |= _set_first(save, rec, value, "sigil flags")
    if not changed:
        raise SystemExit("No sigil edits were applied.")
    return _write_edited(save, args)


def cmd_edit_weapon(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    changed = False
    if args.hash is not None:
        changed |= _set_first(save, (save.find(id_type=2803, unit_id=args.unit) or [None])[0], _resolve_hash_arg(db, args.hash), "weapon hash")
    if args.xp is not None:
        changed |= _set_first(save, (save.find(id_type=2804, unit_id=args.unit) or [None])[0], args.xp, "weapon XP")
    if args.flags is not None:
        changed |= _set_first(save, (save.find(id_type=2815, unit_id=args.unit) or [None])[0], args.flags, "weapon flags")
    if args.stone is not None:
        changed |= _set_first(save, (save.find(id_type=2816, unit_id=args.unit) or [None])[0], _resolve_hash_arg(db, args.stone), "weapon stone hash")
    if args.clear_stone:
        changed |= _set_first(save, (save.find(id_type=2816, unit_id=args.unit) or [None])[0], 0x887AE0B0, "weapon stone hash")
    if not changed:
        raise SystemExit("No weapon edits were applied.")
    return _write_edited(save, args)


def cmd_presets(args: argparse.Namespace) -> int:
    rows = search_preset_packs(args.query or "")
    if args.csv:
        import csv
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["key", "name", "category", "rows", "items", "sigils", "weapons", "description", "notes"])
            for p in rows:
                writer.writerow([p.key, p.name, p.category, p.total_rows, len(p.items), len(p.sigils), len(p.weapons), p.description, p.notes])
        print(f"Wrote {len(rows)} preset rows to {args.csv}")
        return 0
    for p in rows[:args.limit]:
        print(f"{p.key:<30} {p.category:<10} rows={p.total_rows:<3} items={len(p.items):<2} sigils={len(p.sigils):<2} weapons={len(p.weapons):<2}  {p.name}")
        if args.verbose:
            print(p.to_batch_text())
            print()
    if len(rows) > args.limit:
        print(f"... +{len(rows)-args.limit} more")
    return 0


def cmd_apply_preset(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    pack = get_preset_pack(args.preset)
    print(f"Applying preset: {pack.name} ({pack.key})")
    added = 0
    unresolved: list[str] = []
    for key, qty in pack.items:
        try:
            item_hash = _resolve_known_preset_hash(db, key, "item")
        except SystemExit as exc:
            unresolved.append(f"item:{key} ({exc})")
            continue
        slot = _first_empty_item_slot(save)
        if not slot:
            raise SystemExit(f"No empty ItemManager slot found after adding {added} rows. Wrote nothing; rerun with fewer rows or inspect empty slots.")
        _set_first(save, slot["hash_rec"], item_hash, f"item hash in {slot['label']} unit {slot['unit']}")
        _set_first(save, slot["qty_rec"], int(qty), "item quantity/value")
        if slot.get("flag_rec") is not None and int(save.get_values(slot["flag_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("flag_rec"), 1, "item flag")
        hit = db.lookup_hash(item_hash)
        print(f"Added item {(hit.display_name + ' (' + hit.item_id + ')') if hit else key} x{qty} to unit {slot['unit']}")
        added += 1
    for key, level, locked in pack.sigils:
        try:
            sigil_hash = _resolve_known_preset_hash(db, key, "sigil")
        except SystemExit as exc:
            unresolved.append(f"sigil:{key} ({exc})")
            continue
        slot = _first_empty_sigil_slot(save)
        if not slot:
            raise SystemExit(f"No empty Gem/Sigil slot found after adding {added} rows. Wrote nothing; rerun with fewer rows or inspect empty slots.")
        _set_first(save, slot["hash_rec"], sigil_hash, f"sigil hash in unit {slot['unit']}")
        _set_first(save, slot["level_rec"], int(level), "sigil level")
        if slot.get("worn_rec") is not None and int(save.get_values(slot["worn_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("worn_rec"), 0x887AE0B0, "sigil worn-by hash")
        rec = slot.get("flags_rec")
        if rec is not None:
            cur = int(save.get_values(rec, 1)[0])
            _set_first(save, rec, (cur | 1) if locked else (cur & ~1), "sigil flags")
        hit = db.lookup_hash(sigil_hash)
        print(f"Added sigil {(hit.display_name + ' (' + hit.item_id + ')') if hit else key} Lv {level} {'locked' if locked else 'unlocked'} to unit {slot['unit']}")
        added += 1
    for key, xp in pack.weapons:
        try:
            weapon_hash = _resolve_known_preset_hash(db, key, "weapon")
        except SystemExit as exc:
            unresolved.append(f"weapon:{key} ({exc})")
            continue
        slot = _first_empty_weapon_slot(save)
        if not slot:
            raise SystemExit(f"No empty WeaponManager slot found after adding {added} rows. Wrote nothing; rerun with fewer rows or inspect empty slots.")
        _set_first(save, slot["hash_rec"], weapon_hash, f"weapon hash in unit {slot['unit']}")
        _set_first(save, slot["xp_rec"], int(xp), "weapon XP/progress")
        if slot.get("flags_rec") is not None and int(save.get_values(slot["flags_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("flags_rec"), 1, "weapon flags")
        if slot.get("stone_rec") is not None and int(save.get_values(slot["stone_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("stone_rec"), 0x887AE0B0, "weapon stone hash")
        hit = db.lookup_hash(weapon_hash)
        print(f"Added weapon {(hit.display_name + ' (' + hit.item_id + ')') if hit else key} XP {xp} to unit {slot['unit']}")
        added += 1
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + f".{pack.key}.preset"))
    save.save_as(out, update_hash=True)
    print(f"Wrote {out}")
    if unresolved:
        print("Skipped unresolved preset rows:")
        for item in unresolved:
            print("- " + item)
    return 0




def cmd_cheat_list(args: argparse.Namespace) -> int:
    rows = [p for p in search_preset_packs(args.query or "") if p.category.lower() == "cheats"]
    for p in rows[:args.limit]:
        print(f"{p.key:<34} rows={p.total_rows:<3} items={len(p.items):<2} sigils={len(p.sigils):<2} weapons={len(p.weapons):<2}  {p.name}")
        if args.verbose:
            print(p.to_batch_text())
            print()
    if len(rows) > args.limit:
        print(f"... +{len(rows)-args.limit} more")
    return 0


def _cli_first_value(save: GBFRSaveData, rec, default=0):
    if rec is None or rec.value_count < 1:
        return default
    try:
        return int(save.get_values(rec, 1)[0])
    except Exception:
        return default


def cmd_cheat_max_known_items(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    grouped = save.group_by_unit([2102, 2105, 1901, 1903, 2002, 2004])
    patched = 0
    for unit_id, fields in sorted(grouped.items()):
        h = _cli_first_value(save, fields.get(2102) or fields.get(1901) or fields.get(2002), 0)
        qty_rec = fields.get(2105) or fields.get(1903) or fields.get(2004)
        if not qty_rec or h in (0, 0x887AE0B0):
            continue
        entry = db.lookup_hash(h & 0xFFFFFFFF)
        if not entry or entry.category in {"Sigil / Gem", "Weapon", "Character", "Trait / Skill"}:
            continue
        name = entry.display_name
        cap = args.rupie_cap if "rupie" in name.lower() else args.mastery_cap if "mastery point" in name.lower() else args.item_cap
        _set_first(save, qty_rec, int(cap), f"{name} quantity @ unit {unit_id}")
        patched += 1
    if patched == 0:
        raise SystemExit("No known item quantity rows found to patch.")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".cheat-max-items"))
    save.save_as(out, update_hash=True)
    print(f"Patched {patched} known item/material/currency quantity fields and wrote {out}")
    return 0


def cmd_cheat_set_known_items(args: argparse.Namespace) -> int:
    args.item_cap = int(args.quantity)
    args.rupie_cap = int(args.quantity)
    args.mastery_cap = int(args.quantity)
    return cmd_cheat_max_known_items(args)


def cmd_cheat_max_sigils(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    grouped = save.group_by_unit([2703, 2704, 2707])
    patched = 0
    for unit_id, fields in sorted(grouped.items()):
        h = _cli_first_value(save, fields.get(2703), 0)
        level_rec = fields.get(2704)
        if not level_rec or h in (0, 0x887AE0B0):
            continue
        entry = db.lookup_hash(h & 0xFFFFFFFF)
        if not entry or entry.category != "Sigil / Gem":
            continue
        _set_first(save, level_rec, int(args.level), f"{entry.display_name} level @ unit {unit_id}")
        flags_rec = fields.get(2707)
        if args.lock and flags_rec is not None:
            cur = _cli_first_value(save, flags_rec, 0)
            _set_first(save, flags_rec, cur | 1, f"{entry.display_name} flags @ unit {unit_id}")
        patched += 1
    if patched == 0:
        raise SystemExit("No known sigil rows found to patch.")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".cheat-max-sigils"))
    save.save_as(out, update_hash=True)
    print(f"Patched {patched} known sigil level fields and wrote {out}")
    return 0


def cmd_cheat_max_weapons(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    grouped = save.group_by_unit([2803, 2804, 2815])
    patched = 0
    for unit_id, fields in sorted(grouped.items()):
        h = _cli_first_value(save, fields.get(2803), 0)
        xp_rec = fields.get(2804)
        if not xp_rec or h in (0, 0x887AE0B0):
            continue
        entry = db.lookup_hash(h & 0xFFFFFFFF)
        if not entry or entry.category != "Weapon":
            continue
        _set_first(save, xp_rec, int(args.xp), f"{entry.display_name} XP @ unit {unit_id}")
        flags_rec = fields.get(2815)
        if args.flag and flags_rec is not None:
            cur = _cli_first_value(save, flags_rec, 0)
            _set_first(save, flags_rec, cur | 1, f"{entry.display_name} flags @ unit {unit_id}")
        patched += 1
    if patched == 0:
        raise SystemExit("No known weapon rows found to patch.")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".cheat-max-weapons"))
    save.save_as(out, update_hash=True)
    print(f"Patched {patched} known weapon XP/progress fields and wrote {out}")
    return 0


def _existing_hashes_for_cli(save: GBFRSaveData, field_ids: list[int]) -> set[int]:
    hashes: set[int] = set()
    grouped = save.group_by_unit(field_ids)
    for fields in grouped.values():
        for fid in field_ids:
            value = _cli_first_value(save, fields.get(fid), 0)
            if value not in (0, 0x887AE0B0):
                hashes.add(value & 0xFFFFFFFF)
    return hashes


def _known_v_sigil_entries_cli(db: ItemDatabase):
    rows = []
    for entry in db.by_hash.values():
        item_id = entry.item_id.upper()
        if entry.category != "Sigil / Gem" or not item_id.startswith("GEEN_"):
            continue
        if item_id.endswith("_04") or item_id.endswith("_14") or entry.display_name.endswith(" V") or entry.display_name.endswith(" V+"):
            rows.append(entry)
    return sorted(rows, key=lambda e: (e.item_id, e.display_name))


def _known_material_entries_cli(db: ItemDatabase):
    blocked = {"Sigil / Gem", "Weapon", "Character", "Trait / Skill", "Other"}
    rows = []
    allowed_categories = {"Material", "Currency", "Consumable", "Glitterstone", "Wrightstone", "Ticket"}
    for entry in db.by_hash.values():
        if entry.category in blocked:
            continue
        name_low = entry.display_name.lower()
        if name_low.startswith("unnamed / reserved") or name_low.startswith("reserved /"):
            continue
        if entry.category in allowed_categories:
            rows.append(entry)
    return sorted(rows, key=lambda e: (e.category, e.item_id, e.display_name))


def _material_cheat_quantity_cli(name: str) -> int:
    low = name.lower()
    if "rupie" in low:
        return 99_999_999
    if "mastery point" in low:
        return 9_999_999
    if "damascus" in low or "ambrosia" in low:
        return 99
    return 999


def cmd_cheat_add_all_v_sigils(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    existing = _existing_hashes_for_cli(save, [2703])
    entries = [e for e in _known_v_sigil_entries_cli(db) if (e.hash_value & 0xFFFFFFFF) not in existing]
    added = 0
    for entry in entries:
        slot = _first_empty_sigil_slot(save)
        if not slot:
            break
        _set_first(save, slot["hash_rec"], entry.hash_value & 0xFFFFFFFF, f"{entry.display_name} hash")
        _set_first(save, slot["level_rec"], int(args.level), f"{entry.display_name} level")
        if slot.get("worn_rec") is not None and int(save.get_values(slot["worn_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("worn_rec"), 0x887AE0B0, "sigil worn-by hash")
        rec = slot.get("flags_rec")
        if rec is not None:
            cur = int(save.get_values(rec, 1)[0])
            _set_first(save, rec, (cur | 1) if args.lock else (cur & ~1), "sigil flags")
        print(f"Added {entry.display_name} ({entry.item_id}) Lv {args.level} to unit {slot['unit']}")
        added += 1
    if added == 0:
        raise SystemExit("No missing known V/V+ sigils could be added. Check empty slots or database coverage.")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".cheat-add-v-sigils"))
    save.save_as(out, update_hash=True)
    print(f"Added {added} known V/V+ sigils and wrote {out}")
    if added < len(entries):
        print(f"Skipped {len(entries)-added} rows because no more empty sigil slots were available.")
    return 0


def cmd_cheat_add_all_materials(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    existing = _existing_hashes_for_cli(save, [2102, 1901, 2002])
    entries = [e for e in _known_material_entries_cli(db) if (e.hash_value & 0xFFFFFFFF) not in existing]
    added = 0
    for entry in entries:
        slot = _first_empty_item_slot(save)
        if not slot:
            break
        qty = _material_cheat_quantity_cli(entry.display_name)
        _set_first(save, slot["hash_rec"], entry.hash_value & 0xFFFFFFFF, f"{entry.display_name} hash")
        _set_first(save, slot["qty_rec"], qty, f"{entry.display_name} quantity")
        if slot.get("flag_rec") is not None and int(save.get_values(slot["flag_rec"], 1)[0]) == 0:
            _set_first(save, slot.get("flag_rec"), 1, "item flag")
        print(f"Added {entry.display_name} ({entry.item_id}) x{qty:,} to unit {slot['unit']}")
        added += 1
    if added == 0:
        raise SystemExit("No missing known materials/currency could be added. Check empty slots or database coverage.")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".cheat-add-materials"))
    save.save_as(out, update_hash=True)
    print(f"Added {added} known material/currency rows and wrote {out}")
    if added < len(entries):
        print(f"Skipped {len(entries)-added} rows because no more empty item slots were available.")
    return 0


def cmd_save_wizard_cheats(args: argparse.Namespace) -> int:
    rows = list_builtin_save_wizard_cheats(args.query or "")
    imported: list[SaveWizardCheat] = []
    if args.load_sheet:
        text = load_sheet_csv(args.url or SAVE_WIZARD_SHEET_URL, timeout=args.timeout)
        imported = parse_sheet_cheats(text, source=args.url or SAVE_WIZARD_SHEET_URL)
        q = (args.query or "").strip().lower()
        if q:
            toks = q.split()
            imported = [r for r in imported if all(t in " ".join([r.key, r.name, r.category, r.description]).lower() for t in toks)]
    all_rows = rows + imported
    if args.csv:
        import csv
        with open(args.csv, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["key", "name", "category", "action", "target", "safe", "description", "source"])
            for c in all_rows:
                writer.writerow([c.key, c.name, c.category, c.action, c.target, c.safe, c.description, c.source])
        print(f"Wrote {len(all_rows)} Save Wizard cheat rows to {args.csv}")
        return 0
    for c in all_rows[:args.limit]:
        safe = "safe" if c.safe else "reference"
        print(f"{c.key:<32} {c.category:<12} {safe:<9} {c.display_action():<42} {c.name}")
        if args.verbose:
            print(f"  {c.description}")
    if len(all_rows) > args.limit:
        print(f"... +{len(all_rows)-args.limit} more")
    return 0


def cmd_apply_save_wizard_cheat(args: argparse.Namespace) -> int:
    cheat = get_builtin_save_wizard_cheat(args.cheat)
    if cheat.action == "preset":
        args.preset = cheat.target
        return cmd_apply_preset(args)
    if cheat.action == "max_items":
        args.item_cap = args.item_cap
        args.rupie_cap = args.rupie_cap
        args.mastery_cap = args.mastery_cap
        return cmd_cheat_max_known_items(args)
    if cheat.action == "max_sigils":
        args.level = args.level
        args.lock = True
        return cmd_cheat_max_sigils(args)
    if cheat.action == "max_weapons":
        args.xp = args.xp
        args.flag = True
        return cmd_cheat_max_weapons(args)
    if cheat.action == "max_characters":
        args.level = 100
        return cmd_cheat_max_characters(args)
    if cheat.action == "complete_quests":
        return cmd_cheat_complete_quests(args)
    if cheat.action == "unlock_titles":
        return cmd_cheat_unlock_titles(args)
    if cheat.action == "add_all_known_v_sigils":
        if not hasattr(args, "lock"):
            args.lock = True
        return cmd_cheat_add_all_v_sigils(args)
    if cheat.action == "add_all_known_materials":
        return cmd_cheat_add_all_materials(args)
    raise SystemExit(f"Save Wizard cheat is not directly appliable yet: {cheat.key} ({cheat.action})")


CHARACTER_FIELD_IDS_CLI = [1301, 1302, 1303, 1304, 1305, 1307, 1308, 1309, 1310, 1311, 1312, 1313, 1314, 1315, 1316, 1317, 1318, 1321, 1322, 1402, 1403, 1404, 1501, 1502, 1503, 1601, 1602, 1603, 1604, 1605, 1606, 1607]
ITEM_SWAP_FIELD_IDS_CLI = [2102, 2103, 2104, 2105, 1901, 1902, 1903, 1904, 2002, 2003, 2004]
SIGIL_SWAP_FIELD_IDS_CLI = [2702, 2703, 2704, 2706, 2707]
WEAPON_SWAP_FIELD_IDS_CLI = [2803, 2804, 2805, 2806, 2807, 2814, 2815, 2816]


def _records_for_unit(save: GBFRSaveData, unit_id: int, field_ids: list[int]) -> dict[int, object]:
    out = {}
    for fid in field_ids:
        found = save.find(id_type=fid, unit_id=unit_id)
        if found:
            out[fid] = found[0]
    return out


def _record_values_snapshot(save: GBFRSaveData, recs: dict[int, object]) -> dict[int, list]:
    snap = {}
    for fid, rec in recs.items():
        try:
            snap[fid] = list(save.get_values(rec))
        except Exception:
            pass
    return snap


def _patch_record_values(save: GBFRSaveData, recs: dict[int, object], values: dict[int, list], label: str = "slot") -> int:
    patched = 0
    for fid, vals in values.items():
        rec = recs.get(fid)
        if rec is None:
            continue
        cur = save.get_values(rec)
        if len(cur) != len(vals):
            print(f"Skipped {label} field {fid}: vector length mismatch {len(cur)} != {len(vals)}")
            continue
        save.set_values(rec, vals)
        patched += 1
    return patched


def _swap_unit_fields(save: GBFRSaveData, unit_a: int, unit_b: int, field_ids: list[int], label: str) -> int:
    recs_a = _records_for_unit(save, unit_a, field_ids)
    recs_b = _records_for_unit(save, unit_b, field_ids)
    snap_a = _record_values_snapshot(save, recs_a)
    snap_b = _record_values_snapshot(save, recs_b)
    common = sorted(set(snap_a) & set(snap_b))
    if not common:
        raise SystemExit(f"No shared {label} fields were found for units {unit_a} and {unit_b}.")
    patched = 0
    patched += _patch_record_values(save, recs_a, {fid: snap_b[fid] for fid in common}, label)
    patched += _patch_record_values(save, recs_b, {fid: snap_a[fid] for fid in common}, label)
    return patched


def cmd_edit_character(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    changed = False
    if args.hash is not None:
        rec = (save.find(id_type=1301, unit_id=args.unit) or [None])[0]
        changed |= _set_first(save, rec, _resolve_hash_arg(db, args.hash), "character hash")
    if args.level is not None:
        rec = (save.find(id_type=1308, unit_id=args.unit) or [None])[0]
        changed |= _set_first(save, rec, int(args.level), "character level")
    if not changed:
        raise SystemExit("No character edits were applied.")
    return _write_edited(save, args)


def cmd_cheat_max_characters(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    patched = 0
    for unit in range(10000, 10040):
        rec = (save.find(id_type=1308, unit_id=unit) or [None])[0]
        hrec = (save.find(id_type=1301, unit_id=unit) or [None])[0]
        if rec is None or hrec is None:
            continue
        h = _cli_first_value(save, hrec, 0)
        if h in (0, 0x887AE0B0):
            continue
        _set_first(save, rec, int(args.level), f"character level @ unit {unit}")
        patched += 1
    if patched == 0:
        raise SystemExit("No character level rows found to patch.")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".cheat-max-characters"))
    save.save_as(out, update_hash=True)
    print(f"Patched {patched} character level fields and wrote {out}")
    return 0



def cmd_edit_overmastery(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(args.item_csv)
    if args.clear:
        result = clear_character_overmastery_hashes(save, args.unit)
    else:
        if len(args.value or []) != 4:
            raise SystemExit("edit-overmastery needs exactly four --value entries, or use --clear.")
        hashes = [_resolve_hash_arg(db, text) for text in args.value]
        result = set_character_overmastery_hashes(save, args.unit, hashes)
    if not result.changed_values:
        raise SystemExit(result.note or "No overmastery values changed.")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".edited-overmastery"))
    save.save_as(out, update_hash=True)
    print(f"{patch_summary([result])} Wrote {out}")
    return 0


def cmd_cheat_complete_quests(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    results = complete_quest_tables_splusplus(save)
    if not results:
        raise SystemExit("No quest completion candidate fields changed.")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".quests-complete"))
    save.save_as(out, update_hash=True)
    print(f"{patch_summary(results)} Wrote {out}")
    return 0


def cmd_cheat_unlock_titles(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    results = unlock_title_archive_candidates(save)
    if not results:
        raise SystemExit("No title/archive candidate fields changed.")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".titles-unlocked"))
    save.save_as(out, update_hash=True)
    print(f"{patch_summary(results)} Wrote {out}")
    return 0


def cmd_swap_character_slots(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    patched = _swap_unit_fields(save, args.unit_a, args.unit_b, CHARACTER_FIELD_IDS_CLI, "character")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".swapped-characters"))
    save.save_as(out, update_hash=True)
    print(f"Swapped {patched} character field vectors between units {args.unit_a} and {args.unit_b}; wrote {out}")
    return 0


def cmd_swap_item_slots(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    patched = _swap_unit_fields(save, args.unit_a, args.unit_b, ITEM_SWAP_FIELD_IDS_CLI, "item")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".swapped-items"))
    save.save_as(out, update_hash=True)
    print(f"Swapped {patched} item field vectors between units {args.unit_a} and {args.unit_b}; wrote {out}")
    return 0


def cmd_swap_sigil_slots(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    patched = _swap_unit_fields(save, args.unit_a, args.unit_b, SIGIL_SWAP_FIELD_IDS_CLI, "sigil")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".swapped-sigils"))
    save.save_as(out, update_hash=True)
    print(f"Swapped {patched} sigil field vectors between units {args.unit_a} and {args.unit_b}; wrote {out}")
    return 0


def cmd_swap_weapon_slots(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    patched = _swap_unit_fields(save, args.unit_a, args.unit_b, WEAPON_SWAP_FIELD_IDS_CLI, "weapon")
    out = args.output or str(Path(args.save).with_name(Path(args.save).name + ".swapped-weapons"))
    save.save_as(out, update_hash=True)
    print(f"Swapped {patched} weapon field vectors between units {args.unit_a} and {args.unit_b}; wrote {out}")
    return 0




def cmd_id_audit(args: argparse.Namespace) -> int:
    save = GBFRSaveData.open(args.save)
    db = default_item_db(getattr(args, "item_csv", None))
    resource_db = default_resource_db(getattr(args, "resource_csv", None))
    rows = build_id_audit(save, db, resource_db, include_empty=args.include_empty)
    if getattr(args, "skip_ability", False):
        rows = [r for r in rows if r.get("manager") != "Ability"]
    if args.csv:
        write_id_audit_csv(rows, args.csv, unresolved_only=args.unresolved_only)
        print(f"Wrote ID audit CSV to {args.csv}")
        return 0
    if args.summary:
        print(id_audit_summary(rows))
        return 0
    shown = rows
    if args.unresolved_only:
        shown = [r for r in rows if r.get("status") in {"unresolved", "candidate"}]
    for r in shown[: args.limit]:
        print(
            f"{str(r['manager']):<10} id={int(r['field_id']):<5} {str(r['hash']):<12} "
            f"{str(r['status']):<15} {str(r['name'])[:54]:<54} "
            f"occ={int(r['occurrences']):<4} units={str(r['units'])}"
        )
    if len(shown) > args.limit:
        print(f"... {len(shown) - args.limit} more rows")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    data = compare_saves(args.before, args.after, limit=args.limit)
    if args.json:
        write_compare_json(args.before, args.after, args.json, limit=None)
        print(f"Wrote JSON diff to {args.json}")
    if args.csv:
        write_compare_csv(args.before, args.after, args.csv)
        print(f"Wrote CSV diff to {args.csv}")
    if not args.json and not args.csv:
        print(format_compare_text(data, max_rows=args.limit))
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Granblue Fantasy Relink SaveDataBinary scanner/editor")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("info", help="print save summary")
    p.add_argument("save")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("export", help="export full JSON report")
    p.add_argument("save")
    p.add_argument("output")
    p.add_argument("--limit-values", type=int, default=16)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("list", help="list matching save units")
    p.add_argument("save")
    p.add_argument("--kind", choices=["bool", "byte", "ubyte", "short", "ushort", "int", "uint", "long", "ulong", "float"])
    p.add_argument("--id-type", type=int)
    p.add_argument("--unit-id", type=int)
    p.add_argument("--limit-values", type=int, default=8)
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve unit labels")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set", help="patch an existing scalar vector and write a new file")
    p.add_argument("save")
    p.add_argument("--kind", required=True, choices=["bool", "byte", "ubyte", "short", "ushort", "int", "uint", "long", "ulong", "float"])
    p.add_argument("--id-type", type=int, required=True)
    p.add_argument("--unit-id", type=int, default=0)
    p.add_argument("--values", required=True, help="comma-separated replacement values; count must match the record")
    p.add_argument("--output")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("search-value", help="find exact values inside a save")
    p.add_argument("save")
    p.add_argument("query", help="number/hex/string to find, e.g. 999 or 0xEE732781")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--csv", help="write CSV result")
    p.set_defaults(func=cmd_search_value)

    p = sub.add_parser("candidates", help="list useful known/research candidate save units")
    p.add_argument("save")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--item-csv", action="append", help="optional item hash CSV to resolve names")
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("gbid", help="search loaded item/sigil ID hash database")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--item-csv", action="append", help="CSV to search; defaults to packaged seed")
    p.set_defaults(func=cmd_gbid)




    p = sub.add_parser("entity-prefix", help="decode model/scripting prefixes like pl, wp, em, ph, st")
    p.add_argument("code", nargs="+", help="entity/model/stage code or path, e.g. pl0000 wp2200 em1800 ph720 st101f00")
    p.set_defaults(func=cmd_entity_prefix)

    p = sub.add_parser("hash", help="compute GBFR custom XXHash32 for one or more ID strings")
    p.add_argument("text", nargs="+", help="ID text to hash, e.g. GEEN_020_04 WEP_PL0200_01")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to report DB matches")
    p.set_defaults(func=cmd_hash)

    p = sub.add_parser("db-stats", help="show loaded GBID/resource database row counts by category")
    p.add_argument("--item-csv", action="append")
    p.add_argument("--resource-csv", action="append")
    p.set_defaults(func=cmd_db_stats)

    p = sub.add_parser("presets", help="list bundled add/edit preset packs")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--csv", help="write preset list to CSV")
    p.add_argument("--verbose", action="store_true", help="print preset batch text")
    p.set_defaults(func=cmd_presets)

    p = sub.add_parser("apply-preset", help="apply a bundled preset pack into empty item/sigil/weapon slots")
    p.add_argument("save")
    p.add_argument("preset", help="preset key or unique name fragment; use 'presets' to list")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve names")
    p.set_defaults(func=cmd_apply_preset)


    p = sub.add_parser("cheats", help="list bundled cheat packs")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=cmd_cheat_list)

    p = sub.add_parser("cheat-max-known-items", help="set existing known item/material/currency quantities to cheat values")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--item-cap", type=int, default=999)
    p.add_argument("--rupie-cap", type=int, default=99_999_999)
    p.add_argument("--mastery-cap", type=int, default=9_999_999)
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_cheat_max_known_items)

    p = sub.add_parser("cheat-set-known-items", help="set existing known item/material/currency quantities to one exact value")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--quantity", type=int, default=999, help="exact quantity/value to write to all known item rows")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_cheat_set_known_items)

    p = sub.add_parser("cheat-max-sigils", help="set existing known sigil levels and optionally lock them")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--level", type=int, default=15)
    p.add_argument("--lock", action="store_true", default=True)
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_cheat_max_sigils)

    p = sub.add_parser("cheat-max-weapons", help="set existing known weapon XP/progress and flags")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--xp", type=int, default=190)
    p.add_argument("--flag", action="store_true", default=True)
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_cheat_max_weapons)


    p = sub.add_parser("cheat-set-sigils", help="set existing known sigils to an exact level and optionally lock them")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--level", type=int, default=15)
    p.add_argument("--lock", action="store_true", default=True)
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_cheat_max_sigils)

    p = sub.add_parser("cheat-set-weapons", help="set existing known weapons to an exact XP/progress value and optionally set flags")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--xp", type=int, default=190)
    p.add_argument("--flag", action="store_true", default=True)
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_cheat_max_weapons)

    p = sub.add_parser("save-wizard-cheats", help="list editor-native Save Wizard style cheat mappings")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--csv", help="write listed cheat rows to CSV")
    p.add_argument("--load-sheet", action="store_true", help="also download/import the linked Google Sheet tab as reference-only rows")
    p.add_argument("--url", default=SAVE_WIZARD_SHEET_URL, help="Google Sheet URL to load when --load-sheet is used")
    p.add_argument("--timeout", type=int, default=35)
    p.set_defaults(func=cmd_save_wizard_cheats)

    p = sub.add_parser("apply-save-wizard-cheat", help="apply a built-in safe Save Wizard style cheat mapping")
    p.add_argument("save")
    p.add_argument("cheat", help="cheat key or unique name fragment; use save-wizard-cheats to list")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.add_argument("--item-cap", type=int, default=999)
    p.add_argument("--rupie-cap", type=int, default=99_999_999)
    p.add_argument("--mastery-cap", type=int, default=9_999_999)
    p.add_argument("--level", type=int, default=15)
    p.add_argument("--xp", type=int, default=190)
    p.set_defaults(func=cmd_apply_save_wizard_cheat)

    p = sub.add_parser("cheat-add-all-v-sigils", help="add every missing known V/V+ GEEN sigil into empty sigil slots")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--level", type=int, default=15)
    p.add_argument("--lock", action="store_true", default=True)
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_cheat_add_all_v_sigils)

    p = sub.add_parser("cheat-add-all-materials", help="add every missing known material/currency row into empty item slots")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_cheat_add_all_materials)

    p = sub.add_parser("item-id-catalog", help="audit/search the Community Item IDs catalog coverage")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--summary", action="store_true", help="print coverage summary")
    p.add_argument("--download", action="store_true", help="download and merge Community Item IDs before listing")
    p.add_argument("--cache", action="store_true", help="cache merged downloaded rows to resources/item_ids_downloaded.csv")
    p.add_argument("--timeout", type=int, default=35)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--csv", help="write filtered catalog CSV")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to include instead of defaults")
    p.set_defaults(func=cmd_item_id_catalog)

    p = sub.add_parser("sigil-gem-id-catalog", help="search/export Community Sigil/Gem IDs from sigil_id.csv")
    p.add_argument("query", nargs="?", default="", help="filter text, e.g. Damage Cap, GEEN_020, V+, Resistance")
    p.add_argument("--summary", action="store_true", help="print sigil/gem catalog summary")
    p.add_argument("--download", action="store_true", help="download and merge Community sigil_id.csv before listing")
    p.add_argument("--cache", action="store_true", help="cache downloaded rows to resources/item_ids_downloaded.csv")
    p.add_argument("--hide-dummy", action="store_true", help="hide reserved/dummy sigil rows")
    p.add_argument("--timeout", type=int, default=35)
    p.add_argument("--limit", type=int, default=250)
    p.add_argument("--csv", help="write filtered sigil/gem catalog CSV")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to include instead of defaults")
    p.set_defaults(func=cmd_sigil_gem_id_catalog)

    p = sub.add_parser("trait-skill-id-catalog", help="search/export Community Trait/Skill IDs from skill_id.csv")
    p.add_argument("query", nargs="?", default="", help="filter text, e.g. ATK, DMG Cap, War Elemental, SKILL_020, unused")
    p.add_argument("--summary", action="store_true", help="print trait/skill catalog summary")
    p.add_argument("--download", action="store_true", help="download and merge Community skill_id.csv before listing")
    p.add_argument("--cache", action="store_true", help="cache downloaded rows to resources/item_ids_downloaded.csv")
    p.add_argument("--hide-unused", action="store_true", help="hide unused/caution trait rows")
    p.add_argument("--timeout", type=int, default=35)
    p.add_argument("--limit", type=int, default=250)
    p.add_argument("--csv", help="write filtered trait/skill catalog CSV")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to include instead of defaults")
    p.set_defaults(func=cmd_trait_skill_id_catalog)

    p = sub.add_parser("model-id-catalog", help="search/export Community Model IDs with generated GBFR hashes")
    p.add_argument("query", nargs="?", default="", help="filter text, e.g. PL, NP, EM, WP, Bahamut, Rukalsa")
    p.add_argument("--summary", action="store_true", help="print model catalog summary")
    p.add_argument("--limit", type=int, default=250)
    p.add_argument("--csv", help="write filtered model catalog CSV")
    p.add_argument("--resource-csv", action="append", help="optional resource CSV to search instead of bundled DB")
    p.set_defaults(func=cmd_model_id_catalog)


    p = sub.add_parser("phase-id-catalog", help="search/export Community Phase IDs with generated phase/entity hashes")
    p.add_argument("query", nargs="?", default="", help="filter text, e.g. p720, Lucilius, Grandcypher, Folca")
    p.add_argument("--summary", action="store_true", help="print phase catalog summary")
    p.add_argument("--limit", type=int, default=250)
    p.add_argument("--csv", help="write filtered phase catalog CSV")
    p.add_argument("--resource-csv", action="append", help="optional resource CSV to search instead of bundled DB")
    p.set_defaults(func=cmd_phase_id_catalog)


    p = sub.add_parser("quest-id-catalog", help="search/export Community Quest IDs with quest groups and save-friendly values")
    p.add_argument("query", nargs="?", default="", help="filter text, e.g. 407321, Zero, Fate, Grandcypher")
    p.add_argument("--summary", action="store_true", help="print quest catalog summary")
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--csv", help="write filtered quest catalog CSV")
    p.add_argument("--resource-csv", action="append", help="optional resource CSV to search instead of bundled DB")
    p.set_defaults(func=cmd_quest_id_catalog)


    p = sub.add_parser("save-id-catalog", help="search/export GBFRDataTools SaveIDType field labels")
    p.add_argument("query", nargs="?", default="", help="filter text, e.g. rupies, quest flags, option, 2703")
    p.add_argument("--summary", action="store_true", help="print save field catalog summary")
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--csv", help="write filtered save field catalog CSV")
    p.set_defaults(func=cmd_save_id_catalog)

    p = sub.add_parser("google-sheet-audit", help="audit configured Google Sheet tabs and show what can be imported")
    p.add_argument("--offline", action="store_true", help="do not download; only list configured gid tabs")
    p.add_argument("--timeout", type=int, default=35)
    p.add_argument("--dump-dir", help="optional folder to save each fetched gid CSV")
    p.add_argument("--csv", help="write audit summary CSV")
    p.add_argument("--url", action="append", help="Google Sheets edit/export URL to audit; can be repeated")
    p.add_argument("--urls-file", action="append", help="text file containing sheet URLs; can be repeated")
    p.set_defaults(func=cmd_google_sheet_audit)

    p = sub.add_parser("download-gbids", help="download Community/Google Sheet GBID sources into one CSV")
    p.add_argument("output", help="output CSV")
    p.add_argument("--url", action="append", help="CSV/HTML/Google Sheets URL to merge; can be repeated")
    p.add_argument("--urls-file", action="append", help="text file with one source URL per line")
    p.add_argument("--community", action="store_true", help="include Community item IDs source")
    p.add_argument("--merge-seed", action="store_true", help="merge packaged seed rows into the output")
    p.add_argument("--timeout", type=int, default=35)
    p.set_defaults(func=cmd_download_gbids)

    p = sub.add_parser("reference-notes", help="search bundled rate/mechanics/material reference notes")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--csv", action="append", help="optional reference CSV to search")
    p.add_argument("--out-csv", help="write filtered rows to CSV")
    p.set_defaults(func=cmd_reference_notes)

    p = sub.add_parser("resource-ids", help="search non-hash Community resource IDs like quests/models/buffs/actions")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--csv", action="append", help="optional resource ID CSV to search")
    p.set_defaults(func=cmd_resource_ids)

    p = sub.add_parser("download-resource-ids", help="download Community non-hash ID pages into one CSV")
    p.add_argument("output", help="output CSV")
    p.add_argument("--url", action="append", help="resource URL to merge; can be repeated")
    p.add_argument("--urls-file", action="append", help="text file with one resource URL per line")
    p.add_argument("--community", action="store_true", help="include default Community resource pages")
    p.add_argument("--merge-seed", action="store_true", help="merge packaged seed rows into the output")
    p.add_argument("--timeout", type=int, default=35)
    p.set_defaults(func=cmd_download_resource_ids)

    p = sub.add_parser("unit-map", help="list named Unit IDs resolved from save context")
    p.add_argument("save")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--filter", help="filter text")
    p.add_argument("--csv", help="write unit map CSV")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve names")
    p.set_defaults(func=cmd_unit_map)

    p = sub.add_parser("items", help="list resolved item/material candidate rows")
    p.add_argument("save")
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--csv", help="write items CSV")
    p.add_argument("--filter", help="filter text")
    p.add_argument("--item-csv", action="append", help="optional item hash CSV to resolve names")
    p.set_defaults(func=cmd_items)

    p = sub.add_parser("sigils", help="list resolved sigil/gem rows")
    p.add_argument("save")
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--csv", help="write sigils CSV")
    p.add_argument("--filter", help="filter text")
    p.add_argument("--item-csv", action="append", help="optional item hash CSV to resolve names")
    p.set_defaults(func=cmd_sigils)

    p = sub.add_parser("hash-scan", help="scan save for known GBID hashes and optional unknown hash-like fields")
    p.add_argument("save")
    p.add_argument("--unknown", action="store_true", help="also include unknown values from known hash-like save unit IDs")
    p.add_argument("--limit", type=int, default=500)
    p.add_argument("--csv", help="write CSV result")
    p.add_argument("--item-csv", action="append", help="optional item hash CSV to resolve names")
    p.set_defaults(func=cmd_hash_scan)

    p = sub.add_parser("weapons", help="list resolved weapon rows")
    p.add_argument("save")
    p.add_argument("--limit", type=int, default=300)
    p.add_argument("--filter", help="filter text")
    p.add_argument("--csv", help="write weapons CSV")
    p.add_argument("--item-csv", action="append", help="optional item hash CSV to resolve names")
    p.set_defaults(func=cmd_weapons)


    p = sub.add_parser("edit-character", help="patch selected existing character fields by unit id")
    p.add_argument("save")
    p.add_argument("--unit", type=int, required=True, help="character unit id, usually 10000 through 10039")
    p.add_argument("--hash", help="GBID/name/hex/decimal character hash, e.g. PL0000 or Katalina")
    p.add_argument("--level", type=int, help="character level, usually max 100")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_edit_character)

    p = sub.add_parser("cheat-max-characters", help="set existing character slot levels to max/supplied level")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--level", type=int, default=100)
    p.set_defaults(func=cmd_cheat_max_characters)

    p = sub.add_parser("cheat-set-characters", help="set existing character slot levels to an exact level")
    p.add_argument("save")
    p.add_argument("--output")
    p.add_argument("--level", type=int, default=100)
    p.set_defaults(func=cmd_cheat_max_characters)

    p = sub.add_parser("edit-overmastery", help="patch the four observed character RNG/overmastery hash slots on field 1404")
    p.add_argument("save")
    p.add_argument("--unit", type=int, required=True, help="character unit id, usually 10000 through 10039")
    p.add_argument("--value", action="append", help="hash/GBID/name for one overmastery slot; repeat exactly four times")
    p.add_argument("--clear", action="store_true", help="clear the four slots to the empty hash")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_edit_overmastery)

    p = sub.add_parser("cheat-complete-quests", help="experimental: mark quest status/result arrays complete and S+++ rank candidates")
    p.add_argument("save")
    p.add_argument("--output")
    p.set_defaults(func=cmd_cheat_complete_quests)

    p = sub.add_parser("cheat-unlock-titles", help="experimental: raise title/archive/book/list candidate fields to at least unlocked")
    p.add_argument("save")
    p.add_argument("--output")
    p.set_defaults(func=cmd_cheat_unlock_titles)

    p = sub.add_parser("swap-character-slots", help="swap character slot fields between two character unit ids")
    p.add_argument("save")
    p.add_argument("--unit-a", type=int, required=True)
    p.add_argument("--unit-b", type=int, required=True)
    p.add_argument("--output")
    p.set_defaults(func=cmd_swap_character_slots)

    p = sub.add_parser("swap-item-slots", help="swap item/material slot fields between two item unit ids")
    p.add_argument("save")
    p.add_argument("--unit-a", type=int, required=True)
    p.add_argument("--unit-b", type=int, required=True)
    p.add_argument("--output")
    p.set_defaults(func=cmd_swap_item_slots)

    p = sub.add_parser("swap-sigil-slots", help="swap sigil/gem slot fields between two sigil unit ids")
    p.add_argument("save")
    p.add_argument("--unit-a", type=int, required=True)
    p.add_argument("--unit-b", type=int, required=True)
    p.add_argument("--output")
    p.set_defaults(func=cmd_swap_sigil_slots)

    p = sub.add_parser("swap-weapon-slots", help="swap weapon slot fields between two weapon unit ids")
    p.add_argument("save")
    p.add_argument("--unit-a", type=int, required=True)
    p.add_argument("--unit-b", type=int, required=True)
    p.add_argument("--output")
    p.set_defaults(func=cmd_swap_weapon_slots)

    p = sub.add_parser("edit-item", help="patch selected existing item/material fields by unit id")
    p.add_argument("save")
    p.add_argument("--unit", type=int, required=True)
    p.add_argument("--quantity", type=int)
    p.add_argument("--hash", help="GBID/name/hex/decimal hash")
    p.add_argument("--flag", type=int)
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve names")
    p.set_defaults(func=cmd_edit_item)

    p = sub.add_parser("add-item", help="add item/material by reusing the first empty existing ItemManager slot")
    p.add_argument("save")
    p.add_argument("item", help="GBID/name/hash, e.g. Rupie or ITEM_...")
    p.add_argument("--quantity", type=int, default=1)
    p.add_argument("--flag", type=int, help="optional flag/state value; default uses 1 if a flag field exists")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_add_item)

    p = sub.add_parser("batch-add-items", help="batch add item/material rows from a text/CSV list into empty ItemManager slots")
    p.add_argument("save")
    p.add_argument("list_file", help="text file lines like: Rupie, 999999 or Standard Refinium x99")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_batch_add_items)


    p = sub.add_parser("batch-add-sigils", help="batch add sigil rows from a text/CSV list into empty Gem/Sigil slots")
    p.add_argument("save")
    p.add_argument("list_file", help="text file lines like: Damage Cap V, 15 locked or GEEN_020_04 lv15 unlock")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_batch_add_sigils)

    p = sub.add_parser("batch-add-weapons", help="batch add weapon rows from a text/CSV list into empty WeaponManager slots")
    p.add_argument("save")
    p.add_argument("list_file", help="text file lines like: Rukalsa, 0 or WEP_PL0200_01 xp190")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_batch_add_weapons)

    p = sub.add_parser("add-sigil", help="add sigil by reusing the first empty existing Gem/Sigil slot")
    p.add_argument("save")
    p.add_argument("sigil", help="GBID/name/hash, e.g. GEEN_020_04 or Damage Cap V")
    p.add_argument("--level", type=int, default=15)
    lock_group = p.add_mutually_exclusive_group()
    lock_group.add_argument("--lock", action="store_true")
    lock_group.add_argument("--unlock", action="store_true")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_add_sigil)

    p = sub.add_parser("add-weapon", help="add weapon by reusing the first empty existing WeaponManager slot")
    p.add_argument("save")
    p.add_argument("weapon", help="GBID/name/hash, e.g. WEP_PL0200_01 or Rukalsa")
    p.add_argument("--xp", type=int, default=0)
    p.add_argument("--flags", type=int)
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append")
    p.set_defaults(func=cmd_add_weapon)

    p = sub.add_parser("edit-sigil", help="patch selected existing sigil fields by unit id")
    p.add_argument("save")
    p.add_argument("--unit", type=int, required=True)
    p.add_argument("--level", type=int)
    p.add_argument("--hash", help="GBID/name/hex/decimal hash")
    p.add_argument("--worn-by", help="GBID/name/hex/decimal character hash")
    lock_group = p.add_mutually_exclusive_group()
    lock_group.add_argument("--lock", action="store_true")
    lock_group.add_argument("--unlock", action="store_true")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve names")
    p.set_defaults(func=cmd_edit_sigil)

    p = sub.add_parser("edit-weapon", help="patch selected existing weapon fields by unit id")
    p.add_argument("save")
    p.add_argument("--unit", type=int, required=True)
    p.add_argument("--xp", type=int)
    p.add_argument("--hash", help="GBID/name/hex/decimal hash")
    p.add_argument("--flags", type=int)
    p.add_argument("--stone", help="GBID/name/hex/decimal stone hash")
    p.add_argument("--clear-stone", action="store_true")
    p.add_argument("--output")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve names")
    p.set_defaults(func=cmd_edit_weapon)



    p = sub.add_parser("resolve-unknown-hashes", help="try generated GBFR ID patterns against unknown hash fields in a save")
    p.add_argument("save")
    p.add_argument("--limit", type=int, default=5000)
    p.add_argument("--csv", help="write generated candidate matches to CSV")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve existing known hashes first")
    p.set_defaults(func=cmd_resolve_unknown_hashes)

    p = sub.add_parser("save-map", help="summarize the save by manager/field id for mapping and research")
    p.add_argument("save")
    p.add_argument("--summary", action="store_true", help="print compact manager/coverage summary")
    p.add_argument("--unknown-only", action="store_true", help="show only candidate/unknown/research target fields")
    p.add_argument("--limit", type=int, help="row limit for console output")
    p.add_argument("--csv", help="write full save map CSV")
    p.add_argument("--json", help="write full save map JSON")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve hash coverage")
    p.set_defaults(func=cmd_save_map)

    p = sub.add_parser("id-audit", help="audit/clean up known, candidate, and unresolved hash-like IDs in a save")
    p.add_argument("save")
    p.add_argument("--summary", action="store_true", help="print compact ID coverage summary")
    p.add_argument("--unresolved-only", action="store_true", help="show/export only unresolved and generated candidate rows")
    p.add_argument("--include-empty", action="store_true", help="include 0 and empty hash sentinel rows")
    p.add_argument("--skip-ability", action="store_true", help="hide ability/action hash noise from the audit")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--csv", help="write ID audit CSV")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve hashes")
    p.add_argument("--resource-csv", action="append", help="optional resource CSV to resolve non-GBID hashes")
    p.set_defaults(func=cmd_id_audit)

    p = sub.add_parser("unknown-save-fields", help="list candidate/unknown save fields that still need before/after research")
    p.add_argument("save")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--csv", help="write research target CSV")
    p.add_argument("--item-csv", action="append", help="optional GBID CSV to resolve hash coverage")
    p.set_defaults(func=cmd_unknown_save_fields)

    p = sub.add_parser("compare", help="compare two saves and list changed save units")
    p.add_argument("before")
    p.add_argument("after")
    p.add_argument("--limit", type=int, default=200, help="row preview limit for console output")
    p.add_argument("--json", help="write full JSON diff to this path")
    p.add_argument("--csv", help="write full CSV diff to this path")
    p.set_defaults(func=cmd_compare)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
