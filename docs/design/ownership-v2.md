# Pin — Ownership v2: `list<string>`, container ซ้อน, class

สถานะ: **ร่าง ยังไม่ implement** (2026-10-24)

## หลักการตัดสินใจ (owner เลือก)

เรียงตามลำดับความสำคัญ:

1. **memory safety** — ห้ามมี use-after-free, double free, อ่าน/เขียนนอกขอบ **owner ยืนยันแล้ว (2026-10-24): อยู่เหนือความเร็ว bounds check ต้องอยู่ตลอด ไม่มี `unsafe` ไม่มี flag ปิด check** ทางทำให้เร็วคือให้ compiler/LLVM พิสูจน์แล้วตัด check ที่ซ้ำออกเอง
2. **ความเร็วตอนรัน** — ไม่มีค่าใช้จ่ายที่ซ่อนอยู่ ไม่มี GC ไม่มี refcount ไม่มี copy ที่ไม่ได้สั่ง
3. ความชัดของโค้ด — อ่านแล้วเห็นว่าตรงไหนแก้ข้อมูล ตรงไหน copy
4. **ความเหมือน PHP — สำคัญน้อยที่สุด** ใช้ syntax PHP ได้เมื่อไม่ขัดกับข้อ 1–3

## การตัดสินใจ

| # | เรื่อง | เลือก | เหตุผล |
|---|---|---|---|
| D1 | object เป็น value หรือ handle | **value** มีเจ้าของคนเดียว ย้ายเมื่อ assign เหมือน list | ไม่มี refcount ตอนรัน |
| D2 | `$u = $users[0];` | **ยืมแบบอ่านอย่างเดียว** ไม่ copy ไม่ error | เร็วที่สุด (เปลี่ยนจากที่แนะนำเดิมว่าให้ error — การยืมเร็วกว่าและใช้ง่ายกว่า) |
| D3 | `&` ตอนเรียกฟังก์ชัน | **ต้องเขียนทั้งตอนประกาศและตอนเรียก** `discount(&$o)` | ไม่กระทบความเร็ว จึงเลือกที่อ่านชัดกว่า (ข้อ 3) แทนแบบ PHP (ข้อ 4) |
| D4 | string อ่านจาก list/map | ใช้กติกาเดียวกับ D2: ยืม ไม่ copy | ไม่มี copy ซ่อน |

## กติกา

### R1 — ทุก heap value มีเจ้าของคนเดียว
list, map, string, object เหมือนเดิม คืนหน่วยความจำเมื่อเจ้าของหมดอายุ
```php
$b = $a;          // list / map / object: ย้าย ใช้ $a ต่อไม่ได้
$t = $s;          // string: ยังเป็น copy เหมือนเดิม (กติกาเดิม ไม่เปลี่ยน)
```

### R2 — อ่านผ่าน index = ยืมในฟังก์ชันเดียว (ใหม่)
```php
$u = $users[0];            // $u ยืม $users[0] แบบอ่านอย่างเดียว ไม่ copy
echo $u->name;             // ได้
$u->name = "x";            // error: $u ยืมมา แก้ไม่ได้
$users[] = $new;           // error: $users ถูกยืมโดย $u (ยังใช้อยู่บรรทัด 7)
echo $u->name;
```
- การยืมอยู่ได้ **แค่ในฟังก์ชันเดียว** และจบเมื่อใช้ `$u` ครั้งสุดท้าย (แบบ non-lexical lifetime ของ Rust แต่ไม่ข้ามฟังก์ชัน)
- ตัวแปรยืมเก็บลง list/map/field ไม่ได้ และ return ไม่ได้ → compiler ไม่ต้องมี lifetime annotation
- ต้องการของตัวเอง: `copy($users[0])` ต้องการย้ายออก: `array_pop()`, `array_shift()`, `take($users, $i)`

### R3 — แก้ผ่าน path ได้เลย
```php
$users[0]->name = "x";
$m["a"]["b"] += 1;         // map ซ้อน map
$order->items[] = "tea";   // list<string> ใน object
```

