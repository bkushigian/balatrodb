# BalatroDB

Records every Balatro run as an append-only event log, for deep statistics and
eventually run replay.

## Design in one paragraph

The mod computes no statistics. It writes an ordered stream of events — every
hand, discard, purchase, joker scale, cash-out — to one JSONL file per run. An
external ingester folds those files into SQLite, and every statistic is a query
over that database. The point of the split is that the stat list is not final:
anything computed in Lua is frozen at capture time, while anything derived from
the log can be recomputed across every run ever recorded, including runs played
before the statistic was invented.

See [`docs/event-schema.md`](docs/event-schema.md) for the wire format.

## Layout

```
mod/BalatroDB/      the Steamodded mod (Lua)
ingest/             SQLite schema, ingester and web dashboard
  schema.sql          authoritative DDL; sync_schema.py copies it into the docs
  ingest.py           folds logs into the database
  dashboard.py        local web dashboard server
  web/index.html      the dashboard itself
  main.lua          loads modules in dependency order, inside a pcall
  src/util.lua      serialization, number coercion, hook helpers
  src/log.lua       buffered JSONL writer
  src/env.lua       active-mod and version capture
  src/state.lua     run identity, endless latch, envelope context
  src/hooks.lua     every observation point
docs/               schema and design notes
tests/              runs the pure Lua logic under a host interpreter
```

## Install (development)

Link the mod into Balatro's mod folder so edits take effect on next launch.
Creating a symlink on Windows requires administrator rights (or Developer
Mode), so a directory junction is the practical choice — Steamodded's scan
accepts either:

```powershell
cmd /c mklink /J "$env:APPDATA\Balatro\Mods\BalatroDB" "$PWD\mod\BalatroDB"
```

A junction stores an absolute path, so moving this repo breaks it; recreate it
if you do.

Requires Lovely and Steamodded. Logs land in
`%APPDATA%\Balatro\BalatroDB\runs\<run_id>.jsonl`.

## Dashboard

```
python ingest/ingest.py          # fold new runs into the database
python ingest/dashboard.py       # http://localhost:8611
```

A local, Balatro-themed web dashboard over the database: record tiles, per-joker
maxima, best hand by type, how far runs get, and a filterable run list that
drills into a single run. Filters are deck, stake and endless phase, and they
apply to every panel at once.

It reads the SQLite database directly, so it is exactly as current as the last
ingest. The palette is the game's own, lifted from `G.C` in `globals.lua`.

## Tests

```
pip install lupa
python tests/test_util.py
```

52 checks over the parts that are pure logic and easy to get subtly wrong:
number coercion at the 1e14 / inf / nan / Talisman boundaries, card
serialization, and the hook wrapper's contract — return values and arity
preserved, `false` returns reaching the caller intact, observer errors
swallowed, missing targets warning instead of raising.

The host interpreter is Lua 5.5 while Balatro runs LuaJIT, so this checks logic
and syntax, not runtime behaviour in the game. Anything touching `G` needs an
actual run.

## Guiding constraints

**Never break a run.** Every observer is wrapped in `pcall`, and so is module
loading — Steamodded executes mod main files unprotected, so an error raised at
load time doesn't disable the mod, it stops Balatro booting. A hook whose
target has gone missing warns and disables itself. Losing one event type after
a game update is acceptable; taking the run down with it is not. The mod also
never draws from `math.random`, which Balatro uses for real gameplay decisions.

**Record first, decide later.** Seeded and challenge runs are recorded and
tagged rather than discarded, so leaderboards exclude them with a `WHERE`
clause instead of the data being gone. Decaying jokers are logged even though
their "maximum" is not the interesting number.

**Observe where the mutation lands, not where it was requested.** Balatro
defers nearly all state changes into queued events, so an after-hook on a
`G.FUNCS` entry point reads pre-action state. This is the single biggest source
of subtly wrong data and most of the non-obvious code exists to handle it.

## Status

Rewritten after three independent reviews against the decompiled game source,
then run in Balatro. **First live run captured cleanly**: 22 events over one
blind, every line parseable, and the money chain reconciles end to end —
itemized cash-out 3+2=5 → `money.change` +5 from $4 → `shop.buy` at $4 →
`money.change` −4 from $9 → snapshot showing $5. `blind.select`/`round.start`
correctly report the proto and the real 300-chip blind respectively, and
`chips_before` chains 0 → 296 across hands.

