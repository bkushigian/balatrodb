# In-game run-history browser — design

A screen inside Balatro where the player browses their own run history:
filter by deck / stake / endless, sort by the numbers that matter, drill into
one run, and see records.

Platform facts are not re-derived here. See
[`steamodded-ui-reference.md`](steamodded-ui-reference.md) for what Steamodded
adds (file access, scroll box, dropdowns, frame-spreading, teardown) and
[`balatro-ui-reference.md`](balatro-ui-reference.md) for the vanilla UI API
(node vocabulary, paging, row chrome, and the fact that base Balatro has no
scrolling at all).

---

## 1. Measurements this design is built on

Everything below was measured against the ten real logs in
`%APPDATA%/Balatro/BalatroDB/runs/` on 2026-09-21.

### Corpus size

| | |
|---|---|
| Runs | 10 |
| Total | **2.67 MB**, 6457 lines |
| Largest run | 0.63 MB, 1603 lines |
| Mean run | 0.27 MB, 646 lines |
| Bytes per line | ~410 B average |
| Longest single line | **11 629 B** (a `round.end`) |

Where the bytes go, across all ten runs:

| Event | Count | Share of bytes | Avg line |
|---|---:|---:|---:|
| `round.end` | 110 | **25.2 %** | 6 063 B |
| `hand.play` | 326 | **14.9 %** | 1 209 B |
| `state.change` | 2379 | 14.0 % | 156 B |
| `snapshot` | 231 | 13.7 % | 1 567 B |
| `money.change` | 994 | 5.2 % | 138 B |
| `card.remove` | 442 | 4.3 % | 255 B |
| everything else | 1975 | 22.7 % | — |

Three quarters of the log is the deck and joker samples carried by
`round.end`, `hand.play`, `snapshot`, `run.start` and `run.end`. That is the
right call for the ingester and it is exactly what makes naive Lua scanning
expensive.

### rxi `json.lua` decode throughput

Benchmarked by running the bundled parser
(`smods-26.829.0/libs/json/json.lua`) over the real corpus under **Lua 5.5 via
lupa** — the same host the repo's unit tests use.

```
TOTAL 2.668 MB, 6457 lines, 536 ms  ->  4.98 MB/s, 83 us/line
```

Per-line, on the two lines a run-list row actually needs:

| Line | Size | Decode |
|---|---:|---:|
| `run.start` as written today | 5 314 B | **1.00 ms** |
| `run.start` with `deck_cards`/`jokers`/`consumables` removed | 554 B | **0.095 ms** |
| `run.end` as written today | 10 239 B | **1.85 ms** |
| `run.end` with `deck`/`jokers` removed | 270 B | **0.055 ms** |

**Caveat, stated plainly:** this is Lua 5.5, not LuaJIT. Balatro runs LuaJIT,
where `string.find`/`string.sub` are the same C functions but the interpreter
loop around them is faster; expect LuaJIT to be somewhere between equal and a
few times quicker. Treat 5 MB/s as a conservative order of magnitude, not a
number to design to three significant figures. Nobody has yet run this parser
inside the game — that is the first thing to measure once there is a screen to
measure it on.

### The numbers that decide the design

Scaling the measured corpus to a plausible library of **300 runs** (~80 MB):

| Operation | Cost | Verdict |
|---|---:|---|
| Full parse of every log | ~16 s CPU | dead on arrival |
| Head + tail line of every log, as the schema stands today | **~0.9 s CPU** | tolerable once, cached |
| Head + tail with lean lifecycle lines (§7) | **~45 ms** | free |
| Full parse of **one** log, on demand | 125 ms worst observed | fine, frame-spread |
| Read one 420 B/run index file | ~25 ms for 300 runs | free |

The whole design follows from those five rows.

---

## 2. The data-access decision

### Verdict

**A three-tier hybrid, where the mod owns the cheap tier and the ingester owns
the expensive one.**

| Tier | What | Source | Freshness |
|---|---|---|---|
| **0 — run list** | one row per run: deck, stake, date, ante, score, result, endless | **mod-written append-only index file**, rebuildable by head/tail scan | always current, including the run you just closed |
| **1 — one run's detail** | rounds, money curve, jokers held, hands played | **full Lua parse of that one JSONL**, on demand, frame-spread | always current |
| **2 — records / per-joker maxima** | "highest Obelisk I've ever had" | **`records.json` emitted by the ingester** next to the DB | stale between ingests, and labelled as such |

No tier blocks on another. Missing the index costs a rescan; missing
`records.json` costs the per-joker layer and nothing else.

### Why the index file is written by the mod, not the ingester

This is the one place I depart from the obvious reading of option (a).

The player's most likely reason to open this screen is **"how did that run I
just finished compare?"** An ingester-written index cannot answer that — the
ingester may not have run since yesterday. An index the mod appends to on
`run.start` and `run.end` is correct the instant the run closes, because
`end_run` already calls `log.close()`, which flushes
(`mod/BalatroDB/src/log.lua:88-91`). The last line of a finished run's log is
on disk before the player reaches the menu. Verified by reading the writer, not
by watching it happen.

It also removes the ingester from the critical path entirely for the two views
a player uses ninety percent of the time. A fresh install with no Python
toolchain still gets a working run browser.

**This does not violate "the mod computes no statistics."** The index holds no
statistic. Every field in it is a verbatim copy of a field already in
`run.start` or `run.end`, denormalized for lookup. It is *derived state and
must be treated as disposable*: if it is deleted, corrupt, or written by an
older schema version, the viewer rebuilds it by scanning, and the rebuild is
authoritative. Nothing is ever only in the index.

### Why not the alternatives

