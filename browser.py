"""
browser.py — Playwright browser with persistent profile.
Fires once per unique URL navigation on the main frame only.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright, Page, Frame

from job_detector import is_job_page, extract_job_content
from job_cleaner import clean_job
from job_analyser import analyse_job, print_report
import os


# ── Config ────────────────────────────────────────────────────────────────────

PROFILE_DIR = str(Path.home() / ".job_scraper_profile")
START_URL   = "about:blank"


# ── Dedup state ───────────────────────────────────────────────────────────────

_last_processed: dict[int, str] = {}   # id(page) → url


# ── Core ──────────────────────────────────────────────────────────────────────

async def on_frame_navigated(frame: Frame, page: Page) -> str | None:
    """
    Fires on every frame navigation.
    Returns a job info string when the page is a job listing, None otherwise.

    - Seek pages:   returns a formatted string built from the structured dict
    - Generic pages: returns the raw extracted text string directly
    """
    if frame != page.main_frame:
        return None

    url = page.url
    if _last_processed.get(id(page)) == url:
        return None
    _last_processed[id(page)] = url

    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass

    url  = page.url
    html = await page.content()

    os.system('cls' if os.name == 'nt' else 'clear')
    print(f"\n[browser] Navigated → {url}")

    if not is_job_page(html, url):
        print("[browser] Not a job page — skipping.")
        return None

    print("[browser] Job page detected ✓")

    result = extract_job_content(html)

    job_text = dict_to_string(result) if isinstance(result, dict) else result

    # ── Step 2: clean + keyword scan ─────────────────────────────────────
    clean = clean_job(job_text)
    print(f"[cleaner] {clean.summary()}")

    if not clean.passed:
        return None   # avoid-keyword hit — discard silently

    # ── Step 3: LLM analysis + profile comparison ────────────────────
    result = analyse_job(clean.cleaned_text)
    if result is not None:
        print_report(result)

    return clean.cleaned_text


def dict_to_string(info: dict) -> str:
    """
    Format a structured job dict into a clean readable string,
    same style as print_job() was producing before.
    """
    fields = [
        ("title",            "Role"),
        ("company",          "Company"),
        ("location",         "Location"),
        ("classification",   "Classification"),
        ("salary",           "Salary"),
        ("job_type",         "Type"),
        ("posted",           "Posted"),
        ("apply_url",        "Apply"),
        ("description",      "Description"),
        ("responsibilities", "Responsibilities"),
        ("requirements",     "Requirements"),
        ("benefits",         "Benefits"),
        ("questions",        "Employer questions"),
    ]

    lines: list[str] = []

    for key, label in fields:
        value = info.get(key)
        if not value:
            continue

        if isinstance(value, list):
            lines.append(f"{label}:")
            for item in value:
                lines.append(f"  • {item}")

        elif key in ("description", "questions"):
            lines.append(f"{label}:")
            for line in value.splitlines():
                lines.append(f"  {line}" if line.strip() else "")

        else:
            lines.append(f"{label}: {value}")

    return "\n".join(lines)


# ── Browser loop ──────────────────────────────────────────────────────────────

async def run() -> None:
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            args=["--start-maximized"],
            no_viewport=True,
        )

        page = context.pages[0] if context.pages else await context.new_page()

        if START_URL != "about:blank":
            await page.goto(START_URL)

        print(f"[browser] Profile: {PROFILE_DIR}")
        print("[browser] Browse normally. Job pages are detected automatically.")
        print("[browser] Close the browser window to exit.\n")

        async def handle_navigation(frame: Frame, p: Page) -> None:
            job_string = await on_frame_navigated(frame, p)
            #if job_string:
            #    print("\n" + "═" * 60)
            #    print(job_string)
            #    print("═" * 60 + "\n")

        def attach_listener(p: Page) -> None:
            p.on(
                "framenavigated",
                lambda frame: asyncio.ensure_future(handle_navigation(frame, p)),
            )

        context.on("page", attach_listener)
        attach_listener(page)

        await page.wait_for_event("close", timeout=0)
        await context.close()


if __name__ == "__main__":
    asyncio.run(run())
