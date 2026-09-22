"""Syntax-check the dashboard's inline JavaScript.

This exists because a patch once inserted `esc(` without its closing paren,
producing `${esc(j.run_id}`. The page still served, the API still answered, and
the only symptom was that it sat on "loading…" forever -- a broken script does
not announce itself. One `node --check` would have caught it.

    node must be on PATH; the check is skipped if it is not.
"""
import os
import re
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
page = ROOT / "ingest/web/index.html"

if not shutil.which("node"):
    print("SKIP: node not on PATH")
    raise SystemExit(0)

html = page.read_text(encoding="utf-8")
blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
if not blocks:
    print("FAIL: no <script> block found in index.html")
    raise SystemExit(1)

fails = 0
for i, js in enumerate(blocks):
    tmp = os.path.join(tempfile.gettempdir(), f"balatrodb_check_{i}.js")
    pathlib.Path(tmp).write_text(js, encoding="utf-8")
    r = subprocess.run(["node", "--check", tmp], capture_output=True, text=True)
    if r.returncode:
        fails += 1
        print(f"  FAIL script block {i}:")
        print("    " + (r.stderr.strip().splitlines() or ["?"])[0])
        for line in r.stderr.strip().splitlines()[1:5]:
            print("    " + line)
    else:
        print(f"  ok   script block {i} parses ({len(js.splitlines())} lines)")
    os.unlink(tmp)

# A template literal that lost its closing paren often still parses, so also
# look for the specific shape that bit us.
unclosed = re.findall(r"\$\{esc\([^${}()]*\}", html)
if unclosed:
    fails += 1
    print(f"  FAIL {len(unclosed)} unclosed esc() call(s): {unclosed[:3]}")
else:
    print("  ok   no unclosed esc() interpolations")

print("\nFAILURES:", fails)
sys.exit(1 if fails else 0)
