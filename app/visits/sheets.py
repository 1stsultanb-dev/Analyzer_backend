"""Запись отчёта в Google-таблицу: вкладки, заголовки, строки снизу.

Таблица принадлежит человеку, а не программе: у сервисных аккаунтов Google нет
своего места на Диске, создать файл они не могут. Человек создаёт пустую
таблицу и открывает её сервисному аккаунту на правку — остальное
(`ensure_layout`) делается здесь: русская локаль и пояс Алматы, вкладки с
закреплённой жирной шапкой, удаление пустого «Лист1».

Метки UTM, реферер и заголовок страницы приходят от посетителя. Строка,
начинающаяся с «=», в таблице стала бы формулой — и `=IMPORTXML(...)` в метке
кампании отправил бы данные таблицы на чужой сервер. Поэтому весь текст, кроме
дат отчёта, уходит с апострофом впереди (`safe_cell`).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials

from app.visits.config import visits_settings
from app.visits.report import TABS, Report

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
LOCALE = "ru_RU"
TIME_ZONE = "Asia/Almaty"
_DEFAULT_TABS = {"Лист1", "Sheet1"}


class SheetsError(Exception):
    pass


@lru_cache(maxsize=1)
def _client() -> gspread.Client:
    raw = visits_settings.service_account_json
    if not raw:
        raise SheetsError("Не задан ключ сервисного аккаунта (VISITS_SERVICE_ACCOUNT_JSON)")
    try:
        info = json.loads(raw)
    except ValueError as exc:
        raise SheetsError(f"Ключ сервисного аккаунта — не JSON: {exc}") from exc
    return gspread.authorize(Credentials.from_service_account_info(info, scopes=SCOPES))


def open_report_sheet() -> gspread.Spreadsheet:
    if not visits_settings.report_spreadsheet_id:
        raise SheetsError("Не задана таблица отчёта (VISITS_REPORT_SPREADSHEET_ID)")
    return _client().open_by_key(visits_settings.report_spreadsheet_id)


#: Даты отчёта («24.11.2026», «24.11.2026 14:05») — единственный текст, который
#: Google должен разобрать сам: тогда по ним работают сортировка и фильтры.
_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}( \d{2}:\d{2})?$")


def safe_cell(value: object) -> object:
    """Числа — числами, даты — датами, всё остальное — текстом как есть.

    Апостроф впереди Google не показывает, но читает строку текстом: так
    формула из метки UTM не исполнится, а версия «14.2» не станет 14 февраля.
    """
    if value is None:
        return ""
    if isinstance(value, str) and value and not _DATE.match(value):
        return "'" + value
    return value


def ensure_layout(sheet: gspread.Spreadsheet) -> dict[str, gspread.Worksheet]:
    """Вкладки отчёта с шапкой. Повторный вызов ничего не ломает и не дублирует."""
    properties = sheet.fetch_sheet_metadata({"fields": "properties(locale,timeZone)"})["properties"]
    if properties.get("locale") != LOCALE or properties.get("timeZone") != TIME_ZONE:
        sheet.batch_update({"requests": [{
            "updateSpreadsheetProperties": {
                "properties": {"locale": LOCALE, "timeZone": TIME_ZONE},
                "fields": "locale,timeZone",
            }
        }]})

    existing = {ws.title: ws for ws in sheet.worksheets()}
    tabs: dict[str, gspread.Worksheet] = {}
    requests: list[dict] = []
    for title, header in TABS.items():
        ws = existing.get(title)
        if ws is None:
            ws = sheet.add_worksheet(title=title, rows=1000, cols=len(header))
        first_row = ws.row_values(1)
        if first_row != header:
            ws.update([header], "A1", value_input_option="RAW")
            requests += [
                {"repeatCell": {
                    "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "wrapStrategy": "WRAP"}},
                    "fields": "userEnteredFormat(textFormat,wrapStrategy)",
                }},
                {"updateSheetProperties": {
                    "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1}},
                    "fields": "gridProperties(frozenRowCount,frozenColumnCount)",
                }},
            ]
        tabs[title] = ws

    for title, ws in existing.items():
        if title in _DEFAULT_TABS and title not in TABS and not any(ws.get_values("A1:C3")):
            requests.append({"deleteSheet": {"sheetId": ws.id}})
    if requests:
        sheet.batch_update({"requests": requests})
    return tabs


def send_report(report: Report) -> None:
    sheet = open_report_sheet()
    tabs = ensure_layout(sheet)
    for title, rows in report.rows.items():
        if not rows:
            continue
        tabs[title].append_rows(
            [[safe_cell(cell) for cell in row] for row in rows],
            value_input_option="USER_ENTERED",
            insert_data_option="INSERT_ROWS",
            table_range="A1",
        )


__all__ = ["SheetsError", "ensure_layout", "open_report_sheet", "safe_cell", "send_report"]
