#!/usr/bin/env python3
"""Ownership fuzzing: every way of making a list, string or object value (try f(), ??, ?:, await go, calls,
methods, properties, items, copy) put into every place that keeps or reads one (list and map literals, append,
item and property assignment, variables, return, json_encode, foreach), each built with ASAN + UBSAN and run.
A program the compiler refuses is fine; one that runs and trips the sanitizers is a bug (found B16).

    python3 tools/fuzz_ownership.py           (macOS or Linux, clang)
"""
import itertools, os, subprocess, sys, tempfile
P = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="pin-fuzz-")
failed = 0

# ---- lists and strings
PRE_LISTS = r'''
class Box {
    public list<int> $xs = [];
    public string $s = "";
    public ?list<int> $maybe = null;
    public function __construct() {}
    public function get(): list<int> { return copy($this->xs); }
    public function name(): string { return $this->s . "!"; }
}
function mk(int $i): list<int> throws { if ($i == 99) { throw error(1, "x"); } return [$i, $i + 1]; }
function ms(int $i): string throws { if ($i == 99) { throw error(1, "x"); } return "s" . $i; }
function pick(int $i): ?list<int> { return $i > 0 ? [$i] : null; }
function picks(int $i): ?string { return $i > 0 ? "p" . $i : null; }
function plain(int $i): list<int> { return [$i]; }
function work(int $i): list<int> { return [$i * 10]; }
$o = new Box();
$o->xs = [5, 6];
$o->s = "box";
$o->maybe = [8];
list<list<int>> $ll = [[1], [2]];
map<string,list<int>> $ml = ["k" => [3]];
$m = pick(1);
$ms = picks(1);
$c = count($ll) > 1;
'''
LIST_EXPRS = ["try mk(1)", "$m ?? [7]", "plain(2)", "$o->get()", "copy($o->xs)", "$o->maybe ?? [4]",
              "await go work(3)", "copy($ll[0])", "copy($ml[\"k\"])", "pick(2) ?? [0]", "$c ? plain(1) : plain(2)",
              "try mk(1) ?? [1]"]
STR_EXPRS = ["try ms(1)", "$ms ?? \"d\"", "$o->name()", "$o->s", "$c ? \"a\" : $o->s", "picks(2) ?? \"z\"",
             "strtoupper($o->s)", "$ll[0] == [1] ? \"y\" : \"n\""]
SINKS_L = [
    ("$r = [{e}, [9]]; echo json_encode($r);", "list"),
    ("$r = [\"a\" => {e}]; echo json_encode($r);", "list"),
    ("list<list<int>> $r = []; $r[] = {e}; echo json_encode($r);", "list"),
    ("list<list<int>> $r = [[0]]; $r[0] = {e}; echo json_encode($r);", "list"),
    ("$q = new Box(); $q->xs = {e}; echo json_encode($q->xs);", "list"),
    ("$v = {e}; echo json_encode($v);", "list"),
    ("echo json_encode({e});", "list"),
    ("echo count({e});", "list"),
    ("foreach ({e} as $x) {{ echo $x; }}", "list"),
    ("function sinkf{n}(Box $b): list<int> {{ return {eb}; }} echo json_encode(sinkf{n}($o));", "listfn"),
    ("map<string,list<int>> $r = []; $r[\"z\"] = {e}; echo json_encode($r);", "list"),
    ("$ll[] = {e}; echo json_encode($ll);", "list"),
]
SINKS_S = [
    ("$r = [{e}, \"x\"]; echo json_encode($r);", "s"),
    ("$r = [\"a\" => {e}]; echo json_encode($r);", "s"),
    ("list<string> $r = []; $r[] = {e}; echo json_encode($r);", "s"),
    ("$q = new Box(); $q->s = {e}; echo $q->s;", "s"),
    ("$v = {e}; echo $v;", "s"),
    ("echo {e} . \"/\" . {e};", "s"),
    ("echo strlen({e});", "s"),
    ("$v = \"pre\"; $v .= {e}; echo $v;", "s"),
]
progs = []
n = 0
for sinks, exprs in ((SINKS_L, LIST_EXPRS), (SINKS_S, STR_EXPRS)):
    for (sink, kind), e in itertools.product(sinks, exprs):
        n += 1
        eb = e.replace("$o->", "$b->") if "$m" not in e and "$ll" not in e and "$ml" not in e and "$c" not in e else None
        if kind == "listfn" and eb is None:
            continue
        code = sink.format(e=e, n=n, eb=eb)
        progs.append((n, e, sink, PRE_LISTS + code + "\necho \"\\n\";\n"))
