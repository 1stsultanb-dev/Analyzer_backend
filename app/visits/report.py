"""Отчёт о посещениях за период → строки вкладок Google-таблицы.

Периоды идут встык по `VISITS_REPORT_EVERY_DAYS` (3) суток от полуночи по
Казахстану (UTC+5) — день начала учёта. Какой период ушёл последним, записано
в `site_reports`: после простоя сервера отправятся все пропущенные по очереди,
а сбой Google не сдвигает период — следующая попытка через час.

Каждая вкладка — длинная таблица с колонкой «Период» первой: новые отчёты
дописываются снизу, а фильтр по периоду или сводная таблица строятся сами.

В маркетинговые цифры не попадают боты и сотрудники, вошедшие в «Финансы»:
это не аудитория сайта. Их число — отдельными колонками сводки.
"""
from __future__ import annotations

import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import func, select

from app.visits.config import visits_settings
from app.visits.db import visits_session
from app.visits.describe import country_name, page_key
from app.visits.models import SitePageView, SiteReport, SiteVisit, SiteVisitor

log = logging.getLogger(__name__)

#: Казахстан с 1 марта 2024 года живёт в одном поясе, UTC+5.
LOCAL_TZ = timezone(timedelta(hours=5))
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

#: Отказ — визит в одну страницу короче десяти секунд активного времени.
BOUNCE_SECONDS = 10

# ── Вкладки ──────────────────────────────────────────────────────────────────

TAB_SUMMARY = "Сводка"
TAB_DAYS = "По дням"
TAB_SOURCES = "Источники"
TAB_GEO = "География"
TAB_DEVICES = "Устройства"
TAB_PAGES = "Страницы"
TAB_HOURS = "Часы"
TAB_VISITORS = "Посетители"
TAB_VISITS = "Визиты"

TABS: dict[str, list[str]] = {
    TAB_SUMMARY: [
        "Период", "С", "По", "Уникальные посетители", "Новые", "Вернувшиеся", "Визиты",
        "Просмотры страниц", "Просмотров на визит", "Средняя длительность визита, мин",
        "Активное время на визит, мин", "Отказы, %", "Выписок отправлено на анализ",
        "Конверсия посетителя в анализ, %", "Главный канал", "Главный источник",
        "Главная страна", "Доля телефонов, %", "Сотрудники на сайте (не в цифрах)",
        "Визиты ботов (не в цифрах)", "Отчёт сформирован",
    ],
    TAB_DAYS: [
        "Период", "Дата", "День недели", "Уникальные посетители", "Новые", "Визиты",
        "Просмотры", "Средняя длительность, мин", "Отказы, %",
    ],
    TAB_SOURCES: [
        "Период", "Канал", "Источник", "Кампания (utm_campaign)", "Тип трафика (utm_medium)",
        "Визиты", "Уникальные посетители", "Новые", "Просмотры", "Средняя длительность, мин",
        "Отказы, %", "Доля визитов, %",
    ],
    TAB_GEO: [
        "Период", "Страна", "Регион", "Город", "Визиты", "Уникальные посетители", "Новые",
        "Доля визитов, %",
    ],
    TAB_DEVICES: [
        "Период", "Тип устройства", "ОС", "Браузер", "Визиты", "Уникальные посетители",
        "Средняя длительность, мин", "Отказы, %", "Доля визитов, %",
    ],
    TAB_PAGES: [
        "Период", "Страница", "Заголовок", "Просмотры", "Уникальные посетители", "Входы",
        "Выходы", "Среднее время на странице, с", "Средняя прокрутка, %",
    ],
    TAB_HOURS: ["Период", "Час (UTC+5)", "Визиты", "Уникальные посетители", "Просмотры"],
    TAB_VISITORS: [
        "Период", "Посетитель", "Новый / вернувшийся", "Впервые на сайте", "Последняя активность",
        "Визитов за период", "Просмотров за период", "Активное время, мин", "Страна", "Регион",
        "Город", "IP", "Устройство", "Производитель", "Модель", "ОС", "Браузер", "Экран",
        "Язык", "Часовой пояс", "Канал первого визита", "Источник первого визита",
        "Кампания первого визита", "Первая страница на сайте", "Последняя страница",
    ],
    TAB_VISITS: [
        "Период", "Начало", "Посетитель", "Новый / вернувшийся", "Длительность, мин",
        "Активное время, мин", "Просмотров", "Путь по сайту", "Страница входа", "Страница выхода",
        "Канал", "Источник", "Кампания", "Тип трафика", "Ключевое слово (utm_term)",
        "Объявление (utm_content)", "Метка клика", "Реферер", "Страна", "Регион", "Город",
        "Индекс", "IP", "Узел Cloudflare", "Устройство", "Производитель", "Модель", "ОС",
        "Версия ОС", "Браузер", "Версия браузера", "Экран", "Окно браузера", "Язык",
        "Языки браузера", "Часовой пояс", "Сеть", "Тема", "Загрузка страницы, мс",
        "Строка браузера",
    ],
}


