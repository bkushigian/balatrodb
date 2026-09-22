"""Exercises the pure Lua logic in mod/BalatroDB/src/util.lua.

Host interpreter is whatever lupa ships (Lua 5.5); Balatro runs LuaJIT. This
checks logic and syntax, not in-game runtime behaviour. Anything touching G
needs an actual run.

    pip install lupa && python tests/test_util.py
"""
import lupa

L = lupa.LuaRuntime(unpack_returned_tuples=False)
L.execute("unpack = unpack or table.unpack")
L.execute("""
  sendWarnMessage = function(m, s) warned = (warned or 0) + 1 end
  sendInfoMessage = function() end
  G = { STATES = { SELECTING_HAND = 1, SHOP = 5, GAME_OVER = 4 } }
  BalatroDB = {}
""")
util = L.execute(open('mod/BalatroDB/src/util.lua', encoding='utf-8').read())
L.globals().BalatroDB.util = util
g = L.globals()

fails = 0


def check(name, cond, detail=""):
    global fails
    if not cond:
        fails += 1
    print(("  ok   " if cond else "  FAIL ") + name + (f"  {detail}" if detail and not cond else ""))


print("util.num -- plain values pass through")
num = util.num
for name, got, want in [
    ("int", num(42), 42),
    ("float", round(num(1.5), 3), 1.5),
    ("nil", num(None), None),
    ("string", num("hi"), "hi"),
    ("bool", num(True), True),
    ("just under the %.14g limit stays a number", type(num(L.eval("1e13"))).__name__, "float"),
]:
    check(name, got == want, f"got {got!r} want {want!r}")

print("\nutil.num -- values that cannot round-trip become {s = exact, l = sortable}")
# The test is the actual %.14g round trip, not a magnitude threshold. 1e14
# round-trips fine (as "1e+14") and must stay a plain number; what breaks is
# anything needing more than 14 significant digits.
check("1e14 round-trips, stays a number", isinstance(num(L.eval("1e14")), float),
      f"got {num(L.eval('1e14'))!r}")
# Below 1e14 but fractional. The old magnitude rule called this safe and it is
# not: %.14g renders it 12345678901234, silently dropping the .5
frac = num(L.eval("12345678901234.5"))
check("sub-1e14 fractional value is wrapped", not isinstance(frac, float), f"got {frac!r}")

big = num(L.eval("123456789012345"))
check("15-digit integer is wrapped", big is not None and not isinstance(big, float))
check("  carries exact text", isinstance(big['s'], str), f"got {big['s']!r}")
check("  carries log10 for sorting", abs(big['l'] - 14.09) < 0.01, f"got {big['l']!r}")

inf, ninf, nan = num(float('inf')), num(float('-inf')), num(float('nan'))
check("inf -> s='inf'", inf['s'] == 'inf')
check("inf sorts high", inf['l'] > 1e307)
check("-inf -> s='-inf'", ninf['s'] == '-inf')
check("-inf sorts low", ninf['l'] < -1e307)
check("nan -> s='nan'", nan['s'] == 'nan')

# A round number like -1e20 needs one significant digit, so it round-trips and
# stays a plain number -- the old magnitude rule wrapped it needlessly, which
# cost the ability to SUM it. Wrapping now requires real precision loss.
check("-1e20 round-trips, stays a number", isinstance(num(L.eval("-1e20")), float))
neg = num(L.eval("-123456789012345"))
check("negative keeps sign in l", neg['l'] < 0, f"got {neg['l']!r}")
check("negatives order correctly",
      num(L.eval("-12345678901234567890"))['l'] < num(L.eval("-123456789012345"))['l'])

# MAX() over plain text would rank '9' above '1000'; l is what makes ordering
# work in SQL, so it must be present and numeric wherever a value is ordered.
check("l is numeric, not text", isinstance(big['l'], float))

print("\nutil.num -- Talisman-style big numbers")
# A big-number table whose value fits a double AND round-trips unwraps to a
# plain number -- it is then natively sortable and summable in SQL.
plain_tali = L.eval("setmetatable({}, {__tostring = function() return '1.2345e+300' end})")
check("round-trippable big-num unwraps", isinstance(num(plain_tali), float),
      f"got {num(plain_tali)!r}")

