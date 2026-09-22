"""Fold BalatroDB JSONL run logs into SQLite.

The gzipped logs are the system of record; this database is a derived index and
`--rebuild` regenerates it from them. See docs/db-schema.md.

    python ingest/ingest.py [--db PATH] [--logs DIR] [--rebuild] [--report]

Design note -- why there is no partial-tail resume
--------------------------------------------------
An earlier version tracked a byte offset per file and ingested only the tail,
accumulating per-run state (round numbering, validation counters) in memory.
Three independent reviews reproduced the same corruption: a second process
starts with a fresh accumulator, so `round_seq` restarts at 1 and overwrites
earlier rounds, and hands attach to the wrong round.

This version re-derives a whole run whenever its log changes. A run is ~300 KB,
re-deriving one takes milliseconds, and it removes the entire class of bug:
there is no cross-process state to persist, and a full rebuild and an
incremental update really are the same code path.
"""
from __future__ import annotations

import argparse
import glob
import gzip
import hashlib
import json
import math
import os
import sqlite3
import zlib
import sys
import time

DEFAULT_LOGS = os.path.expandvars(r"%APPDATA%\Balatro\BalatroDB\runs")
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(HERE, "balatro.db")


# ─── numbers ──────────────────────────────────────────────────────────────
# The wire delivers a number plainly, or as {"s": exact, "l": signed log10}
# when it cannot round-trip through the encoder's %.14g.

def ord_num(v):
    """(ord, num, txt). `ord` is sign(x)*log10(1+|x|).

    ORDERING ACCELERATOR, NOT AN EXACT KEY. Distinct values collide: plain
    99999999999999 and a wrapped 1e14 both yield 14.0. Any query needing the
    true maximum must break ties on `txt` numerically.
    """
    if v is None:
        return None, None, None
    if isinstance(v, dict):
        s, l = v.get("s"), v.get("l")
        if l is None:
            return None, None, None if s is None else str(s)
        return float(l), as_num(v), str(s)
    if isinstance(v, bool):
        return None, None, str(v)
    if isinstance(v, (int, float)):
        x = float(v)
        sign = -1.0 if x < 0 else 1.0
        return sign * math.log10(1.0 + abs(x)), x, repr(v)
    return None, None, str(v)


def triple(v):
    return ord_num(v)


def as_num(v):
    """A float for arithmetic, accepting the wrapped form.

    Crossing the encoder's 14-significant-digit limit does not imply the value
    overflows a double, so a wrapped value usually still yields a usable float.
    Only a genuinely unrepresentable value gives None -- an earlier version
    returned None for everything wrapped, which made SUM() silently drop the
    largest hands in a round.
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        try:
            f = float(v.get("s"))
            return f if not (math.isinf(f) or math.isnan(f)) else None
        except (TypeError, ValueError):
            l = v.get("l")
            if isinstance(l, (int, float)) and abs(l) < 308:
                return math.copysign(10.0 ** abs(l), l)
            return None
    return None


def as_int(v):
    n = as_num(v)
    return None if n is None or math.isinf(n) or math.isnan(n) else int(n)


# ─── known capture defects, sniffed from the data ─────────────────────────
# The mod never bumped its version string while the layout changed, so the
# format is detected from which events and fields are present, not from `v`
# or `env.balatrodb`.

DEFECTS = {
    "legacy_no_baseline":   "arrays inside run.start/run.end; no run.baseline event",
    "legacy_result_vocab":  "result vocabulary predates died/completed",
    "legacy_run_end_score": "run.end.score rather than final_round_score",
    "no_terminal_flag":     "run.end carries no `terminal`",
    "no_best_hand":         "no round_scores block; best_hand unavailable",
    "no_endless_flag":      "run.end carries no `endless`",
    "consumables_as_cards": "consumables logged as card.add; deck counts inflated",
    "no_card_modify":       "enhancements logged as card.add, not card.modify",
    "encode_error":         "at least one payload failed to encode",
    "no_run_end":           "file ends with no run.end (crash, or still live)",
    "deck_identity_fail":   "baseline + adds - removes != final deck size",
    "sequence_gap":         "event sequence is not gap-free within a segment",
    "won_without_win":      "run.end.won set but no run.win event -- a death "
                            "on the win-ante boss; `won` is taken from the event",
    "count_mismatch":       "the game's own hand/skip counters disagree with the "
                            "events recorded; the log has duplicate or missing lines",
    "value_beyond_double":  "at least one value does not fit a double, so its "
                            "*_num column is NULL and SUM/AVG omit it",
}

LEGACY_RESULTS = {"quit", "loss", "win"}

# Payload keys holding cards, and which hold arrays. `items` is deliberately
# absent: round.end.items[] are cash-out rows, not cards, and a generic
# dictionary sweep put 347 junk rows in `cards`.
CARD_SINGLE = ("card", "from", "to")
CARD_ARRAYS = ("cards", "jokers", "deck", "deck_cards", "consumables", "targets")


def open_log(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def read_events(path):
    """Every complete line of a log. A live run's last line may be partial."""
    with open_log(path) as fh:
        data = fh.read()
    cut = data.rfind("\n")
    if cut < 0:
        return [], 0
    out, bad = [], 0
    # split("\n"), not splitlines(): the latter also breaks on U+2028, U+0085
    # and friends, which the Lua encoder does not escape.
    for line in data[:cut].split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            bad += 1
            continue
        # Valid JSON is not necessarily an event. A bare string, list or null
        # would sail through and then raise AttributeError on .get() deep
        # inside derive(), taking the whole ingest down with it.
        if isinstance(obj, dict):
            out.append(obj)
        else:
            bad += 1
    return out, bad


