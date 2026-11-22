# Generator lifecycle

The decision is recorded when the generator yields. Calling `next()` again
resumes at the next iteration without replaying the prior decision.

```python
def values(items, flag):
    for item in items:
        if item and flag:
            yield item
        else:
            yield 0


iterator = values([False, True], True)
next(iterator)
next(iterator)
iterator.close()
```

```json
{
  "definitions": [
    {"kind": "if", "line": 3, "expression": "item and flag", "logic": "and(c0,c1)", "condition_ranges": [[3, 11, 3, 15], [3, 20, 3, 24]]}
  ],
  "observations": [
    {"kind": "if", "line": 3, "expression": "item and flag", "vector": "0-", "outcome": false},
    {"kind": "if", "line": 3, "expression": "item and flag", "vector": "11", "outcome": true}
  ]
}
```
