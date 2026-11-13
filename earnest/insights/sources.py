"""Getting transactions in: manual import or automated sync.

Manual:     import_file(...)        one CSV export you downloaded from your bank
Automated:  sync_folder(...)        picks up any new CSV dropped into an inbox folder
            watch_folder(...)       keeps syncing on an interval (or schedule sync_folder
                                    with cron / Windows Task Scheduler)
            sync_source(...)        pulls from a connector implementing TransactionSource
                                    (e.g. a future Plaid or bank-API connector)

Every path deduplicates: re-importing the same file, or overlapping exports
(Sep-Oct, then Oct-Nov), never double counts.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

from .statement import Transaction, parse_csv
from .store import TransactionStore


@dataclass
class ImportResult:
    name: str
    source: str
    added: int
    duplicates: int
    skipped: bool = False  # file already imported earlier
    error: str | None = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def import_file(store: TransactionStore, path: str | Path, user_id: str, source: str = "manual",
                card: str | None = None) -> ImportResult:
    """Manual import of one CSV export. `card` labels which card it's for (shadow mode uses it)."""
    p = Path(path)
    data = p.read_bytes()
    txs = parse_csv(data.decode("utf-8-sig"), user_id)
    added, dupes = store.add(user_id, txs, source, card)
    store.record_import(user_id, _hash(data), p.name, source, added, dupes)
    return ImportResult(p.name, source, added, dupes)


def sync_folder(store: TransactionStore, inbox: str | Path, user_id: str,
                card: str | None = None) -> list[ImportResult]:
    """Automated import: ingest every CSV in `inbox` that hasn't been imported yet.
    Files are left in place; a content hash remembers what's done."""
    folder = Path(inbox)
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    for p in sorted(folder.glob("*.csv")):
        data = p.read_bytes()
        if store.file_imported(user_id, _hash(data)):
            continue
        try:
            results.append(import_file(store, p, user_id, source="folder", card=card))
        except Exception as e:  # one bad file shouldn't stop the others
            results.append(ImportResult(p.name, "folder", 0, 0, error=str(e)))
    return results


def watch_folder(store: TransactionStore, inbox: str | Path, user_id: str, every_seconds: float = 3600,
                 on_result: Callable[[ImportResult], None] = print, max_runs: int | None = None,
                 card: str | None = None) -> None:
    """Keep syncing the inbox. Stop with Ctrl+C."""
    runs = 0
    while max_runs is None or runs < max_runs:
        for r in sync_folder(store, inbox, user_id, card):
            on_result(r)
        runs += 1
        if max_runs is None or runs < max_runs:
            time.sleep(every_seconds)


class TransactionSource(Protocol):
    """A connector that fetches transactions automatically (bank API, aggregator, ...)."""
    name: str

    def fetch(self, user_id: str, since: date | None) -> list[Transaction]: ...


def sync_source(store: TransactionStore, source: TransactionSource, user_id: str) -> ImportResult:
    existing = store.all(user_id)
    since = existing[-1].date if existing else None
    txs = source.fetch(user_id, since)
    added, dupes = store.add(user_id, txs, source.name)
    return ImportResult(source.name, source.name, added, dupes)
