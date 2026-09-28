#!/usr/bin/env bash
# Automated SDR by Fred - one-command installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/Fred-In-tech/automated-sdr-agent/main/install.sh | bash
#
# What it does (it never uses sudo; if something is missing it tells you the command to run):
#   1. checks for git and Python 3.11 or newer
#   2. downloads the newest release into $SDR_HOME (default ~/automated-sdr), or moves an
#      existing copy to it (never to unreleased code)
#   3. creates a private Python environment (.venv) and installs the requirements into it
#   4. links the `sdr` command into ~/.local/bin
#   5. starts `sdr setup` (a few friendly questions)
#
# Options (environment variables):
#   SDR_HOME=/path      where to install                   (default: ~/automated-sdr)
#   SDR_REPO=url        git repository to install from     (default: the official repo)
#   SDR_PYTHON=/path    Python 3.11+ interpreter to use    (default: first suitable one found)
#   SDR_BIN_DIR=/path   where to put the `sdr` command     (default: ~/.local/bin)
#   SDR_NO_SETUP=1      install only; run `sdr setup` yourself later (useful for AI agents / CI)
#
# Example:  curl -fsSL <url>/install.sh | SDR_NO_SETUP=1 SDR_HOME=~/tools/sdr bash
#
# Everything below runs inside main(), which is called on the very last line. If the download
# is cut off half-way, bash never reaches that line, so a partial script can't run half an install.

set -euo pipefail

SDR_DEFAULT_REPO="https://github.com/Fred-In-tech/automated-sdr-agent.git"
SDR_RAW_URL="https://raw.githubusercontent.com/Fred-In-tech/automated-sdr-agent/main"
SDR_MIN_PYTHON="3.11"

# ----------------------------------------------------------------------------- output helpers

