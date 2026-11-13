#!/usr/bin/env python3
"""Closure fuzzing (closures.md C4–C7): every kind of captured value (string, list, object, map, ?string, nested
list) × every way of making a closure over it (arrow, use ($v), use (&$v), a closure inside a closure, copy(),
through a function, ?:) × what happens to the value while the closure lives (nothing, changed, replaced, moved
away) × every place a closure can go (called, a variable, lists, maps, properties, returned, a parameter, go,
overwritten, loops, foreach (... as &$f), an error leaving the function), each built with ASAN + UBSAN and run.
A program the compiler refuses is fine, and so is a panic (exit 1 with a message); one that runs and trips the
sanitizers, crashes or hangs is a bug. About 8,000 programs: ~20 minutes on an M2, so not part of run_tests.py.

    python3 tools/fuzz_closures.py [-v]       (macOS or Linux, clang; -v lists the refusal messages)
"""
import itertools, os, subprocess, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
P = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="pin-fuzz-clo-")

PRE = r'''
class Box {
    public list<int> $xs = [];
    public function __construct(int $n) { $this->xs = [$n]; }
}
class Holder {
    public fn(): string $f;
    public function __construct(fn(): string $f) { $this->f = copy($f); }
}
function ps(int $i): ?string { return $i > 0 ? "p" . strval($i) : null; }
function fail(int $i): int throws { if ($i > 0) { throw error(1, "x"); } return $i; }
function call_it(fn(): string $f): string { return $f() . $f(); }
function wrap(fn(): string $f): fn(): string { return copy($f); }
'''
# kind: (setup, read as a string, change, replace, move away)
KINDS = {
    "str": ('$v = "ab" . strval(1);', '$v . "!"', '$v .= "z";', '$v = "new" . strval(2);', '$w = $v; echo $w;'),
    "list": ('$v = [1, 2];', 'json_encode($v)', '$v[] = 9;', '$v = [7];', '$w = $v; echo count($w);'),
    "obj": ('$v = new Box(1);', 'json_encode($v)', '$v->xs[] = 9;', '$v = new Box(2);', '$w = $v; echo count($w->xs);'),
    "map": ('map<string,list<int>> $v = ["k" => [1]];', 'json_encode($v)', '$v["z"] = [2];', '$v = ["n" => [3]];',
            '$w = $v; echo count($w);'),
    "optstr": ('?string $v = ps(1);', '($v ?? "none")', '$v = null;', '$v = ps(2);', '$w = $v; echo $w ?? "n";'),
    "nested": ('list<list<int>> $v = [[1], [2]];', 'json_encode($v)', '$v[0][] = 9;', '$v = [[7]];',
               '$w = $v; echo count($w);'),
}
# maker: (statements before, the closure expression); {r} = the read, {m} = the change
MAKERS = {
    "arrow": ('', 'fn(): string => {r}'),
    "long": ('', 'function () use ($v): string {{ return {r}; }}'),
    "ref": ('', 'function () use (&$v): string {{ {m} return {r}; }}'),
    "nest": ('$inner = fn(): string => {r};', 'fn(): string => $inner() . "+"'),
    "nestref": ('$inner = function () use (&$v): string {{ {m} return {r}; }};', 'function () use ($inner): string {{ return $inner(); }}'),
    "copy": ('$orig = fn(): string => {r};', 'copy($orig)'),
    "wrap": ('', 'wrap(fn(): string => {r})'),
    "cond": ('$c = strlen({r}) > 0;', '$c ? (fn(): string => {r}) : (fn(): string => "x")'),
}
# sink: {s} setup, {p} maker statements, {c} closure, {b} what happens meanwhile, {t} a read of $v at the end
SINKS = [
    '{s} {p} $f = {c}; {b} echo $f(), $f(); {t}',
    '{s} {p} {b} echo ({c})(); {t}',
    '{s} {p} list<fn(): string> $fs = []; $fs[] = {c}; {b} echo $fs[0](); $g = copy($fs); echo $g[0](); {t}',
    '{s} {p} $fs = [{c}, fn(): string => "k"]; {b} $h = array_pop($fs); echo $fs[0](); {t}',
    '{s} {p} map<string, fn(): string> $m = ["a" => {c}]; {b} echo $m["a"](); $m["a"] = fn(): string => "o"; echo $m["a"](); {t}',
    'function scen(): fn(): string {{ {s} {p} $f = {c}; {b} {t} return $f; }} $r = scen(); echo $r(), $r();',
    'function scen(): fn(): string {{ {s} {p} {b} return {c}; }} $r = scen(); echo $r(); $r = scen(); echo $r();',
    'function scen(): list<fn(): string> {{ {s} {p} $f = {c}; {b} return [$f, {c}]; }} $r = scen(); echo $r[0](), $r[1]();',
    '{s} {p} echo call_it({c}); {b} {t}',
    '{s} {p} $t = go call_it({c}); {b} echo await $t; {t}',
    '{s} {p} $h = new Holder({c}); {b} $q = $h->f; echo $q(); $h2 = copy($h); $q2 = $h2->f; echo $q2(); {t}',
    '{s} {p} $h = new Holder(fn(): string => "0"); $h->f = {c}; {b} $q = $h->f; echo $q(); {t}',
    '{s} {p} $f = {c}; $f = fn(): string => "other"; {b} echo $f(); {t}',
    '{s} {p} $f = fn(): string => "first"; $f = {c}; {b} echo $f(); $f = {c}; echo $f(); {t}',
    '{s} list<fn(): string> $fs = []; for ($i = 0; $i < 3; $i++) {{ {p} $fs[] = {c}; {b} }} foreach ($fs as $f) {{ echo $f(); }} {t}',
    '{s} for ($i = 0; $i < 3; $i++) {{ {p} $f = {c}; {b} echo $f(); }} {t}',
    '{s} {p} $f = {c}; for ($i = 0; $i < 3; $i++) {{ {b} echo $f(); }} {t}',
    '{s} {p} $f = {c}; $i = 0; while ($i < 2) {{ echo $f(); {b} $f = {c}; $i++; }} {t}',
    '{s} {p} $fs = [fn(): string => "0", fn(): string => "1"]; foreach ($fs as &$g) {{ $g = {c}; }} {b} echo $fs[0](), $fs[1](); {t}',
    'function scen(): string throws {{ {s} {p} $f = {c}; {b} $n = try fail(1); return $f(); }} echo try scen() catch ($e) {{ "caught"; }};',
    'function scen(): list<fn(): string> throws {{ {s} {p} $r = [{c}, {c}]; {b} $n = try fail(1); return $r; }} $x = try scen() catch ($e) {{ [fn(): string => "caught"]; }}; echo $x[0]();',
    '{s} {p} $f = {c}; $g = fn(): string => $f() . "g"; {b} echo $g(); $k = copy($g); echo $k(); {t}',
    '{s} {p} $f = {c}; $g = $f; {b} echo $g(); {t}',
]

