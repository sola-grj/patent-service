from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.clients.wipo_patentscope_public import WipoPatentScopePublicClient
from app.clients.wipo_patentscope_selenium import (
    build_detail_url as _build_detail_url,
    classify_page_source as _classify_page_source,
)

DETAIL_BASE_URL = "https://patentscope.wipo.int/search/en/detail.jsf"
LOGIN_URL = "https://patentscope.wipo.int/search/wiposso/login"


@dataclass(slots=True)
class ProbeResult:
    state: str
    title: str
    current_url: str
    screenshot_path: str
    html_path: str
    basic_info: dict[str, Any]
    logout_visible: bool
    publication_marker_visible: bool
    captcha_visible: bool


def build_detail_url(*, doc_id: str, cid: str | None) -> str:
    return _build_detail_url(
        base_url=DETAIL_BASE_URL.removesuffix("/detail.jsf"),
        doc_id=doc_id,
        cid=cid,
    )


def classify_page_source(page_source: str) -> str:
    return _classify_page_source(page_source)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Open a WIPO PATENTSCOPE detail page in real Chrome via Selenium, "
            "optionally login, and record whether the page shows bibliographic data or CAPTCHA."
        )
    )
    parser.add_argument("--username", default=os.getenv("WIPO_PROBE_USERNAME", ""))
    parser.add_argument("--password", default=os.getenv("WIPO_PROBE_PASSWORD", ""))
    parser.add_argument(
        "--login",
        action="store_true",
        help="Login before opening the detail page. Default: direct anonymous access.",
    )
    parser.add_argument("--doc-id", default="WO2026044310")
    parser.add_argument("--cid", default="")
    parser.add_argument(
        "--output-dir",
        default="artifacts/wipo-selenium-probe",
        help="Directory where the probe saves HTML and screenshot artifacts.",
    )
    parser.add_argument(
        "--chrome-binary",
        default=os.getenv("WIPO_PROBE_CHROME_BINARY", ""),
        help="Optional path to chrome.exe when Selenium cannot auto-detect it.",
    )
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        help="Run Chrome headless. Default: disabled because WIPO may reject headless sessions.",
    )
    parser.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Run Chrome with a real browser window.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Explicit timeout for navigation and element waits.",
    )
    parser.set_defaults(headless=False)
    return parser.parse_args()


def ensure_credentials(args: argparse.Namespace) -> None:
    if not args.login:
        return
    if args.username and args.password:
        return
    raise SystemExit(
        "Username/password required when --login is enabled. Pass --username/--password or set "
        "WIPO_PROBE_USERNAME and WIPO_PROBE_PASSWORD."
    )


def save_artifacts(
    *,
    output_dir: Path,
    page_source: str,
    driver: Any,
) -> tuple[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    html_path = output_dir / "detail.html"
    screenshot_path = output_dir / "detail.png"
    html_path.write_text(page_source, encoding="utf-8")
    driver.save_screenshot(str(screenshot_path))
    return str(screenshot_path), str(html_path)


def run_probe(args: argparse.Namespace) -> ProbeResult:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from selenium.webdriver import ChromeOptions
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as exc:  # pragma: no cover - exercised in manual use
        raise SystemExit(
            "Selenium is not installed. Run `pip install -e .[probe]` first."
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

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    wait = WebDriverWait(driver, args.timeout_seconds)

    try:
        if args.login:
            driver.get(LOGIN_URL)
            wait.until(
                EC.presence_of_element_located(
                    (By.NAME, "authform_fields:0:authform_text:input")
                )
            ).send_keys(args.username)
            driver.find_element(
                By.NAME, "authform_fields:1:authform_mask:input"
            ).send_keys(args.password)
            driver.find_element(By.ID, "authform_signin").click()

            try:
                wait.until(
                    lambda d: "/search/" in d.current_url
                    and "signin.xhtml" not in d.current_url
                )
            except TimeoutException:
                pass

        detail_url = build_detail_url(doc_id=args.doc_id, cid=args.cid or None)
        driver.get(detail_url)
        try:
            wait.until(lambda d: "Please wait" not in d.page_source)
        except TimeoutException:
            pass

        page_source = driver.page_source
        state = classify_page_source(page_source)
        basic_info: dict[str, Any] = {}
        if state == "bibliographic":
            basic_info = WipoPatentScopePublicClient.parse_patent_details(page_source)
        screenshot_path, html_path = save_artifacts(
            output_dir=Path(args.output_dir),
            page_source=page_source,
            driver=driver,
        )

        return ProbeResult(
            state=state,
            title=driver.title,
            current_url=driver.current_url,
            screenshot_path=screenshot_path,
            html_path=html_path,
            basic_info=basic_info,
            logout_visible="/search/wiposso/logout" in page_source,
            publication_marker_visible="Publication Number" in page_source,
            captcha_visible="psCaptchaForm" in page_source,
        )
    finally:
        driver.quit()


def main() -> int:
    args = parse_args()
    ensure_credentials(args)
    result = run_probe(args)
    json.dump(asdict(result), sys.stdout, ensure_ascii=True, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
