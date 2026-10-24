<?php
// FrankenPHP worker mode: the app boots once and stays in memory between requests
require __DIR__ . '/../php-app.php';
ignore_user_abort(true);
$serve = static function (): void {
    echo handle(parse_url($_SERVER['REQUEST_URI'], PHP_URL_PATH));
};
while (frankenphp_handle_request($serve)) {}
