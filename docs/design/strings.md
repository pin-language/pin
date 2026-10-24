# Pin — String และภาษาไทย

สถานะ: **ร่าง ยังไม่ implement** (2026-10-24) · owner ตกลงตามที่เสนอ · หลักการ: safety > ความเร็ว > อ่านง่าย > เหมือน PHP

## ข้อเท็จจริงที่เช็คแล้ว (Python unicodedata)

- `"ไทย"` = 3 ตัวอักษร = 9 byte
- `"ที่"` = 3 code point (ท + ี + ่) = 9 byte แต่คนเห็นเป็น 1 ช่อง
- `"ำ"` (U+0E33) มี compatibility decomposition เป็น U+0E4D U+0E32 → NFKC ทำให้สองแบบเท่ากัน, NFC ไม่ทำ

## กติกา

### ST1 — `string` เป็น UTF-8 ที่ถูกต้องเสมอ (invariant)
- เช็คที่ขอบเท่านั้น: path/body ของ request, ไฟล์, database, `bytes_to_string()`
- operation บน string ที่ถูกต้องให้ผลที่ถูกต้องเสมอ ไม่ต้องเช็คซ้ำ: `.`, `.=`, interpolation, `str_repeat`, `strtolower`, `trim`, `json_encode`, `substr` (ด้วย ST3)
- เช็คด้วย SIMD (แบบ simdutf) ความเร็วระดับ GB/s
- ปฏิเสธ overlong encoding, surrogate (U+D800–DFFF), ค่าเกิน U+10FFFF
- request ที่ path หรือ body ไม่ใช่ UTF-8 ที่ถูกต้อง → server ตอบ 400 เอง ไม่เข้า handler (ยกเว้น handler ที่รับ `bytes` เมื่อมี ST2)

### ST2 — `bytes` สำหรับข้อมูล binary (รอบหลัง)
- ไฟล์ upload, รูป, hash, ข้อมูลเข้ารหัส
- ใช้แทน `string` ไม่ได้ · `bytes_to_string(bytes $b): string throws` · `string_to_bytes()` ไม่มีวันล้มเหลว
- ยังไม่ต้องทำในรอบแรก

### ST3 — `strlen` / `substr` / `strpos` เป็น byte, O(1)
- ชื่อและความหมายเหมือน PHP (strpos ไม่เจอคืน -1 ตามกติกาเดิม)
- `substr` ที่จุดเริ่มหรือจุดจบตกกลางตัวอักษร → **panic**
```
text.pn:4: panic: substr() would cut a character in half at byte 4 of "ไทย".
  Use mb_substr() to count characters, or grapheme_substr() to count what people see
```
- ตำแหน่งจาก `strpos` / `str_*` ไม่มีวันตกกลางตัวอักษร (needle เป็น UTF-8 ที่ถูกต้อง) → `substr($s, strpos($s, $x))` ไม่มีวัน panic

### ST4 — นับเป็นตัวอักษร: ชื่อ PHP ที่มีอยู่แล้ว
| function | นับเป็น | `"ที่"` | ความเร็ว |
|---|---|---|---|
| `strlen` | byte | 9 | O(1) |
| `mb_strlen` / `mb_substr` / `mb_strpos` | code point | 3 | O(n) |
| `grapheme_strlen` / `grapheme_substr` | ช่องที่คนเห็น (UAX #29) | 1 | O(n) |

- **ตัดข้อความไทยไปแสดงผล ใช้ `grapheme_substr`** (ไม่งั้นวรรณยุกต์/สระลอย)
- string เก็บ flag "ASCII ล้วน" ไว้ (คำนวณตอนเช็ค ST1 หรือตอนสร้าง) → ถ้า ASCII ล้วน `mb_*` / `grapheme_*` เร็วเท่า `strlen`
- layout เดิม `{len, cap, bytes}` + flag 1 bit (เก็บใน bit สูงของ `cap` หรือ field ใหม่ — ตัดสินตอน implement)

### ST5 — ตัวพิมพ์
- `strtolower` / `strtoupper` เปลี่ยนเฉพาะ A–Z (เหมือน PHP 8.2+) ไม่ขึ้นกับ locale
- `mb_strtolower` / `mb_strtoupper` สำหรับ Unicode
- ภาษาไทยไม่มีตัวพิมพ์ ไม่กระทบ

### ST6 — normalize: ไม่อัตโนมัติ มีให้เรียก
- `normalizer_normalize(string $s, int $form): string` · `NFC`, `NFKC`
- เอกสารต้องเขียนชัด: **username, email, key ที่ใช้ค้น/เทียบ → NFKC ก่อนเก็บและก่อนเทียบเสมอ** (กรณี "ำ" ปลอมชื่อได้)

### ST7 — ยังไม่ทำ (ต้องใช้ ICU, ทำเป็น library ภายหลัง)
- เรียงตามพจนานุกรมไทย (`<` เรียงตาม code point: สระหน้า เ แ โ ใ ไ ทำให้ลำดับผิด)
- ตัดคำภาษาไทย (ไม่มีช่องว่างระหว่างคำ)
- เลขไทย ๑๒๓ ใน `intval()` — ไม่รองรับ (ASCII เท่านั้นเหมือน PHP)

## งานที่ต้องทำใน runtime ตอน implement

1. validator UTF-8 (เริ่มแบบ scalar ที่ถูกต้องก่อน แล้วค่อยเปลี่ยนเป็น SIMD พร้อม test ชุดเดียวกัน)
2. เช็ค boundary ใน `pn_substr` (byte ที่ตำแหน่งนั้นต้องไม่ใช่ `10xxxxxx`)
3. `mb_*` + `grapheme_*` (ตาราง Grapheme_Cluster_Break จาก Unicode — ฝังเฉพาะช่วงที่ต้องใช้ หรือทั้งตาราง ~ไม่กี่ KB)
4. test: ไทย, emoji, combining mark, overlong (`C0 AF`), surrogate (`ED A0 80`), ตัดกลางตัวอักษร → panic
