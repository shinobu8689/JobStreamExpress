"""
job_analyser.py — Step 3: LLM analysis pipeline.

Flow:
  analyse_job(text) -> AnalysisResult
    1. Send cleaned job text to local Ollama LLM with analyse prompt
    2. Parse LLM JSON response into structured data
    3. Load user profile (skills + interests)
    4. Normalise skill strings via alias map
    5. Compare job skills against user profile
    6. Print report to terminal

Config:
  config/llm_config.txt       — model name, ollama URL
  config/profile.json         — user skills, interests, projects
  config/skill_aliases.json   — canonical skill names + aliases
  prompts/analyse.txt         — LLM prompt template (uses {text} placeholder)
"""

import json
import re
import requests
from dataclasses import dataclass
from skill_registry import record_skills
from pathlib import Path


# ── Config paths ──────────────────────────────────────────────────────────────

BASE_DIR    = Path(__file__).parent
CONFIG_DIR  = BASE_DIR / "config"
PROMPT_DIR  = BASE_DIR / "prompts"

LLM_CONFIG_FILE  = CONFIG_DIR / "llm_config.txt"
PROFILE_FILE     = CONFIG_DIR / "profile.json"
ALIASES_FILE     = CONFIG_DIR / "skill_aliases.json"
ANALYSE_PROMPT   = PROMPT_DIR / "analyse.txt"

DEFAULT_MODEL      = "gemma3:12b"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MAX_TOKENS = 2048


# ── Skill normaliser ──────────────────────────────────────────────────────────

class SkillNormaliser:
    """
    Loads skill_aliases.json once at startup and maps any known alias
    back to its canonical name.

    Structure of skill_aliases.json:
        { "canonical name": ["alias1", "alias2", ...], ... }

    All comparisons are lowercase. The normaliser is transparent —
    if a skill has no alias it passes through unchanged.

    Usage:
        norm = SkillNormaliser()          # load once at startup
        norm.normalise("Win10")           # → "windows"
        norm.normalise("Python 3")        # → "python"
        norm.normalise("some random str") # → "some random str"  (no match)
    """

    def __init__(self) -> None:
        self._map: dict[str, str] = {}   # alias → canonical
        self._loaded = False
        self._load()

    def _load(self) -> None:
        if not ALIASES_FILE.exists():
            print(f"[normaliser] ⚠  skill_aliases.json not found — skill normalisation disabled.")
            return

        raw = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
        count = 0
        for canonical, aliases in raw.items():
            if canonical.startswith("_"):   # skip comment keys
                continue
            canon_lower = canonical.lower().strip()
            # canonical maps to itself
            self._map[canon_lower] = canon_lower
            for alias in aliases:
                self._map[alias.lower().strip()] = canon_lower
                count += 1

        self._loaded = True
        print(f"[normaliser] Loaded {len(raw)} canonical skills, {count} aliases.")

    def normalise(self, skill: str) -> str:
        """Return the canonical form of a skill string, or the original if unknown."""
        return self._map.get(skill.lower().strip(), skill.lower().strip())

    def normalise_set(self, skills: list[str]) -> set[str]:
        """Normalise a list of skills into a set of canonical names."""
        return {self.normalise(s) for s in skills}


# ── Module-level singleton — loaded once when the module is first imported ────
_normaliser: SkillNormaliser | None = None

def get_normaliser() -> SkillNormaliser:
    global _normaliser
    if _normaliser is None:
        _normaliser = SkillNormaliser()
    return _normaliser


# ── LLM config ────────────────────────────────────────────────────────────────

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


# ── Profile loader ────────────────────────────────────────────────────────────

def load_profile() -> dict:
    if not PROFILE_FILE.exists():
        print(f"[analyser] ⚠  profile.json not found at {PROFILE_FILE}")
        return {"skills": [], "interests": [], "projects": []}
    return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))


# ── Prompt loader ─────────────────────────────────────────────────────────────

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
    # Strategy 1 — fenced block
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
    raw:              dict       # full parsed LLM JSON
    matched_skills:   list[str]  # required skills the user has (canonical names)
    missing_skills:   list[str]  # required skills the user lacks (canonical names)
    matched_optional: list[str]  # optional skills the user has (canonical names)
    interest_hits:    list[str]  # user interest keywords found in job text
    profile:          dict       # the loaded user profile


