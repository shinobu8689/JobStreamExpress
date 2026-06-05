"""
job_detector.py — Determines whether a page is a job listing,
and extracts the relevant raw text content from it.

extract_job_content() returns:
  - dict  if the page is a known structured board (Seek, etc.)
  - str   for all other pages (passed to job_parser.py)
"""

import re
from bs4 import BeautifulSoup


# ── URL patterns that identify job listing pages ──────────────────────────────

URL_JOB_PATTERNS = [
    r"/jobs?/",
    r"/careers?/",
    r"/position/",
    r"/opening/",
    r"/vacancy/",
    r"/role/",
    r"[?&]jk=",
    r"[?&]jobId=",
    r"[?&]job_id=",
    r"lever\.co/",
    r"greenhouse\.io/",
    r"workday\.com/",
    r"ashbyhq\.com/",
    r"smartrecruiters\.com/",
    r"bamboohr\.com/",
    r"myworkdayjobs\.com/",
    r"careers\.linkedin\.com/",
    r"/jd/",
    r"seek\.com\.au/job/",
    r"au\.indeed\.com/",
]

JOB_CONTENT_SIGNALS = [
    "job description", "responsibilities", "qualifications", "requirements",
    "what you'll do", "what you will do", "about the role", "about this role",
    "we are looking for", "we're looking for", "minimum qualifications",
    "apply now", "apply for this job", "years of experience",
    "salary range", "compensation", "equal opportunity employer",
]

MIN_SIGNALS_REQUIRED = 2


# ── Generic container selectors (non-Seek fallback) ───────────────────────────

JOB_CONTAINER_SELECTORS = [
    "main", "article", "[role='main']",
    ".job-description", ".job-details", ".job-content", ".job-posting",
    "#job-description", "#jobDescriptionText",
    ".jobsearch-jobDescriptionText",           # Indeed
    ".description__text", ".jobs-description", # LinkedIn
    "[data-testid='jobsearch-JobComponent']",
    ".job-view-layout", "#job-details",
    "[class*='jobDescription']", "[class*='JobDescription']",
]

POSTED_RE = re.compile(
    r"Posted\s+(?:\d+[a-z]+\s+ago|today|yesterday)",
    re.IGNORECASE,
)


# ── Public API ─────────────────────────────────────────────────────────────────

def is_job_page(html: str, url: str = "") -> bool:
    """True if the page is a job listing (URL heuristic first, then content signals)."""
    if any(re.search(p, url.lower()) for p in URL_JOB_PATTERNS):
        return True
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(separator=" ").lower()
    return sum(1 for s in JOB_CONTENT_SIGNALS if s in text) >= MIN_SIGNALS_REQUIRED


def extract_job_content(html: str) -> "dict | str":
    """
    Returns a structured dict for known boards (Seek), or raw text for others.
    Callers should check: isinstance(result, dict)
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style", "noscript", "iframe",
                               "nav", "footer", "header"]):
        tag.decompose()

    # Seek
    if soup.find(attrs={"data-automation": "jobAdDetails"}):
        return _extract_seek(soup)

    # Generic fallback → raw text for job_parser.py
    container = None
    for selector in JOB_CONTAINER_SELECTORS:
        try:
            found = soup.select_one(selector)
            if found and len(found.get_text(strip=True)) > 200:
                container = found
                break
        except Exception:
            continue

    if not container:
        container = soup.find("main") or soup.find("article") or soup.find("body") or soup

    lines = _render_element(container)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── Seek structured extractor ──────────────────────────────────────────────────

def _txt(el) -> str:
    return el.get_text(separator=" ", strip=True) if el else ""


def _extract_seek(soup) -> dict:
    """
    Pulls each Seek field via its stable data-automation attribute.
    Returns a dict matching the shape job_parser.py would produce,
    so browser.py can print it directly without further parsing.
    """
    def grab(attr: str) -> str:
        return _txt(soup.find(attrs={"data-automation": attr}))

    salary = grab("job-detail-add-expected-salary")
    # Seek shows a placeholder when salary isn't listed — scan for an actual figure
    if not salary or "add expected" in salary.lower():
        salary = ""
        for span in soup.find_all("span"):
            t = _txt(span)
            if re.search(r"\$[\d,]|per year|per hour|p\.a\.", t, re.I) and len(t) < 80:
                salary = t
                break

    posted = ""
    for node in soup.find_all(string=POSTED_RE):
        posted = node.strip()
        break

    # Job description body → render to clean text
    desc_lines: list[str] = []
    desc_el = soup.find(attrs={"data-automation": "jobAdDetails"})
    if desc_el:
        desc_lines = _render_element(desc_el)

    # Employer questions
    q_lines: list[str] = []
    q_el = soup.find(attrs={"data-automation": "employerQuestions"})
    if q_el:
        q_lines = _render_element(q_el)

    description = re.sub(r"\n{3,}", "\n\n", "\n".join(desc_lines)).strip()
    questions   = re.sub(r"\n{3,}", "\n\n", "\n".join(q_lines)).strip()

    return {
        "title":          grab("job-detail-title"),
        "company":        grab("advertiser-name"),
        "location":       grab("job-detail-location"),
        "classification": grab("job-detail-classifications"),
        "job_type":       grab("job-detail-work-type"),
        "salary":         salary,
        "posted":         posted,
        "description":    description,
        "questions":      questions,
        # These would need separate parsing of the description body;
        # left empty so the dict shape stays consistent with job_parser output
        "requirements":     [],
        "responsibilities": [],
        "benefits":         [],
    }


# ── DOM renderer ───────────────────────────────────────────────────────────────

BLOCK_TAGS = {
    "p", "div", "section", "article", "aside",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "dt", "dd", "blockquote", "pre",
    "tr", "td", "th", "caption", "figcaption",
}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _render_element(el) -> list[str]:
    """Walk direct children only; each text node emitted exactly once."""
    from bs4 import NavigableString, Tag

    lines: list[str] = []

    for child in el.children:
        if isinstance(child, NavigableString):
            text = child.strip()
            if text:
                lines.append(text)

        elif isinstance(child, Tag):
            name = child.name

            if name in HEADING_TAGS:
                text = child.get_text(separator=" ", strip=True)
                if text:
                    lines += ["", text, ""]

            elif name == "li":
                text = child.get_text(separator=" ", strip=True)
                if text:
                    lines.append(f"• {text}")

            elif name == "br":
                lines.append("")

            elif name == "a":
                text = child.get_text(separator=" ", strip=True)
                href = child.get("href", "")
                lines.append(f"[{text}]({href})" if (text and href) else text)

            elif name in BLOCK_TAGS:
                inner = _render_element(child)
                if any(l.strip() for l in inner):
                    lines += [""] + inner + [""]

            else:
                lines.extend(_render_element(child))

    # Collapse consecutive blanks
    collapsed: list[str] = []
    prev_blank = False
    for line in lines:
        blank = not line.strip()
        if blank and prev_blank:
            continue
        collapsed.append(line)
        prev_blank = blank

    return collapsed