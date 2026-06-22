from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
import csv
import html
import re
import urllib.request

DEFAULT_RESOURCE_URLS = []

@dataclass(frozen=True)
class ResourceEntry:
    category: str
    id_text: str
    name: str
    source: str = ""
    aliases: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def id_upper(self) -> str:
        return self.id_text.upper()

    @property
    def decimal_value(self) -> Optional[int]:
        text = self.id_text.strip().strip("`")
        try:
            # Support normal decimal IDs, 0x-prefixed hashes, and compact hex
            # strings such as 101F00 / 5F0001 from quest and stage IDs.
            if text.lower().startswith("0x"):
                return int(text, 16)
            # Compact hex quest/stage IDs contain digits plus A-F.
            # Do not treat pure alphabetic prefixes like ba/pl/wp as numbers.
            if re.search(r"[0-9]", text) and re.search(r"[A-Fa-f]", text):
                return int(text, 16)
            if text.isdigit():
                return int(text, 10)
            return None
        except Exception:
            return None

    @property
    def alias_text(self) -> str:
        return "; ".join(x for x in self.aliases if x)

    def tooltip(self) -> str:
        parts = [
            f"Category: {self.category}",
            f"ID: {self.id_text}",
            f"Name: {self.name}",
        ]
        if self.decimal_value is not None:
            parts.append(f"Decimal: {self.decimal_value}")
        if self.aliases:
            parts.append("Aliases/notes: " + self.alias_text)
        if self.source:
            parts.append("Source: " + self.source)
        return "\n".join(parts)

