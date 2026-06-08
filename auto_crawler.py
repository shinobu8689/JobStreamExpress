"""
auto_crawler.py — Automated job page crawler with anti-bot measures.

Anti-bot measures applied:
  - Persistent browser profile (real cookies, session history)
  - Randomised delays between every action
  - Human-like mouse movement to job cards before clicking
  - Random scroll behaviour on search results page
  - Randomised viewport size per session
  - Stealth JS patches (navigator.webdriver removed, plugins spoofed)
  - Realistic User-Agent rotation
  - Tab open/close timing randomisation
  - Random chance of "idle" pauses simulating reading

Usage:
    python auto_crawler.py "https://au.seek.com/jobs?keywords=IT+Support&location=Melbourne"
    python auto_crawler.py "https://au.seek.com/jobs?keywords=IT+Support" --pages 5
    python auto_crawler.py "https://au.seek.com/jobs?keywords=IT+Support" --delay 4 --headless
"""

import asyncio
import argparse
import os
import random
import sys
from pathlib import Path
from playwright.async_api import async_playwright, Page, BrowserContext

from job_detector import is_job_page, extract_job_content
from job_cleaner import clean_job
from job_analyser import analyse_job, print_report, report_exists
from skill_registry import startup_update


# ── Config ────────────────────────────────────────────────────────────────────

PROFILE_DIR = str(Path.home() / ".job_scraper_profile")
GLANCE_SECS = 3

SEEK_JOB_CARD     = "[data-automation='jobTitle']"
SEEK_NEXT_PAGE    = "[data-automation='page-next']"
SEEK_RESULT_COUNT = "[data-automation='totalJobsCount']"

# Delay ranges (seconds) — all actual waits are randint/uniform within these
DELAY_BETWEEN_JOBS   = (2.5, 6.0)    # between opening job tabs
DELAY_PAGE_TURN      = (3.0, 7.0)    # after clicking next page
DELAY_BEFORE_SCROLL  = (0.5, 2.0)    # before scrolling on results page
DELAY_SCROLL_STEP    = (0.1, 0.4)    # between individual scroll steps
DELAY_IDLE_CHANCE    = 0.15          # probability of a longer idle pause
DELAY_IDLE_RANGE     = (8.0, 20.0)   # how long the idle pause lasts

# Viewport sizes — pick one randomly per session to avoid fingerprinting
VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0",
]

# Injected into every page to remove webdriver fingerprint signals
STEALTH_JS = """
() => {
    // Remove navigator.webdriver flag
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // Spoof plugins to look like a real browser
    Object.defineProperty(navigator, 'plugins', {
        get: () => [
            { name: 'Chrome PDF Plugin' },
            { name: 'Chrome PDF Viewer' },
            { name: 'Native Client' },
        ],
    });

    // Spoof languages
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-AU', 'en-US', 'en'],
    });

    // Remove automation-related chrome properties
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;
}
"""


# ── Shared queue ──────────────────────────────────────────────────────────────

_job_queue: asyncio.Queue


# ── Human-like interaction helpers ───────────────────────────────────────────

async def human_delay(min_s: float = 0.5, max_s: float = 1.5) -> None:
    """Sleep for a random duration to simulate human reaction time."""
    await asyncio.sleep(random.uniform(min_s, max_s))


async def human_scroll(page: Page, direction: str = "down",
                        steps: int | None = None) -> None:
    """
    Scroll the page in small random increments, like a human reading.
    direction: "down" or "up"
    """
    if steps is None:
        steps = random.randint(3, 8)

    await human_delay(*DELAY_BEFORE_SCROLL)

    for _ in range(steps):
        delta = random.randint(80, 300) * (1 if direction == "down" else -1)
        await page.mouse.wheel(0, delta)
        await human_delay(*DELAY_SCROLL_STEP)


