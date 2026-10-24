# Prototype: ภาษาหน้าตาคล้าย PHP ที่ compile เป็น native code

ชื่อชั่วคราว ไฟล์นามสกุล `.pn`

```
source (.pn) → lexer → parser → type checker + ownership checker → LLVM IR → clang -O2 → ไฟล์ binary ไฟล์เดียว
```

## ใช้งาน

```bash
python3 pinac.py tests/hello.pn      # ได้ไฟล์ tests/hello
./tests/hello
python3 pinac.py --emit-ll file.pn   # ดู LLVM IR ที่สร้าง
```

ต้องมี: Python 3, clang (ทดสอบกับ clang 18)

## ตัวอย่าง

```php
<?php
function sum(list<int> $xs): int {     // ยืม list มาอ่าน ไม่ได้เป็นเจ้าของ
    $total = 0;                         // type อนุมานเอง = int
    foreach ($xs as $x) { $total += $x; }
    return $total;
}

function squares(int $n): list<int> {   // คืน list ที่ผู้เรียกเป็นเจ้าของ
    list<int> $out = [];
    for ($i = 1; $i <= $n; $i++) { $out[] = $i * $i; }
    return $out;
}

$name = "PinaTo";
echo "Hello $name\n";
echo sum(squares(10)), "\n";
```

## กติกาของภาษา (ที่ต่างจาก PHP)

| เรื่อง | PHP | ภาษานี้ |
|---|---|---|
| type ของตัวแปร | เปลี่ยนได้ตลอด | type เดียวตลอด อนุมานจากค่าแรก |
| parameter / return | type ใส่หรือไม่ใส่ก็ได้ | ต้องใส่ |
| array | ตัวเดียวเป็นทั้ง list และ map | แยกเป็น `list<int>` กับ `map<string,int>` (เรียงตามลำดับที่ใส่เหมือน PHP) |
| string | ต่อด้วย `.` | เหมือน PHP: `.`, `.=`, `"Hello $name"`, `{$x}` |
| อ่าน map ด้วย key ที่ไม่มี | warning แล้วได้ null | panic — ให้เช็ค `isset()` หรือใช้ `$m[$k] ?? 0` ก่อน |
| `$m[$k] += 1` กับ key ใหม่ | warning แล้วนับต่อ | นับต่อจาก 0 ได้เลย ไม่มี warning |
| json_encode ภาษาไทย | แปลงเป็น `\u0e44` | เก็บ UTF-8 ตามเดิม (เหมือน JSON_UNESCAPED_UNICODE) |
| แก้ array ที่รับเป็น parameter | แก้สำเนาของตัวเอง (copy-on-write) | compile error — ต้อง `copy()` ก่อน |
| เงื่อนไข `if ($n)` | แปลงเป็น bool ให้เอง | ต้องเป็น bool จริง เช่น `$n != 0` |
| หน่วยความจำ | reference counting + GC ตอนรัน | ownership ตรวจตอน compile ไม่มี GC ตอนรัน |
| index เกินขนาด | warning แล้วได้ null | หยุดโปรแกรมทันที (panic) |
| int ล้น | กลายเป็น float | วนรอบแบบ 64-bit (ยังไม่ตรวจ) |

## Ownership (แบบ Rust แต่ย่อให้ง่าย)

1. list, map และ string มีเจ้าของคนเดียว เจ้าของจบฟังก์ชันเมื่อไหร่ก็คืนหน่วยความจำทันที
2. list และ map: `$b = $a;` คือการ **ย้าย** หลังจากนั้นใช้ `$a` ไม่ได้
3. string: `$b = $a;` คือการ **copy** แบบ PHP (string ที่เขียนไว้ในโค้ดไม่ถูก copy จริง เพราะไม่มีวันเปลี่ยน)
4. ส่งค่าเข้าฟังก์ชัน = **ให้ยืมแบบอ่านอย่างเดียว** ผู้เรียกยังเป็นเจ้าของ (ใกล้ PHP ที่ฟังก์ชันแก้ array ของผู้เรียกไม่ได้)
5. ของที่ยืมมา (parameter, ตัวแปรของ foreach) แก้หรือย้ายไม่ได้ ต้อง copy ก่อน
6. ห้ามเพิ่มของใน list หรือแก้ map ระหว่างที่ `foreach` กำลังวนอยู่
7. ไม่มีตัวแปร global ฟังก์ชันจึงใช้ข้อมูลร่วมกันไม่ได้ ทำให้ handler ของ HTTP server รันหลาย thread พร้อมกันได้โดยไม่ต้องมี lock

compiler จับผิดให้ตั้งแต่ตอน compile ตัวอย่างใน `tests/errors/`:

```
use_after_move.pn:3: error: $a was moved on line 2 and can no longer be used here. Use copy($a) if you need two separate lists
move_in_loop.pn:3: error: $a is moved inside a loop, so the next round would use a moved value. Move it after the loop, or use copy($a)
append_while_iterating.pn:3: error: cannot append to $a while a foreach is looping over it
```

## Built-in

