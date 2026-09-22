"""Exercises the dashboard's column sorting, using the page's own code.

Every panel is sortable by clicking a header, and the sort runs on the row
objects rather than the rendered text -- a 765,450 score must not sort below
a 9,999 one because "7" < "9". This runs the real index.html script under
node against a DOM stub and checks the orders that come out.

    node must be on PATH; the check is skipped if it is not.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
page = ROOT / "ingest/web/index.html"

if not shutil.which("node"):
    print("SKIP: node not on PATH")
    raise SystemExit(0)

blocks = re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), re.S)
if not blocks:
    print("FAIL: no <script> block in index.html")
    raise SystemExit(1)

# Enough of a browser for the page to finish loading. Anything it reaches for
# during setup answers plausibly and does nothing.
STUB = """
const noop = () => {};
const el = new Proxy({}, {
  get(_, k) {
    if (k === "querySelectorAll") return () => [];
    if (k === "querySelector") return () => el;
    if (k === "addEventListener") return noop;
    if (k === "closest") return () => null;
    if (k === "setAttribute" || k === "remove" || k === "appendChild") return noop;
    if (k === "classList") return { toggle: noop, add: noop, remove: noop };
    if (k === "dataset") return {};
    if (k === "style") return {};
    return "";
  },
  set() { return true; },
});
globalThis.document = {
  querySelector: () => el, getElementById: () => null,
  createElement: () => el, body: el, addEventListener: noop,
};
globalThis.window = { isSecureContext: false };
globalThis.navigator = {};
globalThis.location = { hash: "", pathname: "/" };
globalThis.history = { replaceState: noop };
// Any field the page reads off a response answers as an empty list, which
// every call site here treats as "nothing recorded yet".
// Backed by a real array so array methods work, but any named field the
// page reads off it also answers as an empty list.
const emptyBody = new Proxy([], {
  // `then` must be undefined so the object is not treated as a thenable, and
  // `error` so get()'s error check does not fire (an empty array is truthy).
  get: (t, k) => (k in t ? Reflect.get(t, k)
                         : (k === "then" || k === "error" ? undefined : [])),
});
globalThis.fetch = () => Promise.resolve({ ok: true, json: () => emptyBody });
globalThis.setInterval = noop;
globalThis.detail = el;
"""

# Rows chosen so text order and numeric order disagree, and one row is empty.
HARNESS = """
const ROWS = [
  { key: "j_wee",    value: "765450", ord: 5.88, deck_name: "Red Deck",
    stake_key: "stake_gold",  level: 9 },
  { key: "j_runner", value: "9999",   ord: 4.00, deck_name: "Blue Deck",
    stake_key: "stake_white", level: 30 },
  { key: "j_egg",    value: "66",     ord: 1.83, deck_name: "Abandoned Deck",
    stake_key: "stake_gold",  level: 2 },
  { key: "j_none",   value: null,     ord: null, deck_name: null,
    stake_key: null,          level: null },
];

function order(id, i, dir) {
  const t = { cols: TBL[id].cols, sort: { i, dir } };
  return ROWS.slice().sort((a, b) => {
    const x = sortKey(t, a), y = sortKey(t, b);
    if (x === null && y === null) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    const d = (typeof x === "string" || typeof y === "string")
      ? String(x).localeCompare(String(y)) : (x - y);
    return d * t.sort.dir;
  }).map(r => r.key);
}

// Hand strength must not be alphabetical. Feed the real ranking in, since
// the stubbed fetch leaves SPR null.
SPR = { names: {}, hand_order: ["Flush Five", "Flush House", "Five of a Kind",
  "Straight Flush", "Four of a Kind", "Full House", "Flush", "Straight",
  "Three of a Kind", "Two Pair", "Pair", "High Card"] };
const HANDS = [
  { hand: "High Card" }, { hand: "Flush Five" }, { hand: "Pair" },
  { hand: "Straight Flush" }, { hand: "Mystery Hand" },
];
const handCol = TBL.hands.cols[0];
const handsSorted = HANDS.slice().sort((a, b) => {
  const x = handCol.sort(a), y = handCol.sort(b);
  if (x === null && y === null) return 0;
  if (x === null) return 1;
  if (y === null) return -1;
  return (y - x);
}).map(r => r.hand);

console.log(JSON.stringify({
  handsSorted,
  panels:    Object.keys(TBL),
  peak_desc: order("jokers", 1, -1),
  peak_asc:  order("jokers", 1,  1),
  name_asc:  order("jokers", 0,  1),
  deck_asc:  order("jokers", 2,  1),
  jokerCols: TBL.jokers.cols.map(c => c.label),
  handCols:  TBL.hands.cols.map(c => c.label),
  runCols:   TBL.runs.cols.map(c => c.label),
  defaults:  Object.fromEntries(Object.entries(TBL).map(([k, v]) => [k, v.sort.i])),
}));
"""

tmp = os.path.join(tempfile.gettempdir(), "balatrodb_sort.js")
pathlib.Path(tmp).write_text(STUB + blocks[0] + HARNESS, encoding="utf-8")
r = subprocess.run(["node", tmp], capture_output=True, text=True)
os.unlink(tmp)
if r.returncode:
    print("FAIL: page script did not run under node")
    for line in r.stderr.strip().splitlines()[:6]:
        print("    " + line)
    raise SystemExit(1)

res = json.loads(r.stdout.strip().splitlines()[-1])
fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name +
          (f"  {detail}" if detail and not cond else ""))


print("the page's script runs to completion")
check("all four panels defined",
      sorted(res["panels"]) == ["handlevels", "hands", "jokers", "runs"], res["panels"])

print("\nbig numbers sort numerically, not as text")
# "765450" < "9999" as text; the ordering key is what makes this come out right.
check("descending", res["peak_desc"] == ["j_wee", "j_runner", "j_egg", "j_none"],
      res["peak_desc"])
check("ascending", res["peak_asc"] == ["j_egg", "j_runner", "j_wee", "j_none"],
      res["peak_asc"])

print("\nmissing values sort last whichever way the column points")
check("last when descending", res["peak_desc"][-1] == "j_none")
check("last when ascending", res["peak_asc"][-1] == "j_none")

print("\ntext columns sort by the name shown, not the raw key")
# Display names are "Egg", "Runner", "Wee Joker"; keys are j_egg, j_runner,
# j_wee -- same order here, so check the deck column, where they differ.
check("deck ascending", res["deck_asc"] == ["j_egg", "j_runner", "j_wee", "j_none"],
      res["deck_asc"])

print("\nthe columns you asked for, and no others")
check("jokers has no Field column", res["jokerCols"] == ["Joker", "Peak", "Deck", "Stake"],
      res["jokerCols"])
check("best-hand has no Max column",
      res["handCols"] == ["Hand", "Score", "Lvl", "Deck", "Stake"], res["handCols"])
check("runs ends with Seed", res["runCols"][-1] == "Seed", res["runCols"])

print("\nhands sort by strength, not alphabetically")
check("strongest first",
      res["handsSorted"][:4] == ["Flush Five", "Straight Flush", "Pair", "High Card"],
      res["handsSorted"])
check("an unknown hand sorts last", res["handsSorted"][-1] == "Mystery Hand",
      res["handsSorted"])

print("\nevery panel opens on a sensible column")
check("all panels have a default sort",
      all(v is not None for v in res["defaults"].values()), res["defaults"])

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
