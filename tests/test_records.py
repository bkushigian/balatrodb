"""Exercises derive_records(): what each run was FIRST to achieve.

Order-dependent and cross-run, which makes it easy to get subtly wrong and
impossible to notice by looking at one run. The rules being pinned:

  * the run that first reaches a value keeps the moment, even after a later
    run beats it -- that is the whole point of "at the time";
  * a later run that merely EQUALS it does not take it away;
  * standard and all are separate contests over figures that both start at
    the beginning of the run: standard up to the win, all the whole run --
    so a pre-win value counts in both, and a standard record never has to
    beat an all figure;
  * comparison is on the ordering key, never the text.

    python tests/test_records.py
"""
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from ingest import Ingester, ord_num                       # noqa: E402

# ord_num returns the whole (ord, num, txt) triple.
def ordk(v):
    return ord_num(v)[0]


db = sqlite3.connect(":memory:")
db.executescript((ROOT / "ingest" / "schema.sql").read_text(encoding="utf-8"))

fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


def run(rid, ts):
    db.execute("INSERT INTO runs (run_id, log_file, started_ts) VALUES (?,?,?)",
               (rid, rid + ".jsonl", ts))


def hand(rid, h, score, el=0, n=[0]):
    n[0] += 1
    db.execute("INSERT INTO hands (run_id, seg, n, endless, hand, score_ord,"
               " score_num, score_txt) VALUES (?,0,?,?,?,?,?,?)",
               (rid, n[0], el, h, ordk(score), float(score), str(score)))


# Three runs in a known order. B beats A, C only equals B.
run("A", 100)
run("B", 200)
run("C", 300)

hand("A", "Pair", 1000)
hand("B", "Pair", 5000)
hand("C", "Pair", 5000)          # equal, not greater

# A huge value that sorts below a small one as TEXT.
hand("A", "Flush", 998)
hand("B", "Flush", 8293927041)

# After the win. A's all figure for Pair is then 2000; B's is still its
# pre-win 5000 -- the larger of everything it did -- even though its
# post-win Pair is only 1500.
hand("A", "Pair", 2000, el=1)
hand("B", "Pair", 1500, el=1)

Ingester(db).derive_records()

got = {(r[0], r[1], r[2], r[3]): r[4] for r in db.execute(
    "SELECT run_id, kind, subject, contest, value_txt FROM run_records")}
S, ALL = "standard", "all"

print("the first run to reach a value keeps the moment")
check("A holds the early Pair", got.get(("A", "hand_score", "Pair", S)) == "1000")
check("B holds it once it beats A", got.get(("B", "hand_score", "Pair", S)) == "5000")

print("\nequalling a record does not take it")
check("C gets nothing for matching B", ("C", "hand_score", "Pair", S) not in got,
      str([k for k in got if k[0] == "C"]))

print("\nbig numbers compare on the ordering key, not as text")
# "8293927041" < "998" as a string; only the ord makes B the record holder.
check("A holds the small Flush", got.get(("A", "hand_score", "Flush", S)) == "998")
check("B holds the huge Flush",
      got.get(("B", "hand_score", "Flush", S)) == "8293927041",
      str(got.get(("B", "hand_score", "Flush", S))))

print("\nstandard and all are two contests")
check("A's pre-win Pair (1000) is a standard record", got.get(("A", "hand_score", "Pair", S)) == "1000")
check("A's all figure (2000, after the win) is an all record",
      got.get(("A", "hand_score", "Pair", ALL)) == "2000")
check("B's all figure is its pre-win 5000, which beats A's 2000",
      got.get(("B", "hand_score", "Pair", ALL)) == "5000", str(got.get(("B", "hand_score", "Pair", ALL))))
check("a pre-win value is in both contests: B's Flush",
      got.get(("B", "hand_score", "Flush", S)) == got.get(("B", "hand_score", "Flush", ALL))
      == "8293927041")
check("plays are counted over the whole run for all: A played Pair twice",
      got.get(("A", "hand_played", "Pair", ALL)) == "2"
      and got.get(("A", "hand_played", "Pair", S)) == "1")

print("\nrederiving is idempotent")
before = sorted(db.execute("SELECT * FROM run_records").fetchall())
Ingester(db).derive_records()
after = sorted(db.execute("SELECT * FROM run_records").fetchall())
check("same rows on a second pass", before == after,
      f"{len(before)} -> {len(after)}")

