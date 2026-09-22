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

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── Ingest bookkeeping ────────────────────────────────────────────────
-- Validation findings, written by the ingester. Without an events table there
-- is no SQL spine to check against, so the checks run over the stream while it
-- is being read -- which is cheaper anyway, since ingest already walks it.
CREATE TABLE ingest_issues (
  run_id TEXT NOT NULL,
  check_ TEXT NOT NULL,
  detail TEXT,
  PRIMARY KEY (run_id, check_)
);

CREATE TABLE files (
  path        TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL,
  bytes_read  INTEGER NOT NULL,   -- resume offset; logs grow while a run is live
  mtime       REAL    NOT NULL,
  lines       INTEGER NOT NULL,
  ingested_at REAL    NOT NULL
);

-- ── Runs ──────────────────────────────────────────────────────────────
CREATE TABLE runs (
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
CREATE INDEX runs_slice ON runs(deck_key, stake_key, went_endless);

-- Environment lives here, not on runs: a run can span a mod update. Play,
-- quit, update BalatroDB, resume -- and seg 0 was written by one build and
-- seg 1 by another, with different capture behaviour in the same file.
CREATE TABLE segments (
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
CREATE TABLE rounds (
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
CREATE INDEX rounds_cashout ON rounds(cashout_total DESC);
CREATE INDEX rounds_score   ON rounds(score_ord DESC);

CREATE TABLE cashout_items (
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
CREATE TABLE hands (
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
CREATE INDEX hands_score ON hands(score_ord DESC);
CREATE INDEX hands_name  ON hands(hand, score_ord DESC);

CREATE TABLE hand_levels (
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
CREATE INDEX hand_levels_max ON hand_levels(hand, lvl_to DESC);

-- ── Jokers ────────────────────────────────────────────────────────────
CREATE TABLE joker_scale (
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
CREATE INDEX joker_scale_max ON joker_scale(key, field, endless, to_ord DESC);

-- ── Cards (all 26 nesting shapes) ─────────────────────────────────────
CREATE TABLE cards (
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
CREATE INDEX cards_key ON cards(key, role, endless);

-- ── Money ─────────────────────────────────────────────────────────────
CREATE TABLE money (
  run_id  TEXT    NOT NULL,
  seg     INTEGER NOT NULL,
  n       INTEGER NOT NULL,
  ante    INTEGER,
  endless INTEGER NOT NULL,
  delta   INTEGER,
  before  INTEGER,
  PRIMARY KEY (run_id, seg, n)
);
```

## Ingest

Idempotent, resumable, and safe over files that are still growing.

```
for each runs/*.jsonl:
    row = files[path]
    if row and row.mtime == mtime and row.bytes_read == size:  skip
    seek to row.bytes_read (0 if new)
    read to the last complete newline      # a live run may have a partial line
    for each line: parse, INSERT OR REPLACE into the projections, run checks
    files[path] = (size_consumed, mtime, lines, now)
```

Three properties that matter:

- **`INSERT OR REPLACE` keyed on `(run_id, seg, n)`** makes re-ingest a no-op
  rather than a duplication, so a full rebuild and an incremental update are
  the same code path.
- **Never consume a partial trailing line.** The mod buffers and flushes on
  thresholds, so the last line of a live run's file can be truncated. Stop at
  the last `\n`.
- **`bytes_read` is the resume point**, so an ingest of 300 runs after playing
  one costs one file's tail, not 90 MB.

A `--rebuild` flag drops the database and re-reads the logs. That is the only
rebuild path, and it is cheap: 17 MB of gzip for 1000 runs.

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

**Max money:** `SELECT MAX(before + delta) FROM money WHERE endless = :endless;`

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
`ingest_issues`:

| Check | What it asserts |
|---|---|
| `deck_identity` | `run.baseline.deck_cards` + `card.add` − `card.remove` = `run.end.deck_size`. `card.modify` must not change the count. |
| `sequence_gap` | `n` is gap-free within each segment |
| `encode_error` | the mod recorded an event it could not encode |
| `won_without_win` | `run.end.won` is true but no `run.win` event was seen |
| `unterminated` | no `run.end` — a crash, or a run still in progress |

```sql
SELECT check_, COUNT(*) FROM ingest_issues GROUP BY check_;
SELECT * FROM ingest_issues WHERE check_ = 'deck_identity';
```

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
