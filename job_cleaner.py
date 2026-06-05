"""
job_cleaner.py — Step 2: tidy up raw job text before handing to LLM.

Pipeline:
  1. Strip boilerplate sections (company culture, diversity, about us, etc.)
  2. Strip fluff lines (generic corporate filler)
  3. Scan for avoid-keywords from user config — bail early if found
  4. Extract years-of-experience requirements (regex, fast pre-LLM filter)
  5. Return a CleanResult with the purified text and scan findings

Public API:
  clean_job(text: str) -> CleanResult
"""

import re
from dataclasses import dataclass, field
from pathlib import Path


# ── Config paths ──────────────────────────────────────────────────────────────

CONFIG_DIR = Path(__file__).parent / "config"

# Each file is a newline-separated list of lowercase terms.
# Create these files to customise filtering — defaults are used if missing.
AVOID_KEYWORDS_FILE  = CONFIG_DIR / "avoid_keywords.txt"
FLUFF_KEYWORDS_FILE  = CONFIG_DIR / "fluff_keywords.txt"


# ── Built-in defaults (used if config files are absent) ───────────────────────

DEFAULT_AVOID_KEYWORDS = [
    # Seniority — edit to taste
    "senior", "lead", "principal", "staff engineer",
    "head of", "vp ", "director", "manager",
    "10+ years", "10 years", "8+ years", "8 years",
]

DEFAULT_FLUFF_KEYWORDS = [
    # Generic corporate filler that adds no signal for LLM matching
    "equal opportunity employer",
    "we celebrate diversity",
    "we are committed to",
    "inclusive workplace",
    "all qualified applicants",
    "regardless of race",
    "regardless of gender",
    "regardless of age",
    "privacy policy",
    "cookie policy",
    "terms of service",
    "by applying you",
    "only shortlisted",
    "only successful candidates",
    "thank you for your interest",
    "due to the volume",
    "right to work in australia",
    "must be an australian",
    "applicants must have",          # too vague on its own — pair with work rights context
]

# Sections whose headings signal the start of company-promo content.
# Everything from this heading onward is dropped.
SECTION_CUTOFF_HEADERS = [
    "about us",
    "who we are",
    "our story",
    "our mission",
    "our values",
    "our culture",
    "life at",
    "why join us",
    "why work with us",
    "what we stand for",
    "diversity",
    "inclusion",
    "belonging",
    "perks and benefits",    # keep "benefits" alone — it's useful; full phrase is promo
]


# ── Experience extraction ─────────────────────────────────────────────────────

_WORD_NUM   = r"(?:one|two|three|four|five|six|seven|eight|nine|ten)"
_DIGIT      = r"\d+"
_NUMBER     = rf"(?:{_DIGIT}|{_WORD_NUM})"
_RANGE      = rf"(?P<min>{_NUMBER})\s*[-–to]+\s*(?P<max>{_NUMBER})\+?"
_SINGLE     = rf"(?P<single>{_NUMBER})\+?"
_EXP_PATTERN = re.compile(
    rf"((?:{_RANGE}|{_SINGLE})\s+years?\s*(?:of\s+)?(?:\w+\s+){{0,4}}?experience)",
    re.IGNORECASE | re.VERBOSE,
)

_WORD_TO_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _to_num(val: str | None) -> int | None:
    if not val:
        return None
    val = val.strip().lower()
    return int(val) if val.isdigit() else _WORD_TO_NUM.get(val)


def extract_experience(text: str) -> list[dict]:
    """
    Returns a list of dicts: {phrase, min_years, max_years}
    e.g. "2-4 years of experience" → {min_years: 2, max_years: 4}
         "3+ years experience"     → {min_years: 3, max_years: None}
    """
    results = []
    for m in _EXP_PATTERN.finditer(text):
        results.append({
            "phrase":    m.group(0).strip(),
            "min_years": _to_num(m.group("min") or m.group("single")),
            "max_years": _to_num(m.group("max") if "max" in m.groupdict() else None),
        })
    return results


# ── Config loader ─────────────────────────────────────────────────────────────

def _load_list(path: Path, default: list[str]) -> list[str]:
    """Load a newline-separated keyword file; fall back to default if missing."""
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
        return [l.strip().lower() for l in lines if l.strip() and not l.startswith("#")]
    return [k.lower() for k in default]


