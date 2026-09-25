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

### ~~`money.change.before` is the post-change balance for five sources~~ -- FIXED in 0.4.3
`mod/BalatroDB/src/hooks.lua`

Now a `hook_around` reading `G.GAME.dollars` in the BEFORE observer, which
is correct on both paths: the queued one has not run yet either way, and
the instant one has not reached its own assignment. Logs written before
0.4.3 still carry the wrong `before` at these five sites, and cannot be
repaired -- the true balance was never written down.

The hook was after-only, on the assumption that no gameplay call site passes
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

### ~~`hand.play.hands_left_before` is the value *after* the decrement~~ -- FIXED in 0.4.3
`mod/BalatroDB/src/hooks.lua`

Renamed, not adjusted: the field is `hands_left_after` in the event, the
column and the docs, because that is what it holds. The ingester reads
either key, so pre-0.4.3 logs -- which carry the same quantity under the old
name -- land in the same column. Adding 1 instead would have been a guess
dressed as a fix.

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

### `*_num` is NULL for every genuinely beyond-double value -- now DETECTED
`ingest/ingest.py`, `as_num`

`value_beyond_double` is raised now. The check asks the stored rows -- "any
row where `ord` is set and `num` is NULL" -- over every ord/num/txt triple
found by walking `PRAGMA table_info`, so it covers all eight of them today
and any added later without being updated. It does not fire on the current
corpus: nothing has exceeded a double yet. The real fix, a scaled or
decimal-string column, is still open.

`float("1.2345e+400")` is `inf`, `isinf` → `None`. So a real Talisman score is
dropped from every `SUM`/`AVG`, biased low in a way that grows with how good
the run was. `ord` survives, so the run still *ranks* correctly and only the
arithmetic is wrong — which makes it harder to notice.

**`value_beyond_double` is already in the defect catalogue and is never
raised.** Raising it is the minimum fix; storing a scaled or decimal-string
column is the real one.

### Duplicate `round.end` on a cash-out resume — real, and harmless today
`game.lua:3503-3527` re-runs `evaluate_round` when `G.STATE_COMPLETE` is
false, and `save_run()` is called at `:3510` — inside `update_round_eval`,
*before* `evaluate_round`. So ROUND_EVAL is a normal save point and the
natural place to stop for the night.

**Measured, because the original claim here was wrong.** 8 of 32 logs have
more than one segment; 5 of those open the resumed segment with a
`round.end` inside the first five events. Only **one** of the five is
actually a duplicate:

| | what happened | count |
|---|---|---|
| save taken *before* `evaluate_round` ran | the round.end was never emitted pre-quit, and the resume emits it for the first time | 4 |
| save taken *after* it ran, `STATE_COMPLETE` still false | emitted twice | 1 |

They are told apart by content: the true duplicate repeats the previous
segment's last `round.end` exactly (same blind, score, total and item
count); the other four are for a *different* blind. So the resume is
usually **rescuing** the event, not doubling it — which is why "drop any
`round.end` right after a `run.resume`" would be the wrong fix, losing four
real cash-outs to suppress one duplicate.

**`SUM(total)` is NOT inflated.** The ingest branch is idempotent:
`UPDATE rounds ... WHERE run_id = ? AND round_seq = ?` and
`INSERT OR REPLACE INTO cashout_items` keyed `(run_id, round_seq, i)`. The
resumed `round.end` arrives before any new `round.start`, so `round_seq` has
not advanced and it rewrites the same row with the same values. Verified on
`1789882854-34SJKDTO-0f3e`: 7 `round.end` events, 6 rounds, 6 cash-outs,
`SUM(cashout_total)` = 53.

**The money is paid once, and in the resumed segment.** In seg 0 the payout
for The Window was announced and never paid — the player quit, so no
`money.change` followed. Seg 1 announced it again and paid it: `+8` appears
exactly once in the ledger, at `seg1/n4`, balance 5 → 13.

What is left is **latent, not active**:

- nothing *enforces* that the resumed `round.end` precedes the next
  `round.start`. If one ever arrived after it, `round_seq` would have moved
  and a stale payout would overwrite the wrong round.
- `cashout_items` is keyed by position, so if two emissions of the same
  cash-out ever differed in item count, the extra rows would linger.

The cheap guard, if it is worth one: in the ingester, ignore a `round.end`
whose `(blind, total, score)` matches the round already cashed at that
`round_seq`. That blocks the true duplicate, keeps all four rescues — their
rounds have no `cashout_total` yet — and closes the stale-overwrite case,
with no mod change and retroactively over the whole corpus.

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
- **Two primary keys are unenforceable**: `joker_derived` (NULL `subject`)
  and `joker_state` (NULL `card_id`). NULL never conflicts in SQLite. Both
  now say so in the schema rather than implying a constraint that is not
  there; both tables are truncated and rebuilt, so nothing depends on the
  conflict. (`run_records` had the same shape and it *did* cost rows — see
  the counter-records entry above.)
- ~~**Missing indexes**~~ — added: `joker_state(run_id, round_seq)` and
  `runs(started_ts DESC)`.
- ~~**`log.close()` leaves `armed` set**~~ — fixed; it clears both, so a
  later `commit()` cannot reopen a finished run's file and append past its
  `run.end`. Still open: `log.open` clears the buffer unconditionally,
  dropping events if a retry fails twice.
