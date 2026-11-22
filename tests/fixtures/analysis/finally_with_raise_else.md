# Executable try else after a raising with suite

Coverage.py processes the executable `else` suite as a syntactic continuation
even when the `with` body raises. Its exit keeps the cleanup branch.

```python
def close(enabled, manager):
    try:
        with manager:
            raise ValueError
    except ValueError:
        raise
    else:
        value = 3
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,5,6,8,10,11],"excluded":[],"possible_arcs":[[10,-1],[10,11]],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8,9,10,11]}]}
```