setup_colors() {
    # Colours only on a real terminal, and never when the user asked for none (NO_COLOR standard).
    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
        BLUE=$'\033[1;34m'; GREEN=$'\033[1;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[1;31m'
        BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
    else
        BLUE=""; GREEN=""; YELLOW=""; RED=""; BOLD=""; DIM=""; RESET=""
    fi
}

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$BLUE" "$RESET" "$BOLD" "$*" "$RESET"; }
ok()   { printf '  %s[ok]%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '  %s[!]%s %s\n' "$YELLOW" "$RESET" "$*"; }
fail() { printf '  %s[x]%s %s\n' "$RED" "$RESET" "$*" >&2; }
die()  { fail "$*"; exit 1; }

tilde() {
    # Show paths under the home folder as ~/... - shorter to read, and still valid to paste.
    case "$1" in
        "$HOME") printf '~\n' ;;
        "$HOME"/*) printf '~/%s\n' "${1#"$HOME"/}" ;;
        *) printf '%s\n' "$1" ;;
    esac
}

banner() {
    # The full logo needs ~78 columns; narrow terminals get a one-line title instead of a mess.
    local cols
    cols="$(tput cols 2>/dev/null || echo 80)"
    printf '%s' "$BLUE"
    if [ "${cols:-80}" -ge 78 ] 2>/dev/null; then
        cat <<'EOF'

   _    _   _  _____   ___   __  __    _   _____  ___  ___    ___  ___   ___
  /_\  | | | ||_   _| / _ \ |  \/  |  /_\ |_   _|| __||   \  / __||   \ | _ \
 / _ \ | |_| |  | |  | (_) || |\/| | / _ \  | |  | _| | |) | \__ \| |) ||   /
/_/ \_\ \___/   |_|   \___/ |_|  |_|/_/ \_\ |_|  |___||___/  |___/|___/ |_|_\
EOF
    else
        printf '\n  AUTOMATED SDR\n'
    fi
    printf '%s' "$RESET"
    printf '  %sby Fred%s  %s-  your AI sales rep, installed in about two minutes.%s\n' \
        "$BOLD" "$RESET" "$DIM" "$RESET"
}

# ----------------------------------------------------------------------------- platform checks

is_macos() { [ "$(uname -s 2>/dev/null)" = "Darwin" ]; }

have() { command -v "$1" >/dev/null 2>&1; }

mac_dev_tools_ready() {
    # On a fresh Mac, /usr/bin/git and /usr/bin/python3 are stubs that pop up an "install
    # developer tools" dialog when run. Only treat them as real once the tools are installed.
    xcode-select -p >/dev/null 2>&1
}

install_hint() {
    # Print the command that installs $1 (git | python) on this system. We never run it
    # ourselves: installing system packages needs the user's password and their consent.
    local what="$1"
    if is_macos; then
        if [ "$what" = "git" ]; then
            say "    xcode-select --install        (or, with Homebrew: brew install git)"
        else
            say "    brew install python@3.12      (or download it from https://www.python.org/downloads/)"
        fi
    elif have apt-get; then
        if [ "$what" = "git" ]; then
            say "    sudo apt-get install -y git"
        else
            say "    sudo apt-get install -y python3 python3-venv"
            say "    (Ubuntu 22.04 and older ship Python 3.10: sudo apt-get install -y python3.11 python3.11-venv)"
        fi
    elif have dnf; then
        if [ "$what" = "git" ]; then say "    sudo dnf install -y git"; else say "    sudo dnf install -y python3.11"; fi
    elif have yum; then
        if [ "$what" = "git" ]; then say "    sudo yum install -y git"; else say "    sudo yum install -y python3.11"; fi
    elif have pacman; then
        if [ "$what" = "git" ]; then say "    sudo pacman -S git"; else say "    sudo pacman -S python"; fi
    elif have zypper; then
        if [ "$what" = "git" ]; then say "    sudo zypper install git"; else say "    sudo zypper install python311"; fi
    elif have apk; then
        if [ "$what" = "git" ]; then say "    sudo apk add git"; else say "    sudo apk add python3"; fi
    else
        say "    Install $what with your system's package manager, then run this installer again."
    fi
}

need_git() {
    if have git && { ! is_macos || [ "$(command -v git)" != "/usr/bin/git" ] || mac_dev_tools_ready; }; then
        ok "git $(git --version 2>/dev/null | awk '{print $3}')"
        return 0
    fi
    fail "git is not installed. Install it with:"
    install_hint git
    say "  Then run this installer again."
    exit 1
}

python_ok() {
    # True when "$1" runs and is Python 3.11 or newer (tomllib, modern typing, etc.).
    [ -n "${1:-}" ] || return 1
    if is_macos && [ "$1" = "/usr/bin/python3" ] && ! mac_dev_tools_ready; then
        return 1
    fi
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

python_version() {
    "$1" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo "?"
}

find_python() {
    # Print the path of the first Python 3.11+ found. An explicit SDR_PYTHON is used as-is
    # or rejected - never silently swapped for a different interpreter.
    local candidate path
    if [ -n "${SDR_PYTHON:-}" ]; then
        if python_ok "$SDR_PYTHON"; then
            printf '%s\n' "$SDR_PYTHON"
            return 0
        fi
        return 1
    fi
    for candidate in python3 python3.14 python3.13 python3.12 python3.11 python \
                     /opt/homebrew/bin/python3 /usr/local/bin/python3; do
        path="$(command -v "$candidate" 2>/dev/null || true)"
        if [ -n "$path" ] && python_ok "$path"; then
            printf '%s\n' "$path"
            return 0
        fi
    done
    return 1
}

# ----------------------------------------------------------------------------- install steps

absolute_dir() {
    # Expand a leading "~/" (it stays literal when quoted in SDR_HOME="~/x") and make the path
    # absolute, so the `sdr` link keeps working from any folder.
    local dir="$1"
    case "$dir" in
        "~") dir="$HOME" ;;
        "~/"*) dir="$HOME/${dir#"~/"}" ;;
    esac
    case "$dir" in
        /*) ;;
        *) dir="$PWD/$dir" ;;
    esac
    printf '%s\n' "${dir%/}"
}

newest_release() {
    # The newest release tag (vX.Y.Z, compared numerically so v1.10.0 beats v1.9.0) in the clone
    # at $1, or nothing before the first release. Pre-release tags such as v2.0.0-rc1 are ignored,
    # exactly as core/updater.py does, so the installer and `sdr update` agree on what a release is.
    local tags tag=""
    tags="$(git -C "$1" tag --list --sort=-v:refname 'v*' 2>/dev/null | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' || true)"
    IFS= read -r tag <<< "$tags" || true
    printf '%s\n' "$tag"
}

fetch_code() {
    # Clone into $2, or fetch into an existing clone, then move to the newest release tag: an
    # install must never run unreleased code from `main` (SECURITY.md promises release tags only,
    # and `sdr update` only ever moves between them). Before the first release there is no tag,
    # so the default branch is used. A failed move (offline, local edits) keeps the current
    # version instead of aborting: `sdr update` handles that safely, with backup and rollback.
    local repo="$1" dir="$2" tag
    if [ -d "$dir/.git" ]; then
        if ! git -C "$dir" fetch --tags --quiet origin; then
            warn "Couldn't check $(tilde "$dir") for new releases (offline?). Keeping the version you have."
            return 0
        fi
        tag="$(newest_release "$dir")"
        if [ -z "$tag" ]; then
            if git -C "$dir" pull --ff-only --quiet; then
                ok "Updated the existing copy in $(tilde "$dir") (no release yet, so it follows main)"
            else
                warn "Couldn't update $(tilde "$dir") automatically (local changes?). Keeping the version you have."
            fi
        elif git -C "$dir" checkout --quiet "$tag"; then
            ok "Updated the existing copy in $(tilde "$dir") to release $tag"
        else
            warn "Couldn't move $(tilde "$dir") to release $tag (local changes?). Keeping the version you have."
        fi
        return 0
    fi
    if [ -e "$dir" ] && [ -n "$(ls -A "$dir" 2>/dev/null || echo not-a-dir)" ]; then
        die "$dir already exists and isn't an Automated SDR install. Move it away or pick another folder: SDR_HOME=/some/new/folder"
    fi
    mkdir -p "$(dirname "$dir")"
    if ! git clone --quiet "$repo" "$dir"; then
        die "Couldn't download $repo. Check your internet connection (and access to the repository), then try again."
    fi
    tag="$(newest_release "$dir")"
    if [ -z "$tag" ]; then
        ok "Downloaded into $(tilde "$dir")"
        warn "No release has been tagged yet, so this copy follows the main branch until the first one."
    elif git -C "$dir" checkout --quiet "$tag"; then
        ok "Downloaded release $tag into $(tilde "$dir")"
    else
        die "Couldn't check out release $tag in $dir."
    fi
}

make_venv() {
    # A project-private .venv keeps our requirements away from the system Python. A broken venv
    # (e.g. its Python was upgraded away by Homebrew) is rebuilt instead of reused.
    local py="$1" dir="$2" venv_py
    venv_py="$dir/.venv/bin/python"
    if [ -x "$venv_py" ] && python_ok "$venv_py" && "$venv_py" -m pip --version >/dev/null 2>&1; then
        ok "Using the existing environment ($(tilde "$dir")/.venv)"
        return 0
    fi
    if ! "$py" -m venv --clear "$dir/.venv" || ! "$dir/.venv/bin/python" -m pip --version >/dev/null 2>&1; then
        fail "Couldn't create a Python environment with $py."
        if have apt-get; then
            say "  On Debian/Ubuntu this needs the venv package:"
            say "    sudo apt-get install -y python$(python_version "$py" | cut -d. -f1,2)-venv"
        fi
        exit 1
    fi
    ok "Created $(tilde "$dir")/.venv (Python $(python_version "$venv_py"))"
}

install_requirements() {
    local dir="$1"
    if ! "$dir/.venv/bin/python" -m pip install --disable-pip-version-check --quiet -r "$dir/requirements.txt"; then
        die "Installing the requirements failed. Check your internet connection and run the installer again."
    fi
    ok "Requirements installed"
}

on_path() {
    case ":${PATH:-}:" in
        *":$1:"*) return 0 ;;
    esac
    return 1
}

link_launcher() {
    # Symlink (not copy) so updates to bin/sdr apply automatically. Never clobber a real file, or
    # a working link to something else (another tool, or a second Automated SDR install for another
    # business): that is someone's command. A dangling link (an install that was moved or deleted)
    # is ours to replace.
    local dir="$1" bindir="$2"
    local target="$dir/bin/sdr" link="$bindir/sdr"
    chmod +x "$target" 2>/dev/null || true
    mkdir -p "$bindir"
    if [ -e "$link" ] && [ ! -L "$link" ]; then
        warn "$(tilde "$link") already exists and isn't ours, so it was left alone. Use $(tilde "$target") instead."
        SDR_CMD="$target"
        return 0
    fi
    if [ -L "$link" ] && [ -e "$link" ] && ! [ "$link" -ef "$target" ]; then
        warn "$(tilde "$link") already points to $(readlink "$link"), so it was left alone. Use $(tilde "$target") instead."
        SDR_CMD="$target"
        return 0
    fi
    ln -sfn "$target" "$link"
    ok "Added the sdr command: $(tilde "$link")"
    if on_path "$bindir"; then
        SDR_CMD="sdr"
    else
        SDR_CMD="$link"
    fi
}

path_hint() {
    # New terminals won't find `sdr` unless its folder is on PATH. We only print the fix:
    # editing someone's shell startup files is their call.
    local bindir="$1" shown rc
    on_path "$bindir" && return 0
    shown="$bindir"
    case "$bindir" in
        "$HOME"/*) shown="\$HOME/${bindir#"$HOME"/}" ;;
    esac
    case "$(basename "${SHELL:-bash}")" in
        zsh) rc="~/.zshrc" ;;
        bash) if is_macos; then rc="~/.bash_profile"; else rc="~/.bashrc"; fi ;;
        fish)
            warn "$(tilde "$bindir") is not on your PATH yet. Add it once with:"
            say "      fish_add_path $bindir"
            return 0
            ;;
        *) rc="~/.profile" ;;
    esac
    warn "$(tilde "$bindir") is not on your PATH yet, so new terminals won't find 'sdr'. Fix it once with:"
    say "      echo 'export PATH=\"$shown:\$PATH\"' >> $rc && source $rc"
}

tty_available() {
    # Under `curl | bash` stdin is the download, not the keyboard. Setup needs /dev/tty; when
    # there is none (CI, an AI agent's sandbox) we finish without starting the questions.
    (: </dev/tty) 2>/dev/null
}

start_setup() {
    local launcher="$1"
    if [ "${SDR_NO_SETUP:-0}" = "1" ]; then
        say "  Setup skipped (SDR_NO_SETUP=1). When you're ready, run:  $(tilde "$SDR_CMD") setup"
        return 0
    fi
    if ! tty_available; then
        say "  No terminal to ask questions in. To finish, open a terminal and run:  $(tilde "$SDR_CMD") setup"
        return 0
    fi
    step "Starting setup"
    exec "$launcher" setup </dev/tty
}

# ----------------------------------------------------------------------------- main

main() {
    setup_colors
    banner

    case "$(uname -s 2>/dev/null || echo unknown)" in
        Darwin|Linux) ;;
        MINGW*|MSYS*|CYGWIN*)
            die "This is the macOS/Linux installer. On Windows, open PowerShell and run: irm $SDR_RAW_URL/install.ps1 | iex"
            ;;
        *) warn "This system isn't officially supported; trying anyway." ;;
    esac

    local home repo bindir py
    home="$(absolute_dir "${SDR_HOME:-$HOME/automated-sdr}")"
    repo="${SDR_REPO:-$SDR_DEFAULT_REPO}"
    bindir="$(absolute_dir "${SDR_BIN_DIR:-$HOME/.local/bin}")"
    SDR_CMD="$home/bin/sdr"

    step "1/5  Checking what you need"
    need_git
    if ! py="$(find_python)"; then
        if [ -n "${SDR_PYTHON:-}" ]; then
            fail "SDR_PYTHON=$SDR_PYTHON is not Python $SDR_MIN_PYTHON or newer."
        else
            fail "Python $SDR_MIN_PYTHON or newer is required. Install it with:"
            install_hint python
        fi
        say "  Then run this installer again."
        exit 1
    fi
    ok "Python $(python_version "$py") ($py)"

    step "2/5  Getting Automated SDR"
    fetch_code "$repo" "$home"

    step "3/5  Creating a private Python environment"
    make_venv "$py" "$home"

    step "4/5  Installing requirements (about a minute)"
    install_requirements "$home"

    step "5/5  Adding the sdr command"
    link_launcher "$home" "$bindir"
    path_hint "$bindir"

    printf '\n  %sInstalled!%s Automated SDR lives in %s\n' "$GREEN" "$RESET" "$(tilde "$home")"
    say "  Useful commands:  sdr setup  |  sdr preview  |  sdr dashboard  |  sdr doctor"
    if [ "$SDR_CMD" != "sdr" ]; then
        say "  (Until 'sdr' is on your PATH, type $(tilde "$SDR_CMD") instead of sdr.)"
    fi
    start_setup "$home/bin/sdr"
}

# Tests source this file with SDR_INSTALLER_TEST=1 to call the functions one by one.
if [ -z "${SDR_INSTALLER_TEST:-}" ]; then
    main "$@"
fi
