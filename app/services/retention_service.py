"""Присланное на анализ живёт неделю и удаляется само.

Каждая загрузка выписки — с сайта или через Telegram — оставляет в базе весь
разобранный документ JSON-ом (`sessions.payload`), а скан ещё и черновик
распознавания (`ocr_reviews`). Срока у этих строк не было, и база росла с каждой
выпиской, хотя нужны они ровно на время работы с ней: предпросмотр, правка
строк, выгрузка в Excel.

Что удаляется по сроку (`ANALYZER_RETENTION_DAYS`, по умолчанию 7 дней):

* `sessions` — разобранные выписки анализатора;
* `ocr_reviews` — черновики распознавания сканов;
* `jobs` и их файлы в `data/job-uploads` — очередь фоновых разборов;
* файлы старого файлового хранилища (`data/sessions`, `data/ocr-reviews`).

Что не трогается — это не присланное, а сделанное людьми или учёт:
шаблоны и «образы» (`transformation_templates`), память исправлений, правила
OCR, курсы валют, выписки в «Финансах» (там это проводки учёта), полка
«Таблиц». Образцы подключения нового банка (`onboarding_samples`) сохраняют
нарочно — выписки и черновики, на которые они ссылаются, остаются.

Удаление идёт пачками: один проход не держит долгую транзакцию и не грузит в
память тысячи документов разом — из базы читаются только ключи.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.database import db_session
from app.core.paths import JOB_UPLOADS_DIR, OCR_REVIEWS_DIR, SESSIONS_DIR
from app.models.persistence import (
    JobRecord,
    OCRReviewRecord,
    OnboardingSampleRecord,
    SessionRecord,
)

log = logging.getLogger(__name__)

#: Строк за один `DELETE`. Документ выписки — сотни килобайт JSON, и пачка в
#: двести строк остаётся короткой транзакцией даже на больших выписках.
BATCH = 200


@dataclass
class PurgeResult:
    sessions: int = 0
    ocr_reviews: int = 0
    jobs: int = 0
    files: int = 0

    @property
    def total(self) -> int:
        return self.sessions + self.ocr_reviews + self.jobs + self.files


def retention() -> timedelta:
    return timedelta(days=settings.analyzer_retention_days)


def purge_expired(now: datetime | None = None, max_age: timedelta | None = None) -> PurgeResult:
    """Удалить присланное на анализ старше срока. Возвращает, сколько ушло."""
    cutoff = (now or datetime.now(UTC)) - (max_age or retention())
    result = PurgeResult()

    with db_session() as session:
        samples = session.execute(
            select(OnboardingSampleRecord.session_id, OnboardingSampleRecord.review_id)
        ).all()
    kept_sessions = {row.session_id for row in samples if row.session_id}
    kept_reviews = {row.review_id for row in samples if row.review_id}

    result.sessions = _purge_rows(
        SessionRecord.session_id, SessionRecord.created_at, cutoff, kept_sessions
    )
    result.ocr_reviews = _purge_rows(
        OCRReviewRecord.review_id, OCRReviewRecord.created_at, cutoff, kept_reviews
    )
    result.jobs, job_files = _purge_jobs(cutoff)
    result.files = job_files + sum(
        _purge_files(folder, cutoff) for folder in (JOB_UPLOADS_DIR, SESSIONS_DIR, OCR_REVIEWS_DIR)
    )
    return result


def _purge_rows(key_column, created_column, cutoff: datetime, kept: set[str]) -> int:
    """Удалить строки старше `cutoff` пачками, не трогая ключи из `kept`."""
    model = key_column.class_
    removed = 0
    while True:
        with db_session() as session:
            query = select(key_column).where(created_column < cutoff)
            if kept:
                query = query.where(key_column.not_in(kept))
            keys = list(session.scalars(query.limit(BATCH)))
            if not keys:
                return removed
            session.execute(delete(model).where(key_column.in_(keys)))
        removed += len(keys)


def _purge_jobs(cutoff: datetime) -> tuple[int, int]:
    """Строки очереди и загруженные для них файлы."""
    removed = files = 0
    while True:
        with db_session() as session:
            rows = session.execute(
                select(JobRecord.job_id, JobRecord.payload)
                .where(JobRecord.created_at < cutoff)
                .limit(BATCH)
            ).all()
            if not rows:
                return removed, files
            for row in rows:
                upload = (row.payload or {}).get("upload_path")
                if upload and _unlink(Path(upload)):
                    files += 1
            session.execute(delete(JobRecord).where(JobRecord.job_id.in_([r.job_id for r in rows])))
        removed += len(rows)


def _purge_files(folder: Path, cutoff: datetime) -> int:
    """Файлы прямо в папке, изменённые раньше `cutoff`. Вложенные папки не трогаем."""
    if not folder.is_dir():
        return 0
    border = cutoff.timestamp()
    removed = 0
    for path in folder.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < border and _unlink(path):
                removed += 1
        except OSError as exc:  # файл унесли между листингом и проверкой
            log.debug("retention: %s пропущен (%s)", path.name, exc)
    return removed


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("retention: не удалось удалить %s (%s)", path.name, exc)
        return False


__all__ = ["BATCH", "PurgeResult", "purge_expired", "retention"]
