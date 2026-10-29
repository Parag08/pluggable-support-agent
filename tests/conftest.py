import pytest

from app.config import get_settings
from app.db.base import get_sessionmaker, make_engine
from app.db.init_db import create_schema
from app.db.seed import seed


@pytest.fixture
def settings(tmp_path):
    return get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path / 'test.db'}"})


@pytest.fixture
def db(settings):
    engine = make_engine(settings.database_url, echo=False)
    create_schema(engine)
    with get_sessionmaker(engine)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def seeded_db(db, settings):
    seed(db, settings)
    return db
