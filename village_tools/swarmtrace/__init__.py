"""swarmtrace: a small, source-agnostic "idea trace" format for following how one idea spreads through a
multi-agent swarm, plus a validator. Source-specific conversion lives in swarmtrace.adapters.*."""

from .format import (VERSION, check, clip, data_span, dumps, fit_window, index_entry, iso, parse_iso, validate,
                     validate_index)

__all__ = ["VERSION", "check", "clip", "data_span", "dumps", "fit_window", "index_entry", "iso", "parse_iso",
           "validate", "validate_index"]
