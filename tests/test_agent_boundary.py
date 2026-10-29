"""The agent may only use ports — never a database, a vendor SDK, or an adapter.

These tests fail if agent code imports database/vendor machinery or an adapter, reaches into an
adapter's internals, or if a port grows a new function without this allowlist being updated
(i.e. without a reviewed decision).
"""

import ast
from enum import Enum
from pathlib import Path

import pytest

import app.db.models as models
from app.adapters.commerce.http_json import HttpJsonCommerce
from app.adapters.commerce.sql_reference import SqlReferenceCommerce
from app.adapters.conversation.sql_store import SqlConversationStore
from app.ports.classifier import Classifier
from app.ports.commerce import CommercePort
from app.ports.conversation import ConversationStore
from app.ports.handoff import HandoffPort
from app.ports.identity import IdentityPort

AGENT_DIR = Path(__file__).resolve().parent.parent / "app" / "agent"

ALLOWED_COMMERCE_FUNCTIONS = {
    "capabilities",
    # catalog (read)
    "list_products", "get_product", "find_variant", "convert_to_us_size", "list_stores", "get_discount_code",
    # customer data (read, scoped to customer_id)
    "get_customer_profile", "get_customer_order", "list_customer_orders", "count_item_exchanges",
    # customer writes (after confirmation, idempotent)
    "create_return", "create_exchange", "cancel_order",
}
ALLOWED_STORE_FUNCTIONS = {
    "create_session", "get_session", "save_session_state", "mark_session_escalated", "expire_identity",
    "log_message", "recent_messages", "park_intent", "take_parked_intent", "create_escalation",
    "set_escalation_ticket", "start_turn", "commit",
}

FORBIDDEN_MODULES = (
    "sqlalchemy", "psycopg", "sqlite3", "httpx", "jwt", "smtplib", "openai", "anthropic",
    "app.db.base", "app.db.seed", "app.db.init_db", "app.telemetry", "app.auth", "app.adapters", "app.wiring",
)
FORBIDDEN_NAME_FRAGMENTS = ("__db", "_SqlReferenceCommerce", "_SqlConversationStore", "_db")


def public(obj) -> set[str]:
    return {name for name in dir(obj) if not name.startswith("_")}


def agent_sources():
    return sorted(AGENT_DIR.glob("*.py"))


@pytest.mark.parametrize("path", agent_sources(), ids=lambda p: p.name)
def test_agent_module_touches_no_database_vendor_or_adapter(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(FORBIDDEN_MODULES), f"{path.name} imports {alias.name}"
                assert alias.name != "app.db.models", f"{path.name} imports the ORM module"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith(FORBIDDEN_MODULES), f"{path.name} imports from {mod}"
            if mod in ("app.db.models", "app.db.models.commerce", "app.db.models.telemetry"):
                for alias in node.names:
                    obj = getattr(models, alias.name)
                    assert isinstance(obj, type) and issubclass(obj, Enum), (
                        f"{path.name} imports {alias.name} from {mod}; only enums are allowed"
                    )
        elif isinstance(node, ast.Attribute):
            assert not any(f in node.attr for f in FORBIDDEN_NAME_FRAGMENTS), (
                f"{path.name} accesses private attribute .{node.attr}"
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "Commerce__" not in node.value and "Store__" not in node.value, (
                f"{path.name} references adapter internals"
            )


def test_ports_expose_exactly_the_reviewed_functions():
    assert public(CommercePort) == ALLOWED_COMMERCE_FUNCTIONS
    assert public(ConversationStore) == ALLOWED_STORE_FUNCTIONS
    assert public(Classifier) == {"classify"}
    assert public(IdentityPort) == {"verify", "sign_in_url"}
    assert public(HandoffPort) == {"create_ticket"}


@pytest.mark.parametrize("adapter, port", [
    (SqlReferenceCommerce, CommercePort),
    (HttpJsonCommerce, CommercePort),
    (SqlConversationStore, ConversationStore),
])
def test_adapters_offer_exactly_their_port_and_nothing_more(adapter, port):
    assert public(adapter) == public(port)


def test_ports_have_no_delete_refund_discount_or_identity_grant():
    for port in (CommercePort, ConversationStore, Classifier, IdentityPort, HandoffPort):
        for name in public(port):
            assert not any(w in name for w in ("delete", "refund", "discount_create", "grant", "validate_identity"))
