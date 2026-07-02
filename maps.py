# Track lists, keyed by game edition.
#
# The house rules (race count, vetoes, votoes, line deltas) are identical for
# every edition — only the track list differs. See app.py for the rules.

WII_COURSES = [
    # Nitro (Mushroom Cup)
    "Luigi Circuit",
    "Moo Moo Meadows",
    "Mushroom Gorge",
    "Toad's Factory",
    # Nitro (Flower Cup)
    "Mario Circuit",
    "Coconut Mall",
    "DK Summit",
    "Wario's Gold Mine",
    # Nitro (Star Cup)
    "Daisy Circuit",
    "Koopa Cape",
    "Maple Treeway",
    "Grumble Volcano",
    # Nitro (Special Cup)
    "Dry Dry Ruins",
    "Moonview Highway",
    "Bowser's Castle",
    "Rainbow Road",
    # Retro (Shell Cup)
    "GCN Peach Beach",
    "DS Yoshi Falls",
    "SNES Ghost Valley 2",
    "N64 Mario Raceway",
    # Retro (Banana Cup)
    "N64 Sherbet Land",
    "GBA Shy Guy Beach",
    "DS Delfino Square",
    "GCN Waluigi Stadium",
    # Retro (Leaf Cup)
    "DS Desert Hills",
    "GBA Bowser Castle 3",
    "N64 DK's Jungle Parkway",
    "GCN Mario Circuit",
    # Retro (Lightning Cup)
    "SNES Mario Circuit 3",
    "DS Peach Gardens",
    "GCN DK Mountain",
    "N64 Bowser's Castle",
]

# Mario Kart 8 Deluxe — all 96 tracks (48 base + 48 Booster Course Pass),
# organized by their 24 cups.
MK8DX_COURSES = [
    # --- Base game (48) ---
    # Mushroom Cup
    "Mario Kart Stadium",
    "Water Park",
    "Sweet Sweet Canyon",
    "Thwomp Ruins",
    # Flower Cup
    "Mario Circuit",
    "Toad Harbor",
    "Twisted Mansion",
    "Shy Guy Falls",
    # Star Cup
    "Sunshine Airport",
    "Dolphin Shoals",
    "Electrodrome",
    "Mount Wario",
    # Special Cup
    "Cloudtop Cruise",
    "Bone-Dry Dunes",
    "Bowser's Castle",
    "Rainbow Road",
    # Shell Cup
    "Wii Moo Moo Meadows",
    "GBA Mario Circuit",
    "DS Cheep Cheep Beach",
    "N64 Toad's Turnpike",
    # Banana Cup
    "GCN Dry Dry Desert",
    "SNES Donut Plains 3",
    "N64 Royal Raceway",
    "3DS DK Jungle",
    # Leaf Cup
    "DS Wario Stadium",
    "GCN Sherbet Land",
    "3DS Music Park",
    "N64 Yoshi Valley",
    # Lightning Cup
    "DS Tick-Tock Clock",
    "3DS Piranha Plant Slide",
    "Wii Grumble Volcano",
    "N64 Rainbow Road",
    # Egg Cup
    "GCN Yoshi Circuit",
    "Excitebike Arena",
    "Dragon Driftway",
    "Mute City",
    # Triforce Cup
    "Wii Wario's Gold Mine",
    "SNES Rainbow Road",
    "Ice Ice Outpost",
    "Hyrule Circuit",
    # Crossing Cup
    "GCN Baby Park",
    "GBA Cheese Land",
    "Wild Woods",
    "Animal Crossing",
    # Bell Cup
    "3DS Neo Bowser City",
    "GBA Ribbon Road",
    "Super Bell Subway",
    "Big Blue",
    # --- Booster Course Pass (48) ---
    # Golden Dash Cup
    "Tour Paris Promenade",
    "3DS Toad Circuit",
    "N64 Choco Mountain",
    "Wii Coconut Mall",
    # Lucky Cat Cup
    "Tour Tokyo Blur",
    "DS Shroom Ridge",
    "GBA Sky Garden",
    "Ninja Hideaway",
    # Turnip Cup
    "Tour New York Minute",
    "SNES Mario Circuit 3",
    "N64 Kalimari Desert",
    "DS Waluigi Pinball",
    # Propeller Cup
    "Tour Sydney Sprint",
    "GBA Snow Land",
    "Wii Mushroom Gorge",
    "Sky-High Sundae",
    # Rock Cup
    "Tour London Loop",
    "GBA Boo Lake",
    "3DS Rock Rock Mountain",
    "Wii Maple Treeway",
    # Moon Cup
    "Tour Berlin Byways",
    "DS Peach Gardens",
    "Merry Mountain",
    "3DS Rainbow Road",
    # Fruit Cup
    "Tour Amsterdam Drift",
    "GBA Riverside Park",
    "Wii DK Summit",
    "Yoshi's Island",
    # Boomerang Cup
    "Tour Bangkok Rush",
    "DS Mario Circuit",
    "GCN Waluigi Stadium",
    "Tour Singapore Speedway",
    # Feather Cup
    "Tour Athens Dash",
    "GCN Daisy Cruiser",
    "Wii Moonview Highway",
    "Squeaky Clean Sprint",
    # Cherry Cup
    "Tour Los Angeles Laps",
    "GBA Sunset Wilds",
    "Wii Koopa Cape",
    "Tour Vancouver Velocity",
    # Acorn Cup
    "Tour Rome Avanti",
    "GCN DK Mountain",
    "Wii Daisy Circuit",
    "Piranha Plant Cove",
    # Spiny Cup
    "Tour Madrid Drive",
    "3DS Rosalina's Ice World",
    "SNES Bowser Castle 3",
    "Wii Rainbow Road",
]

# Track lists keyed by edition. The DB stores the edition string on each cup.
TRACK_SETS = {
    "wii": WII_COURSES,
    "mk8dx": MK8DX_COURSES,
}

# Human-readable labels for display.
EDITION_LABELS = {
    "wii": "Wii",
    "mk8dx": "Switch",
}

DEFAULT_EDITION = "wii"


def courses_for(edition):
    """Return the track list for an edition, falling back to the default."""
    return TRACK_SETS.get(edition, TRACK_SETS[DEFAULT_EDITION])


def edition_label(edition):
    """Return a human-readable label for an edition string."""
    return EDITION_LABELS.get(edition, EDITION_LABELS[DEFAULT_EDITION])


# Backward-compat: existing importers expect a flat `COURSES` = the Wii list.
COURSES = WII_COURSES
