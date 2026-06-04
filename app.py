"""Household spending tracker — upload statements, auto-record transactions."""

import os

import streamlit as st

# Load deployment config from Streamlit secrets into the environment BEFORE
# importing db/parser (which read these as env vars). Lets the same code run
# locally (env vars) and on Streamlit Cloud (secrets).
for _k in ("SPENDING_BACKEND", "ANTHROPIC_API_KEY", "SPENDING_MODEL",
           "DATABASE_URL", "APP_PASSWORD", "CLAUDE_BIN"):
    try:
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = str(st.secrets[_k])
    except Exception:  # noqa: BLE001 - no secrets file locally is fine
        pass

# When running in the cloud there's no Claude binary, so default to the API
# backend if an API key is present and no explicit backend was chosen.
if not os.environ.get("SPENDING_BACKEND") and os.environ.get("ANTHROPIC_API_KEY"):
    os.environ["SPENDING_BACKEND"] = "api"

import altair as alt  # noqa: E402
import pandas as pd  # noqa: E402

import db  # noqa: E402
import parser as pdf_parser  # noqa: E402
from parser import CATEGORIES, parse_pdf  # noqa: E402

st.set_page_config(page_title="Household Spending", page_icon="$", layout="wide")
db.init_db()


def _app_password():
    """Password from Streamlit secrets or the APP_PASSWORD env var (None = off)."""
    try:
        if "APP_PASSWORD" in st.secrets:
            return st.secrets["APP_PASSWORD"]
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("APP_PASSWORD")


def require_login():
    """Gate the app behind a password when one is configured. No password = open
    (fine for local use; ALWAYS set one before exposing the app over a tunnel)."""
    pw = _app_password()
    if not pw:
        return
    if st.session_state.get("auth_ok"):
        return

    def _check():
        if st.session_state.get("pw_input") == pw:
            st.session_state["auth_ok"] = True
        else:
            st.session_state["auth_ok"] = False
        st.session_state.pop("pw_input", None)

    st.title("Household Spending Tracker")
    st.text_input("Password", type="password", key="pw_input", on_change=_check)
    if st.session_state.get("auth_ok") is False:
        st.error("Wrong password.")
    st.stop()


require_login()

st.title("Household Spending Tracker")


def _claude_bin():
    try:
        return pdf_parser.find_claude_bin()
    except Exception:  # noqa: BLE001
        return None


_backend = pdf_parser.backend()
st.caption(f"Parsing mode: **{_backend}**")

if _backend == "api":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        st.warning(
            "API backend selected but ANTHROPIC_API_KEY is not set. Set it in your "
            "environment or Streamlit secrets, or switch to the free CLI backend "
            "(unset SPENDING_BACKEND)."
        )
elif not pdf_parser.cli_logged_in():
    cb = _claude_bin()
    st.warning("Claude Code is not logged in — statements can't be parsed yet.")
    if cb:
        st.code(f'"{cb}" auth login --claudeai', language="bash")
        st.caption(
            "Run that once in a terminal and sign in with your Claude subscription. "
            "Then reload this page. No API cost — it uses your subscription."
        )
    else:
        st.caption("Could not find the Claude Code binary. Set CLAUDE_BIN to its path.")

# Fetch all data once, then build a shared filtered view used by the table + dashboard.
all_rows = db.fetch_transactions()
df_all = pd.DataFrame(all_rows)
if not df_all.empty:
    df_all["amount"] = pd.to_numeric(df_all["amount"], errors="coerce").fillna(0)
    df_all["month"] = df_all["date"].astype(str).str.slice(0, 7)


