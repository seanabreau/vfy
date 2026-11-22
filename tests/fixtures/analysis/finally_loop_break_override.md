# Loop break replaced by cleanup raise

Coverage.py retains the loop's syntactic break exit after the inner `finally`
raises. The outer cleanup therefore has a normal branch destination.

```python
def close(enabled):
    try:
        while True:
            try:
                break
            finally:
                raise ValueError
    finally:
        if enabled:
            pass
```

```json
{"executable":[1,2,3,4,5,7,9,10],"excluded":[],"possible_arcs":[[9,-1],[9,10]],"no_branch":[3],"regions":[{"kind":"function","name":"close","start":1,"lines":[2,3,4,5,6,7,8,9,10]}]}
```
