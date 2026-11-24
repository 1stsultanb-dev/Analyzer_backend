"""Запись посещений: что прислал браузер + что знает сервер → три таблицы.

Браузер присылает то, что видно только ему: страницу, реферер, метки UTM,
экран, язык, часовой пояс, подсказки Client Hints. Сервер добавляет то, что
браузер не знает или может подделать: адрес и место (заголовки Cloudflare), а
также сотрудника по cookie «Финансов» (решается снаружи модуля, в маршруте).

Запись идёт мимо пользователя: что бы ни случилось здесь, страница не должна
узнать об этом — маршрут глотает исключения и отвечает 204.
"""
from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError

from app.visits.config import visits_settings
from app.visits.db import visits_session
from app.visits.describe import classify_source, parse_device
from app.visits.models import SitePageView, SiteVisit, SiteVisitor

_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_UTM_KEYS = ("source", "medium", "campaign", "term", "content")

#: Активное время одной страницы больше шести часов — это забытая вкладка, а
#: не чтение. Режем, чтобы одна такая вкладка не перекосила среднее.
MAX_ENGAGED_SECONDS = 6 * 3600
#: Сколько байт «прочего» от браузера сохраняем. Больше — подозрительно и не нужно.
MAX_EXTRA_BYTES = 4096


def _cut(value: object, size: int) -> str:
    return str(value or "").strip()[:size]


class SeenPayload(BaseModel):
    """Просмотр страницы. Ключи короткие: запрос уходит на каждой странице."""

    model_config = ConfigDict(extra="ignore")

    v: str  # посетитель
    s: str  # визит
    p: str  # просмотр
    host: str = Field(default="", max_length=255)
    path: str = Field(default="/", max_length=2048)
    title: str = Field(default="", max_length=1024)
    ref: str = Field(default="", max_length=4096)
    utm: dict[str, str] = Field(default_factory=dict)
    click: list[str] = Field(default_factory=list, max_length=10)
    screen: str = Field(default="", max_length=32)
    viewport: str = Field(default="", max_length=32)
    lang: str = Field(default="", max_length=64)
    langs: list[str] = Field(default_factory=list, max_length=20)
    tz: str = Field(default="", max_length=64)
    touch: int = Field(default=0, ge=0, le=64)
    conn: str = Field(default="", max_length=32)
    scheme: str = Field(default="", max_length=16)
    load_ms: int | None = Field(default=None, ge=0, le=600_000)
    hints: dict[str, object] = Field(default_factory=dict)
    extra: dict[str, object] = Field(default_factory=dict)

    @field_validator("v", "s", "p")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.match(value or ""):
            raise ValueError("неверный идентификатор")
        return value


class LeftPayload(BaseModel):
    """Уход со страницы: сколько её реально читали и докуда прокрутили."""

    model_config = ConfigDict(extra="ignore")

    v: str
    s: str
    p: str
    engaged: float = Field(default=0, ge=0)
    scroll: int = Field(default=0, ge=0, le=100)

    @field_validator("v", "s", "p")
    @classmethod
    def _id(cls, value: str) -> str:
        if not _ID.match(value or ""):
            raise ValueError("неверный идентификатор")
        return value


# ── Ограничитель ─────────────────────────────────────────────────────────────


class RateLimiter:
    """Не больше N запросов учёта с одного адреса в минуту.

    Маршрут открыт без входа и пишет в базу. Без потолка любой скрипт в цикле
    раздул бы таблицы — ровно то, от чего чистится анализатор.
    """

    def __init__(self) -> None:
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            if len(self._hits) > 10_000:
                self._hits = {k: v for k, v in self._hits.items() if now - v[0] < 60}
            started, count = self._hits.get(key, (now, 0))
            if now - started >= 60:
                started, count = now, 0
            if count >= limit:
                return False
            self._hits[key] = (started, count + 1)
            return True


limiter = RateLimiter()


# ── Заголовки ────────────────────────────────────────────────────────────────


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name) or ""
    # Starlette отдаёт заголовки как latin-1; Cloudflare пишет город в UTF-8.
    try:
        return value.encode("latin-1").decode("utf-8").strip()
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value.strip()


