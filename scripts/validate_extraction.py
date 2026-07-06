#!/usr/bin/env python3
"""Dev-only: validate photo score extraction against the real Claude API.

Takes a path to a photo of a Mario Kart final standings screen, calls the API
with the exact same prompt + schema as the /extract-scores route (both import
from extraction.py), and prints the extracted rows. Use this to sanity-check
real phone photos before/after tuning the prompt.

Usage:
    ANTHROPIC_API_KEY=... python scripts/validate_extraction.py path/to/photo.jpg

Costs one real API call per run. Never invoked by the app or the test suite.
"""

import base64
import mimetypes
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from extraction import ExtractionError, extract_standings, extraction_enabled  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    if not extraction_enabled():
        print("ANTHROPIC_API_KEY is not set — export it first.")
        return 2

    path = sys.argv[1]
    mime_type = mimetypes.guess_type(path)[0]
    if mime_type not in ("image/jpeg", "image/png"):
        print(f"Unsupported image type {mime_type!r} — use a .jpg/.jpeg/.png file.")
        return 2

    with open(path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("ascii")

    try:
        standings = extract_standings(image_b64, mime_type)
    except ExtractionError as e:
        print(f"Extraction failed: {e}")
        return 1

    print(f"{'Pos':>3}  {'Character':<20} {'Points':>6}")
    for row in standings.rows:
        print(f"{row.position:>3}  {row.character:<20} {row.points:>6}")
    print(f"\n{len(standings.rows)} rows extracted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
