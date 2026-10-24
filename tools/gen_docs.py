#!/usr/bin/env python3
"""Generate Pin's built-in reference from spec/pin_builtins.py — the one list of built-ins.

  python3 tools/gen_docs.py --check            examples still print their recorded output; spec matches compiler
  python3 tools/gen_docs.py --record           run every example and store its output in spec/outputs.json
  python3 tools/gen_docs.py --docs ../pin-docs write reference/*.md and the AI guide's built-in list there
                                               (also rewrites the VS Code grammar's built-in list here)
Exit code 0 = everything matches.
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUTS = os.path.join(ROOT, "spec", "outputs.json")
GRAMMAR = os.path.join(ROOT, "editors", "vscode", "syntaxes", "pin.tmLanguage.json")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SPEC = load(os.path.join(ROOT, "spec", "pin_builtins.py"), "pin_builtins")
PINAC = load(os.path.join(ROOT, "pinac.py"), "pinac")


def run_example(item):
    """Compile (and, unless run=False, run) one example; return (ok, output_or_error)."""
    with tempfile.TemporaryDirectory(prefix="pin-doc-") as d:
        src = os.path.join(d, "example.pn")
        with open(src, "w", encoding="utf-8") as f:
            f.write(item["example"] + "\n")
        exe = os.path.join(d, "example")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "pinac.py"), src, "-o", exe],
                           capture_output=True, text=True)
        if r.returncode:
            return False, r.stderr.strip()
        if not item["run"]:
            return True, None
        r = subprocess.run([exe], capture_output=True, text=True, timeout=30)
        if r.returncode:
            return False, f"exit {r.returncode}: {r.stderr.strip()}"
        return True, r.stdout


def check_names():
    """Every compiler built-in is documented, and nothing documented is missing from the compiler."""
    fns = {i["name"] for i in SPEC.ITEMS if i["kind"] == "function"}
    consts = {i["name"] for i in SPEC.ITEMS if i["kind"] == "constant"}
    problems = []
    if fns != set(PINAC.BUILTINS):
        problems.append(f"functions in pinac.BUILTINS but not in the spec: {sorted(set(PINAC.BUILTINS) - fns)}; "
                        f"in the spec but not the compiler: {sorted(fns - set(PINAC.BUILTINS))}")
    if consts != set(PINAC.CONSTANTS):
        problems.append(f"constants differ: compiler {sorted(PINAC.CONSTANTS)}, spec {sorted(consts)}")
    return problems


def check(record=False):
    recorded = json.load(open(OUTPUTS)) if os.path.exists(OUTPUTS) and not record else {}
    problems, outputs = check_names(), {}
    for item in SPEC.ITEMS:
        ok, out = run_example(item)
        if not ok:
            problems.append(f"{item['name']}: example does not work: {out}")
            continue
        if out is None:
            continue
        outputs[item["name"]] = out
        if not record and recorded.get(item["name"]) != out:
            problems.append(f"{item['name']}: output changed\n    recorded {recorded.get(item['name'])!r}\n    now      {out!r}")
    if record:
        with open(OUTPUTS, "w") as f:
            json.dump(outputs, f, indent=1, ensure_ascii=False)
            f.write("\n")
    return problems


# --------------------------------------------------------------------------------------------- markdown

def anchor(name):
    return re.sub(r"[^a-z0-9_-]", "", name.lower().replace("(", "").replace(")", ""))


def page(cat, items, outputs):
    lines = [f"# {cat['title']}", "",
             f"Part of the [Pin built-in reference](README.md). Generated from `spec/pin_builtins.py` in the "
             f"[pin repository](https://github.com/pin-language/pin); every example below is compiled and run by "
             f"its test suite, and the output shown is the real output.", ""]
    lines += ["| | |", "|---|---|"]
    lines += [f"| [`{i['name']}`](#{anchor(i['name'])}) | {i['summary']} |" for i in items]
    lines.append("")
    for i in items:
        lines += [f"## {i['name']}", "", "```php", i["signature"], "```", "", i["summary"], ""]
        lines += ["```php", i["example"], "```", ""]
        if i["name"] in outputs:
            lines += ["Output:", "", "```", outputs[i["name"]].rstrip("\n"), "```", ""]
        else:
            lines += ["(Starts a server; not run in the docs.)", ""]
        lines += [f"- {n}" for n in i["notes"]]
        if i["php"]:
            lines.append(f"- **PHP:** {i['php']}")
        if i["notes"] or i["php"]:
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def index(cats, by_cat):
    lines = ["# Pin built-in reference", "",
             "Every built-in function, constant and cast in Pin, grouped by topic. Generated from "
             "`spec/pin_builtins.py` in the [pin repository](https://github.com/pin-language/pin); the examples are "
             "compiled and run by its test suite.", "",
             "New to Pin? Start with the [language guide](../language.md). Writing Pin with an AI agent? Give it the "
             "[AI guide](../ai-guide.md).", ""]
    for c in cats:
        names = ", ".join(f"[`{i['name']}`]({c['id']}.md#{anchor(i['name'])})" for i in by_cat[c["id"]])
        lines += [f"## [{c['title']}]({c['id']}.md)", "", names, ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def builtin_list():
    fns = " ".join(i["name"] for i in SPEC.ITEMS if i["kind"] == "function")
    consts = " ".join(i["name"] for i in SPEC.ITEMS if i["kind"] == "constant")
    return (f"`{fns}` · constants `{consts}` · casts `(int)` from float/bool/string, `(float)` from int/string, "
            f"`(string)` from int/float/bool. Details and examples for each: "
            f"https://github.com/pin-language/docs/blob/HEAD/reference/README.md")


def write_docs(docs):
    outputs = json.load(open(OUTPUTS))
    by_cat = {c["id"]: [i for i in SPEC.ITEMS if i["category"] == c["id"]] for c in SPEC.CATEGORIES}
    ref = os.path.join(docs, "reference")
    os.makedirs(ref, exist_ok=True)
    for c in SPEC.CATEGORIES:
        with open(os.path.join(ref, c["id"] + ".md"), "w", encoding="utf-8") as f:
            f.write(page(c, by_cat[c["id"]], outputs))
    with open(os.path.join(ref, "README.md"), "w", encoding="utf-8") as f:
        f.write(index(SPEC.CATEGORIES, by_cat))
    guide = os.path.join(docs, "ai-guide.md")
    t = open(guide, encoding="utf-8").read()
    new = f"<!-- builtins:start (generated by tools/gen_docs.py in the pin repo) -->\n{builtin_list()}\n<!-- builtins:end -->"
    t2 = re.sub(r"<!-- builtins:start.*?<!-- builtins:end -->", new, t, flags=re.S)
    if t2 == t and "builtins:start" not in t:
        sys.exit("ai-guide.md has no <!-- builtins:start --> ... <!-- builtins:end --> markers")
    open(guide, "w", encoding="utf-8").write(t2)


def write_grammar():
    g = json.load(open(GRAMMAR))
    fns = "|".join(i["name"] for i in SPEC.ITEMS if i["kind"] == "function")
    g["repository"]["builtin-calls"]["match"] = f"\\b({fns})\\s*(?=\\()"
    with open(GRAMMAR, "w") as f:
        json.dump(g, f, indent=2, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--record" in args:
        problems = check(record=True)
    else:
        problems = check()
    for p in problems:
        print("  " + p)
    if problems:
        sys.exit(1)
    if "--docs" in args:
        write_docs(args[args.index("--docs") + 1])
        write_grammar()
        print("wrote reference/, the AI guide's built-in list, and the VS Code grammar")
    print(f"ok: {len(SPEC.ITEMS)} built-ins, every example matches")
