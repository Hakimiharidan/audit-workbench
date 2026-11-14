"""Local review screen for a workbench run. Launch with `workbench review OUTPUT_DIR`.

The screen shows what the tool flagged; the reviewer decides. Nothing here proposes or
pre-selects a disposition, and every save goes to workpaper_reviewed.xlsx, never the original.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from workbench import review
from workbench.workpaper import DISCLAIMER, REVIEW_COLUMNS

# Materiality bands are ordered by severity: one hue, light -> dark (validated ordinal ramp);
# exceptions without an amount (control findings) are neutral gray.
BANDS = ["above_materiality", "above_performance", "above_trivial", "trivial", "no_amount"]
BAND_COLORS = ["#0d366b", "#1c5cab", "#3987e5", "#86b6ef", "#a8a7a2"]
STATUS = ["All", "Not reviewed", "Reviewed"]

st.set_page_config(page_title="Workbench review", layout="wide")

out_dir = Path(os.environ.get("WORKBENCH_REVIEW_DIR", "."))
try:
    wp = review.load_workpaper(out_dir)
except review.ReviewError as exc:
    st.error(str(exc))
    st.stop()

st.title("Exception review")
engagement = wp.cover.get("Engagement") or "(no engagement name)"
st.caption(f"{engagement} · run {wp.cover.get('Run ID', '?')} · showing `{wp.path.name}` "
           f"in folder `{out_dir.resolve().name}`")
st.info(DISCLAIMER, icon="ℹ️")

with st.sidebar:
    st.header("Reviewer")
    reviewer = st.text_input("Your name", key="reviewer", help="Required to save. Recorded with each decision.")
    st.header("Filters")
    if not wp.runs:
        st.warning("This workpaper has no test sheets.")
        st.stop()
    run_id = st.selectbox("Test", list(wp.runs), key="run")
    df_all = wp.runs[run_id]
    bands = st.multiselect("Band", [b for b in BANDS if b in set(df_all["band"])], key="bands")
    labels = st.multiselect("Initial label", sorted(df_all["label"].dropna().unique()), key="labels")
    status = st.radio("Review status", STATUS, key="status")

tab_overview, tab_exceptions = st.tabs(["Overview", "Exceptions"])

with tab_overview:
    counts = []
    for rid, df in wp.runs.items():
        for band, n in df["band"].value_counts().items():
            counts.append({"Test": rid, "Band": band, "Exceptions": int(n)})
    progress = pd.DataFrame([
        {"Test": rid, "Exceptions": len(df), "Reviewed": int(df["Disposition"].notna().sum()),
         "Not reviewed": int(df["Disposition"].isna().sum())}
        for rid, df in wp.runs.items()
    ])
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Band mix per test")
        if counts:
            data = pd.DataFrame(counts)
            data["order"] = data["Band"].map(BANDS.index)
            data["Share"] = data["Exceptions"] / data.groupby("Test")["Exceptions"].transform("sum")
            chart = (
                alt.Chart(data)
                .mark_bar(stroke="white", strokeWidth=2)
                .encode(
                    y=alt.Y("Test:N", sort=list(wp.runs), title=None, axis=alt.Axis(labelOverlap=False)),
                    x=alt.X("sum(Share):Q", title="Share of the test's exceptions",
                            axis=alt.Axis(format="%", grid=False)),
                    color=alt.Color("Band:N", scale=alt.Scale(domain=BANDS, range=BAND_COLORS), sort=BANDS,
                                    legend=alt.Legend(orient="top", title=None, columns=3, labelLimit=200)),
                    order=alt.Order("order:Q"),
                    tooltip=["Test", "Band", "Exceptions", alt.Tooltip("Share:Q", format=".0%")],
                )
                .properties(height=alt.Step(30))
            )
            st.altair_chart(chart, width="stretch")
            st.caption("Each bar is one test's exceptions split by materiality band; counts are in the "
                       "tooltip and the Review progress table.")
        else:
            st.write("No exceptions.")
    with right:
        st.subheader("Review progress")
        st.dataframe(progress, hide_index=True, width="stretch")
    st.subheader("Summary")
    st.dataframe(wp.summary, hide_index=True, width="stretch")
    st.subheader("Integrity gate")
    st.dataframe(wp.integrity, hide_index=True, width="stretch")

with tab_exceptions:
    view = df_all
    if bands:
        view = view[view["band"].isin(bands)]
    if labels:
        view = view[view["label"].isin(labels)]
    if status == "Not reviewed":
        view = view[view["Disposition"].isna()]
    elif status == "Reviewed":
        view = view[view["Disposition"].notna()]
    view = view.reset_index(drop=True)
    shown = view.map(lambda v: v.date() if isinstance(v, dt.datetime) and v.time() == dt.time() else v)

    st.subheader(f"{run_id} · {len(view)} of {len(df_all)} exceptions shown")
    picked = st.dataframe(shown, hide_index=True, width="stretch", on_select="rerun",
                          selection_mode="multi-row", key=f"table_{run_id}")
    rows = list(getattr(getattr(picked, "selection", None), "rows", []) or [])
    apply_all = st.checkbox(f"Apply to all {len(view)} rows shown by the filters", key="apply_all")
    selected = list(view["exception_id"]) if apply_all else [view["exception_id"].iloc[i] for i in rows]

    if len(rows) == 1 and not apply_all:
        st.markdown("**Selected exception**")
        detail = view.iloc[rows[0]].drop(labels=REVIEW_COLUMNS).dropna()
        st.table(pd.DataFrame({"Field": detail.index, "Value": detail.astype(str).values}))

    st.markdown(f"**Your decision** · {len(selected)} exception(s) selected")
    c1, c2 = st.columns([1, 2])
    with c1:
        disposition = st.selectbox("Disposition", review.ALLOWED, index=None, key="disposition",
                                   placeholder="Choose — the tool does not suggest one")
        review_date = st.date_input("Date", value=dt.date.today(), key="date")
    with c2:
        note = st.text_area("Reviewer note", key="note", height=110)

    missing = [m for m, ok in [("your name (sidebar)", bool((reviewer or "").strip())),
                               ("a disposition", disposition is not None),
                               ("at least one exception", bool(selected))] if not ok]
    if missing:
        st.caption("To save, add " + ", ".join(missing) + ".")
    if st.button("Save to reviewed copy", key="save", type="primary", disabled=bool(missing)):
        try:
            dst = review.record_dispositions(out_dir, run_id, selected, disposition=disposition, note=note,
                                             reviewer=reviewer, date=review_date)
        except review.ReviewError as exc:
            st.error(str(exc))
        else:
            st.success(f"Saved {len(selected)} decision(s) to {dst.name}. The original workpaper is unchanged.")
