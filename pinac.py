#!/usr/bin/env python3
"""Prototype compiler: PHP-like syntax -> LLVM IR -> native binary.

Pipeline:  source -> lexer -> parser (AST) -> checker+codegen (LLVM IR) -> clang -> binary

Language rules in this prototype:
  * Every value has one fixed type. Locals are inferred from their first assignment.
  * Types: int, float, bool, string, list<int|float|bool>, map<int|string, int|float|bool|string>
  * Ownership (Rust-style, checked at compile time; no garbage collector, no reference counting):
      - every list, map and string has exactly one owner variable and is freed when the owner's
        function returns (or when the variable is overwritten)
      - lists and maps MOVE on assignment: after `$b = $a;` using $a is a compile error
      - strings COPY on assignment (like PHP), literals are never copied
      - passing a value to a function lends it read-only; the caller keeps ownership
      - borrowed values (heap parameters, foreach variables) cannot be changed, moved or returned
        as-is; strings are copied automatically, lists/maps need copy()
      - a list cannot be appended to, and a map cannot be changed, while a foreach walks over it
  * Checks follow every path separately (class Flow): a variable is usable only if it is assigned
    and not moved on every path that reaches the use; a value moved inside one if branch is still
    usable in the other branch.
  * There are no global variables, so request handlers running on many threads share nothing.
"""
import os
import re
import struct
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


class CompileError(Exception):
    def __init__(self, msg, line):
        super().__init__(msg)
        self.msg, self.line = msg, line


# ----------------------------------------------------------------------------- lexer

TOKEN_RE = re.compile(r"""
  (?P<NL>\n)
 |(?P<WS>[ \t\r]+)
 |(?P<COMMENT>//[^\n]*|\#[^\n]*|/\*(?:.|\n)*?\*/)
 |(?P<OPEN><\?php)
 |(?P<FLOAT>\d+\.\d+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)
 |(?P<INT>\d+)
 |(?P<STRING>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
 |(?P<VAR>\$[A-Za-z_]\w*)
 |(?P<IDENT>[A-Za-z_]\w*)
 |(?P<OP>\+\+|--|\+=|-=|\*=|/=|%=|\.=|===|!==|==|!=|<=|>=|=>|&&|\|\||\?\?|\[\]|[-+*/%<>=!(){}\[\];,:.?])
""", re.VERBOSE)


class Tok:
    __slots__ = ("kind", "val", "line")

    def __init__(self, kind, val, line):
        self.kind, self.val, self.line = kind, val, line

    def __repr__(self):
        return f"{self.kind}:{self.val!r}@{self.line}"


def lex(src):
    toks, pos, line = [], 0, 1
    while pos < len(src):
        m = TOKEN_RE.match(src, pos)
        if not m:
            raise CompileError(f"unexpected character {src[pos]!r}", line)
        kind, text = m.lastgroup, m.group()
        if kind not in ("NL", "WS", "COMMENT", "OPEN"):
            toks.append(Tok(kind, text, line))
        line += text.count("\n")
        pos = m.end()
    toks.append(Tok("EOF", "", line))
    return toks


# ----------------------------------------------------------------------------- AST

class Node:
    def __init__(self, kind, line, **kw):
        self.kind, self.line = kind, line
        self.__dict__.update(kw)

    def __repr__(self):
        return f"{self.kind}({ {k: v for k, v in self.__dict__.items() if k not in ('kind', 'line')} })"


TYPE_NAMES = {"int", "float", "bool", "string", "list", "map", "void"}
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "$": "$", "0": "\0"}
INTERP_RE = re.compile(r"(?<!\\)(\{\$[A-Za-z_]\w*\}|\$[A-Za-z_]\w*)")


def unescape(body, double):
    if not double:
        return body.replace("\\'", "'").replace("\\\\", "\\")
    out, i = [], 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body) and body[i + 1] in ESCAPES:
            out.append(ESCAPES[body[i + 1]])
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# ----------------------------------------------------------------------------- parser

class Parser:
    def __init__(self, toks):
        self.toks, self.i = toks, 0

    @property
    def cur(self):
        return self.toks[self.i]

    def peek(self, n=1):
        return self.toks[min(self.i + n, len(self.toks) - 1)]

    def at(self, val, kind=None):
        t = self.cur
        return t.val == val and (kind is None or t.kind == kind)

    def eat(self, val=None, kind=None):
        t = self.cur
        if val is not None and t.val != val:
            raise CompileError(f"expected '{val}' but found '{t.val or 'end of file'}'", t.line)
        if kind is not None and t.kind != kind:
            want = {"VAR": "a variable like $x", "IDENT": "a name"}.get(kind, kind)
            raise CompileError(f"expected {want} but found '{t.val or 'end of file'}'", t.line)
        self.i += 1
        return t

    def accept(self, val):
        if self.cur.val == val and self.cur.kind in ("OP", "IDENT"):
            self.i += 1
            return True
        return False

    def program(self):
        funcs, main = [], []
        while self.cur.kind != "EOF":
            if self.at("function", "IDENT"):
                funcs.append(self.function())
            else:
                main.append(self.statement())
        return funcs, main

    def function(self):
        line = self.eat("function").line
        name = self.eat(kind="IDENT").val
        self.eat("(")
        params = []
        while not self.at(")"):
            if self.cur.kind == "VAR":
                raise CompileError(f"parameter {self.cur.val} needs a type, e.g. int {self.cur.val}", self.cur.line)
            ptype = self.type_()
            pname = self.eat(kind="VAR").val
            params.append((ptype, pname))
            if not self.accept(","):
                break
        self.eat(")")
        if not self.at(":"):
            raise CompileError(f"function {name}() needs a return type, e.g. ': int' or ': void'", self.cur.line)
        self.eat(":")
        ret = self.type_()
        body = self.block()
        return Node("func", line, name=name, params=params, ret=ret, body=body)

    def type_(self):
        t = self.cur
        if t.val == "?":
            raise CompileError("nullable types (?T) are not in this prototype yet", t.line)
        name = self.eat(kind="IDENT").val
        if name not in TYPE_NAMES:
            raise CompileError(f"unknown type '{name}'", t.line)
        if name == "list":
            self.eat("<")
            inner = self.type_()
            self.eat(">")
            if inner not in ("int", "float", "bool"):
                raise CompileError(f"list<{inner}> is not supported yet (int, float, bool only)", t.line)
            return f"list<{inner}>"
        if name == "map":
            self.eat("<")
            k = self.type_()
            self.eat(",")
            v = self.type_()
            self.eat(">")
            if k not in ("int", "string"):
                raise CompileError(f"map keys must be int or string, not {k}", t.line)
            if v not in KIND:
                raise CompileError(f"map values must be int, float, bool or string, not {v}", t.line)
            return f"map<{k},{v}>"
        if self.at("<"):
            raise CompileError(f"type '{name}' takes no parameters", t.line)
        return name

    def block(self):
        line = self.eat("{").line
        body = []
        while not self.at("}"):
            if self.cur.kind == "EOF":
                raise CompileError("missing '}'", line)
            body.append(self.statement())
        self.eat("}")
        return Node("block", line, body=body)

    def statement(self):
        t = self.cur
        if t.val == "{" and t.kind == "OP":
            return self.block()
        if t.kind == "IDENT":
            if t.val == "if":
                return self.if_()
            if t.val == "while":
                self.eat()
                self.eat("(")
                cond = self.expr()
                self.eat(")")
                return Node("while", t.line, cond=cond, body=self.statement())
            if t.val == "for":
                self.eat()
                self.eat("(")
                init = None if self.at(";") else self.simple()
                self.eat(";")
                cond = None if self.at(";") else self.expr()
                self.eat(";")
                step = None if self.at(")") else self.simple()
                self.eat(")")
                return Node("for", t.line, init=init, cond=cond, step=step, body=self.statement())
            if t.val == "foreach":
                self.eat()
                self.eat("(")
                seq = self.expr()
                self.eat("as")
                key, var = None, self.eat(kind="VAR").val
                if self.accept("=>"):
                    key, var = var, self.eat(kind="VAR").val
                self.eat(")")
                return Node("foreach", t.line, seq=seq, key=key, var=var, body=self.statement())
            if t.val == "return":
                self.eat()
                val = None if self.at(";") else self.expr()
                self.eat(";")
                return Node("return", t.line, val=val)
            if t.val == "echo":
                self.eat()
                args = [self.expr()]
                while self.accept(","):
                    args.append(self.expr())
                self.eat(";")
                return Node("echo", t.line, args=args)
            if t.val in ("break", "continue"):
                self.eat()
                self.eat(";")
                return Node(t.val, t.line)
            if (t.val in TYPE_NAMES and self.peek().kind == "VAR") or (t.val in ("list", "map") and self.peek().val == "<"):
                vtype = self.type_()
                name = self.eat(kind="VAR").val
                self.eat("=")
                val = self.expr()
                self.eat(";")
                return Node("decl", t.line, vtype=vtype, name=name, val=val)
            if t.val == "function":
                raise CompileError("functions must be declared at the top level", t.line)
        s = self.simple()
        self.eat(";")
        return s

    def if_(self):
        line = self.eat("if").line
        self.eat("(")
        cond = self.expr()
        self.eat(")")
        then = self.statement()
        els = None
        if self.at("elseif", "IDENT"):
            self.toks[self.i] = Tok("IDENT", "if", self.cur.line)
            els = self.if_()
        elif self.accept("else"):
            els = self.if_() if self.at("if", "IDENT") else self.statement()
        return Node("if", line, cond=cond, then=then, els=els)

    def simple(self):
        t = self.cur
        e = self.expr()
        op = self.cur.val
        if self.cur.kind == "OP" and op in ("=", "+=", "-=", "*=", "/=", "%=", ".="):
            if e.kind not in ("var", "index", "append"):
                raise CompileError("left side of an assignment must be a variable, a list item or a map entry", t.line)
            if e.kind == "append" and op != "=":
                raise CompileError("use $a[] = value to append", t.line)
            self.eat()
            val = self.expr()
            return Node("assign", t.line, target=e, op=op, val=val)
        if self.cur.kind == "OP" and op in ("++", "--"):
            if e.kind not in ("var", "index"):
                raise CompileError(f"'{op}' needs a variable or a list/map item", t.line)
            self.eat()
            one = Node("int", t.line, val=1)
            return Node("assign", t.line, target=e, op="+=" if op == "++" else "-=", val=one)
        if e.kind == "append":
            raise CompileError("$a[] can only be assigned to", t.line)
        if e.kind != "call":
            raise CompileError("this expression does nothing on its own", t.line)
        return Node("exprstmt", t.line, expr=e)

    # lowest precedence first. PHP 8: '.' binds looser than + - but tighter than comparisons
    BINARY = [("??",), ("||",), ("&&",), ("==", "!=", "===", "!=="), ("<", "<=", ">", ">="),
              (".",), ("+", "-"), ("*", "/", "%")]

    def expr(self, level=None):
        if level is None:                          # ternary: lowest precedence
            c = self.expr(0)
            if self.cur.kind == "OP" and self.cur.val == "?":
                t = self.eat()
                a = self.expr()
                self.eat(":")
                b = self.expr()
                return Node("ternary", t.line, c=c, a=a, b=b)
            return c
        if level == len(self.BINARY):
            return self.unary()
        left = self.expr(level + 1)
        while self.cur.kind == "OP" and self.cur.val in self.BINARY[level]:
            t = self.eat()
            if t.val == "??":                      # right-associative
                right = self.expr(level)
                return Node("coalesce", t.line, l=left, r=right)
            right = self.expr(level + 1)
            op = {"===": "==", "!==": "!="}.get(t.val, t.val)
            left = Node("bin", t.line, op=op, l=left, r=right)
            if level in (3, 4) and self.cur.kind == "OP" and self.cur.val in self.BINARY[level]:
                raise CompileError("chained comparisons need parentheses", self.cur.line)
        return left

    def unary(self):
        t = self.cur
        if t.kind == "OP" and t.val in ("!", "-"):
            self.eat()
            return Node("unary", t.line, op=t.val, e=self.unary())
        if (t.val == "(" and self.peek().kind == "IDENT" and self.peek().val in ("int", "float", "string")
                and self.peek(2).val == ")"):
            self.eat()
            to = self.eat().val
            self.eat(")")
            return Node("cast", t.line, to=to, e=self.unary())
        return self.postfix()

    def postfix(self):
        e = self.primary()
        while True:
            t = self.cur
            if t.kind == "OP" and t.val == "[":
                self.eat()
                idx = self.expr()
                self.eat("]")
                e = Node("index", t.line, seq=e, idx=idx)
            elif t.kind == "OP" and t.val == "[]":
                self.eat()
                if e.kind != "var":
                    raise CompileError("append needs a variable: $a[] = value", t.line)
                e = Node("append", t.line, seq=e)
            elif t.kind == "OP" and t.val == "-" and self.peek().val == ">":
                raise CompileError("objects (->) are not in this prototype yet", t.line)
            else:
                return e

    def primary(self):
        t = self.cur
        if t.kind == "INT":
            self.eat()
            v = int(t.val)
            if v >= 2 ** 63:
                raise CompileError(f"integer {t.val} is too large", t.line)
            return Node("int", t.line, val=v)
        if t.kind == "FLOAT":
            self.eat()
            return Node("float", t.line, val=float(t.val))
        if t.kind == "STRING":
            self.eat()
            double = t.val[0] == '"'
            body = t.val[1:-1]
            if double and INTERP_RE.search(body):
                parts = []
                for piece in INTERP_RE.split(body):
                    if piece.startswith("{$"):
                        parts.append(Node("var", t.line, name=piece[1:-1]))
                    elif piece.startswith("$"):
                        parts.append(Node("var", t.line, name=piece))
                    elif piece:
                        parts.append(Node("str", t.line, val=unescape(piece, True)))
                return Node("interp", t.line, parts=parts)
            return Node("str", t.line, val=unescape(body, double))
        if t.kind == "VAR":
            self.eat()
            return Node("var", t.line, name=t.val)
        if t.kind == "IDENT":
            if t.val in ("true", "false"):
                self.eat()
                return Node("bool", t.line, val=t.val == "true")
            if t.val == "null":
                raise CompileError("null is not in this prototype yet", t.line)
            self.eat()
            if not self.at("("):
                raise CompileError(f"unknown name '{t.val}' (variables start with $)", t.line)
            self.eat("(")
            args = []
            while not self.at(")"):
                args.append(self.expr())
                if not self.accept(","):
                    break
            self.eat(")")
            return Node("call", t.line, name=t.val, args=args)
        if t.kind == "OP" and t.val == "(":
            self.eat()
            e = self.expr()
            self.eat(")")
            return e
        if t.kind == "OP" and t.val == "[]":
            self.eat()
            return Node("listlit", t.line, items=[])
        if t.kind == "OP" and t.val == "[":
            self.eat()
            items, pairs = [], []
            while not self.at("]"):
                k = self.expr()
                if self.accept("=>"):
                    if items:
                        raise CompileError("cannot mix 'key => value' and plain items in one literal", t.line)
                    pairs.append((k, self.expr()))
                else:
                    if pairs:
                        raise CompileError("cannot mix 'key => value' and plain items in one literal", t.line)
                    items.append(k)
                if not self.accept(","):
                    break
            self.eat("]")
            if pairs:
                return Node("maplit", t.line, pairs=pairs)
            return Node("listlit", t.line, items=items)
        raise CompileError(f"unexpected '{t.val or 'end of file'}'", t.line)


