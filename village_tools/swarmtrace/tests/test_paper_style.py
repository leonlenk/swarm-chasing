"""The shared paper style: one set of colour tokens for the HTML pages and the matplotlib figures.

paperfig reads its palette from swarm_mcp's assets/paper.css, so these checks pin the contract:
every role the figures use exists, the categorical slots are the Okabe-Ito colours in their fixed
order, and the sequential ramp gets darker step by step (so it also reads in greyscale).
Run from the repo root: uv run --no-project --with pytest --with jsonschema --with matplotlib pytest village_tools/swarmtrace/tests
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # village_tools/

import pagekit  # noqa: E402
import paperfig  # noqa: E402

OKABE_ITO = ["#0072b2", "#d55e00", "#009e73", "#e69f00", "#cc79a7", "#56b4e9", "#000000"]


def _luminance(hex_: str) -> float:
    r, g, b = (int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def test_palette_comes_from_paper_css():
    assert paperfig.PAPER_CSS.exists() and paperfig.PAPER_CSS == pagekit.ASSETS / "paper.css"
    assert paperfig.CAT == OKABE_ITO
    assert set(paperfig.LAB) == {"Anthropic", "OpenAI", "Google", "Other"}
    markers = [m for _, m in paperfig.LAB.values()]
    assert len(set(markers)) == len(markers)  # every lab has its own marker, not just a colour
    assert set(paperfig.STANCE) == {"for", "neutral", "against"}
    assert len(paperfig.MARKERS) >= len(paperfig.CAT)


def test_sequential_ramp_is_monotone():
    lum = [_luminance(c) for c in paperfig.SEQ]
    assert all(a > b for a, b in zip(lum, lum[1:], strict=False))


def test_rc_is_neurips_sized():
    pytest.importorskip("cycler", reason="needs matplotlib (rc() builds a cycler colour cycle): add --with matplotlib")
    rc = paperfig.rc()
    assert rc["font.family"] == "serif" and rc["font.serif"][0] == "Times New Roman"
    assert min(rc[k] for k in ("font.size", "axes.labelsize", "xtick.labelsize", "ytick.labelsize", "legend.fontsize")) >= 7
    assert rc["pdf.fonttype"] == 42 and rc["axes.spines.top"] is False
    assert paperfig.size(1.0)[0] == 5.5 and paperfig.size(0.5)[0] == 2.75


def test_pagekit_build_is_one_pass():
    tpl = "<style>/*__PAPER_CSS__*/</style><script>/*__PAPERKIT_JS__*/</script><script>/*__D3_JS__*/</script>__DATA__"
    out = pagekit.build(tpl, {"__DATA__": "/*__D3_JS__*/"})
    assert out.endswith("/*__D3_JS__*/")  # data is never re-scanned for tokens
    assert "--oi-vermillion" in out and "window.PaperKit" in out
