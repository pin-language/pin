#!/bin/sh
# The big test round on Linux: the full suite and the sanitizers on arm64 and on x86_64 (emulated on an
# Apple Silicon Mac, slower). The images are built once and kept; only the source is copied in each time.
#   tools/linux_test.sh            both architectures
#   tools/linux_test.sh arm64      one of them
set -e
cd "$(dirname "$0")/.."
arches=${1:-"arm64 amd64"}
status=0
for arch in $arches; do
    image="pin-test:$arch"
    if ! docker image inspect "$image" >/dev/null 2>&1; then
        echo "building $image (once)"
        docker build -q --platform "linux/$arch" -t "$image" tools/linux >/dev/null
    fi
    echo "== linux/$arch"
    docker run --rm --platform "linux/$arch" -v "$PWD":/src:ro "$image" sh -c '
        cp -r /src/pinac.py /src/runtime.c /src/pin /src/tools /src/tests /src/examples /src/spec /src/editors . &&
        mkdir -p build &&
        sh tools/sanitize.sh tests/hello.pn tests/strmap.pn tests/arith.pn tests/flow.pn tests/json_float.pn \
            tests/strings_list.pn tests/json_hostile.pn tests/db_basics.pn tests/db_lifetimes.pn tests/classes.pn \
            tests/nested.pn tests/errors_ok.pn tests/tasks.pn &&
        python3 -u tests/run_tests.py > /tmp/t.log 2>&1; r=$?; grep -E "FAIL|passed" -A3 /tmp/t.log; exit $r' || status=1
done
exit $status
