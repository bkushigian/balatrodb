# Steamodded API reference (for the in-game viewer)

Findings from a source survey of `Mods/smods-26.829.0`. Citations are
`file:line` within that directory unless noted. Written down because it answers
the questions the viewer design depends on.

## File access — the mod CAN read the run logs directly

`SMODS.NFS` (also the global `NFS`) is nativefs, which is **FFI `fopen`, not the
LÖVE sandbox**. Its working directory is set to
`love.filesystem.getSaveDirectory()` at `src/preflight/core.lua:45` — i.e.
`%APPDATA%\Balatro` on Windows. So:

```lua
SMODS.NFS.getDirectoryItems("BalatroDB/runs")        -- string[]
SMODS.NFS.read("BalatroDB/runs/<file>.jsonl")        -- string
SMODS.NFS.getDirectoryItemsInfo("BalatroDB/runs")    -- {type,size,modtime,name}[]
```

Absolute paths anywhere on disk work too. **`getDirectoryItemsInfo` returning
`modtime` and `size` is the key to incremental scanning** — cache by modtime and
only re-read changed files.

Caveat: `getDirectoryItems` temporarily PhysFS-mounts the directory
(`libs/nativefs/nativefs.lua:352-364`) and **swallows errors, returning `{}`** —
an empty directory and a failed mount are indistinguishable.

## LuaJIT FFI is available and unrestricted

`require('ffi')` works and is used throughout: `libs/nativefs/nativefs.lua:25`
calls `C.fopen`/`C.fread` directly, `libs/https/luajit-curl.lua:29` does
`ffi.load`, and `ffi.cast` for C→Lua callbacks works
(`libs/https/smods-https.lua:140`). `os.execute` also works
(`src/index.lua:23`).

So loading `sqlite3.dll` via FFI is *technically* viable. The cost is shipping
and resolving a native library per platform.

## JSON is rxi json.lua — correct but slow

Global `JSON` (`src/preflight/core.lua:47`), `libs/json/json.lua` v0.1.2. Plain
Lua recursive descent with per-token `str:find`/`str:sub`, allocating heavily —
roughly an order of magnitude slower than a C parser. **Decode off the main
frame for multi-MB workloads.**

Semantics to design around: errors on sparse/mixed tables and on NaN/inf; empty
tables encode as `{}` not `[]`; `null` decodes to a sentinel, not `nil`.

No msgpack, no luasocket, no bundled libcurl binary.

## Spreading work across frames

The idiom, from `src/ui.lua:2041-2052`:

```lua
G.E_MANAGER:add_event(Event({
    blocking = false, blockable = false,
    func = function()
        if <not done yet> then return end   -- returning nil retries next frame
        return true                         -- true = finished
    end,
}))
```

Returning nothing re-runs the event next frame — that is the primitive for
parsing large logs without freezing the game. `timer = "REAL"` ticks on
wall-clock so it keeps running while paused in a menu (`src/ui.lua:3130`).

**Named queues** allow batch cancellation: create with
`G.E_MANAGER.queues.<name> = {}`, enqueue via `add_event(evt, '<name>')`, cancel
with `G.E_MANAGER:clear_queue('<name>')` (`src/utils/run_select.lua:34, 634,
598`). Essential for tearing down a screen mid-load.

`love.thread` is also available — `SMODS.https.asyncRequest` spawns one per
request (`libs/https/smods-https.lua:264-280`).

## Reaching the screen from the menu

Three options, cheapest first:

**1. `SMODS.current_mod.extra_tabs`** — zero patching, appears under Mods →
BalatroDB. Handled at `src/ui.lua:583-599`:

```lua
SMODS.current_mod.extra_tabs = function()
    return {{ label = 'Runs', tab_definition_function = function() return <UIBox def> end }}
end
```

**2. Wrap `create_UIBox_main_menu_buttons`** for a real title-screen button —
this is exactly how Steamodded adds its own (`src/ui.lua:2025-2054`):

```lua
local ref = create_UIBox_main_menu_buttons
function create_UIBox_main_menu_buttons()
    local menu = ref()
    table.insert(menu.nodes[1].nodes[1].nodes, UIBox_button{
        id = "bdb_button", button = "bdb_button", label = {'Runs'},
        minh = 1.55, minw = 1.85, col = true, colour = G.C.BOOSTER, scale = 0.45*1.2,
    })
    return menu
end
G.FUNCS.bdb_button = function(e)
    G.SETTINGS.paused = true
    G.FUNCS.overlay_menu({ definition = <UIBox def> })
end
```

