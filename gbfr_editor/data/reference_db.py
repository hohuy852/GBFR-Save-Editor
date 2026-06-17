from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List
import csv


@dataclass(frozen=True)
class ReferenceEntry:
    category: str
    topic: str
    key: str
    value: str
    notes: str = ""
    source: str = ""

    @property
    def haystack(self) -> str:
        return " ".join([self.category, self.topic, self.key, self.value, self.notes, self.source]).lower()


class ReferenceDatabase:
    def __init__(self) -> None:
        self.entries: List[ReferenceEntry] = []

    def add(self, entry: ReferenceEntry) -> None:
        if not any([entry.category, entry.topic, entry.key, entry.value, entry.notes]):
            return
        # Avoid exact duplicate seed rows.
        sig = (entry.category, entry.topic, entry.key, entry.value, entry.notes, entry.source)
        for old in self.entries:
            if (old.category, old.topic, old.key, old.value, old.notes, old.source) == sig:
                return
        self.entries.append(entry)

    def merge(self, other: "ReferenceDatabase") -> None:
        for entry in other.entries:
            self.add(entry)

    def search(self, query: str = "", limit: int = 20000) -> List[ReferenceEntry]:
        q = (query or "").strip().lower()
        rows = sorted(self.entries, key=lambda e: (e.category.lower(), e.topic.lower(), e.key.lower(), e.value.lower()))
        if not q:
            return rows[:limit]
        toks = q.split()
        out: List[ReferenceEntry] = []
        for entry in rows:
            hay = entry.haystack
            if all(tok in hay for tok in toks):
                out.append(entry)
                if len(out) >= limit:
                    break
        return out

    @classmethod
    def load_many(cls, paths: Iterable[str | Path]) -> "ReferenceDatabase":
        db = cls()
        for path in paths:
            p = Path(path)
            if p.exists():
                db.merge(cls.load_csv(p))
        return db

    @classmethod
    def load_csv(cls, path: str | Path) -> "ReferenceDatabase":
        db = cls()
        with Path(path).open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                db.add(ReferenceEntry(
                    category=(row.get("category") or "").strip(),
                    topic=(row.get("topic") or "").strip(),
                    key=(row.get("key") or "").strip(),
                    value=(row.get("value") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                    source=(row.get("source") or "").strip(),
                ))
        return db

    def save_csv(self, path: str | Path) -> None:
        with Path(path).open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["category", "topic", "key", "value", "notes", "source"])
            writer.writeheader()
            for e in self.search(""):
                writer.writerow({
                    "category": e.category,
                    "topic": e.topic,
                    "key": e.key,
                    "value": e.value,
                    "notes": e.notes,
                    "source": e.source,
                })
