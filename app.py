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

import pandas as pd  # noqa: E402

import db  # noqa: E402
import parser as pdf_parser  # noqa: E402
from parser import BACKEND, CATEGORIES, parse_pdf  # noqa: E402

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


if BACKEND == "api":
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

tab_upload, tab_txns, tab_dash, tab_people = st.tabs(
    ["Upload", "Transactions", "Dashboard", "People"]
)

# ---------------------------------------------------------------- Upload
with tab_upload:
    st.subheader("Upload statement PDFs")
    files = st.file_uploader(
        "Drop one or more credit-card statement PDFs",
        type="pdf",
        accept_multiple_files=True,
    )
    if files and st.button("Parse & record", type="primary"):
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
            st.success(
                f"{f.name}: {added} added, {skipped} duplicates skipped "
                f"({len(txns)} found)."
            )

# ---------------------------------------------------------------- Transactions
with tab_txns:
    st.subheader("Transactions")
    rows = db.fetch_transactions()
    if not rows:
        st.info("No transactions yet. Upload a statement to get started.")
    else:
        df = pd.DataFrame(rows)
        people = sorted({r["person"] for r in rows} | {"Unknown"})
        edited = st.data_editor(
            df[["id", "date", "merchant", "amount", "category", "person", "cardholder", "card"]],
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
        if st.button("Save edits"):
            orig = {r["id"]: r for r in rows}
            for _, row in edited.iterrows():
                o = orig[row["id"]]
                if row["category"] != o["category"] or row["person"] != o["person"]:
                    db.update_transaction(
                        row["id"], category=row["category"], person=row["person"]
                    )
            st.success("Saved.")
            st.rerun()

# ---------------------------------------------------------------- Dashboard
with tab_dash:
    st.subheader("Dashboard")
    rows = db.fetch_transactions()
    if not rows:
        st.info("No data yet.")
    else:
        df = pd.DataFrame(rows)
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0)
        df["month"] = df["date"].str.slice(0, 7)

        c1, c2, c3 = st.columns(3)
        c1.metric("Total spent", f"${df['amount'].sum():,.2f}")
        c2.metric("Transactions", len(df))
        c3.metric("Months", df["month"].nunique())

        st.markdown("**By person**")
        st.bar_chart(df.groupby("person")["amount"].sum())

        st.markdown("**By category**")
        st.bar_chart(df.groupby("category")["amount"].sum())

        st.markdown("**By month**")
        st.bar_chart(df.groupby("month")["amount"].sum())

# ---------------------------------------------------------------- People
with tab_people:
    st.subheader("Cardholder to person mapping")
    st.caption(
        "Map the names Claude reads off statements to a person. Useful for shared "
        "accounts or name variations (e.g. 'JANE A DOE' -> 'Jane')."
    )
    mapping = db.get_name_map()
    seen = {r["cardholder"] for r in db.fetch_transactions() if r["cardholder"]}
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

    st.divider()
    with st.form("add_map"):
        st.markdown("**Add a mapping manually**")
        ch = st.text_input("Cardholder name (as on statement)")
        pe = st.text_input("Person")
        if st.form_submit_button("Add") and ch and pe:
            db.set_name_map(ch, pe)
            st.success(f"{ch} -> {pe}")
            st.rerun()
