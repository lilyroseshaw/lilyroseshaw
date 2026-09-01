from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app import config

Path(config.DATABASE_PATH).resolve().parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{config.DATABASE_PATH}",
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    from app import migrations, models  # noqa: F401  (register models on Base.metadata)
    from app.deletion_seeds import seed_known_recipes

    # create_all() only creates *missing tables* (e.g. deletion_recipes/
    # deletion_events on an upgrade) - it never alters an existing table, so
    # it's always safe to run before migrate(), which handles altering the
    # existing `companies` table and needs deletion_recipes to already exist
    # for its backfill step.
    Base.metadata.create_all(bind=engine)
    migrations.migrate(engine, config.DATABASE_PATH)

    session = SessionLocal()
    try:
        seed_known_recipes(session)  # idempotent - never overwrites a researched/manual recipe
    finally:
        session.close()


def get_session() -> Session:
    return SessionLocal()
