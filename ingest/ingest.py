"""Fold BalatroDB JSONL run logs into SQLite.

The gzipped logs are the system of record; this database is a derived index
and `--rebuild` regenerates it. See docs/db-schema.md for the design.

    python ingest/ingest.py [--db PATH] [--logs DIR] [--rebuild] [--report]
"""
import argparse
import glob
import json
import math
import os
import sqlite3
import sys
import time

DEFAULT_LOGS = os.path.expandvars(r"%APPDATA%\Balatro\BalatroDB\runs")
DEFAULT_DB = os.path.join(os.path.dirname(__file__), "balatro.db")

SCHEMA = open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8").read()


def ord_num(v):
    """(ord, num, txt) for a wire value: a plain number, or {"s","l"}, or None.

    ord is sign(x)*log10(1+|x|) -- monotonic across the whole real line, so it
    orders plain numbers and out-of-range ones against each other correctly.
    MAX() over the exact text would be lexicographic and silently wrong.
    """
    if v is None:
        return None, None, None
    if isinstance(v, dict):
        s = v.get("s")
        l = v.get("l")
        if l is None:                       # nan, or an unparseable big number
            return None, None, str(s)
        return float(l), None, str(s)
    if isinstance(v, bool):
        return None, None, str(v)
    if isinstance(v, (int, float)):
        x = float(v)
        sign = -1.0 if x < 0 else 1.0
        return sign * math.log10(1.0 + abs(x)), x, repr(v)
    return None, None, str(v)


def triple(prefix, v):
    o, n, t = ord_num(v)
    return {prefix + "_ord": o, prefix + "_num": n, prefix + "_txt": t}


