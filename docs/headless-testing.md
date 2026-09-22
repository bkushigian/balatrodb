# Headless & automated Balatro — design doc

**Nothing in this document has been executed against a running game.** The hard
constraint for this investigation was: do not launch, close or kill Balatro.
Everything below is either read out of source (cited `file:line`) or explicitly
flagged as an inference. §15 is the honest ledger; §16 is the experiment queue
that converts the inferences into facts.

Unless noted, citations are relative to
`%APPDATA%\Balatro\Mods\lovely\dump\` — the Lovely-patched source that
*actually runs* on this machine. That dump contains **only files Lovely
patched**, so `conf.lua`, `engine/node.lua` and the vanilla form of
`cardarea.lua` are absent from it; where I needed those I read them straight out
of the zip inside `Balatro.exe` and cite them as `exe:<path>:<line>`.

Environment facts established while writing this (all read-only):

| Thing | Value | How known |
|---|---|---|
| LÖVE version | **11.5 r2** | `love.dll` VERSIONINFO |
| SDL version | **2.28.5** | `SDL2.dll` VERSIONINFO |
| Game dir | `C:\Program Files (x86)\Steam\steamapps\common\Balatro` | filesystem |
| `Balatro.exe` | fused LÖVE exe, 304 zip entries, 47 `.lua` | `zipfile` listing |
| Steam DRM wrapper | **absent** — no `.bind` section, no `SteamStub` marker | PE section table |
| Steam appid | `2379780`, in `steam_appid.txt` | file |
| Lovely injector | `version.dll` (Rust), honours `LOVELY_MOD_DIR` | strings in `version.dll` |

---

## 1. Executive summary — the recommendation

**Build an in-process Lua driver mod. Do not build a socket server, and do not
try to run LÖVE windowless.**

In one paragraph: add a second Steamodded mod, `BalatroDBDriver`, that (a)
reads a *script* — an ordered list of actions — from disk at boot via
`SMODS.NFS`, (b) ticks once per frame from an after-hook on `Game:update`, (c)
waits for a **quiescence predicate** (§8) before each action, (d) executes each
action by locating the **real** UI element a human would click and firing its
`G.FUNCS` entry through Handy's fake-node technique (§7), (e) asserts an
action-specific **postcondition** before advancing, and (f) on completion
writes a result file and calls `love.event.quit(code)`. Speed comes from
replacing `love.run`'s throttle and skipping `love.draw`, not from removing the
window (§6). Determinism comes from an explicit seed plus patching three
unseeded `math.random` sites (§9). Golden comparison runs on a **normalized
projection** of the JSONL, not the raw bytes (§10).

Why this shape:

- **The bugs you are hunting are timing and routing bugs.** A driver that
  calls `G.FUNCS.play_cards_from_highlighted` through the same element the UI
  passes, on the same frame budget the UI would, exercises the same routing. A
  driver that calls gameplay functions directly does not, and would have missed
  every bug found so far.
- **BalatroDB's own hooks read the element.** `mod/BalatroDB/src/hooks.lua:571-572`
  reads `args[1].config.ref_table` for `select_blind`; `:605-607` reads
  `e.config.ref_table` for `buy_from_shop`; `:652-654` likewise for `use_card`.
  A driver that invents a bare fake node of the wrong shape silently produces a
  *different log* — which in a golden test reads as a regression in the mod.
  This is the strongest single argument for driving through real elements.
- **The game itself needs the element.** `G.FUNCS.select_blind` reads
  `e.UIBox:get_UIE_by_ID('tag_container')` to record
  `G.GAME.round_resets.blind_tag` (`functions/button_callbacks.lua:2590-2591`).
  A synthetic `{config = {ref_table = blind}}` has no `UIBox`, so the skip tag
  silently becomes `nil` and the run diverges from a human's.

### Rejected alternatives

| Alternative | Why rejected |
|---|---|
| **Socket / JSON-RPC server (BalatroBot shape)** | Adds a second process and an async boundary to a problem whose entire difficulty is *synchronisation*. Every flake source doubles. It is cheap to build — LÖVE preloads LuaSocket, so there is no native dependency (§14.2) — and it is the right shape for goal 1 with an external policy (§2). It is the wrong shape for goal 2. `coder/balatrobot` has four open "hangs indefinitely" issues that are exactly this failure mode (§14.2). |
| **Synthesize mouse/keyboard input** (`love.mousepressed`, `G.CONTROLLER:queue_L_cursor_press`) | Depends on resolution, `G.TILESCALE`, card layout, and on moveables having finished moving — maximally flaky, and precisely what Handy avoids. |
| **Call gameplay functions directly** (`new_round()`, `end_round()`, `Card:add_to_deck`) | Bypasses exactly the routing layer where the bugs live. Useful as a *setup* shortcut inside a fixture, never as the action under test. |
| **`t.window = false` headless LÖVE** | Breaks canvas creation and display enumeration (§4). Buys little: skipping `love.draw` already removes nearly all the GPU cost. |
| **Replay save files** | `Game:start_run` with `args.savetext` restores state wholesale and exercises `CardArea:load` and nothing else. Cannot reproduce a timing bug. |
| **Force `E_MANAGER` with `dt = 0`** | Structurally cannot work. `Event:handle` gates `after`/`ease`/`before` on `G.TIMERS[self.timer]` (`engine/event.lua:53, 61, 91`), and those timers only advance in `Game:update` from `dt` (`game.lua:2586, 2626`). Forcing the queue without advancing time spins forever on the first delayed event. §6 explains what `forced` *does* buy. |

---

## 2. Goal 1: agent-driven play (data collection)

Goal 1 is the easier half and falls out of the same driver.

- The action source is pluggable: a **script** (fixed list, for tests) or a
  **policy** (`policy(observation) -> action`, for play).
- `observation` is built from the same globals the driver already reads for
  synchronisation: `G.STATE`, `G.GAME`, `G.hand.cards`, `G.jokers.cards`,
  `G.shop_jokers.cards`, `G.blind_select_opts`. No serialization is needed
  unless the policy lives out of process.
- A Lua-local policy — even a crude heuristic — needs no IPC and is the fastest
  path to a large corpus. **Start there.**
- For an external policy (an LLM, a trained net), **a TCP socket is available
  with no native dependency**: LÖVE bundles LuaSocket and preloads it
  (`love/src/modules/love/love.cpp:655`), so `require "socket"` works inside
  Balatro as-is. `coder/balatrobot` builds a whole JSON-RPC-over-HTTP server on
  exactly that (§14.2) and it is only ~150 lines. This corrects an earlier
  draft of this section, which assumed sockets needed something extra and
  recommended a file-based bridge instead.
  A file-based bridge — write `obs/<n>.json` with `SMODS.NFS.write`, poll for
  `act/<n>.json` from the per-frame tick — is still the *simpler* option and
  remains a fine starting point; `SMODS.NFS` is unsandboxed FFI `fopen`
  (`steamodded-ui-reference.md`). Pick the socket when you want one process
  driving many instances, the files when you want to debug by `cat`.
  Either way, **for goal 2 keep the driver in-process** (§1).
- **Unattended runs must pass an explicit seed.** With no seed,
  `Game:start_run` calls `generate_starting_seed()` (`game.lua:2229`), which
  derives the seed from `G.CONTROLLER.cursor_hover.T.x/.y` and `.time`
  (`functions/misc_functions.lua:239, 246`), where `.time` is `G.TIMERS.TOTAL`
  (`engine/controller.lua:1063`). With no mouse moving, `T.x`/`T.y` are
  constant and the only entropy is the clock — probably enough to avoid
  collisions, but correlated and unauditable. Generate seeds yourself and pass
  `args.seed`; you get reproducibility for free, and BalatroDB already records
  `seeded: true` (`docs/event-schema.md`).
- Feed the driver a **seed list** and loop: `start_run` → play → `run.end` →
  next seed, never returning to the menu. `G.FUNCS.start_run(nil, args)`
  (`functions/button_callbacks.lua:3025`) already does `clear_queue` →
  `delete_run` → `start_run`, which is exactly the loop body.
- Set `G.F_NO_ACHIEVEMENTS = true` (declared `globals.lua:31`); seeded runs
  already short-circuit `inc_career_stat` (`functions/misc_functions.lua:1563`)
  so the profile is not polluted by bot play.
- `G.F_NO_SAVING = true` (`globals.lua:18`) removes the per-round `save_run`
  write — `Game:update_selecting_hand` calls `save_run()` on every entry
  (`game.lua:3261`). Worth measuring; it is one of the few unavoidable
  synchronous disk writes per round. **But** BalatroDB's resume identity is
  parked on `G.GAME` and round-trips through `save_run`
  (`mod/BalatroDB/src/state.lua`, `persist`/`restore`), so with saving off,
  resume is untestable. Use it for bulk collection, not for the regression
  suite.

---

## 3. Goal 2: deterministic regression tests (the real prize)

The test is: *fixed seed + fixed deck/stake + scripted action list ⇒ equal
normalized JSONL.*

Four things have to be true, in descending order of how likely they are to
bite:

1. **Every action needs a callable funnel** (§7). One does not have one: joker
   reordering. There is a workaround (§7.6).
2. **The driver has to know when to act** (§8). This is the entire flakiness
   budget. Get it wrong and the suite fails at a few percent, which is worse
   than having no suite.
3. **The run has to be reproducible** (§9). Three unseeded `math.random` sites
   plus a process-global card counter.
4. **The assertion has to be about behaviour, not noise** (§10).

A passing golden test proves a lot: it proves the *order* of emitted events,
which is the routing property, and the *relative timing* between them, which is
the timing property. That is exactly the class of bug found so far.

---

## 4. Headless proper — can LÖVE run Balatro with no window?

Short answer: **not without patching the game, and it is not worth it.**

### 4.1 Balatro has a `conf.lua`, and it does not disable the window

`exe:conf.lua` is eight lines, in full:

```lua
_RELEASE_MODE = true
_DEMO = false

function love.conf(t)
	t.console = not _RELEASE_MODE
	t.title = 'Balatro'
	t.window.width = 0
	t.window.height = 0
	t.window.minwidth = 100
	t.window.minheight = 100
end
```

So: no `t.window = false`, no `t.modules.*` disabled, and `t.console = false`
in release. `width = 0, height = 0` asks LÖVE for desktop dimensions.

A Lovely patch could insert `t.window = false` (Lovely can patch `conf.lua`
like any other chunk). The question is what then breaks.

### 4.2 `t.window` is not the headless knob — `t.modules.*` is

This was corrected by a source pass over LÖVE 11.5 and SDL2 (read from the
upstream repositories, not executed here — see §15):

- **`t.window = false` still initializes the SDL video subsystem.** `love.window`
  is still `require`d, so its constructor runs and calls
  `SDL_InitSubSystem(SDL_INIT_VIDEO)`. It means "no window created", not "no
  display touched".
- **The genuinely headless configuration is `t.modules.window = false` *and*
  `t.modules.graphics = false`** — different fields from `t.window`. Conflating
  the two is the commonest piece of bad forum advice on this topic. Real LÖVE
  CI has been done this way with no Xvfb and no software GL, at the cost that
  no game code may touch `love.graphics` outside `love.draw`.
- **Balatro cannot use that configuration.** `Game:start_up` compiles every
  shader in `resources/shaders` through `love.graphics.newShader` during
  `love.load` (`game.lua:128-138`). With `love.graphics` absent, that is an
  immediate nil-index; with the module present but no window, see below.

### 4.2b What breaks under `t.window = false` in Balatro specifically

LÖVE 11.5 has an explicit guard helper, `luax_checkgraphicscreated`
(`love/src/modules/graphics/wrap_Graphics.cpp:60`), which raises
`"love.graphics cannot function without a window!"`. It is called from 14
constructors — `newImage`, `newCanvas`, `newFont`, `newQuad`, `newMesh`,
`newText`, `newVideo`, the array/cube/volume image variants, `newSpriteBatch`,
`newParticleSystem`, `newImageFont`.

**`newShader` is not one of them.** `w_newShader` goes straight to
`instance()->newShader(...)`, which compiles GLSL through GL entry points that
were never loaded. Draw calls are likewise unguarded — in normal operation they
are protected only by the `isActive()` test in `love.run` (`main.lua:988`).

So Balatro's boot fails in two different ways:

| Site | Failure mode |
|---|---|
| `love.graphics.newShader` (`game.lua:136`), reached from `Game:start_up` | **unguarded** — GL call with no context; undefined behaviour rather than a clean Lua error |
| `love.graphics.newCanvas(w*scale, h*scale, ...)` (`main.lua:1311`), reached from `love.resize` at `game.lua:1305` | clean Lua error from the guard; and `w`/`h` would be `0` anyway since `love.graphics.getWidth/Height` report no window |
| `love.graphics.setNewFont` in `boot_timer` (`main.lua:1345`) | clean Lua error from the guard |
| `love.window.getFullscreenModes(i)[1]` then `.width` (`functions/misc_functions.lua:32, 36`) | nil index if the list is empty |

One more trap: with `t.window = false`, an uncaught error makes LÖVE's default
error handler try to *create* a window —
`if not love.graphics.isCreated() or not love.window.isOpen() then pcall(love.window.setMode, 800, 600)`.
Balatro carries two copies of that pattern (`main.lua:647-652` and
`:1178-1182`), both inside its patched/vanilla `love.errorhandler`. So a
"headless" crash can pop a window on your desktop. The `pcall` saves you only
if `setMode` itself fails.

### 4.3 What you get for free instead

`love.run` already gates drawing:

```lua
if love.graphics and love.graphics.isActive() then
    if love.draw then love.draw() end
    love.graphics.present()
