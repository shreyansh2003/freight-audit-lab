"""summary.json and the documents filled from it.

The prose may only quote numbers that summary.json holds, and summary.json may only hold numbers the output files
hold. These tests check both links on the committed outputs, plus the small template engine on its own.
"""

import json
import re

import pytest

from freight_audit_lab.audit.engine import OUTPUT_DIR
from freight_audit_lab.config import REPO_ROOT, load_config
from freight_audit_lab.csv_io import read_csv
from freight_audit_lab.docs import DOCUMENTS, TEMPLATE_DIR, lookup, render
from freight_audit_lab.summary import build_summary

README_WORD_LIMIT = 550         # the spec's one-page memo, not counting the results table or the code block


@pytest.fixture(scope="module")
def summary():
    return json.loads((OUTPUT_DIR / "summary.json").read_text())


def test_render_fills_paths_lists_and_formats():
    data = {"a": {"p": 0.8971, "n": 12345, "d": 1234.5, "when": "2025-08-01"}, "rows": [{"x": 0.0024}, {"x": -0.0031}]}
    text = "{{a.p|pct}} {{a.p|pct0}} {{ a.n|int }} {{a.d|usd}} {{a.when|month}} {{rows.1.x|abspct2}} {{a.n}}"
    assert render(text, data) == "89.7% 90% 12,345 $1,234 Aug 2025 0.31% 12345"


def test_render_stops_on_a_missing_value_or_an_unknown_format():
    with pytest.raises(KeyError, match="no value at 'a.missing'"):
        render("{{a.missing|int}}", {"a": {}})
    with pytest.raises(KeyError, match="unknown format"):
        render("{{a.n|shiny}}", {"a": {"n": 1}})
    with pytest.raises(KeyError):
        lookup({"rows": [1]}, "rows.3")


def test_summary_json_is_what_the_outputs_say_right_now(summary):
    """Rebuilding the summary from outputs/ gives the committed file, so no headline number is stale."""
    rebuilt = json.loads(json.dumps(build_summary(load_config())))
    assert rebuilt == summary


def test_summary_numbers_tie_to_the_output_files(summary):
    ev = read_csv(OUTPUT_DIR / "eval_engine_vs_baseline.csv").set_index("error_type")
    queue = read_csv(OUTPUT_DIR / "exception_queue.csv")
    assert summary["engine_vs_baseline"]["overall"]["engine"]["precision"] == ev.loc["ALL", "engine_precision"]
    assert summary["engine_vs_baseline"]["overall"]["baseline"]["fp"] == ev.loc["ALL", "baseline_fp"]
    assert summary["recoverable"]["total_estimate"] == pytest.approx(queue["recoverable_estimate"].sum(), abs=0.01)
    assert sum(summary["recoverable"]["by_error_type_estimate"].values()) == pytest.approx(
        summary["recoverable"]["total_estimate"], abs=0.01)
    assert summary["engine_misses"]["missed"] == summary["engine_misses"]["below_tolerance"] + summary["engine_misses"]["other"]
    assert summary["engine_misses"]["missed"] == summary["engine_vs_baseline"]["overall"]["engine"]["fn"]
    assert summary["seed"] == load_config()["seed"] and summary["audit_as_of"] == "2026-03-31"


def test_summary_has_no_wall_clock_so_reruns_are_byte_identical(summary):
    assert "run_date" not in summary and "generated_at" not in summary


def test_every_template_renders_from_the_committed_summary_and_matches_the_committed_file(summary):
    for name, target in DOCUMENTS.items():
        text = render((TEMPLATE_DIR / name).read_text(), summary)
        assert "{{" not in text and "}}" not in text, name
        assert target.read_text() == text, f"{target.relative_to(REPO_ROOT)} is stale: rerun the pipeline"


def test_readme_is_one_page_and_keeps_its_placeholders():
    text = (REPO_ROOT / "README.md").read_text()
    prose = re.sub(r"```.*?```", "", re.sub(r"<!--.*?-->", "", text, flags=re.S), flags=re.S)
    words = " ".join(line for line in prose.splitlines() if not line.startswith("|")).split()
    assert len(words) <= README_WORD_LIMIT
    assert "[Shreyansh Agrawal](https://www.linkedin.com/in/shreyansh2003/)" in text
    assert "(https://github.com/shreyansh2003/freight-audit-lab)" in text
    assert "[dashboard link]" in text and "[Your name]" not in text            # the URL exists only after deploying
    assert text.index("## The comparison") < text.index("recoverable estimate")       # the comparison leads, dollars come later
    assert "built the generator" in text                                               # the plain statement about circularity


def test_walkthrough_has_twelve_numbered_questions_and_findings_has_three():
    walkthrough = (REPO_ROOT / "WALKTHROUGH.md").read_text()
    assert re.findall(r"^### (\d+)\. ", walkthrough, flags=re.M) == [str(n) for n in range(1, 13)]
    findings = (OUTPUT_DIR / "findings.md").read_text()
    assert re.findall(r"^## (\d+)\. ", findings, flags=re.M) == ["1", "2", "3"]
    assert findings.count("- Sources: `outputs/") == 3


def test_spec_starts_with_the_departures_note():
    first = (REPO_ROOT / "SPEC.md").read_text().splitlines()[0]
    assert first == "> Original build plan. Where the build departed from it, ASSUMPTIONS.md records the change and why."


def test_p_values_use_one_style_and_the_floor_is_not_printed_as_a_number():
    from freight_audit_lab.docs import render
    assert render("{{p|pval}}", {"p": 1.2e-320}) == "p < 1e-300"
    assert render("{{p|pval}}", {"p": 1.6e-05}) == "p = 1.6e-05"
    assert "e-320" not in (REPO_ROOT / "README.md").read_text() + (REPO_ROOT / "WALKTHROUGH.md").read_text()