MONEY_SKIP = {"state.change", "snapshot", "joker.scale", "money.change",
              "card.modify"}
# A play resolves its money inside evaluate_play, in the frame that ends with
# the hand.play event -- observed gap 0.00s. A discard's triggers are queued
# and land a beat later -- observed 0.22s, and the next thing the player can
# do is seconds away, so a one-second window separates the two cleanly.
MONEY_SAME_FRAME = 0.05
MONEY_QUEUED = 1.0
ACTION_EVENTS = ("hand.play", "hand.discard")
# Events that own money themselves, or that mark the player having moved on.
# Walking back from a money change stops here: anything before one of these
# cannot be the queued consequence of a discard.
MONEY_BOUNDARY = {"hand.play", "shop.buy", "shop.sell", "shop.reroll",
                  "consumable.use", "voucher.redeem", "pack.open", "pack.pick",
                  "round.start", "round.end", "blind.select", "blind.skip",
                  "run.start", "run.resume"}


def same_frame(ev, t):
    """Whether `ev` belongs to the same engine frame as a money change at `t`."""
    et = ev.get("t")
    return t is not None and et is not None and abs(et - t) <= MONEY_SAME_FRAME


def classify_money(events):
    """Trace every money.change back to what caused it.

    Returns {(seg, n): (cause_event_type, action_n or None)}.

    `money` records only that the balance moved, so the cause has to come from
    position in the stream -- which means the whole ordered stream, and so it
    is derived here rather than guessed from timestamps at query time.

    The direction differs by action, which is why this is not simply "the
    previous event":

      * a PLAY emits its money from inside evaluate_play, whose AFTER hook
        emits hand.play -- the money arrives just BEFORE the play, same frame.
      * a DISCARD's jokers fire from queued events that run once
        discard_cards_from_highlighted has returned -- the money arrives AFTER.

    Anything else (a sell, a reroll, a purchase, the cash out) is named by the
    event it follows, and owns no action.
    """
    by_seg = {}
    for ev in events:
        by_seg.setdefault(ev.get("seg", 0), []).append(ev)
    # Position in the stream is the whole signal, so order by it explicitly
    # rather than trusting the order the events arrived in.
    for lst in by_seg.values():
        lst.sort(key=lambda e: (e.get("n") is None, e.get("n")))

    out = {}
    for seg, lst in by_seg.items():
        for i, ev in enumerate(lst):
            if ev.get("e") != "money.change":
                continue
            t = ev.get("t")
            # Scan by TIME, not by event type. An allow-list of "events that
            # may sit between money and its cause" cannot be complete: a
            # Trading Card discard emits card.remove, Space Joker emits
            # hand.levelup mid-scoring, DNA emits card.add -- each of which
            # pushed the real cause out of view and handed the money to
            # whatever happened to be adjacent. Everything the scoring frame
            # emits shares that frame's timestamp, so the frame is the rule.
            j = i + 1
            while j < len(lst) and same_frame(lst[j], t):
                if lst[j].get("e") in ACTION_EVENTS:
                    break
                j += 1
            k = i - 1
            while k >= 0 and lst[k].get("e") in MONEY_SKIP:
                k -= 1
            nxt = lst[j] if j < len(lst) else None
            prv = lst[k] if k >= 0 else None

            cause = cause_n = None
            # Same frame as the action that follows: a play emits its money
            # from inside evaluate_play, whose after-hook emits hand.play.
            if (nxt is not None and nxt.get("e") in ACTION_EVENTS
                    and same_frame(nxt, t)):
                cause, cause_n = nxt["e"], nxt.get("n")
            # Same frame as the action BEFORE it. Scoring can emit money on
            # either side of the hand.play within one frame, and looking only
            # forward left those rows naming a play they did not point at.
            elif (prv is not None and prv.get("e") in ACTION_EVENTS
                  and same_frame(prv, t)):
                cause, cause_n = prv["e"], prv.get("n")
            else:
                # A discard's jokers fire from queued events, and those events
                # can emit their own consequences first -- Trading Card
                # destroys the discarded card, so card.remove lands between
                # the discard and its $3. So walk back through consequences,
                # stopping at anything that owns money itself or means the
                # player has moved on.
                owner = None
                for b in range(i - 1, -1, -1):
                    e2, t2 = lst[b].get("e"), lst[b].get("t")
                    if e2 in MONEY_SKIP:
                        continue
                    if t is None or t2 is None or t - t2 > MONEY_QUEUED:
                        break
                    if e2 == "hand.discard":
                        owner = lst[b]
                        break
                    if e2 in MONEY_BOUNDARY:
                        break
                if owner is not None:
                    cause, cause_n = "hand.discard", owner.get("n")
                elif prv is not None:
                    cause = prv.get("e")
                    if cause in ACTION_EVENTS:
                        # It merely FOLLOWS a play -- an end-of-round payout,
                        # a shop purchase after the last hand. Naming the play
                        # without an n made SUM(delta) WHERE cause='hand.play'
                        # disagree with a JOIN on cause_n, with nothing to say
                        # which was right. The invariant is: an action cause
                        # always carries the action.
                        cause = "unattributed"

            out[(seg, ev.get("n"))] = (cause, cause_n)
    return out


