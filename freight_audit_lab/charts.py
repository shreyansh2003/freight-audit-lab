"""Altair charts for the dashboard: one accent colour plus greys, no gridlines, units in every title.

Each function takes a small DataFrame the dashboard has already prepared and returns an `alt.Chart`.
Engine or "as built" is always the accent; the baseline or the what-if is always grey, so the same colour
means the same thing on every tab.
"""

import altair as alt
import pandas as pd

ACCENT = "#1F5FBF"
INK, MUTED, GREY, LIGHT = "#1F2328", "#57606A", "#A3ACB6", "#D8DEE4"
FONT = "sans-serif"
SERIES = alt.Scale(domain=["Engine", "Baseline"], range=[ACCENT, GREY])


LABEL_ROOM = 64      # pixels of padding right of a bar chart, so the value label on the longest bar is not clipped


def styled(chart, title, height, pad_right=5):
    """Shared look: left-aligned title, no view border, no gridlines, readable label sizes.
    `pad_right` is empty space right of the plot; bar charts use it to fit the value printed after each bar."""
    return (chart.properties(title=alt.TitleParams(title, anchor="start", fontSize=14, fontWeight=600, color=INK,
                                                   offset=10), height=height,
                             padding={"left": 5, "top": 5, "right": pad_right, "bottom": 5})
            .configure_view(stroke=None)
            .configure_axis(grid=False, domainColor=LIGHT, tickColor=LIGHT, labelColor=MUTED, labelFontSize=12,
                            titleColor=MUTED, titleFontSize=12, titleFontWeight="normal", labelFont=FONT, titleFont=FONT)
            .configure_legend(orient="top", title=None, labelColor=MUTED, labelFontSize=12, labelLimit=0, columnPadding=16, symbolType="square",
                              padding=0, offset=4))


def bar_by_category(df, category, value, title, fmt="$,.0f", height=None):
    """Horizontal bars, largest first, value printed at the end of each bar (so no value axis)."""
    base = alt.Chart(df).encode(y=alt.Y(f"{category}:N", sort=alt.EncodingSortField(value, order="descending"),
                                        title=None, axis=alt.Axis(ticks=False, domain=False, labelLimit=240)),
                                x=alt.X(f"{value}:Q", axis=None, scale=alt.Scale(domain=[0, df[value].max() * 1.18])))
    bars = base.mark_bar(color=ACCENT, cornerRadiusEnd=3, size=16).encode(
        tooltip=[alt.Tooltip(f"{category}:N", title=None), alt.Tooltip(f"{value}:Q", format=fmt, title="Estimate")])
    labels = base.mark_text(align="left", dx=5, color=INK, fontSize=12).encode(text=alt.Text(f"{value}:Q", format=fmt))
    return styled(bars + labels, title, height or 30 * len(df) + 10, pad_right=LABEL_ROOM)


def engine_vs_baseline_bars(df, category, title, fmt, height=None):
    """Grouped horizontal bars: `df` has columns [category, system (Engine/Baseline), value]."""
    base = alt.Chart(df).encode(
        y=alt.Y(f"{category}:N", title=None, sort=list(dict.fromkeys(df[category])),
                axis=alt.Axis(ticks=False, domain=False, labelLimit=240)),
        yOffset=alt.YOffset("system:N", sort=["Engine", "Baseline"]),
        x=alt.X("value:Q", axis=None, scale=alt.Scale(domain=[0, df["value"].max() * 1.15])),
        color=alt.Color("system:N", scale=SERIES, sort=["Engine", "Baseline"]))
    bars = base.mark_bar(cornerRadiusEnd=3).encode(tooltip=[alt.Tooltip(f"{category}:N", title=None), "system:N",
                                                            alt.Tooltip("value:Q", format=fmt, title=None)])
    labels = base.mark_text(align="left", dx=4, fontSize=11, color=INK).encode(text=alt.Text("value:Q", format=fmt))
    return styled(bars + labels, title, height or 26 * df[category].nunique() * 2 + 20, pad_right=LABEL_ROOM)