**(b) Pure Lua scan of the raw JSONL, no index.** Quantified above: ~16 s of
CPU to build a records view over 300 runs, and ~0.9 s even for the reduced
head/tail scan. Spread across frames at a 4 ms budget that is 67 seconds and
4 seconds of wall clock respectively. The 4-second version is survivable as a
*rebuild* path with a progress bar. As the thing that happens every time you
open the screen it is not. Rejected as the primary mechanism, **kept as the
fallback** — which is the important part, because it means the viewer has no
hard dependency on anything outside the logs.

**(c) LuaJIT FFI against `sqlite3.dll`.** FFI genuinely works
(`steamodded-ui-reference.md` §"LuaJIT FFI"), and this would give real queries.
Rejected on three counts. First, shipping and resolving a native library for
Windows, macOS (x86-64 and arm64) and Linux, in a mod distributed as a zip —
and being the person who debugs "it says could not load sqlite3" for someone
on Steam Deck. Second, you would hand-write the C binding *and* the SQL in Lua
and then keep both in lockstep with the ingester's schema migrations forever;
the viewer needs perhaps eight fixed queries, so the flexibility that justifies
the cost is flexibility we do not use. Third, the DB may be mid-write when the
game opens it. The one thing it buys that the index does not — arbitrary
per-joker maxima — is better served by a 30 KB JSON file the ingester already
has the data to emit.

**(d) Local helper over HTTP via `SMODS.https`.** The worst failure modes of
the five. `https.request` is **synchronous and blocks the game thread**;
`asyncRequest` spawns one OS thread per in-flight request; and when no backend
resolves, every request silently returns `0, "Failed to load a suitable
backend!"` rather than erroring, so a broken install looks like an empty
history (`smods-https.lua:44-64, 204-208`). On top of that it requires the
player to keep a background process running, and whether LÖVE's Windows `https`
module even accepts plain `http://` is unverified. Rejected.

**(a) verbatim — ingester emits the index.** Rejected for the freshness reason
above, but its *shape* is adopted wholesale for tier 2, where staleness is
genuinely acceptable: a record from last week is still a record, and the view
can say "as of 20 Sep" without lying.

### Graceful degradation, case by case

| Situation | Behaviour |
|---|---|
| Ingester has never run | Runs tab fully functional. Records tab shows the bests derivable from the index alone — best hand, highest ante, win rate, runs per deck — and a line: *"Per-joker records need the ingester. Run `python -m balatrodb.ingest`."* |
| `records.json` is older than the newest run | Records shown with `as of <date> · 7 runs not yet ingested`. Index-derived headline bests are recomputed live so they are never wrong; only the per-joker layer is stale. |
| Index file missing or version-mismatched | Rebuild by head/tail scan with a progress bar, frame-spread on the `bdb_viewer` queue. ~4 s for 300 runs today, sub-second after §7. |
| A run log has no `run.end` (crash) | The tail read still yields *some* last line, and the envelope alone gives `a`, `r`, `el`, `seg`, `n`. Row renders as `— crashed —` with the ante it reached. |
| A run is in progress right now | Same path. Row is marked `· live ·` and sorts first. |
| `getDirectoryItems` returns `{}` | Indistinguishable from a failed mount (`nativefs.lua:352-364`, errors swallowed). Cross-check with `NFS.getInfo("BalatroDB/runs")`: directory exists but lists empty ⇒ show "no runs yet"; directory missing ⇒ show "couldn't read the log folder". |

---

## 3. The index file

```
%APPDATA%/Balatro/BalatroDB/index.jsonl
```

**Append-only, one line per run *state transition*, last-wins by run id.**
Append-only because that is already the mod's entire write discipline and it is
crash-safe by construction; rewriting a whole-file JSON blob on every run end
introduces a truncation window that the run logs deliberately do not have.

```json
{"v":1,"run":"1790037335-V7VC8SEY-4f7e","k":"s","ts":1790037335,
 "deck":"Ghost Deck","deck_key":"b_ghost","stake":8,"stake_key":"stake_gold",
 "seeded":false,"challenge":null,"profile":1,"win_ante":8}
{"v":1,"run":"1790037335-V7VC8SEY-4f7e","k":"e","ts":1790039334,
 "result":"died","terminal":true,"won":false,"endless":false,
 "ante":6,"furthest_ante":6,"furthest_round":18,"best_hand":211320,
 "round":18,"hands_played":83,"dollars":3,"deck_size":68,"size":625113}
```

~200 B + ~220 B per run. A thousand runs is ~420 KB and ~85 ms to decode
whole — at which point compaction is worth it, and **compaction is the
ingester's job**: it reads every log anyway, so it can rewrite the index as one
line per run and the mod never needs a rename-or-truncate dance. The mod only
ever appends.

`size` is the log's byte length at close, so the reader can tell "this row is
current" from "this run has been resumed since" without stat-ing anything it
has already folded.

### Rebuild: two seeks per run, not a full parse

`getDirectoryItemsInfo` gives `size` and `modtime` per file, and nativefs opens
in **binary** mode (`MODEMAP = { r = 'rb', ... }`, `nativefs.lua:411`), so byte
offsets are exact and `File:seek` (`nativefs.lua:190`) is trustworthy. The
head is line 1 (`run.start`, `seg=0 n=0`); the tail is the last line, whatever
it is.

```lua
local TAIL = 16 * 1024   -- covers the 11 629 B longest line observed, 2x over

local function last_line(path, size)
    local f = NFS.newFile(path)
    if not f:open('r') then return nil end
    local want = math.min(TAIL, size)
    f:seek(size - want)
    local blob = f:read(want)                  -- nativefs.lua:100, one-arg form
    f:close()
    blob = blob:gsub('\n+$', '')
    return blob:match('([^\n]*)$'), want < size and not blob:find('\n')
    --      ^ last line             ^ true = window held no newline; retry wider
end
```