def local(value: datetime) -> datetime:
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TZ)


def stamp(value: datetime) -> str:
    return local(value).strftime("%d.%m.%Y %H:%M")


def period_label(start: datetime, end: datetime) -> str:
    last_day = local(end) - timedelta(seconds=1)
    return f"{local(start):%d.%m.%Y} – {last_day:%d.%m.%Y}"


def local_midnight(value: datetime) -> datetime:
    day = local(value).replace(hour=0, minute=0, second=0, microsecond=0)
    return day.astimezone(UTC)


def _minutes(seconds: float) -> float:
    return round(seconds / 60, 1)


def _pct(part: float, whole: float) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _duration(visit: SiteVisit) -> float:
    start = visit.started_at if visit.started_at.tzinfo else visit.started_at.replace(tzinfo=UTC)
    last = visit.last_activity_at
    last = last if last.tzinfo else last.replace(tzinfo=UTC)
    return max(0.0, (last - start).total_seconds())


def _bounced(visit: SiteVisit) -> bool:
    return (visit.pageviews or 0) <= 1 and (visit.engaged_seconds or 0) < BOUNCE_SECONDS


# ── Сборка ───────────────────────────────────────────────────────────────────


@dataclass
class Report:
    start: datetime
    end: datetime
    rows: dict[str, list[list[object]]] = field(default_factory=dict)
    visits: int = 0


def _group_stats(visits: list[SiteVisit], new_ids: set[str]) -> dict[str, float]:
    count = len(visits)
    uniq = {v.visitor_id for v in visits}
    return {
        "visits": count,
        "unique": len(uniq),
        "new": len(uniq & new_ids),
        "pageviews": sum(v.pageviews or 0 for v in visits),
        "duration": _minutes(sum(_duration(v) for v in visits) / count) if count else 0.0,
        "bounce": _pct(sum(1 for v in visits if _bounced(v)), count),
    }


