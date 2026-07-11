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
    """Top filter bar shared by Transactions + Dashboard. Returns the filtered df."""
    if df.empty:
        return df
    people = sorted(x for x in df["person"].dropna().unique() if x != "")
    dts = pd.to_datetime(df["date"], errors="coerce")
    dmin, dmax = dts.min(), dts.max()
    months_desc = sorted(
        (x for x in df["month"].dropna().unique() if x != ""), reverse=True
    )

    c1, c2, c3 = st.columns([2, 3, 2])
    sel_people = c1.multiselect("Person", people, default=people)

    presets = ["All time", "Latest month", "Recent 3 months",
               "Recent 6 months", "Recent 12 months"]
    month_opts = [f"Month: {m}" for m in months_desc]
    choice = c2.selectbox("Period", presets + month_opts + ["Custom dates…"])
    search = c3.text_input("Search merchant")

    # Resolve the chosen period to a [start, end] datetime window.
    start, end = dmin, dmax
    if choice == "Latest month" and pd.notna(dmax):
        start = dmax.replace(day=1)
    elif choice.startswith("Recent ") and pd.notna(dmax):
        n = int(choice.split()[1])
        start = dmax - pd.DateOffset(months=n)
    elif choice.startswith("Month: "):
        start = pd.to_datetime(choice.split("Month: ", 1)[1] + "-01")
        end = start + pd.offsets.MonthEnd(1)
    elif choice == "Custom dates…" and pd.notna(dmin) and pd.notna(dmax):
        rng = c2.date_input(
            "Date range", value=(dmin.date(), dmax.date()),
            min_value=dmin.date(), max_value=dmax.date(),
        )
        if isinstance(rng, (tuple, list)) and len(rng) == 2:
            start, end = pd.Timestamp(rng[0]), pd.Timestamp(rng[1])

    mask = df["person"].isin(sel_people)
    if pd.notna(start) and pd.notna(end):
        mask = mask & (dts >= start) & (dts <= end)
    if search:
        mask = mask & df["merchant"].str.contains(search, case=False, na=False)
    return df[mask]


st.markdown("##### Filters")
fdf = _build_filters(df_all)

tab_upload, tab_txns, tab_dash, tab_people, tab_history = st.tabs(
    ["Upload", "Transactions", "Dashboard", "People", "History"]
)

