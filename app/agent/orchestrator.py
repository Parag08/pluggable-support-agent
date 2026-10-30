"""The support agent pipeline (PRD §10.2).

    message → 0 preprocess → 1 classify (strict contract) → 1b injection guard → 2 validate fields
            → 3 policy/authorization → 4 execute → 5 respond → 6 output check

Every turn is traced (via the gateway's turn recorder): each stage's input, output, params,
status and latency, every LLM call, and a denormalised summary on `turns`.
Customer text and images only ever reach the classifier; everything after it
works from the validated contract object.

The agent has no database or vendor access. Everything outside it goes through ports
(app/ports/): CommercePort for the company's data, ConversationStore for its own memory,
ClassifierPort, HandoffPort and IdentityPort. The adapters are built in app/wiring.py.
"""

import copy
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.agent.config_loader import AgentConfig, IntentSpec, get_agent_config
from app.agent.contracts import Entities, ImageCategory, Sentiment
from app.agent.handlers import Handlers
from app.agent.identity import identity_is_valid
from app.agent.injection_guard import InjectionGuard
from app.agent.llm import LLMClient, compute_cost
from app.agent.output_check import check_reply
from app.agent.policy import PolicyDecision, PolicyEngine
from app.agent.preprocess import ImageInput, preprocess
from app.agent.responder import Responder
from app.agent.slots import Validation, validate
from app.config import Settings, get_settings
from app.core.types import DataAccessError, MessageRef, SessionInfo
from app.db.models import (
    EscalationPriority,
    EscalationReason,
    LLMPurpose,
    MessageRole,
    PipelineStage,
    PolicyOutcome,
    StepStatus,
    TurnOutcome,
)
from app.ports.classifier import Classifier
from app.ports.commerce import CommercePort
from app.ports.conversation import ConversationStore
from app.ports.handoff import HandoffPort, HandoffTicket
from app.ports.identity import IdentityPort


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AgentReply:
    text: str
    outcome: TurnOutcome
    turn_id: str
    payload: dict[str, Any] | None = None


@dataclass
class _Turn:
    session: SessionInfo
    rec: Any  # trace handle from store.start_turn(): turn_id, step(), llm_call(), finish()
    now: datetime
    identity_ok: bool
    state: dict[str, Any]
    info: dict[str, Any] = field(default_factory=dict)  # summary fields for the `turns` row
    # This turn's classification was a write intent below CLASSIFIER_MIN_CONFIDENCE_WRITE: confirm the
    # intent before policy can produce a confirmation card (asking for a missing field is still fine).
    write_intent_unsure: bool = False


