from app.analysis.languages import (
    detect_ocr_language,
    normalize_ocr_language,
    tesseract_language,
)


def test_official_language_hint_wins_over_text_heuristics():
    assert detect_ocr_language("the English abstract", hint="DE") == "de"


def test_uploaded_text_detects_supported_scripts_and_latin_languages():
    assert detect_ocr_language("Die Erfindung betrifft einen Behälter und einen Deckel") == "de"
    assert detect_ocr_language("Настоящее изобретение относится к устройству") == "ru"
    assert detect_ocr_language("本发明涉及一种容器") == "ch"
    assert detect_ocr_language("本発明は容器に関する") == "japan"


def test_language_aliases_map_to_internal_and_tesseract_codes():
    assert normalize_ocr_language("deu") == "de"
    assert normalize_ocr_language("zh-CN") == "ch"
    assert tesseract_language("de") == "deu"