os.makedirs(WORK + "/lists", exist_ok=True)
bad = comp = ok = 0
for n, e, sink, src in progs:
    f = f"{WORK}/lists/t{n}.pn"
    open(f, "w").write(src)
    r = subprocess.run([sys.executable, P + "/pinac.py", "--emit-ll", f], capture_output=True, text=True)
    if r.returncode:
        comp += 1
        continue
    ll = f[:-3] + ".ll"
    o = f[:-3] + ".o"
    subprocess.run(["clang", "-O1", "-c", "-Wno-override-module", ll, "-o", o], check=True, capture_output=True)
    exe = f[:-3]
    subprocess.run(["clang", "-g", "-fsanitize=address,undefined", P + "/runtime.c", o, "-lm", "-pthread", "-o", exe],
                   check=True, capture_output=True)
    r = subprocess.run([exe], capture_output=True, text=True, timeout=20)
    if r.returncode or "ERROR" in r.stderr or "runtime error" in r.stderr:
        bad += 1
        print("BAD", n, e, "|", sink, "|", r.stderr.strip().splitlines()[1:3] if r.stderr else r.returncode)
    else:
        ok += 1
print(f"{len(progs)} programs: {ok} ran clean, {comp} refused by the compiler, {bad} BAD")

failed += bad

# ---- objects
PRE_OBJECTS = r'''
class Box {
    public list<int> $xs = [];
    public ?Box $inner = null;
    public function __construct(int $n) { $this->xs = [$n]; }
    public function twin(): Box { return new Box($this->xs[0] + 1); }
}
function mkb(int $i): Box throws { if ($i == 99) { throw error(1, "x"); } return new Box($i); }
function pickb(int $i): ?Box { return $i > 0 ? new Box($i) : null; }
function bx(int $i): Box { return new Box($i); }
$o = new Box(1);
$ob = pickb(2);
list<Box> $bs = [new Box(3)];
map<string,Box> $mb = ["k" => new Box(4)];
'''
EXPRS = ["new Box(5)", "copy($o)", "try mkb(6)", "pickb(7) ?? new Box(0)", "await go bx(8)", "$o->twin()",
         "copy($bs[0])", "copy($mb[\"k\"])", "bx(9)", "count($bs) > 0 ? bx(1) : bx(2)"]
SINKS = ["$r = [{e}, new Box(0)]; echo json_encode($r);",
         "$r = [\"a\" => {e}]; echo json_encode($r);",
         "$bs[] = {e}; echo json_encode($bs);",
         "$bs[0] = {e}; echo json_encode($bs);",
         "$mb[\"z\"] = {e}; echo json_encode($mb);",
         "$v = {e}; echo json_encode($v);",
         "$o->inner = {e}; echo json_encode($o);",
         "?Box $w = {e}; echo json_encode($w);",
         "echo json_encode({e});",
         "echo {e}->xs[0];",
         "function sk{n}(): Box throws {{ return {e}; }} echo json_encode(try sk{n}());"]
os.makedirs(WORK + "/objects", exist_ok=True)
bad = comp = ok = 0; reasons = {}
for n, (sink, e) in enumerate(itertools.product(SINKS, EXPRS)):
    if "sk{n}" in sink and ("$o" in e or "$bs" in e or "$mb" in e or "$ob" in e):
        continue
    src = PRE_OBJECTS + sink.format(e=e, n=n) + "\necho \"\\n\";\n"
    f = f"{WORK}/objects/t{n}.pn"; open(f, "w").write(src)
    r = subprocess.run([sys.executable, P + "/pinac.py", "--emit-ll", f], capture_output=True, text=True)
    if r.returncode:
        comp += 1; k = r.stderr.strip().splitlines()[-1].split("error: ")[-1][:90]; reasons[k] = reasons.get(k, 0) + 1; continue
    o = f[:-3] + ".o"
    subprocess.run(["clang", "-O1", "-c", "-Wno-override-module", f[:-3] + ".ll", "-o", o], check=True, capture_output=True)
    subprocess.run(["clang", "-g", "-fsanitize=address,undefined", P + "/runtime.c", o, "-lm", "-pthread", "-o", f[:-3]], check=True, capture_output=True)
    r = subprocess.run([f[:-3]], capture_output=True, text=True, timeout=20)
    if r.returncode or "ERROR" in r.stderr or "runtime error" in r.stderr:
        bad += 1; print("BAD", n, e, "|", sink, "|", r.stderr.strip().splitlines()[:3])
    else:
        ok += 1
