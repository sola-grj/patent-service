from app.analysis.counting import count_units, tokenize_counting_units


def test_counting_standard_counts_words_cjk_and_technical_numbers():
    text = "A47J 36/06 中文 테스트 123-A Fig.2"

    units = tokenize_counting_units(text)

    assert units == ["中", "文", "테", "스", "트", "A47J", "36/06", "123-A", "Fig", "2"]
    assert count_units(text) == 10


def test_counting_standard_ignores_punctuation_and_whitespace():
    assert count_units("... ，； ( ) \n") == 0

