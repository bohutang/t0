# t0 — public Git transport bench (GitHub vs Cursor Origin)

Throwaway test repositories for comparing **git clone / fetch / push** over HTTPS:

| Remote | URL | Role |
| --- | --- | --- |
| GitHub | `https://github.com/bohutang/t0.git` | public control |
| Cursor Origin | `https://origin.cursor.com/databendlabs/t0.git` | Origin-hosted (not a GitHub mirror) |

This is **not** a server-capacity test of Continuity (the storage layer in [Git at any scale](https://cursor.com/blog/git-at-any-scale)). It is a single-client, same-laptop, same-object comparison of the two HTTPS git endpoints as they behave from this network.

## Reproduce

```bash
# baseline transport (clone / fetch / small push)
./bench/run.sh

# agent-shaped (concurrent clones, push-then-fetch, write burst, scratch branches)
python3 bench/agent_run.py
```

Runners write `bench/results/<utc-stamp>.*` and `bench/results/agent-<utc-stamp>.*`.

## What is measured

Baseline (`./bench/run.sh`):

1. First push of a generated fixture (same objects to both remotes)
2. Cold clone (repeat)
3. Incremental fetch of one new commit
4. Serial small pushes (one commit, then push, repeat)
5. Concurrent cold clones

Agent-shaped (`python3 bench/agent_run.py`) — the workload Continuity says it is built for:

1. Many concurrent cold clones (agent working copies)
2. Tiny push then immediately fetch the same SHA (linearizability)
3. Burst of serial tiny pushes (agent write storm)
4. Parallel throwaway branches (scratch workspaces)

## What is intentionally omitted

Client ISP, public IP, proxy software, proxy address, DNS path, and traceroute. Those are local-network details, not product metrics.

## Latest numbers

Run `20260819T034759Z` on a single Darwin/arm64 laptop over HTTPS (local forward proxy; path omitted).

| Item | GitHub | Cursor Origin |
| --- | ---: | ---: |
| First push (seed) | 3.234s | 3.526s |
| Cold clone (median, n=3) | 2.174s | 3.485s |
| Incremental fetch | 1.794s | 3.329s |
| Serial push x20 (median) | 2.929s | 2.728s |
| 4 concurrent clones (wall) | 2.176s | 3.625s |

Origin was slightly faster on tiny serial pushes; GitHub was faster on clone/fetch. This is client-path latency, not Continuity cluster throughput. Full samples: [`bench/results/20260819T034759Z.md`](bench/results/20260819T034759Z.md).

### Agent-shaped (`agent-20260819T035607Z`)

Same laptop, HTTPS. 16 concurrent clones, 20 push-then-fetch cycles, 50 tiny serial pushes, 16 parallel scratch branches. Zero errors, both remotes linearizable 20/20.

| Scenario | GitHub | Cursor Origin |
| --- | ---: | ---: |
| 16 concurrent clones (wall) | 4.79s | 7.51s |
| Clone median | 2.39s | 3.74s |
| Push then fetch: SHA visible | 20/20 | 20/20 |
| Fetch-after-push median | 1.88s | 3.28s |
| 50 tiny pushes (rate) | 0.327/s | 0.359/s |
| 16 scratch workspaces (wall) | 10.56s | 11.70s |

What the Continuity post promised vs what this client can see:

- Linearizable reads after push: **holds on both**, not an Origin exclusive.
- Tiny-write ingest: Origin ~10% faster here, nowhere near 120 push/s (that number is server-side, same-region).
- Agent working copies / clones: GitHub still faster from this network.
- Many throwaway *repositories*: not compared. Origin create/delete works; this GitHub token cannot delete repos, so scratch work stayed on branches of `t0`.

Full write-up: [`bench/results/agent-20260819T035607Z.md`](bench/results/agent-20260819T035607Z.md).
