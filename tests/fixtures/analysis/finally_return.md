# Finally after return

Returning from the try suite leaves no normal cleanup continuation.

```python
def close(enabled):
    try:
        return 1
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,5,6],"excluded":[],"possible_arcs":[],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6]}]}
```
