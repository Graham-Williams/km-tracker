from db import get_connection


def create_player(client, name, default_cup=True, has_line=False):
    data = {"name": name}
    if default_cup:
        data["default_cup"] = "on"
    if has_line:
        data["has_line"] = "on"
    return client.post("/players", data=data, follow_redirects=True)


def create_cup(client, date="", notes="", tz_offset="", player_id="1", score="50"):
    """Create a cup with one score (required). Ensure a player exists first."""
    return client.post(
        "/cups",
        data={
            "date": date,
            "notes": notes,
            "tz_offset": tz_offset,
            "player_ids[]": [player_id],
            "scores[]": [score],
            "lines[]": ["0"],
        },
        follow_redirects=True,
    )


def create_cup_with_scores(client, date, player_scores, lines=None, tiebreaker_ids=None):
    """Create a cup with multiple players and scores.

    player_scores: list of (player_id, score) tuples.
    lines: optional list of line values per player. If None, fetches from DB.
    tiebreaker_ids: optional list of player_ids who won tiebreakers.
    """
    player_ids = [str(pid) for pid, _ in player_scores]
    if lines is None:
        conn = get_connection()
        lines = []
        for pid, _ in player_scores:
            row = conn.execute("SELECT line FROM players WHERE id = ?", (pid,)).fetchone()
            lines.append(str(row["line"]) if row else "0")
        conn.close()
    else:
        lines = [str(l) for l in lines]
    data = {
        "date": date,
        "notes": "",
        "tz_offset": "",
        "player_ids[]": player_ids,
        "scores[]": [str(score) for _, score in player_scores],
        "lines[]": lines,
    }
    if tiebreaker_ids:
        data["tiebreakers[]"] = [str(pid) for pid in tiebreaker_ids]
    return client.post("/cups", data=data, follow_redirects=True)


def create_score(client, cup_id=1, player_id=2, score=100, won_tiebreaker=False):
    data = {
        "cup_id": str(cup_id),
        "player_id": str(player_id),
        "score": str(score),
    }
    if won_tiebreaker:
        data["won_tiebreaker"] = "on"
    return client.post("/scores", data=data, follow_redirects=True)
