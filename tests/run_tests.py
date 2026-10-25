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
        --sanitize   build the HTTP servers with ASAN + UBSAN and fail on any report in their logs
                     (tools/sanitize.sh only runs programs that exit; servers never do)
Exit code 0 = all passed.
"""
import glob, os, platform, socket, subprocess, sys, time, urllib.request, urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PINAC = [sys.executable, os.path.join(ROOT, "pinac.py")]
UPDATE = "--update" in sys.argv
SANITIZE = "--sanitize" in sys.argv
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
    san = SANITIZE and os.sep + os.path.join("tests", "server") + os.sep in src
    env = dict(os.environ, PIN_SANITIZE="1") if san else None
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


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
    missing = [b for b in sorted(pinac.BUILTINS) if b not in listed and not b.startswith("__")]
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

print("standard input (read_line)")
_src = os.path.join(BUILD, "read_lines.pn")
open(_src, "w").write('$n = 0;\nwhile (true) {\n    $l = try read_line() catch ($e) { break; };\n'
                      '    $n += 1;\n    echo $n, ": [", $l, "]\\n";\n}\necho "end\\n";\n')
_b = build(_src, os.path.join(BUILD, "read_lines"))
_r = subprocess.run([os.path.join(BUILD, "read_lines")], input="one\r\n\nthree", capture_output=True, text=True,
                    timeout=10) if not _b.returncode else _b
(ok if not _b.returncode and _r.stdout == "1: [one]\n2: []\n3: [three]\nend\n" else
 lambda n: fail(n, (_r.stdout + _r.stderr)[-300:]))("read_line: CRLF, an empty line, a last line without newline, end of input")

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
    if SANITIZE:
        return 0.0                              # ASAN keeps freed memory in quarantine: growth means nothing here
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
                    (("GET", "/tx-left", None), (200, '{"ok":true}')),
                    (("GET", "/tx-boom", None), (500, '{"error":"Internal Server Error"}')),
                    (("GET", "/leftovers", None), (200, '{"n":0}')),
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
            held = []
            ts = [threading.Thread(target=lambda: held.append(http(8086, "GET", "/hold"))) for _ in range(12)]
            for th in ts:
                th.start()
            for th in ts:
                th.join()
            (ok if held.count((200, '{"held":true}')) == 12 else lambda n: fail(n, str(sorted(set(held)))[:300]))(
                "12 requests holding their connection, 1 per thread (db_pool): the others wait, none fail")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

def raw(port, data, wait=2.0):
    """Send bytes exactly as given and return everything the server sends back before closing (or `wait`)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    try:
        if data:
            s.sendall(data)
        chunks = []
        while True:
            try:
                b = s.recv(65536)
            except socket.timeout:
                break
            except ConnectionResetError:
                chunks.append(b"<reset>")
                break
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode("latin-1")
    finally:
        s.close()


