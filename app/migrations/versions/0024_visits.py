"""visits: учёт посещений сайта и журнал отчётов для маркетинга

Схема `visits` — удаляемый модуль: `DROP SCHEMA visits CASCADE` снимает его,
не задев ни одной чужой таблицы. Четыре таблицы:

* `site_visitors` — посетитель (браузер): первое касание, последнее место и устройство;
* `site_visits` — визит: источник, место, устройство, длительность;
* `site_pageviews` — просмотр страницы: путь, активное время, прокрутка;
* `site_reports` — какой период отчёта ушёл в Google-таблицу.

Ревизия идемпотентна, как 0008: схема и таблицы создаются, только если их нет.
`downgrade` снимает схему целиком — в ней только учёт посещений, а отчёты уже
лежат в Google-таблице.

Revision ID: 0024
Revises: 0023
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "visits"


def _text(name: str, size: int) -> sa.Column:
    return sa.Column(name, sa.String(length=size), nullable=False)


def _when(name: str) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False)


def _int(name: str) -> sa.Column:
    return sa.Column(name, sa.Integer(), nullable=False)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        # На SQLite схему заводит `create_all` (app/visits/db.py).
        return

    op.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
    existing = set(sa.inspect(bind).get_table_names(schema=SCHEMA))

    if "site_visitors" not in existing:
        op.create_table(
            "site_visitors",
            _text("id", 64),
            _when("first_seen_at"),
            _when("last_seen_at"),
            _int("visits"),
            _int("pageviews"),
            _int("engaged_seconds"),
            _text("first_channel", 32),
            _text("first_source", 255),
            _text("first_medium", 255),
            _text("first_campaign", 255),
            _text("first_referrer", 1024),
            _text("landing_path", 1024),
            _text("last_ip", 64),
            _text("country", 8),
            _text("region", 128),
            _text("city", 128),
            _text("device_type", 16),
            _text("os", 64),
            _text("browser", 64),
            _text("language", 64),
            _text("timezone", 64),
            _text("staff_name", 255),
            sa.Column("is_bot", sa.Boolean(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            schema=SCHEMA,
        )
        op.create_index("ix_visits_site_visitors_first_seen_at", "site_visitors", ["first_seen_at"], schema=SCHEMA)
        op.create_index("ix_visits_site_visitors_last_seen_at", "site_visitors", ["last_seen_at"], schema=SCHEMA)

    if "site_visits" not in existing:
        op.create_table(
            "site_visits",
            _text("id", 64),
            _text("visitor_id", 64),
            _when("started_at"),
            _when("last_activity_at"),
            sa.Column("is_new_visitor", sa.Boolean(), nullable=False),
            _int("pageviews"),
            _int("engaged_seconds"),
            _text("landing_path", 1024),
            _text("exit_path", 1024),
            _text("channel", 32),
            _text("source", 255),
            _text("medium", 255),
            _text("campaign", 255),
            _text("term", 255),
            _text("content", 255),
            _text("click_id", 32),
            _text("referrer", 1024),
            _text("referrer_host", 255),
            _text("ip", 64),
            _text("country", 8),
            _text("region", 128),
            _text("city", 128),
            _text("postal_code", 32),
            sa.Column("latitude", sa.Float(), nullable=True),
            sa.Column("longitude", sa.Float(), nullable=True),
            _text("edge", 16),
            _text("user_agent", 512),
            _text("device_type", 16),
            _text("device_vendor", 64),
            _text("device_model", 64),
            _text("os", 64),
            _text("os_version", 32),
            _text("browser", 64),
            _text("browser_version", 32),
            _text("screen", 32),
            _text("viewport", 32),
            _text("language", 64),
            _text("languages", 255),
            _text("timezone", 64),
            _text("connection", 32),
            _text("color_scheme", 16),
            sa.Column("load_ms", sa.Integer(), nullable=True),
            _text("staff_name", 255),
            sa.Column("is_bot", sa.Boolean(), nullable=False),
            sa.Column("extra", sa.JSON(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            schema=SCHEMA,
        )
        op.create_index("ix_visits_site_visits_visitor_id", "site_visits", ["visitor_id"], schema=SCHEMA)
        op.create_index("ix_visits_site_visits_started_at", "site_visits", ["started_at"], schema=SCHEMA)

    if "site_pageviews" not in existing:
        op.create_table(
            "site_pageviews",
            _text("id", 64),
            _text("visit_id", 64),
            _text("visitor_id", 64),
            _when("at"),
            _text("path", 1024),
            _text("title", 512),
            _int("engaged_seconds"),
            _int("scroll_depth"),
            sa.PrimaryKeyConstraint("id"),
            schema=SCHEMA,
        )
        op.create_index("ix_visits_site_pageviews_visit_id", "site_pageviews", ["visit_id"], schema=SCHEMA)
        op.create_index("ix_visits_site_pageviews_at", "site_pageviews", ["at"], schema=SCHEMA)

    if "site_reports" not in existing:
        op.create_table(
            "site_reports",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            _when("period_start"),
            _when("period_end"),
            _when("sent_at"),
            _text("status", 16),
            sa.Column("detail", sa.Text(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            schema=SCHEMA,
        )
        op.create_index("ix_visits_site_reports_period_end", "site_reports", ["period_end"], schema=SCHEMA)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    op.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
