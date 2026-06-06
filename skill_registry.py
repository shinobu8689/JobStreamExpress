"""
skill_registry.py — Persistent skill registry with auto-normalisation.

On every job analysis:
  - All LLM-extracted skills are saved to config/skill_registry.json

On startup (via startup_update):
  - Scans registry for skills not yet in skill_aliases.json
  - Fuzzy-matches them against existing canonicals
  - High-confidence matches → auto-added as aliases
  - Low-confidence / unknown → written to config/skill_suggestions.txt for manual review

Registry format (config/skill_registry.json):
  {
    "python":          { "count": 14, "seen_as": ["python", "python 3", "python3"] },
    "microsoft intune":{ "count": 3,  "seen_as": ["microsoft intune", "intune"] },
    ...
  }
  Keys are the raw normalised strings from the LLM (lowercased).
  "seen_as" tracks every surface form observed — useful for spotting aliases.

Auto-alias threshold:
  FUZZY_AUTO   >= 0.82  → add as alias automatically (high confidence)
  FUZZY_REVIEW >= 0.60  → write to suggestions file for manual review
  below               → ignore (too dissimilar)
"""

import json
import re
from pathlib import Path
from difflib import SequenceMatcher


# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR        = Path(__file__).parent
CONFIG_DIR      = BASE_DIR / "config"
REGISTRY_FILE   = CONFIG_DIR / "skill_registry.json"
ALIASES_FILE    = CONFIG_DIR / "skill_aliases.json"
SUGGESTIONS_FILE= CONFIG_DIR / "skill_suggestions.txt"

FUZZY_AUTO      = 0.82   # auto-add as alias
FUZZY_REVIEW    = 0.60   # write to suggestions for manual check


# ── Registry I/O ──────────────────────────────────────────────────────────────

def _load_registry() -> dict:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    return {}


def _save_registry(registry: dict) -> None:
    CONFIG_DIR.mkdir(exist_ok=True)
    REGISTRY_FILE.write_text(
        json.dumps(registry, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def _load_aliases() -> dict:
    if ALIASES_FILE.exists():
        raw = json.loads(ALIASES_FILE.read_text(encoding="utf-8"))
        return {k: v for k, v in raw.items() if not k.startswith("_")}
    return {}


def _save_aliases(aliases: dict) -> None:
    # Preserve the comment key at the top
    comment = {"_comment": "canonical skill name → list of aliases (all lowercase)."}
    merged  = {**comment, **aliases}
    ALIASES_FILE.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ── Fuzzy match helper ────────────────────────────────────────────────────────

def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _best_canonical_match(skill: str, canonicals: list[str]) -> tuple[str, float]:
    """Return (best_canonical, score) for a given skill string."""
    best_canon = ""
    best_score = 0.0
    for canon in canonicals:
        score = _similarity(skill, canon)
        if score > best_score:
            best_score = score
            best_canon = canon
    return best_canon, best_score


def _build_alias_lookup(aliases: dict) -> dict[str, str]:
    """Flat reverse map: alias → canonical (same as SkillNormaliser._map)."""
    lookup: dict[str, str] = {}
    for canon, alias_list in aliases.items():
        canon_lower = canon.lower().strip()
        lookup[canon_lower] = canon_lower
        for a in alias_list:
            lookup[a.lower().strip()] = canon_lower
    return lookup


# ── Public API ────────────────────────────────────────────────────────────────

def record_skills(skills: list[str]) -> None:
    """
    Called after every LLM analysis. Adds each skill to the registry,
    incrementing its count and tracking surface forms seen.

    Args:
        skills: raw skill strings from LLM (required + optional combined)
    """
    registry = _load_registry()

    for raw in skills:
        key = raw.lower().strip()
        if not key:
            continue
        if key not in registry:
            registry[key] = {"count": 0, "seen_as": []}
        registry[key]["count"] += 1
        if raw.lower() not in [s.lower() for s in registry[key]["seen_as"]]:
            registry[key]["seen_as"].append(raw)

    _save_registry(registry)


def startup_update() -> None:
    """
    Run once at startup. Scans the registry for skills not yet covered
    by skill_aliases.json, fuzzy-matches them, and either:
      - Auto-adds them as aliases (high confidence)
      - Writes them to skill_suggestions.txt (low confidence / new)

    Prints a summary of what was added and what needs review.
    """
    registry = _load_registry()
    aliases  = _load_aliases()
    lookup   = _build_alias_lookup(aliases)
    canonicals = list(aliases.keys())

    if not registry:
        print("[registry] No skills recorded yet — skipping update.")
        return

    auto_added:  list[tuple[str, str, float]] = []   # (skill, canonical, score)
    to_review:   list[tuple[str, float, str]] = []   # (skill, score, best_canon)
    truly_new:   list[str] = []                       # no match at all

    aliases_changed = False

    for skill, info in sorted(registry.items(), key=lambda x: -x[1]["count"]):
        # Already covered by the alias map
        if skill in lookup:
            continue

        if not canonicals:
            truly_new.append(skill)
            continue

        best_canon, score = _best_canonical_match(skill, canonicals)

        if score >= FUZZY_AUTO:
            # High confidence — add automatically
            if skill not in aliases[best_canon]:
                aliases[best_canon].append(skill)
                lookup[skill] = best_canon   # update local lookup too
                auto_added.append((skill, best_canon, score))
                aliases_changed = True

        elif score >= FUZZY_REVIEW:
            to_review.append((skill, score, best_canon))

        else:
            truly_new.append(skill)

    if aliases_changed:
        _save_aliases(aliases)

    # ── Print summary ─────────────────────────────────────────────────────────
    print(f"\n[registry] Startup skill update")
    print(f"  Registry: {len(registry)} unique skills tracked")

    if auto_added:
        print(f"  Auto-aliased ({len(auto_added)}):")
        for skill, canon, score in auto_added:
            print(f"    \"{skill}\" → \"{canon}\"  ({int(score*100)}%)")
    else:
        print(f"  No new auto-aliases found.")

    # Write suggestions file
    needs_review = to_review + [(s, 0.0, "") for s in truly_new]
    if needs_review:
        lines = [
            "# skill_suggestions.txt — review and move entries to skill_aliases.json",
            "# Format: skill | best_match_canonical | confidence",
            "# Add the skill as an alias under the canonical, or create a new canonical entry.",
            "",
        ]
        if to_review:
            lines.append("# ── Possible aliases (fuzzy match) ──")
            for skill, score, canon in sorted(to_review, key=lambda x: -x[1]):
                count = registry.get(skill, {}).get("count", 0)
                lines.append(f"{skill:<40} | {canon:<30} | {int(score*100)}%  (seen {count}x)")

        if truly_new:
            lines.append("")
            lines.append("# ── Unknown skills (no close match) ──")
            for skill in sorted(truly_new):
                count = registry.get(skill, {}).get("count", 0)
                seen  = registry.get(skill, {}).get("seen_as", [skill])
                lines.append(f"{skill:<40} | {'?':<30} |  new   (seen {count}x, forms: {seen})")

        SUGGESTIONS_FILE.write_text("\n".join(lines), encoding="utf-8")
        print(f"  {len(needs_review)} skills need review → {SUGGESTIONS_FILE.name}")
    else:
        print(f"  No skills need review.")

    print()
