# BalatroDB SQLite schema

How the JSONL event logs (see [`event-schema.md`](event-schema.md)) become a
queryable database. Every statistic is a SQL query; nothing is aggregated in
the mod.

## Measured corpus

10 real runs, 7374 events, 3.00 MB — roughly **300 KB per run**. Where the
bytes go:

| Event | Count | Total | Avg | Share |
|---|---:|---:|---:|---:|
| `round.end` | 122 | 721 KB | 5911 B | 24% |
| `hand.play` | 364 | 443 KB | 1218 B | 15% |
| `snapshot` | 271 | 437 KB | 1611 B | 15% |
| `state.change` | 2723 | 426 KB | 156 B | 14% |
| `money.change` | 1162 | 161 KB | 138 B | 5% |
| everything else | 2732 | ~810 KB | | 27% |

`round.end` dominates because it carries a full deck sample; `hand.play` and
`snapshot` because they carry joker samples. Those samples are the price of
making the never-scaling jokers observable, and they are the single biggest
lever if the database ever needs to shrink.

Extrapolated: **300 runs ≈ 90 MB of logs, ~220k events, ~400k card rows.**
Comfortably within SQLite's range; no partitioning needed.

## Approach: the logs are the record, the database is an index

**The gzipped JSONL logs are the system of record. The database holds only
derived projections and is disposable** — `--rebuild` regenerates it from the
logs.

An earlier draft kept an `events` table holding every line verbatim. Measured
against the real corpus, that table was **5.14 MB — two-thirds of the database
— to duplicate 0.17 MB of gzipped source**, because JSON-as-text plus indexes
in SQLite is larger than the file it copies. At 1000 runs that is ~776 MB of
database against ~17 MB of logs. Every reason for it (rebuilding projections,
`json_extract` for unanticipated stats, a source of truth inside the DB)
dissolves once the logs are kept: re-reading 17 MB of gzip is trivial and rare.

So there are two layers:

1. **Normalized projections** — `runs`, `segments`, `rounds`, `hands`,
   `joker_scale`, `cards`, `cashout_items`, `money`. What queries actually hit.
2. **Views** — the statistics themselves. Views rather than materialized
   tables, because the stat list is deliberately unstable; that is the whole
   reason the mod aggregates nothing. A view can be redefined without
   re-ingesting.

