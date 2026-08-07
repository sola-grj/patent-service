import pytest

from app.analysis.sections import detect_heading


@pytest.mark.parametrize(
    ("heading", "part"),
    (
        ("权利要求书", "claims"),
        ("权 利 要 求 书", "claims"),
        ("权利要求书 1/2页", "claims"),
        ("權利要求書", "claims"),
        ("特許請求の範囲", "claims"),
        ("说明书摘要", "abstract"),
        ("说 明 书 摘 要", "abstract"),
        ("说明书摘要附图", "abstract_drawing"),
        ("摘要附圖", "abstract_drawing"),
        ("说明书", "description"),
        ("说 明 书 附 图 3/6页", "description_drawings"),
        ("說明書附圖", "description_drawings"),
        ("C L A I M S — PAGE 1 OF 2", "claims"),
    ),
)
def test_detect_heading_accepts_official_and_ocr_spaced_page_titles(
    heading: str, part: str
):
    assert detect_heading(heading) == part


@pytest.mark.parametrize(
    "line",
    (
        "本发明的保护范围由权利要求书限定。",
        "具体结构参见说明书附图。",
        "The claims describe a timer and user equipment.",
        "Fig. 1 shows the abstract drawing in context.",
    ),
)
def test_detect_heading_does_not_match_section_words_inside_body_text(line: str):
    assert detect_heading(line) is None
