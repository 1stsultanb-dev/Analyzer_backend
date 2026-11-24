"""Настройки учёта посещений — отдельный BaseSettings, как у соседних модулей.

Переменные окружения:
    VISITS_ENABLED                 — "false" выключает учёт целиком (по умолчанию true)
    VISITS_REPORT_SPREADSHEET_ID   — Google-таблица отчёта: id или ссылка целиком
    VISITS_SERVICE_ACCOUNT_JSON    — ключ сервисного аккаунта (JSON). Не задан —
                                     берётся GOOGLE_SERVICE_ACCOUNT_JSON
    VISITS_REPORT_EVERY_DAYS       — период отчёта в днях (по умолчанию 3)
    VISITS_RETENTION_DAYS          — сколько дней хранить сырые визиты (по умолчанию 90).
                                     Отчёты к этому времени давно в таблице.
    VISITS_HITS_PER_MINUTE         — потолок запросов учёта с одного адреса в минуту
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/visits/config.py -> корень репозитория
_REPO_ROOT = Path(__file__).resolve().parents[3]

_SPREADSHEET_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


class VisitsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(_REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_prefix="VISITS_",
    )

    enabled: bool = True
    report_spreadsheet_id: str = ""
    service_account_json: str = Field(
        default="",
        validation_alias=AliasChoices("VISITS_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_JSON"),
    )
    report_every_days: float = Field(default=3.0, gt=0)
    retention_days: int = Field(default=90, ge=7)
    hits_per_minute: int = Field(default=120, ge=10)

    @field_validator("report_spreadsheet_id", "service_account_json", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1].strip()
        return value

    @field_validator("report_spreadsheet_id")
    @classmethod
    def _id_from_url(cls, value: str) -> str:
        """Ссылку из адресной строки вставляют чаще, чем id, — принимаем обе."""
        match = _SPREADSHEET_ID.search(value)
        return match.group(1) if match else value

    @property
    def report_configured(self) -> bool:
        return bool(self.enabled and self.report_spreadsheet_id and self.service_account_json)


@lru_cache
def get_visits_settings() -> VisitsSettings:
    return VisitsSettings()


visits_settings = get_visits_settings()

__all__ = ["VisitsSettings", "get_visits_settings", "visits_settings"]
