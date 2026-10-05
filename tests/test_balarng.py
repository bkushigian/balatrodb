"""Checks tools/balarng.py against values taken from LuaJIT itself.

The expected numbers below were printed by LuaJIT 2.1 (run through LÖVE, the
engine Balatro ships on) calling math.randomseed / math.random and the game's
own pseudohash and stream step, verbatim. A port that is off by one bit in the
Tausworthe shifts or the 13-digit rounding still produces plausible-looking
floats, so nothing short of exact agreement is a pass.

The end-to-end check -- every logged shop replayed from its seed -- is
`python tools/shopsim.py validate`, which needs a run database; this file
needs nothing.

    python tests/test_balarng.py
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from balarng import LuaRandom, Streams, _advance, parse_lua_table, pseudohash  # noqa: E402

fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


# seed -> random(), random(), random(64), random(3, 9), as LuaJIT printed them
LUAJIT = {
    0.5: (0.09657393438653461, 0.96226945770684003, 34, 4),
    0.182744094496: (0.53103315349048263, 0.21434994676958552, 63, 5),
    0.123456789: (0.79918423337772637, 0.73692115160461924, 49, 8),
    0.9999: (0.014475081673934298, 0.63534078582037634, 38, 3),
    1e-9: (0.089139610242242151, 0.99107300304648294, 44, 5),
}
r = LuaRandom()
for seed, want in LUAJIT.items():
    r.randomseed(seed)
    got = (r.random(), r.random(), r.random(64), r.random(3, 9))
    check(f"math.random after randomseed({seed})", got == want, f"{got} != {want}")

check("pseudohash of a seed", pseudohash("1F2EZ7Q5") == 0.18274409449600171)
check("pseudohash of a stream key",
      pseudohash("Joker2sho171F2EZ7Q5") == 0.94924951799470136)

st = pseudohash("cdt171F2EZ7Q5")
steps = []
for _ in range(5):
    st = _advance(st)
    steps.append(st)
check("stream steps round to 13 places the way string.format does",
      steps == [0.76461042487560005, 0.45288062004660001, 0.91536107083420004,
                0.71282181913599996, 0.36358088809849998], f"{steps}")

# A stream built from a save resumes where the save left it; one built from
# the seed alone starts from the hash. peek() must not use anything up.
s = Streams("1F2EZ7Q5")
first = s.peek("cdt17", 3)
check("peek does not advance", s.peek("cdt17", 3) == first)
check("pseudoseed returns the peeked values in order",
      [s.pseudoseed("cdt17") for _ in range(3)] == first)
resumed = Streams("1F2EZ7Q5", {"cdt17": s.state["cdt17"], "seed": "1F2EZ7Q5"})
check("a saved stream carries on from its saved value",
      resumed.pseudoseed("cdt17") == s.pseudoseed("cdt17"))

t = parse_lua_table('return {["GAME"]={["pseudorandom"]={["seed"]="AB\\"C",'
                    '["cdt1"]=0.25},["ante"]=3,["flag"]=true},[1]="x",[2]=-1.5e-3}')
check("save parser: nested string keys",
      t["GAME"]["pseudorandom"] == {"seed": 'AB"C', "cdt1": 0.25})
check("save parser: numbers, booleans and array keys",
      t["GAME"]["ante"] == 3 and t["GAME"]["flag"] is True and t[1] == "x" and t[2] == -0.0015)

print("\nFAILURES:", fails)
raise SystemExit(1 if fails else 0)