# ---------------------------------------------------------------- Upload
with tab_upload:
    st.subheader("Upload statement PDFs")
    if st.session_state.pop("upload_done", None):
        for msg in st.session_state.pop("upload_msgs", []):
            st.success(msg)
    files = st.file_uploader(
        "Drop one or more credit-card or debit/checking statement PDFs",
        type="pdf",
        accept_multiple_files=True,
    )
    force = st.checkbox("Re-import even if a statement was already uploaded")
    if files and st.button("Parse & record", type="primary"):
        msgs = []
        for f in files:
            data = f.getvalue()
            fhash = db.statement_hash(data)
            if db.statement_seen(fhash) and not force:
                msgs.append(
                    f"{f.name}: this exact statement was already uploaded — skipped. "
                    "(Tick 'Re-import' above to force it.)"
                )
                continue
            with st.spinner(f"Reading {f.name} with Claude..."):
                try:
                    txns = parse_pdf(data)
                except Exception as e:  # noqa: BLE001
                    st.error(f"{f.name}: {e}")
                    continue
                for t in txns:
                    t["person"] = db.resolve_person(
                        t.get("cardholder", ""), t.get("card", ""), t.get("merchant", "")
                    )
                added, skipped = db.insert_transactions(txns, f.name)
                db.record_statement(fhash, f.name, added)
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
            batch = db.new_batch()
            n = 0
            for _, row in edited.iterrows():
                o = orig[row["id"]]
                if row["category"] != o["category"] or row["person"] != o["person"]:
                    db.update_transaction(
                        row["id"], category=row["category"], person=row["person"],
                        batch=batch, label="Edit transactions",
                    )
                    n += 1
            st.success(f"Saved {n} change(s).")
            st.rerun()

    if not df_all.empty:
        with st.expander("Clean up duplicate transactions"):
            st.caption(
                "Finds charges with the same date, amount, merchant and card — "
                "usually from uploading a statement twice — keeping the oldest copy. "
                "New uploads of the same file are now blocked automatically; use this "
                "to clean up ones saved before. A genuinely repeated same-day charge "
                "could also show up here, so untick anything you want to keep."
            )
            dup_ids = db.find_duplicate_ids()
            if not dup_ids:
                st.success("No duplicates found.")
            else:
                dups = df_all[df_all["id"].isin(dup_ids)][
                    ["id", "date", "merchant", "amount", "card", "person"]
                ].copy()
                dups.insert(0, "remove", True)
                st.write(f"Found **{len(dup_ids)}** likely duplicate(s). "
                         "Ticked rows will be deleted:")
                picked = st.data_editor(
                    dups,
                    column_config={
                        "id": None,
                        "remove": st.column_config.CheckboxColumn("Remove"),
                        "amount": st.column_config.NumberColumn("Amount", format="$%.2f"),
                    },
                    disabled=["date", "merchant", "amount", "card", "person"],
                    hide_index=True,
                    use_container_width=True,
                    key="dup_editor",
                )
                to_delete = [r["id"] for _, r in picked.iterrows() if r["remove"]]
                if st.button(f"Delete {len(to_delete)} selected", type="primary",
                             disabled=not to_delete):
                    n = db.delete_transactions(to_delete)
                    st.success(f"Deleted {n} duplicate(s).")
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
    st.subheader("Who spent it — cardholder to person")
    rules_txt = ", ".join(f"**{last} → {person}**" for last, person in db.NAME_RULES)
    st.caption(
        "Built-in rules match the **last name** shown on each charge — including "
        "when an authorized user's name appears instead of the account owner's. "
        f"Currently: {rules_txt}. These apply automatically to new uploads. "
        "Use the manual override below only for names the rules don't cover."
    )

    # Show how each cardholder seen on statements currently resolves, so you can
    # spot any name the built-in rules miss.
    seen = sorted({r["cardholder"] for r in all_rows if r["cardholder"]})
    if seen:
        st.dataframe(
            pd.DataFrame(
                [{"Cardholder on statement": n, "Mapped to": db.resolve_person(n)}
                 for n in seen]
            ),
            hide_index=True,
            use_container_width=True,
        )

    st.divider()
    st.markdown("##### Cards — assign each card to its owner")
    st.caption(
        "Credit-card statements label each charge with the **authorized user** who "
        "made it, so a card you share (e.g. you buying on your partner's card) gets "
        "split by name. Assign a card to its **owner** here and every charge on it "
        "goes to that person — ignoring the name on individual transactions. This "
        "overrides the name rules above."
    )
    card_map = db.get_card_map()
    cards = sorted({r["card"] for r in all_rows if r["card"]} | set(card_map))
    person_opts = list(dict.fromkeys(
        ["(auto — use name)"]
        + sorted({db.resolve_person(c) for c in seen} | {"Neo", "Jessica"})
    ))
    if not cards:
        st.caption("No cards seen yet. Upload a statement first.")
    for card in cards:
        col1, col2 = st.columns([2, 2])
        col1.text(card)
        current = card_map.get(card, "(auto — use name)")
        idx = person_opts.index(current) if current in person_opts else 0
        choice = col2.selectbox(
            "card owner", person_opts, index=idx,
            key=f"card_{card}", label_visibility="collapsed",
        )
        if choice != current:
            if choice == "(auto — use name)":
                db.clear_card_map(card)
                db.reapply_mappings()
            else:
                db.set_card_map(card, choice)
            st.toast(f"{card} -> {choice}")
            st.rerun()

    st.divider()
    if st.button("Re-apply rules to existing transactions", type="primary"):
        n = db.reapply_mappings()
        st.success(f"Updated {n} transaction(s).")
        st.rerun()

    with st.form("add_map"):
        st.markdown("**Manual override** (for a name the rules don't catch)")
        ch = st.text_input("Cardholder name (exactly as on statement)")
        pe = st.text_input("Person")
        if st.form_submit_button("Add") and ch and pe:
            db.set_name_map(ch, pe)
            st.success(f"{ch} -> {pe}")
            st.rerun()

# ---------------------------------------------------------------- History
with tab_history:
    st.subheader("History — undo a mistake")
    st.caption(
        "Every upload, edit, and delete is recorded here. Click **Undo** to reverse "
        "one — deleted transactions come back, edits roll back, and an upload's rows "
        "are removed. (Only actions taken from now on are tracked.)"
    )
    actions = db.fetch_history()
    if not actions:
        st.info("No actions recorded yet.")
    else:
        for a in actions:
            ts = a["ts"]
            when = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
            c1, c2 = st.columns([5, 1])
            status = " · _already undone_" if a["undone"] else ""
            c1.markdown(
                f"**{a['label']}** — {a['count']} row(s) · {when}{status}"
            )
            if c2.button("Undo", key=f"undo_{a['batch']}", disabled=a["undone"]):
                n = db.undo_batch(a["batch"])
                st.success(f"Reversed {n} change(s).")
                st.rerun()
