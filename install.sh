#!/usr/bin/env bash
# tmsm installer — Linux only
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "tmsm is Linux only." >&2
    exit 1
fi

# --- sudo helper ---
if [[ $EUID -eq 0 ]]; then
    SUDO=""
else
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
    else
        SUDO=""
    fi
fi

# --- detect package manager ---
detect_pm() {
    for pm in apt-get dnf yum pacman zypper apk; do
        if command -v "$pm" >/dev/null 2>&1; then
            echo "$pm"; return 0
        fi
    done
    return 1
}

# Map of generic tool name -> package name per pm
pkg_for() {
    local pm="$1" tool="$2"
    case "$pm:$tool" in
        apt-get:venv)   echo "python3-venv" ;;
        dnf:venv|yum:venv) echo "" ;;       # included in python3
        pacman:venv)    echo "" ;;
        zypper:venv)    echo "python3-venv" ;;
        apk:venv)       echo "" ;;
        *)              echo "$tool" ;;
    esac
}

install_pkgs() {
    local pm="$1"; shift
    local pkgs=("$@")
    [[ ${#pkgs[@]} -eq 0 ]] && return 0
    echo "Installing system packages: ${pkgs[*]}"
    case "$pm" in
        apt-get) $SUDO apt-get update -qq && $SUDO apt-get install -y "${pkgs[@]}" ;;
        dnf)     $SUDO dnf install -y "${pkgs[@]}" ;;
        yum)     $SUDO yum install -y "${pkgs[@]}" ;;
        pacman)  $SUDO pacman -Sy --noconfirm "${pkgs[@]}" ;;
        zypper)  $SUDO zypper --non-interactive install "${pkgs[@]}" ;;
        apk)     $SUDO apk add --no-cache "${pkgs[@]}" ;;
    esac
}

# --- required system tools ---
PM=""
if ! PM=$(detect_pm); then
    PM=""
fi

needed_tools=()
command -v git    >/dev/null 2>&1 || needed_tools+=("git")
command -v screen >/dev/null 2>&1 || needed_tools+=("screen")
command -v curl   >/dev/null 2>&1 || needed_tools+=("curl")

# Extra runtime libs we want present for portable MariaDB / TM dedicated server.
# These are checked / added only for apt-based systems. Package names changed
# on Ubuntu 24.04 (t64 transition / ncurses5 dropped), so we pick whichever
# candidate apt actually has.
extra_pkgs=()
apt_pick() {
    # Echo the first candidate that has an installation candidate in apt-cache.
    for cand in "$@"; do
        if apt-cache policy "$cand" 2>/dev/null | grep -q 'Candidate: [^(]'; then
            echo "$cand"; return 0
        fi
    done
    return 1
}
if [[ "${PM:-}" == "apt-get" ]]; then
    # Make sure apt-cache policy reflects current archives before we probe.
    $SUDO apt-get update -qq || true
    for group in "libaio1 libaio1t64" \
                 "libncurses5 libncurses6 libncursesw6" \
                 "libtinfo5 libtinfo6"; do
        # If any candidate in this group is already installed, skip.
        already=""
        for p in $group; do
            if dpkg -s "$p" >/dev/null 2>&1; then already="$p"; break; fi
        done
        [[ -n "$already" ]] && continue
        # Otherwise pick the first that's installable.
        if pick=$(apt_pick $group); then
            extra_pkgs+=("$pick")
        fi
    done

    # CPython 3.8 build dependencies (needed when tmsm installs PyPlanet
    # and has to compile Python 3.8.20 via pyenv). Pre-install here so the
    # sudo prompt happens in the user's terminal, not behind the TUI.
    for p in build-essential libssl-dev zlib1g-dev libbz2-dev \
             libreadline-dev libsqlite3-dev \
             xz-utils tk-dev libxml2-dev libxmlsec1-dev libffi-dev \
             liblzma-dev llvm; do
        dpkg -s "$p" >/dev/null 2>&1 || extra_pkgs+=("$p")
    done
    # ncurses dev headers: name varies across Ubuntu releases.
    if ! dpkg -s libncurses-dev >/dev/null 2>&1 \
       && ! dpkg -s libncursesw5-dev >/dev/null 2>&1; then
        if pick=$(apt_pick libncurses-dev libncursesw5-dev); then
            extra_pkgs+=("$pick")
        fi
    fi
fi

# python3 + venv: ensure python3 is present, and that `python3 -m venv` works
need_venv=0
if ! command -v python3 >/dev/null 2>&1; then
    needed_tools+=("python3")
    need_venv=1
elif ! python3 -c 'import venv' >/dev/null 2>&1; then
    need_venv=1
fi

pkgs_to_install=()
for t in "${needed_tools[@]}"; do
    p=$(pkg_for "$PM" "$t")
    [[ -n "$p" ]] && pkgs_to_install+=("$p")
done
if (( need_venv )); then
    vp=$(pkg_for "$PM" "venv")
    [[ -n "$vp" ]] && pkgs_to_install+=("$vp")
fi