If the window contains no newline and did not reach byte 0, double `TAIL` and
retry once at 64 KB, then give up and mark the run `summary unavailable`. The
writer only ever appends complete lines (`log.lua:130-135` concatenates
newline-terminated strings), so the file always ends on a line boundary even
while a run is live.

Measured cost of the head+tail decode today: **2.85 ms per run** (1.00 for the
5.3 KB `run.start`, 1.85 for the 10.2 KB `run.end`). After §7 it is 0.15 ms.

### Cache key

`{name, size, modtime}` from `getDirectoryItemsInfo`. A file whose size *and*
modtime match the cached entry is skipped without opening it. A suspended run
that is later resumed changes both, so it re-scans naturally.

---

## 4. Screen layout

Balatro's idiom, and the constraints that produce it: **content budget is
~12–16 units wide × 8–9 tall**, `maxw` scales text down rather than clipping,
and there is no scrolling in the base game. So: chunky rows, few columns,
generous padding, and paging.

### 4.1 Shell

`create_tabs` inside `create_UIBox_generic_options`. Two tabs, `tab_h = 8` to
reserve the region so the box does not resize on switch. Remember
`create_tabs` re-runs the body function on every switch with no caching — all
state lives in `BDB.Viewer`, a flat table outside the tree.

```
╔══════════════════════════════════════════════════════════════════╗
║  ←                    ( RUNS )      RECORDS                      ║
╠══════════════════════════════════════════════════════════════════╣
║                                                                  ║
║   < tab body, 16 wide x 8 tall >                                 ║
║                                                                  ║
╚══════════════════════════════════════════════════════════════════╝
```

### 4.2 Runs tab — list

```
┌──────────────────────────────────────────────────────────────────┐
│ ┌──────────────┐ ┌───────────┐ ┌──────────────┐ ┌──────────────┐ │
│ │ ‹ ALL DECKS ›│ │ ‹ ALL   › │ │ ‹ ALL RUNS › │ │ ‹ RECENT   › │ │   filter bar
│ │    deck      │ │   stake   │ │  endless?    │ │    sort      │ │   1.0 tall
│ └──────────────┘ └───────────┘ └──────────────┘ └──────────────┘ │
├──────────────────────────────────────────────────────────────────┤
│  1 │ Ghost Deck       [GOLD] │ A6 │    211,320 │  DIED  │ Sep 21 │
│  2 │ Red Deck         [BLUE] │ A9 │    833,027 │ ● LIVE │ Sep 21 │   10 rows
│  3 │ Anaglyph Deck   [WHITE] │ A8 │    381,038 │  WON ★ │ Sep 21 │   0.62 tall
│  4 │ Plasma Deck     [BLACK] │ A4 │     23,699 │  QUIT  │ Sep 20 │   each
│  5 │ Red Deck        [WHITE] │ A2 │          — │  QUIT  │ Sep 20 │
│  6 │ Checkered Deck  [PURPLE]│ A1 │          — │ ⏸ SAVE │ Sep 20 │
│  7 │ …                                                           │
│  8 │                                                             │
│  9 │                                                             │
│ 10 │                                                             │
├──────────────────────────────────────────────────────────────────┤
│           ‹        Page 1 / 14   ·   138 runs        ›           │   0.6 tall
└──────────────────────────────────────────────────────────────────┘
```

Columns, and why these six:

| Column | Width | Source | Why |
|---|---:|---|---|
| index | 0.5 | position in filtered view | the challenge-list convention; gives the eye an anchor |
| deck + stake chip | 4.6 | `deck`, `stake_key` | the two things you filter on, so they must be visible to confirm the filter |
| ante | 0.9 | `furthest_ante` | the headline "how far" number |
| best hand | 2.4 | `best_hand` | the headline "how big" number, right-aligned, comma-grouped |
| result | 1.5 | `result` + `won` + `endless` | `WON ★`, `DIED`, `ENDLESS A14`, `⏸ SAVE`, `● LIVE`, `— crashed —` |
| date | 1.6 | `ts` | disambiguates the fifteen Red Deck runs |

≈ 11.5 units of content, ~12.5 with padding. Comfortably inside budget.

Deliberately **not** columns: seed, hands played, money, run length. They go in
the detail view. Six columns is already at the top of what reads as Balatro
rather than as a spreadsheet.

The stake chip is the stake's own colour swatch with the stake name, matching
`G.UIDEF.deck_stake_column` / `G.UIDEF.current_stake`
(`UI_definitions.lua:3400, 3415`) — free recognition, no text density.

Row chrome is the standard data row shared by high-score, hand and progress
rows: `colour = darken(G.C.JOKER_GREY, 0.1), r = 0.1, emboss = 0.05,
padding = 0.05`, columns aligned by matched `minw == maxw`. The whole row is a
`UIBox_button` carrying `id = <run_id>`, following
`G.UIDEF.challenge_list_page` (`UI_definitions.lua:5919`).

### 4.3 Runs tab — one run

Clicking a row replaces the tab body. Not a new overlay — an overlay on an
overlay complicates `exit_overlay_menu` teardown, and the back button is
cheaper.

