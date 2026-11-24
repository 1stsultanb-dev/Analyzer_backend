"""Запись отчёта в Google-таблицу: вкладки, заголовки, строки периода.

Таблица принадлежит человеку, а не программе: у сервисных аккаунтов Google нет
своего места на Диске, создать файл они не могут. Человек создаёт пустую
таблицу и открывает её сервисному аккаунту на правку — остальное
(`ensure_layout`) делается здесь: русская локаль и пояс Алматы, вкладки с
закреплённой жирной шапкой, удаление пустого «Лист1».

Запись периода — замена, а не дописывание (`replace_period`): строки с его
меткой в колонке «Период» удаляются и пишутся заново. Живой период
переписывается каждые полминуты, и дублей от этого не появляется. Цена одной
записи — два запроса к Google: чтение колонки «Период» всех вкладок разом и
одна пакетная правка. Лимит Google — 60 запросов в минуту.

Типы ячеек задаются явно (`cell`): число — числом, дата — датой, текст — текстом
как есть. Метки UTM, реферер и заголовок страницы присылает посетитель, и
`=IMPORTXML(...)` в метке кампании, попади он формулой, отправил бы данные
таблицы на чужой сервер. Текстом он формулой не станет, а версия «14.2» не
станет 14 февраля.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from functools import lru_cache

import gspread
from google.oauth2.service_account import Credentials

from app.visits.config import visits_settings
from app.visits.report import TABS, Report

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
LOCALE = "ru_RU"
TIME_ZONE = "Asia/Almaty"
_DEFAULT_TABS = {"Лист1", "Sheet1"}
_EPOCH = datetime(1899, 12, 30)

#: Ячеек в одной пакетной правке. Google принимает запрос до ~10 МБ; вкладка
#: «Визиты» большого периода — десятки тысяч ячеек, и её строки уходят
#: несколькими правками подряд.
MAX_CELLS_PER_REQUEST = 40_000


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


# ── Ячейки ───────────────────────────────────────────────────────────────────


def _serial(value: datetime) -> float:
    return (value.replace(tzinfo=None) - _EPOCH).total_seconds() / 86400


def cell(value: object) -> dict:
    """Значение → CellData Google с явным типом."""
    if value is None or value == "":
        return {}
    if isinstance(value, bool):
        return {"userEnteredValue": {"stringValue": "да" if value else "нет"}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    if isinstance(value, datetime):
        return {
            "userEnteredValue": {"numberValue": round(_serial(value), 6)},
            "userEnteredFormat": {"numberFormat": {"type": "DATE_TIME", "pattern": "dd.mm.yyyy hh:mm"}},
        }
    if isinstance(value, date):
        return {
            "userEnteredValue": {"numberValue": _serial(datetime(value.year, value.month, value.day))},
            "userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}},
        }
    return {"userEnteredValue": {"stringValue": str(value)[:50_000]}}


# ── Разметка ─────────────────────────────────────────────────────────────────


def ensure_layout(sheet: gspread.Spreadsheet) -> dict[str, int]:
    """Вкладки отчёта с шапкой → {название: sheetId}. Повтор ничего не дублирует."""
    meta = sheet.fetch_sheet_metadata(
        {"fields": "properties(locale,timeZone),sheets(properties(sheetId,title,gridProperties(columnCount)))"}
    )
    columns = {
        s["properties"]["title"]: s["properties"].get("gridProperties", {}).get("columnCount", 26)
        for s in meta["sheets"]
    }
    requests: list[dict] = []
    if meta["properties"].get("locale") != LOCALE or meta["properties"].get("timeZone") != TIME_ZONE:
        requests.append({"updateSpreadsheetProperties": {
            "properties": {"locale": LOCALE, "timeZone": TIME_ZONE},
            "fields": "locale,timeZone",
        }})
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    for title, header in TABS.items():
        if title not in existing:
            requests.append({"addSheet": {"properties": {
                "title": title,
                "gridProperties": {"rowCount": 1000, "columnCount": len(header)},
            }}})
    if requests:
        reply = sheet.batch_update({"requests": requests})
        for answer in reply.get("replies", []):
            added = answer.get("addSheet")
            if added:
                existing[added["properties"]["title"]] = added["properties"]["sheetId"]

    # Шапки — одним чтением первой строки всех вкладок.
    titles = list(TABS)
    heads = sheet.values_batch_get([f"'{t}'!1:1" for t in titles]).get("valueRanges", [])
    fixes: list[dict] = []
    for title, got in zip(titles, heads):
        header = TABS[title]
        current = (got.get("values") or [[]])[0]
        if current == header:
            continue
        sheet_id = existing[title]
        # `appendCells` добавляет строки, но не колонки: сетка должна быть не
        # уже шапки, иначе Google ответит «exceeds grid limits».
        missing = len(header) - columns.get(title, len(header))
        if missing > 0:
            fixes.append({"appendDimension": {"sheetId": sheet_id, "dimension": "COLUMNS", "length": missing}})
        fixes += [
            {"updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
                "rows": [{"values": [cell(h) for h in header]}],
                "fields": "userEnteredValue",
            }},
            {"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "wrapStrategy": "WRAP"}},
                "fields": "userEnteredFormat(textFormat,wrapStrategy)",
            }},
            {"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": 1}},
                "fields": "gridProperties(frozenRowCount,frozenColumnCount)",
            }},
        ]
    for title, sheet_id in existing.items():
        if title in _DEFAULT_TABS and title not in TABS:
            if not any(sheet.worksheet(title).get_values("A1:C3")):
                fixes.append({"deleteSheet": {"sheetId": sheet_id}})
    if fixes:
        sheet.batch_update({"requests": fixes})
    return {title: existing[title] for title in TABS}


# ── Замена периода ───────────────────────────────────────────────────────────


def _runs(indexes: list[int]) -> list[tuple[int, int]]:
    """[3,4,5,9] → [(3,6),(9,10)]: подряд идущие строки одним удалением."""
    runs: list[tuple[int, int]] = []
    for index in sorted(indexes):
        if runs and runs[-1][1] == index:
            runs[-1] = (runs[-1][0], index + 1)
        else:
            runs.append((index, index + 1))
    return runs


def plan_replace(
    report: Report, sheet_ids: dict[str, int], column_a: dict[str, list[str]]
) -> list[list[dict]]:
    """Правки, заменяющие строки периода во всех вкладках, — пачками по размеру.

    Удаления идут первыми и снизу вверх, чтобы номера строк не съезжали;
    `appendCells` дописывает после последней заполненной строки. Правки внутри
    одного запроса Google выполняет по порядку.
    """
    deletes: list[dict] = []
    for title, values in column_a.items():
        mine = [i for i, value in enumerate(values) if i > 0 and value == report.label]
        for start, end in reversed(_runs(mine)):
            deletes.append({"deleteDimension": {"range": {
                "sheetId": sheet_ids[title], "dimension": "ROWS", "startIndex": start, "endIndex": end,
            }}})

    batches: list[list[dict]] = [deletes]
    size = 0
    for title in TABS:
        rows = report.rows.get(title) or []
        width = len(TABS[title])
        step = max(1, MAX_CELLS_PER_REQUEST // width)
        for offset in range(0, len(rows), step):
            chunk = rows[offset : offset + step]
            if size + len(chunk) * width > MAX_CELLS_PER_REQUEST and batches[-1]:
                batches.append([])
                size = 0
            batches[-1].append({"appendCells": {
                "sheetId": sheet_ids[title],
                "rows": [{"values": [cell(v) for v in row]} for row in chunk],
                "fields": "userEnteredValue,userEnteredFormat.numberFormat",
            }})
            size += len(chunk) * width
    return [batch for batch in batches if batch]


class ReportSheet:
    """Таблица отчёта с запомненной разметкой: проверка вкладок — один раз на процесс."""

    def __init__(self) -> None:
        self._sheet: gspread.Spreadsheet | None = None
        self._ids: dict[str, int] | None = None
        self._lock = threading.Lock()

    def _ready(self) -> tuple[gspread.Spreadsheet, dict[str, int]]:
        if self._sheet is None or self._ids is None:
            sheet = open_report_sheet()
            self._ids = ensure_layout(sheet)
            self._sheet = sheet
        return self._sheet, self._ids

    def replace_period(self, report: Report) -> None:
        with self._lock:
            try:
                sheet, ids = self._ready()
                titles = list(TABS)
                got = sheet.values_batch_get([f"'{t}'!A:A" for t in titles]).get("valueRanges", [])
                column_a = {
                    title: [row[0] if row else "" for row in (part.get("values") or [])]
                    for title, part in zip(titles, got)
                }
                for batch in plan_replace(report, ids, column_a):
                    sheet.batch_update({"requests": batch})
            except Exception:
                # Вкладку могли удалить или переименовать руками — в следующий
                # раз разметка проверится заново.
                self._sheet = self._ids = None
                raise


report_sheet = ReportSheet()


def send_report(report: Report) -> None:
    report_sheet.replace_period(report)


__all__ = [
    "ReportSheet",
    "SheetsError",
    "cell",
    "ensure_layout",
    "open_report_sheet",
    "plan_replace",
    "report_sheet",
    "send_report",
]
