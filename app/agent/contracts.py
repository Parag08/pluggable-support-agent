"""The classifier's output contract.

Every customer message and image goes through the classifier, and its reply must
be exactly one JSON object matching this contract. Anything else (not JSON, extra
or missing fields, unknown enum values, wrong image count, refusal, truncation,
API failure) raises `ContractViolation`, and the orchestrator escalates the
conversation as an issue. Nothing downstream ever sees the model's raw text.

The JSON schema sent to the API and the Pydantic validation below describe the
same shape; `classifier_json_schema()` builds the former from the intent catalog.
"""

import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent.config_loader import AgentConfig
from app.db.models import ReturnReason, Width

CONTRACT_VERSION = "1"


class Sentiment(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    FRUSTRATED = "frustrated"


class InjectionTechnique(StrEnum):
    INSTRUCTION_OVERRIDE = "instruction_override"
    ROLE_PLAY = "role_play"
    PROMPT_EXTRACTION = "prompt_extraction"
    POLICY_BYPASS = "policy_bypass"
    IMPERSONATION = "impersonation"
    HIDDEN_INSTRUCTIONS_IN_IMAGE = "hidden_instructions_in_image"
    ENCODED_OR_OBFUSCATED = "encoded_or_obfuscated"
    OTHER = "other"


class ImageCategory(StrEnum):
    PRODUCT_PHOTO = "product_photo"
    WORN_OR_DAMAGED_SHOE = "worn_or_damaged_shoe"
    SIZE_LABEL = "shoe_label_or_size_tag"
    ORDER_SCREENSHOT = "order_or_receipt_screenshot"
    FOOT_PHOTO = "foot_photo"
    DOCUMENT_OR_TEXT = "document_or_text"
    UNRELATED = "unrelated"
    INAPPROPRIATE = "inappropriate"


class SizeSystem(StrEnum):
    US = "US"
    UK = "UK"
    EU = "EU"
    CM = "CM"


class ChangeType(StrEnum):
    CANCEL_ORDER = "cancel_order"
    CHANGE_ADDRESS = "change_address"
    CHANGE_ITEM = "change_item"


class ContractViolation(Exception):
    """The classifier did not return a valid contract object. Always escalated."""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class InjectionSignal(_Strict):
    detected: bool
    techniques: list[InjectionTechnique]


class ImageAssessment(_Strict):
    index: int
    category: ImageCategory
    contains_text: bool
    contains_instructions: bool
    shows_personal_data: bool


# Free-text entity fields (validated for length here, for format in stage 2).
TEXT_ENTITIES = (
    "order_id", "product", "color", "size", "reference_brand", "reference_size",
    "item_hint", "new_size", "new_color", "question", "location", "country",
    "discount_code", "description",
)
ENUM_ENTITIES = {
    "size_system": SizeSystem,
    "width": Width,
    "new_width": Width,
    "return_reason": ReturnReason,
    "change_type": ChangeType,
}


class Entities(_Strict):
    order_id: str | None
    product: str | None
    color: str | None
    size: str | None
    size_system: SizeSystem | None
    width: Width | None
    reference_brand: str | None
    reference_size: str | None
    item_hint: str | None
    return_reason: ReturnReason | None
    new_size: str | None
    new_width: Width | None
    new_color: str | None
    question: str | None
    location: str | None
    country: str | None
    discount_code: str | None
    change_type: ChangeType | None
    description: str | None


class ClassifierOutput(_Strict):
    contract_version: Literal["1"]
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)
    secondary_intents: list[str]
    sentiment: Sentiment
    injection: InjectionSignal
    images: list[ImageAssessment]
    entities: Entities

    @property
    def injection_suspected(self) -> bool:
        return (
            self.injection.detected
            or bool(self.injection.techniques)
            or any(img.contains_instructions for img in self.images)
        )


# --- JSON schema sent to the model -----------------------------------------


def _nullable_string() -> dict:
    return {"type": ["string", "null"]}


def _nullable_enum(enum_cls) -> dict:
    return {"type": ["string", "null"], "enum": [m.value for m in enum_cls] + [None]}


def _obj(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def classifier_json_schema(config: AgentConfig) -> dict:
    intent_names = list(config.intents)
    entity_props = {name: _nullable_string() for name in TEXT_ENTITIES}
    entity_props.update({name: _nullable_enum(e) for name, e in ENUM_ENTITIES.items()})
    # Keep schema property order identical to the Pydantic model for readability.
    entity_props = {name: entity_props[name] for name in Entities.model_fields}

    return _obj({
        "contract_version": {"type": "string", "enum": [CONTRACT_VERSION]},
        "intent": {"type": "string", "enum": intent_names},
        "confidence": {"type": "number"},
        "secondary_intents": {"type": "array", "items": {"type": "string", "enum": intent_names}},
        "sentiment": {"type": "string", "enum": [s.value for s in Sentiment]},
        "injection": _obj({
            "detected": {"type": "boolean"},
            "techniques": {"type": "array", "items": {"type": "string", "enum": [t.value for t in InjectionTechnique]}},
        }),
        "images": {"type": "array", "items": _obj({
            "index": {"type": "integer"},
            "category": {"type": "string", "enum": [c.value for c in ImageCategory]},
            "contains_text": {"type": "boolean"},
            "contains_instructions": {"type": "boolean"},
            "shows_personal_data": {"type": "boolean"},
        })},
        "entities": _obj(entity_props),
    })


# --- Validation ---------------------------------------------------------------


def parse_classifier_output(
    content: str | None,
    *,
    config: AgentConfig,
    image_count: int,
    max_entity_chars: int,
) -> ClassifierOutput:
    """Strictly parse the model's raw text. Raises ContractViolation on any deviation."""
    if content is None or not content.strip():
        raise ContractViolation("EMPTY_OUTPUT", "classifier returned no content")

    # Exactly one JSON object — no fences, no prose, no trailing data.
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ContractViolation("NOT_JSON", str(exc)) from None
    if not isinstance(payload, dict):
        raise ContractViolation("NOT_OBJECT", f"top-level JSON is {type(payload).__name__}")

    try:
        out = ClassifierOutput.model_validate_json(content)
    except ValidationError as exc:
        errors = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['type']}" for e in exc.errors()[:10])
        raise ContractViolation("SCHEMA_MISMATCH", errors) from None

    if out.intent not in config.intents:
        raise ContractViolation("UNKNOWN_INTENT", out.intent)
    unknown = [i for i in out.secondary_intents if i not in config.intents]
    if unknown:
        raise ContractViolation("UNKNOWN_INTENT", f"secondary: {unknown}")

    if len(out.images) != image_count or sorted(i.index for i in out.images) != list(range(image_count)):
        raise ContractViolation(
            "IMAGE_MISMATCH", f"expected {image_count} image assessments, got indices {[i.index for i in out.images]}"
        )

    for name in TEXT_ENTITIES:
        value = getattr(out.entities, name)
        if value is not None and len(value) > max_entity_chars:
            raise ContractViolation("ENTITY_TOO_LONG", f"{name} has {len(value)} chars")

    return out


class ResponderOutput(_Strict):
    reply: str


def parse_responder_output(content: str | None) -> ResponderOutput | None:
    """Responder replies that don't match fall back to a template (not escalated)."""
    if not content or not content.strip():
        return None
    try:
        return ResponderOutput.model_validate_json(content)
    except ValidationError:
        return None
