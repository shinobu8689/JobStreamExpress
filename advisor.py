"""
advisor.py — Standalone report advisor and cover letter generator.

Usage:
    python advisor.py reports/strong_match/seek.com.au_job_12345.txt
    python advisor.py reports/possible/seek.com.au_job_67890.txt --cover
    python advisor.py reports/strong_match/*.txt --cover --platform Seek

Modes:
    (default)   Read report, print job-specific success suggestions
    --cover     Also generate a full cover letter and save it to output/

The report file is the .txt saved by the main pipeline.
All LLM calls use config/llm_config.txt and config/profile.json.
"""

import argparse
import json
import re
import requests
import sys
from datetime import datetime
from pathlib import Path


# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR     = Path(__file__).parent
CONFIG_DIR   = BASE_DIR / "config"
PROMPT_DIR   = BASE_DIR / "prompts"
OUTPUT_DIR   = BASE_DIR / "output"

PROFILE_FILE    = CONFIG_DIR / "profile.json"
LLM_CONFIG_FILE = CONFIG_DIR / "llm_config.txt"

DEFAULT_MODEL      = "gemma3:12b"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MAX_TOKENS = 2048


# ── Config loaders ────────────────────────────────────────────────────────────

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
        print(f"⚠  profile.json not found at {PROFILE_FILE}")
        sys.exit(1)
    return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))


def load_prompt(filename: str, **kwargs) -> str:
    path = PROMPT_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").format(**kwargs)


# ── Report parser ─────────────────────────────────────────────────────────────

def parse_report(report_path: Path) -> dict:
    """
    Parse a saved .txt report back into structured fields.
    Extracts: url, title, company, location, job_type, salary,
              matched_skills, missing_skills, company_focus, score_pct.
    """
    text = report_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    info: dict = {
        "raw_text":      text,
        "url":           "",
        "title":         "",
        "company":       "",
        "location":      "",
        "job_type":      "",
        "salary":        "",
        "matched_skills": [],
        "missing_skills": [],
        "company_focus": "",
        "score_pct":     0,
        "label":         "",
    }

    for line in lines:
        s = line.strip()

        if s.startswith("URL:"):
            info["url"] = s[4:].strip()

        # Title — Company line (between the ━ separators)
        elif "  —  " in s and not info["title"]:
            parts = s.split("  —  ", 1)
            info["title"]   = parts[0].strip()
            info["company"] = parts[1].strip()

        # Location | job_type | salary
        elif "  |  " in s and not info["location"]:
            parts = [p.strip() for p in s.split("  |  ")]
            if len(parts) >= 3:
                info["location"] = parts[0]
                info["job_type"] = parts[1]
                info["salary"]   = parts[2] if parts[2] != "Salary N/A" else ""

        # Matched required skills
        elif s.startswith("✔") and "required" not in s.lower():
            skill = re.sub(r"^✔\s*", "", s).strip()
            if skill:
                info["matched_skills"].append(skill)

        # Missing required skills
        elif s.startswith("✗") and "required" not in s.lower():
            skill = re.sub(r"^✗\s*", "", s).strip()
            if skill:
                info["missing_skills"].append(skill)

        # Score line
        elif "Skill match:" in s:
            m = re.search(r"(\d+)%", s)
            if m:
                info["score_pct"] = int(m.group(1))
            for label in ["Strong match", "Possible", "Stretch", "Skip"]:
                if label in s:
                    info["label"] = label
                    break

        # Company focus
        elif s.startswith("● Company:"):
            info["company_focus"] = s[len("● Company:"):].strip()

    return info


# ── LLM call ──────────────────────────────────────────────────────────────────

def call_ollama(prompt: str, cfg: dict, label: str = "") -> str:
    if label:
        print(f"  ⟲  {label}...")
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
        return resp.json()["response"].strip()
    except requests.exceptions.ConnectionError:
        print(f"✗ Cannot reach Ollama at {cfg['url']}. Is it running?")
        sys.exit(1)


# ── Advisor ───────────────────────────────────────────────────────────────────

