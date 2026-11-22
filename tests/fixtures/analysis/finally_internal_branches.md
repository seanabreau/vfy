# Explicit cleanup destinations

Cleanup reached after a return still reports branches whose destinations are inside the
finally suite.

```python
def close(enabled):
    try:
        return 1
    finally:
        if enabled:
            save()
        cleanup()
```

```json
{"executable":[1,2,3,5,6,7],"excluded":[],"possible_arcs":[[5,6],[5,7]],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7]}]}
```