def build_report(
    start: datetime,
    end: datetime,
    *,
    analyses: int | None = None,
    now: datetime | None = None,
) -> Report:
    """Собрать все вкладки за [start, end). `analyses` — сколько выписок прислали на анализ."""
    now = now or datetime.now(UTC)
    label = period_label(start, end)
    report = Report(start=start, end=end)

    with visits_session() as db:
        all_visits = list(
            db.scalars(
                select(SiteVisit)
                .where(SiteVisit.started_at >= start, SiteVisit.started_at < end)
                .order_by(SiteVisit.started_at)
            )
        )
        visitor_ids = {v.visitor_id for v in all_visits}
        visitors = {
            row.id: row
            for row in db.scalars(select(SiteVisitor).where(SiteVisitor.id.in_(visitor_ids)))
        } if visitor_ids else {}
        staff_ids = {
            vid for vid, row in visitors.items() if row.staff_name
        } | {v.visitor_id for v in all_visits if v.staff_name}
        bot_visits = [v for v in all_visits if v.is_bot or (visitors.get(v.visitor_id) and visitors[v.visitor_id].is_bot)]
        bot_ids = {v.visitor_id for v in bot_visits}
        visits = [v for v in all_visits if v.visitor_id not in staff_ids and v.visitor_id not in bot_ids]
        visit_ids = {v.id for v in visits}
        pages = [
            p
            for p in db.scalars(
                select(SitePageView)
                .where(SitePageView.visit_id.in_(visit_ids))
                .order_by(SitePageView.at)
            )
        ] if visit_ids else []
        # Сессии вне базы не живут: всё, что нужно, вынимаем до закрытия.
        db.expunge_all()

    report.visits = len(visits)
    new_ids = {v.visitor_id for v in visits if v.is_new_visitor}
    total = _group_stats(visits, new_ids)
    count = total["visits"]

    def top(values: list[str]) -> str:
        values = [v for v in values if v]
        return Counter(values).most_common(1)[0][0] if values else "—"

    phones = sum(1 for v in visits if v.device_type == "Телефон")
    report.rows[TAB_SUMMARY] = [[
        label, stamp(start), stamp(end), total["unique"], total["new"], total["unique"] - total["new"],
        count, total["pageviews"], round(total["pageviews"] / count, 2) if count else 0,
        total["duration"], _minutes(sum(v.engaged_seconds or 0 for v in visits) / count) if count else 0,
        total["bounce"], analyses if analyses is not None else "—",
        _pct(analyses, total["unique"]) if analyses is not None else "—",
        top([v.channel for v in visits]), top([v.source for v in visits if v.source != "—"]),
        top([country_name(v.country) for v in visits]), _pct(phones, count),
        len(staff_ids), len(bot_visits), stamp(now),
    ]]

    # По дням.
    by_day: dict[str, list[SiteVisit]] = defaultdict(list)
    for v in visits:
        by_day[local(v.started_at).strftime("%d.%m.%Y")].append(v)
    report.rows[TAB_DAYS] = []
    day = local(start)
    while day < local(end):
        key = day.strftime("%d.%m.%Y")
        stats = _group_stats(by_day.get(key, []), new_ids)
        report.rows[TAB_DAYS].append([
            label, key, WEEKDAYS[day.weekday()], stats["unique"], stats["new"], stats["visits"],
            stats["pageviews"], stats["duration"], stats["bounce"],
        ])
        day += timedelta(days=1)

    # Источники.
    by_source: dict[tuple[str, str, str, str], list[SiteVisit]] = defaultdict(list)
    for v in visits:
        by_source[(v.channel or "—", v.source or "—", v.campaign or "", v.medium or "")].append(v)
    report.rows[TAB_SOURCES] = [
        [label, *key, s["visits"], s["unique"], s["new"], s["pageviews"], s["duration"], s["bounce"],
         _pct(s["visits"], count)]
        for key, group in sorted(by_source.items(), key=lambda kv: -len(kv[1]))
        for s in [_group_stats(group, new_ids)]
    ]

    # География.
    by_geo: dict[tuple[str, str, str], list[SiteVisit]] = defaultdict(list)
    for v in visits:
        by_geo[(country_name(v.country), v.region or "—", v.city or "—")].append(v)
    report.rows[TAB_GEO] = [
        [label, *key, s["visits"], s["unique"], s["new"], _pct(s["visits"], count)]
        for key, group in sorted(by_geo.items(), key=lambda kv: -len(kv[1]))
        for s in [_group_stats(group, new_ids)]
    ]

    # Устройства.
    by_device: dict[tuple[str, str, str], list[SiteVisit]] = defaultdict(list)
    for v in visits:
        os_label = " ".join(x for x in (v.os, v.os_version) if x) or "—"
        by_device[(v.device_type or "—", os_label, v.browser or "—")].append(v)
    report.rows[TAB_DEVICES] = [
        [label, *key, s["visits"], s["unique"], s["duration"], s["bounce"], _pct(s["visits"], count)]
        for key, group in sorted(by_device.items(), key=lambda kv: -len(kv[1]))
        for s in [_group_stats(group, new_ids)]
    ]

    # Страницы.
    page_rows: dict[str, dict[str, object]] = {}
    for p in pages:
        key = page_key(p.path)
        row = page_rows.setdefault(key, {"views": 0, "visitors": set(), "engaged": 0, "scroll": 0, "title": ""})
        row["views"] += 1
        row["visitors"].add(p.visitor_id)
        row["engaged"] += p.engaged_seconds or 0
        row["scroll"] += p.scroll_depth or 0
        row["title"] = row["title"] or p.title
    entries = Counter(page_key(v.landing_path) for v in visits)
    exits = Counter(page_key(v.exit_path) for v in visits)
    report.rows[TAB_PAGES] = [
        [label, key, row["title"], row["views"], len(row["visitors"]), entries.get(key, 0), exits.get(key, 0),
         round(row["engaged"] / row["views"], 1), round(row["scroll"] / row["views"], 1)]
        for key, row in sorted(page_rows.items(), key=lambda kv: -kv[1]["views"])
    ]

    # Часы.
    by_hour: dict[int, list[SiteVisit]] = defaultdict(list)
    for v in visits:
        by_hour[local(v.started_at).hour].append(v)
    report.rows[TAB_HOURS] = [
        [label, f"{hour:02d}:00", len(group), len({v.visitor_id for v in group}), sum(v.pageviews or 0 for v in group)]
        for hour, group in sorted(by_hour.items())
    ]

    # Посетители.
    per_visitor: dict[str, list[SiteVisit]] = defaultdict(list)
    for v in visits:
        per_visitor[v.visitor_id].append(v)
    report.rows[TAB_VISITORS] = []
    for vid, group in sorted(per_visitor.items(), key=lambda kv: kv[1][0].started_at):
        last = group[-1]
        person = visitors.get(vid)
        report.rows[TAB_VISITORS].append([
            label, vid[:12], "Новый" if vid in new_ids else "Вернувшийся",
            stamp(person.first_seen_at) if person else stamp(group[0].started_at),
            stamp(max(_aware(v.last_activity_at) for v in group)),
            len(group), sum(v.pageviews or 0 for v in group),
            _minutes(sum(v.engaged_seconds or 0 for v in group)),
            country_name(last.country), last.region, last.city, last.ip,
            last.device_type, last.device_vendor, last.device_model,
            " ".join(x for x in (last.os, last.os_version) if x),
            " ".join(x for x in (last.browser, last.browser_version) if x),
            last.screen, last.language, last.timezone,
            person.first_channel if person else group[0].channel,
            person.first_source if person else group[0].source,
            person.first_campaign if person else group[0].campaign,
            person.landing_path if person else group[0].landing_path,
            last.exit_path,
        ])

    # Визиты.
    paths: dict[str, list[str]] = defaultdict(list)
    for p in pages:
        trail = paths[p.visit_id]
        key = page_key(p.path)
        if not trail or trail[-1] != key:
            trail.append(key)
    report.rows[TAB_VISITS] = []
    for v in visits:
        trail = paths.get(v.id, [])
        trail_text = " → ".join(trail[:12]) + (" → …" if len(trail) > 12 else "")
        report.rows[TAB_VISITS].append([
            label, stamp(v.started_at), v.visitor_id[:12], "Новый" if v.is_new_visitor else "Вернувшийся",
            _minutes(_duration(v)), _minutes(v.engaged_seconds or 0), v.pageviews or 0, trail_text,
            v.landing_path, v.exit_path, v.channel, v.source, v.campaign, v.medium, v.term, v.content,
            v.click_id, v.referrer, country_name(v.country), v.region, v.city, v.postal_code, v.ip, v.edge,
            v.device_type, v.device_vendor, v.device_model, v.os, v.os_version, v.browser,
            v.browser_version, v.screen, v.viewport, v.language, v.languages, v.timezone,
            v.connection, v.color_scheme, v.load_ms if v.load_ms is not None else "", v.user_agent,
        ])
    return report


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ── Расписание ───────────────────────────────────────────────────────────────

