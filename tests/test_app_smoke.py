"""Smoke test: the dashboard runs end to end on the committed outputs/ and no tab raises."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "streamlit_app.py")
TIMEOUT = 120


def run_app():
    return AppTest.from_file(APP, default_timeout=TIMEOUT).run()


def test_app_runs_without_exceptions_and_has_five_tabs():
    at = run_app()
    assert not at.exception, [e.value for e in at.exception]
    assert [t.label for t in at.tabs] == ["Overview", "Audit quality", "Exception queue", "Month-end accruals",
                                          "Data & assumptions"]
    assert any("Synthetic data. Every dollar figure is an estimate" in m.value for m in at.markdown)


def test_the_controls_work():
    """Switching the sweep tolerance, filtering the queue by carrier, and picking another dispute pack and month."""
    at = run_app()
    at.radio[0].set_value("fsc_ltl_pp").run()
    assert not at.exception, [e.value for e in at.exception]
    at.multiselect[0].set_value(["CARF"]).run()
    assert not at.exception, [e.value for e in at.exception]
    at.selectbox[0].select_index(2).run()          # the dispute pack for the third carrier
    at.selectbox[1].select_index(0).run()          # the first month-end's journal entries
    assert not at.exception, [e.value for e in at.exception]


def test_the_dispute_viewer_opens_on_the_strongest_systemic_carrier_and_the_page_has_its_byline():
    at = run_app()
    assert at.selectbox[0].value.startswith("CARF")
    assert any("built by Shrey" in m.value and "Code on GitHub" in m.value for m in at.markdown)