def _build_filters(df):
    """Sidebar filters shared by Transactions + Dashboard. Returns the filtered df."""
    if df.empty:
        return df
    st.sidebar.header("Filters")
    people = sorted(x for x in df["person"].dropna().unique() if x != "")
    cats = sorted(x for x in df["category"].dropna().unique() if x != "")
    months = sorted(x for x in df["month"].dropna().unique() if x != "")
    cards = sorted(x for x in df["card"].dropna().unique() if x != "")

    sel_people = st.sidebar.multiselect("Person", people, default=people)
    sel_cats = st.sidebar.multiselect("Category", cats, default=cats)
    sel_months = st.sidebar.multiselect("Month", months, default=months)
    sel_cards = st.sidebar.multiselect("Card", cards, default=cards)
    search = st.sidebar.text_input("Search merchant")
    if st.sidebar.button("Reset filters"):
        st.rerun()

    out = df[
        df["person"].isin(sel_people)
        & df["category"].isin(sel_cats)
        & df["month"].isin(sel_months)
        & df["card"].isin(sel_cards)
    ]
    if search:
        out = out[out["merchant"].str.contains(search, case=False, na=False)]
    return out


fdf = _build_filters(df_all)

tab_upload, tab_txns, tab_dash, tab_people = st.tabs(
    ["Upload", "Transactions", "Dashboard", "People"]
)

# ---------------------------------------------------------------- Upload
with tab_upload:
    st.subheader("Upload statement PDFs")
    if st.session_state.pop("upload_done", None):
        for msg in st.session_state.pop("upload_msgs", []):
            st.success(msg)
    files = st.file_uploader(
        "Drop one or more credit-card statement PDFs",
        type="pdf",
        accept_multiple_files=True,
    )
    if files and st.button("Parse & record", type="primary"):
        msgs = []
        for f in files:
            with st.spinner(f"Reading {f.name} with Claude..."):
                try:
                    txns = parse_pdf(f.getvalue())
                except Exception as e:  # noqa: BLE001
                    st.error(f"{f.name}: {e}")
                    continue
                for t in txns:
                    t["person"] = db.resolve_person(t.get("cardholder", ""))
                added, skipped = db.insert_transactions(txns, f.name)
            msgs.append(
                f"{f.name}: {added} added, {skipped} duplicates skipped "
                f"({len(txns)} found)."
            )
        st.session_state["upload_msgs"] = msgs
        st.session_state["upload_done"] = True
        st.rerun()

# ---------------------------------------------------------------- Transactions
with tab_txns:
    st.subheader("Transactions")
    if df_all.empty:
        st.info("No transactions yet. Upload a statement to get started.")
    elif fdf.empty:
        st.warning("No transactions match the current filters.")
    else:
        st.caption(
            f"Showing {len(fdf)} of {len(df_all)} transactions · "
            f"filtered total ${fdf['amount'].sum():,.2f}"
        )
        people = sorted(set(df_all["person"].dropna()) | {"Unknown"})
        edited = st.data_editor(
            fdf[["id", "date", "merchant", "amount", "category", "person",
                 "cardholder", "card"]],
            column_config={
                "id": None,
                "cardholder": st.column_config.TextColumn("Cardholder", disabled=True),
                "card": st.column_config.TextColumn("Card", disabled=True),
                "amount": st.column_config.NumberColumn("Amount", format="$%.2f", disabled=True),
                "date": st.column_config.TextColumn("Date", disabled=True),
                "merchant": st.column_config.TextColumn("Merchant", disabled=True),
                "category": st.column_config.SelectboxColumn("Category", options=CATEGORIES),
                "person": st.column_config.SelectboxColumn("Person", options=people),
            },
            hide_index=True,
            use_container_width=True,
            key="txn_editor",
        )
        if st.button("Save edits", type="primary"):
            orig = {r["id"]: r for r in all_rows}
            n = 0
            for _, row in edited.iterrows():
                o = orig[row["id"]]
                if row["category"] != o["category"] or row["person"] != o["person"]:
                    db.update_transaction(
                        row["id"], category=row["category"], person=row["person"]
                    )
                    n += 1
            st.success(f"Saved {n} change(s).")
            st.rerun()