async def human_mouse_move(page: Page, element) -> None:
    """
    Move the mouse to an element via a slightly randomised path
    rather than teleporting directly to it.
    """
    box = await element.bounding_box()
    if not box:
        return

    # Target slightly inside the element with a small random offset
    target_x = box["x"] + box["width"]  * random.uniform(0.3, 0.7)
    target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

    # Move in two hops with a slight curve via a midpoint
    mid_x = target_x + random.randint(-80, 80)
    mid_y = target_y + random.randint(-40, 40)

    await page.mouse.move(mid_x, mid_y)
    await human_delay(0.05, 0.2)
    await page.mouse.move(target_x, target_y)
    await human_delay(0.05, 0.15)


async def maybe_idle() -> None:
    """Occasionally simulate a longer "reading" pause."""
    if random.random() < DELAY_IDLE_CHANCE:
        idle = random.uniform(*DELAY_IDLE_RANGE)
        print(f"  [crawler] Idle pause ({idle:.1f}s) ...")
        await asyncio.sleep(idle)


# ── Stealth setup ─────────────────────────────────────────────────────────────

async def apply_stealth(page: Page) -> None:
    """Inject stealth patches into the page before any navigation."""
    await page.add_init_script(STEALTH_JS)


# ── Pipeline worker (same as browser.py) ─────────────────────────────────────

