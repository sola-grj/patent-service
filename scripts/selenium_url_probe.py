from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    timed_out: bool
    url: str
    current_url: str
    title: str
    screenshot_path: str
    html_path: str
    page_source_excerpt: str
    error: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open any URL in Chromium via Selenium and save HTML/screenshot artifacts "
            "for basic environment diagnostics."
        )
    )
    parser.add_argument("--url", required=True, help="Target URL to open.")
    parser.add_argument(
        "--output-dir",
        default="artifacts/selenium-url-probe",
        help="Directory where the probe saves HTML and screenshot artifacts.",
    )
    parser.add_argument(
        "--chrome-binary",
        default=(
            os.getenv("WIPO_PROBE_CHROME_BINARY")
            or os.getenv("PATENT_SERVICE_WIPO_SELENIUM_CHROME_BINARY", "")
        ),
        help="Optional path to chromium/chrome binary.",
    )
    parser.add_argument(
        "--driver-path",
        default=(
            os.getenv("WIPO_PROBE_DRIVER_PATH")
            or os.getenv("PATENT_SERVICE_WIPO_SELENIUM_DRIVER_PATH", "")
        ),
        help="Optional path to chromedriver.",
    )
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        help="Run Chromium headless.",
    )
    parser.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Run Chromium with a real window.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Page load timeout in seconds.",
    )
    parser.set_defaults(headless=False)
    return parser.parse_args()


def _save_artifacts(*, output_dir: Path, page_source: str, driver: Any) -> tuple[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "page.html"
    screenshot_path = output_dir / "page.png"
    html_path.write_text(page_source, encoding="utf-8")
    driver.save_screenshot(str(screenshot_path))
    return str(screenshot_path), str(html_path)


def _excerpt(text: str, limit: int = 1200) -> str:
    compact = " ".join(text.split())
    return compact[:limit]


def run_probe(args: argparse.Namespace) -> ProbeResult:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver import ChromeOptions
        from selenium.webdriver.chrome.service import Service
    except ImportError as exc:
        raise SystemExit(
            "Selenium is not installed. Install project dependencies first."
        ) from exc

    options = ChromeOptions()
    if args.chrome_binary:
        options.binary_location = args.chrome_binary
    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-extensions")
    options.add_argument("--remote-allow-origins=*")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1600,1200")
    options.add_argument("--lang=zh-CN")

    service = Service(executable_path=args.driver_path) if args.driver_path else Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(args.timeout_seconds)

    timed_out = False
    error = ""
    try:
        try:
            driver.get(args.url)
        except TimeoutException as exc:
            timed_out = True
            error = str(exc)
        except WebDriverException as exc:
            error = str(exc)

        current_url = ""
        title = ""
        page_source = ""
        try:
            current_url = driver.current_url
        except Exception:
            pass
        try:
            title = driver.title
        except Exception:
            pass
        try:
            page_source = driver.page_source or ""
        except Exception:
            page_source = ""

        screenshot_path, html_path = _save_artifacts(
            output_dir=Path(args.output_dir),
            page_source=page_source,
            driver=driver,
        )
        ok = not timed_out and not error
        return ProbeResult(
            ok=ok,
            timed_out=timed_out,
            url=args.url,
            current_url=current_url,
            title=title,
            screenshot_path=screenshot_path,
            html_path=html_path,
            page_source_excerpt=_excerpt(page_source),
            error=error,
        )
    finally:
        driver.quit()


def main() -> int:
    args = parse_args()
    result = run_probe(args)
    json.dump(asdict(result), sys.stdout, ensure_ascii=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
