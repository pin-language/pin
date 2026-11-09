#!/usr/bin/env python3
"""JSON fuzzing: mutated JSON (inserted tokens, cut and copied pieces, bad escapes, huge numbers, deep nesting) read
by json_decode() into nested classes and by json_string / json_int / json_raw / json_at / json_len / json_has /
json_float / json_bool, built with ASAN + UBSAN. Every input must end in a value or an error, never a sanitizer
report or a crash.

    python3 tools/fuzz_json.py [count]        (default 60000; macOS or Linux, clang)
"""
import os, random, subprocess, sys, tempfile
P = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 60000
PROG = r'''class Line { public function __construct(public string $item, public int $qty, public ?float $price = null) {} }
class Order {
    public list<Line> $lines = [];
    public map<string,string> $tags = [];
    public list<?int> $codes = [];
    public function __construct(public string $id, list<Line> $lines, map<string,string> $tags,
                                public ?bool $paid = null, list<?int> $codes = []) {
        $this->lines = copy($lines); $this->tags = copy($tags); $this->codes = copy($codes);
    }
}
for (;;) {
    $s = try read_line() catch ($e) { return; };
    echo json_string($s, "id"), json_int($s, "x"), json_len(json_raw($s, "lines")), json_string(json_at(json_raw($s, "lines"), 0), "item"),
         json_has($s, "tags") ? "t" : "f", json_float(json_at(json_raw($s, "lines"), 1), "price"), json_bool($s, "paid") ? "p" : "n", "\n";
    Order $o = try json_decode($s) catch ($e) { echo "E", $e->code, "\n"; continue; };
    echo "OK ", count($o->lines), " ", json_encode($o), "\n";
}
'''
d = tempfile.mkdtemp(prefix="pin-fuzz-json-")
open(os.path.join(d, "j.pn"), "w").write(PROG)
subprocess.run([sys.executable, P + "/pinac.py", "--emit-ll", "j.pn"], cwd=d, check=True)
subprocess.run(["clang", "-O1", "-c", "-Wno-override-module", "j.ll", "-o", "j.o"], cwd=d, check=True)
subprocess.run(["clang", "-g", "-fsanitize=address,undefined", P + "/runtime.c", "j.o", "-lm", "-pthread", "-o", "j"], cwd=d, check=True)
rng = random.Random(11)
base = ['{"id":"a1","lines":[{"item":"tea","qty":2,"price":4.5},{"item":"ไทย","qty":1,"price":1e3}],"tags":{"k":"v","x":"\\u0e44"},"paid":true,"codes":[1,null,3]}',
        '{"id":"","lines":[],"tags":{}}', '{"x":5,"id":"z","lines":[{"item":"a","qty":-9223372036854775808}],"tags":{"a":""},"paid":null}']
toks = list('{}[]":,\\') + ['null', 'true', 'false', '1e999', '-0', '\\ud800', '\\u', '"\\', '9' * 30, ' ', '\x01', 'é', 'ไ', '.', 'e', '-']
cases = []
for _ in range(COUNT):
    s = rng.choice(base)
    for _ in range(rng.randint(1, 5)):
        op, i = rng.random(), rng.randint(0, len(s))
        if op < 0.4:
            s = s[:i] + rng.choice(toks) + s[i:]
        elif op < 0.7:
            s = s[:i] + s[i + rng.randint(1, 5):]
        else:
            j = rng.randint(0, len(s))
            s = s[:i] + s[j:j + rng.randint(1, 10)] + s[i:]
    cases.append(s.replace("\n", " "))
cases += ["[" * 5000 + "]" * 5000, '{"id":' + '"x' * 3000, "", "{}", "null", '{"id":"a","lines":' + "[" * 100]
r = subprocess.run([os.path.join(d, "j")], input="\n".join(cases) + "\n", capture_output=True, text=True, timeout=1800)
ok = r.stdout.count("OK ")
print(f"{len(cases)} inputs: {ok} decoded, {len(cases) - ok} refused; exit {r.returncode}")
if r.returncode or "ERROR" in r.stderr or "runtime error" in r.stderr:
    print("\n".join(r.stderr.splitlines()[:20]))
    sys.exit(1)
