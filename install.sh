#!/bin/sh

set -eu

REPOSITORY="${CODEXDECK_REPOSITORY:-Telecaster2147/CodexDeck}"
INSTALL_ROOT="${CODEXDECK_INSTALL_ROOT:-${XDG_DATA_HOME:-$HOME/.local/share}/codexdeck}"
BIN_DIR="${CODEXDECK_BIN_DIR:-$HOME/.local/bin}"
VERSION="${CODEXDECK_VERSION:-}"
WHEEL_SOURCE=""
CHECKSUM_SOURCE=""
TMP_DIR=""
STAGING=""
PREVIOUS_TARGET=""
NO_COLOR_FLAG=0
BOLD=""
DIM=""
CYAN=""
GREEN=""
YELLOW=""
RED=""
RESET=""
OK_MARK="[ok]"
WARN_MARK="[!]"

usage() {
    cat <<'EOF'
Install CodexDeck into an isolated user-owned virtual environment.

Usage: ./install.sh [options]

Options:
  --version VERSION       Install a specific GitHub release tag (for example 0.2.0).
  --wheel PATH_OR_URL     Install a local or remote wheel instead of a GitHub release.
  --checksum PATH_OR_URL  SHA-256 file for --wheel; defaults to PATH_OR_URL.sha256.
  --install-root PATH     Installation data directory.
  --bin-dir PATH          Directory for the codexdeck command link.
  --no-color              Disable installer colors.
  -h, --help              Show this help.

Environment equivalents:
  CODEXDECK_VERSION, CODEXDECK_INSTALL_ROOT, CODEXDECK_BIN_DIR,
  CODEXDECK_REPOSITORY
EOF
}

fail() {
    printf '%s%s error%s  %s\n' "$RED" "$WARN_MARK" "$RESET" "$*" >&2
    exit 1
}

init_ui() {
    if [ "$NO_COLOR_FLAG" -eq 0 ] && [ -t 1 ] && [ -z "${NO_COLOR:-}" ] &&
        [ "${TERM:-}" != "dumb" ]; then
        BOLD=$(printf '\033[1m')
        DIM=$(printf '\033[2m')
        CYAN=$(printf '\033[36m')
        GREEN=$(printf '\033[32m')
        YELLOW=$(printf '\033[33m')
        RED=$(printf '\033[31m')
        RESET=$(printf '\033[0m')
    fi
    case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
        *UTF-8*|*utf8*)
            OK_MARK="✓"
            WARN_MARK="!"
            ;;
    esac
}

banner() {
    printf '\n%s%s┌──────────────────────────────────────────────────┐%s\n' "$BOLD" "$CYAN" "$RESET"
    printf '%s%s│  CODEXDECK  /  INSTALLER                         │%s\n' "$BOLD" "$CYAN" "$RESET"
    printf '%s%s│  Read-only operations console for Codex sessions │%s\n' "$BOLD" "$CYAN" "$RESET"
    printf '%s%s└──────────────────────────────────────────────────┘%s\n\n' "$BOLD" "$CYAN" "$RESET"
}

step() {
    printf '\n%s%s[%s/5]%s %s%s%s\n' "$BOLD" "$CYAN" "$1" "$RESET" "$BOLD" "$2" "$RESET"
}

ok() {
    printf '  %s%s%s  %s\n' "$GREEN" "$OK_MARK" "$RESET" "$*"
}

warn() {
    printf '  %s%s%s  %s\n' "$YELLOW" "$WARN_MARK" "$RESET" "$*" >&2
}

detail() {
    printf '     %s%s%s\n' "$DIM" "$*" "$RESET"
}

cleanup() {
    if [ -n "$STAGING" ] && [ -d "$STAGING" ]; then
        if [ -L "$INSTALL_ROOT/current" ] && [ "$(readlink "$INSTALL_ROOT/current")" = "$STAGING" ]; then
            if [ -n "$PREVIOUS_TARGET" ]; then
                ln -sfn "$PREVIOUS_TARGET" "$INSTALL_ROOT/current"
            else
                rm -f "$INSTALL_ROOT/current" "$BIN_DIR/codexdeck"
            fi
        fi
        rm -rf "$STAGING"
    fi
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
}

