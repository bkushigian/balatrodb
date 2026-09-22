-- Generated from docs/db-schema.md (the DDL block there is authoritative).
-- Regenerate: python ingest/sync_schema.py

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── Ingest bookkeeping ────────────────────────────────────────────────
-- Validation findings, written by the ingester. Without an events table there
-- is no SQL spine to check against, so the checks run over the stream while it
-- is being read -- which is cheaper anyway, since ingest already walks it.
CREATE TABLE IF NOT EXISTS ingest_issues (
  run_id TEXT NOT NULL,
  check_ TEXT NOT NULL,
  detail TEXT,
  PRIMARY KEY (run_id, check_)
);

CREATE TABLE IF NOT EXISTS files (
  path        TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL,
  bytes_read  INTEGER NOT NULL,   -- resume offset; logs grow while a run is live
  mtime       REAL    NOT NULL,
  lines       INTEGER NOT NULL,
  ingested_at REAL    NOT NULL
);

-- ── Runs ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
  run_id       TEXT PRIMARY KEY,
  log_file     TEXT NOT NULL,     -- basename, relative to the runs dir; .jsonl or .jsonl.gz
  log_bytes    INTEGER,
  started_ts   INTEGER NOT NULL,
  seed         TEXT,
  seeded       INTEGER NOT NULL DEFAULT 0,
  challenge    TEXT,
  deck_key     TEXT,              -- b_red etc. Stable; `deck` is a localized display name
  deck_name    TEXT,
  stake        INTEGER,
  stake_key    TEXT,              -- stable across stake-mod changes, unlike the index
  win_ante     INTEGER,
  profile      INTEGER,
  starting_deck_size INTEGER,

  -- outcome. `won` and `result` are orthogonal: a run that beat the win ante,
  -- continued into endless and died is won=1, result='died'.
  won          INTEGER,
  result       TEXT,
  terminal     INTEGER,
  ended_ante   INTEGER,
  ended_round  INTEGER,
  hands_played INTEGER,
  skips        INTEGER,
  final_dollars INTEGER,
  deck_size    INTEGER,
  went_endless INTEGER NOT NULL DEFAULT 0,

  -- bests, maintained by the game itself (check_and_set_high_score)
  best_hand_ord REAL, best_hand_num REAL, best_hand_txt TEXT,
  furthest_ante  INTEGER,
  furthest_round INTEGER,
  final_round_score_ord REAL, final_round_score_num REAL, final_round_score_txt TEXT
);
CREATE INDEX IF NOT EXISTS runs_slice ON runs(deck_key, stake_key, went_endless);

-- Environment lives here, not on runs: a run can span a mod update. Play,
-- quit, update BalatroDB, resume -- and seg 0 was written by one build and
-- seg 1 by another, with different capture behaviour in the same file.
CREATE TABLE IF NOT EXISTS segments (
  run_id   TEXT    NOT NULL REFERENCES runs(run_id),
  seg      INTEGER NOT NULL,
  started_ts INTEGER,
  resumed  INTEGER NOT NULL DEFAULT 0,
  schema_v      INTEGER,          -- envelope `v`, how to parse
  ver_balatrodb TEXT,             -- which capture defects apply
  ver_game      TEXT,
  ver_lovely    TEXT,
  ver_smods     TEXT,
  mods          TEXT,             -- JSON array, id+version
  PRIMARY KEY (run_id, seg)
);

-- ── Rounds ────────────────────────────────────────────────────────────
-- round_seq is synthetic: the Nth round.start of the run. The envelope's `r`
-- is NOT a reliable key -- ease_round is queued, so it lags at boundaries.
CREATE TABLE IF NOT EXISTS rounds (
  run_id     TEXT    NOT NULL REFERENCES runs(run_id),
  round_seq  INTEGER NOT NULL,
  seg        INTEGER NOT NULL,
  ante       INTEGER,
  blind_key  TEXT,
  blind_name TEXT,
  is_boss    INTEGER,
  reward     INTEGER,
  endless    INTEGER NOT NULL,
  required_ord REAL, required_num REAL, required_txt TEXT,
  score_ord    REAL, score_num    REAL, score_txt    TEXT,
  cashout_total INTEGER,
  dollars_before INTEGER,
  deck_size  INTEGER,
  PRIMARY KEY (run_id, round_seq)
);
CREATE INDEX IF NOT EXISTS rounds_cashout ON rounds(cashout_total DESC);
CREATE INDEX IF NOT EXISTS rounds_score   ON rounds(score_ord DESC);

