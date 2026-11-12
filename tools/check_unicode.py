#!/usr/bin/env python3
"""Check Pin's Unicode functions against Python's: grapheme_strlen() / grapheme_substr() against
unicodedata.iter_graphemes() (UAX #29) on random strings made of the code points that matter for the rules
(every property in runtime/unicode_data.h, Thai, Hangul, emoji, regional indicators, Indic conjuncts), plus
mb_strlen(); mb_strtolower() / mb_strtoupper() against str.lower() / str.upper(), and normalizer_normalize()
against unicodedata.normalize(), on every code point and on random strings.

    python3.15 tools/check_unicode.py [count]      (needs Python 3.15+ and clang)
"""
import os
import random
import subprocess
import sys
import tempfile
import unicodedata as ud

if not hasattr(ud, "iter_graphemes"):
    sys.exit("needs Python 3.15 or newer (unicodedata.iter_graphemes)")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 20000

# a few code points of every kind the rules look at
pool, seen = [], {}
for cp in range(0x110000):
    if 0xD800 <= cp <= 0xDFFF or cp in (0x0A, 0x0D, 0x1F):      # line ends; 0x1F separates the answers
        continue
    c = chr(cp)
    key = (ud.grapheme_cluster_break(c), ud.extended_pictographic(c), ud.indic_conjunct_break(c))
    if seen.get(key, 0) < 40:
        seen[key] = seen.get(key, 0) + 1
        pool.append(c)
pool += list("ที่นี่ไทยกำแพงน้ำ") + ["‍", "क", "्", "ष", "ᄀ", "ᅡ", "ᆨ", "가",
                                     "\U0001f1f9", "\U0001f1ed", "\U0001f468", "\U0001f3fd", "e", "́", " "]
rng = random.Random(29)
cases = ["".join(rng.choice(pool) for _ in range(rng.randint(1, 12))) for _ in range(COUNT)]

prog = r'''
for (;;) {
    $line = try read_line() catch ($e) { return; };
    $n = grapheme_strlen($line);
    $out = strval(mb_strlen($line)) . " " . strval($n);
    for ($i = 0; $i < $n; $i++) { $out .= chr(31) . grapheme_substr($line, $i, 1); }
    echo $out, "\n";
}
'''
with tempfile.TemporaryDirectory() as d:
    src = os.path.join(d, "g.pn")
    with open(src, "w") as f:
        f.write(prog)
    exe = os.path.join(d, "g")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "pinac.py"), src, "-o", exe], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stdout + r.stderr)
    r = subprocess.run([exe], input="\n".join(cases) + "\n", capture_output=True, text=True, encoding="utf-8")
lines = r.stdout.split("\n")
bad = 0
for case, line in zip(cases, lines):
    want = [g for g in ud.iter_graphemes(case)]
    head, *parts = line.split("\x1f")
    got_len, got_n = (int(x) for x in head.split())
    if got_len != len(case) or got_n != len(want) or [p for p in parts] != [str(g) for g in want]:
        bad += 1
        if bad <= 10:
            print("MISMATCH", [hex(ord(c)) for c in case], "want", [[hex(ord(c)) for c in str(g)] for g in want],
                  "got", got_n, [[hex(ord(c)) for c in p] for p in parts])
print(f"graphemes: {len(cases)} strings, {bad} differ (Unicode {ud.unidata_version}, {len(pool)} code points in the pool)")

# mb_strtolower / mb_strtoupper / normalizer_normalize: every code point alone, then random strings of the code
# points that change (cased letters, marks, Hangul, compatibility characters) with ASCII and Thai between them
prog2 = r'''
for (;;) {
    $s = try read_line() catch ($e) { return; };
    $u = chr(31);
    echo mb_strtolower($s), $u, mb_strtoupper($s), $u, normalizer_normalize($s), $u,
         normalizer_normalize($s, Normalizer::FORM_D), $u, normalizer_normalize($s, Normalizer::FORM_KC), $u,
         normalizer_normalize($s, Normalizer::FORM_KD), "\n";
}
'''
singles = [chr(cp) for cp in range(0x110000) if not 0xD800 <= cp <= 0xDFFF and cp not in (0x0A, 0x0D, 0x1F, 0x85)
           and chr(cp).splitlines() == [chr(cp)]]
busy = [c for c in singles if c.lower() != c or c.upper() != c or ud.normalize("NFKD", c) != c or ud.combining(c)]
busy += list("aZ ไทย่้") + ["\u03a3"] * 20 + ["\u0301", "\u0327", "\u1100", "\u1161", "\u11a8"]
mixed = ["".join(rng.choice(busy) for _ in range(rng.randint(1, 10))) for _ in range(COUNT)]
with tempfile.TemporaryDirectory() as d:
    src = os.path.join(d, "c.pn")
    with open(src, "w") as f:
        f.write(prog2)
    exe = os.path.join(d, "c")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "pinac.py"), src, "-o", exe], capture_output=True, text=True)
    if r.returncode:
        sys.exit(r.stdout + r.stderr)
    inputs = singles + mixed
    r = subprocess.run([exe], input="\n".join(inputs) + "\n", capture_output=True, text=True, encoding="utf-8")
got = r.stdout.split("\n")
bad2 = 0
for s, line in zip(inputs, got):
    want = [s.lower(), s.upper()] + [ud.normalize(f, s) for f in ("NFC", "NFD", "NFKC", "NFKD")]
    have = line.split("\x1f")
    if have != want:
        bad2 += 1
        if bad2 <= 10:
            names = ["lower", "upper", "NFC", "NFD", "NFKC", "NFKD"]
            print("MISMATCH", [hex(ord(c)) for c in s], [(n, [hex(ord(c)) for c in w], [hex(ord(c)) for c in h])
                                                         for n, w, h in zip(names, want, have + [""] * 6) if w != h])
print(f"case and normalisation: {len(singles)} code points alone + {len(mixed)} strings, {bad2} differ")
sys.exit(1 if bad or bad2 else 0)
