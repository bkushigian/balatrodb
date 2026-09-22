# Balatro UI API reference (vanilla)

Verified against `Mods/lovely/dump/` (the Steamodded-patched source that
actually runs). Paths below are relative to that directory. Companion to
[`steamodded-ui-reference.md`](steamodded-ui-reference.md), which covers what
Steamodded adds on top.

## The headline: there is no scrolling

Base Balatro has **no scrolling container at all**. Confirmed by absence:

- `G.FUNCS.scroll_up` / `scroll_down` do not exist anywhere.
- `UIBox:init` (`engine/ui.lua:18-99`) reads no scroll-related config key.
- `G.UIT.S` (slider) and `G.UIT.I` (input) are declared at `globals.lua:498-499`
  but **never used** — `create_slider` and `create_text_input` are built from
  plain `C`/`B`/`T` nodes.
- `focus_args.type == "scrollbar"` is referenced by the controller
  (`engine/controller.lua:737, 1561`) calling `G.FUNCS.controller_scroll`, which
  **is not defined in the base game** — Steamodded supplies it.
- The cash-out screen hard-caps rows: `if total_cashout_rows > 7 then return end`
  (`functions/common_events.lua:1186`).

**So: either page, or depend on `SMODS.UIScrollBox`.** Every vanilla list is
fixed-size pages driven by `create_option_cycle`.

## Node vocabulary

`globals.lua:491-501`:

```lua
self.UIT = { T=1, B=2, C=3, R=4, O=5, ROOT=7, S=8, I=9, padding=0 }
```

Every node is `{n = <G.UIT.*>, config = {...}, nodes = {...}}`.

**`C` lays its children out left→right; `R` lays them top→bottom** — the
opposite of what the names suggest, and direction is decided by the *child's*
own `n`, not the parent's (`engine/ui.lua:191-206`).

`T`, `B`, `O` are **leaves** — `set_parent_child` only recurses into `C`, `R`,
`ROOT` (`engine/ui.lua:274`), so a `nodes` table on a leaf is silently ignored.

`O` wraps any Moveable — a nested `UIBox`, `CardArea`, `Card`, `Sprite`,
`DynaText`, or a bare `Moveable()` used as a swappable placeholder.

### config keys that matter

| key | notes |
|---|---|
| `align` | 2 chars from `{t,c,b}`×`{l,m,r}`, e.g. `"cm"`, `"cl"`, `"tm"` (`engine/ui.lua:640-648`) |
| `minw`/`minh` | size floor, in game units |
| `maxw`/`maxh` | **not a clamp — a scale-down.** Overflow triggers a second pass multiplying descendants' `config.scale` by `restriction/content` (`engine/ui.lua:167-174`). This is how long labels shrink to fit. |
| `r` | corner radius; `0.1` is the convention |
| `emboss` | drop-shadow lip, and **adds to the parent's computed height** (`engine/ui.lua:195-205`) |
| `colour` | defaults: ROOT→`BACKGROUND_DARK`, T→`TEXT_LIGHT`, O→`WHITE`, **B/C/R→`CLEAR`** (`engine/ui.lua:433-440`) |
| `button` | names a `G.FUNCS` entry; propagates `button_UIE` to descendants so children are clickable (`engine/ui.lua:266, 1081`) |
| `func` | names a `G.FUNCS` entry called **every frame** (`engine/ui.lua:1051-1054`) |
| `id` | key for `UIBox:get_UIE_by_ID(id, node)` (`engine/ui.lua:101-116`) |
| `outline_colour` | **does nothing without `outline` also set** (`engine/ui.lua:858`) |
| `no_fill` | occupy space, draw no background |
| `draw_layer` | `1` floats above siblings |

Units are game tiles: `G.TILESIZE=20`, `G.TILESCALE=3.65` → **1 unit ≈ 73px**.
The play area `G.ROOM` is **20 × 11.5 units**.

## Opening a screen

`G.FUNCS.overlay_menu{ definition, config }` — `functions/button_callbacks.lua:1349`.

**Gotcha: `config` is rebuilt, not merged.** Only `align`, `offset`, `major` and
`no_esc` survive; everything else is discarded (`:1359-1365`). `offset` defaults
to `{x=0,y=10}`, which is what makes it slide up — pass `{x=0,y=0}` for an
instant appearance. The documented `pause` arg is **never read**; callers set
`G.SETTINGS.paused = true` themselves.