CREATE TABLE IF NOT EXISTS cashout_items (
  run_id    TEXT    NOT NULL,
  round_seq INTEGER NOT NULL,
  pos       INTEGER NOT NULL,
  name      TEXT,               -- blind1 / hands / discards / jokerN / tagN / interest
  dollars   INTEGER,
  disp      INTEGER,
  key       TEXT,               -- joker key when the row is a joker payout
  PRIMARY KEY (run_id, round_seq, pos)
);

-- ── Hands ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hands (
  run_id     TEXT    NOT NULL,
  seg        INTEGER NOT NULL,
  n          INTEGER NOT NULL,
  round_seq  INTEGER,
  ante       INTEGER,
  endless    INTEGER NOT NULL,
  hand       TEXT,              -- internal key from G.GAME.last_hand_played
  level      INTEGER,
  oneshot    INTEGER,           -- this hand alone beat the blind; NOT "blind cleared"
  score_ord  REAL, score_num REAL, score_txt TEXT,
  chips_before_ord REAL, chips_before_num REAL, chips_before_txt TEXT,
  hands_left_before INTEGER,
  discards_left_before INTEGER,
  PRIMARY KEY (run_id, seg, n)
);
CREATE INDEX IF NOT EXISTS hands_score ON hands(score_ord DESC);
CREATE INDEX IF NOT EXISTS hands_name  ON hands(hand, score_ord DESC);

CREATE TABLE IF NOT EXISTS hand_levels (
  run_id    TEXT NOT NULL,
  seg       INTEGER NOT NULL,
  n         INTEGER NOT NULL,
  endless   INTEGER NOT NULL,
  hand      TEXT NOT NULL,
  lvl_from  INTEGER,
  lvl_to    INTEGER,
  amount    INTEGER,
  PRIMARY KEY (run_id, seg, n)
);
CREATE INDEX IF NOT EXISTS hand_levels_max ON hand_levels(hand, lvl_to DESC);

-- ── Jokers ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS joker_scale (
  run_id   TEXT    NOT NULL,
  seg      INTEGER NOT NULL,
  n        INTEGER NOT NULL,
  card_id  INTEGER,            -- sort_id; distinguishes two copies of one joker
  key      TEXT    NOT NULL,
  field    TEXT    NOT NULL,   -- mult / chips / x_mult / h_size / ...
  ante     INTEGER,
  endless  INTEGER NOT NULL,
  is_reset INTEGER NOT NULL DEFAULT 0,   -- joker.reset rather than joker.scale
  from_ord REAL, from_num REAL, from_txt TEXT,
  to_ord   REAL, to_num   REAL, to_txt   TEXT,
  PRIMARY KEY (run_id, seg, n)
);
CREATE INDEX IF NOT EXISTS joker_scale_max ON joker_scale(key, field, endless, to_ord DESC);

-- ── Cards (all 26 nesting shapes) ─────────────────────────────────────
CREATE TABLE IF NOT EXISTS cards (
  run_id   TEXT    NOT NULL,
  seg      INTEGER NOT NULL,
  n        INTEGER NOT NULL,
  role     TEXT    NOT NULL,   -- payload key: deck / jokers / cards / targets / card / from / to
  pos      INTEGER NOT NULL,   -- index in array, 0 for a single card
  -- denormalized from the envelope: without an events table there is nothing
  -- else to join against, and every card query slices by these
  ante     INTEGER,
  endless  INTEGER NOT NULL,
  event    TEXT    NOT NULL,   -- the event type the card came from
  card_id  INTEGER,            -- sort_id
  key      TEXT,
  name     TEXT,
  set_     TEXT,
  rank     TEXT,
  suit     TEXT,
  enhancement TEXT,
  edition  TEXT,
  seal     TEXT,
  stickers TEXT,               -- JSON array
  sell_cost INTEGER,
  state    TEXT,               -- JSON object of numeric ability fields
  PRIMARY KEY (run_id, seg, n, role, pos)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS cards_key ON cards(key, role, endless);

-- ── Money ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS money (
  run_id  TEXT    NOT NULL,
  seg     INTEGER NOT NULL,
  n       INTEGER NOT NULL,
  ante    INTEGER,
  endless INTEGER NOT NULL,
  delta   INTEGER,
  before  INTEGER,
  PRIMARY KEY (run_id, seg, n)
);