class ResourceIdDatabase:
    def __init__(self) -> None:
        self.entries: List[ResourceEntry] = []
        self.by_key: Dict[Tuple[str, str], ResourceEntry] = {}
        self.by_value: Dict[int, List[ResourceEntry]] = {}

    def add(self, entry: ResourceEntry) -> None:
        if not entry.id_text or not entry.name:
            return
        key = (entry.category.lower(), entry.id_upper)
        old = self.by_key.get(key)
        if old:
            aliases = list(old.aliases)
            for text in (entry.name, *entry.aliases):
                if text and text not in aliases:
                    aliases.append(text)
            # Prefer longer names over shorter placeholder names.
            name = entry.name if len(entry.name) > len(old.name) else old.name
            merged = ResourceEntry(old.category, old.id_text, name, old.source or entry.source, tuple(aliases))
            self.by_key[key] = merged
            self._rebuild()
            return
        self.by_key[key] = entry
        self.entries.append(entry)
        val = entry.decimal_value
        if val is not None:
            self.by_value.setdefault(val, []).append(entry)

    def _rebuild(self) -> None:
        self.entries = list(self.by_key.values())
        self.by_value = {}
        for e in self.entries:
            val = e.decimal_value
            if val is not None:
                self.by_value.setdefault(val, []).append(e)

    def merge(self, other: "ResourceIdDatabase") -> None:
        for e in other.entries:
            self.add(e)

    def search(self, query: str, limit: int = 20000) -> List[ResourceEntry]:
        q = (query or "").strip().lower()
        rows = sorted(self.entries, key=lambda e: (e.category, e.id_upper, e.name))
        if not q:
            return rows[:limit]
        out: List[ResourceEntry] = []
        for e in rows:
            hay = " ".join([e.category, e.id_text, e.name, e.source, e.alias_text, str(e.decimal_value or "")]).lower()
            if q in hay:
                out.append(e)
                if len(out) >= limit:
                    break
        return out

    def lookup_value(self, value: int, categories: Optional[Iterable[str]] = None) -> Optional[ResourceEntry]:
        rows = self.by_value.get(int(value), [])
        if not rows:
            # For hex-like quest IDs stored as integer, also compare decimal-to-hex text.
            hx = f"{int(value):X}"
            for e in self.entries:
                if e.id_upper == hx:
                    rows.append(e)
        if not rows:
            return None
        if categories:
            wanted = {c.lower() for c in categories}
            for e in rows:
                if e.category.lower() in wanted:
                    return e
        return rows[0]

    @classmethod
    def load_many(cls, paths: Iterable[str | Path]) -> "ResourceIdDatabase":
        db = cls()
        for path in paths:
            p = Path(path)
            if p.exists():
                db.merge(cls.load_csv(p))
        return db

    @classmethod
    def load_csv(cls, path: str | Path) -> "ResourceIdDatabase":
        return cls.from_csv_text(Path(path).read_text(encoding="utf-8-sig"), source=str(path))

    @classmethod
    def from_csv_text(cls, text: str, source: str = "") -> "ResourceIdDatabase":
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
        if reader.fieldnames:
            fields = {_norm_header(x): x for x in reader.fieldnames}
            default_cat = _guess_category_from_source(source)
            cat_col = _pick(fields, ["category", "type", "table"])
            id_col = _pick(fields, ["id", "id name", "id/name", "gbid", "model", "quest", "quest stage id", "value"])
            name_col = _pick(fields, ["name", "title", "description", "id name", "id/name"])
            hash_col = _pick(fields, ["hash", "hash id"])
            source_col = _pick(fields, ["source", "url"])
            alias_col = _pick(fields, ["aliases", "notes", "tooltip", "sound", "array"])
            if id_col and name_col:
                for row in reader:
                    aliases: List[str] = []
                    if alias_col and row.get(alias_col):
                        aliases.extend([x.strip() for x in re.split(r"[|;]", row.get(alias_col, "")) if x.strip()])
                    for k, v in row.items():
                        if k not in {cat_col, id_col, name_col, source_col, alias_col, hash_col} and v:
                            aliases.append(_clean_cell(str(v)))
                    raw_name = _clean_cell(str(row.get(name_col, "")))
                    raw_id = _clean_cell(str(row.get(id_col, "")))
                    # Tables like buff_ids.csv combine id + name as: [2] ATK UP.
                    bracket = re.match(r"^\[([^\]]+)\]\s*(.+)$", raw_id)
                    if bracket and (id_col == name_col or not raw_name or raw_name == raw_id):
                        raw_id, raw_name = bracket.group(1), bracket.group(2)
                    if not raw_name or raw_name == raw_id:
                        raw_name = _clean_cell(str(row.get(name_col, "")))
                    cat = (row.get(cat_col, default_cat) if cat_col else default_cat) or default_cat
                    src = str(row.get(source_col, source) if source_col else source)
                    if raw_id and raw_name:
                        db.add(ResourceEntry(
                            category=cat,
                            id_text=raw_id,
                            name=raw_name,
                            source=src,
                            aliases=tuple(dict.fromkeys(x for x in aliases if x)),
                        ))
                    if hash_col and row.get(hash_col) and raw_name:
                        h = _clean_hash(row.get(hash_col, ""))
                        if h:
                            db.add(ResourceEntry(
                                category=f"{cat} Hash",
                                id_text=h,
                                name=raw_name,
                                source=src,
                                aliases=tuple(dict.fromkeys([raw_id, *[x for x in aliases if x]])),
                            ))
                return db
        # Loose fallback for simple copied tables.
        for line in text.splitlines():
            _try_add_line(db, line, "Misc", source)
        return db

    @classmethod
    def from_community_text(cls, text: str, source: str = "") -> "ResourceIdDatabase":
        db = cls()
        text = html.unescape(text)
        title = _guess_category_from_source(source)
        # Markdown headings/pages become categories as we walk through the page.
        current_category = title
        clean = re.sub(r"<script.*?</script>", "\n", text, flags=re.I | re.S)
        clean = re.sub(r"<style.*?</style>", "\n", clean, flags=re.I | re.S)
        clean = re.sub(r"<[^>]+>", "\n", clean)
        clean = re.sub(r"\r", "", clean)
        for raw in clean.splitlines():
            line = raw.strip()
            if not line:
                continue
            h = re.match(r"^#{1,4}\s+(.+?)\s*$", line)
            if h:
                current_category = _clean_heading(h.group(1)) or title
                continue
            # MkDocs text often drops markdown markers, so infer important headings too.
            inferred = _category_from_heading(line, title, current_category)
            if inferred:
                current_category = inferred
                continue
            _try_add_line(db, line, current_category, source)
        return db

    @classmethod
    def download_url(cls, url: str, timeout: int = 30) -> "ResourceIdDatabase":
        request = urllib.request.Request(url, headers={"User-Agent": "GBFRRelinkEditor/1.0"})
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        if url.lower().endswith(".csv") or text.lstrip().lower().startswith("category,"):
            return cls.from_csv_text(text, source=url)
        return cls.from_community_text(text, source=url)

    @classmethod
    def download_many(cls, urls: Iterable[str], timeout: int = 30) -> tuple["ResourceIdDatabase", List[str]]:
        merged = cls()
        errors: List[str] = []
        for url in urls:
            url = (url or "").strip()
            if not url or url.startswith("#"):
                continue
            try:
                merged.merge(cls.download_url(url, timeout=timeout))
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        return merged, errors

    def save_csv(self, path: str | Path) -> None:
        with Path(path).open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["category", "id", "name", "decimal", "source", "aliases"])
            writer.writeheader()
            for e in sorted(self.entries, key=lambda x: (x.category, x.id_upper, x.name)):
                writer.writerow({
                    "category": e.category,
                    "id": e.id_text,
                    "name": e.name,
                    "decimal": e.decimal_value if e.decimal_value is not None else "",
                    "source": e.source,
                    "aliases": e.alias_text,
                })

