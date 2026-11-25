#!/usr/bin/env python3
"""SQLite fuzzing: mutated text and numbers stored and read back through db_exec / db_query / db_int /
db_float / db_string / db_is_null / db_json / db_all / db_one (runtime/sqlite.c), including SQL that turns values
into blobs and cuts them mid-character (so SQLite hands back bytes that are not UTF-8), JSON functions on mutated
JSON, LIKE / GLOB patterns, and reading a column as the wrong type. Built with ASAN + UBSAN: every input must give
a result or a Pin panic, never a sanitizer report, a crash, a hang or a string that is not valid UTF-8.
PostgreSQL is not covered (it needs a server: tests/pg with PIN_TEST_POSTGRES).

    python3 tools/fuzz_sqlite.py [count]        (default 20000; macOS or Linux, clang, SQLite headers)
"""
import collections, os, random, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fuzz_strings import FRAG, INTS, text  # noqa: E402  (the same mutated text)
P = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
END = b"\x1d\n"
PROG = r'''class Row { public function __construct(public string $x, public ?int $n = null, public ?float $f = null) {} }
function cols(rows $r): string {
    $S = chr(30);
    return db_string($r, "s") . $S . strval(db_int($r, "s")) . $S . strval(db_float($r, "s")) . $S
        . strval(db_int($r, "n")) . $S . db_string($r, "n") . $S . strval(db_float($r, "f")) . $S . db_string($r, "f")
        . $S . db_string($r, "b") . $S . strval(db_int($r, "b")) . $S . (db_is_null($r, "n") ? "null" : "set");
}
$db = db_open(":memory:");
db_exec($db, "CREATE TABLE t (id INTEGER PRIMARY KEY, s TEXT, n INTEGER, f REAL, b BLOB)", []);
for (;;) {
    $line = try read_line() catch ($e) { return; };
    $p = explode("\t", $line);
    if (count($p) < 5) { echo "short", chr(29), "\n"; continue; }
    $op = intval($p[0]);
    $i = $p[1];
    $j = $p[2];
    $a = $p[3];
    $b = $p[4];
    $S = chr(30);
    $out = "";
    if ($op == 0) {
        $c = db_exec($db, "INSERT INTO t (s, n, f, b) VALUES (?, ?, ?, CAST(? AS BLOB))", [$a, $i, $b, $b]);
        $r = db_query($db, "SELECT s, n, f, b FROM t WHERE id = ?", [strval(db_last_id($db))]);
        while (db_next($r)) { $out = $out . cols($r); }
        $out = strval($c) . $S . $out;
    } elseif ($op == 1) {
        $r = db_query($db, "SELECT CAST(substr(CAST(? AS BLOB), ?, ?) AS TEXT) AS s, ? AS n, ? AS f, CAST(substr(CAST(? AS BLOB), ?) AS TEXT) AS b",
                      [$a, $i, $j, $i, $b, $b, $j]);
        while (db_next($r)) { $out = cols($r); }
    } elseif ($op == 2) {
        $out = db_json($db, "SELECT CAST(substr(CAST(? AS BLOB), ?, ?) AS TEXT) AS x, CAST(? AS BLOB) AS y, hex(?) AS h, length(?) AS l, ? + 0 AS v, char(?, ?) AS c",
                       [$a, $i, $j, $b, $a, $a, $b, $i, $j]);
    } elseif ($op == 3) {
        list<Row> $rows = db_all($db, "SELECT coalesce(CAST(substr(CAST(? AS BLOB), ?) AS TEXT), '') AS x, CAST(? AS INTEGER) AS n, CAST(? AS REAL) AS f", [$a, $j, $i, $b]);
        foreach ($rows as $row) { $out = $out . $row->x . $S . strval($row->n ?? -1) . $S . strval($row->f ?? -1.0); }
    } elseif ($op == 4) {
        ?Row $one = db_one($db, "SELECT s AS x, n, f FROM t WHERE s LIKE ? OR s GLOB ? ORDER BY id DESC LIMIT 1", [$a, $b]);
        if ($one !== null) { $out = $one->x . $S . strval($one->n ?? -1); } else { $out = "none"; }
    } elseif ($op == 5) {
        $out = db_json($db, "SELECT json_valid(?) AS v, CASE WHEN json_valid(?) THEN json_extract(?, ?) END AS x", [$a, $a, $a, $b]);
    } elseif ($op == 6) {
        $out = db_json($db, "SELECT replace(?, ?, ?) AS r, upper(?) AS u, lower(?) AS l, trim(?, ?) AS t, instr(?, ?) AS i, printf('%.*s', ?, ?) AS p",
                       [$a, $b, $a, $a, $b, $a, $b, $a, $b, $i, $a]);
    } else {
        $c = db_exec($db, "UPDATE t SET s = ?, n = ? WHERE id = (SELECT max(id) FROM t)", [$b, $j]);
        $out = strval($c) . $S . db_json($db, "SELECT s, n, f, b FROM t ORDER BY id DESC LIMIT 3", []);
    }
    echo $out, chr(29), "\n";
}
'''
OPS = 8
NUMS = [str(n) for n in INTS] + ["1.5", "-0.0", "1e308", "1e999", "NaN", "abc", "", " 7 ", "0x10", "9" * 40]


