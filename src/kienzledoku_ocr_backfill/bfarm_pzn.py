#!/usr/bin/env python3
"""BfArM PZN resolver for the §31b SGB V reference database.

Standard-library only. Can be used as CLI or imported as a module.

CLI examples:
    python3 bfarm_pzn.py update --source-zip BfArM-Lieferung.zip --db bfarm_pzn.sqlite
    python3 bfarm_pzn.py lookup 09322739 09531845 --db bfarm_pzn.sqlite

Integration example:
    from bfarm_pzn import PZNResolver
    resolver = PZNResolver("bfarm_pzn.sqlite")
    drug = resolver.lookup("9322739")
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html.parser
import json
import os
import re
import shutil
import sqlite3
import ssl
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Sequence

__version__ = "1.0.1"

DEFAULT_DB = "bfarm_pzn.sqlite"
DEFAULT_PAGE_URLS = (
    "https://www.bfarm.de/DE/Arzneimittel/Arzneimittelinformationen/Referenzdatenbank/_artikel.html",
    "https://www.bfarm.de/DE/Arzneimittel/Arzneimittelinformationen/Referenzdatenbank/_node.html",
)
USER_AGENT = f"bfarm-pzn-resolver/{__version__} (+Python urllib)"

KINDS = (
    "REFERENCE_MEDICINAL_PRODUCT",
    "REFERENCE_PHARMACEUTICAL_PRODUCT",
    "REFERENCE_SUBSTANCE",
)
FILE_RE = re.compile(
    r"(?P<date>\d{8})-(?P<kind>REFERENCE_(?:MEDICINAL_PRODUCT|PHARMACEUTICAL_PRODUCT|SUBSTANCE))\.dsv",
    re.IGNORECASE,
)


class BfArMError(RuntimeError):
    """Base exception for downloader/import errors."""


class DownloadError(BfArMError):
    pass


class ImportFormatError(BfArMError):
    pass


@dataclass(frozen=True)
class Release:
    date: str
    urls: Mapping[str, str]


class _LinkParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        d = dict(attrs)
        self._href = d.get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None
            self._text = []


def _request(url: str, timeout: float = 45.0) -> urllib.response.addinfourl:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/octet-stream,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        },
    )
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        raise DownloadError(f"Abruf fehlgeschlagen: {url}: {exc}") from exc


def _fetch_text(url: str, timeout: float = 45.0) -> tuple[str, str]:
    with _request(url, timeout=timeout) as response:
        data = response.read()
        final_url = response.geturl()
        content_type = response.headers.get_content_charset() or "utf-8"
    try:
        return data.decode(content_type, errors="replace"), final_url
    except LookupError:
        return data.decode("utf-8", errors="replace"), final_url


def _candidate_from_link(base_url: str, href: str, text: str) -> tuple[str, str, str] | None:
    """Return (date, kind, absolute_url) if href/text clearly identifies a DSV."""
    absolute = urllib.parse.urljoin(base_url, href)
    decoded = urllib.parse.unquote(absolute)
    for haystack in (decoded, text):
        match = FILE_RE.search(haystack)
        if match:
            return match.group("date"), match.group("kind").upper(), absolute
    return None


def discover_latest_release(page_urls: Sequence[str] | None = None, timeout: float = 45.0) -> Release:
    """Discover the newest complete set of the three official BfArM DSV files.

    The BfArM page layout may change. Discovery therefore looks at both hrefs
    and visible link text and only accepts a release date when all three files
    are present.
    """
    urls = tuple(page_urls or DEFAULT_PAGE_URLS)
    errors: list[str] = []

    for page_url in urls:
        try:
            body, final_page_url = _fetch_text(page_url, timeout=timeout)
        except DownloadError as exc:
            errors.append(str(exc))
            continue

        parser = _LinkParser()
        parser.feed(body)
        releases: dict[str, dict[str, str]] = {}

        for href, text in parser.links:
            candidate = _candidate_from_link(final_page_url, href, text)
            if candidate:
                date, kind, absolute = candidate
                releases.setdefault(date, {})[kind] = absolute

        complete = [date for date, found in releases.items() if all(k in found for k in KINDS)]
        if complete:
            newest = max(complete)
            return Release(newest, {k: releases[newest][k] for k in KINDS})

        errors.append(f"Keine vollständige DSV-Lieferung auf {page_url} gefunden")

    raise DownloadError(
        "Die aktuelle BfArM-Lieferung konnte nicht automatisch ermittelt werden. "
        "Das BfArM veröffentlicht auf seiner Referenzdatenbank-Seite keine "
        "direkten DSV-Downloadlinks, sondern stellt die vollständige ZIP-Lieferung "
        "nach Kontaktaufnahme über Referenzdaten@bfarm.de bereit. Importiere diese "
        "Datei mit --source-zip; alternativ sind --source-dir oder die drei "
        "--*-url Optionen möglich. Details: " + " | ".join(errors)
    )


def _download(url: str, destination: Path, timeout: float = 90.0) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(destination.suffix + ".part")
    try:
        with _request(url, timeout=timeout) as response, tmp.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        os.replace(tmp, destination)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        finally:
            raise
    return destination


def _release_from_explicit_urls(urls: Mapping[str, str]) -> Release:
    if set(urls) != set(KINDS):
        raise DownloadError("Für explizite URLs müssen alle drei BfArM-Dateien angegeben werden.")
    dates: set[str] = set()
    for kind, url in urls.items():
        m = FILE_RE.search(urllib.parse.unquote(url))
        if m and m.group("kind").upper() == kind:
            dates.add(m.group("date"))
    date = dates.pop() if len(dates) == 1 else "unknown"
    return Release(date, dict(urls))


def download_release(release: Release, target_dir: str | os.PathLike[str], timeout: float = 90.0) -> dict[str, Path]:
    target = Path(target_dir)
    result: dict[str, Path] = {}
    for kind in KINDS:
        url = release.urls[kind]
        match = FILE_RE.search(urllib.parse.unquote(url))
        filename = match.group(0) if match else f"{release.date}-{kind}.dsv"
        path = target / filename
        _download(url, path, timeout=timeout)
        result[kind] = path
    return result


def find_release_files(source_dir: str | os.PathLike[str]) -> tuple[str, dict[str, Path]]:
    """Find newest complete release in a local directory."""
    source = Path(source_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"Kein Verzeichnis: {source}")
    releases: dict[str, dict[str, Path]] = {}
    for path in source.iterdir():
        if not path.is_file():
            continue
        m = FILE_RE.search(path.name)
        if not m:
            continue
        releases.setdefault(m.group("date"), {})[m.group("kind").upper()] = path
    complete = [date for date, files in releases.items() if all(k in files for k in KINDS)]
    if not complete:
        raise ImportFormatError(
            f"In {source} wurde keine vollständige Lieferung mit allen drei DSV-Dateien gefunden."
        )
    newest = max(complete)
    return newest, {k: releases[newest][k] for k in KINDS}


def extract_release_zip(
    source_zip: str | os.PathLike[str], target_dir: str | os.PathLike[str]
) -> tuple[str, dict[str, Path]]:
    """Safely extract the newest complete DSV release from an official ZIP."""
    source = Path(source_zip)
    if not source.is_file():
        raise FileNotFoundError(f"Keine ZIP-Datei: {source}")
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    releases: dict[str, dict[str, zipfile.ZipInfo]] = {}
    try:
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    continue
                filename = Path(member.filename).name
                match = FILE_RE.fullmatch(filename)
                if match is None:
                    continue
                date = match.group("date")
                kind = match.group("kind").upper()
                found = releases.setdefault(date, {})
                if kind in found:
                    raise ImportFormatError(
                        f"{source.name}: DSV-Datei für {date}/{kind} ist mehrfach enthalten"
                    )
                found[kind] = member

            complete = [
                date
                for date, members in releases.items()
                if all(kind in members for kind in KINDS)
            ]
            if not complete:
                raise ImportFormatError(
                    f"{source.name}: keine vollständige Lieferung mit allen drei "
                    "DSV-Dateien gefunden"
                )
            newest = max(complete)
            result: dict[str, Path] = {}
            for kind in KINDS:
                member = releases[newest][kind]
                destination = target / Path(member.filename).name
                with archive.open(member) as input_file, destination.open(
                    "xb"
                ) as output_file:
                    shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                result[kind] = destination
    except zipfile.BadZipFile as exc:
        raise ImportFormatError(f"Ungültige ZIP-Datei {source}: {exc}") from exc
    except RuntimeError as exc:
        raise ImportFormatError(
            f"ZIP-Datei {source} konnte nicht gelesen werden: {exc}"
        ) from exc
    return newest, result


def normalize_pzn(value: str | int) -> str:
    """Normalize BMP/BfArM PZN to 8 digits (e.g. 9322739 -> 09322739)."""
    text = str(value).strip().upper()
    if text.startswith("PZN"):
        text = text[3:].strip(" :-")
    digits = re.sub(r"[\s.-]", "", text)
    if not digits.isdigit():
        raise ValueError(f"Ungültige PZN: {value!r}")
    if not 1 <= len(digits) <= 8:
        raise ValueError(f"PZN muss 1 bis 8 Ziffern enthalten: {value!r}")
    return digits.zfill(8)


def _open_dsv(path: Path):
    # utf-8-sig tolerates a BOM and is otherwise equivalent to UTF-8.
    return path.open("r", encoding="utf-8-sig", newline="")


def _reader(path: Path) -> csv.DictReader:
    handle = _open_dsv(path)
    reader = csv.DictReader(handle, delimiter="|")
    # Keep a reference so callers can close it reliably.
    setattr(reader, "_bfarm_handle", handle)
    return reader


def _close_reader(reader: csv.DictReader) -> None:
    handle = getattr(reader, "_bfarm_handle", None)
    if handle:
        handle.close()


def _validate_columns(path: Path, fieldnames: Sequence[str] | None, required: Iterable[str]) -> None:
    names = set(fieldnames or ())
    missing = [name for name in required if name not in names]
    if missing:
        raise ImportFormatError(
            f"{path.name}: erwartete Spalten fehlen: {', '.join(missing)}; "
            f"vorhanden: {', '.join(fieldnames or ())}"
        )


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _first(row: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and value != "":
            return value
    return None


def build_database(
    files: Mapping[str, Path],
    db_path: str | os.PathLike[str],
    release_date: str = "unknown",
    source_urls: Mapping[str, str] | None = None,
) -> dict[str, int]:
    """Build an atomic normalized SQLite database from the three BfArM DSV files."""
    for kind in KINDS:
        if kind not in files:
            raise ImportFormatError(f"Datei fehlt: {kind}")

    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    tmp = db.with_name(db.name + f".tmp-{os.getpid()}")
    tmp.unlink(missing_ok=True)

    counts = {"products": 0, "components": 0, "substances": 0}
    con = sqlite3.connect(tmp)
    try:
        con.executescript(
            """
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            PRAGMA temp_store=MEMORY;
            PRAGMA foreign_keys=OFF;

            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE products (
                pzn TEXT PRIMARY KEY,
                raw_pzn TEXT NOT NULL,
                rmp_key TEXT NOT NULL,
                name TEXT,
                form_short TEXT,
                form_long TEXT,
                form_bfarm TEXT,
                form_term_id TEXT,
                substance_count INTEGER,
                multiple_ppt INTEGER
            );

            CREATE TABLE components (
                rpp_key TEXT PRIMARY KEY,
                rmp_key TEXT NOT NULL,
                component_number INTEGER,
                form_short TEXT,
                form_long TEXT,
                form_bfarm TEXT,
                form_term_id TEXT,
                description TEXT
            );

            CREATE TABLE substances (
                rse_key TEXT PRIMARY KEY,
                rpp_key TEXT NOT NULL,
                name TEXT,
                strength TEXT,
                substance_id TEXT,
                rank INTEGER
            );
            """
        )

        # Products
        path = Path(files["REFERENCE_MEDICINAL_PRODUCT"])
        reader = _reader(path)
        try:
            _validate_columns(path, reader.fieldnames, ("RMP_KEY", "RMP_PZN", "RMP_MPD_NAME"))
            batch = []
            for row in reader:
                raw = (row.get("RMP_PZN") or "").strip()
                if not raw:
                    continue
                try:
                    pzn = normalize_pzn(raw)
                except ValueError as exc:
                    raise ImportFormatError(f"{path.name}: ungültige PZN {raw!r}") from exc
                batch.append(
                    (
                        pzn,
                        raw,
                        (row.get("RMP_KEY") or raw).strip(),
                        _first(row, "RMP_MPD_NAME"),
                        _first(row, "RMP_PFM_PUT_SHORT"),
                        _first(row, "RMP_PFM_PUT_LONG"),
                        _first(row, "RMP_PFM_NAME"),
                        _first(row, "RMP_PFM_TERM_ID"),
                        _as_int(_first(row, "RMP_COUNT_SUBSTANCE")),
                        _as_int(_first(row, "RMP_MULTIPLE_PPT", "RMP_IS_SYSTEMPACK")),
                    )
                )
                if len(batch) >= 10000:
                    con.executemany("INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
                    counts["products"] += len(batch)
                    batch.clear()
            if batch:
                con.executemany("INSERT OR REPLACE INTO products VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
                counts["products"] += len(batch)
        finally:
            _close_reader(reader)

        # Components
        path = Path(files["REFERENCE_PHARMACEUTICAL_PRODUCT"])
        reader = _reader(path)
        try:
            _validate_columns(path, reader.fieldnames, ("RPP_KEY", "RMP_KEY"))
            batch = []
            for row in reader:
                rpp_key = (row.get("RPP_KEY") or "").strip()
                rmp_key = (row.get("RMP_KEY") or "").strip()
                if not rpp_key or not rmp_key:
                    continue
                batch.append(
                    (
                        rpp_key,
                        rmp_key,
                        _as_int(_first(row, "RPP_NUMBER")),
                        _first(row, "RPP_PFM_PUT_SHORT"),
                        _first(row, "RPP_PFM_PUT_LONG"),
                        _first(row, "RPP_PFM_NAME"),
                        _first(row, "RPP_PFM_TERM_ID"),
                        _first(row, "RPP_DESCRIPTION"),
                    )
                )
                if len(batch) >= 10000:
                    con.executemany("INSERT OR REPLACE INTO components VALUES (?,?,?,?,?,?,?,?)", batch)
                    counts["components"] += len(batch)
                    batch.clear()
            if batch:
                con.executemany("INSERT OR REPLACE INTO components VALUES (?,?,?,?,?,?,?,?)", batch)
                counts["components"] += len(batch)
        finally:
            _close_reader(reader)

        # Substances
        path = Path(files["REFERENCE_SUBSTANCE"])
        reader = _reader(path)
        try:
            _validate_columns(path, reader.fieldnames, ("RPP_KEY", "RSE_SUBSTANCE_NAME"))
            batch = []
            synthetic = 0
            for row in reader:
                rpp_key = (row.get("RPP_KEY") or "").strip()
                if not rpp_key:
                    continue
                rse_key = (row.get("RSE_KEY") or "").strip()
                if not rse_key:
                    synthetic += 1
                    rse_key = f"{rpp_key}-synthetic-{synthetic}"
                batch.append(
                    (
                        rse_key,
                        rpp_key,
                        _first(row, "RSE_SUBSTANCE_NAME"),
                        _first(row, "RSE_SUBSTANCE_STRENGTH"),
                        _first(row, "RSE_SUBSTANCE_ID"),
                        _as_int(_first(row, "RSE_SUBSTANCE_RANK")),
                    )
                )
                if len(batch) >= 10000:
                    con.executemany("INSERT OR REPLACE INTO substances VALUES (?,?,?,?,?,?)", batch)
                    counts["substances"] += len(batch)
                    batch.clear()
            if batch:
                con.executemany("INSERT OR REPLACE INTO substances VALUES (?,?,?,?,?,?)", batch)
                counts["substances"] += len(batch)
        finally:
            _close_reader(reader)

        con.executescript(
            """
            CREATE INDEX idx_products_rmp_key ON products(rmp_key);
            CREATE INDEX idx_components_rmp_key ON components(rmp_key);
            CREATE INDEX idx_substances_rpp_key ON substances(rpp_key);
            CREATE INDEX idx_substances_name ON substances(name);
            """
        )

        meta = {
            "schema_version": "1",
            "resolver_version": __version__,
            "release_date": release_date,
            "built_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": "BfArM Referenzdatenbank Fertigarzneimittel §31b SGB V",
        }
        if source_urls:
            for kind, url in source_urls.items():
                meta[f"url_{kind.lower()}"] = url
        con.executemany("INSERT INTO metadata(key,value) VALUES (?,?)", meta.items())
        con.commit()
        con.execute("PRAGMA optimize")
        con.close()
        con = None  # type: ignore[assignment]
        os.replace(tmp, db)
        return counts
    except Exception:
        if con is not None:
            con.close()
        tmp.unlink(missing_ok=True)
        raise


class PZNResolver:
    """Thread-friendly read-only resolver for the generated SQLite database."""

    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB):
        self.db_path = str(db_path)
        self._local = threading.local()

    def _connection(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            uri = Path(self.db_path).resolve().as_uri() + "?mode=ro"
            con = sqlite3.connect(uri, uri=True)
            con.row_factory = sqlite3.Row
            self._local.con = con
        return con

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None

    def __enter__(self) -> "PZNResolver":
        self._connection()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def metadata(self) -> dict[str, str]:
        rows = self._connection().execute("SELECT key,value FROM metadata ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def lookup(self, pzn: str | int) -> dict | None:
        norm = normalize_pzn(pzn)
        con = self._connection()
        product = con.execute("SELECT * FROM products WHERE pzn=?", (norm,)).fetchone()
        if product is None:
            return None

        component_rows = con.execute(
            "SELECT * FROM components WHERE rmp_key=? ORDER BY component_number, rpp_key",
            (product["rmp_key"],),
        ).fetchall()

        components: list[dict] = []
        flat_substances: list[dict] = []
        for c in component_rows:
            substance_rows = con.execute(
                "SELECT name,strength,substance_id,rank FROM substances "
                "WHERE rpp_key=? ORDER BY rank, rse_key",
                (c["rpp_key"],),
            ).fetchall()
            substances = [dict(s) for s in substance_rows]
            flat_substances.extend(substances)
            components.append(
                {
                    "number": c["component_number"],
                    "form_short": c["form_short"],
                    "form_long": c["form_long"],
                    "form_bfarm": c["form_bfarm"],
                    "form_term_id": c["form_term_id"],
                    "description": c["description"],
                    "substances": substances,
                }
            )

        return {
            "pzn": product["pzn"],
            "name": product["name"],
            "form_short": product["form_short"],
            "form_long": product["form_long"],
            "form_bfarm": product["form_bfarm"],
            "form_term_id": product["form_term_id"],
            "substance_count": product["substance_count"],
            "multiple_ppt": bool(product["multiple_ppt"]) if product["multiple_ppt"] is not None else None,
            "substances": flat_substances,
            "components": components,
        }

    def lookup_many(self, pzns: Iterable[str | int]) -> dict[str, dict | None]:
        return {normalize_pzn(pzn): self.lookup(pzn) for pzn in pzns}

    def exists(self, pzn: str | int) -> bool:
        norm = normalize_pzn(pzn)
        row = self._connection().execute("SELECT 1 FROM products WHERE pzn=?", (norm,)).fetchone()
        return row is not None


def update_database(
    db_path: str | os.PathLike[str] = DEFAULT_DB,
    *,
    source_dir: str | os.PathLike[str] | None = None,
    source_zip: str | os.PathLike[str] | None = None,
    page_url: str | None = None,
    explicit_urls: Mapping[str, str] | None = None,
    keep_downloads: str | os.PathLike[str] | None = None,
    timeout: float = 90.0,
) -> dict:
    """Download/import the newest BfArM release and return a status dict."""
    if source_dir and source_zip:
        raise ValueError("source_dir und source_zip können nicht kombiniert werden")
    if source_dir:
        release_date, files = find_release_files(source_dir)
        counts = build_database(files, db_path, release_date=release_date)
        return {
            "release_date": release_date,
            "db": str(db_path),
            "counts": counts,
            "downloaded": False,
            "source": str(Path(source_dir)),
        }
    if source_zip:
        with tempfile.TemporaryDirectory(prefix="bfarm-pzn-zip-") as tmpdir:
            release_date, files = extract_release_zip(source_zip, tmpdir)
            counts = build_database(files, db_path, release_date=release_date)
        return {
            "release_date": release_date,
            "db": str(db_path),
            "counts": counts,
            "downloaded": False,
            "source": str(Path(source_zip)),
        }

    release = (
        _release_from_explicit_urls(explicit_urls)
        if explicit_urls
        else discover_latest_release((page_url,) if page_url else None, timeout=min(timeout, 45.0))
    )

    if keep_downloads:
        target_dir = Path(keep_downloads)
        target_dir.mkdir(parents=True, exist_ok=True)
        files = download_release(release, target_dir, timeout=timeout)
        counts = build_database(files, db_path, release_date=release.date, source_urls=release.urls)
    else:
        with tempfile.TemporaryDirectory(prefix="bfarm-pzn-") as tmpdir:
            files = download_release(release, tmpdir, timeout=timeout)
            counts = build_database(files, db_path, release_date=release.date, source_urls=release.urls)

    return {"release_date": release.date, "db": str(db_path), "counts": counts, "downloaded": True}


def _print_human(drug: dict | None, requested: str) -> None:
    if drug is None:
        print(f"{normalize_pzn(requested)}: kein Treffer")
        return
    print(f"PZN:       {drug['pzn']}")
    print(f"Name:      {drug['name'] or '-'}")
    print(f"Form:      {drug['form_long'] or drug['form_short'] or '-'}")
    if drug["substances"]:
        print("Wirkstoff:")
        for s in drug["substances"]:
            strength = f" — {s['strength']}" if s.get("strength") else ""
            print(f"  - {s.get('name') or '-'}{strength}")
    if len(drug["components"]) > 1:
        print(f"Komponenten: {len(drug['components'])}")


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="BfArM-§31b-Lieferung importieren und PZN lokal auflösen"
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_update = sub.add_parser(
        "update", help="BfArM-Lieferung importieren und SQLite-Datenbank bauen"
    )
    p_update.add_argument("--db", default=DEFAULT_DB, help=f"SQLite-Zieldatei (Standard: {DEFAULT_DB})")
    source = p_update.add_mutually_exclusive_group()
    source.add_argument(
        "--source-zip",
        help="Vom BfArM gelieferte ZIP-Datei mit den drei DSV-Dateien importieren",
    )
    source.add_argument(
        "--source-dir",
        help="Statt Download: Verzeichnis mit den drei DSV-Dateien importieren",
    )
    p_update.add_argument("--page-url", help="Abweichende BfArM-Seite für Auto-Discovery")
    p_update.add_argument("--medicinal-url", help="Explizite URL für REFERENCE_MEDICINAL_PRODUCT")
    p_update.add_argument("--pharmaceutical-url", help="Explizite URL für REFERENCE_PHARMACEUTICAL_PRODUCT")
    p_update.add_argument("--substance-url", help="Explizite URL für REFERENCE_SUBSTANCE")
    p_update.add_argument("--keep-downloads", help="DSV-Dateien dauerhaft in diesem Verzeichnis behalten")
    p_update.add_argument("--timeout", type=float, default=90.0)

    p_lookup = sub.add_parser("lookup", help="Eine oder mehrere PZN auflösen")
    p_lookup.add_argument("pzn", nargs="+")
    p_lookup.add_argument("--db", default=DEFAULT_DB)
    p_lookup.add_argument("--json", action="store_true", help="JSON ausgeben")

    p_info = sub.add_parser("info", help="Metadaten der lokalen Datenbank anzeigen")
    p_info.add_argument("--db", default=DEFAULT_DB)

    args = parser.parse_args(argv)

    try:
        if args.command == "update":
            explicit = None
            supplied = [args.medicinal_url, args.pharmaceutical_url, args.substance_url]
            if any(supplied):
                if not all(supplied):
                    parser.error("Bei expliziten URLs müssen alle drei --*-url Optionen gesetzt sein.")
                explicit = {
                    "REFERENCE_MEDICINAL_PRODUCT": args.medicinal_url,
                    "REFERENCE_PHARMACEUTICAL_PRODUCT": args.pharmaceutical_url,
                    "REFERENCE_SUBSTANCE": args.substance_url,
                }
            result = update_database(
                args.db,
                source_dir=args.source_dir,
                source_zip=args.source_zip,
                page_url=args.page_url,
                explicit_urls=explicit,
                keep_downloads=args.keep_downloads,
                timeout=args.timeout,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0

        if args.command == "lookup":
            with PZNResolver(args.db) as resolver:
                if args.json:
                    result = resolver.lookup_many(args.pzn)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                else:
                    for i, pzn in enumerate(args.pzn):
                        if i:
                            print()
                        _print_human(resolver.lookup(pzn), pzn)
            return 0

        if args.command == "info":
            with PZNResolver(args.db) as resolver:
                print(json.dumps(resolver.metadata(), ensure_ascii=False, indent=2))
            return 0

    except (BfArMError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