Canonical minimal call (`functions/button_callbacks.lua:1425`):

```lua
G.FUNCS.run_info = function(e)
  G.SETTINGS.paused = true
  G.FUNCS.overlay_menu{ definition = G.UIDEF.run_info() }
end
```

`create_UIBox_generic_options{ back_func, contents, ... }`
(`functions/UI_definitions.lua:6724`) is the standard chrome every screen uses.
`contents` is spliced in as a **node list**, so it must be an array.

Its ROOT is `minw = G.ROOM.T.w*5, minh = G.ROOM.T.h*5` = 100 × 57.5 units — that
is just an oversized translucent scrim so it still covers the viewport while
easing in. **Do not read it as usable space.**

**Practical content budget: ~12–16 units wide × 8–9 tall.** The widest real
content block in the game is the challenge description pane at `minw = 11.5`
(`UI_definitions.lua:5912`); tab bodies reserve `tab_h = 8`.

## Tabs

`create_tabs(args)` — `functions/UI_definitions.lua:2245`. Args actually read:
`tabs`, `colour`, `tab_alignment`, `scale`, `tab_w`, `tab_h`, `text_scale`,
`no_shoulders`, `snap_to_nav`, `no_loop`, `padding`.

Per tab: `{ label, chosen, tab_definition_function, tab_definition_function_args, func }`.

- The body lives in a single `{n=G.UIT.O, config={id='tab_contents', object=UIBox{...}}}`.
- `tab_h`/`tab_w` become `minh`/`minw` on its parent row — **this is how you
  reserve a fixed region so the box doesn't resize when switching tabs.**
- **Tabs are lazy**: only the `chosen` tab's function runs at build time, and
  `G.FUNCS.change_tab` (`button_callbacks.lua:1320`) re-runs it on *every*
  switch with no caching.
- **`create_tabs` errors if no tab sets `chosen = true`** (`:2302`).
- `args.opt_callback` is assigned then never used — dead code (`:2249`).
- `change_tab` hardcodes `G.OVERLAY_MENU` for infotip cleanup, so `create_tabs`
  outside an overlay nil-errors (`button_callbacks.lua:1322`).

Multiple args are passed as a table and unpacked by the callee — see
`G.UIDEF.usage_tabs` using `tab_definition_function_args = {'consumeable_usage', 'Tarot'}`
(`UI_definitions.lua:2619`, unpacked at `:2657`).

## Paging — the data-browser pattern

`create_option_cycle(args)` — `functions/UI_definitions.lua:2153`. Args:
`options`, `current_option`, `opt_callback`, `scale`, `ref_table`, `ref_value`,
`w`, `h`, `text_scale`, `info`, `no_pips`, `mid`, `label`, `id`,
`cycle_shoulders`, `focus_args`.

`opt_callback` is a **string naming a `G.FUNCS` entry**, and it receives **one
table**: `{from_val, to_val, from_key, to_key, cycle_config}`, where
`cycle_config.current_option` is the new 1-based index
(`button_callbacks.lua:581-589`).

**The pattern to copy for a run browser** — reserve a fixed region holding a
placeholder object, then swap it. Declaration
(`UI_definitions.lua:5901`):

```lua
{n=G.UIT.R, config={align="cm", padding=0.1, minh=7, minw=4.2}, nodes={
  {n=G.UIT.O, config={id='challenge_list', object=Moveable()}},
}},
```

Swap (`button_callbacks.lua:1659`):

```lua
local slot = G.OVERLAY_MENU:get_UIE_by_ID('challenge_list')
if slot.config.object then slot.config.object:remove() end
slot.config.object = UIBox{
  definition = G.UIDEF.challenge_list_page(args.cycle_config.current_option-1),
  config = {offset={x=0,y=0}, align='cm', parent=slot}
}
```

`G.UIDEF.challenge_list_page(_page)` (`UI_definitions.lua:5919`) is **the closest
thing in the codebase to a data table**: index column, clickable row button
carrying `id = k` so the callback identifies the row, and a status cell — all in
a `ROOT` whose `nodes` is the row list. Two details worth copying:
`focus_args = {snap_to = not snapped}` so the gamepad lands on row 1 only, and
`button = <cond> and 'name' or 'nil'` to disable a row (the *string* `'nil'`
renders a dead button, `UI_definitions.lua:6838`).

