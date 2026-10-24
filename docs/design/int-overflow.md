# Pin — Integer overflow (B4)

สถานะ (2026-10-24): **S1–S3 ทำแล้ว** (เช็คขนาดหน่วยความจำ, `intdiv(INT_MIN,-1)`, `(int)` ของ float) และ `wrapping_*()` มีแล้ว
**O1 (panic เมื่อ `+ - *` ล้น) ทำแล้วแต่ถอดออกชั่วคราว** ตามที่ owner สั่ง เพราะทำให้ benchmark ตก (collatz +23%, sieve +10%) — ตอนนี้ `+ - *` วนรอบ 64-bit แบบมีพฤติกรรมชัดเจน (ไม่ใช่ undefined behavior) จะใส่กลับเมื่อทำ O3 (ตัด check ที่พิสูจน์ได้) จนค่าใช้จ่ายเล็กพอ
ร่างเดิม: owner ให้ตัดสินตามกฎ safety > ความเร็ว > อ่านง่าย > เหมือน PHP

## การตัดสินใจ

| # | เรื่อง | เลือก |
|---|---|---|
| O1 | `+ - *` กับ int ล้น | **panic เสมอ** ทั้ง debug และ release (ไม่มี flag ปิด เหมือน bounds check) |
| O2 | ต้องการวนรอบจริง (hash, checksum) | เขียนชัดๆ: `wrapping_add()`, `wrapping_sub()`, `wrapping_mul()` |
| O3 | ลดค่าใช้จ่าย | compiler ตัด check ที่พิสูจน์ได้ว่าไม่ล้น + `array_sum()` ในตัว |
| O4 | ขนาดหน่วยความจำใน runtime | เช็คการคูณ/บวกขนาดทุกจุดเสมอ (แยกจาก O1 — เป็น memory safety) |

ตัดทิ้ง: วนรอบเงียบๆ (บั๊กเรื่องเงินเงียบ), แบบ Rust ที่ release วนรอบ (production เจอบั๊กเงียบ), กลายเป็น float แบบ PHP (ขัด invariant ข้อ 1 และช้า)

## ต้องแก้ก่อน ไม่ว่าเลือกแบบไหน (memory safety / DoS)

พบจากการอ่านโค้ด ยังไม่ได้รันพิสูจน์ (runtime ยัง build บน Mac ไม่ได้)

| # | ปัญหา | ที่ | ผล |
|---|---|---|---|
| S1 | `fill($n)` คำนวณ `cap * esize` โดยไม่เช็คล้น → จองได้ 1 byte แต่ `len = n` → bounds check ผ่าน → เขียนเลยขอบ | `runtime.c` `pn_list_fill` (บรรทัด ~48) | **heap overflow** |
| S2 | การคูณขนาดแบบเดียวกันใน `pn_list_grow`, `pn_list_copy`, การขยาย string (`s->cap * 2`, `s->len + extra`) | `runtime.c` | ต้องตรวจทุกจุด |
| S3 | `intdiv(PHP_INT_MIN, -1)` และ `PHP_INT_MIN % -1` เป็น undefined behavior ใน LLVM — x86 crash ทั้ง process | `pinac.py` `binary()` (บรรทัด ~1192) เช็คแค่หารศูนย์ | **DoS**: ส่งเลขนี้มาใน URL ทำ server ล่ม |

แก้: S1/S2 ใช้ `__builtin_mul_overflow` / `__builtin_add_overflow` ทุกจุดที่คำนวณขนาด ล้น = panic · S3 เพิ่ม check `rv == -1 && lv == INT64_MIN` → panic สำหรับ `intdiv`; สำหรับ `%` ผลคือ 0 (ตรงกับคณิตศาสตร์) ไม่ต้อง panic
ทุกข้อต้องมี test ใน `tests/panics/` และผ่าน ASAN

## O1 — ทำงานยังไง

- codegen ใช้ `llvm.sadd.with.overflow.i64` / `ssub` / `smul` แล้ว branch ไป `@pn_overflow(line)` (มี `!prof` ว่าไม่น่าเกิด เหมือนที่ทำกับหารศูนย์)
- `-$x` เมื่อ `$x == PHP_INT_MIN` ล้นด้วย → panic
- `++` / `--` / `+=` / `-=` / `*=` ใช้กติกาเดียวกัน
- `intval("99999999999999999999")` — ตามหลักเดียวกัน: panic ไม่ใช่ตัดเป็น MAX แบบ PHP *(ทางเลือก: ทำ `intval()` แบบ `throws` ให้ handler ตอบ 400 ได้ — ตัดสินพร้อม json_decode)*
- ใน server: panic = ทิ้ง request ตอบ 500 (error-handling.md E-R6)

```
orders.pn:14: panic: integer overflow in $price * $qty (9223372036854775807 * 2).
  Use wrapping_mul() if wrapping around is what you want
```

## O3 — ลดค่าใช้จ่าย

1. **ตัด check ที่พิสูจน์ได้** — เริ่มจากกรณีที่เจอบ่อยที่สุด: ตัวนับ loop `for ($i = A; $i < $n; $i++)` ที่ `$i` ไม่ถูกแก้ใน loop → `$i + 1 <= $n <= MAX` ไม่ล้น · LLVM ช่วยต่อได้เองบางส่วน
2. **`array_sum(list<int>)`** — loop บวกเลขแบบ `$total += $x` คือจุดที่ check ทำให้ SIMD หาย ทำในตัวด้วยการบวกเป็นก้อนแล้วเช็คทีหลัง (เช่นบวกแบบ 128-bit ต่อก้อน) เร็วใกล้เดิมและยังปลอดภัย
3. float ไม่เปลี่ยน (IEEE 754: ล้นเป็น INF ไม่ใช่ปัญหา memory)

## ต้องวัด (หลัง Task 1 build บน Mac ได้)

รัน `bench/*.pn` และ `api/server.pn` แบบมี/ไม่มี check เทียบกัน — จุดเสี่ยงคาดว่า `listsum` (`$total += $x`) และ `collatz` (`3 * $n + 1`) ถ้าช้าลงเกินที่รับได้ค่อยทำ O3 เพิ่ม ไม่ใช่ถอย O1