# ----------------------------------------------------------------------------- types

KIND = {"int": 0, "float": 1, "bool": 2, "string": 3}
ELEM_LL = {"int": "i64", "float": "double", "bool": "i8"}
ELEM_SIZE = {"int": 8, "float": 8, "bool": 1}


def llty(t):
    return {"int": "i64", "float": "double", "bool": "i1", "void": "void"}.get(t, "ptr")


def is_list(t):
    return t.startswith("list<")


def is_map(t):
    return t.startswith("map<")


def is_heap(t):
    return t == "string" or is_list(t) or is_map(t)


def elem_of(t):
    return t[5:-1]


def map_kv(t):
    k, v = t[4:-1].split(",")
    return k, v


def free_fn(t):
    return "@pn_str_free" if t == "string" else ("@pn_list_free" if is_list(t) else "@pn_map_free")


def mentions(node, name):
    if isinstance(node, Node):
        if node.kind == "var" and node.name == name:
            return True
        return any(mentions(v, name) for k, v in node.__dict__.items() if k not in ("kind", "line"))
    if isinstance(node, (list, tuple)):
        return any(mentions(x, name) for x in node)
    return False


def flatten_concat(e):
    if e.kind == "bin" and e.op == ".":
        return flatten_concat(e.l) + flatten_concat(e.r)
    if e.kind == "interp":
        return list(e.parts)
    return [e]


BUILTINS = {"count", "fill", "copy", "sqrt", "intdiv", "strlen", "substr", "strpos", "str_contains",
            "str_starts_with", "str_ends_with", "str_repeat", "strtolower", "strtoupper", "trim", "intval",
            "strval", "json_encode", "isset", "array_key_exists", "unset", "serve", "http_status",
            "wrapping_add", "wrapping_sub", "wrapping_mul"}

RUNTIME_DECLS = """
declare ptr @pn_list_new(i64)
declare ptr @pn_list_fill(i64, i64, i64)
declare void @pn_list_push(ptr, i64, i64)
declare ptr @pn_list_copy(ptr, i64)
declare void @pn_list_free(ptr)
declare void @pn_list_grow(ptr, i64) cold
declare void @pn_oob(i64, i64) noreturn cold
declare void @pn_divzero(i32) noreturn cold
declare void @pn_noreturn(i32) noreturn cold
declare void @pn_overflow(i32) noreturn cold
declare void @pn_float_to_int(i32) noreturn cold
declare {i64, i1} @llvm.sadd.with.overflow.i64(i64, i64)
declare {i64, i1} @llvm.ssub.with.overflow.i64(i64, i64)
declare {i64, i1} @llvm.smul.with.overflow.i64(i64, i64)
declare void @pn_map_missing(i32) noreturn cold
declare void @pn_print_int(i64)
declare void @pn_print_float(double)
declare void @pn_print_str(ptr)
declare ptr @pn_str_new(i64)
declare void @pn_str_free(ptr)
declare ptr @pn_str_dup(ptr)
declare ptr @pn_str_add_str(ptr, ptr)
declare ptr @pn_str_add_int(ptr, i64)
declare ptr @pn_str_add_float(ptr, double)
declare ptr @pn_str_from_int(i64)
declare ptr @pn_str_from_float(double)
declare i64 @pn_str_eq(ptr, ptr)
declare i64 @pn_str_cmp(ptr, ptr)
declare i64 @pn_str_to_int(ptr)
declare ptr @pn_substr(ptr, i64, i64, i64)
declare i64 @pn_strpos(ptr, ptr)
declare i64 @pn_str_contains(ptr, ptr)
declare i64 @pn_str_starts(ptr, ptr)
declare i64 @pn_str_ends(ptr, ptr)
declare ptr @pn_str_repeat(ptr, i64)
declare ptr @pn_str_case(ptr, i64)
declare ptr @pn_str_trim(ptr)
declare ptr @pn_map_new(i64, i64)
declare ptr @pn_map_slot(ptr, i64, i64)
declare i64 @pn_map_has(ptr, i64)
declare void @pn_map_del(ptr, i64)
declare i64 @pn_map_next(ptr, i64)
declare ptr @pn_map_entry(ptr, i64)
declare void @pn_map_free(ptr)
declare ptr @pn_map_copy(ptr)
declare ptr @pn_json_list(ptr, i64)
declare ptr @pn_json_map(ptr)
declare ptr @pn_json_str(ptr)
declare ptr @pn_json_float(double)
declare void @pn_http_status(i64)
declare void @pn_serve(i64, ptr)
declare double @llvm.sqrt.f64(double)
"""


# ----------------------------------------------------------------------------- codegen

