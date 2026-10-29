"""Import every model so `Base.metadata` knows about all tables."""

from app.db.models.commerce import *  # noqa: F401,F403
from app.db.models.telemetry import *  # noqa: F401,F403
