"""Assemble a self-contained page: the shared paper style, PaperKit, d3 and the data, inlined.

Templates mark the slots with tokens that appear exactly once:
  /*__PAPER_CSS__*/   inside <style>     -> swarm_mcp/.../viz/assets/paper.css (shared with the timeline)
  /*__PAPERKIT_JS__*/ inside <script>    -> swarm_mcp/.../viz/assets/paperkit.js (SVG/PNG export)
  /*__D3_JS__*/       inside <script>    -> vendor/d3.v7.9.0.min.js (ISC licence, vendor/d3.LICENSE)
plus any data tokens the builder passes (e.g. __DATA__). All substitutions happen in ONE pass
over the template, so text inside the data can never be mistaken for a token.
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "swarm_mcp" / "src" / "swarm_mcp" / "scope" / "viz" / "assets"
D3 = HERE / "vendor" / "d3.v7.9.0.min.js"


def assets() -> dict[str, str]:
    d3 = D3.read_text(encoding="utf-8")
    if re.search(r"</script|<!--", d3, re.I):  # would end the inline <script> early
        raise ValueError(f"{D3.name} contains a sequence that cannot be inlined in <script>")
    return {
        "/*__PAPER_CSS__*/": (ASSETS / "paper.css").read_text(encoding="utf-8"),
        "/*__PAPERKIT_JS__*/": (ASSETS / "paperkit.js").read_text(encoding="utf-8"),
        "/*__D3_JS__*/": d3,
    }


def build(template: str, data: dict[str, str]) -> str:
    """Return the template with assets and data tokens substituted (each token exactly once)."""
    parts = {**assets(), **data}
    for tok in parts:
        n = template.count(tok)
        if n != 1:
            raise ValueError(f"template must contain {tok} exactly once (found {n})")
    rx = re.compile("|".join(re.escape(t) for t in sorted(parts, key=len, reverse=True)))
    return rx.sub(lambda m: parts[m.group(0)], template)
