<?php
function build(int $n): array {
    $xs = [];
    for ($i = 0; $i < $n; $i++) { $xs[] = ($i * 7) % 1000; }
    return $xs;
}
$total = 0;
for ($round = 0; $round < 10; $round++) {
    $xs = build(5000000);
    foreach ($xs as $x) { $total += $x; }
}
echo $total, "\n";