- ~~**`tests/test_util.py` checks `ord()` against a reimplementation**~~ --
  it imports `ingest.ord_num` now, so the two cannot drift apart into
  agreement.

## 6. Dashboard

- ~~**Stake sorts alphabetically**~~ -- fixed. `/api/meta` returns the game's
  ordinal per stake and `balatro.js` holds one `stakeOrd()` that all three
  stake columns sort on, including the two whose rows carry only the key.
  (True order: white 1, blue 5, orange 7, gold 8.)
- ~~**Result sorts by the raw field**~~ -- fixed. `resultRank()` ranks the
  chips as rendered, so `won` -- a separate flag from `result` -- no longer
  scatters the WON runs across completed, died and suspended.
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

### ~~`held` is a page-wide filter that one panel honours~~ — FIXED
`ingest/dashboard.py`, `attach_records`

The toggle reaches the record badges now, scoped to the rows that carry the
distinction: `rr.held IS NULL OR rr.held = ?`. A bare equality would have
dropped all 155 hand and joker-scale records the moment the toggle was
touched, since NULL never equals anything. Held 34 -> 7 / 27 across the two
settings, with the other 155 constant.

Chasing this turned up the reason the filter had nothing to filter — see
below.

### ~~Counter jokers set no records at all~~ — FIXED
`ingest/ingest.py`, `derive_records`; `ingest/schema.sql`, `run_records`

Two bugs stacked, both silent, both producing "no rows" — which reads
exactly like "no run happened to set one".

**The lookup used the wrong key.** Rekeying `joker_counter_peaks` from the
counter to the joker — forced by Bootstraps, which reads the same `dollars`
as Bull — left `derive_records` calling `COUNTER_JOKERS.get(metric)`, which
no longer keys it. Every lookup missed and the block `continue`d. So Bull,
Bootstraps, Supernova, Stone Joker, Steel Joker, Fortune Teller and
Throwback were absent from records entirely: 0 of 182 rows, `held` NULL in
all of them. This is the second time that block has silently produced
nothing; the first was the bug it was written to fix.

**`held` was not in the primary key.** It is documented as "which of its two
records this is", and a counter joker genuinely has two for one
`(run_id, kind, subject, endless)`. They collided, and `INSERT OR REPLACE`
kept whichever pass ran last — the not-held one — so every counter joker
also lost its held record.

41 records recovered (224 total, was 182). The block now reads the stored
`contributed_value` / `ambient_value` rather than reapplying the conversion
formula, which is what the table stores them for. An inert value is also no
longer a record: `if value` catches a zero chips or mult but X-mult is inert
at 1, so Steel Joker with no steel cards was filing "X1" as an achievement.

Pinned by `tests/test_records.py`, which is what caught the second bug.

### `sort=` is dead server-side — the CRASH is fixed
`ingest/dashboard.py`, `api_runs`

`?sort=score&metric=hand_score:Pair&endless=0` raised
`ProgrammingError: Incorrect number of bindings`: `tail` held the ORDER BY's
placeholder and the metric branch then overwrote `sort`, orphaning it. The
metric branch clears `tail` now, and both URLs return 200.

Still open, and only a scale problem: five sort modes exist, `state` has no
`sort` key, so the page never sends one and the Runs table sorts client-side
over whatever 300 rows came back. Harmless at 31 runs, wrong at 400.

### ~~"Pick the maximum" has two tiebreak rules~~ — FIXED
One rule, in `dashboard.top(ord_col, txt_col)`, applied at the seven sites
that pick a single row to display and previously ordered on `ord` alone.
`ord` is an accelerator and distinct values collide on it, so without the
text tiebreak the summary tile and a Runs row could print different text for
the same maximum. 0 collisions in the corpus today — it would have started
the first time two hands in one run landed on the same ordering key.

### Snapshots carry the game's own per-hand play counts, and ingest ignores them
`hooks.lua:862-876` logs `G.GAME.hands` as `{played, level}` per hand.
Nothing reads `played`. Supernova's counter is instead reconstructed as
`COUNT(*) OVER (PARTITION BY run_id, hand)` (`ingest.py:826`), which
duplicate log lines inflate. `derive()` already cross-checks `hands_played`
and `skips` against `run.end` to raise `count_mismatch`; the same check per
hand type is sitting unused in every snapshot.

### ~~`/api/antes` is dead~~ -- REMOVED
The endpoint, its route and the `.bars`/`.bar` CSS written to render it. The
stats page answers "how far did runs get" already, and this was the one
endpoint that took no `endless_col`, so wiring it up as-is would have added
a panel that ignores the phase toggle.

### Naming drift
- ~~**`COUNTER_JOKERS` is two different constants**~~ — **fixed.** There is
  one, in `ingest.py`, now `joker_key → (metric, field, convert)`; the
  dashboard keeps only `COUNTER_LABEL` for display names, keyed the same
  way. Keying by joker was forced by Bootstraps, which reads the same
  `dollars` counter as Bull.
- ~~**`joker_derived.held` is a dead column**~~ -- **removed**, along with
  the six-line comment describing the Fortune Teller problem it was meant to
  solve. The feature lives in `joker_counter_peaks`.
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
