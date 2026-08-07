import re
import unicodedata

_HEADINGS = {
    "abstract": (
        "abstract",
        "zusammenfassung",
        "abrégé",
        "abrege",
        "resumen",
        "resumo",
        "реферат",
        "摘要",
        "说明书摘要",
        "說明書摘要",
        "要約",
        "초록",
        "ملخص",
    ),
    "abstract_drawing": (
        "abstract drawing",
        "abstract drawings",
        "abstract figure",
        "abstract figures",
        "摘要附图",
        "说明书摘要附图",
        "摘要附圖",
        "說明書摘要附圖",
        "要約図",
        "요약도",
    ),
    "description": (
        "description",
        "beschreibung",
        "descripción",
        "descripcion",
        "descrição",
        "descricao",
        "описание",
        "说明书",
        "說明書",
        "発明の詳細な説明",
        "명세서",
        "الوصف",
    ),
    "claims": (
        "claims",
        "claim",
        "patentansprüche",
        "patentanspruche",
        "revendications",
        "reivindicaciones",
        "reivindicações",
        "reivindicacoes",
        "формула изобретения",
        "权利要求",
        "权利要求书",
        "權利要求",
        "權利要求書",
        "請求の範囲",
        "特許請求の範囲",
        "청구범위",
        "المطالبات",
    ),
    "description_drawings": (
        "drawings",
        "drawing",
        "zeichnungen",
        "dessins",
        "dibujos",
        "desenhos",
        "чертежи",
        "附图",
        "说明书附图",
        "附圖",
        "說明書附圖",
        "図面",
        "도면",
        "الرسومات",
    ),
}

_SEARCH_REPORT = re.compile(
    r"international\s+search\s+report|european\s+search\s+report|"
    r"internationaler\s+recherchenbericht|rapport\s+de\s+recherche",
    re.IGNORECASE,
)

_PAGE_SUFFIX = re.compile(
    r"\s*(?:"
    r"(?:第\s*)?\d+\s*(?:/\s*\d+)?\s*(?:页|頁)|"
    r"pages?\s*\d+(?:\s*of\s*\d+)?|"
    r"\d+\s*/\s*\d+\s*pages?"
    r")\s*$",
    re.IGNORECASE,
)
_HEADING_SEPARATORS = re.compile(r"[\s\u00b7\u2022\u30fb_\-\u2013\u2014]+")


def detect_heading(text: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", text).strip().lower()
    normalized = re.sub(r"^\s*(?:\(\d+\)|\d+[.)])\s*", "", normalized)
    normalized = _PAGE_SUFFIX.sub("", normalized).rstrip(":：.。 ")
    if len(normalized) > 100:
        return None
    normalized_key = _heading_key(normalized)
    for part, values in _HEADINGS.items():
        if any(normalized_key == _heading_key(value) for value in values):
            return part
    return None


def _heading_key(text: str) -> str:
    return _HEADING_SEPARATORS.sub("", text)


def contains_search_report(text: str) -> bool:
    return bool(_SEARCH_REPORT.search(text))