def as_int(v):
    """Bounded game quantities: dollars, ante, deck size. None if out of range."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def card_row(run, seg, n, role, pos, c, ante, el, event):
    if not isinstance(c, dict):
        return None
    return (
        run, seg, n, role, pos, ante, el, event,
        as_int(c.get("id")), c.get("key"), c.get("name"), c.get("set"),
        c.get("rank"), c.get("suit"), c.get("enhancement"), c.get("edition"),
        c.get("seal"),
        json.dumps(c["stickers"]) if c.get("stickers") else None,
        as_int(c.get("sell_cost")),
        json.dumps(c["state"]) if c.get("state") else None,
    )


# Payload keys that hold cards, singly or as arrays. Covers all 26 nesting
# shapes seen in the corpus; unknown keys are simply not projected.
CARD_KEYS = ("card", "from", "to", "cards", "jokers", "deck", "deck_cards",
             "consumables", "targets", "items")


class Ingester:
    def __init__(self, db):
        self.db = db
        self.state = {}       # run_id -> per-run accumulators

    # -- per-run accumulator -------------------------------------------------
    def st(self, run):
        return self.state.setdefault(run, {
            "round_seq": 0, "open_round": None, "went_endless": 0,
            "max_n": {}, "lines": {}, "card_add": 0, "card_remove": 0,
            "baseline_deck": None, "encode_errors": 0,
            "saw_win": False, "saw_end": False, "end_deck_size": None,
            "ver": None})

    def ingest_file(self, path):
        size = os.path.getsize(path)
        mtime = os.path.getmtime(path)
        row = self.db.execute(
            "SELECT bytes_read, mtime FROM files WHERE path=?", (path,)).fetchone()
        start = 0
        if row:
            if row[0] == size and abs(row[1] - mtime) < 1e-6:
                return 0                      # unchanged
            start = row[0]

        with open(path, "rb") as fh:
            fh.seek(start)
            blob = fh.read()
        # Never consume a partial trailing line: a live run's file is mid-flush.
        cut = blob.rfind(b"\n")
        if cut < 0:
            return 0
        consumed = start + cut + 1
        lines = blob[:cut].decode("utf-8", "replace").splitlines()

        n_ok = 0
        run_id = None
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except Exception:
                continue
            run_id = ev.get("run") or run_id
            self.apply(ev, os.path.basename(path), size)
            n_ok += 1

        self.db.execute(
            "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
            (path, run_id or os.path.basename(path), consumed, mtime, n_ok, time.time()))
        return n_ok

    # -- one event -----------------------------------------------------------
    def apply(self, ev, log_file=None, log_bytes=None):
        self.cur_log, self.cur_bytes, self.cur_v = log_file, log_bytes, ev.get("v")
        run, seg, n = ev.get("run"), ev.get("seg", 0), ev.get("n")
        e, d = ev.get("e"), ev.get("d") or {}
        el = 1 if ev.get("el") else 0
        ante, rnd = as_int(ev.get("a")), as_int(ev.get("r"))
        st = self.st(run)
        st["max_n"][seg] = max(st["max_n"].get(seg, -1), n if n is not None else -1)
        st["lines"][seg] = st["lines"].get(seg, 0) + 1
        if e == "card.add":
            st["card_add"] += 1
        elif e == "card.remove":
            st["card_remove"] += 1
        elif e == "encode.error":
            st["encode_errors"] += 1
        elif e == "run.win":
            st["saw_win"] = True

        # cards, wherever they are nested
        rows = []
        for key in CARD_KEYS:
            v = d.get(key)
            if isinstance(v, dict):
                r = card_row(run, seg, n, key, 0, v, ante, el, e)
                if r:
                    rows.append(r)
            elif isinstance(v, list):
                for i, c in enumerate(v):
                    r = card_row(run, seg, n, key, i, c, ante, el, e)
                    if r:
                        rows.append(r)
        if rows:
            self.db.executemany(
                "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

        getattr(self, "on_" + e.replace(".", "_"), lambda *_: None)(run, seg, n, ante, el, d, st)

    # -- handlers ------------------------------------------------------------
    def on_run_start(self, run, seg, n, ante, el, d, st, resumed=False):
        env = d.get("env") or {}
        if not resumed:
            self.db.execute(
                """INSERT OR REPLACE INTO runs
                   (run_id, log_file, log_bytes, started_ts, seed, seeded, challenge,
                    deck_key, deck_name, stake, stake_key, win_ante, profile,
                    starting_deck_size)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run, self.cur_log, self.cur_bytes, as_int(d.get("ts")) or 0,
                 d.get("seed"), 1 if d.get("seeded") else 0,
                 json.dumps(d["challenge"]) if d.get("challenge") else None,
                 d.get("deck_key"), d.get("deck"), as_int(d.get("stake")),
                 d.get("stake_key"), as_int(d.get("win_ante")),
                 as_int(d.get("profile")), as_int(d.get("starting_deck_size"))))
        # Environment per SEGMENT: a run can span a mod update, so seg 0 and
        # seg 1 of one file may have been written by different builds with
        # different capture behaviour.
        self.db.execute(
            "INSERT OR REPLACE INTO segments VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run, seg, as_int(d.get("ts")), 1 if resumed else 0,
             as_int(self.cur_v), env.get("balatrodb"), env.get("game"),
             env.get("lovely"), env.get("smods"),
             json.dumps(env.get("mods") or [])))

    def on_run_resume(self, run, seg, n, ante, el, d, st):
        self.on_run_start(run, seg, n, ante, el, d, st, resumed=True)

    def on_run_baseline(self, run, seg, n, ante, el, d, st):
        if st["baseline_deck"] is None:
            st["baseline_deck"] = len(d.get("deck_cards") or [])

    def on_run_win(self, run, seg, n, ante, el, d, st):
        self.db.execute("UPDATE runs SET won=1 WHERE run_id=?", (run,))

    def on_blind_select(self, run, seg, n, ante, el, d, st):
        if d.get("entered_endless"):
            st["went_endless"] = 1
            self.db.execute("UPDATE runs SET went_endless=1 WHERE run_id=?", (run,))

    def on_round_start(self, run, seg, n, ante, el, d, st):
        st["round_seq"] += 1
        st["open_round"] = st["round_seq"]
        cols = {}
        cols.update(triple("required", d.get("chips")))
        self.db.execute(
            """INSERT OR REPLACE INTO rounds
               (run_id, round_seq, seg, ante, blind_key, blind_name, is_boss, reward,
                endless, required_ord, required_num, required_txt)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run, st["round_seq"], seg, as_int(d.get("ante")) or ante,
             d.get("blind_key"), d.get("name"), 1 if d.get("boss") else 0,
             as_int(d.get("reward")), el,
             cols["required_ord"], cols["required_num"], cols["required_txt"]))

    def on_round_end(self, run, seg, n, ante, el, d, st):
        rs = st["open_round"] or st["round_seq"]
        if rs == 0:
            return
        sc = triple("score", d.get("score"))
        self.db.execute(
            """UPDATE rounds SET score_ord=?, score_num=?, score_txt=?,
                   cashout_total=?, dollars_before=?, deck_size=?
                 WHERE run_id=? AND round_seq=?""",
            (sc["score_ord"], sc["score_num"], sc["score_txt"],
             as_int(d.get("total")), as_int(d.get("dollars_before")),
             as_int(d.get("deck_size")), run, rs))
        items = d.get("items") or []
        self.db.executemany(
            "INSERT OR REPLACE INTO cashout_items VALUES (?,?,?,?,?,?,?)",
            [(run, rs, i, it.get("name"), as_int(it.get("dollars")),
              as_int(it.get("disp")), it.get("key"))
             for i, it in enumerate(items) if isinstance(it, dict)])
        st["open_round"] = None

    def on_hand_play(self, run, seg, n, ante, el, d, st):
        sc = triple("score", d.get("score"))
        cb = triple("chips_before", d.get("chips_before"))
        self.db.execute(
            """INSERT OR REPLACE INTO hands VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run, seg, n, st["open_round"] or st["round_seq"], ante, el,
             d.get("hand"), as_int(d.get("level")),
             1 if d.get("oneshot") else 0,
             sc["score_ord"], sc["score_num"], sc["score_txt"],
             cb["chips_before_ord"], cb["chips_before_num"], cb["chips_before_txt"],
             as_int(d.get("hands_left_before")), as_int(d.get("discards_left_before"))))

    def on_hand_levelup(self, run, seg, n, ante, el, d, st):
        self.db.execute("INSERT OR REPLACE INTO hand_levels VALUES (?,?,?,?,?,?,?,?)",
                        (run, seg, n, el, d.get("hand"), as_int(d.get("from")),
                         as_int(d.get("to")), as_int(d.get("amount"))))

    def _scale(self, run, seg, n, ante, el, d, is_reset):
        f, t = triple("from", d.get("from")), triple("to", d.get("to"))
        self.db.execute(
            "INSERT OR REPLACE INTO joker_scale VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run, seg, n, as_int(d.get("id")), d.get("key"), d.get("field"), ante, el,
             1 if is_reset else 0,
             f["from_ord"], f["from_num"], f["from_txt"],
             t["to_ord"], t["to_num"], t["to_txt"]))

    def on_joker_scale(self, run, seg, n, ante, el, d, st):
        self._scale(run, seg, n, ante, el, d, False)

    def on_joker_reset(self, run, seg, n, ante, el, d, st):
        self._scale(run, seg, n, ante, el, d, True)

    def on_money_change(self, run, seg, n, ante, el, d, st):
        self.db.execute("INSERT OR REPLACE INTO money VALUES (?,?,?,?,?,?,?)",
                        (run, seg, n, ante, el, as_int(d.get("delta")),
                         as_int(d.get("before"))))

    def on_run_end(self, run, seg, n, ante, el, d, st):
        st["saw_end"] = True
        st["end_deck_size"] = as_int(d.get("deck_size"))
        bh = triple("best_hand", d.get("best_hand"))
        fs = triple("final_round_score", d.get("final_round_score") or d.get("score"))
        self.db.execute(
            """UPDATE runs SET won=?, result=?, terminal=?, ended_ante=?, ended_round=?,
                   hands_played=?, skips=?, final_dollars=?, deck_size=?, went_endless=?,
                   best_hand_ord=?, best_hand_num=?, best_hand_txt=?,
                   furthest_ante=?, furthest_round=?,
                   final_round_score_ord=?, final_round_score_num=?, final_round_score_txt=?
                 WHERE run_id=?""",
            (1 if d.get("won") else 0, d.get("result"),
             1 if d.get("terminal", True) else 0,
             as_int(d.get("ante")), as_int(d.get("round")),
             as_int(d.get("hands_played")), as_int(d.get("skips")),
             as_int(d.get("dollars")), as_int(d.get("deck_size")),
             1 if (d.get("endless") or st["went_endless"]) else 0,
             bh["best_hand_ord"], bh["best_hand_num"], bh["best_hand_txt"],
             as_int(d.get("furthest_ante")), as_int(d.get("furthest_round")),
             fs["final_round_score_ord"], fs["final_round_score_num"],
             fs["final_round_score_txt"], run))