The collections screens use a different idiom — they never rebuild the tree,
they mutate `CardArea` contents in place
(`G.FUNCS.your_collection_joker_page`, `button_callbacks.lua:612`).

## Standard data-row chrome

Shared by high-score rows (`UI_definitions.lua:2960`), hand rows (`:3275`) and
progress rows (`:2884`):

```lua
{n=G.UIT.R, config={align="cm", padding=0.05, r=0.1,
                    colour=darken(G.C.JOKER_GREY, 0.1), emboss=0.05}, nodes={ ... }}
```

Columns align by giving matched `minw == maxw`. `{n=G.UIT.B, config={w=0.08, h=0.01}}`
is the idiomatic spacer.

## Buttons

`UIBox_button(args)` — `UI_definitions.lua:6792`. Args: `button`, `func`,
`colour`, `label` (**array**, one line each), `minw`, `maxw`, `minh`, `scale`,
`col` (outer node becomes `C`), `id`, `ref_table`, `count = {tally=, of=}`,
`choice`, `chosen`, `one_press`, `text_colour`, `focus_args`.

Callbacks are `function(e)` where `e` is the element carrying `config.button`.
Read `e.config.id`, `e.config.ref_table`; mutate `e.config.colour` /
`e.config.button` to enable and disable. Reach the tree via `e.UIBox` and
`:get_UIE_by_ID(...)`, then `:recalculate()`.

Enable/disable idiom (`button_callbacks.lua:2068`):

```lua
G.FUNCS.can_play = function(e)
  if <disallowed> then e.config.colour = G.C.UI.BACKGROUND_INACTIVE; e.config.button = nil
  else e.config.colour = G.C.BLUE; e.config.button = 'play_cards_from_highlighted' end
end
```

## Updating without rebuilding

**Cheapest — ref-bound text.** `{n=G.UIT.T, config={ref_table=t, ref_value='k'}}`
repaints whenever `t.k` changes (`engine/ui.lua:653-665`). `tostring()` is
automatic and it self-diffs. **It auto-calls `UIBox:recalculate()` whenever the
string *length* changes** — set `no_recalc = true` if you have reserved width.

**Animated — `DynaText` in an `O` node.** String entries can themselves be ref
bindings: `DynaText({string = {{ref_table=args, ref_value="current_option_val"}}})`.

**Custom — `func` running every frame.** Guard on a diff, mutate
`e.config.object`, call `:update_text()`, never touch the tree
(`button_callbacks.lua:1960`).

**Structural** — swap a sub-UIBox as above, or `UIBox:add_child(node, parent)`
(`engine/ui.lua:350`) where `parent` is a live element from `get_UIE_by_ID`.

## Text

`localize(key)` / `localize(key, category)` / `localize{type=, key=, set=, vars=}`
— `functions/misc_functions.lua:1859`. Returns the string `'ERROR'` on a miss,
never nil.

**`localize{type='descriptions'}` returns nothing** — it writes into
`args.nodes`, each element is itself a node *list* needing an `R` wrapper, and
the last entry is always dropped (`UI_definitions.lua:3458-3463`).

Scale conventions: `0.3` fine print, `0.4–0.45` body/table cells, **`0.5`
standard button label**, `0.55–0.7` headings.

**Force `lang = G.LANGUAGES['en-us']` on numeric `T` nodes in fixed-width
grids** (`UI_definitions.lua:627`) so CJK locales don't reflow numeric columns.

## Gotchas

1. `overlay_menu` discards all `config` keys except `align`, `offset`, `major`, `no_esc`.
2. `C` = horizontal children, `R` = vertical children.
3. `maxw`/`maxh` scale text down, they don't clip.
4. `outline_colour` needs `outline` set too.
5. `T`/`B`/`O` are leaves; their `nodes` are ignored.
6. `create_tabs` errors with no `chosen` tab, and re-runs the body function on every switch.
7. `opt_callback` is a string, and receives a single table.
8. Ref-bound text recalculates the whole UIBox on length change.
9. `UIBox_button{button = 'nil'}` — the *string* — renders a dead button.
10. No scrolling exists; page, or use `SMODS.UIScrollBox`.
