import io
import importlib.util
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageEnhance, ImageOps

from app.analysis.languages import normalize_ocr_language, tesseract_language
from app.analysis.cancellation import current_cancellation, raise_if_cancelled
from app.config import Settings

logger = logging.getLogger("patent_service")


@dataclass(slots=True)
class OcrResult:
    text: str = ""
    confidence: float | None = None
    language: str = ""
    provider: str = "ocr"
    warnings: list[str] = field(default_factory=list)


class OcrEngine(Protocol):
    def recognize(
        self,
        image_bytes: bytes,
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> OcrResult: ...

    def recognize_many(
        self,
        images: list[bytes],
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> list[OcrResult]: ...


def recognize_many(
    engine: OcrEngine,
    images: list[bytes],
    *,
    sparse: bool = False,
    language: str | None = None,
) -> list[OcrResult]:
    """Use backend batching when available and preserve a stub-safe fallback."""
    raise_if_cancelled()
    method = getattr(engine, "recognize_many", None)
    if callable(method):
        return method(images, sparse=sparse, language=language)
    return [
        engine.recognize(image, sparse=sparse, language=language)
        for image in images
    ]


class TesseractOcrEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def is_available(self) -> bool:
        return self._resolve_command() is not None

    def _resolve_command(self) -> str | None:
        if self._settings.tesseract_command:
            configured = Path(self._settings.tesseract_command)
            return str(configured) if configured.is_file() else None
        detected = shutil.which("tesseract")
        if detected:
            return detected
        candidates = (
            Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
            Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
            Path.home() / "AppData/Local/Programs/Tesseract-OCR/tesseract.exe",
        )
        return str(next((path for path in candidates if path.is_file()), "")) or None

    def recognize(
        self,
        image_bytes: bytes,
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> OcrResult:
        raise_if_cancelled()
        try:
            import pytesseract
            from pytesseract import Output
        except ImportError as exc:
            return OcrResult(
                provider="tesseract",
                warnings=[f"pytesseract is unavailable: {exc}"],
            )

        command = self._resolve_command()
        if not command:
            return OcrResult(
                provider="tesseract",
                warnings=["Tesseract executable is not installed or configured."],
            )
        pytesseract.pytesseract.tesseract_cmd = command
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                source.load()
                if source.width * source.height > self._settings.analysis_max_image_pixels:
                    return OcrResult(
                        provider="tesseract",
                        warnings=["image exceeds the configured pixel limit"],
                    )
                image = _prepare_image(source, sparse=sparse)
            requested = (
                [tesseract_language(language)]
                if language
                else self._settings.ocr_languages.split("+")
            )
            installed = set(pytesseract.get_languages(config=""))
            selected = [language for language in requested if language in installed]
            if not selected and "eng" in installed:
                selected = ["eng"]
            if not selected:
                return OcrResult(
                    provider="tesseract",
                    warnings=["no configured Tesseract language is installed"],
                )
            language = "+".join(selected)
            data = pytesseract.image_to_data(
                image,
                lang=language,
                config=f"--psm {11 if sparse else 6}",
                output_type=Output.DICT,
                timeout=self._settings.ocr_timeout_seconds,
            )
        except Exception as exc:
            return OcrResult(
                provider="tesseract", warnings=[f"OCR failed: {exc}"]
            )

        lines: dict[tuple[int, int, int, int], list[str]] = {}
        confidences: list[float] = []
        texts = data.get("text", [])
        for index, (text, confidence) in enumerate(
            zip(texts, data.get("conf", []))
        ):
            stripped = str(text).strip()
            if not stripped:
                continue
            key = tuple(
                int(data.get(name, [0] * len(texts))[index])
                for name in ("page_num", "block_num", "par_num", "line_num")
            )
            lines.setdefault(key, []).append(stripped)
            try:
                numeric_confidence = float(confidence)
            except (TypeError, ValueError):
                continue
            if numeric_confidence >= 0:
                confidences.append(numeric_confidence)
        average = sum(confidences) / len(confidences) if confidences else None
        result = OcrResult(
            text="\n".join(" ".join(words) for words in lines.values()),
            confidence=average,
            language=language,
            provider="tesseract",
        )
        raise_if_cancelled()
        return result

    def recognize_many(
        self,
        images: list[bytes],
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> list[OcrResult]:
        results = [
            self.recognize(image, sparse=sparse, language=language)
            for image in images
        ]
        raise_if_cancelled()
        return results


_RAPID_V5_LANGUAGES = {
    "ar": "arabic",
    "korean": "korean",
    "ru": "cyrillic",
}


class RapidOcrEngine:
    """Persistent, bounded RapidOCR worker pool backed by ONNX/OpenVINO."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._workers = max(1, settings.rapidocr_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=self._workers,
            thread_name_prefix="patent-rapidocr",
        )
        self._thread_local = threading.local()
        self._engine_init_lock = threading.Lock()
        self._cache_dir = Path(
            settings.rapidocr_model_cache_dir
            or _persistent_model_cache("rapidocr")
        ).resolve()
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def is_available(self) -> bool:
        runtime_module = (
            "onnxruntime"
            if self._settings.rapidocr_engine == "onnxruntime"
            else "openvino"
        )
        return (
            importlib.util.find_spec("rapidocr") is not None
            and importlib.util.find_spec(runtime_module) is not None
        )

    def recognize(
        self,
        image_bytes: bytes,
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> OcrResult:
        cancellation = current_cancellation()
        if cancellation:
            cancellation.raise_if_cancelled()
        future = self._executor.submit(
            self._recognize_worker_with_cancellation,
            cancellation,
            image_bytes,
            sparse,
            language,
        )
        return _wait_for_ocr_future(future, cancellation)

    def recognize_many(
        self,
        images: list[bytes],
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> list[OcrResult]:
        if not images:
            return []
        started_at = time.monotonic()
        selected_language = normalize_ocr_language(
            language, default=self._settings.ocr_default_language
        )
        logger.info(
            "OCR engine batch started provider=rapidocr images=%s workers=%s language=%s sparse=%s model=%s engine=%s",
            len(images),
            self._workers,
            selected_language,
            sparse,
            self._model_specification(selected_language)[0],
            self._settings.rapidocr_engine,
        )
        cancellation = current_cancellation()
        if cancellation:
            cancellation.raise_if_cancelled()
        futures = [
            self._executor.submit(
                self._recognize_worker_with_cancellation,
                cancellation,
                image,
                sparse,
                language,
            )
            for image in images
        ]
        try:
            results = [
                _wait_for_ocr_future(future, cancellation)
                for future in futures
            ]
        except BaseException:
            for future in futures:
                future.cancel()
            logger.info(
                "OCR engine batch cancelled provider=rapidocr images=%s",
                len(images),
            )
            raise
        logger.info(
            "OCR engine batch finished provider=rapidocr images=%s recognized=%s warnings=%s elapsed_ms=%s",
            len(images),
            sum(bool(result.text.strip()) for result in results),
            sum(len(result.warnings) for result in results),
            int((time.monotonic() - started_at) * 1000),
        )
        return results

    def _recognize_worker_with_cancellation(
        self,
        cancellation,
        image_bytes: bytes,
        sparse: bool,
        language: str | None,
    ) -> OcrResult:
        if cancellation:
            cancellation.raise_if_cancelled()
        result = self._recognize_worker(image_bytes, sparse, language)
        if cancellation:
            cancellation.raise_if_cancelled()
        return result

    def preload(self, languages: list[str]) -> None:
        """Download and initialize each distinct configured model once."""
        seen: set[tuple[str, str, str]] = set()
        for language in languages:
            selected = normalize_ocr_language(
                language, default=self._settings.ocr_default_language
            )
            specification = self._model_specification(selected)
            if specification in seen:
                continue
            seen.add(specification)
            self._build_engine(specification)

    def _recognize_worker(
        self, image_bytes: bytes, sparse: bool, language: str | None
    ) -> OcrResult:
        selected_language = normalize_ocr_language(
            language, default=self._settings.ocr_default_language
        )
        if not self.is_available():
            return OcrResult(
                provider="rapidocr",
                language=selected_language,
                warnings=["RapidOCR or its configured inference engine is unavailable."],
            )
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                source.load()
                if source.width * source.height > self._settings.analysis_max_image_pixels:
                    return OcrResult(
                        provider="rapidocr",
                        language=selected_language,
                        warnings=["image exceeds the configured pixel limit"],
                    )
                image = ImageOps.exif_transpose(source).convert("RGB")
            import numpy as np

            engine = self._get_worker_engine(selected_language)
            output = engine(np.asarray(image))
        except Exception as exc:
            return OcrResult(
                provider="rapidocr",
                language=selected_language,
                warnings=[f"RapidOCR failed: {exc}"],
            )

        texts = [
            str(value).strip()
            for value in (getattr(output, "txts", None) or ())
            if str(value).strip()
        ]
        scores = [
            float(value) for value in (getattr(output, "scores", None) or ())
        ]
        return OcrResult(
            text="\n".join(texts),
            confidence=(sum(scores) / len(scores) * 100) if scores else None,
            language=selected_language,
            provider="rapidocr",
        )

    def _get_worker_engine(self, language: str):
        engines = getattr(self._thread_local, "engines", None)
        if engines is None:
            engines = {}
            self._thread_local.engines = engines
        specification = self._model_specification(language)
        engine = engines.get(specification)
        if engine is not None:
            return engine
        # Prevent concurrent first-use downloads. Inference itself is unlocked.
        with self._engine_init_lock:
            engine = engines.get(specification)
            if engine is None:
                engine = self._build_engine(specification)
                engines[specification] = engine
        return engine

    def _model_specification(self, language: str) -> tuple[str, str, str]:
        v5_language = _RAPID_V5_LANGUAGES.get(language)
        if v5_language:
            return ("PP-OCRv5", "mobile", v5_language)
        return ("PP-OCRv6", self._settings.rapidocr_model_type, "multi")

    def _build_engine(self, specification: tuple[str, str, str]):
        from rapidocr import (
            EngineType,
            LangDet,
            LangRec,
            ModelType,
            OCRVersion,
            RapidOCR,
        )

        version, model_type, language = specification
        started_at = time.monotonic()
        logger.info(
            "OCR model initialization started provider=rapidocr version=%s model_type=%s language_model=%s engine=%s cache_dir=%s",
            version,
            model_type,
            language,
            self._settings.rapidocr_engine,
            self._cache_dir,
        )
        engine_type = EngineType(self._settings.rapidocr_engine)
        ocr_version = OCRVersion(version)
        recognition_language = (
            LangRec.EN if version == "PP-OCRv6" else LangRec(language)
        )
        params = {
            "Global.model_root_dir": str(self._cache_dir),
            "Global.use_cls": False,
            "Global.max_side_len": self._settings.rapidocr_max_side,
            "Global.log_level": "warning",
            "Det.engine_type": engine_type,
            # PP-OCRv6 uses one multilingual model; the language value is only
            # used by RapidOCR's model resolver, not to select a dictionary.
            "Det.lang_type": LangDet.EN,
            "Det.model_type": ModelType(
                self._settings.rapidocr_model_type
                if version == "PP-OCRv6"
                else "small"
            ),
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Rec.engine_type": engine_type,
            "Rec.lang_type": recognition_language,
            "Rec.model_type": ModelType(model_type),
            "Rec.ocr_version": ocr_version,
        }
        if engine_type is EngineType.ONNXRUNTIME:
            params.update(
                {
                    "EngineConfig.onnxruntime.intra_op_num_threads": (
                        self._settings.rapidocr_intra_op_num_threads
                    ),
                    "EngineConfig.onnxruntime.inter_op_num_threads": (
                        self._settings.rapidocr_inter_op_num_threads
                    ),
                }
            )
        engine = RapidOCR(params=params)
        logger.info(
            "OCR model initialization finished provider=rapidocr version=%s model_type=%s language_model=%s engine=%s elapsed_ms=%s",
            version,
            model_type,
            language,
            self._settings.rapidocr_engine,
            int((time.monotonic() - started_at) * 1000),
        )
        return engine


class AutoOcrEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tesseract = TesseractOcrEngine(settings)
        self._rapidocr = RapidOcrEngine(settings)

    def recognize(
        self,
        image_bytes: bytes,
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> OcrResult:
        raise_if_cancelled()
        backend = self._settings.ocr_backend
        if backend == "tesseract":
            return self._tesseract.recognize(
                image_bytes, sparse=sparse, language=language
            )
        if backend == "rapidocr":
            return self._rapidocr.recognize(
                image_bytes, sparse=sparse, language=language
            )
        failures: list[str] = []
        for candidate in (self._rapidocr, self._tesseract):
            if not candidate.is_available():
                continue
            result = candidate.recognize(
                image_bytes, sparse=sparse, language=language
            )
            # Empty pages are valid. Only actual backend errors fall through.
            if not result.warnings:
                return result
            failures.extend(result.warnings)
        return OcrResult(
            provider="none",
            warnings=failures or ["No OCR backend is available."],
        )

    def recognize_many(
        self,
        images: list[bytes],
        *,
        sparse: bool = False,
        language: str | None = None,
    ) -> list[OcrResult]:
        raise_if_cancelled()
        if not images:
            return []
        configured = self._settings.ocr_backend
        explicit = {
            "rapidocr": self._rapidocr,
            "tesseract": self._tesseract,
        }.get(configured)
        if explicit is not None:
            logger.info(
                "OCR backend selected configured=%s selected=%s images=%s language=%s",
                configured,
                configured,
                len(images),
                language or self._settings.ocr_default_language,
            )
            return recognize_many(
                explicit, images, sparse=sparse, language=language
            )
        primary = next(
            (
                candidate
                for candidate in (self._rapidocr, self._tesseract)
                if candidate.is_available()
            ),
            None,
        )
        if primary is None:
            logger.warning(
                "OCR backend unavailable configured=auto images=%s",
                len(images),
            )
            return [
                OcrResult(
                    provider="none", warnings=["No OCR backend is available."]
                )
                for _ in images
            ]
        results = recognize_many(
            primary, images, sparse=sparse, language=language
        )
        fallback = self._tesseract if primary is self._rapidocr else None
        fallback_count = sum(bool(result.warnings) for result in results)
        if fallback_count and fallback is not None and fallback.is_available():
            logger.warning(
                "OCR backend fallback selected primary=rapidocr fallback=tesseract images=%s language=%s",
                fallback_count,
                language or self._settings.ocr_default_language,
            )
        final_results = [
            result
            if not result.warnings
            else fallback.recognize(image, sparse=sparse, language=language)
            if fallback is not None and fallback.is_available()
            else result
            for image, result in zip(images, results)
        ]
        raise_if_cancelled()
        return final_results
    def diagnostics(self) -> dict[str, object]:
        tesseract_available = self._tesseract.is_available()
        rapidocr_available = self._rapidocr.is_available()
        configured = self._settings.ocr_backend
        selected = configured
        if configured == "auto":
            selected = (
                "rapidocr"
                if rapidocr_available
                else "tesseract"
                if tesseract_available
                else "none"
            )
        return {
            "configured_backend": configured,
            "selected_backend": selected,
            "tesseract_available": tesseract_available,
            "rapidocr_available": rapidocr_available,
            "rapidocr_model": f"PP-OCRv6_{self._settings.rapidocr_model_type}",
            "rapidocr_engine": self._settings.rapidocr_engine,
            "available": selected != "none",
        }


def _wait_for_ocr_future(future, cancellation):
    if cancellation is None:
        return future.result()
    while True:
        cancellation.raise_if_cancelled()
        try:
            return future.result(timeout=0.1)
        except FutureTimeoutError:
            continue


def _persistent_model_cache(provider: str) -> Path:
    if os.name == "nt" and os.getenv("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"])
    else:
        root = Path.home() / ".cache"
    return root / "patent-service" / "models" / provider


def _prepare_image(source: Image.Image, *, sparse: bool) -> Image.Image:
    image = ImageOps.exif_transpose(source).convert("L")
    if image.width < 1600:
        scale = min(3, max(1, 1600 // max(1, image.width)))
        if scale > 1:
            image = image.resize(
                (image.width * scale, image.height * scale), Image.Resampling.LANCZOS
            )
    image = ImageOps.autocontrast(image)
    image = ImageEnhance.Contrast(image).enhance(1.5)
    if sparse:
        image = image.point(lambda value: 255 if value > 190 else 0)
    return image
