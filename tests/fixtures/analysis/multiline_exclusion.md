# Excluded if suite

The pragma on the single-line `if` header excludes that header and its suite
while retaining the `else` suite. The multiline-header case is checked separately
by `test_multiline_if_exclusion_preserves_else_suite` in
`tests/integration/test_analysis_compat.py`.

```python
flag = True
if flag:  # pragma: no cover
    excluded = 1
else:
    included = 2
```

```json
{"executable":[1,5],"excluded":[2,3],"possible_arcs":[],"no_branch":[],"regions":[]}
```
