# BalatroDB Event Schema v1

> **Status: alpha. The wire format is unstable and carries no compatibility
> promise.** See [Compatibility policy](#compatibility-policy).

The mod writes an **append-only JSONL file per run**. Nothing is aggregated in
Lua. Every statistic — high scores, per-joker maxima, per-round records — is
derived downstream by an ingester that folds the event log into SQLite.

The reason is that our stat list is not final. Anything computed in Lua is
frozen at capture time; anything derived from the log can be recomputed over
every run ever recorded, including runs played before the stat existed.

## Files

```
%APPDATA%/Balatro/BalatroDB/runs/<run_id>.jsonl
```

`run_id` is `<utc-unix-seconds>-<seed>-<4 hex>`, unique even when the same seed
is replayed. The hex comes from a private generator, never `math.random` —
Balatro draws on the shared `math.random` stream for real decisions, so
consuming from it would shift gameplay.

### One run, one file, several sessions

Save-and-quit resumes into the **same file**. `Game:start_run` is the same
entry point for a fresh run and a loaded save (`game.lua:2048`,
distinguished by `args.savetext`), so the mod parks `bdb_run_id`, `bdb_seg`,
`bdb_endless` and `bdb_won_pending` on `G.GAME`. `save_run` serializes `G.GAME`
wholesale through `recursive_table_cull` (`misc_functions.lua:1569`), which
copies plain scalars verbatim, so identity survives the round trip.

Each session is a **segment**. `seg` increments per resume and `n` restarts at
0 within it, so `(seg, n)` totally orders a run without the mod having to read
the file back to find where it left off. A resumed session opens with
`run.resume` carrying a full inventory baseline, because `CardArea:load`
(`cardarea.lua:718`) rebuilds the board directly and fires no add events.

## Envelope

```json
{"v":1,"run":"1758240000-ABCD1234-9f3a","seg":0,"n":412,"t":903.44,
 "e":"joker.scale","a":5,"r":13,"el":false,
 "d":{"key":"j_ride_bus","field":"mult","from":14,"to":15}}
```

| Field | Meaning |
|---|---|
| `v`   | schema version |
| `run` | run id, repeated on every line so files can be concatenated |
| `seg` | session index; `(seg, n)` is the ordering key |
| `n`   | sequence number within the segment, from 0, gap-free |
| `t`   | seconds since this segment began, 2dp |
| `e`   | event type |
| `a`   | ante (`G.GAME.round_resets.ante`) |
| `r`   | round (`G.GAME.round`) |
| `el`  | endless flag |
| `d`   | payload |

`a` and `r` are stamped best-effort and are **not** reliable round keys: both
are advanced by queued `ease_*` events, so they lag by one at round
boundaries. Derive round identity from `round.start` → `round.end` pairs
instead.

**Envelope key order is fixed, with `e` first.** The envelope is assembled by
hand rather than encoded from a table, because Lua tables are unordered and the
encoder emits them in hash order — in one real corpus the event type landed up
to 11KB into a line, and the order shifted whenever a key was added. A fixed
order lets a reader classify a line from its first few bytes. Payload key order
inside `d` is still unordered.

### The endless flag

A run is not endless or non-endless; it *becomes* endless partway through. The
flag is stamped per event so a single run feeds both leaderboards honestly.

`el` is **latched by the mod**, not read from `G.GAME.won`. That field is set
at `state_events.lua:112`, inside `end_round`, the instant the win-ante boss
dies — before `win_game()` is even queued. Reading it at emit time would stamp
the winning round's own cash-out as endless and permanently remove the largest
legitimate non-endless payout from every winning run.

The real boundary is the player choosing to carry on, so:

- `run.win` fires when the win ante is beaten, and is itself `el: false`. It
  *arms* the latch.
- The latch flips on the **next `blind.select`**, which carries
  `entered_endless: true`.
- Everything up to and including the winning round counts as non-endless.

The latch persists across a resume, so continuing an endless run in a later
session stays endless.

### Numbers

Any numeric field may arrive in one of two forms:

- a plain JSON number, when it round-trips exactly, or
- `{"s": "<exact text>", "l": <signed log10>}` when it does not.

**The test is the actual round trip, not a magnitude threshold.** The encoder
is `%.14g` (`smods/libs/json/json.lua:110`), so the mod formats the value,
parses it back, and wraps only if it differs. A magnitude rule is wrong in both
directions: `12345678901234.5` is below 1e14 and still loses its fraction,
while `1e20` needs one significant digit and round-trips exactly. Wrapping only
on real precision loss also keeps large round numbers natively summable.

`s` preserves the value; `l` exists because **`MAX()` over TEXT in SQLite is
lexicographic**, so `MAX('9','1000')` is `'9'` and every per-joker maximum
would be quietly wrong. Store both: `value_exact TEXT` and `value_order REAL`.
`l` is signed so negatives order correctly. `inf`/`-inf`/`nan` arrive as `s`
with sentinel or absent `l`.

**`l` is an ordering *accelerator*, not an exact key.** Distinct values can
collide in it — verified: plain `99999999999999` and a wrapped `1e14` both
yield `14.0`, and `1e-17` and `0` both yield `0.0`. A consumer that needs the
true maximum must break ties on the exact text with a numeric comparison, not
a lexicographic one.

## Event types

### Lifecycle

| Event | Notes |
|---|---|
| `run.start` | fresh run; environment block below. **Metadata only** |
| `run.resume` | same fields, for a later segment |
| `run.baseline` / `run.rebaseline` | follows start/resume: `jokers`, `consumables`, `deck_cards`, `dollars`, `chips` |
| `run.win` | win ante beaten; arms the endless latch; `el: false` |
| `run.final` | final `jokers` and `deck`, emitted **before** `run.end` |
| `run.end` | why the run ended, plus `terminal`; see below. **Metadata only** |
| `snapshot` | periodic backstop |

**The bulky card arrays are deliberately separate events.** A baseline is
5–10KB, and keeping it out of `run.start` means a reader can identify a run —
deck, stake, seed, date — from the file's *first line* alone, and learn how it
ended from its *last* line, without parsing anything in between. Measured at
roughly 19× cheaper for building a run list. `run.final` is emitted before
`run.end` for the same reason: `run.end` stays the small last line.

```json
{"ts":1758240000,"seed":"ABCD1234","seeded":true,"challenge":null,
 "deck":"Red Deck","deck_key":"b_red","stake":3,"stake_key":"stake_blue",
 "win_ante":8,"profile":1,"starting_deck_size":52,
 "env":{"game":"1.0.1o","lovely":"0.9.0","smods":"26.829.0",
        "balatrodb":"0.2.0",
        "mods":[{"id":"Handy","version":"2.0.6"}]}}
```

`stake_key` is recorded alongside `stake` because the index is a position in
`G.P_CENTER_POOLS.Stake`, and that ordering shifts when stake-adding mods come
and go — so "stake 8" stops meaning Gold across your own history.

`seeded` runs are recorded and tagged rather than discarded. The vanilla game
refuses to count them (`inc_career_stat`, `misc_functions.lua:1562`); tagging
makes exclusion a `WHERE` clause instead of missing data.

### Did the run win, and how did it end

These are **two independent questions** and `run.end` answers them separately.

**`won`** is the win flag. Winning is beating the win ante, full stop. A run
that beat ante 8, continued into endless and then died is still a won run.
Win rate is `COUNT(won) / COUNT(terminal)` — never a test against `result`.

**`result`** is how the session ended:

| Result | Meaning |
|---|---|
| `died` | ran out of hands on a blind. Says nothing about `won` |
| `completed` | beat the win ante and left without continuing into endless |
| `new_run` | abandoned to start a different run |
| `restart` | abandoned via the restart button (same deck and stake) |
| `suspended` | left to the menu with a resumable save — **not terminal** |
| `abandoned` | left to the menu with no save to return to |
| `profile_switch` | torn down because the player changed profile |
| `exited` | the game was closed from inside the run |
| `unknown` | teardown with no recognized cause |

So `won: true, result: "died"` is a win that ended in an endless death — and
the envelope's `el` on the surrounding events says how deep it got.

`terminal` is `false` only for `suspended`, so completion stats can filter on
it without enumerating reasons. A crash produces no `run.end` at all, which is
distinguishable from every case above.

`run.end` also carries `endless`, repeated from the envelope so an endless
filter is answerable from that one line without scanning the file for
`run.win`.

**Scores on `run.end`.** `final_round_score` is `G.GAME.chips`, the score of
the round the run ended *on* — a run that peaked at 800k and then died on a 40k
round reports 40k, so it is the wrong field to sort a run list by. The run's
actual bests are `best_hand`, `furthest_ante` and `furthest_round`, taken from
`G.GAME.round_scores`, which the game maintains as running maxima for free
(`check_and_set_high_score`, `misc_functions.lua:1146`).

These cannot be told apart at teardown time: `Game:delete_run`
(`game.lua:1177`) is shared by at least six entry points, and a save file still
exists on disk at that moment even when the player is abandoning the run to
start a different one — so testing for the save alone reports a reroll as a
suspension. The reason is therefore recorded where the player acts
(`G.FUNCS.start_run`, `go_to_menu`, `load_profile`) and consumed when the
teardown arrives. `G.FUNCS.start_run` distinguishes `restart` from `new_run`
via `e.config.id == 'restart_button'` (`button_callbacks.lua:3037`).

An earlier vocabulary used `win` and `loss` for these, which conflated the two
questions: it made an endless death look like a loss, and made `win`
unreachable entirely, since winning never reaches `GAME_OVER`.

`died` comes from the `GAME_OVER` transition instead, which happens before any
teardown. Note that winning never reaches that state: `game_over` and
`game_won` are independent (`state_events.lua:110-115`) and the run stays alive
so the player can continue into endless, so `GAME_OVER` always means a death.

Mods can add teardown paths of their own. Steamodded replaces the run-select
screen and its start path calls `G:delete_run()` directly rather than going
through `G.FUNCS.start_run` (`smods/src/utils/run_select.lua:325`), so it is
hooked separately. `unknown` is deliberately kept as a distinct result rather
than being folded into a plausible default: it is the signal that some path
tore a run down without announcing itself, and `run.end.at_state` records the
`G.STATES` name it happened in to help track down which.

### Rounds and blinds

| Event | Payload |
|---|---|
| `blind.select` | the **decision**: `blind_key`, `name`, `entered_endless?` |
| `round.start` | the **consequence**: `blind_key`, `name`, `chips`, `boss`, `reward`, `ante` |
| `blind.skip` | `blind_on_deck`, `ante`, `skips`, `tag` |
| `round.end` | itemized cash-out, below |

`blind.select` and `round.start` are split because `G.FUNCS.select_blind` does
all its work in queued events — an after-hook there reads the *previous*
round's blind, and on the first blind of a run an empty `Blind` with
`chips: 0`. The authoritative record comes from `Blind:set_blind`
(`blind.lua:99`), where the selection actually lands.

```json
{"items":[{"name":"blind1","dollars":8},
          {"name":"hands","dollars":3,"disp":3},
          {"name":"joker1","dollars":4,"key":"j_rocket"},
          {"name":"interest","dollars":5}],
 "total":20,"dollars_before":31,"score":12400,
 "jokers":[…],"deck":[…],"deck_size":54}
```

`total` is taken from the `bottom` row (`state_events.lua:1118`) rather than
summed, so adjustments made through the `modify_final_cashout` context are
included. Collecting rows here also captures entries past the seven-row cap
the UI itself drops.

`round.end` carries a full deck sample — once per round is affordable, and it
is the only way to derive Stone Joker, Hiker and max deck size.

### Play

| Event | Payload |
|---|---|
| `hand.play` | `cards`, `hand`, `level`, `score`, `blind_chips`, `chips_before`, `oneshot`, `hands_left_before`, `discards_left_before`, `jokers` |
| `hand.discard` | `cards`, `discards_left_before`, `forced?` |
| `hand.levelup` | `hand`, `from`, `to`, `amount` |

`hand` is `G.GAME.last_hand_played`, the **internal key** set synchronously at
`state_events.lua:592`. Do not use `current_round.current_hand.handname`: it
holds the localized display string, so it is not comparable across languages.

`score` is `SMODS.last_hand_score`, set inline before `evaluate_play` returns.
The running total after the hand is `chips_before + score` — `G.GAME.chips` is
raised by a queued ease and has not moved yet.

`oneshot` means *this single hand beat the blind*, which is what SMODS
computes. It is **not** "the blind is now cleared": a three-hand clear is
`false` on every hand including the last. Clearing is derivable from the
`round.end` that follows.

Counters are reported with explicit `_before` semantics because the `ease_*`
calls that decrement them are queued, and hands and discards are not even
consistent with each other.

### Money

`money.change` — `delta`, `before`.

Every balance change funnels through `ease_dollars` (`common_events.lua:68`),
which queues its mutation unless `instant` is passed, and no gameplay call site
passes it. So no after-hook anywhere can observe a new balance. Recording
deltas here is exact; shop events deliberately carry no `dollars_after`.

### Jokers and cards

| Event | Payload |
|---|---|
| `joker.scale` | `id`, `key`, `name`, `field`, `from`, `to`, `op` |
| `joker.reset` | same, for `SMODS.reset_card` |
| `joker.add` / `card.add` / `consumable.add` / `voucher.add` | `card`, `area` |
| `joker.remove` / `card.remove` / `consumable.remove` / `voucher.remove` | `card`, `reason` (`sold` \| `destroyed`) |
| `card.modify` | `from`, `to`, `what` (`ability` \| `seal`) |
| `consumable.use` | `card`, `targets`, `set` |
| `pack.open` | `card`, `targets`, `set` |
| `pack.pick` | `card`, `targets`, `set` — taken out of a booster |
| `voucher.redeem` | `card`, `targets`, `set` |

**`joker.scale` carries most of the per-joker stat list.** Steamodded's
`lovely/scaling.toml` rewrites the vanilla scaling jokers to call
`SMODS.scale_card` (`smods/src/utils.lua:3357`), so one wrapper sees them all
plus any modded joker using the same API. A joker's maximum is
`MAX(to) GROUP BY key, field`, sliced by stake/deck/endless.

Ownership is resolved rather than taken from the first argument: Madness passes
`context.blueprint_card or self` while mutating its own ability
(`card.lua:2901`), so a copied Madness would otherwise be logged against the
Blueprint.

`consumable.use`, `pack.open`, `pack.pick` and `voucher.redeem` are all the
same game function, `G.FUNCS.use_card`, split apart afterwards. **Taking a
joker or a playing card out of a booster goes through it too**, which is why
`pack.pick` exists — it is told apart by the card's area, the way the game
itself does it (`button_callbacks.lua:2214`). Without that split, a joker
picked from a pack was recorded as a consumable use.

The event is only emitted when the function actually returns `true`. Its
rejection branch returns *nothing* rather than `false`
(`button_callbacks.lua:2187`), so a refused use — Ankh with no joker room —
previously logged as a successful one.

**`hand.discard` carries `forced: true`** when The Hook discarded for you
(`blind.lua:526` calls the same function with `hook = true`). It is not a
decision and does not spend one of your discards.

**Ownership is tracked by the mod, not by `added_to_deck`.** The game clears
that flag while a joker is debuffed and restores it afterwards
(`card.lua:690-728`), so reading it directly logged a fresh acquisition on
every Crimson Heart tick and lost the removal of any joker sold while
debuffed.

Adds and removes hook `Card:add_to_deck` (`card.lua:748`) and `Card:remove`
(`card.lua:5169`), the universal funnels. Three earlier choices were wrong and
are documented here so they are not reintroduced: `add_joker` takes a centre
**key string**, not a Card, and is only reachable from challenge setup and
debug; `create_playing_card` has two callers; and `start_dissolve` misses
`Card:shatter` (every Glass card, `state_events.lua:820`) and
`SMODS.pinch_and_remove` (Gros Michel / Cavendish, `card.lua:3428`).

Removals are filtered to cards with `added_to_deck` set, because screen-wipe
cards (`button_callbacks.lua:3237`) and the unlock-overlay card
(`UI_definitions.lua:4524`) are real `Card` objects that travel the same path.

**Events are routed by the card's `set`**, because `add_to_deck` is shared by
every card type (`buy_from_shop` runs it for all purchases,
`button_callbacks.lua:2468`). Tarots, Planets and Spectrals become
`consumable.*`, not `card.*`. Lumping them together made deck size
underivable: one real ante-6 run logged 67 Tarot, 20 Planet and 7 Spectral
acquisitions as `card.add`.

**`card.modify` exists because enhancing a card looks exactly like acquiring
one.** `Card:set_ability` (`card.lua:255`) re-applies a card by calling
`remove_from_deck()` — which clears the `added_to_deck` guard
(`card.lua:834`) — and adding it back at `:486`. The same run logged 40 tarot
enhancements as `card.add`. The round trip is now marked so it emits a
modification instead, carrying `from` and `to` snapshots. `Card:set_seal`
(`card.lua:608`) is a separate path and emits `what: "seal"`.

Deck size should reconcile as `run.start.deck_cards` + `card.add` −
`card.remove`, and `card.modify` never changes the count. That identity is
worth asserting in the ingester: it is what caught both of these.

#### Jokers that never scale

Five jokers recompute from game state each scoring pass and never call
`scale_card`: **Supernova**, **Fortune Teller**, **Stone Joker**,
**Throwback**, **Swashbuckler**. **Hiker** writes `perma_bonus` onto deck cards
directly (`card.lua:3460`), and two vanilla paths still mutate ability fields
without the API (Glass Joker via The Hanged Man `card.lua:3122`, Invisible
Joker's counter `:3347`).

All of these are covered by **sampling `jokers` on every `hand.play` and
`round.end`**, with numeric ability state included in each card. Per-hand
sampling makes their maxima exact, not approximate. Snapshots alone would not:
`save_run` is not a timer, and it refuses to run during every booster-pack
state (`misc_functions.lua:1588`).

Decaying jokers (Ice Cream, Popcorn, Turtle Bean, Ramen) fire `scale_card`
normally and are recorded like any other. Their useful statistic is inverted —
rounds survived, or minimum before expiry — but that is a query concern.

### Shop

| Event | Payload |
|---|---|
| `shop.buy` | `card`, `cost`, `and_use?` |
| `shop.sell` | `card`, `value` |
| `shop.reroll` | `cost` |

All three gate on the game's return value: `buy_from_shop` returns `false`
without buying when there is no room (`button_callbacks.lua:2457`), and
`use_card` early-returns when `check_use` rejects (`:2187`). `shop.reroll`
carries no item list — the shop is repopulated in a queued event.

### State

`state.change` — `from`, `to` as `G.STATES` names.

`encode.error` — emitted in place of an event whose payload failed to encode,
so `n` stays gap-free and the ingester can see something was lost.

## Card serialization

```json
{"id":77,"key":"j_ride_bus","name":"Ride the Bus","set":"Joker",
 "edition":"e_foil","stickers":["eternal","pinned"],
 "state":{"mult":14,"extra.odds":4},"sell_cost":3}
```

Only non-default fields appear, so a plain card is `{"id":9,"rank":"7","suit":"Hearts"}`.

- **`id`** is Balatro's own `sort_id` (`card.lua:24`), stable across saves. The
  game uses it for its own one-action replay. Without it two copies of a joker
  are indistinguishable and no log is replayable.
- **`state`** holds numeric ability fields, including nested `extra.*`. This is
  what lets a sample stand in for a missed scaling event, and the only way the
  never-scaling jokers are observable at all.
- **`enhancement`** appears only when `set == "Enhanced"`. `ability.name` is
  the centre's name for *every* card type (`card.lua:345`), so reading it
  unconditionally labels every joker with a bogus enhancement.
- **`stickers`** includes `pinned`, which lives on the card rather than on
  `ability` (`card.lua:1148`).

## Compatibility policy

`v` in the envelope is the **wire contract**. What it promises depends on the
release stage:

| Stage | `v` | Promise |
|---|---|---|
| **alpha** (now) | `1` | None. The format changes freely and `v` is not bumped for it. Alpha logs are test fixtures, not data. |
| **beta** | `2`+ | Real users. Every breaking change bumps `v`, and the ingester reads **every** beta `v` from 2 onward. |
| **1.0** | frozen | `v` bumps only for genuinely breaking changes, each one supported indefinitely. |

**Entering beta bumps `v` to 2**, which draws a hard line under the alpha
churn. That matters because the format has already changed repeatedly under
`v: 1` — the `run.baseline` split, the `final_round_score` rename,
`consumable.*` routing, the fixed envelope key order — so `v: 1` cannot
identify anything, and alpha logs are only interpretable via
`env.balatrodb`.

### What counts as breaking

Breaking, so bumps `v`: removing or renaming a field, changing a field's units
or meaning, changing an event's semantics, splitting or merging event types.

Not breaking, so no bump: **adding** a field or an event type. Readers must
ignore unknown fields and unknown event types rather than failing — that is what
makes additive change free.

### The escape hatch

`env.balatrodb` on every `run.start` and `run.resume` is the finer-grained
record: it identifies the exact build, and so which capture *defects* applied,
which is a different question from which fields exist. A bug that makes a field
wrong without changing its shape is not a `v` bump, but a consumer still needs
to know — for example, builds before the `Card:add_to_deck` routing fix counted
consumables as deck cards, so their deck-size figures are wrong while their
schema is identical.

The database records both, **per segment rather than per run**, because a run
can span a mod update: play, quit, update, resume.

## Replay

Seed plus ordered decisions should reconstruct a run: Balatro's RNG is
deterministic but consumed in action order, and `pseudorandom_element` sorts
candidates by `sort_id` before choosing, so visual ordering does not affect
*which* card is picked.

Decisions captured today: `blind.select`, `blind.skip`, `hand.play`,
`hand.discard`, `shop.buy`, `shop.sell`, `shop.reroll`, `consumable.use`,
`pack.open`, `voucher.redeem`.

**Known gaps** — joker reordering (drag sets `T.x` and `align_cards` re-sorts
every frame; no discrete event exists to hook), hand reordering and the sort
buttons, `reroll_boss` (Director's Cut), `cash_out` (which shuffles the deck),
`skip_booster`, and `toggle_shop` (the `ending_shop` context). None affects
statistics, and the schema absorbs new event types additively — but `id` had to
land now, because logs written without it can never be made replayable.

Replay is only valid within a matching environment, which is why `run.start`
pins game, Lovely, Steamodded, BalatroDB and mod versions.
