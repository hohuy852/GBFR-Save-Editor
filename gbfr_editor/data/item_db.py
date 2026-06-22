from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
import csv
import html
import re
import urllib.request
from urllib.parse import parse_qs, urlparse

# Remote auto-download URLs are intentionally omitted from release builds.
# The parser still accepts user-provided CSV/Google Sheet exports when pasted/imported.
FALLBACK_ITEM_URL = ""
RAW_ITEM_URL = ""
RAW_TRAIT_SKILL_URL = ""
RAW_SIGIL_GEM_URL = ""
FALLBACK_SIGIL_GEM_URL = ""
TRAIT_SKILL_URL = RAW_TRAIT_SKILL_URL
FALLBACK_TRAIT_SKILL_URL = ""
DEFAULT_ITEM_URL = RAW_ITEM_URL
GOOGLE_SHEET_EXPORT_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([^/]+)")

_RESERVED_NAMES = {"", "nan", "none", "null", "-"}


@dataclass
class ItemEntry:
    item_id: str
    name: str
    hash_value: int
    aliases: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def hash_hex(self) -> str:
        return f"{self.hash_value & 0xFFFFFFFF:08X}"

    @property
    def category(self) -> str:
        return infer_category(self.item_id)

    @property
    def display_name(self) -> str:
        return self.name or f"Internal / unused {self.item_id}"

    @property
    def alias_text(self) -> str:
        return "; ".join(self.aliases)

    def tooltip(self) -> str:
        lines = [
            f"Name: {self.display_name}",
            f"GBID: {self.item_id}",
            f"Category: {self.category}",
            f"Hash: {self.hash_hex}",
            f"Decimal: {self.hash_value & 0xFFFFFFFF}",
        ]
        if self.aliases:
            lines.append("Aliases / source names: " + self.alias_text)
        return "\n".join(lines)


