#!/usr/bin/env python3
"""Load-test the four API servers (all pinned to CPU 0) with wrk pinned to CPU 1.

Usage: loadtest.py <path-to-wrk>   -> prints a table and writes loadtest.json

macOS has no taskset, so there wrk runs unpinned and results are less controlled.
"""
import json, os, platform, re, subprocess, sys, time

LINUX = platform.system() == "Linux"

WRK = sys.argv[1]
DURATION = os.environ.get("DURATION", "10s")
SERVERS = {                      # name -> (port, process names whose memory we add up)
    "new":            (8081, ["server"]),
    "go":             (8082, ["server-go"]),
    "php-frankenphp": (8083, ["frankenphp"]),
    "php-classic":    (8084, ["php"]),
}
ENDPOINTS = ["/hello", "/user/42", "/primes/20000"]


def rss_mb(names):
    total = 0
    for n in names:
        pids = subprocess.run(["pgrep", "-x", n], capture_output=True, text=True).stdout.split()
        for pid in pids:
            if not LINUX:                                   # ps reports RSS in KB
                rss = subprocess.run(["ps", "-o", "rss=", "-p", pid], capture_output=True, text=True).stdout
                total += int(rss.strip() or 0)
                continue
            try:
                for line in open(f"/proc/{pid}/status"):
                    if line.startswith("VmRSS:"):
                        total += int(line.split()[1])
            except FileNotFoundError:
                pass
    return total / 1024


def to_ms(s):
    v, unit = re.match(r"([\d.]+)(\w+)", s).groups()
    return float(v) * {"us": 0.001, "ms": 1, "s": 1000}[unit]


def wrk(url, duration):
    pin = ["taskset", "-c", "1"] if LINUX else []
    out = subprocess.run([*pin, WRK, "-t1", "-c64", f"-d{duration}", "--latency", url],
                         capture_output=True, text=True).stdout
    rps = float(re.search(r"Requests/sec:\s+([\d.]+)", out).group(1))
    p50 = to_ms(re.search(r"50%\s+(\S+)", out).group(1))
    p99 = to_ms(re.search(r"99%\s+(\S+)", out).group(1))
    errors = re.search(r"Non-2xx or 3xx responses: (\d+)|Socket errors: (.*)", out)
    return rps, p50, p99, (errors.group(0) if errors else "")


results = {}
for name, (port, procs) in SERVERS.items():
    results[name] = {}
    for ep in ENDPOINTS:
        url = f"http://127.0.0.1:{port}{ep}"
        wrk(url, "2s")                                  # warm-up (lets the JITs compile)
        rps, p50, p99, err = wrk(url, DURATION)
        results[name][ep] = {"rps": rps, "p50_ms": p50, "p99_ms": p99, "rss_mb": rss_mb(procs), "errors": err}
        print(f"{name:15} {ep:15} {rps:10.0f} req/s  p50 {p50:7.2f} ms  p99 {p99:7.2f} ms  "
              f"RSS {results[name][ep]['rss_mb']:6.1f} MB {err}", flush=True)

json.dump(results, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "loadtest.json"), "w"), indent=1)
