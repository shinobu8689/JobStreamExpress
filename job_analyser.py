"""
job_analyser.py — Step 3: LLM analysis pipeline.

Flow:
  analyse_job(text) -> AnalysisResult
    1. Send cleaned job text to local Ollama LLM with analyse prompt
    2. Parse LLM JSON response into structured data
    3. Load user profile (skills + interests)
    4. Normalise skill strings via alias map
    5. Compare job skills against user profile
    6. Build + print + save report

Config:
  config/llm_config.txt       — model name, ollama URL
  config/profile.json         — user skills, interests, projects
  config/skill_aliases.json   — canonical skill names + aliases
  prompts/analyse.txt         — LLM prompt template (uses {text} placeholder)

Reports saved to:
  reports/strong_match/   score >= 75%
  reports/possible/       score >= 50%
  reports/no_match/       everything else (avoids re-running LLM on revisit)
"""

import json
import re
import requests
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from skill_registry import record_skills


# ── Config paths ──────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).parent
CONFIG_DIR  = BASE_DIR / "config"
PROMPT_DIR  = BASE_DIR / "prompts"

LLM_CONFIG_FILE  = CONFIG_DIR / "llm_config.txt"
PROFILE_FILE     = CONFIG_DIR / "profile.json"
ALIASES_FILE     = CONFIG_DIR / "skill_aliases.json"
ANALYSE_PROMPT   = PROMPT_DIR / "analyse.txt"
ANALYSE_PROMPT_JA = PROMPT_DIR / "analyse_ja.txt"

DEFAULT_MODEL      = "gemma3:12b"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MAX_TOKENS = 4096


# ── Skill normaliser ──────────────────────────────────────────────────────────

class SkillNormaliser:
    """
    Loads skill_aliases.json once at startup and maps any alias
    back to its canonical name. Transparent for unknown skills.

    Usage:
        norm = SkillNormaliser()
        norm.normalise("Win10")      # → "windows"
        norm.normalise("Python 3")   # → "python"
        norm.normalise("unknownthing") # → "unknownthing"
    """

    def __init__(self) -> None:
        self._map: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not ALIASES_FILE.exists():
            print("[normaliser] ⚠  skill_aliases.json not found — normalisation disabled.")
            return
        raw   = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
        count = 0
        for canonical, aliases in raw.items():
            if canonical.startswith("_"):
                continue
            canon_lower = canonical.lower().strip()
            self._map[canon_lower] = canon_lower
            for alias in aliases:
                self._map[alias.lower().strip()] = canon_lower
                count += 1
        print(f"[normaliser] Loaded {len(raw)} canonical skills, {count} aliases.")

    def normalise(self, skill: str) -> str:
        return self._map.get(skill.lower().strip(), skill.lower().strip())

    def normalise_set(self, skills: list[str]) -> set[str]:
        return {self.normalise(s) for s in skills}


# Module-level singleton — loaded once on first import
_normaliser: SkillNormaliser | None = None

def get_normaliser() -> SkillNormaliser:
    global _normaliser
    if _normaliser is None:
        _normaliser = SkillNormaliser()
    return _normaliser


# ── Config / profile loaders ──────────────────────────────────────────────────

def load_llm_config() -> dict:
    defaults = {
        "model":      DEFAULT_MODEL,
        "url":        DEFAULT_OLLAMA_URL,
        "max_tokens": DEFAULT_MAX_TOKENS,
    }
    if not LLM_CONFIG_FILE.exists():
        return defaults
    for line in LLM_CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key in defaults:
            defaults[key] = int(val) if key == "max_tokens" else val
    return defaults


def load_profile() -> dict:
    if not PROFILE_FILE.exists():
        print(f"[analyser] ⚠  profile.json not found at {PROFILE_FILE}")
        return {"skills": [], "interests": [], "projects": []}
    return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))


def load_prompt(path: Path, **kwargs) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").format(**kwargs)


# ── LLM call ──────────────────────────────────────────────────────────────────

def call_ollama(prompt: str, cfg: dict) -> str:
    try:
        resp = requests.post(
            cfg["url"],
            json={
                "model":   cfg["model"],
                "prompt":  prompt,
                "stream":  False,
                "options": {"num_predict": int(cfg["max_tokens"])},
            },
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json()["response"]
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            f"Cannot reach Ollama at {cfg['url']}. Is it running? → `ollama serve`"
        )


