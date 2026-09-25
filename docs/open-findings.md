# Open findings

What three reviews turned up that is **not yet fixed**, plus the things this
project already knows are wrong. Written down because it otherwise lived only
in a chat log.

Ordered by what it costs you to leave alone. The top section matters most
right now: a feature freeze means collecting a corpus, and a log written
wrong today cannot be repaired later — the log is the system of record.

---

## 1. Wrong data, silently

These produce a plausible number rather than an obvious failure, which is the
worst kind. Fix before collecting a corpus you intend to keep.

### `money.change.before` is the post-change balance for five sources
`mod/BalatroDB/src/hooks.lua:493`

The hook is after-only, on the assumption that no gameplay call site passes
`instant` to `ease_dollars`. Five do, and they apply the change synchronously:

| site | what |
|---|---|
| `blind.lua:611` | The Ox — sets money to $0 |
| `card.lua:1784` | Wraith — sets money to $0 |
| `card.lua:1710` | The Hermit — doubles money |
| `card.lua:1718` | Temperance |
| `tag.lua:205` | Economy Tag — doubles money |

`delta` stays exact (read from the argument); `before` does not. For Ox and
Wraith the ingester's `after = before + delta` yields **negative money that
never existed**. Confirmed in `1790055560-CX8GHTIX-574a.jsonl`:
`{"delta":18,"before":36}` right after the balance reached 18.

Fix: `hook_around`, capture `G.GAME.dollars` in the *before* observer. Correct
for both the instant and the queued path.

### `hand.play.hands_left_before` is the value *after* the decrement
`mod/BalatroDB/src/hooks.lua:415`

`ease_hands_played(-1)` (`state_events.lua:491`) is queued before
`evaluate_play` is reached, so the counter has already moved. Confirmed: on a
4-hand deck every round's first play reports `3` and the last reports `0`.

`discards_left_before` on `hand.discard` genuinely *is* before
(`state_events.lua:452` runs after the hooked entry point) — so two fields
with the same suffix have opposite meanings, which is worse than either being
wrong alone. Either rename to `hands_left_after` or add 1 in the hook.

### ~~Card removals vanish after a resume~~ — FIXED, unverified in play
`mod/BalatroDB/src/hooks.lua`, in the baseline emitter

`bdb_owned` is set in the `add_to_deck` hook, and `Card:load()` never calls
it — it assigns `added_to_deck` directly. So after a resume every pre-existing
card has `bdb_owned == nil` and the `Card:remove` hook drops it: a popped Gros
Michel, a shattered Glass card, an expired perishable, a card eaten by The
Tooth. `shop.sell` still fires, so the log looks internally consistent and is
just missing destructions. Deck size and joker inventory derived from
add/remove drift permanently wrong from the first resume.

Fixed by the second option: the baseline emitter now stamps `bdb_owned` on
everything in `G.playing_cards`, `G.jokers` and `G.consumeables` before
emitting, on both a fresh run and a resume. Gating on `added_to_deck`
instead would have reintroduced the debuff bug the flag exists to avoid —
the game clears `added_to_deck` while a joker is debuffed.

**Observed, which is how this surfaced:** two runs carry
`deck_identity_fail`, both with two segments. `1790229463` (Ghost Deck)
resumed at 49 cards, logged 11 adds and 4 removes, and ended at 28 — so 32
cards were destroyed and 4 were recorded. `1790055560` (Yellow) is the same
shape, missing 2.

The ingester now also re-anchors the deck baseline at a `run.rebaseline`
and restarts the card counters, so the identity asks whether the events
since the last known-good state explain the final size. Anchored on the
first baseline it asked about the whole run, so one gap before a resume
condemned everything after it.

Those two runs still fail, correctly: the events were never written and
cannot be recovered.

**Not verified in play.** Needs: Continue a saved run, then destroy a card
that existed before the resume — shatter a Glass card, or Death/Hanged Man
a base card — and check the log gains a `card.remove`.

