"""A small synthetic corpus for the phase tests: six runs, each one a case the
standard / endless split has to get right, written as the mod writes them
and folded in by the real ingester.

    A  never wins; dies in ante 2
    B  wins, goes endless. Hieroglyph drops its ante before the win; the
       winning cash-out ($10) and the shop after it ($500 spike) come after
       run.win but before the latch, so today they count as standard; Pair
       is played three times on each side
    C  wins and stops at the win screen
    D  seeded Plasma deck, one big hand, dies in ante 1
    E  restarted before a hand was played
    F  still live: won, latched into endless, no run.end yet
    G  after B: a pre-win Pair of 3,500 beats every earlier standard Pair
       (B's 3,000) but not B's all-figure Pair of 4,000, set after the win --
       a standard record that is not an all record

Every number the tests assert is derivable from this file by hand; the
comments say where each comes from.
"""
import json
import pathlib
import sqlite3
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ingest"))
from ingest import Ingester  # noqa: E402

DECK = [{"id": i, "key": "c_base", "rank": "2", "suit": "Spades"} for i in range(52)]


class Run:
    """One synthetic log. `at` sets the envelope -- ante, round, endless --
    for the events that follow, as the mod stamps every event."""

    def __init__(self, ts, seed, deck_key="b_red", deck="Red Deck", seeded=False):
        self.name = f"{ts}-{seed}-00{ts % 100:02d}"
        self.events, self.n, self.a, self.r, self.el = [], 0, 1, 0, False
        self.dollars = 4
        self.add("run.start", {"ts": ts, "deck_key": deck_key, "deck": deck,
                               "stake_key": "stake_white", "stake": 1, "seed": seed,
                               "seeded": seeded, "win_ante": 8, "starting_deck_size": 52})
        self.add("run.baseline", {"deck_cards": DECK, "dollars": 4, "chips": 0})

    def add(self, e, d=None):
        self.events.append({"e": e, "v": 1, "run": self.name, "seg": 0, "n": self.n,
                            "t": float(self.n), "a": self.a, "r": self.r,
                            "el": self.el, "d": d or {}})
        self.n += 1
        return self

    def at(self, ante=None, endless=None):
        if ante is not None:
            self.a = ante
        if endless is not None:
            self.el = endless
        return self

    def round(self, ante, hands, payout=5, boss=False, wins=False):
        """A blind selected, played and cashed out: `hands` is [(hand, score)].

        `wins` makes it the win-ante boss. In the real logs run.win fires at
        ROUND_EVAL, BEFORE the winning round's round.end and cash-out -- and
        before any planet used on that screen -- so it goes there here too.
        """
        self.at(ante)
        self.add("blind.select", {"name": "Boss" if boss else "Small Blind"})
        self.r += 1
        self.add("round.start", {"name": "Small Blind", "ante": ante, "boss": boss,
                                 "chips": 300, "reward": payout})
        total = 0
        for i, (hand, score) in enumerate(hands):
            self.add("hand.play", {"hand": hand, "score": score, "level": 1,
                                   "chips_before": total, "hands_left_after": 3 - i,
                                   "discards_left_before": 3, "oneshot": i == 0,
                                   "cards": DECK[:5], "blind_chips": 300})
            total += score
        if wins:
            self.win()
        self.add("round.end", {"deck_size": 52, "total": payout, "blind": "Small Blind",
                               "deck": DECK, "items": [{"name": "blind1", "dollars": payout}],
                               "dollars_before": self.dollars, "score": total})
        return self.money(payout)

    def money(self, delta):
        self.add("money.change", {"before": self.dollars, "delta": delta})
        self.dollars += delta
        return self

    def win(self):
        # win_game fires after the win-ante boss; the game has already moved
        # the ante on, and the latch is armed but not flipped.
        self.at(ante=9)
        return self.add("run.win", {"round": self.r, "score": 1, "win_ante": 8})

    def latch(self):
        # The first blind selected after the win: from here on, endless.
        return self.at(endless=True)

    def end(self, **d):
        base = {"result": "died", "terminal": True, "won": False, "endless": self.el,
                "ante": self.a, "round": self.r, "skips": 0, "deck_size": 52,
                "final_round_score": 0, "dollars": self.dollars,
                "furthest_ante": self.a, "furthest_round": self.r}
        base.update(d)
        return self.add("run.end", base)


def corpus():
    A = Run(1700000100, "AAAAAAA1")
    A.round(1, [("Pair", 500), ("Flush", 2000)])
    A.round(2, [("Pair", 900)])
    A.end(hands_played=3, best_hand=2000)

    B = Run(1700000200, "BBBBBBB2", "b_ghost", "Ghost Deck")
    B.round(1, [("Pair", 1000)])
    B.round(5, [("Pair", 3000)])
    B.round(4, [("Pair", 2500)])                 # Hieroglyph: ante went DOWN
    B.round(8, [("Flush", 50000)], payout=10, boss=True, wins=True)
    B.money(500)                                 # the post-win shop: still standard
    B.money(-480)
    B.latch()
    B.round(9, [("Pair", 4000), ("Pair", 4000), ("Flush", 9_000_000_000)], payout=20)
    B.round(12, [("Pair", 100)], payout=7)
    B.money(1000)
    B.end(won=True, endless=True, hands_played=8, best_hand=9_000_000_000)

    C = Run(1700000300, "CCCCCCC3")
    C.round(1, [("Pair", 700)])
    C.round(8, [("Straight", 40000)], payout=10, boss=True, wins=True)
    C.end(won=True, endless=False, hands_played=2, best_hand=40000)

    D = Run(1700000400, "DDDDDDD4", "b_plasma", "Plasma Deck", seeded=True)
    D.round(1, [("Flush", 1_000_000)])
    D.end(hands_played=1, best_hand=1_000_000)

    E = Run(1700000500, "EEEEEEE5")
    E.end(result="new_run", hands_played=0, ante=1, furthest_ante=1, round=0)

    F = Run(1700000600, "FFFFFFF6", "b_ghost", "Ghost Deck")
    F.round(8, [("Pair", 600)], payout=10, boss=True, wins=True)
    F.latch()
    F.round(9, [("Pair", 800), ("Pair", 800)])
    # no run.end: still being played

    G = Run(1700000700, "GGGGGGG7")
    G.round(1, [("Pair", 3500)])
    G.end(hands_played=1, best_hand=3500)

    return {"A": A, "B": B, "C": C, "D": D, "E": E, "F": F, "G": G}


def build():
    """Ingest the corpus into a fresh in-memory database, the way the
    dashboard's sync does. Returns (db, {letter: run_id})."""
    runs = corpus()
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript((ROOT / "ingest" / "schema.sql").read_text(encoding="utf-8"))
    ing = Ingester(db)
    with tempfile.TemporaryDirectory() as tmp:
        for r in runs.values():
            p = pathlib.Path(tmp) / (r.name + ".jsonl")
            p.write_text("".join(json.dumps(e) + "\n" for e in r.events), encoding="utf-8")
            ing.ingest_file(str(p))
    ing.derive_counters()
    ing.derive_records()
    ing.derive_abandoned()
    db.commit()
    return db, {k: r.name for k, r in runs.items()}
