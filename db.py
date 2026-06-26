"""Storage for the spending tracker.

Uses SQLAlchemy so the same code runs on:
  - local SQLite  (default: a spending.db file)
  - cloud Postgres (set DATABASE_URL, e.g. a free Neon database) for an always-on
    deployed app where data must persist.
"""

import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import uuid

from sqlalchemy import (
    Column, Float, Integer, MetaData, String, Table, DateTime,
    create_engine, delete, insert, select, update,
)


def _database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        path = os.path.join(os.path.dirname(__file__), "spending.db")
        return f"sqlite:///{path}"
    # SQLAlchemy wants the 'postgresql://' scheme, not 'postgres://'.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


ENGINE = create_engine(_database_url(), pool_pre_ping=True)
META = MetaData()

transactions = Table(
    "transactions", META,
    Column("id", String, primary_key=True),
    Column("date", String),
    Column("merchant", String),
    Column("amount", Float),
    Column("category", String),
    Column("cardholder", String),
    Column("person", String),
    Column("card", String),
    Column("statement_file", String),
    Column("created_at", DateTime, default=_dt.datetime.utcnow),
)

name_map = Table(
    "name_map", META,
    Column("cardholder", String, primary_key=True),
    Column("person", String),
)

# Maps a whole account (by the card/account string on the statement) to a person
# or to "Shared". Used for joint accounts where the statement shows one name but
# the spending belongs to both — e.g. a checking account that pays rent/utilities.
card_map = Table(
    "card_map", META,
    Column("card", String, primary_key=True),
    Column("person", String),
)

# Maps a specific payee/merchant to a person. The key case: a joint account that
# pays each partner's OWN rent/utilities (long-distance, separate bills) — the
# statement can't say whose, but the landlord/provider name can. Most specific.
merchant_map = Table(
    "merchant_map", META,
    Column("merchant", String, primary_key=True),
    Column("person", String),
)

# Records which statement PDFs have been imported, keyed by a hash of the file's
# bytes. Lets us reject re-uploading the same file even when parsing is not
# byte-for-byte reproducible.
statements = Table(
    "statements", META,
    Column("file_hash", String, primary_key=True),
    Column("filename", String),
    Column("txn_count", Integer),
    Column("imported_at", DateTime, default=_dt.datetime.utcnow),
)

# Audit log of every change to transactions so any action (upload, edit, delete)
# can be undone. One user action shares a `batch` id; `before`/`after` hold the
# row as JSON so a delete can be re-inserted and an edit rolled back.
history = Table(
    "history", META,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, default=_dt.datetime.utcnow),
    Column("action", String),   # insert | update | delete
    Column("batch", String),    # groups the rows touched by one user action
    Column("label", String),    # human-readable description
    Column("txn_id", String),
    Column("before", String),   # JSON of the row before (None for insert)
    Column("after", String),    # JSON of the row after (None for delete)
    Column("undone", Integer, default=0),
)


def init_db():
    META.create_all(ENGINE)


def new_batch():
    return uuid.uuid4().hex


def _row_json(row):
    """Serialize a transaction row dict to JSON (datetimes -> ISO strings)."""
    if row is None:
        return None
    d = dict(row)
    if isinstance(d.get("created_at"), _dt.datetime):
        d["created_at"] = d["created_at"].isoformat()
    return json.dumps(d)


def _json_row(s):
    if not s:
        return None
    d = json.loads(s)
    ca = d.get("created_at")
    if isinstance(ca, str):
        try:
            d["created_at"] = _dt.datetime.fromisoformat(ca)
        except ValueError:
            d["created_at"] = _dt.datetime.utcnow()
    return d


def _log(conn, action, batch, label, txn_id, before=None, after=None):
    conn.execute(insert(history).values(
        ts=_dt.datetime.utcnow(), action=action, batch=batch, label=label,
        txn_id=txn_id, before=_row_json(before), after=_row_json(after), undone=0,
    ))


def make_id(date, amount, merchant, card, occ=0):
    # `occ` distinguishes legitimately identical charges within one statement
    # (e.g. two $4 coffees, same day, same merchant) while keeping re-uploads of
    # the same statement idempotent (same charges -> same occ order -> same ids).
    raw = f"{date}|{amount}|{merchant}|{card}|{occ}".lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# Built-in household rules: match the LAST NAME shown on the statement, in any
