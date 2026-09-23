-- BalatroDB derived index. The gzipped logs are the system of record; this
-- database is rebuildable from them. See docs/db-schema.md for the reasoning.
--
-- This file is authoritative. `python ingest/sync_schema.py` copies it into
-- the DDL block of docs/db-schema.md so the document cannot drift from it.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = OFF;   -- projections are purged and re-derived per run

-- ── Ingest bookkeeping ────────────────────────────────────────────────
-- `sig` is size+mtime. A changed file means the whole run is re-derived;
-- there is deliberately no byte-offset resume (see the ingester's header).
CREATE TABLE IF NOT EXISTS files (
  path        TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL,
  bytes       INTEGER NOT NULL,
  mtime       REAL    NOT NULL,
  sig         TEXT    NOT NULL,
  ingested_at REAL    NOT NULL
);

-- Capture defects, sniffed from the data rather than trusted from a version
-- string: the mod never bumped `v` or `env.balatrodb` while the layout
-- changed three times, so the presence of events and fields is the only
-- reliable signal.
CREATE TABLE IF NOT EXISTS run_defects (
  run_id TEXT NOT NULL,
  defect TEXT NOT NULL,
  detail TEXT,
  PRIMARY KEY (run_id, defect)
);

-- ── Runs ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS runs (
  run_id       TEXT PRIMARY KEY,
  log_file     TEXT NOT NULL,     -- basename; .jsonl or .jsonl.gz
  log_bytes    INTEGER,
  started_ts   INTEGER NOT NULL,
  seed         TEXT,
  seeded       INTEGER NOT NULL DEFAULT 0,
  challenge    TEXT,
  deck_key     TEXT,              -- b_red etc. Stable; deck_name is localized
  deck_name    TEXT,
  stake        INTEGER,
  stake_key    TEXT,              -- stable across stake-mod changes
  win_ante     INTEGER,
  profile      INTEGER,
  starting_deck_size INTEGER,

  -- Outcome. `won` and `result` are orthogonal: a run that beat the win ante,
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

  best_hand_ord REAL, best_hand_num REAL, best_hand_txt TEXT,
  furthest_ante  INTEGER,
  furthest_round INTEGER,
  final_round_score_ord REAL, final_round_score_num REAL, final_round_score_txt TEXT
);
CREATE INDEX IF NOT EXISTS runs_slice ON runs(deck_key, stake_key, went_endless);

-- Environment lives here, not on runs: a run can span a mod update. Play,
-- quit, update BalatroDB, resume -- and seg 0 was written by one build and
-- seg 1 by another, with different capture behaviour inside one file.
CREATE TABLE IF NOT EXISTS segments (
  run_id   TEXT    NOT NULL,
  seg      INTEGER NOT NULL,
  started_ts INTEGER,
  resumed  INTEGER NOT NULL DEFAULT 0,
  schema_v      INTEGER,
  ver_balatrodb TEXT,
  ver_game      TEXT,
  ver_lovely    TEXT,
  ver_smods     TEXT,
  mods          TEXT,
  PRIMARY KEY (run_id, seg)
);

-- ── Rounds ────────────────────────────────────────────────────────────
-- round_seq is the Nth round.start of the run. It is durable because a whole
-- run is re-derived at once; the envelope's `r` is never used as a key.
CREATE TABLE IF NOT EXISTS rounds (
  run_id     TEXT    NOT NULL,
  round_seq  INTEGER NOT NULL,
  seg        INTEGER NOT NULL,
  ante       INTEGER,
  blind_key  TEXT,
  blind_name TEXT,
  is_boss    INTEGER,
  reward     INTEGER,
  endless    INTEGER NOT NULL,
  -- Event sequence of round.start. Money events carry no round, so this is
  -- what bounds a round's window: ease_dollars is called synchronously inside
  -- evaluate_play, whose AFTER hook emits hand.play, so a hand's money lands
  -- at a LOWER n than the hand itself. Attributing money to the next action
  -- needs a floor, and this is it.
  start_n    INTEGER,
  required_ord REAL, required_num REAL, required_txt TEXT,
  score_ord    REAL, score_num    REAL, score_txt    TEXT,
  cashout_total INTEGER,
  dollars_before INTEGER,
  deck_size  INTEGER,
  -- Per-round scalars folded out of the deck sample, so the 40-50 cards
  -- behind them need not be stored as rows. Every statistic wanted from a
  -- deck sample is one of these.
  deck_stone       INTEGER,   -- Stone Joker reads this
  deck_perma_max   REAL,      -- largest single Hiker bonus
  deck_perma_total REAL,      -- Hiker's accumulated bonus across the deck
  PRIMARY KEY (run_id, round_seq)
);
CREATE INDEX IF NOT EXISTS rounds_slice ON rounds(endless, cashout_total DESC);

CREATE TABLE IF NOT EXISTS cashout_items (
  run_id    TEXT    NOT NULL,
  round_seq INTEGER NOT NULL,
  pos       INTEGER NOT NULL,
  name      TEXT,             -- blind1 / hands / discards / jokerN / tagN / interest
  dollars   INTEGER,
  disp      INTEGER,
  key       TEXT,             -- joker key when the row is a joker payout
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
  hand       TEXT,             -- internal key from G.GAME.last_hand_played
  level      INTEGER,
  oneshot    INTEGER,          -- this hand alone beat the blind
  score_ord  REAL, score_num REAL, score_txt TEXT,
  chips_before_ord REAL, chips_before_num REAL, chips_before_txt TEXT,
  hands_left_before INTEGER,
  discards_left_before INTEGER,
  -- Engine clock (love.timer) at the moment the event was emitted.
  -- Money is attributed to an action by comparing these: a play's
  -- money resolves in the SAME frame, a discard's a beat later.
  t         REAL,
  PRIMARY KEY (run_id, seg, n)
);
CREATE INDEX IF NOT EXISTS hands_round ON hands(run_id, round_seq);
CREATE INDEX IF NOT EXISTS hands_best  ON hands(endless, score_ord DESC);

-- Discards, so a round's timeline can interleave plays and discards. The
-- cards themselves are already in `cards` at the same (seg, n).
CREATE TABLE IF NOT EXISTS discards (
  run_id    TEXT NOT NULL,
  seg       INTEGER NOT NULL,
  n         INTEGER NOT NULL,
  round_seq INTEGER,
  ante      INTEGER,
  endless   INTEGER NOT NULL,
  cards     INTEGER,
  -- Engine clock (love.timer) at the moment the event was emitted.
  -- Money is attributed to an action by comparing these: a play's
  -- money resolves in the SAME frame, a discard's a beat later.
  t         REAL,
  PRIMARY KEY (run_id, seg, n)
);
CREATE INDEX IF NOT EXISTS discards_round ON discards(run_id, round_seq);

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
  card_id  INTEGER,           -- sort_id; distinguishes two copies of one joker
  key      TEXT    NOT NULL,
  field    TEXT    NOT NULL,  -- mult / chips / x_mult / h_size / ...
  ante     INTEGER,
  endless  INTEGER NOT NULL,
  is_reset INTEGER NOT NULL DEFAULT 0,
  from_ord REAL, from_num REAL, from_txt TEXT,
  to_ord   REAL, to_num   REAL, to_txt   TEXT,
  PRIMARY KEY (run_id, seg, n)
);
CREATE INDEX IF NOT EXISTS joker_scale_max ON joker_scale(key, field, endless, to_ord DESC);

