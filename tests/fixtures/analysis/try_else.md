# Excluded try and else suites

An excluded conditional removes its nested `try`, handler, and `else` suites.

```python
if enabled:  # pragma: no cover
    try:
        value = load()
    except LookupError:
        value = None
    else:
        use(value)
```

```json
{"executable":[],"excluded":[1,2,3,4,5,6,7],"possible_arcs":[],"no_branch":[],"regions":[]}
```
