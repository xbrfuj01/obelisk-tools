import os

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.schema import CreateColumn

from . import config

DB_PATH = os.path.join(config.DATA_DIR, "app.db")

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    """WAL lets readers (status polling) proceed without blocking on writers
    (progress-hook/ffmpeg commits, which happen often) instead of the default
    rollback journal's exclusive lock during a write. NORMAL sync still fsyncs
    at WAL checkpoints, just not on every single commit - the standard
    pairing for an app that commits this often on plain disk I/O.

    WAL only fixes reader/writer blocking, not two writers landing at the
    same moment - SQLite still serializes those, and without busy_timeout
    the second one fails immediately ("database is locked") instead of
    just waiting the few milliseconds it'd take for the first to finish -
    a real risk here given how often progress-hook commits fire during an
    active download/conversion. 5s is generous - actual contention
    resolves in well under that."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def _auto_migrate():
    """Add columns/indexes that exist in the models but not yet in an older DB file on disk."""
    inspector = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
        missing = [c for c in table.columns if c.name not in existing_cols]
        if missing:
            with engine.begin() as conn:
                for column in missing:
                    ddl = str(CreateColumn(column).compile(dialect=engine.dialect))
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {ddl}"))
        # SQLite's ADD COLUMN has no SQL-level DEFAULT unless a
        # server_default was set on the model - a plain Python-side
        # default=False (or similar) only applies to rows the ORM inserts
        # from now on, leaving every row that already existed NULL instead
        # of the intended value. Run for every column with a simple scalar
        # (non-callable - skips e.g. default=datetime.utcnow, a per-row
        # value with nothing sensible to backfill in bulk) default on every
        # startup, not just ones just added this run: a column ADD-ed in an
        # earlier deploy, before this backfill existed, would otherwise be
        # stuck NULL forever.
        with engine.begin() as conn:
            for column in table.columns:
                default = column.default
                if default is not None and getattr(default, "is_scalar", False):
                    conn.execute(
                        text(f"UPDATE {table.name} SET {column.name} = :val WHERE {column.name} IS NULL"),
                        {"val": default.arg},
                    )
        for index in table.indexes:
            index.create(bind=engine, checkfirst=True)


def init_db():
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _auto_migrate()
