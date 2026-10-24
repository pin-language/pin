# Pin — Concurrency

สถานะ: **ร่าง ยังไม่ implement** (2026-10-24) · owner ตกลง · แก้ decision 3A เดิม (goroutine แบบ Go) เป็น **`go` + `await` แบบ structured** · หลักการ: safety > ความเร็ว > อ่านง่าย > เหมือน PHP

## ทำไมแก้ 3A

`go` ที่ปล่อยทิ้งได้ (แบบภาษา Go): error/panic หายเงียบ, task ค้าง (leak), task อยู่นานกว่า request → ยืมข้อมูลไม่ได้ ต้อง copy ทุกอย่าง (ช้า). Structured ปิดทั้ง 3 ข้อ และทำให้ยืมข้อมูลข้าม thread ได้โดยไม่ copy

## กติกา

### C1 — `go` คืน `Task<T>` และต้อง `await` ก่อนฟังก์ชันจบทุกเส้นทาง
```php
$u = go findUser($id);          // Task<?User> เริ่มทำงานทันที
$user = try await $u;           // f() throws → ต้อง try await
$n = await go count2($xs);      // f() ไม่ throws → await เฉยๆ
```
- `Task<T>` เป็น value มีเจ้าของคนเดียว ย้ายได้ (เก็บใน `list<Task<T>>` ได้) copy ไม่ได้
- `await` กิน Task (move) → await ซ้ำ = compile error
- Task ที่ยังไม่ await ตอนออกจากฟังก์ชัน (return ปกติ) = compile error
- ออกด้วย `throw` / `try` ที่ส่ง error ต่อ / panic → Task ที่ค้าง **ถูกยกเลิกและรอให้หยุดจริง** ก่อนออก
- Task return หรือเก็บลง property ไม่ได้ (ต้อง await ในฟังก์ชันที่สร้าง) — ทำให้ไม่ต้องมี lifetime

### C2 — ยืมอ่านข้ามขอบ task ได้ ไม่ copy
- argument ที่ไม่มี `&` = ยืมอ่าน การยืมจบเมื่อ `await` (ไม่ใช่ตอนบรรทัดนั้นจบ)
- หลาย task ยืมอ่านของเดียวกันพร้อมกันได้
- ระหว่างนั้นเจ้าของแก้/ย้ายไม่ได้ (กติกา R2 ของ ownership-v2)
- ปลอดภัยข้าม thread เพราะ Pin ไม่มี refcount และไม่มี interior mutability

### C3 — `&` ส่งให้ task แก้ได้ แบบ exclusive
- ห้ามมี task อื่นหรือเจ้าของใช้ของนั้นจนกว่าจะ await (กติกา R4)
- → **data race เกิดไม่ได้** (compile error)

### C4 — error และ panic
- error จาก task ส่งกลับทาง `await` (`try await` / `try await ... catch`)
- panic ใน task → ทั้ง request panic (500) ตาม error-handling E-R6

### C5 — function ในตัว
| function | ทำอะไร |
|---|---|
| `await_all(list<Task<T>> $ts): list<T>` | รอครบ คืนผลตามลำดับเดิม · `throws` ถ้า task ใด throws · error ตัวแรก → ยกเลิกที่เหลือ |
| `await_timeout(Task<T> $t, int $ms): T throws` | เกินเวลา → ยกเลิก task แล้ว throw `error(504, "timeout")` |
| `await_any(list<Task<T>> $ts): T` | ได้ตัวแรกที่เสร็จ ยกเลิกที่เหลือ *(รอบหลัง)* |

ไม่เพิ่ม keyword `timeout` — ใช้ function (syntax น้อยกว่า)

### C6 — การยกเลิก
- cooperative: task หยุดที่จุดรอ I/O หรือ `await` ถัดไป
- loop คำนวณยาวๆ: compiler ใส่จุดเช็คการยกเลิกที่หัว loop *(ต้องวัดค่าใช้จ่าย — คาดว่าเล็กมาก เป็น load + branch ที่ทายถูกเกือบตลอด)*
- หลังยกเลิก ของที่ task เป็นเจ้าของถูกคืนตามกติกา ownership

### C7 — runtime
- green thread M:N, work stealing ระหว่าง core, event loop ร่วมกับ HTTP server (kqueue บน macOS / epoll บน Linux)
- stack ต่อ task: จองใน virtual memory + **guard page** → stack overflow = panic ไม่ใช่เขียนทับ
- task รอ I/O ไม่บล็อก OS thread
- `PN_THREADS` เดิมยังใช้กำหนดจำนวน OS thread

### C8 — ไม่มี async/await แบบ JS/Rust
ไม่มีฟังก์ชัน async แยกพวก ทุกฟังก์ชันใช้ `go` ได้ และถูกเรียกด้วย `go` ได้

## ตัวอย่าง

```php
// เรียกพร้อมกันใน handler
function profile(int $id): string throws {
    $u = go findUser($id);
    $o = go findOrders($id);
    $p = go getPoints($id);
    $user   = try await $u;
    $orders = try await $o;
    $points = try await $p catch ($e) { 0; };
    return json_encode(["user" => $user, "orders" => $orders, "points" => $points]);
}

// หลาย task ใน loop
function prices(list<int> $ids): list<float> throws {
    list<Task<float>> $tasks = [];
    foreach ($ids as $id) { $tasks[] = go fetchPrice($id); }
    return try await_all($tasks);
}

// หลาย core ยืมข้อมูลเดียวกัน
function stats(list<int> $xs): string {
    $half = intdiv(count($xs), 2);
    $a = go sumRange($xs, 0, $half);
    $b = go sumRange($xs, $half, count($xs));
    return json_encode(["sum" => await $a + await $b]);
}

// timeout
$points = try await_timeout(go getPoints($id), 200) catch ($e) { 0; };
```

## ตัวอย่าง error message
```
profile.pn:3: error: task $u started on line 2 is not awaited when the function returns on line 3.
  Add await $u before returning
stats.pn:3: error: cannot append to $xs while task $t (line 2) is reading it. Move this line after await $t
order.pn:3: error: $o is already being changed by task $a (line 2). Await $a first, or give each task its own copy()
user.pn:4: error: $t was already awaited on line 3
user.pn:5: error: findUser() can fail, so write try await $t
```

## เลื่อนไปทีหลัง

| เรื่อง | แนวทาง |
|---|---|
| channel | ส่ง = move ตามกติกาเดิม · ทำเมื่อมี use case producer/consumer จริง |
| ข้อมูลร่วมข้าม request (cache, rate limit, DB pool) | ไม่มี global ของ user · runtime ให้ type สำเร็จรูปที่ปลอดภัย (`Cache`, `DbPool`) · ตัดสินพร้อมเรื่อง database |
| งานหลังตอบ request (ส่ง email) | ไม่ใช้ `go` ปล่อยทิ้ง · runtime ให้ queue ที่มี retry และไม่ทำ error หาย |

## ลำดับ implement

ต้องมาหลัง: Task 1 (build บน Mac + kqueue), control-flow analysis ร่วม, `throws`/`try`, panic → 500 (arena ต่อ request)
1. runtime: green thread + stack พร้อม guard page + scheduler ต่อ core
2. ผูก I/O ของ server เข้ากับ scheduler
3. compiler: `go` / `await` / `Task<T>` + C1–C3
4. `await_all`, `await_timeout`, การยกเลิก
5. test: race ที่ต้อง compile error, ยกเลิกเมื่อ error, ASAN + TSAN สะอาด, วัด overhead ของ `go` ต่อ task