class Ingester:
    def __init__(self, db):
        self.db = db

    # -- file level ---------------------------------------------------------
    def ingest_file(self, path):
        st = os.stat(path)
        sig = hashlib.sha1(f"{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()
        row = self.db.execute("SELECT sig FROM files WHERE path=?", (path,)).fetchone()
        if row and row[0] == sig:
            return 0, False

        events, bad = read_events(path)
        if not events:
            return 0, False
        run_id = events[0].get("run") or os.path.basename(path)

        # Purge and re-derive as one unit. Without the savepoint a failure
        # midway leaves the run purged and half rebuilt, and a later successful
        # file commits that wreckage along with itself.
        self.db.execute("SAVEPOINT run_rebuild")
        try:
            self.purge(run_id)
            self.derive(run_id, os.path.basename(path), st.st_size, events, bad)
            self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
                            (path, run_id, st.st_size, st.st_mtime, sig, time.time()))
        except Exception:
            self.db.execute("ROLLBACK TO run_rebuild")
            self.db.execute("RELEASE run_rebuild")
            raise
        self.db.execute("RELEASE run_rebuild")
        return len(events), True

    def purge(self, run_id):
        """Everything derived from one run, so re-derivation cannot duplicate."""
        for t in ("cards", "joker_scale", "joker_state", "joker_derived", "hands",
                  "hand_levels", "discards", "money", "cashout_items", "rounds",
                  "segments", "blind_skips", "consumable_uses", "run_defects", "runs"):
            self.db.execute(f"DELETE FROM {t} WHERE run_id=?", (run_id,))

