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

### Card removals vanish after a resume
`mod/BalatroDB/src/hooks.lua:781`, `:807`

`bdb_owned` is set in the `add_to_deck` hook, and `Card:load()` never calls
it — it assigns `added_to_deck` directly. So after a resume every pre-existing
card has `bdb_owned == nil` and the `Card:remove` hook drops it: a popped Gros
Michel, a shattered Glass card, an expired perishable, a card eaten by The
Tooth. `shop.sell` still fires, so the log looks internally consistent and is
just missing destructions. Deck size and joker inventory derived from
add/remove drift permanently wrong from the first resume.

Fix: gate on `card.bdb_owned or card.added_to_deck`, or stamp `bdb_owned` on
everything in `G.playing_cards` / `G.jokers` / `G.consumeables` while building
`run.rebaseline`.

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

**The consumable fix is unverified live.** `use_card` was gated on a truthy
return that never comes, so 0.4.0 logged **zero** `consumable.use`,
`pack.open`, `pack.pick` and `voucher.redeem` where 0.3.0 logged 76/31/2. It
now gates on `G.CONTROLLER.locks.use`. One round that uses a tarot, opens a
pack and takes a joker out of a Buffoon pack would confirm all four paths.

**`pack.pick` has never appeared in any log**, including builds where
`consumable.use` was firing. Worth the same test.

**The save/resume latch fix is unverified live.** `state.begin` was wiping
`won`/`endless` on the resume path and persisting the blanks. Covered by
`tests/test_state.py`, but one win → quit → Continue → play a blind would
confirm it against the real save.

---

## 3. Data that exists and is thrown away

All present in the logs today; each needs only an ingest branch.

- **`shop.buy` / `shop.sell` / `shop.reroll`** — `hooks.lua:643/660/669` carry
  cost and value. `money.cause='shop.buy'` rows exist with no shop table to
  join to. "What do I spend on", "how many rerolls per shop", "what do I sell"
  are all unanswerable. **Biggest single gap.**
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

## Already fixed

Kept so a future reader does not re-report them: the `use_card` gate; the
`state.begin` latch wipe; `G.GAME.won` reporting a death on the win-ante boss
as a win; money attribution by event-type allow-list; `won` dropped when a run
has no `run.end`; `derive_counters` writing `round_seq` into an event-sequence
column; balance not re-anchored on `run.rebaseline`; the dashboard bypassing
the schema-drift guard; the summary's win rate and count using different
populations; run rows ignoring the phase filter; score truncation; five
unescaped interpolations reaching `innerHTML`; and the `sprites.json` race
that left sorting and names silently inert.
