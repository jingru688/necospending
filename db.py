"""Storage for the spending tracker.

Uses SQLAlchemy so the same code runs on:
  - local SQLite  (default: a spending.db file)
  - cloud Postgres (set DATABASE_URL, e.g. a free Neon database) for an always-on
    deployed app where data must persist.
"""

import datetime as _dt
import hashlib
import os

from sqlalchemy import (
    Column, Float, MetaData, String, Table, DateTime,
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


def init_db():
    META.create_all(ENGINE)


def make_id(date, amount, merchant, card, occ=0):
    # `occ` distinguishes legitimately identical charges within one statement
    # (e.g. two $4 coffees, same day, same merchant) while keeping re-uploads of
    # the same statement idempotent (same charges -> same occ order -> same ids).
    raw = f"{date}|{amount}|{merchant}|{card}|{occ}".lower()
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def resolve_person(cardholder):
    if not cardholder:
        return "Unknown"
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
    """Re-derive every transaction's person from the current name map."""
    mapping = get_name_map()
    if not mapping:
        return 0
    updated = 0
    with ENGINE.begin() as conn:
        for cardholder, person in mapping.items():
            res = conn.execute(
                update(transactions)
                .where(transactions.c.cardholder.ilike(cardholder))
                .values(person=person)
            )
            updated += res.rowcount or 0
    return updated


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


def fetch_transactions():
    with ENGINE.connect() as conn:
        rows = conn.execute(
            select(transactions).order_by(
                transactions.c.date.desc(), transactions.c.created_at.desc()
            )
        ).fetchall()
    return [dict(r._mapping) for r in rows]