class Module:
    def __init__(self):
        self.lits = {}
        self.funcs = {}
        self.handler = None

    def lit(self, s):
        """A string literal: a constant {len, -1, bytes} the runtime never frees or writes."""
        if s not in self.lits:
            self.lits[s] = f"@.L{len(self.lits)}"
        return self.lits[s]

    def header(self):
        h = ["; generated by pinac (prototype)", "%List = type { i64, i64, ptr }"]
        for s, name in self.lits.items():
            data = s.encode("utf-8") + b"\0"
            enc = "".join(chr(b) if 32 <= b < 127 and b not in (34, 92) else f"\\{b:02X}" for b in data)
            n = len(data)
            h.append(f'{name} = private unnamed_addr constant {{ i64, i64, [{n} x i8] }} '
                     f'{{ i64 {n - 1}, i64 -1, [{n} x i8] c"{enc}" }}, align 8')
        h.append(RUNTIME_DECLS)
        if self.handler:
            h.append('define ptr @pn_user_handler(ptr %m, ptr %p, ptr %b) {\n'
                     f'  %r = call ptr @"f.{self.handler}"(ptr %m, ptr %p, ptr %b)\n  ret ptr %r\n}}')
        return h


class Loop:
    def __init__(self, brk, cont):
        self.brk, self.cont = brk, cont
        self.breaks, self.conts = [], []    # Flow at each break / continue


class Flow:
    """What the checker knows at one point of a function: which variables surely hold a value, which
    were moved out (name -> line), and whether the point can be reached at all. Each branch works on
    its own copy; where paths meet the copies are merged, so a variable counts as assigned only if it
    is assigned on every path that gets there, and as moved if it was moved on any of them."""

    def __init__(self, assigned=(), moved=None, live=True, maybe=()):
        self.assigned, self.moved, self.live = set(assigned), dict(moved or {}), live
        self.maybe = set(maybe)            # moved on some paths only (for a clearer message)

    def copy(self):
        return Flow(self.assigned, self.moved, self.live, self.maybe)

    @staticmethod
    def merge(flows, fallback):
        live = [f for f in flows if f.live]
        if not live:                       # nothing reaches this point: keep checking dead code calmly
            r = fallback.copy()
            r.live = False
            return r
        r = live[0].copy()
        for f in live[1:]:
            r.assigned &= f.assigned
            r.maybe |= f.maybe
            for n, ln in f.moved.items():
                r.moved.setdefault(n, ln)
        r.maybe |= {n for n in r.moved if any(n not in f.moved for f in live)}
        return r


