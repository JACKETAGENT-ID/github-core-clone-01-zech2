# zech-forge2 — github-core-clone-01 (make.fun, ZECH)

A pure-Python-stdlib, single-file (`main.py`, ~3.300 lines) GitHub-core clone:
REST v3, GraphQL, MCP, and git smart HTTP (v0/v2, shallow fetch/deepen),
in-memory store with a pack cache. No dependencies, no build step.

## Run

```
MAKEFUN_ENV_URL=http://<env-host>:<env-port> MAKEFUN_SHOP_TOKEN=<token> \
  python3 -P -s main.py
```

The entry binds an ephemeral port on **0.0.0.0** (never 127.0.0.1 — the
grading env dials the entry container's network IP), registers the port with
the env, then long-polls `/done` and exits 0.

## Practice results (local, kit env server run directly)

| run | checks | perf points |
|-----|--------|-------------|
| 1   | 102/120, working 1 | 170 |
| 2   | 119/120, working 1 | 169 |
| 3   | **120/120, working 1** | 133 |
| 4   | **120/120, working 1** | 135 |

Conformance is deterministic (120/120 twice). Perf points vary run to run on
the shared VM (same code measured 133–170); locally the read path costs
~0.35 ms/request. Projected server score ≈ 600 + 200 + 133–170 = 933–970.

## Why v1 scored 0 (diagnosis)

`zech-forge` bound `127.0.0.1`. The grader records the register request's
peer address as the entry base URL and dials it from a separate container,
so a loopback-only listener refuses the connection, the boot check fails,
and every column scores 0 — while local `grade-local` (process driver, all
on 127.0.0.1) passes. v2 binds `0.0.0.0`, verified with `ss` showing
`LISTEN 0.0.0.0:<port>`.

## Bugs fixed after run 1 (102 → 120)

- GraphQL tokenizer: commas are insignificant in GraphQL but were unhandled;
  `$` sat inside the `punct` character class so `$owner` never tokenized as
  a variable; variable definitions then failed to parse.
- GraphQL mutations returned the Issue/PR directly instead of a payload
  object (`{issue: …}`), so `createIssue { issue { … } }` errored.
- Fetch object set: `reachable_objects` pulled parent commits/trees into the
  pack, re-sending history the client already held (broke incremental and
  shallow fetches); the closure now stops at the depth boundary and excludes
  have-held objects, with the have-closure cut at shallow boundaries so a
  `deepen` actually delivers the parent commit.
- PR merge-conflict used the stale `base_sha` snapshot from PR creation
  instead of the current base-branch tip, so a conflict after an earlier
  merge went undetected (405 now works).
