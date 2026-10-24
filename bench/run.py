#!/usr/bin/env python3
"""Run every benchmark in every language, check outputs match, record best time and peak memory."""
import json, os, resource, subprocess, sys, time

os.chdir(os.path.dirname(os.path.abspath(__file__)))
JIT = ["-d", "opcache.enable_cli=1", "-d", "opcache.jit=tracing", "-d", "opcache.jit_buffer_size=128M"]
LANGS = {
    "new":      lambda b: [f"./{b}"],
    "rust":     lambda b: [f"./{b}.rsbin"],
    "go":       lambda b: [f"./{b}.gobin"],
    "php-jit":  lambda b: ["php", *JIT, "-d", "memory_limit=-1", f"{b}.php"],
    "php":      lambda b: ["php", "-d", "opcache.enable_cli=0", "-d", "memory_limit=-1", f"{b}.php"],
    "python":   lambda b: ["python3", f"{b}.py"],
}
BENCHES = sys.argv[1:] or ["fib", "sieve", "collatz", "pi", "listsum"]


def run_once(cmd):
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t = time.perf_counter()
    p = subprocess.run(cmd, capture_output=True, text=True)
    dt = time.perf_counter() - t
    if p.returncode:
        raise RuntimeError(f"{cmd} failed: {p.stderr}")
    return dt, p.stdout.strip()


def peak_mem_mb(cmd):
    # memrun is a tiny C parent (fork + wait4) so the measurement excludes Python's own memory
    err = subprocess.run(["./memrun", *cmd], capture_output=True, text=True).stderr
    return int(err.strip().splitlines()[-1]) / 1024


if __name__ == "__main__":
    results = {}
    for b in BENCHES:
        results[b] = {}
        outputs = {}
        for lang, mk in LANGS.items():
            cmd = mk(b)
            first, out = run_once(cmd)
            times = [first]
            reps = 3 if first < 5 else 1
            for _ in range(reps - 1):
                times.append(run_once(cmd)[0])
            outputs[lang] = out
            results[b][lang] = {"time": min(times), "mem_mb": peak_mem_mb(cmd), "out": out}
            print(f"{b:8} {lang:8} {min(times):8.3f}s  {results[b][lang]['mem_mb']:7.1f} MB  -> {out}", flush=True)
        if len(set(outputs.values())) != 1:
            print(f"!! OUTPUT MISMATCH in {b}: {outputs}", flush=True)
        results[b]["_match"] = len(set(outputs.values())) == 1

    with open("results.json", "w") as f:
        json.dump(results, f, indent=1)
