#!/usr/bin/env python3
"""One command to check the whole prototype:  python3 tests/run_tests.py

  tests/*.pn            compile + run, stdout must equal tests/expected/<name>.out
  tests/panics/*.pn     compile + run, must exit 1 and stderr must contain tests/panics/<name>.expected
  tests/errors/*.pn     must FAIL to compile; stderr must contain tests/errors/<name>.expected
  examples/*.pn         compile; the ones without serve() must run and exit 0
  tests/bench/*.pn      the benchmark programs: compile + run, output must match the known answer
  tests/server/api.pn   start the HTTP server, check 5 endpoints (status + body)
  tests/server/panics.pn  handler panics must give 500, free the request's memory, and keep serving

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

def server_log(binary):
    """A server's stderr goes to a file: a pipe nobody reads fills up after ~64 KB of panic messages and then
    blocks the server's writes, which looks like a hang."""
    return open(binary + ".log", "w")


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

print("pin --json (machine-readable errors for tools and AI agents)")
import json  # noqa: E402
PIN = [sys.executable, os.path.join(ROOT, "pin")]
r = subprocess.run(PIN + ["check", "--json", os.path.join(ROOT, "tests", "errors", "use_after_move.pn")],
                   capture_output=True, text=True)
try:
    d = json.loads(r.stdout.strip().splitlines()[-1])
    good = r.returncode == 1 and d["line"] == 3 and d["severity"] == "error" and "was moved" in d["message"]
except (ValueError, IndexError, KeyError):
    good = False
(ok if good else lambda n: fail(n, f"got exit {r.returncode}: {r.stdout!r} {r.stderr!r}"))("check --json on an error")
r = subprocess.run(PIN + ["check", "--json", os.path.join(ROOT, "examples", "hello.pn")], capture_output=True, text=True)
(ok if r.returncode == 0 and json.loads(r.stdout)["ok"] else
 lambda n: fail(n, r.stdout + r.stderr))("check --json on a good file")

print("built-in reference (spec/pin_builtins.py: matches the compiler, every example prints its recorded output)")
r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "gen_docs.py")], capture_output=True, text=True)
(ok if r.returncode == 0 else lambda n: fail(n, r.stdout + r.stderr))("tools/gen_docs.py --check")
try:
    gram = json.load(open(os.path.join(ROOT, "editors", "vscode", "syntaxes", "pin.tmLanguage.json")))
    for f in ("package.json", "language-configuration.json"):
        json.load(open(os.path.join(ROOT, "editors", "vscode", f)))
    sys.path.insert(0, ROOT)
    import pinac  # noqa: E402
    listed = gram["repository"]["builtin-calls"]["match"]
    missing = [b for b in sorted(pinac.BUILTINS) if b not in listed]
    (ok if not missing else lambda n: fail(n, f"builtins missing from the grammar (run tools/gen_docs.py --docs ...): {missing}"))("vscode grammar")
except (OSError, ValueError, KeyError) as e:
    fail("vscode grammar", repr(e))

print("linking (only programs that call db_* need SQLite)")
sys.path.insert(0, ROOT)
import pinac  # noqa: E402
hello_ir = pinac.compile_file(os.path.join(ROOT, "examples", "hello.pn"))
db_ir = pinac.compile_file(os.path.join(ROOT, "tests", "db_basics.pn"))
(ok if not pinac.needs_sqlite(hello_ir) and pinac.needs_sqlite(db_ir) else
 lambda n: fail(n, "needs_sqlite() is wrong"))("hello.pn does not link SQLite; db_basics.pn does")

