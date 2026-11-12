"""
Database connection setup.

Default: SQLite file (inventory.db) - zero setup, perfect for a single-PC /
single-warehouse deployment. To move to PostgreSQL for multi-user / networked
access later, just set an environment variable before starting the server:

    export DATABASE_URL="postgresql://user:password@localhost:5432/inventory"

No code changes needed - SQLAlchemy handles both identically.
"""
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./inventory.db")

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args=connect_args)
else:
    # Bound each web process to a modest number of DB connections. Worker
    # count multiplied by this pool should stay within the PostgreSQL plan.
    engine = create_engine(
        DATABASE_URL,
        pool_size=max(1, int(os.getenv("DB_POOL_SIZE", "5"))),
        max_overflow=max(0, int(os.getenv("DB_MAX_OVERFLOW", "5"))),
        pool_timeout=max(1, int(os.getenv("DB_POOL_TIMEOUT", "30"))),
        pool_recycle=max(60, int(os.getenv("DB_POOL_RECYCLE", "1800"))),
        pool_pre_ping=True,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI dependency - one DB session per request, always closed after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