class FuncGen:
    def __init__(self, mod, name, params, ret, line, is_main=False):
        self.mod, self.name, self.ret, self.line, self.is_main = mod, name, ret, line, is_main
        self.params = params
        self.allocas, self.inits, self.body = [], [], []
        self.n = 0
        self.term = False
        self.vars = {}          # name -> type
        self.borrowed = set()   # heap vars this function does not own (parameters, foreach variables)
        self.flow = Flow()      # assigned / moved state at the current point (see Flow)
        self.iterating = []     # vars a foreach is currently walking over
        self.loops = []         # Loop: labels + the flows that leave by break / continue
        self.temps = []         # owned temporaries (value, type) freed at the end of the statement
        self.pending = []       # temporaries that must live until a foreach ends
        for ptype, pname in params:
            self.declare(pname, ptype, borrowed=is_heap(ptype))
            self.flow.assigned.add(pname)
            self.inits.append(f"  store {llty(ptype)} %arg.{pname[1:]}, ptr {self.slot(pname)}")

    @property
    def moved(self):
        return self.flow.moved

    # --- emission helpers
    def tmp(self):
        self.n += 1
        return f"%t{self.n}"

    def newlabel(self, hint):
        self.n += 1
        return f"{hint}{self.n}"

    def emit(self, ins):
        if self.term:
            self.place_label(self.newlabel("dead"))
        self.body.append("  " + ins)
        if ins.startswith(("br ", "ret", "unreachable")):
            self.term = True

    def place_label(self, name):
        if not self.term:
            self.body.append(f"  br label %{name}")
        self.body.append(f"{name}:")
        self.term = False
        self.label = name

    label = "body"

    def call(self, ret, fn, *args):
        r = None if ret == "void" else self.tmp()
        a = ", ".join(args)
        self.emit(f"{r} = call {ret} {fn}({a})" if r else f"call void {fn}({a})")
        return r

    @staticmethod
    def slot(name):
        return f"%v.{name[1:]}"

    def declare(self, name, t, borrowed=False):
        if name in self.vars:
            return
        self.vars[name] = t
        self.allocas.append(f"  {self.slot(name)} = alloca {llty(t)}")
        if borrowed:
            self.borrowed.add(name)
        elif t == "string":
            self.inits.append(f"  store ptr {self.mod.lit('')}, ptr {self.slot(name)}")
        elif is_heap(t):
            self.inits.append(f"  store ptr null, ptr {self.slot(name)}")

    def owned_vars(self):
        return [(n, t) for n, t in self.vars.items() if is_heap(t) and n not in self.borrowed]

    def free_owned(self, skip=()):
        for n, t in self.owned_vars():
            if n in skip:
                continue
            r = self.tmp()
            self.emit(f"{r} = load ptr, ptr {self.slot(n)}")
            self.call("void", free_fn(t), f"ptr {r}")

    def free_temps(self, temps):
        for v, t in temps:
            self.call("void", free_fn(t), f"ptr {v}")

    def flush_temps(self, since=0):
        self.free_temps(self.temps[since:])
        del self.temps[since:]

    def check_writable(self, name, line, what="changed"):
        if name in self.borrowed:
            kind = "a parameter" if any(p == name for _, p in self.params) else "a foreach variable"
            fix = f"$copy = {name};" if self.vars[name] == "string" else f"$copy = copy({name});"
            raise CompileError(f"{name} is {kind} lent to this code, so it cannot be {what}. "
                               f"Work on a copy instead: {fix}", line)

    # --- expressions: return (llvm value, type, owned)
    def require_assigned(self, name, line):
        if name not in self.vars:
            raise CompileError(f"variable {name} is used before it is assigned", line)
        if name not in self.flow.assigned and name not in self.moved:
            raise CompileError(
                f"{name} may be used before it is assigned: on some paths to this line it never gets a "
                f"value (an if without else, or a loop that can run zero times). Give {name} a value "
                f"before the if/loop", line)

    def use_var(self, name, line):
        self.require_assigned(name, line)
        if name in self.moved:
            how = "may have been moved" if name in self.flow.maybe else "was moved"
            where = " (on one path of an if or loop)" if name in self.flow.maybe else ""
            raise CompileError(
                f"{name} {how} on line {self.moved[name]}{where} and can no longer be used here. "
                f"Use copy({name}) if you need two separate values", line)
        t = self.vars[name]
        r = self.tmp()
        self.emit(f"{r} = load {llty(t)}, ptr {self.slot(name)}")
        return r, t

    def coerce(self, val, t, want, line, what):
        if t == want:
            return val
        if t == "int" and want == "float":
            r = self.tmp()
            self.emit(f"{r} = sitofp i64 {val} to double")
            return r
        raise CompileError(f"{what}: expected {want} but got {t}", line)

    def borrow(self, e, hint=None):
        """Evaluate an expression for reading only; owned results are freed after the statement."""
        v, t, owned = self.expr(e, hint)
        if owned:
            self.temps.append((v, t))
        return v, t

    def dup_str(self, v):
        if v.startswith("@.L"):        # literal: lives forever, no copy needed
            return v
        return self.call("ptr", "@pn_str_dup", f"ptr {v}")

    def owned_value(self, e, hint, line, may_move_string=False):
        """Evaluate an expression whose result will be stored somewhere: the result must be owned."""
        if e.kind == "var" and e.name in self.vars and is_heap(self.vars[e.name]):
            v, t = self.use_var(e.name, line)
            if t == "string":
                if may_move_string and e.name not in self.borrowed:
                    self.emit(f"store ptr {self.mod.lit('')}, ptr {self.slot(e.name)}")
                    return v, t
                return self.dup_str(v), t
            self.move_out(e.name, line)
            return v, t
        v, t, owned = self.expr(e, hint)
        if is_heap(t) and not owned:
            if t != "string":
                raise CompileError("internal: borrowed list/map cannot be stored", line)
            v = self.dup_str(v)
        return v, t

    def move_out(self, src, line):
        if src in self.borrowed:
            self.check_writable(src, line, "moved or returned")
        if src in self.iterating:
            raise CompileError(f"cannot move {src} while a foreach is looping over it", line)
        self.moved[src] = line
        self.emit(f"store ptr null, ptr {self.slot(src)}")

    def expr(self, e, hint=None):
        k = e.kind
        if k == "int":
            return str(e.val), "int", False
        if k == "float":
            bits = struct.unpack("<Q", struct.pack("<d", float(e.val)))[0]
            return f"0x{bits:016X}", "float", False
        if k == "bool":
            return ("true" if e.val else "false"), "bool", False
        if k == "str":
            return self.mod.lit(e.val), "string", False
        if k == "interp" or (k == "bin" and e.op == "."):
            return self.concat(flatten_concat(e), e.line)
        if k == "var":
            v, t = self.use_var(e.name, e.line)
            return v, t, False
        if k == "cast":
            return self.cast(e)
        if k == "unary":
            v, t = self.borrow(e.e)
            r = self.tmp()
            if e.op == "!":
                if t != "bool":
                    raise CompileError(f"'!' needs a bool but got {t}", e.line)
                self.emit(f"{r} = xor i1 {v}, true")
                return r, "bool", False
            if t == "int":
                return self.checked_int("-", "0", v, e.line), t, False
            elif t == "float":
                self.emit(f"{r} = fneg double {v}")
            else:
                raise CompileError(f"'-' needs a number but got {t}", e.line)
            return r, t, False
        if k == "bin":
            return self.binary(e)
        if k == "index":
            return self.index_read(e)
        if k == "coalesce":
            return self.coalesce(e)
        if k == "ternary":
            return self.ternary(e)
        if k == "append":
            raise CompileError("$a[] can only be assigned to", e.line)
        if k == "listlit":
            return self.listlit(e, hint)
        if k == "maplit":
            return self.maplit(e, hint)
        if k == "call":
            return self.call_expr(e, hint)
        raise CompileError(f"cannot use {k} here", e.line)

    # --- strings
    def concat(self, parts, line):
        est = sum(len(p.val.encode()) for p in parts if p.kind == "str") + 16 * sum(p.kind != "str" for p in parts)
        s = self.call("ptr", "@pn_str_new", f"i64 {est}")
        for p in parts:
            s = self.append_part(s, p, line)
        return s, "string", True

    def append_part(self, s, p, line):
        v, t = self.borrow(p)
        if t == "string":
            return self.call("ptr", "@pn_str_add_str", f"ptr {s}", f"ptr {v}")
        if t == "int":
            return self.call("ptr", "@pn_str_add_int", f"ptr {s}", f"i64 {v}")
        if t == "float":
            return self.call("ptr", "@pn_str_add_float", f"ptr {s}", f"double {v}")
        if t == "bool":
            return self.call("ptr", "@pn_str_add_str", f"ptr {s}", f"ptr {self.bool_str(v)}")
        raise CompileError(f"cannot join a {t} into a string (use json_encode())", line)

    def bool_str(self, v):
        r = self.tmp()
        self.emit(f"{r} = select i1 {v}, ptr {self.mod.lit('1')}, ptr {self.mod.lit('')}")
        return r

    def cast(self, e):
        v, t = self.borrow(e.e)
        if e.to == "float":
            return self.coerce(v, t, "float", e.line, "(float) cast"), "float", False
        if e.to == "string":
            return self.to_string(v, t, e.line)
        return self.to_int(v, t, e.line), "int", False

    def to_string(self, v, t, line):
        if t == "string":
            return v, t, False
        if t == "int":
            return self.call("ptr", "@pn_str_from_int", f"i64 {v}"), "string", True
        if t == "float":
            return self.call("ptr", "@pn_str_from_float", f"double {v}"), "string", True
        if t == "bool":
            return self.bool_str(v), "string", False
        raise CompileError(f"cannot turn a {t} into a string (use json_encode())", line)

    def to_int(self, v, t, line):
        if t == "int":
            return v
        r = self.tmp()
        if t == "float":
            # fptosi of NAN/INF/out-of-range is poison in LLVM: panic instead
            lo, hi, inr = self.tmp(), self.tmp(), self.tmp()
            self.emit(f"{lo} = fcmp oge double {v}, 0xC3E0000000000000")
            self.emit(f"{hi} = fcmp olt double {v}, 0x43E0000000000000")
            self.emit(f"{inr} = and i1 {lo}, {hi}")
            bad = self.tmp()
            self.emit(f"{bad} = xor i1 {inr}, true")
            self.panic_if(bad, "@pn_float_to_int", line)
            r = self.tmp()
            self.emit(f"{r} = fptosi double {v} to i64")
        elif t == "bool":
            self.emit(f"{r} = zext i1 {v} to i64")
        elif t == "string":
            return self.call("i64", "@pn_str_to_int", f"ptr {v}")
        else:
            raise CompileError(f"cannot turn a {t} into an int", line)
        return r

    def i64_to_bool(self, v):
        r = self.tmp()
        self.emit(f"{r} = icmp ne i64 {v}, 0")
        return r

    # --- lists
    def listlit(self, e, hint):
        if hint and is_map(hint) and not e.items:
            return self.maplit(Node("maplit", e.line, pairs=[]), hint)
        et = None
        vals = []
        if e.items:
            vals = [self.borrow(i) for i in e.items]
            types = {t for _, t in vals}
            et = "float" if types == {"int", "float"} else (types.pop() if len(types) == 1 else None)
            if et not in ELEM_SIZE:
                raise CompileError("list items must all be int, float or bool "
                                   f"(got {', '.join(sorted({t for _, t in vals}))})", e.line)
        elif hint and is_list(hint):
            et = elem_of(hint)
        else:
            raise CompileError("an empty [] needs a type, e.g.  list<int> $a = [];  or  map<string,int> $m = [];", e.line)
        r = self.call("ptr", "@pn_list_new", f"i64 {ELEM_SIZE[et]}")
        for v, t in vals:
            v = self.coerce(v, t, et, e.line, "list item")
            self.call("void", "@pn_list_push", f"ptr {r}", f"i64 {ELEM_SIZE[et]}", f"i64 {self.to_bits(v, et)}")
        return r, f"list<{et}>", True

    def to_bits(self, v, et):
        if et == "int":
            return v
        r = self.tmp()
        if et == "float":
            self.emit(f"{r} = bitcast double {v} to i64")
        else:
            self.emit(f"{r} = zext i1 {v} to i64")
        return r

    def list_bounds(self, lst, iv, line):
        ln, ok, bad = self.tmp(), self.newlabel("in"), self.newlabel("oob")
        c = self.tmp()
        self.emit(f"{ln} = load i64, ptr {lst}, !tbaa !1")
        self.emit(f"{c} = icmp ult i64 {iv}, {ln}")
        self.emit(f"br i1 {c}, label %{ok}, label %{bad}, !prof !10")
        self.place_label(bad)
        self.call("void", "@pn_oob", f"i64 {iv}", f"i64 {ln}")
        self.emit("unreachable")
        self.place_label(ok)

    def list_elem_ptr(self, lst, et, iv):
        dp, d, p = self.tmp(), self.tmp(), self.tmp()
        self.emit(f"{dp} = getelementptr inbounds %List, ptr {lst}, i32 0, i32 2")
        self.emit(f"{d} = load ptr, ptr {dp}, !tbaa !2")
        self.emit(f"{p} = getelementptr inbounds {ELEM_LL[et]}, ptr {d}, i64 {iv}")
        return p

    def load_elem(self, p, et):
        r = self.tmp()
        tb = {"int": "!3", "float": "!4", "bool": "!5"}[et]
        self.emit(f"{r} = load {ELEM_LL[et]}, ptr {p}, !tbaa {tb}")
        if et == "bool":
            b = self.tmp()
            self.emit(f"{b} = icmp ne i8 {r}, 0")
            return b
        return r

    def store_elem(self, p, v, et):
        tb = {"int": "!3", "float": "!4", "bool": "!5"}[et]
        if et == "bool":
            z = self.tmp()
            self.emit(f"{z} = zext i1 {v} to i8")
            v = z
        self.emit(f"store {ELEM_LL[et]} {v}, ptr {p}, !tbaa {tb}")

    # --- maps: values live in 8-byte slots
    def map_key(self, m_t, key_e, line):
        kt, _ = map_kv(m_t)
        kv, ktt = self.borrow(key_e)
        if ktt != kt:
            raise CompileError(f"map key must be {kt} but got {ktt}", line)
        if kt == "string":
            r = self.tmp()
            self.emit(f"{r} = ptrtoint ptr {kv} to i64")
            return r
        return kv

    def load_slot(self, p, vt):
        r = self.tmp()
        if vt == "bool":
            self.emit(f"{r} = load i64, ptr {p}")
            return self.i64_to_bool(r)
        self.emit(f"{r} = load {llty(vt)}, ptr {p}")
        return r

    def store_slot(self, p, v, vt):
        if vt == "bool":
            z = self.tmp()
            self.emit(f"{z} = zext i1 {v} to i64")
            v = z
        self.emit(f"store {llty(vt) if vt != 'bool' else 'i64'} {v}, ptr {p}")

    def maplit(self, e, hint):
        if e.pairs:
            kt, vt = None, None
            if hint and is_map(hint):
                kt, vt = map_kv(hint)
            first_k = self.expr_type_probe(e.pairs[0][0]) if kt is None else kt
            mt = None
            entries = []
            for ke, ve in e.pairs:
                entries.append((ke, ve))
            if kt is None:
                kt = first_k
            if kt not in ("int", "string"):
                raise CompileError(f"map keys must be int or string, not {kt}", e.line)
            m = None
            for i, (ke, ve) in enumerate(entries):
                v, t = self.owned_value(ve, vt, e.line)
                if vt is None:
                    vt = t
                    if vt not in KIND:
                        raise CompileError(f"map values must be int, float, bool or string, not {vt}", e.line)
                    mt = f"map<{kt},{vt}>"
                    m = self.call("ptr", "@pn_map_new", f"i64 {KIND[kt]}", f"i64 {KIND[vt]}")
                elif m is None:
                    mt = f"map<{kt},{vt}>"
                    m = self.call("ptr", "@pn_map_new", f"i64 {KIND[kt]}", f"i64 {KIND[vt]}")
                v = self.coerce(v, t, vt, e.line, "map value")
                key = self.map_key(mt, ke, e.line)
                p = self.call("ptr", "@pn_map_slot", f"ptr {m}", f"i64 {key}", "i64 1")
                if vt == "string":
                    old = self.load_slot(p, vt)
                    self.call("void", "@pn_str_free", f"ptr {old}")
                self.store_slot(p, v, vt)
            return m, mt, True
        if not (hint and is_map(hint)):
            raise CompileError("an empty [] needs a type, e.g.  map<string,int> $m = [];", e.line)
        kt, vt = map_kv(hint)
        return self.call("ptr", "@pn_map_new", f"i64 {KIND[kt]}", f"i64 {KIND[vt]}"), hint, True

    def expr_type_probe(self, e):
        """Type of a simple key expression without generating code twice."""
        if e.kind in ("str", "interp") or (e.kind == "bin" and e.op == "."):
            return "string"
        if e.kind == "int":
            return "int"
        if e.kind == "float":
            return "float"
        if e.kind == "bool":
            return "bool"
        if e.kind == "bin" and e.op in ("+", "-", "*", "%"):
            l, r = self.expr_type_probe(e.l), self.expr_type_probe(e.r)
            return "float" if "float" in (l, r) else l
        if e.kind == "bin" and e.op == "/":
            return "float"
        if e.kind == "unary" and e.op == "-":
            return self.expr_type_probe(e.e)
        if e.kind == "var" and e.name in self.vars:
            return self.vars[e.name]
        if e.kind == "call" and e.name in self.mod.funcs:
            return self.mod.funcs[e.name][1]
        if e.kind == "call" and e.name in ("strval", "substr", "trim", "strtolower", "strtoupper", "json_encode"):
            return "string"
        return "int"

    def seq_of(self, e, line):
        if e.kind not in ("var", "call"):
            raise CompileError("can only index a variable or a function result", line)
        return self.borrow(e)

    def index_read(self, e):
        seq, st = self.seq_of(e.seq, e.line)
        if is_list(st):
            iv, it = self.borrow(e.idx)
            if it != "int":
                raise CompileError(f"list index must be int but got {it}", e.line)
            self.list_bounds(seq, iv, e.line)
            et = elem_of(st)
            return self.load_elem(self.list_elem_ptr(seq, et, iv), et), et, False
        if is_map(st):
            _, vt = map_kv(st)
            key = self.map_key(st, e.idx, e.line)
            p = self.call("ptr", "@pn_map_slot", f"ptr {seq}", f"i64 {key}", "i64 0")
            c, ok, bad = self.tmp(), self.newlabel("found"), self.newlabel("missing")
            self.emit(f"{c} = icmp ne ptr {p}, null")
            self.emit(f"br i1 {c}, label %{ok}, label %{bad}, !prof !10")
            self.place_label(bad)
            self.call("void", "@pn_map_missing", f"i32 {e.line}")
            self.emit("unreachable")
            self.place_label(ok)
            return self.load_slot(p, vt), vt, False
        if st == "string":
            raise CompileError("use substr($s, $i, 1) to read one character", e.line)
        raise CompileError(f"cannot index a value of type {st}", e.line)

    def coalesce(self, e):
        """$m[$k] ?? default   /   $list[$i] ?? default"""
        if e.l.kind != "index":
            raise CompileError("?? needs a list item or map entry on the left, e.g. $m[$k] ?? 0", e.line)
        seq, st = self.seq_of(e.l.seq, e.line)
        has, dflt, merge = self.newlabel("has"), self.newlabel("dflt"), self.newlabel("coal")
        if is_map(st):
            _, vt = map_kv(st)
            key = self.map_key(st, e.l.idx, e.line)
            p = self.call("ptr", "@pn_map_slot", f"ptr {seq}", f"i64 {key}", "i64 0")
            c = self.tmp()
            self.emit(f"{c} = icmp ne ptr {p}, null")
        elif is_list(st):
            vt = elem_of(st)
            iv, it = self.borrow(e.l.idx)
            if it != "int":
                raise CompileError(f"list index must be int but got {it}", e.line)
            ln, c = self.tmp(), self.tmp()
            self.emit(f"{ln} = load i64, ptr {seq}, !tbaa !1")
            self.emit(f"{c} = icmp ult i64 {iv}, {ln}")
        else:
            raise CompileError(f"?? needs a list or map, not {st}", e.line)
        self.emit(f"br i1 {c}, label %{has}, label %{dflt}")
        self.place_label(has)
        v1 = self.load_slot(p, vt) if is_map(st) else self.load_elem(self.list_elem_ptr(seq, vt, iv), vt)
        if vt == "string":
            v1 = self.dup_str(v1)      # result is owned on both paths
        end1 = self.label
        self.emit(f"br label %{merge}")
        self.place_label(dflt)
        mark = len(self.temps)
        dv, dt, downed = self.expr(e.r, vt)
        dv = self.coerce(dv, dt, vt, e.line, "?? default")
        if vt == "string" and not downed:
            dv = self.dup_str(dv)
        self.flush_temps(mark)         # temporaries made only on this path die on this path
        end2 = self.label
        self.emit(f"br label %{merge}")
        self.place_label(merge)
        r = self.tmp()
        self.emit(f"{r} = phi {llty(vt)} [ {v1}, %{end1} ], [ {dv}, %{end2} ]")
        return r, vt, vt == "string"

    def branch_value(self, e, want, line):
        """Evaluate one arm of ?: / ?? inside its own block; strings come out owned."""
        mark = len(self.temps)
        v, t, owned = self.expr(e, want)
        if want is not None:
            v = self.coerce(v, t, want, line, "value")
            t = want
        if t == "string" and not owned:
            v = self.dup_str(v)
        elif is_heap(t) and not owned:
            raise CompileError("?: cannot hand out a borrowed list or map; use copy()", line)
        self.flush_temps(mark)
        return v, t

    def ternary(self, e):
        c = self.cond_value(e.c)
        a_l, b_l, merge = self.newlabel("ta"), self.newlabel("tb"), self.newlabel("tend")
        self.emit(f"br i1 {c}, label %{a_l}, label %{b_l}")
        self.place_label(a_l)
        # numbers: int and float mix to float; probe both arm types first
        ta, tb = self.expr_type_probe(e.a), self.expr_type_probe(e.b)
        want = "float" if {ta, tb} == {"int", "float"} else None
        va, ta = self.branch_value(e.a, want, e.line)
        end_a = self.label
        self.emit(f"br label %{merge}")
        self.place_label(b_l)
        vb, tb = self.branch_value(e.b, want or ta, e.line)
        end_b = self.label
        self.emit(f"br label %{merge}")
        self.place_label(merge)
        if ta != tb:
            raise CompileError(f"both sides of ?: must have the same type (got {ta} and {tb})", e.line)
        if ta == "void":
            raise CompileError("?: arms cannot be void", e.line)
        r = self.tmp()
        self.emit(f"{r} = phi {llty(ta)} [ {va}, %{end_a} ], [ {vb}, %{end_b} ]")
        return r, ta, is_heap(ta)

    def cond_value(self, e):
        mark = len(self.temps)
        v, t = self.borrow(e)
        if t != "bool":
            raise CompileError(f"condition must be bool but got {t}", e.line)
        self.flush_temps(mark)
        return v

    # --- operators
    def binary(self, e):
        op = e.op
        if op in ("&&", "||"):
            lv, lt = self.borrow(e.l)
            if lt != "bool":
                raise CompileError(f"'{op}' needs bool values but got {lt}", e.line)
            start = self.label
            rhs, end = self.newlabel("rhs"), self.newlabel("sc")
            if op == "&&":
                self.emit(f"br i1 {lv}, label %{rhs}, label %{end}")
            else:
                self.emit(f"br i1 {lv}, label %{end}, label %{rhs}")
            self.place_label(rhs)
            mark = len(self.temps)
            rv, rt = self.borrow(e.r)
            if rt != "bool":
                raise CompileError(f"'{op}' needs bool values but got {rt}", e.line)
            self.flush_temps(mark)
            rend = self.label
            self.place_label(end)
            r = self.tmp()
            short = "false" if op == "&&" else "true"
            self.emit(f"{r} = phi i1 [ {short}, %{start} ], [ {rv}, %{rend} ]")
            return r, "bool", False
        lv, lt = self.borrow(e.l)
        rv, rt = self.borrow(e.r)
        if lt == "string" or rt == "string":
            if lt != rt:
                raise CompileError(f"'{op}' cannot combine {lt} and {rt}"
                                   + (" (use . to join strings)" if op == "+" else ""), e.line)
            if op in ("==", "!="):
                r = self.call("i64", "@pn_str_eq", f"ptr {lv}", f"ptr {rv}")
                b = self.tmp()
                self.emit(f"{b} = icmp {'ne' if op == '==' else 'eq'} i64 {r}, 0")
                return b, "bool", False
            if op in ("<", "<=", ">", ">="):
                r = self.call("i64", "@pn_str_cmp", f"ptr {lv}", f"ptr {rv}")
                b = self.tmp()
                cc = {"<": "slt", "<=": "sle", ">": "sgt", ">=": "sge"}[op]
                self.emit(f"{b} = icmp {cc} i64 {r}, 0")
                return b, "bool", False
            raise CompileError(f"'{op}' does not work on strings" + (" (use . to join strings)" if op == "+" else ""), e.line)
        if lt == "bool" and rt == "bool" and op in ("==", "!="):
            r = self.tmp()
            self.emit(f"{r} = icmp {'eq' if op == '==' else 'ne'} i1 {lv}, {rv}")
            return r, "bool", False
        if lt not in ("int", "float") or rt not in ("int", "float"):
            raise CompileError(f"'{op}' cannot combine {lt} and {rt}", e.line)
        if op == "%div" and (lt, rt) != ("int", "int"):
            raise CompileError(f"intdiv() needs two ints but got {lt} and {rt}", e.line)
        t = "float" if "float" in (lt, rt) or op == "/" else "int"   # PHP: / always gives a float
        lv = self.coerce(lv, lt, t, e.line, op)
        rv = self.coerce(rv, rt, t, e.line, op)
        r = self.tmp()
        if op in ("<", "<=", ">", ">=", "==", "!="):
            if t == "int":
                cc = {"<": "slt", "<=": "sle", ">": "sgt", ">=": "sge", "==": "eq", "!=": "ne"}[op]
                self.emit(f"{r} = icmp {cc} i64 {lv}, {rv}")
            else:
                cc = {"<": "olt", "<=": "ole", ">": "ogt", ">=": "oge", "==": "oeq", "!=": "une"}[op]
                self.emit(f"{r} = fcmp {cc} double {lv}, {rv}")
            return r, "bool", False
        if t == "int":
            if op in ("+", "-", "*"):
                return self.checked_int(op, lv, rv, e.line), t, False
            z = self.tmp()
            self.emit(f"{z} = icmp eq i64 {rv}, 0")
            self.panic_if(z, "@pn_divzero", e.line)
            m1 = self.tmp()
            self.emit(f"{m1} = icmp eq i64 {rv}, -1")
            if op == "%":
                # x % -1 is 0, but srem INT_MIN, -1 is undefined in LLVM: divide by 1 instead
                d = self.tmp()
                self.emit(f"{d} = select i1 {m1}, i64 1, i64 {rv}")
                self.emit(f"{r} = srem i64 {lv}, {d}")
            else:
                mn, bad = self.tmp(), self.tmp()
                self.emit(f"{mn} = icmp eq i64 {lv}, -9223372036854775808")
                self.emit(f"{bad} = and i1 {mn}, {m1}")
                self.panic_if(bad, "@pn_overflow", e.line)
                self.emit(f"{r} = sdiv i64 {lv}, {rv}")
        else:
            if op == "%":
                raise CompileError("'%' works on int only", e.line)
            ins = {"+": "fadd", "-": "fsub", "*": "fmul", "/": "fdiv"}[op]
            self.emit(f"{r} = {ins} double {lv}, {rv}")
        return r, t, False

    # --- calls
    def argc(self, e, lo, hi=None):
        hi = lo if hi is None else hi
        if not lo <= len(e.args) <= hi:
            want = str(lo) if lo == hi else f"{lo} or {hi}"
            raise CompileError(f"{e.name}() takes {want} argument(s) but got {len(e.args)}", e.line)

    def str_arg(self, e, i):
        v, t = self.borrow(e.args[i])
        if t != "string":
            raise CompileError(f"{e.name}() argument {i + 1} must be a string but got {t}", e.line)
        return v

    def panic_if(self, cond, fn, line):
        ok, bad = self.newlabel("ok"), self.newlabel("panic")
        self.emit(f"br i1 {cond}, label %{bad}, label %{ok}, !prof !11")
        self.place_label(bad)
        self.call("void", fn, f"i32 {line}")
        self.emit("unreachable")
        self.place_label(ok)

    def checked_int(self, op, lv, rv, line):
        """int + - * that panics on overflow instead of wrapping around."""
        fn = {"+": "sadd", "-": "ssub", "*": "smul"}[op]
        pair, r, o = self.tmp(), self.tmp(), self.tmp()
        self.emit(f"{pair} = call {{i64, i1}} @llvm.{fn}.with.overflow.i64(i64 {lv}, i64 {rv})")
        self.emit(f"{r} = extractvalue {{i64, i1}} {pair}, 0")
        self.emit(f"{o} = extractvalue {{i64, i1}} {pair}, 1")
        self.panic_if(o, "@pn_overflow", line)
        return r

    def int_arg(self, e, i):
        v, t = self.borrow(e.args[i])
        if t != "int":
            raise CompileError(f"{e.name}() argument {i + 1} must be an int but got {t}", e.line)
        return v

    def call_expr(self, e, hint):
        name, args = e.name, e.args
        if name in BUILTINS:
            return self.builtin(e, hint)
        if name not in self.mod.funcs:
            raise CompileError(f"unknown function {name}()", e.line)
        ptypes, ret = self.mod.funcs[name]
        if len(args) != len(ptypes):
            raise CompileError(f"{name}() takes {len(ptypes)} argument(s) but got {len(args)}", e.line)
        vals = []
        for a, pt in zip(args, ptypes):
            v, t = self.borrow(a, pt)     # heap values are lent to the callee
            vals.append(f"{llty(pt)} {self.coerce(v, t, pt, e.line, f'argument of {name}()')}")
        r = self.call(llty(ret), f'@"f.{name}"', *vals)
        return r, ret, is_heap(ret)

    def builtin(self, e, hint):
        name, args = e.name, e.args
        if name == "count":
            self.argc(e, 1)
            v, t = self.borrow(args[0])
            if not (is_list(t) or is_map(t)):
                raise CompileError(f"count() needs a list or map but got {t}", e.line)
            r = self.tmp()
            self.emit(f"{r} = load i64, ptr {v}" + (", !tbaa !1" if is_list(t) else ""))
            return r, "int", False
        if name == "strlen":
            self.argc(e, 1)
            v = self.str_arg(e, 0)
            r = self.tmp()
            self.emit(f"{r} = load i64, ptr {v}")
            return r, "int", False
        if name == "sqrt":
            self.argc(e, 1)
            v, t = self.borrow(args[0])
            v = self.coerce(v, t, "float", e.line, "sqrt()")
            return self.call("double", "@llvm.sqrt.f64", f"double {v}"), "float", False
        if name in ("wrapping_add", "wrapping_sub", "wrapping_mul"):
            self.argc(e, 2)
            a, b = self.int_arg(e, 0), self.int_arg(e, 1)
            r = self.tmp()
            self.emit(f"{r} = {name[9:]} i64 {a}, {b}")      # plain add/sub/mul wrap around in LLVM
            return r, "int", False
        if name == "intdiv":
            self.argc(e, 2)
            return self.binary(Node("bin", e.line, op="%div", l=args[0], r=args[1]))
        if name == "fill":
            self.argc(e, 2)
            n = self.int_arg(e, 0)
            v, vt = self.borrow(args[1])
            if hint and is_list(hint) and elem_of(hint) == "float":
                v, vt = self.coerce(v, vt, "float", e.line, "fill()"), "float"
            if vt not in ELEM_SIZE:
                raise CompileError(f"fill() value must be int, float or bool but got {vt}", e.line)
            r = self.call("ptr", "@pn_list_fill", f"i64 {n}", f"i64 {ELEM_SIZE[vt]}", f"i64 {self.to_bits(v, vt)}")
            return r, f"list<{vt}>", True
        if name == "copy":
            self.argc(e, 1)
            v, t = self.borrow(args[0])
            if is_list(t):
                return self.call("ptr", "@pn_list_copy", f"ptr {v}", f"i64 {ELEM_SIZE[elem_of(t)]}"), t, True
            if is_map(t):
                return self.call("ptr", "@pn_map_copy", f"ptr {v}"), t, True
            if t == "string":
                return self.dup_str(v), t, not v.startswith("@.L")
            raise CompileError(f"copy() needs a list, map or string but got {t}", e.line)
        if name in ("strval",):
            self.argc(e, 1)
            v, t = self.borrow(args[0])
            return self.to_string(v, t, e.line)
        if name == "intval":
            self.argc(e, 1)
            v, t = self.borrow(args[0])
            return self.to_int(v, t, e.line), "int", False
        if name == "substr":
            self.argc(e, 2, 3)
            s = self.str_arg(e, 0)
            start = self.int_arg(e, 1)
            ln = self.int_arg(e, 2) if len(args) == 3 else "0"
            r = self.call("ptr", "@pn_substr", f"ptr {s}", f"i64 {start}", f"i64 {ln}", f"i64 {int(len(args) == 3)}")
            return r, "string", True
        if name == "strpos":
            self.argc(e, 2)
            h, n = self.str_arg(e, 0), self.str_arg(e, 1)
            return self.call("i64", "@pn_strpos", f"ptr {h}", f"ptr {n}"), "int", False
        if name in ("str_contains", "str_starts_with", "str_ends_with"):
            self.argc(e, 2)
            h, n = self.str_arg(e, 0), self.str_arg(e, 1)
            fn = {"str_contains": "@pn_str_contains", "str_starts_with": "@pn_str_starts",
                  "str_ends_with": "@pn_str_ends"}[name]
            return self.i64_to_bool(self.call("i64", fn, f"ptr {h}", f"ptr {n}")), "bool", False
        if name == "str_repeat":
            self.argc(e, 2)
            s, n = self.str_arg(e, 0), self.int_arg(e, 1)
            return self.call("ptr", "@pn_str_repeat", f"ptr {s}", f"i64 {n}"), "string", True
        if name in ("strtolower", "strtoupper"):
            self.argc(e, 1)
            s = self.str_arg(e, 0)
            return self.call("ptr", "@pn_str_case", f"ptr {s}", f"i64 {int(name == 'strtoupper')}"), "string", True
        if name == "trim":
            self.argc(e, 1)
            return self.call("ptr", "@pn_str_trim", f"ptr {self.str_arg(e, 0)}"), "string", True
        if name == "json_encode":
            self.argc(e, 1)
            v, t = self.borrow(args[0])
            if is_list(t):
                return self.call("ptr", "@pn_json_list", f"ptr {v}", f"i64 {KIND[elem_of(t)]}"), "string", True
            if is_map(t):
                return self.call("ptr", "@pn_json_map", f"ptr {v}"), "string", True
            if t == "string":
                return self.call("ptr", "@pn_json_str", f"ptr {v}"), "string", True
            if t == "int":
                return self.call("ptr", "@pn_str_from_int", f"i64 {v}"), "string", True
            if t == "float":
                return self.call("ptr", "@pn_json_float", f"double {v}"), "string", True
            if t == "bool":
                r = self.tmp()
                self.emit(f"{r} = select i1 {v}, ptr {self.mod.lit('true')}, ptr {self.mod.lit('false')}")
                return r, "string", False
        if name in ("isset", "array_key_exists"):
            if name == "isset":
                self.argc(e, 1)
                if args[0].kind != "index":
                    raise CompileError("isset() needs a list item or map entry, e.g. isset($m[$k])", e.line)
                seq_e, key_e = args[0].seq, args[0].idx
            else:
                self.argc(e, 2)
                key_e, seq_e = args
            seq, st = self.seq_of(seq_e, e.line)
            if is_map(st):
                key = self.map_key(st, key_e, e.line)
                return self.i64_to_bool(self.call("i64", "@pn_map_has", f"ptr {seq}", f"i64 {key}")), "bool", False
            if is_list(st):
                iv, it = self.borrow(key_e)
                if it != "int":
                    raise CompileError(f"list index must be int but got {it}", e.line)
                ln, c = self.tmp(), self.tmp()
                self.emit(f"{ln} = load i64, ptr {seq}, !tbaa !1")
                self.emit(f"{c} = icmp ult i64 {iv}, {ln}")
                return c, "bool", False
            raise CompileError(f"{name}() needs a list or map but got {st}", e.line)
        if name == "unset":
            self.argc(e, 1)
            a = args[0]
            if a.kind != "index" or a.seq.kind != "var":
                raise CompileError("unset() needs a map entry, e.g. unset($m[$k])", e.line)
            mname = a.seq.name
            m, mt = self.use_var(mname, e.line)
            if not is_map(mt):
                raise CompileError(f"unset() works on maps; {mname} is {mt}", e.line)
            self.check_writable(mname, e.line)
            if mname in self.iterating:
                raise CompileError(f"cannot change {mname} while a foreach is looping over it", e.line)
            key = self.map_key(mt, a.idx, e.line)
            self.call("void", "@pn_map_del", f"ptr {m}", f"i64 {key}")
            return None, "void", False
        if name == "http_status":
            self.argc(e, 1)
            self.call("void", "@pn_http_status", f"i64 {self.int_arg(e, 0)}")
            return None, "void", False
        if name == "serve":
            self.argc(e, 2)
            port = self.int_arg(e, 0)
            h = args[1]
            sig = (["string", "string", "string"], "string")
            if h.kind != "str" or h.val not in self.mod.funcs or self.mod.funcs[h.val] != sig:
                raise CompileError('serve($port, "handler") needs the name of a function declared as '
                                   'handler(string $method, string $path, string $body): string', e.line)
            if self.mod.handler and self.mod.handler != h.val:
                raise CompileError("only one serve() handler per program", e.line)
            self.mod.handler = h.val
            self.call("void", "@pn_serve", f"i64 {port}", "ptr @pn_user_handler")
            return None, "void", False
        raise CompileError(f"{name}() cannot be used with these arguments", e.line)

    # --- statements
    def stmt(self, s):
        k = s.kind
        if k == "block":
            for x in s.body:
                self.stmt(x)
            return
        if k == "decl":
            if s.name in self.vars and self.vars[s.name] != s.vtype:
                raise CompileError(f"{s.name} is already {self.vars[s.name]}; a variable keeps one type", s.line)
            self.declare(s.name, s.vtype)
            self.assign_var(s.name, s.val, s.line)
        elif k == "assign":
            self.assign(s)
        elif k == "exprstmt":
            v, t, owned = self.expr(s.expr)
            if owned:
                self.temps.append((v, t))
        elif k == "echo":
            for a in s.args:
                for p in flatten_concat(a):
                    v, t = self.borrow(p)
                    if t == "int":
                        self.call("void", "@pn_print_int", f"i64 {v}")
                    elif t == "float":
                        self.call("void", "@pn_print_float", f"double {v}")
                    elif t == "string":
                        self.call("void", "@pn_print_str", f"ptr {v}")
                    elif t == "bool":
                        self.call("void", "@pn_print_str", f"ptr {self.bool_str(v)}")
                    else:
                        raise CompileError(f"cannot echo a {t} (use json_encode())", s.line)
        elif k == "if":
            self.if_(s)
        elif k == "while":
            self.loop(s.cond, None, s.body)
        elif k == "for":
            if s.init:
                self.stmt(s.init)
            self.loop(s.cond, s.step, s.body)
        elif k == "foreach":
            self.foreach(s)
        elif k in ("break", "continue"):
            if not self.loops:
                raise CompileError(f"'{k}' outside a loop", s.line)
            self.flush_temps()
            lp = self.loops[-1]
            (lp.breaks if k == "break" else lp.conts).append(self.flow.copy())
            self.flow.live = False
            self.emit(f"br label %{lp.brk if k == 'break' else lp.cont}")
        elif k == "return":
            self.return_(s)
        else:
            raise CompileError(f"unknown statement {k}", s.line)
        self.flush_temps()

    def assign_var(self, name, val_e, line):
        if name in self.borrowed:
            self.check_writable(name, line)
        t = self.vars.get(name)
        if t and is_heap(t) and name in self.iterating:
            raise CompileError(f"cannot replace {name} while a foreach is looping over it", line)
        if t is None and val_e.kind == "call" and val_e.name in self.mod.funcs and is_heap(self.mod.funcs[val_e.name][1]):
            # a first assignment inside a loop still runs many times: declare now so the
            # previous round's value can be freed before the new one is built
            t = self.mod.funcs[val_e.name][1]
            self.declare(name, t)
        early = False
        if t and is_heap(t) and not mentions(val_e, name):
            # ownership tells us the old value is dead right now: free it before building the new one
            self.free_var(name, t)
            early = True
        v, vt = self.owned_value(val_e, t, line)
        if t is None:
            if vt == "void":
                raise CompileError("cannot assign the result of a void function", line)
            self.declare(name, vt)
            t = vt
        v = self.coerce(v, vt, t, line, f"assigning to {name}")
        if is_heap(t):
            if not early:
                old = self.tmp()
                self.emit(f"{old} = load ptr, ptr {self.slot(name)}")
                self.call("void", free_fn(t), f"ptr {old}")
            self.moved.pop(name, None)
            self.flow.maybe.discard(name)
        self.emit(f"store {llty(t)} {v}, ptr {self.slot(name)}")
        self.flow.assigned.add(name)

    def free_var(self, name, t):
        old = self.tmp()
        self.emit(f"{old} = load ptr, ptr {self.slot(name)}")
        self.call("void", free_fn(t), f"ptr {old}")
        empty = self.mod.lit("") if t == "string" else "null"
        self.emit(f"store ptr {empty}, ptr {self.slot(name)}")

    def assign(self, s):
        tg, op = s.target, s.op
        if tg.kind == "var":
            name = tg.name
            if op == "=":
                return self.assign_var(name, s.val, s.line)
            self.require_assigned(name, s.line)
            vt = self.vars[name]
            if op == ".=":
                if vt != "string":
                    raise CompileError(f"'.=' needs a string variable; {name} is {vt}", s.line)
                self.check_writable(name, s.line)
                if mentions(s.val, name):
                    rv, _ = self.owned_value(s.val, "string", s.line)
                    self.temps.append((rv, "string"))
                    cur, _ = self.use_var(name, s.line)
                    cur = self.call("ptr", "@pn_str_add_str", f"ptr {cur}", f"ptr {rv}")
                else:
                    cur, _ = self.use_var(name, s.line)
                    for p in flatten_concat(s.val):
                        cur = self.append_part(cur, p, s.line)
                self.emit(f"store ptr {cur}, ptr {self.slot(name)}")
                return
            if is_heap(vt):
                raise CompileError(f"'{op}' does not work on {vt}", s.line)
            bin_e = Node("bin", s.line, op=op[0], l=tg, r=s.val)
            v, t = self.borrow(bin_e)
            v = self.coerce(v, t, vt, s.line, f"assigning to {name}")
            self.emit(f"store {llty(vt)} {v}, ptr {self.slot(name)}")
            return
        if tg.seq.kind != "var":
            raise CompileError("can only assign into a list or map variable", s.line)
        name = tg.seq.name
        self.require_assigned(name, s.line)
        if name in self.moved:
            self.use_var(name, s.line)          # reports the move
        st = self.vars[name]
        self.check_writable(name, s.line)
        if tg.kind == "append":
            if name in self.iterating:
                raise CompileError(f"cannot append to {name} while a foreach is looping over it", s.line)
            if not is_list(st):
                raise CompileError(f"{name} is {st}; $a[] = value appends to lists only", s.line)
            et = elem_of(st)
            v, t = self.borrow(s.val)
            v = self.coerce(v, t, et, s.line, f"appending to {name}")
            lst, _ = self.use_var(name, s.line)
            ln, cp, capp, full = self.tmp(), self.tmp(), self.tmp(), self.tmp()
            grow, fast = self.newlabel("grow"), self.newlabel("push")
            self.emit(f"{ln} = load i64, ptr {lst}, !tbaa !1")
            self.emit(f"{capp} = getelementptr inbounds %List, ptr {lst}, i32 0, i32 1")
            self.emit(f"{cp} = load i64, ptr {capp}, !tbaa !13")
            self.emit(f"{full} = icmp eq i64 {ln}, {cp}")
            self.emit(f"br i1 {full}, label %{grow}, label %{fast}, !prof !11")
            self.place_label(grow)
            self.call("void", "@pn_list_grow", f"ptr {lst}", f"i64 {ELEM_SIZE[et]}")
            self.place_label(fast)
            self.store_elem(self.list_elem_ptr(lst, et, ln), v, et)
            n2 = self.tmp()
            self.emit(f"{n2} = add i64 {ln}, 1")
            self.emit(f"store i64 {n2}, ptr {lst}, !tbaa !1")
            return
        # $x[$i] op= value — the value is computed first, so no pointer into the container is held
        # while other code runs
        if is_list(st):
            et = elem_of(st)
            if op == ".=":
                raise CompileError("'.=' needs a string", s.line)
            v, t = self.borrow(s.val)
            lst, _ = self.use_var(name, s.line)
            iv, it = self.borrow(tg.idx)
            if it != "int":
                raise CompileError(f"list index must be int but got {it}", s.line)
            self.list_bounds(lst, iv, s.line)
            p = self.list_elem_ptr(lst, et, iv)
            if op != "=":
                v, t = self.arith(self.load_elem(p, et), et, v, t, op, s.line)
            self.store_elem(p, self.coerce(v, t, et, s.line, f"storing into {name}"), et)
            return
        if is_map(st):
            if name in self.iterating:
                raise CompileError(f"cannot change {name} while a foreach is looping over it", s.line)
            _, vt = map_kv(st)
            if op == "=":
                v, t = self.owned_value(s.val, vt, s.line)
            else:
                v, t = self.borrow(s.val)
            m, _ = self.use_var(name, s.line)
            key = self.map_key(st, tg.idx, s.line)
            p = self.call("ptr", "@pn_map_slot", f"ptr {m}", f"i64 {key}", "i64 1")
            if op == "=":
                v = self.coerce(v, t, vt, s.line, f"storing into {name}")
                if vt == "string":
                    old = self.load_slot(p, vt)
                    self.call("void", "@pn_str_free", f"ptr {old}")
                self.store_slot(p, v, vt)
            elif op == ".=":
                if vt != "string" or t != "string":
                    raise CompileError("'.=' needs string values", s.line)
                cur = self.load_slot(p, vt)
                self.store_slot(p, self.call("ptr", "@pn_str_add_str", f"ptr {cur}", f"ptr {v}"), vt)
            else:
                if vt not in ("int", "float"):
                    raise CompileError(f"'{op}' needs numbers", s.line)
                v, t = self.arith(self.load_slot(p, vt), vt, v, t, op, s.line)   # missing key counts as 0
                self.store_slot(p, self.coerce(v, t, vt, s.line, f"storing into {name}"), vt)
            return
        raise CompileError(f"cannot assign into a {st}", s.line)

    def arith(self, a, at, b, bt, op, line):
        if at not in ("int", "float") or bt not in ("int", "float"):
            raise CompileError(f"'{op}' needs numbers", line)
        if op[0] in "/%":
            raise CompileError(f"'{op}' on list/map items is not supported yet", line)
        tt = "float" if "float" in (at, bt) else "int"
        a = self.coerce(a, at, tt, line, op)
        b = self.coerce(b, bt, tt, line, op)
        r = self.tmp()
        ins = {"+": "add", "-": "sub", "*": "mul"}[op[0]]
        self.emit(f"{r} = {'f' if tt == 'float' else ''}{ins} {llty(tt)} {a}, {b}")
        return r, tt

    def cond(self, e):
        v, t = self.borrow(e)
        if t != "bool":
            raise CompileError(f"condition must be bool but got {t} (write the comparison, e.g. $x != 0)", e.line)
        self.flush_temps()
        return v

    def if_(self, s):
        v = self.cond(s.cond)
        then, els, end = self.newlabel("then"), self.newlabel("else"), self.newlabel("endif")
        self.emit(f"br i1 {v}, label %{then}, label %{els}")
        start = self.flow
        self.flow = start.copy()
        self.place_label(then)
        self.stmt(s.then)
        then_flow = self.flow
        if not self.term:
            self.emit(f"br label %{end}")
        self.flow = start.copy()
        self.place_label(els)
        if s.els:
            self.stmt(s.els)
        self.place_label(end)
        self.flow = Flow.merge([then_flow, self.flow], start)

    def loop(self, cond, step, body):
        head, bodyl, stepl, end = (self.newlabel(x) for x in ("loop", "body", "step", "endloop"))
        head_flow = self.flow.copy()
        self.place_label(head)
        if cond is not None:
            v = self.cond(cond)
            self.emit(f"br i1 {v}, label %{bodyl}, label %{end}")
        self.place_label(bodyl)
        lp = Loop(end, stepl)
        self.loops.append(lp)
        self.stmt(body)
        self.loops.pop()
        self.flow = Flow.merge([self.flow] + lp.conts, head_flow)
        self.place_label(stepl)
        if step:
            self.stmt(step)
        self.check_back_edge(head_flow)
        self.emit(f"br label %{head}")
        self.place_label(end)
        forever = cond is None or (cond.kind == "bool" and cond.val)
        self.flow = Flow.merge(lp.breaks + ([] if forever else [head_flow]), head_flow)

    def check_back_edge(self, head_flow):
        """The state going round again must be no worse than the state the loop started with."""
        if not self.flow.live:
            return
        for n, ln in self.moved.items():
            if n in head_flow.assigned and n not in head_flow.moved:
                raise CompileError(
                    f"{n} is moved inside a loop, so the next round would use a moved value. "
                    f"Move it after the loop, or use copy({n})", ln)

    def loop_var(self, name, t, line):
        if name in self.vars:
            if self.vars[name] != t:
                raise CompileError(f"{name} is already {self.vars[name]}; cannot hold {t}", line)
            if is_heap(t) and name not in self.borrowed:
                raise CompileError(f"{name} already owns a {t}; pick another name for the foreach variable", line)
            return
        self.declare(name, t, borrowed=is_heap(t))

    def foreach(self, s):
        mark = len(self.temps)
        seq, st = self.borrow(s.seq)
        mine = self.temps[mark:]           # a temporary being walked over lives until the loop ends
        del self.temps[mark:]
        self.flush_temps()
        if not (is_list(st) or is_map(st)):
            raise CompileError(f"foreach needs a list or map but got {st}", s.line)
        if is_list(st):
            kt, vt = "int", elem_of(st)
        else:
            kt, vt = map_kv(st)
        if s.key:
            self.loop_var(s.key, kt, s.line)
        self.loop_var(s.var, vt, s.line)
        pos = f"%v.__pos{self.n}"
        self.n += 1
        self.allocas.append(f"  {pos} = alloca i64")
        self.emit(f"store i64 0, ptr {pos}")
        walked = s.seq.name if s.seq.kind == "var" else None
        if walked:
            self.iterating.append(walked)
        head_flow = self.flow.copy()
        head, bodyl, stepl, end = (self.newlabel(x) for x in ("fe", "febody", "festep", "feend"))
        self.place_label(head)
        i, c = self.tmp(), self.tmp()
        self.emit(f"{i} = load i64, ptr {pos}")
        if is_list(st):
            n = self.tmp()
            self.emit(f"{n} = load i64, ptr {seq}, !tbaa !1")
            self.emit(f"{c} = icmp slt i64 {i}, {n}")
            self.emit(f"br i1 {c}, label %{bodyl}, label %{end}")
            self.place_label(bodyl)
            v = self.load_elem(self.list_elem_ptr(seq, vt, i), vt)
            self.emit(f"store {llty(vt)} {v}, ptr {self.slot(s.var)}")
            if s.key:
                self.emit(f"store i64 {i}, ptr {self.slot(s.key)}")
            nxt = self.tmp()
            self.emit(f"{nxt} = add i64 {i}, 1")
        else:
            idx = self.call("i64", "@pn_map_next", f"ptr {seq}", f"i64 {i}")
            self.emit(f"{c} = icmp sge i64 {idx}, 0")
            self.emit(f"br i1 {c}, label %{bodyl}, label %{end}")
            self.place_label(bodyl)
            ent = self.call("ptr", "@pn_map_entry", f"ptr {seq}", f"i64 {idx}")
            kp, vp = self.tmp(), self.tmp()
            self.emit(f"{kp} = getelementptr inbounds i8, ptr {ent}, i64 8")
            self.emit(f"{vp} = getelementptr inbounds i8, ptr {ent}, i64 16")
            if s.key:
                self.emit(f"store {llty(kt)} {self.load_slot(kp, kt)}, ptr {self.slot(s.key)}")
            self.emit(f"store {llty(vt)} {self.load_slot(vp, vt)}, ptr {self.slot(s.var)}")
            nxt = self.tmp()
            self.emit(f"{nxt} = add i64 {idx}, 1")
        self.emit(f"store i64 {nxt}, ptr {pos}")
        lp = Loop(end, stepl)
        self.loops.append(lp)
        self.pending.append(mine)
        self.flow = head_flow.copy()
        self.flow.assigned.update(n for n in (s.key, s.var) if n)
        self.stmt(s.body)
        self.pending.pop()
        self.loops.pop()
        self.flow = Flow.merge([self.flow] + lp.conts, head_flow)
        self.place_label(stepl)
        self.check_back_edge(head_flow)
        self.emit(f"br label %{head}")
        self.place_label(end)
        if walked:
            self.iterating.pop()
        self.free_temps(mine)
        self.flow = Flow.merge(lp.breaks + [head_flow], head_flow)

    def return_(self, s):
        if self.is_main:
            if s.val is not None:
                raise CompileError("top-level return cannot have a value", s.line)
            self.flush_temps()
            self.free_pending()
            self.free_owned()
            self.emit("ret i32 0")
            self.flow.live = False
            return
        if s.val is None:
            if self.ret != "void":
                raise CompileError(f"{self.name}() must return {self.ret}", s.line)
            self.flush_temps()
            self.free_pending()
            self.free_owned()
            self.emit("ret void")
            self.flow.live = False
            return
        if self.ret == "void":
            raise CompileError(f"{self.name}() is void and cannot return a value", s.line)
        if is_heap(self.ret):
            v, t = self.owned_value(s.val, self.ret, s.line, may_move_string=True)
        else:
            v, t = self.borrow(s.val, self.ret)
        v = self.coerce(v, t, self.ret, s.line, f"return value of {self.name}()")
        self.flush_temps()
        self.free_pending()
        self.free_owned()
        self.emit(f"ret {llty(self.ret)} {v}")
        self.flow.live = False

    def free_pending(self):
        for group in self.pending:
            self.free_temps(group)

    def finish(self):
        if not self.term:
            self.flush_temps()
            if self.is_main:
                self.free_owned()
                self.emit("ret i32 0")
            elif self.ret == "void":
                self.free_owned()
                self.emit("ret void")
            else:
                self.call("void", "@pn_noreturn", f"i32 {self.line}")
                self.emit("unreachable")
        if self.is_main:
            sig = "define i32 @main()"
        else:
            ps = ", ".join(f"{llty(t)} %arg.{n[1:]}" for t, n in self.params)
            sig = f"define internal {llty(self.ret)} @\"f.{self.name}\"({ps})"
        return [sig + " {", "entry:"] + self.allocas + self.inits + ["  br label %body", "body:"] + self.body + ["}"]