# -- counter jokers reach records at all -----------------------------------
# They have no joker_scale rows -- their value is a function of run history,
# not of their own ability fields -- so they arrive through
# joker_counter_peaks instead. That path has now silently produced nothing
# TWICE: once when the feature was written, and again when the peaks table
# was rekeyed from the counter to the joker while the lookup here kept using
# the old key. Both times the symptom was zero rows, which reads exactly
# like "no run happened to set one".
print("\ncounter jokers set records too")
db.execute("DELETE FROM run_records")
for rid, ts in (("K1", 100), ("K2", 200)):
    db.execute("INSERT INTO runs (run_id, log_file, started_ts) VALUES (?,?,?)",
               (rid, rid + ".jsonl", ts))
peak = ("INSERT INTO joker_counter_peaks (run_id, joker_key, metric, endless,"
        " field, contributed_value, ambient_value) VALUES (?,?,?,0,?,?,?)")
#                                                   held   not held
db.execute(peak, ("K1", "j_bull", "dollars", "chips", 100.0, 200.0))
db.execute(peak, ("K2", "j_bull", "dollars", "chips", 300.0, 150.0))
# Supernova is held_only: no ambient reading exists for it.
db.execute(peak, ("K1", "j_supernova", "hand_plays", "mult", 40.0, None))
# Steel Joker at X1 is holding nothing; an inert value is not an achievement.
db.execute(peak, ("K2", "j_steel_joker", "steel_cards", "x_mult", 1.0, 1.0))

Ingester(db).derive_records()
recs = {(r[0], r[1], r[2]): r[3] for r in db.execute(
    "SELECT run_id, subject, held, value_txt FROM run_records"
    " WHERE held IS NOT NULL")}

check("a counter joker reaches run_records at all", len(recs) > 0, str(recs))
check("the held reading is its own contest",
      recs.get(("K1", "j_bull", 1)) == "100"
      and recs.get(("K2", "j_bull", 1)) == "300", str(recs))
check("the not-held reading is a separate one, won by the other run",
      recs.get(("K1", "j_bull", 0)) == "200"
      and ("K2", "j_bull", 0) not in recs, str(recs))
check("a held_only joker offers no not-held record",
      ("K1", "j_supernova", 1) in recs
      and ("K1", "j_supernova", 0) not in recs, str(recs))
check("an inert X1 is not a record",
      not any(k[1] == "j_steel_joker" for k in recs), str(recs))

db.execute("DELETE FROM joker_counter_peaks")
db.execute("DELETE FROM runs")
db.execute("DELETE FROM run_records")

print("\na paused run is over once a later run overwrites the save")
# Balatro keeps ONE save per profile, so "suspended" means resumable only
# until something else starts. Nothing can log that moment -- the paused run
# stopped writing when it was suspended -- so it is read across runs, which
# makes both the profile and the ordering load-bearing.
#
# Last in this file because it clears the runs table, which everything above
# it depends on.
db.execute("DELETE FROM runs")


def prun(rid, ts, result, profile):
    db.execute("INSERT INTO runs (run_id, log_file, started_ts, result, profile)"
               " VALUES (?,?,?,?,?)", (rid, rid + ".jsonl", ts, result, profile))


prun("P1", 100, "suspended", 1)     # a later run on profile 1 displaces it
prun("P2", 200, "died", 1)
prun("Q1", 150, "suspended", 2)     # a different profile, its own save slot
prun("Q2", 250, "died", 2)
prun("N1", 300, "died", 1)          # never suspended, never abandoned
prun("P3", 400, "suspended", 1)     # the newest run of all: still resumable

Ingester(db).derive_abandoned()
ab = {r[0]: r[1] for r in db.execute("SELECT run_id, abandoned FROM runs")}

check("a later run on the same profile ends it", ab["P1"] == 1, str(ab))
check("the newest paused run is still resumable", ab["P3"] == 0, str(ab))
check("each profile has its own save slot", ab["Q1"] == 1, str(ab))
check("a run that died is never abandoned",
      ab["P2"] == 0 and ab["N1"] == 0, str(ab))

# Strictly later, so a run never displaces itself.
db.execute("DELETE FROM runs")
prun("S1", 500, "suspended", 1)
Ingester(db).derive_abandoned()
check("a lone paused run stands",
      db.execute("SELECT abandoned FROM runs").fetchone()[0] == 0)

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
