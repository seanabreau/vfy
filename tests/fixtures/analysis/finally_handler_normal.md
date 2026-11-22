# Normal handler cleanup

A handler that completes normally preserves the cleanup fallthrough branch even when the
try suite raises.

```python
def close(enabled):
    try:
        raise ValueError
    except ValueError:
        value = 1
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,5,7,8],"excluded":[],"possible_arcs":[[7,-1],[7,8]],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8]}]}
```
