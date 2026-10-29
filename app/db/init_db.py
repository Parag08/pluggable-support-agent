"""Create (and optionally reset / seed) the database.

    python -m app.db.init_db                 # create missing tables
    python -m app.db.init_db --seed          # create tables + load seed data
    python -m app.db.init_db --reset --seed  # drop everything, recreate, seed
"""

import argparse
from dataclasses import asdict

from sqlalchemy import Engine, func, select

from app.config import get_settings
from app.db import models
from app.db.base import Base, get_engine, get_sessionmaker
from app.db.seed import seed


def create_schema(engine: Engine, reset: bool = False) -> None:
    if reset:
        if engine.dialect.name == "sqlite":
            # Telemetry tables reference each other (turns ↔ messages ↔ parked_intents);
            # SQLite can't drop such a cycle with FK enforcement on.
            with engine.connect() as conn:
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
                Base.metadata.drop_all(conn)
                conn.commit()
                conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        else:
            Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="drop all tables first (destroys data)")
    parser.add_argument("--seed", action="store_true", help="load seed data")
    args = parser.parse_args()

    settings = get_settings()
    engine = get_engine()
    print(f"Database: {engine.url.render_as_string(hide_password=True)}")

    create_schema(engine, reset=args.reset)
    print(f"Schema ready ({len(Base.metadata.tables)} tables){' — reset' if args.reset else ''}")

    if args.seed:
        with get_sessionmaker(engine)() as db:
            if db.scalar(select(func.count()).select_from(models.Product)):
                raise SystemExit("Database already has data. Re-run with --reset --seed to start over.")
            summary = seed(db, settings)
        width = max(len(k) for k in asdict(summary))
        for key, value in asdict(summary).items():
            print(f"  {key:<{width}}  {value}")


if __name__ == "__main__":
    main()
