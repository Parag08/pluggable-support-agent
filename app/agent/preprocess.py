"""Stage 0: clean, redact and validate a customer message before anything else sees it."""

import base64
import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

from app.config import Settings

# File signatures used to confirm an image is what its declared type says.
_MAGIC = {
    "image/png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda b: b.startswith(b"\xff\xd8\xff"),
    "image/webp": lambda b: b[:4] == b"RIFF" and b[8:12] == b"WEBP",
    "image/gif": lambda b: b[:6] in (b"GIF87a", b"GIF89a"),
}


@dataclass(frozen=True)
class ImageInput:
    media_type: str
    data: bytes


@dataclass
class Preprocessed:
    text: str
    images: list[ImageInput]
    redactions: list[str] = field(default_factory=list)
    stripped_hidden_chars: int = 0
    rejection: str | None = None  # template key when the message can't be processed
    rejection_params: dict = field(default_factory=dict)

    def image_meta(self) -> list[dict]:
        return [
            {"index": i, "media_type": img.media_type, "bytes": len(img.data),
             "sha256": hashlib.sha256(img.data).hexdigest()}
            for i, img in enumerate(self.images)
        ]

    def image_data_urls(self) -> list[str]:
        return [
            f"data:{img.media_type};base64,{base64.standard_b64encode(img.data).decode('ascii')}"
            for img in self.images
        ]


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def redact(text: str, rules: list[dict]) -> tuple[str, list[str]]:
    found: list[str] = []
    for rule in rules:
        pattern = re.compile(rule["pattern"])

        def _sub(m: re.Match, rule=rule) -> str:
            if rule["luhn"] and not _luhn_ok(re.sub(r"\D", "", m.group(0))):
                return m.group(0)
            found.append(rule["name"])
            return rule["replacement"]

        text = pattern.sub(_sub, text)
    return text, sorted(set(found))


def preprocess(text: str, images: list[ImageInput], settings: Settings, redaction_cfg: dict) -> Preprocessed:
    # NFKC folds look-alike characters (full-width letters etc.) used to dodge filters.
    cleaned = unicodedata.normalize("NFKC", text or "")
    hidden = sum(cleaned.count(ch) for ch in redaction_cfg["strip_characters"])
    for ch in redaction_cfg["strip_characters"]:
        cleaned = cleaned.replace(ch, "")
    cleaned = cleaned.strip()
    cleaned, redactions = redact(cleaned, redaction_cfg["rules"])

    result = Preprocessed(text=cleaned, images=list(images), redactions=redactions, stripped_hidden_chars=hidden)

    if not cleaned and not images:
        result.rejection = "empty_message"
    elif len(cleaned) > settings.max_message_chars:
        result.rejection = "message_too_long"
    elif images and not settings.classifier_vision_model:
        result.rejection = "images_unsupported"
    elif len(images) > settings.max_images_per_message:
        result.rejection = "image_too_many"
        result.rejection_params = {"max_images": settings.max_images_per_message}
    else:
        for img in images:
            if img.media_type not in settings.allowed_image_type_set or not _MAGIC.get(img.media_type, lambda b: False)(img.data):
                result.rejection = "image_bad_type"
                break
            if len(img.data) > settings.max_image_bytes:
                result.rejection = "image_too_large"
                result.rejection_params = {"max_mb": settings.max_image_bytes // (1024 * 1024)}
                break
    return result
