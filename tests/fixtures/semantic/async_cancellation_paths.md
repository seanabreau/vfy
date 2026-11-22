# Async cancellation paths

An async cleanup region has distinct normal and cancelled outcomes. The `try`
path records whether the body completed or propagated cancellation; the
cancellation decision records whether cleanup ran under task cancellation.

```python
import asyncio
from contextlib import suppress


async def worker(started, block):
    try:
        started.set()
        if block:
            await asyncio.sleep(10)
        else:
            await asyncio.sleep(0)
    finally:
        cleaned = True


async def main():
    await worker(asyncio.Event(), False)
    started = asyncio.Event()
    task = asyncio.create_task(worker(started, True))
    await started.wait()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


asyncio.run(main())
```

```json
{
  "definitions": [
    {"kind": "if", "line": 8, "expression": "block", "logic": "c0", "condition_ranges": [[8, 11, 8, 16]]}
  ],
  "observations": [
    {"kind": "if", "line": 8, "expression": "block", "vector": "0", "outcome": false},
    {"kind": "if", "line": 8, "expression": "block", "vector": "1", "outcome": true}
  ],
  "path_definitions": [
    {
      "kind": "async_cancellation",
      "line": 6,
      "expression": "try:\n        started.set()\n        if block:\n            await asyncio.sleep(10)\n        else:\n            await asyncio.sleep(0)\n    finally:\n        cleaned = True",
      "outcomes": [
        {"label": "normal completion", "range": [6, 4, 13, 22]},
        {"label": "cancelled", "range": [6, 4, 13, 22]}
      ]
    },
    {
      "kind": "try",
      "line": 6,
      "expression": "try:\n        started.set()\n        if block:\n            await asyncio.sleep(10)\n        else:\n            await asyncio.sleep(0)\n    finally:\n        cleaned = True",
      "outcomes": [
        {"label": "normal completion", "range": [6, 4, 13, 22]},
        {"label": "unhandled exception", "range": [6, 4, 13, 22]}
      ]
    }
  ],
  "path_observations": [
    {"kind": "async_cancellation", "line": 6, "outcome": 0},
    {"kind": "async_cancellation", "line": 6, "outcome": 1},
    {"kind": "try", "line": 6, "outcome": 0},
    {"kind": "try", "line": 6, "outcome": 1}
  ]
}
```