def parse_json_response(raw: str) -> dict:
    # Strategy 1 — fenced ```json``` block
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 2 — bare { } object
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Strategy 3 — orphaned fields without wrapping braces
    stripped = raw.strip().strip(",")
    if stripped and not stripped.startswith("{"):
        try:
            return json.loads("{" + stripped + "}")
        except json.JSONDecodeError:
            pass

    print(f"[analyser] ✗  Full LLM response:\n{'─'*40}\n{raw}\n{'─'*40}")
    raise ValueError("Could not parse JSON from LLM response (see above)")


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class AnalysisResult:
    raw:              dict
    matched_skills:   list[str]
    missing_skills:   list[str]
    matched_optional: list[str]
    interest_hits:    list[str]
    profile:          dict


# ── Core analysis pipeline ────────────────────────────────────────────────────

def _save_temp(label: str, text: str, url: str = "") -> None:
    """Save a debug file to temp/ — label is used as the filename prefix."""
    temp_dir = BASE_DIR / "temp"
    temp_dir.mkdir(exist_ok=True)
    slug = re.sub(r"[^\w\-]", "_", url)[-60:] if url else "latest"
    path = temp_dir / f"{label}_{slug}.txt"
    path.write_text(text, encoding="utf-8")
    print(f"[analyser] {label:10s} → temp/{path.name}  ({len(text)} chars)")


def analyse_job(cleaned_text: str, lang: str = "en", url: str = "") -> AnalysisResult | None:
    cfg     = load_llm_config()
    profile = load_profile()
    norm    = get_normaliser()

    # Truncate input to prevent context-window overflow.
    # Japanese characters are information-dense; 5000 chars covers a full job post.
    max_chars = 5000 if lang == "ja" else 8000
    if len(cleaned_text) > max_chars:
        cleaned_text = cleaned_text[:max_chars]
        print(f"[analyser] ⚠  Input truncated to {max_chars} chars")

    prompt_file = ANALYSE_PROMPT_JA if lang == "ja" else ANALYSE_PROMPT
    lang_tag    = "ja 🇯🇵" if lang == "ja" else "en"
    print(f"[analyser] ⟲  Calling {cfg['model']} (lang={lang_tag})...")
    try:
        prompt  = load_prompt(prompt_file, text=cleaned_text)
        _save_temp("prompt", prompt, url)
        raw_llm = call_ollama(prompt, cfg)
    except Exception as e:
        print(f"[analyser] ✗  LLM call failed: {e}")
        return None

    _save_temp("llm", raw_llm, url)

    try:
        data = parse_json_response(raw_llm)
    except ValueError as e:
        print(f"[analyser] ✗  JSON parse failed: {e}")
        return None

    user_skills  = norm.normalise_set(profile.get("skills", []))
    job_required = [norm.normalise(s) for s in (data.get("skills") or [])]
    job_optional = [norm.normalise(s) for s in (data.get("optional_skills") or [])]

    matched_skills   = [s for s in job_required if s in user_skills]
    missing_skills   = [s for s in job_required if s not in user_skills]
    matched_optional = [s for s in job_optional if s in user_skills]

    # Record all skills seen for the registry
    record_skills(list(set(job_required + job_optional)))

    # Interest match against title + responsibilities + company focus
    job_text_lower = " ".join(filter(None, [
        data.get("title") or "",
        data.get("company_focus") or "",
        " ".join(data.get("responsibilities") or []),
    ])).lower()
    user_interests = [i.lower().strip() for i in profile.get("interests", [])]
    interest_hits  = [i for i in user_interests if i in job_text_lower]

    return AnalysisResult(
        raw=data,
        matched_skills=matched_skills,
        missing_skills=missing_skills,
        matched_optional=matched_optional,
        interest_hits=interest_hits,
        profile=profile,
    )


# ── Report helpers ────────────────────────────────────────────────────────────

def _score_label(result: AnalysisResult) -> tuple[float, str, str]:
    """Return (score 0-1, display label, folder name)."""
    total = len(result.raw.get("skills") or [])
    n_hit = len(result.matched_skills)
    score = n_hit / total if total > 0 else 0
    if score >= 0.75:
        return score, "Strong match 🟢", "strong_match"
    elif score >= 0.50:
        return score, "Possible     🟡", "possible"
    elif score >= 0.30:
        return score, "Stretch      🟠", "no_match"
    else:
        return score, "Skip         🔴", "no_match"