### `*_num` is NULL for every genuinely beyond-double value
`ingest/ingest.py`, `as_num`

`float("1.2345e+400")` is `inf`, `isinf` → `None`. So a real Talisman score is
dropped from every `SUM`/`AVG`, biased low in a way that grows with how good
the run was. `ord` survives, so the run still *ranks* correctly and only the
arithmetic is wrong — which makes it harder to notice.

**`value_beyond_double` is already in the defect catalogue and is never
raised.** Raising it is the minimum fix; storing a scaled or decimal-string
column is the real one.

### Duplicate `round.end` when a save is resumed at the cash-out screen
`game.lua:3503-3527` re-runs `evaluate_round` when `G.STATE_COMPLETE` is false,
and `save_run()` is called at `:3510` — inside `update_round_eval`, before
`evaluate_round`. So ROUND_EVAL is a normal save point and the natural place to
stop for the night. Resuming there emits a second `round.end` with the same
items and total. Any `SUM(total)` is inflated. The player is paid once.

---

## 2. Verify in-game before trusting

~~The consumable fix is unverified live.~~ **Verified in play**, all four
paths, in `1790147343-3SK973V5-e297.jsonl`:

    n=20  pack.open   set=Booster  p_buffoon_normal_1
    n=23  pack.pick   set=Joker    j_ancient

plus `consumable.use` and `voucher.redeem` across four earlier runs. The
first gate (`G.CONTROLLER.locks.use`) had restored three of the four and
left `pack.open` at zero, because a Booster clears that lock synchronously
at `button_callbacks.lua:2255` while every other path clears it from a
queued event. It now gates on the card leaving its area, which holds for
all four. `pack.pick` had never appeared in any log before this.

**The save/resume latch fix is unverified live.** `state.begin` was wiping
`won`/`endless` on the resume path and persisting the blanks. Covered by
`tests/test_state.py`, but one win → quit → Continue → play a blind would
confirm it against the real save.

---

### Live testing is confounded by buffering
`mod/BalatroDB/src/log.lua:30`

Flushes happen at 16 KB, 128 events, or after `hand.play` / `round.end` /
`snapshot` / quit. Snapshots are skipped during booster states, so a shop
visit with a pack open can sit entirely in the buffer with nothing on disk
for minutes. That is correct for play — flushes are deliberately rare — but
it makes "did that event fire?" unanswerable without playing on until
something forces a write.

Worth a debug flush: a key, a console command, or a flush on `blind.select`.
Cheap, and it makes every future live test faster.

## 3. Data that exists and is thrown away

All present in the logs today; each needs only an ingest branch.

- ~~**`shop.buy` / `shop.sell` / `shop.reroll`**~~ — **fixed.** They land in
  a `shop` table now, filed under the round just played, since shopping
  happens after a round's cash-out when `open_round` has been cleared. The
  whole corpus re-derived: 603 buys ($2,206), 1,011 rerolls ($7,179), 488
  sells ($1,024). Buying a *booster* does not emit `shop.buy` — it comes
  through as `pack.open`, which `consumable_uses` already holds (480).
- **`hand.discard.forced`** (The Hook, `hooks.lua:466`) — without it, forced
  discards pollute every discard statistic.
- **`consumable_uses` conflates three events** — `consumable.use`, `pack.open`
  and `voucher.redeem` share a table with no discriminator. Fortune Teller's
  count is safe only by coincidence of the mod's dispatch.
- **`run.win` detail** — `win_ante`, `round`, `score` are logged; only the
  boolean survives.
- **`blind.select.entered_endless`** — the exact latch moment, currently
  reconstructed by scanning all events.

## 4. Not captured at all — decide before the freeze

Unreconstructable later, so if you want them they have to go in now.