end
```
— `main.lua:988-991`.

`love.graphics.isActive()` is false when the window is closed or minimized. So
a **minimized** window already skips `love.draw` and `present` with no patch at
all. And a driver can simply stub `love.draw = function() end`, which costs one
line and removes the entire render path — including `Game:draw`'s shader passes
(`game.lua:3120-3130`) — while leaving the context alive so canvases and
shaders still construct normally.

**Recommendation: keep the window. Stub `love.draw`. Optionally minimize.**
If a genuinely invisible window is wanted, the low-risk lever is
`love.window.setPosition` far off-screen, or `t.window.borderless` plus
`minwidth/minheight` (100×100 per `conf.lua`) — a tiny window, not no window.

### 4.4 SDL video drivers: `dummy` no, `offscreen` maybe

- **`SDL_AUDIODRIVER=dummy`** is low-risk and worth trying: it removes OpenAL
  device init. Balatro runs sound on its own thread when `G.F_SOUND_THREAD`
  (`globals.lua:20`, default true), so the saving is modest, but it removes a
  class of CI failure (no audio device in a container).
- **`SDL_VIDEODRIVER=dummy` is a dead end.** It provides a video subsystem with
  no GL context at all. LÖVE 11.5 wants desktop GL 2.1/3.3; there is nothing to
  attach a context to.
- **`SDL_VIDEODRIVER=offscreen` is the interesting one**, and it is the one
  genuinely open question in this section. SDL2 ships a second headless driver
  that *does* wire up GL — `src/video/offscreen/SDL_offscreenvideo.c` assigns
  `GL_CreateContext`/`GL_MakeCurrent`/`GL_SwapWindow` under
  `#ifdef SDL_VIDEO_OPENGL_EGL`, and in `SDL_video.c`'s bootstrap array
  `OFFSCREEN_bootstrap` sits immediately *before* `DUMMY_bootstrap`, so a
  headless Linux box that fails x11/wayland picks `offscreen` when it is
  compiled in.

  Two catches, both unresolved:
  1. **Windows availability.** SDL2's hand-written
     `include/SDL_config_windows.h` defines only `SDL_VIDEO_DRIVER_DUMMY` and
     `SDL_VIDEO_DRIVER_WINDOWS` — **no OFFSCREEN**. (SDL3's equivalent header
     does define it, but LÖVE 11.5 uses SDL2.) Whether the SDL2 binary shipped
     with Balatro has offscreen depends on whether it was configured via CMake
     — where `SDL_OFFSCREEN` defaults ON — or via that static header. **Not
     resolvable from source; it needs a run.**
  2. **Offscreen's GL is EGL-only**, while LÖVE requests desktop GL. The lever
     is an undocumented, source-only SDL hint read at
     `love/src/modules/window/sdl/Window.cpp:239`:
     `SDL_GetHint("LOVE_GRAPHICS_USE_OPENGLES")`. SDL hints fall back to
     environment variables, so `LOVE_GRAPHICS_USE_OPENGLES=1` works from a
     shell. Siblings: `LOVE_GRAPHICS_USE_GL2`, `LOVE_GRAPHICS_DEBUG`. You would
     still need `libEGL.dll`/`libGLESv2.dll` (ANGLE or Mesa) on `PATH`.

  **Worth exactly one fifteen-minute experiment (§16-E4b)** before committing to
  the Xvfb + llvmpipe path on Linux. It does not unblock anything on Windows.
- On Linux the established answer for LÖVE in CI remains **Xvfb plus software
  GL** (`xvfb-run`, `LIBGL_ALWAYS_SOFTWARE=1`, llvmpipe), not the dummy driver.
- **Confidence:** the SDL/LÖVE source citations above come from a research pass
  over the upstream repositories. I did not read those files myself and nothing
  was executed. Treat them as well-sourced but second-hand.

### 4.5 The honest conclusion

**Headless-proper is closed off for Balatro by `Game:start_up` compiling
shaders in `love.load`**, and it gates nothing anyway. What actually gates CI
is §5 (Steam) and §6 (speed). Keep the window, stub `love.draw`, spend the
effort elsewhere. The `offscreen` + EGL idea (§4.4) is the only remaining
thread and it is worth a quarter of an hour, not a project.

---

## 5. Steam dependency — does `Balatro.exe` launch without Steam?

**Strong evidence: yes.** Three independent signals, none of them a launch:

1. **The executable is not DRM-wrapped.** Its PE section table is
   `.text .rdata .data .pdata .rsrc .reloc` — six sections, no `.bind`. Steam
   DRM wrapping adds a `.bind` section and a `SteamStub` marker; neither is
   present, and the byte string `SteamStub` occurs zero times in the file. So
   there is no pre-main check that phones the Steam client.
2. **Steam is optional in Lua, by construction.** `love.load`
   (`main.lua:999`) does:

   ```lua
   local success, _st = pcall(require, 'luasteam')
   if success then st = _st else sendWarnMessage(_st, "LuaSteam"); st = {} end
   ...
   if not (st.init and st:init()) then st = nil end
   ...
   G.STEAM = st
   ```
   — `main.lua:1017-1018` (Windows branch; the Linux branch is `:1013-1014`)
   and `main.lua:1026-1028`. Both the `require` and the init are
   guarded; failure leaves `G.STEAM = nil`. Every later use is guarded too
   (e.g. `game.lua:2800`).
3. **`steam_appid.txt` is present** and contains `2379780`. Its purpose is to
   let `SteamAPI_RestartAppIfNecessary` short-circuit so the binary does not
   relaunch itself through the Steam client.

The residual unknown is whether `luasteam.dll` → `steam_api64.dll` does
anything fatal at *load* time (before `st:init()`) when no Steam client is
running. Historically `SteamAPI_Init` just returns false. I have not verified
it.

### The experiment (do not run now — see §16-E1)

```powershell
# Steam must NOT be running. Check first:
Get-Process steam -ErrorAction SilentlyContinue

# Launch detached, with a separate profile dir so nothing of yours is touched.
$env:LOVELY_MOD_DIR = "C:\Users\bkush\Code\BalatroDB\ci\Mods"
Start-Process -FilePath "C:\Program Files (x86)\Steam\steamapps\common\Balatro\Balatro.exe" `
  -WorkingDirectory "C:\Program Files (x86)\Steam\steamapps\common\Balatro" `
  -PassThru
```

**Reading the result:**

| Observation | Meaning |
|---|---|
| Window appears, main menu loads | Steam is not required. CI is unblocked. |
| Window appears, Lovely log shows `LuaSteam` warning, menu loads | Same — the warning is the expected `pcall` path. |
| Process exits immediately, no window | Steam *is* required, or the Lovely injector failed. Distinguish by checking for a new file in `%APPDATA%\Balatro\Mods\lovely\log\`. |
| Steam client launches itself | `RestartAppIfNecessary` fired; `steam_appid.txt` is not being found — check the working directory. |

**Also verify** that `G.STEAM == nil` does not break achievement/stat paths the
mod touches. `inc_steam_stat` is called from `G.FUNCS.select_blind`
(`functions/button_callbacks.lua:2587`) but only under `if _DEMO`, which is
false (`exe:conf.lua`). Grep for other `G.STEAM` uses before trusting this.

---

## 6. Time control — how fast can this go?

This is the section with the most concrete leverage, and the most
counter-intuitive result.

### 6.1 The four throttles, in the order they bind

**(a) The frame-rate cap and sleep.** `love.run`'s loop ends with:

```lua
run_time = math.min(love.timer.getTime() - run_time, 0.1)
G.FPS_CAP = G.FPS_CAP or 500
if run_time < 1./G.FPS_CAP then love.timer.sleep(1./G.FPS_CAP - run_time) end
```
— `main.lua:993-995`. `G.FPS_CAP` is *only ever* set here, so it is `500` and
nothing else assigns it. **Setting `G.FPS_CAP` to a huge number from a mod
removes the sleep entirely, with no patch.** That is a free lever.

**(b) `dt` smoothing and clamping.** `main.lua:983-984`:

```lua
if love.timer then dt = love.timer.step() end
dt_smooth = math.min(0.8*dt_smooth + 0.2*dt, 0.1)
if love.update then love.update(dt_smooth) end
```

So **`dt` reaching the game is clamped to 0.1 s and low-pass filtered** with a
0.2 coefficient. Feeding a "large synthetic dt" through `love.timer` does
nothing: 0.1 s is the hard ceiling per frame, and it takes ~5 frames of large
dt to get there from a small one. This is the fact that kills the naive
"advance time in one jump" plan.

You *can* bypass the smoothing by wrapping `love.update` and passing your own
constant — `coder/balatrobot` v1.5.2 does exactly that with `4.99/60` ≈ 0.083,
and `besteon`'s config warns that anything above `8/60` ≈ 0.133 (i.e. above the
clamp) *"seems to cause instability"* (§14.2). That is the only empirical
datum anywhere on Balatro's `dt` tolerance. Note that `coder`'s `dev` branch
**removed** the fixed-dt patch and now relies on `GAMESPEED` alone — treat that
as a considered retreat, and treat the dt hack as a goal-1 lever, not a
golden-test one.

**(c) `SPEEDFACTOR` scales the `TOTAL` timer only.** `Game:update`
(`game.lua:2569`) does:

```lua
self.TIMERS.REAL  = self.TIMERS.REAL + dt          -- :2586
self.real_dt      = dt                             -- :2591
self.SPEEDFACTOR  = (... ) and self.SETTINGS.GAMESPEED or 1   -- :2617
self.TIMERS.TOTAL = self.TIMERS.TOTAL + dt*self.SPEEDFACTOR   -- :2626
self.E_MANAGER:update(self.real_dt)                -- :2644
```

Note carefully: **`E_MANAGER:update` gets `real_dt`, not scaled dt**
(`game.lua:2644`). `GAMESPEED` does not make the event manager tick more often;
it makes `G.TIMERS.TOTAL` run faster, and since most events default to
`timer = 'TOTAL'` (`engine/event.lua:23`), their `delay`s elapse sooner. That
is the real mechanism. Events created while `G.SETTINGS.paused` default to
`timer = 'REAL'` and are **immune to `GAMESPEED`** — a trap.

`GAMESPEED` is a settings field (`globals.lua:173`, default 1) and the UI only
offers `{0.5, 1, 2, 4}` (`functions/UI_definitions.lua:2521`), but nothing
clamps it. A mod can set `G.SETTINGS.GAMESPEED = 64` directly. Note also
`G.ACC` (`game.lua:2612, 2619`), which already adds up to 14 to `SPEEDFACTOR`
during `HAND_PLAYED`/`NEW_ROUND` — the game's own hold-to-skip.

**(d) Moveable easing is frame-bound and *not* speed-scaled.**
`game.lua:2765`:

```lua
local move_dt = math.min(1/20, self.real_dt)
...
for k, v in pairs(self.MOVEABLES) do
    if v.FRAME.MOVE < G.FRAMES.MOVE then v:move(move_dt) end
end
```

`move_dt` is capped at **50 ms per frame regardless of `dt`**, and `GAMESPEED`
does not touch it. `G.exp_times.xy/.scale/.r` (`game.lua:2761-2763`) are
`exp(-k*real_dt)` — also unscaled.

This matters because **the game gates real logic on moveable positions.**
`Game:update_shop` populates the shop only inside an event that waits for
`math.abs(G.shop.T.y - G.shop.VT.y) < 3` (`game.lua:3285`). So "how fast can the
shop open" is a question about how many frames of ≤50 ms of exponential easing
it takes, and no amount of `GAMESPEED` shortens it.

### 6.2 So what is the bound?

**Frame-bound, with a floor set by moveable easing.** Concretely:

- Upper bound on *simulated* time per frame: 0.1 s of `REAL`,
  `0.1 × GAMESPEED` of `TOTAL`, 0.05 s of moveable motion.
- With the sleep removed and `love.draw` stubbed, the wall-clock cost of a
  frame is one `Game:update`: `nuGC`, sound modulation, the `E_MANAGER` pass,
  and a `move`+`update` over every `G.MOVEABLES`. On a desktop CPU that is
  plausibly 0.2–1 ms with drawing off — call it **1000–5000 frames/s**, which
  at 50 ms of simulated motion per frame is **50–250× real time for
  animation-bound segments**, and more for delay-bound ones once `GAMESPEED`
  is raised.
- **I have not measured any of this.** §16-E5 is the measurement.

A human ante-8 run is roughly 15–25 minutes. If the above holds, a run lands in
the **5–20 second** range, i.e. **3–12 runs/minute per process**. Runs scale
linearly across processes (each is a separate `Balatro.exe`), so the practical
throughput knob is process count, not in-process speed.

### 6.3 What `EventManager:update(dt, forced)` actually buys

`engine/event.lua:171-198`:

```lua
function EventManager:update(dt, forced)
    self.queue_timer = self.queue_timer + dt
    if self.queue_timer >= self.queue_last_processed + self.queue_dt or forced then
        self.queue_last_processed = self.queue_last_processed + (forced and 0 or self.queue_dt)
        ...one pass over every queue...
