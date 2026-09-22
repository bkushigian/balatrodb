"""Regenerate ingest/schema.sql from the DDL block in docs/db-schema.md.

The doc is authoritative; this keeps the executable copy from drifting.
"""
import pathlib, re
root = pathlib.Path(__file__).resolve().parent.parent
doc = (root / 'docs/db-schema.md').read_text(encoding='utf-8')
m = re.search(r'## DDL\s*\n\n```sql\n(.*?)\n```', doc, re.S)
if not m:
    raise SystemExit("DDL block not found in docs/db-schema.md")
ddl = (m.group(1).replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ')
                 .replace('CREATE INDEX ', 'CREATE INDEX IF NOT EXISTS '))
(root / 'ingest/schema.sql').write_text(
    "-- Generated from docs/db-schema.md (the DDL block there is authoritative).\n"
    "-- Regenerate: python ingest/sync_schema.py\n\n" + ddl + "\n", encoding='utf-8')
print("ingest/schema.sql regenerated")
