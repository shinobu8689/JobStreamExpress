"""
browser.py — Playwright browser with persistent profile.

Navigation flow:
  framenavigated → fast check (dedup, already-processed) → push to queue
  worker()       → pulls from queue, runs full pipeline, prints report

The browser stays fully responsive while the worker processes jobs in the
background. Multiple clicks queue up and are handled one at a time.
"""

import asyncio
import os
from pathlib import Path
from playwright.async_api import async_playwright, Page, Frame

from job_detector import is_job_page, extract_job_content
from job_cleaner import clean_job
from job_analyser import analyse_job, print_report, report_exists
from skill_registry import startup_update


# ── Config ────────────────────────────────────────────────────────────────────

PROFILE_DIR  = str(Path.home() / ".job_scraper_profile")
START_URL    = "about:blank"
GLANCE_SECS  = 3     # seconds to display "queued" message before clearing


# ── Shared state ──────────────────────────────────────────────────────────────

_last_processed: dict[int, str] = {}   # id(page) → last url seen
_job_queue: asyncio.Queue                # url → pipeline worker


# ── Navigation handler (fast path — never blocks) ─────────────────────────────

async def on_frame_navigated(frame: Frame, page: Page) -> None:
    """
    Fires on every frame navigation. Does only fast, cheap checks then
    either discards or pushes the URL to the worker queue.
    Never awaits the pipeline — returns immediately.
    """
    if frame != page.main_frame:
        return

    url = page.url

    # Dedup — same URL as last navigation on this tab
    if _last_processed.get(id(page)) == url:
        return
    _last_processed[id(page)] = url

    # Wait for the page to settle before grabbing HTML
    try:
        await page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass

    # Re-read URL after settle (handles redirects)
    url  = page.url
    html = await page.content()

    os.system("cls" if os.name == "nt" else "clear")
    print(f"\n[browser] Navigated → {url}")

    # Already have a saved report for this URL — skip entirely
    exists, existing_folder = report_exists(url)
    if exists:
        print(f"[browser] Already processed → reports/{existing_folder}/ — skipping.")
        return

    # Fast content check — no LLM involved
    if not is_job_page(html, url):
        print("[browser] Not a job page — skipping.")
        return

    # Push to queue and give a quick glance message
    await _job_queue.put((url, html))
    queue_size = _job_queue.qsize()
    print(f"[browser] Job page queued ✓  (queue depth: {queue_size})")
    print(f"[browser] Processing in background — you can keep browsing.\n")


# ── Worker (runs for the lifetime of the browser) ─────────────────────────────

async def pipeline_worker() -> None:
    """
    Pulls (url, html) pairs from the queue one at a time and runs the
    full pipeline. Restarts itself on unexpected errors so the queue
    never silently stalls.
    """
    print("[worker] Started — waiting for jobs.")
    while True:
        url, html = await _job_queue.get()

        try:
            os.system("cls" if os.name == "nt" else "clear")
            print(f"\n[worker] Processing → {url}\n")

            # ── Step 1: extract ───────────────────────────────────────────────
            raw      = extract_job_content(html)
            job_text = dict_to_string(raw) if isinstance(raw, dict) else raw

            # ── Step 2: clean + keyword scan ──────────────────────────────────
            clean = clean_job(job_text)
            print(f"[cleaner] {clean.summary()}")

            if not clean.passed:
                print(f"[worker] Discarded — avoid keyword hit.\n")
                continue

            # ── Step 3: LLM analysis + report ─────────────────────────────────
            result = analyse_job(clean.cleaned_text)
            if result is not None:
                print_report(result, url=url)

            # Pause so you can read the report before the next job clears the screen
            remaining = _job_queue.qsize()
            if remaining:
                print(f"[worker] {remaining} job(s) still queued. "
                      f"Continuing in {GLANCE_SECS}s...\n")
                await asyncio.sleep(GLANCE_SECS)

        except Exception as e:
            import traceback
            print(f"[worker] ✗ Pipeline error: {e}")
            traceback.print_exc()

        finally:
            _job_queue.task_done()


# ── Helpers ───────────────────────────────────────────────────────────────────

def dict_to_string(info: dict) -> str:
    """Format a Seek structured dict into plain text for the pipeline."""
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
    global _job_queue
    _job_queue = asyncio.Queue()

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

        startup_update()
        print(f"[browser] Profile: {PROFILE_DIR}")
        print(f"[browser] Queue glance delay: {GLANCE_SECS}s")
        print("[browser] Browse normally — job pages are queued automatically.")
        print("[browser] Close the browser window to exit.\n")

        # Start the background worker — wrap in a supervisor that restarts on crash
        async def supervised_worker():
            while True:
                try:
                    await pipeline_worker()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    import traceback
                    print(f"[worker] ✗ Worker crashed: {e} — restarting in 2s...")
                    traceback.print_exc()
                    await asyncio.sleep(2)

        worker_task = asyncio.create_task(supervised_worker())

        def attach_listener(p: Page) -> None:
            p.on(
                "framenavigated",
                lambda frame: asyncio.ensure_future(on_frame_navigated(frame, p)),
            )

        context.on("page", attach_listener)
        attach_listener(page)

        await page.wait_for_event("close", timeout=0)

        # Clean shutdown — let the worker finish current job
        worker_task.cancel()
        await context.close()


if __name__ == "__main__":
    asyncio.run(run())