1. **`G.GAME.pseudorandom` per round.** 49 floats. Unlocks all run
   verification: each key's state is the nth iterate of a fixed recurrence
   from `pseudohash(key..seed)`, so the value proves reachability *and*
   reveals exactly how many draws happened.
2. **Hash-chain the log lines.** `love.data.hash('sha256', …)` is available.
   Defeats "open the file and change a number".
3. **Shop and pack offerings.** You record what was bought, never what was
   offered. Appearance rate vs take rate is the highest-value analysis the
   schema cannot support.
4. **Deck order after each shuffle.**

## 5. Robustness and hygiene

- **Stale rows are never cleaned** (`ingest/ingest.py`): a deleted log leaves
  its run; a path whose `run_id` changes orphans the old one; a log truncated
  to empty returns before the savepoint and keeps old rows.
- **A mid-derive failure preserves the previous derivation with no marker**,
  and the dashboard reprints the exception every 3s. A run failing for an hour
  looks identical to one that is simply unchanged.
- **Duplicate log lines corrupt counters.** `round_seq` and `balance` are
  `+=`; `INSERT OR REPLACE` self-heals every other table but not those. The
  mod can genuinely produce duplicates (`log.lua:115` re-queues the whole
  chunk after a failed append). `count_mismatch` now detects it; nothing
  refuses to use the poisoned columns.
- **Two primary keys are unenforceable**: `joker_derived` (NULL `subject`) and
  `joker_state` (NULL `card_id`). NULL never conflicts in SQLite.
- **Missing indexes**: `joker_state(run_id, round_seq)` — the per-round view
  scans every sample in the run — and `runs(started_ts)`.
- **`log.close()` leaves `armed` set**, so a later `commit()` would reopen a
  finished run's file and append past its `run.end`. And `log.open` clears the
  buffer unconditionally, dropping events if a retry fails twice.
- **`tests/test_util.py` checks `ord()` against a Python reimplementation in
  the test file**, not `ingest.ord_num`. The exact historical bug it describes
  could return and the suite would stay green.

## 6. Dashboard

- **Stake sorts alphabetically** (gold → orange → white). `runs.stake` holds
  the real ordinal and `api_meta` already uses it; the run payload does not
  include it.
- **Result sorts by the raw field** (`died`/`loss` apart, the three WON runs
  scattered) rather than by what is rendered.
- **Not keyboard reachable**: sortable headers are `<th>` with `onclick` and
  no `tabindex`; run rows are `<tr onclick>`. `aria-sort` is set correctly, so
  a screen reader is told the state of a control it cannot operate.
- **Contrast**: `--ink-mute` on `--surface` is ~3.4:1, below 4.5:1, and is used
  for every `<th>` and several columns.
- **The record panel is hover-only** and "+N more" is still a native tooltip.

---

## 7. Consistency: the same quantity derived twice

From a review aimed specifically at duplicated work. The ones that produced
wrong output are fixed; these are what is left.

### `held` is a page-wide filter that one panel honours
`ingest/dashboard.py:128` (`where`), `:369` (`attach_records`)

The "Joker records: Held / Not held" toggle reaches `api_derived` and
nothing else. `run_records.held` carries exactly the same distinction, so
the record badges on the Runs table — 183 of them — ignore the toggle while
the Jokers panel correctly moves between 14 and 16 rows. The run dialog
ignoring it is deliberate and documented; the Runs column is not.

### `sort=` is dead server-side, and crashes when combined with a metric
`ingest/dashboard.py:281-320`

Five sort modes exist; `state` has no `sort` key, so the page never sends
one and the Runs table sorts client-side over whatever 300 rows came back.
Harmless at 24 runs, wrong at 400. And `?sort=score&metric=hand_score:Pair&
endless=0` raises `ProgrammingError: Incorrect number of bindings` — `tail`
is appended and then the metric branch overwrites `sort`, orphaning it.
Only reachable by hand-crafted URL.