Confirmed live across several runs: accumulator scaling series (Square Joker
`chips` 0→4→…→40, Spare Trousers `mult` 0→2→…→20), decayer series (Popcorn
20→16→12, Turtle Bean `h_size` 5→4→3→2), `shop.sell` pairing with
`joker.remove reason: "sold"`, and resume working repeatedly — one run carried
segments 0, 1 and 2 in a single file with correct baselines.

Bugs the live runs exposed, all now fixed:

- **Run teardown was logged as gameplay.** `Game:delete_run` (`game.lua:1177`)
  empties every card area and resets `G.GAME`, and it runs *before* the stage
  flips to `MAIN_MENU`. Closing the run from the stage watcher meant `run.end`
  recorded ante 1, round 0, $0 and an empty deck — the single most important
  event for run-level stats was useless — while the teardown itself produced 75
  `card.remove` and 4 `joker.remove` events all marked `destroyed`. The run is
  now closed from `delete_run` itself, with `G.in_delete_run` as a second guard
  on removals.
- **Runs could not say why they ended.** `G.SAVED_GAME` is only populated while
  a save is being *loaded*, so it is nil when leaving a run — and the
  replacement test, "does a save file exist", is no better on its own, because
  one still exists at teardown even when the player is abandoning the run to
  start a different one. Rerolling a fresh run therefore recorded as
  `suspended`. `Game:delete_run` is shared by six entry points, so the reason
  is now recorded where the player acts and consumed at teardown, giving
  `died`, `completed`, `new_run`, `restart`, `suspended`, `abandoned`,
  `profile_switch` and `exited`, plus a `terminal` flag that is false only for
  `suspended`. Whether the run was *won* is a separate field, since beating the
  win ante and then dying in endless is still a won run.
- **The starting deck was never recorded.** It is built inside
  `Game:start_run`, before the log opens, so those `card.add` events are never
  written. `run.start` now carries a full baseline like `run.resume`.

- **Run ids came out as `0000`.** The id generator multiplied a ~2^31 state by
  1103515245, overflowing double precision, so the low bits were rounded away
  and every draw returned the same value. The unit test passed because the host
  interpreter has real integers while LuaJIT has only doubles. Replaced with
  MINSTD, whose largest intermediate product is ~3.6e13, and the test now
  asserts that invariant directly instead of sampling output.
- **44% of the log was inert zeros.** Every card carries the full numeric
  ability set whether it uses any of it or not, so a 40-card deck sample
  repeated thirteen default fields forty times. Default-valued fields are now
  elided; an absent field means the default.

Fixed in this pass: three hooks that pointed at functions normal gameplay never
calls; the number encoding (threshold was 10× too high, and text-only values
break SQL `MAX()`); the endless boundary latching a round early; events fired
for actions the game refused; sales double-logging as destructions; a flush
path that dropped buffered events on a transient write failure; `math.random`
consumption perturbing the game's own RNG; missing card identity; and module
load errors preventing the game from booting.

### Verify in game

- Run identity across save → quit → resume: one file, `seg` incrementing,
  `run.resume` carrying a correct baseline.
- The endless latch: `run.win` stamped `el: false`, and the next
  `blind.select` carrying `entered_endless: true`.
- `round.end.total` matching the number the game displays, with money jokers in
  play.
- Glass cards, Gros Michel/Cavendish and sold jokers each producing exactly one
  removal event with the right `reason`.
- Performance during heavy retrigger boards (Hanging Chad + Sock and Buskin +
  Blueprint), where `joker.scale` fires hundreds of times per hand.

### Not yet implemented

- The SQLite ingester and schema
- `shop.enter` (shop contents on entry)
- Replay-only decisions: joker/hand reordering, `reroll_boss`, `cash_out`,
  `skip_booster`, `toggle_shop`
- Mod *configuration* capture (`env.mods` records id/name/version only)
- Log rotation or compaction — a long endless run can reach tens of MB, and
  nothing currently deletes anything
