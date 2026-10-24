#!/bin/sh
# Build programs with AddressSanitizer + UndefinedBehaviorSanitizer and run them.
# LeakSanitizer is on by default on Linux (not on macOS).
#   tools/sanitize.sh tests/hello.pn tests/strmap.pn ...
set -e
cd "$(dirname "$0")/.."
mkdir -p build/sanitize
status=0
for src in "$@"; do
    name=$(basename "$src" .pn)
    python3 pinac.py --emit-ll "$src"
    ll="${src%.pn}.ll"
    mv "$ll" "build/sanitize/$name.ll"
    clang -O1 -c -Wno-override-module "build/sanitize/$name.ll" -o "build/sanitize/$name.o"
    clang -g -fsanitize=address,undefined runtime.c "build/sanitize/$name.o" -lm -pthread -o "build/sanitize/$name"
    if "build/sanitize/$name" > /dev/null 2> "build/sanitize/$name.err" \
        && ! grep -q "ERROR\|runtime error" "build/sanitize/$name.err"; then
        echo "  clean  $src"
    else
        echo "  FAIL   $src"; cat "build/sanitize/$name.err"; status=1
    fi
done
exit $status