TBAA = """
!0 = !{!"pina"}
!6 = !{!"len", !0, i64 0}
!7 = !{!"data", !0, i64 0}
!8 = !{!"elem.int", !0, i64 0}
!9 = !{!"elem.float", !0, i64 0}
!12 = !{!"elem.bool", !0, i64 0}
!14 = !{!"cap", !0, i64 0}
!13 = !{!14, !14, i64 0}
!1 = !{!6, !6, i64 0}
!2 = !{!7, !7, i64 0}
!3 = !{!8, !8, i64 0}
!4 = !{!9, !9, i64 0}
!5 = !{!12, !12, i64 0}
!10 = !{!"branch_weights", i32 2000, i32 1}
!11 = !{!"branch_weights", i32 1, i32 2000}
"""


def compile_source(src):
    toks = lex(src)
    funcs, main = Parser(toks).program()
    mod = Module()
    for f in funcs:
        if f.name in mod.funcs or f.name in BUILTINS:
            raise CompileError(f"function {f.name}() is already defined", f.line)
        names = [p for _, p in f.params]
        if len(set(names)) != len(names):
            raise CompileError(f"{f.name}() has two parameters with the same name", f.line)
        mod.funcs[f.name] = ([t for t, _ in f.params], f.ret)
    out = []
    for f in funcs:
        g = FuncGen(mod, f.name, f.params, f.ret, f.line)
        g.stmt(f.body)
        out += g.finish()
    g = FuncGen(mod, "main", [], "void", 1, is_main=True)
    for s in main:
        g.stmt(s)
    out += g.finish()
    return "\n".join(mod.header() + out) + "\n" + TBAA


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Compile a .pn file to a native binary")
    ap.add_argument("src")
    ap.add_argument("-o", "--out")
    ap.add_argument("--emit-ll", action="store_true", help="only write the LLVM IR")
    a = ap.parse_args()
    src = open(a.src, encoding="utf-8").read()
    try:
        ir = compile_source(src)
    except CompileError as e:
        print(f"{a.src}:{e.line}: error: {e.msg}", file=sys.stderr)
        sys.exit(1)
    base = os.path.splitext(a.src)[0]
    ll = base + ".ll"
    with open(ll, "w") as f:
        f.write(ir)
    if a.emit_ll:
        return
    out = a.out or base
    r = subprocess.run(["clang", "-O2", "-pthread", "-Wno-override-module", ll, os.path.join(HERE, "runtime.c"),
                        "-lm", "-o", out], capture_output=True, text=True)
    if r.returncode:
        print(r.stderr, file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