def main():
    d = tempfile.mkdtemp(prefix="pin-fuzz-sqlite-")
    open(os.path.join(d, "q.pn"), "w").write(PROG)
    subprocess.run([sys.executable, P + "/pinac.py", "q.pn", "-o", "q"], cwd=d, check=True,
                   env=dict(os.environ, PIN_SANITIZE="1"))
    rng = random.Random(33)
    json_like = ['{"a":[1,2,{"b":"ไทย"}]}', '[1,', '{"a":"\\ud800"}', '"x"', '[' * 300 + ']' * 300, '{"a":1e999}']
    paths = ["$", "$.a", "$.a[2].b", "$.a[#-1]", "$[", "$.a[99999999999]", "x"]
    cases = []
    for n in range(COUNT):
        op = n % OPS
        num = lambda: (rng.choice(NUMS) if rng.random() < 0.8 else str(rng.randint(-20, 20))).encode()
        a, b = text(rng), text(rng)
        if op == 5:
            a = (rng.choice(json_like) if rng.random() < 0.5 else rng.choice(FRAG)).encode()
            b = rng.choice(paths).encode()
        cases.append(b"%d\t%s\t%s\t%s\t%s" % (op, num(), num(), a, b))
    env = dict(os.environ, ASAN_OPTIONS="allocator_may_return_null=1:max_allocation_size_mb=256:detect_leaks=0")
    panics, reports, bad_utf8, slow = collections.Counter(), [], [], []
    start = done = 0
    while start < len(cases):
        chunk = cases[start:]
        try:
            r = subprocess.run([os.path.join(d, "q")], input=b"\n".join(chunk) + b"\n", capture_output=True,
                               timeout=300, env=env)
            out, err, code, timed_out = r.stdout, r.stderr.decode(errors="replace"), r.returncode, False
        except subprocess.TimeoutExpired as e:
            out, err, code, timed_out = e.stdout or b"", (e.stderr or b"").decode(errors="replace"), -1, True
        results = out.split(END)
        finished = len(results) - 1
        for k, res in enumerate(results[:finished]):
            try:
                res.decode("utf-8")
            except UnicodeDecodeError:
                bad_utf8.append((chunk[k], res[:200]))
        done += finished
        if code == 0:
            break
        failed = chunk[finished] if finished < len(chunk) else b"(after the last input)"
        if "ERROR: " in err or "runtime error" in err:
            reports.append((failed, "\n".join(ln for ln in err.splitlines() if ln.strip())[:3000]))
        elif timed_out:
            slow.append(failed)
        elif "panic:" in err:
            opn = failed.split(b"\t")[0].decode()
            msg = err[err.index("panic:"):].splitlines()[0]
            panics[f"op {opn}: {msg[:90]}"] += 1
        elif "fatal: out of memory" in err:
            panics["out of memory (a size the allocator refused)"] += 1
        else:
            reports.append((failed, f"exit {code}: {err[:2000]}"))
        start += finished + 1
    print(f"{len(cases)} inputs: {done} gave a result, {sum(panics.values())} panicked, {len(slow)} too slow, "
          f"{len(bad_utf8)} invalid UTF-8 results, {len(reports)} sanitizer/crash reports")
    for k, v in panics.most_common(15):
        print(f"  {v:5}  {k}")
    for c in slow[:5]:
        print("SLOW  ", c[:300])
    for c, res in bad_utf8[:5]:
        print("BAD UTF-8  ", c[:300], "->", res)
    for c, rep in reports[:3]:
        print("REPORT  ", c[:300]); print(rep)
    if reports or slow or bad_utf8:
        sys.exit(1)


if __name__ == "__main__":
    main()
