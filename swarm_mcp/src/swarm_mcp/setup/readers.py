"""Find tables in a dataset folder and stream their rows, without loading big files.

A *table* is anything that yields rows (dicts):

| format   | files                                   | table key                    |
|----------|-----------------------------------------|------------------------------|
| jsonl    | ``.jsonl``/``.ndjson`` (optionally .gz)  | relative path                |
| json     | ``.json`` (.gz): an array of objects     | relative path                |
| json     | ``.json`` object holding arrays          | ``path#key`` per array       |
| csv/tsv  | ``.csv``/``.tsv`` (.gz)                  | relative path                |
| parquet  | ``.parquet`` (needs duckdb or pyarrow)   | relative path                |
| sqlite   | ``.db``/``.sqlite``/``.sqlite3``         | ``path#table`` per table     |

Docs (``.md``, ``.txt``, ``.rst``) are listed separately. Everything streams:
``Table.rows(limit, byte_budget)`` stops early, and ``Table.eof`` /
``Table.raw_pos`` tell the profiler how much of the file it saw (for row-count
estimates). CSV empty cells become ``None``.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import os
import sqlite3
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

DOC_SUFFIXES = {".md", ".markdown", ".txt", ".rst"}
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".swarmscope", ".cache", ".venv"}
JSON_OBJECT_MAX_BYTES = 64 * 1024 * 1024  # a .json *object* must be parsed whole; bigger ones are skipped
MAX_FILES = 5000

_csv_limit = sys.maxsize
while True:  # raise the CSV field limit as far as the platform allows (long message bodies)
    try:
        csv.field_size_limit(_csv_limit)
        break
    except OverflowError:
        _csv_limit //= 10


def _suffixes(path: Path) -> tuple[str, bool]:
    """(base suffix, gzipped) e.g. ('.jsonl', True) for x.jsonl.gz."""
    name = path.name.lower()
    gz = name.endswith(".gz")
    if gz:
        name = name[:-3]
    return os.path.splitext(name)[1], gz


def file_format(path: Path) -> str | None:
    suf, _ = _suffixes(path)
    if suf in (".jsonl", ".ndjson"):
        return "jsonl"
    if suf == ".json":
        return "json"
    if suf in (".csv", ".tsv"):
        return suf[1:]
    if suf in (".parquet", ".pq"):
        return "parquet"
    if suf in (".db", ".sqlite", ".sqlite3"):
        return "sqlite"
    if suf in DOC_SUFFIXES:
        return "doc"
    return None


def _gz_lines(raw, chunk: int = 32 * 1024, state: dict[str, int] | None = None) -> Iterator[bytes]:
    """Lines of a (multi-member) gzip stream, decompressed in small chunks so ``raw.tell()``
    stays close to what was actually consumed (``GzipFile`` reads far ahead)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    buf = b""
    while True:
        data = raw.read(chunk)
        if not data:
            break
        out = d.decompress(data)
        while d.eof and d.unused_data:  # next gzip member
            rest = d.unused_data
            d = zlib.decompressobj(16 + zlib.MAX_WBITS)
            out += d.decompress(rest)
        buf += out
        if state is not None:
            state["produced"] = state.get("produced", 0) + len(out)
        if b"\n" in buf:
            *done, buf = buf.split(b"\n")
            for line in done:
                yield line + b"\n"
    if buf:
        yield buf


def _open_binary(path: Path, gz: bool):
    raw = open(path, "rb")
    return raw, (gzip.GzipFile(fileobj=raw) if gz else raw)