class ItemDatabase:
    def __init__(self) -> None:
        self.by_hash: Dict[int, ItemEntry] = {}
        self.by_id: Dict[str, ItemEntry] = {}

    def add(self, entry: ItemEntry) -> None:
        h = entry.hash_value & 0xFFFFFFFF
        clean_name = clean_item_name(entry.name, entry.item_id)
        incoming_aliases = list(entry.aliases)
        if clean_name and clean_name not in incoming_aliases:
            incoming_aliases.append(clean_name)
        existing = self.by_hash.get(h)
        if existing:
            aliases = list(existing.aliases)
            for alias in incoming_aliases:
                if alias and alias not in aliases:
                    aliases.append(alias)
            # Prefer specific/longer names over blank, reserved, or shorter base names.
            chosen_name = existing.name
            if _name_score(clean_name) > _name_score(existing.name):
                chosen_name = clean_name
            chosen_id = existing.item_id
            # Keep the first non-placeholder ID, but let a concrete ID replace NP/unknown.
            if _id_score(entry.item_id) > _id_score(existing.item_id):
                chosen_id = entry.item_id
            merged = ItemEntry(item_id=chosen_id, name=chosen_name, hash_value=h, aliases=tuple(aliases))
            self.by_hash[h] = merged
            self.by_id[entry.item_id.upper()] = merged
            self.by_id[chosen_id.upper()] = merged
            return
        normalized = ItemEntry(entry.item_id.strip(), clean_name, h, tuple(dict.fromkeys(incoming_aliases)))
        self.by_hash[h] = normalized
        self.by_id[normalized.item_id.upper()] = normalized

    def __len__(self) -> int:
        return len(self.by_hash)

    def lookup_hash(self, value: int) -> Optional[ItemEntry]:
        return self.by_hash.get(value & 0xFFFFFFFF)

    def lookup_text(self, value: int) -> str:
        entry = self.lookup_hash(value)
        return f"{entry.display_name} ({entry.item_id})" if entry else f"0x{value & 0xFFFFFFFF:08X}"

    def search(self, query: str, limit: int = 200) -> List[ItemEntry]:
        q = query.strip().lower()
        rows = sorted(self.by_hash.values(), key=lambda e: (e.category, e.item_id, e.hash_hex))
        if not q:
            return rows[:limit]
        out: List[ItemEntry] = []
        for entry in rows:
            hay = " ".join([
                entry.name,
                entry.item_id,
                entry.hash_hex,
                str(entry.hash_value & 0xFFFFFFFF),
                entry.category,
                entry.alias_text,
            ]).lower()
            tokens = [t for t in q.split() if t]
            if (tokens and all(t in hay for t in tokens)) or (not tokens and q in hay):
                out.append(entry)
                if len(out) >= limit:
                    break
        return out

    @classmethod
    def load_many(cls, paths: Iterable[str | Path]) -> "ItemDatabase":
        db = cls()
        for path in paths:
            p = Path(path)
            if p.exists():
                db.merge(cls.load_csv(p))
        return db

    def merge(self, other: "ItemDatabase") -> None:
        for entry in other.by_hash.values():
            self.add(entry)

    @classmethod
    def load_csv(cls, path: str | Path) -> "ItemDatabase":
        return cls.from_csv_text(Path(path).read_text(encoding="utf-8-sig"))

    @classmethod
    def from_csv_text(cls, text: str) -> "ItemDatabase":
        from io import StringIO
        db = cls()
        f = StringIO(text)
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample)
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(f, dialect=dialect)
        if not reader.fieldnames:
            return db
        fields = {name.lower().strip(): name for name in reader.fieldnames}
        id_col = _pick(fields, ["id", "item_id", "item id", "gbid"])
        name_col = _pick(fields, ["name", "item_name", "item name"])
        hash_col = _pick(fields, ["hash", "id hash", "hash_hex", "id_hash"])
        alias_col = _pick(fields, ["aliases", "alias", "tooltip", "notes", "description"])
        if not (id_col and name_col and hash_col):
            f.seek(0)
            plain = csv.reader(f, dialect=dialect)
            for row in plain:
                _try_add_loose_row(db, row)
            return db
        for row in reader:
            aliases: List[str] = []
            if alias_col and row.get(alias_col):
                aliases = [x.strip() for x in re.split(r"[|;]", row.get(alias_col, "")) if x.strip()]
            # Keep every extra non-empty sheet column as searchable tooltip text.
            for k, v in row.items():
                if k not in {id_col, name_col, hash_col, alias_col} and v:
                    text = str(v).strip()
                    if text and text not in aliases:
                        aliases.append(text)
            _try_add_row(db, row.get(id_col, ""), row.get(name_col, ""), row.get(hash_col, ""), aliases)
        return db

    @classmethod
    def from_community_html(cls, text: str) -> "ItemDatabase":
        db = cls()
        text = html.unescape(text)
        text = re.sub(r"<script.*?</script>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", "\n", text)
        text = re.sub(r"[\t ]+", " ", text)
        id_pat = r"([A-Z]{2,8}[A-Z0-9]*(?:_[A-Z0-9]{2,})+)"
        line_re = re.compile(rf"\b{id_pat}\s+(.+?)\s+([0-9A-Fa-f]{{8}})\b")
        for line in text.splitlines():
            line = line.strip()
            if not line or line.lower().startswith("id name"):
                continue
            m = line_re.search(line)
            if not m:
                continue
            item_id, name, hash_hex = m.groups()
            _try_add_row(db, item_id, name, hash_hex)
        return db

    @classmethod
    def download_url(cls, url: str, timeout: int = 30) -> "ItemDatabase":
        candidate = normalize_source_url(url)
        request = urllib.request.Request(candidate, headers={"User-Agent": "GBFRRelinkEditor/0.9"})
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
        lower = candidate.lower()
        stripped = data.lstrip().lower()
        if lower.endswith(".csv") or "export?format=csv" in lower or stripped.startswith("id,name") or stripped.startswith("id\tname"):
            return cls.from_csv_text(data)
        return cls.from_community_html(data)

    @classmethod
    def download_many(cls, urls: Iterable[str], timeout: int = 30) -> tuple["ItemDatabase", List[str]]:
        merged = cls()
        errors: List[str] = []
        for url in urls:
            u = (url or "").strip()
            if not u or u.startswith("#"):
                continue
            try:
                merged.merge(cls.download_url(u, timeout=timeout))
            except Exception as exc:
                errors.append(f"{u}: {exc}")
        return merged, errors

    @classmethod
    def download_community(cls, url: str = DEFAULT_ITEM_URL, timeout: int = 30) -> "ItemDatabase":
        urls = [u for u in [url, RAW_ITEM_URL, TRAIT_SKILL_URL, RAW_SIGIL_GEM_URL, FALLBACK_ITEM_URL, FALLBACK_TRAIT_SKILL_URL, FALLBACK_SIGIL_GEM_URL] if str(u or "").strip()]
        if not urls:
            raise RuntimeError("No bundled remote source URL is configured. Import a local CSV or paste a Google Sheet/CSV URL instead.")
        db, errors = cls.download_many(urls, timeout=timeout)
        if len(db):
            return db
        raise RuntimeError("Could not download item IDs: " + "; ".join(errors))

    def save_csv(self, path: str | Path) -> None:
        rows = sorted(self.by_hash.values(), key=lambda e: (e.category, e.item_id, e.hash_hex))
        with Path(path).open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "name", "hash", "category", "aliases"])
            writer.writeheader()
            for e in rows:
                writer.writerow({"id": e.item_id, "name": e.display_name, "hash": e.hash_hex, "category": e.category, "aliases": e.alias_text})