def _url_to_slug(url: str) -> str:
    """
    Extract a stable filename stem from a job URL.

    Priority:
      1. Numeric job ID from the URL path — e.g. seek.com.au/job/91995152 -> "91995152"
         Covers Seek, Indeed, and most ATS systems that put IDs in the path.
      2. Sanitised URL fallback for boards without a numeric ID.

    Two URLs for the same job with different ?ref= or #sol= params
    resolve to the same filename and are correctly identified as duplicates.
    """
    # Match a numeric ID anywhere after a job-related path segment
    # Handles: /job/123, /jobs/view/123, /position/123, /opening/123 etc.
    m = re.search(
        r"/(?:jobs?|jobdetail|position|opening|vacancy)(?:/[^/]+)?/(\d{5,})",
        url, re.IGNORECASE
    )
    if m:
        return m.group(1)

    # Fallback — strip query string and fragment, then sanitise
    slug = re.sub(r"https?://", "", url)
    slug = slug.split("?")[0].split("#")[0]
    slug = re.sub(r'[/:*?"<>|&=#\\]', "_", slug)
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug[:120]


def build_report(result: AnalysisResult, url: str = "") -> tuple[str, str, str]:
    """
    Build the full report as a string.
    Returns (report_text, label, folder).
    """
    d     = result.raw
    sep   = "━" * 60
    lines = []

    # Header
    if url:
        lines.append(f"URL: {url}")
    lines.append(f"Checked: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(sep)
    lines.append(f"  {d.get('title', 'Unknown Role')}  —  {d.get('company', '')}")
    lines.append(f"  {d.get('location', '')}  |  {d.get('job_type', '')}  |  {d.get('salary') or 'Salary N/A'}")
    lines.append(sep)

    # Skills comparison
    norm      = get_normaliser()
    req       = [norm.normalise(s) for s in (d.get("skills") or [])]
    opt       = [norm.normalise(s) for s in (d.get("optional_skills") or [])]
    matched   = set(result.matched_skills)
    opt_match = set(result.matched_optional)
    col_w     = 34

    lines.append(f"\n  {'● Required Skills':<{col_w}}  ● Optional Skills")
    for i in range(max(len(req), len(opt), 1)):
        left = right = ""
        if i < len(req):
            tick = "✔" if req[i] in matched else "✗"
            left = f"  {tick}  {req[i]}"
        if i < len(opt):
            tick = "✔" if opt[i] in opt_match else "-"
            right = f"  {tick}  {opt[i]}"
        lines.append(f"  {left:<{col_w}}  {right}")

    # Score bar
    score, label, folder = _score_label(result)
    n_hit  = len(result.matched_skills)
    total  = len(req)
    pct    = int(score * 100)
    filled = int(30 * score)
    bar    = "█" * filled + "░" * (30 - filled)

    lines.append(f"\n  Skill match: [{bar}] {pct}%  {label}")
    lines.append(f"  {n_hit}/{total} required  |  +{len(result.matched_optional)} optional")

    if result.interest_hits:
        lines.append(f"\n  ● Interest alignment: {', '.join(result.interest_hits)}")

    if result.missing_skills:
        lines.append("\n  ● Missing required skills:")
        for s in result.missing_skills:
            lines.append(f"      ✗  {s}")

    exp = d.get("min_experience_years")
    if exp:
        lines.append("\n  ● Experience requirements:")
        for e in exp:
            min_yrs = e.get("min_years")
            max_yrs = e.get("max_years")
            yrs_str = f"{min_yrs}–{max_yrs} yrs" if max_yrs else f"{min_yrs}+ yrs"
            lines.append(f"      {yrs_str}  ({e.get('phrase', '')})")

    responsibilities = d.get("responsibilities") or []
    if responsibilities:
        lines.append("\n  ● Responsibilities:")
        for r in responsibilities:
            lines.append(f"      •  {r}")

    if focus := d.get("company_focus"):
        lines.append(f"\n  ● Company: {focus}")

    lines.append(f"\n{sep}\n")
    return "\n".join(lines), label, folder


def save_report(report_text: str, folder: str, url: str) -> Path:
    """Save report to reports/<folder>/<url-slug>.txt — returns the path."""
    out_dir = BASE_DIR / "reports" / folder
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (_url_to_slug(url) + ".txt")
    path.write_text(report_text, encoding="utf-8")
    return path


def report_exists(url: str) -> tuple[bool, str | None]:
    """
    Check whether a report for this URL already exists in any reports subfolder.
    Returns (True, folder_name) or (False, None).
    """
    slug      = _url_to_slug(url) + ".txt"
    reports   = BASE_DIR / "reports"
    if not reports.exists():
        return False, None
    for folder in reports.iterdir():
        if folder.is_dir() and (folder / slug).exists():
            return True, folder.name
    return False, None


def print_report(result: AnalysisResult, url: str = "") -> tuple[str, str]:
    """Build, print, save the report. Returns (folder, file_path_str)."""
    report_text, _, folder = build_report(result, url)
    print(report_text)
    path = save_report(report_text, folder, url)
    print(f"  📄 Saved → reports/{folder}/{path.name}\n")
    return folder, str(path)
