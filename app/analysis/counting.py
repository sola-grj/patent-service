import re
import unicodedata

from app.utils.text_metrics import normalize_text

_WORD_PATTERN = re.compile(r"[^\W_]+(?:[’'/-][^\W_]+)*", re.UNICODE)


def is_cjk_character(value: str) -> bool:
    code = ord(value)
    return any(
        start <= code <= end
        for start, end in (
            (0x3400, 0x4DBF),
            (0x4E00, 0x9FFF),
            (0xF900, 0xFAFF),
            (0x3040, 0x30FF),
            (0x31F0, 0x31FF),
            (0xAC00, 0xD7AF),
        )
    )


def tokenize_counting_units(text: str) -> list[str]:
    normalized = normalize_text(unicodedata.normalize("NFKC", text))
    if not normalized:
        return []
    cjk_units: list[str] = []
    remaining: list[str] = []
    for character in normalized:
        if is_cjk_character(character):
            cjk_units.append(character)
            remaining.append(" ")
        else:
            remaining.append(character)
    return cjk_units + _WORD_PATTERN.findall("".join(remaining))


def count_units(text: str) -> int:
    return len(tokenize_counting_units(text))


def text_five_grams(text: str) -> set[tuple[str, ...]]:
    tokens = [token.lower() for token in tokenize_counting_units(text)]
    if len(tokens) < 5:
        return set()
    return {tuple(tokens[index : index + 5]) for index in range(len(tokens) - 4)}
