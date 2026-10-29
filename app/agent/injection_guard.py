"""Stage 1b: a deterministic prompt-injection guard that runs right after the classifier.

The classifier is itself an LLM reading hostile text, so its own `injection` flag can be
talked out of firing. This guard does not trust it. It scores two things against the
patterns in `agent_config/injection_guard.json`:

- the cleaned customer text (also in normalised, de-spaced and base64-decoded forms), and
- every free-text entity the classifier extracted, because those values travel on into
  handlers and the responder prompt. A classifier that decoded or paraphrased an attack
  into an entity is caught here.

No model call, no network: same input, same verdict. A block escalates the turn exactly
like a classifier-detected injection.
"""

import base64
import binascii
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.agent.contracts import TEXT_ENTITIES, ClassifierOutput, InjectionTechnique

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_SPACED_LETTERS = re.compile(r"\b(?:\w[ .\-_*]){4,}\w\b")  # "i g n o r e"
_BASE64_TOKEN = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")


@dataclass(frozen=True)
class Finding:
    id: str
    technique: str
    weight: int
    source: str  # "text", "text:normalized", "text:base64", or "entity:<name>"


@dataclass
class GuardResult:
    blocked: bool = False
    score: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def techniques(self) -> list[str]:
        return sorted({f.technique for f in self.findings})

    def to_log(self) -> dict[str, Any]:
        return {
            "blocked": self.blocked,
            "score": self.score,
            "findings": [{"id": f.id, "technique": f.technique, "weight": f.weight, "source": f.source}
                         for f in self.findings],
        }


def _views(text: str) -> list[tuple[str, str]]:
    """The text as the model might read it: as written, normalised, de-spaced and leet-folded."""
    base = unicodedata.normalize("NFKC", text).casefold()
    despaced = _SPACED_LETTERS.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group(0)), base)  # before whitespace collapses
    base = re.sub(r"\s+", " ", base)
    despaced = re.sub(r"\s+", " ", despaced)
    return [("text", text.casefold()), ("text:normalized", base), ("text:normalized", despaced),
            ("text:normalized", despaced.translate(_LEET))]


def _decoded_blobs(text: str, max_chars: int) -> list[str]:
    out = []
    for token in _BASE64_TOKEN.findall(text):
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), validate=True)
            decoded = raw.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if decoded.isprintable() or "\n" in decoded:
            out.append(decoded[:max_chars])
    return out


class InjectionGuard:
    def __init__(self, cfg: dict[str, Any]):
        self.enabled: bool = cfg["enabled"]
        self.threshold: int = cfg["block_threshold"]
        self.max_decoded_chars: int = cfg["max_decoded_chars"]
        self.patterns = [
            (p["id"], InjectionTechnique(p["technique"]).value, int(p["weight"]), re.compile(p["regex"], re.IGNORECASE))
            for p in cfg["patterns"]
        ]

    def _scan(self, views: list[tuple[str, str]], seen: dict[str, Finding]) -> None:
        for pid, technique, weight, rx in self.patterns:
            if pid in seen:
                continue
            for source, text in views:
                if rx.search(text):
                    seen[pid] = Finding(pid, technique, weight, source)
                    break

    def inspect(self, text: str, classification: ClassifierOutput | None = None) -> GuardResult:
        if not self.enabled:
            return GuardResult()
        seen: dict[str, Finding] = {}
        self._scan(_views(text), seen)

        decoded = _decoded_blobs(text, self.max_decoded_chars)
        if decoded:
            before = set(seen)
            self._scan([("text:base64", d.casefold()) for d in decoded], seen)
            if set(seen) != before:  # an encoded payload that is itself an attack
                seen["encoded_attack"] = Finding("encoded_attack", InjectionTechnique.ENCODED_OR_OBFUSCATED.value, 3,
                                                 "text:base64")

        if classification is not None:
            for name in TEXT_ENTITIES:
                value = getattr(classification.entities, name)
                if value:
                    self._scan([(f"entity:{name}", unicodedata.normalize("NFKC", value).casefold())], seen)

        findings = list(seen.values())
        score = sum(f.weight for f in findings)
        return GuardResult(blocked=score >= self.threshold, score=score, findings=findings)