-- Joker samples: one row per joker per observation, with numeric state
-- hoisted into columns so no query needs json_extract.
CREATE TABLE IF NOT EXISTS joker_state (
  run_id    TEXT    NOT NULL,
  seg       INTEGER NOT NULL,
  n         INTEGER NOT NULL,
  round_seq INTEGER,
  ante      INTEGER,
  endless   INTEGER NOT NULL,
  -- Board position, left to right. Joker order decides scoring order in
  -- Balatro, so it is part of the observation -- card_id is creation order
  -- and says nothing about where a joker sits.
  pos       INTEGER,
  card_id   INTEGER,
  key       TEXT,
  mult      REAL,
  x_mult    REAL,
  chips     REAL,
  extra     REAL,
  stone_tally REAL,
  perma_bonus REAL,
  state     TEXT,
  PRIMARY KEY (run_id, seg, n, card_id)
);
CREATE INDEX IF NOT EXISTS joker_state_key ON joker_state(key, endless);

-- Game counters that certain jokers read INSTEAD of their own ability fields,
-- so their value is a function of run history and can never come from a
-- sample: Supernova (times this hand was played), Throwback (blinds skipped),
-- Stone Joker (stone cards in deck), Fortune Teller (tarots used).
CREATE TABLE IF NOT EXISTS joker_derived (
  run_id  TEXT NOT NULL,
  seg     INTEGER NOT NULL,
  n       INTEGER NOT NULL,
  endless INTEGER NOT NULL,
  metric  TEXT NOT NULL,      -- hand_plays | skips | stone_cards | tarots | dollars
  subject TEXT,               -- the poker hand, for hand_plays
  value   REAL,
  -- Whether the joker that reads this counter was actually in hand at the
  -- time. Without it the board credited a run with "Fortune Teller 83" when
  -- that run never held one -- 83 tarots were simply used. Both readings are
  -- worth having: held is the joker's real peak, and the counter regardless
  -- is what it WOULD have been worth.
  held    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (run_id, seg, n, metric, subject)
);
CREATE INDEX IF NOT EXISTS joker_derived_metric ON joker_derived(metric, endless, value DESC);

CREATE TABLE IF NOT EXISTS blind_skips (
  run_id  TEXT NOT NULL,
  seg     INTEGER NOT NULL,
  n       INTEGER NOT NULL,
  ante    INTEGER,
  endless INTEGER NOT NULL,
  tag     TEXT,
  PRIMARY KEY (run_id, seg, n)
);

