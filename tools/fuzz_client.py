#!/usr/bin/env python3
"""HTTP client fuzzing: http_get / http_post / http_stream + stream_next (runtime/client.c) against a local fake
server that answers with mutated responses (broken status lines, odd or doubled Content-Length, bad chunk sizes,
whitespace where a number should be, SSE events with odd ids, bodies cut short, responses that fill the read buffer
exactly, slow pieces), built with ASAN + UBSAN. Every call must give a value or an error, never a sanitizer report,
a crash, a hang or a string that is not valid UTF-8. Only 127.0.0.1 is contacted (no name lookups).

    python3 tools/fuzz_client.py [count]        (default 3000; macOS or Linux, clang)
"""
import collections, os, random, socket, subprocess, sys, tempfile, threading, time
P = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
PORT = 18282
END = b"\x1d\n"
PROG = r'''for (;;) {
    $line = try read_line() catch ($e) { return; };
    $f = explode("\t", $line);
    if (count($f) < 3) { echo "short", chr(29), "\n"; continue; }
    $url = $f[1];
    if ($f[0] == "get") {
        echo try http_get($url, ["X-Test" => "1"], 400) catch ($e) { "E" . strval($e->code); }, chr(29), "\n";
    } elseif ($f[0] == "post") {
        echo try http_post($url, [], "{\"a\":1}", 400) catch ($e) { "E" . strval($e->code); }, chr(29), "\n";
    } else {
        $s = try http_stream($url, [], intval($f[2]), 400) catch ($e) { echo "E", $e->code, chr(29), "\n"; continue; };
        $out = "";
        for ($k = 0; $k < 6; $k++) {
            $ev = try stream_next($s, 300) catch ($e) { "E" . strval($e->code); };
            $out = $out . $ev . chr(30) . strval(stream_last_id($s)) . chr(30);
            if (str_starts_with($ev, "E")) { break; }
        }
        echo $out, chr(29), "\n";
    }
}
'''
BASE = [b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello",
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n3;x=y\r\n th\r\n0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\nid: 3\ndata: a\ndata: b\n\n: c\n\nid: 9\r\ndata: x\r\n\r\n",
        b"HTTP/1.0 200 OK\r\n\r\nuntil close \xe0\xb9\x84",
        b"HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nnot found",
        b"HTTP/1.1 500 X\r\nTransfer-Encoding: chunked\r\n\r\n4\r\noops\r\n0\r\n\r\n"]
TOKS = [b"\r\n", b"\r\n\r\n", b"\n", b"\n\n", b" ", b"   \r\n", b"\t", b"-1", b"-", b"+", b"0", b"99999999999999999999",
        b"ffffffffffffffff", b"7fffffffffffffff", b";ext", b"\x00", b"\xff", b"\xe0\xb8", b"Content-Length: ",
        b"Content-Length: 3\r\n", b"Transfer-Encoding: chunked\r\n", b"data:", b"data: ", b"id:", b"id: -5\n",
        b"id: 99999999999999999999\n", b"id:\n", b": ping\n\n", b"HTTP/1.1 ", b"HTTP/1.", b"A" * 70000]


def mutate(rng, s):
    for _ in range(rng.randint(0, 4)):
        i, op = rng.randint(0, len(s)), rng.random()
        if op < 0.5:
            s = s[:i] + rng.choice(TOKS) + s[i:]
        elif op < 0.8:
            s = s[:i] + s[i + rng.randint(1, 8):]
        else:
            s = s[:i] + bytes([rng.randint(0, 255)]) + s[i + 1:]
    return s


def special(rng):
    """Responses that end exactly where a buffer ends, or whose numbers are only whitespace."""
    head = b"HTTP/1.1 200 OK\r\n"
    pick = rng.randint(0, 9)
    fill = rng.choice([16384, 32768, 65536]) - rng.randint(0, 3)
    if pick == 0:
        return head + b"Transfer-Encoding: chunked\r\n\r\n" + b"\r\n" + b" " * (fill - 49)
    if pick == 1:
        return head + b"Content-Length:" + b" " * rng.randint(0, 3) + b"\r\n\r\n"
    if pick == 2:
        return b"HTTP/1.1\r\n\r\n"
    if pick == 3:
        return b"HTTP/1.1 " + b" " * rng.randint(0, 5) + b"\r\n\r\n"
    if pick == 4:
        return head + b"Content-Type: text/event-stream\r\n\r\nid:" + b" " * rng.randint(0, 3) + b"\n\n"
    if pick == 5:
        return head + b"Content-Type: text/event-stream\r\n\r\ndata: x\nid:\n\n" + b"\n" * rng.randint(0, 3)
    if pick == 6:
        return head + b"Transfer-Encoding: chunked\r\n\r\n" + b"%x\r\n" % 20000 + b"z" * 20000 + b"\r\n0\r\n\r\n"
    if pick == 7:
        return head + b"X: " + b"a" * (65536 - 60) + b"\r\n\r\n"
    if pick == 8:
        return head + b"Content-Type: text/event-stream\r\n\r\n" + b"data: " + b"q" * rng.randint(1 << 19, (1 << 20) + 10) + b"\n\n"
    return head + b"Transfer-Encoding: chunked\r\n\r\n" + b"\t\r\n5\r\nhello\r\n0\r\n\r\n"