print("modules (tests/modules: app/ runs; err_*/ must fail with the message in expected.txt, file name included)")
app = os.path.join(ROOT, "tests", "modules", "app")
r = subprocess.run(PIN + ["main.pn"], cwd=app, capture_output=True, text=True)
want = open(os.path.join(app, "expected.out"), encoding="utf-8").read()
(ok if r.returncode == 0 and r.stdout == want else lambda n: fail(n, f"got {r.stdout!r} {r.stderr!r}"))("tests/modules/app")
for d in sorted(glob.glob(os.path.join(ROOT, "tests", "modules", "err_*"))):
    want = open(os.path.join(d, "expected.txt"), encoding="utf-8").read().strip()
    r = subprocess.run(PIN + ["check", "main.pn"], cwd=d, capture_output=True, text=True)
    (ok if r.returncode == 1 and want in r.stderr else
     lambda n: fail(n, f"want {want!r}\n        got {r.stderr.strip()!r}"))(rel(d))
r = subprocess.run(PIN + ["check", "--json", "main.pn"], cwd=os.path.join(ROOT, "tests", "modules", "err_in_module"),
                   capture_output=True, text=True)
(ok if '"file": "broken.pn", "line": 4' in r.stdout else lambda n: fail(n, r.stdout))("--json names the imported file")

print("packages (pin get + pin.lock with a local git repository standing in for github.com)")
import shutil  # noqa: E402
pk = os.path.join(BUILD, "pkgtest")
shutil.rmtree(pk, ignore_errors=True)
repo = os.path.join(pk, "repos", "example.com", "acme", "strutil")
proj = os.path.join(pk, "proj")
os.makedirs(repo)
os.makedirs(proj)
genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t",
            PIN_GIT_BASE="file://" + os.path.join(pk, "repos"), PIN_CACHE=os.path.join(pk, "cache"))
def git(*a):
    subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True, env=genv)
git("init", "-q")
open(os.path.join(repo, "words.pn"), "w").write(
    'import inner/helper;\nexport function greet(string $n): string { return helper::wrap("hi " . $n); }\n')
os.makedirs(os.path.join(repo, "inner"))
open(os.path.join(repo, "inner", "helper.pn"), "w").write('export function wrap(string $s): string { return "[" . $s . "]"; }\n')
git("add", "-A"); git("commit", "-q", "-m", "v1"); git("tag", "v1.0.0")
open(os.path.join(repo, "inner", "helper.pn"), "w").write('export function wrap(string $s): string { return "<" . $s . ">"; }\n')
git("add", "-A"); git("commit", "-q", "-m", "v2"); git("tag", "v1.1.0")
open(os.path.join(proj, "main.pn"), "w").write('import example.com/acme/strutil/words;\necho words::greet("pin"), "\\n";\n')
def pin(*a):
    return subprocess.run(PIN + list(a), cwd=proj, capture_output=True, text=True, env=genv)
r = pin("main.pn")
(ok if r.returncode == 1 and "not in pin.lock. Run:  pin get example.com/acme/strutil@<version>" in r.stderr
 else lambda n: fail(n, r.stderr))("without pin.lock: tells you to run pin get")
r = pin("get", "example.com/acme/strutil@v1.0.0")
r2 = pin("main.pn")
(ok if r.returncode == 0 and r2.stdout == "[hi pin]\n" else lambda n: fail(n, r.stderr + r2.stdout + r2.stderr))("pin get v1.0.0, build, run")
cached = glob.glob(os.path.join(pk, "cache", "example.com", "acme", "strutil@*", "inner", "helper.pn"))[0]
open(cached, "a").write("// tampered\n")
r = pin("main.pn")
(ok if r.returncode == 1 and "does not match pin.lock" in r.stderr else lambda n: fail(n, r.stdout + r.stderr))("a changed package does not build")
shutil.rmtree(os.path.join(pk, "cache"))
r = pin("main.pn")
(ok if r.stdout == "[hi pin]\n" else lambda n: fail(n, r.stdout + r.stderr))("empty cache: downloads the locked commit again")
pin("get", "example.com/acme/strutil@v1.1.0")
r = pin("main.pn")
(ok if r.stdout == "<hi pin>\n" else lambda n: fail(n, r.stdout + r.stderr))("pin get v1.1.0 moves the lock")