# ---------------------------------------------------------------- Dashboard
with tab_dash:
    if df_all.empty:
        st.info("No data yet.")
    elif fdf.empty:
        st.warning("No transactions match the current filters.")
    else:
        total = fdf["amount"].sum()
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Total spent", f"${total:,.0f}")
        c2.metric("Transactions", f"{len(fdf):,}")
        c3.metric("Avg / transaction", f"${total / max(len(fdf), 1):,.0f}")
        c4.metric("Months", fdf["month"].nunique())

        st.markdown("##### Spending over time")
        by_mp = fdf.groupby(["month", "person"], as_index=False)["amount"].sum()
        st.altair_chart(
            alt.Chart(by_mp).mark_bar().encode(
                x=alt.X("month:N", title=None),
                y=alt.Y("amount:Q", title="Spent ($)", stack="zero"),
                color=alt.Color("person:N", title="Person",
                                scale=alt.Scale(scheme="tableau10")),
                tooltip=["month", "person",
                         alt.Tooltip("amount:Q", format="$,.2f", title="Spent")],
            ).properties(height=300),
            use_container_width=True,
        )

        col_a, col_b = st.columns(2)
        with col_a:
            st.markdown("##### By category")
            by_cat = fdf.groupby("category", as_index=False)["amount"].sum()
            st.altair_chart(
                alt.Chart(by_cat).mark_bar().encode(
                    x=alt.X("amount:Q", title="Spent ($)"),
                    y=alt.Y("category:N", sort="-x", title=None),
                    color=alt.Color("category:N", legend=None,
                                    scale=alt.Scale(scheme="tableau20")),
                    tooltip=["category",
                             alt.Tooltip("amount:Q", format="$,.2f", title="Spent")],
                ).properties(height=320),
                use_container_width=True,
            )
        with col_b:
            st.markdown("##### By person")
            by_p = fdf.groupby("person", as_index=False)["amount"].sum()
            st.altair_chart(
                alt.Chart(by_p).mark_arc(innerRadius=60).encode(
                    theta=alt.Theta("amount:Q", stack=True),
                    color=alt.Color("person:N", title="Person",
                                    scale=alt.Scale(scheme="tableau10")),
                    tooltip=["person",
                             alt.Tooltip("amount:Q", format="$,.2f", title="Spent")],
                ).properties(height=320),
                use_container_width=True,
            )

        st.markdown("##### Top merchants")
        by_m = (fdf.groupby("merchant", as_index=False)["amount"].sum()
                .sort_values("amount", ascending=False).head(12))
        st.altair_chart(
            alt.Chart(by_m).mark_bar().encode(
                x=alt.X("amount:Q", title="Spent ($)"),
                y=alt.Y("merchant:N", sort="-x", title=None),
                color=alt.Color("amount:Q", legend=None,
                                scale=alt.Scale(scheme="blues")),
                tooltip=["merchant",
                         alt.Tooltip("amount:Q", format="$,.2f", title="Spent")],
            ).properties(height=360),
            use_container_width=True,
        )

# ---------------------------------------------------------------- People
with tab_people:
    st.subheader("Cardholder to person mapping")
    st.caption(
        "Map the names Claude reads off statements to a person. Changing a mapping "
        "now updates existing transactions too (not just new uploads)."
    )
    mapping = db.get_name_map()
    seen = {r["cardholder"] for r in all_rows if r["cardholder"]}
    for name in sorted(seen | set(mapping)):
        col1, col2 = st.columns([2, 2])
        col1.text(name or "(blank)")
        val = col2.text_input(
            "person", value=mapping.get(name, ""), key=f"map_{name}",
            label_visibility="collapsed", placeholder="person name",
        )
        if val and val != mapping.get(name, ""):
            db.set_name_map(name, val)
            st.toast(f"{name} -> {val}")
            st.rerun()

    st.divider()
    with st.form("add_map"):
        st.markdown("**Add a mapping manually**")
        ch = st.text_input("Cardholder name (as on statement)")
        pe = st.text_input("Person")
        if st.form_submit_button("Add") and ch and pe:
            db.set_name_map(ch, pe)
            st.success(f"{ch} -> {pe}")
            st.rerun()

    if st.button("Re-apply all mappings to existing transactions"):
        n = db.reapply_mappings()
        st.success(f"Re-applied mappings to {n} transaction(s).")
        st.rerun()
