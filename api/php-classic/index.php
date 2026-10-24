<?php
// Classic PHP: the script runs from the top for every request (like PHP-FPM)
require __DIR__ . '/../php-app.php';
echo handle(parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH));
