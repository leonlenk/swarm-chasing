"""NeurIPS paper style for the static matplotlib figures.

The rcParams replicate ``tueplots.bundles.neurips2024(usetex=False, family="serif")``
(Times text and STIX math, 5.5 in text width, golden-ratio panels, constrained layout,
tight bbox) with tueplots' 0.5 pt axes, and the font sizes clamped so no figure text is
below 7 pt at print size. Colours are not defined here: they are read from the shared
HTML tokens in ``swarm_mcp/src/swarm_mcp/scope/viz/assets/paper.css``, so a static figure
and an interactive page always agree on what each colour means.

Use::

    import paperfig
    paperfig.use()
    fig, ax = plt.subplots(figsize=paperfig.size(1.0, 0.5))
    ...
    paperfig.save(fig, OUT / "figure_name")   # figure_name.pdf (vector) + figure_name.png (300 dpi)

Colour roles (one per figure, always paired with a second channel for black-and-white
print): ``CAT`` + ``MARKERS`` for generic series, ``LAB`` for model labs (colour and
marker), ``STANCE`` for for/neutral/against, ``SEQ`` for magnitude, ``DIV`` for signed
differences.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAPER_CSS = ROOT / "swarm_mcp" / "src" / "swarm_mcp" / "scope" / "viz" / "assets" / "paper.css"

TEXT_WIDTH_IN = 5.5     # NeurIPS \textwidth
HALF_WIDTH_IN = 2.7     # one of two side-by-side panels with a small gap
GOLDEN = (5 ** 0.5 - 1) / 2


def _tokens(path: Path = PAPER_CSS) -> dict[str, str]:
    css = path.read_text(encoding="utf-8")
    root = css[css.index(":root") : css.index("}", css.index(":root"))]
    return {k: v.lower() for k, v in re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})\b", root)}


TOKENS = _tokens()
INK, INK2, INK3 = TOKENS["ink"], TOKENS["ink-2"], TOKENS["ink-3"]
RULE, HAIR, WASH = TOKENS["rule"], TOKENS["hair"], TOKENS["wash"]
CAT = [TOKENS[f"c{i}"] for i in range(1, 8)]
CAT_OTHER = TOKENS["c-other"]
MARKERS = ["o", "s", "^", "D", "v", "P", "X"]
LINESTYLES = ["-", "--", ":", "-."]
LAB = {  # lab -> (colour, marker); humans are a hollow black circle
    "Anthropic": (TOKENS["lab-anthropic"], "o"),
    "OpenAI": (TOKENS["lab-openai"], "s"),
    "Google": (TOKENS["lab-google"], "^"),
    "Other": (TOKENS["lab-other"], "D"),
}
STANCE = {"for": TOKENS["st-for"], "neutral": TOKENS["st-neutral"], "against": TOKENS["st-against"]}
SEQ = [TOKENS[f"seq-{i}"] for i in range(7)]
DIV = (TOKENS["div-lo"], TOKENS["div-mid"], TOKENS["div-hi"])

# Liberation Serif (metric-compatible with Times New Roman) comes before the Nimbus Roman OTF so
# PDFs embed plain TrueType (pdf.fonttype 42) rather than CFF.
SERIF = ["Times New Roman", "Times", "Liberation Serif", "TeX Gyre Termes", "Nimbus Roman", "STIX Two Text", "DejaVu Serif"]


def rc() -> dict:
    """rcParams: tueplots neurips2024 (serif, no TeX) + thin axes, sizes >= 7 pt."""
    from cycler import cycler  # ships with matplotlib

    return {
        # fonts (tueplots fonts.neurips2024)
        "text.usetex": False,
        "font.family": "serif",
        "font.serif": SERIF,
        "mathtext.fontset": "stix",
        "mathtext.rm": "serif",
        "mathtext.it": "serif:italic",
        "mathtext.bf": "serif:bold",
        # sizes (tueplots fontsizes.neurips2024 is 8/8/6/6/6/8; 6 pt is below the 7 pt floor)
        "font.size": 8,
        "axes.labelsize": 8,
        "axes.titlesize": 8,
        "legend.fontsize": 7,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "figure.titlesize": 8,
        # layout (tueplots figsizes.neurips2024)
        "figure.figsize": (TEXT_WIDTH_IN, TEXT_WIDTH_IN * GOLDEN),
        "figure.constrained_layout.use": True,
        "figure.autolayout": False,
        "savefig.bbox": "tight",
        "savefig.pad_inches": 0.015,
        "figure.dpi": 150,
        "savefig.dpi": 300,
        "figure.facecolor": "white",
        "savefig.facecolor": "white",
        "axes.facecolor": "white",
        # thin dark axes, left and bottom only (tueplots axes.lines + axes.spines)
        "axes.linewidth": 0.5,
        "axes.edgecolor": RULE,
        "axes.labelcolor": INK,
        "axes.titlecolor": INK,
        "axes.titlelocation": "left",
        "axes.titlepad": 4,
        "axes.labelpad": 2.5,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": False,
        "grid.color": HAIR,
        "grid.linewidth": 0.4,
        "axes.axisbelow": True,
        "axes.prop_cycle": cycler(color=CAT),
        "xtick.direction": "out",
        "ytick.direction": "out",
        "xtick.major.width": 0.5,
        "ytick.major.width": 0.5,
        "xtick.minor.width": 0.4,
        "ytick.minor.width": 0.4,
        "xtick.major.size": 2.5,
        "ytick.major.size": 2.5,
        "xtick.minor.size": 1.5,
        "ytick.minor.size": 1.5,
        "xtick.major.pad": 2,
        "ytick.major.pad": 2,
        "xtick.color": RULE,
        "ytick.color": RULE,
        "xtick.labelcolor": INK,
        "ytick.labelcolor": INK,
        "lines.linewidth": 1.0,
        "lines.markersize": 3.5,
        "lines.markeredgewidth": 0.6,
        "patch.linewidth": 0.5,
        "errorbar.capsize": 0,
        "legend.frameon": False,
        "legend.handlelength": 1.6,
        "legend.handletextpad": 0.4,
        "legend.borderaxespad": 0.2,
        "legend.columnspacing": 1.0,
        "legend.labelspacing": 0.3,
        # vector output that venues accept: embed TrueType (Type 42), never Type 3
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
        "svg.fonttype": "none",
    }


def use() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update(rc())


def size(rel_width: float = 1.0, aspect: float = GOLDEN, *, height_in: float | None = None) -> tuple[float, float]:
    """(width, height) in inches: rel_width of the 5.5 in text width, height = width * aspect."""
    w = TEXT_WIDTH_IN * rel_width
    return (w, height_in if height_in is not None else w * aspect)


def save(fig, stem: Path | str, *, formats: tuple[str, ...] = ("pdf", "png"), dpi: int = 300) -> list[Path]:
    """Write stem.pdf (vector) and stem.png (300 dpi); returns the paths written."""
    stem = Path(stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for fmt in formats:
        p = stem.with_suffix("." + fmt)
        fig.savefig(p, dpi=dpi if fmt == "png" else None, metadata={"CreationDate": None} if fmt == "pdf" else None)
        out.append(p)
    return out


def despine(ax, *, left: bool = True, bottom: bool = True) -> None:
    ax.spines["left"].set_visible(left)
    ax.spines["bottom"].set_visible(bottom)
    if not left:
        ax.tick_params(axis="y", length=0)
    if not bottom:
        ax.tick_params(axis="x", length=0)


def lab_style(lab: str | None) -> tuple[str, str]:
    """(colour, marker) for a model lab; anything outside the big three is 'Other'."""
    return LAB.get(lab or "", LAB["Other"])
