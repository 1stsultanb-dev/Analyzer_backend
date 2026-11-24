"""Схема `visits` — учёт посещений сайта.

Отдельная схема по той же причине, что у `webexcel` и `finance`: удалить модуль
можно одним `DROP SCHEMA visits CASCADE`, не задев ни одной чужой таблицы.

На Postgres схему и таблицы заводит ревизия 0024. `create_all` ниже — для
SQLite, у которого схем нет и под который ревизии не написаны; на Postgres он
приходит к готовым таблицам и ничего не меняет. Имена таблиц с префиксом
`site_`, потому что на SQLite все схемы сливаются в одну, а `sessions` там уже
занята анализатором.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import MetaData, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.database import get_engine

VISITS_SCHEMA = "visits"


class VisitsBase(DeclarativeBase):
    metadata = MetaData(schema=VISITS_SCHEMA)


_session_factory: sessionmaker[Session] | None = None
_initialized = False


def visits_engine():
    engine = get_engine()
    if engine.dialect.name == "sqlite":
        return engine.execution_options(schema_translate_map={VISITS_SCHEMA: None})
    return engine


def init_visits_database() -> None:
    global _initialized
    if _initialized:
        return
    import app.visits.models  # noqa: F401 — регистрирует таблицы в метаданных

    engine = get_engine()
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{VISITS_SCHEMA}"'))
    VisitsBase.metadata.create_all(bind=visits_engine())
    _initialized = True


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=visits_engine(), autoflush=False, autocommit=False, future=True
        )
    init_visits_database()
    return _session_factory


@contextmanager
def visits_session() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = ["VISITS_SCHEMA", "VisitsBase", "init_visits_database", "visits_engine", "visits_session"]
