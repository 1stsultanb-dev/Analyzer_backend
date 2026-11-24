"""Учёт посещений: запись визитов, отчёт за период и его расписание.

Google здесь не участвует: отправка подменяется списком, проверяется то, что
ушло бы в таблицу. Обещания модуля:

* один браузер — один посетитель, визит — до тишины, просмотр — страница;
* время на странице присылается нарастающим итогом и не удваивается повтором;
* боты и сотрудники «Финансов» не попадают в маркетинговые цифры;
* период отчёта не сдвигается, пока отчёт не ушёл, и пропущенные уходят все;
* текст от посетителя не становится формулой в таблице;
* модуль не знает про «Финансы» — сотрудника определяет маршрут снаружи.
"""
from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api.router import api_router
from app.visits import collect, report
from app.visits.db import visits_session
from app.visits.models import SitePageView, SiteReport, SiteVisit, SiteVisitor
from app.visits.sheets import safe_cell

BACKEND = Path(__file__).resolve().parents[1]
DAY = datetime(2026, 11, 23, 19, 0, tzinfo=UTC)  # 24.11.2026 00:00 по Алматы
CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PHONE = (
    "Mozilla/5.0 (Linux; Android 14; SM-A536B) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
HEADERS = {"user-agent": CHROME, "cf-ipcountry": "KZ", "x-client-ip": "5.76.1.1"}


def _wipe() -> None:
    with visits_session() as db:
        for model in (SitePageView, SiteVisit, SiteVisitor, SiteReport):
            db.execute(delete(model))


@pytest.fixture(autouse=True)
def clean():
    _wipe()
    yield
    _wipe()


def _seen(v: str, s: str, p: str, *, at: datetime, path: str = "/", headers=None, staff: str = "", **extra):
    payload = collect.SeenPayload(v=v, s=s, p=p, path=path, host="finance-analyze.win", **extra)
    collect.record_seen(payload, headers or HEADERS, staff_name=staff, now=at)


def _left(v: str, s: str, p: str, engaged: float, *, at: datetime, scroll: int = 0):
    collect.record_left(collect.LeftPayload(v=v, s=s, p=p, engaged=engaged, scroll=scroll), now=at)


# ── Запись ───────────────────────────────────────────────────────────────────


def test_posetitel_vizit_i_prosmotry() -> None:
    _seen("visitor_one_1", "visit_one_001", "page_one_0001", at=DAY, path="/", ref="https://www.google.kz/")
    _seen("visitor_one_1", "visit_one_001", "page_one_0002", at=DAY + timedelta(minutes=2), path="/services")
    # Тот же просмотр пришёл повторно (сеть переслала) — не считается дважды.
    _seen("visitor_one_1", "visit_one_001", "page_one_0002", at=DAY + timedelta(minutes=2), path="/services")

    with visits_session() as db:
        visitor = db.get(SiteVisitor, "visitor_one_1")
        visit = db.get(SiteVisit, "visit_one_001")
        assert visitor.visits == 1 and visitor.pageviews == 2
        assert visit.pageviews == 2
        assert visit.landing_path == "/" and visit.exit_path == "/services"
        assert visit.channel == "Поиск" and visit.source == "Google"
        assert visit.country == "KZ" and visit.ip == "5.76.1.1"
        assert visit.browser == "Chrome" and visit.device_type == "Компьютер"
        assert visit.is_new_visitor is True
        assert visitor.first_source == "Google"


def test_vremya_na_stranice_ne_udvaivaetsya() -> None:
    _seen("visitor_two_1", "visit_two_001", "page_two_0001", at=DAY)
    _left("visitor_two_1", "visit_two_001", "page_two_0001", 30, at=DAY + timedelta(seconds=30), scroll=40)
    _left("visitor_two_1", "visit_two_001", "page_two_0001", 30, at=DAY + timedelta(seconds=31), scroll=20)
    _left("visitor_two_1", "visit_two_001", "page_two_0001", 75, at=DAY + timedelta(seconds=80), scroll=90)

    with visits_session() as db:
        page = db.get(SitePageView, "page_two_0001")
        assert page.engaged_seconds == 75 and page.scroll_depth == 90
        assert db.get(SiteVisit, "visit_two_001").engaged_seconds == 75
        assert db.get(SiteVisitor, "visitor_two_1").engaged_seconds == 75


def test_chuzhoy_prosmotr_ne_menyaet_vremya() -> None:
    _seen("visitor_thr_1", "visit_thr_001", "page_thr_0001", at=DAY)
    _left("visitor_xxx_1", "visit_thr_001", "page_thr_0001", 500, at=DAY)
    with visits_session() as db:
        assert db.get(SitePageView, "page_thr_0001").engaged_seconds == 0


def test_vernuvshiysya_posetitel() -> None:
    _seen("visitor_ret_1", "visit_ret_001", "page_ret_0001", at=DAY)
    _seen("visitor_ret_1", "visit_ret_002", "page_ret_0002", at=DAY + timedelta(days=1))
    with visits_session() as db:
        assert db.get(SiteVisit, "visit_ret_001").is_new_visitor is True
        assert db.get(SiteVisit, "visit_ret_002").is_new_visitor is False
        assert db.get(SiteVisitor, "visitor_ret_1").visits == 2


def test_marshrut_otkryt_i_ne_padaet(monkeypatch) -> None:
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    client = TestClient(app)
    body = {"v": "visitor_api_1", "s": "visit_api_001", "p": "page_api_0001", "path": "/"}
    assert client.post("/api/v1/site/seen", json=body, headers=HEADERS).status_code == 204
    assert client.post("/api/v1/site/seen", json={"v": "плохой"}).status_code == 422

    # Сбой записи не доходит до страницы: ответ всё равно 204.
    def broken(*_a, **_k):
        raise RuntimeError("база легла")

    monkeypatch.setattr(collect, "record_seen", broken)
    assert client.post("/api/v1/site/seen", json=body, headers=HEADERS).status_code == 204


def test_ogranichitel_po_adresu() -> None:
    limiter = collect.RateLimiter()
    assert all(limiter.allow("1.1.1.1", 3, now=0) for _ in range(3))
    assert not limiter.allow("1.1.1.1", 3, now=1)
    assert limiter.allow("2.2.2.2", 3, now=1)
    assert limiter.allow("1.1.1.1", 3, now=61)


def test_adres_iz_zagolovkov() -> None:
    assert collect.client_ip({"x-client-ip": "1.1.1.1", "cf-connecting-ip": "2.2.2.2"}) == "1.1.1.1"
    assert collect.client_ip({"cf-connecting-ip": "2.2.2.2", "x-forwarded-for": "3.3.3.3, 4.4.4.4"}) == "2.2.2.2"
    assert collect.client_ip({"x-forwarded-for": "3.3.3.3, 4.4.4.4"}) == "3.3.3.3"
    assert collect.client_ip({}, "9.9.9.9") == "9.9.9.9"


# ── Отчёт ────────────────────────────────────────────────────────────────────


def _period():
    return DAY, DAY + timedelta(days=3)


def test_otchet_bez_botov_i_sotrudnikov() -> None:
    _seen("visitor_rep_1", "visit_rep_001", "page_rep_0001", at=DAY + timedelta(hours=10), ref="https://l.instagram.com/")
    _seen("visitor_rep_1", "visit_rep_001", "page_rep_0002", at=DAY + timedelta(hours=10, minutes=1), path="/web-excel")
    _left("visitor_rep_1", "visit_rep_001", "page_rep_0002", 120, at=DAY + timedelta(hours=10, minutes=3))
    _seen("visitor_rep_2", "visit_rep_002", "page_rep_0003", at=DAY + timedelta(days=1, hours=3),
          headers={**HEADERS, "user-agent": PHONE})
    _seen("visitor_bot_1", "visit_bot_001", "page_bot_0001", at=DAY + timedelta(hours=1),
          headers={**HEADERS, "user-agent": "Googlebot/2.1"})
    _seen("visitor_stf_1", "visit_stf_001", "page_stf_0001", at=DAY + timedelta(hours=2), staff="Асхат")
    # Тот же сотрудник позже без cookie — всё равно сотрудник.
    _seen("visitor_stf_1", "visit_stf_002", "page_stf_0002", at=DAY + timedelta(hours=5))

    start, end = _period()
    built = report.build_report(start, end, analyses=1, now=end)
    summary = built.rows[report.TAB_SUMMARY][0]
    header = report.TABS[report.TAB_SUMMARY]
    row = dict(zip(header, summary))

    assert row["Период"] == "24.11.2026 – 26.11.2026"
    assert row["Уникальные посетители"] == 2
    assert row["Визиты"] == 2
    assert row["Просмотры страниц"] == 3
    assert row["Выписок отправлено на анализ"] == 1
    assert row["Конверсия посетителя в анализ, %"] == 50.0
    assert row["Сотрудники на сайте (не в цифрах)"] == 1
    assert row["Визиты ботов (не в цифрах)"] == 1
    assert row["Доля телефонов, %"] == 50.0
    assert row["Отказы, %"] == 50.0  # второй — одна страница без активного времени

    assert len(built.rows[report.TAB_DAYS]) == 3  # три дня периода, даже пустые
    sources = {r[2] for r in built.rows[report.TAB_SOURCES]}
    assert "Instagram" in sources
    assert len(built.rows[report.TAB_VISITORS]) == 2  # бот и сотрудник не вошли
    assert all("Асхат" not in str(r) for rows in built.rows.values() for r in rows)
    trail = built.rows[report.TAB_VISITS][0][7]
    assert trail == "/ → /web-excel"
    for tab, rows in built.rows.items():
        for r in rows:
            assert len(r) == len(report.TABS[tab]), f"{tab}: колонок не столько, сколько в шапке"


def test_stranicy_kartochki_skladyvayutsya() -> None:
    from app.visits.describe import page_key

    assert page_key("/finance/contracts/8f3e2b1c9d0a4e5f8f3e2b1c9d0a4e5f") == "/finance/contracts/:id"
    assert page_key("/finance/contracts/123?tab=1") == "/finance/contracts/:id"
    assert page_key("/web-excel") == "/web-excel"


# ── Расписание ───────────────────────────────────────────────────────────────


def test_period_ne_sdvigaetsya_poka_otchet_ne_ushel() -> None:
    _seen("visitor_sch_1", "visit_sch_001", "page_sch_0001", at=DAY + timedelta(hours=9))
    sent: list[report.Report] = []

    # Три дня ещё не прошли — отправлять нечего.
    assert report.run_due(sent.append, now=DAY + timedelta(days=2)) == 0

    def broken(_report):
        raise RuntimeError("Google не ответил")

    with pytest.raises(RuntimeError):
        report.run_due(broken, now=DAY + timedelta(days=3, hours=1))
    with pytest.raises(RuntimeError):
        report.run_due(broken, now=DAY + timedelta(days=3, hours=2))
    with visits_session() as db:
        failed = list(db.scalars(select(SiteReport)))
        assert len(failed) == 1 and failed[0].status == "failed", "повтор копит строки сбоя"

    # Сервер лежал неделю: уходят оба созревших периода, по порядку.
    assert report.run_due(sent.append, now=DAY + timedelta(days=7)) == 2
    assert [r.start for r in sent] == [DAY, DAY + timedelta(days=3)]
    assert report.run_due(sent.append, now=DAY + timedelta(days=7)) == 0
    assert report.next_period(DAY + timedelta(days=9)) == (DAY + timedelta(days=6), DAY + timedelta(days=9))


def test_bez_dannyh_otchet_ne_nachinaetsya() -> None:
    assert report.run_due(lambda _r: None, now=DAY) == 0


def test_staroe_uchityvaetsya_i_udalyaetsya() -> None:
    _seen("visitor_old_1", "visit_old_001", "page_old_0001", at=DAY - timedelta(days=200))
    _seen("visitor_new_1", "visit_new_001", "page_new_0001", at=DAY)
    assert collect.purge_old(now=DAY) == 2  # визит и просмотр; посетителю меньше года
    with visits_session() as db:
        assert db.get(SiteVisit, "visit_old_001") is None
        assert db.get(SiteVisit, "visit_new_001") is not None
        assert db.get(SiteVisitor, "visitor_old_1") is not None


# ── Таблица ──────────────────────────────────────────────────────────────────


def test_tekst_ne_stanovitsya_formuloy() -> None:
    assert safe_cell('=IMPORTXML("https://evil", "//a")') == "'=IMPORTXML(\"https://evil\", \"//a\")"
    assert safe_cell("14.2") == "'14.2"  # версия iOS, а не 14 февраля
    assert safe_cell("24.11.2026 14:05") == "24.11.2026 14:05"
    assert safe_cell("24.11.2026") == "24.11.2026"
    assert safe_cell(42) == 42 and safe_cell(None) == "" and safe_cell("") == ""


def test_modul_ne_znaet_pro_finansy() -> None:
    for path in (BACKEND / "app" / "visits").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("app.finance", "app.bbc", "app.webexcel")), path.name
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(("app.finance", "app.bbc")), path.name
