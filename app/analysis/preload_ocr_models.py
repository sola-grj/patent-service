import argparse

from app.analysis.ocr import RapidOcrEngine
from app.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download RapidOCR models into the configured persistent cache."
    )
    parser.add_argument(
        "--languages",
        default="en,de,fr,ru,ko,ar",
        help="Comma-separated document languages to preload.",
    )
    args = parser.parse_args()
    languages = [value.strip() for value in args.languages.split(",") if value.strip()]
    engine = RapidOcrEngine(Settings(ocr_backend="rapidocr", rapidocr_workers=1))
    if not engine.is_available():
        raise SystemExit("RapidOCR and its inference engine must be installed first.")
    engine.preload(languages)


if __name__ == "__main__":
    main()