CREATE TABLE IF NOT EXISTS consumable_uses (
  run_id  TEXT NOT NULL,
  seg     INTEGER NOT NULL,
  n       INTEGER NOT NULL,
  ante    INTEGER,
  endless INTEGER NOT NULL,
  key     TEXT,
  set_    TEXT,               -- Tarot / Planet / Spectral; Fortune Teller counts Tarot
  PRIMARY KEY (run_id, seg, n)
);

-- ── Cards ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cards (
  run_id   TEXT    NOT NULL,
  seg      INTEGER NOT NULL,
  n        INTEGER NOT NULL,
  role     TEXT    NOT NULL,   -- payload key the card came from
  pos      INTEGER NOT NULL,   -- index in an array, 0 for a single card
  -- Denormalized from the envelope: with no events table there is nothing
  -- else to join against, and every card query slices by these.
  ante     INTEGER,
  endless  INTEGER NOT NULL,
  event    TEXT    NOT NULL,
  card_id  INTEGER,            -- sort_id
  key      TEXT,
  name     TEXT,
  set_     TEXT,
  rank     TEXT,
  suit     TEXT,
  enhancement TEXT,
  edition  TEXT,
  seal     TEXT,
  stickers TEXT,
  sell_cost INTEGER,
  state    TEXT,
  PRIMARY KEY (run_id, seg, n, role, pos)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS cards_key ON cards(key, role, endless);

-- ── Money ─────────────────────────────────────────────────────────────
-- `balance` is a running sum from the baseline. `before` is retained but is
-- NOT reliable: consecutive queued ease_dollars calls all report the same
-- pre-value, so before+delta invents peaks that never occurred.
-- Records as they stood AT THE TIME. A row means: when this run was played,
-- this value beat every earlier run's. That is not the same question as
-- "is it the record now" -- a run keeps the moment it held it, even after
-- something later beats it.
--
-- Derived over the whole corpus in run order, so it is rebuilt wholesale
-- rather than per run: adding an OLD log would shift what came after it.
-- Endless and non-endless records are separate contests, so the two are
-- derived independently and stored side by side: a run can hold the
-- non-endless best for a hand and a different, higher endless best for the
-- same hand, and both are true. Nothing here is computed at query time.
CREATE TABLE IF NOT EXISTS run_records (
  run_id    TEXT NOT NULL,
  kind      TEXT NOT NULL,   -- joker | hand_score | hand_level | hand_played
  subject   TEXT NOT NULL,   -- joker key, or poker hand
  endless   INTEGER NOT NULL,
  value_txt TEXT,
  value_ord REAL,
  -- What this beat: the value it displaced and the run that had held it.
  -- Known only while walking the corpus in order, so it is recorded here
  -- rather than reconstructed by a query that would have to re-derive the
  -- same chronology.
  prev_txt  TEXT,
  prev_run  TEXT,
  PRIMARY KEY (run_id, kind, subject, endless)
);
CREATE INDEX IF NOT EXISTS run_records_run ON run_records(run_id);

-- The two records a counter joker can hold, which are different questions.
--
--   contributed: the counter's value at a moment the joker was actually
--                scoring -- a hand played while it was in hand. This is the
--                joker's own record.
--   ambient:     the highest the counter reached at all, whether or not
--                anyone held the joker. A Fortune Teller record set without
--                ever owning a Fortune Teller is this one, and it is a real
--                record about the run rather than about the joker.
--
-- Peak dollars mid-shop is not a Bull score, which is why contributed is
-- evaluated at plays rather than over the whole series.
CREATE TABLE IF NOT EXISTS joker_counter_peaks (
  run_id      TEXT NOT NULL,
  metric      TEXT NOT NULL,
  endless     INTEGER NOT NULL,
  contributed REAL,
  ambient     REAL,
  PRIMARY KEY (run_id, metric, endless)
);

CREATE TABLE IF NOT EXISTS money (
  run_id  TEXT    NOT NULL,
  seg     INTEGER NOT NULL,
  n       INTEGER NOT NULL,
  ante    INTEGER,
  endless INTEGER NOT NULL,
  delta   INTEGER,
  before  INTEGER,
  balance INTEGER,
  -- What earned or spent it: the event type the change was traced back to
  -- ('hand.play', 'shop.sell', 'round.end', ...), and, when that was a play
  -- or a discard, that action's `n`. Derived here because it needs the whole
  -- ordered stream, which only ingest has.
  cause    TEXT,
  cause_n  INTEGER,
  -- Engine clock (love.timer) at the moment the event was emitted.
  -- Money is attributed to an action by comparing these: a play's
  -- money resolves in the SAME frame, a discard's a beat later.
  t         REAL,
  PRIMARY KEY (run_id, seg, n)
);
CREATE INDEX IF NOT EXISTS money_peak ON money(endless, balance DESC);
CREATE INDEX IF NOT EXISTS money_cause ON money(run_id, seg, cause_n);