trap cleanup EXIT HUP INT TERM

while [ "$#" -gt 0 ]; do
    case "$1" in
        --version)
            [ "$#" -ge 2 ] || fail "--version requires a value"
            VERSION=$2
            shift 2
            ;;
        --wheel)
            [ "$#" -ge 2 ] || fail "--wheel requires a value"
            WHEEL_SOURCE=$2
            shift 2
            ;;
        --checksum)
            [ "$#" -ge 2 ] || fail "--checksum requires a value"
            CHECKSUM_SOURCE=$2
            shift 2
            ;;
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
        --no-color)
            NO_COLOR_FLAG=1
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

init_ui
banner

step 1 "Inspecting this system"
[ "$(uname -s)" = "Linux" ] || fail "Linux is required"

absolute_path() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s/%s\n' "$(pwd -P)" "$1" ;;
    esac
}

INSTALL_ROOT=$(absolute_path "$INSTALL_ROOT")
BIN_DIR=$(absolute_path "$BIN_DIR")

find_python() {
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c '
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
' >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON=$(find_python) || fail "Python 3.10 or newer is required"
PYTHON_VERSION=$("$PYTHON" -c 'import platform; print(platform.python_version())')
ok "Linux and Python $PYTHON_VERSION"
detail "Python: $PYTHON"
command -v ps >/dev/null 2>&1 || fail "ps is required"
ok "Required process collector: ps"
if ! command -v ss >/dev/null 2>&1; then
    warn "Optional network collector missing: ss (install iproute2 for network evidence)"
else
    ok "Optional network collector: ss"
fi

download() {
    source=$1
    destination=$2
    case "$source" in
        http://*|https://*)
            if command -v curl >/dev/null 2>&1; then
                curl --fail --location --silent --show-error "$source" --output "$destination"
            elif command -v wget >/dev/null 2>&1; then
                wget -q "$source" -O "$destination"
            else
                fail "curl or wget is required for remote downloads"
            fi
            ;;
        file://*)
            cp "${source#file://}" "$destination"
            ;;
        *)
            cp "$source" "$destination"
            ;;
    esac
}

