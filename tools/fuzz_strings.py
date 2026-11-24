#!/usr/bin/env python3
"""String fuzzing: every string, Unicode, URL, number-parsing and date built-in (runtime/text.c, unicode.c,
crypto.c, request.c) called with mutated text (Thai with stacked marks, emoji sequences, case mappings that change
length, long combining runs, %XX pieces, invalid UTF-8 that read_line() must clean) and edge-case ints, in a
program built with ASAN + UBSAN. Each input must give a result or a Pin panic, never a sanitizer report, a crash,
a hang or a string that is not valid UTF-8.

    python3 tools/fuzz_strings.py [count]        (default 30000; macOS or Linux, clang)
"""
import collections, os, random, subprocess, sys, tempfile
P = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 30000
SEP, END = "\x1e", "\x1d"
PROG = r'''function pad_type(int $i): int {
    $t = abs($i % 3);
    if ($t == 0) { return STR_PAD_RIGHT; }
    if ($t == 1) { return STR_PAD_LEFT; }
    return STR_PAD_BOTH;
}
function yes(bool $b): string { return $b ? "1" : "0"; }
function op(int $op, int $i, int $j, string $a, string $b): string {
    $S = chr(30);
    if ($op == 0) { return strval(strlen($a)) . $S . strval(mb_strlen($a)) . $S . strval(grapheme_strlen($a)) . $S . strval(mb_ord($a)) . $S . strval(ord($a)); }
    if ($op == 1) { return substr($a, $i, $j); }
    if ($op == 2) { return substr($a, $i); }
    if ($op == 3) { return mb_substr($a, $i, $j) . $S . mb_substr($a, $i); }
    if ($op == 4) { return grapheme_substr($a, $i, $j) . $S . grapheme_substr($a, $i); }
    if ($op == 5) { return implode($S, mb_str_split($a, $i)); }
    if ($op == 6) { return strval(strpos($a, $b)) . $S . strval(mb_strpos($a, $b)) . $S . yes(str_contains($a, $b)) . yes(str_starts_with($a, $b)) . yes(str_ends_with($a, $b)); }
    if ($op == 7) { return mb_chr($i); }
    if ($op == 8) { return mb_strtolower($a) . $S . mb_strtoupper($a) . $S . strtolower($a) . $S . strtoupper($a); }
    if ($op == 9) { return normalizer_normalize($a) . $S . normalizer_normalize($a, Normalizer::FORM_D) . $S . normalizer_normalize($a, Normalizer::FORM_KC) . $S . normalizer_normalize($a, Normalizer::FORM_KD); }
    if ($op == 10) { return str_repeat($a, $i); }
    if ($op == 11) { return str_pad($a, $i, $b, pad_type($j)); }
    if ($op == 12) { return mb_str_pad($a, $i, $b, pad_type($j)); }
    if ($op == 13) { return ucwords($a) . $S . strrev($a) . $S . ucfirst($a) . $S . lcfirst($a) . $S . trim($a) . $S . ltrim($a) . $S . rtrim($a) . $S . nl2br($a) . $S . htmlspecialchars($a); }
    if ($op == 14) { return str_replace($b, $a, $a . $b . $a); }
    if ($op == 15) {
        $d = try base64_decode($a) catch ($e) { "E" . strval($e->code); };
        $r = try base64_decode(base64_encode($a)) catch ($e) { "E" . strval($e->code); };
        return base64_encode($a) . $S . $d . $S . ($r == $a ? "same" : "DIFFERENT");
    }
    if ($op == 16) { return rawurlencode($a) . $S . rawurldecode($a) . $S . urldecode($a) . $S . (rawurldecode(rawurlencode($a)) == $a ? "same" : "DIFFERENT"); }
    if ($op == 17) { return implode($S, explode($b, $a)) . $S . strval(count(explode($b, $a))); }
    if ($op == 18) {
        return yes(is_numeric($a)) . $S . strval(intval($a)) . $S . strval(floatval($a))
            . $S . strval(filter_var($a, FILTER_VALIDATE_INT) ?? -1) . $S . strval(filter_var($a, FILTER_VALIDATE_FLOAT) ?? -1.0)
            . $S . yes(filter_var($a, FILTER_VALIDATE_BOOL) ?? false) . $S . (filter_var($a, FILTER_VALIDATE_EMAIL) ?? "-")
            . $S . (filter_var($a, FILTER_VALIDATE_URL) ?? "-") . $S . (filter_var($a, FILTER_VALIDATE_IP) ?? "-");
    }
    if ($op == 19) { return number_format(floatval($a), $i, $b, $a) . $S . number_format(floatval($b), $i % 40); }
    if ($op == 20) { return date($a, $i); }
    if ($op == 21) { return url_path($a) . $S . query_param($a, $b) . $S . yes(query_has($a, $b)) . $S . form_param($a, $b) . $S . yes(form_has($a, $b)); }
    if ($op == 22) { return yes(json_validate($a)) . $S . json_encode($a) . $S . json_encode([$a => $b]); }
    if ($op == 23) { return yes(ctype_digit($a)) . yes(ctype_alpha($a)) . yes(ctype_alnum($a)) . yes(ctype_space($a)) . yes(ctype_upper($a)) . yes(ctype_lower($a)) . yes(ctype_xdigit($a)); }
    if ($op == 24) { return chr($i); }
    if ($op == 25) { return sprintf("%s|%5.2s|%-8s|%.1s|%d|%x|%o|%b|%c|%.3f|%e|%+05d", $a, $a, $b, $b, $i, $i, $i, $j, $j, floatval($a), floatval($b), $j); }
    if ($op == 26) { return hash("sha256", $a) . $S . hash_hmac("sha256", $a, $b) . $S . yes(hash_equals($a, $b)) . $S . yes(password_verify($a, $b)); }
    if ($op == 27) { return strval(round(floatval($a), $i)) . $S . strval(intdiv($i, $j)) . $S . strval($i ** ($j % 70)) . $S . strval(pow(floatval($a), floatval($b))); }
    if ($op == 28) { return implode(",", range($i, $j, intval($a))); }
    if ($op == 29) { return implode($S, array_slice(explode(",", $a), $i, $j)) . $S . implode($S, array_slice(explode(",", $a), $i)); }
    if ($op == 30) { return implode($S, array_unique(explode(",", $a . "," . $b))) . $S . json_encode(array_count_values(explode(",", $a))) . $S . json_encode(array_flip(array_count_values(explode(",", $b)))); }
    return "";
}
for (;;) {
    $line = try read_line() catch ($e) { return; };
    $f = explode("\t", $line);
    if (count($f) < 5) { echo "short", chr(29), "\n"; continue; }
    echo op(intval($f[0]), intval($f[1]), intval($f[2]), $f[3], $f[4]), chr(29), "\n";
}
'''
OPS = 31
INTS = [0, 1, -1, 2, 3, 4, 5, 7, 8, 10, 16, 31, 32, 63, 64, 65, 100, 127, 128, 255, 256, 1000, 65535, 0x10FFFF, 0x110000,
        0xD800, 0xDFFF, -2, -3, -100, 2**31 - 1, -2**31, 2**32, 2**48, 2**62, 2**63 - 1, -2**63, -2**63 + 1, 1762646400,
        253402300800, -62135596800]
