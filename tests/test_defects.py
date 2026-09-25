"""Exercises the capture-defect sniffing in derive().

The defect set is the only thing standing between a log that lost events and
a dashboard that reports its numbers as fact, so a false negative hides real
damage and a false positive trains you to ignore the warning. Both have
happened, which is why this file exists.

The shapes pinned here:

  * a clean resume raises nothing -- the destruction of a card that existed
    before the resume is recorded, which is what `bdb_owned` is for;
  * a resume that LOSES those destructions raises deck_identity_fail;
  * a run suspended and then continued, still being played, is `no_run_end`
    and NOT an identity failure -- its only run.end describes a state the
    run has since moved past.

    python tests/test_defects.py
"""
import json
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from ingest import Ingester                                # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


def card(i):
    return {"id": i, "key": "c_base", "rank": "2", "suit": "Spades"}


def deck(n):
    return [card(i) for i in range(n)]


class Log:
    """A synthetic run, written as the mod would write it."""

    def __init__(self):
        self.events, self.n, self.seg = [], 0, 0

    def add(self, e, d=None):
        self.events.append({"v": 1, "e": e, "n": self.n, "seg": self.seg,
                            "t": float(self.n), "d": d or {}})
        self.n += 1
        return self

    def resume(self, seg, size):
        self.seg, self.n = seg, 0
        self.add("run.resume")
        return self.add("run.rebaseline", {"deck_cards": deck(size), "dollars": 10})

    def defects(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = pathlib.Path(tmp) / "1700000000-TESTSEED-0000.jsonl"
            p.write_text("".join(json.dumps(e) + "\n" for e in self.events),
                         encoding="utf-8")
            db = sqlite3.connect(":memory:")
            db.executescript((ROOT / "ingest" / "schema.sql").read_text(encoding="utf-8"))
            Ingester(db).ingest_file(str(p))
            return {r[0] for r in db.execute("SELECT defect FROM run_defects")}


def opened(size=52):
    return (Log()
            .add("run.start", {"deck": "Red Deck", "deck_key": "b_red", "stake": 1,
                               "stake_key": "stake_white", "seed": "TESTSEED",
                               "ts": 1700000000, "starting_deck_size": size})
            .add("run.baseline", {"deck_cards": deck(size), "dollars": 4}))


# A complete, current-format run.end. Every field here is one the sniffer
# looks for, so leaving any out would raise a legacy defect and drown the
# one this file is about.
ENDED = {"result": "died", "terminal": True, "won": False, "endless": False,
         "ante": 3, "round": 5, "hands_played": 0, "skips": 0,
         "best_hand": 1000, "final_round_score": 500,
         "furthest_ante": 3, "furthest_round": 5}


print("a resume that records its destructions is clean")
# Two cards that existed before the resume are destroyed after it. This is
# the case the bdb_owned fix exists to make possible: Card:remove gates on a
# flag the resume path never used to set, so these two went unlogged and the
# arithmetic below could not close.
lg = opened(52)
lg.add("run.end", dict(ENDED, result="suspended", terminal=False, deck_size=52))
lg.resume(1700009999, 52)
lg.add("card.remove", {"card": card(7), "reason": "destroyed"})
lg.add("card.remove", {"card": card(9), "reason": "destroyed"})
lg.add("run.end", dict(ENDED, deck_size=50))
d = lg.defects()
check("no defects at all", d == set(), str(d))

print("\na resume that loses them is caught")
lg = opened(52)
lg.add("run.end", dict(ENDED, result="suspended", terminal=False, deck_size=52))
lg.resume(1700009999, 52)
# ...the two card.remove events are simply absent, which is exactly what the
# bug produced: shop.sell and the rest still fired, so nothing else looked
# wrong.
lg.add("run.end", dict(ENDED, deck_size=50))
d = lg.defects()
check("deck_identity_fail", "deck_identity_fail" in d, str(d))

print("\na run suspended and then continued is live, not broken")
# The file HAS a run.end -- the suspend -- so `no_run_end` would not fire on
# presence alone, and the stale deck_size made the identity compare a
# pre-resume final against a post-resume baseline. 52 + 0 - 2 = 50 was
# checked against the 52 recorded when the save was put down, and every
# resumed-and-still-playing run reported a capture defect it did not have.
lg = opened(52)
lg.add("run.end", dict(ENDED, result="suspended", terminal=False, deck_size=52))
lg.resume(1700009999, 52)
lg.add("card.remove", {"card": card(7), "reason": "destroyed"})
lg.add("card.remove", {"card": card(9), "reason": "destroyed"})
# ...and no run.end: the run is still being played.
d = lg.defects()
check("not an identity failure", "deck_identity_fail" not in d, str(d))
check("reported as still live", "no_run_end" in d, str(d))

print("\na run that simply never finished is still flagged")
lg = opened(52)
lg.add("card.remove", {"card": card(7), "reason": "destroyed"})
d = lg.defects()
check("no_run_end", "no_run_end" in d, str(d))
check("and no identity claim without a final size",
      "deck_identity_fail" not in d, str(d))

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
