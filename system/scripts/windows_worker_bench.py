#!/usr/bin/env python3
"""Time background workers on this machine at 1, 2 and 3 at once.

    python3 system/scripts/windows_worker_bench.py [--counts 1,2,3] [--model sonnet] [--out <dir>]

For each count it starts that many headless `claude -p` workers together. Each
one writes a small single-file HTML page (a table of 5 mock books) and replies
"done". It records per worker the time to first output (first stream event on
stdout), the time to finish, and whether the file exists, plus process-spawn
latency and windows_memory readings before and after each round. Results go to
<out>/bench.json and a Markdown table on stdout.

Run it on a fresh boot or right after `windows_memory.py --check` says healthy;
on a machine at its commit limit it measures the leak, not the workers
(near the limit even a bare `cmd /c exit` takes most of a second).
CLAUDE_CODE_DISABLE_CLAUDE_MDS=1 keeps the large PM OS instructions out,
as a private session launcher does. Costs a few cents of API use per worker.
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
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import windows_memory  # noqa: E402

PROMPT = ("Use the Write tool to create the file {path} containing one self-contained HTML page: a heading "
          "'Reading list' and a table of 5 mock books (title, author, shelf, pages). No external assets. "
          "Then reply with the single word done.")


def spawn_latency_ms(n: int = 30) -> float:
    shell = ["cmd.exe", "/c", "exit"] if os.name == "nt" else ["true"]
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        subprocess.run(shell, capture_output=True)
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times)


def one_worker(claude: str, model: str, path: Path, result: dict, t_start: float) -> None:
    env = {**os.environ, "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}
    # The bench needs only Write: no permission bypass, Bash and web tools off.
    cmd = [claude, "-p", PROMPT.format(path=path.as_posix()), "--model", model, "--output-format", "stream-json",
           "--verbose", "--permission-mode", "acceptEdits", "--allowedTools", "Write",
           "--disallowedTools", "Bash", "WebFetch", "WebSearch"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            cwd=str(path.parent), env=env)
    first = None
    for line in iter(proc.stdout.readline, b""):
        if first is None and line.strip():
            first = time.perf_counter() - t_start
    proc.wait()
    result.update(first_output_s=round(first, 1) if first is not None else None,
                  done_s=round(time.perf_counter() - t_start, 1), exit=proc.returncode,
                  file_ok=path.exists() and path.stat().st_size > 200)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--counts", default="1,2,3")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    claude = shutil.which("claude")
    if not claude:
        print("claude CLI not found on PATH")
        return 2
    out = Path(args.out or tempfile.mkdtemp(prefix="worker-bench-"))
    out.mkdir(parents=True, exist_ok=True)
    report = {"before": windows_memory.snapshot(), "spawn_ms": round(spawn_latency_ms(), 1), "rounds": []}
    for n in [int(x) for x in args.counts.split(",") if x.strip()]:
        before = windows_memory.snapshot()
        results = [{} for _ in range(n)]
        t0 = time.perf_counter()
        threads = [threading.Thread(target=one_worker, args=(claude, args.model, out / f"n{n}-w{i + 1}.html", results[i], t0))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        after = windows_memory.snapshot()
        report["rounds"].append({"workers": n, "results": results, "spawn_ms_after": round(spawn_latency_ms(10), 1),
                                 "commit_pct": [before.get("commit_pct"), after.get("commit_pct")],
                                 "zombies": [before.get("zombie_processes"), after.get("zombie_processes")]})
    (out / "bench.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"| Workers | First output (s) | Done (s) | Files ok | Spawn after (ms) |\n|---|---|---|---|---|")
    for r in report["rounds"]:
        fo = [x.get("first_output_s") for x in r["results"]]
        dn = [x.get("done_s") for x in r["results"]]
        ok = sum(1 for x in r["results"] if x.get("file_ok"))
        print(f"| {r['workers']} | {', '.join(map(str, fo))} | {', '.join(map(str, dn))} | {ok} of {r['workers']} | {r['spawn_ms_after']} |")
    print(f"\nspawn latency before: {report['spawn_ms']} ms; {windows_memory.summary(windows_memory.snapshot())}")
    print(f"saved {out / 'bench.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