# One that needs 17 significant digits cannot, so it keeps exact text.
tali = L.eval("setmetatable({}, {__tostring = function() return '1.2345678901234567e+300' end})")
t = num(tali)
check("exact text preserved", t['s'] == '1.2345678901234567e+300', f"got {t['s']!r}")
check("log10 recovered from exponent", abs(t['l'] - 300.09) < 0.1, f"got {t['l']!r}")
small = L.eval("setmetatable({}, {__tostring = function() return '42' end})")
check("round-trippable big-num unwraps to a number", num(small) == 42, f"got {num(small)!r}")
junk = L.eval("setmetatable({}, {__tostring = function() return 'not a number' end})")
check("unparseable keeps text, no l", num(junk)['s'] == 'not a number' and num(junk)['l'] is None)

print("\nutil.card -- a joker")
joker = L.eval("""{
  sort_id = 77,
  config = { center = { key = 'j_ride_bus', name = 'Ride the Bus' } },
  ability = { set = 'Joker', name = 'Ride the Bus', eternal = true, rental = true,
              mult = 14, extra = { odds = 4, active = true } },
  edition = { key = 'e_foil' },
  pinned = true,
  sell_cost = 3,
}""")
c = util.card(joker)
st = sorted(c['stickers'].values())
check("id carries sort_id", c['id'] == 77)
check("key kept", c['key'] == 'j_ride_bus')
# ability.name is the centre's name for EVERY card type, so reading it
# unconditionally labels every joker with a bogus enhancement.
check("no enhancement on a joker", c['enhancement'] is None, f"got {c['enhancement']!r}")
check("edition key", c['edition'] == 'e_foil')
# pinned lives on the card, not on ability.
check("stickers incl. pinned", st == ['eternal', 'pinned', 'rental'], f"got {st}")
check("numeric ability state captured", c['state']['mult'] == 14, f"got {c['state']['mult']!r}")
check("nested extra captured", c['state']['extra.odds'] == 4)
check("non-numeric extra skipped", c['state']['extra.active'] is None)

print("\nutil.card -- playing cards")
enh = L.eval("""{
  sort_id = 5, base = { value = '7', suit = 'Hearts' },
  ability = { set = 'Enhanced', name = 'Bonus Card', perma_bonus = 30 },
  seal = 'Gold',
}""")
e = util.card(enh)
check("enhancement kept when set == Enhanced", e['enhancement'] == 'Bonus Card')
check("seal kept", e['seal'] == 'Gold')
# Hiker writes here, and it is the only place its effect is observable.
check("perma_bonus captured", e['state']['perma_bonus'] == 30)

plain = util.card(L.eval("{ sort_id = 9, base = { value = '7', suit = 'Hearts' } }"))
pd = {k: v for k, v in plain.items()}
check("plain card is minimal", pd == {'id': 9, 'rank': '7', 'suit': 'Hearts'}, f"got {pd}")

print("\nutil.cards -- must never build a sparse array")
# The JSON encoder errors outright on sparse arrays, which would cost the whole
# event rather than one card.
mixed = util.cards(L.eval("{ {sort_id=1, base={value='2',suit='Clubs'}}, 'junk', {sort_id=3, base={value='4',suit='Clubs'}} }"))
ids = [mixed[i]['id'] for i in range(1, len(mixed) + 1)]
check("non-tables dropped, indices stay dense", ids == [1, 3], f"got {ids}")
check("nil input tolerated", util.cards(None) is None)

print("\nutil.card -- inert ability values are elided")
# Every card carries the whole numeric ability set whether it uses any of it or
# not. Emitting the inert ones made default zeros 44% of the first real log.
inert = util.card(L.eval("""{
  sort_id = 1, base = { value = '4', suit = 'Spades' },
  ability = { set = 'Default', name = 'Default Base',
              mult = 0, x_mult = 1, chips = 0, x_chips = 1, perma_bonus = 0 },
}"""))
idict = {k: v for k, v in inert.items()}
check("all-default state omitted entirely", idict.get('state') is None, f"got {idict.get('state')}")
check("redundant 'Default' set omitted", 'set' not in idict)
check("redundant 'Default Base' name omitted", 'name' not in idict)
check("inert card reduces to identity", idict == {'id': 1, 'rank': '4', 'suit': 'Spades'}, f"got {idict}")