```
┌──────────────────────────────────────────────────────────────────┐
│  ‹ BACK      Ghost Deck  ·  [GOLD STAKE]  ·  21 Sep 2026 20:08   │
├────────────────────────────────┬─────────────────────────────────┤
│  BEST HAND        211,320      │   ┌────┐┌────┐┌────┐┌────┐      │
│  FURTHEST         Ante 6 · R18 │   │Blue││Rid ││Ban ││Obe │      │  CardArea,
│  HANDS PLAYED     83           │   │prnt││Bus ││ana ││lisk│      │  final jokers
│  DECK SIZE        68 (from 52) │   └────┘└────┘└────┘└────┘      │  card_limit 5
│  ENDED            Died, A6     │      x3.0  +40   —    x7.4      │
│  SEED             V7SC8SEY     │                                 │
├────────────────────────────────┴─────────────────────────────────┤
│  ANTE   1    2    3    4    5    6                               │
│  score  ▁    ▂    ▂    ▃    ▅    █    211k                       │  per-ante bars
│  hands  4    6    5    9   12   13                               │  from round.end
├──────────────────────────────────────────────────────────────────┤
│        ‹ SUMMARY ›   ROUNDS    JOKERS    PURCHASES               │
└──────────────────────────────────────────────────────────────────┘
```

Built from a full parse of that one file (125 ms worst observed, frame-spread
over ~30 frames — invisible). The sub-view cycler at the bottom re-renders from
the already-parsed table; only the first open pays the parse, and the parsed
result is held until the player returns to the list.

`ROUNDS` is the one genuinely list-shaped sub-view — up to ~30 rows for a deep
endless run. Page it at 10, same mechanism as the run list.

### 4.4 Records tab

```
┌──────────────────────────────────────────────────────────────────┐
│  ‹ ALL DECKS ›   ‹ ALL STAKES ›   ‹ NON-ENDLESS ›                │
│                              as of 20 Sep · 7 runs not ingested  │   staleness
├──────────────────────────────────────────────────────────────────┤
│   BEST HAND          833,027     Red Deck · Blue · 21 Sep   →    │
│   FURTHEST ANTE      Ante 14     Red Deck · Blue · 21 Sep   →    │   index-derived,
│   MOST MONEY         $312        Plasma · Gold · 18 Sep     →    │   always fresh
│   WIN RATE           4 / 31  (13%)                               │
├──────────────────────────────────────────────────────────────────┤
│   PER-JOKER MAXIMA                        ‹ by value ›           │
│   ┌────┐  ┌────┐  ┌────┐  ┌────┐  ┌────┐                        │
│   │Obe │  │Rid │  │Con │  │Spa │  │Squ │                         │   ingester-
│   │lisk│  │Bus │  │stel│  │Trou│  │Joke│                         │   derived
│   └────┘  └────┘  └────┘  └────┘  └────┘                        │
│    x7.4    +40     x4.5    +20     +40                           │
│    Sep 21  Sep 21  Sep 18  Sep 21  Sep 12                        │
├──────────────────────────────────────────────────────────────────┤
│                    ‹      Page 1 / 6      ›                      │
└──────────────────────────────────────────────────────────────────┘
```

**"Highest Obelisk I've ever had" is a record, not a run sort.** Offering it in
the run-list sort cycler would mean every run row carries a per-joker maximum,
which means tier-2 data for every run — the 16-second scan. In the Records tab
it is one lookup in `records.json`, and clicking the tile jumps straight to the
run that set it (`records.json` carries the `run` id). That is the query the
player actually wants and it costs nothing.

The joker tiles are real `Card` objects in one `CardArea`, five per row, two
rows, paged. One CardArea for the whole grid, rebuilt in place on page change —
the collection screens' idiom (`G.FUNCS.your_collection_joker_page`,
`button_callbacks.lua:612`) rather than tearing down the tree. See §6 for why
that matters.

### 4.5 Paging, not scrolling

Base Balatro has no scrolling container
(`balatro-ui-reference.md` §"The headline"). Two options: `SMODS.UIScrollBox`
(`smods/src/ui.lua:211-299`) or vanilla paging via `create_option_cycle`.

**Choose paging.** Three reasons. It is what every list in the game does, so
the screen looks native instead of looking like a mod. It has working
controller focus out of the box — `SMODS.UIScrollBox` depends on
`G.FUNCS.controller_scroll`, which Steamodded supplies and the base game does
not, and gamepad scrolling in a mod screen is a support burden nobody wants.
And it bounds the node count: ten rows is ten rows whether the player has 30
runs or 3000, whereas a scroll box builds every row up front.

10 rows per page, matching `G.CHALLENGE_PAGE_SIZE`
(`UI_definitions.lua:5883`).

---

## 5. Navigation and code sketches

### 5.1 Reaching the screen

**Both**, in this order of preference at runtime:

1. **A title-screen button**, because "check the run I just finished" should be
   one click from where the player lands after a run ends.
2. **`SMODS.current_mod.extra_tabs`**, always, as the guaranteed path.

The menu wrap touches `menu.nodes[1].nodes[1].nodes`, an undocumented node path
that Steamodded itself also mutates (`smods/src/ui.lua:2040`). That is exactly
the kind of thing a Balatro patch breaks — and it breaks at the title screen,
where an error is fatal. So it is guarded in the mod's own house style: a
structure check, a `pcall`, and degrade to "no button" rather than "no title
screen".

```lua
-- entry.lua
local ref = create_UIBox_main_menu_buttons
function create_UIBox_main_menu_buttons()
    local menu = ref()
    local ok = pcall(function()
        local slot = menu.nodes[1].nodes[1].nodes
        assert(type(slot) == 'table', 'unexpected main menu shape')
        slot[#slot + 1] = UIBox_button{
            id = 'bdb_runs', button = 'bdb_open_viewer', label = { 'Runs' },
            minh = 1.55, minw = 1.85, col = true,
            colour = G.C.BOOSTER, scale = 0.45 * 1.2,
        }
    end)
    if not ok then
        sendWarnMessage('main menu shape changed; use Mods > BalatroDB > Runs', 'BalatroDB')
    end
    return menu
end

G.FUNCS.bdb_open_viewer = function()
    G.SETTINGS.paused = true
    BDB.Viewer.open()
end

SMODS.current_mod.extra_tabs = function()
    return {{ label = 'Runs', tab_definition_function = function()
        return BDB.Viewer.tab_body()        -- returns a UIBox definition
    end }}
end
```

