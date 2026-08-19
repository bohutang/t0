#!/usr/bin/env python3
"""Agent-shaped GitHub vs Cursor Origin HTTPS bench.

Models how coding agents actually hit a forge:
  1. many short-lived working copies (concurrent clones)
  2. tiny commit + push + immediately fetch the same SHA (linearizability)
  3. a burst of serial small pushes (agent write storm)
  4. many throwaway branches created in parallel (scratch workspaces)

Does not log proxy, IP, DNS, credentials, or traceroute.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GITHUB_URL = os.environ.get("GITHUB_URL", "https://github.com/bohutang/t0.git")
ORIGIN_URL = os.environ.get("ORIGIN_URL", "https://origin.cursor.com/databendlabs/t0.git")
GIT = os.environ.get("GIT_BIN", "git")

AUTHOR_ENV = {
    "GIT_AUTHOR_NAME": "t0-bench",
    "GIT_AUTHOR_EMAIL": "t0-bench@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "t0-bench",
    "GIT_COMMITTER_EMAIL": "t0-bench@users.noreply.github.com",
}


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def now() -> float:
    return time.perf_counter()


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(AUTHOR_ENV)
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def timed(cmd: list[str], cwd: Path | None = None) -> tuple[float, subprocess.CompletedProcess[str]]:
    t0 = now()
    proc = run(cmd, cwd=cwd, check=False)
    elapsed = now() - t0
    return elapsed, proc


def stats(vals: list[float]) -> dict | None:
    if not vals:
        return None
    return {
        "n": len(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": statistics.fmean(vals),
        "median": statistics.median(vals),
        "p90": sorted(vals)[max(0, int(round(0.9 * (len(vals) - 1))))],
        "stdev": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
        "values": vals,
    }


def fmt_stats(s: dict | None) -> str:
    if not s:
        return "n/a"
    return (
        f"n={s['n']}  min={s['min']:.3f}s  median={s['median']:.3f}s  "
        f"p90={s['p90']:.3f}s  mean={s['mean']:.3f}s  max={s['max']:.3f}s"
    )


def git_ok(proc: subprocess.CompletedProcess[str], label: str) -> None:
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"{label} failed ({proc.returncode}): {err[:800]}")


def clone_into(url: str, dest: Path) -> tuple[float, Path]:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    elapsed, proc = timed([GIT, "clone", "--quiet", url, str(dest)])
    git_ok(proc, f"clone {url}")
    return elapsed, dest


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Agent-shaped forge bench")
    p.add_argument("--clones", type=int, default=16, help="concurrent cold clones per remote")
    p.add_argument("--linearizable", type=int, default=20, help="push-then-immediate-fetch cycles")
    p.add_argument("--burst", type=int, default=50, help="serial tiny pushes per remote")
    p.add_argument("--scratch", type=int, default=16, help="parallel throwaway branches")
    p.add_argument("--workers", type=int, default=8, help="thread pool size")
    return p.parse_args()


def concurrent_clones(url: str, work: Path, n: int, workers: int, prefix: str) -> dict:
    times: list[float] = []
    errors: list[str] = []
    t0 = now()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {
            ex.submit(clone_into, url, work / f"{prefix}-{i:03d}"): i for i in range(n)
        }
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                elapsed, _ = fut.result()
                times.append(elapsed)
            except Exception as e:  # noqa: BLE001 — collect and keep going
                errors.append(f"{prefix}-{i:03d}: {e}")
    wall = now() - t0
    return {
        "wall_s": wall,
        "ok": len(times),
        "errors": errors,
        "per_clone": stats(times),
    }


def linearizable_cycles(url: str, work: Path, n: int, remote_name: str) -> dict:
    """Push a unique SHA, then clone/fetch it from a second working copy.

    If the forge is linearizable, the SHA must be readable immediately.
    """
    src = work / f"lin-{remote_name}-src"
    elapsed, src = clone_into(url, src)
    run([GIT, "checkout", "-B", f"bench/lin-{remote_name}"], cwd=src)

    # Second copy starts at current main so each fetch is incremental.
    mirror = work / f"lin-{remote_name}-mirror"
    clone_into(url, mirror)
    run([GIT, "remote", "set-url", "origin", url], cwd=mirror)

    push_times: list[float] = []
    fetch_times: list[float] = []
    visible: list[bool] = []
    mismatches: list[str] = []

    for i in range(n):
        marker = src / "bench" / "lin.txt"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(f"{remote_name} {i} {utc_stamp()}\n")
        run([GIT, "add", "bench/lin.txt"], cwd=src)
        run([GIT, "commit", "-m", f"lin {remote_name} {i}"], cwd=src)
        sha = run([GIT, "rev-parse", "HEAD"], cwd=src).stdout.strip()
        p_elapsed, p_proc = timed(
            [GIT, "push", "--quiet", "origin", f"HEAD:refs/heads/bench/lin-{remote_name}"],
            cwd=src,
        )
        git_ok(p_proc, f"lin push {remote_name} {i}")
        push_times.append(p_elapsed)

        f_elapsed, f_proc = timed([GIT, "fetch", "--quiet", "origin"], cwd=mirror)
        git_ok(f_proc, f"lin fetch {remote_name} {i}")
        fetch_times.append(f_elapsed)
        got = run(
            [GIT, "rev-parse", f"origin/bench/lin-{remote_name}"],
            cwd=mirror,
            check=False,
        )
        ok = got.returncode == 0 and got.stdout.strip() == sha
        visible.append(ok)
        if not ok:
            mismatches.append(f"{remote_name} i={i} pushed={sha} got={got.stdout.strip()!r}")

    return {
        "seed_clone_s": elapsed,
        "push": stats(push_times),
        "fetch": stats(fetch_times),
        "visible_immediately": sum(visible),
        "cycles": n,
        "mismatches": mismatches,
    }


def burst_pushes(url: str, work: Path, n: int, remote_name: str) -> dict:
    src = work / f"burst-{remote_name}"
    clone_into(url, src)
    run([GIT, "checkout", "-B", f"bench/burst-{remote_name}"], cwd=src)
    times: list[float] = []
    errors: list[str] = []
    writes = src / "bench" / "burst"
    writes.mkdir(parents=True, exist_ok=True)
    t0 = now()
    for i in range(n):
        (writes / f"w-{i:03d}.txt").write_text(f"{remote_name} burst {i} {utc_stamp()}\n")
        run([GIT, "add", f"bench/burst/w-{i:03d}.txt"], cwd=src)
        run([GIT, "commit", "-m", f"burst {remote_name} {i}"], cwd=src)
        elapsed, proc = timed(
            [GIT, "push", "--quiet", "origin", f"HEAD:refs/heads/bench/burst-{remote_name}"],
            cwd=src,
        )
        if proc.returncode != 0:
            errors.append((proc.stderr or proc.stdout or "")[:400])
        else:
            times.append(elapsed)
    wall = now() - t0
    return {
        "wall_s": wall,
        "ok": len(times),
        "errors": errors,
        "per_push": stats(times),
        "throughput_ok_per_s": (len(times) / wall) if wall else None,
    }


def parallel_scratch(url: str, work: Path, n: int, workers: int, remote_name: str) -> dict:
    """Each worker clones, commits a unique branch, pushes, done — throwaway agent workspace."""

    def one(i: int) -> tuple[float, str | None]:
        dest = work / f"scratch-{remote_name}-{i:03d}"
        t0 = now()
        try:
            clone_into(url, dest)
            run([GIT, "checkout", "-B", f"bench/scratch/{remote_name}/{i:03d}"], cwd=dest)
            p = dest / "bench" / "scratch.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{remote_name} scratch {i} {utc_stamp()}\n")
            run([GIT, "add", "bench/scratch.txt"], cwd=dest)
            run([GIT, "commit", "-m", f"scratch {remote_name} {i}"], cwd=dest)
            elapsed, proc = timed(
                [
                    GIT,
                    "push",
                    "--quiet",
                    "origin",
                    f"HEAD:refs/heads/bench/scratch/{remote_name}/{i:03d}",
                ],
                cwd=dest,
            )
            git_ok(proc, f"scratch push {remote_name} {i}")
            return now() - t0, None
        except Exception as e:  # noqa: BLE001
            return now() - t0, str(e)

    times: list[float] = []
    errors: list[str] = []
    t0 = now()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(one, i) for i in range(n)]
        for fut in as_completed(futs):
            elapsed, err = fut.result()
            times.append(elapsed)
            if err:
                errors.append(err[:400])
    wall = now() - t0
    ok = len(times) - len(errors)
    return {
        "wall_s": wall,
        "ok": ok,
        "errors": errors,
        "per_workspace": stats(times),
        "throughput_ok_per_s": (ok / wall) if wall else None,
    }


def write_report(payload: dict, json_path: Path, md_path: Path) -> None:
    json_path.write_text(json.dumps(payload, indent=2) + "\n")
    sm = payload["summary"]
    gh = sm["github"]
    cu = sm["cursor"]
    md = f"""# t0 agent-shaped bench {payload["id"]}