def _norm_header(text: str) -> str:
    text = html.unescape(str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("`", "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text

def _clean_cell(text: str) -> str:
    text = html.unescape(str(text or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("`", "")
    text = text.strip().strip('"').strip()
    return re.sub(r"\s+", " ", text)

def _clean_hash(text: str) -> str:
    m = re.search(r"(?:0x)?([0-9A-Fa-f]{8})", str(text or ""))
    if not m:
        return ""
    return "0x" + m.group(1).upper()

def _pick(fields: Dict[str, str], names: List[str]) -> Optional[str]:
    for name in names:
        if name in fields:
            return fields[name]
    for key, value in fields.items():
        for name in names:
            if name in key:
                return value
    return None

_HEX_OR_CODE = r"(?:0x[0-9A-Fa-f]{8}|[A-Za-z]{1,3}\d{2,}|[A-Za-z]{2,3}|\d{1,6}[A-Fa-f]?\d{0,3}|[0-9A-Fa-f]{5,})"

def _try_add_line(db: ResourceIdDatabase, line: str, category: str, source: str) -> None:
    line = _clean_cell(line).strip().strip("-*").strip()
    if not line or line.lower().startswith(("table of contents", "copyright", "made with", "back to top")):
        return
    line = re.sub(r"¶$", "", line).strip()
    # Model pages: PL0000 -- Gran (Rebel); Action pages: 100 = Attack 1
    m = re.match(rf"^({_HEX_OR_CODE})\s*(?:--|—|-|:|=)\s*(.+)$", line)
    if not m:
        # CSV-ish rows: p100,Tempeal or 100000,Prologue.
        m = re.match(rf"^({_HEX_OR_CODE})\s*,\s*(.+?)$", line)
    if not m:
        # Quest pages: 200000 A Lingering Regret
        m = re.match(rf"^({_HEX_OR_CODE})\s+(.+?)$", line)
    if not m:
        return
    ident, name = m.groups()
    name = name.strip().strip("|")
    if not name or len(name) > 180:
        return
    if name.lower().startswith(("chapter", "results", "save", "the", "a ", "an ")) or re.search(r"[A-Za-z]", name):
        db.add(ResourceEntry(category=category or _guess_category_from_source(source), id_text=ident, name=name, source=source))

def _clean_heading(text: str) -> str:
    text = re.sub(r"[:#`*\[\]()]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _category_from_heading(line: str, page_title: str, previous: str = "") -> Optional[str]:
    raw = _clean_cell(line).strip().rstrip("¶").strip()
    if not raw or len(raw) > 120:
        return None
    low_title = (page_title or "").lower()
    low = raw.lower()
    # Ignore table/body rows.
    if re.match(rf"^{_HEX_OR_CODE}\s*(?:--|—|-|:|=|,|\s)", raw):
        return None
    if raw.lower().startswith(("source", "data version", "tip", "function with signature")):
        return None

    # Entity Prefixes page sections.
    if "entity_prefixes" in (page_title or "").lower() or "entity_prefixes" in (previous or "").lower():
        if low == "models":
            return "Entity Prefix - Model"
        if low == "scripting":
            return "Entity Prefix - Scripting"

    # Model page sections.
    model_map = {
        "player (pl)": "Model Player",
        "npc (np)": "Model NPC",
        "enemy (em)": "Model Enemy",
        "map animated (ba)": "Model Map Animated",
        "map breakable props (bh)": "Model Breakable Prop",
        "player weapons (wp)": "Model Player Weapon",
        "enemy weapons (we)": "Enemy Weapon",
        "et": "Model Extra",
    }
    if low in model_map:
        return model_map[low]

    # Character/action subsections: Captain (Pl0000/Pl0100) -> Action - Captain.
    m = re.match(r"^(.+?)\s*\((Pl\d{4}(?:/Pl\d{4})?)\)$", raw, flags=re.I)
    if m:
        name = m.group(1).strip()
        if "action" in low_title:
            return f"Action - {name}"
        if "model" in low_title and "Weapon" in previous:
            return f"Model Player Weapon - {name}"
        return f"{page_title} - {name}"

    # Common player system headings.
    if low in {"buffs", "buff hashes", "character specific buffs", "debuffs", "ailments", "control types", "motions"}:
        return _clean_heading(raw).title()

    # Mechanics pages have useful small subheadings; keep them namespaced.
    if any(x in low_title for x in ["overmaster", "pwr", "terminus", "quest result"]):
        return f"{page_title} - {_clean_heading(raw)}"
    return None


def _guess_category_from_source(source: str) -> str:
    s = source.lower()
    if "entity_prefixes" in s or "entity-prefix" in s:
        return "Entity Prefix"
    if "quest" in s:
        return "Quest"
    if "model" in s:
        return "Model"
    if "phase" in s:
        return "Phase"
    if "buff" in s:
        return "Buff"
    if "debuff" in s or "ailment" in s:
        return "Debuff/Ailment"
    if "action" in s:
        return "Action"
    if "control" in s:
        return "Control Type"
    if "motion" in s:
        return "Motion"
    if "quest_result" in s:
        return "Quest Result Title"
    if "overmaster" in s:
        return "Overmastery"
    if "pwr" in s or "power" in s:
        return "PWR / Power"
    if "terminus" in s:
        return "Terminus Weapon Rolling"
    if "obj" in s:
        return "Obj"
    if "user_attributes" in s:
        return "User Attribute"
    return "Misc"
