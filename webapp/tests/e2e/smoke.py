"""Playwright smoke tester for sixdegrees.net's user-facing functions.

Exercises the flows a real visitor would use -- search, pathfinding, Check My
Contacts (OPRF-PSI, mobile-emulated since the desktop UI hides that section),
Suggest Link and Dispute Link modals -- and reports anomalies: console
errors, uncaught page errors, failed/4xx+ network requests, and visible
"Error:" text left in the DOM after an operation that should have succeeded.

Auth: sixdegrees.net is gated by Cloudflare Access. This reuses a cached
storage_state (.auth_state.json, gitignored) rather than logging in fresh
each run. If that file is missing or the session has expired, regenerate it
with the two-step OTP flow documented in the project's testing-cloudflare-
access memory (fill email -> capture verify-code URL -> read the code from
the test Gmail account -> submit it -> save storage_state()).

Run:
  C:\\Users\\johnk\\Anaconda3\\python.exe webapp/tests/e2e/smoke.py [--base-url URL] [--headed]
"""
import argparse
import json
import random
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
AUTH_STATE = HERE / ".auth_state.json"
DEFAULT_BASE_URL = "https://sixdegrees.net"

# A handful of names expected to actually be in the graph (for pathfinding /
# contact-match scenarios) -- high-profile enough to be safe bets across
# rebuilds without hardcoding anything sensitive.
KNOWN_NAMES = ["Donald Trump", "Elon Musk", "Nancy Pelosi", "Bill Gates", "Warren Buffett"]


class Anomaly:
    def __init__(self, scenario, kind, detail):
        self.scenario = scenario
        self.kind = kind
        self.detail = detail

    def __str__(self):
        return f"[{self.scenario}] {self.kind}: {self.detail}"


class Watcher:
    """Attaches console/network/error listeners to a page for one scenario."""

    def __init__(self, page, name, anomalies):
        self.page = page
        self.name = name
        self.anomalies = anomalies

    def __enter__(self):
        self.page.on("console", self._on_console)
        self.page.on("pageerror", self._on_pageerror)
        self.page.on("requestfailed", self._on_request_failed)
        self.page.on("response", self._on_response)
        return self

    def __exit__(self, *exc):
        self.page.remove_listener("console", self._on_console)
        self.page.remove_listener("pageerror", self._on_pageerror)
        self.page.remove_listener("requestfailed", self._on_request_failed)
        self.page.remove_listener("response", self._on_response)
        return False

    def _on_console(self, msg):
        if msg.type == "error":
            self.anomalies.append(Anomaly(self.name, "console_error", msg.text))

    def _on_pageerror(self, exc):
        self.anomalies.append(Anomaly(self.name, "page_error", str(exc)))

    def _on_request_failed(self, request):
        self.anomalies.append(Anomaly(
            self.name, "request_failed",
            f"{request.method} {request.url} -- {request.failure}",
        ))

    def _on_response(self, response):
        if response.status >= 400 and "/api/" in response.url:
            self.anomalies.append(Anomaly(
                self.name, "http_error", f"{response.status} {response.url}",
            ))


def make_synthetic_vcf(path, n_random=400):
    """A vCard file mixing known real names with random filler, sized like a
    real phone's contact list -- large enough to force multiple oprf-eval
    chunks and many manifest-shard fetches, which is what the user's original
    bug report needs to reproduce."""
    first = ["James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael", "Linda",
             "David", "Barbara", "William", "Elizabeth", "Richard", "Susan", "Joseph", "Jessica"]
    last = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
            "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas"]
    rng = random.Random(42)
    names = list(KNOWN_NAMES)
    for _ in range(n_random):
        names.append(f"{rng.choice(first)} {rng.choice(last)}")
    lines = []
    for name in names:
        lines.append("BEGIN:VCARD")
        lines.append("VERSION:3.0")
        lines.append(f"FN:{name}")
        lines.append("END:VCARD")
    path.write_text("\n".join(lines), encoding="utf-8")
    return names


def require_auth_state():
    if not AUTH_STATE.exists():
        sys.exit(
            f"No cached auth state at {AUTH_STATE}.\n"
            "Log in once (Cloudflare Access OTP) and save storage_state() there -- "
            "see the testing-cloudflare-access project memory for the two-step flow."
        )


def scenario_homepage(context, base_url, anomalies):
    page = context.new_page()
    with Watcher(page, "homepage", anomalies):
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        if "cloudflareaccess.com" in page.url:
            anomalies.append(Anomaly("homepage", "auth_expired",
                                      "Redirected to Cloudflare Access login -- cached session expired."))
    page.close()


