CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    default_cup BOOLEAN NOT NULL DEFAULT 1,
    line INTEGER NOT NULL DEFAULT 0,
    has_line BOOLEAN NOT NULL DEFAULT 0,
    default_character_wii TEXT,     -- character this player mains in MK Wii (photo score matching)
    default_character_switch TEXT   -- character this player mains in MK8 Deluxe (photo score matching)
);

CREATE TABLE IF NOT EXISTS cups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date DATETIME NOT NULL UNIQUE,
    notes TEXT,
    deleted_at DATETIME,
    status TEXT NOT NULL DEFAULT 'completed',
    voto_count INTEGER NOT NULL DEFAULT 0,
    game_edition TEXT NOT NULL DEFAULT 'wii',
    first_edition TEXT              -- mixed cups only: coin-flip winner ('wii'|'mk8dx'); NULL for pure cups
);

CREATE TABLE IF NOT EXISTS cup_players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cup_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    half_veto_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (cup_id) REFERENCES cups(id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    UNIQUE(cup_id, player_id)
);

CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cup_id INTEGER NOT NULL,
    race_number INTEGER NOT NULL,
    map TEXT NOT NULL,
    FOREIGN KEY (cup_id) REFERENCES cups(id),
    UNIQUE(cup_id, race_number)
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

CREATE TABLE IF NOT EXISTS cup_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cup_id INTEGER NOT NULL,
    image BLOB NOT NULL,
    mime_type TEXT NOT NULL,
    created_at DATETIME NOT NULL,
    block INTEGER,          -- mixed cups: which BLOCK (1 = first console, 2 = second) this
                            -- screen belongs to. NULL = "the cup's photo" (every pure cup,
                            -- and every row written before per-block photos existed).
                            -- Tagged by ORDINAL, never an edition string: the console is
                            -- derived from (game_edition, first_edition, block) so a photo
                            -- row can never disagree with the cup's coin flip.
    FOREIGN KEY (cup_id) REFERENCES cups(id)
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cup_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    score INTEGER NOT NULL,
    line INTEGER NOT NULL DEFAULT 0,
    line_score INTEGER NOT NULL,
    won_tiebreaker BOOLEAN,  -- nullable: ties are allowed and don't always need to be broken
    block1_score INTEGER,    -- mixed cups only: this player's points on the FIRST console.
    block2_score INTEGER,    -- mixed cups only: their points on the SECOND console.
                             -- Both NULL for pure cups (and for a mixed cup entered as a
                             -- plain total). `score` always holds the cup TOTAL; when the
                             -- blocks are set, score == block1_score + block2_score by
                             -- construction — save_scores DELETEs + re-INSERTs every row,
                             -- so any write path that doesn't know about blocks writes NULL
                             -- rather than leaving a stale half behind.
    FOREIGN KEY (cup_id) REFERENCES cups(id),
    FOREIGN KEY (player_id) REFERENCES players(id),
    UNIQUE(cup_id, player_id)
);
