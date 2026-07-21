#!/bin/sh

set -eu

INSTALL_ROOT="${CODEXDECK_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/codexdeck}"
BIN_DIR="${CODEXDECK_BIN_DIR:-$HOME/.local/bin}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/codexdeck"
PURGE_CONFIG=0

usage() {
    cat <<'EOF'
Uninstall the user-owned CodexDeck installation.

Usage: ./uninstall.sh [options]

Options:
  --install-root PATH  Installation data directory.
  --bin-dir PATH       Directory containing the codexdeck command link.
  --purge-config       Also remove CodexDeck preferences.
  -h, --help           Show this help.
EOF
}

fail() {
    printf 'codexdeck uninstaller: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --install-root)
            [ "$#" -ge 2 ] || fail "--install-root requires a value"
            INSTALL_ROOT=$2
            shift 2
            ;;
        --bin-dir)
            [ "$#" -ge 2 ] || fail "--bin-dir requires a value"
            BIN_DIR=$2
            shift 2
            ;;
        --purge-config)
            PURGE_CONFIG=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

COMMAND_LINK="$BIN_DIR/codexdeck"
if [ -L "$COMMAND_LINK" ]; then
    LINK_TARGET=$(readlink "$COMMAND_LINK")
    case "$LINK_TARGET" in
        "$INSTALL_ROOT"/*) rm -f "$COMMAND_LINK" ;;
        *) fail "$COMMAND_LINK does not point into the CodexDeck installation" ;;
    esac
elif [ -e "$COMMAND_LINK" ]; then
    fail "$COMMAND_LINK exists but is not the CodexDeck installer link"
fi

if [ -d "$INSTALL_ROOT" ]; then
    rm -rf "$INSTALL_ROOT"
fi

if [ "$PURGE_CONFIG" -eq 1 ] && [ -d "$CONFIG_DIR" ]; then
    rm -rf "$CONFIG_DIR"
fi

printf 'CodexDeck was removed.\n'
if [ "$PURGE_CONFIG" -eq 0 ]; then
    printf 'Preferences were retained in %s.\n' "$CONFIG_DIR"
fi
