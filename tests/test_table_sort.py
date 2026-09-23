"""Exercises the dashboard's sorting and number formatting, using the page's
own code.

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

# The page loads balatro.js first and its own inline script second, and the
# two share one global lexical scope in the browser. Concatenated here in
# that order so the test evaluates what the browser actually evaluates --
# checking the inline block alone would fail on every helper that moved.
SHARED = ROOT / "ingest/web/balatro.js"


def page_js(html):
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    return [SHARED.read_text(encoding="utf-8")] + blocks


blocks = page_js(page.read_text(encoding="utf-8"))
if len(blocks) < 2:
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
  // Set from Python: the table ids that actually exist in the markup.
  hasTable: id => TABLE_IDS.includes(id),
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
SPR = { sprites: {}, names: { j_wee: "Wee Joker" }, hand_order: ["Flush Five", "Flush House", "Five of a Kind",
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

// Formatting. Scores are whole chips; joker peaks keep their decimals,
// because a Hologram really is at x1.25.
const FMT = [
  ["12345.75", "score"], ["20987.5", "score"], ["765450", "score"],
  ["9999999999", "score"], ["10000000000", "score"], ["123456789012", "score"],
  ["1.2345e+400", "score"],
  ["2.75", "peak"], ["1.25", "peak"], ["2080", "peak"],
  [null, "peak"], ["<img src=x onerror=1>", "peak"],
];

// The four metric pickers are mutually exclusive because they all write the
// same state key -- choosing in one stops the others matching any option.
// Spy on buildPicker to see how they are wired, rather than drive the DOM.
const built = [];
const realBuildPicker = buildPicker;
buildPicker = (id, key, items, kind, px, label, onPick) =>
  built.push({ id, key, specs: items.map(i => i.k), hasHook: !!onPick });
// A counter joker carries a metric name rather than a joker key, and
// offers TWO records -- so the joker picker must emit both kinds.
METRICS = { jokers: ["j_wee"], hands: ["Pair", "Flush"],
            counters: [{ metric: "dollars", key: "j_bull", field: "chips" }] };
buildMetricPickers();
buildPicker = realBuildPicker;
const pickerProbe = {
  count: built.length,
  keys: [...new Set(built.map(b => b.key))],
  ids: built.map(b => b.id),
  // Every option's kind, not just the first: one picker offers several
  // now, and reading only specs[0] hid the ones that came after it.
  kinds: built.flatMap(b => b.specs.map(x => x.split(":")[0])),
  allHaveHook: built.every(b => b.hasHook),
  wellFormed: built.every(b => b.specs.every(x => /^[a-z_]+:.+$/.test(x))),
};

// Column sorting and metric sorting are alternatives. Picking a metric adds
// its column and takes the sort; clicking any OTHER header drops the metric.
const metricProbe = {};
state.metric = "";
defineRunsTable();
metricProbe.colsWithout = TBL.runs.cols.map(c => c.label);

state.metric = "joker:j_wee";
defineRunsTable();
metricProbe.colsWith = TBL.runs.cols.map(c => c.label);
metricProbe.sortsOnMetric = TBL.runs.sort.i === METRIC_AT;
metricProbe.label = TBL.runs.cols[METRIC_AT].label;
metricProbe.at = METRIC_AT;
metricProbe.before = TBL.runs.cols[METRIC_AT - 1].label;
// Clicking the metric's own column only flips direction.
metricProbe.ownHeaderKeeps = TBL.runs.onHeaderSort(METRIC_AT) === false
                             && state.metric === "joker:j_wee";
// Clicking another column drops the metric.
metricProbe.otherHeaderClears = TBL.runs.onHeaderSort(0) === true
                                && state.metric === "";
defineRunsTable();
metricProbe.colsAfterClear = TBL.runs.cols.map(c => c.label);

console.log(JSON.stringify({
  metricProbe,
  pickerProbe,
  fmt: FMT.map(([v, k]) => (k === "score" ? fmtScore(v) : fmt(v))),
  handsSorted,
  panels:    Object.keys(TBL),
  // Every panel must have a table element to render into, or it silently
  // draws nothing. That is the invariant worth checking -- not the list.
  orphans:   Object.keys(TBL).filter(id => !document.hasTable(id)),
  noSort:    Object.keys(TBL).filter(id =>
               TBL[id].cols.some(c => typeof c.sort !== "function")),
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

# The ids that actually exist as <table id="..."> in the page.
table_ids = re.findall(r'<table id="([^"]+)"', page.read_text(encoding="utf-8"))
PRELUDE = f"const TABLE_IDS = {json.dumps(table_ids)};\n"

tmp = os.path.join(tempfile.gettempdir(), "balatrodb_sort.js")
pathlib.Path(tmp).write_text(PRELUDE + STUB + chr(10).join(blocks) + HARNESS, encoding="utf-8")
r = subprocess.run(["node", tmp], capture_output=True, text=True,
                   encoding="utf-8")   # node emits UTF-8; the Windows
                                       # locale would mangle an em dash
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
check("panels are defined", len(res["panels"]) >= 4, res["panels"])
check("every panel has a table to render into", res["orphans"] == [],
      f"defined but absent from the markup: {res['orphans']}")
check("every column declares how to sort itself", res["noSort"] == [],
      f"panels with an unsortable column: {res['noSort']}")

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

print("\nthe columns that were removed stay removed")
# The joker's scaling field and the best-hand "Max" column were both removed
# deliberately; "Sort runs" was dead UI. Assert their absence, not an exact
# column list, which breaks on any unrelated addition without meaning anything.
check("jokers has no Field column", "Field" not in res["jokerCols"], res["jokerCols"])
check("best-hand has no Max column", "Max" not in res["handCols"], res["handCols"])
check("runs still ends with Seed", res["runCols"][-1] == "Seed", res["runCols"])
check("runs shows rounds won", "Rounds won" in res["runCols"], res["runCols"])

print("\nhands sort by strength, not alphabetically")
check("strongest first",
      res["handsSorted"][:4] == ["Flush Five", "Straight Flush", "Pair", "High Card"],
      res["handsSorted"])
check("an unknown hand sorts last", res["handsSorted"][-1] == "Mystery Hand",
      res["handsSorted"])

print("\nscores are whole numbers, joker peaks keep their decimals")
got = dict(zip(
    ["12345.75", "20987.5", "765450", "9999999999", "10000000000",
     "123456789012", "1.2345e+400", "2.75", "1.25", "2080", "null", "xss"],
    res["fmt"]))
check("a fractional score truncates", got["12345.75"] == "12,345", got["12345.75"])
check("truncates rather than rounds up", got["20987.5"] == "20,987", got["20987.5"])
check("a whole score is untouched", got["765450"] == "765,450", got["765450"])
check("9,999,999,999 still reads as digits",
      got["9999999999"] == "9,999,999,999", got["9999999999"])
check("ten billion switches to an exponent",
      got["10000000000"] == "1.00e10", got["10000000000"])
check("and stays there above it", got["123456789012"] == "1.23e11", got["123456789012"])
# Number() gives Infinity for this one, so it is read off the string instead.
check("a value beyond a double still formats",
      got["1.2345e+400"] == "1.23e400", got["1.2345e+400"])
check("a joker peak keeps its fraction", got["2.75"] == "2.75", got["2.75"])
check("and its quarter", got["1.25"] == "1.25", got["1.25"])
check("a big peak still groups", got["2080"] == "2,080", got["2080"])
check("nothing renders as an em dash", got["null"] == "—", got["null"])
check("a non-numeric string is escaped", "&lt;img" in got["xss"], got["xss"])

print("\nthe four metric pickers are mutually exclusive")
pp = res["pickerProbe"]
check("four pickers are built", pp["count"] == 4, pp["ids"])
check("all write the same state key, so only one can be active",
      pp["keys"] == ["metric"], pp["keys"])
check("each redraws its siblings when picked", pp["allHaveHook"])
# The kinds are a contract between the page and the server. Compare against
# the real dict rather than a copy of it, so the two cannot drift apart.
try:
    sys.path.insert(0, str(ROOT / "ingest"))
    from dashboard import METRICS as SERVER_METRICS
except Exception as exc:                                   # noqa: BLE001
    print(f"  SKIP server contract ({exc})")
else:
    check("every kind the pickers emit is one the server accepts",
          set(pp["kinds"]) <= set(SERVER_METRICS),
          f"page {sorted(set(pp['kinds']))} vs server {sorted(SERVER_METRICS)}")
    check("and the pickers cover every kind the server offers",
          set(pp["kinds"]) == set(SERVER_METRICS),
          f"page {sorted(set(pp['kinds']))} vs server {sorted(SERVER_METRICS)}")
check("every option is a well-formed kind:subject spec", pp["wellFormed"])

print("\na metric sort and a column sort are alternatives")
mp = res["metricProbe"]
check("no metric column when none is picked",
      "Wee Joker peak" not in mp["colsWithout"], mp["colsWithout"])
check("picking one inserts its column at the metric slot",
      mp["colsWith"][mp["at"]] == "Wee Joker peak", mp["colsWith"])
check("which sits just after Records", mp["before"] == "Records", mp["colsWith"])
check("and appears exactly once",
      mp["colsWith"].count("Wee Joker peak") == 1, mp["colsWith"])
check("and it takes the sort", mp["sortsOnMetric"])
check("clicking its own header only flips direction", mp["ownHeaderKeeps"])
check("clicking another header drops the metric", mp["otherHeaderClears"])
check("and the column goes with it",
      mp["colsAfterClear"] == mp["colsWithout"], mp["colsAfterClear"])

print("\nevery panel opens on a sensible column")
check("all panels have a default sort",
      all(v is not None for v in res["defaults"].values()), res["defaults"])

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
