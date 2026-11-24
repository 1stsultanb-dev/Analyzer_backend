"""Учёт посещений: два открытых маршрута, куда браузер шлёт просмотры.

Лежит снаружи пакета `app.visits` по той же причине, что маршруты «Книг» и
«Финансов»: здесь сходятся модули. Пакет учёта не знает про учётки — а отличить
сотрудника от посетителя можно только по cookie «Финансов», и это делается тут.

Оба маршрута отвечают 204 всегда, кроме неверного тела: сбой учёта — не повод
показывать человеку ошибку, и страница его ответа не ждёт (`sendBeacon`).
Пути нейтральные (`/site/...`, а не `/track` или `/analytics`), иначе
блокировщики рекламы режут запросы по слову в адресе и цифры занижаются.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app.core.config import settings
from app.visits import collect
from app.visits.config import visits_settings
from app.visits.describe import host_of

log = logging.getLogger(__name__)

router = APIRouter(prefix="/site", tags=["visits"])


def _own_hosts() -> set[str]:
    hosts = {host_of(origin) for origin in settings.cors_origins}
    return {h for h in hosts if h}


def _staff_name(request: Request) -> str:
    """Имя вошедшего в «Финансы» или пустая строка. «Финансы» выключены — пусто."""
    try:
        from app.finance import auth
        from app.finance.config import finance_settings
        from app.finance.db import finance_session
    except Exception:  # noqa: BLE001 — модуль «Финансы» удалён
        return ""
    token = request.cookies.get(auth.COOKIE_NAME)
    if not token or not finance_settings.enabled:
        return ""
    try:
        with finance_session() as session:
            member = auth.resolve(session, token)
    except Exception as exc:  # noqa: BLE001
        log.debug("visits: сотрудник не определился (%s)", exc)
        return ""
    if member is None:
        return ""
    return member.full_name or member.email or member.phone or "сотрудник"


def _allowed(request: Request) -> bool:
    if not visits_settings.enabled:
        return False
    ip = collect.client_ip(request.headers, request.client.host if request.client else "")
    return collect.limiter.allow(ip or "?", visits_settings.hits_per_minute)


@router.post("/seen", status_code=204)
def seen(payload: collect.SeenPayload, request: Request) -> Response:
    if _allowed(request):
        try:
            collect.record_seen(
                payload,
                request.headers,
                fallback_ip=request.client.host if request.client else "",
                staff_name=_staff_name(request),
                own_hosts=_own_hosts(),
            )
        except Exception as exc:  # noqa: BLE001 — учёт не мешает странице
            log.warning("visits: просмотр не записан (%s: %s)", type(exc).__name__, exc)
    return Response(status_code=204)


@router.post("/left", status_code=204)
def left(payload: collect.LeftPayload, request: Request) -> Response:
    if _allowed(request):
        try:
            collect.record_left(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("visits: уход со страницы не записан (%s: %s)", type(exc).__name__, exc)
    return Response(status_code=204)