FRAG = ["a", "Z", "tea", "Hello World", " ", "  x  ", "ก", "ไทย", "กำ", "ก่", "ก่ำ", "น้ำ", "ั", "่", "ํา", "เ", "๑๒",
        "é", "́", "́" * 40, "ﬁ", "①", "ß", "ẞ", "İ", "ı", "ǅ", "Σ", "ς", "ΐ", "ﬃ", "Å", "Å",
        "👨‍👩‍👧", "🇹🇭", "🏳️‍🌈", "‍", "﻿", "\U0010ffff", "\U0001f600", "한", "각", " ",
        "%", "%2", "%41", "%e0%b9%84", "%E0%B9", "%ff", "%c0%80", "%00", "+", "&", "=", "?", "#", "/a?b=c&d=%20e",
        "/x?q=ไทย&q=2", "a@b.co", "@", "http://x.y/z", "::1", "1.2.3.4", "999.1.1.1", "1", "-0", "0x1A", " 42 ",
        "1e999", "-1e999", "1.5e-400", "NaN", "INF", "9" * 30, "-9223372036854775808", "9223372036854775808",
        "0.1", ".", "e", "true", "on", "Y-m-d H:i:s", "D, jS F Y", "c r U", "\\", "\\Y", "l N S w z t L o",
        "dXNlcjpzZWNyZXQ=", "4LmE4LiX4Lii", "=", "==", "-_", "/+", "AAAA", "gA", "{\"a\":1}", "[", "\"", "\\u0e44",
        "<b>&amp;'\"", "\n", "\r", "\x00", "\x01", "\x7f", ",", ",,", "a,b,,c", "$pbkdf2-sha256$1$00$" + "0" * 64,
        "$pbkdf2-sha256$2$abcd$" + "a" * 64, "$pbkdf2-sha256$", "$pbkdf2-sha256$10000001$00$" + "0" * 64,
        "$pbkdf2-sha256$1$" + "ab" * 40 + "$" + "0" * 64]
BAD = [b"\xff", b"\xfe", b"\xc0\x80", b"\xc1\xbf", b"\xe0\x80\x80", b"\xed\xa0\x80", b"\xf4\x90\x80\x80", b"\xe0\xb8",
       b"\xe0", b"\x80", b"\xbf", b"\xf0\x9f\x98", b"\xf8\x88\x80\x80\x80"]


