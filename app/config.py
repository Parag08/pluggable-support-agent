"""Application settings.

Every parameter is read from the environment / `.env`. Fields have no
defaults on purpose: a missing key fails fast at startup instead of silently
falling back to a value buried in code. See `.env.example` for the full list.
"""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Database -----------------------------------------------------------
    database_url: str
    db_echo: bool

    # --- Store / policy -----------------------------------------------------
    store_currency: str
    free_shipping_threshold: Decimal
    standard_shipping_fee: Decimal
    return_window_days: int

    # --- Seed data ----------------------------------------------------------
    seed_data_dir: Path
    seed_random_seed: int
    seed_num_customers: int
    seed_guest_customer_ratio: float
    seed_num_orders: int
    seed_order_history_days: int
    seed_return_rate: float
    seed_out_of_stock_rate: float
    seed_restock_probability: float
    seed_discount_usage_rate: float
    seed_cancellation_rate: float

    # --- Telemetry ----------------------------------------------------------
    # Store full (redacted) LLM request/response bodies in llm_calls.
    telemetry_store_llm_payloads: bool

    # --- Inference (pluggable provider, see app/adapters/llm) ----------------
    # openai_compatible | anthropic | <python.module>:<ClassName>
    llm_provider: str
    llm_api_key: SecretStr
    # Empty = the provider's default endpoint.
    llm_base_url: str
    llm_timeout_seconds: float
    llm_max_retries: int
    # json_schema | json_object | prompt — how JSON output is requested from the API.
    # The contract is validated in code regardless of this setting.
    llm_json_mode: Literal["json_schema", "json_object", "prompt"]
    # Reasoning effort for providers that support it (anthropic). Empty = model default.
    llm_effort: Literal["", "low", "medium", "high", "xhigh", "max"]

    classifier_model: str
    # Vision-capable model used when a message has images. Empty = images are rejected.
    classifier_vision_model: str
    classifier_max_tokens: int
    classifier_temperature: float
    classifier_min_confidence: float
    classifier_min_confidence_write: float
    classifier_history_messages: int
    classifier_max_entity_chars: int

    responder_model: str
    responder_max_tokens: int
    responder_temperature: float
    responder_max_reply_chars: int

    # --- Ports and adapters (see app/wiring.py) -----------------------------
    # Each *_PROVIDER is a built-in name or '<python.module>:<ClassName>' for your own adapter.
    # CommercePort: sql_reference | http_json
    commerce_provider: str
    commerce_http_base_url: str
    commerce_http_token: SecretStr
    commerce_http_timeout_seconds: float
    # ClassifierPort: llm_oneshot
    classifier_provider: str
    # IdentityPort: signed_jwt | dev (development only)
    identity_provider: str
    identity_max_session_minutes: int
    identity_sign_in_url: str  # where to send a signed-out customer; {session_id} is filled in
    identity_jwt_algorithm: str
    identity_jwt_secret: SecretStr
    identity_jwt_jwks_url: str
    identity_jwt_issuer: str
    identity_jwt_audience: str
    identity_jwt_customer_claim: str
    identity_jwt_max_age_seconds: int
    # HandoffPort: local | email
    handoff_provider: str
    handoff_transcript_messages: int
    handoff_email_to: str
    handoff_email_from: str
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: SecretStr
    smtp_starttls: bool
    # Extra attempts for a write that fails in transit. Safe: every write has an idempotency key.
    write_retries: int

    # --- Agent behaviour ----------------------------------------------------
    # The company name used in prompts and reply templates ({brand_name}).
    brand_name: str
    agent_config_dir: Path
    app_version: str
    prompt_version: str
    policy_config_version: str
    max_message_chars: int
    max_images_per_message: int
    max_image_bytes: int
    allowed_image_types: str  # comma-separated MIME types
    max_slot_attempts: int
    frustration_escalate_after: int
    max_exchanges_per_item: int
    order_id_pattern: str
    discount_code_pattern: str
    currency_symbol: str
    order_picker_limit: int
    pending_action_ttl_minutes: int
    identity_idle_timeout_minutes: int
    return_id_prefix: str
    return_label_url_template: str

    # --- Evals --------------------------------------------------------------
    eval_trials: int
    eval_workers: int

    @property
    def allowed_image_type_set(self) -> frozenset[str]:
        return frozenset(t.strip() for t in self.allowed_image_types.split(",") if t.strip())

    @field_validator("seed_data_dir", "agent_config_dir")
    @classmethod
    def _resolve_relative_to_root(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v).resolve()

    @field_validator("database_url")
    @classmethod
    def _resolve_sqlite_path(cls, v: str) -> str:
        # Relative SQLite paths are resolved against the project root, not the CWD.
        prefix = "sqlite:///"
        if v.startswith(prefix) and not v.startswith(prefix + "/") and v != prefix + ":memory:":
            return prefix + str((PROJECT_ROOT / v[len(prefix):]).resolve())
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
