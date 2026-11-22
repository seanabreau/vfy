# Excluded loop else

Excluding a loop's `else` suite leaves the loop body and its break arcs.

```python
for item in items:
    if ready(item):
        break
else:  # pragma: no cover
    raise AssertionError("unreachable")
```

```json
{"executable":[1,2,3],"excluded":[4,5],"possible_arcs":[[2,1],[2,3]],"no_branch":[],"regions":[]}
```
