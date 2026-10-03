"""The context object handed to every module: config, shared cache, logger,
plus a ``tool`` decorator that applies the naming/error conventions."""

from __future__ import annotations

import inspect
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar

from swarm_mcp import sdk
from swarm_mcp.config import Config
from swarm_mcp.events import EventSource, EventSources, make_event_id
from swarm_mcp.toolkit import Scrubber, clamp_limit, untrusted, wrap_tool

T = TypeVar("T")


class LazyCache:
    """Thread-safe compute-once cache shared by all modules.

    ``cache.get(key, loader)`` calls ``loader()`` the first time and returns
    the stored value afterwards. Concurrent first calls for the same key wait
    for a single load. Keys are plain strings; ``ModuleContext.lazy`` prefixes
    them with the module name so modules don't collide.
    """

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._meta: dict[str, float] = {}
        self._guard = threading.Lock()

    def get(self, key: str, loader: Callable[[], T]) -> T:
        if key in self._values:
            return self._values[key]
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if key not in self._values:
                t0 = time.perf_counter()
                self._values[key] = loader()
                self._meta[key] = round(time.perf_counter() - t0, 3)
                logging.getLogger("swarm_mcp.cache").info("loaded %s in %.2fs", key, self._meta[key])
        return self._values[key]

    def __contains__(self, key: str) -> bool:
        return key in self._values

    def clear(self, prefix: str = "") -> int:
        with self._guard:
            keys = [k for k in self._values if k.startswith(prefix)]
            for k in keys:
                self._values.pop(k, None)
                self._meta.pop(k, None)
        return len(keys)

    def stats(self) -> dict[str, Any]:
        return {"entries": {k: {"load_seconds": v} for k, v in sorted(self._meta.items())}}


@dataclass
class ModuleRecord:
    """What the server knows about one discovered module."""

    name: str
    description: str = ""
    source: str = ""
    status: str = "pending"  # loaded | skipped
    reasons: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    load_ms: float | None = None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "tools": self.tools,
        }
        if self.reasons:
            d["reasons"] = self.reasons
        if self.resources:
            d["resources"] = self.resources
        if self.prompts:
            d["prompts"] = self.prompts
        if self.warnings:
            d["warnings"] = self.warnings
        if self.load_ms is not None:
            d["load_ms"] = self.load_ms
        return d


class Registry:
    """Ordered record of every module discovery saw (loaded or skipped)."""

    def __init__(self) -> None:
        self.records: dict[str, ModuleRecord] = {}
        self.notes: list[str] = []  # server-level notes, e.g. unknown module names
        self.events = EventSources()  # event_id sources registered by modules (see events.py)

    def add(self, rec: ModuleRecord) -> ModuleRecord:
        self.records[rec.name] = rec
        return rec

    @property
    def loaded(self) -> list[ModuleRecord]:
        return [r for r in self.records.values() if r.status == "loaded"]

    @property
    def skipped(self) -> list[ModuleRecord]:
        return [r for r in self.records.values() if r.status == "skipped"]