Workload is modeled on coding agents: many working copies, tiny commits, push then immediately read.

| Scenario | GitHub | Cursor Origin |
| --- | ---: | ---: |
| Concurrent cold clones (n={payload["config"]["clones"]}, wall) | {gh["clones"]["wall_s"]:.3f}s  ok={gh["clones"]["ok"]} | {cu["clones"]["wall_s"]:.3f}s  ok={cu["clones"]["ok"]} |
| Concurrent cold clones (per clone) | {fmt_stats(gh["clones"]["per_clone"])} | {fmt_stats(cu["clones"]["per_clone"])} |
| Linearizable push (median) | {fmt_stats(gh["linearizable"]["push"])} | {fmt_stats(cu["linearizable"]["push"])} |
| Linearizable fetch-after-push (median) | {fmt_stats(gh["linearizable"]["fetch"])} | {fmt_stats(cu["linearizable"]["fetch"])} |
| SHA visible on next fetch | {gh["linearizable"]["visible_immediately"]}/{gh["linearizable"]["cycles"]} | {cu["linearizable"]["visible_immediately"]}/{cu["linearizable"]["cycles"]} |
| Burst tiny pushes (n={payload["config"]["burst"]}, wall) | {gh["burst"]["wall_s"]:.3f}s  {gh["burst"]["throughput_ok_per_s"]:.3f}/s | {cu["burst"]["wall_s"]:.3f}s  {cu["burst"]["throughput_ok_per_s"]:.3f}/s |
| Burst tiny pushes (per push) | {fmt_stats(gh["burst"]["per_push"])} | {fmt_stats(cu["burst"]["per_push"])} |
| Parallel scratch workspaces (n={payload["config"]["scratch"]}, wall) | {gh["scratch"]["wall_s"]:.3f}s  ok={gh["scratch"]["ok"]} | {cu["scratch"]["wall_s"]:.3f}s  ok={cu["scratch"]["ok"]} |

