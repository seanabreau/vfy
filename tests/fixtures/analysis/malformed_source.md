# Malformed source

Invalid Python produces an empty analysis marked as missing. It does not record
partial statements or branch arcs.

```python
def broken(:
    return 1
```

```json
{"executable":[],"excluded":[],"possible_arcs":[],"no_branch":[],"source_missing":true,"regions":[]}
```
