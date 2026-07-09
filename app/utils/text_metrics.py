import re
from collections.abc import Iterable

_PARAGRAPH_MARKER_PATTERN = re.compile(r"\[\d{4,}\]\s*")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_WORD_PATTERN = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+(?:[’'/-][0-9A-Za-zÀ-ÖØ-öø-ÿ]+)*")
_DRAWING_LABEL_PATTERN = re.compile(r"^FIGS?\.\s+.+", re.IGNORECASE)


def normalize_text(text: str) -> str:
    stripped = _PARAGRAPH_MARKER_PATTERN.sub("", text)
    return _WHITESPACE_PATTERN.sub(" ", stripped).strip()


def count_words(text: str) -> int:
    normalized = normalize_text(text)
    if not normalized:
        return 0
    return len(_WORD_PATTERN.findall(normalized))


def extract_drawing_labels(paragraphs: Iterable[str]) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for paragraph in paragraphs:
        normalized = normalize_text(paragraph)
        if not normalized or not _DRAWING_LABEL_PATTERN.match(normalized):
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        labels.append(normalized)
    return labels
