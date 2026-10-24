#!/usr/bin/env python3
"""One command to check the whole prototype:  python3 tests/run_tests.py

  tests/*.pn            compile + run, stdout must equal tests/expected/<name>.out
  tests/panics/*.pn     compile + run, must exit 1 and stderr must contain tests/panics/<name>.expected
  tests/errors/*.pn     must FAIL to compile; stderr must contain tests/errors/<name>.expected
  bench/*.pn            compile + run, output must match the known answer
  api/server.pn         start the HTTP server, check 5 endpoints (status + body)

Flags:  --update   rewrite expected files from current output (review the diff before committing!)
        --no-server  skip the HTTP check
Exit code 0 = all passed.
"""
import glob, os, platform, socket, subprocess, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PINAC = [sys.executable, os.path.join(ROOT, "pinac.py")]
UPDATE = "--update" in sys.argv
BUILD = os.path.join(ROOT, "build")
os.makedirs(BUILD, exist_ok=True)

BENCH_ANSWERS = {
    "fib": "9227465", "sieve": "1270607", "collatz": "837799 524",
    "pi": "3.1415926435893", "listsum": "24975000000",
}
API_CHECKS = [  # path, status, exact body
    ("/hello", 200, '{"message":"hello world"}'),
    ("/user/42", 200, '{"id":42,"name":"user42","email":"user42@pinato.co","orders":[294,336,378,420,462,504,546,588,630,672,714,756,798,840,882,924,966,11,53,95],"total":10869}'),
    ("/primes/20000", 200, '{"n":20000,"count":2262}'),
    ("/primes/1", 400, '{"error":"n must be between 2 and 1000000"}'),
    ("/nope", 404, '{"error":"not found"}'),
]

passed, failed = 0, []


def ok(name):
    global passed
    passed += 1
    print(f"  ok    {name}")


def fail(name, why):
    failed.append(name)
    print(f"  FAIL  {name}\n        {why.strip()[:600]}")


def build(src, out, emit_ll_only=False):
    cmd = PINAC + [src, "-o", out] + (["--emit-ll"] if emit_ll_only else [])
    return subprocess.run(cmd, capture_output=True, text=True)


def run(binary, timeout=60):
    return subprocess.run([binary], capture_output=True, text=True, timeout=timeout)


def rel(p):
    return os.path.relpath(p, ROOT)


print("programs (tests/*.pn)")
for src in sorted(glob.glob(os.path.join(ROOT, "tests", "*.pn"))):
    name = os.path.splitext(os.path.basename(src))[0]
    exp_path = os.path.join(ROOT, "tests", "expected", name + ".out")
    out = os.path.join(BUILD, name)
    b = build(src, out)
    if b.returncode:
        fail(rel(src), "compile failed: " + b.stderr)
        continue
    r = run(out)
    if UPDATE:
        open(exp_path, "w").write(r.stdout)
    expected = open(exp_path).read() if os.path.exists(exp_path) else None
    if r.returncode:
        fail(rel(src), f"exit {r.returncode}: {r.stderr}")
    elif expected is None:
        fail(rel(src), f"no expected output; run with --update to create {rel(exp_path)}")
    elif r.stdout != expected:
        fail(rel(src), "output differs from " + rel(exp_path))
    else:
        ok(rel(src))

print("runtime panics (tests/panics/*.pn)")
for src in sorted(glob.glob(os.path.join(ROOT, "tests", "panics", "*.pn"))):
    name = os.path.splitext(os.path.basename(src))[0]
    exp_path = src[:-3] + ".expected"
    out = os.path.join(BUILD, "panic_" + name)
    b = build(src, out)
    if b.returncode:
        fail(rel(src), "compile failed: " + b.stderr)
        continue
    r = run(out)
    if UPDATE:
        open(exp_path, "w").write(r.stderr.strip() + "\n")
    expected = open(exp_path).read().strip()
    if r.returncode != 1 or expected not in r.stderr:
        fail(rel(src), f"want exit 1 + '{expected}', got exit {r.returncode}: {r.stderr}")
    else:
        ok(rel(src))

print("compile errors (tests/errors/*.pn)")
for src in sorted(glob.glob(os.path.join(ROOT, "tests", "errors", "*.pn"))):
    exp_path = src[:-3] + ".expected"
    b = build(src, os.path.join(BUILD, "err"), emit_ll_only=True)
    msg = b.stderr.strip().split(": error: ", 1)[-1]
    if UPDATE and b.returncode:
        open(exp_path, "w").write(msg + "\n")
    expected = open(exp_path).read().strip() if os.path.exists(exp_path) else None
    if b.returncode == 0:
        fail(rel(src), "compiled, but it must be rejected")
    elif expected is None:
        fail(rel(src), f"no {rel(exp_path)}; run with --update")
    elif expected not in b.stderr:
        fail(rel(src), f"want '{expected}'\n        got  '{b.stderr.strip()}'")
    else:
        ok(rel(src))
for ll in glob.glob(os.path.join(ROOT, "tests", "**", "*.ll"), recursive=True):
    os.remove(ll)

print("benchmarks (bench/*.pn)")
for name, answer in BENCH_ANSWERS.items():
    src = os.path.join(ROOT, "bench", name + ".pn")
    out = os.path.join(BUILD, "bench_" + name)
    b = build(src, out)
    if b.returncode:
        fail(rel(src), "compile failed: " + b.stderr)
        continue
    r = run(out, timeout=120)
    if r.stdout.strip() != answer:
        fail(rel(src), f"want {answer}, got {r.stdout.strip()!r} {r.stderr}")
    else:
        ok(rel(src))

if "--no-server" not in sys.argv:
    print("http server (api/server.pn on :8081)")
    out = os.path.join(BUILD, "api_server")
    b = build(os.path.join(ROOT, "api", "server.pn"), out)
    if b.returncode:
        hint = ""
        if platform.system() != "Linux":
            hint = ("\n        (expected on macOS until runtime.c's server is ported from epoll to kqueue "
                    "— see HANDOFF.md task 1)")
        fail("api/server.pn", "build failed: " + b.stderr[-400:] + hint)
    else:
        env = dict(os.environ, PN_THREADS="2")
        proc = subprocess.Popen([out], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", 8081), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            for path, status, body in API_CHECKS:
                try:
                    resp = urllib.request.urlopen("http://127.0.0.1:8081" + path, timeout=5)
                    got_status, got_body = resp.status, resp.read().decode()
                except urllib.error.HTTPError as e:
                    got_status, got_body = e.code, e.read().decode()
                except Exception as e:  # noqa: BLE001
                    got_status, got_body = None, repr(e)
                if (got_status, got_body) == (status, body):
                    ok("GET " + path)
                else:
                    fail("GET " + path, f"want {status} {body}\n        got  {got_status} {got_body}")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

print(f"\n{passed} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
