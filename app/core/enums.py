"""Domain enums. Plain Python: no database, no ORM."""

from enum import StrEnum


class Gender(StrEnum):
    MEN = "men"
    WOMEN = "women"
    UNISEX = "unisex"


class Width(StrEnum):
    NARROW = "narrow"
    REGULAR = "regular"
    WIDE = "wide"


class FitNote(StrEnum):
    RUNS_SMALL = "runs_small"
    TRUE_TO_SIZE = "true_to_size"
    RUNS_LARGE = "runs_large"


class OrderStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FULFILLING = "fulfilling"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"


class OrderItemStatus(StrEnum):
    ACTIVE = "active"
    RETURN_REQUESTED = "return_requested"
    RETURNED = "returned"
    EXCHANGE_REQUESTED = "exchange_requested"
    EXCHANGED = "exchanged"
    CANCELLED = "cancelled"


class ReturnType(StrEnum):
    RETURN = "return"
    EXCHANGE = "exchange"


class ReturnStatus(StrEnum):
    REQUESTED = "requested"
    LABEL_SENT = "label_sent"
    IN_TRANSIT = "in_transit"
    RECEIVED = "received"
    COMPLETED = "completed"
    REJECTED = "rejected"


class ReturnReason(StrEnum):
    TOO_SMALL = "too_small"
    TOO_LARGE = "too_large"
    TOO_NARROW = "too_narrow"
    TOO_WIDE = "too_wide"
    UNCOMFORTABLE = "uncomfortable"
    DEFECTIVE = "defective"
    NOT_AS_DESCRIBED = "not_as_described"
    CHANGED_MIND = "changed_mind"
    OTHER = "other"


class RefundStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    PROCESSING = "processing"
    REFUNDED = "refunded"
    FAILED = "failed"


class ActorType(StrEnum):
    """Who performed an action — lets us measure what the agent did vs. humans."""

    AGENT = "agent"
    HUMAN = "human"
    CUSTOMER_PORTAL = "customer_portal"
    SYSTEM = "system"


class IdentityStatus(StrEnum):
    ANONYMOUS = "anonymous"
    PENDING = "pending"  # redirected to identity server, waiting for callback
    VALIDATED = "validated"
    FAILED = "failed"
    EXPIRED = "expired"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    EVENT = "event"  # system events shown in the transcript (e.g. "signed in", "handed to agent")


class IntentType(StrEnum):
    PUBLIC = "public"
    PROTECTED = "protected"


class PipelineStage(StrEnum):
    PREPROCESS = "preprocess"
    CLASSIFY = "classify"
    VALIDATE = "validate"
    POLICY = "policy"
    EXECUTE = "execute"
    RESPOND = "respond"
    OUTPUT_CHECK = "output_check"


class StepStatus(StrEnum):
    OK = "ok"
    SHORT_CIRCUIT = "short_circuit"  # stage ended the pipeline early (clarify, deny, auth_required…)
    SKIPPED = "skipped"
    ERROR = "error"


class PolicyOutcome(StrEnum):
    ALLOW = "allow"
    AUTH_REQUIRED = "auth_required"
    DENY = "deny"
    CONFIRM = "confirm"
    ESCALATE = "escalate"


class TurnOutcome(StrEnum):
    """How the turn ended — the main field for funnel / failure analysis."""

    ANSWERED = "answered"
    CLARIFY = "clarify"  # low confidence or ambiguous intent
    SLOT_REQUEST = "slot_request"  # asked the customer for a missing field
    AUTH_REQUIRED = "auth_required"
    DENIED = "denied"
    CONFIRM_PENDING = "confirm_pending"
    ESCALATED = "escalated"
    DECLINED = "declined"  # out of scope
    FALLBACK = "fallback"  # output check failed → templated reply
    ERROR = "error"


class LLMPurpose(StrEnum):
    CLASSIFY = "classify"
    RESPOND = "respond"
    SUMMARIZE = "summarize"
    OUTPUT_CHECK = "output_check"


class ParkedIntentStatus(StrEnum):
    PARKED = "parked"
    RESUMED = "resumed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ParkReason(StrEnum):
    AUTH_REQUIRED = "auth_required"
    QUEUED_SECONDARY = "queued_secondary"  # 2nd intent in a multi-intent message


class AuthEventType(StrEnum):
    REDIRECT_ISSUED = "redirect_issued"
    CALLBACK_RECEIVED = "callback_received"
    TOKEN_VALIDATED = "token_validated"
    VALIDATION_FAILED = "validation_failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    EXPIRED = "expired"
    SIGNED_OUT = "signed_out"


class EscalationReason(StrEnum):
    CUSTOMER_REQUEST = "customer_request"
    FRUSTRATION = "frustration"
    POLICY_EXCEPTION = "policy_exception"
    WARRANTY = "warranty"
    SAFETY_LEGAL = "safety_legal"
    REPEATED_FAILURE = "repeated_failure"
    HANDLER_ERROR = "handler_error"
    AUTH_FAILURE = "auth_failure"
    PROMPT_INJECTION = "prompt_injection"
    CLASSIFIER_CONTRACT_VIOLATION = "classifier_contract_violation"


class EscalationPriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class EscalationStatus(StrEnum):
    OPEN = "open"
    PICKED_UP = "picked_up"
    RESOLVED = "resolved"


class FeedbackRating(StrEnum):
    UP = "up"
    DOWN = "down"


class EvalTarget(StrEnum):
    MESSAGE = "message"
    TURN = "turn"
    SESSION = "session"


class EvalType(StrEnum):
    RESPONSE_QUALITY = "response_quality"
    HALLUCINATION = "hallucination"
    GUARDRAIL = "guardrail"
    ESCALATION_QUALITY = "escalation_quality"
    RED_TEAM = "red_team"
    SIMULATION = "simulation"