def load_config() -> tuple[list[str], list[str]]:
    """Returns (avoid_keywords, fluff_keywords) from config files or defaults."""
    return (
        _load_list(AVOID_KEYWORDS_FILE,  DEFAULT_AVOID_KEYWORDS),
        _load_list(FLUFF_KEYWORDS_FILE,  DEFAULT_FLUFF_KEYWORDS),
    )


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class CleanResult:
    passed:          bool          # False = hit an avoid-keyword, discard this job
    avoid_hits:      list[str]     # which avoid-keywords triggered (if any)
    experience:      list[dict]    # extracted experience requirements
    cleaned_text:    str           # purified text ready for LLM
    removed_sections: list[str]    # section headers that were cut (debug info)
    removed_lines:   int           # count of fluff lines removed (debug info)

    def summary(self) -> str:
        """One-line status string for terminal output."""
        if not self.passed:
            return f"⚠  AVOID keyword hit: {self.avoid_hits}"
        exp_str = ""
        if self.experience:
            ranges = []
            for e in self.experience:
                if e["max_years"]:
                    ranges.append(f"{e['min_years']}–{e['max_years']}yrs")
                else:
                    ranges.append(f"{e['min_years']}+yrs")
            exp_str = f" | exp: {', '.join(ranges)}"
        return f"✓  Clean ({len(self.cleaned_text)} chars{exp_str})"


# ── Core cleaning pipeline ────────────────────────────────────────────────────

def clean_job(text: str,
              avoid_keywords: list[str] | None = None,
              fluff_keywords: list[str] | None = None) -> CleanResult:
    """
    Full cleaning pipeline for a raw job string.

    Args:
        text:             Raw job text from on_frame_navigated() or extract_job_content()
        avoid_keywords:   Override the config-file list for this call
        fluff_keywords:   Override the config-file list for this call

    Returns:
        CleanResult — check .passed before proceeding to LLM
    """
    cfg_avoid, cfg_fluff = load_config()
    avoid_kw = [k.lower() for k in (avoid_keywords or cfg_avoid)]
    fluff_kw = [k.lower() for k in (fluff_keywords or cfg_fluff)]

    # ── 1. Avoid-keyword scan ─────────────────────────────────────────────────
    #
    # Seniority keywords (e.g. "senior", "manager") are ONLY checked against
    # the header zone — the first few lines where Role/Title/Type live.
    # Scanning the full description would false-positive on lines like
    # "liaise with senior stakeholders" or "report to the manager", which are
    # normal duties in a junior role, not indicators of seniority.
    #
    # Experience-threshold keywords (e.g. "8+ years") are checked against the
    # full text but with word-boundary matching so "8 years" doesn't hit "18 years".
    #
    # The header zone is defined as lines before the first blank line after
    # the opening block — typically Role / Company / Location / Type / Salary.

    lines_for_scan = text.splitlines()

    # Build header zone: take lines until we hit the description body.
    # Heuristic: stop at the first line that starts with a bullet or is very long.
    header_zone_lines = []
    for ln in lines_for_scan:
        stripped = ln.strip()
        if stripped.startswith("•") or len(stripped) > 120:
            break
        header_zone_lines.append(stripped)
        if len(header_zone_lines) >= 12:   # cap at 12 lines — plenty for header fields
            break

    header_zone = "\n".join(header_zone_lines).lower()

    hits: list[str] = []

    for kw in avoid_kw:
        kw_lower = kw.lower()
        # Experience thresholds contain digits — scan full text, word-boundary aware
        if re.search(r"\d", kw_lower):
            pattern = rf"(?<!\d){re.escape(kw_lower)}(?!\d)"
            if re.search(pattern, text.lower()):
                hits.append(kw)
        else:
            # Seniority / role words — header zone only, word-boundary match
            pattern = rf"\b{re.escape(kw_lower)}\b"
            if re.search(pattern, header_zone):
                hits.append(kw)

    if hits:
        return CleanResult(
            passed=False,
            avoid_hits=hits,
            experience=[],
            cleaned_text="",
            removed_sections=[],
            removed_lines=0,
        )

    # ── 2. Section cutoff ─────────────────────────────────────────────────────
    # Walk lines; when a line matches a cutoff header, drop it and everything after.
    lines = lines_for_scan
    cutoff_idx = len(lines)
    removed_sections: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip().lower().rstrip(":").rstrip()
        if any(hdr in stripped for hdr in SECTION_CUTOFF_HEADERS):
            # Make sure it looks like a heading (short, not mid-sentence)
            if len(stripped) < 60:
                cutoff_idx = i
                removed_sections.append(line.strip())
                break

    lines = lines[:cutoff_idx]

    # ── 3. Fluff line removal ─────────────────────────────────────────────────
    # Remove any line that contains a fluff phrase.
    clean_lines = []
    removed_count = 0
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in fluff_kw):
            removed_count += 1
        else:
            clean_lines.append(line)

    # ── 4. Collapse excessive blank lines left by removals ────────────────────
    collapsed: list[str] = []
    prev_blank = False
    for line in clean_lines:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        collapsed.append(line)
        prev_blank = is_blank

    cleaned = "\n".join(collapsed).strip()

    # ── 5. Experience extraction (on cleaned text) ────────────────────────────
    experience = extract_experience(cleaned)

    return CleanResult(
        passed=True,
        avoid_hits=[],
        experience=experience,
        cleaned_text=cleaned,
        removed_sections=removed_sections,
        removed_lines=removed_count,
    )