# ── Core pipeline ─────────────────────────────────────────────────────────────

def analyse_job(cleaned_text: str) -> AnalysisResult | None:
    cfg      = load_llm_config()
    profile  = load_profile()
    norm     = get_normaliser()

    print(f"[analyser] ⟲  Calling {cfg['model']}...")
    try:
        prompt  = load_prompt(ANALYSE_PROMPT, text=cleaned_text)
        raw_llm = call_ollama(prompt, cfg)
    except Exception as e:
        print(f"[analyser] ✗  LLM call failed: {e}")
        return None

    try:
        data = parse_json_response(raw_llm)
    except ValueError as e:
        print(f"[analyser] ✗  JSON parse failed: {e}")
        return None

    # Normalise user skills
    user_skills = norm.normalise_set(profile.get("skills", []))

    # Normalise job skills from LLM response
    job_required = [norm.normalise(s) for s in (data.get("skills") or [])]
    job_optional = [norm.normalise(s) for s in (data.get("optional_skills") or [])]

    matched_skills   = [s for s in job_required if s in user_skills]
    # Record all skills seen in this job for the registry
    record_skills(list(set(job_required + job_optional)))
    missing_skills   = [s for s in job_required if s not in user_skills]
    matched_optional = [s for s in job_optional if s in user_skills]

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


# ── Report printer ────────────────────────────────────────────────────────────

def print_report(result: AnalysisResult) -> None:
    d   = result.raw
    sep = "━" * 60

    print(f"\n{sep}")
    print(f"  {d.get('title', 'Unknown Role')}  —  {d.get('company', '')}")
    print(f"  {d.get('location', '')}  |  {d.get('job_type', '')}  |  {d.get('salary') or 'Salary N/A'}")
    print(sep)

    # ── Skills comparison ─────────────────────────────────────────────────────
    norm     = get_normaliser()
    req      = [norm.normalise(s) for s in (d.get("skills") or [])]
    opt      = [norm.normalise(s) for s in (d.get("optional_skills") or [])]
    matched  = set(result.matched_skills)
    opt_match= set(result.matched_optional)
    col_w    = 34

    print(f"\n  {'● Required Skills':<{col_w}}  {'● Optional Skills'}")
    for i in range(max(len(req), len(opt), 1)):
        left = right = ""
        if i < len(req):
            tick = "✔" if req[i] in matched else "-"
            left = f"  {tick}  {req[i]}"
        if i < len(opt):
            tick = "✔" if opt[i] in opt_match else "-"
            right = f"  {tick}  {opt[i]}"
        print(f"  {left:<{col_w}}  {right}")

    # ── Skill summary bar ─────────────────────────────────────────────────────
    total  = len(req)
    n_hit  = len(result.matched_skills)
    score  = n_hit / total if total > 0 else 0
    filled = int(30 * score)
    bar    = "█" * filled + "░" * (30 - filled)
    pct    = int(score * 100)

    label = (
        "Strong match 🟢" if score >= 0.75 else
        "Possible     🟡" if score >= 0.50 else
        "Stretch      🟠" if score >= 0.30 else
        "Skip         🔴"
    )
    print(f"\n  Skill match: [{bar}] {pct}%  {label}")
    print(f"  {n_hit}/{total} required skills matched  |  +{len(result.matched_optional)} optional")

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
            min_yrs = e.get("min_years")
            max_yrs = e.get("max_years")
            yrs_str = f"{min_yrs}–{max_yrs} yrs" if max_yrs else f"{min_yrs}+ yrs"
            print(f"      {yrs_str}  ({e.get('phrase', '')})")

    # ── Responsibilities ──────────────────────────────────────────────────────
    responsibilities = d.get("responsibilities") or []
    if responsibilities:
        print(f"\n  ● Responsibilities:")
        for r in responsibilities:
            print(f"      •  {r}")

    # ── Company focus ─────────────────────────────────────────────────────────
    if focus := d.get("company_focus"):
        print(f"\n  ● Company: {focus}")

    print(f"\n{sep}\n")