@dataclass
class Table:
    key: str
    path: Path
    format: str
    table: str | None = None  # sqlite table name or json object key (dotted)
    gz: bool = False
    note: str = ""
    # filled while reading
    eof: bool = False
    raw_pos: int = 0
    bad_rows: int = 0
    total_rows: int | None = None  # exact count when cheaply known (sqlite, parquet, json object)
    _extra: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def rows(self, limit: int | None = None, byte_budget: int | None = None) -> Iterator[dict[str, Any]]:
        """Stream rows as dicts. Stops after ``limit`` rows or ``byte_budget`` decompressed bytes."""
        self.eof, self.raw_pos, self.bad_rows = False, 0, 0
        reader = getattr(self, f"_rows_{self.format}")
        n = 0
        for row in reader(byte_budget):
            if limit is not None and n >= limit:
                return
            n += 1
            yield row if isinstance(row, dict) else {"value": row}

    # ------------------------------------------------------------------ jsonl
    def _rows_jsonl(self, byte_budget: int | None) -> Iterator[Any]:
        raw = open(self.path, "rb")
        state: dict[str, int] = {}
        lines = _gz_lines(raw, state=state) if self.gz else raw
        seen = 0
        try:
            for line in lines:
                seen += len(line)
                # compressed bytes behind the lines consumed so far: chunk read-ahead scaled out
                pos = raw.tell()
                self.raw_pos = int(seen * pos / state["produced"]) if self.gz and state.get("produced") else pos
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        self.bad_rows += 1
                if byte_budget is not None and seen >= byte_budget:
                    return
            self.eof = True
        finally:
            raw.close()

    # ------------------------------------------------------------------ json
    def _rows_json(self, byte_budget: int | None) -> Iterator[Any]:
        if self.table is not None:  # an array inside a top-level object: parsed whole (size-capped at discovery)
            obj = _load_json(self.path, self.gz)
            for part in self.table.split("."):
                obj = obj.get(part) if isinstance(obj, dict) else None
            items = obj if isinstance(obj, list) else ([] if obj is None else [obj])
            self.total_rows = len(items)
            yield from items
            self.raw_pos, self.eof = self.size, True
            return
        raw, fh = _open_binary(self.path, self.gz)
        text = io.TextIOWrapper(fh, encoding="utf-8")
        try:
            yield from self._iter_array(text, raw, byte_budget)
        finally:
            text.close()
            raw.close()

    def _iter_array(self, text, raw, byte_budget: int | None) -> Iterator[Any]:
        dec = json.JSONDecoder()
        chunk = 1 << 20
        buf, pos, seen, done = "", 0, 0, False

        def more() -> bool:
            nonlocal buf, pos, seen, done
            data = text.read(chunk)
            self.raw_pos = raw.tell()
            if not data:
                done = True
                return False
            seen += len(data)
            buf, pos = buf[pos:] + data, 0
            return True

        more()
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n":
                pos += 1
            if pos < len(buf) or not more():
                break
        if pos >= len(buf):
            self.eof = True
            return
        if buf[pos] != "[":  # a single object: one row
            obj = json.loads(buf[pos:] + text.read())
            self.eof = True
            yield obj
            return
        pos += 1
        while True:
            while pos < len(buf) and buf[pos] in " \t\r\n,":
                pos += 1
            if pos >= len(buf):
                if not more():
                    self.eof = True
                    return
                continue
            if buf[pos] == "]":
                self.eof = True
                return
            try:
                obj, end = dec.raw_decode(buf, pos)
                if end >= len(buf) and not done and more():  # a scalar may continue in the next chunk
                    continue
            except ValueError:
                if not more():
                    raise
                continue
            pos = end
            yield obj
            if byte_budget is not None and seen >= byte_budget:
                return

    # ------------------------------------------------------------------ csv / tsv
    def _rows_csv(self, byte_budget: int | None, delimiter: str | None = None) -> Iterator[Any]:
        raw, fh = _open_binary(self.path, self.gz)
        text = io.TextIOWrapper(fh, encoding="utf-8-sig", newline="")
        try:
            if delimiter is None:
                head = text.read(64 * 1024)
                try:
                    delimiter = csv.Sniffer().sniff(head, delimiters=",;|\t").delimiter
                except csv.Error:
                    delimiter = ","
                text = _Prepend(head, text)
            seen = 0
            for row in csv.DictReader(text, delimiter=delimiter):
                self.raw_pos = raw.tell()
                seen += sum(len(v or "") for v in row.values() if isinstance(v, str)) + len(row)
                yield {k: (v if v != "" else None) for k, v in row.items() if k is not None}
                if byte_budget is not None and seen >= byte_budget:
                    return
            self.eof = True
        finally:
            text.close()
            raw.close()

    def _rows_tsv(self, byte_budget: int | None) -> Iterator[Any]:
        return self._rows_csv(byte_budget, delimiter="\t")

    # ------------------------------------------------------------------ sqlite
    def _rows_sqlite(self, byte_budget: int | None) -> Iterator[Any]:
        con = connect_sqlite(self.path)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(f"SELECT * FROM {_quote_ident(self.table or '')}")
            seen = 0
            for r in cur:
                d = dict(r)
                seen += sum(len(v) if isinstance(v, (str, bytes)) else 8 for v in d.values())
                yield d
                if byte_budget is not None and seen >= byte_budget:
                    return
            self.eof = True
        finally:
            con.close()

    # ------------------------------------------------------------------ parquet
    def _rows_parquet(self, byte_budget: int | None) -> Iterator[Any]:
        try:
            import duckdb  # type: ignore[import-not-found]
        except ImportError:
            duckdb = None
        if duckdb is not None:
            con = duckdb.connect()
            try:
                self.total_rows = con.execute("SELECT count(*) FROM read_parquet(?)", [str(self.path)]).fetchone()[0]
                cur = con.execute("SELECT * FROM read_parquet(?)", [str(self.path)])
                cols = [d[0] for d in cur.description]
                while True:
                    batch = cur.fetchmany(1000)
                    if not batch:
                        self.eof = True
                        return
                    for r in batch:
                        yield dict(zip(cols, r, strict=True))
            finally:
                con.close()
        try:
            import pyarrow.parquet as pq  # type: ignore[import-not-found]
        except ImportError:
            raise RuntimeError("reading .parquet needs duckdb or pyarrow (neither is installed)") from None
        f = pq.ParquetFile(self.path)
        self.total_rows = f.metadata.num_rows
        for batch in f.iter_batches(batch_size=1000):
            yield from batch.to_pylist()
        self.eof = True