```

with `self.queue_dt = 1/60` (`:115`). So:

- `forced = true` **bypasses the 1/60 s rate limit** and does not advance
  `queue_last_processed`. That is all it does. It runs **one** pass.
- It does **not** advance `G.TIMERS`, so an `after` event with `delay = 0.5`
  will be re-examined and re-declined on every forced pass
  (`engine/event.lua:52-56`).
- Therefore the useful pattern is: advance the timers *and* force the queue, in
  a loop. `Handy` does exactly this — its `EventManager:update` override splits
  `real_dt` into `retriggers + 1` slices and calls the original with
  `forced = 1` per slice, bailing out early when fewer than 3 events remain
  (`HandyBalatro-2.0.5/src/controls/speed_multiplier/hooks.lua:34-86`). It also
  carefully saves/restores `G.TIMERS` per sub-step (`:39-55, 66-68`) so per-frame
  timer deltas are not applied N times. That file is the reference
  implementation; read it before writing your own.

### 6.4 The lever list, cheapest first

| Lever | How | Risk |
|---|---|---|
| Remove the FPS sleep | `G.FPS_CAP = 1e9` | none — it is only read at `main.lua:994`. **Do not set it to `nil`**: `main.lua:994` is `G.FPS_CAP = G.FPS_CAP or 500`, so `nil` silently means a 500 cap. `coder/balatrobot` has this exact bug (§14.2) |
| Skip rendering | `love.graphics.isActive = function() return false end` | none for logic. Better than stubbing `love.draw`, because Balatro's own loop then skips `love.draw` **and** `love.graphics.present` (`main.lua:988-991`) rather than calling a stub. Gate behind a flag so you can watch a run |
| Kill the tween layer | override `Moveable.move_xy` to snap `VT` to `T` (`besteon/balatrobot` does this) | **high — goal 1 only.** It removes §6.1(d)'s floor entirely, but it also changes *when* position-gated logic fires (`game.lua:3285`), so it cannot be used for golden tests |
| Raise game speed | `G.SETTINGS.GAMESPEED = 8` (or 64) | **changes `delay` values baked into events** — see below |
| Force extra event passes | Handy-style sliced `EventManager:update(dt, true)` | medium; re-entrancy and timer bookkeeping |
| Mute | `G.F_MUTE = true` (`globals.lua:19`) | none |
| Disable saving | `G.F_NO_SAVING = true` | breaks resume testing |

**`GAMESPEED` is not log-neutral.** Several call sites bake `GAMESPEED` into
the *delay value itself*: `delay = G.SETTINGS.GAMESPEED*0.05` (`blind.lua:260`),
`delay = 0.06*G.SETTINGS.GAMESPEED` (`card.lua:1835`,
`functions/common_events.lua:584`, `functions/misc_functions.lua:995`,
`functions/state_events.lua:509, 836`), and
`1.3*math.sqrt(G.SETTINGS.GAMESPEED)` (`card.lua:2062, 2127`). Raising
`GAMESPEED` *lengthens* those delays in `TOTAL` time while shortening others,
so the relative ordering of events can shift. **For golden tests, pin
`GAMESPEED` to a single value and record it in the fixture.** Do not treat it
as a free speed knob.

### 6.5 Two footguns

- `Game:update` prints on every long frame:
  `if self.real_dt > 0.05 then print('LONG DT @ ...') end` (`game.lua:2593`).
  Driving at the 0.1 s clamp means a `print` per frame. On Windows with
  `t.console = false` (`exe:conf.lua`) this is cheap but not free, and it
  pollutes any stdout you want to parse. Stub `print` in the driver, or keep
  `dt ≤ 0.05`.
- `G.fbf` / `G.new_frame` (`game.lua:2594-2595`) is a vestigial frame-by-frame
  gate: with `G.fbf` truthy, the whole body of `Game:update` — including
  `E_MANAGER:update` — is skipped unless `G.new_frame` was set. Nothing else in
  the dump sets `G.fbf`. It is a *pause* primitive, not a step primitive, since
  it also freezes the event manager; useful for a debugger, not for a driver.

---

## 7. Driving actions — the callable funnel

### 7.1 The technique

Handy's `fake_events` (`HandyBalatro-2.0.5/src/core/fake_events.lua`) is 79
lines and is the whole trick:

```lua
Handy.fake_events.check{ func = G.FUNCS.can_play, node = <real UIE> }
-- runs the predicate against the real element, returns (is_enabled, button_name)

Handy.fake_events.execute_button(function() return G.buttons:get_UIE_by_ID("play_button") end)
-- resolves the element, then calls element:click() or G.FUNCS[element.config.button](element)
```

Two things make it correct rather than a hack:

1. **The `can_*` predicates are pure legality oracles.** They take an element,
   set `e.config.colour` and `e.config.button`, and set `button = nil` when the
   action is illegal — `can_play` (`functions/button_callbacks.lua:2068`),
   `can_discard` (`:2112`), `can_reroll` (`:2096`), `can_buy` (`:55`),
   `can_buy_and_use` (`:77`), `can_redeem` (`:96`), `can_open` (`:111`),
   `can_use_consumeable` (`:2122`), `can_select_card` (`:2132`),
   `can_sell_card` (`:2144`), `can_skip_booster` (`:2154`). Running the
   predicate first and refusing to act when it says no is how a driver gets a
   *loud* failure instead of a silent no-op.
2. **It passes the real element**, so `e.UIBox`, `e.config.ref_table` and
   `e.config.id` are all what the game and BalatroDB's hooks expect.

`fake_events.execute_button` prefers `button:click()` when the element defines
one (`fake_events.lua:68-70`) — worth keeping, because `Card:click`
(`exe:card.lua:4610`) is what highlights/selects cards.

### 7.2 Selector table — the complete funnel for one run

These are Handy's own selectors, verified in
`HandyBalatro-2.0.5/src/definitions/controls/regular_keybinds/`:

| Action | Element / entry point | Source |
|---|---|---|
| Select blind | `G.blind_select_opts[string.lower(G.GAME.blind_on_deck)]:get_UIE_by_ID("select_blind_button")` | `blind_select.lua:62, 67` |
| Skip blind | `G.blind_select_opts[...]:get_UIE_by_ID("tag_"..G.GAME.blind_on_deck).children[2]` | `blind_select.lua:25-28, 33` |
| Reroll boss | `G.blind_prompt_box.UIRoot.children[3].children[1]` (guard `require_exact_func = "reroll_boss_button"`) | `blind_select.lua:96, 104` |
| Play hand | `G.buttons:get_UIE_by_ID("play_button")` | `hand.lua:22, 28` |
| Discard | `G.buttons:get_UIE_by_ID("discard_button")` | `hand.lua:62, 68` |
| Sort hand | `Handy.regular_keybinds.set_sorting(G.hand, "rank"\|"suit")` → `G.FUNCS.sort_hand_value` / `sort_hand_suit` (`button_callbacks.lua:36, 45`) | `hand.lua:103, 128` |
| Reroll shop | `G.shop:get_UIE_by_ID("next_round_button").parent.children[2]` (guard `require_exact_func = "can_reroll"`) | `shop.lua:23, 29` |
| Leave shop | `G.shop:get_UIE_by_ID("next_round_button")` → `G.FUNCS.toggle_shop` (`button_callbacks.lua:2533`) | `shop.lua:63, 69` |
| Buy card | `card.children.buy_button.UIRoot` → `G.FUNCS.buy_from_shop` | `insta_actions/buttons.lua:84-85` |
| Buy and use | `card.children.buy_and_use_button.UIRoot` (`e.config.id == 'buy_and_use'`) | `insta_actions/buttons.lua:84` |
| Use consumable | build `UIBox{definition = G.UIDEF.use_and_sell_buttons(card)}`, crawl for `can_use_consumeable`, fire, then `:remove()` | `insta_actions/buttons.lua:66-81` |
| Sell card | same crawl, `can_sell_card` → `G.FUNCS.sell_card` (`button_callbacks.lua:2371`) | `insta_actions/buttons.lua:174-181` |
| Pick from pack | `G.FUNCS.can_select_card` → `G.FUNCS.use_card` on the pack card | `button_callbacks.lua:2132, 2179` |
| Skip pack | `G.FUNCS.can_skip_booster` → `G.FUNCS.skip_booster` (no element needed) | `shop.lua:99, 109` |
| Cash out | `G.FUNCS.cash_out` with `{ id = "cash_out_button" }` | `round.lua:69-71` |
| Select cards in hand | `card:click()` or `G.hand:add_to_highlighted(card)` / `remove_from_highlighted` (`exe:cardarea.lua:131, 187`) | — |
| Start a run | `G.FUNCS.start_run(nil, { seed=, stake=, deck_choice=, challenge= })` | `button_callbacks.lua:3025`, consumed at `game.lua:2077, 2228-2229` |
| Reorder jokers | **no funnel** — see §7.6 | — |

The two crawling cases (use/sell, and the card-attached buttons) matter:
Handy's `crawl_for_use_and_sell_buttons` *constructs* a throwaway `UIBox` from
`G.UIDEF.use_and_sell_buttons(card)` and registers a cleanup that removes it
(`insta_actions/buttons.lua:66-81`). It also highlights the card first and
un-highlights after (`card_executing.lua:34-37, 50-57`). Copy that discipline —
a leaked `UIBox` is a leaked `CardArea` and those are not garbage collected
(`steamodded-ui-reference.md`, "Teardown matters").

### 7.3 The one-action-per-settle rule

Handy sets a *blocker* flag on every action and clears it from a queued
`no_delete, blocking = false` event — `hand.lua:26-37` for play, `:66-77` for
discard, `shop.lua:27-38` for reroll, `card_executing.lua:77-89` for card
actions. That is Handy saying, in code, *"one of these per event-manager pass,
or the game double-fires."* A driver should adopt the same rule and then some:
one action, then full quiescence (§8), then the next.

`G.FUNCS.cash_out` needs even more care: Handy wraps it in **three nested
events** with `stop_use()` calls between (`round.lua:58-82`). That is empirical
knowledge about cash-out being fragile mid-animation. Do not simplify it away.

Two specifics for cash-out, read out of `functions/button_callbacks.lua:2974-3000`:

- It takes a bare fake node, `{ config = { id = 'cash_out_button' } }` — Handy
  passes exactly that (`round.lua:69-71`). **Do not set `config.button` on it**:
  the Lovely-patched guard at `:2975` is
  `if Handy.regular_keybinds.cashout_skipped and e.config.button then return end`,
  and a node carrying a `button` can be silently dropped.
- It requires `G.round_eval` (`:2977`) and it ends in `G.STATE = G.STATES.SHOP`
  (`:2994`), **not** `BLIND_SELECT`. It also reshuffles the deck via
  `G.deck:shuffle('cashout'..G.GAME.round_resets.ante)` (`:2981`) — seeded, so
  deterministic, but it means the post-cash-out deck order is part of what a
  golden log locks down.

### 7.4 Two details about `select_blind_button`

Verified at `functions/UI_definitions.lua:1758`:

```lua
{n=G.UIT.R, config={id = 'select_blind_button', align = "cm",
  ref_table = blind_choice.config, ...,
  one_press = true, button = 'select_blind'}, nodes={...}}
