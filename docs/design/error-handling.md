# Pin — Error handling

สถานะ: **ร่าง ยังไม่ implement** (2026-10-24) — ตอบ decision 4 ที่ค้างอยู่ใน HANDOFF แทน default เดิม (try/catch แบบ PHP)

หลักการ: memory safety > ความเร็ว > อ่านง่าย > เหมือน PHP (ดู `ownership-v2.md`)

## การตัดสินใจ

| # | เรื่อง | เลือก | ใครตัดสิน |
|---|---|---|---|
| E1 | รูปแบบ | **แบบ Swift/Zig**: `throws` + `try` ที่จุดเรียก + `catch` แต่ compile เป็น return value ไม่ใช่ unwinding | owner |
| E2 | ชนิดของ error | `Error{int code, string message}` แบบเดียว (error class ที่ประกาศเองรอหลังมี class) | ใช้หลัก owner: เร็วที่สุด ทำได้ทันที |
| E3 | error หลุดออกจาก handler | ตอบ status ตาม `code` ถ้าอยู่ในช่วง 400–599 ไม่งั้นตอบ 500 · **body ไม่ส่ง message ออกไป** ส่งแค่ชื่อ status มาตรฐาน · message ไปลง log | ใช้หลัก owner: security ก่อน (กันข้อมูลภายในรั่วไปถึง client) |

E2 กับ E3 ผมเลือกตามหลักของ owner ถ้าไม่ตรงใจเปลี่ยนได้ ยังไม่ได้ implement

## กติกา

### E-R1 — ประกาศ `throws`
```php
function findUser(int $id): User throws {
    if ($id <= 0) { throw error(400, "bad id"); }
    throw error(404, "user $id not found");
}
```
ฟังก์ชันที่ไม่ได้ประกาศ `throws` จะ `throw` หรือส่ง error ต่อไม่ได้ (compile error)

### E-R2 — เรียกต้องมี `try` เสมอ
```php
$u = try findUser($id);                       // error → ส่งต่อทันที (ฟังก์ชันนี้ต้อง throws ด้วย)
$u = try findUser($id) catch ($e) {           // จัดการเอง: block ต้อง return/throw หรือให้ค่าแทน
    http_status($e->code);
    return json_encode(["error" => $e->message]);
};
try { ... } catch ($e) { ... }                // แบบ block สำหรับหลายบรรทัด
$u = findUser($id);                            // compile error: findUser() can fail, write try findUser(...)
```

### E-R3 — ทำงานจริงยังไง
- ฟังก์ชัน `throws` คืนค่า 2 ช่อง: ค่าปกติ + ตัวชี้ error (null = สำเร็จ) ผ่าน register
- `try` = เช็ค 1 branch หลังเรียก ถ้ามี error → คืนของที่ถืออยู่ตามกติกา ownership เดิม แล้ว return ต่อ
- ไม่มีตารางสำหรับ unwinding ไม่มีการไล่ stack ใช้ทาง return ที่ผ่าน ASAN อยู่แล้ว
- `throw error(404, "literal")` ไม่ต้องจองหน่วยความจำ (string literal `cap = -1`) ส่วน `"user $id not found"` จองแค่ตอน throw จริง

### E-R4 — ownership กับ error
- ตัวเช็ค ownership นับ `try` ทุกตัวเป็นทางออกจากฟังก์ชัน: ของที่เป็นเจ้าของอยู่ถูกคืน ของที่ย้ายไปแล้วไม่ถูกคืนซ้ำ (ใช้ control-flow analysis ชุดเดียวกับ R2 ใน ownership-v2 และ B3)
- ของที่ยืมด้วย `&` อาจถูกแก้ไปครึ่งทางก่อน error — ไม่ใช่ปัญหา memory safety แต่ต้องเขียนไว้ใน docs
- `$e->message` เป็นของ `$e` ใน catch block ย้ายออกได้ (return ได้)

### E-R5 — handler ของ `serve()`
```php
function handle(string $method, string $path, string $body): string throws { ... }
```
- error หลุดออกมา → status ตาม E3, body `{"error":"Not Found"}` (ชื่อ status มาตรฐาน), log `code + message + path` ไปที่ stderr
- server ทำ request ถัดไปต่อตามปกติ

### E-R6 — panic แยกจาก error
| | error (`throws`) | panic |
|---|---|---|
| ใช้กับ | เรื่องที่คาดไว้: input ผิด หาของไม่เจอ | bug: index เกิน, หารศูนย์, map ไม่มี key, จองหน่วยความจำเกินขนาด |
| จับได้ด้วย `catch` | ได้ | **ไม่ได้** |
| ใน server | ตอบตาม E3 | ทิ้ง request นั้นทั้งหมด ตอบ 500 ทำ request ถัดไปต่อ (แก้ B2) |
| นอก server | ถ้าหลุดจาก top-level: พิมพ์แล้ว exit(1) | พิมพ์แล้ว exit(1) |

panic ใน server ปลอดภัยเพราะไม่มีตัวแปร global: ข้อมูลของ request ไม่ปนกับ request อื่น วิธีทำ: per-request arena + setjmp/longjmp ใน worker (ตามที่ HANDOFF Task 2 เสนอไว้)

## ตัวอย่าง error message

```
api.pn:12: error: findUser() can fail. Write try findUser($id), or handle it with try findUser($id) catch ($e) { ... }
api.pn:20: error: throw is only allowed in a function declared with throws. Add throws after the return type
api.pn:31: error: this catch block must return, throw, or give a value of type User
```

## ลำดับ implement ที่แนะนำ

1. Task 1 ใน HANDOFF (build บน Mac) — ทุกอย่างต่อจากนี้ต้องรันบน Mac ได้ก่อน
2. control-flow analysis ร่วม (ใช้กับ B3, R2 ของ ownership-v2 และ E-R4)
3. `throws` / `try` / `catch` + handler (E-R1 ถึง E-R5)
4. panic → 500 ด้วย arena (E-R6 = HANDOFF Task 2)
5. วัดความเร็ว: request ที่สำเร็จ และ request ที่ตอบ 404 เทียบกับก่อนเพิ่ม `throws`
