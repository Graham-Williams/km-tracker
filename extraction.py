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

# What ALWAYS gets read, regardless of edition: every row, tagged human vs CPU.
_PROMPT_TASK = (
    "This photo shows a Mario Kart results screen (the end-of-cup or "
    "post-race standings). Extract EVERY row of the standings: the finishing "
    "position, the character name exactly as displayed, and that row's total "
    "points. Include ALL rows — human players and CPU racers alike. For each "
    "row set is_highlighted=true ONLY if that row belongs to a HUMAN player "
    "(never for a CPU), because only the human players' scores matter. The "
    "human marker is the ROW STYLING described below — it is independent of "
    "finishing position (a human can place anywhere)."
)

# Per-edition descriptions of the real human-vs-CPU visual cue(s). These were
# written from real-photo validation: the highlight signal is REAL but easy for
# the model to miss (dark/glare photos, two different Wii screens), so we
# describe the exact cues rather than a vague "brighter row."
_PROMPT_CUES_WII = (
    "This is Mario Kart Wii. There are TWO possible results-screen layouts — "
    "handle whichever one you see:\n"
    "1) A single VERTICAL 12-row final-standings list (positions 1-12, top to "
    "bottom). A HUMAN player's row is drawn as a SOLID, OPAQUE colored bar "
    "(e.g. pink, cyan, orange — the color is per-player, not fixed). A CPU "
    "row is SEMI-TRANSPARENT / TRANSLUCENT — the background track shows "
    "through it. Set is_highlighted=true for the solid opaque (human) rows "
    "only; false for the see-through (CPU) rows.\n"
    "2) A TWO-COLUMN trophy/credits results screen (it says something like "
    "\"You got Nth place!\" with a trophy in the middle; positions 1-6 are in "
    "the LEFT column and 7-12 in the RIGHT column). Read BOTH columns. Here a "
    "HUMAN row is marked by a WHITE / light ROUNDED OUTLINE BOX around the "
    "character nameplate; CPUs have NO outline. Set is_highlighted=true for "
    "the outlined (human) rows only."
)
_PROMPT_CUES_MK8DX = (
    "This is Mario Kart 8 Deluxe (Switch). One vertical 12-row layout "
    "(post-race and end-of-cup look the same). A HUMAN row carries a small "
    "colored PLAYER-NUMBER BADGE — \"P1\", \"P2\", \"P3\" or \"P4\" — next to "
    "the character portrait/name. CPUs have NO player badge (character name "
    "only). A row is human if and only if it shows a P# tag; set "
    "is_highlighted=true for exactly those rows."
)
# When the edition is unknown, describe every cue and let the screen dictate.
_PROMPT_CUES_UNKNOWN = (
    "The edition is unknown, so watch for any of these human-row markers "
    "(whichever the screen uses): a SOLID OPAQUE colored bar vs a "
    "SEMI-TRANSPARENT CPU row (Mario Kart Wii vertical list); a WHITE ROUNDED "
    "OUTLINE BOX around the nameplate (Mario Kart Wii two-column trophy screen "
    "— read both the left column for positions 1-6 and the right for 7-12); "
    "or a \"P1\"/\"P2\"/\"P3\"/\"P4\" player-number badge next to the "
    "character (Mario Kart 8 Deluxe / Switch). Set is_highlighted=true for the "
    "human rows so identified."
)
_PROMPT_ROBUSTNESS = (
    "The photo may be dark, glare-washed, or shot at an angle — do your best; "
    "the human marker is the ROW STYLING, not the position. If a value is "
    "unreadable, make your best guess from what is visible."
)


def build_extraction_prompt(edition=None):
    """Assemble the edition-specific extraction prompt.

    edition: "wii", "mk8dx", or None/unknown. The task + robustness framing is
    shared; the middle section describes the real human-vs-CPU visual cue(s)
    for that edition (or all of them when the edition is unknown).
    """
    if edition == "wii":
        cues = _PROMPT_CUES_WII
    elif edition == "mk8dx":
        cues = _PROMPT_CUES_MK8DX
    else:
        cues = _PROMPT_CUES_UNKNOWN
    return "\n\n".join((_PROMPT_TASK, cues, _PROMPT_ROBUSTNESS))


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