```

- **`ref_table = blind_choice.config`** is exactly what BalatroDB's hook reads
  (`mod/BalatroDB/src/hooks.lua:572`). Confirms the real element is the right
  thing to pass.
- **`one_press = true`** is a UI-level debounce. Firing `G.FUNCS.select_blind`
  directly bypasses it, so the driver owns the debounce. Another vote for the
  one-action-per-settle rule (§7.3).
- **There is no `can_select_blind` oracle** — the node carries
  `button = 'select_blind'` unconditionally, and `disabled` only changes its
  colour. So for this action the precondition must be written by hand; use
  Handy's, which is
  `G.GAME.blind_on_deck and G.blind_select and G.GAME.round_resets.blind_choices[G.GAME.blind_on_deck] and G.STATE == G.STATES.BLIND_SELECT`
  (`blind_select.lua:18-22`).

### 7.5 The `can_*` oracles are not pure — use them anyway, carefully

`can_buy` writes `e.UIBox.alignment.offset.y` (`button_callbacks.lua:63-68`)
and `can_buy_and_use` writes `e.UIBox.states.visible`
(`:79, 84`). So "just checking legality" moves and shows/hides UI. This is
fine when you run them against the **real** element — that is what the game
does every frame anyway (`engine/ui.lua:1051-1054`) — and actively harmful if
you run them against a hand-built node whose `UIBox` is a stub. One more
argument for real elements.

### 7.6 Joker reordering — the missing funnel, and the workaround

There is no `G.FUNCS` for it. Drag-and-drop bottoms out in `Node:release`,
which is an empty prototype (`exe:engine/node.lua:380`), reached via
`Card:release` → `self.area:release(dragged)` (`exe:card.lua:4576-4578`).
`CardArea` does not override `release`.

The actual reorder is a **side effect of layout**. `CardArea:align_cards`
assigns each card's `T.x` from its index and then re-sorts the table by that
same `T.x`:

```lua
if self.config.type == 'joker' or self.config.type == 'title_2' then
    for k, card in ipairs(self.cards) do
        if not card.states.drag.is then
            ...
            card.T.x = self.T.x + (self.T.w-self.card_w)*((k-1)/(#self.cards-1)) + ...
            ...
        end
    end
    table.sort(self.cards, function (a, b)
        return a.T.x + a.T.w/2 - 100*(a.pinned and a.sort_id or 0)
             < b.T.x + b.T.w/2 - 100*(b.pinned and b.sort_id or 0) end)
end
```
— `exe:cardarea.lua:509-528` (joker branch; the same assign-then-sort pattern
repeats for every area type at `:448, :464, :478, :507, :544`). A dragged card is skipped by the
`not card.states.drag.is` guard, keeps the cursor's `T.x`, and so sorts into a
new slot.

**Therefore the programmatic funnel is: permute `G.jokers.cards` directly, then
call `G.jokers:align_cards()`.** Because `align_cards` recomputes `T.x` *from
the new index order* before sorting, the sort is then a no-op and the
permutation sticks. Two caveats:

- Ensure no joker has `states.drag.is` set, or it will be skipped and
  re-sorted back by its stale `T.x`.
- `align_cards` also re-stamps `card.rank = k` (`exe:cardarea.lua:546-548`),
  which is what you want.

`coder/balatrobot` does the same thing a slightly different way — it replaces
`G.jokers.cards` wholesale and writes `card.ability.order` /
`card.config.center.order`, without calling `align_cards` or `set_ranks`
(§14.2). Doing both (permute, set the `order` fields, then `align_cards()`) is
the belt-and-braces version.

Because this bypasses `G.FUNCS` entirely, **BalatroDB will not log it.** Two
options: have the driver emit its own `joker.reorder` marker event, or add a
`CardArea:align_cards` hook to BalatroDB that diffs the order. The second is
more honest (it would also catch reorders from mods and from the human), but
`align_cards` runs every frame for every area, so it needs a cheap
order-fingerprint guard.

### 7.7 Actions with no element until you build one

`G.UIDEF.card_focus_ui(card)` and `G.UIDEF.use_and_sell_buttons(card)` produce
the attach/use/sell button trees on demand
(`insta_actions/buttons.lua:33, 67`). `card.children.buy_button` only exists
once `create_shop_card_ui` has run on the card (referenced at
`game.lua:3299`), which happens inside the delayed shop-population event — one
more reason the shop readiness predicate (§8) matters.

---

## 8. Synchronisation — the central design problem

Treat this as the deliverable's core. Everything else is mechanical.

### 8.1 Why the obvious predicates are not enough

- **`G.STATE` alone is wrong.** It changes at the *start* of a transition, not
  the end. `G.FUNCS.toggle_shop` sets `G.STATE = G.STATES.BLIND_SELECT` from
  inside an event with `delay = 0.5` (`functions/button_callbacks.lua:2554-2555`).
- **`G.STATE_COMPLETE` alone is wrong.** It is a latch meaning "the
  `update_<state>` entry work has run once", set at the top of each handler
  (`game.lua:3256-3257, 3268, 3390, 3411, 3449, 3459, 3503, 3543, 3594, 3645,
  3694, 3730`). It is set *before* the entry work's queued events finish. In
  `update_shop` it is set at `:3268` while the shop cards are still populated
  inside a nested `delay = 0.2` event gated on the shop UIBox having *moved*
  (`:3276-3300`).
- **An empty `E_MANAGER` alone is wrong.** Queues empty transiently between
  chained events. `dec_stop_use` alone enqueues seven chained
  `no_delete, blocking = false` events (`functions/misc_functions.lua:1544-1559`),
  so between any two of them the base queue can look idle.

### 8.2 The proposed predicate

Quiescent iff **all** of:

| # | Condition | Citation |
|---|---|---|
| 1 | `G.STATE_COMPLETE == true` | `game.lua:3256` etc. |
| 2 | `not G.CONTROLLER.locked` | recomputed every frame from `G.CONTROLLER.locks` at `engine/controller.lua:189-194` |
| 3 | `(G.GAME.STOP_USE or 0) == 0` | `functions/misc_functions.lua:1539-1559` |
| 4 | **every** queue in `G.E_MANAGER.queues` is empty | `engine/event.lua:107-113, 119-131, 175-196` |
| 5 | `not G.screenwipe` | `engine/controller.lua:190`; set by `G.FUNCS.wipe_on` (`button_callbacks.lua:3129`) |
| 6 | `G.SETTINGS.paused == false` | also a determinism requirement, §9. **See the carve-out below** |
| 7 | `G.OVERLAY_MENU == nil` | `button_callbacks.lua:1349` |
| 8 | the **state-specific** readiness clause below holds | |
| 9 | 1–8 have held for **3 consecutive frames** | see §8.3 |

`G.CONTROLLER.locked` (condition 2) is the best single primitive in the game:
`Controller:update` recomputes it from the whole `locks` table every frame, and
the game uses it to decide whether *a human* may act. Deferring to it is
exactly "act when a player could act". Contributors include `locks.wipe`
(screen wipe), `locks.frame` (a 0.1 s post-overlay grace at `:199-210`),
`locks.toggle_shop` (`button_callbacks.lua:2534`), and per-tag locks
(`tag.lua:229`).

**A correction worth stating explicitly, because I got it wrong first:**
`no_delete` does *not* mark a permanent background event. It only makes
`EventManager:clear_queue` skip the event (`engine/event.lua:138-167`);
completion removal is separate and unconditional — `EventManager:update`
removes any event whose `handle` reports `completed and time_done`
(`engine/event.lua:189-191`). So `no_delete` events drain normally.

This matters because `no_delete` is used for *real work*, not daemons:
`G.FUNCS.skip_blind` (`button_callbacks.lua:2799`), the `start_run` teardown
pair (`:3032, 3040`), `go_to_menu` (`:3054, 3061`), and the whole
`wipe_on`/`wipe_off` machinery (`:3185-3264`). Excluding them from the count
would let the driver act in the middle of a screen wipe. **Require every queue
empty, full stop.**

The only thing that could violate this is an event whose `func` returns nil
forever — a genuine per-frame daemon. I found none in vanilla; `dec_stop_use`'s
chain (`misc_functions.lua:1544-1559`) and the controller's frame-unlock event
(`engine/controller.lua:199-210`) both return `true` and so drain. A *mod*
could add one, which is another reason to control the mod set (§13.1). If one
turns up, the fix is to whitelist it by identity, not to weaken the condition.

Note also that `G.E_MANAGER.queues` is not a fixed set: Steamodded creates a
named `run_select` queue on demand (`smods/src/utils/run_select.lua:34-35`).
Iterate `pairs(G.E_MANAGER.queues)`, do not enumerate the five built-in names
from `engine/event.lua:107-113`.

**Condition 6 needs a carve-out for terminal states.** Reaching `GAME_OVER`
sets `G.SETTINGS.paused = true`, and `Event:handle` then skips every event that
was created while unpaused —
`if self.created_on_pause == false and G.SETTINGS.paused then _results.pause_skip = true; return end`
(`engine/event.lua:50`). So after a death the queues stop draining, quiescence
is never reached, and a driver that only acts when quiescent hangs until its
watchdog fires. `coder/balatrobot` hit this and special-cased it outside the
event system (§14.2).

The fix is structural, not a new clause: **terminal detection runs
unconditionally at the top of the tick, before the quiescence gate** — check
`G.STATE == G.STATES.GAME_OVER`, `G.STAGE ~= G.STAGES.RUN`, and whether
BalatroDB has emitted `run.end`. Because §8.6 puts the tick on a `Game:update`
after-hook and `Game:update` runs while paused (it only zeroes `dt`, at
`game.lua:2606`), the driver keeps getting frames and can finish cleanly.

### 8.2b A worked illustration: `skip_blind`

`functions/button_callbacks.lua:2795-2806` is the whole design in miniature:

```lua
G.FUNCS.skip_blind = function(e)
    stop_use()
    G.CONTROLLER.locks.skip_blind = true
    G.E_MANAGER:add_event(Event({
        no_delete = true,
        trigger = 'after',
        blocking = false, blockable = false,
        delay = 2.5,
        timer = 'TOTAL',
        func = function()
          G.CONTROLLER.locks.skip_blind = nil
          ...
```

Every clause earns its place here:

- `stop_use()` bumps `G.GAME.STOP_USE` → **condition 3** blocks for ~7 passes.
- `locks.skip_blind` → **condition 2** (`G.CONTROLLER.locked`) blocks until the
  event fires. This is the clause doing the real work: `blocking = false` means
  condition 4 alone would *not* stall the base queue behind it.
- `no_delete` → drains normally on completion; **condition 4** still blocks for
  the full 2.5 s.
- `timer = 'TOTAL'` → the 2.5 s is in `TOTAL` time, so `G.SETTINGS.GAMESPEED`
  **does** shorten it (`game.lua:2626`). Contrast an event created while paused,
  which defaults to `timer = 'REAL'` (`engine/event.lua:22-23`) and does not
  shorten. This is the single best argument for §9.5's "never pause".

### 8.3 State-specific readiness (condition 8)

| `G.STATE` | Additional clause | Why |
|---|---|---|
| `SELECTING_HAND` | `G.buttons and G.buttons.states.visible` and `G.buttons:get_UIE_by_ID("play_button")` resolves | `G.buttons` is created lazily at `game.lua:3241-3246` and hidden while the deck preview is up (`:3208-3210`) |
| `SHOP` | `G.shop` exists **and** `math.abs(G.shop.T.y - G.shop.VT.y) < 0.1` **and** `G.shop_jokers` has cards (or the shop is legitimately empty) | mirrors the game's own gate at `game.lua:3285` (vanilla: `exe:game.lua:3090`), tightened |
| `BLIND_SELECT` | `G.blind_select` and `G.blind_select_opts[string.lower(G.GAME.blind_on_deck)]` and the `select_blind_button` UIE resolves with `states.visible` | Handy's own guard, `blind_select.lua:18-29` |
| `ROUND_EVAL` | `G.round_eval` and `not G.TAROT_INTERRUPT` and `not G.PACK_INTERRUPT` | Handy's cash-out guard, `round.lua:52-55` |
| any `*_PACK` / `SMODS_BOOSTER_OPENED` | `G.pack_cards and G.pack_cards.cards[1]` | Handy, `shop.lua:93-97` |
| `HAND_PLAYED`, `DRAW_TO_HAND`, `NEW_ROUND`, `PLAY_TAROT` | **never quiescent** — these are transient; the driver waits them out | `game.lua:3386, 3407, 3445, 3382` |

**Handy had to patch in its own shop-ready flag.** `Handy.regular_keybinds.on_shop_loaded()`
appears in the patched dump at `game.lua:3286`, inside the shop-population
event, and **is absent from vanilla `exe:game.lua` entirely** (grepped) — so it
is a Lovely patch Handy adds. Every Handy shop keybind then gates on
`Handy.regular_keybinds.shop_loaded` (`shop.lua:19, 59`). That is direct
evidence from a mature mod that **no vanilla shop-ready signal exists**. The
driver should either add the same Lovely patch or use the moveable-position
clause above. The moveable clause is preferable: no patch, and it is what the
game itself tests.

### 8.4 Postconditions — the second half

Quiescence says "the game is idle"; it does not say "my action happened". Pair
every action with a postcondition and a watchdog:

| Action | Postcondition |
|---|---|
| play hand | `G.GAME.current_round.hands_left` decreased **and** `G.STATE ∈ {SELECTING_HAND, ROUND_EVAL, GAME_OVER, NEW_ROUND}` |
| discard | `G.GAME.current_round.discards_left` decreased |
| select blind | `G.GAME.round_resets.blind_states[<slot>] == 'Current'` (set at `button_callbacks.lua:2593`) and `G.STATE == SELECTING_HAND` |
| skip blind | that slot's state `== 'Skipped'` and `#G.GAME.tags` increased |
| buy | the card's `sort_id` appears in `G.jokers.cards`/`G.consumeables.cards`/`G.playing_cards`, and `G.GAME.dollars` decreased by the price |
| reroll shop | `G.GAME.current_round.reroll_cost` changed or the `shop_jokers` card ids changed |
| leave shop | `G.STATE == BLIND_SELECT` |
| cash out | `G.STATE == SHOP` (set at `button_callbacks.lua:2994`) and `G.GAME.dollars` increased |
| use consumable | consumable's `sort_id` gone from `G.consumeables.cards` |

Failing a postcondition within the watchdog must be a **test failure with a
dump**, never a retry. A retry hides exactly the timing bug you are trying to
catch.

### 8.5 Watchdogs and diagnostics

- Per action: N frames (start at 3000 — generous, since frames are cheap).
- Per run: M frames.
This is not hypothetical rigour. `coder/balatrobot` has no timeout anywhere,
and the result is four open issues in which a single unmet predicate wedges the
entire API until the process is killed (§14.2). Build the watchdog first.

- On timeout, write a diagnostic blob next to the log: `G.STATE`,
  `G.STATE_COMPLETE`, `G.GAME.STOP_USE`, `G.CONTROLLER.locks` (keys with truthy
  values), per-queue event counts with each event's `trigger`/`delay`/
  `blocking`/`blockable`/`no_delete`, and the last 20 emitted events. Without
  this, a CI timeout is unactionable.
- Handy ships an event-queue debugger
  (`HandyBalatro-2.0.5/src/controls/animation_skip/event_queue_debug.lua`) —
  read it before writing the dump code.

### 8.6 Where the tick lives

Hook `Game:update` with an after-observer, the way BalatroDB already does
(`mod/BalatroDB/src/hooks.lua:285`). That point runs once per frame, after
`E_MANAGER:update` (`game.lua:2644`) and after the moveable pass
(`game.lua:2769-2777`), so every predicate input is fresh. Do **not** drive from
a self-requeuing `E_MANAGER` event: the driver would then be an item in the
queue it is trying to observe as empty.

---

## 9. Determinism

### 9.1 The RNG model

`pseudorandom(seed, min, max)` calls `math.randomseed(seed)` and then draws
(`functions/misc_functions.lua:346-351`). So every *seeded* draw resets the
global LuaJIT stream. The stream's state between seeded draws is therefore
determined by how many **bare** `math.random()` calls happened since the last
reseed.

That is the whole determinism story: bare draws are everywhere, and three of
them make real gameplay decisions.

### 9.2 The three known hazards, confirmed

1. **`pseudoseed` while paused.** `functions/misc_functions.lua:328-330`:
   ```lua
   function pseudoseed(key, predict_seed)
     if key == 'seed' then return math.random() end
     if G.SETTINGS.paused and key ~= 'to_do' then return math.random() end
   ```
   Any pseudorandom decision taken while `G.SETTINGS.paused` is true is
   **unseeded**. Note `G.FUNCS.start_run` sets `G.SETTINGS.paused = true`
   (`functions/button_callbacks.lua:3027`) and `G.FUNCS.go_to_menu` likewise
   (`:3050`), so there are genuine paused windows in the run lifecycle.
   **Driver rule: never open an overlay menu during a scripted run, and assert
   `G.SETTINGS.paused == false` in the quiescence predicate (§8.2 condition 6).**
2. **First-buffoon pack.** `functions/common_events.lua:2274-2278`:
   ```lua
   function get_pack(_key, _type)
       if not G.GAME.first_shop_buffoon and not G.GAME.banned_keys['p_buffoon_normal_1'] then
           G.GAME.first_shop_buffoon = true
           return G.P_CENTERS['p_buffoon_normal_'..(math.random(1, 2))]
   ```
3. **Charm Tag and Meteor Tag mega packs.** `tag.lua:232` and `tag.lua:247`:
   `'p_arcana_mega_'..(math.random(1,2))` and `'p_celestial_mega_'..(math.random(1,2))`.
   Both hazard #2 and #3 are *pack variant* choices, which change pack size and
   therefore the whole downstream draw sequence.

### 9.3 Two further hazards found here

4. **`Card:get_id` draws unseeded.** `card.lua:1174-1176`:
   ```lua
   function Card:get_id()
       if SMODS.has_no_rank(self) and not self.vampired then
           return -math.random(100, 1000000)
   ```
   Every call on a rankless card (Stone cards, and modded no-rank cards)
   consumes from the stream. `get_id` is called in scoring paths, so the number
   of draws depends on how many rankless cards are evaluated — which is
   gameplay-dependent, but *also* frame-dependent if any per-frame code calls
   it.
5. **Sound/particle pitch draws.** Dozens of bare `math.random()` calls feed
   `play_sound` pitch arguments — `blind.lua:173, 475`, `card.lua:2460-2461,
   2519-2520, 2577-2578, 2738-2739, 2967, 4711, 4742`, `game.lua:1527, 1653-1654,
   1835-1836, 2672`, `tag.lua:78-79, 543-544`, `card_character.lua:116-135`.
   Arguments are evaluated before the call, so **muting does not stop the
   draws**. What *can* stop them is Handy-style animation skipping: `juice_card`
   and `Moveable:juice_up` become no-ops
   (`HandyBalatro-2.0.5/src/controls/animation_skip/hooks.lua:14-27`), so their
   internal draws (`card.lua:4742`) stop happening — which is another reason a
   test run must control which mods are loaded (§13).

Everything else I checked is properly seeded: `pseudoshuffle`
(`misc_functions.lua:206-218`) and `pseudorandom_element`
(`:254-298`) both call `math.randomseed` before drawing. Note that
`pseudorandom_element` sorts its candidate list by `sort_id` when the entries
are cards (`:289-290`) — so *relative* card creation order feeds RNG outcomes.

### 9.4 The other determinism input: `G.ID` and `G.sort_id`

- `G.sort_id` is a **process-global monotonic counter**, incremented once per
  `Card` construction (`exe:card.lua:24-25`) and **never reset** — not by
  `Game:start_run`, not by `Game:delete_run` (grepped: the only assignment in
  the dump is `card.lua:24`).
- `G.ID` is the same but for **every `Node`**, i.e. every UI element too
  (`exe:engine/node.lua:43-45`), and `Card:init` derives
  `self.unique_val = 1 - self.ID/1603301` (`exe:card.lua:40`).
- `unique_val` is a tie-break inside `Card:get_nominal`
  (`exe:card.lua:954`), which is the sort key for `CardArea:sort`
  (`exe:cardarea.lua:577-586`).

Consequences:

- **Absolute `sort_id` values differ between the first and second run of a
  process, and between a boot that visited the collection screens and one that
  did not.** BalatroDB logs `sort_id` as the card `id`
  (`mod/BalatroDB/src/util.lua:172`). Golden logs must renumber (§10).
- *Relative* order is preserved as long as card creation order is identical, so
  sorting and `pseudorandom_element` stay deterministic within a run. This is
  the assumption to state loudly and test: **run the same fixture twice in one
  process and confirm the normalized logs match** (§16-E7).

### 9.5 Recommended determinism regime for the golden suite

1. Pass `args.seed` explicitly; `G.GAME.seeded` becomes true (`game.lua:2228`).
2. Pin `G.SETTINGS.GAMESPEED` in the fixture (§6.4) and record it.
3. Assert `G.SETTINGS.paused == false` before every action.
4. **Patch the three unseeded decision sites** in the driver mod, behind a flag
   that is on only for tests, replacing `math.random(1, 2)` with
   `pseudorandom(pseudoseed('bdb_test_pack'..G.GAME.round_resets.ante), 1, 2)`.
   This makes the run *differ* from an unmodded run — an acceptable trade for a
   test harness, but it must be a loud, documented flag, and the same fixture
   should also be runnable unpatched to confirm the patch is the only
   difference.
5. Run each fixture **in a fresh process** (no menu browsing first) so `G.ID`
   and `G.sort_id` start from the same place.
6. Control the mod set exactly (§13). Handy in particular changes event
   blocking and delays globally (`animation_skip/event_manager.lua:24-45`) and
   swallows `ease_dollars` entirely when skipping (`animation_skip/hooks.lua:66-68`)
   — which would collide with BalatroDB's own `ease_dollars` hook
   (`mod/BalatroDB/src/hooks.lua:476`).

---

## 10. Golden-log testing — what to assert

Raw line equality is untenable: `t` is wall-clock, `run` embeds `os.time()` and
random hex, `ts` is a timestamp, `env` carries version strings, and card `id`s
are process-global (§9.4).

### 10.1 Normalization

Given a run's JSONL, produce a canonical form:

**Envelope**
- **Drop `t`.** It is `love.timer.getTime()` relative to segment start
  (`mod/BalatroDB/src/state.lua`, `state.context`) and varies with frame
  timing. Keep it in a *separate* relaxed assertion if you want timing
  coverage — e.g. "the gap between `round.start` and `round.end` is under X" —
  but never in equality.
- **Replace `run`** with the literal `"<run>"`.
- **Keep `v`, `seg`, `n`, `e`, `a`, `r`, `el`.** `n` being dense and gap-free
  is the strongest ordering assertion available; a single inserted event shifts
  every subsequent `n`, which is a *feature* — it means "an event appeared" is
  never silently absorbed.

**Payload (`d`)**
- **Renumber card ids.** Walk the file in order; the first time an `id` value
  is seen, map it to the next integer from 1. Rewrite every `id` through the
  map. This preserves identity relations (two references to the same joker
  still match) while erasing the `G.sort_id` offset. The map is built per file,
  so it is stable.
- **Drop `run.start`/`run.resume`'s `ts`** and **`env`** (the whole block —
  `game`, `lovely`, `smods`, `balatrodb` versions and the `mods` list). Assert
  `env` separately against an expected fixture so a version bump fails
  *loudly and once*, not on every golden diff.
- **Drop `profile`** (`event-schema.md` run-start block).
- **Keep `seed`, `seeded`, `deck_key`, `stake_key`, `win_ante`,
  `starting_deck_size`** — these are the fixture identity and should match
  exactly.
- **Numbers:** the encoder is `%.14g` (`smods/libs/json/json.lua:110`), so
  representation is already canonical. The `{"s": ..., "l": ...}` big-number
  form (`event-schema.md`) is deterministic given identical arithmetic. No
  normalization needed; do **not** round, because rounding would hide real
  scoring regressions.
- **Sort keys within each `d` object** before comparing. Payload key order is
  explicitly unordered (`event-schema.md`), so a hash-order change is noise.

**Do not normalize**
- `e`, and the *order* of events. That is the assertion.
- `a`, `r`, `el`. Their known lag at round boundaries (`event-schema.md`) is
  itself a behaviour worth locking down.

### 10.2 Comparison and reporting

Compare as a sequence of `(e, canonical_json(d))` tuples. On mismatch, report
the first divergent index, the preceding 5 events for context, and both sides.
A naive `diff` over the whole file will produce a wall of shifted lines after
the first insertion; index-of-first-divergence is the actionable output.

### 10.3 Two tiers of assertion

Separating these keeps a run from failing for two reasons at once:

- **Tier 1 — structural (strict equality on the normalized form).** The
  regression test.
- **Tier 2 — invariants (checked on every run, including agent runs).**
  Cheap, and they catch corruption that a golden file cannot because the golden
  file would be regenerated wrong:
  - `n` is dense and starts at 0 per segment.
  - `run.start`/`run.resume` is the first line; `run.end` is the last (if
    terminal); `run.final` immediately precedes `run.end`.
  - every `round.start` has a matching `round.end`.
  - `el` is monotonic false→true and flips only on a `blind.select` carrying
    `entered_endless`.
  - no `encode.error` events.
  - dollars are consistent: fold `ease_dollars` deltas and compare against the
    next snapshot's `dollars`.

### 10.4 Regenerating goldens

Make it a one-flag operation (`--update-goldens`) that reruns every fixture and
rewrites the normalized files, and make the diff reviewable. The cost of a
brittle golden suite is that people regenerate without reading; make reading
cheap.

---

## 11. Architecture of the test-driver mod

Separate mod, `BalatroDBDriver`, **not** part of `BalatroDB`. Reasons: it must
be absent from normal play; it patches RNG sites (§9.5) that must never ship;
and keeping it separate means the golden suite exercises the *shipping*
BalatroDB unchanged.

```
mod/BalatroDBDriver/
  BalatroDBDriver.json       -- SMODS manifest; dependency on BalatroDB
  main.lua                   -- boot: read config, install hooks, arm
  src/
    config.lua               -- read %APPDATA%/Balatro/BalatroDB/driver.json
    speed.lua                -- FPS_CAP, love.draw stub, GAMESPEED, mute
    determinism.lua          -- the three RNG patches, behind a flag
    selectors.lua            -- the §7.2 table, one function per action
    ready.lua                -- the §8.2 predicate + §8.3 state clauses
    actions.lua              -- execute + postcondition per action type
    script.lua               -- script runner (goal 2)
    policy.lua               -- policy runner (goal 1)
    report.lua               -- result file, diagnostics dump, exit
  lovely/                    -- only if a patch proves unavoidable
```

### Boot sequence

1. `main.lua` reads `driver.json` via `SMODS.NFS`. **If the file is absent, do
   nothing at all** — the mod is inert in normal play. This is the safety
   property that lets it sit in the Mods folder.
2. Apply speed settings (§6.4) and determinism patches (§9.5) per config.
3. Install an after-hook on `Game:update` (§8.6).
4. Arm a state machine: `WAIT_MENU → START_RUN → RUNNING → FINISH`.
5. `WAIT_MENU` waits for `G.STAGE == G.STAGES.MAIN_MENU` and quiescence, then
   calls `G.FUNCS.start_run(nil, {seed=..., stake=..., deck_choice=...})`.
6. `RUNNING` is: *if quiescent → pick the next action → precheck legality with
   the `can_*` oracle → execute → switch to WAIT_POST with a postcondition and
   a deadline*.
7. `FINISH` flushes BalatroDB's buffer (`BalatroDB.log.flush()`), writes a
   result JSON, and calls `love.event.quit(0 or 1)`.

### Exit

`love.event.quit(n)` pushes a quit event; `love.run` returns `a or 0`
(`main.lua:966`) after `love.quit()` runs (`main.lua:1039`). BalatroDB
already hooks `love.quit` (`mod/BalatroDB/src/hooks.lua:831`) so the log is
closed properly. **Confirm the process exit code actually reaches the shell**
(§16-E6) — if a fused LÖVE exe on Windows always exits 0, the harness must read
the result file instead.

### Two things the driver must not do

- **Never open an overlay menu.** It sets `G.SETTINGS.paused = true`
  (`button_callbacks.lua:1405, 1412, 1419, 1426, …`), which makes `pseudoseed`
  unseeded (§9.2) and makes newly-created events use the `REAL` timer
  (`engine/event.lua:22-23`), which `GAMESPEED` does not accelerate.
- **Never leak a constructed `UIBox`.** Follow Handy's cleanup discipline
  (§7.2).

---

## 12. Worked example: one scripted test

Fixture `tests/golden/ante1_smallblind_play/`:

```json
// fixture.json
{
  "seed": "BDBTEST1",
  "deck_key": "b_red",
  "stake": 1,
  "gamespeed": 4,
  "determinism_patches": true,
  "max_frames": 200000,
  "script": [
    {"do": "select_blind",  "slot": "Small"},
    {"do": "select_cards",  "by": "index", "indices": [1,2,3,4,5]},
    {"do": "play_hand"},
    {"do": "select_cards",  "by": "index", "indices": [1,2,3]},
    {"do": "discard"},
    {"do": "select_cards",  "by": "index", "indices": [1,2,3,4,5]},
    {"do": "play_hand"},
    {"do": "cash_out"},
    {"do": "leave_shop"},
    {"do": "end"}
  ],
  "env_expect": { "game": "1.0.1o", "smods": "26.829.0", "balatrodb": "0.2.0" }
}
```

Driver code for the two most representative actions (illustrative — not run):

```lua
-- selectors.lua
local S = {}

function S.play_button()
  return G.buttons and G.buttons.states.visible
     and G.buttons:get_UIE_by_ID("play_button") or nil
end

function S.select_blind_button()
  local slot = G.GAME and G.GAME.blind_on_deck
  local opts = slot and G.blind_select_opts and G.blind_select_opts[slot:lower()]
  return opts and opts:get_UIE_by_ID("select_blind_button") or nil
end

-- actions.lua
local A = {}

local function fire(element)
  -- Mirrors Handy.fake_events.execute_button (fake_events.lua:63-78).
  if not (element and element.config) then return false, 'no element' end
  if type(element.click) == 'function' then element:click(); return true end
  -- Run the gating func first if the element has one: many buttons only get
  -- config.button assigned by their own per-frame func (engine/ui.lua:1051).
  if element.config.func and G.FUNCS[element.config.func] then
    G.FUNCS[element.config.func](element)
  end
  local name = element.config.button
  if not name or not G.FUNCS[name] then return false, 'no button on element' end
  G.FUNCS[name](element)
  return true
end

local function legal(element, predicate_name)
  -- Mirrors Handy.fake_events.check (fake_events.lua:2-21): run the game's own
  -- can_* oracle against the real element and read back config.button.
  local f = G.FUNCS[predicate_name]
  if not f then return true end        -- no oracle for this action
  f(element)
  return element.config.button ~= nil
end

A.play_hand = {
  ready = function()
    local e = S.play_button()
    return e and legal(e, 'can_play')
  end,
  exec = function(st)
    st.hands_left_before = G.GAME.current_round.hands_left
    return fire(S.play_button())
  end,
  post = function(st)
    return G.GAME.current_round.hands_left < st.hands_left_before
       and (G.STATE == G.STATES.SELECTING_HAND
         or G.STATE == G.STATES.ROUND_EVAL
         or G.STATE == G.STATES.NEW_ROUND
         or G.STATE == G.STATES.GAME_OVER)
  end,
}

A.select_blind = {
  ready = function()
    return G.STATE == G.STATES.BLIND_SELECT
       and G.blind_select and S.select_blind_button() ~= nil
  end,
  exec = function(st)
    st.slot = G.GAME.blind_on_deck
    -- The REAL element: G.FUNCS.select_blind reads e.UIBox:get_UIE_by_ID('tag_container')
    -- at button_callbacks.lua:2590 to record the skip tag.
    return fire(S.select_blind_button())
  end,
  post = function(st)
    return G.GAME.round_resets.blind_states[st.slot] == 'Current'
       and G.STATE == G.STATES.SELECTING_HAND
  end,
}
```

Card selection uses `Card:click()` (`exe:card.lua:4610`) or, more directly,
`G.hand:add_to_highlighted(card)` / `remove_from_highlighted`
(`exe:cardarea.lua:131, 187`), after `G.hand:unhighlight_all()` (`:201`) — and
with the hand **sorted deterministically first** (`G.FUNCS.sort_hand_value`,
`button_callbacks.lua:45`) so that `"indices"` in the script means something
stable.

Runner, per frame:

```
tick():
  if not ready.quiescent() then return end
  if state == WAIT_POST:
      if action.post(ctx) then state = NEXT; return
      if frames_waited > deadline then fail('postcondition timeout', dump())
      return
  action = script[i]
  if not action.ready() then
      if frames_waited > deadline then fail('never became ready', dump())
      return
  ok, err = action.exec(ctx)
  if not ok then fail('exec refused: '..err, dump())
  state, frames_waited = WAIT_POST, 0
```

The harness side:

```powershell
# run one fixture
$env:LOVELY_MOD_DIR = "$repo\ci\Mods"
Copy-Item $fixture\fixture.json "$env:APPDATA\Balatro\BalatroDB\driver.json"
Start-Process -Wait -FilePath $balatro
python tools\normalize.py "$env:APPDATA\Balatro\BalatroDB\runs\*.jsonl" > actual.jsonl
python tools\compare.py $fixture\golden.jsonl actual.jsonl
```

---

## 13. CI story

### 13.1 Mod isolation — the most important part

A golden run must load **exactly** BalatroDB, BalatroDBDriver and Steamodded.
Two mechanisms exist, both read out of `version.dll`'s string table:

- **`LOVELY_MOD_DIR`** environment variable — points the injector at a
  different Mods directory. This is the clean one: a `ci/Mods/` tree in the
  repo with symlinks to `smods`, `BalatroDB`, `BalatroDBDriver` and nothing
  else.
- **`Mods/lovely/blacklist.txt`** — one mod folder name per line, skipped at
  load. The live one currently contains `GodMode` and `Brainstorm`. Useful as
  a fallback, but it mutates the developer's own install; prefer the env var.

The injector also has CLI flags; the strings in `version.dll` are stored in a
concatenated table so they read truncated (`--vanill`, `--disabl`, `--dump-a`,
plus the complete `--disable-console`). These are almost certainly `--vanilla`,
`--disable-mods`/`--disable-…`, `--dump-all`. **Confirm against Lovely's own
README/`--help` before relying on them** (§16-E3).

Why this matters beyond tidiness: Handy, if loaded, rewrites `EventManager:add_event`
to mutate `blocking`, `blockable` and `delay` on nearly every event
(`animation_skip/event_manager.lua:24-45`), overrides `EventManager:update`
(`speed_multiplier/hooks.lua:34`), no-ops `juice_card`/`Moveable:juice_up`
(`animation_skip/hooks.lua:14-27`) — which changes bare-`math.random`
consumption (§9.3) — and swallows `ease_dollars` (`:66-68`), which BalatroDB
hooks (`mod/BalatroDB/src/hooks.lua:476`). Any of these can change the log.

### 13.2 Platform

- **Windows self-hosted runner is the realistic target.** It is where the
  install, Lovely (`version.dll`) and `luasteam.dll` already work, and where GL
  is real. A GitHub-hosted `windows-latest` runner has no GPU; LÖVE on it would
  need a software GL path, which is untested here.
- **Linux is a bigger project**: a Linux Balatro build, Lovely's Linux
  injector, `xvfb-run` plus `LIBGL_ALWAYS_SOFTWARE=1`. Feasible (LÖVE games do
  run under Xvfb + llvmpipe), but it is a second port, not a config change.
- Run the suite **serially per process, parallel across processes**, one
  fixture per process (§9.5 item 5 requires a fresh process anyway).

### 13.3 The Steam question

Per §5, the evidence says no Steam client is needed. **This gates everything
else**, so §16-E1 is experiment number one. If it turns out Steam *is* needed,
the fallback is a self-hosted runner with Steam running in offline mode — which
works but rules out ephemeral cloud runners.

### 13.4 Repo layout and the test pyramid

This suite is the third tier, not the only one:

| Tier | What it covers | Where |
|---|---|---|
| 1 — pure Lua unit tests | number coercion, card serialization, hook-wrapper contract | `tests/test_util.py` (existing, lupa/Lua 5.5) |
| 2 — log invariants | the §10.3 tier-2 checks, runnable over *any* JSONL including real play | new, pure Python, no game needed |
| 3 — golden runs | this document | new, needs the game |

Tier 2 is worth building **before** tier 3: it is cheap, it runs over the
corpus already on disk, and it will find schema bugs that a golden file would
simply enshrine.

Proposed layout:

```
ci/Mods/                       LOVELY_MOD_DIR target: junctions to smods, BalatroDB, BalatroDBDriver
mod/BalatroDBDriver/           the driver mod (§11)
tests/golden/<name>/fixture.json
tests/golden/<name>/golden.jsonl
tools/normalize.py             §10.1
tools/compare.py               §10.2
tools/invariants.py            §10.3 tier 2
tools/run_fixture.ps1          launch, wait, normalize, compare
```

### 13.5 Artifacts

On failure, upload: the raw JSONL, the normalized JSONL, the golden, the
diagnostics dump (§8.5), and the Lovely log from
`%APPDATA%\Balatro\Mods\lovely\log\`.

### 13.6 Isolate the save directory

`love.filesystem.getSaveDirectory()` is `%APPDATA%\Balatro` and is where both
BalatroDB's `runs/` (`mod/BalatroDB/src/log.lua`, `log.root`) and the game's
profiles live. CI runs will write profile data and, unless `G.F_NO_SAVING`,
save files. Point `%APPDATA%` at a scratch directory for the CI process so the
developer's profile is never touched — and so each run starts from a known
profile state, which matters because `G.SETTINGS` (including `GAMESPEED`) is
persisted per profile.

---

## 14. Prior art: BalatroBot, BalatroLLM, Handy

### 14.1 Handy — study it, borrow from it, do not depend on it

Handy is the most valuable artifact here and it is already on disk at
`%APPDATA%\Balatro\Mods\HandyBalatro-2.0.5\`. It has independently solved four
of this document's problems:

| Problem | Handy's answer |
|---|---|
| Firing `G.FUNCS` without a mouse | `src/core/fake_events.lua` (79 lines, §7.1) |
| Which element per action | `src/definitions/controls/regular_keybinds/*.lua` (§7.2) |
| Legality checks | reuses the game's `can_*` predicates (§7.1) |
| Speed | sliced, forced `EventManager:update` (`src/controls/speed_multiplier/hooks.lua:34-86`), delay multipliers on enqueue (`src/controls/animation_skip/event_manager.lua`) |
| "Is the shop ready?" | had to Lovely-patch its own `on_shop_loaded` flag in (§8.3) |

Its speed multiplier goes to `2^18` with a "safe" cap of `2^9`
(`src/controls/speed_multiplier/index.lua:2-3`) — i.e. 512× is considered safe
by people who have run it a lot. That is a useful prior for §6's ceiling.

**Borrow the techniques; do not load the mod during golden tests** (§13.1).

### 14.2 BalatroBot — the lineage, and what to take

**Get the repo right.** The brief for this investigation named
`AleksaMilovanovic/balatrobot`; that is a 0-star, untouched fork of the real
project. The lineage is:

| Repo | State | License |
|---|---|---|
| `besteon/balatrobot` | **original, dead** (last commit 2024-04-28). Targets Steamodded 0.9.3 and will not load today — it uses `SMODS.INIT`, removed in SMODS 1.0 | **no license file — all rights reserved. Do not copy code from it.** |
| `coder/balatrobot` | **the live one.** v1.5.2 on `main`; `dev` is ~180 commits ahead and is a substantial rework | MIT |
| `giewev/balatrobot` | fork of *besteon* by an original contributor; adds a large RL stack over its own Python sim | — |

Findings below come from a source-reading pass over the GitHub API. I have not
read these files myself; they are second-hand but specifically cited.

**Transport.** `coder/balatrobot` is JSON-RPC 2.0 over a hand-rolled HTTP/1.1
server on TCP 12346 (`src/lua/core/server.lua`), single-client, one request per
connection, pumped once per frame from a `love.update` hook. `besteon` was UDP
with a pipe-delimited text protocol.

**This corrects something in §2.** LÖVE bundles LuaSocket and preloads it
(`love/src/modules/love/love.cpp:655`), so `require "socket"` works inside
Balatro with **no native dependency and no extra install**. My "files and a
spin" suggestion in §2 assumed sockets needed something extra; they do not. For
goal 1's external-policy bridge a socket is now the obvious choice. For goal 2
the argument against a socket was never about dependencies — it was about
adding a second clock — and that still holds.

**Hooking.** `coder/balatrobot` ships **no `lovely.toml` at all**. It is a
plain Steamodded mod whose only engine patch is:

```lua
local love_update = love.update
love.update = function(dt)
  BB_GAMESTATE.check_game_over()
  love_update(dt)
  BB_SERVER.update(BB_DISPATCHER)
end
```

That is a working existence proof for §11's "no Lovely patch needed" claim.

**Synchronisation — the most important finding, and it validates §8.4 more than
§8.2.** Direct answers to the questions §8 poses:

| Does it… | Answer |
|---|---|
| poll `G.STATE` / `G.STATE_COMPLETE` | yes, pervasively |
| check `G.E_MANAGER.queues` emptiness | **never — zero occurrences in the whole `src/lua/` tree** |
| wait on `G.CONTROLLER.locked` | yes (`play.lua:120`, `load.lua:125`) |
| wait on `G.CONTROLLER.locks.<name>` | yes (`use.lua`: `not G.CONTROLLER.locks.use`) |
| wait on `G.GAME.STOP_USE` | yes (`use.lua:196-215`) |
| hook `G.FUNCS` | no — it *calls* them |

There is **no generic settle routine**. Every endpoint hand-writes its own
completion predicate inside a `trigger = "condition"` event. The best of them,
`src/lua/endpoints/sell.lua:120-155`, is pure postcondition — card count
decreased by one, money increased by exactly `sell_cost`, the card's `sort_id`
absent from the area, `G.STATE_COMPLETE`, and still in a valid state. That is
§8.4, arrived at independently.

**And the cost of having no §8.2 is documented in their issue tracker.** There
is no server-side timeout anywhere, and `accept()` is gated on
`client_socket == nil`, so a predicate that never becomes true **wedges the
whole API**. Four open issues on `main` are exactly this: #195 (sell hangs on
Invisible Joker), #198 and #199 (buy hangs on packs), #233 (skip-opened tag
packs). All are marked fixed on `dev` and unfixed in the released version.
#199's root cause is worth reading: `buy.lua` infers "does this pack deal a
hand?" from the *first card's* `ability.set`, and Black Hole is `set="Spectral"`
but appears in Celestial packs, so the predicate waits for a hand that is never
dealt.

**Read this as direct empirical support for §8.5's watchdog requirement.** A
per-action deadline with a diagnostic dump is not defensive programming; it is
the difference between a test suite and a hang.

**Their quiescence ADR is the single most valuable document in the
ecosystem** — `docs/adr/0003-screenshot-settling-quiescence.md` on the `dev`
branch. Two findings from it:

1. *"A drained event queue does not imply a still screen, because the tween
   layer keeps gliding after the last event completes."* That is §6.1(d) and
   §8.3's shop clause, stated generally. Their predicate is over `G.MOVEABLES`,
   not the event queue: for every moveable, `m.juice == nil` and
   `|T.x-VT.x| < 0.01`, `|T.y-VT.y| < 0.01`, `|T.r-VT.r| < 0.001`.
2. They **rejected `Moveable.STATIONARY`** because Balatro keeps a default UI
   element hovered even with no mouse, giving a perpetual ~0.05 scale delta.
   *"A first implementation using STATIONARY directly exhibited exactly this:
   15 s per call, deadman on every request."*

Scope caveat they state themselves: that predicate gates **screenshot capture
only**, not normal API responses. So it is a design they wrote but did not put
on the hot path. §8.3's targeted clause — check the *specific* moveable the
game itself checks (`G.shop`) — is cheaper and sidesteps the hover trap
entirely. Reach for the general form only if the targeted one proves
insufficient.

**`GAME_OVER` needs a carve-out, and they hit it.** Quoted from their
`play.lua`:

> `-- NOTE: GAME_OVER detection cannot happen inside this event function`
> `-- because when G.STATE becomes GAME_OVER, the game sets G.SETTINGS.paused = true,`
> `-- which stops all event processing.`

The mechanism is `Event:handle`'s first line:
`if self.created_on_pause == false and G.SETTINGS.paused then _results.pause_skip = true; return end`
(`engine/event.lua:50`). Their answer is a callback fired from the
`love.update` hook, *before* the game's own update. §8.6 already puts the
driver's tick outside the event system, so it keeps ticking — **but §8.2's
condition 6 (`paused == false`) would stop it ever acting.** Fix: terminal-state
detection (`GAME_OVER`, and BalatroDB's `run.end` having been emitted) must run
*outside* the quiescence gate, as an unconditional check at the top of the tick.

**Dispatch style — mixed, which matches §1's recommendation.** Real UIElements
for `play`, `discard`, `select`, `skip`, `next_round`
(`UIBox:get_UIE_by_ID("play_button", G.buttons.UIRoot)` →
`G.FUNCS.play_cards_from_highlighted`); a minimal fake node
`{ config = { ref_table = card } }` for `sell` and `use`; bare tables for the
functions that tolerate them — `G.FUNCS.reroll_shop(nil)`,
`G.FUNCS.toggle_shop({})`, `G.FUNCS.cash_out({ config = {} })`,
`G.FUNCS.skip_booster({})`. Note their `cash_out` call passes `config = {}`
with no `button`, consistent with §7.3's warning. Card selection is
`G.hand:unhighlight_all()` then `G.hand.cards[i+1]:click()` — indices are
**0-based** throughout their API.

**Joker reordering — they do support it** (`src/lua/endpoints/rearrange.lua`),
by direct array replacement:

```lua
G.jokers.cards = new_array
...
if card.ability then card.ability.order = i end
if card.config and card.config.center then card.config.center.order = i end
```

They do **not** call `G.jokers:set_ranks()` (`exe:cardarea.lua:263`), which
`besteon`'s older equivalent did. §7.6's recommendation — permute then
`align_cards()` — covers that, because `align_cards` re-stamps `card.rank = k`
at `exe:cardarea.lua:546-548`. Their `ability.order` / `center.order` writes are
worth copying if anything reads those; I did not check what does.

**Cheat endpoints worth knowing for fixtures.** `set` (money, chips, ante,
round, hands, discards, shop re-stock) and `add` (spawn any card by key with
arbitrary seal/edition/enhancement/sticker). These void seed determinism, but
they are how you build a fixture that starts at ante 6 without playing five
antes first. Worth reimplementing in the driver behind an explicit flag, and
worth recording in the log so a golden file can never be mistaken for organic
play.

**Speed — they do everything in §6.4 and more.** From `src/lua/settings.lua`:
`G.FPS_CAP`, `G.SETTINGS.GAMESPEED` (4 normal, 10 "fast"; `dev`'s `turbo`
profile uses **128**), `love.draw = function() end`,
`love.graphics.present = function() end`, plus `G.ANIMATION_FPS`,
`love.window.setVSync(0)`, `G.F_SOUND_THREAD = false`, `G.F_MUTE = true`,
shadows/bloom/CRT off, `reduced_motion`, `skip_splash`, `screenshake = false`,
`G.F_SKIP_TUTORIAL = true`.

Four specifics worth stealing or avoiding:

- **`love.graphics.isActive = function() return false end`** is a neater draw
  kill than stubbing `love.draw`: it makes Balatro's *own* loop skip both
  `love.draw` and `love.graphics.present` at `main.lua:988-991`, rather than
  calling a stub. Prefer this over §6.4's version.
- **They inject a fixed dt**: `love.update = function(_) love_update(dt) end`
  with `dt = 4.99/60` (≈0.083) headless, `1/60` otherwise — bypassing Balatro's
  `dt_smooth` (§6.1(b)). `besteon`'s config comment says values above `8/60`
  (≈0.133, i.e. above the 0.1 clamp) *"seem to cause instability"*. That is the
  only empirical datum anywhere on how large a `dt` Balatro tolerates.
  **`coder`'s `dev` branch removes the fixed-dt patch entirely** and gets speed
  purely from `GAMESPEED` — read that as a considered retreat from the dt hack.
- **`besteon` overrode `Moveable.move_xy` to snap `VT` straight to `T`** — a
  blunt kill of the tween layer, and a lever §6.4 does not list. It would
  collapse §6.1(d)'s animation floor outright. It also changes *when*
  position-gated logic fires (the shop gate at `game.lua:3285`), so it is
  **incompatible with golden tests** and belongs only in the goal-1 bulk-play
  configuration.
- **A bug not to copy**: their "fast" mode sets `G.FPS_CAP = nil` with the
  comment *"Unlimited FPS"*. Balatro then does `G.FPS_CAP = G.FPS_CAP or 500`
  (`main.lua:994`) — that is a 500 cap with a busy sleep, not unlimited.
  §6.4's advice to set a large number, not `nil`, is the correct form.

**Neither project is headless in the LÖVE sense.** `coder`'s
`configure_headless()` does `love.window.minimize()`, `setMode(1,1)`,
`setPosition(-1000,-1000)` and no-ops the window API; the window and GL context
still exist, and their Linux launcher hard-fails without `DISPLAY` or
`WAYLAND_DISPLAY`. Independent confirmation of §4.5.

**Determinism.** Both pass the seed straight to
`G.FUNCS.start_run(nil, {seed=…})` — identical to §2. More interesting: their
`load` endpoint reads a `.jkr`, `STR_UNPACK`s it and calls
`G:start_run{savetext = …}`, and because `recursive_table_cull` only replaces
tables that are `Object`s, `G.GAME.pseudorandom` (a plain table of floats)
**survives the round trip exactly**. So branch-and-restore against the real
game is feasible — a genuinely useful fixture primitive: snapshot a mid-run
state once, then run many short scripted tests from it instead of replaying
antes.

**Independent corroboration of §9.2.** Both `coder/balatrobot`'s issue history
and the Jackdaw reimplementation (below) name the *same three* unseeded
`math.random` sites: Charm Tag, Meteor Tag, first Buffoon pack. Jackdaw
documents deviating from the real game at exactly those points. Two independent
projects landing on the same three is decent evidence the list is complete for
vanilla — though note that neither names `Card:get_id` (§9.3), which I found
here, so "complete" should still be treated as unproven.

### 14.3 BalatroLLM and the wider ecosystem

- **`coder/balatrollm`** (MIT) sits entirely on top of `coder/balatrobot`'s
  JSON-RPC API and never touches the game. It adds a strategy format
  (Jinja-templated prompts over the gamestate dict), a rolling 10-action memory
  window, OpenAI-style tool calls, and a task runner doing a Cartesian product
  over model × seed × deck × stake × strategy with N parallel game instances on
  consecutive ports. **The parallel-instances-on-consecutive-ports pattern is
  the one thing to copy** for goal 1's throughput.
- **`coder/balatrobench`** is a leaderboard over BalatroLLM artifacts; its
  published data is roughly seven months stale at n=15 runs per model. Not a
  benchmark to target.
- **`TylerFlar/jackdaw-balatro`** (MIT, active) is a Python reimplementation
  that reproduces LuaJIT's TW223 PRNG and Balatro's `pseudohash`/`pseudoseed`
  bit-for-bit, and — the notable part — **validates differentially against the
  live game through BalatroBot**, ~250 injected-state scenarios with
  checked-in fixtures. If goal 1 ever needs millions of games rather than
  thousands, that is the path; and its differential-testing methodology applies
  directly to validating *our* golden suite.
- **Calibration for goal 1, from the ecosystem's honest failures:**
  `taggarttufte/balatro-rl` reached a 2.35% win rate over eight architectures
  and five months, then self-audited and found the simulator underpinning that
  result was broken. `idIing/jackhammer` ran 240-seed baselines with bootstrap
  CIs: random-legal play reaches ante 1.0, cheapest-joker heuristics ante 3.2,
  **0/240 wins in every baseline**. `Khetnen/balatro-zero` ran ~47k AlphaZero
  self-play games for zero wins until LLM demonstrations were added as
  curriculum. Balatro is *hard* for automated play. Size the data-collection
  goal accordingly: a corpus of losing runs is still a corpus, and is probably
  what you will get first.

### 14.4 Licensing — read this before copying anything

- **Handy is GPL-3.0** (`HandyBalatro-2.0.5/LICENSE`). Its techniques are fine
  to learn from, and its selector *facts* (which element ID, which `G.FUNCS`)
  are facts rather than expression — but **do not paste Handy code into
  BalatroDB** unless BalatroDB is willing to be GPL-3.0. Reimplement from the
  described behaviour.
- **`besteon/balatrobot` has no license file** — all rights reserved. Do not
  copy from it at all.
- **`coder/balatrobot`, `coder/balatrollm` and `jackdaw-balatro` are MIT** —
  safe to borrow with attribution.

## 15. Verified vs. assumed

### Verified by reading source or files (citations above)

- LÖVE 11.5 r2, SDL 2.28.5, fused exe, no Steam DRM wrapper, `steam_appid.txt`
  present.
- `conf.lua` exists and does **not** set `t.window = false`.
- Steam is `pcall`-guarded in Lua and `G.STEAM` may legitimately be `nil`.
- `G.FPS_CAP` is read at exactly one place and assigned nowhere else.
- `dt` is smoothed and clamped to 0.1 before reaching `love.update`.
- `E_MANAGER:update` receives `real_dt`, not `SPEEDFACTOR`-scaled dt.
- `forced` bypasses only the `queue_dt` rate limit and does not advance timers.
- Moveable movement uses `min(1/20, real_dt)` and ignores `SPEEDFACTOR`.
- The game gates shop population on a moveable position.
- `G.CONTROLLER.locked` is recomputed from `locks` every frame.
- `stop_use`/`dec_stop_use` decays over seven chained `no_delete` events.
- `no_delete` only exempts an event from `clear_queue`; completion removal in
  `EventManager:update` is unconditional. `no_delete` is used for real work
  (`skip_blind`, `wipe_on`/`wipe_off`, `start_run`, `go_to_menu`), not for
  daemons.
- `Handy.regular_keybinds.on_shop_loaded` is a Handy Lovely patch, absent from
  vanilla `game.lua` — there is no vanilla shop-ready signal.
- The complete `can_*` predicate list and the `G.FUNCS` entry points in §7.2.
- `can_buy` and `can_buy_and_use` mutate `e.UIBox` — the legality oracles are
  not pure (§7.5).
- `Event:handle` skips every non-pause-created event while `G.SETTINGS.paused`,
  which is why `GAME_OVER` needs a carve-out (`engine/event.lua:50`).
- Handy is GPL-3.0; `besteon/balatrobot` has no license file (§14.4).
- `G.FUNCS.select_blind` reads the tag from `e.UIBox`.
- Joker reordering has no `G.FUNCS`; `CardArea:align_cards` sorts by `T.x`.
- `pseudoseed`-while-paused, `get_pack` first-buffoon, and the two tag mega-pack
  sites all draw unseeded; `Card:get_id` does too.
- `G.sort_id` and `G.ID` are process-global and never reset.
- `LOVELY_MOD_DIR` and `blacklist.txt` exist as isolation mechanisms.
- Handy's fake-event, selector, animation-skip and speed-multiplier
  implementations, as cited.

### Second-hand — read by a research pass, not by me

Everything in §4.2/§4.2b/§4.4 about LÖVE and SDL internals, and everything in
§14.2/§14.3 about BalatroBot, BalatroLLM, Jackdaw and the rest, comes from a
source-reading pass over the upstream GitHub repositories. The citations are
specific and I have no reason to doubt them, but I did not open those files and
nothing was executed. Two specific caveats the research pass flagged about its
own work:

- It could not resolve whether Balatro's shipped `lua51.dll` is LuaJIT 2.0.5 or
  2.1 — two sources disagree. (Resolvable here: check the DLL's version
  resource or its embedded version string. I did not.)
- It read `coder/balatrobot`'s endpoints for the sync predicates and dispatch
  style, but not its validator or error modules, and it did not read
  `besteon`'s `src/utils.lua`, so the older project's exact action grammar is
  unread.

Where this document's own source reading and the research pass disagree, the
first-hand citations win.

### Inferred, not verified

- That `Balatro.exe` launches with no Steam client running. Evidence is strong
  but indirect. **E1.**
- Whether the SDL2 binary shipped with Balatro includes the `offscreen` video
  driver at all. Unresolvable from source. **E4b.**
- That the §9.2/§9.3 list of unseeded `math.random` sites is *complete*. Two
  independent projects name the same three gameplay sites (§14.2), which is
  reassuring, but neither names `Card:get_id` (§9.3) — so the search has not
  converged. **E7 is the empirical test.**
- All numbers in §6.2. No profiling was done. **E5.**
- That skipping `love.draw` does not perturb game logic. `Game:draw` does touch
  `G.SHADER_CANVAS_*` (`game.lua:3120-3121`) and `Card:draw` mutates
  `self.shadow_height` (`exe:card.lua:4361`) — **there may be logic hiding in
  draw**. This needs checking before trusting it. **E8.**
- That the quiescence predicate in §8.2 is sufficient. It is a design, not a
  measurement. **E2.**
- That the process exit code from `love.event.quit(n)` survives a fused Windows
  exe. **E6.**
- That relative `sort_id` order is stable across identical runs. **E7.**
- Lovely's exact CLI flags (strings are truncated in the binary). **E3.**

### Known unknowns I did not resolve

- Whether any Lovely patch already in the dump changes `love.run` or
  `Game:update` in ways that affect the above. I read the *patched* source, so
  the citations reflect what runs — but I did not diff patched vs. vanilla for
  these files.
- Whether `G.SCORING_COROUTINE` (referenced by Handy at
  `speed_multiplier/hooks.lua:36`) exists in this Steamodded version. It does
  not appear in the dump; if a future SMODS adds it, the quiescence predicate
  needs a clause for it.
- Whether modded content (any joker that schedules its own events) breaks
  quiescence. The predicate is generic, but it has not met real mods.
- Audio thread interaction: `G.F_SOUND_THREAD` (`globals.lua:20`) runs sound on
  a `love.thread`. Whether a 1000 fps main loop starves or floods it is unknown.

---

## 16. Ranked experiments to run

Ordered by *how much each unblocks*, not by effort. Every one of these assumes
the owner is not mid-run.

---

### E1 — Does Balatro launch with Steam closed? **(gates CI entirely)**

```powershell
Get-Process steam -ErrorAction SilentlyContinue   # must be empty
$before = (Get-ChildItem "$env:APPDATA\Balatro\Mods\lovely\log").Count
$p = Start-Process -PassThru -WorkingDirectory "C:\Program Files (x86)\Steam\steamapps\common\Balatro" `
     -FilePath "C:\Program Files (x86)\Steam\steamapps\common\Balatro\Balatro.exe"
Start-Sleep 30
$p.HasExited
Get-ChildItem "$env:APPDATA\Balatro\Mods\lovely\log" | Sort LastWriteTime | Select -Last 1
```

- **Reaches the main menu** → Steam not required; CI unblocked; proceed to E2.
- **Exits immediately, new Lovely log exists** → read it; likely a mod error,
  not Steam.
- **Exits immediately, no new Lovely log** → the injector did not load; try
  without Lovely (rename `version.dll` temporarily — *ask first*).
- **Steam client launches** → `steam_appid.txt` not found; check the working
  directory.

Also, in-game, open the Lovely console or check the log for the `LuaSteam`
warning from `main.lua:1014`/`:1018` — its presence confirms the `pcall` path was taken
and the game carried on.

---

### E2 — Can a driver play one blind unattended? **(proves the whole approach)**

Write the minimum driver: speed levers off, no determinism patches, script =
`[select_blind, select 5 cards, play_hand]`. Log the quiescence predicate's
inputs every frame to a file.

- **Works first try** → §8.2 is sufficient for these states; extend.
- **Hangs** → the per-frame trace says which clause never satisfied. Most
  likely candidates, in order: the shop/blind-select state clause (§8.3), a
  queue that never empties because a mod left a per-frame daemon event in it
  (§8.2 condition 4), and `G.CONTROLLER.locks` holding a lock nothing clears.
  Log `pairs(G.E_MANAGER.queues)` counts per queue, not just a boolean.
- **Double-fires** → the one-action-per-settle rule (§7.3) is not being
  honoured; add Handy's blocker-flag pattern.

Run it **20 times** and count failures. Anything above 0/20 means the predicate
is wrong, not flaky — chase it now, because at scale it compounds.

---

### E3 — Confirm the mod-isolation mechanism

```powershell
$env:LOVELY_MOD_DIR = "C:\Users\bkush\Code\BalatroDB\ci\Mods"
# ci\Mods contains only: smods-26.829.0, BalatroDB, BalatroDBDriver
Start-Process ... Balatro.exe
```

Check the Lovely log's loaded-mod list and the in-game Mods screen.

- **Only the three load** → use this everywhere; §13.1 is settled.
- **The env var is ignored** → fall back to `blacklist.txt`, and find Lovely's
  real flag names (`version.dll --help`, or the Lovely GitHub README).

---

### E4 — What actually breaks with no window

Two variants, cheapest first:

1. **Minimize the window at boot** (`love.window.minimize()` from the driver)
   and confirm `love.graphics.isActive()` goes false and the game keeps
   simulating. This is the *useful* result — it is what §4.3 recommends.
2. **Stub `love.draw` only** (no window change) and confirm the normalized log
   is unchanged. This is E8, and it is the real speed win.
3. Only if you want the §4 question closed on the record: Lovely-patch
   `conf.lua` to add `t.window = false`. Expect trouble at
   `love.graphics.newShader` (`game.lua:136`) — which per §4.2b is
   **unguarded**, so it may hard-crash rather than raise a Lua error. Do this
   last, and expect to have to revert the patch.

- **(1) works and logic continues** → headless-proper is dead as a priority;
  close §4.
- **(1) freezes the game** → something in the update path depends on the draw
  path; that is important and feeds E8.

---

### E4b — Is `offscreen` + EGL even available? *(15 minutes, low expected value)*

```powershell
$env:SDL_VIDEODRIVER = "offscreen"
$env:LOVE_GRAPHICS_USE_OPENGLES = "1"
Start-Process -WorkingDirectory "C:\Program Files (x86)\Steam\steamapps\common\Balatro" `
  -FilePath "...\Balatro.exe"
```

- **Boots and simulates** → surprising and excellent; revisit §4 entirely.
- **"No available video device" or similar** → SDL2's Windows build has no
  `offscreen` driver (as `SDL_config_windows.h` suggests). Close the thread.
- **Video init succeeds but GL context creation fails** → EGL is missing; the
  next step would be dropping ANGLE's `libEGL.dll`/`libGLESv2.dll` next to the
  exe. Decide whether that is worth it *before* doing it — per §4.5, probably
  not.

Do this **after** E1–E3, and abandon it the moment it costs more than fifteen
minutes.

---

### E5 — Measure the speed ceiling

Instrument: wall-clock time and frame count from `run.start` to `run.end`, for
one fixed-seed run, across a matrix:

| Variant | `G.FPS_CAP` | `love.draw` | `GAMESPEED` | forced queue |
|---|---|---|---|---|
| baseline | 500 | on | 1 | no |
| A | 1e9 | on | 1 | no |
| B | 1e9 | stub | 1 | no |
| C | 1e9 | stub | 4 | no |
| D | 1e9 | stub | 4 | Handy-style slicing |
| E | 1e9 | stub | 128 | no — `coder/balatrobot`'s `turbo` profile value |

Use `love.graphics.isActive = function() return false end` rather than stubbing
`love.draw` (§6.4), and record the variant in the fixture result so a timing
number is never quoted without its configuration.

Report wall seconds, frames, and frames/second per variant.

- **B is close to A** → the bottleneck is CPU-side update, not rendering; stop
  optimizing the draw path.
- **C ≫ B** → the run is delay-bound; `GAMESPEED` is the main lever (but see
  §6.4's warning about it changing the log).
- **C ≈ B** → the run is frame/animation-bound; only D helps, and D is the
  risky one.
- Record **whether the normalized log differs between variants.** If it does,
  that variant cannot be used for golden tests — and *which* events differ is
  itself a finding.

---

### E6 — Does the exit code escape?

```lua
-- in the driver, immediately after boot
love.event.quit(3)
```
```powershell
Start-Process -Wait ... ; $LASTEXITCODE   # or $p.ExitCode with -PassThru
```

- **3** → the harness can use exit codes.
- **0 always** → the harness must read the result file; make the driver always
  write one, including on the failure paths.

---

### E7 — Is a run reproducible at all?

Run the *same* fixture twice **in one process** (loop via `G.FUNCS.start_run`),
and twice in **two fresh processes**. Normalize (§10.1) and compare all four.

- **All four match** → determinism is in better shape than §9 fears; the RNG
  patches (§9.5 item 4) may be unnecessary, which would be excellent news
  because it removes the "tests run modified code" caveat.
- **Same-process pair matches, cross-process pair does not** → the culprit is
  `G.ID`/`G.sort_id` absolute values leaking somewhere normalization missed, or
  boot-order variation. Diff the two and find the field.
- **Neither pair matches** → a bare-`math.random` site is being hit; bisect by
  logging every `math.random` call (wrap it and record a stack hash) for one
  run and diffing the call sequences. That is the definitive tool and it is
  worth building early.

---

### E7b — Does save/load round-trip the RNG state?

Cheap, and it unlocks a much better fixture story (§14.2). In a live run, at a
known point: `save_run()`, then later load the `.jkr` back through
`G:start_run{savetext = …}` and confirm that `G.GAME.pseudorandom` is
bit-identical and that the *next* shop is identical.

- **Identical** → build fixtures as "restore this snapshot, then run 5
  actions" instead of "play 6 antes, then run 5 actions". Tests get ~50×
  shorter and stop depending on the whole preceding run being reproducible.
- **Not identical** → find which stream drifted; it is likely a key that
  `recursive_table_cull` replaced because it holds an `Object`.

---

### E8 — Is there logic hiding in `love.draw`?

Statically: read `Game:draw` (`game.lua:2875`, and the shader pass at
`:3120-3130`) and
`Card:draw`/`CardArea:draw` for mutations of game state rather than render
state. Dynamically: run E7's fixture with `love.draw` stubbed and unstubbed and
diff the normalized logs.

- **Logs identical** → stubbing draw is safe; it is the best speed lever.
- **Logs differ** → find the mutation. `Card:draw` writing `self.shadow_height`
  (`exe:card.lua:4361`) and `CardArea:draw` reading
  `G.CONTROLLER.dragging.target` (`exe:cardarea.lua:325-361`) are the places to
  look first.

---

### E9 — Joker reordering by permutation

In a live run, with a console: permute `G.jokers.cards` and call
`G.jokers:align_cards()`; observe whether the order sticks across several
frames and whether scoring order follows.

- **Sticks** → §7.6 is the funnel; add a BalatroDB hook so it is logged.
- **Snaps back** → some card had `states.drag.is`, or `align_cards` is not the
  only sorter; check `CardArea:update` and `hard_set_cards`
  (`exe:cardarea.lua:565`).

---

### E10 — Audio/sound-thread behaviour at high frame rates

Only after E5. Run variant D for a full run with `G.F_MUTE = false` and watch
for sound-thread backlog or crashes; then with `SDL_AUDIODRIVER=dummy`.

- If dummy audio works, use it in CI regardless of speed — it removes a whole
  class of container failure.
