"""Pins how the dashboard splits standard play from the whole run, and checks
it against the model it is moving to.

THE MODEL. Every statistic has two figures per run, both counted from the
start of the run:

  standard  the run up to the moment it was won -- run.win, which fires
            before the winning round's cash-out, before a planet used on that
            screen, before the shop. A run that never wins is all standard.
  all       the whole run.

A figure is the max of the statistic over that stretch; for a count, that
is just its last value. There is no "after the win only".

Records are two contests, each a running maximum over runs in order: a
standard record beats every earlier run's STANDARD figure, an all record
every earlier run's ALL figure. Run outcomes (runs, wins, win rate) are not
figures and do not change with the phase. Each statistic has one source:
the logged events, so a run still being played has every figure.

Two parts:

  PINNED     what the code does now, exactly. These must pass. A deliberate
             change moves the pin in the same commit.
  INVARIANT  the model's rules. Where today's code breaks one, the case is in
             KNOWN with the reason, and is reported rather than failed. A
             KNOWN case that starts passing FAILS the suite until its entry
             is deleted, so the list cannot go stale.

The data is tests/phase_fixture.py: seven synthetic runs through the real
ingester, so nothing here depends on anyone's own database.

    python tests/test_phase_semantics.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
sys.path.insert(0, str(ROOT / "tests"))
import dashboard as d                     # noqa: E402
from phase_fixture import build           # noqa: E402

db, R = build()
LETTER = {v: k for k, v in R.items()}

fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


# The phase as the page sends it. The one place that knows the parameter.
PHASES = {"standard": {"endless": "0"}, "all": {}}


def runs(phase, **extra):
    return {LETTER[r["run_id"]]: r for r in d.api_runs(db, dict(PHASES[phase], **extra))}


def summary(phase, **extra):
    return d.api_summary(db, dict(PHASES[phase], **extra))


def col(phase, key):
    return {k: r[key] for k, r in runs(phase).items()}


def records(letter):
    """A run's records as (kind, subject, contest, value) -- hand kinds only;
    the fixture's joker records are the money-driven counters, a separate
    story."""
    out = set()
    for r in d.run_records_for(db, R[letter]):
        if r["kind"].startswith("hand_"):
            out.add((r["kind"], r["subject"], "all" if r["endless"] else "standard",
                     r["value_txt"]))
    return out


# ─── PINNED ──────────────────────────────────────────────────────────────

print("PINNED: the run list")
check("all: furthest ante (runs.furthest_ante; a won run's ante moved on to 9)",
      col("all", "ante") == {"A": 2, "B": 12, "C": 9, "D": 1, "E": 1, "F": None, "G": 1})
check("all: hands played (the game's own count, from run.end)",
      col("all", "hands_played") == {"A": 3, "B": 8, "C": 2, "D": 1, "E": 0, "F": None, "G": 1})
check("all: best hand",
      col("all", "best_hand") == {"A": "2000", "B": "9000000000", "C": "40000",
                                  "D": "1000000", "E": None, "F": "800", "G": "3500"})
check("all: peak money",
      col("all", "peak_money") == {"A": 14, "B": 1076, "C": 19, "D": 9, "E": None,
                                   "F": 19, "G": 9})
check("standard keeps every run", set(runs("standard")) == set(R))
check("standard: B's ante is the max of its pre-win rounds (1, 5, 4, 8), not the last",
      col("standard", "ante")["B"] == 8)
check("standard: B's hands and best are its pre-win ones",
      (col("standard", "hands_played")["B"], col("standard", "best_hand")["B"]) == (4, "50000"))
check("standard: B's peak money includes the shop after the win (the latch boundary)",
      col("standard", "peak_money")["B"] == 529)

print("PINNED: the summary")
s_all, s_std = summary("all"), summary("standard")
tiles = ("runs", "won", "win_pct", "best_hand", "max_money", "max_ante", "max_cashout")
check("all", tuple(s_all[k] for k in tiles) == (7, 3, 42.9, "9000000000", 1076, 12, 20),
      str(tuple(s_all[k] for k in tiles)))
check("standard", tuple(s_std[k] for k in tiles) == (7, 3, 42.9, "1000000", 529, 9, 10),
      str(tuple(s_std[k] for k in tiles)))

print("PINNED: records")
check("B: the pre-win rows are standard, the post-win ones survive as 'endless'",
      records("B") == {("hand_played", "Pair", "standard", "3"),
                       ("hand_score", "Flush", "standard", "50000"),
                       ("hand_score", "Flush", "all", "9000000000"),
                       ("hand_score", "Pair", "standard", "3000"),
                       ("hand_score", "Pair", "all", "4000")}, str(sorted(records("B"))))

print("PINNED: sorting")
check("sort=money orders by final dollars",
      [LETTER[r["run_id"]] for r in d.api_runs(db, {"sort": "money"})][:3] == ["B", "C", "A"])


# ─── INVARIANTS ──────────────────────────────────────────────────────────

def figure_le(key):
    def holds():
        std, al = col("standard", key), col("all", key)
        bad = [k for k in std if std[k] is not None and al.get(k) is not None
               and float(std[k]) > float(al[k])]
        return not bad, f"standard above all for {bad}"
    return holds


def tiles_match_list():
    bad = []
    for p in PHASES:
        s, rl = summary(p), list(runs(p).values())
        for tile, key in (("max_money", "peak_money"), ("max_ante", "ante")):
            vals = [r[key] for r in rl if r[key] is not None]
            want = max(vals) if vals else None
            if s[tile] != want:
                bad.append(f"{p}.{tile}={s[tile]} vs list max {want}")
    return not bad, "; ".join(bad)


def outcomes_ignore_phase():
    got = {p: (summary(p)["runs"], summary(p)["won"], summary(p)["win_pct"]) for p in PHASES}
    return len(set(got.values())) == 1, f"{got}"


def standard_money_ends_at_win():
    # B's balance was $19 when run.win fired. Its $10 cash-out and the $500
    # in the shop came after.
    got = col("standard", "peak_money")["B"]
    return got == 19, f"B standard peak money {got}"


def standard_cashout_ends_at_win():
    # Every pre-win cash-out in the fixture is $5; the winning ones ($10) are
    # paid after run.win.
    got = summary("standard")["max_cashout"]
    return got == 5, f"standard biggest cash-out {got}"


def ante_is_highest_played():
    # C won at ante 8 and stopped. The game moves the ante on to 9 at the win,
    # but no round of ante 9 was ever played.
    got = (col("standard", "ante")["C"], col("all", "ante")["C"])
    return got == (8, 8), f"C ante (standard, all) {got}"


def live_run_has_figures():
    got = (col("standard", "ante")["F"], col("all", "ante")["F"],
           col("standard", "hands_played")["F"], col("all", "hands_played")["F"])
    return got == (8, 9, 1, 3), f"F (standard ante, all ante, standard hands, all hands) {got}"


def hands_from_events():
    counted = {LETTER[rid]: n for rid, n in db.execute(
        "SELECT r.run_id, (SELECT COUNT(*) FROM hands h WHERE h.run_id = r.run_id) FROM runs r")}
    got = col("all", "hands_played")
    bad = {k: (got[k], counted[k]) for k in got if got[k] != counted[k]}
    return not bad, f"(shown, logged) {bad}"


def reference_records():
    """Hand records the model's way, straight from the hands table: per run a
    standard and an all figure, then a running max per contest."""
    want = {}
    figs = {}
    for rid, hand, endless, ord_, txt in db.execute(
            "SELECT run_id, hand, endless, score_ord, score_txt FROM hands"):
        for contest in ("standard", "all"):
            if contest == "standard" and endless:
                continue
            k = (rid, hand, contest)
            f = figs.setdefault(k, {"score": (None, None), "played": 0})
            if f["score"][0] is None or ord_ > f["score"][0]:
                f["score"] = (ord_, txt)
            f["played"] += 1
    order = [rid for (rid,) in db.execute("SELECT run_id FROM runs ORDER BY started_ts")]
    best = {}
    for rid in order:
        for (r2, hand, contest), f in sorted(figs.items()):
            if r2 != rid:
                continue
            for kind, val, txt in (("hand_score", f["score"][0], f["score"][1]),
                                   ("hand_played", f["played"], str(f["played"]))):
                k = (kind, hand, contest)
                if k not in best or val > best[k]:
                    best[k] = val
                    want.setdefault(LETTER[rid], set()).add((kind, hand, contest, txt))
    return want


def records_are_two_contests():
    want = reference_records()
    bad = {k: (sorted(records(k)), sorted(want.get(k, set())))
           for k in R if records(k) != want.get(k, set())}
    return not bad, "; ".join(f"{k}: got {g} want {w}" for k, (g, w) in sorted(bad.items()))


def standard_record_against_standard_only():
    # G's pre-win Pair (3,500) beats every earlier standard Pair but not B's
    # all-figure 4,000: a standard record, not an all record.
    got = {(c, v) for (k, s, c, v) in records("G") if k == "hand_score" and s == "Pair"}
    return got == {("standard", "3500")}, f"G's Pair records {got}"


def money_sort_matches_column():
    # Compared by value, so runs tied on peak money may come in either order.
    order = [LETTER[r["run_id"]] for r in d.api_runs(db, {"sort": "money"})]
    peaks = col("all", "peak_money")
    shown = [peaks[k] if peaks[k] is not None else -1 for k in order]
    return shown == sorted(shown, reverse=True), f"sorted {order}: peak money {shown}"


def latch_monotone():
    bad = []
    for table, order in (("hands", "seg, n"), ("money", "seg, n"), ("rounds", "round_seq")):
        for (rid,) in db.execute(f"SELECT DISTINCT run_id FROM {table}"):
            seq = [e for (e,) in db.execute(
                f"SELECT endless FROM {table} WHERE run_id = ? ORDER BY {order}", (rid,))]
            if any(a == 1 and b == 0 for a, b in zip(seq, seq[1:])):
                bad.append(f"{table}:{LETTER[rid]}")
    return not bad, f"standard after endless in {bad}"


def narrowing_never_raises():
    bad = []
    for p in PHASES:
        wide, narrow = summary(p), summary(p, deck="b_ghost")
        for tile in ("max_money", "max_ante", "max_cashout"):
            if narrow[tile] is not None and wide[tile] is not None and narrow[tile] > wide[tile]:
                bad.append(f"{p}.{tile}")
    return not bad, f"a narrower slice raised {bad}"


INVARIANTS = [
    ("standard best hand <= all", figure_le("best_hand")),
    ("standard peak money <= all", figure_le("peak_money")),
    ("standard ante <= all", figure_le("ante")),
    ("standard hands played <= all", figure_le("hands_played")),
    ("summary tiles = max over the run list", tiles_match_list),
    ("run outcomes do not change with the phase", outcomes_ignore_phase),
    ("standard money stops at run.win", standard_money_ends_at_win),
    ("standard cash-outs stop at run.win", standard_cashout_ends_at_win),
    ("ante is the highest ante played", ante_is_highest_played),
    ("a live run has every figure", live_run_has_figures),
    ("hands played comes from the logged hands", hands_from_events),
    ("records are two contests over per-run figures", records_are_two_contests),
    ("a standard record is judged against standard figures only",
     standard_record_against_standard_only),
    ("sort=money orders by the money the column shows", money_sort_matches_column),
    ("the latch never turns back off", latch_monotone),
    ("narrowing the slice never raises a maximum", narrowing_never_raises),
]

KNOWN = {
    "standard money stops at run.win":
        "the boundary is the latch, which flips at the next blind select",
    "standard cash-outs stop at run.win":
        "same boundary; the winning cash-out is paid between run.win and the latch",
    "ante is the highest ante played":
        "the ante comes from runs.furthest_ante, which the win moves to 9",
    "a live run has every figure":
        "ante and hands come from run.end, which a run in progress lacks; and a "
        "standard figure is only split out for runs marked went_endless",
    "hands played comes from the logged hands":
        "the all figure is run.end's hands_played, absent for a live run",
    "records are two contests over per-run figures":
        "records are stored per disjoint part (pre-win / post-win) and "
        "endless_union() keeps the larger, so a pre-win value is never also an "
        "all record and no record holds a count's whole total",
    "sort=money orders by the money the column shows":
        "the sort reads final_dollars; the column shows peak money",
}

print("\nINVARIANTS")
for name, holds in INVARIANTS:
    ok, detail = holds()
    if name in KNOWN:
        if ok:
            fails += 1
            print(f"  FAIL {name}  -- now holds: remove it from KNOWN")
        else:
            print(f"  known {name}\n         {detail}\n         why: {KNOWN[name]}")
    else:
        check(name, ok, detail)

stale = set(KNOWN) - {n for n, _ in INVARIANTS}
check("every KNOWN entry names an invariant", not stale, f"{sorted(stale)}")

print("\nFAILURES:", fails)
raise SystemExit(1 if fails else 0)
