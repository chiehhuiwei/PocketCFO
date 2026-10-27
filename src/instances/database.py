"""
src/instances/database.py
SQLAlchemy async engine & session factory singleton.
"""
from __future__ import annotations

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from src.instances.config import get_settings

settings = get_settings()

connect_args = {}
if "postgresql" in settings.database_url:
    # Enforce SSL and set search_path to public schema for PostgreSQL
    if settings.database_ssl:
        connect_args["ssl"] = "require"
    connect_args["server_settings"] = {"search_path": "public"}

engine_args = {
    "echo": settings.app_debug,
    "connect_args": connect_args,
}
if "postgresql" in settings.database_url:
    engine_args["pool_size"] = 2
    engine_args["max_overflow"] = 0
    engine_args["pool_recycle"] = 300
    engine_args["pool_pre_ping"] = True
    engine_args["pool_timeout"] = 15


engine = create_async_engine(
    settings.database_url,
    **engine_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models."""
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yield a database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def run_migrations() -> None:
    """
    Apply incremental schema changes to existing tables.
    Uses ADD COLUMN IF NOT EXISTS so it is safe to run on every startup.
    """
    import logging
    log = logging.getLogger(__name__)

    migrations: list[str] = [
        # 2026-06-26: extend TransactionCategory enum with expense sub-categories
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'food'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'transport'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'medical'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'entertainment'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'FOOD'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'TRANSPORT'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'MEDICAL'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'ENTERTAINMENT'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'insurance'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'exercise'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'INSURANCE'",
        "ALTER TYPE transactioncategory ADD VALUE IF NOT EXISTS 'EXERCISE'",
        # 2026-06-26: drop orphaned expense_category column (superseded by category enum)
        "ALTER TABLE transactions DROP COLUMN IF EXISTS expense_category",
        # 2026-06-26: per-user keyword → category override rules
        """
        CREATE TABLE IF NOT EXISTS category_rules (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            keyword VARCHAR(128) NOT NULL,
            category VARCHAR(32) NOT NULL,
            created_at TIMESTAMP DEFAULT now(),
            UNIQUE (user_id, keyword)
        )
        """,
        # 2026-07-07: migrate old prefix-style account codes (bank_inst_num / broker_inst_num)
        # to digits-only codes. Uses a DO block so it is idempotent.
        """
        DO $$
        DECLARE
            r RECORD;
            new_code TEXT;
        BEGIN
            FOR r IN SELECT id, code FROM accounts
                WHERE code LIKE 'bank\\_%' OR code LIKE 'broker\\_%' OR code LIKE 'cc\\_%'
            LOOP
                -- Extract digits from the trailing segment after the last underscore
                new_code := regexp_replace(split_part(r.code, '_', array_length(string_to_array(r.code, '_'), 1)), '[^0-9]', '', 'g');
                IF new_code = '' THEN
                    CONTINUE;
                END IF;
                -- Only update if the digits-only code is not already taken
                IF NOT EXISTS (SELECT 1 FROM accounts WHERE code = new_code AND id <> r.id) THEN
                    UPDATE accounts SET code = new_code WHERE id = r.id;
                END IF;
            END LOOP;
        END $$
        """,
        # 2026-07-07: purge all soft-deleted accounts (is_active=false) and their data
        # Wrapped in a DO block so it is a no-op if the column was already dropped.
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'accounts' AND column_name = 'is_active'
            ) THEN
                -- Cascade delete related rows for inactive accounts
                DELETE FROM securities
                    WHERE account_id IN (SELECT id FROM accounts WHERE is_active = false);
                DELETE FROM account_snapshots
                    WHERE account_id IN (SELECT id FROM accounts WHERE is_active = false);
                DELETE FROM transactions
                    WHERE account_id IN (SELECT id FROM accounts WHERE is_active = false);
                -- Delete the inactive accounts themselves
                DELETE FROM accounts WHERE is_active = false;
            END IF;
        END $$
        """,
        # 2026-07-07: drop is_active from accounts (replaced by hard cascade delete)
        "ALTER TABLE accounts DROP COLUMN IF EXISTS is_active",
        # 2026-07-31: price_alerts — support notify-only alerts (target price / MA20) and broker selection
        """
        DO $$
        BEGIN
            -- Labels are the Python enum member NAMES: that is what SQLAlchemy's
            -- Enum(PyEnum) persists, matching the pre-existing pricealertside/status types.
            CREATE TYPE pricealerttype AS ENUM ('AUTO_TRADE', 'NOTIFY_PRICE', 'NOTIFY_MA20');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$
        """,
        """
        DO $$
        BEGIN
            CREATE TYPE pricealertdirection AS ENUM ('ABOVE', 'BELOW');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$
        """,
        """
        DO $$
        BEGIN
            CREATE TYPE pricealertbroker AS ENUM ('ESUN', 'TAISHIN', 'SINOPAC');
        EXCEPTION
            WHEN duplicate_object THEN NULL;
        END $$
        """,
        "ALTER TABLE price_alerts ADD COLUMN IF NOT EXISTS alert_type pricealerttype NOT NULL DEFAULT 'AUTO_TRADE'",
        "ALTER TABLE price_alerts ADD COLUMN IF NOT EXISTS direction pricealertdirection",
        "ALTER TABLE price_alerts ADD COLUMN IF NOT EXISTS broker pricealertbroker DEFAULT 'ESUN'",
        "UPDATE price_alerts SET broker = 'ESUN' WHERE broker IS NULL",
        "ALTER TABLE price_alerts ALTER COLUMN side DROP NOT NULL",
        "ALTER TABLE price_alerts ALTER COLUMN quantity DROP NOT NULL",
        # 2026-08-05: rebalance_strategies — replace directly-editable trigger thresholds
        # with a single assumed stock price rise % that both thresholds are derived from.
        # (stock_min_threshold uses the round-trip fall back to the pre-rise price:
        #  fall_frac = rise_frac / (1 + rise_frac).)
        "ALTER TABLE rebalance_strategies ADD COLUMN IF NOT EXISTS assumed_rise_pct FLOAT NOT NULL DEFAULT 50.0",
        "ALTER TABLE rebalance_strategies DROP COLUMN IF EXISTS assumed_fall_pct",
        """
        UPDATE rebalance_strategies SET
            stock_trigger_threshold = round(((
                (target_stock_pct / 100.0) * (1.0 + assumed_rise_pct / 100.0)
                / (1.0 + (assumed_rise_pct / 100.0) * (target_stock_pct / 100.0))
            ) * 100.0)::numeric, 2),
            stock_min_threshold = round(((
                (target_stock_pct / 100.0) * (1.0 - (assumed_rise_pct / 100.0) / (1.0 + assumed_rise_pct / 100.0))
                / (1.0 - ((assumed_rise_pct / 100.0) / (1.0 + assumed_rise_pct / 100.0)) * (target_stock_pct / 100.0))
            ) * 100.0)::numeric, 2)
        """,
        # 2026-08-27: accounts — real installment schedule (起始日/期數/還款日) instead of
        # the flat is_installment + installment_amount straight-line estimate.
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS installment_start_date DATE",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS installment_count INTEGER",
        "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS installment_payment_day INTEGER",
        # 2026-08-27: price_alerts — notify-only alerts can target foreign-currency tickers
        "ALTER TABLE price_alerts ADD COLUMN IF NOT EXISTS currency VARCHAR(8) NOT NULL DEFAULT 'TWD'",
        # 2026-08-31: account_snapshots — manual override for brokerage un-invested cash
        # (e.g. Firstrade idle cash not yet reflected by parsed holdings), native currency.
        "ALTER TABLE account_snapshots ADD COLUMN IF NOT EXISTS manual_cash_override FLOAT",
    ]

    # Each statement gets its own transaction: on Postgres a single failed statement
    # aborts the whole transaction, which would silently skip every later migration.
    for stmt in migrations:
        try:
            async with engine.begin() as conn:
                await conn.execute(__import__("sqlalchemy").text(stmt))
            log.info(f"migration.ok: {stmt}")
        except Exception as e:
            log.warning(f"migration.skip: {stmt} — {e}")


async def create_all_tables() -> None:
    """Create all tables on startup, then apply incremental column migrations."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_migrations()

