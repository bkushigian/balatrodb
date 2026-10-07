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
  main.lua            loads modules in dependency order, inside a pcall
  src/util.lua        serialization, number coercion, hook helpers
  src/log.lua         buffered JSONL writer
  src/env.lua         active-mod and version capture
  src/state.lua       run identity, endless latch, envelope context
  src/hooks.lua       every observation point
ingest/             the database and dashboard (Python, stdlib only)
  schema.sql          authoritative DDL; sync_schema.py copies it into the docs
  ingest.py           folds logs into SQLite, one run re-derived at a time
  dashboard.py        local web server + read-only JSON API
  web/index.html      the dashboard
docs/               schema, designs and API references
tools/              shop prediction: the game's RNG, reproduced in Python
tests/              runs the pure Lua logic under a host interpreter
```

## Install (development)

Requires Lovely and Steamodded. Then, once:

```
python ingest/install.py
```

That links this repo's mod into Balatro's `Mods` folder, so edits take effect
on next launch, and writes the launcher behind the in-game dashboard button.

Everything lives under the game's save folder, which LÖVE picks per OS
(`ingest/paths.py` is the one place that knows):

| OS      | Save folder                              |
|---------|------------------------------------------|
| Windows | `%APPDATA%\Balatro`                      |
| macOS   | `~/Library/Application Support/Balatro`  |

Mods go in `<save folder>/Mods`. BalatroDB keeps everything it owns in
`<save folder>/BalatroDB`, outside this repo:

```
BalatroDB/
  runs/<run_id>.jsonl    the event logs -- the only real data
  balatro.db             SQLite, derived from the logs; delete it to rebuild
  launch-dashboard.sh    written by install.py (.bat on Windows)
```

On Windows the link is a directory junction, because a symlink needs
administrator rights (or Developer Mode) there; Steamodded's scan accepts
either. A junction stores an absolute path, so moving this repo breaks it;
delete it and rerun the installer if you do. By hand:

```powershell
cmd /c mklink /J "$env:APPDATA\Balatro\Mods\BalatroDB" "$PWD\mod\BalatroDB"
```

On macOS it is a plain symlink. Note that Steam's Play button does not load
Lovely on macOS -- start the game with `run_lovely_macos.sh` from the game
folder instead, as Lovely's own instructions say.

## Dashboard

```
python ingest/dashboard.py       # http://localhost:8611
```

Leave it running and it keeps itself current: a background thread watches the
log directory, folds in whatever changed, and the page picks it up within a few
seconds. Finish a run in Balatro and it appears on its own — no refresh, no
separate ingest step. A run still in progress shows as such and updates as you
play, because the mod appends to its log throughout.

`python ingest/ingest.py` does the same fold as a one-off, for scripting or a
first build.

Started from the in-game button the server runs in the background, with no
terminal to Ctrl-C. Stop or restart it from the **Server** menu at the right
of the page header, or from a shell:

```
python ingest/dashboard.py --stop       # stop the one on port 8611
python ingest/dashboard.py --restart    # stop it, then start fresh
```

Restart after editing the server's Python; the page also offers it when it
notices the server is running older code than what is on disk.

A local, Balatro-themed web dashboard over the database: record tiles, per-joker
maxima, best hand by type, how far runs get, and a filterable run list that
drills into a single run. Filters are deck, stake and endless phase, and they
apply to every panel at once.

The palette is the game's own, lifted from `G.C` in `globals.lua`.

For joker, deck and stake sprites, extract the game's atlases once:

```
python ingest/extract_assets.py
```

The game is a LÖVE archive (`Balatro.exe` on Windows, `Balatro.love` inside
the macOS app), so the textures and the lua that positions them can be read
straight out of it. The extracted art is gitignored -- it is
the game's own, not ours to redistribute -- and the dashboard falls back to
plain text if it is absent.

## Shop prediction

Balatro's randomness is a set of named streams (`cdt17`, `Joker2sho17`, ...)
derived from the seed, and the save file records where each one has got to.
`tools/` reproduces them outside the game -- LuaJIT's `math.random` and the
game's stream functions, ported bit for bit -- and simulates the shop on top:

```
python tools/shopsim.py predict --find Mime      # the run in your save
python tools/shopsim.py streams --explain        # every stream in the save
python tools/shopsim.py validate                 # replay every logged shop
python tools/shopsim.py calibrate                # re-learn the joker order
```

`predict` shows the coming rerolls, where a card appears this ante and in the
next few, and where it falls in this ante's Buffoon packs. Mid-shop the save
lags behind by however many rerolls you have made, so it catches up from the
run log first. A prediction assumes your jokers stay as they are: a joker you
hold cannot be offered, so buying or selling one changes the picks after it.

The one input that cannot be derived is the order of each joker rarity list,
which the game builds from a hash table and never sorts. `calibrate` learns it
from the logs and writes `tools/joker_pools.json`; rerun it if a mod adds or
removes jokers. `validate` is the evidence: it replays every logged shop from
its seed, through resumes, vouchers, tags and Gros Michel's extinction.

## Tests

```
pip install lupa
python tests/test_util.py    # serialization, numbers, hook wrapper
python tests/test_log.py     # what reaches disk, and what is deliberately dropped
python tests/test_web.py     # the dashboard's inline JS parses (needs node)
python tests/test_balarng.py # the RNG port, against values LuaJIT printed
python tests/test_phase_semantics.py  # standard vs endless: pinned, and the model's invariants
```

Checks over the parts that are pure logic and easy to get subtly wrong:
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

- `shop.enter` (shop contents on entry)
- Replay-only decisions: joker/hand reordering, `reroll_boss`, `cash_out`,
  `skip_booster`, `toggle_shop`
- Mod *configuration* capture (`env.mods` records id/name/version only)
- Log rotation or compaction — a long endless run can reach tens of MB, and
  nothing currently deletes anything