resolve_latest_version() {
    command -v curl >/dev/null 2>&1 || fail "curl is required to resolve the latest release"
    release_url=$(curl --fail --location --silent --show-error \
        --output /dev/null --write-out '%{url_effective}' \
        "https://github.com/$REPOSITORY/releases/latest")
    tag=${release_url##*/}
    tag=${tag#v}
    [ -n "$tag" ] && [ "$tag" != "latest" ] || fail "latest release could not be resolved"
    printf '%s\n' "$tag"
}

step 2 "Resolving the package"
if [ -z "$WHEEL_SOURCE" ]; then
    if [ -z "$VERSION" ]; then
        detail "Looking up the latest release from $REPOSITORY"
        VERSION=$(resolve_latest_version)
    fi
    VERSION=${VERSION#v}
    WHEEL_NAME="codexdeck-$VERSION-py3-none-any.whl"
    RELEASE_BASE="https://github.com/$REPOSITORY/releases/download/v$VERSION"
    WHEEL_SOURCE="$RELEASE_BASE/$WHEEL_NAME"
else
    WHEEL_NAME=${WHEEL_SOURCE##*/}
    WHEEL_NAME=${WHEEL_NAME%%\?*}
    if [ -z "$VERSION" ]; then
        VERSION=$(printf '%s\n' "$WHEEL_NAME" | sed -n 's/^codexdeck-\([^-][^-]*\)-py3-none-any\.whl$/\1/p')
        [ -n "$VERSION" ] || VERSION=local
    fi
fi
ok "Selected CodexDeck $VERSION"
detail "Package: $WHEEL_NAME"

if [ -z "$CHECKSUM_SOURCE" ]; then
    CHECKSUM_SOURCE="$WHEEL_SOURCE.sha256"
fi

TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/codexdeck-install.XXXXXX")
WHEEL_FILE="$TMP_DIR/$WHEEL_NAME"
CHECKSUM_FILE="$TMP_DIR/$WHEEL_NAME.sha256"

step 3 "Downloading release assets"
detail "$WHEEL_SOURCE"
download "$WHEEL_SOURCE" "$WHEEL_FILE"
download "$CHECKSUM_SOURCE" "$CHECKSUM_FILE"
ok "Wheel and checksum downloaded"

step 4 "Verifying package integrity"
EXPECTED=$(awk 'match($0, /[0-9A-Fa-f]{64}/) { print substr($0, RSTART, RLENGTH); exit }' \
    "$CHECKSUM_FILE")
[ -n "$EXPECTED" ] || fail "checksum file does not contain a SHA-256 digest"

if command -v sha256sum >/dev/null 2>&1; then
    ACTUAL=$(sha256sum "$WHEEL_FILE" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
    ACTUAL=$(shasum -a 256 "$WHEEL_FILE" | awk '{print $1}')
else
    fail "sha256sum or shasum is required"
fi

[ "$ACTUAL" = "$EXPECTED" ] || fail "SHA-256 verification failed"
ok "SHA-256 verified"
detail "$ACTUAL"

step 5 "Installing the isolated runtime"
VERSIONS_DIR="$INSTALL_ROOT/versions"
TARGET="$VERSIONS_DIR/$VERSION-$(date +%Y%m%d%H%M%S)-$$"
STAGING="$TARGET"
mkdir -p "$VERSIONS_DIR" "$BIN_DIR"

if [ -e "$BIN_DIR/codexdeck" ] || [ -L "$BIN_DIR/codexdeck" ]; then
    [ -L "$BIN_DIR/codexdeck" ] || fail "$BIN_DIR/codexdeck exists and is not a symlink"
    case "$(readlink "$BIN_DIR/codexdeck")" in
        "$INSTALL_ROOT"/*) ;;
        *) fail "$BIN_DIR/codexdeck is not managed by this installer" ;;
    esac
fi

if [ -L "$INSTALL_ROOT/current" ]; then
    PREVIOUS_TARGET=$(readlink "$INSTALL_ROOT/current")
fi

"$PYTHON" -m venv "$STAGING" || fail "virtual environment creation failed; install python3-venv"
"$STAGING/bin/python" -m pip install --disable-pip-version-check "$WHEEL_FILE"
"$STAGING/bin/codexdeck" --version >/dev/null
ok "Virtual environment created"

ln -sfn "$TARGET" "$INSTALL_ROOT/current"
ln -sfn "$INSTALL_ROOT/current/bin/codexdeck" "$BIN_DIR/codexdeck"
"$BIN_DIR/codexdeck" --version >/dev/null
ok "Command link activated"
detail "$BIN_DIR/codexdeck"
STAGING=""

if [ -n "$PREVIOUS_TARGET" ] && [ "$PREVIOUS_TARGET" != "$TARGET" ]; then
    case "$PREVIOUS_TARGET" in
        "$VERSIONS_DIR"/*) rm -rf "$PREVIOUS_TARGET" ;;
    esac
fi

printf '\n%s%s┌──────────────────────────────────────────────────┐%s\n' "$BOLD" "$GREEN" "$RESET"
printf '%s%s│  INSTALLATION COMPLETE                           │%s\n' "$BOLD" "$GREEN" "$RESET"
printf '%s%s└──────────────────────────────────────────────────┘%s\n' "$BOLD" "$GREEN" "$RESET"
printf '\n  Version   %s\n' "$VERSION"
printf '  Command   %s/codexdeck\n' "$BIN_DIR"
printf '  Data      %s\n' "$INSTALL_ROOT"
case ":$PATH:" in
    *":$BIN_DIR:"*) printf '\n  Run now:  codexdeck\n' ;;
    *)
        warn "$BIN_DIR is not currently in PATH"
        printf '\n  Run now:  %s/codexdeck\n' "$BIN_DIR"
        ;;
esac
printf '\n'