# Append the apt extras (already package names, not generic tool names)
if (( ${#extra_pkgs[@]} )); then
    pkgs_to_install+=("${extra_pkgs[@]}")
fi

# de-duplicate
if (( ${#pkgs_to_install[@]} )); then
    IFS=$'\n' pkgs_to_install=($(printf '%s\n' "${pkgs_to_install[@]}" | awk '!seen[$0]++'))
    unset IFS
fi

if (( ${#pkgs_to_install[@]} )); then
    if [[ -z "$PM" ]]; then
        echo "Missing tools and no supported package manager detected." >&2
        echo "Please install manually: ${pkgs_to_install[*]}" >&2
        exit 1
    fi
    install_pkgs "$PM" "${pkgs_to_install[@]}"
fi

# Re-verify after install
for t in git screen curl python3; do
    if ! command -v "$t" >/dev/null 2>&1; then
        echo "Required tool still missing after install: $t" >&2
        exit 1
    fi
done
if ! python3 -c 'import venv' >/dev/null 2>&1; then
    echo "python3 venv module still unavailable." >&2
    exit 1
fi

# --- compat symlinks for libncurses.so.5 / libtinfo.so.5 ---
# Ubuntu 24.04 dropped libncurses5/libtinfo5, but MariaDB 10.11 client
# binaries still ask for the old soname. We point them at the .so.6.
ensure_ncurses5_compat() {
    [[ "${PM:-}" == "apt-get" ]] || return 0
    local nc6 ti6 dir
    nc6=$(dpkg -L libncurses6 2>/dev/null | grep -E 'libncurses\.so\.6$' | head -1 || true)
    ti6=$(dpkg -L libtinfo6   2>/dev/null | grep -E 'libtinfo\.so\.6$'   | head -1 || true)
    [[ -n "$nc6" ]] || return 0
    dir=$(dirname "$nc6")
    if [[ ! -e "$dir/libncurses.so.5" ]]; then
        echo "Creating $dir/libncurses.so.5 -> $(basename "$nc6")"
        $SUDO ln -sf "$(basename "$nc6")" "$dir/libncurses.so.5"
    fi
    if [[ -n "$ti6" && ! -e "$(dirname "$ti6")/libtinfo.so.5" ]]; then
        local tdir; tdir=$(dirname "$ti6")
        echo "Creating $tdir/libtinfo.so.5 -> $(basename "$ti6")"
        $SUDO ln -sf "$(basename "$ti6")" "$tdir/libtinfo.so.5"
    fi
    $SUDO ldconfig 2>/dev/null || true
}
ensure_ncurses5_compat

# --- locate python 3.11+ ---
PY=""
for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1; then
        v=$("$c" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
        major=${v%.*}; minor=${v#*.}
        if [[ "$major" -ge 3 && "$minor" -ge 11 ]]; then
            PY="$c"; break
        fi
    fi
done
if [[ -z "$PY" ]]; then
    echo "Need Python >= 3.11 on PATH." >&2
    exit 1
fi
echo "Using: $($PY --version) at $(command -v $PY)"

# --- paths ---
TMSM_HOME="${TMSM_HOME:-$HOME/.tmsm}"
VENV="$TMSM_HOME/tmsm-venv"
BIN_DIR="${TMSM_BIN_DIR:-$HOME/.local/bin}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$TMSM_HOME" "$BIN_DIR"

# --- venv ---
if [[ ! -d "$VENV" ]]; then
    echo "Creating venv at $VENV"
    "$PY" -m venv "$VENV"
fi
"$VENV/bin/pip" install --upgrade pip >/dev/null
"$VENV/bin/pip" install -e "$SRC_DIR"

# --- launcher ---
LAUNCHER="$BIN_DIR/tmsm"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$VENV/bin/tmsm" "\$@"
EOF
chmod +x "$LAUNCHER"

# --- lazysql (external DB TUI) ---
install_lazysql() {
    if command -v lazysql >/dev/null 2>&1; then
        return 0
    fi
    local arch asset
    arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64)  asset="lazysql_Linux_x86_64.tar.gz" ;;
        aarch64|arm64) asset="lazysql_Linux_arm64.tar.gz" ;;
        i386|i686)     asset="lazysql_Linux_i386.tar.gz" ;;
        *)
            echo "NOTE: skipping lazysql install — no prebuilt binary for arch '$arch'."
            echo "      Install manually: https://github.com/jorgerojas26/lazysql/releases"
            return 0
            ;;
    esac
    local ver="v0.5.1"
    local url="https://github.com/jorgerojas26/lazysql/releases/download/${ver}/${asset}"
    local tmpdir
    tmpdir="$(mktemp -d)"
    trap 'rm -rf "$tmpdir"' RETURN
    echo "Downloading lazysql ${ver}..."
    if ! curl -fsSL "$url" -o "$tmpdir/lazysql.tar.gz"; then
        echo "NOTE: lazysql download failed ($url)."
        echo "      Install manually: https://github.com/jorgerojas26/lazysql/releases"
        return 0
    fi
    tar -xzf "$tmpdir/lazysql.tar.gz" -C "$tmpdir"
    local bin
    bin="$(find "$tmpdir" -type f -name lazysql | head -n1)"
    if [[ -z "$bin" ]]; then
        echo "NOTE: lazysql binary not found in archive."
        return 0
    fi
    install -m 0755 "$bin" "$BIN_DIR/lazysql"
    echo "Installed lazysql -> $BIN_DIR/lazysql"
}
install_lazysql

echo
echo "Installed. Run: tmsm"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
        echo "NOTE: $BIN_DIR is not on your PATH."
        # Add to ~/.bashrc and ~/.profile so it persists across sessions
        for rc in "$HOME/.bashrc" "$HOME/.profile"; do
            if [[ -f "$rc" ]] && ! grep -qF "$BIN_DIR" "$rc"; then
                echo "" >> "$rc"
                echo "# added by tmsm installer" >> "$rc"
                echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$rc"
                echo "Added $BIN_DIR to PATH in $rc"
            fi
        done
        export PATH="$BIN_DIR:$PATH"
        echo "PATH updated for this session. Open a new terminal or run: source ~/.bashrc"
        ;;
esac