The node path `menu.nodes[1].nodes[1].nodes` is load-bearing and undocumented.
Note SMODS overwrites `menu.nodes[1].nodes[1].config` at `:2040`.

**3. A Lovely patch** for the in-run options menu (`lovely/menu.toml:6-37`).

There is no public "add a main menu button" API — wrapping is the sanctioned
approach.

## UI helpers worth using

| Helper | Where | Notes |
|---|---|---|
| `SMODS.UIScrollBox(args)` | `src/ui.lua:211-299` | scrolling container; pass into `{n=G.UIT.O, config={object=…}}` |
| `SMODS.GUI.scrollbar(args)` | `src/ui.lua:2834` | returns a UI node |
| `SMODS.GUI.dropdown_select(args)` | `src/ui.lua:3021` | **the filter widget** |
| `SMODS.GUI.createOptionSelector(args)` | `src/ui.lua:2078` | like `create_option_cycle` but matches by value; public pager |
| `SMODS.GUI.DynamicUIManager.initTab / updateDynamicAreas` | `src/ui.lua:2196, 2210` | static shell + swappable dynamic slot |
| `SMODS.GUI.staticModListContent()` | `src/ui.lua:2244-2342` | **canonical example**: static shell + dynamic slot + pager |
| `SMODS.card_collection_UIBox(pool, rows, args)` | `src/ui.lua:2426` | grid of cards with a working page cycler |
| `SMODS.smart_line_splitter(phrase, len)` | `src/ui.lua:333` | text wrapping |

Vanilla, used throughout: `create_tabs{...}`, `create_UIBox_generic_options{
back_func, contents, ... }`, `create_text_input{ id, w, max_length, ref_table,
ref_value, prompt_text, ... }` (SMODS patches it to allow several per screen —
`lovely/ui_elements.toml:80-125`).

`cycler(args)` in `src/utils/run_select.lua:441` is **file-local**, not
callable. Use `createOptionSelector` instead.

## The paging template — Steamodded's own run-select

`src/utils/run_select.lua` is the best in-tree example of a large custom
multi-page screen. The structural idea (`:32-66`):

- One `{n=G.UIT.O, config={id='run_select', object=UIBox{...}}}` holding the page
  body, plus a **static** nav bar sibling.
- Paging destroys and rebuilds only that object (`:361-374`):

```lua
local slot = ui.UIBox:get_UIE_by_ID('run_select')
slot.config.object:remove()
slot.config.object = UIBox{ definition = <new page>, config = {offset={x=0,y=0}, parent=slot, type='cm'} }
slot.UIBox:recalculate()
```

- Cross-page state lives in one flat table (`SMODS.RunSelect.Setup.choices`)
  that survives rebuilds.
- A per-frame node `func` decides button enablement/labels
  (`G.FUNCS.run_select_can_change_page`, `:243-266`).
- Within-page item paging is a second, inner pager (`:420-439`, `:497-519`),
  clamping the grid to the pool size.

**Teardown matters**: CardAreas are not garbage collected. `clean_up()`
(`:721-746`) clears the named event queue and `remove_all()`s every CardArea,
and is hooked into `G.FUNCS.exit_overlay_menu` (`:748-753`). It also manually
splices stale areas out of `G.I.CARDAREA` (`:376-389`).

Card clicks are dispatched by stamping tags onto `card.params` and hooking
`Card:click` (`:908-930`).

## `SMODS.https`

`require("SMODS.https")`. Two backends with a silent stub fallback
(`libs/https/smods-https.lua:44-64, 204-208`): LÖVE's built-in `https` module
(per the code's own comment, *"usually only accessible on windows"*), else
libcurl via FFI, else every request returns `0, "Failed to load a suitable
backend!"`.

- `https.request(url, opts)` — **synchronous, blocks the game thread.**
- `https.asyncRequest(url, opts, cb)` — spawns a `love.thread` per request,
  callback dispatched from a `love.update` hook. One OS thread per in-flight
  request.

Plain `http://localhost:PORT` works on the curl backend — the authors' own smoke
test does exactly that (`libs/https/smods-https.lua:282-285`). Whether LÖVE's
Windows `https` module accepts plain `http://` is **unverified** — test, don't
assume. Platform coverage is not guaranteed: on Linux/macOS it depends on
`ffi.load("libcurl")` resolving.
