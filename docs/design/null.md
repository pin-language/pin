# Pin — Null

สถานะ: **ร่าง ยังไม่ implement** (2026-10-24) · owner ตกลงตามที่เสนอ · หลักการ: safety > ความเร็ว > อ่านง่าย > เหมือน PHP

## กติกา

### N1 — ไม่เป็น null โดย default
ทุก type ห้ามเป็น null เว้นแต่เขียน `?` นำหน้า: `?User`, `?int`, `?string`, `?list<int>`
```php
function findUser(int $id): ?User { ... }
function name(User $u): string { ... }      // $u ไม่มีวันเป็น null
```
`null` ใส่ได้เฉพาะตัวแปร/parameter/return/property ที่เป็น `?T`

### N2 — ต้องเช็คก่อนใช้ compiler รู้เอง (flow typing)
```php
$u = findUser($id);
echo $u->name;                  // error
if ($u !== null) { echo $u->name; }        // ได้
if ($u === null) { return "{}"; }
echo $u->name;                  // ได้
```
- รูปที่ compiler รู้: `!== null`, `=== null` + return/throw/break/continue, `isset($u)`, `&&` / `||`
- ถ้า `$u` ถูก assign ใหม่หลังเช็ค ต้องเช็คใหม่
- property `$u->x` ที่เป็น `?T` เช็คแล้วรู้ได้เฉพาะเมื่อ `$u` ไม่ถูกแก้/ส่ง `&` ระหว่างนั้น
- ใช้ control-flow analysis ร่วมกับ B3, ownership-v2 R2, error-handling E-R4

### N3 — syntax PHP 8
```php
$name = $u?->name;                                   // ?string
$name = $u?->name ?? "guest";                        // string
$len  = $u?->getName()?->length();                   // ต่อกันได้
$u = findUser($id) ?? throw error(404, "not found"); // User
```

### N4 — ไม่มี force-unwrap (`!` / `!!`)
ใช้ `?? throw error(...)` แทน — เห็นชัดว่า null แล้วเกิดอะไร

### N5 — property ต้องมีค่าครบหลัง constructor (compile time)
```php
class User {
    public int $id;
    public string $name;
    public ?string $email;           // ?T ได้ค่า null อัตโนมัติถ้าไม่ได้กำหนด
    function __construct(int $id, string $name) { $this->id = $id; }
}
// error: User::__construct() does not set $this->name on every path
```
ห้ามเรียก method ของ `$this` ใน constructor ก่อนกำหนด property ครบ

### N6 — layout (ความเร็ว)
| type | เก็บยังไง | ค่าใช้จ่าย |
|---|---|---|
| `?User`, `?string`, `?list<T>`, `?map<K,V>` | pointer, null = 0 | ไม่มี |
| `?int`, `?float`, `?bool` ในตัวแปร/arg/return | `{i64, i1}` ส่งผ่าน 2 register | น้อยมาก |
| `list<?int>` ฯลฯ | ข้อมูลเดิม + bitmap แยก 1 bit/ช่อง | หน่วยความจำ +1.6% |
| `map<K, ?V>` | ใช้ bit ใน entry ที่มีอยู่ | ไม่มี |

### N7 — `?T` หรือ `throws`
- **ไม่เจอเป็นเรื่องปกติ** → `?T` (หา user, header ที่ไม่บังคับ, field ที่ไม่บังคับใน JSON)
- **ล้มเหลวและมีเหตุผล** → `throws` (JSON ผิดรูป, database ล่ม, input ไม่ผ่าน)
- `$m[$k]` key ไม่มี → panic เหมือนเดิม · `$m[$k] ?? x` ใช้ได้เหมือนเดิม

### N8 — `json_encode`
`null` → `null` · property ที่เป็น null ใส่ใน output (`"email":null`) เหมือน PHP

## ตัวอย่าง error message
```
users.pn:8: error: $u may be null (findUser() returns ?User). Check it with if ($u !== null), or use $u?->name / ?? 
users.pn:3: error: cannot pass null to name(): parameter $u is User, not ?User
users.pn:12: error: User::__construct() does not set $this->name on every path
users.pn:20: error: $u was checked on line 15 but assigned again on line 18. Check it again before using it
```
