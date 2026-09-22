"""Copy ingest/schema.sql into the DDL block of docs/db-schema.md.

schema.sql is authoritative -- it is what actually runs. This keeps the
document from drifting away from it. Run after changing the schema.
"""
import pathlib
import re

root = pathlib.Path(__file__).resolve().parent.parent
ddl = (root / "ingest/schema.sql").read_text(encoding="utf-8")

# Drop the file's own header comment; the document supplies its own prose.
ddl = re.sub(r"\A(--[^\n]*\n)+\n", "", ddl).strip()

doc_path = root / "docs/db-schema.md"
doc = doc_path.read_text(encoding="utf-8")
new, count = re.subn(
    r"(## DDL\s*\n\nGenerated from[^\n]*\n\n```sql\n).*?(\n```)",
    lambda m: m.group(1) + ddl + m.group(2),
    doc,
    flags=re.S,
)
if not count:
    new, count = re.subn(
        r"(## DDL\s*\n\n)(?:Generated from[^\n]*\n\n)?```sql\n.*?\n```",
        lambda m: m.group(1)
        + "Generated from `ingest/schema.sql`, which is authoritative.\n\n"
        + "```sql\n" + ddl + "\n```",
        doc,
        flags=re.S,
    )
if not count:
    raise SystemExit("DDL block not found in docs/db-schema.md")

doc_path.write_text(new, encoding="utf-8")
print(f"docs/db-schema.md DDL block synced from schema.sql ({len(ddl.splitlines())} lines)")