# Events that sit between a money change and whatever caused it, or that are
# part of the same burst. The scoring burst interleaves joker.scale and
# money.change, so both have to be stepped over to find its edges.
    # -- run level ----------------------------------------------------------
    def derive(self, run_id, log_file, log_bytes, events, bad_lines):
        kinds = {e.get("e") for e in events}
        defects = set()
        if bad_lines or "encode.error" in kinds:
            defects.add("encode_error")

        # Format sniffing. `v` stayed at 1 across every layout change, so the
        # only reliable signal is which events and fields are present.
        legacy = not (kinds & {"run.baseline", "run.rebaseline"})
        if legacy:
            defects.add("legacy_no_baseline")
            defects.add("consumables_as_cards")
        # Absence of card.modify is NOT evidence of a defect: a run in which
        # nothing was ever enhanced legitimately has none. Only flag it for
        # builds that predate the event, which are exactly the ones without a
        # baseline.
        if legacy and "card.modify" not in kinds:
            defects.add("no_card_modify")

        causes = classify_money(events)
        seg_lines, seg_max_n = {}, {}
        round_seq, open_round = 0, None
        baseline_deck = baseline_dollars = None
        card_add = card_remove = 0
        saw_win = False
        run_row = end = None
        balance = None

        for ev in events:
            e, d = ev.get("e"), ev.get("d") or {}
            seg, n = ev.get("seg", 0), ev.get("n")
            el = 1 if ev.get("el") else 0
            ante = as_int(ev.get("a"))
            ts = ev.get("t") if isinstance(ev.get("t"), (int, float)) else None

            seg_lines[seg] = seg_lines.get(seg, 0) + 1
            if isinstance(n, int):
                seg_max_n[seg] = max(seg_max_n.get(seg, -1), n)

            self.project_cards(run_id, seg, n, ante, el, e, d)

            if e in ("run.start", "run.resume"):
                env = d.get("env") or {}
                if e == "run.start":
                    run_row = d
                self.db.execute("INSERT OR REPLACE INTO segments VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (run_id, seg, as_int(d.get("ts")), 1 if e == "run.resume" else 0,
                                 as_int(ev.get("v")), env.get("balatrodb"), env.get("game"),
                                 env.get("lovely"), env.get("smods"),
                                 json.dumps(env.get("mods") or [])))
                if legacy and baseline_deck is None:
                    baseline_deck = len(d.get("deck_cards") or []) or None
                    baseline_dollars = as_int(d.get("dollars"))

            elif e in ("run.baseline", "run.rebaseline"):
                if baseline_deck is None:
                    baseline_deck = len(d.get("deck_cards") or [])
                if baseline_dollars is None:
                    baseline_dollars = as_int(d.get("dollars"))
                # A rebaseline is ground truth read straight off G.GAME at a
                # resume. The running balance is a sum of deltas, so anything
                # the log missed skews it for the rest of the run -- re-anchor
                # instead of carrying the drift forward.
                if e == "run.rebaseline" and as_int(d.get("dollars")) is not None:
                    balance = as_int(d.get("dollars"))

            elif e == "run.win":
                saw_win = True

            elif e == "round.start":
                round_seq += 1
                open_round = round_seq
                o, nu, t = triple(d.get("chips"))
                self.db.execute(
                    "INSERT OR REPLACE INTO rounds (run_id,round_seq,seg,ante,blind_key,"
                    "blind_name,is_boss,reward,endless,start_n,"
                    "required_ord,required_num,required_txt)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, round_seq, seg, as_int(d.get("ante")) or ante, d.get("blind_key"),
                     d.get("name"), 1 if d.get("boss") else 0, as_int(d.get("reward")), el,
                     n, o, nu, t))

            elif e == "round.end":
                rs = open_round or round_seq
                if rs:
                    o, nu, t = triple(d.get("score"))
                    self.db.execute(
                        "UPDATE rounds SET score_ord=?,score_num=?,score_txt=?,cashout_total=?,"
                        "dollars_before=?,deck_size=? WHERE run_id=? AND round_seq=?",
                        (o, nu, t, as_int(d.get("total")), as_int(d.get("dollars_before")),
                         as_int(d.get("deck_size")), run_id, rs))
                    self.db.executemany(
                        "INSERT OR REPLACE INTO cashout_items VALUES (?,?,?,?,?,?,?)",
                        [(run_id, rs, i, it.get("name"), as_int(it.get("dollars")),
                          as_int(it.get("disp")), it.get("key"))
                         for i, it in enumerate(d.get("items") or []) if isinstance(it, dict)])
                    self.sample_jokers(run_id, seg, n, rs, ante, el, d.get("jokers"))
                    self.sample_deck(run_id, rs, d.get("deck"))
                open_round = None

            elif e == "hand.play":
                so, sn, stx = triple(d.get("score"))
                co, cn, ctx = triple(d.get("chips_before"))
                self.db.execute(
                    "INSERT OR REPLACE INTO hands VALUES (" + ",".join("?" * 18) + ")",
                    (run_id, seg, n, open_round or round_seq, ante, el, d.get("hand"),
                     as_int(d.get("level")), 1 if d.get("oneshot") else 0,
                     so, sn, stx, co, cn, ctx,
                     as_int(d.get("hands_left_before")), as_int(d.get("discards_left_before")),
                     ts))
                self.sample_jokers(run_id, seg, n, open_round or round_seq, ante, el,
                                   d.get("jokers"))

            elif e == "hand.discard":
                self.db.execute("INSERT OR REPLACE INTO discards VALUES (?,?,?,?,?,?,?,?)",
                                (run_id, seg, n, open_round or round_seq, ante, el,
                                 len(d.get("cards") or []), ts))

            elif e == "hand.levelup":
                self.db.execute("INSERT OR REPLACE INTO hand_levels VALUES (?,?,?,?,?,?,?,?)",
                                (run_id, seg, n, el, d.get("hand"), as_int(d.get("from")),
                                 as_int(d.get("to")), as_int(d.get("amount"))))

            elif e in ("joker.scale", "joker.reset"):
                fo, fn, ft = triple(d.get("from"))
                to, tn, tt = triple(d.get("to"))
                self.db.execute(
                    "INSERT OR REPLACE INTO joker_scale VALUES (" + ",".join("?" * 15) + ")",
                    (run_id, seg, n, as_int(d.get("id")), d.get("key"), d.get("field"), ante, el,
                     1 if e == "joker.reset" else 0, fo, fn, ft, to, tn, tt))

            elif e == "money.change":
                delta = as_int(d.get("delta")) or 0
                # `before` is unreliable: consecutive queued ease_dollars calls
                # all report the same pre-value, so before+delta invents peaks
                # that never happened. The balance is a running sum instead.
                if balance is None:
                    balance = baseline_dollars if baseline_dollars is not None \
                        else (as_int(d.get("before")) or 0)
                balance += delta
                cause, cause_n = causes.get((seg, n), (None, None))
                self.db.execute("INSERT OR REPLACE INTO money VALUES (" +
                                ",".join("?" * 11) + ")",
                                (run_id, seg, n, ante, el, delta,
                                 as_int(d.get("before")), balance, cause, cause_n, ts))

            elif e == "blind.skip":
                # Throwback's value is the running skip count, so the skips
                # need their own timeline, not just the run total.
                self.db.execute("INSERT OR REPLACE INTO blind_skips VALUES (?,?,?,?,?,?)",
                                (run_id, seg, n, ante, el, d.get("tag")))

            elif e in ("consumable.use", "pack.open", "voucher.redeem"):
                c = d.get("card") or {}
                self.db.execute(
                    "INSERT OR REPLACE INTO consumable_uses VALUES (?,?,?,?,?,?,?)",
                    (run_id, seg, n, ante, el, c.get("key"), c.get("set")))

            elif e == "card.add":
                card_add += 1
            elif e == "card.remove":
                card_remove += 1
            elif e == "run.end":
                end = d

        self.write_run(run_id, log_file, log_bytes, run_row, end, events, saw_win, defects)

        if baseline_deck is not None and end and end.get("deck_size") is not None \
                and "consumables_as_cards" not in defects:
            if baseline_deck + card_add - card_remove != as_int(end.get("deck_size")):
                defects.add("deck_identity_fail")

        # The same trick for hands and skips. run.end carries Balatro's own
        # counters; the tables are our reconstruction. They should agree, and
        # when they do not the log has duplicate or missing lines -- which is
        # the one shape `sequence_gap` cannot see, because re-emitted events
        # carry fresh, gap-free `n`s.
        if end:
            for field, table in (("hands_played", "hands"),
                                 ("skips", "blind_skips")):
                theirs = as_int(end.get(field))
                if theirs is None:
                    continue
                ours = self.db.execute(
                    "SELECT COUNT(*) FROM " + table + " WHERE run_id=?",
                    (run_id,)).fetchone()[0]
                if theirs != ours:
                    defects.add("count_mismatch")

        for seg, mx in seg_max_n.items():
            if seg_lines.get(seg) != mx + 1:
                defects.add("sequence_gap")

        for name in sorted(defects):
            self.db.execute("INSERT OR REPLACE INTO run_defects VALUES (?,?,?)",
                            (run_id, name, DEFECTS.get(name, "")))

    def write_run(self, run_id, log_file, log_bytes, rs, end, events, saw_win, defects):
        rs = rs or {}
        self.db.execute(
            "INSERT OR REPLACE INTO runs (run_id,log_file,log_bytes,started_ts,seed,seeded,"
            "challenge,deck_key,deck_name,stake,stake_key,win_ante,profile,"
            "starting_deck_size,won)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, log_file, log_bytes, as_int(rs.get("ts")) or 0, rs.get("seed"),
             1 if rs.get("seeded") else 0,
             json.dumps(rs["challenge"]) if rs.get("challenge") else None,
             rs.get("deck_key"), rs.get("deck"), as_int(rs.get("stake")), rs.get("stake_key"),
             as_int(rs.get("win_ante")), as_int(rs.get("profile")),
             as_int(rs.get("starting_deck_size")),
             # Here, not only in the UPDATE below: a run that is still being
             # played has no run.end, and the dashboard ingests while you
             # play. A won run continuing into endless would otherwise read
             # won IS NULL for as long as the session lasted.
             1 if saw_win else 0))

        if end is None:
            defects.add("no_run_end")
            return

        if end.get("result") in LEGACY_RESULTS:
            defects.add("legacy_result_vocab")
        if "terminal" not in end:
            defects.add("no_terminal_flag")
        if "score" in end and "final_round_score" not in end:
            defects.add("legacy_run_end_score")
        if end.get("best_hand") is None:
            defects.add("no_best_hand")
        if "endless" not in end:
            defects.add("no_endless_flag")
        # run.end carried G.GAME.won in builds up to 0.4.0, which the game
        # sets from "ante == win_ante and the blind is a Boss" BEFORE it
        # checks whether you survived (state_events.lua:111). Dying to the
        # final boss therefore reported a win. A run.win event means
        # win_game() actually ran, which only happens from ROUND_EVAL, so
        # that -- not the flag -- decides.
        won = 1 if saw_win else 0
        if end.get("won") and not saw_win:
            defects.add("won_without_win")

        terminal = end.get("terminal")
        if terminal is None:                       # legacy logs: infer it
            terminal = 0 if end.get("result") == "suspended" else 1
        bo, bn, bt = triple(end.get("best_hand"))
        fo, fn, ft = triple(end.get("final_round_score", end.get("score")))
        went_endless = 1 if (end.get("endless") or any(e.get("el") for e in events)) else 0

        self.db.execute(
            "UPDATE runs SET won=?,result=?,terminal=?,ended_ante=?,ended_round=?,"
            "hands_played=?,skips=?,final_dollars=?,deck_size=?,went_endless=?,"
            "best_hand_ord=?,best_hand_num=?,best_hand_txt=?,furthest_ante=?,"
            "furthest_round=?,final_round_score_ord=?,final_round_score_num=?,"
            "final_round_score_txt=? WHERE run_id=?",
            (won, end.get("result"), 1 if terminal else 0,
             as_int(end.get("ante")), as_int(end.get("round")),
             as_int(end.get("hands_played")), as_int(end.get("skips")),
             as_int(end.get("dollars")), as_int(end.get("deck_size")), went_endless,
             bo, bn, bt, as_int(end.get("furthest_ante")),
             as_int(end.get("furthest_round")), fo, fn, ft, run_id))

    # -- projections --------------------------------------------------------
    def project_cards(self, run_id, seg, n, ante, el, event, d):
        rows = []
        for key in CARD_SINGLE:
            v = d.get(key)
            if isinstance(v, dict) and ("id" in v or "key" in v or "rank" in v):
                rows.append(self.card_row(run_id, seg, n, key, 0, v, ante, el, event))
        for key in CARD_ARRAYS:
            v = d.get(key)
            if isinstance(v, list):
                for i, c in enumerate(v):
                    if isinstance(c, dict):
                        rows.append(self.card_row(run_id, seg, n, key, i, c, ante, el, event))
        if rows:
            self.db.executemany(
                "INSERT OR REPLACE INTO cards VALUES (" + ",".join("?" * 20) + ")", rows)

    def card_row(self, run_id, seg, n, role, pos, c, ante, el, event):
        st = c.get("state") if isinstance(c.get("state"), dict) else None
        return (run_id, seg, n, role, pos, ante, el, event,
                as_int(c.get("id")), c.get("key"), c.get("name"), c.get("set"),
                c.get("rank"), c.get("suit"), c.get("enhancement"), c.get("edition"),
                c.get("seal"),
                json.dumps(c["stickers"]) if c.get("stickers") else None,
                as_int(c.get("sell_cost")),
                json.dumps(st) if st else None)

    def sample_jokers(self, run_id, seg, n, round_seq, ante, el, jokers):
        """One row per joker per observation, with its numeric state hoisted
        into columns so queries need no json_extract."""
        if not isinstance(jokers, list):
            return
        rows = []
        for pos, j in enumerate(jokers):
            if not isinstance(j, dict):
                continue
            st = j.get("state") if isinstance(j.get("state"), dict) else {}
            # The mod samples G.jokers.cards in board order, so the index is
            # the board position.
            rows.append((run_id, seg, n, round_seq, ante, el,
                         pos, as_int(j.get("id")), j.get("key"),
                         as_num(st.get("mult")), as_num(st.get("x_mult")),
                         as_num(st.get("chips")), as_num(st.get("extra")),
                         as_num(st.get("stone_tally")), as_num(st.get("perma_bonus")),
                         json.dumps(st) if st else None))
        if rows:
            self.db.executemany(
                "INSERT OR REPLACE INTO joker_state VALUES (" + ",".join("?" * 16) + ")", rows)

    def sample_deck(self, run_id, round_seq, deck):
        """Per-round scalars from a deck sample, rather than 40-50 card rows.

        The deck sample is the single largest contributor to database size, and
        every statistic wanted from it -- deck size, stone count for Stone
        Joker, Hiker's accumulated bonus -- is a per-round scalar.
        """
        if not isinstance(deck, list):
            return
        stone = best = total = 0
        for c in deck:
            if not isinstance(c, dict):
                continue
            # The enhancement is named "Stone Card", not "Stone" -- the
            # earlier comparison never matched, so deck_stone was always 0
            # and Stone Joker was unreconstructable.
            if (c.get("enhancement") or "").startswith("Stone"):
                stone += 1
            stt = c.get("state")
            if isinstance(stt, dict):
                b = as_num(stt.get("perma_bonus")) or 0.0
                best = max(best, b)
                total += b
        self.db.execute(
            "UPDATE rounds SET deck_stone=?, deck_perma_max=?, deck_perma_total=?"
            " WHERE run_id=? AND round_seq=?", (stone, best, total, run_id, round_seq))

    # -- counters the derived jokers actually read --------------------------
    def derive_counters(self):
        """Supernova, Throwback and Fortune Teller read GAME counters, not
        their own ability fields, so their value cannot come from a joker
        sample. Reconstruct the counters from the projections instead.
        """
        self.db.execute("DELETE FROM joker_derived")
        # Supernova: times the played hand type has been played, so far.
        self.db.execute("""
            INSERT INTO joker_derived (run_id, seg, n, endless, metric, subject, value)
            SELECT run_id, seg, n, endless, 'hand_plays', hand,
                   COUNT(*) OVER (PARTITION BY run_id, hand ORDER BY seg, n)
              FROM hands WHERE hand IS NOT NULL
        """)
        # Throwback: blinds skipped so far.
        self.db.execute("""
            INSERT INTO joker_derived (run_id, seg, n, endless, metric, subject, value)
            SELECT run_id, seg, n, endless, 'skips', NULL,
                   ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY seg, n)
              FROM blind_skips
        """)
        # Stone Joker: stone cards in the deck, from the per-round scalar.
        self.db.execute("""
            INSERT INTO joker_derived (run_id, seg, n, endless, metric, subject, value)
            -- start_n, not round_seq: this column is joker_derived.n, an
            -- event sequence. round_seq happens to be a valid n for some
            -- other event in the run, so the error is invisible at rest.
            SELECT run_id, seg, start_n, endless, 'stone_cards', NULL, deck_stone
              FROM rounds WHERE deck_stone IS NOT NULL AND start_n IS NOT NULL
        """)
        # Fortune Teller: tarots used so far.
        self.db.execute("""
            INSERT INTO joker_derived (run_id, seg, n, endless, metric, subject, value)
            SELECT run_id, seg, n, endless, 'tarots', NULL,
                   ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY seg, n)
              FROM consumable_uses WHERE set_ = 'Tarot'
        """)


