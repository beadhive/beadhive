# bh-cntr0 `drivers.py` output

psycopg 3.3.6, pg8000 1.31.6 and asyncpg 0.32.0 against XTDB 2.2.0 pgwire on 127.0.0.1:15434 (no
password configured).

## stdout

```text
{
 "psycopg": {
  "version": "PostgreSQL 16 (XTDB 2.2.0)",
  "count": 9088,
  "param_select": "Serve the authenticated factory and exac",
  "dml_readback": [
   "psycopg",
   7
  ],
  "timestamptz": "datetime 2026-08-24T20:56:03+00:00"
 },
 "pg8000": {
  "version": "PostgreSQL 16 (XTDB 2.2.0)",
  "count": 9088,
  "param_select": "Serve the authenticated factory and exac",
  "dml_bound_params": "FAIL DatabaseError: {'V': 'ERROR', 'S': 'ERROR', 'C': 'XX000', 'M': '', 'D': '{\"category\":\"fault\",\"code\":\"xtdb.error/unk",
  "dml_literal_only": "ok",
  "dml_readback": [
   "pg8000",
   7
  ],
  "timestamptz": "datetime 2026-08-24T20:56:03+00:00"
 },
 "asyncpg": {
  "version": "PostgreSQL 16 (XTDB 2.2.0)",
  "count": 9088,
  "param_select": "Serve the authenticated factory and exac",
  "dml_bound_params": "FAIL InterfaceError: the server expects 0 arguments for this query, 3 were passed\nHINT:  Check the query agains",
  "dml_literal_only": "ok",
  "dml_readback": [
   "asyncpg",
   7
  ],
  "timestamptz": "datetime 2026-08-24T20:56:03+00:00"
 }
}
```