def progs():
    n = 0
    for (kind, (setup, r, mut, repl, move)), (mk, (pre, clo)), sink in itertools.product(KINDS.items(), MAKERS.items(), SINKS):
        for bname, b in (("-", ""), ("change", mut), ("replace", repl), ("move", move)):
            for t in ("", "echo " + r + ";"):
                if "{t}" not in sink and t:
                    continue
                n += 1
                code = sink.format(s=setup, p=pre.format(r=r, m=mut), c=clo.format(r=r, m=mut), b=b, t=t)
                yield n, f"{kind} {mk} {bname}{' +read' if t else ''} | {sink[:60]}", PRE + code + '\necho "\\n";\n'

def run(job):
    n, what, src = job
    f = f"{WORK}/t{n}.pn"
    open(f, "w").write(src)
    r = subprocess.run([sys.executable, P + "/pinac.py", "--emit-ll", f], capture_output=True, text=True)
    if r.returncode:
        last = (r.stderr.strip().splitlines() or ["?"])[-1]
        if "error: " not in last:               # a crash of the compiler itself, not a refusal
            return "BAD", what, ["compiler: " + last], f
        return "refused", what, last.split("error: ")[-1][:80], f
    o = f[:-3] + ".o"
    subprocess.run(["clang", "-O1", "-c", "-Wno-override-module", f[:-3] + ".ll", "-o", o], check=True, capture_output=True)
    subprocess.run(["clang", "-g", "-fsanitize=address,undefined", RT, o, "-lm", "-pthread", "-o", f[:-3]], check=True, capture_output=True)
    try:
        r = subprocess.run([f[:-3]], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "BAD", what, ["timeout"], f
    for x in (o, f[:-3], f[:-3] + ".ll"):
        os.remove(x)
    if "ERROR" in r.stderr or "runtime error" in r.stderr or r.returncode not in (0, 1):
        return "BAD", what, r.stderr.strip().splitlines()[:3] or [f"exit {r.returncode}"], f
    if r.returncode:                            # a panic is defined behaviour (copy() of a use (&...) closure)
        return ("panic", what, r.stderr.strip()[:80], f) if r.stderr.startswith("panic: ") else ("BAD", what, [r.stderr[:200]], f)
    return "ok", what, "", f

# the runtime once, with the sanitizers (building it for each program is most of the time)
RT = WORK + "/runtime.o"
subprocess.run(["clang", "-g", "-fsanitize=address,undefined", "-c", P + "/runtime.c", "-o", RT], check=True, capture_output=True)
count = {"ok": 0, "refused": 0, "panic": 0, "BAD": 0}
reasons = {}
with ThreadPoolExecutor(os.cpu_count() or 4) as pool:
    for res, what, info, f in pool.map(run, progs()):
        count[res] += 1
        if res == "BAD":
            print("BAD", what, "|", info, "|", f)
        elif res != "ok":
            reasons[res + ": " + info] = reasons.get(res + ": " + info, 0) + 1
print(f"closures: {sum(count.values())} programs: {count['ok']} ran clean, {count['refused']} refused by the compiler, {count['panic']} panicked, {count['BAD']} BAD")
if "-v" in sys.argv:
    for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
        print(v, k)
sys.exit(1 if count["BAD"] else 0)
