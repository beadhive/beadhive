# bh-w56ts `fidelity.py` output

Captured stdout of `../fidelity.py` against XTDB 2.2.0 on 2026-10-30 (setup: the spike doc).

```text
case | col type | top/json | nested/json | top/transit | nested/transit
text | text | str OK | str OK | str OK | str OK
text_1mib | text | str OK | str OK | str OK | str OK
unicode | text | str OK | str OK | str OK | str OK
text_tilde | text | str OK | str OK | str OK | str OK
int | bigint | int OK | int OK | int OK | int OK
int_big | bigint | int OK | int OK | int OK | int OK
float | double precision | float OK | float OK | float OK | float OK
bool | boolean | bool OK | bool OK | bool OK | Boolean OK
ts_tagged | timestamp with time zone | datetime DIFF datetime.datetime(2026, 10, 30, 19, 25, 0, 123000, tzinfo=zo...(87 chars) | str DIFF '2026-10-30T19:25:00.123Z' | datetime DIFF datetime.datetime(2026, 10, 30, 19, 25, 0, 123000, tzinfo=zo...(87 chars) | TaggedValue DIFF time/zoned-date-time '2026-10-30T19:25:00.123Z[UTC]'
ts_instant_tag | timestamp with time zone | datetime OK | str DIFF '2026-10-30T19:25:00.123456Z' | datetime OK | TaggedValue OK
ts_plain_string | text | str OK | str OK | str OK | str OK
list_str | ARRAY | list[str] OK | list[str] OK | list[str] OK | tuple OK
list_empty | json | list OK | list OK | tuple OK | tuple OK
list_mixed | json | list[bool,int,str] OK | list[bool,int,str] OK | tuple OK | tuple OK
set_str | json | list[str] DIFF ['a', 'b'] | list[str] DIFF ['a', 'b'] | frozenset OK | frozenset OK
set_empty | json | list DIFF [] | list DIFF [] | frozenset OK | frozenset OK
struct | json | dict OK | dict OK | frozendict OK | frozendict OK
struct_deep | json | dict OK | dict OK | frozendict OK | frozendict OK
struct_empty | json | dict OK | dict OK | frozendict OK | frozendict OK
list_of_struct | json | list[dict] DIFF [{'type': 'blocks', 'at': '2026-10-30T19:25Z'}] | list[dict] DIFF [{'type': 'blocks', 'at': '2026-10-30T19:25Z'}] | tuple OK | tuple OK
json_text | text | str OK | str OK | str OK | str OK
uuid | uuid | UUID OK | str DIFF '5ce912df-0cd0-5de8-96c9-0b04d7fd512f' | UUID OK | UUID OK
keyword | USER-DEFINED | str OK | str OK | str OK | Keyword OK

-- null vs absent
user columns: ['keep', 'n']
absent n = {'b': 1}
present-null n = {'b': 1}
('absent', None)
('has-v', 'x')
('present-null', None)

-- NUL char
('a\x00b',)

-- native psycopg params in RECORDS {k: %s}
[('ts', 1184, 'datetime', 'datetime.datetime(2026, 10, 30, 19, 25, 0, 123456, tzinfo=zo...(87 chars)'), ('l', 1009, 'list[str]', "['a', 'b']"), ('j', 114, 'dict', "{'x': [1, 2], 'at': '2026-10-30T19:25:00Z'}"), ('i', 21, 'int', '7')]
```