def text(rng):
    parts = [rng.choice(FRAG).encode() for _ in range(rng.randint(0, 5))]
    s = b"".join(parts)
    r = rng.random()
    if r < 0.03:
        s = rng.choice(FRAG).encode() * rng.randint(100, 3000)
    elif r < 0.05:
        s = ("e" + "́" * rng.randint(100, 4000)).encode()
    elif r < 0.07:
        s = ("ก" + "่ั้" * rng.randint(50, 1500)).encode()
    for _ in range(rng.randint(0, 2)):
        i = rng.randint(0, len(s))
        op = rng.random()
        if op < 0.4:
            s = s[:i] + rng.choice(BAD) + s[i:]
        elif op < 0.7:
            s = s[:i] + s[i + rng.randint(1, 3):]
        else:
            s = s[:i] + bytes([rng.randint(0, 255)]) + s[i:]
    return s.replace(b"\t", b" ").replace(b"\n", b" ").replace(b"\r", b" ")


def main():
    d = tempfile.mkdtemp(prefix="pin-fuzz-strings-")
    open(os.path.join(d, "s.pn"), "w").write(PROG)
    subprocess.run([sys.executable, P + "/pinac.py", "s.pn", "-o", "s"], cwd=d, check=True,
                   env=dict(os.environ, PIN_SANITIZE="1"))
    rng = random.Random(21)
    cases = []
    for n in range(COUNT):
        op = n % OPS
        i, j = (rng.choice(INTS) if rng.random() < 0.7 else rng.randint(-50, 50) for _ in range(2))
        cases.append(b"%d\t%d\t%d\t%s\t%s" % (op, i, j, text(rng), text(rng)))
    # big allocations return NULL (Pin then panics "out of memory") instead of filling RAM and swap
    env = dict(os.environ, ASAN_OPTIONS="allocator_may_return_null=1:max_allocation_size_mb=256:detect_leaks=0",
               UBSAN_OPTIONS="print_stacktrace=1")
    panics, slow, bad_utf8, reports, roundtrip = collections.Counter(), [], [], [], []
    done, start = 0, 0
    while start < len(cases):
        chunk = cases[start:]
        try:
            r = subprocess.run([os.path.join(d, "s")], input=b"\n".join(chunk) + b"\n", capture_output=True,
                               timeout=120, env=env)
            out, err, code, timed_out = r.stdout, r.stderr.decode(errors="replace"), r.returncode, False
        except subprocess.TimeoutExpired as e:
            out, err, code, timed_out = e.stdout or b"", (e.stderr or b"").decode(errors="replace"), -1, True
        results = out.split(END.encode() + b"\n")
        finished = len(results) - 1
        for k, res in enumerate(results[:finished]):
            try:
                txt = res.decode("utf-8")
            except UnicodeDecodeError:
                bad_utf8.append((chunk[k], res[:200]))
                continue
            if "DIFFERENT" in txt:
                roundtrip.append(chunk[k])
        done += finished
        if code == 0:
            break
        failed = chunk[finished] if finished < len(chunk) else b"(after the last input)"
        if "ERROR: AddressSanitizer" in err or "runtime error" in err or "ERROR: " in err:
            reports.append((failed, "\n".join(ln for ln in err.splitlines() if ln.strip())[:3000]))
        elif "fatal: out of memory" in err and code == 2:
            opn = failed.split(b"\t")[0].decode()
            panics[f"op {opn}: out of memory (a size the allocator refused)"] += 1
        elif timed_out:
            slow.append(failed)
        elif err.startswith("panic:") or "\npanic:" in err:
            msg = err[err.index("panic:"):].splitlines()[0]
            opn = failed.split(b"\t")[0].decode()
            panics[f"op {opn}: {msg[:70]}"] += 1
        else:
            reports.append((failed, f"exit {code}: {err[:2000]}"))
        start += finished + 1
    print(f"{len(cases)} inputs: {done} gave a result, {sum(panics.values())} panicked, {len(slow)} too slow, "
          f"{len(bad_utf8)} invalid UTF-8 results, {len(roundtrip)} failed round trips, {len(reports)} sanitizer/crash reports")
    for k, v in panics.most_common():
        print(f"  {v:5}  {k}")
    for c in slow[:5]:
        print("SLOW  ", c[:300])
    for c, res in bad_utf8[:5]:
        print("BAD UTF-8  ", c[:300], "->", res)
    for c in roundtrip[:5]:
        print("ROUND TRIP  ", c[:300])
    for c, rep in reports[:3]:
        print("REPORT  ", c[:300]); print(rep)
    if reports or slow or bad_utf8 or roundtrip:
        sys.exit(1)


if __name__ == "__main__":
    main()
