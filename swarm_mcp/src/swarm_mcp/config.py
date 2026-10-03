"""Server configuration, read once from the environment.

Every setting has a default so the server starts with no env at all.

| env var                      | default            | meaning                                        |
|------------------------------|--------------------|------------------------------------------------|
| SWARM_DATA_DIR               | data               | root of local datasets (relative: see below)   |
| SWARM_MCP_MODULES            | (all)              | comma list of modules to load                  |
| SWARM_MCP_DISABLE            | (none)             | comma list of modules to skip                  |
| SWARM_MCP_DEFAULT_LIMIT      | 20                 | default result count for list/search tools     |
| SWARM_MCP_MAX_LIMIT          | 200                | hard cap on result counts                      |
| SWARM_MCP_MAX_TEXT           | 1000               | default max chars per returned text field      |
| SWARM_MCP_SCRUB              | 1                  | mask emails/phones in returned text (0 = off)  |
| SWARM_MCP_EMAIL_ALLOWLIST    | agentvillage.org   | email domains left unmasked (comma list)       |
| SWARM_MCP_LOG_LEVEL          | INFO               | stderr log level                               |

A relative SWARM_DATA_DIR is resolved against the current directory if it
exists there, otherwise against the project root (the nearest ancestor that
contains ``.mcp.json`` or ``.git``). This matters because
``uv run --directory swarm_mcp`` changes the working directory to swarm_mcp/.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


def _split(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(p.strip().lower() for p in value.split(",") if p.strip())


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def find_project_root(start: Path) -> Path:
    for p in [start, *start.parents]:
        if (p / ".mcp.json").exists() or (p / ".git").exists():
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


@dataclass(frozen=True)
class Config:
    data_dir: Path
    modules: tuple[str, ...] = ()  # empty = all
    disable: tuple[str, ...] = ()
    default_limit: int = 20
    max_limit: int = 200
    max_text: int = 1000
    scrub: bool = True
    email_allowlist: tuple[str, ...] = ("agentvillage.org",)
    log_level: str = "INFO"
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, cwd: Path | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        return cls(
            data_dir=resolve_data_dir(env.get("SWARM_DATA_DIR") or "data", cwd),
            modules=_split(env.get("SWARM_MCP_MODULES")),
            disable=_split(env.get("SWARM_MCP_DISABLE")),
            default_limit=max(1, _int(env, "SWARM_MCP_DEFAULT_LIMIT", 20)),
            max_limit=max(1, _int(env, "SWARM_MCP_MAX_LIMIT", 200)),
            max_text=max(80, _int(env, "SWARM_MCP_MAX_TEXT", 1000)),
            scrub=env.get("SWARM_MCP_SCRUB", "1").strip().lower() not in ("0", "false", "no", "off"),
            email_allowlist=_split(env.get("SWARM_MCP_EMAIL_ALLOWLIST", "agentvillage.org")),
            log_level=(env.get("SWARM_MCP_LOG_LEVEL") or "INFO").upper(),
            env=env,
        )

    def module_setting(self, module: str, key: str, default: str | None = None) -> str | None:
        """Per-module setting from env ``SWARM_<MODULE>_<KEY>`` (upper-cased)."""
        return self.env.get(f"SWARM_{module}_{key}".upper(), default)

    def public(self) -> dict[str, Any]:
        """JSON-friendly view for core_server_info (no raw env dump)."""
        return {
            "data_dir": str(self.data_dir),
            "data_dir_exists": self.data_dir.exists(),
            "modules": list(self.modules) or "all",
            "disable": list(self.disable),
            "default_limit": self.default_limit,
            "max_limit": self.max_limit,
            "max_text": self.max_text,
            "scrub": self.scrub,
            "email_allowlist": list(self.email_allowlist),
            "log_level": self.log_level,
        }
