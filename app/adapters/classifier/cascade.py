"""Cascade classifier: a fast local model answers the clear cases, the LLM handles the rest.

Pick it with CLASSIFIER_PROVIDER=cascade. For each message:

1. Anything the local model should not decide goes straight to the LLM classifier:
   images, long messages, frustrated wording, anything the injection guard's patterns react to,
   and replies to a follow-up question other than "what is your order number?".
2. A TF-IDF + logistic-regression model (trained at start-up from `agent_config/classifier_seed.json`,
   no network, a few milliseconds per message) predicts the intent.
3. The local answer is used only when the intent is one of `local_intents` (those whose required
   fields rules can extract), confidence is at least CASCADE_MIN_CONFIDENCE and no second intent is close.
   Otherwise the LLM classifier runs and its answer is used unchanged.

The local path returns the same strict `ClassifierOutput`, validated by the same parser, so nothing
downstream changes. Sign-in, ownership and policy are still decided in later stages, and the
injection guard (stage 1b) still checks the result.
"""

import json
import re
from typing import Any

import numpy as np
from scipy.sparse import hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

from app.adapters.classifier.llm_oneshot import OneShotLLMClassifier
from app.agent.config_loader import AgentConfig
from app.agent.contracts import Entities, parse_classifier_output
from app.agent.injection_guard import InjectionGuard
from app.agent.llm import LLMClient
from app.agent.preprocess import Preprocessed
from app.config import Settings
from app.ports.classifier import ClassifierRun

_TOKEN = re.compile(r"[A-Za-z0-9]+")
_SECOND_INTENT_MARGIN = 0.25  # a runner-up this likely means the message may hold two requests


class LocalModel:
    """Text → (intent, probability, runner-up probability). Deterministic."""

    def __init__(self, examples: dict[str, list[str]]):
        texts, labels = [], []
        for intent, items in examples.items():
            for t in items:
                texts.append(t)
                labels.append(intent)
        self.words = TfidfVectorizer(ngram_range=(1, 2), lowercase=True, sublinear_tf=True)
        self.chars = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), lowercase=True, sublinear_tf=True)
        x = hstack([self.words.fit_transform(texts), self.chars.fit_transform(texts)]).tocsr()
        self.clf = LogisticRegression(C=30.0, max_iter=2000, class_weight="balanced")
        self.clf.fit(x, labels)

    def predict(self, text: str) -> tuple[str, float, float]:
        x = hstack([self.words.transform([text]), self.chars.transform([text])]).tocsr()
        probs = self.clf.predict_proba(x)[0]
        order = np.argsort(probs)[::-1]
        return str(self.clf.classes_[order[0]]), float(probs[order[0]]), float(probs[order[1]])


class CascadeClassifier:
    name = "cascade"

    def __init__(self, settings: Settings, config: AgentConfig, llm: LLMClient):
        self.s = settings
        self.config = config
        self.fallback = OneShotLLMClassifier(settings, config, llm)
        seed = json.loads((settings.agent_config_dir / "classifier_seed.json").read_text(encoding="utf-8"))
        unknown = [i for i in [*seed["examples"], *seed["local_intents"]] if i not in config.intents]
        if unknown:
            raise RuntimeError(f"classifier_seed.json names unknown intents: {unknown}")
        self.local_intents = frozenset(seed["local_intents"])
        self.frustration = tuple(m.lower() for m in seed["frustration_markers"])
        self.model = LocalModel(seed["examples"])
        self.guard = InjectionGuard(config.injection_guard)
        self.order_id = re.compile(settings.order_id_pattern.strip("^$"))
        self.discount = re.compile(settings.discount_code_pattern.strip("^$"))

    # --- helpers --------------------------------------------------------------

    def _order_ids(self, text: str) -> list[str]:
        return list(dict.fromkeys(t for t in _TOKEN.findall(text.upper()) if self.order_id.fullmatch(t)))

    def _discount_codes(self, text: str) -> list[str]:
        """Upper-case tokens that look like codes: letters and digits, and not an order id."""
        found = [t for t in _TOKEN.findall(text) if t == t.upper()]
        return list(dict.fromkeys(
            t for t in found if self.discount.fullmatch(t) and any(c.isdigit() for c in t)
            and any(c.isalpha() for c in t) and not self.order_id.fullmatch(t)))

    def _local_route(self, pre: Preprocessed, state: dict[str, Any]) -> tuple[dict | None, str]:
        """Return (classification payload, reason). Payload is None when the LLM must decide."""
        text = pre.text
        if pre.images:
            return None, "has_image"
        if len(text) > self.s.cascade_max_chars:
            return None, "long_message"
        lower = text.lower()
        if any(m in lower for m in self.frustration):
            return None, "frustration_wording"
        if self.guard.inspect(text).findings:
            return None, "injection_pattern"

        active = state.get("active") or {}
        awaiting = active.get("awaiting")
        order_ids = self._order_ids(text)

        if awaiting:  # the customer is answering a question we asked
            if awaiting == "order_id" and len(order_ids) == 1 and len(_TOKEN.findall(text)) <= 6:
                return self._payload(active["intent"], 0.95, {"order_id": order_ids[0]}), "answers_order_id"
            return None, "slot_reply"

        intent, conf, runner_up = self.model.predict(text)
        if intent not in self.local_intents:
            return None, "not_a_local_intent"
        if conf < self.s.cascade_min_confidence:
            return None, "low_confidence"
        if runner_up > _SECOND_INTENT_MARGIN:
            return None, "second_intent_close"

        entities: dict[str, Any] = {}
        if intent in ("track_order", "refund_status"):
            if len(order_ids) > 1:
                return None, "several_order_ids"
            if order_ids:
                entities["order_id"] = order_ids[0]
        elif intent == "discount_code":
            codes = self._discount_codes(text)
            if len(codes) != 1:
                return None, "discount_code_unclear"
            entities["discount_code"] = codes[0]
        return self._payload(intent, conf, entities), "local_model"

    @staticmethod
    def _payload(intent: str, confidence: float, entities: dict[str, Any]) -> dict:
        return {
            "contract_version": "1",
            "intent": intent,
            "confidence": round(confidence, 4),
            "secondary_intents": [],
            "sentiment": "neutral",
            "injection": {"detected": False, "techniques": []},
            "images": [],
            "entities": {**{name: None for name in Entities.model_fields}, **entities},
        }

    # --- port -----------------------------------------------------------------

    def classify(self, pre: Preprocessed, state: dict[str, Any], history: list[dict[str, Any]]) -> ClassifierRun:
        payload, reason = self._local_route(pre, state)
        if payload is not None:
            run = ClassifierRun(model="cascade:local", params={"route": "local", "reason": reason,
                                                               "min_confidence": self.s.cascade_min_confidence})
            run.output = parse_classifier_output(json.dumps(payload), config=self.config, image_count=0,
                                                 max_entity_chars=self.s.classifier_max_entity_chars)
            return run
        run = self.fallback.classify(pre, state, history)
        run.params = {**run.params, "route": "llm", "reason": reason}
        return run
