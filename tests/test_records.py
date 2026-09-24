"""Exercises derive_records(): what each run was FIRST to achieve.

Order-dependent and cross-run, which makes it easy to get subtly wrong and
impossible to notice by looking at one run. The rules being pinned:

  * the run that first reaches a value keeps the moment, even after a later
    run beats it -- that is the whole point of "at the time";
  * a later run that merely EQUALS it does not take it away;
  * endless and non-endless are separate contests, so the same run can hold
    one of each for the same subject with different values;
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

# Endless is its own contest: A's endless best is lower than B's
# non-endless best, and must still count as an endless record.
hand("A", "Pair", 2000, el=1)
hand("B", "Pair", 1500, el=1)    # lower than A's endless -> no record

Ingester(db).derive_records()

got = {(r[0], r[1], r[2], r[3]): r[4] for r in db.execute(
    "SELECT run_id, kind, subject, endless, value_txt FROM run_records")}

print("the first run to reach a value keeps the moment")
check("A holds the early Pair", got.get(("A", "hand_score", "Pair", 0)) == "1000")
check("B holds it once it beats A", got.get(("B", "hand_score", "Pair", 0)) == "5000")

print("\nequalling a record does not take it")
check("C gets nothing for matching B", ("C", "hand_score", "Pair", 0) not in got,
      str([k for k in got if k[0] == "C"]))

print("\nbig numbers compare on the ordering key, not as text")
# "8293927041" < "998" as a string; only the ord makes B the record holder.
check("A holds the small Flush", got.get(("A", "hand_score", "Flush", 0)) == "998")
check("B holds the huge Flush",
      got.get(("B", "hand_score", "Flush", 0)) == "8293927041",
      str(got.get(("B", "hand_score", "Flush", 0))))

print("\nendless is a separate contest")
check("A's endless Pair is its own record",
      got.get(("A", "hand_score", "Pair", 1)) == "2000")
check("B does not take it with a lower endless value",
      ("B", "hand_score", "Pair", 1) not in got)
check("and A holds both contests at once for the same hand",
      got.get(("A", "hand_score", "Pair", 0)) == "1000"
      and got.get(("A", "hand_score", "Pair", 1)) == "2000")

print("\nrederiving is idempotent")
before = sorted(db.execute("SELECT * FROM run_records").fetchall())
Ingester(db).derive_records()
after = sorted(db.execute("SELECT * FROM run_records").fetchall())
check("same rows on a second pass", before == after,
      f"{len(before)} -> {len(after)}")

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