### "Pick the maximum" has two tiebreak rules
Four queries break an `ord` tie on `CAST(txt AS REAL)`
(`dashboard.py:199`, `:450`, `:533`, `ingest.py:942`); five comparable ones
do not (`dashboard.py:240`, `:246`, `:335`, `:613`, `ingest.py:729`/`:786`).
So the summary tile and the Runs row can print different text for the same
maximum. **0 collisions in the current corpus** — it starts the first time
two hands in one run land on the same ordering key.

### Snapshots carry the game's own per-hand play counts, and ingest ignores them
`hooks.lua:862-876` logs `G.GAME.hands` as `{played, level}` per hand.
Nothing reads `played`. Supernova's counter is instead reconstructed as
`COUNT(*) OVER (PARTITION BY run_id, hand)` (`ingest.py:826`), which
duplicate log lines inflate. `derive()` already cross-checks `hands_played`
and `skips` against `run.end` to raise `count_mismatch`; the same check per
hand type is sitting unused in every snapshot.

### `/api/antes` is dead
In `ROUTES` with `.bars`/`.bar` CSS to render it, no caller. It is also the
one endpoint that takes no `endless_col`, so wiring it up as-is would add a
panel that ignores the phase toggle.

### Naming drift
- ~~**`COUNTER_JOKERS` is two different constants**~~ — **fixed.** There is
  one, in `ingest.py`, now `joker_key → (metric, field, convert)`; the
  dashboard keeps only `COUNTER_LABEL` for display names, keyed the same
  way. Keying by joker was forced by Bootstraps, which reads the same
  `dollars` counter as Bull.
- **`joker_derived.held` is a dead column** carrying a six-line comment
  explaining the Fortune Teller problem it was meant to solve. Never
  written, never read; the feature moved to `joker_counter_peaks`. A reader
  will trust the comment.
- **Jokers are stored twice** — `CARD_ARRAYS` includes `"jokers"`, so every
  snapshot writes them into `cards` (10,568 rows) *and* `joker_state`
  (6,473). `db-schema.md:565` still documents the `cards` path that
  `joker_state` exists to replace.
- **`kind` means two things** (`run_records.kind` vs a round step's kind),
  and **`metric`** means three (the counter name, the ability field's
  sibling, and the run-list sort key `kind:subject`).
- **`db-schema.md` has drifted**: its win-rate query uses the old
  `terminal = 1 AND seeded = 0` population, and "Max round score" is
  documented but implemented nowhere.

### The "Largest deck" tile and the "Deck size" column are different questions
`MAX(rounds.deck_size)` = 67 against `MAX(runs.deck_size)` = 68. Both are
sanctioned by the schema doc; they just wear similar names.

---

## Already fixed

From the consistency review: the "furthest ante" tile deriving
`MAX(rounds.ante)` when every other surface uses `runs.furthest_ante` (they
disagreed on 8 of 24 runs, and the tile read 8 above a column reaching 9);
`rounds_won` computed in SQL and again in page JS with different filters (24
in the table, 39 in the dialog); the counter→contribution formula written in
three places, now converted once at derive time and stored; `ord_of` as a
third implementation of the ordering key; the decaying-joker exclusion list
in four places, one of which (`--report`) had already drifted; and the
dashboard's 495-line `<style>` block, now `web/balatro.css`.

Kept so a future reader does not re-report them: the `use_card` gate; the
`state.begin` latch wipe; `G.GAME.won` reporting a death on the win-ante boss
as a win; money attribution by event-type allow-list; `won` dropped when a run
has no `run.end`; `derive_counters` writing `round_seq` into an event-sequence
column; balance not re-anchored on `run.rebaseline`; the dashboard bypassing
the schema-drift guard; the summary's win rate and count using different
populations; run rows ignoring the phase filter; score truncation; five
unescaped interpolations reaching `innerHTML`; and the `sprites.json` race
that left sorting and names silently inert.
