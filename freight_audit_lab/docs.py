"""Fill the prose documents from outputs/summary.json.

README.md, WALKTHROUGH.md and outputs/findings.md are written as templates in `docs/` with `{{path|format}}`
placeholders, for example `{{engine_vs_baseline.overall.engine.precision|pct}}`. The path walks summary.json
(a list is indexed by number: `systemic.strongest`, `systemic.others.0`); the format says how to print it. A
placeholder that does not resolve stops the run, so a document can never quote a number the summary lacks, and a
rerun of the pipeline rewrites every quoted number.
"""

import json
import re
from datetime import date

from freight_audit_lab.audit.engine import OUTPUT_DIR
from freight_audit_lab.config import REPO_ROOT

TEMPLATE_DIR = REPO_ROOT / "docs"
PLACEHOLDER = re.compile(r"\{\{\s*([\w.]+)\s*(?:\|\s*(\w+)\s*)?\}\}")

FORMATS = {
    "int": lambda x: f"{int(x):,}",
    "usd": lambda x: f"${x:,.0f}",
    "absusd": lambda x: f"${abs(x):,.0f}",
    "usd2": lambda x: f"${x:,.2f}",
    "pct": lambda x: f"{x:.1%}",             # 0.8970 -> 89.7%
    "pct0": lambda x: f"{x:.0%}",
    "pct2": lambda x: f"{x:.2%}",
    "abspct2": lambda x: f"{abs(x):.2%}",    # for "ran 0.24% below": the sentence carries the sign
    "p": lambda x: f"{x:.1e}",               # 1.6e-05
    "month": lambda x: f"{date.fromisoformat(x):%b %Y}",
    "text": str,
}

# template -> where the rendered file goes
DOCUMENTS = {"README.template.md": REPO_ROOT / "README.md",
             "WALKTHROUGH.template.md": REPO_ROOT / "WALKTHROUGH.md",
             "findings.template.md": OUTPUT_DIR / "findings.md"}


def lookup(summary, path):
    """Walk a dotted path through nested dicts and lists; raise KeyError naming the path if it is not there."""
    node = summary
    for part in path.split("."):
        try:
            node = node[int(part)] if isinstance(node, list) else node[part]
        except (KeyError, IndexError, ValueError, TypeError):
            raise KeyError(f"summary.json has no value at '{path}'") from None
    return node


def render(template, summary):
    """Replace every {{path|format}} in `template` with the formatted value from `summary`."""
    def fill(match):
        path, fmt = match.group(1), match.group(2) or "text"
        if fmt not in FORMATS:
            raise KeyError(f"unknown format '{fmt}' in {{{{{path}|{fmt}}}}}")
        return FORMATS[fmt](lookup(summary, path))
    return PLACEHOLDER.sub(fill, template)


def write_docs(summary=None, template_dir=TEMPLATE_DIR, documents=None):
    """Render every document in DOCUMENTS from outputs/summary.json (or the `summary` dict given)."""
    summary = summary if summary is not None else json.loads((OUTPUT_DIR / "summary.json").read_text())
    for name, target in (documents or DOCUMENTS).items():
        text = render((template_dir / name).read_text(), summary)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