class SupportAgent:
    def __init__(
        self,
        commerce: CommercePort,
        store: ConversationStore,
        *,
        llm: LLMClient,
        classifier: Classifier,
        handoff: HandoffPort | None = None,
        identity: IdentityPort | None = None,
        settings: Settings | None = None,
        config: AgentConfig | None = None,
        now_fn: Callable[[], datetime] = utcnow,
    ):
        self.commerce = commerce
        self.store = store
        self.llm = llm
        self.classifier = classifier
        self.handoff = handoff
        self.identity = identity
        self.s = settings or get_settings()
        self.config = config or get_agent_config()
        self.guard = InjectionGuard(self.config.injection_guard)
        self.now_fn = now_fn
        self.responder = Responder(self.llm, self.s, self.config)
        self.policy = PolicyEngine(commerce, self.s)
        self.handlers = Handlers(commerce, self.s, self.config.policies)

    # ======================================================================
    # Public API
    # ======================================================================

    def start_session(self, channel: str, **meta: Any) -> SessionInfo:
        session = self.store.create_session(channel, app_version=self.s.app_version, prompt_version=self.s.prompt_version,
                                         policy_config_version=self.s.policy_config_version, **meta)
        self.store.commit()
        return session

    def handle_message(self, session_id: str, text: str, images: list[ImageInput] | None = None) -> AgentReply:
        now = self.now_fn()
        session = self._session(session_id)
        identity_ok = identity_is_valid(self.store, session, now, self.s)  # before activity is bumped
        pre = preprocess(text, images or [], self.s, self.config.redaction)
        user_msg = self.store.log_message(
            session.id, MessageRole.USER, pre.text,
            payload={"images": pre.image_meta()} if pre.images else None,
            redactions=pre.redactions,
        )
        t = _Turn(session, self.store.start_turn(session.id, user_msg.id), now, identity_ok,
                  copy.deepcopy(session.state or {}))
        try:
            reply = self._handle(t, pre, user_msg)
        except Exception as exc:  # never leave the customer without a reply
            reply = self._escalate(t, EscalationReason.HANDLER_ERROR, EscalationPriority.HIGH, "generic_error",
                                   reason_code="UNHANDLED_ERROR", error=f"{type(exc).__name__}: {exc}")
        self.store.commit()
        return reply

    def confirm_action(self, session_id: str, action_id: str, accept: bool) -> AgentReply:
        """Called when the customer clicks Confirm/Cancel on a confirmation card."""
        now = self.now_fn()
        session = self._session(session_id)
        identity_ok = identity_is_valid(self.store, session, now, self.s)
        event = self.store.log_message(session.id, MessageRole.EVENT,
                                    f"Customer {'confirmed' if accept else 'declined'} action {action_id}")
        t = _Turn(session, self.store.start_turn(session.id, event.id), now, identity_ok,
                  copy.deepcopy(session.state or {}))
        try:
            reply = self._confirm(t, action_id, accept)
        except Exception as exc:
            reply = self._escalate(t, EscalationReason.HANDLER_ERROR, EscalationPriority.HIGH, "generic_error",
                                   reason_code="UNHANDLED_ERROR", error=f"{type(exc).__name__}: {exc}")
        self.store.commit()
        return reply

    def resume_after_auth(self, session_id: str) -> AgentReply | None:
        """Called by the auth module right after identity is validated. Resumes the parked intent."""
        now = self.now_fn()
        session = self._session(session_id)
        if not identity_is_valid(self.store, session, now, self.s):
            return None
        parked = self.store.take_parked_intent(session.id, now)
        if parked is None:
            return None
        event = self.store.log_message(session.id, MessageRole.EVENT, "Customer signed in")
        t = _Turn(session, self.store.start_turn(session.id, event.id), now, True, copy.deepcopy(session.state or {}))
        t.info["resumed_parked_intent_id"] = parked.id
        spec = self.config.intents[parked.intent]
        t.info.update(intent=spec.name, intent_type=spec.type)
        t.state["active"] = {"intent": spec.name, "slots": parked.slots, "awaiting": None, "attempts": {}}
        try:
            reply = self._process(t, spec, None)
        except Exception as exc:
            reply = self._escalate(t, EscalationReason.HANDLER_ERROR, EscalationPriority.HIGH, "generic_error",
                                   reason_code="UNHANDLED_ERROR", error=f"{type(exc).__name__}: {exc}")
        self.store.commit()
        return reply

    # ======================================================================
    # Message pipeline
    # ======================================================================

    def _handle(self, t: _Turn, pre, user_msg: MessageRef) -> AgentReply:
        rec = t.rec

        # --- 0. preprocess -------------------------------------------------
        with rec.step(PipelineStage.PREPROCESS,
                      input={"chars": len(pre.text), "image_count": len(pre.images)},
                      params={"max_message_chars": self.s.max_message_chars,
                              "max_images": self.s.max_images_per_message,
                              "max_image_bytes": self.s.max_image_bytes,
                              "allowed_image_types": sorted(self.s.allowed_image_type_set)}) as st:
            st.output = {"redactions": pre.redactions, "stripped_hidden_chars": pre.stripped_hidden_chars,
                         "images": pre.image_meta(), "rejection": pre.rejection}
            if pre.rejection:
                st.status = StepStatus.SHORT_CIRCUIT
        if pre.rejection:
            return self._finish(t, TurnOutcome.DECLINED, self.config.template(pre.rejection, **pre.rejection_params))

        # --- 1. classify -----------------------------------------------------
        history = self._history(t.session.id, before_seq=user_msg.seq)
        with rec.step(PipelineStage.CLASSIFY,
                      input={"text": pre.text, "images": pre.image_meta(), "history_messages": len(history),
                             "state": {"active": t.state.get("active"), "signed_in": t.identity_ok}},
                      params={"min_confidence": self.s.classifier_min_confidence,
                              "min_confidence_write": self.s.classifier_min_confidence_write}) as st:
            run = self.classifier.classify(pre, {**t.state, "signed_in": t.identity_ok}, history)
            st.params.update(model=run.model, **run.params)
            if run.request_messages:  # non-LLM classifiers make no LLM call to log
                self._log_llm(rec, st, LLMPurpose.CLASSIFY, run, error=str(run.violation) if run.violation else None)
            if run.violation:
                st.status = StepStatus.SHORT_CIRCUIT
                st.output = {"contract_violation": {"code": run.violation.code, "detail": run.violation.detail}}
            else:
                st.output = run.output.model_dump(mode="json")

        if run.violation:
            return self._escalate(t, EscalationReason.CLASSIFIER_CONTRACT_VIOLATION, EscalationPriority.HIGH,
                                  "contract_violation", reason_code=run.violation.code, error=str(run.violation))

        cls = run.output
        spec = self.config.intents[cls.intent]
        t.info.update(intent=cls.intent, intent_type=spec.type, classifier_confidence=cls.confidence,
                      sentiment=cls.sentiment.value, entities=cls.entities.model_dump(mode="json", exclude_none=True))

        # --- 1b. injection guard: doesn't trust the classifier's own injection flag -----
        with rec.step(PipelineStage.INJECTION_GUARD,
                      input={"chars": len(pre.text), "classifier_flagged": cls.injection_suspected},
                      params={"enabled": self.guard.enabled, "block_threshold": self.guard.threshold}) as st:
            guard = self.guard.inspect(pre.text, cls)
            st.output = guard.to_log()
            if guard.blocked:
                st.status = StepStatus.SHORT_CIRCUIT
        if guard.blocked or cls.injection_suspected:
            techniques = {x.value for x in cls.injection.techniques} | set(guard.techniques)
            return self._escalate(t, EscalationReason.PROMPT_INJECTION, EscalationPriority.HIGH, "injection_detected",
                                  reason_code="PROMPT_INJECTION" if cls.injection_suspected else "PROMPT_INJECTION_GUARD",
                                  error=json.dumps({"techniques": sorted(techniques),
                                                    "guard_findings": [f.id for f in guard.findings],
                                                    "image_instructions": [i.index for i in cls.images if i.contains_instructions]}))
        if any(img.category == ImageCategory.INAPPROPRIATE for img in cls.images):
            return self._finish(t, TurnOutcome.DECLINED, self.config.template("image_inappropriate"))

        frustrated = t.state.get("frustrated_turns", 0) + 1 if cls.sentiment == Sentiment.FRUSTRATED else 0
        t.state["frustrated_turns"] = frustrated
        if frustrated >= self.s.frustration_escalate_after:
            return self._escalate(t, EscalationReason.FRUSTRATION, EscalationPriority.HIGH, "escalated",
                                  reason_code="FRUSTRATION")

        if spec.kind == "escalate":  # safety and "talk to a person" never wait on a confidence gate
            return self._process(t, spec, cls.entities)
        if cls.confidence < self.s.classifier_min_confidence:
            options = [cls.intent, *cls.secondary_intents]
            replies = [{"label": self.config.intents[i].label} for i in dict.fromkeys(options)
                       if self.config.intents[i].kind != "decline"]
            return self._finish(t, TurnOutcome.CLARIFY, self.config.template("clarify"),
                                payload={"type": "quick_replies", "quick_replies": replies})
        # The higher bar for write intents is applied in _process, right before policy: asking for a
        # missing field (e.g. the order number) is harmless even if the intent is slightly off.
        t.write_intent_unsure = (cls.intent in self.config.write_intents
                                 and cls.confidence < self.s.classifier_min_confidence_write)

        t.state["queued_intents"] = [i for i in cls.secondary_intents if i != cls.intent]
        return self._process(t, spec, cls.entities)

    def _process(self, t: _Turn, spec: IntentSpec, entities: Entities | None) -> AgentReply:
        rec = t.rec
        if spec.kind == "decline":
            return self._finish(t, TurnOutcome.DECLINED, self.config.template("out_of_scope"))

        gap = self.policy.missing_capabilities(spec)
        if gap:  # the company's adapter can't do this: hand it to a person instead of failing in front of the customer
            with rec.step(PipelineStage.POLICY, input={"intent": spec.name, "requires": list(spec.requires)},
                          params={"capabilities": sorted(self.policy.capabilities)}) as st:
                st.output = {"outcome": PolicyOutcome.ESCALATE.value, "reason_code": "CAPABILITY_UNAVAILABLE",
                             "missing": gap}
                st.status = StepStatus.SHORT_CIRCUIT
            t.info.update(policy_outcome=PolicyOutcome.ESCALATE, policy_reason_code="CAPABILITY_UNAVAILABLE")
            return self._escalate(t, EscalationReason.POLICY_EXCEPTION, spec.escalation_priority or EscalationPriority.NORMAL,
                                  "escalated", reason_code="CAPABILITY_UNAVAILABLE", error=f"missing: {', '.join(gap)}")

        active = t.state.get("active") or {}
        same = active.get("intent") == spec.name
        slots = active.get("slots", {}) if same else {}
        attempts = dict(active.get("attempts", {})) if same else {}

        # --- 2. validate ----------------------------------------------------
        with rec.step(PipelineStage.VALIDATE,
                      input={"entities": entities.model_dump(mode="json", exclude_none=True) if entities else None,
                             "prior_slots": slots},
                      params={"intent": spec.name, "required": list(spec.required),
                              "identity_valid": t.identity_ok, "order_id_pattern": self.s.order_id_pattern}) as st:
            v = validate(self.commerce, spec, slots, entities, identity_valid=t.identity_ok,
                         customer_id=t.session.customer_id, settings=self.s, config=self.config)
            st.output = v.to_log()
            ask = next(iter(v.invalid), None) or (v.missing[0] if v.missing else None)
            if ask:
                st.status = StepStatus.SHORT_CIRCUIT
        t.state["active"] = {"intent": spec.name, "slots": v.slots, "awaiting": None, "attempts": attempts}
        t.info["missing_fields"] = v.missing or None

        if ask and spec.kind != "escalate":
            # Eligibility first: if what is already known rules the request out (e.g. the order is past
            # the return window), deny now instead of asking for details that can't change the answer.
            early = self._precheck(t, spec, v)
            if early is not None:
                return self._apply_decision(t, spec, v, early)
            attempts[ask] = attempts.get(ask, 0) + 1
            if attempts[ask] > self.s.max_slot_attempts:
                return self._escalate(t, EscalationReason.REPEATED_FAILURE, EscalationPriority.NORMAL,
                                      "too_many_attempts", reason_code="MAX_SLOT_ATTEMPTS")
            t.state["active"]["awaiting"] = ask
            options = v.quick_replies.get(ask)
            return self._finish(t, TurnOutcome.SLOT_REQUEST, self.config.template(v.invalid.get(ask) or f"ask_{ask}"),
                                payload={"type": "quick_replies", "quick_replies": options} if options else None)

        if t.write_intent_unsure:
            label = spec.label.lower()
            return self._finish(t, TurnOutcome.CLARIFY, self.config.template("confirm_intent", intent_label=label),
                                payload={"type": "quick_replies",
                                         "quick_replies": [{"label": f"Yes, {label}"}, {"label": "No"}]})

        # --- 3. policy / authorization --------------------------------------
        with rec.step(PipelineStage.POLICY,
                      input={"intent": spec.name, "slots": v.slots, "identity_valid": t.identity_ok},
                      params={"policy_config_version": self.s.policy_config_version,
                              "return_window_days": self.s.return_window_days,
                              "max_exchanges_per_item": self.s.max_exchanges_per_item}) as st:
            decision = self.policy.decide(spec, v, identity_valid=t.identity_ok,
                                          customer_id=t.session.customer_id, now=t.now)
            st.output = decision.to_log()
            if decision.outcome != PolicyOutcome.ALLOW:
                st.status = StepStatus.SHORT_CIRCUIT
        t.info.update(policy_outcome=decision.outcome, policy_reason_code=decision.reason_code)

        if decision.outcome != PolicyOutcome.ALLOW:
            return self._apply_decision(t, spec, v, decision)

        # --- 4. execute -----------------------------------------------------
        with rec.step(PipelineStage.EXECUTE, input={"handler": spec.handler, "slots": v.slots}) as st:
            data = self._read_handler(spec, v.slots, t.session.customer_id, t.now)
            st.output = data
        t.info["handler"] = spec.handler

        # --- 5. respond -----------------------------------------------------
        profile = self.commerce.get_customer_profile(t.session.customer_id) if t.session.customer_id else None
        with rec.step(PipelineStage.RESPOND, input={"intent": spec.name}) as st:
            run = self.responder.write(spec.name, data, profile.first_name if profile else None)
            st.params.update(model=run.model, **run.params)
            self._log_llm(rec, st, LLMPurpose.RESPOND, run, error=run.error)
            st.output = {"reply": run.reply, "error": run.error}
            if run.error:
                st.status = StepStatus.ERROR

        # --- 6. output check ------------------------------------------------
        with rec.step(PipelineStage.OUTPUT_CHECK, input={"reply": run.reply},
                      params={"max_reply_chars": self.s.responder_max_reply_chars}) as st:
            failures = (check_reply(run.reply, data, cfg=self.config.output_check,
                                    max_chars=self.s.responder_max_reply_chars,
                                    customer_email=profile.email if profile else None)
                        if run.reply else [run.error or "NO_REPLY"])
            st.output = {"failures": failures}
            if failures:
                st.status = StepStatus.SHORT_CIRCUIT
        t.info["output_check_passed"] = not failures
        t.state["active"] = None

        if failures:
            return self._finish(t, TurnOutcome.FALLBACK, self.config.template("fallback_answered"),
                                payload=self._handoff_offer(), error=";".join(failures))
        return self._finish(t, TurnOutcome.ANSWERED, run.reply, payload=self._queued_offer(t))

    def _precheck(self, t: _Turn, spec: IntentSpec, v: Validation) -> PolicyDecision | None:
        """Policy stage run early, while fields are still missing. Returns a denial, or None to keep asking."""
        decision = self.policy.precheck_eligibility(spec, v, customer_id=t.session.customer_id, now=t.now)
        if decision is None:
            return None
        with t.rec.step(PipelineStage.POLICY,
                        input={"intent": spec.name, "slots": v.slots, "identity_valid": t.identity_ok,
                               "precheck": True},
                        params={"policy_config_version": self.s.policy_config_version,
                                "return_window_days": self.s.return_window_days}) as st:
            st.output = decision.to_log()
            st.status = StepStatus.SHORT_CIRCUIT
        t.info.update(policy_outcome=decision.outcome, policy_reason_code=decision.reason_code)
        return decision

    def _apply_decision(self, t: _Turn, spec: IntentSpec, v: Validation, d: PolicyDecision) -> AgentReply:
        if d.outcome == PolicyOutcome.AUTH_REQUIRED:
            parked_id = self.store.park_intent(t.session.id, t.rec.turn_id, spec.name, v.slots, t.now)
            payload = {"type": "sign_in", "parked_intent_id": parked_id}
            url = self.identity.sign_in_url(t.session.id) if self.identity else None
            if url:
                payload["sign_in_url"] = url
            return self._finish(t, TurnOutcome.AUTH_REQUIRED, self.config.template("auth_required"), payload=payload)

        if d.outcome == PolicyOutcome.DENY:
            payload = None
            if d.reason_code == "ORDER_NOT_FOUND":
                t.state["active"]["slots"].pop("order_id", None)
                payload = {"type": "quick_replies", "quick_replies": self._recent_orders(t.session.customer_id)}
            else:
                t.state["active"] = None
            return self._finish(t, TurnOutcome.DENIED, self.config.template(f"deny_{d.reason_code}", **d.params),
                                payload=payload)

        if d.outcome == PolicyOutcome.ESCALATE:
            priority = spec.escalation_priority or EscalationPriority.NORMAL
            template = "escalated_urgent" if priority == EscalationPriority.URGENT else "escalated"
            return self._escalate(t, d.escalation_reason, priority, template, reason_code=d.reason_code)

        # CONFIRM — nothing is written until the customer confirms.
        action_id = str(uuid.uuid4())
        t.state["pending_action"] = {
            "id": action_id, "intent": spec.name, "slots": v.slots, "params": d.params,
            "expires_at": (t.now + timedelta(minutes=self.s.pending_action_ttl_minutes)).isoformat(),
        }
        template = {"return_request": "confirm_return", "exchange_request": "confirm_exchange",
                    "cancel_or_modify_order": "confirm_cancel"}[spec.name]
        return self._finish(t, TurnOutcome.CONFIRM_PENDING, self.config.template(template, **d.params),
                            payload={"type": "confirm", "action_id": action_id, "summary": d.params})

    # ======================================================================
    # Confirmation of write actions
    # ======================================================================

    def _confirm(self, t: _Turn, action_id: str, accept: bool) -> AgentReply:
        pending = t.state.get("pending_action")
        if not pending or pending["id"] != action_id:
            return self._finish(t, TurnOutcome.DECLINED, self.config.template("action_not_found"))
        t.state["pending_action"] = None
        spec = self.config.intents[pending["intent"]]
        t.info.update(intent=spec.name, intent_type=spec.type)
        if not accept:
            t.state["active"] = None
            return self._finish(t, TurnOutcome.ANSWERED, self.config.template("action_cancelled"))
        if datetime.fromisoformat(pending["expires_at"]) <= t.now:
            return self._finish(t, TurnOutcome.DECLINED, self.config.template("action_expired"))

        action_id = pending["id"]
        # Re-check everything: the order or identity may have changed since the card was shown.
        with t.rec.step(PipelineStage.VALIDATE, input={"prior_slots": pending["slots"]},
                        params={"intent": spec.name, "identity_valid": t.identity_ok}) as st:
            v = validate(self.commerce, spec, pending["slots"], None, identity_valid=t.identity_ok,
                         customer_id=t.session.customer_id, settings=self.s, config=self.config)
            st.output = v.to_log()
        with t.rec.step(PipelineStage.POLICY, input={"intent": spec.name, "slots": v.slots},
                        params={"policy_config_version": self.s.policy_config_version}) as st:
            d = self.policy.decide(spec, v, identity_valid=t.identity_ok, customer_id=t.session.customer_id, now=t.now)
            st.output = d.to_log()
        t.info.update(policy_outcome=d.outcome, policy_reason_code=d.reason_code)
        t.state["active"] = {"intent": spec.name, "slots": v.slots, "awaiting": None, "attempts": {}}
        if d.outcome != PolicyOutcome.CONFIRM:
            return self._apply_decision(t, spec, v, d)

        with t.rec.step(PipelineStage.EXECUTE, input={"handler": spec.handler, "slots": v.slots,
                                                       "idempotency_key": action_id}) as st:
            # A company's system can't be rolled back, so the write carries an idempotency key (the
            # card's id). If the call fails after the write may have happened, retrying with the same
            # key either completes it or returns the original result: it can never write twice.
            cid, sid = t.session.customer_id, t.session.id
            if spec.name == "return_request":
                write = lambda: self.handlers.create_return(v.slots, cid, sid, t.now, action_id)  # noqa: E731
                template = "done_return"
            elif spec.name == "exchange_request":
                write = lambda: self.handlers.create_exchange(  # noqa: E731
                    v.slots, cid, sid, t.now, d.target_variant_id, d.params, action_id)
                template = "done_exchange"
            else:
                write = lambda: self.handlers.cancel_order(v.slots, cid, t.now, action_id)  # noqa: E731
                template = "done_cancel"
            try:
                data = self._write_with_retry(write, st)
            except DataAccessError:
                raise  # refused outright: nothing was written, and it is escalated by the caller
            except Exception as exc:
                st.status = StepStatus.ERROR
                # Retries exhausted: the write may or may not have happened. Say so to the human.
                return self._escalate(
                    t, EscalationReason.HANDLER_ERROR, EscalationPriority.HIGH, "generic_error",
                    reason_code="WRITE_FAILED_UNCERTAIN", error=f"{type(exc).__name__}: {exc}",
                    note=f"A {spec.name} write may or may not have been applied. Check the company's system "
                         f"for idempotency key {action_id}.")
            text = self.config.template(template, **{**d.params, **data})
            st.output = data
        t.info["handler"] = spec.handler
        t.state["active"] = None
        return self._finish(t, TurnOutcome.ANSWERED, text, payload={"type": "action_done", **data})

    # ======================================================================
    # Helpers
    # ======================================================================

    def _write_with_retry(self, write: Callable[[], dict], step) -> dict:
        """Run an idempotent write, retrying on transport/unknown errors. A refusal (DataAccessError)
        is deterministic and is never retried."""
        for attempt in range(self.s.write_retries + 1):
            try:
                return write()
            except DataAccessError:
                raise
            except Exception:
                step.params["write_attempts"] = attempt + 1
                if attempt == self.s.write_retries:
                    raise
        raise AssertionError("unreachable")

    def _session(self, session_id: str) -> SessionInfo:
        session = self.store.get_session(session_id)
        if session is None:
            raise LookupError(f"chat session {session_id} not found")
        return session

    def _read_handler(self, spec: IntentSpec, slots: dict, customer_id: str | None, now: datetime) -> dict:
        h = self.handlers
        dispatch = {
            "size_advice": lambda: h.size_advice(slots),
            "product_info": lambda: h.product_info(slots),
            "stock_check": lambda: h.stock_check(slots),
            "shipping_policy": lambda: h.shipping_policy(slots),
            "store_info": lambda: h.store_info(slots),
            "discount_code": lambda: h.discount_code(slots, now),
            "track_order": lambda: h.track_order(slots, customer_id),
            "refund_status": lambda: h.refund_status(slots, customer_id),
        }
        return dispatch[spec.handler]()

    def _history(self, session_id: str, before_seq: int) -> list[dict]:
        lines = self.store.recent_messages(session_id, before_seq, self.s.classifier_history_messages)
        return [{"role": m.role, "content": m.content} for m in lines]

    def _recent_orders(self, customer_id: str | None) -> list[dict]:
        if not customer_id:
            return []
        orders = self.commerce.list_customer_orders(customer_id, self.s.order_picker_limit)
        return [{"label": f"{o.id} · {o.placed_at:%b %d} · {o.status.value}"} for o in orders]

    def _queued_offer(self, t: _Turn) -> dict | None:
        queued = [i for i in t.state.get("queued_intents", []) if i in self.config.intents]
        t.state["queued_intents"] = []
        if not queued:
            return None
        return {"type": "quick_replies", "quick_replies": [{"label": self.config.intents[i].label} for i in queued]}

    def _handoff_offer(self) -> dict:
        return {"type": "quick_replies", "quick_replies": [{"label": self.config.intents["human_handoff"].label}]}

    def _log_llm(self, rec, step, purpose: LLMPurpose, run, error: str | None) -> None:
        # Never store image bytes or the (versioned, hashed) system prompt in every row.
        logged = []
        for m in run.request_messages:
            if m["role"] == "system":
                continue
            content = m["content"]
            if isinstance(content, list):
                content = [
                    {"type": "image_url", "image_url": "[image omitted]"} if c.get("type") == "image_url" else c
                    for c in content
                ]
            logged.append({"role": m["role"], "content": content})
        r = run.result
        rec.llm_call(
            step=step, purpose=purpose, provider=self.llm.provider, model=run.model,
            prompt_version=self.s.prompt_version, system_prompt_hash=run.system_prompt_hash,
            params=run.params, request_messages=logged,
            response_content={"content": r.content, "finish_reason": r.finish_reason, "served_by": r.model} if r else None,
            stop_reason=r.finish_reason if r else None,
            input_tokens=r.input_tokens if r else None,
            output_tokens=r.output_tokens if r else None,
            latency_ms=r.latency_ms if r else None,
            cost_usd=compute_cost(self.config.pricing, run.model, r.input_tokens, r.output_tokens) if r else None,
            error=error,
        )

    def _escalate(
        self, t: _Turn, reason: EscalationReason, priority: EscalationPriority, template: str,
        *, reason_code: str, error: str | None = None, note: str | None = None,
    ) -> AgentReply:
        active = t.state.get("active") or {}
        recent = self._history(t.session.id, before_seq=10**9)
        summary = "\n".join([
            f"Reason: {reason.value} ({reason_code})",
            *([f"Note: {note}"] if note else []),
            f"Intent: {t.info.get('intent') or active.get('intent') or 'unknown'}",
            f"Signed in: {'yes, ' + t.session.customer_id if t.identity_ok and t.session.customer_id else 'no'}",
            f"Collected details: {json.dumps(active.get('slots') or {}, default=str)}",
            "Recent messages:",
            *[f"  {m['role']}: {m['content']}" for m in recent],
        ])
        intent = t.info.get("intent") or active.get("intent")
        esc_id = self.store.create_escalation(t.session.id, t.rec.turn_id, reason, priority, intent, summary)
        self.store.mark_session_escalated(t.session.id)
        error = self._create_ticket(t, esc_id, reason, priority, intent, summary, active) or error
        t.state["active"] = None
        t.info.setdefault("policy_outcome", PolicyOutcome.ESCALATE)
        t.info["policy_reason_code"] = reason_code
        text = self.config.template(template, support_hours=self.config.policies.get("human_support_hours"))
        return self._finish(t, TurnOutcome.ESCALATED, text, payload={"type": "handoff", "escalation_id": esc_id},
                            error=error)

    def _create_ticket(self, t: _Turn, esc_id: int, reason: EscalationReason, priority: EscalationPriority,
                       intent: str | None, summary: str, active: dict) -> str | None:
        """Open a ticket in the company's helpdesk. The local escalation record already exists, so a
        helpdesk failure never loses the handoff; it is only noted on the turn."""
        if self.handoff is None:
            return None
        order_id = (active.get("slots") or {}).get("order_id")
        signed_in = t.identity_ok and t.session.customer_id
        try:
            ref = self.handoff.create_ticket(HandoffTicket(
                session_id=t.session.id, escalation_id=esc_id, reason=reason.value, priority=priority.value,
                intent=intent, summary=summary,
                transcript=tuple(self.store.recent_messages(t.session.id, 10**9, self.s.handoff_transcript_messages)),
                customer_id=t.session.customer_id if signed_in else None,
                order_ids=(order_id,) if order_id and signed_in else (),
            ))
            self.store.set_escalation_ticket(esc_id, ref.external_id)
            return None
        except Exception as exc:
            return f"HANDOFF_FAILED: {type(exc).__name__}: {exc}"

    def _finish(self, t: _Turn, outcome: TurnOutcome, text: str, *, payload: dict | None = None,
                error: str | None = None) -> AgentReply:
        self.store.save_session_state(t.session.id, t.state)
        msg = self.store.log_message(t.session.id, MessageRole.ASSISTANT, text, payload=payload, turn_id=t.rec.turn_id)
        summary = {k: v for k, v in t.info.items()}
        if error:
            summary["error"] = error
        t.rec.finish(outcome, assistant_message_id=msg.id, **summary)
        return AgentReply(text=text, outcome=outcome, turn_id=t.rec.turn_id, payload=payload)
