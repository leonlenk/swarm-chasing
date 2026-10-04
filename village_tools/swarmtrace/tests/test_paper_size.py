"""paperfig saves figures at exactly their size (never past the 5.5 in = 396 pt NeurIPS text width) and says when
it falls back from Times. Synthetic plot only."""

import logging
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # village_tools/

pytest.importorskip("matplotlib", reason="needs matplotlib: add --with matplotlib")

import paperfig  # noqa: E402


def test_saved_full_width_figure_fits_the_text_width(tmp_path):
    paperfig.use()
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=paperfig.size(1.0, height_in=2.6))
    ax.plot(range(10), [i * i for i in range(10)], label="a long legend entry " * 2)
    ax.set_xlabel("a long x label " * 6)
    ax.set_ylabel("y label")
    ax.set_yticks([0, 40, 80], ["zero point zero", "forty", "eighty, the most"])
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.text(0.97, 0.5, "a label running past the edge")  # outside the layout: a tight bbox would widen the figure
    pdf, png = paperfig.save(fig, tmp_path / "fig")
    plt.close(fig)
    box = re.search(rb"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", pdf.read_bytes())
    width_pt = float(box.group(3)) - float(box.group(1))
    assert width_pt <= 396.0 + 1e-6, width_pt
    w_px = int.from_bytes(png.read_bytes()[16:20], "big")  # PNG IHDR width
    assert w_px <= 5.5 * 300, w_px


def test_font_fallback_is_logged_once(monkeypatch, caplog):
    monkeypatch.setattr(paperfig, "serif_font", lambda: "Liberation Serif")
    monkeypatch.setattr(paperfig, "_FONT_NOTED", False)
    with caplog.at_level(logging.WARNING, logger="paperfig"):
        paperfig.use()
        paperfig.use()
    notes = [r for r in caplog.records if "Times" in r.getMessage()]
    assert len(notes) == 1 and "Liberation Serif" in notes[0].getMessage()


def test_no_fallback_note_with_times(monkeypatch, caplog):
    monkeypatch.setattr(paperfig, "serif_font", lambda: "Times New Roman")
    monkeypatch.setattr(paperfig, "_FONT_NOTED", False)
    with caplog.at_level(logging.WARNING, logger="paperfig"):
        paperfig.use()
    assert not caplog.records