print("examples (examples/*.pn: must compile; programs without serve() must run and exit 0)")
for src in sorted(glob.glob(os.path.join(ROOT, "examples", "*.pn"))):
    name = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(BUILD, "example_" + name)
    b = build(src, out)
    if b.returncode:
        fail(rel(src), "compile failed: " + b.stderr)
        continue
    if "serve(" in open(src, encoding="utf-8").read():
        ok(rel(src) + " (compiles)")
        continue
    r = run(out)
    if r.returncode:
        fail(rel(src), f"exit {r.returncode}: {r.stderr}")
    else:
        ok(rel(src))

print("benchmark programs (tests/bench/*.pn; the comparisons live in pin-language/benchmarks)")
for name, answer in BENCH_ANSWERS.items():
    src = os.path.join(ROOT, "tests", "bench", name + ".pn")
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
    print("http server (tests/server/api.pn on :8081)")
    out = os.path.join(BUILD, "api_server")
    b = build(os.path.join(ROOT, "tests", "server", "api.pn"), out)
    if b.returncode:
        fail("tests/server/api.pn", "build failed: " + b.stderr[-400:])
    else:
        env = dict(os.environ, PN_THREADS="2")
        proc = subprocess.Popen([out], env=env, stdout=subprocess.DEVNULL, stderr=server_log(out))
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


def http_get(port, path):
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return None, repr(e)


def rss_mb(pid):
    """Memory the process really holds. macOS compresses idle pages, so RSS there hides leaks;
    footprint's phys_footprint counts compressed memory too."""
    if platform.system() == "Linux":
        for line in open(f"/proc/{pid}/status"):
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    try:
        out = subprocess.run(["footprint", "-p", str(pid)], capture_output=True, text=True).stdout
    except FileNotFoundError:
        out = ""
    for line in out.splitlines():
        parts = line.split()
        if parts[:1] == ["phys_footprint:"]:
            return float(parts[1]) * {"B": 1 / 1048576, "KB": 1 / 1024, "MB": 1, "GB": 1024}[parts[2]]
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True).stdout
    return int(out.strip() or 0) / 1024                 # fallback: RSS in KB


if "--no-server" not in sys.argv:
    print("handler panics (tests/server/panics.pn on :8085)")
    out = os.path.join(BUILD, "server_panics")
    b = build(os.path.join(ROOT, "tests", "server", "panics.pn"), out)
    if b.returncode:
        fail("tests/server/panics.pn", "build failed: " + b.stderr[-400:])
    else:
        proc = subprocess.Popen([out], env=dict(os.environ, PN_THREADS="2"),
                                stdout=subprocess.DEVNULL, stderr=server_log(out))
        err500 = '{"error":"Internal Server Error"}'
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", 8085), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            for path, want in [("/boom", (500, err500)), ("/ok", (200, '{"ok":true}')), ("/oob", (500, err500)), ("/objects", (500, err500)),
                               ("/notfound", (404, '{"error":"Not Found"}')), ("/odd", (500, err500)),
                               ("/caught", (200, '{"code":404,"message":"no such thing: /notfound"}')),
                               ("/overflow", (500, err500)), ("/ok", (200, '{"ok":true}'))]:
                got = http_get(8085, path)
                if got == want:
                    ok("GET " + path)
                else:
                    fail("GET " + path, f"want {want}\n        got  {got}")
            # each /boom holds 8 MB when it panics: if the cleanup leaked, 500 more would add ~4 GB
            for _ in range(50):                   # warm up malloc's free lists first
                http_get(8085, "/boom")
            before = rss_mb(proc.pid)
            bad = [r for r in (http_get(8085, "/boom") for _ in range(500)) if r != (500, err500)]
            grew = rss_mb(proc.pid) - before
            if bad or grew > 150 or http_get(8085, "/ok")[0] != 200:
                fail("500 panicking requests", f"bad answers {bad[:2]}, RSS grew {grew:.0f} MB")
            else:
                ok(f"500 panicking requests (RSS grew {grew:.0f} MB, server still answers)")
            # errors leaving the handler free what the handler held
            before = rss_mb(proc.pid)
            bad = [r for r in (http_get(8085, "/notfound") for _ in range(300)) if r[0] != 404]
            grew = rss_mb(proc.pid) - before
            if bad or grew > 50:
                fail("300 errors leaving the handler", f"bad answers {bad[:2]}, RSS grew {grew:.0f} MB")
            else:
                ok(f"300 errors leaving the handler answer 404 (RSS grew {grew:.0f} MB)")
            log = open(out + ".log").read()
            if "error 404: no such thing: /notfound" in log and "no such thing" not in http_get(8085, "/notfound")[1]:
                ok("the error message goes to the log, not to the client")
            else:
                fail("error message only in the log", log[-300:])
            # each /objects builds an object holding 1000 lists and 1000 strings before it panics
            before = rss_mb(proc.pid)
            bad = [r for r in (http_get(8085, "/objects") for _ in range(300)) if r != (500, err500)]
            grew = rss_mb(proc.pid) - before
            if bad or grew > 100:
                fail("300 panics holding objects", f"bad answers {bad[:2]}, RSS grew {grew:.0f} MB")
            else:
                ok(f"300 panics holding objects and nested lists (RSS grew {grew:.0f} MB)")
        finally:
            proc.terminate()
            proc.wait(timeout=5)


