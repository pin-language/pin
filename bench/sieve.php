<?php
$n = 20000000;
$composite = array_fill(0, $n + 1, false);
$count = 0;
for ($i = 2; $i <= $n; $i++) {
    if (!$composite[$i]) {
        $count++;
        for ($j = $i * $i; $j <= $n; $j += $i) { $composite[$j] = true; }
    }
}
echo $count, "\n";