def run_advisor(report: dict, profile: dict, cfg: dict) -> str:
    """Generate job-specific success suggestions."""
    profile_summary = (
        f"Name: {profile.get('name', 'Candidate')}\n"
        f"Skills: {', '.join(profile.get('skills', []))}\n"
        f"Projects: {'; '.join(p['name'] + ' — ' + p['description'] for p in profile.get('projects', []))}\n"
        f"Interests: {', '.join(profile.get('interests', []))}"
    )

    prompt = load_prompt(
        "advisor.txt",
        profile_summary=profile_summary,
        report_text=report["raw_text"],
    )

    return call_ollama(prompt, cfg, label="Generating suggestions")


# ── Cover letter ──────────────────────────────────────────────────────────────

def run_cover_letter(report: dict, profile: dict, cfg: dict,
                     platform: str) -> str:
    """Generate a four-paragraph cover letter from the report + profile."""

    matched = ", ".join(report["matched_skills"]) or "relevant technical skills"
    projects = "; ".join(
        f"{p['name']} ({p['description']})"
        for p in profile.get("projects", [])
    )

    print("\n  Generating cover letter (4 paragraphs)...")

    p1 = call_ollama(load_prompt(
        "cover_p1.txt",
        title=report["title"],
        company=report["company"],
        platform=platform,
    ), cfg, "  1/4 Opening")

    p2 = call_ollama(load_prompt(
        "cover_p2.txt",
        matched_skills=matched,
        projects=projects,
    ), cfg, "  2/4 Skills & projects")

    p3 = call_ollama(load_prompt(
        "cover_p3.txt",
        company=report["company"],
        company_focus=report["company_focus"] or report["company"],
    ), cfg, "  3/4 Why this company")

    p4 = call_ollama(load_prompt(
        "cover_p4.txt",
        company=report["company"],
    ), cfg, "  4/4 Closing")

    try:
        sig = load_prompt("signature.txt", name=profile.get("name", ""))
    except FileNotFoundError:
        sig = f"Best regards,\n{profile.get('name', '')}"

    return "\n\n".join([p1, p2, p3, p4, sig])


# ── Output saver ──────────────────────────────────────────────────────────────

def save_output(content: str, report_path: Path, suffix: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    stem = report_path.stem
    out  = OUTPUT_DIR / f"{stem}_{suffix}.txt"
    out.write_text(content, encoding="utf-8")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def process_report(report_path: Path, cover: bool, platform: str,
                   cfg: dict, profile: dict) -> None:
    sep = "━" * 60

    print(f"\n{sep}")
    print(f"  Report: {report_path.name}")

    report = parse_report(report_path)

    print(f"  {report['title']}  —  {report['company']}")
    print(f"  Score: {report['score_pct']}%  {report['label']}")
    print(sep)

    # ── Advisor suggestions ───────────────────────────────────────────────────
    print("\n  ● Success suggestions\n")
    suggestions = run_advisor(report, profile, cfg)
    print(suggestions)

    adv_path = save_output(suggestions, report_path, "advice")
    print(f"\n  📄 Advice saved → {adv_path.name}")

    # ── Cover letter ──────────────────────────────────────────────────────────
    if cover:
        cover_text = run_cover_letter(report, profile, cfg, platform)

        print(f"\n{sep}")
        print("  Cover Letter\n")
        print(cover_text)

        cl_path = save_output(cover_text, report_path, "cover_letter")
        print(f"\n  📄 Cover letter saved → {cl_path.name}")

    print(f"\n{sep}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Job advisor and cover letter generator."
    )
    parser.add_argument(
        "reports",
        nargs="+",
        help="Path(s) to report .txt files. Supports globs.",
    )
    parser.add_argument(
        "--cover",
        action="store_true",
        help="Also generate a cover letter for each report.",
    )
    parser.add_argument(
        "--platform",
        default="Seek",
        help="Job platform name for cover letter (default: Seek)",
    )
    args = parser.parse_args()

    cfg     = load_llm_config()
    profile = load_profile()

    paths = [Path(p) for p in args.reports]
    valid = [p for p in paths if p.exists() and p.suffix == ".txt"]

    if not valid:
        print("✗ No valid .txt report files found.")
        sys.exit(1)

    print(f"[advisor] Model:   {cfg['model']}")
    print(f"[advisor] Reports: {len(valid)}")
    print(f"[advisor] Cover:   {args.cover}")
    if args.cover:
        print(f"[advisor] Platform: {args.platform}")

    for path in valid:
        process_report(path, args.cover, args.platform, cfg, profile)


if __name__ == "__main__":
    main()