#: Сколько пропущенных периодов отправлять за один проход после долгого простоя.
MAX_PERIODS_PER_PASS = 10


def next_period(now: datetime | None = None) -> tuple[datetime, datetime] | None:
    """Следующий период к отправке или None, если отправлять пока нечего."""
    now = now or datetime.now(UTC)
    every = timedelta(days=visits_settings.report_every_days)
    with visits_session() as db:
        last_end = db.scalar(select(func.max(SiteReport.period_end)).where(SiteReport.status == "ok"))
        if last_end is None:
            first = db.scalar(select(func.min(SiteVisit.started_at)))
            if first is None:
                return None
            start = local_midnight(_aware(first))
        else:
            start = _aware(last_end)
    end = start + every
    return (start, end) if end <= now else None


def run_due(
    send: Callable[[Report], None],
    *,
    analyses: Callable[[datetime, datetime], int] | None = None,
    now: datetime | None = None,
) -> int:
    """Отправить все созревшие периоды по очереди. Возвращает, сколько ушло.

    Сбой отправки записывается в журнал и прерывает проход: период не
    сдвигается, следующая попытка — в следующий проход.
    """
    sent = 0
    while sent < MAX_PERIODS_PER_PASS:
        period = next_period(now)
        if period is None:
            return sent
        start, end = period
        count = None
        if analyses is not None:
            try:
                count = analyses(start, end)
            except Exception as exc:  # noqa: BLE001 — без числа анализов отчёт всё равно нужен
                log.warning("visits: не посчитались анализы за период (%s)", exc)
        report = build_report(start, end, analyses=count, now=now)
        try:
            send(report)
        except Exception as exc:
            _log_report(start, end, "failed", str(exc)[:2000])
            raise
        _log_report(start, end, "ok", f"визитов: {report.visits}")
        sent += 1
    return sent


def _log_report(start: datetime, end: datetime, status: str, detail: str) -> None:
    with visits_session() as db:
        # Неудачная попытка за тот же период одна: повтор раз в час обновляет её,
        # а не копит сотню одинаковых строк за сутки простоя Google.
        row = db.scalar(
            select(SiteReport).where(SiteReport.period_end == end, SiteReport.status == "failed")
        )
        if row is None:
            row = SiteReport(period_start=start, period_end=end)
            db.add(row)
        row.status = status
        row.detail = detail
        row.sent_at = datetime.now(UTC)


__all__ = [
    "LOCAL_TZ",
    "Report",
    "TABS",
    "build_report",
    "next_period",
    "period_label",
    "run_due",
]