def normalize_source_url(url: str) -> str:
    """Accept normal CSV URLs plus Google Sheets edit URLs and return a fetchable CSV/HTML URL."""
    u = (url or "").strip()
    if not u:
        return u
    if "docs.google.com/spreadsheets" in u:
        parsed = urlparse(u)
        match = GOOGLE_SHEET_EXPORT_RE.search(u)
        if match:
            doc_id = match.group(1)
            query = parse_qs(parsed.query)
            gid = (query.get("gid") or [""])[0]
            if not gid and parsed.fragment:
                gid = (parse_qs(parsed.fragment).get("gid") or [""])[0]
            gid_part = f"&gid={gid}" if gid else ""
            return f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=csv{gid_part}"
    return u


def source_urls_from_text(text: str) -> List[str]:
    """Pull one URL per line, ignoring comments and blank lines."""
    urls: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls

def infer_category(item_id: str) -> str:
    ident = (item_id or "").upper()
    if re.match(r"P[0-9A-F]{3}$", ident) or re.match(r"PH[0-9A-F]{3}$", ident):
        return "Phase"
    if ident.startswith("PL") and ident[2:].isdigit():
        return "Character"
    if ident.startswith("NP") and ident[2:].isdigit():
        return "NPC / Model"
    if ident.startswith("EM") and ident[2:].isdigit():
        return "Enemy Model"
    if ident.startswith("WP") and ident[2:].isdigit():
        return "Player Weapon Model"
    if re.match(r"(BA|BH)[0-9A-F]{4}$", ident):
        return "Map Object Model"
    if ident.startswith("ET") or re.match(r"[A-F0-9]{4}$", ident):
        return "Extra Model"
    if ident.startswith("WEP_"):
        return "Weapon"
    if ident.startswith("GEEN_"):
        return "Sigil / Gem"
    if ident.startswith("ITEM_35"):
        return "Currency"
    if ident.startswith("ITEM_13"):
        return "Consumable"
    if ident.startswith("ITEM_23"):
        return "Crewmate Card"
    if re.match(r"ITEM_(25|26|27|28|29)_", ident):
        return "Wrightstone"
    if ident.startswith("ITEM_70") or ident.startswith("ITEM_80"):
        return "Key Item"
    if ident.startswith("ITEM_50"):
        return "Fate / Special Item"
    if ident.startswith("ITEM_34"):
        return "Glitterstone"
    if ident.startswith("ITEM_36"):
        return "Ticket"
    if re.match(r"ITEM_(0[1-9]|1[0-8]|22|30|31|32|33)_", ident):
        return "Material"
    if ident.startswith("ITEM_"):
        return "Item"
    if ident.startswith("SKILL_"):
        return "Trait / Skill"
    if ident.startswith("ABILITY"):
        return "Ability"
    return "Other"


