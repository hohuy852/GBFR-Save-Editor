from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple
import csv
import io
import re
import urllib.request
from urllib.parse import parse_qs, urlparse

from item_db import ItemDatabase, normalize_source_url, source_urls_from_text
from gbfr_editor.paths import RESOURCE_DIR

SHEET_DOC_ID = "1mGf987Njg3VodeXp8kVwzEgvYSAeMzkHnkGnAj1_RjY"
DEFAULT_GOOGLE_SHEET_TABS_FILE = RESOURCE_DIR / "google_sheet_tabs.txt"

KNOWN_SHEET_GIDS: Tuple[Tuple[str, str], ...] = (
    ("0", "Main / index tab"),
    ("1502701072", "Save Wizard cheats"),
    ("528798748", "Community data tab"),
    ("1672697976", "Community data tab"),
    ("1387008576", "Community data tab"),
    ("1695746575", "Community data tab"),
    ("717708738", "Community data tab"),
    ("1495923803", "Community data tab"),
    ("590100080", "Sigil / gem sheet tab"),
    ("321220778", "Community data tab"),
    ("1539189767", "Community data tab"),
    ("1099533", "Community data tab"),
    ("547963474", "Community data tab"),
)

HASH_RE = re.compile(r"^(?:0x)?[0-9A-Fa-f]{8}$")
ID_RE = re.compile(r"\b(?:ITEM|GEEN|SKILL|WEP|PL|NP|EM|WP|BA|BH|P|PH|ST)[A-Z0-9_]*\b", re.I)


@dataclass
class SheetAuditRow:
    gid: str
    label: str
    url: str
    normalized_url: str
    status: str = "pending"
    columns: int = 0
    rows: int = 0
    hash_like_rows: int = 0
    id_like_rows: int = 0
    importable_rows: int = 0
    saved_csv: str = ""
    notes: str = ""
    headers: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        return {
            "gid": self.gid,
            "label": self.label,
            "status": self.status,
            "columns": self.columns,
            "rows": self.rows,
            "hash_like_rows": self.hash_like_rows,
            "id_like_rows": self.id_like_rows,
            "importable_rows": self.importable_rows,
            "saved_csv": self.saved_csv,
            "url": self.url,
            "normalized_url": self.normalized_url,
            "headers": " | ".join(self.headers),
            "notes": self.notes,
        }


def urls_from_resource_file(path: str | Path = DEFAULT_GOOGLE_SHEET_TABS_FILE) -> List[str]:
    p = Path(path)
    if p.exists():
        return [u for u in source_urls_from_text(p.read_text(encoding="utf-8")) if "docs.google.com/spreadsheets" in u]
    return [f"https://docs.google.com/spreadsheets/d/{SHEET_DOC_ID}/edit?gid={gid}#gid={gid}" for gid, _ in KNOWN_SHEET_GIDS]


def gid_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    gid = (query.get("gid") or [""])[0]
    if not gid and parsed.fragment:
        gid = (parse_qs(parsed.fragment).get("gid") or [""])[0]
    return gid or "0"


def label_for_gid(gid: str) -> str:
    for known, label in KNOWN_SHEET_GIDS:
        if str(known) == str(gid):
            return label
    return "Community sheet tab"


def _looks_hash(value: object) -> bool:
    text = str(value or "").strip().strip("`\"'")
    return bool(HASH_RE.match(text))


def _looks_id(value: object) -> bool:
    text = str(value or "").strip().strip("`\"'")
    return bool(ID_RE.search(text))


def _count_importable(text: str) -> int:
    try:
        db = ItemDatabase.from_csv_text(text)
        return len(db)
    except Exception:
        return 0


def analyze_csv_text(text: str, row: SheetAuditRow) -> SheetAuditRow:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        row.status = "empty"
        row.notes = "No rows returned by CSV export."
        return row
    row.headers = tuple(str(x).strip() for x in rows[0])
    data_rows = rows[1:] if row.headers else rows
    row.columns = max((len(r) for r in rows), default=0)
    row.rows = len(data_rows)
    row.hash_like_rows = sum(1 for r in data_rows if any(_looks_hash(c) for c in r))
    row.id_like_rows = sum(1 for r in data_rows if any(_looks_id(c) for c in r))
    row.importable_rows = _count_importable(text)
    row.status = "ok"
    if row.importable_rows:
        row.notes = "Directly importable as ID/name/hash rows."
    elif row.hash_like_rows or row.id_like_rows:
        row.notes = "Has useful IDs/hashes, but columns need a custom mapper before they become editor-native rows."
    else:
        row.notes = "Reference/info tab; not directly importable as GBID rows."
    return row


def audit_sheet_sources(
    urls: Optional[Iterable[str]] = None,
    *,
    fetch: bool = False,
    timeout: int = 35,
    dump_dir: Optional[str | Path] = None,
) -> List[SheetAuditRow]:
    source_urls = [u for u in list(urls or urls_from_resource_file()) if "docs.google.com/spreadsheets" in str(u)]
    rows: List[SheetAuditRow] = []
    out_dir = Path(dump_dir) if dump_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    for url in source_urls:
        gid = gid_from_url(url)
        audit = SheetAuditRow(
            gid=gid,
            label=label_for_gid(gid),
            url=url,
            normalized_url=normalize_source_url(url),
            status="configured",
            notes="Configured source. Run with download/audit enabled to inspect rows and columns.",
        )
        if fetch:
            try:
                request = urllib.request.Request(audit.normalized_url, headers={"User-Agent": "GBFRRelinkEditor/5.2"})
                with urllib.request.urlopen(request, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8-sig", errors="replace")
                if out_dir:
                    path = out_dir / f"google_sheet_gid_{gid}.csv"
                    path.write_text(text, encoding="utf-8")
                    audit.saved_csv = str(path)
                audit = analyze_csv_text(text, audit)
            except Exception as exc:
                audit.status = "failed"
                audit.notes = str(exc)
        rows.append(audit)
    return rows


def audit_summary(rows: List[SheetAuditRow]) -> str:
    configured = len(rows)
    fetched = sum(1 for r in rows if r.status == "ok")
    failed = sum(1 for r in rows if r.status == "failed")
    importable = sum(r.importable_rows for r in rows)
    hash_like = sum(r.hash_like_rows for r in rows)
    lines = [
        "Google Sheet source coverage",
        "============================",
        f"Configured tabs: {configured}",
        f"Fetched tabs:    {fetched}",
        f"Failed tabs:     {failed}",
        f"Importable rows: {importable}",
        f"Hash-like rows:  {hash_like}",
        "",
    ]
    for r in rows:
        lines.append(
            f"gid={r.gid:<10} {r.status:<10} rows={r.rows:<5} cols={r.columns:<3} "
            f"ids={r.id_like_rows:<5} hashes={r.hash_like_rows:<5} importable={r.importable_rows:<5} {r.label}"
        )
        if r.notes:
            lines.append(f"  - {r.notes}")
        if r.headers:
            head = ", ".join(h for h in r.headers[:12] if h)
            if head:
                lines.append(f"  - columns: {head}")
        if r.saved_csv:
            lines.append(f"  - saved: {r.saved_csv}")
    return "\n".join(lines)


def write_audit_csv(rows: List[SheetAuditRow], path: str | Path) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = ["gid", "label", "status", "columns", "rows", "hash_like_rows", "id_like_rows", "importable_rows", "saved_csv", "url", "normalized_url", "headers", "notes"]
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow(r.to_dict())


