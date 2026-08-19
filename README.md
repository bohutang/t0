# t0 — public Git transport bench (GitHub vs Cursor Origin)

Throwaway test repositories for comparing **git clone / fetch / push** over HTTPS:

| Remote | URL | Role |
| --- | --- | --- |
| GitHub | `https://github.com/bohutang/t0.git` | public control |
| Cursor Origin | `https://origin.cursor.com/databendlabs/t0.git` | Origin-hosted (not a GitHub mirror) |

This is **not** a server-capacity test of Continuity (the storage layer in [Git at any scale](https://cursor.com/blog/git-at-any-scale)). It is a single-client, same-laptop, same-object comparison of the two HTTPS git endpoints as they behave from this network.

## Reproduce

```bash
# after cloning either remote
./bench/run.sh
```

The runner writes `bench/results/<utc-stamp>.json` and `bench/results/<utc-stamp>.md`.

## What is measured

1. First push of a generated fixture (same objects to both remotes)
2. Cold clone (repeat)
3. Incremental fetch of one new commit
4. Serial small pushes (one commit, then push, repeat)
5. Concurrent cold clones

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
