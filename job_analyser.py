"""
job_analyser.py — Step 3: LLM analysis pipeline.

Flow:
  analyse_job(text) -> AnalysisResult
    1. Send cleaned job text to local Ollama LLM with analyse prompt
    2. Parse LLM JSON response into structured data
    3. Load user profile (skills + interests)
    4. Compare job skills against user profile
    5. Print report to terminal

Config:
  config/llm_config.txt   — model name, ollama URL
  config/profile.json     — user skills, interests, projects
  prompts/analyse.txt     — LLM prompt template (uses {text} placeholder)
"""

import json
import re
import requests
from dataclasses import dataclass, field
from pathlib import Path


# ── Config paths ──────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).parent
CONFIG_DIR  = BASE_DIR / "config"
PROMPT_DIR  = BASE_DIR / "prompts"

LLM_CONFIG_FILE  = CONFIG_DIR / "llm_config.txt"
PROFILE_FILE     = CONFIG_DIR / "profile.json"
ANALYSE_PROMPT   = PROMPT_DIR / "analyse.txt"

# ── LLM config defaults ───────────────────────────────────────────────────────

DEFAULT_MODEL       = "gemma3:12b"   # swap to gemma3:4b for speed, gemma3:27b for quality
DEFAULT_OLLAMA_URL  = "http://localhost:11434/api/generate"
DEFAULT_MAX_TOKENS  = 2048


def load_llm_config() -> dict:
    """
    Reads config/llm_config.txt.
    Format (one key=value per line):
        model=gemma3:12b
        url=http://localhost:11434/api/generate
        max_tokens=2048
    """
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


# ── Profile loader ────────────────────────────────────────────────────────────

def load_profile() -> dict:
    """Load config/profile.json. Returns empty profile if missing."""
    if not PROFILE_FILE.exists():
        print(f"[analyser] ⚠  profile.json not found at {PROFILE_FILE}")
        return {"skills": [], "interests": [], "projects": []}
    return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))


# ── Prompt loader ─────────────────────────────────────────────────────────────

def load_prompt(path: Path, **kwargs) -> str:
    """Load a prompt file and substitute {placeholders}."""
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    template = path.read_text(encoding="utf-8")
    return template.format(**kwargs)


# ── LLM call ──────────────────────────────────────────────────────────────────

