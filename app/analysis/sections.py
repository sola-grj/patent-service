import re

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
        "要約",
        "초록",
        "ملخص",
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
        "請求の範囲",
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


def detect_heading(text: str) -> str | None:
    normalized = re.sub(r"^\s*(?:\(\d+\)|\d+[.)])\s*", "", text.strip().lower())
    normalized = normalized.rstrip(":：. ")
    if len(normalized) > 100:
        return None
    for part, values in _HEADINGS.items():
        if normalized in values:
            return part
        if part == "abstract" and re.match(r"^\(?57\)?\s+abstract$", normalized):
            return part
    return None


def contains_search_report(text: str) -> bool:
    return bool(_SEARCH_REPORT.search(text))
