# bh-w56ts `widening.py` output

Captured stdout of `../widening.py` against XTDB 2.2.0 on 2026-10-30 (setup: the spike doc).

```text
-- 1. timestamp then string in the same column
{"after": "timestamp row", "type": "timestamp with time zone", "json": [[1, "datetime", "2026-10-30 19:25:00+00:00"]], "transit": [[1, "datetime", "2026-10-30 19:25:00+00:00"]]}
{"after": "+ string row", "type": "json", "json": [[1, "str", "2026-10-30T19:25Z"], [2, "str", "2026-10-30T19:25:00Z"]], "transit": [[1, "str", "transit:[\"~#time/zoned-date-time\",\"2026-10-30T19:25Z[UTC]\"]"], [2, "str", "transit:[\"~#'\",\"2026-10-30T19:25:00Z\"]"]]}
  ERR SELECT _id FROM wid_ts_8ab1e2 WHERE ts < TIMESTAMP '2027-01-01T00:00:00Z' ORDER BY _id -> class java.lang.Long cannot be cast to class java.nio.ByteBuffer (java.lang.Long and java.nio.ByteBuffer are in module java.base of loader 'bootstrap')
  OK  SELECT _id FROM wid_ts_8ab1e2 WHERE ts = '2026-10-30T19:25:00Z' ORDER BY _id -> [(1,), (2,)]
  ERR SELECT _id FROM wid_ts_8ab1e2 ORDER BY ts -> compare not applicable to types instant and utf8
-- 2. bigint then string (priority 2 vs 'P2')
{"after": "int row", "type": "bigint", "json": [[1, "int", 2]], "transit": [[1, "int", 2]]}
{"after": "+ string row", "type": "json", "json": [[1, "int", 2], [2, "str", "P2"]], "transit": [[1, "str", "transit:[\"~#'\",2]"], [2, "str", "transit:[\"~#'\",\"P2\"]"]]}
  ERR SELECT _id FROM wid_int_8ab1e2 WHERE p < 3 -> class java.lang.Long cannot be cast to class java.nio.ByteBuffer (java.lang.Long and java.nio.ByteBuffer are in module java.base of loader 'bootstrap')
  OK  SELECT _id FROM wid_int_8ab1e2 WHERE p = 'P2' -> [(2,)]
-- 3. native psycopg ints: int2 then int8
{"after": "int2 row (psycopg default for small ints)", "type": "smallint", "json": [["a", "int", 2]], "transit": [["a", "int", 2]]}
{"after": "+ int8 row", "type": "bigint", "json": [["a", "int", 2], ["b", "int", 1099511627776]], "transit": [["a", "int", 2], ["b", "int", 1099511627776]]}
  OK  SELECT _id FROM wid_nat_8ab1e2 WHERE p > 1 ORDER BY _id -> [('a',), ('b',)]
-- 4. list[str] then list[int] then scalar string
{"after": "list[str]", "type": "ARRAY", "json": [[1, "list", ["a"]]], "transit": [[1, "list", ["a"]]]}
{"after": "+ list[int]", "type": "json", "json": [[1, "list", ["a"]], [2, "list", [1]]], "transit": [[1, "str", "transit:[\"a\"]"], [2, "str", "transit:[1]"]]}
{"after": "+ string", "type": "json", "json": [[1, "list", ["a"]], [2, "list", [1]], [3, "str", "x"]], "transit": [[1, "str", "transit:[\"a\"]"], [2, "str", "transit:[1]"], [3, "str", "transit:[\"~#'\",\"x\"]"]]}
  OK  SELECT x._id, v.e FROM wid_list_8ab1e2 x, UNNEST(x.l) AS v(e) ORDER BY x._id -> [(1, 'a'), (2, 1)]
-- 5. nested struct field int then string
{"after": "struct{a:int} + struct{a:str}", "type": "json", "json": [[1, "dict", {"a": 1, "b": "k"}], [2, "dict", {"a": "x"}]], "transit": [[1, "str", "transit:[\"^ \",\"a\",1,\"b\",\"k\"]"], [2, "str", "transit:[\"^ \",\"a\",\"x\"]"]]}
  OK  SELECT _id, (m).a, (m).b FROM wid_nest_8ab1e2 ORDER BY _id -> [(1, 1, 'k'), (2, 'x', None)]
  OK  SELECT _id FROM wid_nest_8ab1e2 WHERE (m).a = 1 -> [(1,)]
-- 6. real data: bd events new_value with embedded JSON decoded
  column type: json
  json: wire type_code=114 python types={'str': 13862, 'NoneType': 15657, 'dict': 7197}
  transit: wire type_code=16384 python types={'str': 13862, 'NoneType': 15657, 'frozendict': 7197}
  OK  SELECT count(*) FROM wid_ev_8ab1e2 WHERE (new_value).status = 'in_progress' -> [(1936,)]
  OK  SELECT count(*) FROM wid_ev_8ab1e2 WHERE new_value = '' -> [(7736,)]
```