### R4 — `&` = ยืมแบบแก้ได้ ต้องเขียนทั้งสองฝั่ง
```php
function discount(Order &$o): void { $o->total -= 10; }

discount(&$order);         // ได้
discount($order);          // error: ต้องเขียน &$order
discount(&$orders[0]);     // ได้
swap(&$a[0], &$a[1]);      // error: ยืม $a แบบแก้ได้ 2 ครั้งพร้อมกัน
```
- ระหว่างยืมด้วย `&` ห้ามใช้ต้นทางทางอื่น (exclusive)
- parameter ที่ไม่มี `&` ยังเป็นยืมอ่านอย่างเดียวเหมือนเดิม

### R5 — foreach
```php
foreach ($users as $u)  { echo $u->name; }    // ยืมอ่าน
foreach ($users as &$u) { $u->age += 1; }     // ยืมแก้ได้ (ไม่มี copy)
```
ห้ามเพิ่ม/ลบของใน container ระหว่างวน (กติกาเดิม)

### R6 — method
- method ที่ assign `$this->...` หรือส่ง `$this` แบบ `&` ไปต่อ = **method แบบแก้ได้** compiler อนุมานเอง ไม่ต้องเขียน keyword
- เรียก method แบบแก้ได้ต้องมีสิทธิ์แก้ receiver: ตัวแปรที่เป็นเจ้าของ, `&` param หรือ path ของมัน
```php
function show(Order $o): void { $o->recalc(); }   // error: recalc() แก้ $o แต่ $o ยืมมาอ่านอย่างเดียว
```

### R7 — ไม่มี reference ที่ชี้กันไปมา
object ชี้ parent ไม่ได้ ใช้ id/index แทน (`int $parentId`) ถ้าโค้ดจริงต้องใช้จริง ค่อยพิจารณา `shared class` แบบ refcount ทีหลัง — **ยังไม่ทำ**

## ตัวอย่าง error message

```
users.pn:7: error: cannot append to $users while $u is borrowing $users[0] (used again on line 8).
  Use $u for the last time before appending, or use copy($users[0]) to keep your own value
users.pn:4: error: $u is borrowed from $users[0] and cannot be changed.
  Change $users[0]->name directly, or use copy($users[0])
orders.pn:12: error: discount() can change its argument, so write discount(&$order)
orders.pn:15: error: $a is borrowed for changing twice in the same call (&$a[0], &$a[1])
orders.pn:20: error: recalc() changes $o, but $o is a read-only parameter.
  Declare it as Order &$o, or work on copy($o)
```

## ค่าใช้จ่ายของ implement (ประเมิน ยังไม่ได้วัด)

- R2 ต้องมี liveness analysis ต่อฟังก์ชัน (หาว่าใช้ตัวแปรครั้งสุดท้ายบรรทัดไหน) งานใหม่ที่ใหญ่ที่สุดในสเปคนี้
- R4/R6 เช็คได้ในฟังก์ชันเดียว เพราะไม่มี reference ถูกเก็บไว้
- ควรแก้ B3 (move ไม่แยก branch) ไปพร้อม R2 เพราะใช้ control-flow analysis ชุดเดียวกัน

## คำถามที่ยังเปิด

ไม่มี

## ตัดสินแล้ว

- ~~concurrency (decision 3A)~~ → `go` + `await` แบบ structured ดู `concurrency.md` · `null.md` · `strings.md` (owner, 2026-10-24)
- ~~int overflow (B4)~~ → panic เสมอ + `wrapping_*()` + เช็คขนาดใน runtime ทุกจุด ดู `int-overflow.md` (ตามกฎของ owner, 2026-10-24)
- ~~ชื่อภาษา~~ → **Pin** นามสกุล `.pn` เหมือนเดิม (owner, 2026-10-24)
- ~~error handling (decision 4)~~ → แบบ Swift/Zig (`throws` + `try` + `catch` compile เป็น return value) ดู `error-handling.md` (owner, 2026-10-24)
- ~~memory safety อยู่เหนือความเร็วไหม?~~ → ใช่ bounds check อยู่ตลอด (owner, 2026-10-24)
