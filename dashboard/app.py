"""Dashboard for the OSS triage copilot -- shows real issue volume trends
from the full historical corpus, plus shadow-mode triage activity from the
watcher (src/watch_new_issues.py).

Deliberately does NOT claim to show "response-time impact" or "label
accuracy" -- neither is measurable yet. Nothing has been posted to GitHub
(shadow mode, pending maintainer approval), and there's no feedback loop
comparing suggested labels against what a maintainer actually chose. What's
shown here is real triage activity, not validated accuracy.

Run locally:
    streamlit run dashboard/app.py

Deploy free on Streamlit Community Cloud: point it at this repo, main file
path dashboard/app.py. It reads data/triage.db directly, which is already
committed to the repo by the ingest and watcher workflows -- no separate
database setup needed.
"""

import json
import sqlite3
from pathlib import Path

import pandas as pd
import streamlit as st

DB_PATH = "data/triage.db"
REPO = "Textualize/textual"

st.set_page_config(page_title="OSS Triage Copilot", page_icon="\U0001f50d", layout="wide")


@st.cache_data(ttl=300)
def load_data(db_path: str):
    if not Path(db_path).exists():
        return pd.DataFrame(), pd.DataFrame()

    conn = sqlite3.connect(db_path)
    issues = pd.read_sql_query("SELECT * FROM issues", conn)

    try:
        triaged = pd.read_sql_query("SELECT * FROM triaged_log", conn)
    except pd.errors.DatabaseError:
        # triaged_log doesn't exist yet -- fine, the watcher just hasn't run yet
        triaged = pd.DataFrame(
            columns=["number", "triaged_at", "labels", "priority",
                     "duplicate_confidence", "likely_duplicate_of", "output"]
        )
    conn.close()
    return issues, triaged


def main():
    st.title("\U0001f50d OSS Triage Copilot")
    st.caption(f"For {REPO} — built as a hands-on RAG + LLM agent project")

    st.warning(
        "**Shadow mode.** Nothing shown here has been posted to GitHub. This bot reads "
        "new issues and drafts triage suggestions, but posting requires maintainer "
        "approval, which hasn't happened yet.",
        icon="\u26a0\ufe0f",
    )

    issues, triaged = load_data(DB_PATH)

    if issues.empty:
        st.error(f"No data found at `{DB_PATH}`. Run `src/ingest_issues.py` first.")
        return

    # --- Top-line metrics ---
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Issues in corpus", f"{len(issues):,}")
    col2.metric("Open issues", f"{(issues['state'] == 'open').sum():,}")
    col3.metric("Triaged so far (shadow mode)", f"{len(triaged):,}")
    if len(triaged) > 0:
        dup_rate = (triaged["duplicate_confidence"] != "none").mean() * 100
        col4.metric("Duplicate flagged", f"{dup_rate:.0f}%")
    else:
        col4.metric("Duplicate flagged", "—")

    st.divider()

    # --- Issue volume over time (real historical data, not shadow-mode-dependent) ---
    st.subheader("Issue volume over time")
    issues_dated = issues.copy()
    issues_dated["created_at"] = pd.to_datetime(issues_dated["created_at"], errors="coerce")
    issues_dated = issues_dated.dropna(subset=["created_at"])
    monthly = issues_dated.set_index("created_at").resample("ME").size()
    monthly.index = monthly.index.strftime("%Y-%m")
    st.bar_chart(monthly)

    st.divider()

    # --- Shadow-mode triage activity ---
    if triaged.empty:
        st.info(
            "No issues triaged yet. The watcher runs every 30 minutes "
            "(`.github/workflows/triage-watch.yml`) -- check back soon."
        )
        return

    col_a, col_b, col_c = st.columns(3)

    with col_a:
        st.subheader("Suggested labels")
        all_labels = [label for row in triaged["labels"] for label in json.loads(row)]
        if all_labels:
            st.bar_chart(pd.Series(all_labels).value_counts())
        else:
            st.caption("No labels suggested yet.")

    with col_b:
        st.subheader("Priority")
        st.bar_chart(triaged["priority"].value_counts())

    with col_c:
        st.subheader("Duplicate confidence")
        st.bar_chart(triaged["duplicate_confidence"].value_counts())

    st.divider()

    # --- Browsable detail view ---
    st.subheader("Triage decisions")
    triaged_sorted = triaged.sort_values("triaged_at", ascending=False)

    options = [
        f"#{row.number} — {row.priority} priority, dup: {row.duplicate_confidence}"
        for row in triaged_sorted.itertuples()
    ]
    if options:
        selected = st.selectbox("View a specific decision", options)
        selected_number = int(selected.split(" ")[0].lstrip("#"))
        selected_row = triaged_sorted[triaged_sorted["number"] == selected_number].iloc[0]

        st.markdown(f"[Open issue #{selected_number} on GitHub](https://github.com/{REPO}/issues/{selected_number})")
        st.markdown(selected_row["output"], unsafe_allow_html=True)

    with st.expander("All triaged issues (table)"):
        display_df = triaged_sorted[["number", "triaged_at", "priority", "duplicate_confidence"]].copy()
        st.dataframe(display_df, use_container_width=True, hide_index=True)

    st.divider()
    st.caption(
        "**Known limitations:** \"Response-time impact\" and \"label accuracy\" aren't shown "
        "above because neither is measurable yet -- there's no live posting to compare "
        "against, and no human-feedback loop confirming whether suggested labels match "
        "what a maintainer would actually choose. See the README's Known Limitations "
        "section for the specific case (#5225) that shaped this project's safety-net design."
    )


if __name__ == "__main__":
    main()