# position and case-insensitively. The statement shows whoever made the charge
# — including an authorized user whose name differs from the account owner — so
# matching on the displayed name is correct. Add (last_name, person) pairs here.
NAME_RULES = [
    ("Yuan", "Neo"),
    ("Jia", "Jessica"),
]


def _person_from_rules(cardholder):
    """Return the person for a cardholder via the built-in last-name rules, or
    None if no rule matches. Whole-token match so 'Jia' won't hit 'Jiang'."""
    if not cardholder:
        return None
    tokens = set(re.split(r"[\s,]+", cardholder.lower().strip()))
    for last, person in NAME_RULES:
        if last.lower() in tokens:
            return person
    return None


def resolve_person(cardholder, card="", merchant=""):
    # Most specific wins: a payee rule (each partner's own landlord/utility on a
    # joint account) > a whole-account rule > the name on the statement.
    if merchant:
        with ENGINE.connect() as conn:
            mrow = conn.execute(
                select(merchant_map.c.person).where(
                    merchant_map.c.merchant.ilike(merchant)
                )
            ).fetchone()
        if mrow:
            return mrow[0]
    if card:
        with ENGINE.connect() as conn:
            crow = conn.execute(
                select(card_map.c.person).where(card_map.c.card.ilike(card))
            ).fetchone()
        if crow:
            return crow[0]
    if not cardholder:
        return "Unknown"
    ruled = _person_from_rules(cardholder)
    if ruled:
        return ruled
    # Fall back to a manual override in name_map, then the raw name.
    with ENGINE.connect() as conn:
        row = conn.execute(
            select(name_map.c.person).where(
                name_map.c.cardholder.ilike(cardholder)
            )
        ).fetchone()
    return row[0] if row else cardholder


def set_name_map(cardholder, person):
    with ENGINE.begin() as conn:
        exists = conn.execute(
            select(name_map.c.cardholder).where(name_map.c.cardholder == cardholder)
        ).fetchone()
        if exists:
            conn.execute(
                update(name_map)
                .where(name_map.c.cardholder == cardholder)
                .values(person=person)
            )
        else:
            conn.execute(insert(name_map).values(cardholder=cardholder, person=person))
    # Recompute all rows so the right precedence (merchant > card > name) holds,
    # instead of blindly overwriting rows that have a more specific rule.
    reapply_mappings()


def reapply_mappings():
    """Re-derive every transaction's person from the card mapping, built-in
    last-name rules, and manual name overrides. Returns the number changed."""
    rows = fetch_transactions()
    changes = []
    for r in rows:
        np = resolve_person(
            r.get("cardholder", ""), r.get("card", ""), r.get("merchant", "")
        )
        if np != r["person"]:
            changes.append((r["id"], np))
    with ENGINE.begin() as conn:
        for tid, person in changes:
            conn.execute(
                update(transactions)
                .where(transactions.c.id == tid)
                .values(person=person)
            )
    return len(changes)