Note the two entry points return different things: `extra_tabs` wants a
definition to be embedded in Steamodded's own tab shell, while the menu button
opens a full overlay. `tab_body()` is the shared piece.

**No in-run entry point in v1.** The guiding constraint is *never break a run*,
and the viewer allocates heavily and holds CardAreas. If it is wanted later, a
Lovely patch on the in-run options menu is the route
(`smods/lovely/menu.toml:6-37`), gated behind a config flag.

### 5.2 Opening the overlay

```lua
function BDB.Viewer.open()
    BDB.Viewer.state = BDB.Viewer.state or {
        page = 1, sel = nil, view = nil,
        filter = { deck = nil, stake = nil, endless = nil },
        sort = 'recent',
    }
    G.FUNCS.overlay_menu{
        definition = BDB.Viewer.shell(),
        config = { offset = { x = 0, y = 0 }, align = 'cm' },
    }
    BDB.Viewer.begin_load()       -- async; the list slot shows a spinner meanwhile
end
```

`overlay_menu` rebuilds `config` and keeps only `align`, `offset`, `major`,
`no_esc` (`button_callbacks.lua:1359-1365`); `offset = {x=0,y=0}` suppresses
the slide-up.

### 5.3 Loading, spread across frames

One named queue so leaving the screen cancels everything in flight
(`smods/src/utils/run_select.lua:34, 598, 634`).

```lua
local QUEUE = 'bdb_viewer'
local BUDGET = 0.004                       -- seconds of work per frame

function BDB.Viewer.begin_load()
    local S = BDB.Viewer.state
    S.rows, S.loading, S.progress = {}, true, 0

    local idx = BDB.Index.read()                     -- one file, ~25 ms / 300 runs
    local info = NFS.getDirectoryItemsInfo('BalatroDB/runs') or {}
    local todo = {}
    for _, e in ipairs(info) do
        if e.name:sub(-6) == '.jsonl' then
            local run = e.name:sub(1, -7)
            local have = idx[run]
            if have and have.size == e.size then
                S.rows[#S.rows + 1] = have           -- cache hit, no file open
            else
                todo[#todo + 1] = { run = run, info = e }
            end
        end
    end

    if #todo == 0 then return BDB.Viewer.finish_load() end

    local i = 1
    G.E_MANAGER:add_event(Event({
        blocking = false, blockable = false, timer = 'REAL',
        func = function()
            local t0 = love.timer.getTime()
            while i <= #todo and love.timer.getTime() - t0 < BUDGET do
                local ok, row = pcall(BDB.Index.scan_one, todo[i])
                if ok and row then S.rows[#S.rows + 1] = row end
                i = i + 1
            end
            S.progress = i / #todo
            if i > #todo then BDB.Viewer.finish_load(); return true end
            -- returning nothing re-runs next frame
        end,
    }), QUEUE)
end
```

`timer = 'REAL'` so it keeps ticking while the game is paused in a menu
(`smods/src/ui.lua:3130`). `blockable = false` so an unrelated animation cannot
stall it.

### 5.4 Filter bar

Four `create_option_cycle`s. `opt_callback` is a **string naming a `G.FUNCS`
entry** and receives **one table** whose `cycle_config.current_option` is the
new 1-based index (`button_callbacks.lua:581-589`).

```lua
local function filter_bar()
    local S = BDB.Viewer.state
    return {n = G.UIT.R, config = {align = 'cm', padding = 0.08}, nodes = {
        create_option_cycle{
            id = 'bdb_f_deck', options = BDB.Viewer.deck_options(),
            current_option = S.opt_deck or 1, w = 3.4, scale = 0.8,
            text_scale = 0.4, colour = G.C.RED, no_pips = true,
            opt_callback = 'bdb_set_filter',
            focus_args = {snap_to = true},
        },
        {n = G.UIT.B, config = {w = 0.08, h = 0.01}},
        create_option_cycle{ id = 'bdb_f_stake',   --[[ … ]] },
        {n = G.UIT.B, config = {w = 0.08, h = 0.01}},
        create_option_cycle{ id = 'bdb_f_endless',
            options = {'All Runs', 'Non-Endless', 'Endless Only'}, --[[ … ]] },
        {n = G.UIT.B, config = {w = 0.08, h = 0.01}},
        create_option_cycle{ id = 'bdb_f_sort',
            options = {'Recent', 'Best Hand', 'Furthest Ante', 'Longest'}, --[[ … ]] },
    }}
end

G.FUNCS.bdb_set_filter = function(args)
    local S  = BDB.Viewer.state
    local id = args.cycle_config.id
    local i  = args.cycle_config.current_option

    if     id == 'bdb_f_deck'    then S.opt_deck = i;  S.filter.deck    = BDB.Viewer.deck_options_keys[i]
    elseif id == 'bdb_f_stake'   then S.opt_stake = i; S.filter.stake   = BDB.Viewer.stake_keys[i]
    elseif id == 'bdb_f_endless' then S.opt_end = i;   S.filter.endless = ({nil, false, true})[i]
    elseif id == 'bdb_f_sort'    then S.opt_sort = i;  S.sort = ({'recent','best_hand','ante','hands'})[i]
    end

    BDB.Viewer.reproject()      -- rebuild S.view from S.rows; microseconds for 300
    S.page = 1
    BDB.Viewer.swap_list()
end
```

**How filters combine: conjunctively, with `nil` meaning "no constraint."**
Deck, stake and endless are AND-ed; sort is orthogonal. Reprojection is a
filter-then-`table.sort` over in-memory tables — a few hundred entries, well
under a frame, so no spreading and no incremental machinery.