@dataclass
class ModuleContext:
    """Passed to ``requires(ctx)`` and ``register(mcp, ctx)``.

    Attributes:
        name: the module's NAME (also the tool-name prefix).
        config: global ``Config`` (data dir, limits, privacy settings).
        cache: the shared ``LazyCache``.
        log: a stderr logger named ``swarm_mcp.<name>``.
        registry: all module records (used by ``core``).
        scrub: a ``Scrubber`` configured from the privacy settings.

    Helpers: ``tool``/``resource``/``prompt`` (registration), ``lazy`` (cache),
    ``limit`` (result limits), ``untrusted`` (wrap dataset text), ``store`` (DuckDB).
    """

    name: str
    config: Config
    cache: LazyCache
    log: logging.Logger
    registry: Registry
    mcp: sdk.App | None = None
    scrub: Scrubber = field(default_factory=Scrubber)

    # -- conveniences ---------------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.config.data_dir

    def setting(self, key: str, default: str | None = None) -> str | None:
        """Per-module env setting ``SWARM_<NAME>_<KEY>``."""
        return self.config.module_setting(self.name, key, default)

    def lazy(self, key: str, loader: Callable[[], T]) -> T:
        """Module-namespaced ``cache.get``."""
        return self.cache.get(f"{self.name}:{key}", loader)

    def event_id(self, kind: str, local_id: str, source: str | None = None) -> str:
        """Build an event id owned by this module: ``<source or NAME>:<kind>:<local_id>``."""
        return make_event_id(source or self.name, kind, local_id)

    def event_source(
        self, kinds: dict[str, str], *, source: str | None = None, description: str = ""
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register the resolver that makes this module's event ids retrievable through ``core_get_event``.

        ``kinds`` maps each kind to a one-line description. The resolver is called as
        ``fn(kind, local_id, before=, after=, max_chars=)`` and returns
        ``{"event": record, "before": [...], "after": [...], "context": "..."}`` (see ``events.Resolver``).
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.registry.events.add(
                EventSource(
                    name=source or self.name, owner=self.name, kinds=dict(kinds), resolve=fn, description=description
                )
            )
            return fn

        return decorator

    def limit(self, limit: int | None, default: int | None = None) -> tuple[int, str | None]:
        return clamp_limit(limit, default or self.config.default_limit, self.config.max_limit)

    def untrusted(self, text: str | None, max_chars: int | None = None, focus: Any = None) -> dict[str, Any]:
        """Dataset text for the caller: masked, capped (default ``config.max_text`` = 500) and wrapped as
        ``{"content": ..., "untrusted": True}``. Use this for every agent/human-authored string you return."""
        return untrusted(text, self.scrub, self.config.max_text if max_chars is None else max_chars, focus)

    @property
    def store_path(self) -> Path:
        """The SwarmScope DuckDB store (``SWARMSCOPE_DB``, default ``<data_dir>/swarmscope.duckdb``)."""
        return self.config.store_path

    def store(self, read_only: bool = True):
        """``with ctx.store() as s:`` - a short-lived ``scope.db.Store`` (closed on exit, so other
        processes such as ingest or the Stop hook can open the file between tool calls)."""
        from swarm_mcp.scope import db

        return db.connect(self.store_path, read_only=read_only)

    def _full(self, suffix: str) -> str:
        return suffix if suffix.startswith(f"{self.name}_") else f"{self.name}_{suffix}"

    def _app(self) -> sdk.App:
        if self.mcp is None:  # pragma: no cover - set by the server before register()
            raise RuntimeError("ModuleContext.mcp is not set")
        return self.mcp

    def tool(
        self,
        name: str | None = None,
        *,
        description: str | None = None,
        title: str | None = None,
        read_only: bool = True,
        structured_output: bool | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a tool named ``<module>_<name or function name>``.

        Parameters become the tool's JSON schema: use type hints and
        ``Annotated[T, Field(description=...)]``. The docstring is the tool
        description unless ``description`` is given. Sync functions run in a
        worker thread; ``ToolInputError`` and unexpected errors become clean
        one-line messages (see ``toolkit.wrap_tool``).
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            full = self._full(name or fn.__name__)
            sdk.add_tool(
                self._app(),
                wrap_tool(fn, full, self.log),
                name=full,
                title=title,
                description=description or inspect.cleandoc(fn.__doc__ or "") or None,
                read_only=read_only,
                structured_output=structured_output,
            )
            return fn

        return decorator

    def resource(
        self, path: str, *, description: str | None = None, mime_type: str | None = "text/plain"
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a resource at ``<module>://<path>`` (``{param}`` makes it a template)."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            sdk.add_resource(
                self._app(),
                fn,
                uri=f"{self.name}://{path}",
                description=description or inspect.cleandoc(fn.__doc__ or "") or None,
                mime_type=mime_type,
            )
            return fn

        return decorator

    def prompt(
        self, name: str | None = None, *, description: str | None = None
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a prompt named ``<module>_<name or function name>``."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            sdk.add_prompt(
                self._app(),
                fn,
                name=self._full(name or fn.__name__),
                description=description or inspect.cleandoc(fn.__doc__ or "") or None,
            )
            return fn

        return decorator
