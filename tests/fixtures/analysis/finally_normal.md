# Finally after normal completion

Normal completion preserves both cleanup destinations, including the function exit.

```python
def close(enabled):
    try:
        value = 1
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,5,6],"excluded":[],"possible_arcs":[[5,-1],[5,6]],"no_branch":[],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6]}]}
```