def open_fds(pid):
    if platform.system() == "Linux":
        return len(os.listdir(f"/proc/{pid}/fd"))
    out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True).stdout
    return len(out.splitlines())


def http(port, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body.encode() if body else None,
                                 method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:  # noqa: BLE001
        return None, repr(e)


if "--no-server" not in sys.argv:
    print("database server (tests/server/db.pn on :8086, SQLite)")
    out = os.path.join(BUILD, "server_db")
    b = build(os.path.join(ROOT, "tests", "server", "db.pn"), out)
    if b.returncode:
        fail("tests/server/db.pn", "build failed: " + b.stderr[-400:])
    else:
        for f in glob.glob(os.path.join(BUILD, "server_db_test.db*")):
            os.remove(f)
        proc = subprocess.Popen([out], cwd=BUILD, env=dict(os.environ, PN_THREADS="4"),
                                stdout=subprocess.DEVNULL, stderr=server_log(out))
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", 8086), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            for (m, path, body), want in [
                    (("POST", "/items", '{"name":"tea","qty":2}'), (201, '{"id":1}')),
                    (("POST", "/items", '{"name":"tea","qty":5}'), (409, '{"error":"UNIQUE constraint failed: items.name"}')),
                    (("GET", "/items", None), (200, '[{"id":1,"name":"tea","qty":2}]')),
                    (("GET", "/boom", None), (500, '{"error":"Internal Server Error"}')),
                    (("GET", "/total", None), (200, '{"total":2}'))]:
                got = http(8086, m, path, body)
                (ok if got == want else lambda n: fail(n, f"want {want}\n        got  {got}"))(f"{m} {path}")
            for _ in range(20):
                http(8086, "GET", "/boom")
            before = open_fds(proc.pid)
            bad = [r for r in (http(8086, "GET", "/boom") for _ in range(300)) if r[0] != 500]
            grew = open_fds(proc.pid) - before
            if bad or grew > 5:
                fail("300 panics with an open connection and statement", f"bad {bad[:2]}, open files grew by {grew}")
            else:
                ok(f"300 panics with an open connection and statement (open files grew by {grew})")
            import threading  # noqa: E402
            results = []
            def insert(i):
                results.append(http(8086, "POST", "/items", '{"name":"item%d","qty":1}' % i)[0])
            ts = [threading.Thread(target=insert, args=(i,)) for i in range(200)]
            for th in ts:
                th.start()
            for th in ts:
                th.join()
            total = http(8086, "GET", "/total")
            if results.count(201) == 200 and total == (200, '{"total":202}'):
                ok("200 parallel inserts on 4 threads, all stored")
            else:
                fail("200 parallel inserts", f"statuses {sorted(set(results))}, total {total}")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

print(f"\n{passed} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