if "--no-server" not in sys.argv:
    print("headers and limits (tests/server/limits.pn on :8087)")
    out = os.path.join(BUILD, "server_limits")
    b = build(os.path.join(ROOT, "tests", "server", "limits.pn"), out)
    if b.returncode:
        fail("tests/server/limits.pn", "build failed: " + b.stderr[-400:])
    else:
        env = dict(os.environ, PN_THREADS="1", PN_READ_TIMEOUT_MS="1000", PN_IDLE_TIMEOUT_MS="1500",
                   PN_MAX_CONNS="20")
        proc = subprocess.Popen([out], env=env, stdout=subprocess.DEVNULL, stderr=server_log(out))
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", 8087), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)

            def check(name, cond, got):
                (ok(name) if cond else fail(name, f"got {got[:400]!r}"))

            r = raw(8087, b"GET /echo-header HTTP/1.1\r\nX-Name:   Pin  \r\nConnection: close\r\n\r\n")
            check("request_header: case-insensitive, trimmed, \"\" when absent",
                  r.endswith('{"name":"Pin","has":"yes","missing":""}'), r)
            r = raw(8087, b"GET /redirect HTTP/1.1\r\nConnection: close\r\n\r\n")
            check("response_header: Location and two Set-Cookie",
                  r.startswith("HTTP/1.1 302 Found\r\n") and "\r\nLocation: /target\r\n" in r
                  and r.count("Set-Cookie:") == 2, r)
            r = raw(8087, b"GET /html HTTP/1.0\r\nConnection: keep-alive\r\n\r\n", wait=0.5)
            check("HTTP/1.0 keep-alive: the answer says Connection: keep-alive (B13) and the connection stays",
                  "\r\nConnection: keep-alive\r\n" in r and "<reset>" not in r, r)
            r = raw(8087, b"GET /html HTTP/1.1\r\nConnection: close\r\n\r\n")
            check("response_header: Content-Type replaces application/json",
                  "Content-Type: text/html; charset=utf-8\r\n" in r and "application/json" not in r, r)
            r = raw(8087, b"GET /inject?v=x%0D%0ASet-Cookie:%20evil=1 HTTP/1.1\r\nConnection: close\r\n\r\n")
            check("header injection (CR/LF in a value) panics: 500, nothing injected",
                  r.startswith("HTTP/1.1 500") and "evil" not in r, r)
            r = raw(8087, b"GET /fail HTTP/1.1\r\nConnection: close\r\n\r\n")
            check("an error leaving the handler drops the headers it set", r.startswith("HTTP/1.1 403")
                  and "Set-Cookie" not in r and r.endswith('{"error":"Forbidden"}'), r)
            r = raw(8087, b"GET /own HTTP/1.1\r\nConnection: close\r\n\r\n")
            check("setting Content-Length yourself panics (500)", r.startswith("HTTP/1.1 500"), r)
            r = raw(8087, b"POST /x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n")
            check("Transfer-Encoding is refused (501), never guessed", r.startswith("HTTP/1.1 501"), r)
            r = raw(8087, b"POST /x HTTP/1.1\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello!")
            check("two different Content-Length values: 400", r.startswith("HTTP/1.1 400"), r)
            r = raw(8087, b"POST /x HTTP/1.1\r\nContent-Length: 5x\r\n\r\nhello")
            check("a Content-Length that is not digits: 400", r.startswith("HTTP/1.1 400"), r)
            r = raw(8087, b"GET / HTTP/1.1\r\n" + b"X-Big: " + b"a" * 70000 + b"\r\n\r\n")
            check("more than 64 KB of headers: 431", r.startswith("HTTP/1.1 431"), r)

            t0 = time.time()
            r = raw(8087, b"GET / HTTP/1.1\r\nX-Slow: 1\r\n", wait=5)       # slowloris: never finishes
            dt = time.time() - t0
            check(f"a request that never finishes: 408 and closed ({dt:.1f}s, limit 1s)",
                  r.startswith("HTTP/1.1 408") and dt < 3.5, r)
            t0 = time.time()
            r = raw(8087, b"", wait=5)                                          # connects, sends nothing
            dt = time.time() - t0
            check(f"a connection that sends nothing is closed ({dt:.1f}s)", dt < 3.5, r)
            s2 = socket.create_connection(("127.0.0.1", 8087), timeout=5)
            s2.sendall(b"GET / HTTP/1.1\r\n\r\n")
            first = s2.recv(65536)
            t0 = time.time()
            rest = s2.recv(65536)                                               # idle keep-alive: closed by the server
            dt = time.time() - t0
            s2.close()
            check(f"idle keep-alive connection closed ({dt:.1f}s, limit 1.5s)",
                  first.startswith(b"HTTP/1.1 200") and rest == b"" and 0.8 < dt < 4, (first + rest).decode())
            held = [socket.create_connection(("127.0.0.1", 8087), timeout=5) for _ in range(20)]
            time.sleep(0.2)
            r = raw(8087, b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n")
            for h in held:
                h.close()
            check("connection limit (PN_MAX_CONNS=20): the 21st is refused (503 or reset), not served",
                  r.startswith("HTTP/1.1 503") or r == "<reset>", r)
            time.sleep(0.3)
            r = raw(8087, b"GET / HTTP/1.1\r\nConnection: close\r\n\r\n")
            check("after they close, new connections are served again", r.startswith("HTTP/1.1 200"), r)
        finally:
            proc.terminate()
            proc.wait(timeout=5)

if "--no-server" not in sys.argv:
    print("handlers as tasks (tests/server/tasks.pn on :8088, one thread)")
    out = os.path.join(BUILD, "server_tasks")
    b = build(os.path.join(ROOT, "tests", "server", "tasks.pn"), out)
    if b.returncode:
        fail("tests/server/tasks.pn", "build failed: " + b.stderr[-400:])
    else:
        proc = subprocess.Popen([out], env=dict(os.environ, PN_THREADS="1"), stdout=subprocess.DEVNULL,
                                stderr=server_log(out))
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", 8088), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            import threading  # noqa: E402
            slow = {}
            def get_slow():
                t0 = time.time()
                slow["r"] = http_get(8088, "/slow")
                slow["t"] = time.time() - t0
            th = threading.Thread(target=get_slow)
            th.start()
            time.sleep(0.05)
            t0 = time.time()
            fast = http_get(8088, "/fast")
            dt = time.time() - t0
            th.join()
            if fast == (200, '{"fast":true}') and dt < 0.15 and slow.get("r") == (200, '{"slow":true}'):
                ok(f"a waiting handler does not hold up others on its thread (fast {dt*1000:.0f} ms while slow waits)")
            else:
                fail("waiting handler", f"fast {fast} in {dt:.2f}s, slow {slow}")
            t0 = time.time()
            r = http_get(8088, "/fanout")
            dt = time.time() - t0
            if r == (200, '["user1","user2","user3"]') and dt < 0.25:
                ok(f"three go calls run at once ({dt*1000:.0f} ms for three 100 ms lookups)")
            else:
                fail("go in a handler", f"{r} in {dt:.2f}s")
            err500 = (500, '{"error":"Internal Server Error"}')
            r = http_get(8088, "/childpanic")
            if r == err500 and http_get(8088, "/fast") == (200, '{"fast":true}'):
                ok("a panic in a task started by the handler answers 500; the server goes on")
            else:
                fail("panic in a child task", str(r))
            for _ in range(20):
                http_get(8088, "/childpanic")
            before = rss_mb(proc.pid)
            bad = [x for x in (http_get(8088, "/childpanic") for _ in range(200)) if x != err500]
            grew = rss_mb(proc.pid) - before
            if bad or grew > 60:
                fail("200 child-task panics", f"bad {bad[:2]}, RSS grew {grew:.0f} MB")
            else:
                ok(f"200 child-task panics free their memory and tasks (RSS grew {grew:.0f} MB)")
            for _ in range(30):                    # clients that hang up while their handler waits
                sk = socket.create_connection(("127.0.0.1", 8088), timeout=2)
                sk.sendall(b"GET /slow HTTP/1.1\r\n\r\n")
                sk.close()
            time.sleep(0.5)
            if http_get(8088, "/fast") == (200, '{"fast":true}') and proc.poll() is None:
                ok("clients that disconnect while their handler waits do not break the server")
            else:
                fail("disconnect while waiting", "server not answering")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

if "--no-server" not in sys.argv:
    print("hub: long-poll and SSE between requests (tests/server/hub.pn on :8089, 4 threads)")
    out = os.path.join(BUILD, "server_hub")
    b = build(os.path.join(ROOT, "tests", "server", "hub.pn"), out)
    if b.returncode:
        fail("tests/server/hub.pn", "build failed: " + b.stderr[-400:])
    else:
        proc = subprocess.Popen([out], env=dict(os.environ, PN_THREADS="4"), stdout=subprocess.DEVNULL,
                                stderr=server_log(out))
        try:
            for _ in range(50):
                try:
                    socket.create_connection(("127.0.0.1", 8089), timeout=0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)
            import threading  # noqa: E402
            worst, bad = 0.0, []
            for i in range(20):                    # the waiting and the sending request are often on different threads
                got = {}
                def wait():
                    got["r"] = http(8089, "GET", f"/wait?agent=b{i}")
                th = threading.Thread(target=wait)
                th.start()
                time.sleep(0.1)
                t0 = time.time()
                http(8089, "POST", f"/send?to=b{i}", f"hello {i}")
                th.join()
                worst = max(worst, time.time() - t0)
                if got.get("r") != (200, f"hello {i}"):
                    bad.append(got.get("r"))
            if not bad and worst < 0.5:
                ok(f"long-poll: 20 waiting requests woken by a message from another request (slowest {worst*1000:.0f} ms)")
            else:
                fail("long-poll", f"bad {bad[:3]}, slowest {worst:.2f}s")
            t0 = time.time()
            r = http(8089, "GET", "/wait?agent=nobody")
            dt = time.time() - t0
            (ok if r[0] == 204 and 2.5 < dt < 4 else lambda n: fail(n, f"{r} after {dt:.1f}s"))(
                f"long-poll with no message ends after its timeout with 204 ({dt:.1f}s)")

            sk = socket.create_connection(("127.0.0.1", 8089), timeout=5)
            sk.sendall(b"GET /stream?agent=s1 HTTP/1.1\r\nLast-Event-ID: 0\r\n\r\n")
            time.sleep(0.2)
            for j in range(3):
                http(8089, "POST", "/send?to=s1", f"line {j}\nsecond")
            data, deadline = b"", time.time() + 3
            while data.count(b"\n\n") < 4 and time.time() < deadline:   # headers + 3 events
                data += sk.recv(65536)
            text = data.decode()
            want = "id: 1\ndata: line 0\ndata: second\n\nid: 2\ndata: line 1\ndata: second\n\nid: 3\n"
            (ok if "Content-Type: text/event-stream" in text and want in text else lambda n: fail(n, text[-300:]))(
                "SSE: three messages arrive as events with ids, multi-line text split into data: lines")
            time.sleep(0.5)
            more = sk.recv(65536).decode()
            (ok if ": ping" in more else lambda n: fail(n, repr(more)))("SSE: a ping comment while nothing happens")
            sk.close()
            sk = socket.create_connection(("127.0.0.1", 8089), timeout=5)
            sk.sendall(b"GET /stream?agent=s1 HTTP/1.1\r\nLast-Event-ID: 2\r\n\r\n")
            data, deadline = b"", time.time() + 3
            while b"id: 3" not in data and time.time() < deadline:
                data += sk.recv(65536)
            sk.close()
            (ok if b"id: 3" in data and b"id: 2" not in data else lambda n: fail(n, data[-200:]))(
                "SSE: reconnecting with Last-Event-ID 2 gets only what came after it")
            for _ in range(10):
                c = socket.create_connection(("127.0.0.1", 8089), timeout=5)
                c.sendall(b"GET /stream?agent=gone HTTP/1.1\r\n\r\n")
                c.recv(4096)
                c.close()
            before = rss_mb(proc.pid)
            for _ in range(200):                   # streams whose readers leave: each request must end and free
                c = socket.create_connection(("127.0.0.1", 8089), timeout=5)
                c.sendall(b"GET /stream?agent=gone HTTP/1.1\r\n\r\n")
                c.recv(4096)
                c.close()
            time.sleep(1.0)
            grew = rss_mb(proc.pid) - before
            alive = http(8089, "GET", "/wait?agent=x&nothing") if False else http(8089, "POST", "/send?to=x", "y")
            (ok if grew < 30 and alive == (200, '{"ok":true}') else lambda n: fail(n, f"RSS grew {grew:.0f} MB, {alive}"))(
                f"200 SSE clients that leave: their requests end and free (RSS grew {grew:.0f} MB)")
            cb = build(os.path.join(ROOT, "tests", "server", "client.pn"), os.path.join(BUILD, "client"))
            want = ("{\"ok\":true}\nping from the client\nevent 1 (id 1)\nevent 2 (id 2)\nevent 3 (id 3)\n"
                    "nothing more: 504\nevent 3\nstatus 404\n")
            cr = run(os.path.join(BUILD, "client"), timeout=30) if not cb.returncode else cb
            (ok if not cb.returncode and cr.stdout == want else lambda n: fail(n, (cr.stdout + cr.stderr)[-400:]))(
                "the HTTP client against our own server: long-poll, a stream with ids, resume, 404")
        finally:
            proc.terminate()
            proc.wait(timeout=5)

if SANITIZE:
    print("sanitizer reports in the servers' logs")
    for log in sorted(glob.glob(os.path.join(BUILD, "*.log"))):
        text = open(log, errors="replace").read()
        bad = [l for l in text.splitlines() if "ERROR: AddressSanitizer" in l or "runtime error:" in l]
        name = os.path.basename(log)
        (ok if not bad else lambda n: fail(n, "\n".join(bad[:5])))(f"no ASAN/UBSAN report in {name}")

print(f"\n{passed} passed, {len(failed)} failed")
sys.exit(1 if failed else 0)