live = util.card(L.eval("""{
  sort_id = 2, config = { center = { key = 'j_runner', name = 'Runner' } },
  ability = { set = 'Joker', name = 'Runner', mult = 0, x_mult = 1, chips = 45 },
}"""))
lstate = {k: v for k, v in live['state'].items()}
check("non-default value kept", lstate.get('chips') == 45)
check("default siblings dropped", 'mult' not in lstate and 'x_mult' not in lstate, f"got {lstate}")

print("\nutil.nonempty -- empty map must not serialize as []")
check("empty table -> nil", util.nonempty(L.eval("{}")) is None)
check("non-empty passes through", util.nonempty(L.eval("{1}")) is not None)

print("\nutil.hook_around -- contract")
L.execute("""
  target = { f = function(a, b) return a + b, 'second' end }
  BalatroDB.util.hook_around(target, 'f',
    function(args) return args[1] * 10 end,
    function(args, rets, pre) observed = { rets[1], rets[2], pre }; error('boom') end)
  r1, r2 = target.f(2, 3)
""")
check("ret 1 preserved", g.r1 == 5)
check("ret 2 preserved", g.r2 == 'second')
check("after saw returns", g.observed[1] == 5 and g.observed[2] == 'second')
check("before value passed to after", g.observed[3] == 20)
check("observer error swallowed", (g.warned or 0) >= 1)

# Several hooked functions signal refusal by returning false, and the mod gates
# events on that -- so `false` must survive the wrapper intact.
L.execute("""
  refuser = { f = function() return false end }
  BalatroDB.util.hook_around(refuser, 'f', nil, function(a, rets) saw_false = (rets[1] == false) end)
  passed_through = refuser.f()
""")
check("false return reaches caller", g.passed_through is False)
check("false return visible to observer", g.saw_false is True)

L.execute("""
  niller = { f = function() return nil, 'after_nil' end }
  BalatroDB.util.hook_around(niller, 'f', nil, function() end)
  n1, n2 = niller.f()
""")
check("arity preserved across embedded nil", g.n2 == 'after_nil', f"got {g.n2!r}")

before = g.warned or 0
L.execute("missing_ok = BalatroDB.util.hook_around({}, 'nope', nil, function() end)")
check("missing target warns and returns false",
      g.missing_ok is False and (g.warned or 0) > before)

print("\nutil.short_id -- must not touch math.random")
# Balatro draws on math.random for real decisions (get_pack), so consuming from
# the shared stream would shift gameplay outcomes.
L.execute("""
  math.randomseed(1)
  drew = 0
  local ref = math.random
  math.random = function(...) drew = drew + 1; return ref(...) end
  id1 = BalatroDB.util.short_id(4)
  id2 = BalatroDB.util.short_id(4)
  math.random = ref
""")
check("math.random untouched", g.drew == 0, f"called {g.drew} times")
check("id has requested length", len(g.id1) == 4, f"got {g.id1!r}")
check("ids differ", g.id1 != g.id2, f"{g.id1!r} == {g.id2!r}")

# The real failure mode is invisible from here: LuaJIT has no integers, so a
# generator whose intermediate product exceeds 2^53 silently loses its low bits
# and emits a constant. The first live run produced id "0000" for exactly this
# reason, while this host -- which has real integers -- computed it correctly
# and the test passed. So assert the invariant directly rather than by sampling.
check("intermediate product is double-safe",
      util.RNG_MAX_PRODUCT < 2 ** 53,
      f"{util.RNG_MAX_PRODUCT:.3e} exceeds 2^53 ({2 ** 53:.3e})")

L.execute("""
  local seen, n = {}, 0
  for _ = 1, 400 do
    for ch in BalatroDB.util.short_id(4):gmatch('.') do
      if not seen[ch] then seen[ch] = true; n = n + 1 end
    end
  end
  distinct_hex = n
""")
check("output covers the hex alphabet", g.distinct_hex == 16,
      f"saw {g.distinct_hex}/16 distinct chars")

print("\nFAILURES:", fails)
raise SystemExit(1 if fails else 0)
