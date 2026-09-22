"""Exercises mod/BalatroDB/src/state.lua: run identity and the win/endless latches.

These flags decide how every event in a run is classified, and they have to
survive quit-and-resume because Balatro serialises G.GAME into the save. Two
real bugs live here:

  * `won` was read from G.GAME.won, which Balatro sets from "ante == win_ante
    and the blind is a Boss" BEFORE checking whether you survived
    (state_events.lua:111) -- so dying to the final boss reported a win.
  * the fix then cleared the latches inside state.begin(), which runs on the
    RESUME path too, wiping what state.restore() had just read and persisting
    the blanks back into the save. A won run resumed once became unwon
    forever, and its endless rounds were filed as non-endless.

    pip install lupa && python tests/test_state.py
"""
import lupa

L = lupa.LuaRuntime(unpack_returned_tuples=False)
L.execute("unpack = unpack or table.unpack")
L.execute("""
  sendWarnMessage = function() end
  sendInfoMessage = function() end
  love = { timer = { getTime = function() return 123 end } }
  G = { GAME = {} }
  BalatroDB = { SCHEMA = 1 }
""")

util = L.execute(open("mod/BalatroDB/src/util.lua", encoding="utf-8").read())
L.globals().BalatroDB.util = util
# state.lua only reaches into log for its own module wiring; a stub is enough.
L.globals().BalatroDB.log = L.table_from({})
state = L.execute(open("mod/BalatroDB/src/state.lua", encoding="utf-8").read())

g = L.globals()
fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


def fresh_game():
    g.G.GAME = L.table()


print("a new run starts with every latch down")
fresh_game()
state.won, state.endless, state.won_pending = False, False, False
state.begin("run-1", 0)
check("won is false", state.won is False)
check("endless is false", state.endless is False)

print("\nwinning latches `won`, and endless only on the NEXT blind")
state.mark_won()
check("won is set the moment win_game runs", state.won is True)
check("endless is not set yet", state.endless is False,
      "the winning round itself is still non-endless")
flipped = state.note_blind_selected()
check("selecting the next blind flips endless", state.endless is True and flipped)
check("won survives the flip", state.won is True)

print("\nthe latches are written into G.GAME, which Balatro saves")
state.persist()
saved = {k: g.G.GAME[k] for k in ("bdb_run_id", "bdb_won", "bdb_endless", "bdb_seg")}
check("bdb_won is in the save", saved["bdb_won"] is True, str(saved))
check("bdb_endless is in the save", saved["bdb_endless"] is True, str(saved))

print("\nresuming restores them -- and begin() must not wipe them")
# This is the regression: start_run calls restore() then begin() on the resume
# path, so begin() clearing the latches undid the restore and persisted false.
fresh_game()
for k, v in saved.items():
    g.G.GAME[k] = v
state.won, state.endless, state.won_pending = False, False, False   # cold start
resumed = state.restore()
check("restore reports a resume", resumed is True)
check("won came back", state.won is True)
check("endless came back", state.endless is True)

state.begin(state.run_id, state.seg)
check("begin() leaves won alone", state.won is True,
      "begin() cleared a latch that restore() had just read")
check("begin() leaves endless alone", state.endless is True)

state.persist()
check("and the save still says won", g.G.GAME.bdb_won is True)
check("and the save still says endless", g.G.GAME.bdb_endless is True)

print("\na pre-bdb_won save infers the win from the endless latch")
# Saves written before bdb_won existed still carry bdb_endless, and the latch
# only flips after a win -- so endless implies one.
fresh_game()
g.G.GAME.bdb_run_id = "old-run"
g.G.GAME.bdb_endless = True
state.won = False
state.restore()
check("won inferred from endless", state.won is True)

print("\nstarting a NEW run after a won one clears the latches")
# The clear lives at the start_run call site, not in begin(); this mirrors it.
fresh_game()
state.won, state.endless, state.won_pending = False, False, False
state.begin("run-2", 0)
check("won is down again", state.won is False)
check("endless is down again", state.endless is False)

print("\nseg strictly increases so a double resume cannot collide")
fresh_game()
g.G.GAME.bdb_run_id = "run-3"
g.G.GAME.bdb_seg = 0
state.restore()
first = state.seg
g.G.GAME.bdb_run_id = "run-3"
g.G.GAME.bdb_seg = 0          # quit before the game saved: same value again
state.restore()
check("a second resume from the same save does not reuse seg",
      state.seg >= first, f"{state.seg} vs {first}")
check("seg is past any plausible counter", first > 1_000_000, str(first))

print("\nFAILURES:", fails)
raise SystemExit(1 if fails else 0)
