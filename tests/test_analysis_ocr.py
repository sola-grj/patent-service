from app.analysis.ocr import AutoOcrEngine, OcrResult, RapidOcrEngine
from app.config import Settings


class StubBackend:
    def __init__(self, *, available: bool, result: OcrResult) -> None:
        self._available = available
        self._result = result
        self.calls = 0

    def is_available(self) -> bool:
        return self._available

    def recognize(
        self,
        image_bytes: bytes,
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> OcrResult:
        self.calls += 1
        self.language = language
        return self._result


def test_auto_ocr_prefers_rapidocr_and_forwards_language():
    engine = AutoOcrEngine(Settings(ocr_backend="auto"))
    rapid = StubBackend(
        available=True,
        result=OcrResult(text="complete text", provider="rapidocr"),
    )
    tesseract = StubBackend(
        available=True,
        result=OcrResult(text="other text", provider="tesseract"),
    )
    engine._rapidocr = rapid
    engine._tesseract = tesseract

    result = engine.recognize(b"image", language="de")

    assert result.provider == "rapidocr"
    assert rapid.calls == 1
    assert rapid.language == "de"
    assert tesseract.calls == 0


def test_auto_ocr_falls_back_when_primary_backend_fails():
    engine = AutoOcrEngine(Settings(ocr_backend="auto"))
    rapid = StubBackend(
        available=True,
        result=OcrResult(provider="rapidocr", warnings=["failed"]),
    )
    tesseract = StubBackend(
        available=True,
        result=OcrResult(text="fallback text", provider="tesseract"),
    )
    engine._rapidocr = rapid
    engine._tesseract = tesseract

    result = engine.recognize(b"image")

    assert result.provider == "tesseract"
    assert rapid.calls == 1
    assert tesseract.calls == 1


def test_explicit_tesseract_backend_does_not_switch_providers():
    engine = AutoOcrEngine(Settings(ocr_backend="tesseract"))
    tesseract = StubBackend(
        available=False,
        result=OcrResult(provider="tesseract", warnings=["unavailable"]),
    )
    engine._tesseract = tesseract

    result = engine.recognize(b"image")

    assert result.warnings == ["unavailable"]
    assert tesseract.calls == 1


def test_rapidocr_uses_one_v6_model_for_latin_and_v5_for_uncovered_scripts(
    tmp_path,
):
    engine = RapidOcrEngine(
        Settings(rapidocr_model_cache_dir=str(tmp_path), rapidocr_workers=1)
    )

    assert engine._model_specification("de") == ("PP-OCRv6", "small", "multi")
    assert engine._model_specification("en") == ("PP-OCRv6", "small", "multi")
    assert engine._model_specification("ru") == (
        "PP-OCRv5",
        "mobile",
        "cyrillic",
    )