def sweep_chart(df, x_title, floor, title):
    """Precision and recall against one tolerance. `df` has [label, precision, recall, note]; the point marked
    in `note` (current and/or recommended) gets a ring and a caption. The dashed line is the precision floor."""
    long = df.melt(["label", "note"], ["precision", "recall"], var_name="measure", value_name="share")
    long["measure"] = long["measure"].str.capitalize()
    order = list(df["label"])
    scale = alt.Scale(domain=["Precision", "Recall"], range=[ACCENT, MUTED])
    base = alt.Chart(long).encode(
        x=alt.X("label:N", sort=order, title=x_title, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("share:Q", title=None, scale=alt.Scale(domain=[0, 1.05]), axis=alt.Axis(format="%", values=[0, .25, .5, .75, 1])),
        color=alt.Color("measure:N", scale=scale))
    lines = base.mark_line(strokeWidth=2)
    points = base.mark_point(filled=True, size=45).encode(
        tooltip=[alt.Tooltip("label:N", title="Tolerance"), "measure:N", alt.Tooltip("share:Q", format=".1%", title=None)])
    marked = long[long["note"] != ""]
    rings = alt.Chart(marked).mark_point(size=260, filled=False, stroke=INK, strokeWidth=1.5).encode(
        x=alt.X("label:N", sort=order), y="share:Q")
    captions = (alt.Chart(marked[marked["measure"] == "Precision"]).mark_text(dy=-16, fontSize=12, color=INK, fontWeight=600)
                .encode(x=alt.X("label:N", sort=order), y="share:Q", text="note:N"))
    rule = (alt.Chart(pd.DataFrame({"y": [floor]})).mark_rule(strokeDash=[4, 4], color=GREY).encode(y="y:Q")
            + alt.Chart(pd.DataFrame({"y": [floor], "t": ["precision floor"]})).mark_text(
                align="right", dx=-4, dy=-6, color=MUTED, fontSize=11, x="width").encode(y="y:Q", text="t:N"))
    return styled(lines + points + rings + captions + rule, title, 300)


def month_bars(df, title, fmt="$~s"):
    """Grouped vertical bars per month. `df` has [month, series, value]; the first series is the accent."""
    series = list(dict.fromkeys(df["series"]))
    base = alt.Chart(df).encode(
        x=alt.X("month:N", sort=list(dict.fromkeys(df["month"])), title=None, axis=alt.Axis(labelAngle=0)),
        xOffset=alt.XOffset("series:N", sort=series),
        y=alt.Y("value:Q", title=None, axis=alt.Axis(format=fmt)),
        color=alt.Color("series:N", scale=alt.Scale(domain=series, range=[ACCENT, GREY]), sort=series))
    bars = base.mark_bar(cornerRadiusEnd=2).encode(
        tooltip=[alt.Tooltip("month:N", title=None), "series:N", alt.Tooltip("value:Q", format="$,.0f", title=None)])
    return styled(bars, title, 280)


def error_lines(df, title):
    """Two lines of monthly accrual error % (as built = accent, what-if = grey) around a zero line.
    `df` has [month, series, error_pct] where error_pct is a fraction."""
    series = list(dict.fromkeys(df["series"]))
    base = alt.Chart(df).encode(
        x=alt.X("month:N", sort=list(dict.fromkeys(df["month"])), title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("error_pct:Q", title=None, axis=alt.Axis(format=".1%")),
        color=alt.Color("series:N", scale=alt.Scale(domain=series, range=[ACCENT, GREY]), sort=series))
    lines = base.mark_line(strokeWidth=2)
    points = base.mark_point(filled=True, size=45).encode(
        tooltip=[alt.Tooltip("month:N", title=None), "series:N", alt.Tooltip("error_pct:Q", format="+.2%", title="Error")])
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=LIGHT).encode(y="y:Q")
    return styled(zero + lines + points, title, 280)


def diesel_line(df, title):
    """The weekly diesel price series in a single accent line."""
    line = alt.Chart(df).mark_line(color=ACCENT, strokeWidth=2).encode(
        x=alt.X("week_start:T", title=None, axis=alt.Axis(format="%b %Y")),
        y=alt.Y("price_per_gallon:Q", title=None, scale=alt.Scale(zero=False), axis=alt.Axis(format="$.2f")),
        tooltip=[alt.Tooltip("week_start:T", title="Week of", format="%Y-%m-%d"),
                 alt.Tooltip("price_per_gallon:Q", title="Price", format="$.3f")])
    return styled(line, title, 240)
