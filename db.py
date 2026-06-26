"""Storage for the spending tracker.

Uses SQLAlchemy so the same code runs on:
  - local SQLite  (default: a spending.db file)
  - cloud Postgres (set DATABASE_URL, e.g. a free Neon database) for an always-on
    deployed app where data must persist.
"""

import datetime as _dt
import hashlib
import os
import re

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


def init_db():
    META.create_all(ENGINE)


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


def resolve_person(cardholder):
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
        # Retroactively apply to transactions already in the table so a new
        # mapping fixes old rows instead of creating a separate "person".
        conn.execute(
            update(transactions)
            .where(transactions.c.cardholder.ilike(cardholder))
            .values(person=person)
        )


def reapply_mappings():
    """Re-derive every transaction's person from the built-in last-name rules
    and any manual name_map overrides. Returns the number of rows changed."""
    rows = fetch_transactions()
    changes = [
        (r["id"], resolve_person(r["cardholder"]))
        for r in rows
        if resolve_person(r["cardholder"]) != r["person"]
    ]
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


def insert_transactions(txns, statement_file):
    """Insert transaction dicts. Returns (added, skipped_duplicates)."""
    added = skipped = 0
    occ_counts = {}
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
            conn.execute(
                insert(transactions).values(
                    id=tid,
                    date=t["date"],
                    merchant=t["merchant"],
                    amount=t["amount"],
                    category=t.get("category", "Uncategorized"),
                    cardholder=t.get("cardholder", ""),
                    person=t.get("person", resolve_person(t.get("cardholder", ""))),
                    card=t.get("card", ""),
                    statement_file=statement_file,
                    created_at=_dt.datetime.utcnow(),
                )
            )
            added += 1
    return added, skipped


def update_transaction(tid, category=None, person=None):
    vals = {}
    if category is not None:
        vals["category"] = category
    if person is not None:
        vals["person"] = person
    if not vals:
        return
    with ENGINE.begin() as conn:
        conn.execute(update(transactions).where(transactions.c.id == tid).values(**vals))


def delete_transaction(tid):
    with ENGINE.begin() as conn:
        conn.execute(delete(transactions).where(transactions.c.id == tid))


def delete_transactions(ids):
    """Delete many transactions by id. Returns the number removed."""
    ids = list(ids)
    if not ids:
        return 0
    with ENGINE.begin() as conn:
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
def _dup_sig(r):
    """Signature for spotting duplicate charges across re-uploads of one
    statement. Date, amount and card come verbatim from the statement, so they
    match exactly; merchant is reduced to letters only so parse wording
    differences ('STARBUCKS #123' vs 'Starbucks') still collapse together."""
    merchant = re.sub(r"[^a-z]", "", (r.get("merchant") or "").lower())
    amount = round(float(r.get("amount") or 0), 2)
    return (str(r.get("date")), amount, merchant, (r.get("card") or "").lower())


def find_duplicate_ids():
    """Ids of duplicate transactions, keeping the earliest of each matching set."""
    rows = fetch_transactions()
    groups = {}
    for r in rows:
        groups.setdefault(_dup_sig(r), []).append(r)
    dup_ids = []
    for rs in groups.values():
        if len(rs) > 1:
            rs.sort(key=lambda x: str(x.get("created_at") or ""))
            dup_ids.extend(r["id"] for r in rs[1:])
    return dup_ids