def _validate_impl(self):
    """Checks run over the stream, not over SQL -- there is no events table to
    query, and ingest already walks every line anyway."""
    for run, st in self.state.items():
        issues = []
        for seg, mx in st["max_n"].items():
            if st["lines"][seg] != mx + 1:
                issues.append(("sequence_gap",
                               f"seg {seg}: {st['lines'][seg]} lines, max n={mx}"))
        if st["encode_errors"]:
            issues.append(("encode_error", f"{st['encode_errors']} events failed to encode"))
        if not st["saw_end"]:
            issues.append(("unterminated", "no run.end -- crash, or still in progress"))
        # A run can be won without run.win: G.GAME.won is set inside end_round
        # while win_game() only runs from a later queued event.
        won = self.db.execute("SELECT won FROM runs WHERE run_id=?", (run,)).fetchone()
        if won and won[0] and not st["saw_win"]:
            issues.append(("won_without_win", "run.end.won set but no run.win event"))
        # Deck identity, only for builds that routed consumables and
        # enhancements correctly -- older ones inflate card.add.
        ver = self.db.execute(
            "SELECT ver_balatrodb FROM segments WHERE run_id=? ORDER BY seg LIMIT 1",
            (run,)).fetchone()
        if st["baseline_deck"] is not None and st["end_deck_size"] is not None:
            pred = st["baseline_deck"] + st["card_add"] - st["card_remove"]
            if pred != st["end_deck_size"]:
                issues.append(("deck_identity",
                               f"{st['baseline_deck']}+{st['card_add']}-{st['card_remove']}"
                               f"={pred}, run.end says {st['end_deck_size']}"
                               f" (build {ver[0] if ver else '?'})"))
        for check, detail in issues:
            self.db.execute("INSERT OR REPLACE INTO ingest_issues VALUES (?,?,?)",
                            (run, check, detail))


