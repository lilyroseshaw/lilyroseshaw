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
    from app import models  # noqa: F401  (register models on Base.metadata)

    Base.metadata.create_all(bind=engine)


def get_session() -> Session:
    return SessionLocal()