def dict_to_string(info: dict) -> str:
    fields = [
        ("title",            "Role"),
        ("company",          "Company"),
        ("location",         "Location"),
        ("classification",   "Classification"),
        ("salary",           "Salary"),
        ("job_type",         "Type"),
        ("posted",           "Posted"),
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


async def pipeline_worker() -> None:
    print("[worker] Started — waiting for jobs.")
    while True:
        url, html = await _job_queue.get()
        try:
            os.system("cls" if os.name == "nt" else "clear")
            print(f"\n[worker] Processing → {url}\n")

            raw      = extract_job_content(html)
            job_text = dict_to_string(raw) if isinstance(raw, dict) else raw

            clean = clean_job(job_text)
            print(f"[cleaner] {clean.summary()}")

            if not clean.passed:
                print("[worker] Discarded — avoid keyword hit.\n")
                continue

            result = analyse_job(clean.cleaned_text)
            if result is not None:
                print_report(result, url=url)

            remaining = _job_queue.qsize()
            if remaining:
                print(f"[worker] {remaining} job(s) still queued. "
                      f"Continuing in {GLANCE_SECS}s...\n")
                await asyncio.sleep(GLANCE_SECS)

        except Exception as e:
            import traceback
            print(f"[worker] ✗ Error: {e}")
            traceback.print_exc()
        finally:
            _job_queue.task_done()


# ── Job tab fetcher ───────────────────────────────────────────────────────────

async def fetch_job_tab(context: BrowserContext, url: str) -> None:
    """
    Open a job in a new tab with human-like behaviour.
    Grabs the rendered HTML and pushes it to the queue, then closes.
    """
    exists, folder = report_exists(url)
    if exists:
        print(f"  [crawler] Already processed ({folder}) — skipping.")
        return

    tab = await context.new_page()
    await apply_stealth(tab)

    try:
        # Randomise viewport slightly per tab
        vp = random.choice(VIEWPORTS)
        await tab.set_viewport_size(vp)

        await tab.goto(url, wait_until="domcontentloaded", timeout=25_000)
        try:
            await tab.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        # Simulate reading the page
        await human_scroll(tab, direction="down", steps=random.randint(2, 5))
        await maybe_idle()

        html = await tab.content()

        if not is_job_page(html, url):
            print(f"  [crawler] Not a job page — {url}")
            return

        await _job_queue.put((url, html))
        print(f"  [crawler] Queued ✓  (depth: {_job_queue.qsize()})  {url}")

    except Exception as e:
        print(f"  [crawler] ✗ Failed {url}: {e}")
    finally:
        # Small delay before closing, like a human switching tabs
        await human_delay(0.8, 2.0)
        await tab.close()


# ── Main crawl loop ───────────────────────────────────────────────────────────

async def crawl(search_url: str, max_pages: int, base_delay: float,
                headless: bool) -> None:
    global _job_queue
    _job_queue = asyncio.Queue()

    ua = random.choice(USER_AGENTS)
    vp = random.choice(VIEWPORTS)

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=headless,
            args=[
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",  # key stealth flag
                f"--user-agent={ua}",
            ],
            no_viewport=True,
        )

        # Start supervised background worker
        async def supervised_worker():
            while True:
                try:
                    await pipeline_worker()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    import traceback
                    print(f"[worker] Crashed: {e} — restarting in 2s...")
                    traceback.print_exc()
                    await asyncio.sleep(2)

        worker_task = asyncio.create_task(supervised_worker())
        startup_update()

        page = context.pages[0] if context.pages else await context.new_page()
        await apply_stealth(page)
        await page.set_viewport_size(vp)

        print(f"[crawler] UA: {ua[:60]}...")
        print(f"[crawler] Viewport: {vp['width']}x{vp['height']}")

        await page.goto(search_url, wait_until="domcontentloaded")
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        # Initial human-like scroll on the results page
        await human_scroll(page, steps=random.randint(2, 4))

        try:
            count_el = await page.query_selector(SEEK_RESULT_COUNT)
            if count_el:
                print(f"\n[crawler] Results: {(await count_el.inner_text()).strip()}")
        except Exception:
            pass

        for page_num in range(1, max_pages + 1):
            print(f"\n[crawler] ── Page {page_num} / {max_pages} ──")

            try:
                await page.wait_for_selector(SEEK_JOB_CARD, timeout=10_000)
            except Exception:
                print("[crawler] No job cards found — stopping.")
                break

            cards = await page.query_selector_all(SEEK_JOB_CARD)
            urls: list[str] = []
            for card in cards:
                href = await card.get_attribute("href")
                if href:
                    if href.startswith("/"):
                        href = "https://au.seek.com" + href
                    # Strip tracking fragments — keep only clean job URL
                    href = href.split("#")[0]
                    urls.append(href)

            # Shuffle slightly so crawl order isn't perfectly sequential
            random.shuffle(urls)
            print(f"[crawler] {len(urls)} cards found.")

            for i, url in enumerate(urls):
                # Move mouse to a job card before opening — looks more human
                try:
                    card_el = cards[i] if i < len(cards) else None
                    if card_el:
                        await human_mouse_move(page, card_el)
                except Exception:
                    pass

                await fetch_job_tab(context, url)

                # Randomised delay between jobs, scaled by the base_delay arg
                jitter = random.uniform(
                    DELAY_BETWEEN_JOBS[0] * (base_delay / 2),
                    DELAY_BETWEEN_JOBS[1] * (base_delay / 2),
                )
                await asyncio.sleep(jitter)

            # Scroll back up before clicking next page (human behaviour)
            await human_scroll(page, direction="up", steps=random.randint(1, 3))
            await human_delay(1.0, 2.5)

            next_btn = await page.query_selector(SEEK_NEXT_PAGE)
            if not next_btn:
                print("[crawler] No next page — crawl complete.")
                break
            if await next_btn.get_attribute("disabled") is not None:
                print("[crawler] Next page disabled — crawl complete.")
                break

            await human_mouse_move(page, next_btn)
            await human_delay(0.3, 0.8)
            await next_btn.click()

            await page.wait_for_load_state("networkidle", timeout=12_000)
            await human_delay(*DELAY_PAGE_TURN)

        print(f"\n[crawler] All pages done. Queue draining "
              f"({_job_queue.qsize()} remaining)...")
        await _job_queue.join()
        print("[crawler] Done.\n")

        worker_task.cancel()
        await context.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Auto-crawl Seek job listings with anti-bot measures."
    )
    parser.add_argument("url",        help="Seek search URL to crawl")
    parser.add_argument("--pages",    type=int,   default=10,  help="Max pages (default 10)")
    parser.add_argument("--delay",    type=float, default=2.0, help="Base delay multiplier (default 2.0)")
    parser.add_argument("--headless", action="store_true",     help="Run headless")
    args = parser.parse_args()

    print(f"[crawler] URL:      {args.url}")
    print(f"[crawler] Pages:    {args.pages}")
    print(f"[crawler] Delay:    {args.delay}x")
    print(f"[crawler] Headless: {args.headless}\n")

    asyncio.run(crawl(args.url, args.pages, args.delay, args.headless))


if __name__ == "__main__":
    main()