The deck and stake option lists are built **from the data**, not from
`G.P_CENTER_POOLS`: only decks the player has actually played appear. Fewer
cycler steps, and it sidesteps the `stake` index instability the schema already
warns about — filter on `stake_key`, display via `stake` for ordering.

### 5.5 The list region and its swap

The vanilla data-table pattern: a fixed-size region holding a placeholder
`Moveable()`, whose `config.object` is replaced per page
(`UI_definitions.lua:5901` / `button_callbacks.lua:1659`).

```lua
function BDB.Viewer.tab_body()
    return {n = G.UIT.ROOT, config = {align = 'cm', colour = G.C.CLEAR}, nodes = {
        {n = G.UIT.C, config = {align = 'cm', padding = 0.1}, nodes = {
            filter_bar(),
            {n = G.UIT.R, config = {align = 'cm', padding = 0.05, minh = 6.4, minw = 12.4}, nodes = {
                {n = G.UIT.O, config = {id = 'bdb_list', object = Moveable()}},
            }},
            {n = G.UIT.R, config = {align = 'cm', padding = 0.1}, nodes = {
                create_option_cycle{
                    id = 'bdb_page', options = BDB.Viewer.page_labels(),
                    current_option = 1, w = 3.5, h = 0.3, scale = 0.9,
                    no_pips = true, cycle_shoulders = true,
                    colour = G.C.BOOSTER, opt_callback = 'bdb_change_page',
                },
            }},
        }},
    }}
end

function BDB.Viewer.swap_list()
    local slot = G.OVERLAY_MENU and G.OVERLAY_MENU:get_UIE_by_ID('bdb_list')
    if not slot then return end
    if slot.config.object then slot.config.object:remove() end
    slot.config.object = UIBox{
        definition = BDB.Viewer.list_page(BDB.Viewer.state.page),
        config = {offset = {x = 0, y = 0}, align = 'cm', parent = slot},
    }
    slot.UIBox:recalculate()
end

G.FUNCS.bdb_change_page = function(args)
    BDB.Viewer.state.page = args.cycle_config.current_option
    BDB.Viewer.swap_list()
end
```

`minh = 6.4, minw = 12.4` on the region is what stops the whole box resizing
when a short page renders. Without it, page 14 with three rows makes the dialog
jump.

### 5.6 One row

```lua
local PAGE = 10
local RESULT_COLOUR = {
    won = G.C.GREEN, died = G.C.RED, suspended = G.C.BLUE,
    live = G.C.ORANGE, crashed = G.C.UI.BACKGROUND_INACTIVE,
}

local function cell(text, w, colour, align)
    return {n = G.UIT.C, config = {align = align or 'cm', minw = w, maxw = w}, nodes = {
        {n = G.UIT.T, config = {
            text = text, scale = 0.4, colour = colour or G.C.UI.TEXT_LIGHT,
            lang = G.LANGUAGES['en-us'],     -- keep numeric columns from reflowing in CJK
        }},
    }}
end

function BDB.Viewer.list_page(page)
    local S, nodes, snapped = BDB.Viewer.state, {}, false
    if S.loading then
        return {n = G.UIT.ROOT, config = {align = 'cm', colour = G.C.CLEAR}, nodes = {
            {n = G.UIT.T, config = {
                text = ('Reading runs… %d%%'):format(math.floor(S.progress * 100)),
                scale = 0.5, colour = G.C.UI.TEXT_LIGHT}},
        }}
    end

    for i = PAGE * (page - 1) + 1, math.min(PAGE * page, #S.view) do
        local r = S.view[i]
        nodes[#nodes + 1] = {n = G.UIT.R, config = {
            align = 'cm', padding = 0.05, r = 0.1, emboss = 0.05,
            colour = darken(G.C.JOKER_GREY, 0.1),
            button = 'bdb_open_run', id = r.run,
            focus_args = {snap_to = not snapped},
        }, nodes = {
            cell(tostring(i), 0.5),
            cell(r.deck, 2.9, G.C.WHITE, 'cl'),
            BDB.Viewer.stake_chip(r.stake_key, 1.6),
            cell('A' .. tostring(r.furthest_ante or r.ante or '?'), 0.9),
            cell(BDB.fmt.score(r.best_hand), 2.4, G.C.BLUE, 'cr'),
            cell(BDB.Viewer.result_label(r), 1.5, RESULT_COLOUR[BDB.Viewer.result_kind(r)]),
            cell(BDB.fmt.date(r.ts), 1.6, G.C.UI.TEXT_INACTIVE),
        }}
        snapped = true
    end

    return {n = G.UIT.ROOT, config = {align = 'tm', padding = 0.05, colour = G.C.CLEAR}, nodes = nodes}
end
```

Three details lifted straight from `challenge_list_page`
(`UI_definitions.lua:5919-5946`): `id` on the clickable node so the callback
knows which row, `focus_args = {snap_to = not snapped}` so the gamepad lands on
row 1 only, and a *string* `'nil'` as `button` to render a dead row — used here
for a run whose log could not be read.

Note `C` lays children left→right and `R` top→bottom, which is backwards from
the names (`engine/ui.lua:191-206`). Rows are `R` containing `C` cells.

### 5.7 Detail view — full parse of one file