The one exception to "views, not tables" is per-joker maxima, discussed under
[Performance](#performance).

### Runs point back at their log

`runs.log_file` is the basename of the log the run came from, relative to the
runs directory, and it tolerates either `.jsonl` or `.jsonl.gz`. Two things
depend on it:

- **The in-game viewer.** Its design reads the run list from an index and then
  opens a single run's log to show detail. The database gives it the index; the
  pointer tells it which file to open.
- **Rebuilds and repairs.** Any question the projections cannot answer is one
  file away, addressable rather than requiring a full scan.

A `log_file` whose target is missing means the log was deleted or moved. That
is detectable rather than silent, which is the point.

### Cards get one table

Cards appear nested in **26 distinct (event, role) shapes** in the real corpus
— `round.end.deck[]`, `hand.play.jokers[]`, `hand.play.cards[]`,
`snapshot.jokers[]`, `card.remove.card`, `consumable.use.targets[]`,
`card.modify.from`/`.to`, and twenty more. A column per shape is unmaintainable
and a table per shape is worse.

One `cards` table keyed by `(run_id, seg, n, role, pos)` handles all of them,
and new roles need no migration. `role` is the payload key the card came from;
`pos` is its index in an array, or 0 for a single card.

## Numbers

The wire format delivers a number either plainly or as `{"s": exact, "l":
log10}` past 1e14. SQL `MAX()` over TEXT is lexicographic — `MAX('9','1000')`
is `'9'` — so any column that gets ordered needs a genuinely orderable form.

**Convention: quantities that can grow unboundedly get three columns.**

```
<q>_ord  REAL           -- sign(x) * log10(1 + |x|). Ordering accelerator, NOT exact.
<q>_num  REAL            -- the plain value when it fits a double; NULL otherwise. SUM/AVG this.
<q>_txt  TEXT NOT NULL   -- exact decimal string. Display this.
```

`sign(x) * log10(1 + |x|)` rather than raw `log10` because it is monotonic
across the whole real line including zero and negatives: `ord(0) = 0`,
`ord(5) ≈ 0.78`, `ord(-5) ≈ -0.78`, `ord(1e20) = 20`.

**It is monotonic but not injective, so it is an accelerator rather than an
exact sort key.** Verified collisions: plain `99999999999999` and a wrapped
`1e14` both produce `14.0`; `1e-17` and `0` both produce `0.0`. `MAX(_ord)` can
therefore identify a tie containing unequal values, and picking a row from that
tie arbitrarily reports the wrong maximum. Any query that must be exact has to
break ties on `_txt` with a **numeric** comparison — lexicographic text is not
a valid numeric tie-breaker either.

**Only three quantities need this**: hand and round scores, joker scale values,
and chip totals. Money, ante, deck size, hand counts and levels are bounded by
the game and are plain `INTEGER`. Applying the triple everywhere would triple
the schema for no benefit.

## DDL

Generated from `ingest/schema.sql`, which is authoritative.

```sql
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
  metric  TEXT NOT NULL,      -- hand_plays | skips | stone_cards | tarots
  subject TEXT,               -- the poker hand, for hand_plays
  value   REAL,
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
```

## Ingest

Idempotent, atomic per run, and safe over files still being appended to.

```
for each runs/*.jsonl[.gz]:
    if size+mtime matches what was ingested:  skip
    read every COMPLETE line          # a live run's last line may be partial
    SAVEPOINT
      purge this run's projections
      re-derive them from the whole log
      record the file signature
    RELEASE                            # ROLLBACK on any failure
```

**A whole run is re-derived whenever its log changes.** There is deliberately
no byte-offset tail resume. An earlier version had one, and three independent
reviews reproduced the same corruption: the per-run accumulators (round
numbering especially) lived in memory, so a second process restarted `round_seq`
at 1 and overwrote earlier rounds. A run is ~300 KB and re-deriving one takes
milliseconds, so there is nothing to win and a whole bug class to lose.

The savepoint matters for the same reason: purge-then-fail would otherwise
leave a run deleted and half rebuilt, and the next successful file would commit
that wreckage along with itself.


## The statistics

Every wanted stat, as a query. `:deck`, `:stake` and `:endless` are the
slicing parameters; omit a predicate to aggregate across it.

**Max value per scaling joker** — the headline stat, and the reason the whole
thing exists:

```sql
SELECT js.key, js.field,
       MAX(js.to_ord)                                   AS ord,
       (SELECT to_txt FROM joker_scale x
         WHERE x.key = js.key AND x.field = js.field AND x.endless = js.endless
         ORDER BY x.to_ord DESC LIMIT 1)                AS best,
       (SELECT run_id FROM joker_scale x
         WHERE x.key = js.key AND x.field = js.field AND x.endless = js.endless
         ORDER BY x.to_ord DESC LIMIT 1)                AS run_id
  FROM joker_scale js
  JOIN runs r USING (run_id)
 WHERE js.endless = :endless
   AND (:deck  IS NULL OR r.deck_key  = :deck)
   AND (:stake IS NULL OR r.stake_key = :stake)
   AND js.is_reset = 0
 GROUP BY js.key, js.field;
```

**Decaying jokers inverted** — Ice Cream, Popcorn, Turtle Bean, Ramen. The
interesting number is how long it survived, not its maximum:

```sql
SELECT run_id, key, COUNT(*) AS decay_steps, MIN(to_num) AS lowest
  FROM joker_scale
 WHERE key IN ('j_ice_cream','j_popcorn','j_turtle_bean','j_ramen')
 GROUP BY run_id, key
 ORDER BY decay_steps DESC;
```

**The five never-scaling jokers plus Hiker** — these emit no `joker.scale` at
all, so they come from the per-hand and per-round samples. Their value lives in
the card's `state` JSON:

```sql
SELECT c.key,
       MAX(CAST(json_extract(c.state, '$.mult')  AS REAL)) AS max_mult,
       MAX(CAST(json_extract(c.state, '$.chips') AS REAL)) AS max_chips
  FROM cards c
  JOIN runs  r USING (run_id)
 WHERE c.role = 'jokers'
   AND c.key IN ('j_supernova','j_fortune_teller','j_stone','j_throwback','j_swashbuckler')
   AND c.endless = :endless
 GROUP BY c.key;
```

Hiker is the same query against `role = 'deck'` and `$.perma_bonus`, since its
effect lives on the deck cards rather than the joker.

**Max round money, with attribution** — `round.end` keeps the itemized
breakdown, so this answers *why* as well as *how much*:

```sql
SELECT ro.run_id, ro.round_seq, ro.ante, ro.cashout_total,
       (SELECT group_concat(name || '=' || dollars, ', ')
          FROM cashout_items ci
         WHERE ci.run_id = ro.run_id AND ci.round_seq = ro.round_seq
         ORDER BY ci.dollars DESC)                       AS breakdown
  FROM rounds ro JOIN runs r USING (run_id)
 WHERE ro.endless = :endless AND (:deck IS NULL OR r.deck_key = :deck)
 ORDER BY ro.cashout_total DESC LIMIT 20;
```

**Max round score** — `SUM` of the hands in the round, not
`rounds.score`, which is a point-in-time read:

```sql
SELECT run_id, round_seq, SUM(score_num) AS round_score
  FROM hands WHERE endless = :endless
 GROUP BY run_id, round_seq ORDER BY round_score DESC LIMIT 20;
```

**Max hand level per poker hand:** `SELECT hand, MAX(lvl_to) FROM hand_levels
WHERE endless = :endless GROUP BY hand;`

**Max deck size:** `SELECT MAX(deck_size) FROM rounds WHERE endless = :endless;`

**Max money:** `SELECT MAX(balance) FROM money WHERE endless = :endless;`

`balance` is a running sum from the baseline. **Do not use `before + delta`:**
consecutive queued `ease_dollars` calls all report the same pre-value, so that
form invents peaks that never happened — it reported 135 on a run whose true
peak was 97.

**Max ante:** `SELECT MAX(furthest_ante) FROM runs;` — the game maintains it,
and unlike `MAX(ante)` it is immune to the Hieroglyph/Petroglyph vouchers,
which call `ease_ante(-n)` and make the counter non-monotonic.

**Win rate** — note it tests `won`, never `result`:

```sql
SELECT r.deck_key, r.stake_key,
       COUNT(*) AS runs, SUM(r.won) AS wins,
       ROUND(100.0 * SUM(r.won) / COUNT(*), 1) AS win_pct
  FROM runs r WHERE r.terminal = 1 AND r.seeded = 0
 GROUP BY r.deck_key, r.stake_key;
```

## Validation

Run after every ingest. This is not hygiene theatre — the deck identity caught
two real capture bugs (consumables logged as deck cards, and tarot
enhancements logged as card additions).

The checks run in the ingester as it reads each stream, and land in
`run_defects`:

| Check | What it asserts |
|---|---|
| `deck_identity` | `run.baseline.deck_cards` + `card.add` − `card.remove` = `run.end.deck_size`. `card.modify` must not change the count. |
| `sequence_gap` | `n` is gap-free within each segment |
| `encode_error` | the mod recorded an event it could not encode |
| `won_without_win` | `run.end.won` is true but no `run.win` event was seen |
| `unterminated` | no `run.end` — a crash, or a run still in progress |

```sql
SELECT defect, COUNT(*) FROM run_defects GROUP BY defect;
SELECT * FROM run_defects WHERE defect = 'deck_identity_fail';
```

The format is **sniffed from which events and fields are present**, not from
`v` or `env.balatrodb`: neither moved while the layout changed three times, so
presence is the only reliable signal. A defect flag therefore means "this run
was captured by a build with this known problem", which is what makes old and
new runs safely distinguishable rather than silently mixed.

The deck identity is not hygiene theatre: it caught two real capture bugs
(consumables logged as deck cards, and tarot enhancements logged as card
additions). Runs written by builds with those defects are skipped for that
check rather than reported as failures — `segments.ver_balatrodb` says which.

## Performance

At 300 runs — 220k events, 400k card rows — every query above runs
comfortably as a view on modern hardware. The indexes that matter are
`joker_scale(key, field, endless, to_ord DESC)` and `runs(deck_key, stake_key,
went_endless)`.

**The one candidate for materialization is per-joker maxima**, because the
in-game viewer wants them instantly and they scan the largest table. Materialize
as `records` refreshed at the end of ingest, not as a view — the viewer design
already expects a `records.json` export with a staleness banner. Everything
else stays a view.

The `state.change` events (2723 of 7374, 14% of bytes) are noise for every
statistic here. They earn their place in the logs for replay and debugging, but
the database projects nothing from them.

## Migration

**Version is tracked per segment, not per run**, because a run can span a mod
update: play, quit, update BalatroDB, resume, and seg 0 was written by one
build and seg 1 by another — different capture behaviour inside one file.
`segments.schema_v` is the envelope's `v` (how to parse) and
`segments.ver_balatrodb` is the mod build (which capture defects apply).

**Right now `v` alone is not enough.** The wire format has changed repeatedly
without `v` being bumped — the `run.baseline` split, the `final_round_score`
rename, `consumable.*` routing, the fixed envelope key order all landed under
`v: 1`. Pre-1.0 that is tolerable, but it means **the ingester must key
compatibility off `ver_balatrodb`, not `v`.**

The release stages set what the ingester must support — see
[Compatibility policy](event-schema.md#compatibility-policy). In short: alpha
logs (`v: 1`) are fixtures, not data, and are interpretable only via
`ver_balatrodb`; **beta starts at `v: 2` and every beta version must stay
readable** thereafter. Practically that means the ingester grows a small
per-version normalization layer at beta, not a rewrite.

The corpus already spans several vocabularies:

| Older form | Current form |
|---|---|
| `result: "quit"` / `"loss"` | `died` / `completed` / `abandoned` / … |
| consumables as `card.add` | `consumable.add` |
| tarot enhancement as `card.add` | `card.modify` |
| `run.start` carrying `deck_cards` | separate `run.baseline` |
| `run.end.score` | `final_round_score` + `best_hand` |

The ingester normalizes old vocabularies forward where it can and **flags where
it cannot** rather than silently mixing incomparable data. Runs whose
`card.add` counts include consumables cannot produce a trustworthy deck-size
identity, so they are excluded from that validation rather than reported as
failures.

A schema change is a `--rebuild`, which re-reads the logs. They are the record;
the database is disposable.

## Open: are the logs permanent or disposable?

**Unresolved, and it belongs to the project owner.** The schema works either
way, but the consequences differ:

**Permanent** (gzip on close, DB rebuildable) — the logs are the system of
record and the database is a cache. A schema bug costs a rebuild, not data.
JSONL compresses ~10:1, so 300 runs is ~9 MB. This also keeps replay possible,
since replay needs the ordered decision stream, not the aggregates.

**Disposable** (fold in once, delete) — the database becomes the system of
record, so it must be backed up, a projection bug discovered later is
unfixable for already-deleted runs, and the in-game viewer loses the data
source its design depends on. Saves disk that gzipping largely saves anyway.

**Recommendation: permanent, gzipped.** The stated reason for this whole
architecture is that the statistic list is not final — which is exactly the
argument for keeping the raw input. Deleting logs forecloses the retroactive
recomputation the design exists to enable, and the disk saving is small.