class _Prepend(io.TextIOBase):
    """A text stream that replays ``head`` before continuing with ``rest``."""

    def __init__(self, head: str, rest):
        self._head, self._rest = head, rest

    def read(self, n: int = -1) -> str:
        if self._head:
            if n is None or n < 0:
                out, self._head = self._head + self._rest.read(), ""
                return out
            out, self._head = self._head[:n], self._head[n:]
            return out
        return self._rest.read(n)

    def readline(self, limit: int = -1) -> str:
        if self._head:
            i = self._head.find("\n")
            if i >= 0:
                out, self._head = self._head[: i + 1], self._head[i + 1 :]
                return out
            out, self._head = self._head, ""
            return out + self._rest.readline()
        return self._rest.readline()

    def __iter__(self):
        while True:
            line = self.readline()
            if not line:
                return
            yield line

    def close(self) -> None:
        self._rest.close()


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def connect_sqlite(path: Path) -> sqlite3.Connection:
    """Read-only connection (never creates or modifies the file)."""
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _load_json(path: Path, gz: bool) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") if gz else open(path, encoding="utf-8") as f:
        return json.load(f)


def _first_char(path: Path, gz: bool) -> str:
    raw, fh = _open_binary(path, gz)
    try:
        head = fh.read(4096).decode("utf-8", errors="ignore").lstrip("﻿ \t\r\n")
        return head[:1]
    finally:
        fh.close()
        raw.close()


def _array_keys(obj: dict[str, Any], prefix: str = "", depth: int = 0) -> list[tuple[str, int]]:
    """Dotted keys of arrays (of objects) inside a JSON object, up to two levels down."""
    out = []
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, list) and v and isinstance(v[0], dict):
            out.append((key, len(v)))
        elif isinstance(v, dict) and depth < 1:
            out.extend(_array_keys(v, key + ".", depth + 1))
    return out


