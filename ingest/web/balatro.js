/* Everything both pages need: formatting, the game's art and names, the
   record vocabulary, the tooltip layer and the picker.

   A plain script, not a module -- no build step, and each page still loads
   as a file the server hands over unchanged. Top-level const/let in classic
   scripts share one global lexical scope, so a page's own script sees
   these by name and can define the rest on top. */

// ── page plumbing ───────────────────────────────────────────────────────

const $ = s => document.querySelector(s);

// Everything rendered here came out of a log file, and a log can carry a
// modded deck or joker name verbatim. These strings go into innerHTML, so they
// are escaped at the point of interpolation rather than trusted.
const esc = v => String(v ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Two independent toggles rather than three exclusive options. Both on (or
// both off) means no phase filter at all -- which is what "all runs" should
// mean, and is not expressible with mutually exclusive buttons.
const phase = { "0": true, "1": true };
const state = { deck: "", stake: "", endless: "", noplasma: "", metric: "",
                seeded: "", held: "" };

function syncPhase() {
  const on = Object.keys(phase).filter(k => phase[k]);
  // exactly one selected -> filter to it; otherwise include everything
  state.endless = on.length === 1 ? on[0] : "";
}

// ── formatting numbers the game's way ───────────────────────────────────

// Big numbers arrive as exact decimal text and can exceed Number range, so
// format from the string rather than parseFloat where possible.
// Past ten billion the digits stop being readable and only the magnitude
// means anything, so numbers switch to A.BBe12 there.
const EXP_AT = 1e10;

// A value the log carries but a double cannot hold. These arrive as exact
// decimal strings -- either a very long run of digits or already in
// exponential form -- so the exponent is counted off the string rather than
// computed, which would have overflowed to Infinity.
const sciFromString = str => {
  const m = /^([+-]?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(str);
  if (!m) return null;
  const [, sign, int, frac = "", exp] = m;
  const lead = int.replace(/^0+/, "");
  const digits = (lead + frac) || "0";
  const e = Math.max(lead.length - 1, 0) + (exp ? Number(exp) : 0);
  return `${sign}${digits[0]}.${digits.slice(1, 3).padEnd(2, "0")}e${e}`;
};

const fmtNum = (v, whole) => {
  if (v === null || v === undefined || v === "") return "—";
  const str = String(v).trim();
  const n = Number(str);
  if (!isFinite(n)) {
    // Escaped: fmt is handed TEXT columns straight out of the log
    // (score_txt, to_txt, required_txt), and a modded joker can put
    // anything in them. Every caller interpolates into innerHTML.
    return sciFromString(str) ?? esc(str);
  }
  if (Math.abs(n) >= EXP_AT) return n.toExponential(2).replace("e+", "e");
  // Balatro scores in whole chips, so a .75 is an artefact of the maths
  // rather than something the player saw. Truncated, not rounded: rounding
  // up would report more than was actually scored.
  if (whole) return Math.trunc(n).toLocaleString();
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
};

const fmt = v => fmtNum(v, false);
// Scores and chip requirements: whole numbers only.
const fmtScore = v => fmtNum(v, true);
const ago = ts => {
  if (!ts) return "—";
  const d = new Date(ts * 1000), s = (Date.now() / 1000) - ts;
  if (s < 86400) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
};

// ── the game's own art, names and hand order ────────────────────────────

// Atlas metadata, extracted from the game by ingest/extract_assets.py. Absent
// if that has not been run, in which case everything falls back to text.
let SPR = null;
// Awaited rather than raced: the pickers, the joker names and the hand
// strength sort all read it, and losing the race left them silently inert.
const SPR_READY = fetch("assets/sprites.json")
  .then(r => (r.ok ? r.json() : null))
  .then(d => { SPR = d; })
  .catch(() => {});

// The Planet card that levels a hand. Spelled out at three call sites
// before this existed, each with its own fallback.
const planetOf = h => (SPR && SPR.hand_planets && SPR.hand_planets[h]) || "";

function sprite(key, kind, px) {
  if (!SPR || !SPR.sprites || !key || !SPR.sprites[key]) return "";
  const s = SPR.sprites[key], a = SPR.atlases[s.a];
  if (!a) return "";
  const scale = px / a.cw;
  // The game's name for it, not the asset key: hovering a stake said
  // "stake_white" and a joker "j_wee". Playing-card layers carry their own
  // combined title on the wrapper instead.
  const tip = kind.startsWith("pc") ? "" : ` title="${esc(nameOf(key))}"`;
  // Six jokers are two cells: a bordered card with nothing on it, and a
  // "soul" drawn over it -- Hologram and the five Legendaries. Drawing
  // only the base showed them as blank cards. Two background layers in the
  // one element rather than two elements, so every caller is unchanged and
  // nothing has to learn to stack. The soul is listed FIRST because CSS
  // paints the first background layer on top.
  const at = c => `-${(c.x * a.cw * scale).toFixed(1)}px `
                + `-${(c.y * a.ch * scale).toFixed(1)}px`;
  const layers = s.soul ? [s.soul, s] : [s];
  return `<span class="spr ${kind}"${tip} style="
      width:${px}px; height:${(a.ch * scale).toFixed(1)}px;
      background-image:${layers.map(() => `url(assets/${s.a})`).join(",")};
      background-size:${(a.w * scale).toFixed(1)}px ${(a.h * scale).toFixed(1)}px;
      background-position:${layers.map(at).join(",")}"></span>`;
}

// The game's own display names, from localization/en-us.lua. Without them a
// key has to be un-snake-cased, which gives "ride the bus" and "mail in
// rebate" -- and mixed casing against the panels that do have real names.
// Poker hands sort by strength, not alphabetically -- "Flush Five" belongs
// above "High Card", not next to it. The ranking is the game's own, from
// evaluate_poker_hand; anything unrecognised sorts last.
const handRank = hand => {
  const o = (SPR && SPR.hand_order) || [];
  const i = o.indexOf(hand);
  return i < 0 ? null : o.length - i;
};

// ── a scaling value, written the way the game writes it ─────────────────

// The Planet card that levels a hand, from the game's own centre data. It
// gives the hand panels real art, and with it their rows match the joker
// rows' height instead of sitting at a different rhythm.
// A scaling value, written the way the game writes it: chips blue and
// additive, mult red and additive, X-mult red with the X. Only jokers that
// are actually accumulating get one -- a static `extra` is not a score.
const SCALE = {
  chips:       ["sc-chips", "+", ""],
  mult:        ["sc-mult",  "+", ""],
  x_mult:      ["sc-xmult", "X", ""],
  h_size:      ["sc-hand",  "+", " hand"],
  extra_value: ["sc-money", "+$", ""],
};
const scaleTag = sc => {
  if (!sc || sc.value === null || sc.value === undefined) return "";
  const [cls, pre, post] = SCALE[sc.field] || ["sc-other", "+", ""];
  return `<span class="sc ${cls}" title="${esc(sc.field)}"
    >${pre}${fmt(sc.value)}${post}</span>`;
};

// ── records, and the panel that explains one ────────────────────────────

const RECORD_KIND = {
  joker:       ["sc-mult",  k => nameOf(k), "peak",       fmt],
  hand_score:  ["sc-chips", h => h,         "best score", fmtScore],
  hand_level:  ["sc-level", h => h,         "level",      fmt],
  hand_played: ["sc-money", h => h,         "played",     fmt],
};
const recFmt = rec => (RECORD_KIND[rec.kind] || [, , , fmt])[3];
// A joker record is whatever that joker scales -- chips, mult or X-mult --
// so it is coloured by its field rather than by being a joker.
const FIELD_CLS = { chips: "sc-chips", mult: "sc-mult", x_mult: "sc-xmult" };
const recCls = rec => FIELD_CLS[rec.field]
  || (RECORD_KIND[rec.kind] || ["sc-other"])[0];
const recPre = rec => rec.field === "x_mult" ? "X" : "";
// A counter joker's record carries the same seal the board uses, so which
// of its two records this is reads the same way in both places.
const recordArt = (rec, px) => {
  const art = rec.kind === "joker"
    ? sprite(rec.subject, "j", px || 34)
    : sprite((SPR && SPR.hand_planets && SPR.hand_planets[rec.subject]) || "",
             "j", px || 34);
  if (!art) return "";
  return rec.held === null || rec.held === undefined ? art
    : `<span class="jart">${art}${handIcon(rec)}</span>`;
};
const recordText = rec => {
  const [, name, what] = RECORD_KIND[rec.kind] || ["", x => x, ""];
  return `${name(rec.subject)} ${what} ${recFmt(rec)(rec.value_txt)}`
    + (rec.endless ? " (endless)" : "");
};
const RECORDS_SHOWN = 2;

// Records get a real panel rather than a title attribute: a title cannot be
// styled, and there is more to say than fits on one line. The records
// themselves are held here and referenced by index, so nothing has to be
// serialised into an attribute and escaped back out.
let TIPS = [];

function tipHTML(rec) {
  const [, name, what] = RECORD_KIND[rec.kind] || ["sc-other", x => x, ""];
  const cls = recCls(rec), f = recFmt(rec);
  const beat = rec.prev_txt === null || rec.prev_txt === undefined
    ? `<div class="none">First of its kind — nothing to beat.</div>`
    : `<div class="beat">Beat <b>${f(rec.prev_txt)}</b>` +
      (rec.prev_deck ? ` · ${esc(rec.prev_deck)}` : "") +
      (rec.prev_ts ? ` · ${ago(rec.prev_ts)}` : "") + `</div>`;
  return `<div class="row">${recordArt(rec, 44)}<div>
      <div class="ttl">${esc(name(rec.subject))}</div>
      <div class="sub">${esc(what)}${rec.endless ? " · endless" : ""}</div>
      <div class="big ${cls}">${recPre(rec)}${f(rec.value_txt)}</div>
    </div></div>${beat}`;
}

function showTip(el, html) {
  const tip = $("#tip");
  tip.innerHTML = html;
  tip.hidden = false;
  // Placed under the thing it describes, nudged back inside the viewport
  // rather than allowed to run off the right or bottom edge.
  const r = el.getBoundingClientRect(), t = tip.getBoundingClientRect();
  let x = r.left, y = r.bottom + 8;
  if (x + t.width > innerWidth - 8) x = innerWidth - t.width - 8;
  if (y + t.height > innerHeight - 8) y = r.top - t.height - 8;
  tip.style.left = Math.max(8, x) + "px";
  tip.style.top = Math.max(8, y) + "px";
}

document.addEventListener("mouseover", e => {
  const el = e.target.closest("[data-tip]");
  if (!el) return;
  const rec = TIPS[+el.dataset.tip];
  if (rec) showTip(el, tipHTML(rec));
});
document.addEventListener("mouseout", e => {
  if (e.target.closest("[data-tip]")) $("#tip").hidden = true;
});
document.addEventListener("scroll", () => { $("#tip").hidden = true; }, true);

// ── display names ───────────────────────────────────────────────────────

// Stake order is the game's, not the alphabet's. Filled from /api/meta by
// whichever page loads it; an unknown key sorts last rather than at white.
let STAKE_ORD = {};
const setStakeOrder = stakes => {
  STAKE_ORD = Object.fromEntries((stakes || []).map(s => [s.k, s.o]));
};
const stakeOrd = key => (key in STAKE_ORD ? STAKE_ORD[key] : null);

const nameOf = (key, fallback) =>
  (SPR && SPR.names && SPR.names[key]) || fallback ||
  (key || "").replace(/^(j|b|m|c)_/, "").replace(/_/g, " ");

// ── the summary tiles ──────────────────────────────────────
// Both pages open on the same six numbers, off the same /api/summary. The
// list lives here so adding a tile, or changing what one says, is one edit
// rather than two that have to be kept in step.
//
// Each row is [label, value, sub-label, tone]. The tone is the colour the
// game would use for that quantity: scores in mult red, money in gold,
// counts in purple.
const summaryTiles = s => [
  ["Best score", fmtScore(s.best_hand), esc(s.best_hand_name || ""), "mult"],
  ["Most money", s.max_money !== null ? "$" + fmt(s.max_money) : "—",
   "peak balance", "money"],
  ["Furthest ante", fmt(s.max_ante), "", "gold"],
  ["Biggest cash-out", s.max_cashout !== null ? "$" + fmt(s.max_cashout) : "—",
   "one round", "money"],
  ["Largest deck", fmt(s.max_deck), "cards", "chips"],
  ["Runs", fmt(s.runs), `${s.won} won`, "purple"],
  // Only when there is any. A "Deepest debt: $0" tile is a column of the
  // page spent saying nothing happened.
  ...(s.max_debt < 0
      ? [["Deepest debt", "-$" + fmt(-s.max_debt), "below zero", "mult"]]
      : []),
];

// Renders them into `el`. The values are already formatted and escaped by
// the list above; the labels are literals.
function drawSummary(el, s) {
  if (!el) return;
  el.innerHTML = summaryTiles(s).map(([k, v, n, c]) =>
    `<div class="tile"><div class="k">${k}</div><div class="v ${c}">${v}</div>
     <div class="n">${n}</div></div>`).join("");
}

// The one-line version under the title, which both pages also share.
const summaryLine = s =>
  `${s.runs} runs · ${s.won} won` +
  (s.win_pct !== null ? ` · ${s.win_pct}% win rate` : "");

// Shown when the server is running code older than what is on disk. Both
// pages poll /api/version already, so this only needs somewhere to appear.
//
// A server too old to KNOW it is old reports no `stale` field at all, which
// is itself the older case -- so an absent field is not treated as false
// forever; the banner simply cannot help until the first restart after this
// shipped.
function showStale(on) {
  let el = $("#stalebar");
  if (!on) { if (el) el.remove(); return; }
  if (el) return;
  el = document.createElement("div");
  el.id = "stalebar";
  el.className = "stalebar";
  el.textContent = "The dashboard server is running older code than the "
    + "files on disk. Restart it to pick up the changes.";
  document.body.prepend(el);
}

// ── talking to the API ──────────────────────────────────────────────────

const qs = () => {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(state)) if (v !== "") p.set(k, v);
  return p.toString();
};
const get = path => fetch(path + "?" + qs()).then(r => {
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}).then(d => {
  // The server answers errors with a 200 and {"error": ...}; without this
  // the object flowed on and rendered as "undefined runs".
  if (d && d.error) throw new Error(`${path}: ${d.error}`);
  return d;
});

// ── held / not-held, as a seal ──────────────────────────────────────────

// Which of the two a counter joker's record is, said with a hand rather
// than with the words "IF HELD": the hand cut out of Four Fingers for one
// it actually held, and the same hand crossed out for the counter reached
// with nobody holding it.
const handIcon = r => r.held === undefined || r.held === null ? "" :
  `<img class="hand" src="assets/hand_${r.held ? "held" : "unheld"}.png"
     alt="${r.held ? "set while held" : "set with nobody holding it"}">`;

// ── the picker ──────────────────────────────────────────────────────────

// A dropdown that shows each option's own art. Options are real buttons so
// Tab and Enter work; a native <select> cannot carry an image, and a row of
// buttons per deck stops fitting once every deck is in play.
// `store` is where the choice is kept, defaulting to the filter state.
// A page with a selection that is NOT a server filter -- which joker is on
// screen, say -- passes its own object, so the choice stays out of the
// query string instead of riding along as a parameter nothing reads.
function buildPicker(id, key, items, kind, px, allLabel, onPick, store) {
  const root = $("#" + id);
  const held = store || state;
  const opts = [{ k: "", n: allLabel }].concat(items);
  // An item may carry its own art (the metric list mixes jokers and
  // planets), and a group entry is a heading rather than a choice.
  const face = it => (it.art !== undefined ? it.art
    : (it.k ? sprite(it.k, kind, px) || "" : "")) + `<span>${esc(it.n)}</span>`;

  function render() {
    const cur = opts.find(o => o.k === held[key]) || opts[0];
    root.innerHTML =
      `<button class="seg pixel-pill" aria-haspopup="listbox" aria-expanded="false"
        >${face(cur)}<span class="caret">▾</span></button>` +
      `<div class="menu" role="listbox">` +
      opts.map((o, i) => o.group
        ? `<div class="grp">${esc(o.n)}</div>`
        : `<button role="option" data-i="${i}"
        aria-selected="${o.k === held[key]}">${face(o)}</button>`).join("") +
      `</div>`;

    const btn = root.firstElementChild;
    const close = () => {
      root.classList.remove("open");
      btn.setAttribute("aria-expanded", "false");
    };
    btn.onclick = ev => {
      ev.stopPropagation();
      const open = !root.classList.contains("open");
      // Only one menu open at a time.
      document.querySelectorAll(".pick.open").forEach(o => {
        o.classList.remove("open");
        o.firstElementChild.setAttribute("aria-expanded", "false");
      });
      root.classList.toggle("open", open);
      btn.setAttribute("aria-expanded", open);
    };
    root.querySelector(".menu").onclick = ev => {
      const b = ev.target.closest("button[role=option]");
      if (!b) return;
      ev.stopPropagation();
      held[key] = opts[+b.dataset.i].k;
      close();
      render();
      // Sibling pickers share this key, so they have to redraw: choosing a
      // joker has to clear whatever the hand pickers were showing.
      if (onPick) onPick();
      // Each page defines its own refresh; the picker does not care which.
      if (typeof refresh === "function") refresh();
    };
    root.onkeydown = ev => { if (ev.key === "Escape") { close(); btn.focus(); } };
  }
  render();
}

document.addEventListener("click", () => {
  document.querySelectorAll(".pick.open").forEach(o => {
    o.classList.remove("open");
    o.firstElementChild.setAttribute("aria-expanded", "false");
  });
});
