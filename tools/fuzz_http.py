#!/usr/bin/env python3
"""HTTP fuzzing: mutated requests (inserted CR/LF, NUL and non-UTF-8 bytes, broken %XX, huge or doubled
Content-Length, chunked, 70 KB headers, pipelined requests, random bytes) sent to tests/server/api.pn built with
ASAN + UBSAN. The server must answer or close each one, keep serving, and print no sanitizer report.

    python3 tools/fuzz_http.py [count]        (default 1500; macOS or Linux, clang)
"""
import collections, os, random, socket, subprocess, sys, tempfile, time
P = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
PORT = 18181
d = tempfile.mkdtemp(prefix="pin-fuzz-http-")
src = open(os.path.join(P, "tests", "server", "api.pn")).read().replace("serve(8081", f"serve({PORT}")
open(os.path.join(d, "api.pn"), "w").write(src)
subprocess.run([sys.executable, P + "/pinac.py", "api.pn", "-o", "api"], cwd=d, check=True,
               env=dict(os.environ, PIN_SANITIZE="1"))
log = open(os.path.join(d, "server.log"), "w")
srv = subprocess.Popen([os.path.join(d, "api")], stdout=log, stderr=log,
                       env=dict(os.environ, PN_THREADS="2", PN_READ_TIMEOUT_MS="300"))
time.sleep(1)
rng = random.Random(9)
base = [b"GET /user/42 HTTP/1.1\r\nHost: x\r\n\r\n",
        b"POST /echo HTTP/1.1\r\nContent-Length: 13\r\nContent-Type: application/json\r\n\r\n{\"a\":[1,2,3]}",
        b"GET /primes/100?x=%E0%B9%84&y=a+b HTTP/1.0\r\nConnection: keep-alive\r\n\r\n",
        b"GET /hello HTTP/1.1\r\nX: y\r\n\r\nGET /hello HTTP/1.1\r\n\r\n"]
toks = [b"\r\n", b"\r\n\r\n", b"\x00", b"\xff", b"\xc8", b"%", b"%E0", b"Content-Length: 99999999999\r\n",
        b"Content-Length: -1\r\n", b"Transfer-Encoding: chunked\r\n", b" ", b":", b"?", b"\n",
        b"Content-Length: 5\r\nContent-Length: 5\r\n", b"A" * 70000]
codes, fails = collections.Counter(), 0
try:
    for n in range(COUNT):
        s = rng.choice(base)
        for _ in range(rng.randint(1, 3)):
            i, op = rng.randint(0, len(s)), rng.random()
            if op < 0.5:
                s = s[:i] + rng.choice(toks) + s[i:]
            elif op < 0.8:
                s = s[:i] + s[i + rng.randint(1, 8):]
            else:
                s = s[:i] + bytes([rng.randint(0, 255)]) + s[i + 1:]
        try:
            c = socket.create_connection(("127.0.0.1", PORT), timeout=0.5)
            c.sendall(s)
            data = b""
            try:
                while len(data) < 20:
                    b = c.recv(65536)
                    if not b:
                        break
                    data += b
            except (socket.timeout, ConnectionResetError, BrokenPipeError):
                pass
            c.close()
            codes[data[9:12].decode(errors="replace") if data.startswith(b"HTTP/1.1") else "none"] += 1
        except OSError:
            fails += 1
            time.sleep(0.2)
            if fails > 20:
                break
    c = socket.create_connection(("127.0.0.1", PORT), timeout=2)
    c.sendall(b"GET /hello HTTP/1.1\r\nConnection: close\r\n\r\n")
    alive = c.recv(20).startswith(b"HTTP/1.1 200")
except OSError:
    alive = False
finally:
    srv.terminate()
    srv.wait(timeout=10)
    log.close()
report = [ln for ln in open(os.path.join(d, "server.log")) if "ERROR" in ln or "runtime error" in ln]
print(f"{COUNT} requests: answers {dict(codes)}, connect failures {fails}, still serving: {alive}")
if report or not alive or fails:
    print("".join(report[:20]))
    sys.exit(1)
