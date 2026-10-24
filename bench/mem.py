#!/usr/bin/env python3
"""Re-measure peak memory for every entry in results.json using memrun (tiny C parent)."""
import json, os, subprocess
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from run import LANGS, peak_mem_mb  # noqa: E402  (run.py only executes its loop under __main__)

res = json.load(open("results.json"))
for b, langs in res.items():
    for lang in LANGS:
        langs[lang]["mem_mb"] = peak_mem_mb(LANGS[lang](b))
        print(f"{b:8} {lang:8} {langs[lang]['mem_mb']:7.1f} MB", flush=True)
json.dump(res, open("results.json", "w"), indent=1)
