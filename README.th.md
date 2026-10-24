# Pin

**ภาษาสำหรับให้ AI coder เขียน web backend ที่เร็วและปลอดภัยเรื่องหน่วยความจำ**
หน้าตาแบบ PHP, compile เป็น native code, ไม่มี garbage collector

ทุกวันนี้โค้ดส่วนใหญ่เขียนโดย AI และ AI รู้จัก PHP ดีอยู่แล้ว Pin ใช้ syntax เดียวกัน แต่ย้ายความผิดพลาดจาก
production มาจับตอน compile ทุก error บอกวิธีแก้ และออกเป็น JSON ได้ (`pin check --json`) ให้ AI วน
แก้เองได้ · คู่มือสำหรับ AI: [ai-guide.md](https://github.com/pin-language/docs/blob/HEAD/ai-guide.md)

คนที่เขียน PHP อ่าน Pin ได้ทันที แต่ตัวแปรแต่ละตัวมี type เดียว หน่วยความจำจัดการด้วย ownership ที่เช็คตอน
compile (คล้าย Rust แต่ย่อให้ง่าย) และ compile ผ่าน LLVM ออกมาเป็น binary ไฟล์เดียว รวมถึง HTTP server แบบหลาย thread

> **สถานะ: v0.1 สำหรับให้คนเริ่มทดลอง** ภาษายังเล็กและยังเปลี่ยนได้ ห้ามใช้ใน production และห้ามเปิด HTTP
> server ออก internet (ยังไม่มี TLS และ timeout) · English: [README.md](README.md)

หลักการออกแบบ: **memory safety > ความเร็ว > อ่านง่าย > เหมือน PHP**

## เริ่มใช้งาน

ต้องมี **Python 3.10+** และ **clang**

- macOS: `xcode-select --install`
- Ubuntu / Debian: `sudo apt install clang python3`
- Windows: ยังไม่รองรับ (WSL + Ubuntu น่าจะใช้ได้ แต่ยังไม่ได้ทดสอบ)

```bash
git clone https://github.com/pin-language/pin.git
cd pin
./pin examples/hello.pn
```

ใส่ `pin` ลง PATH (ไม่ต้องใช้ sudo ถ้า `pin -v` ขึ้นว่า command not found ให้เพิ่ม
`export PATH="$HOME/.local/bin:$PATH"` ใน `~/.zshrc`):

```bash
mkdir -p ~/.local/bin && ln -sf "$PWD/pin" ~/.local/bin/pin && pin -v
```

| คำสั่ง | ทำอะไร |
|---|---|
| `pin file.pn` | compile แล้วรันเลย (เหมือน `pin run file.pn`) |
| `pin run file.pn` | compile แล้วรันเลย |
| `pin build file.pn [-o out]` | compile เป็น binary |
| `pin check file.pn` | เช็ค type และ ownership อย่างเดียว |
| `pin version` | ดูเวอร์ชัน |

**VS Code:** มีสีตาม syntax ใน [`editors/vscode`](editors/vscode/) ติดตั้งด้วย
`cd editors/vscode && npx @vscode/vsce package && code --install-extension pin-0.1.0.vsix`

ตัวอย่างอยู่ใน [`examples/`](examples/) และคู่มือภาษาเต็ม (ภาษาอังกฤษ) อยู่ที่ [language.md](https://github.com/pin-language/docs/blob/HEAD/language.md) (เอกสารทั้งหมดอยู่ที่ [pin-language/docs](https://github.com/pin-language/docs))

## ตัวอย่าง

```php
function squares(int $n): list<int> {   // คืน list ให้คนเรียกเป็นเจ้าของ
    list<int> $out = [];
    for ($i = 1; $i <= $n; $i++) { $out[] = $i * $i; }
    return $out;
}

function total(list<int> $xs): int {    // ยืม list มาอ่าน ไม่ copy
    $sum = 0;
    foreach ($xs as $x) { $sum += $x; }
    return $sum;
}

$name = "PinaTo";
echo "สวัสดี $name\n";
echo total(squares(10)), "\n";          // 385
```

## ต่างจาก PHP ตรงไหน

| เรื่อง | PHP | Pin |
|---|---|---|
| type ของตัวแปร | เปลี่ยนได้ตลอด | type เดียวตลอด อนุมานจากค่าแรก |
| parameter / return | ใส่ type หรือไม่ก็ได้ | ต้องใส่ |
| array | ตัวเดียวเป็นทั้ง list และ map | แยก `list<int>` กับ `map<string,int>` (map เรียงตามลำดับที่ใส่เหมือน PHP) |
| string | `.`, `.=`, `"Hello $name"` | เหมือนกัน |
| อ่าน map ด้วย key ที่ไม่มี | warning แล้วได้ null | panic — เช็คด้วย `isset()` หรือใช้ `$m[$k] ?? 0` |
| index เกิน | warning แล้วได้ null | panic |
| `if ($n)` | แปลงเป็น bool ให้ | ต้องเป็น bool จริง เช่น `$n != 0` |
| แก้ array ที่รับเป็น parameter | แก้สำเนาของตัวเอง | compile error — ต้อง `copy()` ก่อน |
| ใช้ตัวแปรที่อาจยังไม่มีค่า | warning | compile error |
| หน่วยความจำ | reference counting + GC | ownership เช็คตอน compile ไม่มี GC |
| int ล้น | กลายเป็น float | วนรอบแบบ 64-bit |
| `json_encode` ภาษาไทย | `ไ...` | เก็บ UTF-8 ตามเดิม |

## Ownership ใน 1 นาที

```php
$a = [1, 2, 3];
echo total($a);      // ส่งเข้าฟังก์ชัน = ให้ยืมอ่าน $a ยังเป็นของเรา
$b = $a;             // list และ map "ย้าย": $b เป็นเจ้าของแทน
echo count($a);      // compile error: $a was moved on line 3 ...
$c = copy($b);       // อยากได้สองชุดต้อง copy เอง
$s = "text";
$t = $s;             // string "copy" เหมือน PHP
```

compiler เช็คแยกทีละ path: ถ้า move `$a` ใน if branch หนึ่ง อีก branch ยังใช้ `$a` ได้ ข้อความ error บอกวิธีแก้เสมอ

## Web server ในไฟล์เดียว

```php
function handle(string $method, string $path, string $body): string {
    if ($path == "/hello") { return json_encode(["message" => "hello"]); }
    http_status(404);
    return json_encode(["error" => "not found"]);
}
serve(8080, "handle");
```

ได้ HTTP/1.1 server ที่รองรับ keep-alive และ pipelining ใช้ 1 thread ต่อ 1 core (`PN_THREADS=4` เพื่อเปลี่ยน)
ใช้ได้ทั้ง Linux และ macOS ไม่มีตัวแปร global จึงไม่ต้องมี lock ถ้า handler panic client ได้ 500 หน่วยความจำของ
request นั้นถูกคืนทั้งหมด แล้ว server ทำงานต่อ

## อะไรถูกเช็คให้บ้าง

- **ตอน compile:** type, ownership (ใช้หลัง move, move ใน loop, แก้ของที่ยืมมา, แก้ list ระหว่าง foreach)
  และตัวแปรต้องมีค่าในทุก path
- **ตอนรัน (เปิดตลอด ปิดไม่ได้):** index เกิน, key ไม่มี, หารศูนย์, `intdiv(PHP_INT_MIN, -1)`, `(int)` ของ
  float ที่ใช้ไม่ได้, `intval()` เลขใหญ่เกิน และขนาดหน่วยความจำทุกครั้งที่จอง
- **ยังไม่เช็ค:** `+ - *` ของ int ที่ล้นจะวนรอบ เพราะเช็คแล้วช้าลงสูงสุด 23% จะใส่กลับเมื่อ compiler ตัด check
  ที่ไม่จำเป็นได้

## Benchmark

อัลกอริทึมเดียวกันทุกภาษา output ตรงกันทุกภาษา · Apple M2 (8 core), macOS 14, ดีที่สุดจาก 3 รอบ ·
macOS ล็อก CPU core ไม่ได้ ส่วนต่างต่ำกว่า ~20% ถือเป็น noise · ตัวเลขหน่วยความจำและวิธีวัดอยู่ใน repo benchmarks

| benchmark | Pin | Rust 1.95 | Go 1.26 | PHP 8.5 + JIT | PHP 8.5 | Python 3.15 |
|---|---|---|---|---|---|---|
| sieve (20M) | 0.048s | 0.074s | 0.046s | 0.642s | 1.385s | 4.420s |
| collatz (1M) | 0.163s | 0.133s | 0.174s | 0.715s | 3.223s | 6.370s |
| pi (100M terms) | 0.098s | 0.096s | 0.093s | 0.943s | 2.260s | 13.265s |
| list build + sum | 0.116s | 0.141s | 0.178s | 0.459s | 1.210s | 5.280s |

ข้อควรรู้: เป็น loop CPU เล็กๆ ไม่ใช่แอปจริง · ตัด `fib` ออกเพราะ clang optimize recursion จนไม่ได้บอกอะไรเรื่องภาษา ·
ผล load test ของ HTTP server (Linux 2 core) อยู่ใน [pin-language/benchmarks](https://github.com/pin-language/benchmarks) server ของ Pin ทำงานน้อยกว่า Go
`net/http` มาก (ไม่มี TLS, HTTP/2, header, timeout) ตัวเลขนั้นจึงไม่ใช่การเทียบภาษาตรงๆ

## ยังไม่มี

class, `null`, `try`/`catch`, `list<string>` และ container ซ้อนกัน, closure, module/import, header ของ request,
`json_decode`, database, `go`/`await` · ส่วนใหญ่ออกแบบไว้แล้วใน [pin-language/docs/design](https://github.com/pin-language/docs/blob/HEAD/design/)

## อยากได้ feedback

[เปิด issue](https://github.com/pin-language/pin/issues) ได้เลย โดยเฉพาะ:

- โค้ดที่คิดว่าน่าจะใช้ได้แต่ compile ไม่ผ่าน (และข้อความ error ช่วยไหม)
- อะไรก็ตามที่ crash แทนที่จะพิมพ์ `panic:` — นั่นคือบั๊กที่อยากเห็นที่สุด
- ต้องมีอะไรก่อน ถึงจะลองใช้ Pin ทำ API จริง

รัน test ทั้งชุด: `python3 tests/run_tests.py` (ประมาณ 1 นาที)

## License

MIT — ดู [LICENSE](LICENSE)