# ─── reporting ────────────────────────────────────────────────────────────
def report(db):
    q = lambda s, *p: db.execute(s, p).fetchall()
    print("\n-- runs --")
    for r in q("""SELECT substr(run_id,1,24), deck_name, stake_key, won, result,
                         went_endless, deck_size FROM runs ORDER BY started_ts"""):
        print("   %-26s %-16s %-12s won=%s %-11s el=%s deck=%s" % r)

    print("\n-- per-joker maxima, non-endless (one consistently filtered relation) --")
    for r in q("""
        WITH ranked AS (
          SELECT js.key, js.field, js.to_txt, js.to_ord, js.run_id,
                 ROW_NUMBER() OVER (PARTITION BY js.key, js.field
                                    ORDER BY js.to_ord DESC, CAST(js.to_txt AS REAL) DESC) rn
            FROM joker_scale js JOIN runs r USING (run_id)
           WHERE js.endless = 0 AND js.is_reset = 0)
        SELECT key, field, to_txt, substr(run_id,1,20) FROM ranked
         WHERE rn = 1 ORDER BY to_ord DESC LIMIT 8"""):
        print("   %-20s %-13s %-10s %s" % r)

    print("\n-- max money (running sum, not before+delta) --")
    for r in q("""SELECT substr(run_id,1,24), MAX(balance) FROM money
                  GROUP BY run_id ORDER BY 2 DESC LIMIT 4"""):
        print("   %-26s $%s" % r)

    print("\n-- biggest cash-outs, with attribution --")
    for r in q("""SELECT ro.cashout_total, ro.ante, r.deck_name,
                    (SELECT group_concat(name||'='||dollars,' ') FROM cashout_items ci
                      WHERE ci.run_id=ro.run_id AND ci.round_seq=ro.round_seq)
                  FROM rounds ro JOIN runs r USING(run_id)
                  WHERE ro.cashout_total IS NOT NULL
                  ORDER BY ro.cashout_total DESC LIMIT 4"""):
        print("   $%-3s ante %-2s %-16s %s" % r)

    print("\n-- capture defects across the corpus --")
    for r in q("SELECT defect, COUNT(*) FROM run_defects GROUP BY defect ORDER BY 2 DESC"):
        print("   %-24s %d run(s)" % r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--logs", default=DEFAULT_LOGS)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()

    ddl = open(os.path.join(HERE, "schema.sql"), encoding="utf-8").read()
    # CREATE TABLE IF NOT EXISTS adds tables but never columns, so editing
    # schema.sql would otherwise leave an existing database one column short
    # and fail at query time, far from the cause. The database is derived from
    # the logs and disposable, so drift just rebuilds it.
    want = zlib.crc32(ddl.encode("utf-8")) & 0x7FFFFFFF
    stale = False
    if not a.rebuild and os.path.exists(a.db):
        probe = sqlite3.connect(a.db)
        stale = probe.execute("PRAGMA user_version").fetchone()[0] != want
        probe.close()
        if stale:
            print("schema changed since this database was built; rebuilding")

    if a.rebuild or stale:
        for suffix in ("", "-wal", "-shm"):
            p = a.db + suffix
            if os.path.exists(p):
                os.remove(p)

    db = sqlite3.connect(a.db)
    db.executescript(ddl)
    db.execute(f"PRAGMA user_version = {want}")

    ing = Ingester(db)
    total = changed = 0
    files = sorted(glob.glob(os.path.join(a.logs, "*.jsonl")) +
                   glob.glob(os.path.join(a.logs, "*.jsonl.gz")))
    failed = []
    for p in files:
        # One unreadable log must not take the others with it. dashboard.py
        # already guards each file this way; the CLI did not, so a truncated
        # .gz aborted the run and skipped every log after it alphabetically.
        try:
            n, did = ing.ingest_file(p)
        except Exception as exc:
            failed.append((os.path.basename(p), exc))
            db.rollback()
            continue
        total += n
        changed += 1 if did else 0
    ing.derive_counters()
    db.commit()

    print(f"{len(files)} logs, {changed} re-derived, {total} events -> {a.db}")
    for name, exc in failed:
        print(f"  FAILED {name}: {type(exc).__name__}: {exc}")
    if a.report:
        report(db)


if __name__ == "__main__":
    sys.exit(main())
