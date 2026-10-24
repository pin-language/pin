#!/bin/sh
# Install Pin:  curl -fsSL https://raw.githubusercontent.com/pin-language/pin/HEAD/install.sh | sh
#
# Clones Pin into ~/.pin (or updates it), links the `pin` command into ~/.local/bin, and runs hello.
# Nothing is installed system-wide and no sudo is needed. Settings (all optional):
#   PIN_HOME  where to keep Pin      (default ~/.pin)
#   PIN_BIN   where to put `pin`     (default ~/.local/bin)
#   PIN_REPO  git URL to clone from  (default https://github.com/pin-language/pin.git)
set -e

REPO="${PIN_REPO:-https://github.com/pin-language/pin.git}"
DIR="${PIN_HOME:-$HOME/.pin}"
BIN="${PIN_BIN:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
fail() { say "error: $*" >&2; exit 1; }

case "$(uname -s)" in
    Darwin) CLANG_HINT="xcode-select --install" ;;
    Linux) CLANG_HINT="sudo apt install clang   (Fedora: sudo dnf install clang)" ;;
    *) fail "Pin runs on macOS and Linux (on Windows, use WSL with Ubuntu)" ;;
esac

command -v git >/dev/null 2>&1 || fail "git is needed: $CLANG_HINT"
command -v python3 >/dev/null 2>&1 || fail "python3 is needed (3.10 or newer)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || fail "python3 is $(python3 -c 'import sys; print(sys.version.split()[0])'); Pin needs 3.10 or newer"
command -v clang >/dev/null 2>&1 || fail "clang is needed. Install it with:  $CLANG_HINT"

if [ -d "$DIR/.git" ]; then
    say "Updating Pin in $DIR"
    git -C "$DIR" pull --ff-only -q
elif [ -e "$DIR" ]; then
    fail "$DIR exists and is not a Pin checkout; set PIN_HOME to another folder"
else
    say "Downloading Pin into $DIR"
    git clone -q --depth 1 "$REPO" "$DIR"
fi

mkdir -p "$BIN"
if [ -e "$BIN/pin" ] && [ ! -L "$BIN/pin" ]; then
    fail "$BIN/pin exists and is not a link to Pin; remove it or set PIN_BIN"
fi
ln -sf "$DIR/pin" "$BIN/pin"
say "Linked $BIN/pin"

say ""
"$BIN/pin" -v
"$BIN/pin" "$DIR/examples/hello.pn"
say ""

case ":$PATH:" in
    *":$BIN:"*)
        say "Done. Try:  pin $DIR/examples/fizzbuzz.pn" ;;
    *)
        case "${SHELL##*/}" in
            zsh) RC="$HOME/.zshrc" ;;
            bash) RC="$HOME/.bashrc" ;;
            *) RC="your shell's startup file" ;;
        esac
        say "Done, but $BIN is not on your PATH yet. Run this once, then open a new terminal:"
        say "  echo 'export PATH=\"$BIN:\$PATH\"' >> $RC"
        ;;
esac
if ! printf '#include <sqlite3.h>\n' | clang -E -x c - >/dev/null 2>&1; then
    say "Optional: to use the database (db_* functions), install SQLite's development files:"
    say "  sudo apt install libsqlite3-dev   (Fedora: sudo dnf install sqlite-devel)"
fi
say "For AI agents: https://raw.githubusercontent.com/pin-language/docs/HEAD/ai-guide.md"
