# Unreachable statement after return

CPython removes the assignment after `return`, so it cannot give the cleanup
suite a normal continuation.

```python
def close(enabled):
    try:
        return 1
        value = 2
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,6,7],"excluded":[],"possible_arcs":[],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7]}]}
```