def clean_item_name(name: str, item_id: str = "") -> str:
    n = (name or "").strip().strip('"')
    if n.lower() in _RESERVED_NAMES:
        if item_id:
            return f"Internal / unused {item_id.strip()}"
        return "Internal / unused"
    # Public data uses Dummy### for reserved/unused sigil rows. Make that obvious
    # instead of presenting it like a normal player-facing item name.
    if re.fullmatch(r"Dummy\d+\+?", n, flags=re.I):
        return f"Internal / reserved {n}"
    return n


def _name_score(name: str) -> int:
    n = (name or "").strip()
    if not n or n.lower().startswith("unnamed / reserved"):
        return 0
    score = len(n)
    if "," in n:
        score += 20
    return score


def _id_score(item_id: str) -> int:
    ident = (item_id or "").upper()
    if not ident:
        return 0
    score = len(ident)
    if "_NP" in ident or ident.endswith("_99"):
        score -= 10
    return score


def _pick(fields: Dict[str, str], names: List[str]) -> Optional[str]:
    for name in names:
        if name in fields:
            return fields[name]
    for low, original in fields.items():
        for name in names:
            if name in low:
                return original
    return None


def _parse_hash(value: str) -> Optional[int]:
    value = (value or "").strip()
    value = value.removeprefix("0x").removeprefix("0X")
    if not re.fullmatch(r"[0-9A-Fa-f]{1,8}", value):
        return None
    return int(value, 16)


def _try_add_row(db: ItemDatabase, item_id: str, name: str, hash_hex: str, aliases: Optional[List[str]] = None) -> None:
    item_id = (item_id or "").strip()
    h = _parse_hash(hash_hex)
    if not item_id or h is None:
        return
    db.add(ItemEntry(item_id=item_id, name=clean_item_name(name, item_id), hash_value=h, aliases=tuple(aliases or [])))



def _try_add_loose_row(db: ItemDatabase, row: List[str]) -> None:
    """Best-effort parser for community Google Sheets with inconsistent column names/order."""
    cells = [str(c).strip().strip('"') for c in row]
    if not cells or all(not c for c in cells):
        return
    joined = " ".join(cells).lower()
    if "id hash" in joined and "name" in joined:
        return
    id_idx = None
    hash_idx = None
    for i, c in enumerate(cells):
        if id_idx is None and re.fullmatch(r"[A-Z]{2,12}[A-Z0-9]*(?:_[A-Z0-9]{1,})+", c.upper()):
            id_idx = i
        if hash_idx is None and re.fullmatch(r"(?:0x)?[0-9A-Fa-f]{8}", c):
            hash_idx = i
    if id_idx is None or hash_idx is None or id_idx == hash_idx:
        return
    name_candidates: List[str] = []
    for i, c in enumerate(cells):
        if i in (id_idx, hash_idx) or not c:
            continue
        low = c.lower()
        if low in {"id", "name", "hash", "id hash", "gbid", "notes", "tooltip", "description"}:
            continue
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", c):
            continue
        name_candidates.append(c)
    name = name_candidates[0] if name_candidates else ""
    aliases = name_candidates[1:]
    _try_add_row(db, cells[id_idx], name, cells[hash_idx], aliases)
