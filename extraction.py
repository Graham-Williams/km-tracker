# Photo score extraction — read a Mario Kart final-standings photo via the
# Claude API (vision + structured outputs) and return the rows on screen.
#
# This module owns the model call so the /extract-scores route (app.py) and the
# dev validation script (scripts/validate_extraction.py) share the exact same
# prompt + schema. The feature is optional: with no ANTHROPIC_API_KEY in the
# environment, extraction_enabled() is False and the app hides the UI.

import os

import anthropic
from pydantic import BaseModel
from typing import List

EXTRACTION_MODEL = "claude-sonnet-4-6"
EXTRACTION_MAX_TOKENS = 2000

# What ALWAYS gets read, regardless of edition: every row of the screen.
_PROMPT_READ = (
    "This photo shows a Mario Kart results screen (the end-of-cup or "
    "post-race standings). Extract EVERY row of the standings: the finishing "
    "position, the character name exactly as displayed, and that row's total "
    "points. Include ALL rows — human players and CPU racers alike."
)

# Highlight framing — used ONLY for editions where a reliable human-vs-CPU cue
# exists (Wii). Switch has no such cue, so its prompt omits this entirely.
_PROMPT_HIGHLIGHT_INTRO = (
    "Also mark which rows belong to HUMAN players: set is_highlighted=true "
    "ONLY for a human row (never for a CPU), because only the human players' "
    "scores matter. The human marker is the ROW STYLING described below — it "
    "is independent of finishing position (a human can place anywhere)."
)

# Mario Kart Wii human-vs-CPU cue. From real-photo validation: the ONE
# consistent cue across BOTH Wii results layouts is opaque-bar (human) vs
# translucent (CPU). The two layouts differ only in shape, not in the cue.
_PROMPT_CUES_WII = (
    "This is Mario Kart Wii. The human cue is ONE consistent thing: a HUMAN "
    "player's row is a SOLID, OPAQUE colored bar (e.g. pink, cyan, orange — "
    "the color is per-player, not fixed); a CPU row is SEMI-TRANSPARENT / "
    "TRANSLUCENT, so the background track shows through it. Set "
    "is_highlighted=true for the solid opaque (human) rows and false for the "
    "see-through (CPU) rows.\n"
    "There are TWO possible Wii layouts and they differ ONLY in shape — the "
    "opaque-vs-translucent cue is the same in both:\n"
    "1) A single VERTICAL list of 12 rows (positions 1-12, top to bottom).\n"
    "2) A TWO-COLUMN trophy/credits screen (\"You got Nth place!\", a trophy "
    "in the middle) with positions 1-6 in the LEFT column and 7-12 in the "
    "RIGHT column — read BOTH columns; the human bars are simply shorter "
    "because each row is split across two columns."
)

# Mario Kart 8 Deluxe (Switch): NO reliable human-vs-CPU marker exists, so we
# do NOT attempt highlight detection here. Every Switch row is left
# is_highlighted=false, which routes matching through the character-only
# fallback (naive best-effort, same as Wii's pre-highlight behavior). The photo
# is still saved so we can collect Switch data. Switch highlight-aware auto-fill
# is a deliberate FUTURE follow-up once a real cue is identified.
_PROMPT_SWITCH = (
    "This is Mario Kart 8 Deluxe (Switch). This screen has NO reliable "
    "human-vs-CPU visual marker, so DO NOT try to guess which rows are humans "
    "— leave is_highlighted=false for EVERY row. Just read the position, "
    "character and points for all rows."
)

# Unknown edition: fall back to the Wii opaque-vs-translucent cue (the only
# reliable one) and let the screen dictate.
_PROMPT_CUES_UNKNOWN = (
    "The edition is unknown. If this is a Mario Kart Wii screen, a HUMAN row "
    "is a SOLID OPAQUE colored bar and a CPU row is SEMI-TRANSPARENT / "
    "TRANSLUCENT (the track shows through) — in either the single 12-row list "
    "or the two-column trophy screen (positions 1-6 left, 7-12 right; read "
    "both). Set is_highlighted=true for the opaque (human) rows. If the screen "
    "has no such cue, leave is_highlighted=false for every row."
)
_PROMPT_ROBUSTNESS = (
    "The photo may be dark, glare-washed, or shot at an angle — do your best. "
    "If a value is unreadable, make your best guess from what is visible."
)


def build_extraction_prompt(edition=None):
    """Assemble the edition-specific extraction prompt.

    edition: "wii", "mk8dx", or None/unknown.
      - "wii"   → read all rows + detect the opaque-vs-translucent human cue.
      - "mk8dx" → read all rows only; NO highlight detection (no reliable cue),
                  so matching falls through to character-only (best-effort).
      - None    → read all rows + best-effort (Wii) cue if present.
    """
    if edition == "wii":
        parts = (_PROMPT_READ, _PROMPT_HIGHLIGHT_INTRO, _PROMPT_CUES_WII, _PROMPT_ROBUSTNESS)
    elif edition == "mk8dx":
        # No highlight framing at all — Switch is character-only for now.
        parts = (_PROMPT_READ, _PROMPT_SWITCH, _PROMPT_ROBUSTNESS)
    else:
        parts = (_PROMPT_READ, _PROMPT_HIGHLIGHT_INTRO, _PROMPT_CUES_UNKNOWN, _PROMPT_ROBUSTNESS)
    return "\n\n".join(parts)


# Back-compat default (edition-agnostic) — the route passes an explicit edition.
EXTRACTION_PROMPT = build_extraction_prompt(None)


class StandingsRow(BaseModel):
    position: int
    character: str
    points: int
    is_highlighted: bool = False


class Standings(BaseModel):
    rows: List[StandingsRow]


class ExtractionError(Exception):
    """The Claude API call failed or returned nothing usable."""


def extraction_enabled():
    """Photo extraction is available only when an API key is configured."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def extract_standings(image_b64, media_type, edition=None):
    """Extract standings rows from a base64-encoded photo. Returns a Standings.

    edition ("wii" / "mk8dx" / None) selects the prompt's description of the
    human-vs-CPU visual cue so it can be specific to the screen we expect.

    Raises ExtractionError on any API failure (network, timeout, rate limit,
    unparsable response) so callers can turn it into a clean 502.
    """
    prompt = build_extraction_prompt(edition)
    client = anthropic.Anthropic()
    try:
        response = client.messages.parse(
            model=EXTRACTION_MODEL,
            max_tokens=EXTRACTION_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            output_format=Standings,
        )
    except anthropic.APIError as e:
        raise ExtractionError(f"Claude API call failed: {e}") from e
    standings = response.parsed_output
    if standings is None:
        raise ExtractionError("model response did not match the standings schema")
    return standings