```lua
function BDB.Viewer.load_run(run_id)
    local S = BDB.Viewer.state
    S.detail = { run = run_id, loading = true, events = {}, progress = 0 }

    local text = NFS.read('BalatroDB/runs/' .. run_id .. '.jsonl')
    if not text then return BDB.Viewer.detail_error('could not read log') end

    local pos, total = 1, #text
    G.E_MANAGER:add_event(Event({
        blocking = false, blockable = false, timer = 'REAL',
        func = function()
            local t0 = love.timer.getTime()
            while pos <= total and love.timer.getTime() - t0 < BUDGET do
                local nl = text:find('\n', pos, true) or (total + 1)
                local line = text:sub(pos, nl - 1)
                pos = nl + 1
                if #line > 1 then
                    local ok, ev = pcall(JSON.decode, line)
                    if ok then BDB.Detail.fold(S.detail, ev) end
                    -- a bad line is skipped, never fatal: logs outlive schemas
                end
            end
            S.detail.progress = pos / total
            if pos > total then
                S.detail.loading = false
                BDB.Viewer.swap_detail()
                return true
            end
        end,
    }), QUEUE)
end
```

`BDB.Detail.fold` accumulates into a summary and **discards the event**, so
peak memory is the file string plus the summary, not 1600 decoded tables. On
the largest observed run this is ~0.6 MB of string and a few KB of summary.
Holding every decoded event would be roughly 5–10× the file size in Lua table
overhead; don't.

---

## 6. Performance and teardown

### Where it degrades

| Runs | List open, warm index | List open, cold rebuild (today) | Cold rebuild after §7 |
|---:|---:|---:|---:|
| 30 | ~3 ms | 85 ms | ~5 ms |
| 300 | ~25 ms | **~0.9 s CPU → ~4 s wall** | ~45 ms |
| 1 000 | ~85 ms | ~2.9 s CPU → ~12 s wall | ~150 ms |
| 3 000 | ~250 ms, index needs compaction | ~9 s CPU → ~36 s wall | ~450 ms |

The warm path — which is the path on every open after the first — stays under a
frame until roughly **1000 runs**, and the mitigation past that is index
compaction (ingester-side, §3), which turns 2 lines per run into 1.

The cold path only happens on first install, after deleting the index, or after
copying logs from another machine. It shows a progress bar and is cancellable.
It is also the number §7 improves by 19×, which is why §7 is the cheapest thing
on this list.

Rendering is flat regardless: 10 rows × ~8 nodes = ~80 nodes per page.

### Teardown — the thing that actually leaks

**CardAreas are not garbage collected.** `SMODS.RunSelect.clean_up`
(`smods/src/utils/run_select.lua:721-746`) is the template: clear the named
event queue, `remove_all()` every CardArea, and manually splice stale areas out
of `G.I.CARDAREA` (`:376-389`), all hooked into `G.FUNCS.exit_overlay_menu`
(`:748-753`).

```lua
local ref_exit = G.FUNCS.exit_overlay_menu
G.FUNCS.exit_overlay_menu = function(...)
    if BDB.Viewer.active then
        G.E_MANAGER:clear_queue(QUEUE)          -- cancel any in-flight scan
        for _, area in ipairs(BDB.Viewer.areas) do
            area:remove_all()
            for i = #G.I.CARDAREA, 1, -1 do
                if G.I.CARDAREA[i] == area then table.remove(G.I.CARDAREA, i) end
            end
        end
        BDB.Viewer.areas = {}
        BDB.Viewer.state.detail = nil           -- drop the parsed run
        BDB.Viewer.active = false
    end
    return ref_exit(...)
end
```

Three rules that follow:

1. **One CardArea per screen region, reused.** The records grid creates its
   CardArea once and mutates its contents on page change
   (`G.FUNCS.your_collection_joker_page`, `button_callbacks.lua:612`), rather
   than building a new one per page. Ten pages of five jokers is 1 CardArea,
   not 10.
2. **Everything async goes on `QUEUE`.** A scan that outlives the screen writes
   into a state table nothing reads and burns frames forever.
3. **Drop `state.detail` on exit.** It is the only large allocation the viewer
   holds, and keeping it means the last-viewed run's parse stays resident for
   the whole session.

`S.rows` (the index) is deliberately *kept* across opens — it is ~420 B per run
and rebuilding it is the expensive part. Invalidate it by comparing
`getDirectoryItemsInfo` on reopen; only changed files are rescanned.

---

## 7. Changes the mod should make — ranked by payoff per line

The mod is still malleable, so these are cheap now and expensive later.

### 7.1 Split the bulky arrays out of `run.start` / `run.end` — **highest payoff**

`run.start` is 5.3 KB and `run.end` is 10.2 KB, and in both cases ~95 % of that
is `deck_cards` / `deck` / `jokers`. Those two lines are exactly what a
run-list row needs and exactly what the head/tail scan must decode.

Emit the baseline as its own event immediately after:

```
run.start   {..., deck:"Ghost Deck", stake:8, ...}          554 B    0.095 ms
run.baseline {jokers:[...], consumables:[...], deck_cards:[...]}
…
run.end     {result:"died", won:false, ante:6, ...}         270 B    0.055 ms
run.final   {jokers:[...], deck:[...]}
```

**Measured effect: 2.85 ms → 0.15 ms per run. A cold rebuild of 300 runs goes
from ~0.9 s to ~45 ms.** The ingester loses nothing — it reads every line
anyway and the pairing is positional (`run.baseline` always follows
`run.start` within the same segment). This is a one-afternoon change that
removes the only real performance problem in the design.

### 7.2 Put the leaderboard numbers in `run.end`

`run.end.score` is `util.num(game.chips)` (`src/hooks.lua:167`) — the chips
scored in the **final round**, not the best hand. Sorting a run list by it is
wrong: a run that peaked at 800k in ante 8 and then died in ante 9 with 40k
would sort as a 40k run. Nobody would notice until they wondered why their best
run was in the middle of the list.

Balatro already tracks exactly the right numbers, all plain scalars, all free
at teardown (`game.lua:1910-1920`):

