#!/usr/bin/env bash
# Public, reproducible GitHub vs Cursor Origin HTTPS transport bench.
# Does not log proxy, IP, DNS, or credential material.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

GITHUB_URL="${GITHUB_URL:-https://github.com/bohutang/t0.git}"
ORIGIN_URL="${ORIGIN_URL:-https://origin.cursor.com/databendlabs/t0.git}"

STAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
WORK="$ROOT/bench/work/$STAMP"
RES_DIR="$ROOT/bench/results"
FIXTURE="$ROOT/bench/fixture"
mkdir -p "$WORK" "$RES_DIR" "$FIXTURE"

GIT_BIN="${GIT_BIN:-git}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

# Keep commits anonymous in this public test repo.
export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-t0-bench}"
export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-t0-bench@users.noreply.github.com}"
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "run from the t0 git work tree" >&2
  exit 1
fi

git config --local user.name "$GIT_AUTHOR_NAME"
git config --local user.email "$GIT_AUTHOR_EMAIL"

if ! git remote get-url github >/dev/null 2>&1; then
  git remote add github "$GITHUB_URL"
fi
if ! git remote get-url cursor >/dev/null 2>&1; then
  git remote add cursor "$ORIGIN_URL"
fi

now() {
  "$PYTHON_BIN" -c 'import time; print(f"{time.time():.6f}")'
}

elapsed() {
  "$PYTHON_BIN" -c "print(f'{float('$2')-float('$1'):.6f}')"
}

# time a command; print seconds to stdout, keep command stdout/stderr on fd 3 if provided
run_timed() {
  local start end
  start="$(now)"
  "$@"
  end="$(now)"
  elapsed "$start" "$end"
}

json_escape() {
  "$PYTHON_BIN" -c 'import json,sys; print(json.dumps(sys.stdin.read()[:-1] if False else sys.argv[1]))' "$1"
}

