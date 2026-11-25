# Pin

**ภาษาโปรแกรมสำหรับงาน web backend ที่ปลอดภัยเรื่องหน่วยความจำ ไวยากรณ์คล้าย PHP และ compile เป็น native code**

[![test](https://github.com/pin-language/pin/actions/workflows/test.yml/badge.svg)](https://github.com/pin-language/pin/actions/workflows/test.yml)
[![version](https://img.shields.io/badge/version-0.9.0_preview-orange)](CHANGELOG.md)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

[English](README.md) · ภาษาไทย

Pin เป็นภาษาโปรแกรมภาษาใหม่ ไฟล์ซอร์สใช้นามสกุล `.pn` ผู้ที่เขียน PHP อ่านโค้ด Pin ได้ทันที แต่ Pin จะถูก
compile ล่วงหน้าผ่าน LLVM ออกมาเป็นไฟล์โปรแกรม native ไฟล์เดียว Pin ไม่มี garbage collector หน่วยความจำจัดการด้วย
กฎ ownership ที่ compiler ตรวจสอบให้ คล้าย Rust แต่เรียบง่ายกว่า Pin ออกแบบมาสำหรับ JSON API และ web backend
อื่นๆ ให้ทั้งคนและ AI coding agent เขียนได้

> **สถานะ: public preview (0.9.0)** เปิดให้ทดลองใช้และให้ความเห็น ตัวภาษายังมีการเปลี่ยนแปลงระหว่างเวอร์ชัน
> **ยังไม่ควรนำไปใช้ใน production**

> **เหตุใด GitHub จึงแสดงภาษา Python และ C สำหรับ repository นี้** compiler ของ Pin (`pinac.py`) เป็นต้นแบบที่เขียน
> ด้วย Python ส่วน runtime library เขียนด้วย C แต่โปรแกรมที่เขียนด้วย Pin ไม่ได้รันบน Python โปรแกรมเหล่านั้นถูก
> compile เป็นรหัสเครื่องโดยตรง เนื่องจาก GitHub ยังไม่รู้จักภาษา Pin ไฟล์ `.pn` จึงแสดงสีแบบ PHP ซึ่งมีไวยากรณ์ใกล้เคียงกัน

## สารบัญ

1. [หลักการออกแบบ](#หลักการออกแบบ)
2. [เริ่มต้นใช้งาน](#เริ่มต้นใช้งาน)
3. [ภาพรวมของภาษา](#ภาพรวมของภาษา)
4. [การสร้าง web backend](#การสร้าง-web-backend)
5. [ความปลอดภัย](#ความปลอดภัย)
6. [ประสิทธิภาพ](#ประสิทธิภาพ)
7. [โครงสร้างการทำงาน](#โครงสร้างการทำงาน)
8. [สถานะของโครงการ](#สถานะของโครงการ)
9. [เอกสาร](#เอกสาร)
10. [การใช้ Pin กับ AI coding agent](#การใช้-pin-กับ-ai-coding-agent)
11. [การมีส่วนร่วมและข้อเสนอแนะ](#การมีส่วนร่วมและข้อเสนอแนะ)
12. [สัญญาอนุญาต](#สัญญาอนุญาต)

## หลักการออกแบบ

การตัดสินใจด้านการออกแบบทุกเรื่องของ Pin ยึดลำดับความสำคัญดังนี้

1. **ความปลอดภัยของหน่วยความจำ** โปรแกรม Pin อ่านหรือเขียนหน่วยความจำที่ตนไม่ได้เป็นเจ้าของไม่ได้ การตรวจขอบเขต
   และขนาดเปิดอยู่เสมอ ไม่มี flag หรือ `unsafe` ให้ปิด
2. **ความเร็ว** compile เป็น native code ไม่มี garbage collector และไม่มี interpreter
3. **ความชัดเจน** ตัวแปรแต่ละตัวมี type เดียว เงื่อนไขต้องเป็น `bool` และข้อความ error บอกวิธีแก้
4. **ความคุ้นเคย** ไวยากรณ์ใกล้เคียง PHP 8 มากที่สุดเท่าที่สามข้อแรกยอมให้

## เริ่มต้นใช้งาน

### สิ่งที่ต้องมี

- macOS หรือ Linux (Windows ใช้ผ่าน WSL กับ Ubuntu ได้ แต่ยังไม่ได้ทดสอบ)
- Python 3.10 ขึ้นไป สำหรับรัน compiler
- clang สำหรับสร้างไฟล์โปรแกรม native

### การติดตั้ง

ไม่ต้องใช้ `sudo` สคริปต์จะตรวจสิ่งที่ต้องมี ติดตั้ง Pin ไว้ที่ `~/.pin` เชื่อมคำสั่ง `pin` ไว้ใน `~/.local/bin`
และรันโปรแกรมทดสอบให้

```bash
curl -fsSL https://raw.githubusercontent.com/pin-language/pin/HEAD/install.sh | sh
```

หากไม่มี clang ให้ติดตั้งด้วย `xcode-select --install` บน macOS หรือ `sudo apt install clang` บน Ubuntu/Debian
หาก shell แจ้งว่า `pin: command not found` สคริปต์ติดตั้งจะพิมพ์บรรทัดที่ต้องเพิ่มในไฟล์ตั้งค่าของ shell ไว้ให้

<details><summary>ติดตั้งด้วยตนเอง</summary>

```bash
git clone https://github.com/pin-language/pin.git ~/.pin
mkdir -p ~/.local/bin && ln -sf ~/.pin/pin ~/.local/bin/pin
pin ~/.pin/examples/hello.pn
```

</details>

อัปเดตโดยรันสคริปต์ติดตั้งอีกครั้ง ถอนการติดตั้งด้วย `rm -rf ~/.pin ~/.local/bin/pin`

### โปรแกรมแรก

```bash
pin -v
printf '%s\n' 'echo "สวัสดีจาก Pin\n";' > hello.pn
pin hello.pn
```

### คำสั่ง

| คำสั่ง | คำอธิบาย |
|---|---|
| `pin file.pn` | compile แล้วรัน (เหมือน `pin run file.pn`) |
| `pin build file.pn [-o out]` | compile เป็นไฟล์โปรแกรม native |
| `pin check file.pn` | ตรวจ type และ ownership โดยไม่สร้างโปรแกรม |
| `pin check --json file.pn` | เหมือนข้างบน แต่แสดง error เป็น JSON ทีละรายการ |
| `pin get github.com/user/repo@v1.0.0` | เพิ่ม package และบันทึกเวอร์ชันไว้ใน `pin.lock` |
| `pin version` | แสดงเวอร์ชัน |

โฟลเดอร์ [`examples/`](examples/) มีโปรแกรมสั้นๆ ให้เริ่มต้น ได้แก่ `hello`, `fizzbuzz`, `wordcount`, `ownership`,
`primes`, `api` (JSON server) และ `notes_api` (server ที่เก็บข้อมูลใน SQLite)

## ภาพรวมของภาษา

```php
function squares(int $n): list<int> {   // คืน list ใหม่ ผู้เรียกเป็นเจ้าของ
    list<int> $out = [];
    for ($i = 1; $i <= $n; $i++) { $out[] = $i * $i; }
    return $out;
}

function total(list<int> $xs): int {    // ยืม list มาอ่านอย่างเดียว ไม่มีการคัดลอก
    $sum = 0;
    foreach ($xs as $x) { $sum += $x; }
    return $sum;
}

$name = "world";
echo "Hello, $name!\n";
echo total(squares(10)), "\n";          // 385
```

### Type

| ประเภท | ตัวอย่าง |
|---|---|
| ค่าพื้นฐาน | `int`, `float`, `bool`, `string` (เป็น UTF-8 ที่ถูกต้องเสมอ) |
| Container | `list<int>`, `map<string, User>` (เรียงตามลำดับที่ใส่แบบ array ของ PHP) ซ้อนกันได้ |
| Class | `class User { ... }` ไม่มี inheritance |
| ค่าที่อาจเป็น null | `?User`, `?int` ต้องตรวจก่อนใช้ |
| ฟังก์ชัน | `fn(int): string` สำหรับ closure และชื่อฟังก์ชัน |

type ของตัวแปรกำหนดจากค่าแรกและเปลี่ยนไม่ได้ parameter และค่าที่คืนต้องระบุ type เสมอ

### Ownership

```php
$a = [1, 2, 3];
echo total($a);      // การเรียกฟังก์ชันให้ยืมไปอ่านอย่างเดียว $a ยังเป็นเจ้าของ
$b = $a;             // list และ map จะถูกย้าย (move) ตอนนี้ $b เป็นเจ้าของ
echo count($a);      // compile error: $a ถูกย้ายไปแล้วที่บรรทัด 3 ใช้ copy($a) ...
$c = copy($b);       // คัดลอกโดยระบุชัดเจน
$s = "text";
$t = $s;             // string ถูกคัดลอก เหมือน PHP
```

list, map และ string แต่ละค่ามีเจ้าของเพียงหนึ่งเดียว และถูกคืนหน่วยความจำเมื่อเจ้าของหมดขอบเขต compiler ตรวจ
ทุกเส้นทางในฟังก์ชันแยกกัน และข้อความ error บอกวิธีแก้โค้ด

```
ownership.pn:20: error: $a was moved on line 18 and can no longer be used here. Use copy($a) if you need two separate values
```

### การจัดการข้อผิดพลาด

ฟังก์ชันที่อาจล้มเหลวประกาศด้วย `throws` และทุกครั้งที่เรียกต้องจัดการด้วย `try` จะส่งต่อหรือดักจับก็ได้

```php
$body = try http_get("http://127.0.0.1:8080/health", [], 2000) catch ($e) { "down: " . $e->code; };
```

ข้อผิดพลาดถูก compile เป็นค่าที่คืนจากฟังก์ชันตามปกติ จึงไม่มีต้นทุนเมื่อไม่มีอะไรล้มเหลว

### ความแตกต่างหลักจาก PHP

| | PHP | Pin |
|---|---|---|
| type ของตัวแปร | เปลี่ยนได้ตลอด | คงที่ กำหนดจากค่าแรก |
| type ของ parameter และค่าที่คืน | ไม่บังคับ | บังคับ |
| array | `array` แบบเดียวใช้ทั้ง list และ map | `list<T>` และ `map<K, V>` |
| ไม่มี key ใน map หรือ index เกินขอบเขต | warning แล้วได้ `null` | หยุดทำงาน (`panic`) ใช้ `isset()` หรือ `??` |
| `if ($n)` | ตีความ truthy / falsy | เงื่อนไขต้องเป็น `bool` |
| แก้ array ที่ส่งเข้าฟังก์ชัน | แก้สำเนา | compile error ต้องเรียก `copy()` ก่อน |
| การจัดการหน่วยความจำ | reference counting และ garbage collection | ownership ตรวจตอน compile |
| int เกินขนาด | กลายเป็น float | วนรอบ (64 bit) |
| string | ไบต์ใดก็ได้ | UTF-8 ที่ถูกต้องเสมอ มี `mb_*` และ `grapheme_*` ในตัว |

ข้อความ error เขียนสำหรับคนที่รู้จัก PHP หากเขียน `extends`, `finally`, `switch`, `array()` หรือ `include`
จะได้คำอธิบายว่าใน Pin ต้องเขียนอย่างไร ไม่ใช่ syntax error เฉยๆ

### Module และ package

```php
import prices;                                   // prices.pn ในโฟลเดอร์โปรเจกต์
import github.com/someone/strutil/words;         // package ที่เพิ่มด้วย: pin get github.com/someone/strutil@v1.0.0
echo prices::price("tea"), " ", words::greet("pin"), "\n";
```

เรียกใช้จากไฟล์อื่นได้เฉพาะฟังก์ชันที่ระบุ `export` package คือ Git repository ใดก็ได้ `pin get` บันทึก commit และ
SHA-256 ของโค้ดไว้ใน `pin.lock` และทุกครั้งที่ build จะตรวจค่าเหล่านี้ ผลลัพธ์ยังคงเป็นไฟล์โปรแกรมไฟล์เดียว

## การสร้าง web backend

```php
function handle(string $method, string $path, string $body): string {
    if ($path == "/hello") { return json_encode(["message" => "hello"]); }
    http_status(404);
    return json_encode(["error" => "not found"]);
}
serve(8080, "handle");
```

| ด้าน | สิ่งที่ Pin มีให้ |
|---|---|
| HTTP server | HTTP/1.1 รองรับ keep-alive และ pipelining หนึ่ง thread ต่อ CPU core (เปลี่ยนด้วย `PN_THREADS`) ใช้ epoll บน Linux และ kqueue บน macOS มี timeout และจำกัดจำนวน connection รองรับ server-sent events |
| การอ่าน request | `url_path`, `query_param`, `form_param`, `request_header`, `json_decode` เข้า class, `json_string` / `json_int` และอื่นๆ |
| ฐานข้อมูล | SQLite และ PostgreSQL ผ่านฟังก์ชัน `db_*` ชุดเดียวกัน มี connection pool อ่านแถวเข้า class ได้ |
| การทำงานพร้อมกัน | `go` / `await` แบบมีโครงสร้าง และ Hub สำหรับ publish/subscribe |
| HTTP client | `http_get`, `http_post`, `http_stream` สำหรับ URL แบบ `http://` |
| ความปลอดภัย | `password_hash`, `hash_hmac`, `hash_equals`, `random_token`, `random_int` |

Pin ไม่มีตัวแปร global ดังนั้น handler แต่ละตัวไม่ใช้อะไรร่วมกันและไม่ต้องใช้ lock หาก handler ล้มเหลวขณะทำงาน
เฉพาะ client รายนั้นที่ได้ `500 Internal Server Error` หน่วยความจำทั้งหมดของ request นั้นถูกคืน และ server
ทำงานต่อ ส่วน TLS (https) ให้ reverse proxy เช่น Caddy หรือ nginx จัดการ ตามที่ออกแบบไว้
([design/tls.md](https://github.com/pin-language/docs/blob/HEAD/design/tls.md))

## ความปลอดภัย

**ตรวจตอน compile:** type, ownership (ใช้ค่าที่ย้ายไปแล้ว แก้ค่าที่ยืมมา แก้ list ระหว่าง `foreach`) และตัวแปรต้องมีค่า
ในทุกเส้นทาง

**ตรวจตอนรันเสมอ:** ขอบเขตของ list, key ที่ไม่มีใน map, การหารด้วยศูนย์, `intdiv(PHP_INT_MIN, -1)`, การแปลง float
เป็น int ที่ไม่ถูกต้อง และขนาดของการจองหน่วยความจำทุกครั้ง

**มีในตัว server และ library:**

- SQL ต้องเป็น string literal ที่ใช้ `?` แทนค่า SQL injection จึง compile ไม่ผ่าน
- response header ที่มี CR หรือ LF ถูกปฏิเสธ ป้องกัน header injection
- request ที่ header ไม่ตรงตาม RFC 9112 ถูกปฏิเสธด้วย `400` ป้องกัน request smuggling เมื่ออยู่หลัง proxy
- path และ body ของ request ต้องเป็น UTF-8 ที่ถูกต้อง
- hash ของ map ใช้ key สุ่มตอนเริ่มโปรแกรม ผู้โจมตีจึงส่ง key ที่ชนกันทั้งหมดไม่ได้
- HTTP client รับ host ที่เป็นตัวเลขเฉพาะรูปแบบ IPv4 ปกติ (`a.b.c.d`)

**วิธีทดสอบ:** ชุดทดสอบกว่า 300 รายการรันทุกครั้งที่มีการเปลี่ยนแปลง ทั้งบน Linux และ macOS ทั้งแบบปกติและภายใต้
AddressSanitizer กับ UndefinedBehaviorSanitizer นอกจากนี้มี fuzzer ใน [`tools/`](tools/) ที่ป้อนโปรแกรมที่สร้างขึ้น
JSON ที่ถูกดัดแปลง HTTP request และ response ข้อความ และ SQL ให้ compiler และ runtime

**ข้อจำกัดที่ทราบ:** `+ - *` ของ int วนรอบเมื่อเกินขนาดแทนการแจ้ง error เพราะการตรวจทำให้ลูปที่คำนวณ int ช้าลงถึง 23%
จึงเลื่อนไว้จนกว่า compiler จะตัดการตรวจที่พิสูจน์ได้ว่าไม่จำเป็นออกได้
([design/int-overflow.md](https://github.com/pin-language/docs/blob/HEAD/design/int-overflow.md))
และยังไม่มีการจำกัดหน่วยความจำต่อ request

**การแจ้งช่องโหว่:** กรุณาอย่าเปิด issue สาธารณะ ให้แจ้งผ่าน
[GitHub private vulnerability reporting](https://github.com/pin-language/pin/security/advisories/new)
(แท็บ *Security* แล้วเลือก *Report a vulnerability*)

## ประสิทธิภาพ

ใช้ algorithm เดียวกันในทุกภาษา และตรวจแล้วว่าผลลัพธ์ตรงกันทั้งหมด ทดสอบบน Apple M2 (8 core), macOS 14
ใช้ค่าดีที่สุดจาก 3 ครั้ง

| Benchmark | Pin | Rust 1.95 | Go 1.26 | PHP 8.5 + JIT | PHP 8.5 | Python 3.15 |
|---|---|---|---|---|---|---|
| sieve (20M) | 0.042 s | 0.043 s | 0.041 s | 0.652 s | 1.337 s | 4.283 s |
| collatz (1M) | 0.165 s | 0.131 s | 0.175 s | 0.714 s | 3.204 s | 6.302 s |
| pi (100M terms) | 0.090 s | 0.090 s | 0.091 s | 0.930 s | 2.235 s | 13.239 s |
| list build + sum | 0.114 s | 0.131 s | 0.183 s | 0.440 s | 1.192 s | 4.754 s |

**ข้อควรทราบ**

- เป็นลูปที่ใช้ CPU ขนาดเล็ก ไม่ใช่แอปพลิเคชันจริง และ macOS ไม่ให้ตรึงโปรแกรมไว้กับ CPU core ความต่างที่ต่ำกว่า
  ประมาณ 20% จึงควรถือว่าอยู่ในระดับความคลาดเคลื่อน
- ไม่รวม `fib` เพราะ clang ปรับแต่ง recursion นั้นในแบบที่ไม่ได้บอกอะไรเกี่ยวกับตัวภาษา
- การทดสอบ HTTP ก่อนหน้านี้บน Linux container 2 core วัดได้ 132k request/วินาทีสำหรับ Pin, 42k สำหรับ Go `net/http`
  และ 10k สำหรับ FrankenPHP ที่ endpoint "hello" แต่ server ของ Pin ทำงานน้อยกว่ามาก (ไม่มี TLS ไม่มี HTTP/2)
  จึงไม่ใช่การเปรียบเทียบตัวภาษาที่ยุติธรรม ส่วน endpoint ที่ใช้ CPU หนัก Pin ได้ 1.2 เท่าของ Go

โปรแกรม วิธีวัด และผลดิบ: [pin-language/benchmarks](https://github.com/pin-language/benchmarks)

## โครงสร้างการทำงาน

```
file.pn ──► pinac.py ───────────────────────────────────► LLVM IR ──► clang -O2 ──► native binary
            lexer → parser → type and ownership checker              + runtime (C)
```

| ตำแหน่ง | เนื้อหา |
|---|---|
| [`pinac.py`](pinac.py) | compiler (ต้นแบบ เขียนด้วย Python) |
| [`runtime/`](runtime/) | runtime library ภาษา C: หน่วยความจำ string map JSON task HTTP server และ client SQLite PostgreSQL Unicode และ cryptography |
| [`pin`](pin) | เครื่องมือบรรทัดคำสั่ง `pin` |
| [`spec/`](spec/) | คำอธิบายฟังก์ชันในตัวทุกตัว ใช้สร้างเอกสารอ้างอิง |
| [`tests/`](tests/) | ชุดทดสอบ |
| [`tools/`](tools/) | fuzzer สคริปต์ sanitizer และตัวสร้างเอกสาร |
| [`examples/`](examples/) | ตัวอย่างโปรแกรม |
| [`editors/vscode/`](editors/vscode/) | การแสดงสีไวยากรณ์สำหรับ Visual Studio Code |

## สถานะของโครงการ

**มีแล้วในเวอร์ชัน 0.9.0:** class, closure, null safety ด้วย `?T`, `throws` / `try` / `catch`, `go` / `await`,
module และ Git package, `json_decode` เข้า class, SQLite และ PostgreSQL, server-sent events, HTTP client,
ข้อความ Unicode (`mb_*`, `grapheme_*`, normalisation) และฟังก์ชันกับค่าคงที่ในตัว 210 รายการ

**ยังไม่มี:** inheritance และ interface, `finally`, type `bytes` สำหรับข้อมูล binary, การตรวจ int เกินขนาด, การจำกัด
หน่วยความจำต่อ request และ HTTP/2

ฟีเจอร์ที่วางแผนไว้มีเอกสารออกแบบอยู่ใน
[pin-language/docs/design](https://github.com/pin-language/docs/blob/HEAD/design/) และการเปลี่ยนแปลงทั้งหมดอยู่ใน
[CHANGELOG.md](CHANGELOG.md)

## เอกสาร

| เอกสาร | สำหรับ |
|---|---|
| [Learn Pin](https://github.com/pin-language/docs/blob/HEAD/learn/README.md) | บทเรียนสั้น 14 บทสำหรับผู้เริ่มต้น |
| [Language guide](https://github.com/pin-language/docs/blob/HEAD/language.md) | รายละเอียดภาษาทั้งหมด |
| [Built-in reference](https://github.com/pin-language/docs/blob/HEAD/reference/README.md) | ฟังก์ชันในตัวทุกตัวพร้อมตัวอย่าง |
| [Design documents](https://github.com/pin-language/docs/blob/HEAD/design/) | เหตุผลของการออกแบบภาษา |
| [AI guide](https://github.com/pin-language/docs/blob/HEAD/ai-guide.md) | ภาษาทั้งหมดในหน้าเดียว สำหรับ AI coding agent |

เอกสารทั้งหมดอยู่ที่ [pin-language/docs](https://github.com/pin-language/docs) (ภาษาอังกฤษ)

## การใช้ Pin กับ AI coding agent

AI coding agent ส่วนใหญ่เรียนรู้จากโค้ด PHP นิสัยหลายอย่างแบบ PHP จึง compile ใน Pin ไม่ผ่าน ก่อนให้ agent เขียนโค้ด
Pin ควรให้อ่าน [AI guide](https://github.com/pin-language/docs/blob/HEAD/ai-guide.md) ก่อน เอกสารนี้สรุปภาษาทั้งหมดใน
หน้าเดียว พร้อมตารางเทียบ PHP กับ Pin และตาราง error กับวิธีแก้

1. เพิ่มข้อความลักษณะนี้ใน `AGENTS.md` หรือ `CLAUDE.md` ของโปรเจกต์: *"This project is written in Pin. Read
   https://raw.githubusercontent.com/pin-language/docs/HEAD/ai-guide.md before writing code, and run
   `pin check --json` after every change."*
2. ให้ agent วนแก้ด้วย `pin check --json file.pn` ซึ่งแสดงหนึ่ง object ต่อหนึ่ง error
   (`{"file", "line", "severity", "message"}`) หรือ `{"file", "ok": true}` เมื่อไม่มี error และจบด้วย exit code 1
   เมื่อมี error
3. รันโปรแกรมด้วย `pin file.pn` error ระหว่างรันจะแสดง `panic: ...` พร้อมหมายเลขบรรทัด และจบด้วย exit code 1

ดูเพิ่มเติมที่ [AGENTS.md](AGENTS.md) และ [llms.txt](llms.txt)

## การมีส่วนร่วมและข้อเสนอแนะ

preview นี้จัดทำขึ้นเพื่อเรียนรู้ว่าคนและ AI agent พบว่าส่วนใดเป็นธรรมชาติหรือสับสน กรุณา
[เปิด issue](https://github.com/pin-language/pin/issues) เมื่อพบ

- โค้ดที่คาดว่าจะ compile ผ่านแต่ไม่ผ่าน และข้อความ error ช่วยได้หรือไม่
- สิ่งใดก็ตามที่ crash แทนการแสดง `panic:` ซึ่งถือเป็น bug เสมอ
- สิ่งที่ Pin ต้องมีก่อนที่คุณจะลองใช้กับ API จริง

ก่อนส่งการแก้ไข compiler หรือ runtime กรุณาอ่าน [CLAUDE.md](CLAUDE.md) เรื่องแนวทางและกฎที่ภาษาต้องคงไว้ แล้วรัน
ชุดทดสอบ (ประมาณหนึ่งนาที)

```bash
python3 tests/run_tests.py
```

## สัญญาอนุญาต

Pin เผยแพร่ภายใต้ [MIT License](LICENSE)
