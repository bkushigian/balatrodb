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
from typing import NamedTuple
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

_BEYOND_DOUBLE_COLS = None


def beyond_double_columns(db):
    """Every (table, prefix) carrying an ord/num/txt triple, from the schema.

    Discovered rather than listed so a new triple is covered the day it is
    added; the alternative is a constant that silently stops being complete.
    """
    global _BEYOND_DOUBLE_COLS
    if _BEYOND_DOUBLE_COLS is None:
        found = []
        for (table,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
            cols = {r[1] for r in db.execute(
                "PRAGMA table_info(" + table + ")")}
            if "run_id" not in cols:
                continue
            found += [(table, c[:-4]) for c in sorted(cols)
                      if c.endswith("_num") and c[:-4] + "_ord" in cols]
        _BEYOND_DOUBLE_COLS = found
    return _BEYOND_DOUBLE_COLS


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


# Jokers whose value is a function of game state rather than of anything
# they store: the counter they read, the key that reads it, the field it
# adds to, and how much per unit. Rates are the game's own `extra` values.
# Bull's ability says extra = 2, which is +2 Chips per dollar -- the rate,
# not the contribution.
#
# Lives here rather than in the dashboard because ingest needs it to turn a
# counter into a score; the dashboard imports it so the two cannot drift.
# Jokers whose value is a function of GAME state rather than their own
# ability fields, keyed by the joker: (counter, field, what it contributes).
#
# Keyed by joker and not by counter because the COUNTER is shared. Bull and
# Bootstraps both read your money, and read it differently -- 2 chips per
# dollar against 2 mult per five dollars -- so one counter feeds two jokers
# with different units. Keyed the other way round Bootstraps had nowhere to
# go: `dollars` was already Bull's.
#
# The conversion is a function rather than a rate because they are not all
# linear. Bootstraps pays per $5 and Throwback multiplies.
class Counter(NamedTuple):
    """How one state-reading joker turns a counter into a contribution."""
    metric: str                 # the counter it reads
    field: str                  # chips, mult or x_mult
    convert: object             # counter -> what it contributes
    # Whether the counter means anything when nobody holds the joker.
    #
    # For most of them it does, and that is a record worth keeping: a
    # Fortune Teller high set in a run that never owned one is a real fact
    # about the run. Supernova is the exception. Its counter is simply how
    # many times that hand has been played -- which the hand leaderboards
    # already report -- and the game counts every one of them, including
    # the thousand you played before buying it. So "if you had held it"
    # says nothing Supernova-shaped; only the plays it was actually present
    # for are its own.
    held_only: bool = False


COUNTER_JOKERS = {
    "j_supernova":      Counter("hand_plays",  "mult",   lambda c: c,
                                held_only=True),
    "j_throwback":      Counter("skips",       "x_mult", lambda c: 1 + 0.25 * c),
    "j_fortune_teller": Counter("tarots",      "mult",   lambda c: c),
    "j_stone":          Counter("stone_cards", "chips",  lambda c: 25 * c),
    "j_steel_joker":    Counter("steel_cards", "x_mult", lambda c: 1 + 0.2 * c),
    "j_bull":           Counter("dollars",     "chips",  lambda c: 2 * c),
    "j_bootstraps":     Counter("dollars",     "mult",   lambda c: 2 * (c // 5)),
}


# Jokers that COUNT DOWN. Their `to` value falls every round, so the maximum
# of it is just their starting value -- "Ice Cream peaked at 100 chips" says
# nothing about the run. Every surface that ranks jokers by peak excludes
# them; the list lived in four places and `--report` had already been missed.
DECAYING = ("j_turtle_bean", "j_popcorn", "j_ice_cream", "j_ramen")
# These are module constants, never user input.
DECAYING_SQL = "(" + ", ".join("'" + k + "'" for k in DECAYING) + ")"


# What each ability field is worth when it is doing nothing. Mult and chips
# add, so zero; X-mult multiplies, so one.
INERT_FIELD = {"chips": 0, "mult": 0, "x_mult": 1}


def counter_value(joker_key, counter):
    """What that joker contributes for a given counter reading."""
    spec = COUNTER_JOKERS.get(joker_key)
    if spec is None or counter is None:
        return None, None
    return spec.field, spec.convert(counter)


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
                # The deck is re-anchored for the same reason, and the card
                # counters restart with it: the identity asks whether the
                # events since the last KNOWN-GOOD state explain the final
                # size. Anchored on the first baseline it instead asks about
                # the whole run, so one gap before a resume condemns
                # everything after it and the check stops saying where the
                # problem is.
                if e == "run.rebaseline" and d.get("deck_cards") is not None:
                    baseline_deck = len(d.get("deck_cards") or [])
                    card_add = card_remove = 0

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
                     # Pre-0.4.3 logs carry this quantity under the old,
                     # wrong name. Same number either way.
                     as_int(d.get("hands_left_after",
                                  d.get("hands_left_before"))),
                     as_int(d.get("discards_left_before")),
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

            elif e in ("shop.offer", "pack.offer"):
                # One event carries several areas; each becomes its own
                # source so "what did the shop have" and "what did the pack
                # have" stay separable.
                areas = ((("shop", d.get("cards")), ("voucher", d.get("vouchers")),
                          ("booster", d.get("boosters")))
                         if e == "shop.offer" else (("pack", d.get("cards")),))
                for source, cards in areas:
                    for slot, c in enumerate(cards or []):
                        if not isinstance(c, dict):
                            continue
                        self.db.execute(
                            "INSERT OR REPLACE INTO offers (run_id, seg, n,"
                            " ante, round_seq, endless, source, slot, key,"
                            " set_, name, cost)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (run_id, seg, n, ante, open_round or round_seq, el,
                             source, slot, c.get("key"), c.get("set"),
                             c.get("name"), as_int(c.get("cost"))))

            elif e in ("shop.buy", "shop.sell", "shop.reroll"):
                c = d.get("card") or {}
                self.db.execute(
                    "INSERT OR REPLACE INTO shop (run_id, seg, n, ante,"
                    " round_seq, endless, action, key, set_, name, amount,"
                    " and_use) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    # Shopping happens between rounds, when open_round has
                    # already been cleared, so it is filed under the round
                    # just finished -- which is the shop you were in.
                    (run_id, seg, n, ante, open_round or round_seq, el,
                     e.split(".", 1)[1],
                     c.get("key"), c.get("set"), c.get("name"),
                     # A buy costs, a sell pays, a reroll costs. One column,
                     # since `action` already says which way it went.
                     as_int(d.get("cost") if e != "shop.sell" else d.get("value")),
                     1 if d.get("and_use") else 0))

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

        # A value too large for a double leaves `ord` set and `num` NULL,
        # so the run still RANKS correctly and only the arithmetic is wrong
        # -- SUM and AVG quietly omit it, biased low in proportion to how
        # good the run was. Asked of the stored rows rather than at the point
        # of conversion, so it holds for every column that carries the
        # triple, including ones added later.
        for table, col in beyond_double_columns(self.db):
            if self.db.execute(
                    f"SELECT 1 FROM {table} WHERE run_id = ?"
                    f"   AND {col}_ord IS NOT NULL AND {col}_num IS NULL"
                    "  LIMIT 1", (run_id,)).fetchone():
                defects.add("value_beyond_double")
                break

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
        stone = steel = best = total = 0
        for c in deck:
            if not isinstance(c, dict):
                continue
            # The enhancement is named "Stone Card", not "Stone" -- the
            # earlier comparison never matched, so deck_stone was always 0
            # and Stone Joker was unreconstructable.
            enh = c.get("enhancement") or ""
            if enh.startswith("Stone"):
                stone += 1
            # Steel Joker reads the whole deck the same way Stone Joker
            # does, so it is counted in the same pass.
            elif enh.startswith("Steel"):
                steel += 1
            stt = c.get("state")
            if isinstance(stt, dict):
                b = as_num(stt.get("perma_bonus")) or 0.0
                best = max(best, b)
                total += b
        self.db.execute(
            "UPDATE rounds SET deck_stone=?, deck_steel=?, deck_perma_max=?,"
            " deck_perma_total=? WHERE run_id=? AND round_seq=?",
            (stone, steel, best, total, run_id, round_seq))

    # -- counters the derived jokers actually read --------------------------
    def derive_abandoned(self):
        """Which paused runs a later run quietly ended.

        Balatro keeps ONE save per profile. Quitting to the menu writes
        `run.end` with `suspended`, which means "resumable" -- and it is,
        right up until you start something else, at which point the save is
        replaced and that run is over. Nothing can be logged at that moment:
        the paused run stopped writing when it was suspended, and the new
        run has no idea it displaced anything.

        So it is read across runs instead. Same profile, later start: the
        save is gone. `IS` rather than `=` on the profile because older logs
        carry no profile and must still compare equal to each other.
        """
        self.db.execute("UPDATE runs SET abandoned = 0")
        self.db.execute("""
            UPDATE runs SET abandoned = 1
             WHERE result = 'suspended'
               AND EXISTS (SELECT 1 FROM runs b
                            WHERE b.run_id <> runs.run_id
                              AND b.profile IS runs.profile
                              AND b.started_ts > runs.started_ts)""")

    def derive_records(self):
        """Work out what each run held a record for at the time it was played.

        Endless and non-endless are separate contests and are derived
        independently -- continuing past the win ante changes the scale of
        everything, so a value reached there is not competing with one
        reached before it. A run can hold both for the same subject.

        Cross-run and order-dependent, so it is recomputed wholesale rather
        than per run: inserting an older log changes what every later run was
        first to achieve.

        Strictly greater, so the run that first reached a value keeps the
        moment; a later run that merely equals it does not take it away.
        Comparison is on the ordering key, never the text -- an 8,293,927,041
        Pair sorts below 998 as a string.
        """
        self.db.execute("DELETE FROM run_records")
        order = {r[0]: i for i, r in enumerate(self.db.execute(
            "SELECT run_id FROM runs ORDER BY started_ts, run_id"))}

        # Each source yields (run, subject, value, ord, field). A joker's
        # field comes from the scale row -- it is the difference between
        # +3,200 Chips and X1.5 Mult, which must not render alike.
        sources = {
            "joker": f"""
                SELECT run_id, key subject, to_txt v, to_ord o, field FROM joker_scale
                 WHERE is_reset = 0 AND to_ord IS NOT NULL AND endless = ?
                   AND key NOT IN {DECAYING_SQL}""",
            "hand_score": """
                SELECT run_id, hand subject, score_txt v, score_ord o, 'score' FROM hands
                 WHERE hand IS NOT NULL AND score_ord IS NOT NULL AND endless = ?""",
            "hand_level": """
                SELECT run_id, hand subject, lvl_to v, lvl_to o, 'level' FROM hand_levels
                 WHERE hand IS NOT NULL AND lvl_to IS NOT NULL AND endless = ?""",
            "hand_played": """
                SELECT run_id, hand subject, COUNT(*) v, COUNT(*) o, 'played' FROM hands
                 WHERE hand IS NOT NULL AND endless = ? GROUP BY run_id, hand""",
        }

        # Counter jokers never scale, so joker_scale has nothing for them and
        # they could not set a record at all -- Bull, Supernova, Stone Joker,
        # Fortune Teller and Throwback were absent from the whole feature.
        # Their peaks come from joker_counter_peaks instead, converted from
        # the counter into what the joker actually contributes, and both of
        # their records are eligible.
        #
        # Keyed by JOKER. Rekeying joker_counter_peaks from the counter to
        # the joker -- forced by Bootstraps, which reads the same `dollars`
        # as Bull -- left this block looking COUNTER_JOKERS up by `metric`,
        # which no longer keys it. Every lookup missed, so the block silently
        # went back to producing nothing and counter jokers dropped out of
        # records again: 0 of 182 rows, and `held` NULL in all of them.
        #
        # The values are read rather than reconverted. The table stores what
        # each joker was worth at each moment precisely so no reader has to
        # apply the formula a second time.
        counter_rows = {0: [], 1: []}
        for run_id, joker_key, el, held_value, ambient_value, field in                 self.db.execute(
                    "SELECT run_id, joker_key, endless, contributed_value,"
                    "       ambient_value, field FROM joker_counter_peaks"):
            if joker_key not in COUNTER_JOKERS:
                continue
            # A held-only joker has no ambient reading, so it offers only
            # the held record -- which is the whole point of held_only.
            for value, held in ((held_value, 1), (ambient_value, 0)):
                # An inert value is not a record. `if value` catches a zero
                # chips or mult, but X-mult is inert at 1, so Steel Joker
                # with no steel cards was filing "X1" as an achievement.
                if value is not None and value != INERT_FIELD.get(field, 0):
                    counter_rows[el].append((run_id, joker_key, value, field, held))

        out = []
        for el in (0, 1):
            for want_held in (1, 0):
                best, fields = {}, {}
                for run_id, key, value, field, held in counter_rows.get(el, []):
                    if held != want_held or run_id not in order:
                        continue
                    if value > best.get((run_id, key), float("-inf")):
                        best[(run_id, key)] = value
                        fields[key] = field
                high = {}
                for (run_id, key), value in sorted(
                        best.items(), key=lambda kv: order[kv[0][0]]):
                    prior = high.get(key)
                    if prior is None or value > prior[0]:
                        prev_txt, prev_run = (None, None) if prior is None                             else (f"{prior[0]:g}", prior[1])
                        high[key] = (value, run_id)
                        out.append((run_id, "joker", key, el, fields[key],
                                    want_held, f"{value:g}", ord_num(value)[0],
                                    prev_txt, prev_run))

            for kind, sql in sources.items():
                # Best value per (run, subject) first, then walk the runs in
                # the order they were played.
                best = {}
                for run_id, subject, v, o, field in self.db.execute(sql, (el,)):
                    if run_id not in order or o is None:
                        continue
                    cur = best.get((run_id, subject))
                    if cur is None or o > cur[1]:
                        best[(run_id, subject)] = (v, o, field)
                high = {}
                for (run_id, subject), (v, o, field) in sorted(
                        best.items(), key=lambda kv: order[kv[0][0]]):
                    held = high.get(subject)
                    if held is None or o > held[0]:
                        # The displaced holder, captured while we still know
                        # it -- a later query cannot tell which run it was
                        # without redoing this whole walk.
                        prev_txt, prev_run = (None, None) if held is None                             else (held[1], held[2])
                        high[subject] = (o, str(v), run_id)
                        out.append((run_id, kind, subject, el, field, None,
                                    str(v), o, prev_txt, prev_run))
        if out:
            self.db.executemany(
                "INSERT OR REPLACE INTO run_records VALUES (" +
                ",".join("?" * 10) + ")", out)

    # The counters worth deriving: what the jokers between them read.
    COUNTER_METRICS = sorted({spec[0] for spec in COUNTER_JOKERS.values()})

    def derive_counters(self):
        """Supernova, Throwback, Fortune Teller, Stone Joker and Bull read
        GAME state rather than their own ability fields, so their value can
        never come from a joker sample -- Bull's sample says `extra = 2`,
        which is its rate, not its contribution.

        Each row is also marked with whether the joker was actually in hand
        at that moment. Without that the board credited a run with "Fortune
        Teller 83" when that run had never held one: 83 tarots were simply
        used in it. Both readings are useful, so both are kept -- `held` is
        the joker's real peak, and the counter regardless of possession is
        what it would have been worth.
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
        # Steel Joker: steel cards in the deck, from the per-round scalar.
        self.db.execute("""
            INSERT INTO joker_derived (run_id, seg, n, endless, metric, subject, value)
            SELECT run_id, seg, start_n, endless, 'steel_cards', NULL, deck_steel
              FROM rounds WHERE deck_steel IS NOT NULL AND start_n IS NOT NULL
        """)
        # Fortune Teller: tarots used so far.
        self.db.execute("""
            INSERT INTO joker_derived (run_id, seg, n, endless, metric, subject, value)
            SELECT run_id, seg, n, endless, 'tarots', NULL,
                   ROW_NUMBER() OVER (PARTITION BY run_id ORDER BY seg, n)
              FROM consumable_uses WHERE set_ = 'Tarot'
        """)
        # Bull: dollars held. The balance is already a running series.
        self.db.execute("""
            INSERT INTO joker_derived (run_id, seg, n, endless, metric, subject, value)
            SELECT run_id, seg, n, endless, 'dollars', NULL, balance
              FROM money WHERE balance IS NOT NULL
        """)

        self.derive_counter_peaks()

    def derive_counter_peaks(self):
        """The two records a counter joker can hold.

        `contributed` is the counter's value at a moment the joker was
        actually scoring -- a hand played while it was in hand. That is the
        joker's own record, and it is stricter than "owned it at some
        point": your peak dollars may well happen mid-shop, when Bull is
        contributing nothing.

        `unheld` is the highest it reached while the joker was NOT in your
        hands. A Fortune Teller record set without ever owning a Fortune
        Teller is this one -- a record about the run rather than about the
        joker.

        `ambient` is the highest it reached at all, which is the maximum of
        the other two. Stored rather than worked out at read time so the
        three can never disagree about which of them is the largest.
        """
        self.db.execute("DELETE FROM joker_counter_peaks")

        # When each joker was in hand, as (seg, lo, hi) spans per run. Samples
        # are taken at plays and at round end, so a joker bought and sold
        # without a hand in between never shows -- and never scored either.
        spans = {}
        for key in COUNTER_JOKERS:
            for run_id, seg, lo, hi in self.db.execute(
                    "SELECT run_id, seg, MIN(n), MAX(n) FROM joker_state "
                    "WHERE key = ? GROUP BY run_id, seg, card_id", (key,)):
                spans.setdefault((run_id, key), []).append((seg, lo, hi))

        plays = {}
        for run_id, seg, n, el in self.db.execute(
                "SELECT run_id, seg, n, endless FROM hands ORDER BY run_id, seg, n"):
            plays.setdefault(run_id, []).append((seg, n, el))

        series = {}
        for run_id, metric, seg, n, el, val in self.db.execute(
                "SELECT run_id, metric, seg, n, endless, value FROM joker_derived "
                "WHERE value IS NOT NULL ORDER BY run_id, metric, seg, n"):
            series.setdefault((run_id, metric), []).append((seg, n, el, val))

        # One counter can feed several jokers, and each converts it its own
        # way, so the peaks are stored per joker rather than per counter.
        # Which jokers read each counter. Usually one; `dollars` feeds two.
        readers = {}
        for jk, spec in COUNTER_JOKERS.items():
            readers.setdefault(spec.metric, []).append((jk, spec))

        out = []
        for (run_id, metric), rows_ in series.items():
            # The counter's own high-water mark does not depend on who was
            # holding anything, so it is read once for the whole series.
            ambient = {}
            for seg, n, el, val in rows_:
                if val > ambient.get(el, float("-inf")):
                    ambient[el] = val

            for joker_key, spec in readers.get(metric, []):
                field = spec.field
                held = spans.get((run_id, joker_key), [])
                inside = lambda seg, n, h=held: any(
                    s == seg and lo <= n <= hi for s, lo, hi in h)

                # Everything that happened while it was not in your hands.
                # With no spans at all that is the whole series, which is
                # the case this record exists for.
                unheld = {}
                for seg, n, el, val in rows_:
                    if not inside(seg, n) and val > unheld.get(el, float("-inf")):
                        unheld[el] = val

                contributed = {}
                if held:
                    # Carry the counter forward to each play, read it there.
                    i, cur = 0, None
                    for pseg, pn, pel in plays.get(run_id, []):
                        while i < len(rows_) and (rows_[i][0], rows_[i][1]) <= (pseg, pn):
                            cur = rows_[i][3]
                            i += 1
                        if cur is None:
                            continue
                        if inside(pseg, pn) and cur > contributed.get(pel, float("-inf")):
                            contributed[pel] = cur

                # A held-only joker has no meaningful reading for the
                # moments it was absent, so those two are not written at
                # all rather than written and then explained away.
                if spec.held_only:
                    unheld, ambient_ = {}, {}
                else:
                    ambient_ = ambient

                for el in set(ambient_) | set(contributed) | set(unheld):
                    c = contributed.get(el)
                    a = ambient_.get(el)
                    u = unheld.get(el)
                    # Converted here, once. A counter is not comparable
                    # across jokers -- 189 dollars is 378 chips to Bull and
                    # 74 mult to Bootstraps -- so the value in the joker's
                    # own unit is what every reader actually wants.
                    _f, cv = counter_value(joker_key, c)
                    _f, av = counter_value(joker_key, a)
                    _f, uv = counter_value(joker_key, u)
                    out.append((run_id, joker_key, metric, el, c, a, u, field,
                                cv, av, uv,
                                ord_num(cv)[0], ord_num(av)[0], ord_num(uv)[0]))

        if out:
            # Named, not positional: this row has fourteen columns and the
            # next person to add one should not have to count them.
            self.db.executemany(
                "INSERT OR REPLACE INTO joker_counter_peaks "
                "(run_id, joker_key, metric, endless, contributed, ambient,"
                " unheld, field, contributed_value, ambient_value,"
                " unheld_value, contributed_ord, ambient_ord, unheld_ord)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", out)


# ─── reporting ────────────────────────────────────────────────────────────
def report(db):
    q = lambda s, *p: db.execute(s, p).fetchall()
    print("\n-- runs --")
    for r in q("""SELECT substr(run_id,1,24), deck_name, stake_key, won, result,
                         went_endless, deck_size FROM runs ORDER BY started_ts"""):
        print("   %-26s %-16s %-12s won=%s %-11s el=%s deck=%s" % r)

    print("\n-- per-joker maxima, non-endless (one consistently filtered relation) --")
    for r in q(f"""
        WITH ranked AS (
          SELECT js.key, js.field, js.to_txt, js.to_ord, js.run_id,
                 ROW_NUMBER() OVER (PARTITION BY js.key, js.field
                                    ORDER BY js.to_ord DESC, CAST(js.to_txt AS REAL) DESC) rn
            FROM joker_scale js JOIN runs r USING (run_id)
           WHERE js.endless = 0 AND js.is_reset = 0
             AND js.key NOT IN {DECAYING_SQL})
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
    ing.derive_records()
    ing.derive_abandoned()
    db.commit()

    print(f"{len(files)} logs, {changed} re-derived, {total} events -> {a.db}")
    for name, exc in failed:
        print(f"  FAILED {name}: {type(exc).__name__}: {exc}")
    if a.report:
        report(db)


if __name__ == "__main__":
    sys.exit(main())