Ingester.validate = _validate_impl


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--logs", default=DEFAULT_LOGS)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    if a.rebuild and os.path.exists(a.db):
        os.remove(a.db)
    db = sqlite3.connect(a.db)
    db.executescript(SCHEMA)

    ing = Ingester(db)
    total = 0
    files = sorted(glob.glob(os.path.join(a.logs, "*.jsonl")))
    for p in files:
        total += ing.ingest_file(p)
    ing.validate()
    db.commit()
    print(f"ingested {total} events from {len(files)} files -> {a.db}")

    if a.report:
        report(db)


def report(db):
    q = lambda s, *p: db.execute(s, p).fetchall()
    print("\n-- runs --")
    for r in q("""SELECT deck_name, stake_key, won, result, furthest_ante,
                         best_hand_txt, deck_size FROM runs ORDER BY started_ts"""):
        print("  ", r)

    print("\n-- per-joker maxima (non-endless) --")
    for r in q("""SELECT key, field, to_txt, run_id FROM joker_scale js
                   WHERE endless=0 AND is_reset=0
                     AND to_ord = (SELECT MAX(to_ord) FROM joker_scale x
                                    WHERE x.key=js.key AND x.field=js.field AND x.endless=0)
                   GROUP BY key, field ORDER BY to_ord DESC"""):
        print("  ", r)

    print("\n-- best hand levels --")
    for r in q("SELECT hand, MAX(lvl_to) FROM hand_levels GROUP BY hand ORDER BY 2 DESC LIMIT 6"):
        print("  ", r)

    print("\n-- biggest cash-outs, with attribution --")
    for r in q("""SELECT ro.cashout_total, ro.ante, r.deck_name,
                    (SELECT group_concat(name||'='||dollars, ' ') FROM cashout_items ci
                      WHERE ci.run_id=ro.run_id AND ci.round_seq=ro.round_seq)
                  FROM rounds ro JOIN runs r USING(run_id)
                  WHERE ro.cashout_total IS NOT NULL
                  ORDER BY ro.cashout_total DESC LIMIT 5"""):
        print("  ", r)

    print("\n-- max round score (summed from hands) --")
    for r in q("""SELECT run_id, round_seq, SUM(score_num) s FROM hands
                  GROUP BY run_id, round_seq ORDER BY s DESC LIMIT 5"""):
        print("  ", r)

    print("\n-- sanity: gap-free sequences --")
    bad = q("""SELECT run_id, seg, COUNT(*), MAX(n)+1 FROM events
               GROUP BY run_id, seg HAVING COUNT(*) <> MAX(n)+1""")
    print("  ", "OK" if not bad else bad)

    print("\n-- sanity: encode errors --")
    print("  ", q("SELECT COUNT(*) FROM events WHERE e='encode.error'")[0][0])


if __name__ == "__main__":
    sys.exit(main())
