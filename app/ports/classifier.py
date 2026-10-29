"""ClassifierPort: turn a customer message (and images) into the strict classifier contract.

This is the one place customer text and images are read. The default is a one-shot LLM
(`app/adapters/classifier/llm_oneshot.py`), but anything that produces a valid
`ClassifierOutput` can replace it: a fine-tuned small model, an embedding + linear head, a
rules/regex layer, a cascade of cheap-then-expensive models, an injection detector in front of
an intent model, and so on. Choose with `CLASSIFIER_PROVIDER` in `.env`.

A classifier must:
- return either `output` (a valid `ClassifierOutput`) or `violation`; never raise for bad input;
- fill `output.images` with exactly one assessment per attached image, and `output.injection`
  (use a separate detector if your model doesn't do it): the pipeline escalates on those signals;
- leave all decisions to later stages: sign-in, ownership and policy are not its job.
"""

from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agent.contracts import ClassifierOutput, ContractViolation
from app.agent.llm import LLMResult
from app.agent.preprocess import Preprocessed


@dataclass
class ClassifierRun:
    output: ClassifierOutput | None = None
    violation: ContractViolation | None = None
    model: str = ""  # model or version name, shown in the trace
    params: dict[str, Any] = field(default_factory=dict)  # settings in effect, shown in the trace
    # Filled only by LLM-backed classifiers, so the call is logged in `llm_calls`.
    request_messages: list[dict[str, Any]] = field(default_factory=list)
    result: LLMResult | None = None
    system_prompt_hash: str = ""


class Classifier(Protocol):
    name: str

    def classify(self, pre: Preprocessed, state: dict[str, Any], history: list[dict[str, Any]]) -> ClassifierRun:
        """`state` is trusted session state; `history` holds earlier messages (customer lines are untrusted)."""
