from dataclasses import dataclass, field

from app.analysis.counting import count_units
from app.models.patents import (
    PatentAnalysisWarning,
    PatentFileAnalysis,
    PatentFivePartAnalysis,
    PatentPartAnalysis,
)

PART_NAMES = (
    "abstract",
    "abstract_drawing",
    "description",
    "description_drawings",
    "claims",
    "unclassified",
)


@dataclass(slots=True)
class PartDraft:
    text: str = ""
    status: str = "missing"
    method: str = "none"
    confidence: str = "none"


@dataclass(slots=True)
class AnalysisDraft:
    filename: str
    file_type: str
    sha256: str = ""
    parts: dict[str, PartDraft] = field(
        default_factory=lambda: {name: PartDraft() for name in PART_NAMES}
    )
    warnings: list[PatentAnalysisWarning] = field(default_factory=list)
    unclassified_document_text: str = ""
    unclassified_drawing_text: str = ""

    def add_text(
        self,
        part: str,
        text: str,
        *,
        method: str,
        confidence: str,
        status: str = "found",
        is_drawing: bool = False,
    ) -> None:
        if not text.strip():
            return
        current = self.parts[part]
        current.text = f"{current.text} {text}".strip()
        current.status = status
        current.method = _merge_method(current.method, method)
        current.confidence = _lower_confidence(current.confidence, confidence)
        if part == "unclassified":
            if is_drawing:
                self.unclassified_drawing_text = (
                    f"{self.unclassified_drawing_text} {text}"
                ).strip()
            else:
                self.unclassified_document_text = (
                    f"{self.unclassified_document_text} {text}"
                ).strip()

    def mark_error(self, part: str, message: str) -> None:
        current = self.parts[part]
        if not current.text:
            current.status = "error"
            current.method = "ocr"
            current.confidence = "none"
        self.warnings.append(
            PatentAnalysisWarning(
                code="ocr_failed", message=message, filename=self.filename
            )
        )

    def to_result(self) -> PatentFileAnalysis:
        part_models = {
            name: PatentPartAnalysis(
                word_count=count_units(draft.text),
                status=draft.status,
                method=draft.method,
                confidence=draft.confidence,
            )
            for name, draft in self.parts.items()
        }
        document_words = sum(
            part_models[name].word_count
            for name in ("abstract", "description", "claims")
        ) + count_units(self.unclassified_document_text)
        drawing_words = sum(
            part_models[name].word_count
            for name in ("abstract_drawing", "description_drawings")
        ) + count_units(self.unclassified_drawing_text)
        status = "partial" if self.warnings else "success"
        if all(part.status in {"missing", "error"} for part in part_models.values()):
            status = "failed"
        return PatentFileAnalysis(
            filename=self.filename,
            file_type=self.file_type,
            sha256=self.sha256,
            status=status,
            parts=PatentFivePartAnalysis(**part_models),
            document_text_words=document_words,
            drawing_ocr_words=drawing_words,
            total_words=document_words + drawing_words,
            warnings=self.warnings,
        )

    @property
    def comparable_text(self) -> str:
        return " ".join(
            self.parts[name].text for name in ("abstract", "description", "claims", "unclassified")
        )


def _merge_method(current: str, new: str) -> str:
    if current == "none":
        return new
    if current == new:
        return current
    return "mixed"


def _lower_confidence(current: str, new: str) -> str:
    order = {"none": 4, "high": 3, "medium": 2, "low": 1}
    if current == "none":
        return new
    return current if order[current] <= order[new] else new
