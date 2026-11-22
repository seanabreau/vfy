# Atomic condition source ranges

Ranges use zero-based UTF-8 byte columns and exclusive ends. Nested `not`
changes polarity without widening either operand's range. Repeating `flag` on
one line produces two distinct condition ranges. The two-byte `é` distinguishes
byte offsets from character offsets for the following operand.

```python
def nested(first, second):
    if (
        not (first or
             not second)
    ):  # fmt: skip
        return True
    return False


def repeated(flag):
    if flag and flag:
        return True
    return False


def unicode_name(é, flag):
    if é and flag:
        return True
    return False


nested(True, False)
nested(False, False)
nested(False, True)
repeated(False)
repeated(True)
unicode_name(False, False)
unicode_name(True, False)
unicode_name(True, True)
```

```json
{
  "definitions": [
    {"kind": "if", "line": 3, "expression": "not (first or\n             not second)", "logic": "not(or(c0,not(c1)))", "condition_ranges": [[3, 13, 3, 18], [4, 17, 4, 23]]},
    {"kind": "if", "line": 11, "expression": "flag and flag", "logic": "and(c0,c1)", "condition_ranges": [[11, 7, 11, 11], [11, 16, 11, 20]]},
    {"kind": "if", "line": 17, "expression": "é and flag", "logic": "and(c0,c1)", "condition_ranges": [[17, 7, 17, 9], [17, 14, 17, 18]]}
  ],
  "observations": [
    {"kind": "if", "line": 3, "expression": "not (first or\n             not second)", "vector": "1-", "outcome": false},
    {"kind": "if", "line": 3, "expression": "not (first or\n             not second)", "vector": "00", "outcome": false},
    {"kind": "if", "line": 3, "expression": "not (first or\n             not second)", "vector": "01", "outcome": true},
    {"kind": "if", "line": 11, "expression": "flag and flag", "vector": "0-", "outcome": false},
    {"kind": "if", "line": 11, "expression": "flag and flag", "vector": "11", "outcome": true},
    {"kind": "if", "line": 17, "expression": "é and flag", "vector": "0-", "outcome": false},
    {"kind": "if", "line": 17, "expression": "é and flag", "vector": "10", "outcome": false},
    {"kind": "if", "line": 17, "expression": "é and flag", "vector": "11", "outcome": true}
  ]
}
```