## What this is testing (from the Continuity post)

- Agents create lots of short-lived working copies → concurrent clones
- Agents push then immediately CI/fetch the same SHA → linearizability
- Agents write many tiny commits, not one giant pack → burst / scratch
- Not tested: 100-replica monorepo read scaling, S3 WAL ingest ceiling, compaction

## Environment (non-sensitive)

- OS: {payload["client"]["os"]} / {payload["client"]["arch"]}
- {payload["client"]["git"]}
- Transport: HTTPS
- GitHub: `{payload["remotes"]["github"]}`
- Origin: `{payload["remotes"]["cursor_origin"]}`
- Client network: local HTTPS forward proxy; ISP/IP/proxy address omitted
- Workers: {payload["config"]["workers"]}

## Errors

GitHub clone errors: {len(gh["clones"]["errors"])}
Origin clone errors: {len(cu["clones"]["errors"])}
GitHub linearizable mismatches: {gh["linearizable"]["mismatches"]}
Origin linearizable mismatches: {cu["linearizable"]["mismatches"]}
GitHub burst errors: {len(gh["burst"]["errors"])}
Origin burst errors: {len(cu["burst"]["errors"])}
GitHub scratch errors: {len(gh["scratch"]["errors"])}
Origin scratch errors: {len(cu["scratch"]["errors"])}

## Caveats

