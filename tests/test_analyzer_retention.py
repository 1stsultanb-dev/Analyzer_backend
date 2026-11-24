"""Присланное на анализ живёт неделю и удаляется само.

Без срока каждая выписка — с сайта или из Telegram — оставалась в базе целиком
(весь разобранный документ JSON-ом), и база росла с каждой загрузкой. Проверки
держат три обещания:

* старше срока — удаляется: выписки, черновики OCR, задачи и их файлы;
* моложе срока и сохранённое нарочно (образцы подключения банка) — остаётся;
* скан в памяти процесса живёт не дольше, чем выписка в базе.
"""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete

from app.api.routes import scanned
from app.core.database import db_session
from app.core.paths import JOB_UPLOADS_DIR
from app.models.persistence import (
    JobRecord,
    OCRReviewRecord,
    OnboardingProjectRecord,
    OnboardingSampleRecord,
    SessionRecord,
)
from app.services import retention_service

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=8)
FRESH = NOW - timedelta(days=1)


def _id() -> str:
    return "rt" + uuid.uuid4().hex[:20]


@pytest.fixture
def made():
    """Всё, что тест завёл, удаляется после него — даже если тест упал."""
    created: dict[str, list[str]] = {"sessions": [], "reviews": [], "jobs": [], "projects": [], "files": []}
    yield created
    with db_session() as session:
        session.execute(delete(OnboardingSampleRecord).where(OnboardingSampleRecord.project_id.in_(created["projects"])))
        session.execute(delete(OnboardingProjectRecord).where(OnboardingProjectRecord.project_id.in_(created["projects"])))
        session.execute(delete(SessionRecord).where(SessionRecord.session_id.in_(created["sessions"])))
        session.execute(delete(OCRReviewRecord).where(OCRReviewRecord.review_id.in_(created["reviews"])))
        session.execute(delete(JobRecord).where(JobRecord.job_id.in_(created["jobs"])))
    for path in created["files"]:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _session(made, at: datetime) -> str:
    key = _id()
    with db_session() as session:
        session.add(SessionRecord(session_id=key, parser_key="kaspi", source_filename="x.pdf", title="x", created_at=at, payload={}))
    made["sessions"].append(key)
    return key


def _review(made, at: datetime) -> str:
    key = _id()
    with db_session() as session:
        session.add(OCRReviewRecord(review_id=key, source_filename="x.png", created_at=at, payload={}))
    made["reviews"].append(key)
    return key


def _exists(model, key: str) -> bool:
    with db_session() as session:
        return session.get(model, key) is not None


def test_starshe_nedeli_udalyaetsya_molozhe_ostaetsya(made) -> None:
    old_session, fresh_session = _session(made, OLD), _session(made, FRESH)
    old_review, fresh_review = _review(made, OLD), _review(made, FRESH)

    result = retention_service.purge_expired(now=NOW)

    assert not _exists(SessionRecord, old_session)
    assert _exists(SessionRecord, fresh_session)
    assert not _exists(OCRReviewRecord, old_review)
    assert _exists(OCRReviewRecord, fresh_review)
    assert result.sessions >= 1 and result.ocr_reviews >= 1


def test_obrazcy_podklyucheniya_banka_ne_udalyayutsya(made) -> None:
    """Образец нового банка сохраняют нарочно — его выписка и черновик остаются."""
    kept_session, kept_review = _session(made, OLD), _review(made, OLD)
    project = _id()
    with db_session() as session:
        session.add(OnboardingProjectRecord(project_id=project, name="Банк", bank_name="Банк"))
        session.flush()
        session.add(OnboardingSampleRecord(
            sample_id=_id(), project_id=project, source_filename="x.pdf",
            session_id=kept_session, review_id=kept_review,
        ))
    made["projects"].append(project)

    retention_service.purge_expired(now=NOW)

    assert _exists(SessionRecord, kept_session)
    assert _exists(OCRReviewRecord, kept_review)


def test_zadacha_uhodit_vmeste_s_failom(made) -> None:
    job = _id()
    upload = JOB_UPLOADS_DIR / f"{job}-statement.pdf"
    upload.write_bytes(b"%PDF")
    orphan = JOB_UPLOADS_DIR / f"{_id()}-orphan.pdf"
    orphan.write_bytes(b"%PDF")
    old_stamp = OLD.timestamp()
    os.utime(orphan, (old_stamp, old_stamp))
    made["files"] += [str(upload), str(orphan)]
    with db_session() as session:
        session.add(JobRecord(
            job_id=job, job_type="preview_transform", status="completed",
            created_at=OLD, updated_at=OLD, payload={"upload_path": str(upload)},
        ))
    made["jobs"].append(job)

    result = retention_service.purge_expired(now=NOW)

    assert not _exists(JobRecord, job)
    assert not upload.exists(), "файл задачи остался на диске"
    assert not orphan.exists(), "осиротевший файл старше срока остался на диске"
    assert result.jobs >= 1 and result.files >= 2


def test_srok_beretsya_iz_nastroek(made, monkeypatch) -> None:
    """ANALYZER_RETENTION_DAYS=10 — выписке восьми дней ещё жить."""
    monkeypatch.setattr(retention_service.settings, "analyzer_retention_days", 10.0)
    key = _session(made, OLD)
    retention_service.purge_expired(now=NOW)
    assert _exists(SessionRecord, key)


def test_skan_v_pamyati_zhivet_ne_dolshe_vypiski() -> None:
    scan_id = "scan" + uuid.uuid4().hex
    scanned._store_scan(scan_id, {"document": object(), "filename": "x.png"})
    assert scanned._get_scan(scan_id) is not None

    # Состарим запись на срок хранения и ещё секунду.
    scanned._scan_store[scan_id]["stored_at"] -= scanned._scan_ttl_seconds() + 1
    assert scanned.purge_expired_scans() >= 1
    assert scanned._get_scan(scan_id) is None
