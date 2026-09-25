"""Compiles every Lua file in the mod.

A syntax error here does not surface until Balatro boots, and main.lua loads
its modules inside a pcall, so the failure mode is the mod quietly not
existing rather than an error anyone sees.

CAVEAT, and it has bitten this project before: lupa embeds Lua 5.5, while
Balatro runs LuaJIT, which is 5.1. Compiling here proves the syntax is valid
for 5.5, NOT that LuaJIT accepts it -- an earlier bug passed its test under
5.5 and produced run ids of "0000" in the game, because 5.5 has integers and
LuaJIT has only doubles. So the compile is backed up by a scan for library
calls that do not exist in 5.1.

    pip install lupa && python tests/test_lua_syntax.py
"""
import pathlib
import re
import json
import sys

try:
    import lupa
except ImportError:
    print("SKIP: lupa not installed")
    raise SystemExit(0)

ROOT = pathlib.Path(__file__).resolve().parent.parent
MOD = ROOT / "mod" / "BalatroDB"

L = lupa.LuaRuntime(unpack_returned_tuples=False)
# Returns nil when the chunk compiles and the message when it does not.
# `return load(...)` would hand back BOTH of load's values as a tuple, which
# is truthy either way -- so the first version of this test passed a file
# with `function broken(` appended to it.
check_lua = L.eval("""
function(src, name)
  local chunk, err = load(src, name)
  if chunk then return nil end
  return err or "did not compile"
end
""")

# Standard-library calls added after 5.1, which LuaJIT therefore lacks.
NOT_IN_51 = re.compile(
    r"\b(math\.type|math\.tointeger|math\.ult|table\.move|table\.pack|"
    r"table\.unpack|string\.pack|string\.unpack|utf8\.)")

files = sorted(MOD.rglob("*.lua"))
fails = 0

if not files:
    print("FAIL: no Lua files found under", MOD)
    raise SystemExit(1)

for path in files:
    src = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT).as_posix()

    err = check_lua(src, "@" + rel)
    if err is not None:
        fails += 1
        print(f"  FAIL {rel}\n        {err}")
        continue

    # Comment lines are skipped so prose may name these freely.
    bad = []
    for i, line in enumerate(src.splitlines(), 1):
        if line.lstrip().startswith("--"):
            continue
        m = NOT_IN_51.search(line)
        if m:
            bad.append(f"line {i}: {m.group(1)} is not in Lua 5.1 (LuaJIT)")
    if bad:
        fails += 1
        print(f"  FAIL {rel}")
        for b in bad:
            print("        " + b)
    else:
        print(f"  ok   {rel}  ({len(src.splitlines())} lines)")

# ── the two version strings agree ─────────────────────────────────────────
# BalatroDB.json is what Steamodded reads; main.lua's VERSION is what every
# log line is stamped with, because env.lua deliberately reports the constant
# the running code carries rather than the manifest. Nothing kept them in
# step, so 0.4.3 shipped stamping its logs 0.4.2 -- which defeats the point
# of bumping a version at all, since the stamp is how a reader tells which
# runs were captured by which build.
manifest = json.loads((MOD / "BalatroDB.json").read_text(encoding="utf-8"))["version"]
m = re.search(r"VERSION\s*=\s*'([^']+)'", (MOD / "main.lua").read_text(encoding="utf-8"))
lua = m.group(1) if m else None
if lua == manifest:
    print(f"  ok   version {manifest} in both BalatroDB.json and main.lua")
else:
    fails += 1
    print(f"  FAIL BalatroDB.json says {manifest!r}, main.lua says {lua!r}")

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
