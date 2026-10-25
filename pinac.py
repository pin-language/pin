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
    def __init__(self, msg, line, file=None):
        super().__init__(msg)
        self.msg, self.line, self.file = msg, line, file   # file: set for errors in imported modules


# ----------------------------------------------------------------------------- lexer

TOKEN_RE = re.compile(r"""
  (?P<NL>\n)
 |(?P<WS>[ \t\r]+)
 |(?P<COMMENT>//[^\n]*|\#[^\n]*|/\*(?:.|\n)*?\*/)
 |(?P<OPEN><\?(?:php|pin)?|\?>\s*\Z)
 |(?P<FLOAT>\d+\.\d+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+)
 |(?P<INT>\d+)
 |(?P<STRING>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
 |(?P<VAR>\$[A-Za-z_]\w*)
 |(?P<IDENT>[A-Za-z_]\w*)
 |(?P<OP>::|\+\+|--|->|\+=|-=|\*=|/=|%=|\.=|===|!==|==|!=|<=|>=|=>|&&|\|\||\?\?|\[\]|[-+*/%<>=!(){}\[\];,:.?&])
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


TYPE_NAMES = {"int", "float", "bool", "string", "list", "map", "void", "db", "rows", "sub", "stream"}
ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "$": "$", "0": "\0"}
INTERP_RE = re.compile(r"(?<!\\)(\{\$[^{}\"]*\}|\$[A-Za-z_]\w*(?:->[A-Za-z_]\w*)?)")


def set_line(node, line):
    if isinstance(node, Node):
        node.line = line
        for v in node.__dict__.values():
            set_line(v, line)
    elif isinstance(node, (list, tuple)):
        for v in node:
            set_line(v, line)


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
        funcs, main, imports, classes = [], [], [], []
        while self.cur.kind != "EOF":
            if self.at("function", "IDENT"):
                funcs.append(self.function())
            elif self.at("class", "IDENT"):
                classes.append(self.class_())
            elif self.at("export", "IDENT"):
                line = self.eat().line
                if self.at("class", "IDENT"):
                    c = self.class_()
                    c.exported = True
                    classes.append(c)
                    continue
                if not self.at("function", "IDENT"):
                    raise CompileError("export goes before a function or a class: export function name(...): type "
                                       "{ ... }  or  export class Name { ... }", line)
                f = self.function()
                f.exported = True
                funcs.append(f)
            elif self.at("import", "IDENT"):
                imports.append(self.import_())
            elif self.cur.val in ("abstract", "final", "readonly") and self.peek().val == "class":
                raise CompileError(f"{self.cur.val} classes are not in Pin: classes have no inheritance, write "
                                   f"class Name {{ ... }}", self.cur.line)
            else:
                main.append(self.statement())
        return funcs, main, imports, classes

    CLASS_WORDS = ("public", "private", "protected", "static", "readonly", "abstract", "final", "var")

    def class_(self):
        """class Name { public int $x = 0; private string $s; function __construct(...) { } function m(): T { } }"""
        line = self.eat("class").line
        name = self.eat(kind="IDENT").val
        if name in TYPE_NAMES or name in NOT_IN_PIN or name in ("true", "false", "null"):
            raise CompileError(f"'{name}' cannot be a class name", line)
        if self.at("extends", "IDENT"):
            raise CompileError("Pin classes have no inheritance (extends). Put the shared part in its own class "
                               "and hold it as a property, e.g. public Address $address;", self.cur.line)
        if self.at("implements", "IDENT"):
            raise CompileError("interfaces are not in Pin yet; remove implements ...", self.cur.line)
        self.eat("{")
        fields, methods = [], []
        while not self.at("}"):
            t = self.cur
            if t.kind == "EOF":
                raise CompileError(f"missing '}}' at the end of class {name}", line)
            vis = None
            while self.cur.kind == "IDENT" and self.cur.val in self.CLASS_WORDS:
                w = self.eat()
                if w.val == "protected":
                    raise CompileError("protected is for inheritance, which Pin does not have: use private", w.line)
                if w.val == "static":
                    raise CompileError("static properties and methods are not in Pin (there is no shared state): "
                                       "use a top-level function, or a property set in __construct()", w.line)
                if w.val in ("readonly", "abstract", "final"):
                    raise CompileError(f"{w.val} is not in Pin yet", w.line)
                vis = "public" if w.val == "var" else w.val
            if self.at("function", "IDENT"):
                f = self.function(in_class=name)
                f.vis = vis or "public"
                if f.name == "__construct":
                    if any(m.name == "__construct" for m in methods):
                        raise CompileError(f"class {name} has two constructors", f.line)
                    for i, (pvis, ptype, pname) in enumerate(f.promoted):
                        fields.append(Node("prop", f.line, vis=pvis, vtype=ptype, name=pname[1:], default=None))
                methods.append(f)
                continue
            if self.at("const", "IDENT"):
                raise CompileError("class constants are not in Pin yet; use a method that returns the value", t.line)
            if vis is None:
                raise CompileError("a property needs public or private in front, e.g.  public int $id;  "
                                   "(methods start with function)", t.line)
            if self.cur.kind == "VAR":
                raise CompileError(f"property {self.cur.val} needs a type, e.g.  {vis} int {self.cur.val};", t.line)
            ptype = self.type_()
            pname = self.eat(kind="VAR").val
            default = self.expr() if self.accept("=") else None
            self.eat(";")
            fields.append(Node("prop", t.line, vis=vis, vtype=ptype, name=pname[1:], default=default))
        self.eat("}")
        return Node("class", line, name=name, props=fields, methods=methods, exported=False)

    def import_(self):
        """import prices;   import http/router;   import github.com/owner/repo/path as name;"""
        line = self.eat("import").line
        parts = []
        while not self.at(";") and not self.at("as", "IDENT") and self.cur.kind != "EOF":
            if self.cur.kind not in ("IDENT", "OP", "INT"):
                break
            parts.append(self.cur.val)
            self.i += 1
        path = "".join(parts)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.\-]*(/[A-Za-z0-9_][A-Za-z0-9_.\-]*)*", path) or ".." in path.split("/"):
            raise CompileError("import needs a module path like  import prices;  or  import http/router;  "
                               "(a file in the project folder, without .pn; paths start at the main file's folder, "
                               "so no ../ is needed)", line)
        alias = None
        if self.accept("as"):
            alias = self.eat(kind="IDENT").val
        self.eat(";")
        return Node("import", line, path=path, alias=alias or path.split("/")[-1].replace("-", "_").replace(".", "_"))

    def function(self, in_class=None):
        line = self.eat("function").line
        exported = False
        name = self.eat(kind="IDENT").val
        ctor = in_class is not None and name == "__construct"
        self.eat("(")
        params, promoted = [], []
        while not self.at(")"):
            pvis = None
            if self.cur.kind == "IDENT" and self.cur.val in ("public", "private", "protected", "readonly"):
                w = self.eat()
                if not ctor:
                    raise CompileError(f"'{w.val}' on a parameter only works in __construct() (it declares a "
                                       f"property)", w.line)
                if w.val in ("protected", "readonly"):
                    raise CompileError(f"{w.val} is not in Pin: use public or private", w.line)
                pvis = w.val
            if self.cur.kind == "VAR":
                raise CompileError(f"parameter {self.cur.val} needs a type, e.g. int {self.cur.val}, string "
                                   f"{self.cur.val} or list<int> {self.cur.val}", self.cur.line)
            ptype = self.type_()
            ref = self.accept("&")
            pname = self.eat(kind="VAR").val
            if self.at("="):
                raise CompileError(f"parameters cannot have default values in Pin yet ({pname} = ...); pass the "
                                   f"value at every call" + (", or set the property in the constructor body"
                                                             if pvis else ""), self.cur.line)
            if pname == "$this":
                raise CompileError("$this cannot be a parameter name", line)
            if pvis:
                if ref:
                    raise CompileError("a promoted constructor parameter cannot use &", line)
                promoted.append((pvis, ptype, pname))
            params.append((ptype, pname, ref))
            if not self.accept(","):
                break
        self.eat(")")
        if ctor:
            if self.accept(":"):
                if self.type_() != "void":
                    raise CompileError("__construct() returns nothing; leave out the return type", line)
            ret = "void"
        else:
            if not self.at(":"):
                raise CompileError(f"function {name}() needs a return type, e.g. ': int' or ': void'", self.cur.line)
            self.eat(":")
            ret = self.type_()
        throws = self.accept("throws")
        body = self.block()
        for pvis, ptype, pname in reversed(promoted):     # public int $id  ->  $this->id = $id; first
            this = Node("var", line, name="$this")
            body.body.insert(0, Node("assign", line, target=Node("field", line, obj=this, name=pname[1:]), op="=",
                                     val=Node("var", line, name=pname), promoted=True))
        return Node("func", line, name=name, params=params, ret=ret, body=body, exported=exported,
                    promoted=promoted, vis="public", throws=throws)

    def type_(self):
        t = self.cur
        if t.val == "?":
            raise CompileError("nullable types (?T) are not in Pin yet (designed: pin-language/docs design/null.md)", t.line)
        name = self.eat(kind="IDENT").val
        if name not in TYPE_NAMES:
            if name in NOT_IN_PIN or name in ("array", "mixed", "object", "integer", "boolean", "double", "str"):
                hint = {"array": " (use list<int> or map<string,int>)", "integer": " (use int)",
                        "boolean": " (use bool)", "double": " (use float)", "str": " (use string)",
                        "mixed": " (every value has one type in Pin)", "object": " (name the class)"}.get(name, "")
                raise CompileError(f"unknown type '{name}'{hint}", t.line)
            if name == "Task" and self.at("<"):     # Task<T>: a running `go` (concurrency.md C1)
                self.eat("<")
                inner = self.type_()
                self.close_angle()
                return f"Task<{inner}>"
            if self.at("::"):                       # a class from an imported module: shop::Order
                self.eat()
                name = f"{name}::{self.eat(kind='IDENT').val}"
            return name                             # a class name; checked once every class is known
        if name == "list":
            self.eat("<")
            inner = self.type_()
            self.close_angle()
            if inner == "void":
                raise CompileError("list<void> is not a type", t.line)
            return f"list<{inner}>"
        if name == "map":
            self.eat("<")
            k = self.type_()
            self.eat(",")
            v = self.type_()
            self.close_angle()
            if k not in ("int", "string"):
                raise CompileError(f"map keys must be int or string, not {k}", t.line)
            if v == "void":
                raise CompileError("map values cannot be void", t.line)
            return f"map<{k},{v}>"
        if self.at("<"):
            raise CompileError(f"type '{name}' takes no parameters", t.line)
        return name

    def close_angle(self):
        """The '>' that ends list<...> / map<...>; '>>' and '>=' come from the lexer as one token at times."""
        t = self.cur
        if t.val == ">":
            self.eat()
        elif t.val in (">=", ">>"):
            self.toks[self.i] = Tok("OP", t.val[1:], t.line)
        else:
            self.eat(">")

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
            if t.val == "try" and self.peek().val == "{":
                self.eat()
                body = self.block()
                if not self.at("catch", "IDENT"):
                    raise CompileError("try { ... } needs a catch ($e) { ... } after it" +
                                       (" (finally is not in Pin yet)" if self.at("finally", "IDENT") else ""), t.line)
                var, cbody, _ = self.catch_clause(value=False)
                return Node("trystmt", t.line, body=body, var=var, cbody=cbody)
            if t.val == "throw":
                self.eat()
                val = self.expr()
                self.eat(";")
                return Node("throw", t.line, val=val)
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
                ref = self.accept("&")
                key, var = None, self.eat(kind="VAR").val
                if self.accept("=>"):
                    if ref:
                        raise CompileError("& goes on the value: foreach ($m as $k => &$v)", t.line)
                    ref = self.accept("&")
                    key, var = var, self.eat(kind="VAR").val
                self.eat(")")
                return Node("foreach", t.line, seq=seq, key=key, var=var, ref=ref, body=self.statement())
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
            if ((t.val in TYPE_NAMES and self.peek().kind == "VAR") or (t.val in ("list", "map") and self.peek().val == "<")
                    or (t.val not in NOT_IN_PIN and t.val not in ("new", "print", "try", "throw", "await", "go") and self.peek().kind == "VAR")
                    or (self.peek().val == "::" and self.peek(2).kind == "IDENT" and self.peek(3).kind == "VAR")):
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
            if e.kind not in ("var", "index", "append", "field"):
                raise CompileError("left side of an assignment must be a variable, a list item, a map entry or a "
                                   "property", t.line)
            if e.kind == "append" and op != "=":
                raise CompileError("use $a[] = value to append", t.line)
            self.eat()
            val = self.expr()
            return Node("assign", t.line, target=e, op=op, val=val)
        if self.cur.kind == "OP" and op in ("++", "--"):
            if e.kind not in ("var", "index", "field"):
                raise CompileError(f"'{op}' needs a variable, a list/map item or a property", t.line)
            self.eat()
            one = Node("int", t.line, val=1)
            return Node("assign", t.line, target=e, op="+=" if op == "++" else "-=", val=one)
        if e.kind == "append":
            raise CompileError("$a[] can only be assigned to", t.line)
        if e.kind == "go":
            raise CompileError("a task started with go must be awaited: $t = go f(...); ... await $t;", t.line)
        if e.kind not in ("call", "mcall", "try", "new", "await"):
            raise CompileError("this expression does nothing on its own. (In a catch block, the value it gives must be "
                               "its last line, outside any if: catch ($e) { if (...) { return ...; } \"\"; })", t.line)
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

    def catch_clause(self, value):
        """catch ($e) { ... }  /  catch (Exception $e) { ... }. With `value`, the block may end with a bare
        expression: the value the try expression gives when it fails."""
        line = self.eat("catch").line
        self.eat("(")
        if self.cur.kind == "IDENT":
            w = self.eat()
            if w.val not in ("Error", "Exception", "Throwable"):
                raise CompileError(f"Pin has one error type, Error, so catch takes no class (or Error): "
                                   f"catch ($e) {{ ... }}", w.line)
        var = None
        if self.cur.kind == "VAR":
            var = self.eat().val
        self.eat(")")
        start = self.eat("{").line
        body, val = [], None
        while not self.at("}"):
            if self.cur.kind == "EOF":
                raise CompileError("missing '}'", start)
            if value and not (self.cur.kind == "IDENT" and self.cur.val in self.STATEMENT_WORDS):
                save = self.i
                try:
                    e = self.expr()
                    if self.at(";") and self.peek().val == "}":
                        self.eat(";")
                        val = e
                        break
                except CompileError:
                    pass
                self.i = save
            body.append(self.statement())
        self.eat("}")
        if self.at("catch", "IDENT"):
            raise CompileError("Pin has one error type, so one catch is enough", self.cur.line)
        if self.at("finally", "IDENT"):
            raise CompileError("finally is not in Pin yet: put the clean-up after the try/catch", self.cur.line)
        return var, Node("block", line, body=body), val

    STATEMENT_WORDS = ("if", "while", "for", "foreach", "return", "echo", "break", "continue", "throw", "try")

    def unary(self):
        t = self.cur
        if t.kind == "IDENT" and t.val == "go" and self.peek().kind == "IDENT":
            self.eat()
            e = self.unary()
            if e.kind not in ("call", "mcall"):
                raise CompileError("go starts a function call: $t = go fetch($id);", t.line)
            return Node("go", t.line, call=e)
        if t.kind == "IDENT" and t.val == "await" and self.peek().kind in ("VAR", "IDENT"):
            self.eat()
            return Node("await", t.line, e=self.unary())
        if t.kind == "IDENT" and t.val == "try" and self.peek().val != "{":
            self.eat()
            e = self.unary()
            if self.at("catch", "IDENT"):
                var, body, val = self.catch_clause(value=True)
                return Node("try", t.line, e=e, var=var, cbody=body, cval=val, catch=True)
            return Node("try", t.line, e=e, catch=False)
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
                if e.kind not in ("var", "field", "index"):
                    raise CompileError("append needs a variable or a property: $a[] = value", t.line)
                e = Node("append", t.line, seq=e)
            elif t.kind == "OP" and t.val == "->":
                self.eat()
                if self.cur.kind == "VAR":
                    raise CompileError(f"write $obj->{self.cur.val[1:]} without the second $", t.line)
                name = self.eat(kind="IDENT").val
                if self.at("("):
                    e = Node("mcall", t.line, obj=e, name=name, args=self.args())
                else:
                    e = Node("field", t.line, obj=e, name=name)
            elif t.kind == "OP" and t.val == "?" and self.peek().val == "->":
                raise CompileError("?-> is not in Pin yet: values are never null", t.line)
            elif t.kind == "OP" and t.val == "::" and e.kind == "var":
                raise CompileError("static members are not in Pin; use $obj->name", t.line)
            else:
                return e

    def args(self):
        """( a, b, &$c )"""
        self.eat("(")
        args = []
        while not self.at(")"):
            if self.at("&"):
                t = self.eat()
                args.append(Node("ref", t.line, e=self.postfix()))
            else:
                args.append(self.expr())
            if not self.accept(","):
                break
        self.eat(")")
        return args

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
                    if piece.startswith("{$"):          # "{$order->items[0]}": any path
                        try:
                            sub = Parser(lex(piece[1:-1]))
                            part = sub.expr()
                            if sub.cur.kind != "EOF":
                                raise CompileError("", t.line)
                        except CompileError:
                            raise CompileError(f"cannot read {piece} inside the string: write a variable, a "
                                               f"property or an item, e.g. {{$o->name}} or {{$xs[0]}}", t.line)
                        set_line(part, t.line)
                        parts.append(part)
                    elif piece.startswith("$"):
                        names = piece.split("->")
                        part = Node("var", t.line, name=names[0])
                        for f in names[1:]:
                            part = Node("field", t.line, obj=part, name=f)
                        parts.append(part)
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
                raise CompileError("null is not in Pin yet (designed: pin-language/docs design/null.md); use a value "
                                   "that means 'none', e.g. -1, \"\" or an empty list", t.line)
            if t.val in CONSTANTS:
                self.eat()
                kind, val = CONSTANTS[t.val]
                return Node(kind, t.line, val=val)
            if t.val == "new":
                self.eat()
                if self.cur.kind != "IDENT":
                    raise CompileError("new needs a class name: new Order(...)", t.line)
                cname = self.eat().val
                if self.at("::"):
                    self.eat()
                    cname = f"{cname}::{self.eat(kind='IDENT').val}"
                return Node("new", t.line, cls=cname, args=self.args() if self.at("(") else [])
            if t.val in NOT_IN_PIN and not (t.val == "list" and self.peek().val == "<"):
                raise CompileError(NOT_IN_PIN[t.val], t.line)
            self.eat()
            module = None
            if self.at("::"):                       # prices::price(...)
                self.eat()
                module, t = t.val, self.eat(kind="IDENT")
            if not self.at("("):
                raise CompileError(f"unknown name '{t.val}' (variables start with $)", t.line)
            return Node("call", t.line, name=t.val, args=self.args(), module=module)
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
ELEM_LL = {"int": "i64", "float": "double", "bool": "i8", "string": "ptr"}
ELEM_SIZE = {"int": 8, "float": 8, "bool": 1, "string": 8}   # list<string> holds owned Str pointers
ELEM_TBAA = {"int": "!3", "float": "!4", "bool": "!5"}


CLASSES = {}    # class key ("Order", or "shop::Order" in a module) -> ClassInfo; filled by compile_file
NEEDED = {}     # helper functions the generated code calls: '@"free:list<Order>"' -> (kind, type)


def llty(t):
    return {"int": "i64", "float": "double", "bool": "i1", "void": "void"}.get(t, "ptr")


def fn_llret(ret, throws):
    """LLVM return type of a function: a function that can fail also returns an error pointer (null = no error),
    in registers next to the result (error-handling.md E-R3)."""
    if not throws:
        return llty(ret)
    return "ptr" if ret == "void" else f"{{ {llty(ret)}, ptr }}"


class Catch:
    """Where errors go inside `try ... catch` (block=False) or `try { } catch` (block=True)."""
    def __init__(self, label, block, temp_mark, pending_mark, stash_mark):
        self.label, self.block = label, block
        self.temp_mark, self.pending_mark, self.stash_mark = temp_mark, pending_mark, stash_mark
        self.errs, self.flows = [], []      # (error value, block) and Flow at every place that can fail


def is_class(t):
    return t in CLASSES


def is_task(t):
    return t.startswith("Task<")


def holds_task(t):
    return is_task(t) or (is_list(t) and holds_task(elem_of(t)))


def owns_ptr(t):
    """Lists, maps, objects and tasks: an owned pointer whose freeing / copying / JSON needs per-type code."""
    return is_list(t) or is_map(t) or is_class(t) or is_task(t)


def ell(t):
    """How a value is stored inside a list, an object field: bools take one byte, the rest 8."""
    return {"int": "i64", "float": "double", "bool": "i8"}.get(t, "ptr")


def esize(t):
    return 1 if t == "bool" else 8


def kind_of(t):
    return KIND.get(t, 4)


def is_list(t):
    return t.startswith("list<")


def is_map(t):
    return t.startswith("map<")


def is_heap(t):
    return t in ("string", "db", "rows", "sub", "stream") or is_list(t) or is_map(t) or is_class(t) or is_task(t)


def elem_of(t):
    return t[5:-1]


def map_kv(t):
    k, v = t[4:-1].split(",", 1)
    return k, v


def need(kind, t):
    name = f'@"{kind}:{t}"'
    NEEDED.setdefault(name, (kind, t))
    return name


def free_fn(t):
    if t == "string":
        return "@pn_str_free"
    if t == "db":
        return "@pn_db_close"
    if t == "rows":
        return "@pn_rows_free"
    if t == "sub":
        return "@pn_hub_unsubscribe"
    if t == "stream":
        return "@pn_stream_close"
    if is_list(t):
        e = elem_of(t)
        return "@pn_list_free_str" if e == "string" else need("free", t) if owns_ptr(e) else "@pn_list_free"
    if is_map(t):
        return need("free", t) if owns_ptr(map_kv(t)[1]) else "@pn_map_free"
    return need("free", t)


def uncopyable(t, seen=()):
    """The part of type t that cannot be copied (a db connection, a result set, a running task), or None."""
    if t in ("db", "rows", "sub", "stream") or is_task(t):
        return t
    if is_list(t):
        return uncopyable(elem_of(t), seen)
    if is_map(t):
        return uncopyable(map_kv(t)[1], seen)
    if is_class(t) and t not in seen:
        for f in CLASSES[t].fields.values():
            if uncopyable(f.type, seen + (t,)):
                return f"{t}->{f.name} ({f.type})"
    return None


def copy_fn(t):
    if t == "string":
        return "@pn_str_dup"
    if is_list(t) and elem_of(t) == "string":
        return "@pn_list_copy_str"
    if is_map(t) and not owns_ptr(map_kv(t)[1]):
        return "@pn_map_copy"
    return need("copy", t)


def item_fn(kind, t):
    """A free / copy function to hand to the runtime as a callback. Always a generated function with a (ptr)
    signature: calling a C function such as pn_map_free(Map *) through void (*)(void *) is undefined
    behaviour in C (UBSan's -fsanitize=function reports it)."""
    return need(kind, t)


def json_fn(t):
    """A function (ptr out, ptr value) -> ptr out that appends the JSON of a list, map or object."""
    return need("json", t)


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


# PHP functions an agent will try; what to do in Pin instead
PHP_FUNCTION_HINTS = {
    "str_split": "no str_split() yet: use substr($s, $i, 1) in a loop",
    "usort": "no usort() yet: sort($xs) / rsort($xs) sort lists of int, float, bool or string",
    "ksort": "no ksort() yet: maps keep insertion order; collect the keys in a list and sort() it",
    "asort": "no asort() yet: write the loop", "arsort": "no arsort() yet: write the loop",
    "array_map": "no array_map() yet: use a for/foreach loop into a new list",
    "array_filter": "no array_filter() yet: use a foreach loop into a new list",
    "array_reduce": "no array_reduce() yet: use a foreach loop",
    "array_sum": "no array_sum() yet: $t = 0; foreach ($xs as $x) { $t += $x; }",
    "in_array": "no in_array() yet: loop and compare, or keep a map<int,bool> and use isset()",
    "array_search": "no array_search() yet: loop and return the index, -1 if not found",
    "array_keys": "no array_keys() yet: foreach ($m as $k => $v)", "array_values": "no array_values() yet",
    "array_push": "write $xs[] = $value;", "array_pop": "no array_pop() yet",
    "array_merge": "no array_merge() yet: copy() one list and append the other in a loop",
    "array_slice": "no array_slice() yet: copy the range in a loop", "count_chars": "not in Pin",
    "max": "no max() yet: $a > $b ? $a : $b", "min": "no min() yet: $a < $b ? $a : $b",
    "abs": "no abs() yet: $x < 0 ? -$x : $x",
    "round": "no round() yet: (int)($x + 0.5) for positive numbers, or keep money in integer satang/cents",
    "floor": "no floor() yet: (int)$x truncates toward zero", "ceil": "no ceil() yet",
    "number_format": "no number_format() yet: keep money as integer satang/cents and format with intdiv() and %",
    "sprintf": "no sprintf() yet: build the string with . and \"$var\" interpolation",
    "printf": "no printf() yet: use echo with . or \"$var\" interpolation",
    "print_r": "no print_r(): echo json_encode($value)", "var_dump": "no var_dump(): echo json_encode($value)",
    "print": "use echo",
    "json_decode": "no json_decode() yet: read values with json_string / json_int / json_float / json_bool, and go "
                   "into nested objects and arrays with json_raw($json, \"key\"), json_at($array, $i), json_len($array)",
    "fgets": "try read_line() reads one line of standard input", "readline": "try read_line()",
    "stream_get_line": "try read_line()",
    "is_numeric": "no is_numeric() yet: check the characters with substr() in a loop",
    "str_replace": "no str_replace() yet: rebuild the string with strpos()/substr()",
    "ucfirst": "no ucfirst() yet: strtoupper(substr($s, 0, 1)) . substr($s, 1)",
    "strrev": "no strrev() yet: loop from the end with substr($s, $i, 1)",
    "usleep": "sleep_ms($milliseconds) waits without blocking other tasks",
    "sleep": "sleep_ms($milliseconds): Pin counts milliseconds (sleep_ms(1000) is one second) and does not block "
             "other tasks",
    "rand": "no random numbers yet", "mt_rand": "no random numbers yet", "time": "no clock yet",
    "date": "no dates yet", "microtime": "no clock yet", "exit": "return from the top level instead",
    "die": "return from the top level instead",
    "header": "response_header(\"Name\", \"value\") adds a response header; http_status() sets the status",
    "getallheaders": "request_header(\"Name\") reads one request header (\"\" if absent)",
    "apache_request_headers": "request_header(\"Name\") reads one request header (\"\" if absent)",
    "setcookie": "response_header(\"Set-Cookie\", \"name=value; HttpOnly; Secure; SameSite=Lax\")",
    "file_get_contents": "http_get($url, [], 5000) for http:// URLs (no file access yet)", "fopen": "no file access yet",
    "curl_init": "http_get($url, $headers, $ms) / http_post($url, $headers, $body, $ms)",
    "exec": "run($program, [\"arg\", ...], $ms): no shell, the arguments go to the program as they are",
    "shell_exec": "run($program, [\"arg\", ...], $ms): no shell, so nothing can be injected",
    "system": "run($program, [\"arg\", ...], $ms)", "passthru": "run($program, [\"arg\", ...], $ms)",
    "proc_open": "run($program, [\"arg\", ...], $ms)",
}


# PHP words a newcomer (or an AI trained on PHP) will reach for, and what to do instead in Pin
NOT_IN_PIN = {
    "interface": "interfaces are not in Pin yet", "trait": "traits are not in Pin yet",
    "enum": "enums are not in Pin yet; use int or string constants", "abstract": "classes are not in Pin yet",
    "catch": "catch goes after try: try f() catch ($e) { ... }  or  try { ... } catch ($e) { ... }",
    "finally": "finally is not in Pin yet: put the clean-up after the try/catch",
    "switch": "switch is not in Pin; use if / elseif / else", "match": "match is not in Pin; use if / elseif / else",
    "do": "do/while is not in Pin; use while", "goto": "goto is not in Pin",
    "include": "use `import name;` (loads name.pn; call name::fn())", "require": "use `import name;`",
    "include_once": "use `import name;`", "require_once": "use `import name;`",
    "use": "use `import name;` to load name.pn and call name::fn()", "namespace": "each file is a module: use `import name;` and name::fn()",
    "global": "Pin has no global variables, by design: pass values as parameters",
    "static": "Pin has no static variables: pass values as parameters",
    "const": "const is not in Pin yet; use a variable or a function that returns the value",
    "define": "define() is not in Pin yet; use a variable or a function that returns the value",
    "function": "anonymous functions (closures) are not in Pin yet; declare a named function",
    "fn": "arrow functions are not in Pin yet; declare a named function",
    "array": "array() is not in Pin: write [1, 2] for a list<int> or [\"k\" => 1] for a map<string,int>",
    "list": "list() destructuring is not in Pin; assign each value separately",
    "yield": "generators are not in Pin",
}
CONSTANTS = {"PHP_INT_MAX": ("int", 9223372036854775807), "PHP_INT_MIN": ("int", -9223372036854775808),
             "PHP_EOL": ("str", "\n")}

BUILTINS = {"json_raw", "json_at", "json_len", "read_line", "http_get", "http_post", "http_stream", "stream_next", "stream_last_id", "run", "await_all", "await_timeout", "hub_publish", "hub_subscribe", "hub_resume", "hub_next", "hub_last_id",
            "sse_start", "sse_send", "sse_send_id", "sse_comment", "sleep_ms", "error", "request_header", "request_has_header", "response_header", "count", "fill", "copy", "sqrt", "intdiv", "strlen", "substr", "strpos", "str_contains",
            "str_starts_with", "str_ends_with", "str_repeat", "strtolower", "strtoupper", "trim", "intval",
            "strval", "json_encode", "isset", "array_key_exists", "unset", "serve", "http_status",
            "wrapping_add", "wrapping_sub", "wrapping_mul", "explode", "implode", "sort", "rsort",
            "url_path", "query_param", "form_param", "query_has", "form_has", "floatval", "json_has",
            "db_open", "db_exec", "db_query", "db_json", "db_next", "db_int", "db_float", "db_string",
            "db_is_null", "db_last_id", "db_error", "json_string", "json_int", "json_float",
            "json_bool"}

RUNTIME_DECLS = """
declare ptr @pn_list_new(i64)
declare ptr @pn_list_fill(i64, i64, i64)
declare void @pn_list_push(ptr, i64, i64)
declare ptr @pn_list_copy(ptr, i64)
declare void @pn_list_free(ptr)
declare void @pn_list_free_str(ptr)
declare ptr @pn_list_copy_str(ptr)
declare ptr @pn_list_fill_str(i64, ptr)
declare ptr @pn_explode(ptr, ptr)
declare ptr @pn_implode(ptr, ptr, i64)
declare void @pn_list_sort(ptr, i64, i64)
declare ptr @pn_url_path(ptr)
declare ptr @pn_query_param(ptr, ptr)
declare ptr @pn_form_param(ptr, ptr)
declare i64 @pn_jget_has(ptr, ptr)
declare ptr @pn_db_open(ptr)
declare void @pn_db_close(ptr)
declare void @pn_rows_free(ptr)
declare i64 @pn_db_exec(ptr, ptr, ptr, i64)
declare ptr @pn_db_query(ptr, ptr, ptr, i64)
declare ptr @pn_db_json(ptr, ptr, ptr, i64)
declare i64 @pn_db_next(ptr)
declare i64 @pn_db_int(ptr, ptr)
declare double @pn_db_float(ptr, ptr)
declare ptr @pn_db_string(ptr, ptr)
declare i64 @pn_db_is_null(ptr, ptr)
declare i64 @pn_db_last_id(ptr)
declare ptr @pn_db_error(ptr)
declare double @pn_str_to_float(ptr)
declare i64 @pn_query_has(ptr, ptr)
declare i64 @pn_form_has(ptr, ptr)
declare ptr @pn_jget_string(ptr, ptr)
declare i64 @pn_jget_int(ptr, ptr)
declare double @pn_jget_float(ptr, ptr)
declare i64 @pn_jget_bool(ptr, ptr)
declare void @pn_list_grow(ptr, i64) cold
declare void @pn_oob(i64, i64) noreturn cold
declare void @pn_divzero(i32) noreturn cold
declare void @pn_noreturn(i32) noreturn cold
declare void @pn_overflow(i32) noreturn cold
declare void @pn_float_to_int(i32) noreturn cold
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
declare ptr @pn_obj_new(i64)
declare void @pn_uncaught(ptr) noreturn cold
declare i32 @pn_run_main(ptr)
declare void @pn_sleep(i64)
declare ptr @pn_go(ptr, ptr)
declare ptr @pn_await(ptr)
declare ptr @pn_await_all(ptr, i64, ptr, ptr, ptr)
declare ptr @pn_await_timeout(ptr, i64, ptr, ptr)
declare ptr @pn_timeout_error()
declare void @pn_hub_publish(ptr, ptr)
declare ptr @pn_hub_subscribe(ptr)
declare ptr @pn_hub_resume(ptr, i64)
declare void @pn_hub_unsubscribe(ptr)
declare { ptr, ptr } @pn_hub_next(ptr, i64)
declare i64 @pn_hub_last_id(ptr)
declare ptr @pn_sse_start()
declare ptr @pn_sse_send(ptr)
declare ptr @pn_sse_send_id(i64, ptr)
declare ptr @pn_sse_comment(ptr)
declare { ptr, ptr } @pn_http_get(ptr, ptr, i64)
declare { ptr, ptr } @pn_http_post(ptr, ptr, ptr, i64)
declare { ptr, ptr } @pn_http_stream(ptr, ptr, i64, i64)
declare { ptr, ptr } @pn_stream_next(ptr, i64)
declare i64 @pn_stream_last_id(ptr)
declare void @pn_stream_close(ptr)
declare { ptr, ptr } @pn_run(ptr, ptr, i64)
declare ptr @pn_jget_raw(ptr, ptr)
declare ptr @pn_jget_at(ptr, i64)
declare i64 @pn_jget_len(ptr)
declare { ptr, ptr } @pn_read_line()
declare ptr @pn_request_header(ptr)
declare i64 @pn_request_has_header(ptr)
declare void @pn_response_header(ptr, ptr)
declare ptr @pn_handler_error(ptr)
declare void @pn_obj_free(ptr)
declare void @pn_list_free_with(ptr, ptr)
declare ptr @pn_list_copy_with(ptr, ptr)
declare ptr @pn_json_add_list(ptr, ptr, i64)
declare ptr @pn_json_add_list_with(ptr, ptr, ptr)
declare ptr @pn_json_add_map(ptr, ptr)
declare ptr @pn_json_add_map_with(ptr, ptr, ptr)
declare ptr @pn_json_add_str(ptr, ptr)
declare ptr @pn_json_add_float(ptr, double)
declare void @pn_map_free_with(ptr, ptr)
declare ptr @pn_map_copy_with(ptr, ptr)
declare void @pn_map_del_with(ptr, i64, ptr)
"""


# ----------------------------------------------------------------------------- codegen

class Module:
    def __init__(self):
        self.lits = {}
        self.funcs = {}         # key ("name" in the main file, "module::name" elsewhere) -> (param types, ret)
        self.exported = {}      # key -> bool
        self.params = {}        # key -> [(type, name, ref)]
        self.throws = {}        # key -> declared `throws`
        self.handler = None
        self.inferring = False  # compiling methods only to find out which ones change $this (R6)
        self.go_funcs = []      # IR of the functions that run a `go` call inside its task
        self.ngo = 0
        self.changed = False

    def go_trampoline(self, key, argtys, ret, throws, owned):
        """A function the new task runs: unpack the frame, call the function, store result and error back."""
        self.ngo += 1
        name = f'@"go.{self.ngo}"'
        if self.inferring:
            return name
        L = [f"define internal void {name}(ptr %f) {{", "entry:"]
        args = []
        for i, ty in enumerate(argtys):
            L += [f"  %p{i} = getelementptr inbounds i8, ptr %f, i64 {16 + 8 * i}", f"  %a{i} = load {ty}, ptr %p{i}"]
            args.append(f"{ty} %a{i}")
        rt = fn_llret(ret, throws)
        call = f"call {rt} @\"f.{key}\"({', '.join(args)})"
        L.append(f"  %r = {call}" if rt != "void" else f"  {call}")
        if throws:
            if ret == "void":
                L += ["  %ep = getelementptr inbounds i8, ptr %f, i64 8", "  store ptr %r, ptr %ep"]
            else:
                L += [f"  %v = extractvalue {rt} %r, 0", f"  %e = extractvalue {rt} %r, 1",
                      "  %ep = getelementptr inbounds i8, ptr %f, i64 8", "  store ptr %e, ptr %ep",
                      f"  store {llty(ret)} %v, ptr %f"]
        elif ret != "void":
            L.append(f"  store {llty(ret)} %r, ptr %f")
        for j, t in enumerate(owned):
            off = 16 + 8 * (len(argtys) + j)
            L += [f"  %o{j}p = getelementptr inbounds i8, ptr %f, i64 {off}", f"  %o{j} = load ptr, ptr %o{j}p",
                  f"  call void {free_fn(t)}(ptr %o{j})"]
        L += ["  ret void", "}"]
        self.go_funcs += L
        return name

    def lit(self, s):
        """A string literal: a constant {len, -1, bytes} the runtime never frees or writes."""
        if s not in self.lits:
            self.lits[s] = f"@.L{len(self.lits)}"
        return self.lits[s]

    def header(self):
        helpers, done = [], set()
        while len(done) < len(NEEDED):          # helpers may need more helpers (and string literals)
            for name, (kind, t) in list(NEEDED.items()):
                if name not in done:
                    done.add(name)
                    helpers += gen_helper(self, name, kind, t)
        h = ["; generated by pinac (prototype)", "%List = type { i64, i64, ptr }"]
        for s, name in self.lits.items():
            data = s.encode("utf-8") + b"\0"
            enc = "".join(chr(b) if 32 <= b < 127 and b not in (34, 92) else f"\\{b:02X}" for b in data)
            n = len(data)
            h.append(f'{name} = private unnamed_addr constant {{ i64, i64, [{n} x i8] }} '
                     f'{{ i64 {n - 1}, i64 -1, [{n} x i8] c"{enc}" }}, align 8')
        h.append(RUNTIME_DECLS)
        h += helpers
        h += self.go_funcs
        if self.handler and self.throws.get(self.handler):
            # an error leaving the handler answers its code (400-599, else 500) with the standard status text
            h.append('define ptr @pn_user_handler(ptr %m, ptr %p, ptr %b) {\n'
                     f'  %r = call {{ ptr, ptr }} @"f.{self.handler}"(ptr %m, ptr %p, ptr %b)\n'
                     '  %v = extractvalue { ptr, ptr } %r, 0\n  %e = extractvalue { ptr, ptr } %r, 1\n'
                     '  %bad = icmp ne ptr %e, null\n  br i1 %bad, label %err, label %ok\n'
                     'ok:\n  ret ptr %v\nerr:\n  %s = call ptr @pn_handler_error(ptr %e)\n  ret ptr %s\n}')
        elif self.handler:
            h.append('define ptr @pn_user_handler(ptr %m, ptr %p, ptr %b) {\n'
                     f'  %r = call ptr @"f.{self.handler}"(ptr %m, ptr %p, ptr %b)\n  ret ptr %r\n}}')
        return h


class Field:
    def __init__(self, name, type_, vis, default, index, line):
        self.name, self.type, self.vis, self.default, self.index, self.line = name, type_, vis, default, index, line


class Method:
    def __init__(self, cls, node, params, ret):
        self.cls, self.node, self.params, self.ret = cls, node, params, ret     # params: (type, name, ref)
        self.name, self.vis = node.name, node.vis
        self.changing = node.name == "__construct"     # inferred for the others (ownership-v2.md R6)
        self.throws = bool(getattr(node, "throws", False))
        self.llname = f'@"m.{cls.key}.{node.name}"'


class ClassInfo:
    def __init__(self, node, key, mid, file, imports):
        self.node, self.name, self.key, self.mid, self.file, self.imports = node, node.name, key, mid, file, imports
        self.exported = node.exported or not mid
        self.fields = {}        # name (without $) -> Field, in declaration order (the JSON order)
        self.methods = {}       # name -> Method

    @property
    def size(self):
        return 8 * max(1, len(self.fields))


def gen_helper(mod, name, kind, t):
    """IR for one of the per-type functions lists, maps and objects of lists, maps and objects need."""
    n = [0]
    out = []

    def tmp():
        n[0] += 1
        return f"%h{n[0]}"

    def emit(x):
        out.append("  " + x)

    def field_ptr(o, f):
        p = tmp()
        emit(f"{p} = getelementptr inbounds i8, ptr {o}, i64 {8 * f.index}")
        return p

    def load_field(o, f):
        v = tmp()
        emit(f"{v} = load {ell(f.type)}, ptr {field_ptr(o, f)}")
        return v

    def json_value(d, v, ft):
        r = tmp()
        if ft == "int":
            emit(f"{r} = call ptr @pn_str_add_int(ptr {d}, i64 {v})")
        elif ft == "float":
            emit(f"{r} = call ptr @pn_json_add_float(ptr {d}, double {v})")
        elif ft == "bool":
            b, sel = tmp(), tmp()
            emit(f"{b} = icmp ne i8 {v}, 0")
            emit(f"{sel} = select i1 {b}, ptr {mod.lit('true')}, ptr {mod.lit('false')}")
            emit(f"{r} = call ptr @pn_str_add_str(ptr {d}, ptr {sel})")
        elif ft == "string":
            emit(f"{r} = call ptr @pn_json_add_str(ptr {d}, ptr {v})")
        else:
            emit(f"{r} = call ptr {json_fn(ft)}(ptr {d}, ptr {v})")
        return r

    if kind == "free" and is_task(t):
        # a task dropped without await (only on an error or panic path): wait for it, discard what it gave
        rt = t[5:-1]
        out += [f"define internal void {name}(ptr %p) {{", "entry:", "  %z = icmp eq ptr %p, null",
                "  br i1 %z, label %done, label %go", "go:", "  %f = call ptr @pn_await(ptr %p)"]
        if is_heap(rt):
            out += ["  %r = load ptr, ptr %f", f"  call void {free_fn(rt)}(ptr %r)"]
        out += ["  %ep = getelementptr inbounds i8, ptr %f, i64 8", "  %e = load ptr, ptr %ep",
                f"  call void {free_fn('Error')}(ptr %e)", "  call void @pn_obj_free(ptr %f)", "  br label %done",
                "done:", "  ret void", "}"]
        return out
    if kind == "free":
        out.append(f"define internal void {name}(ptr %p) {{")
        out.append("entry:")
        if t == "string" or (is_list(t) and not owns_ptr(elem_of(t))) or (is_map(t) and not owns_ptr(map_kv(t)[1])):
            emit(f"call void {free_fn(t)}(ptr %p)")        # a wrapper with a (ptr) signature, see item_fn
        elif is_list(t):
            emit(f"call void @pn_list_free_with(ptr %p, ptr {item_fn('free', elem_of(t))})")
        elif is_map(t):
            emit(f"call void @pn_map_free_with(ptr %p, ptr {item_fn('free', map_kv(t)[1])})")
        else:
            emit("%z = icmp eq ptr %p, null")
            emit("br i1 %z, label %done, label %go")
            out.append("go:")
            for f in CLASSES[t].fields.values():
                if is_heap(f.type):
                    emit(f"call void {free_fn(f.type)}(ptr {load_field('%p', f)})")
            emit("call void @pn_obj_free(ptr %p)")
            emit("br label %done")
            out.append("done:")
        emit("ret void")
    elif kind == "copy":
        out.append(f"define internal ptr {name}(ptr %p) {{")
        out.append("entry:")
        if t == "string" or (is_list(t) and elem_of(t) == "string") or (is_map(t) and not owns_ptr(map_kv(t)[1])):
            emit(f"%r = call ptr {copy_fn(t)}(ptr %p)")
        elif is_list(t):
            e = elem_of(t)
            if owns_ptr(e):
                emit(f"%r = call ptr @pn_list_copy_with(ptr %p, ptr {item_fn('copy', e)})")
            else:
                emit(f"%r = call ptr @pn_list_copy(ptr %p, i64 {esize(e)})")
        elif is_map(t):
            emit(f"%r = call ptr @pn_map_copy_with(ptr %p, ptr {item_fn('copy', map_kv(t)[1])})")
        else:
            ci = CLASSES[t]
            emit(f"%r = call ptr @pn_obj_new(i64 {ci.size})")
            for f in ci.fields.values():
                v = load_field("%p", f)
                if is_heap(f.type):
                    c = tmp()
                    emit(f"{c} = call ptr {copy_fn(f.type)}(ptr {v})")
                    v = c
                emit(f"store {ell(f.type)} {v}, ptr {field_ptr('%r', f)}")
        emit("ret ptr %r")
    else:
        out.append(f"define internal ptr {name}(ptr %d, ptr %p) {{")
        out.append("entry:")
        d = "%d"
        if is_list(t):
            e = elem_of(t)
            r = tmp()
            if owns_ptr(e):
                emit(f"{r} = call ptr @pn_json_add_list_with(ptr %d, ptr %p, ptr {json_fn(e)})")
            else:
                emit(f"{r} = call ptr @pn_json_add_list(ptr %d, ptr %p, i64 {KIND[e]})")
            d = r
        elif is_map(t):
            v = map_kv(t)[1]
            r = tmp()
            if owns_ptr(v):
                emit(f"{r} = call ptr @pn_json_add_map_with(ptr %d, ptr %p, ptr {json_fn(v)})")
            else:
                emit(f"{r} = call ptr @pn_json_add_map(ptr %d, ptr %p)")
            d = r
        else:
            public = [f for f in CLASSES[t].fields.values() if f.vis == "public"]
            if not public:
                r = tmp()
                emit(f"{r} = call ptr @pn_str_add_str(ptr %d, ptr {mod.lit('{}')})")
                d = r
            for i, f in enumerate(public):
                key = ("{" if i == 0 else ",") + json_string_literal(f.name) + ":"
                r = tmp()
                emit(f"{r} = call ptr @pn_str_add_str(ptr {d}, ptr {mod.lit(key)})")
                d = json_value(r, load_field("%p", f), f.type)
            if public:
                r = tmp()
                emit(f"{r} = call ptr @pn_str_add_str(ptr {d}, ptr {mod.lit('}')})")
                d = r
        emit(f"ret ptr {d}")
    out.append("}")
    return out


def json_string_literal(s):
    return '"' + s + '"'          # property names are plain identifiers: nothing to escape


def unjsonable(t, seen=()):
    """The part of type t that json_encode() cannot write, or None. Private properties are left out."""
    if t in ("db", "rows", "sub", "stream") or is_task(t):
        return t
    if is_list(t):
        return unjsonable(elem_of(t), seen)
    if is_map(t):
        return unjsonable(map_kv(t)[1], seen)
    if is_class(t) and t not in seen:
        for f in CLASSES[t].fields.values():
            if f.vis == "public" and unjsonable(f.type, seen + (t,)):
                return f"{t}->{f.name} ({f.type})"
    return None


def path_root(e):
    """The variable a path starts from, and the properties it goes through before its first index:
    $o->a->b[1]->c  ->  ("$o", ("a", "b")). Paths that start at a function result have no root."""
    fields = []
    while True:
        if e.kind == "var":
            return e.name, tuple(reversed(fields))
        if e.kind == "field":
            fields.append(e.name)
            e = e.obj
        elif e.kind in ("index", "append"):
            fields = []                 # past an index, conflicts are judged on the whole container
            e = e.seq
        else:
            return None, ()


def overlaps(a, b):
    """Two property paths touch the same data when one is a prefix of the other."""
    k = min(len(a), len(b))
    return a[:k] == b[:k]


def show(e):
    """Source-like text for a path, for error messages."""
    k = e.kind
    if k == "var":
        return e.name
    if k == "field":
        return f"{show(e.obj)}->{e.name}"
    if k == "index":
        i = e.idx
        inner = (i.name if i.kind == "var" else str(i.val) if i.kind == "int" else f'"{i.val}"' if i.kind == "str"
                 else "...")
        return f"{show(e.seq)}[{inner}]"
    if k == "append":
        return f"{show(e.seq)}[]"
    if k == "call":
        return f"{e.name}(...)"
    if k == "mcall":
        return f"{show(e.obj)}->{e.name}(...)"
    if k == "new":
        return f"new {e.cls}(...)"
    return "the value"


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
    def __init__(self, mod, name, params, ret, line, is_main=False, modid="", imports=None, method=None,
                 throws=False):
        self.mod, self.name, self.ret, self.line, self.is_main = mod, name, ret, line, is_main
        self.modid, self.imports = modid, imports or {}     # imports: alias -> (module id, file)
        self.key = name if not modid else f"{modid}::{name}"
        self.method = method
        self.cls = method.cls if method else None
        self.is_ctor = bool(method and method.name == "__construct")
        self.llname = method.llname if method else f'@"f.{self.key}"'
        if method:
            self.name = f"{self.cls.name}::{name}"
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
        self.slot_over = {}     # variables whose memory is somewhere else: & parameters, foreach & variables
        self.refs = set()       # those variables: they change the caller's value and own nothing
        self.borrow_vars = {}   # $u = $users[0]: name -> {(root variable, properties)} it borrows from (R2)
        self.loop_scope = {}    # foreach variable -> the variables that existed before its loop
        self.uses = {}          # variable -> [(order, line)] of its reads, from prepare()
        self.open_loops = []    # loops being generated: (first order, last order, kills)
        self.at = 0             # order of the statement being generated
        self.ctor_need = set()  # in __construct: properties without a default, which it must set
        self.throws = throws or bool(method and method.throws)
        self.try_stack = []     # Catch handlers, innermost last
        self.try_ok = 0         # > 0 inside the operand of a try: calls that can fail are allowed
        self.fail_count = 0     # places that can fail so far (a try with none is an error)
        self.temp_stack = []    # statement temporaries set aside while a catch block's statements run
        self.task_borrows = {}  # task (or list of tasks) variable -> {(root, props)} its tasks read (C2)
        self.task_lent = {}     # task variable -> roots lent to it with & (C3): no other use until await
        self.task_fail = {}     # task variable -> its tasks can fail (await needs try)
        self.task_line = {}     # task variable -> line of its go
        self.foreach_root = {}  # foreach variable -> (root, props) of what it walks (None: a temporary)
        self.go_ok = False      # a go expression is allowed here (stored in a variable, or awaited)
        if method:
            self.declare("$this", self.cls.key, borrowed=not method.changing)
            self.flow.assigned.add("$this")
            self.inits.append(f"  store ptr %arg.this, ptr {self.slot('$this')}")
            if self.is_ctor:
                self.ctor_need = {f.name for f in self.cls.fields.values() if f.default is None}
        for ptype, pname, ref in params:
            if ref:
                self.vars[pname] = ptype
                self.slot_over[pname] = f"%arg.{pname[1:]}"
                self.refs.add(pname)
            else:
                self.declare(pname, ptype, borrowed=is_heap(ptype))
                self.inits.append(f"  store {llty(ptype)} %arg.{pname[1:]}, ptr {self.slot(pname)}")
            self.flow.assigned.add(pname)

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

    def slot(self, name):
        return self.slot_over.get(name) or f"%v.{name[1:]}"

    # --- the order things happen in, for the borrow checks
    def prepare(self, nodes):
        """Number every node in evaluation order (children before their parent) and note where each variable
        is read. A borrowed variable ($u = $users[0]) blocks changes to what it borrows from only while it is
        used again later (ownership-v2.md R2); these numbers say what "later" is."""
        count = [0]
        self.uses = {}

        def walk(x, target=False):
            if isinstance(x, (list, tuple)):
                for y in x:
                    walk(y)
                return
            if not isinstance(x, Node):
                return
            first = count[0] + 1
            if x.kind == "assign":
                walk(x.val)
                walk(x.target, target=x.target.kind == "var" and x.op == "=")
            else:
                for k, v in list(x.__dict__.items()):
                    if k not in ("kind", "line", "order", "first", "kills"):
                        walk(v)
            count[0] += 1
            x.order = count[0]
            if x.kind == "var" and not target:
                self.uses.setdefault(x.name, []).append((x.order, x.line))
            if x.kind in ("while", "for", "foreach"):
                x.first = first
                body = x.body.body if x.body.kind == "block" else [x.body]
                x.kills = {}
                for st in body:     # statements every round runs before anything later in the body
                    if st.kind == "assign" and st.target.kind == "var" and st.op == "=":
                        x.kills.setdefault(st.target.name, []).append(st.order)
                    elif st.kind == "decl":
                        x.kills.setdefault(st.name, []).append(st.order)
        walk(nodes)

    def live_use(self, name, at):
        """Line of a read of `name` that can happen after point `at`, or None."""
        uses = self.uses.get(name, ())
        for o, ln in uses:
            if o > at:
                return ln
        for lo, hi, kills in self.open_loops:       # earlier in a loop body: reached again by the next round
            for o, ln in uses:
                if lo <= o <= at and not any(lo <= k < o for k in kills.get(name, ())):
                    return ln
        return None

    def walked(self, name):
        return any(r == name for r, _, _ in self.iterating)

    def mutating(self, root, props, line, at=None, what="changed"):
        """Every change goes through here: `root` is the variable the changed data hangs from, `props` the
        properties on the way ($o->items[] = ...: "$o", ("items",))."""
        at = self.at if at is None else at
        if root == "$this" and self.method:
            if not self.method.changing:
                if not self.mod.inferring:
                    raise CompileError("internal: changing method not inferred", line)
                self.method.changing = True         # R6: a method that changes $this is a changing method
                self.mod.changed = True
        else:
            self.check_writable(root, line, what)
        for r, p, ln in self.iterating:
            if r == root and overlaps(p, props):
                walked = r + "".join(f"->{x}" for x in p)
                path = root + "".join(f"->{x}" for x in props)
                verb = {"appended to": "append to", "sorted": "sort", "moved": "move", "replaced": "replace",
                        "moved or returned": "move"}.get(what, "call " + what[11:] + " on" if what.startswith(
                            "changed by ") else "change")
                raise CompileError(f"cannot {verb} {path} while a foreach is looping over "
                                   f"{'it' if walked == path else walked + f' (line {ln})'}", line)
        for b, roots in self.task_borrows.items():
            for r, p in roots:
                if r == root and overlaps(p, props) and b != root:
                    ln = self.live_use(b, at)
                    if ln:
                        raise CompileError(
                            f"cannot change {root} here: the task in {b} (started on line {self.task_line.get(b, '?')}) "
                            f"reads it until it is awaited on line {ln}. Change it after the await, or give the "
                            f"task its own copy()", line)
        for b, roots in self.borrow_vars.items():
            for r, p in roots:
                if r == root and overlaps(p, props) and b != root:
                    ln = self.live_use(b, at)
                    if ln:
                        raise CompileError(
                            f"cannot change {root} here: {b} borrows from it and is used again on line {ln}. "
                            f"Use {b} for the last time before this line, or keep your own value with "
                            f"{b} = copy(...)", line)

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
        return [(n, t) for n, t in self.vars.items()
                if is_heap(t) and n not in self.borrowed and n not in self.refs and n != "$this"]

    def free_owned(self, skip=()):
        # tasks first: freeing one waits for it, and it may still be reading the other variables
        for n, t in sorted(self.owned_vars(), key=lambda v: not holds_task(v[1])):
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
            t = self.vars[name]
            fix = f"$copy = {name};" if t == "string" else f"$copy = copy({name});"
            if name == "$this":
                raise CompileError(f"$this cannot be {what} here", line)
            if name in self.borrow_vars:
                raise CompileError(f"{name} is borrowed (read-only) from another value, so it cannot be {what}. "
                                   f"Change the original directly (e.g. $list[$i]->name = ...), or work on "
                                   f"{fix}", line)
            if any(p == name for _, p, _ in self.params):
                amp = ("" if what == "moved or returned" else
                       f"To change the caller's value declare it as {t} &{name} (and call with &$value); or ")
                raise CompileError(f"{name} is a parameter lent to this function read-only, so it cannot be {what}. "
                                   f"{amp or ''}{'work' if amp else 'Work'} on {fix}", line)
            raise CompileError(f"{name} is a foreach variable lent read-only, so it cannot be {what}. Use "
                               f"foreach (... as &{name}) to change the items, or work on {fix}", line)

    # --- expressions: return (llvm value, type, owned)
    def require_assigned(self, name, line):
        if name not in self.vars:
            if name == "$this":
                raise CompileError("$this only exists inside the methods of a class", line)
            raise CompileError(f"variable {name} is used before it is assigned", line)
        if name not in self.flow.assigned and name not in self.moved:
            raise CompileError(
                f"{name} may be used before it is assigned: on some paths to this line it never gets a "
                f"value (an if without else, or a loop that can run zero times). Give {name} a value "
                f"before the if/loop", line)

    def use_var(self, name, line, raw=False):
        self.require_assigned(name, line)
        for tv, lent in self.task_lent.items():
            if name in lent and tv != name:
                ln = self.live_use(tv, self.at)
                if ln:
                    raise CompileError(f"{name} is lent with & to the task in {tv} (line {self.task_line.get(tv, '?')}) "
                                       f"until it is awaited on line {ln}; use it after the await", line)
        if name == "$this" and self.ctor_need and not raw:
            missing = sorted(f for f in self.ctor_need if f"$this->{f}" not in self.flow.assigned)
            if missing:
                raise CompileError(f"__construct() uses $this before every property is set (still missing: "
                                   f"{', '.join('$this->' + m for m in missing)}). Set them first", line)
        if name in self.moved:
            how = "may have been moved" if name in self.flow.maybe else "was moved"
            where = " (on one path of an if or loop)" if name in self.flow.maybe else ""
            fix = (f"Pass {name} to functions instead of assigning it (functions borrow it), or open another one"
                   if self.vars[name] in ("db", "rows") else f"Use copy({name}) if you need two separate values")
            if holds_task(self.vars[name]):
                raise CompileError(f"the task in {name} was already awaited (or moved) on line {self.moved[name]}"
                                   f"{where}; a task gives its result once — keep the result in a variable", line)
            raise CompileError(
                f"{name} {how} on line {self.moved[name]}{where} and can no longer be used here. {fix}", line)
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

    def owned_value(self, e, hint, line, may_move_string=False, into=None):
        """Evaluate an expression whose result will be stored somewhere: the result must be owned.
        `into`: the variable the result is stored under (it cannot be stored inside itself)."""
        if e.kind == "var" and e.name in self.vars and is_heap(self.vars[e.name]):
            if into == e.name and self.vars[e.name] != "string":
                raise CompileError(f"cannot store {e.name} inside itself; store copy({e.name})", line)
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
                raise CompileError(f"{show(e)} belongs to the value it is part of and cannot be moved out of it. "
                                   f"Store copy({show(e)}) to have your own", line)
            v = self.dup_str(v)
        return v, t

    def move_out(self, src, line):
        if src == "$this":
            raise CompileError("$this cannot be moved or returned; return copy($this) for a separate value", line)
        if src in self.refs:
            raise CompileError(f"{src} is lent with & and cannot be moved or returned (the caller keeps it). "
                               f"Use copy({src})", line)
        if src in self.borrowed:
            self.check_writable(src, line, "moved or returned")
        if self.walked(src):
            raise CompileError(f"cannot move {src} while a foreach is looping over it", line)
        self.mutating(src, (), line, what="moved")
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
                self.emit(f"{r} = sub i64 0, {v}")
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
        if k == "field":
            return self.field_read(e)
        if k == "mcall":
            return self.method_call(e)
        if k == "new":
            return self.new_object(e)
        if k == "try":
            return self.try_expr(e, hint)
        if k == "go":
            if not self.go_ok:
                raise CompileError("a task started with go must be kept in a variable or a list and awaited, "
                                   "e.g. $t = go f(...); ... $x = await $t;  or awaited at once: await go f(...)",
                                   e.line)
            self.go_ok = False
            return self.go_expr(e)
        if k == "await":
            return self.await_expr(e)
        if k == "ref":
            raise CompileError("& goes only in front of an argument of a function or method that changes it, "
                               "e.g. discount(&$order)", e.line)
        raise CompileError(f"cannot use {k} here", e.line)

    # --- classes and paths
    def rt(self, t, line):
        """Resolve a type written in this file (class names as written) to its canonical form."""
        return resolve_type(t, self.modid, self.imports, line)

    def class_of(self, t, line, what):
        if not is_class(t):
            raise CompileError(f"{what} needs an object, but this is {t}", line)
        return CLASSES[t]

    def visible(self, ci, member, vis, line, kind):
        if vis == "private" and (self.cls is None or self.cls.key != ci.key):
            raise CompileError(f"{ci.name}::{member} is private: only the methods of {ci.name} can use "
                               f"{'it' if kind == 'property' else 'this method'}", line)

    def field_info(self, t, name, line):
        ci = self.class_of(t, line, f"->{name}")
        f = ci.fields.get(name)
        if f is None:
            m = " (it is a method: add parentheses)" if name in ci.methods else ""
            raise CompileError(f"class {ci.name} has no property ${name}{m}", line)
        self.visible(ci, "$" + name, f.vis, line, "property")
        return f

    def field_ptr(self, o, f):
        p = self.tmp()
        self.emit(f"{p} = getelementptr inbounds i8, ptr {o}, i64 {8 * f.index}")
        return p

    def ctor_check_read(self, e):
        """Inside __construct, a property without a default cannot be read before it is set."""
        if self.ctor_need and e.obj.kind == "var" and e.obj.name == "$this" and e.name in self.ctor_need \
                and f"$this->{e.name}" not in self.flow.assigned:
            raise CompileError(f"__construct() reads $this->{e.name} before setting it on every path", e.line)

    def obj_value(self, e):
        """The object pointer of a path's base, for reading (borrowed)."""
        if e.kind == "var":
            return self.use_var(e.name, e.line, raw=True)
        return self.borrow(e)

    def field_read(self, e):
        self.ctor_check_read(e)
        o, ot = self.obj_value(e.obj)
        f = self.field_info(ot, e.name, e.line)
        return self.load_at(self.field_ptr(o, f), f.type, "elem"), f.type, False

    def static_type(self, e):
        """The type of a path or call without generating code, or None when it cannot be told this way."""
        k = e.kind
        if k == "var":
            return self.vars.get(e.name)
        if k == "field":
            ot = self.static_type(e.obj)
            if ot and is_class(ot):
                f = CLASSES[ot].fields.get(e.name)
                return f.type if f else None
            return None
        if k in ("index", "append"):
            st = self.static_type(e.seq)
            if st and is_list(st):
                return elem_of(st)
            if st and is_map(st) and k == "index":
                return map_kv(st)[1]
            return None
        if k == "new":
            try:
                return self.rt(e.cls, e.line)
            except CompileError:
                return None
        if k == "try":
            return self.static_type(e.e)
        if k == "mcall":
            ot = self.static_type(e.obj)
            if ot and is_class(ot) and e.name in CLASSES[ot].methods:
                return CLASSES[ot].methods[e.name].ret
            return None
        if k == "call" and self.fkey(e, quiet=True):
            return self.mod.funcs[self.fkey(e, quiet=True)][1]
        return None

    def load_at(self, p, t, storage):
        """storage: 'var' (a variable: bool as i1), 'elem' (list item or property: bool as i8), 'slot' (map
        value: 8 bytes)."""
        if storage == "slot":
            return self.load_slot(p, t)
        if storage == "elem":
            return self.load_elem(p, t)
        r = self.tmp()
        self.emit(f"{r} = load {llty(t)}, ptr {p}")
        return r

    def store_at(self, p, v, t, storage):
        if storage == "slot":
            self.store_slot(p, v, t)
        elif storage == "elem":
            self.store_elem(p, v, t)
        else:
            self.emit(f"store {llty(t)} {v}, ptr {p}")

    def place(self, e, line, create=False):
        """The memory a path names, for changing it: (pointer, type, storage). Map entries are created only
        when `create` (the final step of an assignment); a missing key on the way panics."""
        k = e.kind
        if k == "var":
            self.require_assigned(e.name, line)
            if e.name in self.moved:
                self.use_var(e.name, line)              # reports the move
            return self.slot(e.name), self.vars[e.name], "var"
        if k == "field":
            o, ot = self.base_value(e.obj, line)
            f = self.field_info(ot, e.name, line)
            return self.field_ptr(o, f), f.type, "elem"
        if k == "index":
            b, bt = self.base_value(e.seq, line)
            if is_list(bt):
                iv, it = self.borrow(e.idx)
                if it != "int":
                    raise CompileError(f"list index must be int but got {it}", line)
                self.list_bounds(b, iv, line)
                et = elem_of(bt)
                return self.list_elem_ptr(b, et, iv), et, "elem"
            if is_map(bt):
                _, vt = map_kv(bt)
                key = self.map_key(bt, e.idx, line)
                p = self.call("ptr", "@pn_map_slot", f"ptr {b}", f"i64 {key}", f"i64 {int(create)}")
                if not create:
                    self.missing_check(p, line)
                return p, vt, "slot"
            raise CompileError(f"cannot index a value of type {bt}", line)
        raise CompileError("can only change a value stored in a variable, a list item, a map entry or a property",
                           line)

    def base_value(self, e, line):
        """The container a path step goes into, for changing it."""
        if e.kind == "var":
            return self.use_var(e.name, line, raw=True)
        if e.kind in ("field", "index"):
            if e.kind == "field":
                self.ctor_check_read(e)
            p, t, st = self.place(e, line)
            return self.load_at(p, t, st), t
        raise CompileError("can only change a value stored in a variable (this path starts at a function result)",
                           line)

    def missing_check(self, p, line):
        c, ok, bad = self.tmp(), self.newlabel("found"), self.newlabel("missing")
        self.emit(f"{c} = icmp ne ptr {p}, null")
        self.emit(f"br i1 {c}, label %{ok}, label %{bad}, !prof !10")
        self.place_label(bad)
        self.call("void", "@pn_map_missing", f"i32 {line}")
        self.emit("unreachable")
        self.place_label(ok)

    def exclusive(self, root, others, line, what):
        """While `root` is being changed by a call, the call's other arguments may not read it (directly or
        through a variable that borrows from it): the callee could free what they point at."""
        borrowers = [b for b, roots in self.borrow_vars.items() if any(r == root for r, _ in roots)]
        for o in others:
            for n in [root] + borrowers:
                if mentions(o, n):
                    raise CompileError(f"{what}, so the other arguments cannot use {n} at the same time. "
                                       f"Pass copy(...) of what they need, or read it into a variable first", line)

    def args_for(self, e, params, who):
        """Evaluate call arguments: read-only borrows, and &-arguments as the address of the caller's value."""
        if len(e.args) != len(params):
            raise CompileError(f"{who}() takes {len(params)} argument(s) but got {len(e.args)}", e.line)
        vals = []
        for i, (a, (pt, pn, ref)) in enumerate(zip(e.args, params)):
            if ref:
                if a.kind != "ref":
                    raise CompileError(f"{who}() can change its argument {pn}, so write {who}(&{show(a)})" if a.kind
                                       in ("var", "field", "index") else
                                       f"{who}() changes its argument {pn}: pass a variable with &, e.g. &$x", e.line)
                vals.append(f"ptr {self.ref_arg(a.e, pt, e, [x for j, x in enumerate(e.args) if j != i], who)}")
            else:
                if a.kind == "ref":
                    raise CompileError(f"{who}() does not change {pn} (it is not declared {pt} &{pn}); remove the &",
                                       e.line)
                v, t = self.borrow(a, pt)
                vals.append(f"{llty(pt)} {self.coerce(v, t, pt, e.line, f'argument {pn} of {who}()')}")
        return vals

    def ref_arg(self, a, want, call, others, who):
        root, props = path_root(a)
        if root is None or a.kind not in ("var", "field", "index"):
            raise CompileError("& needs a variable or a path into one, e.g. &$order or &$orders[0]", call.line)
        if a.kind == "var" and root == "$this":
            raise CompileError("$this cannot be lent with &; lend the property that changes, e.g. &$this->items",
                               call.line)
        t = self.static_type(a)
        if t is None:
            self.require_assigned(root, call.line)
        if t != want:
            raise CompileError(f"{who}() needs {want} for &{show(a)}, but it is {t}", call.line)
        if t == "bool" and a.kind != "var":
            raise CompileError("a bool inside a list, map or object cannot be lent with &; use a variable", call.line)
        self.mutating(root, props, call.line, at=call.order)
        self.exclusive(root, others, call.line, f"{who}() changes {show(a)}")
        p, _, _ = self.place(a, call.line)
        return p

    def method_call(self, e):
        ot = self.static_type(e.obj)
        root, props = path_root(e.obj)
        if ot is None or root is None:
            o, ot = self.borrow(e.obj)              # a function result: a temporary that may be changed
            root = None
        ci = self.class_of(ot, e.line, f"->{e.name}()")
        m = ci.methods.get(e.name)
        if m is None:
            hint = " (it is a property: drop the parentheses)" if e.name in ci.fields else ""
            raise CompileError(f"class {ci.name} has no method {e.name}(){hint}", e.line)
        if m.name == "__construct":
            raise CompileError("__construct() runs only through new", e.line)
        self.visible(ci, e.name + "()", m.vis, e.line, "method")
        if root is not None:
            if m.changing:
                self.mutating(root, props, e.line, at=e.order, what=f"changed by {e.name}()")
                self.exclusive(root, e.args, e.line, f"{e.name}() changes {show(e.obj)}")
            o, _ = self.obj_value(e.obj) if e.obj.kind != "var" or e.obj.name != "$this" else \
                self.use_var("$this", e.line)
        vals = self.args_for(e, m.params, f"{ci.name}::{e.name}")
        if m.throws:
            how = f"{show(e.obj)}->{e.name}(...)"
            return self.failing_call(m.ret, m.llname, [f"ptr {o}"] + vals, e.line, how), m.ret, is_heap(m.ret)
        r = self.call(llty(m.ret), m.llname, f"ptr {o}", *vals)
        return r, m.ret, is_heap(m.ret)

    # --- tasks: go / await (concurrency.md)
    def go_expr(self, e):
        c = e.call
        if c.kind == "mcall":
            raise CompileError("go works on functions for now: put the method call in a function and go that", e.line)
        if c.name in BUILTINS and getattr(c, "module", None) is None:
            raise CompileError(f"go starts one of your functions, not the built-in {c.name}()", e.line)
        key = self.fkey(c)
        if key is None:
            raise CompileError(f"unknown function {c.name}()", e.line)
        params, (_, ret), throws = self.mod.params[key], self.mod.funcs[key], self.mod.throws.get(key, False)
        if len(c.args) != len(params):
            raise CompileError(f"{c.name}() takes {len(params)} argument(s) but got {len(c.args)}", e.line)
        mark = len(self.temps)
        slots, roots, lent = [], set(), set()
        for i, (a, (pt, pn, ref)) in enumerate(zip(c.args, params)):
            if ref:
                if a.kind != "ref":
                    raise CompileError(f"{c.name}() can change its argument {pn}, so write go {c.name}(&...)", e.line)
                others = [x for j, x in enumerate(c.args) if j != i]
                slots.append(("ptr", self.ref_arg(a.e, pt, c, others, c.name)))
                rr = self.task_roots(a.e)
                roots |= rr
                lent |= {r for r, _ in rr}
            else:
                if a.kind == "ref":
                    raise CompileError(f"{c.name}() does not change {pn}; remove the &", e.line)
                v, t = self.borrow(a, pt)
                slots.append((llty(pt), self.coerce(v, t, pt, e.line, f"argument {pn} of {c.name}()")))
                if is_heap(pt):
                    roots |= self.task_roots(a)
        owned = self.temps[mark:]                   # temporaries made for the arguments: the task frees them
        del self.temps[mark:]
        frame = self.call("ptr", "@pn_obj_new", f"i64 {16 + 8 * (len(slots) + len(owned))}")
        for i, (ty, v) in enumerate(slots + [("ptr", v) for v, _ in owned]):
            p = self.tmp()
            self.emit(f"{p} = getelementptr inbounds i8, ptr {frame}, i64 {16 + 8 * i}")
            self.emit(f"store {ty} {v}, ptr {p}")
        tramp = self.mod.go_trampoline(key, [ty for ty, _ in slots], ret, throws, [t for _, t in owned])
        task = self.call("ptr", "@pn_go", f"ptr {tramp}", f"ptr {frame}")
        self.last_go = (roots, lent, throws)
        return task, f"Task<{ret}>", True

    def await_expr(self, e):
        op = e.e
        fails = False
        if op.kind == "go":
            self.go_ok = True
            try:
                task, tt, _ = self.expr(op)
            finally:
                self.go_ok = False
            fails = self.last_go[2]
            how = f"await go {op.call.name}(...)"
        else:
            tt = self.static_type(op)
            if tt is None or not is_task(tt):
                raise CompileError(f"await needs a task started with go, e.g. $t = go f(); ... await $t; "
                                   f"(this is {tt})", e.line)
            if op.kind != "var":
                raise CompileError("await a task variable ($t), or a list of them with await_all($tasks)", e.line)
            fails = self.task_fail.get(op.name, False)
            how = f"await {op.name}"
            task, tt = self.owned_value(op, None, e.line)
        ret = tt[5:-1]
        if fails:
            self.may_fail(e.line, how)
        frame = self.call("ptr", "@pn_await", f"ptr {task}")
        if fails:
            ep, err = self.tmp(), self.tmp()
            self.emit(f"{ep} = getelementptr inbounds i8, ptr {frame}, i64 8")
            self.emit(f"{err} = load ptr, ptr {ep}")
            self.error_branch(err, e.line, before=lambda: self.call("void", "@pn_obj_free", f"ptr {frame}"))
        if ret == "void":
            self.call("void", "@pn_obj_free", f"ptr {frame}")
            return None, "void", False
        v = self.tmp()
        self.emit(f"{v} = load {llty(ret)}, ptr {frame}")
        self.call("void", "@pn_obj_free", f"ptr {frame}")
        return v, ret, is_heap(ret)

    def tasks_done(self, line):
        """C1: a task started in this function is awaited before it returns (on every path)."""
        for n, t in self.vars.items():
            if holds_task(t) and n not in self.borrowed and self.flow.live:
                started = self.task_line.get(n, "?")
                if n in self.flow.assigned and n not in self.moved:
                    if self.is_main and line == self.line and isinstance(started, int):
                        line = started                      # the end of the program: point at the go
                    raise CompileError(f"the task in {n} (started on line {started}) is not awaited when "
                                       f"{'the program ends' if self.is_main else self.name + '() returns here'}. "
                                       f"Add " +
                                       (f"await_all({n})" if is_list(t) else f"await {n}") + " before returning",
                                       line)
                if n in self.flow.maybe:
                    raise CompileError(f"the task in {n} (started on line {started}) may not be awaited on every "
                                       f"path to this return", line)

    def new_object(self, e):
        key = self.rt(e.cls, e.line)
        if not is_class(key):
            raise CompileError(f"new needs a class, and {e.cls} is {key}", e.line)
        if key == "Error":
            raise CompileError('make an error with error(code, message), e.g. throw error(404, "not found");', e.line)
        ci = CLASSES[key]
        ctor = ci.methods.get("__construct")
        if ctor is None and e.args:
            raise CompileError(f"class {ci.name} has no __construct(), so new {ci.name}() takes no arguments", e.line)
        if ctor:
            self.visible(ci, "__construct()", ctor.vis, e.line, "method")
            vals = self.args_for(e, ctor.params, f"new {ci.name}")
        o = self.call("ptr", "@pn_obj_new", f"i64 {ci.size}")
        if any(f.default is not None for f in ci.fields.values()):
            self.call("void", f'@"init.{key}"', f"ptr {o}")
        if ctor and ctor.throws:
            self.may_fail(e.line, f"new {e.cls}(...)")
            err = self.call("ptr", ctor.llname, f"ptr {o}", *vals)
            self.error_branch(err, e.line, before=lambda: self.call("void", free_fn(key), f"ptr {o}"))
        elif ctor:
            self.call("void", ctor.llname, f"ptr {o}", *vals)
        return o, key, True

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
            if t == "string":
                return self.call("double", "@pn_str_to_float", f"ptr {v}"), "float", False
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
            ih = elem_of(hint) if hint and is_list(hint) else None
            for i in e.items:
                probe = self.static_type(i) if i.kind in ("var", "field", "index") else None
                if (probe and is_heap(probe)) or i.kind in ("listlit", "maplit", "new", "call", "mcall"):
                    vals.append(self.owned_value(i, ih, e.line) + (True,))     # the list owns what it holds
                else:
                    vals.append(self.borrow(i, ih) + (False,))
            types = {t for _, t, _ in vals}
            et = "float" if types == {"int", "float"} else (types.pop() if len(types) == 1 else None)
            if et is None:
                raise CompileError("list items must all have the same type "
                                   f"(got {', '.join(sorted({t for _, t, _ in vals}))})", e.line)
            if et in ("void", "db", "rows"):
                raise CompileError(f"a list cannot hold {et} values", e.line)
        elif hint and is_list(hint):
            et = elem_of(hint)
        else:
            raise CompileError("an empty [] needs a type, e.g.  list<int> $a = [];  or  map<string,int> $m = [];", e.line)
        r = self.call("ptr", "@pn_list_new", f"i64 {esize(et)}")
        for v, t, owned in vals:
            v = self.coerce(v, t, et, e.line, "list item")
            if et == "string" and not owned:
                v = self.dup_str(v)             # the list owns its strings
            self.call("void", "@pn_list_push", f"ptr {r}", f"i64 {esize(et)}", f"i64 {self.to_bits(v, et)}")
        return r, f"list<{et}>", True

    def to_bits(self, v, et):
        if et == "int":
            return v
        r = self.tmp()
        if et == "float":
            self.emit(f"{r} = bitcast double {v} to i64")
        elif is_heap(et):
            self.emit(f"{r} = ptrtoint ptr {v} to i64")
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
        self.emit(f"{p} = getelementptr inbounds {ell(et)}, ptr {d}, i64 {iv}")
        return p

    def load_elem(self, p, et):
        r = self.tmp()
        tb = f", !tbaa {ELEM_TBAA[et]}" if et in ELEM_TBAA else ""
        self.emit(f"{r} = load {ell(et)}, ptr {p}{tb}")
        if et == "bool":
            b = self.tmp()
            self.emit(f"{b} = icmp ne i8 {r}, 0")
            return b
        return r

    def store_elem(self, p, v, et):
        tb = f", !tbaa {ELEM_TBAA[et]}" if et in ELEM_TBAA else ""
        if et == "bool":
            z = self.tmp()
            self.emit(f"{z} = zext i1 {v} to i8")
            v = z
        self.emit(f"store {ell(et)} {v}, ptr {p}{tb}")

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
            if vt is None:                      # ints and floats mixed: the map holds floats (like [1, 2.5])
                probes = {self.expr_type_probe(ve) for _, ve in entries}
                if "float" in probes and probes <= {"int", "float", None}:
                    vt = "float"
            m = None
            for i, (ke, ve) in enumerate(entries):
                v, t = self.owned_value(ve, vt, e.line)
                if vt is None:
                    vt = t
                    if vt in ("void", "db", "rows"):
                        raise CompileError(f"a map cannot hold {vt} values", e.line)
                    mt = f"map<{kt},{vt}>"
                    m = self.call("ptr", "@pn_map_new", f"i64 {KIND[kt]}", f"i64 {kind_of(vt)}")
                elif m is None:
                    mt = f"map<{kt},{vt}>"
                    m = self.call("ptr", "@pn_map_new", f"i64 {KIND[kt]}", f"i64 {kind_of(vt)}")
                if t != vt and not (t == "int" and vt == "float"):
                    raise CompileError(
                        f"a map holds one value type: this literal is {mt} (from its first value), but a value "
                        f"here is {t}. For a JSON object with values of different types, declare a class with those "
                        f"properties and json_encode() an object of it (see the AI guide, Classes)", e.line)
                v = self.coerce(v, t, vt, e.line, "map value")
                key = self.map_key(mt, ke, e.line)
                p = self.call("ptr", "@pn_map_slot", f"ptr {m}", f"i64 {key}", "i64 1")
                if is_heap(vt):
                    old = self.load_slot(p, vt)
                    self.call("void", free_fn(vt), f"ptr {old}")
                self.store_slot(p, v, vt)
            return m, mt, True
        if not (hint and is_map(hint)):
            raise CompileError("an empty [] needs a type, e.g.  map<string,int> $m = [];", e.line)
        kt, vt = map_kv(hint)
        return self.call("ptr", "@pn_map_new", f"i64 {KIND[kt]}", f"i64 {kind_of(vt)}"), hint, True

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
        if e.kind == "call" and self.fkey(e, quiet=True):
            return self.mod.funcs[self.fkey(e, quiet=True)][1]
        if e.kind == "call" and e.name in ("strval", "substr", "trim", "strtolower", "strtoupper", "json_encode",
                                           "implode", "url_path", "query_param", "form_param", "json_string",
                                           "db_json", "db_string", "db_error"):
            return "string"
        return "int"

    def seq_of(self, e, line):
        if e.kind not in ("var", "call", "field", "index", "mcall"):
            raise CompileError("can only index a variable, a property or a function result", line)
        return self.borrow(e)

    def index_read(self, e):
        seq, st = self.seq_of(e.seq, e.line)
        if is_list(st):
            iv, it = self.borrow(e.idx)
            if it != "int":
                raise CompileError(f"list index must be int but got {it}"
                                   + (" (/ always gives a float: use intdiv($a, $b))" if it == "float" else ""), e.line)
            self.list_bounds(seq, iv, e.line)
            et = elem_of(st)
            return self.load_elem(self.list_elem_ptr(seq, et, iv), et), et, False
        if is_map(st):
            _, vt = map_kv(st)
            key = self.map_key(st, e.idx, e.line)
            p = self.call("ptr", "@pn_map_slot", f"ptr {seq}", f"i64 {key}", "i64 0")
            self.missing_check(p, e.line)
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
                raise CompileError(f"list index must be int but got {it}"
                                   + (" (/ always gives a float: use intdiv($a, $b))" if it == "float" else ""), e.line)
            ln, c = self.tmp(), self.tmp()
            self.emit(f"{ln} = load i64, ptr {seq}, !tbaa !1")
            self.emit(f"{c} = icmp ult i64 {iv}, {ln}")
        else:
            raise CompileError(f"?? needs a list or map, not {st}", e.line)
        if owns_ptr(vt):
            raise CompileError(f"?? works on items that are numbers, bools or strings; these are {vt}. Check with "
                               f"isset() first", e.line)
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
            if t != want and not (t == "int" and want == "float"):
                raise CompileError(f"both sides of ?: / ?? must have the same type (got {want} and {t}). "
                                   f"Convert one side, e.g. strval($n) or intval($s)", line)
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
            raise CompileError(f"both sides of ?: must have the same type (got {ta} and {tb}). "
                               f"Convert one side, e.g. strval($n) or intval($s)", e.line)
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
        if is_class(lt) or is_class(rt):
            raise CompileError(f"'{op}' does not work on objects; compare their properties, e.g. $a->id == $b->id",
                               e.line)
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
                # 64-bit wrap-around (defined in LLVM: no nsw flag). Panicking on overflow was tried and
                # cost up to 23% on integer loops, so it waits for check elision (int-overflow.md O3).
                self.emit(f"{r} = {dict(zip('+-*', ('add', 'sub', 'mul')))[op]} i64 {lv}, {rv}")
                return r, t, False
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

    def typed_arg(self, e, i, want):
        v, t = self.borrow(e.args[i])
        if t != want:
            raise CompileError(f"{e.name}() argument {i + 1} must be {want} but got {t}"
                               + (" — open one with $db = db_open(\"app.db\");" if want == "db" else ""), e.line)
        return v

    def sql_arg(self, e):
        """The SQL must be written in the program; values go through ? placeholders. That rules out SQL
        injection: nothing a client sends can become part of the SQL text."""
        def literal(n):
            return n.kind == "str" or (n.kind == "bin" and n.op == "." and literal(n.l) and literal(n.r))
        if not literal(e.args[1]):
            raise CompileError(
                f"{e.name}(): write the SQL as a string literal in the code and pass values with ? placeholders, "
                f'e.g. {e.name}($db, "SELECT * FROM t WHERE id = ?", [strval($id)]). SQL built from variables could '
                f"let a client inject SQL", e.line)
        return self.str_arg(e, 1)

    def params_arg(self, e, i):
        v, t = self.borrow(e.args[i], "list<string>")
        if not is_list(t) or elem_of(t) not in ("int", "float", "string"):
            raise CompileError(f"{e.name}() argument {i + 1} must be a list of values for the ? placeholders "
                               f'(list<string>, list<int> or list<float>), e.g. ["tea", "2"]; got {t}', e.line)
        return v, KIND[elem_of(t)]

    def db_builtin(self, e):
        name = e.name
        if name == "db_open":
            self.argc(e, 1)
            return self.call("ptr", "@pn_db_open", f"ptr {self.str_arg(e, 0)}"), "db", True
        if name in ("db_exec", "db_query", "db_json"):
            self.argc(e, 3)
            d = self.typed_arg(e, 0, "db")
            sql = self.sql_arg(e)
            ps, kind = self.params_arg(e, 2)
            fn = f"@pn_{name}"
            if name == "db_exec":
                return self.call("i64", fn, f"ptr {d}", f"ptr {sql}", f"ptr {ps}", f"i64 {kind}"), "int", False
            ret = "rows" if name == "db_query" else "string"
            return self.call("ptr", fn, f"ptr {d}", f"ptr {sql}", f"ptr {ps}", f"i64 {kind}"), ret, True
        if name == "db_next":
            self.argc(e, 1)
            return self.i64_to_bool(self.call("i64", "@pn_db_next", f"ptr {self.typed_arg(e, 0, 'rows')}")), "bool", False
        if name in ("db_int", "db_float", "db_string", "db_is_null"):
            self.argc(e, 2)
            r, col = self.typed_arg(e, 0, "rows"), self.str_arg(e, 1)
            fn = f"@pn_{name}"
            if name == "db_int":
                return self.call("i64", fn, f"ptr {r}", f"ptr {col}"), "int", False
            if name == "db_float":
                return self.call("double", fn, f"ptr {r}", f"ptr {col}"), "float", False
            if name == "db_string":
                return self.call("ptr", fn, f"ptr {r}", f"ptr {col}"), "string", True
            return self.i64_to_bool(self.call("i64", fn, f"ptr {r}", f"ptr {col}")), "bool", False
        if name in ("db_last_id", "db_error"):
            self.argc(e, 1)
            d = self.typed_arg(e, 0, "db")
            if name == "db_last_id":
                return self.call("i64", "@pn_db_last_id", f"ptr {d}"), "int", False
            return self.call("ptr", "@pn_db_error", f"ptr {d}"), "string", True
        raise CompileError(f"unknown function {name}()", e.line)

    def int_arg(self, e, i):
        v, t = self.borrow(e.args[i])
        if t != "int":
            raise CompileError(f"{e.name}() argument {i + 1} must be an int but got {t}", e.line)
        return v

    def fkey(self, e, quiet=False):
        """The function-table key for a call: name in this file, or module::name for an imported module."""
        module = getattr(e, "module", None)
        if module is None:
            k = e.name if not self.modid else f"{self.modid}::{e.name}"
            return k if k in self.mod.funcs else None
        if module not in self.imports:
            if quiet:
                return None
            raise CompileError(f"{module}::{e.name}(): there is no `import {module};` in this file", e.line)
        target, file = self.imports[module]
        k = f"{target}::{e.name}" if target else e.name
        if k not in self.mod.funcs:
            if quiet:
                return None
            raise CompileError(f"{module}::{e.name}(): {os.path.basename(file)} has no function {e.name}()", e.line)
        if not self.mod.exported.get(k) and not quiet:
            raise CompileError(f"{module}::{e.name}() is not exported. Add `export` before `function {e.name}` "
                               f"in {os.path.basename(file)} to let other files call it", e.line)
        return k

    def call_expr(self, e, hint):
        name, args = e.name, e.args
        if name in BUILTINS and getattr(e, "module", None) is None:
            return self.builtin(e, hint)
        key = self.fkey(e)
        if key is None:
            hint = PHP_FUNCTION_HINTS.get(name)
            raise CompileError(f"unknown function {name}()" + (f": {hint}" if hint else
                               ". See the built-in list in the AI guide, github.com/pin-language/docs"), e.line)
        _, ret = self.mod.funcs[key]
        vals = self.args_for(e, self.mod.params[key], name)   # heap values are lent to the callee
        if self.mod.throws.get(key):
            return self.failing_call(ret, f'@"f.{key}"', vals, e.line, f"{name}(...)"), ret, is_heap(ret)
        r = self.call(llty(ret), f'@"f.{key}"', *vals)
        return r, ret, is_heap(ret)

    # --- errors (error-handling.md)
    def may_fail(self, line, how):
        if self.try_ok or (self.try_stack and self.try_stack[-1].block):
            self.fail_count += 1
            return
        if how.startswith("await"):
            raise CompileError(f"{how} can fail (the task's function is declared throws). Write try {how}, or "
                               f"handle the error: try {how} catch ($e) {{ ... }}", line)
        raise CompileError(f"{how.split('(')[0]}() can fail. Write try {how}, or handle the error: "
                           f"try {how} catch ($e) {{ ... }}", line)

    def failing_call(self, ret, fn, args, line, how):
        """Call a function declared throws and send its error where it belongs."""
        self.may_fail(line, how)
        r = self.tmp()
        self.emit(f"{r} = call {fn_llret(ret, True)} {fn}({', '.join(args)})")
        if ret == "void":
            v, err = None, r
        else:
            v, err = self.tmp(), self.tmp()
            self.emit(f"{v} = extractvalue {fn_llret(ret, True)} {r}, 0")
            self.emit(f"{err} = extractvalue {fn_llret(ret, True)} {r}, 1")
        self.error_branch(err, line)
        return v

    def error_branch(self, err, line, before=None):
        """If `err` is not null: run `before()` (e.g. free a half-built object), then route the error."""
        c, ok, bad = self.tmp(), self.newlabel("noerr"), self.newlabel("err")
        self.emit(f"{c} = icmp ne ptr {err}, null")
        self.emit(f"br i1 {c}, label %{bad}, label %{ok}, !prof !11")
        self.place_label(bad)
        if before:
            before()
        self.route_error(err, line)
        self.place_label(ok)

    def route_error(self, err, line):
        """Jump to the innermost catch (cleaning up what it does not keep), or leave the function with it."""
        if self.try_stack:
            ctx = self.try_stack[-1]
            if not ctx.block and ctx.stash_mark < len(self.temp_stack):
                raise CompileError("an error inside the catch of a try expression cannot go to an outer try "
                                   "expression; use a try { ... } catch ($e) { ... } block instead", line)
            self.free_temps(self.temps[ctx.temp_mark:])
            for group, _ in self.temp_stack[ctx.stash_mark:]:
                self.free_temps(group)
            for group in self.pending[ctx.pending_mark:]:
                self.free_temps(group)
            ctx.errs.append((err, self.label))
            ctx.flows.append(self.flow.copy())
            self.emit(f"br label %{ctx.label}")
            return
        if self.is_main:                    # escaping the top level: print it and exit 1 (E-R6)
            self.call("void", "@pn_uncaught", f"ptr {err}")
            self.emit("unreachable")
            return
        if not self.throws:
            raise CompileError(f"{self.name}() is not declared throws, so an error cannot leave it. Add throws "
                               f"after the return type (: {self.ret} throws), or handle it with catch", line)
        self.free_temps(self.temps)
        for group, _ in self.temp_stack:
            self.free_temps(group)
        self.free_pending()
        self.free_owned()
        if self.ret == "void":
            self.emit(f"ret ptr {err}")
        else:
            r = self.tmp()
            self.emit(f"{r} = insertvalue {fn_llret(self.ret, True)} zeroinitializer, ptr {err}, 1")
            self.emit(f"ret {fn_llret(self.ret, True)} {r}")

    def emit_ret(self, v=None):
        if self.is_main:
            self.emit("ret i32 0")
        elif not self.throws:
            self.emit("ret void" if v is None else f"ret {llty(self.ret)} {v}")
        elif v is None:
            self.emit("ret ptr null")
        else:
            rt = fn_llret(self.ret, True)
            a, b = self.tmp(), self.tmp()
            self.emit(f"{a} = insertvalue {rt} undef, {llty(self.ret)} {v}, 0")
            self.emit(f"{b} = insertvalue {rt} {a}, ptr null, 1")
            self.emit(f"ret {rt} {b}")

    def bind_error(self, var, err, line):
        if var is None:
            self.call("void", free_fn("Error"), f"ptr {err}")
            return
        if var in self.vars and self.vars[var] != "Error":
            raise CompileError(f"{var} is already {self.vars[var]}; name the caught error differently", line)
        self.declare(var, "Error")
        self.check_writable(var, line)
        old = self.tmp()                    # the slot holds null or an Error this function owns
        self.emit(f"{old} = load ptr, ptr {self.slot(var)}")
        self.call("void", free_fn("Error"), f"ptr {old}")
        self.emit(f"store ptr {err}, ptr {self.slot(var)}")
        self.moved.pop(var, None)
        self.flow.maybe.discard(var)
        self.flow.assigned.add(var)

    def can_propagate(self, line, what):
        if not (self.try_stack or self.throws or self.is_main):
            raise CompileError(f"{self.name}() is not declared throws, so {what}. Add throws after the return "
                               f"type (: {self.ret} throws), or handle the error: try ... catch ($e) {{ ... }}", line)

    def try_expr(self, e, hint):
        before = self.fail_count
        if not e.catch:
            self.can_propagate(e.line, "try cannot pass the error on")
            self.try_ok += 1
            try:
                r = self.expr(e.e, hint)
            finally:
                self.try_ok -= 1
            if self.fail_count == before:
                raise CompileError("nothing after this try can fail; remove try", e.line)
            return r
        ctx = Catch(self.newlabel("catch"), False, len(self.temps), len(self.pending), len(self.temp_stack))
        self.try_stack.append(ctx)
        self.try_ok += 1
        try:
            v, t, owned = self.expr(e.e, hint)
        finally:
            self.try_ok -= 1
            self.try_stack.pop()
        if self.fail_count == before:
            raise CompileError("nothing in this try can fail; remove try ... catch", e.line)
        if t != "void" and is_heap(t) and not owned:
            if t != "string":
                raise CompileError(f"try ... catch must give its own value; use copy({show(e.e)})", e.line)
            v = self.dup_str(v)
        self.flush_temps(ctx.temp_mark)
        ok_end, ok_flow = self.label, self.flow
        merge = self.newlabel("tryend")
        self.emit(f"br label %{merge}")
        cv, c_end, c_live = self.catch_body(ctx, e.var, e.cbody, e.cval, t, e.line)
        self.place_label(merge)
        self.flow = Flow.merge([ok_flow] + ([self.catch_flow] if c_live else []), ok_flow)
        if t == "void":
            return None, "void", False
        r = self.tmp()
        arms = f"[ {v}, %{ok_end} ]" + (f", [ {cv}, %{c_end} ]" if c_live else "")
        self.emit(f"{r} = phi {llty(t)} {arms}")
        return r, t, is_heap(t)

    def catch_body(self, ctx, var, body, val, want, line):
        """Generate a catch block; returns (value, its block, whether it falls through)."""
        self.place_label(ctx.label)
        err = self.tmp()
        self.emit(f"{err} = phi ptr " + ", ".join(f"[ {v}, %{b} ]" for v, b in ctx.errs))
        self.flow = Flow.merge(ctx.flows, self.flow)
        saved_ok, self.try_ok = self.try_ok, 0
        self.temp_stack.append((self.temps, len(self.loops)))
        self.temps = []
        try:
            self.bind_error(var, err, line)
            self.stmt(body)
            cv = None
            if val is not None and self.flow.live:
                if want in (None, "void"):
                    v, t, owned = self.expr(val)
                    if owned:
                        self.temps.append((v, t))
                else:
                    cv, ct, owned = self.expr(val, want)
                    if ct != want and not (ct == "int" and want == "float"):
                        raise CompileError(f"the catch block gives {ct}, but the try gives {want}", line)
                    cv = self.coerce(cv, ct, want, line, "catch value")
                    if is_heap(want) and not owned:
                        if want != "string":
                            raise CompileError("the catch value must be its own value; use copy()", line)
                        cv = self.dup_str(cv)
                self.flush_temps()
        finally:
            self.temps, _ = self.temp_stack.pop()
            self.try_ok = saved_ok
        live = self.flow.live and not self.term
        if live and want not in (None, "void") and cv is None:
            raise CompileError(f"this catch block must return, throw, or end with a value of type {want} "
                               f"(e.g. {'0' if want == 'int' else chr(34) * 2 if want == 'string' else '...'};)", line)
        self.catch_flow = self.flow
        return cv, self.label, live          # the caller's place_label() adds the jump to after the try

    def builtin(self, e, hint):
        name, args = e.name, e.args
        if name == "error":
            self.argc(e, 2)
            code = self.int_arg(e, 0)
            msg, mt = self.owned_value(args[1], "string", e.line)
            if mt != "string":
                raise CompileError(f"error() argument 2 must be a string (the message) but got {mt}", e.line)
            o = self.call("ptr", "@pn_obj_new", "i64 16")
            self.emit(f"store i64 {code}, ptr {o}")
            p = self.tmp()
            self.emit(f"{p} = getelementptr inbounds i8, ptr {o}, i64 8")
            self.emit(f"store ptr {msg}, ptr {p}")
            return o, "Error", True
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
                raise CompileError(f"fill() value must be int, float, bool or string but got {vt}", e.line)
            if vt == "string":
                return self.call("ptr", "@pn_list_fill_str", f"i64 {n}", f"ptr {v}"), "list<string>", True
            r = self.call("ptr", "@pn_list_fill", f"i64 {n}", f"i64 {ELEM_SIZE[vt]}", f"i64 {self.to_bits(v, vt)}")
            return r, f"list<{vt}>", True
        if name == "copy":
            self.argc(e, 1)
            v, t = self.borrow(args[0])
            if t == "string":
                return self.dup_str(v), t, not v.startswith("@.L")
            if is_list(t) and not owns_ptr(elem_of(t)) and elem_of(t) != "string":
                return self.call("ptr", "@pn_list_copy", f"ptr {v}", f"i64 {esize(elem_of(t))}"), t, True
            if owns_ptr(t):
                bad = uncopyable(t)
                if bad:
                    raise CompileError(f"copy() cannot copy {t}: it holds {bad}, which cannot be copied", e.line)
                return self.call("ptr", copy_fn(t), f"ptr {v}"), t, True
            raise CompileError(f"copy() needs a list, map, object or string but got {t}", e.line)
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
        if name == "explode":
            self.argc(e, 2)
            sep, s = self.str_arg(e, 0), self.str_arg(e, 1)
            return self.call("ptr", "@pn_explode", f"ptr {sep}", f"ptr {s}"), "list<string>", True
        if name == "implode":
            self.argc(e, 2)
            g = self.str_arg(e, 0)
            v, t = self.borrow(args[1])
            if not is_list(t) or elem_of(t) not in ("int", "float", "string"):
                raise CompileError(f"implode() needs a list<string>, list<int> or list<float> but got {t}", e.line)
            return self.call("ptr", "@pn_implode", f"ptr {g}", f"ptr {v}", f"i64 {KIND[elem_of(t)]}"), "string", True
        if name in ("sort", "rsort"):
            self.argc(e, 1)
            a = args[0]
            root, props = path_root(a)
            if root is None or a.kind not in ("var", "field", "index"):
                raise CompileError(f"{name}() sorts a list variable in place, e.g. {name}($xs);", e.line)
            t = self.static_type(a) or self.use_var(root, e.line)[1]
            if not is_list(t):
                raise CompileError(f"{name}() needs a list but {show(a)} is {t}", e.line)
            if elem_of(t) not in KIND:
                raise CompileError(f"{name}() sorts lists of int, float, bool or string; {show(a)} is {t}", e.line)
            self.mutating(root, props, e.line, at=e.order, what="sorted")
            v, _ = self.base_value(a, e.line)
            self.call("void", "@pn_list_sort", f"ptr {v}", f"i64 {KIND[elem_of(t)]}", f"i64 {int(name == 'rsort')}")
            return None, "void", False
        if name == "url_path":
            self.argc(e, 1)
            return self.call("ptr", "@pn_url_path", f"ptr {self.str_arg(e, 0)}"), "string", True
        if name in ("query_has", "form_has"):
            self.argc(e, 2)
            src, key = self.str_arg(e, 0), self.str_arg(e, 1)
            return self.i64_to_bool(self.call("i64", f"@pn_{name}", f"ptr {src}", f"ptr {key}")), "bool", False
        if name == "floatval":
            self.argc(e, 1)
            v, t = self.borrow(args[0])
            if t == "string":
                return self.call("double", "@pn_str_to_float", f"ptr {v}"), "float", False
            return self.coerce(v, t, "float", e.line, "floatval()"), "float", False
        if name in ("query_param", "form_param"):
            self.argc(e, 2)
            src, key = self.str_arg(e, 0), self.str_arg(e, 1)
            return self.call("ptr", f"@pn_{name}", f"ptr {src}", f"ptr {key}"), "string", True
        if name in ("json_has", "json_string", "json_int", "json_float", "json_bool"):
            self.argc(e, 2)
            src, key = self.str_arg(e, 0), self.str_arg(e, 1)
            fn = "@pn_jget_" + name[5:]
            if name == "json_string":
                return self.call("ptr", fn, f"ptr {src}", f"ptr {key}"), "string", True
            if name == "json_float":
                return self.call("double", fn, f"ptr {src}", f"ptr {key}"), "float", False
            if name == "json_int":
                return self.call("i64", fn, f"ptr {src}", f"ptr {key}"), "int", False
            return self.i64_to_bool(self.call("i64", fn, f"ptr {src}", f"ptr {key}")), "bool", False
        if name.startswith("db_"):
            return self.db_builtin(e)
        if name == "json_encode":
            self.argc(e, 1)
            v, t = self.borrow(args[0])
            if owns_ptr(t) and (is_class(t) or owns_ptr(elem_of(t) if is_list(t) else map_kv(t)[1])):
                bad = unjsonable(t)
                if bad:
                    raise CompileError(f"json_encode() cannot write {bad}; make that property private to leave "
                                       f"it out", e.line)
                d = self.call("ptr", "@pn_str_new", "i64 64")
                return self.call("ptr", json_fn(t), f"ptr {d}", f"ptr {v}"), "string", True
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
                    raise CompileError(f"list index must be int but got {it}"
                                   + (" (/ always gives a float: use intdiv($a, $b))" if it == "float" else ""), e.line)
                ln, c = self.tmp(), self.tmp()
                self.emit(f"{ln} = load i64, ptr {seq}, !tbaa !1")
                self.emit(f"{c} = icmp ult i64 {iv}, {ln}")
                return c, "bool", False
            raise CompileError(f"{name}() needs a list or map but got {st}", e.line)
        if name == "unset":
            self.argc(e, 1)
            a = args[0]
            root, props = path_root(a)
            if a.kind != "index" or root is None:
                raise CompileError("unset() needs a map entry, e.g. unset($m[$k])", e.line)
            mt = self.static_type(a.seq)
            if mt is None or not is_map(mt):
                raise CompileError(f"unset() works on maps; {show(a.seq)} is {mt}. To drop items from a list, "
                                   f"build a new list without them in a loop", e.line)
            self.mutating(root, props, e.line, at=e.order)
            m, _ = self.base_value(a.seq, e.line)
            key = self.map_key(mt, a.idx, e.line)
            vt = map_kv(mt)[1]
            if owns_ptr(vt):
                self.call("void", "@pn_map_del_with", f"ptr {m}", f"i64 {key}", f"ptr {item_fn('free', vt)}")
            else:
                self.call("void", "@pn_map_del", f"ptr {m}", f"i64 {key}")
            return None, "void", False
        if name == "await_all":
            self.argc(e, 1)
            a = args[0]
            lt = self.static_type(a)
            if lt is None or not is_list(lt) or not is_task(elem_of(lt)):
                raise CompileError(f"await_all() needs a list of tasks (list<Task<T>>), not {lt}", e.line)
            if a.kind != "var":
                raise CompileError("await_all() takes a list variable: await_all($tasks)", e.line)
            fails = self.task_fail.get(a.name, False)
            if fails:
                self.may_fail(e.line, f"await_all({a.name})")
            lst, _ = self.owned_value(a, None, e.line)
            rt = elem_of(lt)[5:-1]
            errslot = f"%v.__err{self.n}"
            self.n += 1
            self.allocas.append(f"  {errslot} = alloca ptr")
            self.emit(f"store ptr null, ptr {errslot}")
            fr = item_fn("free", rt) if is_heap(rt) else "null"
            res = self.call("ptr", "@pn_await_all", f"ptr {lst}", f"i64 {esize(rt) if rt != 'void' else 8}",
                            f"ptr {fr}", f"ptr {item_fn('free', 'Error')}", f"ptr {errslot}")
            if fails:
                err = self.tmp()
                self.emit(f"{err} = load ptr, ptr {errslot}")
                self.error_branch(err, e.line)
            if rt == "void":
                self.call("void", "@pn_list_free", f"ptr {res}")
                return None, "void", False
            return res, f"list<{rt}>", True
        if name == "json_raw":
            self.argc(e, 2)
            src, key = self.str_arg(e, 0), self.str_arg(e, 1)
            return self.call("ptr", "@pn_jget_raw", f"ptr {src}", f"ptr {key}"), "string", True
        if name == "json_at":
            self.argc(e, 2)
            src = self.str_arg(e, 0)
            return self.call("ptr", "@pn_jget_at", f"ptr {src}", f"i64 {self.int_arg(e, 1)}"), "string", True
        if name == "json_len":
            self.argc(e, 1)
            return self.call("i64", "@pn_jget_len", f"ptr {self.str_arg(e, 0)}"), "int", False
        if name == "read_line":
            self.argc(e, 0)
            return self.failing_call("string", "@pn_read_line", [], e.line, "read_line()"), "string", True
        if name in ("http_get", "http_post", "http_stream"):
            n = {"http_get": 3, "http_post": 4, "http_stream": 4}[name]
            self.argc(e, n)
            url = self.str_arg(e, 0)
            hv, ht = self.borrow(args[1], "map<string,string>")
            if ht != "map<string,string>":
                raise CompileError(f"{name}() argument 2 is the request headers, a map<string,string> (use [] for "
                                   f"none), not {ht}", e.line)
            if name == "http_get":
                a = [f"ptr {url}", f"ptr {hv}", f"i64 {self.int_arg(e, 2)}"]
            elif name == "http_post":
                a = [f"ptr {url}", f"ptr {hv}", f"ptr {self.str_arg(e, 2)}", f"i64 {self.int_arg(e, 3)}"]
            else:
                a = [f"ptr {url}", f"ptr {hv}", f"i64 {self.int_arg(e, 2)}", f"i64 {self.int_arg(e, 3)}"]
            ret = "stream" if name == "http_stream" else "string"
            return self.failing_call(ret, f"@pn_{name}", a, e.line, f"{name}(...)"), ret, True
        if name in ("stream_next", "stream_last_id"):
            self.argc(e, 2 if name == "stream_next" else 1)
            sv = self.typed_arg(e, 0, "stream")
            if name == "stream_last_id":
                return self.call("i64", "@pn_stream_last_id", f"ptr {sv}"), "int", False
            return self.failing_call("string", "@pn_stream_next", [f"ptr {sv}", f"i64 {self.int_arg(e, 1)}"], e.line,
                                     "stream_next(...)"), "string", True
        if name == "run":
            self.argc(e, 3)
            cmd = self.str_arg(e, 0)
            lv, lt = self.borrow(args[1], "list<string>")
            if lt != "list<string>":
                raise CompileError(f"run() argument 2 is the program's arguments, a list<string> ([] for none), "
                                   f"not {lt}", e.line)
            return self.failing_call("string", "@pn_run", [f"ptr {cmd}", f"ptr {lv}", f"i64 {self.int_arg(e, 2)}"],
                                     e.line, "run(...)"), "string", True
        if name == "hub_publish":
            self.argc(e, 2)
            t_, m = self.str_arg(e, 0), self.str_arg(e, 1)
            self.call("void", "@pn_hub_publish", f"ptr {t_}", f"ptr {m}")
            return None, "void", False
        if name in ("hub_subscribe", "hub_resume"):
            self.argc(e, 1 if name == "hub_subscribe" else 2)
            t_ = self.str_arg(e, 0)
            if name == "hub_subscribe":
                return self.call("ptr", "@pn_hub_subscribe", f"ptr {t_}"), "sub", True
            return self.call("ptr", "@pn_hub_resume", f"ptr {t_}", f"i64 {self.int_arg(e, 1)}"), "sub", True
        if name in ("hub_next", "hub_last_id"):
            self.argc(e, 2 if name == "hub_next" else 1)
            sv = self.typed_arg(e, 0, "sub")
            if name == "hub_last_id":
                return self.call("i64", "@pn_hub_last_id", f"ptr {sv}"), "int", False
            ms = self.int_arg(e, 1)
            v = self.failing_call("string", "@pn_hub_next", [f"ptr {sv}", f"i64 {ms}"], e.line, "hub_next(...)")
            return v, "string", True
        if name in ("sse_start", "sse_send", "sse_send_id", "sse_comment"):
            want = {"sse_start": 0, "sse_send": 1, "sse_send_id": 2, "sse_comment": 1}[name]
            self.argc(e, want)
            a = []
            if name == "sse_send_id":
                a = [f"i64 {self.int_arg(e, 0)}", f"ptr {self.str_arg(e, 1)}"]
            elif want == 1:
                a = [f"ptr {self.str_arg(e, 0)}"]
            self.failing_call("void", f"@pn_{name}", a, e.line, f"{name}(...)")
            return None, "void", False
        if name == "await_timeout":
            self.argc(e, 2)
            op = args[0]
            tt = self.static_type(op)
            if op.kind != "var" or tt is None or not is_task(tt):
                raise CompileError("await_timeout() takes a task variable and milliseconds: try await_timeout($t, 500)",
                                   e.line)
            fails = self.task_fail.get(op.name, False)
            self.may_fail(e.line, f"await_timeout({op.name}, ...)")    # running out of time is an error
            ms = self.int_arg(e, 1)
            task, _ = self.owned_value(op, None, e.line)
            rt = tt[5:-1]
            fr = item_fn("free", rt) if is_heap(rt) else "null"
            frame = self.call("ptr", "@pn_await_timeout", f"ptr {task}", f"i64 {ms}", f"ptr {fr}",
                              f"ptr {item_fn('free', 'Error')}")
            late = self.tmp()
            self.emit(f"{late} = icmp eq ptr {frame}, null")
            slow, ok = self.newlabel("late"), self.newlabel("intime")
            self.emit(f"br i1 {late}, label %{slow}, label %{ok}, !prof !11")
            self.place_label(slow)
            self.route_error(self.call("ptr", "@pn_timeout_error"), e.line)
            self.place_label(ok)
            if fails:
                ep, err = self.tmp(), self.tmp()
                self.emit(f"{ep} = getelementptr inbounds i8, ptr {frame}, i64 8")
                self.emit(f"{err} = load ptr, ptr {ep}")
                self.error_branch(err, e.line, before=lambda: self.call("void", "@pn_obj_free", f"ptr {frame}"))
            if rt == "void":
                self.call("void", "@pn_obj_free", f"ptr {frame}")
                return None, "void", False
            v = self.tmp()
            self.emit(f"{v} = load {llty(rt)}, ptr {frame}")
            self.call("void", "@pn_obj_free", f"ptr {frame}")
            return v, rt, is_heap(rt)
        if name == "sleep_ms":
            self.argc(e, 1)
            self.call("void", "@pn_sleep", f"i64 {self.int_arg(e, 0)}")
            return None, "void", False
        if name == "request_header":
            self.argc(e, 1)
            return self.call("ptr", "@pn_request_header", f"ptr {self.str_arg(e, 0)}"), "string", True
        if name == "request_has_header":
            self.argc(e, 1)
            return self.i64_to_bool(self.call("i64", "@pn_request_has_header", f"ptr {self.str_arg(e, 0)}")), "bool", False
        if name == "response_header":
            self.argc(e, 2)
            n, v = self.str_arg(e, 0), self.str_arg(e, 1)
            self.call("void", "@pn_response_header", f"ptr {n}", f"ptr {v}")
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
            if h.kind != "str" or self.mod.funcs.get(h.val) != sig:
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
        self.at = getattr(s, "order", self.at)
        if k == "decl":
            vt = self.rt(s.vtype, s.line)
            if s.name in self.vars and self.vars[s.name] != vt:
                raise CompileError(f"{s.name} is already {self.vars[s.name]}; a variable keeps one type", s.line)
            if not self.is_borrow(s.name, s.val):
                self.declare(s.name, vt)
            self.assign_var(s.name, s.val, s.line, want=vt)
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
                        raise CompileError(f"cannot echo {'an object ' + t if is_class(t) else 'a ' + t} (use json_encode())", s.line)
        elif k == "if":
            self.if_(s)
        elif k == "while":
            self.loop(s.cond, None, s.body, s)
        elif k == "for":
            if s.init:
                self.stmt(s.init)
                self.at = s.order
            self.loop(s.cond, s.step, s.body, s)
        elif k == "foreach":
            self.foreach(s)
        elif k in ("break", "continue"):
            if not self.loops:
                raise CompileError(f"'{k}' outside a loop", s.line)
            self.flush_temps()
            for group, depth in self.temp_stack:
                if depth >= len(self.loops):     # the statement around this catch is inside the loop left
                    self.free_temps(group)
            lp = self.loops[-1]
            (lp.breaks if k == "break" else lp.conts).append(self.flow.copy())
            self.flow.live = False
            self.emit(f"br label %{lp.brk if k == 'break' else lp.cont}")
        elif k == "return":
            self.return_(s)
        elif k == "throw":
            v, t = self.owned_value(s.val, "Error", s.line)
            if t != "Error":
                raise CompileError(f'throw needs an Error, e.g. throw error(404, "not found"); — not {t}', s.line)
            self.fail_count += 1
            self.can_propagate(s.line, "throw is not allowed here")
            self.route_error(v, s.line)
            self.flow.live = False
        elif k == "trystmt":
            ctx = Catch(self.newlabel("catch"), True, len(self.temps), len(self.pending), len(self.temp_stack))
            before = self.fail_count
            self.try_stack.append(ctx)
            try:
                self.stmt(s.body)
            finally:
                self.try_stack.pop()
            if self.fail_count == before:
                raise CompileError("nothing in this try block can fail; remove try { } catch", s.line)
            ok_flow = self.flow
            end = self.newlabel("tryend")
            if not self.term:
                self.emit(f"br label %{end}")
            self.catch_body(ctx, s.var, s.cbody, None, None, s.line)
            self.place_label(end)
            self.flow = Flow.merge([ok_flow, self.catch_flow], ok_flow)
        else:
            raise CompileError(f"unknown statement {k}", s.line)
        self.flush_temps()

    def is_borrow(self, name, val_e):
        """$u = $users[0] / $items = $o->items: a list, map or object read through a path is borrowed (D2)."""
        if val_e.kind not in ("field", "index"):
            return False
        vt = self.static_type(val_e)
        return bool(vt and owns_ptr(vt) and (name not in self.vars or name in self.borrow_vars))

    def assign_borrow(self, name, val_e, line, want=None):
        root, props = path_root(val_e)
        if root is None:
            raise CompileError(f"{show(val_e)} is part of a temporary value that is gone after this line; store "
                               f"the function result in a variable first, or use copy({show(val_e)})", line)
        vt = self.static_type(val_e)
        if want and vt != want:
            raise CompileError(f"{name} is declared {want} but {show(val_e)} is {vt}", line)
        if name in self.vars and self.vars[name] != vt:
            raise CompileError(f"{name} is already {self.vars[name]}; a variable keeps one type", line)
        roots = set(self.borrow_vars[root]) if root in self.borrow_vars else {(root, props)}
        for r, _ in roots:
            if r in self.loop_scope and name in self.loop_scope[r]:
                raise CompileError(f"{name} exists outside the foreach that owns {r}, so it cannot borrow from "
                                   f"{r} (that borrow would outlive the loop). Use a variable first assigned "
                                   f"inside the loop, or {name} = copy({show(val_e)})", line)
        v, _ = self.borrow(val_e)
        self.declare(name, vt, borrowed=True)
        self.borrow_vars.setdefault(name, set()).update(roots)
        self.emit(f"store ptr {v}, ptr {self.slot(name)}")
        self.moved.pop(name, None)
        self.flow.maybe.discard(name)
        self.flow.assigned.add(name)

    def task_roots(self, a):
        """What a task argument borrows: the owning variables behind it."""
        root, props = path_root(a) if a.kind in ("var", "field", "index") else (None, ())
        if root is None:
            return set()
        out, todo, seen = set(), [(root, props)], set()
        while todo:                                 # follow borrowed and foreach variables to their owners
            r, p = todo.pop()
            if r in seen:
                continue
            seen.add(r)
            if r in self.borrow_vars:
                todo += list(self.borrow_vars[r])
            elif r in self.foreach_root:
                if self.foreach_root[r] is None:
                    raise CompileError(f"{r} is an item of a temporary list that is gone after its foreach; a "
                                       f"task cannot borrow it. Store the list in a variable first, or pass "
                                       f"copy({show(a)})", a.line)
                todo.append(self.foreach_root[r])
            out.add((r, p))
        return out

    def assign_task(self, name, val_e, line, want=None):
        """$t = go f(...);  (or moving a task / list of tasks to another variable)"""
        if name in self.vars and holds_task(self.vars[name]) and name in self.flow.assigned and name not in self.moved:
            raise CompileError(f"the task in {name} (line {self.task_line.get(name, '?')}) is not awaited before "
                               f"{name} gets a new one. await {name} first", line)
        if val_e.kind == "go":
            self.go_ok = True
            try:
                v, t, _ = self.expr(val_e)
            finally:
                self.go_ok = False
            roots, lent, fails = self.last_go
            self.task_borrows[name] = set(roots)
            self.task_lent[name] = set(lent)
            self.task_fail[name] = fails
            self.task_line[name] = line
        else:
            src = val_e.name
            v, t = self.owned_value(val_e, None, line)
            self.task_borrows[name] = set(self.task_borrows.get(src, ()))
            self.task_lent[name] = set(self.task_lent.get(src, ()))
            self.task_fail[name] = self.task_fail.get(src, False)
            self.task_line[name] = self.task_line.get(src, line)
        if want and t != want:
            raise CompileError(f"{name} is declared {want} but the task gives {t}", line)
        if name in self.vars and self.vars[name] != t:
            raise CompileError(f"{name} is already {self.vars[name]}; a variable keeps one type", line)
        self.declare(name, t)
        self.emit(f"store ptr {v}, ptr {self.slot(name)}")      # the old one was awaited (moved): slot is null
        self.moved.pop(name, None)
        self.flow.maybe.discard(name)
        self.flow.assigned.add(name)

    def assign_var(self, name, val_e, line, want=None):
        if name == "$this":
            raise CompileError("$this cannot be assigned", line)
        if self.is_borrow(name, val_e):
            return self.assign_borrow(name, val_e, line, want)
        if val_e.kind == "go" or (val_e.kind == "var" and holds_task(self.vars.get(val_e.name, ""))):
            return self.assign_task(name, val_e, line, want)
        if name in self.borrow_vars:
            raise CompileError(f"{name} borrows from another value, so it cannot hold this new value. Use a new "
                               f"variable name", line)
        if name in self.borrowed:
            self.check_writable(name, line)
        t = self.vars.get(name)
        if t and is_heap(t) and self.walked(name):
            raise CompileError(f"cannot replace {name} while a foreach is looping over it", line)
        if t and is_heap(t) and name in self.vars:
            self.mutating(name, (), line, what="replaced")
        if t is None and val_e.kind == "call" and self.fkey(val_e, quiet=True) and is_heap(self.mod.funcs[self.fkey(val_e, quiet=True)][1]):
            # a first assignment inside a loop still runs many times: declare now so the
            # previous round's value can be freed before the new one is built
            t = self.mod.funcs[self.fkey(val_e, quiet=True)][1]
            self.declare(name, t)
        early = False
        if t and is_heap(t) and not mentions(val_e, name):
            # ownership tells us the old value is dead right now: free it before building the new one
            self.free_var(name, t)
            early = True
        v, vt = self.owned_value(val_e, t, line, into=name)
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
        if tg.kind == "field" or tg.seq.kind != "var" or tg.seq.name == "$this":
            return self.assign_path(s)
        name = tg.seq.name
        self.require_assigned(name, s.line)
        if name in self.moved:
            self.use_var(name, s.line)          # reports the move
        st = self.vars[name]
        self.mutating(name, (), s.line, what="appended to" if tg.kind == "append" else "changed")
        if tg.kind == "append":
            if self.walked(name):
                raise CompileError(f"cannot append to {name} while a foreach is looping over it", s.line)
            if not is_list(st):
                raise CompileError(f"{name} is {st}; $a[] = value appends to lists only", s.line)
            et = elem_of(st)
            if s.val.kind == "go":
                self.go_ok = True
                try:
                    v, t, _ = self.expr(s.val, et)
                finally:
                    self.go_ok = False
                roots, lent, fails = self.last_go
                if lent:
                    raise CompileError("a task in a list cannot take & arguments; keep it in its own variable", s.line)
                self.task_borrows.setdefault(name, set()).update(roots)
                self.task_fail[name] = self.task_fail.get(name, False) or fails
                self.task_line.setdefault(name, s.line)
            elif is_heap(et):
                v, t = self.owned_value(s.val, et, s.line, into=name)    # the list owns what it holds
            else:
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
            self.call("void", "@pn_list_grow", f"ptr {lst}", f"i64 {esize(et)}")
            self.place_label(fast)
            self.store_elem(self.list_elem_ptr(lst, et, ln), v, et)
            n2 = self.tmp()
            self.emit(f"{n2} = add i64 {ln}, 1")
            self.emit(f"store i64 {n2}, ptr {lst}, !tbaa !1")
            return
        # $x[$i] op= value — the value is computed first, so no pointer into the container is held
        # while other code runs
        if is_list(st) and is_heap(elem_of(st)):
            et = elem_of(st)
            if self.walked(name):
                raise CompileError(f"cannot change {name} while a foreach is looping over it", s.line)
            if op not in ("=", ".=") or (op == ".=" and et != "string"):
                raise CompileError(f"'{op}' needs numbers; {name} is {st}", s.line)
            v, t = self.owned_value(s.val, et, s.line, into=name) if op == "=" else self.borrow(s.val)
            if t != et:
                raise CompileError(f"storing into {name}: expected {et} but got {t}", s.line)
            lst, _ = self.use_var(name, s.line)
            iv, it = self.borrow(tg.idx)
            if it != "int":
                raise CompileError(f"list index must be int but got {it}", s.line)
            self.list_bounds(lst, iv, s.line)
            p = self.list_elem_ptr(lst, et, iv)
            cur = self.load_elem(p, et)
            if op == "=":
                self.call("void", free_fn(et), f"ptr {cur}")
                self.store_elem(p, v, et)
            else:
                self.store_elem(p, self.call("ptr", "@pn_str_add_str", f"ptr {cur}", f"ptr {v}"), "string")
            return
        if is_list(st):
            et = elem_of(st)
            if op == ".=":
                raise CompileError("'.=' needs a string", s.line)
            v, t = self.borrow(s.val)
            lst, _ = self.use_var(name, s.line)
            iv, it = self.borrow(tg.idx)
            if it != "int":
                raise CompileError(f"list index must be int but got {it}"
                                   + (" (/ always gives a float: use intdiv($a, $b))" if it == "float" else ""), s.line)
            self.list_bounds(lst, iv, s.line)
            p = self.list_elem_ptr(lst, et, iv)
            if op != "=":
                v, t = self.arith(self.load_elem(p, et), et, v, t, op, s.line)
            self.store_elem(p, self.coerce(v, t, et, s.line, f"storing into {name}"), et)
            return
        if is_map(st):
            if self.walked(name):
                raise CompileError(f"cannot change {name} while a foreach is looping over it", s.line)
            _, vt = map_kv(st)
            if op == "=":
                v, t = self.owned_value(s.val, vt, s.line, into=name)
            else:
                v, t = self.borrow(s.val)
            m, _ = self.use_var(name, s.line)
            key = self.map_key(st, tg.idx, s.line)
            p = self.call("ptr", "@pn_map_slot", f"ptr {m}", f"i64 {key}", "i64 1")
            if op == "=":
                v = self.coerce(v, t, vt, s.line, f"storing into {name}")
                if is_heap(vt):
                    old = self.load_slot(p, vt)
                    self.call("void", free_fn(vt), f"ptr {old}")
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

    def assign_path(self, s):
        """$o->total += 5;  $users[0]->name = "x";  $o->items[] = "tea";  $m["a"]["b"] = 1;"""
        tg, op, line = s.target, s.op, s.line
        root, props = path_root(tg)
        if root is None:
            raise CompileError("can only change a value stored in a variable (this path starts at a function result)",
                               line)
        self.require_assigned(root, line)
        if tg.kind == "append":
            st = self.static_type(tg.seq)
            if st is None:
                self.place(tg.seq, line)
            if not is_list(st):
                raise CompileError(f"{show(tg.seq)} is {st}; $a[] = value appends to lists only", line)
            tt = elem_of(st)
        else:
            tt = self.static_type(tg)
            if tt is None:
                self.place(tg, line)            # reports what is wrong with the path
                raise CompileError(f"cannot assign to {show(tg)}", line)
        if getattr(s, "promoted", False) and owns_ptr(tt):
            raise CompileError(f"a promoted constructor parameter cannot be {tt}: Pin would have to copy it. Declare "
                               f"the property in the class and write $this->{tg.name} = copy(${tg.name}); "
                               f"in __construct()", line)
        self.mutating(root, props, line, at=s.order, what="appended to" if tg.kind == "append" else "changed")
        this_field = tg.kind == "field" and tg.obj.kind == "var" and tg.obj.name == "$this"
        if op == "=":
            if is_heap(tt):
                v, vt = self.owned_value(s.val, tt, line, into=root)
            else:
                v, vt = self.borrow(s.val, tt)
            v = self.coerce(v, vt, tt, line, f"storing into {show(tg)}")
            if tg.kind == "append":
                lst, _ = self.base_value(tg.seq, line)
                self.call("void", "@pn_list_push", f"ptr {lst}", f"i64 {esize(tt)}", f"i64 {self.to_bits(v, tt)}")
                return
            p, t, st = self.place(tg, line, create=True)
            if is_heap(t):
                self.call("void", free_fn(t), f"ptr {self.load_at(p, t, st)}")
            self.store_at(p, v, t, st)
            if this_field:
                self.flow.assigned.add(f"$this->{tg.name}")
            return
        if tg.kind == "append":
            raise CompileError("use $a[] = value to append", line)
        if this_field:
            self.ctor_check_read(tg)
        if op == ".=":
            if tt != "string":
                raise CompileError(f"'.=' needs a string; {show(tg)} is {tt}", line)
            rv, _ = self.owned_value(s.val, "string", line)
            self.temps.append((rv, "string"))
            p, t, st = self.place(tg, line, create=True)
            self.store_at(p, self.call("ptr", "@pn_str_add_str", f"ptr {self.load_at(p, t, st)}", f"ptr {rv}"), t, st)
            return
        if tt not in ("int", "float"):
            raise CompileError(f"'{op}' needs a number; {show(tg)} is {tt}", line)
        v, vt = self.borrow(s.val)
        p, t, st = self.place(tg, line, create=True)
        r, rt = self.arith(self.load_at(p, t, st), t, v, vt, op, line)
        self.store_at(p, self.coerce(r, rt, t, line, f"storing into {show(tg)}"), t, st)

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

    def loop(self, cond, step, body, node):
        head, bodyl, stepl, end = (self.newlabel(x) for x in ("loop", "body", "step", "endloop"))
        head_flow = self.flow.copy()
        self.open_loops.append((node.first, node.order, node.kills))
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
        self.open_loops.pop()
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
        ref = getattr(s, "ref", False)
        root, props = path_root(s.seq) if s.seq.kind in ("var", "field", "index") else (None, ())
        if ref:
            if root is None:
                raise CompileError("foreach with & changes the items in place, so it needs a variable or a "
                                   "property, e.g. foreach ($users as &$u)", s.line)
            self.require_assigned(root, s.line)
            self.mutating(root, props, s.line, at=s.seq.order)
            if s.var in self.vars:
                raise CompileError(f"{s.var} is already used in this function; give the & foreach variable a new "
                                   f"name", s.line)
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
        before = set(self.vars)
        if s.key:
            self.loop_var(s.key, kt, s.line)
        if ref:
            if vt == "bool":
                raise CompileError("foreach with & does not work on bools yet; assign by index: $xs[$i] = ...",
                                   s.line)
            self.vars[s.var] = vt
            self.refs.add(s.var)
        else:
            self.loop_var(s.var, vt, s.line)
        if is_heap(vt):
            self.loop_scope[s.var] = before
            self.foreach_root[s.var] = (root, props) if root is not None else None
        pos = f"%v.__pos{self.n}"
        self.n += 1
        self.allocas.append(f"  {pos} = alloca i64")
        self.emit(f"store i64 0, ptr {pos}")
        walked = root is not None
        if walked:
            self.iterating.append((root, props, s.line))
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
            ep = self.list_elem_ptr(seq, vt, i)
            if ref:
                self.slot_over[s.var] = ep          # the variable is the item itself
            else:
                self.emit(f"store {llty(vt)} {self.load_elem(ep, vt)}, ptr {self.slot(s.var)}")
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
            if ref:
                self.slot_over[s.var] = vp
            else:
                self.emit(f"store {llty(vt)} {self.load_slot(vp, vt)}, ptr {self.slot(s.var)}")
            nxt = self.tmp()
            self.emit(f"{nxt} = add i64 {idx}, 1")
        self.emit(f"store i64 {nxt}, ptr {pos}")
        lp = Loop(end, stepl)
        self.loops.append(lp)
        self.pending.append(mine)
        self.flow = head_flow.copy()
        self.flow.assigned.update(n for n in (s.key, s.var) if n)
        self.open_loops.append((s.first, s.order, s.kills))
        self.stmt(s.body)
        self.open_loops.pop()
        self.pending.pop()
        self.loops.pop()
        self.flow = Flow.merge([self.flow] + lp.conts, head_flow)
        self.place_label(stepl)
        self.check_back_edge(head_flow)
        self.emit(f"br label %{head}")
        self.place_label(end)
        if walked:
            self.iterating.pop()
        self.loop_scope.pop(s.var, None)
        if ref:                                 # the & variable is gone with its loop
            del self.vars[s.var], self.slot_over[s.var]
            self.refs.discard(s.var)
        self.free_temps(mine)
        self.flow = Flow.merge(lp.breaks + [head_flow], head_flow)

    def return_(self, s):
        if self.is_main:
            self.tasks_done(s.line)
            if s.val is not None:
                raise CompileError("top-level return cannot have a value", s.line)
            self.flush_temps()
            self.free_stashed()
            self.free_pending()
            self.free_owned()
            self.emit("ret i32 0")
            self.flow.live = False
            return
        if s.val is None:
            if self.ret != "void":
                raise CompileError(f"{self.name}() must return {self.ret}", s.line)
            self.ctor_done(s.line)
            self.tasks_done(s.line)
            self.flush_temps()
            self.free_stashed()
            self.free_pending()
            self.free_owned()
            self.emit_ret()
            self.flow.live = False
            return
        if self.ret == "void":
            raise CompileError(f"{self.name}() is void and cannot return a value", s.line)
        if is_heap(self.ret):
            v, t = self.owned_value(s.val, self.ret, s.line, may_move_string=True)
        else:
            v, t = self.borrow(s.val, self.ret)
        v = self.coerce(v, t, self.ret, s.line, f"return value of {self.name}()")
        self.tasks_done(s.line)                 # after the value: it may be what awaits them
        self.flush_temps()
        self.free_stashed()
        self.free_pending()
        self.free_owned()
        self.emit_ret(v)
        self.flow.live = False

    def free_stashed(self):
        for group, _ in self.temp_stack:
            self.free_temps(group)

    def ctor_done(self, line):
        """N5: when __construct() ends, on every path, every property has a value."""
        if not self.is_ctor:
            return
        missing = sorted(f for f in self.ctor_need if f"$this->{f}" not in self.flow.assigned)
        if missing and self.flow.live:
            raise CompileError(f"{self.cls.name}::__construct() does not set "
                               f"{', '.join('$this->' + m for m in missing)} on every path. Set it in every branch, "
                               f"or give the property a default: public {self.cls.fields[missing[0]].type} "
                               f"${missing[0]} = ...;", line)

    def free_pending(self):
        for group in self.pending:
            self.free_temps(group)

    def finish(self):
        if not self.term:
            self.flush_temps()
            self.tasks_done(self.line)
            if self.is_main:
                self.free_owned()
                self.emit("ret i32 0")
            elif self.ret == "void":
                self.ctor_done(self.line)
                self.free_owned()
                self.emit_ret()
            else:
                if self.flow.live:
                    raise CompileError(f"{self.name}() can reach its end without returning {'an' if self.ret[0] in 'aeiou' else 'a'} {self.ret}. "
                                       f"Add a return at the end (or in every branch)", self.line)
                self.call("void", "@pn_noreturn", f"i32 {self.line}")
                self.emit("unreachable")
        if self.is_main:      # main runs as the first task (runtime pn_run_main), so it can await and sleep
            sig = 'define internal i32 @"main.body"()'
            return ([sig + " {", "entry:"] + self.allocas + self.inits + ["  br label %body", "body:"] + self.body +
                    ["}", "define i32 @main() {", '  %r = call i32 @pn_run_main(ptr @"main.body")', "  ret i32 %r",
                     "}"])
        else:
            ps = [f"{'ptr' if ref else llty(t)} %arg.{n[1:]}" for t, n, ref in self.params]
            if self.method:
                ps.insert(0, "ptr %arg.this")
            sig = f"define internal {fn_llret(self.ret, self.throws)} {self.llname}({', '.join(ps)})"
        return [sig + " {", "entry:"] + self.allocas + self.inits + ["  br label %body", "body:"] + self.body + ["}"]


TBAA = """
!0 = !{!"pin"}
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


def parse_file(path, src=None):
    try:
        src = read_source(path) if src is None else src
        return Parser(lex(src)).program()
    except CompileError as err:
        err.file = err.file or path
        raise


def split_type_args(inner):
    depth = 0
    for i, c in enumerate(inner):
        depth += c == "<"
        depth -= c == ">"
        if c == "," and depth == 0:
            return inner[:i], inner[i + 1:]
    return inner, None


def resolve_type(t, mid, imports, line, file=None):
    """Turn a type as written in a file into its canonical form: class names become class keys
    ("Order" in the main file, "shop::Order" for class Order of module shop)."""
    if t in ("int", "float", "bool", "string", "void", "db", "rows", "sub", "stream"):
        return t
    if t.startswith("Task<"):
        return f"Task<{resolve_type(t[5:-1], mid, imports, line, file)}>"
    if t.startswith("list<"):
        e = resolve_type(t[5:-1], mid, imports, line, file)
        if e in ("db", "rows", "sub", "stream"):
            raise CompileError(f"a list cannot hold {e} values", line, file)
        return f"list<{e}>"
    if t.startswith("map<"):
        k, v = split_type_args(t[4:-1])
        v = resolve_type(v, mid, imports, line, file)
        if v in ("db", "rows", "sub", "stream") or holds_task(v):
            raise CompileError(f"a map cannot hold {v} values", line, file)
        return f"map<{k},{v}>"
    if "::" in t:
        alias, name = t.split("::", 1)
        if alias not in imports:
            raise CompileError(f"{t}: there is no `import {alias};` in this file", line, file)
        target, tfile = imports[alias]
        key = f"{target}::{name}" if target else name
        if key not in CLASSES:
            raise CompileError(f"{t}: {os.path.basename(tfile)} has no class {name}", line, file)
        if not CLASSES[key].exported:
            raise CompileError(f"class {name} is not exported. Write `export class {name}` in "
                               f"{os.path.basename(tfile)} to use it in other files", line, file)
        return key
    key = f"{mid}::{t}" if mid else t
    if key in CLASSES:
        return key
    near = {"integer": "int", "boolean": "bool", "double": "float", "str": "string", "array": "list<int>"}
    raise CompileError(f"unknown type '{t}'" + (f" (did you mean {near[t]}?)" if t in near else
                       " (types: int, float, bool, string, list<T>, map<K,V>, db, rows, or a class name)"), line, file)


def check_default(e, line):
    """Property defaults are literals, like PHP's constant expressions."""
    ok = e.kind in ("int", "float", "bool", "str") or (e.kind == "unary" and e.op == "-" and e.e.kind in ("int", "float"))
    if e.kind == "listlit":
        ok = all(check_default(i, line) for i in e.items)
    if e.kind == "maplit":
        ok = all(check_default(k, line) and check_default(v, line) for k, v in e.pairs)
    if not ok:
        raise CompileError("a property default must be a literal such as 0, 1.5, \"text\", true, [] or [1, 2]; set "
                           "other values in __construct()", line)
    return True


def compile_file(path, src=None):
    """Compile the program whose main file is `path`, with every module it imports, into one LLVM module."""
    main_path = os.path.abspath(path)
    mods = {}               # absolute file -> (module id, funcs, main, imports {alias: (id, file)}, classes)
    order = []
    CLASSES.clear()
    NEEDED.clear()

    def load(file, mid, stack, src=None):
        if file in mods:
            return
        funcs, main, imports, classes = parse_file(file, src)
        if mid and main:
            raise CompileError("a module (a file other files import) may only contain functions, classes and "
                               "imports; move this code into a function, or into the main file", main[0].line, file)
        table = {}
        for imp in imports:
            target = resolve_import(imp, file, main_path)
            if target in stack:
                chain = " -> ".join(os.path.basename(f) for f in stack[stack.index(target):] + [target])
                raise CompileError(f"import cycle: {chain}. Move what both files need into a third module",
                                   imp.line, file)
            if imp.alias in table:
                raise CompileError(f"two imports are both called {imp.alias}; rename one with `as`", imp.line, file)
            tid = module_id(target, main_path, imp.path)
            table[imp.alias] = (tid, target)
            load(target, tid, stack + [target])
        mods[file] = (mid, funcs, main, table, classes)
        order.append(file)

    load(main_path, "", [main_path], src)
    mod = Module()
    # the one error type: Error { int $code; string $message; } (error-handling.md E2)
    err = ClassInfo(Node("class", 0, name="Error", props=[], methods=[], exported=True), "Error", "", "<built-in>", {})
    err.fields["code"] = Field("code", "int", "public", None, 0, 0)
    err.fields["message"] = Field("message", "string", "public", None, 1, 0)
    err.builtin = True
    CLASSES["Error"] = err
    # classes first: function signatures and other classes may name them
    for file in order:
        mid, _, _, table, classes = mods[file]
        for c in classes:
            key = c.name if not mid else f"{mid}::{c.name}"
            if key in CLASSES:
                raise CompileError(f"class {c.name} is " + ("built in (the error type)" if key == "Error" else
                                   "already defined"), c.line, file)
            CLASSES[key] = ClassInfo(c, key, mid, file, table)
    for ci in list(CLASSES.values()):
        if getattr(ci, "builtin", False):
            continue
        for pr in ci.node.props:
            if pr.name in ci.fields:
                raise CompileError(f"class {ci.name} declares ${pr.name} twice", pr.line, ci.file)
            ft = resolve_type(pr.vtype, ci.mid, ci.imports, pr.line, ci.file)
            if ft == "void":
                raise CompileError("a property cannot be void", pr.line, ci.file)
            if holds_task(ft):
                raise CompileError("a task cannot be kept in a property: await it in the function that started it",
                                   pr.line, ci.file)
            if pr.default is not None:
                try:
                    check_default(pr.default, pr.line)
                except CompileError as err:
                    err.file = ci.file
                    raise
            ci.fields[pr.name] = Field(pr.name, ft, pr.vis, pr.default, len(ci.fields), pr.line)
        for m in ci.node.methods:
            if m.name in ci.methods:
                raise CompileError(f"class {ci.name} has two methods called {m.name}()", m.line, ci.file)
            ps = [(resolve_type(t, ci.mid, ci.imports, m.line, ci.file), n, r) for t, n, r in m.params]
            if len({n for _, n, _ in ps}) != len(ps):
                raise CompileError(f"{m.name}() has two parameters with the same name", m.line, ci.file)
            ci.methods[m.name] = Method(ci, m, ps, resolve_type(m.ret, ci.mid, ci.imports, m.line, ci.file))
        if "__construct" not in ci.methods:
            bare = [f.name for f in ci.fields.values() if f.default is None]
            if bare:
                f = ci.fields[bare[0]]
                raise CompileError(f"class {ci.name} has no __construct(), so every property needs a default "
                                   f"(public {f.type} ${f.name} = ...;), or add a __construct() that sets "
                                   f"${f.name}", ci.node.line, ci.file)

    def holds(t, target, seen):          # does a value of type t contain a `target` object directly?
        if not is_class(t) or t in seen:
            return False
        return any(f.type == target or holds(f.type, target, seen | {t}) for f in CLASSES[t].fields.values())
    for ci in CLASSES.values():
        if holds(ci.key, ci.key, set()):
            raise CompileError(f"class {ci.name} contains itself through its properties, so no {ci.name} could ever "
                               f"be built. Hold it in a list (list<{ci.name}>) or store an id instead",
                               ci.node.line, ci.file)
    for file in order:
        mid, funcs, _, table, _ = mods[file]
        for f in funcs:
            key = f.name if not mid else f"{mid}::{f.name}"
            if key in mod.funcs or (not mid and f.name in BUILTINS) or (mid and f.name in BUILTINS):
                raise CompileError(f"function {f.name}() is already defined", f.line, file)
            names = [p for _, p, _ in f.params]
            if len(set(names)) != len(names):
                raise CompileError(f"{f.name}() has two parameters with the same name", f.line, file)
            ps = [(resolve_type(t, mid, table, f.line, file), n, r) for t, n, r in f.params]
            if any(holds_task(t) for t, _, _ in ps) or holds_task(resolve_type(f.ret, mid, table, f.line, file)):
                raise CompileError("a task cannot be passed to or returned from a function: await it in the function "
                                   "that started it (concurrency.md C1)", f.line, file)
            mod.params[key] = ps
            mod.funcs[key] = ([t for t, _, _ in ps], resolve_type(f.ret, mid, table, f.line, file))
            mod.exported[key] = getattr(f, "exported", False) or not mid
            mod.throws[key] = bool(getattr(f, "throws", False))

    def gen_method(m):
        ci = m.cls
        try:
            g = FuncGen(mod, m.name, m.params, m.ret, m.node.line, modid=ci.mid, imports=ci.imports, method=m)
            g.prepare(m.node.body)
            g.stmt(m.node.body)
            return g.finish()
        except CompileError as err:
            err.file = err.file or ci.file
            raise

    def gen_init(ci):                       # init.Class: stores the property defaults into a new object
        try:
            g = FuncGen(mod, f"init.{ci.key}", [], "void", ci.node.line, modid=ci.mid, imports=ci.imports)
            for f in ci.fields.values():
                if f.default is not None:
                    v, t = g.owned_value(f.default, f.type, f.line)
                    v = g.coerce(v, t, f.type, f.line, f"default of ${f.name}")
                    g.store_elem(g.field_ptr("%arg.o", f), v, f.type)
            lines = g.finish()
        except CompileError as err:
            err.file = err.file or ci.file
            raise
        lines[0] = f'define internal void @"init.{ci.key}"(ptr %arg.o) {{'
        return lines

    methods = [m for ci in CLASSES.values() for m in ci.methods.values()]
    for m in methods:
        if m.throws and m.name == "__construct":
            pass                            # new Name(...) can fail: callers write try new Name(...)
    out = []
    mod.inferring = True                    # R6: which methods change $this; repeat until nothing new is found
    while True:
        mod.changed = False
        for m in methods:
            gen_method(m)
        if not mod.changed:
            break
    mod.inferring = False
    for m in methods:
        out += gen_method(m)
    for ci in CLASSES.values():
        if any(f.default is not None for f in ci.fields.values()):
            out += gen_init(ci)
    for file in order:
        mid, funcs, main, table, _ = mods[file]
        try:
            for f in funcs:
                key = f.name if not mid else f"{mid}::{f.name}"
                g = FuncGen(mod, f.name, mod.params[key], mod.funcs[key][1], f.line, modid=mid, imports=table,
                            throws=mod.throws[key])
                g.prepare(f.body)
                g.stmt(f.body)
                out += g.finish()
            if not mid:
                g = FuncGen(mod, "main", [], "void", 1, is_main=True, imports=table)
                g.prepare(main)
                for s in main:
                    g.stmt(s)
                out += g.finish()
        except CompileError as err:
            err.file = err.file or file
            raise
    return "\n".join(mod.header() + out) + "\n" + TBAA


def compile_source(src):
    """Compile a single file given as text (no imports relative to a real directory)."""
    return compile_file(os.path.join(os.getcwd(), "<source>.pn"), src)


def module_id(file, main_path, import_path):
    """A readable, unique name for a module: its path from the main file's folder, or its package path."""
    rel = os.path.relpath(file, os.path.dirname(main_path))
    if rel.startswith(".."):
        return import_path
    return rel[:-3] if rel.endswith(".pn") else rel


def import_root(from_file, main_path):
    """Local import paths start at the project folder (the main file's folder), whichever file imports them,
    so `import posts;` means the same file everywhere. Inside a package they start at the package's root."""
    cache = os.path.abspath(cache_root())
    f = os.path.abspath(from_file)
    if f.startswith(cache + os.sep):
        parts = os.path.relpath(f, cache).split(os.sep)
        return os.path.join(cache, *parts[:3])          # host/owner/repo@commit
    return os.path.dirname(os.path.abspath(main_path))


def resolve_import(imp, from_file, main_path):
    """A local import is a file under the project (or package) folder; a path whose first part has a dot is a
    package."""
    first = imp.path.split("/")[0]
    if "." in first:
        return package_file(imp.path, main_path, imp.line, from_file)
    root = import_root(from_file, main_path)
    f = os.path.join(root, imp.path + ".pn")
    if not os.path.isfile(f):
        raise CompileError(f"import {imp.path}: there is no file {imp.path}.pn in the project folder "
                           f"{os.path.relpath(root) or '.'} (import paths start at the folder of the main file)",
                           imp.line, from_file)
    return os.path.abspath(f)


# ----------------------------------------------------------------------------- packages
# import github.com/owner/repo/path;  needs a pin.lock entry made by  pin get github.com/owner/repo@version.
# pin.lock pins the exact commit and a sha256 of every .pn file in the package; the hash is checked on every
# build, so a package that changes after it was locked (a moved tag, a tampered cache) does not compile.

def cache_root():
    return os.environ.get("PIN_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "pin", "pkg")


def find_lock(start_dir):
    d = os.path.abspath(start_dir)
    while True:
        f = os.path.join(d, "pin.lock")
        if os.path.isfile(f):
            return f
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def read_lock(path):
    entries = {}
    if path and os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            parts = line.split()
            if len(parts) == 4 and not line.startswith("#"):
                entries[parts[0]] = {"version": parts[1], "commit": parts[2], "hash": parts[3]}
    return entries


def write_lock(path, entries):
    lines = ["# pin.lock — written by `pin get`; commit it. Each package is pinned to a commit and a sha256 of",
             "# its .pn files, checked on every build.", ""]
    for repo in sorted(entries):
        e = entries[repo]
        lines.append(f"{repo} {e['version']} {e['commit']} {e['hash']}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def package_hash(d):
    import hashlib
    h = hashlib.sha256()
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x != ".git")
        for name in sorted(files):
            if name.endswith(".pn"):
                p = os.path.join(root, name)
                h.update(os.path.relpath(p, d).replace(os.sep, "/").encode() + b"\0")
                h.update(open(p, "rb").read() + b"\0")
    return "sha256:" + h.hexdigest()


def git_url(repo):
    base = os.environ.get("PIN_GIT_BASE")              # tests point this at local repositories
    return f"{base.rstrip('/')}/{repo}" if base else f"https://{repo}.git"


def fetch_package(repo, ref):
    """Clone repo at ref into the cache; return (commit, directory)."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="pin-get-")
    try:
        r = subprocess.run(["git", "clone", "--quiet", git_url(repo), tmp], capture_output=True, text=True)
        if r.returncode:
            raise CompileError(f"cannot download {repo}: {r.stderr.strip()}", 0)
        if ref:
            r = subprocess.run(["git", "-C", tmp, "checkout", "--quiet", ref], capture_output=True, text=True)
            if r.returncode:
                raise CompileError(f"{repo} has no version {ref}: {r.stderr.strip()}", 0)
        commit = subprocess.run(["git", "-C", tmp, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        dest = os.path.join(cache_root(), f"{repo}@{commit}")
        if not os.path.isdir(dest):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copytree(tmp, dest, ignore=shutil.ignore_patterns(".git"))
        return commit, dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def package_file(path, main_path, line, from_file):
    parts = path.split("/")
    if len(parts) < 4:
        raise CompileError(f"import {path}: a package import is host/owner/repo/module, e.g. "
                           f"github.com/pin-language/web/router", line, from_file)
    repo, rest = "/".join(parts[:3]), "/".join(parts[3:])
    lock = find_lock(os.path.dirname(main_path))
    entry = read_lock(lock).get(repo)
    if not entry:
        raise CompileError(f"import {path}: {repo} is not in pin.lock. Run:  pin get {repo}@<version>", line, from_file)
    d = os.path.join(cache_root(), f"{repo}@{entry['commit']}")
    if not os.path.isdir(d):
        commit, d = fetch_package(repo, entry["commit"])
    got = package_hash(d)
    if got != entry["hash"]:
        raise CompileError(f"{repo}: the code does not match pin.lock ({entry['hash']}, found {got}). The "
                           f"package changed after it was locked, so it is not built. To get the locked code "
                           f"back, delete {d} (it is downloaded again). Run  pin get {repo}@<version>  only "
                           f"after reading what changed", line, from_file)
    f = os.path.join(d, rest + ".pn")
    if not os.path.isfile(f):
        raise CompileError(f"import {path}: {repo} has no {rest}.pn", line, from_file)
    return os.path.abspath(f)


def pin_get(spec):
    """pin get host/owner/repo[@version]: download, pin the commit and hash in ./pin.lock."""
    repo, _, ref = spec.partition("@")
    if len(repo.split("/")) != 3 or "." not in repo.split("/")[0]:
        print("usage: pin get github.com/owner/repo[@version]", file=sys.stderr)
        return 2
    try:
        commit, d = fetch_package(repo, ref)
    except CompileError as e:
        print(f"error: {e.msg}", file=sys.stderr)
        return 1
    lock = find_lock(os.getcwd()) or os.path.join(os.getcwd(), "pin.lock")
    entries = read_lock(lock)
    entries[repo] = {"version": ref or "HEAD", "commit": commit, "hash": package_hash(d)}
    write_lock(lock, entries)
    print(f"{repo} {ref or 'HEAD'} -> {commit[:12]} ({entries[repo]['hash'][:19]}…) in {os.path.relpath(lock)}")
    return 0


VERSION = "0.8.0"

USAGE = """pin — compile and run Pin programs (.pn)

  pin FILE.pn                compile, then run it (same as pin run FILE.pn)
  pin run FILE.pn            compile, then run it (the binary is thrown away)
  pin build FILE.pn [-o OUT] compile to a native binary (default: FILE without .pn)
  pin check FILE.pn          type- and ownership-check only, no binary
  pin get HOST/OWNER/REPO[@VERSION]  add a package to ./pin.lock (then: import host/owner/repo/module;)
  pin version                (also -v, --version)

  --json                     errors as JSON lines on stdout: {"file","line","severity","message"}
                             (pin check --json prints {"file","ok":true} on success)

Needs clang. Old form `pinac.py FILE.pn [-o OUT] [--emit-ll]` still works."""


def read_source(path):
    try:
        return open(path, encoding="utf-8").read()
    except OSError as e:
        report(path, 0, f"cannot read the file ({e.strerror})")
        sys.exit(1)
    except UnicodeDecodeError:
        report(path, 0, "the file is not UTF-8 text")
        sys.exit(1)


JSON_ERRORS = False      # --json: report errors as one JSON object per line on stdout (for tools / AI agents)


def report(path, line, msg):
    if JSON_ERRORS:
        import json
        print(json.dumps({"file": path, "line": line, "severity": "error", "message": msg}, ensure_ascii=False))
    else:
        print(f"{path}{f':{line}' if line else ''}: error: {msg}", file=sys.stderr)


def to_ir(path):
    try:
        return compile_file(path)
    except CompileError as e:
        f = e.file or path
        shown = f if f.startswith("<") else os.path.relpath(f) if os.path.isabs(f) else f
        report(shown, e.line, e.msg)
        sys.exit(1)


def needs_sqlite(ir):
    """Only programs that call a db_* function link SQLite (every program *declares* them)."""
    return re.search(r"call [^\n]*@pn_db_", ir) is not None


def link(ir, out):
    """LLVM IR + runtime.c -> native binary at `out`. The .ll lives in a temp dir, not next to the source."""
    import shutil
    import tempfile
    if not shutil.which("clang"):
        print("error: pin needs clang. macOS: xcode-select --install · Ubuntu/Debian: sudo apt install clang",
              file=sys.stderr)
        sys.exit(2)
    with tempfile.TemporaryDirectory(prefix="pin-") as d:
        ll = os.path.join(d, "prog.ll")
        with open(ll, "w") as f:
            f.write(ir)
        db = ["-DPIN_SQLITE", "-lsqlite3"] if needs_sqlite(ir) else []
        r = subprocess.run(["clang", "-O2", "-pthread", "-Wno-override-module", ll, os.path.join(HERE, "runtime.c"),
                            "-lm", *db, "-o", out], capture_output=True, text=True)
    if r.returncode:
        if db and ("sqlite3.h" in r.stderr or "lsqlite3" in r.stderr):
            print("error: this program uses the database, which needs SQLite's development files. "
                  "Ubuntu/Debian: sudo apt install libsqlite3-dev · Fedora: sudo dnf install sqlite-devel "
                  "(macOS has them already)", file=sys.stderr)
        else:
            print(r.stderr, file=sys.stderr)
        sys.exit(2)


SUBCOMMANDS = ("run", "build", "check", "get", "version", "-v", "--version", "help", "-h", "--help")


def cli(argv, default_run=False):
    """default_run: the `pin` command runs `pin FILE.pn` like `python file.py`; plain `pinac.py FILE.pn`
    keeps its old meaning (compile only), which the tests rely on."""
    import argparse
    global JSON_ERRORS
    if "--json" in argv:
        JSON_ERRORS = True
        argv = [a for a in argv if a != "--json"]
    if not argv:
        print(USAGE)
        return 0
    if default_run and argv[0] not in SUBCOMMANDS and not argv[0].startswith("-"):
        argv = ["run"] + argv
    if argv[0] in SUBCOMMANDS:
        cmd, rest = argv[0], argv[1:]
        if cmd in ("help", "-h", "--help"):
            print(USAGE)
            return 0
        if cmd in ("version", "-v", "--version"):
            print(f"pin {VERSION}")
            return 0
        if not rest:
            print(USAGE, file=sys.stderr)
            return 2
        if cmd == "get":
            return max(pin_get(spec) for spec in rest)
        src = rest[0]
        if cmd == "check":
            to_ir(src)
            if JSON_ERRORS:
                import json
                print(json.dumps({"file": src, "ok": True}))
            else:
                print(f"{src}: ok")
            return 0
        if cmd == "build":
            ap = argparse.ArgumentParser(prog="pin build")
            ap.add_argument("src")
            ap.add_argument("-o", "--out")
            a = ap.parse_args(rest)
            link(to_ir(a.src), a.out or os.path.splitext(a.src)[0])
            return 0
        import tempfile                                   # run
        ir = to_ir(src)
        with tempfile.TemporaryDirectory(prefix="pin-run-") as d:
            exe = os.path.join(d, os.path.splitext(os.path.basename(src))[0])
            link(ir, exe)
            try:
                return subprocess.run([exe] + rest[1:]).returncode
            except KeyboardInterrupt:
                return 130
    ap = argparse.ArgumentParser(description="Compile a .pn file to a native binary (see also: pin help)")
    ap.add_argument("src")
    ap.add_argument("-o", "--out")
    ap.add_argument("--emit-ll", action="store_true", help="only write the LLVM IR, next to the source")
    a = ap.parse_args(argv)
    ir = to_ir(a.src)
    if a.emit_ll:
        with open(os.path.splitext(a.src)[0] + ".ll", "w") as f:
            f.write(ir)
        return 0
    link(ir, a.out or os.path.splitext(a.src)[0])
    return 0


def main():
    sys.exit(cli(sys.argv[1:]))


if __name__ == "__main__":
    main()