RESP, slow = {}, set()


def serve(sock):
    while True:
        try:
            c, _ = sock.accept()
        except OSError:
            return
        threading.Thread(target=answer, args=(c,), daemon=True).start()


def answer(c):
    try:
        c.settimeout(2)
        req = b""
        while b"\r\n\r\n" not in req:
            b = c.recv(65536)
            if not b:
                return
            req += b
        path = req.split(b" ", 2)[1] if req.count(b" ") >= 2 else b"/0"
        n = int(path.strip(b"/").split(b"?")[0] or b"0") if path.strip(b"/").split(b"?")[0].isdigit() else -1
        data = RESP.get(n, b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        rng = random.Random(n)
        i = 0
        while i < len(data):
            k = rng.choice([1, 7, 100, 4096, 70000])
            c.sendall(data[i:i + k])
            i += k
            if n in slow:
                time.sleep(0.01)
        if n % 17 == 0:
            time.sleep(0.5)          # keep it open past the client's deadline
    except OSError:
        pass
    finally:
        c.close()


def main():
    d = tempfile.mkdtemp(prefix="pin-fuzz-client-")
    open(os.path.join(d, "c.pn"), "w").write(PROG)
    subprocess.run([sys.executable, P + "/pinac.py", "c.pn", "-o", "c"], cwd=d, check=True,
                   env=dict(os.environ, PIN_SANITIZE="1"))
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", PORT))
    sock.listen(64)
    threading.Thread(target=serve, args=(sock,), daemon=True).start()
    rng = random.Random(5)
    base = f"http://127.0.0.1:{PORT}"
    cases = []
    for n in range(COUNT):
        RESP[n] = special(rng) if rng.random() < 0.15 else mutate(rng, rng.choice(BASE))
        if rng.random() < 0.05:
            slow.add(n)
        kind = rng.choice(["get", "get", "post", "stream", "stream"])
        url = f"{base}/{n}"
        r = rng.random()
        if r < 0.04:      # URLs that must fail before any connection (no name lookups: only local hosts)
            url = rng.choice(["http://", "http:/x", "https://127.0.0.1/", "http://:80/", "http://" + "a" * 300 + "/",
                              f"http://127.0.0.1:{'9' * 9}/", f"http://127.0.0.1:/{n}", "http://127.0.0.1:x/",
                              f"http://127.0.0.1:{PORT}/a b", "ftp://x", ""])
        elif r < 0.06:
            url = f"http://localhost:{PORT}/{n}?" + "%" * rng.randint(1, 5000)
        cases.append(b"%s\t%s\t%d" % (kind.encode(), url.encode(), rng.choice([0, 1, 7, -1, 2**63 - 1, -2**63])))
    env = dict(os.environ, ASAN_OPTIONS="allocator_may_return_null=1:max_allocation_size_mb=256:detect_leaks=0")
    reports, bad_utf8, hung, outcomes = [], [], [], collections.Counter()
    start = 0
    while start < len(cases):
        chunk = cases[start:]
        try:
            r = subprocess.run([os.path.join(d, "c")], input=b"\n".join(chunk) + b"\n", capture_output=True,
                               timeout=900, env=env)
            out, err, code, timed_out = r.stdout, r.stderr.decode(errors="replace"), r.returncode, False
        except subprocess.TimeoutExpired as e:
            out, err, code, timed_out = e.stdout or b"", (e.stderr or b"").decode(errors="replace"), -1, True
        results = out.split(END)
        finished = len(results) - 1
        for k, res in enumerate(results[:finished]):
            try:
                txt = res.decode("utf-8")
            except UnicodeDecodeError:
                bad_utf8.append((chunk[k], res[:200]))
                continue
            outcomes[txt[:4] if txt.startswith("E") else "value"] += 1
        if code == 0:
            break
        failed = chunk[finished] if finished < len(chunk) else b"(after the last input)"
        n = failed.split(b"\t")[1].rsplit(b"/", 1)[-1]
        resp = RESP.get(int(n), b"") if n.isdigit() else b""
        if timed_out:
            hung.append((failed, resp[:300]))
        else:
            reports.append((failed, resp[:300], "\n".join(ln for ln in err.splitlines() if ln.strip())[:3000]))
        start += finished + 1
    sock.close()
    print(f"{len(cases)} calls: {dict(outcomes)}; {len(hung)} hung, {len(bad_utf8)} invalid UTF-8 results, "
          f"{len(reports)} sanitizer/crash reports")
    for c, resp in hung[:5]:
        print("HUNG  ", c, resp)
    for c, res in bad_utf8[:5]:
        print("BAD UTF-8  ", c, res)
    for c, resp, rep in reports[:3]:
        print("REPORT  ", c, "\n  response:", resp); print(rep)
    if reports or hung or bad_utf8:
        sys.exit(1)


if __name__ == "__main__":
    main()
