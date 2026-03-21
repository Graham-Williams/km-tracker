CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS cups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATE NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cup_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    score INTEGER NOT NULL,
    won_tiebreaker BOOLEAN,  -- nullable: ties are allowed and don't always need to be broken
    FOREIGN KEY (cup_id) REFERENCES cups(id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    UNIQUE(cup_id, player_id)
);
