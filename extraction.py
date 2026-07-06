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

EXTRACTION_PROMPT = (
    "This photo shows a Mario Kart final standings screen (the end-of-cup "
    "results table). Extract EVERY row of the standings: the finishing "
    "position, the character name exactly as displayed, and that row's total "
    "points. Include all rows — human players and CPU racers alike. If a value "
    "is unreadable, make your best guess from what is visible."
)


class StandingsRow(BaseModel):
    position: int
    character: str
    points: int


class Standings(BaseModel):
    rows: List[StandingsRow]


class ExtractionError(Exception):
    """The Claude API call failed or returned nothing usable."""


def extraction_enabled():
    """Photo extraction is available only when an API key is configured."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def extract_standings(image_b64, media_type):
    """Extract standings rows from a base64-encoded photo. Returns a Standings.

    Raises ExtractionError on any API failure (network, timeout, rate limit,
    unparsable response) so callers can turn it into a clean 502.
    """
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
                        {"type": "text", "text": EXTRACTION_PROMPT},
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