{chr(10).join("- " + c for c in payload["caveats"])}
"""
    md_path.write_text(md)


def main() -> int:
    args = parse_args()
    stamp = utc_stamp()
    work = ROOT / "bench" / "work" / f"agent-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    res_dir = ROOT / "bench" / "results"
    res_dir.mkdir(parents=True, exist_ok=True)

    git_ver = run([GIT, "--version"]).stdout.strip()
    uname = os.uname()

    print(f"== agent bench {stamp} clones={args.clones} lin={args.linearizable} burst={args.burst} scratch={args.scratch}")

    print("-- concurrent clones github")
    gh_clones = concurrent_clones(GITHUB_URL, work / "gh-clones", args.clones, args.workers, "gh")
    print(f"   wall={gh_clones['wall_s']:.3f}s ok={gh_clones['ok']}")
    print("-- concurrent clones origin")
    cu_clones = concurrent_clones(ORIGIN_URL, work / "cu-clones", args.clones, args.workers, "cu")
    print(f"   wall={cu_clones['wall_s']:.3f}s ok={cu_clones['ok']}")

    print("-- linearizable github")
    gh_lin = linearizable_cycles(GITHUB_URL, work, args.linearizable, "github")
    print(f"   visible={gh_lin['visible_immediately']}/{gh_lin['cycles']}")
    print("-- linearizable origin")
    cu_lin = linearizable_cycles(ORIGIN_URL, work, args.linearizable, "cursor")
    print(f"   visible={cu_lin['visible_immediately']}/{cu_lin['cycles']}")

    print("-- burst github")
    gh_burst = burst_pushes(GITHUB_URL, work, args.burst, "github")
    print(f"   wall={gh_burst['wall_s']:.3f}s ok={gh_burst['ok']} rate={gh_burst['throughput_ok_per_s']}")
    print("-- burst origin")
    cu_burst = burst_pushes(ORIGIN_URL, work, args.burst, "cursor")
    print(f"   wall={cu_burst['wall_s']:.3f}s ok={cu_burst['ok']} rate={cu_burst['throughput_ok_per_s']}")

    print("-- scratch github")
    gh_scratch = parallel_scratch(GITHUB_URL, work, args.scratch, args.workers, "github")
    print(f"   wall={gh_scratch['wall_s']:.3f}s ok={gh_scratch['ok']}")
    print("-- scratch origin")
    cu_scratch = parallel_scratch(ORIGIN_URL, work, args.scratch, args.workers, "cursor")
    print(f"   wall={cu_scratch['wall_s']:.3f}s ok={cu_scratch['ok']}")

    payload = {
        "id": stamp,
        "kind": "agent-shaped",
        "measured_at_utc": datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")
        .replace(tzinfo=timezone.utc)
        .isoformat(),
        "config": {
            "clones": args.clones,
            "linearizable": args.linearizable,
            "burst": args.burst,
            "scratch": args.scratch,
            "workers": args.workers,
        },
        "client": {
            "os": uname.sysname,
            "arch": uname.machine,
            "git": git_ver,
            "transport": "https",
            "network_notes": "Single client. HTTPS via a local forward proxy (address omitted). Geography omitted.",
        },
        "remotes": {"github": GITHUB_URL, "cursor_origin": ORIGIN_URL},
        "caveats": [
            "Single laptop talking to two HTTPS git endpoints. Not Continuity cluster ingest (120-300 push/s).",
            "Throwaway agent workspaces are branches on the shared t0 repo, not N separate repositories (GitHub delete_repo scope missing; Origin create/delete works).",
            "Linearizability check is 'fetch immediately after this client's own push', not a second region / second replica race.",
            "Proxy and underlay RTT dominate sub-second work; compare ratios, not absolute seconds, across networks.",
        ],
        "summary": {
            "github": {
                "clones": gh_clones,
                "linearizable": gh_lin,
                "burst": gh_burst,
                "scratch": gh_scratch,
            },
            "cursor": {
                "clones": cu_clones,
                "linearizable": cu_lin,
                "burst": cu_burst,
                "scratch": cu_scratch,
            },
        },
    }
    json_path = res_dir / f"agent-{stamp}.json"
    md_path = res_dir / f"agent-{stamp}.md"
    write_report(payload, json_path, md_path)
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
