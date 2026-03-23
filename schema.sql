CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    default_cup BOOLEAN NOT NULL DEFAULT 1,
    line INTEGER NOT NULL DEFAULT 0,
    has_line BOOLEAN NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATETIME NOT NULL UNIQUE,
    notes TEXT,
    deleted_at DATETIME
);

CREATE TABLE IF NOT EXISTS line_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cup_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    line_before INTEGER NOT NULL,
    line_after INTEGER NOT NULL,
    FOREIGN KEY (cup_id) REFERENCES cups(id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    UNIQUE(cup_id, player_id)
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cup_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    score INTEGER NOT NULL,
    line INTEGER NOT NULL DEFAULT 0,
    line_score INTEGER NOT NULL,
    won_tiebreaker BOOLEAN,  -- nullable: ties are allowed and don't always need to be broken
    FOREIGN KEY (cup_id) REFERENCES cups(id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    UNIQUE(cup_id, player_id)
);
