import re


_LANGUAGE_ALIASES = {
    "ar": "ar",
    "ara": "ar",
    "arabic": "ar",
    "de": "de",
    "deu": "de",
    "ger": "de",
    "german": "de",
    "en": "en",
    "eng": "en",
    "english": "en",
    "es": "es",
    "spa": "es",
    "spanish": "es",
    "fr": "fr",
    "fra": "fr",
    "fre": "fr",
    "french": "fr",
    "ja": "japan",
    "jpn": "japan",
    "japan": "japan",
    "japanese": "japan",
    "ko": "korean",
    "kor": "korean",
    "korean": "korean",
    "pt": "pt",
    "por": "pt",
    "portuguese": "pt",
    "ru": "ru",
    "rus": "ru",
    "russian": "ru",
    "zh": "ch",
    "zho": "ch",
    "chi": "ch",
    "chi_sim": "ch",
    "ch": "ch",
    "chinese": "ch",
}

_TESSERACT_LANGUAGES = {
    "ar": "ara",
    "ch": "chi_sim",
    "de": "deu",
    "en": "eng",
    "es": "spa",
    "fr": "fra",
    "japan": "jpn",
    "korean": "kor",
    "pt": "por",
    "ru": "rus",
}

_LATIN_MARKERS = {
    "de": (" der ", " die ", " das ", " und ", " anspruch", "beschreibung", "ä", "ö", "ü", "ß"),
    "fr": (" le ", " la ", " les ", " et ", " revendication", "abrégé", "é", "è", "à", "ç"),
    "es": (" el ", " la ", " los ", " y ", " reivindicación", "descripción", "ñ", "¿"),
    "pt": (" o ", " a ", " os ", " e ", " reivindicação", "descrição", "ã", "õ"),
    "en": (" the ", " and ", " claim", " abstract", " invention ", " wherein "),
}


def normalize_ocr_language(value: str | None, *, default: str = "en") -> str:
    normalized = (value or "").strip().lower().replace("-", "_")
    if normalized in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[normalized]
    primary = normalized.split("_", 1)[0]
    return _LANGUAGE_ALIASES.get(primary, _LANGUAGE_ALIASES.get(default, "en"))


def detect_ocr_language(text: str, *, hint: str | None = None, default: str = "en") -> str:
    if hint:
        return normalize_ocr_language(hint, default=default)
    sample = f" {' '.join(text.lower().split())[:20000]} "
    if re.search(r"[\u3040-\u30ff]", sample):
        return "japan"
    if re.search(r"[\uac00-\ud7af]", sample):
        return "korean"
    if re.search(r"[\u3400-\u9fff]", sample):
        return "ch"
    if re.search(r"[\u0600-\u06ff]", sample):
        return "ar"
    if re.search(r"[\u0400-\u04ff]", sample):
        return "ru"
    scores = {
        language: sum(sample.count(marker) for marker in markers)
        for language, markers in _LATIN_MARKERS.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] else normalize_ocr_language(default)


def tesseract_language(value: str | None, *, default: str = "eng") -> str:
    normalized = normalize_ocr_language(value, default=default)
    return _TESSERACT_LANGUAGES.get(normalized, default)