def client_ip(headers: Mapping[str, str], fallback: str = "") -> str:
    """Адрес человека: прокси Next кладёт его в `x-client-ip` (из `cf-connecting-ip`)."""
    for name in ("x-client-ip", "cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
        value = _header(headers, name).split(",")[0].strip()
        if value:
            return value[:64]
    return fallback[:64]


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def geo_from_headers(headers: Mapping[str, str]) -> dict[str, object]:
    """Место — из заголовков Cloudflare.

    Страна (`cf-ipcountry`) приходит всегда. Город, регион, индекс и координаты —
    только если в Cloudflare включено правило «Add visitor location headers»
    (Rules → Transform Rules → Managed Transforms). Без него эти поля пустые.
    """
    ray = _header(headers, "cf-ray")
    return {
        "country": _header(headers, "cf-ipcountry").upper()[:8],
        "region": _header(headers, "cf-region")[:128],
        "city": _header(headers, "cf-ipcity")[:128],
        "postal_code": _header(headers, "cf-postal-code")[:32],
        "latitude": _float(_header(headers, "cf-iplatitude")),
        "longitude": _float(_header(headers, "cf-iplongitude")),
        "edge": ray.rsplit("-", 1)[-1][:16] if "-" in ray else "",
    }


# ── Запись ───────────────────────────────────────────────────────────────────


def _extra(payload: SeenPayload) -> dict[str, object]:
    extra = dict(payload.extra)
    if payload.hints:
        extra["hints"] = payload.hints
    if len(json.dumps(extra, ensure_ascii=False, default=str)) > MAX_EXTRA_BYTES:
        return {"cut": True}
    return extra


def record_seen(
    payload: SeenPayload,
    headers: Mapping[str, str],
    *,
    fallback_ip: str = "",
    staff_name: str = "",
    own_hosts: set[str] | None = None,
    now: datetime | None = None,
) -> None:
    """Просмотр страницы. Новый визит и новый посетитель заводятся по ходу."""
    for attempt in range(2):
        try:
            _record_seen(payload, headers, fallback_ip, staff_name, own_hosts or set(), now)
            return
        except IntegrityError:
            # Две вкладки одного нового посетителя пришли одновременно и обе
            # завели его. Вторая попытка увидит строку первой.
            if attempt:
                raise


def _record_seen(
    payload: SeenPayload,
    headers: Mapping[str, str],
    fallback_ip: str,
    staff_name: str,
    own_hosts: set[str],
    now: datetime | None,
) -> None:
    now = now or datetime.now(UTC)
    path = _cut(payload.path, 1024) or "/"
    hosts = set(own_hosts)
    if payload.host:
        hosts.add(payload.host.lower().removeprefix("www."))

    with visits_session() as db:
        visitor = db.get(SiteVisitor, payload.v)
        new_visitor = visitor is None
        if visitor is None:
            visitor = SiteVisitor(id=payload.v, first_seen_at=now, last_seen_at=now)
            db.add(visitor)

        visit = db.get(SiteVisit, payload.s)
        if visit is None:
            user_agent = _header(headers, "user-agent")
            hints = dict(payload.hints)
            hints["webdriver"] = bool(payload.extra.get("webdriver"))
            device = parse_device(user_agent, hints, payload.touch)
            utm = {k: _cut(payload.utm.get(k), 255) for k in _UTM_KEYS if payload.utm.get(k)}
            source = classify_source(payload.ref, utm, payload.click, hosts, device.browser)
            geo = geo_from_headers(headers)
            ip = client_ip(headers, fallback_ip)
            visit = SiteVisit(
                id=payload.s,
                visitor_id=payload.v,
                started_at=now,
                last_activity_at=now,
                is_new_visitor=new_visitor,
                landing_path=path,
                exit_path=path,
                channel=source.channel,
                source=_cut(source.source, 255),
                medium=source.medium,
                campaign=source.campaign,
                term=source.term,
                content=source.content,
                click_id=source.click_id,
                referrer=_cut(payload.ref if source.referrer_host else "", 1024),
                referrer_host=_cut(source.referrer_host, 255),
                ip=ip,
                user_agent=user_agent[:512],
                device_type=device.device_type,
                device_vendor=device.vendor,
                device_model=device.model,
                os=device.os,
                os_version=device.os_version,
                browser=device.browser,
                browser_version=device.browser_version,
                screen=payload.screen,
                viewport=payload.viewport,
                language=payload.lang,
                languages=_cut(", ".join(payload.langs), 255),
                timezone=payload.tz,
                connection=payload.conn,
                color_scheme=payload.scheme,
                load_ms=payload.load_ms,
                is_bot=device.is_bot,
                extra=_extra(payload),
                **geo,
            )
            db.add(visit)
            visitor.visits = (visitor.visits or 0) + 1
            if new_visitor:
                visitor.first_channel = source.channel
                visitor.first_source = visit.source
                visitor.first_medium = source.medium
                visitor.first_campaign = source.campaign
                visitor.first_referrer = visit.referrer
                visitor.landing_path = path
            visitor.last_ip = ip
            visitor.country = str(geo["country"])
            visitor.region = str(geo["region"])
            visitor.city = str(geo["city"])
            visitor.device_type = device.device_type
            visitor.os = device.os
            visitor.browser = device.browser
            visitor.language = payload.lang
            visitor.timezone = payload.tz
            visitor.is_bot = bool(visitor.is_bot or device.is_bot)

        if db.get(SitePageView, payload.p) is None:
            db.add(
                SitePageView(
                    id=payload.p,
                    visit_id=payload.s,
                    visitor_id=payload.v,
                    at=now,
                    path=path,
                    title=_cut(payload.title, 512),
                )
            )
            visit.pageviews = (visit.pageviews or 0) + 1
            visitor.pageviews = (visitor.pageviews or 0) + 1
            visit.exit_path = path

        visit.last_activity_at = now
        visitor.last_seen_at = now
        if staff_name:
            visit.staff_name = staff_name[:255]
            visitor.staff_name = staff_name[:255]


def record_left(payload: LeftPayload, *, now: datetime | None = None) -> None:
    """Время на странице присылается нарастающим итогом — повтор ничего не удвоит."""
    now = now or datetime.now(UTC)
    engaged = int(min(payload.engaged, MAX_ENGAGED_SECONDS))
    with visits_session() as db:
        page = db.get(SitePageView, payload.p)
        if page is None or page.visit_id != payload.s or page.visitor_id != payload.v:
            return
        delta = max(0, engaged - (page.engaged_seconds or 0))
        page.engaged_seconds = max(page.engaged_seconds or 0, engaged)
        page.scroll_depth = max(page.scroll_depth or 0, payload.scroll)
        visit = db.get(SiteVisit, payload.s)
        if visit is not None:
            visit.engaged_seconds = (visit.engaged_seconds or 0) + delta
            visit.last_activity_at = max(_aware(visit.last_activity_at), now)
        visitor = db.get(SiteVisitor, payload.v)
        if visitor is not None:
            visitor.engaged_seconds = (visitor.engaged_seconds or 0) + delta
            visitor.last_seen_at = max(_aware(visitor.last_seen_at), now)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def purge_old(now: datetime | None = None) -> int:
    """Сырые визиты и просмотры старше `VISITS_RETENTION_DAYS`, посетители — старше года.

    Отчёт к этому времени давно в таблице; в базе нужен только хвост, по
    которому считаются следующие периоды и «новый ли посетитель».
    """
    now = now or datetime.now(UTC)
    raw_cutoff = now - timedelta(days=visits_settings.retention_days)
    visitor_cutoff = now - timedelta(days=max(365, visits_settings.retention_days))
    with visits_session() as db:
        removed = db.execute(delete(SitePageView).where(SitePageView.at < raw_cutoff)).rowcount or 0
        removed += db.execute(delete(SiteVisit).where(SiteVisit.started_at < raw_cutoff)).rowcount or 0
        removed += db.execute(
            delete(SiteVisitor).where(SiteVisitor.last_seen_at < visitor_cutoff)
        ).rowcount or 0
    return removed


__all__ = [
    "LeftPayload",
    "SeenPayload",
    "client_ip",
    "geo_from_headers",
    "limiter",
    "purge_old",
    "record_left",
    "record_seen",
]
