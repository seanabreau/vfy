# Finally branch

Normal completion of a conditional in a `try` suite reaches the `finally`
cleanup branch.

```python
def close(enabled):
    try:
        if enabled:
            save()
    finally:
        cleanup()
```

```json
{"executable":[1,2,3,4,6],"excluded":[],"possible_arcs":[[3,4],[3,6]],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6]}]}
```