def call_ollama(prompt: str, cfg: dict) -> str:
    """Send prompt to Ollama, return raw response text."""
    try:
        resp = requests.post(
            cfg["url"],
            json={
                "model":  cfg["model"],
                "prompt": prompt,
                "stream": False,
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
    """
    Extract JSON from LLM response. Attempts multiple strategies:
      1. Fenced ```json ... ``` block
      2. Bare { ... } object
      3. Orphaned content (LLM returned fields without opening brace) — wrap it
    Raises ValueError with full raw output if nothing works.
    """
    # Strategy 1 — fenced block
    match = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            pass

    # Strategy 2 — bare JSON object
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    # Strategy 3 — LLM returned fields without wrapping braces
    # e.g. '\n  "title": "...",\n  "company": "..."'
    stripped = raw.strip().strip(",")
    if stripped and not stripped.startswith("{"):
        try:
            return json.loads("{" + stripped + "}")
        except json.JSONDecodeError:
            pass

    # Nothing worked — print full raw so you can see what the LLM returned
    print(f"[analyser] ✗  Full LLM response:\n{'─'*40}\n{raw}\n{'─'*40}")
    raise ValueError("Could not parse JSON from LLM response (see above)")


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class AnalysisResult:
    raw:              dict          # full parsed LLM JSON
    matched_skills:   list[str]     # required skills the user has
    missing_skills:   list[str]     # required skills the user lacks
    matched_optional: list[str]     # optional skills the user has (bonus)
    interest_hits:    list[str]     # user interest keywords found in job text
    profile:          dict          # the loaded user profile


# ── Core pipeline ─────────────────────────────────────────────────────────────

def analyse_job(cleaned_text: str) -> AnalysisResult | None:
    """
    Full analysis pipeline:
      1. Call LLM with analyse prompt
      2. Parse JSON response
      3. Compare against user profile (skills + interests)
      4. Return AnalysisResult

    Returns None if the LLM call or JSON parse fails.
    """
    cfg     = load_llm_config()
    profile = load_profile()

    # Step 1 — build and send prompt
    print(f"[analyser] ⟲  Calling {cfg['model']}...")
    try:
        prompt  = load_prompt(ANALYSE_PROMPT, text=cleaned_text)
        raw_llm = call_ollama(prompt, cfg)
    except Exception as e:
        print(f"[analyser] ✗  LLM call failed: {e}")
        return None

    # Step 2 — parse JSON
    try:
        data = parse_json_response(raw_llm)
    except ValueError as e:
        print(f"[analyser] ✗  JSON parse failed: {e}")
        return None

    # Step 3 — normalise everything to lowercase sets for comparison
    user_skills = {s.lower().strip() for s in profile.get("skills", [])}
    user_interests = [i.lower().strip() for i in profile.get("interests", [])]

    job_required = [s.lower().strip() for s in (data.get("skills") or [])]
    job_optional = [s.lower().strip() for s in (data.get("optional_skills") or [])]

    matched_skills   = [s for s in job_required if s in user_skills]
    missing_skills   = [s for s in job_required if s not in user_skills]
    matched_optional = [s for s in job_optional if s in user_skills]

    # Interest match — scan job title + responsibilities + company_focus
    job_text_lower = " ".join([
        data.get("title", ""),
        data.get("company_focus", ""),
        " ".join(data.get("responsibilities", [])),
    ]).lower()
    interest_hits = [i for i in user_interests if i in job_text_lower]

    return AnalysisResult(
        raw=data,
        matched_skills=matched_skills,
        missing_skills=missing_skills,
        matched_optional=matched_optional,
        interest_hits=interest_hits,
        profile=profile,
    )


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(result: AnalysisResult) -> None:
    """Print a structured analysis report to the terminal."""
    d = result.raw
    sep = "━" * 60

    print(f"\n{sep}")
    print(f"  {d.get('title', 'Unknown Role')}  —  {d.get('company', '')}")
    print(f"  {d.get('location', '')}  |  {d.get('job_type', '')}  |  {d.get('salary') or 'Salary N/A'}")
    print(sep)

    # ── Skills comparison ─────────────────────────────────────────────────────
    req     = [s.lower() for s in (d.get("skills") or [])]
    opt     = [s.lower() for s in (d.get("optional_skills") or [])]
    col_w   = 34

    print(f"\n  {'● Required Skills':<{col_w}}  {'● Optional Skills'}")
    for i in range(max(len(req), len(opt))):
        left = right = ""
        if i < len(req):
            tick = "✔" if req[i] in {s.lower() for s in result.matched_skills} else "✗"
            left = f"  {tick}  {req[i]}"
        if i < len(opt):
            tick = "✔" if opt[i] in {s.lower() for s in result.matched_optional} else "-"
            right = f"  {tick}  {opt[i]}"
        print(f"  {left:<{col_w}}  {right}")

    # ── Skill summary ─────────────────────────────────────────────────────────
    total    = len(req)
    matched  = len(result.matched_skills)
    score    = matched / total if total > 0 else 0
    bar_len  = 30
    filled   = int(bar_len * score)
    bar      = "█" * filled + "░" * (bar_len - filled)
    pct      = int(score * 100)

    label = (
        "Strong match 🟢" if score >= 0.75 else
        "Possible     🟡" if score >= 0.50 else
        "Stretch      🟠" if score >= 0.30 else
        "Skip         🔴"
    )
    print(f"\n  Skill match: [{bar}] {pct}%  {label}")
    print(f"  {matched}/{total} required skills matched  |  +{len(result.matched_optional)} optional")

    # ── Interests ─────────────────────────────────────────────────────────────
    if result.interest_hits:
        print(f"\n  ● Interest alignment: {', '.join(result.interest_hits)}")

    # ── Missing skills ────────────────────────────────────────────────────────
    if result.missing_skills:
        print(f"\n  ● Missing required skills:")
        for s in result.missing_skills:
            print(f"      ✗  {s}")

    # ── Experience ────────────────────────────────────────────────────────────
    exp = d.get("min_experience_years")
    if exp:
        print(f"\n  ● Experience requirements:")
        for e in exp:
            phrase   = e.get("phrase", "")
            min_yrs  = e.get("min_years")
            max_yrs  = e.get("max_years")
            yrs_str  = f"{min_yrs}–{max_yrs} yrs" if max_yrs else f"{min_yrs}+ yrs"
            print(f"      {yrs_str}  ({phrase})")

    # ── Responsibilities ──────────────────────────────────────────────────────
    responsibilities = d.get("responsibilities") or []
    if responsibilities:
        print(f"\n  ● Responsibilities:")
        for r in responsibilities:
            print(f"      •  {r}")

    # ── Company focus ─────────────────────────────────────────────────────────
    focus = d.get("company_focus")
    if focus:
        print(f"\n  ● Company: {focus}")

    print(f"\n{sep}\n")
