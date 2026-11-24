"""Таблицы учёта посещений: посетитель → визиты → просмотры страниц, и журнал отчётов.

Посетитель — браузер (id из localStorage/cookie). Визит — непрерывная работа
на сайте: новый начинается после 30 минут тишины или с новой рекламной меткой.
Просмотр — одна открытая страница.

Устройство, место и источник записываются в визит, а не в просмотр: за визит
они не меняются, а просмотров в разы больше. В посетителе — первое касание
(откуда пришёл впервые) и последнее известное место и устройство.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.visits.db import VisitsBase


def _now() -> datetime:
    return datetime.now(UTC)


class SiteVisitor(VisitsBase):
    __tablename__ = "site_visitors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    visits: Mapped[int] = mapped_column(Integer, default=0)
    pageviews: Mapped[int] = mapped_column(Integer, default=0)
    engaged_seconds: Mapped[int] = mapped_column(Integer, default=0)

    # Первое касание — откуда человек пришёл впервые.
    first_channel: Mapped[str] = mapped_column(String(32), default="")
    first_source: Mapped[str] = mapped_column(String(255), default="")
    first_medium: Mapped[str] = mapped_column(String(255), default="")
    first_campaign: Mapped[str] = mapped_column(String(255), default="")
    first_referrer: Mapped[str] = mapped_column(String(1024), default="")
    landing_path: Mapped[str] = mapped_column(String(1024), default="")

    # Последнее известное.
    last_ip: Mapped[str] = mapped_column(String(64), default="")
    country: Mapped[str] = mapped_column(String(8), default="")
    region: Mapped[str] = mapped_column(String(128), default="")
    city: Mapped[str] = mapped_column(String(128), default="")
    device_type: Mapped[str] = mapped_column(String(16), default="")
    os: Mapped[str] = mapped_column(String(64), default="")
    browser: Mapped[str] = mapped_column(String(64), default="")
    language: Mapped[str] = mapped_column(String(64), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="")

    #: Имя вошедшего в «Финансы», если этот браузер хоть раз был под учёткой.
    staff_name: Mapped[str] = mapped_column(String(255), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)


class SiteVisit(VisitsBase):
    __tablename__ = "site_visits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    visitor_id: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    is_new_visitor: Mapped[bool] = mapped_column(Boolean, default=False)
    pageviews: Mapped[int] = mapped_column(Integer, default=0)
    engaged_seconds: Mapped[int] = mapped_column(Integer, default=0)
    landing_path: Mapped[str] = mapped_column(String(1024), default="")
    exit_path: Mapped[str] = mapped_column(String(1024), default="")

    # Источник.
    channel: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(String(255), default="")
    medium: Mapped[str] = mapped_column(String(255), default="")
    campaign: Mapped[str] = mapped_column(String(255), default="")
    term: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(String(255), default="")
    click_id: Mapped[str] = mapped_column(String(32), default="")
    referrer: Mapped[str] = mapped_column(String(1024), default="")
    referrer_host: Mapped[str] = mapped_column(String(255), default="")

    # Место.
    ip: Mapped[str] = mapped_column(String(64), default="")
    country: Mapped[str] = mapped_column(String(8), default="")
    region: Mapped[str] = mapped_column(String(128), default="")
    city: Mapped[str] = mapped_column(String(128), default="")
    postal_code: Mapped[str] = mapped_column(String(32), default="")
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge: Mapped[str] = mapped_column(String(16), default="")

    # Устройство.
    user_agent: Mapped[str] = mapped_column(String(512), default="")
    device_type: Mapped[str] = mapped_column(String(16), default="")
    device_vendor: Mapped[str] = mapped_column(String(64), default="")
    device_model: Mapped[str] = mapped_column(String(64), default="")
    os: Mapped[str] = mapped_column(String(64), default="")
    os_version: Mapped[str] = mapped_column(String(32), default="")
    browser: Mapped[str] = mapped_column(String(64), default="")
    browser_version: Mapped[str] = mapped_column(String(32), default="")
    screen: Mapped[str] = mapped_column(String(32), default="")
    viewport: Mapped[str] = mapped_column(String(32), default="")
    language: Mapped[str] = mapped_column(String(64), default="")
    languages: Mapped[str] = mapped_column(String(255), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="")
    connection: Mapped[str] = mapped_column(String(32), default="")
    color_scheme: Mapped[str] = mapped_column(String(16), default="")
    load_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    staff_name: Mapped[str] = mapped_column(String(255), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Остальное, что прислал браузер: память, ядра, касания, DNT, тайминги.
    extra: Mapped[dict] = mapped_column(JSON, default=dict)


class SitePageView(VisitsBase):
    __tablename__ = "site_pageviews"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    visit_id: Mapped[str] = mapped_column(String(64), index=True)
    visitor_id: Mapped[str] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    path: Mapped[str] = mapped_column(String(1024), default="")
    title: Mapped[str] = mapped_column(String(512), default="")
    engaged_seconds: Mapped[int] = mapped_column(Integer, default=0)
    scroll_depth: Mapped[int] = mapped_column(Integer, default=0)


class SiteReport(VisitsBase):
    """Журнал отчётов: какой период ушёл в таблицу. По нему считается следующий."""

    __tablename__ = "site_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    status: Mapped[str] = mapped_column(String(16), default="ok")
    detail: Mapped[str] = mapped_column(Text, default="")


__all__ = ["SitePageView", "SiteReport", "SiteVisit", "SiteVisitor"]