- list/map: `count`, `fill($n, $v)`, `copy`, `isset($m[$k])`, `array_key_exists`, `unset($m[$k])`, `$m[$k] ?? $default`, `foreach ($m as $k => $v)`
- string: `strlen`, `substr` (รองรับเลขติดลบแบบ PHP), `strpos` (**ไม่เจอคืน -1** ไม่ใช่ false), `str_contains`, `str_starts_with`, `str_ends_with`, `str_repeat`, `strtolower`, `strtoupper`, `trim`, `intval`, `strval`
- อื่นๆ: `json_encode`, `sqrt`, `intdiv`, cast `(int)` `(float)` `(string)`, `?:`
- HTTP: `serve($port, "handler")`, `http_status($code)`

## HTTP server

```php
function handle(string $method, string $path, string $body): string {
    if ($path == "/hello") { return "{\"message\":\"hello world\"}"; }
    http_status(404);
    return "{\"error\":\"not found\"}";
}
serve(8080, "handle");
```

ได้ไฟล์เดียวที่เป็น web server เลย รัน 1 thread ต่อ 1 core (เปลี่ยนด้วย `PN_THREADS=4`) รองรับ HTTP/1.1 keep-alive และ pipelining

ตัวอย่างเต็มอยู่ที่ `api/server.pn` (3 endpoint) พร้อมตัวเดียวกันที่เขียนด้วย Go และ PHP

## ยังไม่มีใน prototype นี้

- class/object, null/nullable, `list<string>`, map ซ้อน map, `explode`
- try/catch
- คำสั่ง `go` สำหรับงานพร้อมกันทั่วไป (ตอนนี้มีแค่ server ที่รันหลาย thread ให้เอง)
- HTTP: TLS, HTTP/2, header ของ request/response, timeout, จำกัดขนาด request
- json_decode, database
- ตรวจ int overflow

## Benchmark: API server

ทุก server รันบน CPU core 0 ส่วนตัวยิงโหลด (wrk, 64 connections, 10 วินาที) รันบน core 1 ทุก server ตอบ byte ตรงกันทุก endpoint

| endpoint | ใหม่ | Go net/http | PHP FrankenPHP worker | PHP classic (php -S) |
|---|---|---|---|---|
| /hello | 132,268 req/s * | 41,696 | 10,439 | 4,703 |
| /user/42 (JSON) | 94,897 | 37,999 | 10,887 | 4,441 |
| /primes/20000 (CPU) | 17,019 | 14,295 | 3,518 | 2,324 |
| หน่วยความจำ server | 2.3 MB | 13 MB | 102 MB | 96–102 MB |

\* ตอน /hello server ใช้ CPU แค่ 80% ตัวยิงโหลดตันก่อน ความสามารถจริงสูงกว่านี้

ข้อควรรู้:
- server ของภาษาใหม่ทำงานน้อยกว่า Go net/http มาก (ไม่มี TLS, HTTP/2, timeout, header API) ส่วนต่างใน /hello กับ /user ส่วนหนึ่งมาจากตรงนี้
- /primes วัดความเร็วของภาษาตรงที่สุด: เร็วกว่า Go 1.2 เท่า, เร็วกว่า PHP worker 4.8 เท่า
- FrankenPHP ใช้ `opcache.jit=function` เพราะโหมด `tracing` ทำให้ FrankenPHP 1.13.2 / PHP 8.5.12 crash ที่ endpoint /primes
- php -S ปิด connection ทุก request (ไม่มี keep-alive) และเป็น server สำหรับ dev จึงเป็นตัวแทนแบบ PHP-FPM ได้ไม่สมบูรณ์

รันเอง: เปิด server ทั้ง 4 ตัวแล้ว `python3 api/loadtest.py <path-to-wrk>`

## Benchmark: CPU

ผลบนเครื่องทดสอบ (Linux 2 cores, เวลาดีสุดจาก 3 รอบ / หน่วยความจำสูงสุด) ทุกภาษาได้ผลลัพธ์ตรงกัน

| benchmark | new | rust | go | php-jit | php | python |
|---|---|---|---|---|---|---|
| fib | 0.02s / 2MB | 0.03s / 2MB | 0.04s / 2MB | 0.14s / 34MB | 0.41s / 31MB | 0.88s / 8MB |
| sieve | 0.13s / 21MB | 0.14s / 21MB | 0.13s / 21MB | 1.26s / 339MB | 2.44s / 336MB | 4.67s / 161MB |
| collatz | 0.17s / 2MB | 0.17s / 2MB | 0.25s / 2MB | 1.25s / 34MB | 2.66s / 31MB | 9.63s / 8MB |
| pi | 0.13s / 2MB | 0.10s / 2MB | 0.12s / 2MB | 0.82s / 34MB | 1.76s / 31MB | 11.13s / 8MB |
| listsum | 0.28s / 40MB | 0.27s / 40MB | 1.20s / 162MB | 1.75s / 239MB | 2.01s / 236MB | 7.31s / 312MB |

รันเอง: `python3 bench/run.py` ผลอยู่ที่ `bench/results.json`