print(f"{ok} clean, {comp} refused, {bad} BAD")
for k, v in sorted(reasons.items(), key=lambda x: -x[1]): print(v, k)

failed += bad

# ---- ?T items (list<?string>, map<string,?string>, list<?Box>)
PRE_OPT = PRE_OBJECTS + r'''
function ps(int $i): ?string { return $i > 0 ? "p" . $i : null; }
function tryps(int $i): ?string throws { if ($i == 99) { throw error(1, "x"); } return $i > 1 ? "t" . $i : null; }
?string $vs = ps(1);
list<?string> $los = ["a", null];
map<string,?string> $mos = ["k" => null, "j" => "v"];
list<?Box> $lob = [new Box(1), null];
'''
OPT_S = ["ps(1)", "ps(0)", "$vs", "try tryps(2)", "try tryps(0)", "$los[0]", "$mos[\"j\"]", "null", "\"lit\"",
         "$vs ?? \"d\"", "copy($los)[1]"]
OPT_B = ["pickb(1)", "pickb(0)", "$ob", "null", "new Box(2)", "copy($lob[0])", "$lob[0] === null ? null : new Box(3)"]
SINKS_OPT = [("list<?string> $r = [{e}, null]; echo json_encode($r);", OPT_S),
             ("$los[] = {e}; echo json_encode($los);", OPT_S),
             ("$los[1] = {e}; echo json_encode($los);", OPT_S),
             ("$mos[\"z\"] = {e}; echo json_encode($mos);", OPT_S),
             ("map<string,?string> $r = [\"a\" => {e}]; echo json_encode($r);", OPT_S),
             ("?string $v = {e}; echo $v ?? \"none\";", OPT_S),
             ("list<?Box> $r = [{e}]; echo json_encode($r);", OPT_B),
             ("$lob[] = {e}; echo json_encode($lob);", OPT_B),
             ("$lob[0] = {e}; echo json_encode($lob);", OPT_B),
             ("$o->inner = {e}; echo json_encode($o);", OPT_B),
             ("?Box $w = {e}; echo json_encode($w);", OPT_B)]
os.makedirs(WORK + "/opt", exist_ok=True)
bad = comp = ok = 0
for n, (sink, e) in enumerate((s_, e_) for s_, es in SINKS_OPT for e_ in es):
    src = PRE_OPT + sink.format(e=e) + "\necho \"\\n\";\n"
    f = f"{WORK}/opt/t{n}.pn"; open(f, "w").write(src)
    r = subprocess.run([sys.executable, P + "/pinac.py", "--emit-ll", f], capture_output=True, text=True)
    if r.returncode:
        comp += 1; continue
    o = f[:-3] + ".o"
    subprocess.run(["clang", "-O1", "-c", "-Wno-override-module", f[:-3] + ".ll", "-o", o], check=True, capture_output=True)
    subprocess.run(["clang", "-g", "-fsanitize=address,undefined", P + "/runtime.c", o, "-lm", "-pthread", "-o", f[:-3]], check=True, capture_output=True)
    r = subprocess.run([f[:-3]], capture_output=True, text=True, timeout=20)
    if r.returncode or "ERROR" in r.stderr or "runtime error" in r.stderr:
        bad += 1; print("BAD", n, e, "|", sink, "|", r.stderr.strip().splitlines()[:3])
    else:
        ok += 1
print(f"?T items: {ok} clean, {comp} refused, {bad} BAD")
failed += bad
sys.exit(1 if failed else 0)
