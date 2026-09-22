"""Exercises classify_money() -- which action earned each dollar.

`money.change` records only that the balance moved, never why. The cause has
to be recovered from position in the event stream, and the direction is not
uniform, which is exactly why this is worth pinning down:

  * a PLAY emits its money from inside evaluate_play, whose AFTER hook emits
    hand.play -- the money arrives just BEFORE the play, in the same frame.
  * a DISCARD's jokers fire from queued events that run after the discard
    function has returned -- the money arrives AFTER it, a beat later.

Both shapes are taken from real logs. Getting the direction wrong is silent:
the dashboard still shows a number, just on the wrong row.

    python tests/test_money_cause.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "ingest"))
from ingest import classify_money          # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


def ev(e, n, t, seg=0):
    return {"e": e, "n": n, "t": t, "seg": seg}


print("a play's money lands in the same frame, just before hand.play")
# Taken from 1789971878-S29CRBP1-c3b3 n=830..833: joker.scale, two money
# changes and the hand.play all share t=1067.41.
stream = [
    ev("hand.discard", 822, 1053.10),
    ev("state.change", 829, 1066.95),
    ev("joker.scale", 830, 1067.41),
    ev("money.change", 831, 1067.41),
    ev("money.change", 832, 1067.41),
    ev("hand.play", 833, 1067.41),
]
c = classify_money(stream)
check("both changes belong to the play",
      c[(0, 831)] == ("hand.play", 833) and c[(0, 832)] == ("hand.play", 833), c)
check("the earlier discard does not claim them",
      c[(0, 831)][1] != 822, c)

print("\na discard's money lands after it, a beat later")
# From 1790048876-X5DIHJ46-80b1 n=258..260: a discard holding three face cards
# (Faceless Joker) pays $5 at t+0.22, through a queued event.
stream = [
    ev("hand.discard", 258, 452.97),
    ev("state.change", 259, 452.98),
    ev("money.change", 260, 453.19),
    ev("hand.discard", 263, 462.03),
]
c = classify_money(stream)
check("attributed to the discard it followed",
      c[(0, 260)] == ("hand.discard", 258), c)
check("not to the later discard", c[(0, 260)][1] != 263, c)

print("\nthe cash out is not an action's money")
stream = [
    ev("hand.play", 300, 505.94),
    ev("state.change", 302, 510.39),
    ev("round.end", 303, 511.64),
    ev("money.change", 304, 513.41),
]
c = classify_money(stream)
check("cash out owns no action", c[(0, 304)] == ("round.end", None), c)

print("\nselling a joker mid-round is the sell's money, not the last discard's")
# A sell can happen at any time, so a bare "the last action before it" rule
# hands the seller's dollars to whichever discard came first.
stream = [
    ev("hand.discard", 620, 700.00),
    ev("shop.sell", 641, 769.08),
    ev("money.change", 642, 769.24),
]
c = classify_money(stream)
check("attributed to the sell", c[(0, 642)] == ("shop.sell", None), c)

print("\nsegments are independent: n and t both restart on resume")
stream = [
    ev("hand.discard", 5, 900.00, seg=0),
    ev("money.change", 9, 12.00, seg=1),
    ev("hand.play", 10, 12.00, seg=1),
]
c = classify_money(stream)
check("seg 1 money finds its own seg's play",
      c[(1, 9)] == ("hand.play", 10), c)
check("seg 0 contributes nothing", (0, 9) not in c, c)

print("\na discard's window does not stretch to the next player action")
# Same shape as the queued case, but seconds later -- that is the player doing
# something else, not the discard resolving.
stream = [
    ev("hand.discard", 100, 100.00),
    ev("consumable.use", 104, 140.00),
    ev("money.change", 105, 140.30),
]
c = classify_money(stream)
check("late money is not the discard's", c[(0, 105)] == ("consumable.use", None), c)

# ── the win flag ─────────────────────────────────────────────────────────
# Kept here rather than in its own file because it is the same shape of bug:
# a game field that looks authoritative and is not.
print("\na death on the win-ante boss is not a win")
# Balatro sets G.GAME.won at state_events.lua:111 from "ante == win_ante and
# the blind is a Boss", three lines BEFORE it checks game_over -- so dying to
# the final boss sets it too. Two real runs in the corpus were marked won.
import re                                            # noqa: E402
ing = (pathlib.Path(__file__).resolve().parent.parent
       / "ingest/ingest.py").read_text(encoding="utf-8")
check("ingest takes `won` from run.win, not run.end.won",
      re.search(r"won\s*=\s*1\s+if\s+saw_win\s+else\s+0", ing) is not None)
check("the UPDATE writes that derived value, not end.get('won')",
      "(won, end.get(\"result\")" in ing, "still writing end.get('won')")

st = (pathlib.Path(__file__).resolve().parent.parent
      / "mod/BalatroDB/src/state.lua").read_text(encoding="utf-8")
hk = (pathlib.Path(__file__).resolve().parent.parent
      / "mod/BalatroDB/src/hooks.lua").read_text(encoding="utf-8")
check("the mod latches its own win flag in mark_won", "state.won = true" in st)
check("run.end reports that flag, not G.GAME.won",
      "won     = state.won," in hk and "won     = game.won" not in hk)
check("a new run clears it",
      "state.won, state.won_pending, state.endless = false, false, false" in st)

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