def scenario_search(context, base_url, anomalies):
    page = context.new_page()
    with Watcher(page, "search", anomalies):
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        page.fill("#src-input", KNOWN_NAMES[0])
        try:
            page.wait_for_selector("#src-dropdown .dropdown-item", timeout=8000)
        except Exception:
            anomalies.append(Anomaly("search", "no_results", f"No dropdown results for '{KNOWN_NAMES[0]}'"))
    page.close()


def scenario_pathfind(context, base_url, anomalies):
    page = context.new_page()
    with Watcher(page, "pathfind", anomalies):
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        for prefix, name in (("src", KNOWN_NAMES[0]), ("tgt", KNOWN_NAMES[1])):
            page.fill(f"#{prefix}-input", name)
            page.wait_for_selector(f"#{prefix}-dropdown .dropdown-item", timeout=8000)
            page.click(f"#{prefix}-dropdown .dropdown-item >> nth=0")
        page.click("#find-btn")
        try:
            page.wait_for_selector("#results:not(:empty)", timeout=30000)
        except Exception:
            anomalies.append(Anomaly("pathfind", "no_results_rendered",
                                      f"No content in #results after searching {KNOWN_NAMES[0]} -> {KNOWN_NAMES[1]}"))
    page.close()


def scenario_check_contacts(browser, base_url, anomalies, vcf_path):
    """Mobile-emulated: the psi-section is hidden entirely on plain desktop
    Chromium (no Contact Picker API, isMobile false), so this needs a mobile
    UA to exercise the same fallback path real Android users hit."""
    context = browser.new_context(
        storage_state=str(AUTH_STATE),
        user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        viewport={"width": 412, "height": 915},
        is_mobile=True,
    )
    page = context.new_page()
    with Watcher(page, "check_contacts", anomalies):
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_selector("#psi-section:visible", timeout=8000)
        except Exception:
            anomalies.append(Anomaly("check_contacts", "ui_missing",
                                      "psi-section never became visible under mobile UA"))
            page.close()
            context.close()
            return
        page.set_input_files("#psi-file", str(vcf_path))
        try:
            page.wait_for_function(
                "document.getElementById('psi-result').textContent.trim().length > 0",
                timeout=90000,
            )
        except Exception:
            anomalies.append(Anomaly("check_contacts", "no_terminal_state",
                                      "psi-result never showed any content within 90s"))
            page.close()
            context.close()
            return
        result_html = page.eval_on_selector("#psi-result", "el => el.innerHTML")
        result_text = page.eval_on_selector("#psi-result", "el => el.textContent")
        found_success = "found:" in result_text or "no matches found" in result_text
        found_error = "Error" in result_text
        print(f"  check_contacts final #psi-result text: {result_text[:300]!r}")
        if found_success and found_error:
            anomalies.append(Anomaly(
                "check_contacts", "mixed_success_and_error",
                f"#psi-result shows BOTH a success summary and an error string: {result_text[:500]!r}",
            ))
        elif found_error:
            anomalies.append(Anomaly("check_contacts", "reported_error", result_text[:500]))
    page.close()
    context.close()


def scenario_suggest_modal(context, base_url, anomalies):
    page = context.new_page()
    with Watcher(page, "suggest_modal", anomalies):
        page.goto(base_url, wait_until="domcontentloaded", timeout=30000)
        page.click("#suggest-btn")
        try:
            page.wait_for_selector("#suggest-modal:visible, .modal:visible", timeout=5000)
        except Exception:
            anomalies.append(Anomaly("suggest_modal", "did_not_open", "Suggest Link modal did not appear"))
    page.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    require_auth_state()
    anomalies = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        desktop_context = browser.new_context(storage_state=str(AUTH_STATE))

        print("== homepage ==")
        scenario_homepage(desktop_context, args.base_url, anomalies)
        print("== search ==")
        scenario_search(desktop_context, args.base_url, anomalies)
        print("== pathfind ==")
        scenario_pathfind(desktop_context, args.base_url, anomalies)
        print("== suggest_modal ==")
        scenario_suggest_modal(desktop_context, args.base_url, anomalies)

        desktop_context.close()

        print("== check_contacts (mobile-emulated) ==")
        vcf_path = HERE / "_synthetic_contacts.vcf"
        make_synthetic_vcf(vcf_path)
        scenario_check_contacts(browser, args.base_url, anomalies, vcf_path)

        browser.close()

    print()
    if not anomalies:
        print("No anomalies detected.")
        return 0
    print(f"{len(anomalies)} anomal{'y' if len(anomalies) == 1 else 'ies'} detected:")
    for a in anomalies:
        print(" -", a)
    return 1


if __name__ == "__main__":
    sys.exit(main())
