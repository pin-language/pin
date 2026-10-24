<?php
function user(int $id): string {
    $orders = [];
    $total = 0;
    for ($i = 0; $i < 20; $i++) {
        $amount = ($id * ($i + 7)) % 997;
        $orders[] = $amount;
        $total += $amount;
    }
    return json_encode(["id" => $id, "name" => "user" . $id, "email" => "user$id@pinato.co",
                        "orders" => $orders, "total" => $total]);
}

function primes(int $n): string {
    if ($n < 2 || $n > 1000000) {
        http_response_code(400);
        return '{"error":"n must be between 2 and 1000000"}';
    }
    $composite = array_fill(0, $n + 1, false);
    $count = 0;
    for ($i = 2; $i <= $n; $i++) {
        if (!$composite[$i]) {
            $count++;
            for ($j = $i * $i; $j <= $n; $j += $i) { $composite[$j] = true; }
        }
    }
    return json_encode(["n" => $n, "count" => $count]);
}

function handle(string $path): string {
    header('Content-Type: application/json');
    if ($path === "/hello") return '{"message":"hello world"}';
    if (str_starts_with($path, "/user/")) return user(intval(substr($path, 6)));
    if (str_starts_with($path, "/primes/")) return primes(intval(substr($path, 8)));
    http_response_code(404);
    return '{"error":"not found"}';
}