ensure_fixture() {
  if [[ -f "$FIXTURE/.ready" ]]; then
    return
  fi
  echo "generating fixture under bench/fixture/"
  "$PYTHON_BIN" - <<'PY'
import os, hashlib
root = os.path.join("bench", "fixture")
os.makedirs(os.path.join(root, "rand"), exist_ok=True)
os.path.join(root, "text")
os.makedirs(os.path.join(root, "text"), exist_ok=True)
# 64 x 64KiB incompressible blobs ~ 4.0 MiB
for i in range(64):
    payload = hashlib.sha256(f"t0-rand-{i}".encode()).digest() * (65536 // 32)
    with open(os.path.join(root, "rand", f"{i:03d}.bin"), "wb") as f:
        f.write(payload)
# 200 small text files
for i in range(200):
    body = "\n".join(f"t0 text line {i} {j}" for j in range(20)) + "\n"
    with open(os.path.join(root, "text", f"{i:03d}.txt"), "w") as f:
        f.write(body)
open(os.path.join(root, ".ready"), "w").write("ok\n")
PY
}

seed_repo() {
  ensure_fixture
  if git rev-parse --verify HEAD >/dev/null 2>&1; then
    return
  fi
  git add README.md .gitignore bench
  git commit -m "seed: harness, fixture, and public methodology"
}

push_timed() {
  local remote="$1"
  local refspec="${2:-HEAD:refs/heads/main}"
  run_timed "$GIT_BIN" push --quiet "$remote" "$refspec"
}

clone_timed() {
  local url="$1"
  local dest="$2"
  rm -rf "$dest"
  run_timed "$GIT_BIN" clone --quiet "$url" "$dest"
}

fetch_timed() {
  local dir="$1"
  local remote="$2"
  (
    cd "$dir"
    run_timed "$GIT_BIN" fetch --quiet "$remote"
  )
}

object_stats() {
  git count-objects -v
  echo "---"
  git rev-list --all --count
  echo "commits_above"
  git log -1 --format='%H %s'
}

seed_repo

echo "== object stats before first push =="
object_stats || true

JSON="$RES_DIR/${STAMP}.json"
MD="$RES_DIR/${STAMP}.md"
: > "$WORK/raw.txt"

record() {
  local name="$1"
  local seconds="$2"
  echo "$name $seconds" | tee -a "$WORK/raw.txt"
}

echo "== first push (same local objects) =="
# Push GitHub first, then Origin. Pack is generated locally; we time the push RPC.
GH_FIRST="$(push_timed github HEAD:refs/heads/main)"
record first_push_github "$GH_FIRST"
CU_FIRST="$(push_timed cursor HEAD:refs/heads/main)"
record first_push_cursor "$CU_FIRST"

echo "== cold clone x3 =="
for i in 1 2 3; do
  t="$(clone_timed "$GITHUB_URL" "$WORK/clone-github-$i")"
  record "cold_clone_github_$i" "$t"
  t="$(clone_timed "$ORIGIN_URL" "$WORK/clone-cursor-$i")"
  record "cold_clone_cursor_$i" "$t"
done

echo "== incremental commit + fetch =="
echo "incremental $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> bench/INCREMENTAL.txt
git add bench/INCREMENTAL.txt
git commit -m "bench: incremental fetch marker ${STAMP}"
INC_GH="$(push_timed github HEAD:refs/heads/main)"
record incremental_push_github "$INC_GH"
INC_CU="$(push_timed cursor HEAD:refs/heads/main)"
record incremental_push_cursor "$INC_CU"

# fetch into the last cold clones
t="$(fetch_timed "$WORK/clone-github-3" origin)"
record incremental_fetch_github "$t"
# Origin clone remote is named origin by git clone
t="$(fetch_timed "$WORK/clone-cursor-3" origin)"
record incremental_fetch_cursor "$t"

echo "== serial small pushes x20 (interleaved) =="
mkdir -p bench/writes
git checkout -B bench/writes
for i in $(seq -w 1 20); do
  echo "write $i $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "bench/writes/w-$i.txt"
  git add "bench/writes/w-$i.txt"
  git commit -m "bench write $i"
  t="$(push_timed github HEAD:refs/heads/bench/writes)"
  record "serial_push_github_$i" "$t"
  t="$(push_timed cursor HEAD:refs/heads/bench/writes)"
  record "serial_push_cursor_$i" "$t"
done
git checkout main

echo "== concurrent cold clones x4 =="
conc_clone() {
  local url="$1"
  local prefix="$2"
  local pids=()
  local start end
  start="$(now)"
  for i in 1 2 3 4; do
    "$GIT_BIN" clone --quiet "$url" "$WORK/${prefix}-$i" &
    pids+=("$!")
  done
  local fail=0
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      fail=1
    fi
  done
  end="$(now)"
  if [[ "$fail" -ne 0 ]]; then
    echo "concurrent clone failed for $prefix" >&2
    return 1
  fi
  elapsed "$start" "$end"
}
t="$(conc_clone "$GITHUB_URL" "par-github")"
record concurrent_4_clone_github "$t"
t="$(conc_clone "$ORIGIN_URL" "par-cursor")"
record concurrent_4_clone_cursor "$t"

GIT_VER="$("$GIT_BIN" --version)"
UNAME="$(uname -s)"
ARCH="$(uname -m)"

"$PYTHON_BIN" - "$JSON" "$MD" "$STAMP" "$GIT_VER" "$UNAME" "$ARCH" "$GITHUB_URL" "$ORIGIN_URL" "$WORK/raw.txt" <<'PY'
import json, statistics, sys, collections, datetime
json_path, md_path, stamp, git_ver, uname, arch, gh_url, cu_url, raw_path = sys.argv[1:]
rows = []
with open(raw_path) as f:
    for line in f:
        name, sec = line.strip().split()
        rows.append((name, float(sec)))

def group(prefix):
    return [s for n, s in rows if n.startswith(prefix)]

def stats(vals):
    if not vals:
        return None
    return {
        "n": len(vals),
        "min": min(vals),
        "max": max(vals),
        "mean": statistics.fmean(vals),
        "median": statistics.median(vals),
        "values": vals,
    }

payload = {
    "id": stamp,
    "measured_at_utc": datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc).isoformat(),
    "client": {
        "os": uname,
        "arch": arch,
        "git": git_ver,
        "transport": "https",
        "network_notes": "Single client. HTTPS via a local forward proxy (address omitted). Geography omitted.",
    },
    "remotes": {
        "github": gh_url,
        "cursor_origin": cu_url,
    },
    "caveats": [
        "Single laptop, not a multi-replica CI farm.",
        "Both remotes received the same objects from this client.",
        "Origin repo is Origin-hosted, not a GitHub mirror passthrough.",
        "First-push pack generation happens locally; times are git push RPC.",
        "Proxy and underlay RTT dominate small requests; do not read this as Continuity disk/S3 throughput.",
    ],
    "samples": {n: s for n, s in rows},
    "summary": {
        "first_push_github_s": dict(rows).get("first_push_github"),
        "first_push_cursor_s": dict(rows).get("first_push_cursor"),
        "cold_clone_github": stats(group("cold_clone_github_")),
        "cold_clone_cursor": stats(group("cold_clone_cursor_")),
        "incremental_fetch_github_s": dict(rows).get("incremental_fetch_github"),
        "incremental_fetch_cursor_s": dict(rows).get("incremental_fetch_cursor"),
        "serial_push_github": stats(group("serial_push_github_")),
        "serial_push_cursor": stats(group("serial_push_cursor_")),
        "concurrent_4_clone_github_s": dict(rows).get("concurrent_4_clone_github"),
        "concurrent_4_clone_cursor_s": dict(rows).get("concurrent_4_clone_cursor"),
    },
}

def fmt_stats(s):
    if not s:
        return "n/a"
    return f"n={s['n']}  min={s['min']:.3f}s  median={s['median']:.3f}s  mean={s['mean']:.3f}s  max={s['max']:.3f}s"

sm = payload["summary"]
md = f"""# t0 bench {stamp}

Single-client HTTPS comparison. Network path details omitted on purpose.

| Item | GitHub | Cursor Origin |
| --- | ---: | ---: |
| First push (seed) | {sm['first_push_github_s']:.3f}s | {sm['first_push_cursor_s']:.3f}s |
| Cold clone | {fmt_stats(sm['cold_clone_github'])} | {fmt_stats(sm['cold_clone_cursor'])} |
| Incremental fetch | {sm['incremental_fetch_github_s']:.3f}s | {sm['incremental_fetch_cursor_s']:.3f}s |
| Serial push x20 | {fmt_stats(sm['serial_push_github'])} | {fmt_stats(sm['serial_push_cursor'])} |
| 4 concurrent clones (wall) | {sm['concurrent_4_clone_github_s']:.3f}s | {sm['concurrent_4_clone_cursor_s']:.3f}s |

## Environment (non-sensitive)

- OS: {uname} / {arch}
- {git_ver}
- Transport: HTTPS
- GitHub: `{gh_url}`
- Origin: `{cu_url}`
- Client network: local HTTPS forward proxy; ISP/IP/proxy address omitted

## Caveats

{chr(10).join('- ' + c for c in payload['caveats'])}

## Raw samples

```
{open(raw_path).read().rstrip()}
```
"""
with open(json_path, "w") as f:
    json.dump(payload, f, indent=2)
    f.write("\n")
with open(md_path, "w") as f:
    f.write(md)
print(f"wrote {json_path}")
print(f"wrote {md_path}")
PY

echo "done $STAMP"
