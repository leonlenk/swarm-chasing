"""Configuration: an optional ``swarm.toml`` at the project root, plus three env vars.

Every setting has a default, so the server starts with no file and no env at all.

Env vars (the only ones read):

| env var             | meaning                                                      |
|---------------------|--------------------------------------------------------------|
| SWARM_DATA_DIR      | root of local datasets (overrides ``[data] dir``)            |
| ANTHROPIC_API_KEY   | enables model calls (sweeps, ``swarm-mcp add --agent api``)  |
| SWARM_LLM_MODEL     | model id (overrides ``[llm] model``)                         |

``swarm.toml`` (every key optional; relative paths are against the project root)::

    [data]
    dir = "data"                       # datasets; the store and derived paths below
    # db = "data/swarmscope.duckdb"    # default: <dir>/swarmscope.duckdb
    # findings = "findings"            # findings.jsonl + audit.jsonl
    # sweeps = "sweeps"                # rubric sweep results (gitignored)

    [llm]
    model = "claude-sonnet-5-5"
    effort = "low"                     # "none" omits output_config.effort (e.g. Haiku)
    fallbacks = true                   # server-side refusal fallback
    # concurrency = 4                  # parallel sweep requests
    # prices = { "my-model" = [1.0, 5.0] }   # USD per 1M tokens (input, output)

    [privacy]
    email_allowlist = ["agentvillage.org"]   # email domains left unmasked
    # scrub = true                           # mask emails/phones/credentials in tool output

    [server]
    # modules = ["core", "scope"]      # load only these (core is always on)
    # disable = ["wiki"]
    max_text = 500                     # default max chars per returned text field
    # default_limit = 20
    # max_limit = 200
    # log_level = "INFO"

    [modules.git]                      # per-module settings: ctx.setting("dir")
    # dir = "data/repos"

The project root is the nearest ancestor of the current directory that contains
``swarm.toml``, ``.mcp.json`` or ``.git``. A relative SWARM_DATA_DIR is resolved
against the current directory if it exists there, otherwise against the project
root (``uv run --directory swarm_mcp`` changes the working directory).

Per-module settings come from ``[modules.<name>]``. For the git, wiki and village
modules the older per-module env vars ``SWARM_<MODULE>_<KEY>`` (``SWARM_GIT_DIR``,
``SWARM_WIKI_DB``, ``SWARM_VILLAGE_DIR``) are still honoured as a fallback.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

CONFIG_FILE = "swarm.toml"
ENV_VARS = ("SWARM_DATA_DIR", "ANTHROPIC_API_KEY", "SWARM_LLM_MODEL")
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_EFFORT = "low"
# modules whose per-module env vars (SWARM_<MODULE>_<KEY>) predate swarm.toml
LEGACY_MODULE_ENV = frozenset({"git", "wiki", "village"})
SECTIONS = ("data", "llm", "privacy", "server", "modules")


class ConfigError(ValueError):
    """swarm.toml is unreadable or has a bad value."""


def _names(value: Any) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    items = value.split(",") if isinstance(value, str) else value
    return tuple(str(p).strip().lower() for p in items if str(p).strip())


def _int(value: Any, default: int, key: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{key} must be an integer, got {value!r}") from None


def find_project_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / CONFIG_FILE).exists() or (p / ".mcp.json").exists() or (p / ".git").exists():
            return p
    return start


def resolve_data_dir(raw: str, cwd: Path | None = None) -> Path:
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    cwd = cwd or Path.cwd()
    if (cwd / path).exists():
        return (cwd / path).resolve()
    return (find_project_root(cwd) / path).resolve()


def _under(root: Path, raw: Any) -> Path | None:
    if raw is None or str(raw).strip() == "":
        return None
    p = Path(str(raw)).expanduser()
    return p if p.is_absolute() else (root / p).resolve()


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    unknown = sorted(set(data) - set(SECTIONS))
    if unknown:
        raise ConfigError(f"{path}: unknown section(s) {', '.join(unknown)}; known: {', '.join(SECTIONS)}")
    return data


@dataclass(frozen=True)
class Config:
    data_dir: Path
    project_root: Path = field(default_factory=Path.cwd)
    modules: tuple[str, ...] = ()  # empty = all
    disable: tuple[str, ...] = ()
    default_limit: int = 20
    max_limit: int = 200
    max_text: int = 500
    scrub: bool = True
    email_allowlist: tuple[str, ...] = ("agentvillage.org",)
    log_level: str = "INFO"
    db_path: Path | None = None  # None = <data_dir>/swarmscope.duckdb
    findings_dir: Path | None = None  # None = <project root>/findings
    sweeps_dir: Path | None = None  # None = <project root>/sweeps
    llm_model: str = DEFAULT_MODEL
    llm_effort: str = DEFAULT_EFFORT
    llm_fallbacks: bool = True
    llm_concurrency: int = 4
    llm_prices: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    module_settings: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    config_file: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    # -- derived paths -------------------------------------------------------
    @property
    def store_path(self) -> Path:
        """The SwarmScope DuckDB file (``[data] db``, default <data_dir>/swarmscope.duckdb)."""
        return self.db_path or (self.data_dir / "swarmscope.duckdb")

    @property
    def findings_path(self) -> Path:
        """Directory holding findings.jsonl and audit.jsonl (``[data] findings``, default <root>/findings)."""
        return self.findings_dir or (self.project_root / "findings")

    @property
    def sweeps_path(self) -> Path:
        """Directory holding rubric sweep results (``[data] sweeps``, default <root>/sweeps)."""
        return self.sweeps_dir or (self.project_root / "sweeps")

    @property
    def api_key(self) -> str | None:
        return (self.env.get("ANTHROPIC_API_KEY") or "").strip() or None

    # -- loading ---------------------------------------------------------------
    @classmethod
    def load(cls, env: Mapping[str, str] | None = None, cwd: Path | None = None, file: Path | None = None) -> "Config":
        """Read ``swarm.toml`` (``file``, else the project root's, if present) plus the three env vars."""
        cwd = cwd or Path.cwd()
        root = find_project_root(cwd)
        path = file or (root / CONFIG_FILE)
        data = read_toml(path) if path.exists() else {}
        return cls.from_dict(data, env=env, cwd=cwd, root=path.parent if file else root, file=path if data else None)

    # older name, still used by the Stop hook
    from_env = load

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        root: Path | None = None,
        file: Path | None = None,
    ) -> "Config":
        """Build a Config from parsed swarm.toml data (tests pass a dict directly)."""
        env = dict(os.environ if env is None else env)
        cwd = cwd or Path.cwd()
        root = root or find_project_root(cwd)
        data = dict(data or {})
        d, lm, pv, sv = (dict(data.get(k) or {}) for k in ("data", "llm", "privacy", "server"))
        if env.get("SWARM_DATA_DIR"):
            data_dir = resolve_data_dir(env["SWARM_DATA_DIR"], cwd)
        else:
            data_dir = _under(root, d.get("dir")) or resolve_data_dir("data", cwd)
        prices: dict[str, tuple[float, float]] = {}
        for model, pair in dict(lm.get("prices") or {}).items():
            try:
                prices[str(model)] = (float(pair[0]), float(pair[1]))
            except (TypeError, ValueError, IndexError, KeyError):
                raise ConfigError(f"[llm] prices.{model} must be [input, output] USD per 1M tokens") from None
        modules = {str(k).lower(): dict(v or {}) for k, v in dict(data.get("modules") or {}).items()}
        return cls(
            data_dir=data_dir,
            project_root=root,
            modules=_names(sv.get("modules")),
            disable=_names(sv.get("disable")),
            default_limit=max(1, _int(sv.get("default_limit"), 20, "[server] default_limit")),
            max_limit=max(1, _int(sv.get("max_limit"), 200, "[server] max_limit")),
            max_text=max(80, _int(sv.get("max_text"), 500, "[server] max_text")),
            scrub=bool(pv.get("scrub", True)),
            email_allowlist=_names(pv.get("email_allowlist", ["agentvillage.org"])),
            log_level=str(sv.get("log_level") or "INFO").upper(),
            db_path=_under(root, d.get("db")),
            findings_dir=_under(root, d.get("findings")),
            sweeps_dir=_under(root, d.get("sweeps")),
            llm_model=(env.get("SWARM_LLM_MODEL") or str(lm.get("model") or "") or DEFAULT_MODEL).strip(),
            llm_effort=str(lm.get("effort") or DEFAULT_EFFORT).strip().lower(),
            llm_fallbacks=bool(lm.get("fallbacks", True)),
            llm_concurrency=max(1, _int(lm.get("concurrency"), 4, "[llm] concurrency")),
            llm_prices=prices,
            module_settings=modules,
            config_file=file,
            env=env,
        )

    def module_setting(self, module: str, key: str, default: str | None = None) -> str | None:
        """Per-module setting from ``[modules.<module>] <key>`` in swarm.toml."""
        value = (self.module_settings.get(module.lower()) or {}).get(key)
        if value is None and module.lower() in LEGACY_MODULE_ENV:
            value = self.env.get(f"SWARM_{module}_{key}".upper())
        return default if value is None else str(value)

    def public(self) -> dict[str, Any]:
        """JSON-friendly summary for core_info and ``swarm-mcp info`` (no secrets, no env dump)."""
        return {
            "config_file": str(self.config_file) if self.config_file else None,
            "project_root": str(self.project_root),
            "data_dir": str(self.data_dir),
            "data_dir_exists": self.data_dir.exists(),
            "db_path": str(self.store_path),
            "db_exists": self.store_path.exists(),
            "findings_dir": str(self.findings_path),
            "sweeps_dir": str(self.sweeps_path),
            "modules": list(self.modules) or "all",
            "disable": list(self.disable),
            "default_limit": self.default_limit,
            "max_limit": self.max_limit,
            "max_text": self.max_text,
            "scrub": self.scrub,
            "email_allowlist": list(self.email_allowlist),
            "llm": {
                "model": self.llm_model,
                "effort": self.llm_effort,
                "fallbacks": self.llm_fallbacks,
                "api_key_set": self.api_key is not None,
            },
            "log_level": self.log_level,
        }
