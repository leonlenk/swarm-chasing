"""The Idea Flow page at a 400 px phone width: no panel title runs off the screen and the sparkline's peak label
stays clear of the y-axis labels. Synthetic inputs only (test_ideaflow_build._inputs).

The layout test needs playwright and a Chromium (`--with playwright`; skipped otherwise); the static test always runs.
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # village_tools/

import build_viz  # noqa: E402
from swarmtrace.tests.test_ideaflow_build import _inputs  # noqa: E402

TEMPLATE = Path(build_viz.ROOT) / "viz_template.html"


def test_template_wraps_panel_titles_and_fits_the_peak_label():
    html = TEMPLATE.read_text()
    panels = html[html.index("function timePanels("):html.index("function drawWeekly(")]
    assert "wrapText(p.label" in panels and "titles[i].forEach" in panels
    spark = html[html.index("function renderSpark("):html.index("/* ---------- Figure 4")]
    assert "roomL" in spark and "roomR" in spark and "W - 210" not in spark


def _chromium():
    for name in ("chromium-browser", "chromium", "google-chrome"):
        if shutil.which(name):
            return shutil.which(name)
    return None


@pytest.mark.parametrize("peak_week", [0, 2, 4])
def test_narrow_layout(tmp_path, monkeypatch, peak_week):
    sync_api = pytest.importorskip("playwright.sync_api", reason="needs playwright: add --with playwright")
    exe = _chromium()
    if not exe:
        pytest.skip("no Chromium on PATH")
    _inputs(tmp_path)
    weeks = [w["week"] for w in json.loads((tmp_path / "cooperation.json").read_text())["weekly"]]
    ideas = json.loads((tmp_path / "ideas.json").read_text())
    ideas["durable"][0]["weekly"] = [[w, 327 if i == peak_week else 20 + i] for i, w in enumerate(weeks)]
    (tmp_path / "ideas.json").write_text(json.dumps(ideas))
    monkeypatch.setattr(build_viz, "OUT", tmp_path)
    build_viz.main()
    with sync_api.sync_playwright() as pw:
        br = pw.chromium.launch(executable_path=exe, headless=True)
        pg = br.new_page(viewport={"width": 400, "height": 900})
        pg.goto((tmp_path / "village_idea_flow.html").as_uri())
        pg.wait_for_timeout(1000)
        r = pg.evaluate("""() => {
          const vw = document.documentElement.clientWidth;
          const over = [...document.querySelectorAll('svg text')].filter(t => t.getBoundingClientRect().right > vw + 1)
            .map(t => t.textContent.slice(0, 40));
          const svg = document.querySelector('#td-spark svg'), sb = svg.getBoundingClientRect();
          const val = svg.querySelector('text.val'), vb = val.getBoundingClientRect();
          const axis = [...svg.querySelectorAll('text')].filter(t => t !== val).map(t => t.getBoundingClientRect())
            .filter(b => b.right < sb.left + 60 && b.top < vb.bottom && b.bottom > vb.top);
          return {over, valL: vb.left, valR: vb.right, axisR: Math.max(sb.left, ...axis.map(b => b.right)), svgR: sb.right};
        }""")
        br.close()
    assert r["over"] == []
    assert r["axisR"] <= r["valL"] and r["valR"] <= r["svgR"], r