```lua
local rs = game.round_scores or {}
best_hand      = util.num(rs.hand and rs.hand.amt),
furthest_ante  = util.num(rs.furthest_ante  and rs.furthest_ante.amt),
furthest_round = util.num(rs.furthest_round and rs.furthest_round.amt),
cards_played   = rs.cards_played and rs.cards_played.amt,
```

Rename or keep `score`, but the list needs `best_hand`. Without it, "sort by
best score" requires scanning every `hand.play` in every log — 14.9 % of the
corpus — which moves a tier-0 column into tier 2 single-handedly.

### 7.3 Put the endless flag in `run.end`

Endless-vs-non-endless is a **requested filter** and today it is only derivable
by finding a `run.win` line somewhere in the middle of the file, or by scanning
for any event with `el: true`. Both defeat head/tail scanning entirely.

`state.lua` already latches it. One field:

```lua
endless = state.endless and true or false,
```

A companion `entered_endless_at_ante` would let the list show `ENDLESS A14`
instead of `WON`, which is the more interesting label.

### 7.4 Serialize the envelope in an explicit key order

Every one of the 6457 lines in the corpus has key order
`t, el, n, seg, run, d, r, v, a, e` — the event type `e` is **last**, after the
payload. 747 lines put `e` past byte 512, and on a `round.end` it is 11 KB in.
That order is LuaJIT's hash order for that exact key set; add a field and it
can change silently.

Write the envelope with `d` last and a fixed order, so a reader can cheaply
classify a line from `line:sub(1, 96)` before deciding whether to decode it:

```json
{"v":1,"run":"…","seg":0,"n":412,"e":"joker.scale","a":5,"r":13,"el":false,"t":903.44,"d":{…}}
```

This makes a *filtered* scan possible — "decode only `joker.scale` and
`round.end`" — which is the difference between a records rebuild that is
feasible in Lua and one that is not. It also makes the format robust against a
LuaJIT change nobody would think to test for. Cost: build the line with
concatenation around one `JSON.encode(d)` instead of encoding the whole table.

### 7.5 Expose `log.flush()` and call it when the viewer opens

`log.flush` is already a module function (`src/log.lua:68`). The viewer must
call it before scanning so an in-progress run's last 16 KB
(`FLUSH_BYTES = 16 * 1024`, `FLUSH_EVENTS = 128`) is on disk. Needs only that
`BalatroDB.log` stays reachable from the viewer module.

### 7.6 Write the index

Append to `BalatroDB/index.jsonl` in `emit_run_start` and `end_run`. ~30 lines,
reusing the existing writer. Format in §3.

### 7.7 Tolerate old vocabularies

The existing corpus contains `result` values `"quit"` and `"loss"` that no
longer appear in the schema, and early runs have no `terminal` and no
`at_state`. **Logs outlive schemas.** The viewer maps unknown `result` strings
to a neutral `— ended —` chip rather than showing `ERROR`, and every field read
from a log is treated as optional. That is a viewer rule, not a mod change, but
it is worth writing down because the temptation to `assert` is real.

### 7.8 Log rotation interacts with this

The README already flags that nothing deletes anything and a long endless run
can reach tens of MB. A single 40 MB log makes tier 1 (full parse of one run)
cost ~8 s — the one place the viewer's timing assumptions break. Mitigation
when it happens: cap the detail parse, decode only the events the current
sub-view needs (which §7.4 makes possible), and show "this run is very large,
showing summary only."

---

## 8. Staged plan

**v1 — the list.** Entry points (menu button + `extra_tabs`). Index file
written by the mod, rebuilt by head/tail scan when absent. Runs tab: paged
list, deck / stake / endless filters, recent / best-hand / furthest-ante sorts.
Progress bar and cancellable load. Full teardown hook. No detail view, no
records. Ship §7.1, §7.2, §7.3, §7.5, §7.6 alongside it — v1 is substantially
worse without them and they are all small.

This is the version that answers "how did that run go" and "show me my Plasma
Deck runs," which is most of the value.

**v2 — one run.** Click-through detail view: summary panel, final jokers in a
CardArea, per-ante bars, and a paged rounds table. Full parse of one file,
frame-spread, dropped on exit.

**v3 — records.** Ingester emits `records.json`. Records tab with the
index-derived headline bests always live, the per-joker grid from the file,
staleness banner, and click-a-tile-to-open-that-run. Also the point at which
index compaction lands.

**v4 — polish.** Deck thumbnails in list rows. A seed-search text input
(`create_text_input`; SMODS patches it to allow several per screen,
`lovely/ui_elements.toml:80-125`). Per-deck and per-stake summary cards. An
in-run entry point behind a config flag.

**Explicitly not planned.** Replay playback — the schema is built for it but it
is a separate screen with a separate problem. Cross-profile aggregation —
`profile` is recorded, but mixing profiles in one leaderboard is a decision the
player should make, not the default.

---

## 9. Open questions

- **LuaJIT parse throughput has not been measured in-game.** Every timing here
  is from Lua 5.5 under lupa. The first thing v1 should do is log the real
  number, because if LuaJIT is 3× faster the cold-rebuild path stops being
  worth optimising at all, and if it is somehow slower §7.1 goes from
  "high payoff" to "required."
- **Does `SMODS.UIScrollBox` handle gamepad focus acceptably?** If yes, the
  rounds table in the detail view would read better as a scroll than as pages.
  Worth 20 minutes before v2.
- **Should the index live in the DB directory rather than next to the runs?**
  Keeping it in `BalatroDB/` beside `runs/` means a player who zips their
  BalatroDB folder carries it along. Keeping it next to the SQLite file groups
  derived artefacts. Mild preference for beside `runs/`, since the mod writes
  it and the mod may not know where the DB is.