def sqlite_tables(path: Path) -> list[str]:
    con = connect_sqlite(path)
    try:
        return [
            r[0]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
    finally:
        con.close()


def sqlite_info(path: Path, table: str) -> dict[str, Any]:
    """Columns, declared foreign keys and a row count (exact for small files, max(rowid) otherwise)."""
    con = connect_sqlite(path)
    try:
        q = _quote_ident(table)
        cols = [{"name": r[1], "type": r[2], "pk": bool(r[5])} for r in con.execute(f"PRAGMA table_info({q})")]
        fks = [{"from": r[3], "table": r[2], "to": r[4]} for r in con.execute(f"PRAGMA foreign_key_list({q})")]
        rows: int | None
        try:
            if path.stat().st_size < 256 * 1024 * 1024:
                rows = con.execute(f"SELECT count(*) FROM {q}").fetchone()[0]
            else:
                rows = con.execute(f"SELECT max(rowid) FROM {q}").fetchone()[0]
        except sqlite3.Error:
            rows = None
        return {"columns": cols, "foreign_keys": fks, "rows": rows}
    finally:
        con.close()


def _table_for(root: Path, path: Path) -> tuple[list[Table], dict[str, Any] | None]:
    """Tables in one file, or a skip record."""
    rel = path.relative_to(root).as_posix() if path != root else path.name
    fmt = file_format(path)
    _, gz = _suffixes(path)
    if fmt is None:
        return [], {"path": rel, "reason": "unrecognised file type"}
    if fmt == "sqlite":
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return [], {"path": rel, "reason": "has a database suffix but is not SQLite"}
        try:
            return [Table(f"{rel}#{t}", path, "sqlite", table=t) for t in sqlite_tables(path)], None
        except sqlite3.Error as e:
            return [], {"path": rel, "reason": f"sqlite error: {e}"}
    if fmt == "json":
        try:
            first = _first_char(path, gz)
        except OSError as e:
            return [], {"path": rel, "reason": f"unreadable: {e}"}
        if first == "{":
            if path.stat().st_size > JSON_OBJECT_MAX_BYTES:
                return [], {
                    "path": rel,
                    "reason": "JSON object larger than 64 MB: convert it to JSONL (one record per line) to profile it",
                }
            try:
                obj = _load_json(path, gz)
            except ValueError as e:
                return [], {"path": rel, "reason": f"invalid JSON: {e}"}
            keys = _array_keys(obj) if isinstance(obj, dict) else []
            if keys:
                return [Table(f"{rel}#{k}", path, "json", table=k, gz=gz, total_rows=n) for k, n in keys], None
            return [Table(rel, path, "json", gz=gz, note="a single JSON object (one row)")], None
        return [Table(rel, path, "json", gz=gz)], None
    return [Table(rel, path, fmt, gz=gz)], None


def discover(root: Path) -> tuple[list[Table], list[dict[str, Any]], list[dict[str, Any]]]:
    """Walk ``root`` (a file or folder): (tables, docs, skipped). Hidden and cache folders are skipped."""
    root = Path(root)
    files: list[Path]
    base = root
    if root.is_file():
        files, base = [root], root.parent
    else:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
            for fn in sorted(filenames):
                if not fn.startswith("."):
                    files.append(Path(dirpath) / fn)
            if len(files) > MAX_FILES:
                break
    tables: list[Table] = []
    docs: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for f in files[:MAX_FILES]:
        rel = f.relative_to(base).as_posix()
        if file_format(f) == "doc":
            docs.append({"path": rel, "bytes": f.stat().st_size, "title": _doc_title(f)})
            continue
        found, skip = _table_for(base, f)
        tables.extend(found)
        if skip:
            skip["bytes"] = f.stat().st_size
            skipped.append(skip)
    if len(files) > MAX_FILES:
        skipped.append({"path": "…", "reason": f"stopped after {MAX_FILES} files"})
    return tables, docs, skipped


def _doc_title(path: Path) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for _, line in zip(range(40), f, strict=False):
                line = line.strip()
                if line:
                    return line.lstrip("#").strip()[:80]
    except OSError:
        pass
    return ""


def find_tables(root: Path, pattern: str) -> list[Table]:
    """Tables whose key matches ``pattern`` exactly, or as a glob (``logs/*.jsonl``, ``forum.db#*``)."""
    from fnmatch import fnmatchcase

    tables, _, _ = discover(root)
    exact = [t for t in tables if t.key == pattern]
    if exact:
        return exact
    return [t for t in tables if fnmatchcase(t.key, pattern)]