def get_name_map():
    with ENGINE.connect() as conn:
        rows = conn.execute(
            select(name_map.c.cardholder, name_map.c.person).order_by(
                name_map.c.cardholder
            )
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def get_card_map():
    with ENGINE.connect() as conn:
        rows = conn.execute(
            select(card_map.c.card, card_map.c.person).order_by(card_map.c.card)
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def set_card_map(card, person):
    with ENGINE.begin() as conn:
        exists = conn.execute(
            select(card_map.c.card).where(card_map.c.card == card)
        ).fetchone()
        if exists:
            conn.execute(
                update(card_map).where(card_map.c.card == card).values(person=person)
            )
        else:
            conn.execute(insert(card_map).values(card=card, person=person))
    reapply_mappings()  # respects merchant > card > name precedence


def clear_card_map(card):
    """Remove a card override; existing rows keep their value until re-applied."""
    with ENGINE.begin() as conn:
        conn.execute(delete(card_map).where(card_map.c.card == card))


def get_merchant_map():
    with ENGINE.connect() as conn:
        rows = conn.execute(
            select(merchant_map.c.merchant, merchant_map.c.person).order_by(
                merchant_map.c.merchant
            )
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def set_merchant_map(merchant, person):
    with ENGINE.begin() as conn:
        exists = conn.execute(
            select(merchant_map.c.merchant).where(merchant_map.c.merchant == merchant)
        ).fetchone()
        if exists:
            conn.execute(
                update(merchant_map)
                .where(merchant_map.c.merchant == merchant)
                .values(person=person)
            )
        else:
            conn.execute(insert(merchant_map).values(merchant=merchant, person=person))
    reapply_mappings()  # respects merchant > card > name precedence


def clear_merchant_map(merchant):
    with ENGINE.begin() as conn:
        conn.execute(delete(merchant_map).where(merchant_map.c.merchant == merchant))


def insert_transactions(txns, statement_file):
    """Insert transaction dicts. Returns (added, skipped_duplicates)."""
    added = skipped = 0
    occ_counts = {}
    batch = new_batch()
    label = f"Upload {statement_file}"
    with ENGINE.begin() as conn:
        for t in txns:
            base = (t["date"], t["amount"], t["merchant"], t.get("card", ""))
            occ = occ_counts.get(base, 0)
            occ_counts[base] = occ + 1
            tid = make_id(t["date"], t["amount"], t["merchant"], t.get("card", ""), occ)
            exists = conn.execute(
                select(transactions.c.id).where(transactions.c.id == tid)
            ).fetchone()
            if exists:
                skipped += 1
                continue
            vals = dict(
                id=tid,
                date=t["date"],
                merchant=t["merchant"],
                amount=t["amount"],
                category=t.get("category", "Uncategorized"),
                cardholder=t.get("cardholder", ""),
                person=t.get("person", resolve_person(
                    t.get("cardholder", ""), t.get("card", ""), t.get("merchant", ""))),
                card=t.get("card", ""),
                statement_file=statement_file,
                created_at=_dt.datetime.utcnow(),
            )
            conn.execute(insert(transactions).values(**vals))
            _log(conn, "insert", batch, label, tid, before=None, after=vals)
            added += 1
    return added, skipped


def update_transaction(tid, category=None, person=None, batch=None, label="Edit"):
    vals = {}
    if category is not None:
        vals["category"] = category
    if person is not None:
        vals["person"] = person
    if not vals:
        return
    with ENGINE.begin() as conn:
        before = conn.execute(
            select(transactions).where(transactions.c.id == tid)
        ).fetchone()
        before = dict(before._mapping) if before else None
        conn.execute(update(transactions).where(transactions.c.id == tid).values(**vals))
        after = conn.execute(
            select(transactions).where(transactions.c.id == tid)
        ).fetchone()
        after = dict(after._mapping) if after else None
        _log(conn, "update", batch or new_batch(), label, tid, before, after)


def delete_transaction(tid, batch=None, label="Delete transaction"):
    delete_transactions([tid], batch=batch, label=label)


def delete_transactions(ids, batch=None, label="Delete duplicates"):
    """Delete many transactions by id. Returns the number removed."""
    ids = list(ids)
    if not ids:
        return 0
    batch = batch or new_batch()
    with ENGINE.begin() as conn:
        rows = conn.execute(
            select(transactions).where(transactions.c.id.in_(ids))
        ).fetchall()
        for r in rows:
            m = dict(r._mapping)
            _log(conn, "delete", batch, label, m["id"], before=m, after=None)
        res = conn.execute(delete(transactions).where(transactions.c.id.in_(ids)))
    return res.rowcount or 0


def fetch_transactions():
    with ENGINE.connect() as conn:
        rows = conn.execute(
            select(transactions).order_by(
                transactions.c.date.desc(), transactions.c.created_at.desc()
            )
        ).fetchall()
    return [dict(r._mapping) for r in rows]


# ----------------------------------------------------------------- history / undo
def fetch_history(limit=50):
    """Recent actions, newest first, grouped into one row per user action."""
    with ENGINE.connect() as conn:
        rows = conn.execute(select(history).order_by(history.c.id.desc())).fetchall()
    batches, order = {}, []
    for r in rows:
        m = r._mapping
        b = m["batch"]
        if b not in batches:
            batches[b] = {"batch": b, "ts": m["ts"], "action": m["action"],
                          "label": m["label"], "count": 0, "undone": True}
            order.append(b)
        batches[b]["count"] += 1
        if not m["undone"]:
            batches[b]["undone"] = False  # batch is still active if any row stands
    return [batches[b] for b in order][:limit]


def undo_batch(batch):
    """Reverse every not-yet-undone change in a batch. Returns rows affected."""
    with ENGINE.begin() as conn:
        entries = conn.execute(
            select(history)
            .where(history.c.batch == batch, history.c.undone == 0)
            .order_by(history.c.id.desc())
        ).fetchall()
        n = 0
        for e in entries:
            m = e._mapping
            action = m["action"]
            if action == "insert":
                conn.execute(
                    delete(transactions).where(transactions.c.id == m["txn_id"])
                )
            elif action == "delete":
                row = _json_row(m["before"])
                if row:
                    already = conn.execute(
                        select(transactions.c.id).where(
                            transactions.c.id == row["id"]
                        )
                    ).fetchone()
                    if not already:
                        conn.execute(insert(transactions).values(**row))
            elif action == "update":
                row = _json_row(m["before"])
                if row:
                    conn.execute(
                        update(transactions)
                        .where(transactions.c.id == m["txn_id"])
                        .values(category=row.get("category"),
                                person=row.get("person"))
                    )
            conn.execute(
                update(history).where(history.c.id == m["id"]).values(undone=1)
            )
            n += 1
    return n


# ----------------------------------------------------------------- statements
def statement_hash(pdf_bytes):
    """Stable fingerprint of a statement file's contents."""
    return hashlib.sha256(pdf_bytes).hexdigest()


def statement_seen(file_hash):
    with ENGINE.connect() as conn:
        return conn.execute(
            select(statements.c.file_hash).where(
                statements.c.file_hash == file_hash
            )
        ).fetchone() is not None


def record_statement(file_hash, filename, count):
    with ENGINE.begin() as conn:
        exists = conn.execute(
            select(statements.c.file_hash).where(
                statements.c.file_hash == file_hash
            )
        ).fetchone()
        vals = dict(filename=filename, txn_count=count,
                    imported_at=_dt.datetime.utcnow())
        if exists:
            conn.execute(
                update(statements)
                .where(statements.c.file_hash == file_hash)
                .values(**vals)
            )
        else:
            conn.execute(insert(statements).values(file_hash=file_hash, **vals))


# ----------------------------------------------------------------- de-duplication
def _dup_key(r):
    """Hard key for a charge: date, amount and card all come verbatim from the
    statement, so they must match exactly for two rows to be the same charge."""
    amount = round(float(r.get("amount") or 0), 2)
    return (str(r.get("date")), amount, (r.get("card") or "").lower())


def _norm_merchant(m):
    return re.sub(r"[^a-z0-9]+", " ", (m or "").lower()).strip()


def _similar_merchant(a, b, threshold=0.6):
    """Whether two merchant names refer to the same payee, allowing for parse
    wording differences ('carle consolidated pmt' vs 'carle consolidated
    payment'). Same first word, or a high overall similarity, counts as a match."""
    a, b = _norm_merchant(a), _norm_merchant(b)
    if not a or not b or a == b:
        return True
    if a.split(" ", 1)[0] == b.split(" ", 1)[0]:  # same leading word, e.g. 'carle'
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def find_duplicate_ids():
    """Ids of duplicate transactions. Rows are duplicates when date, amount and
    card match exactly AND the merchant names are similar (need not be identical).
    The earliest of each cluster is kept; the rest are returned as duplicates."""
    rows = fetch_transactions()
    groups = {}
    for r in rows:
        groups.setdefault(_dup_key(r), []).append(r)
    dup_ids = []
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda x: str(x.get("created_at") or ""))
        kept = []  # representative rows we keep
        for r in group:
            if any(_similar_merchant(r["merchant"], k["merchant"]) for k in kept):
                dup_ids.append(r["id"])
            else:
                kept.append(r)
    return dup_ids
