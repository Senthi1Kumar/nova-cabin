#!/usr/bin/env bash
# Nova Cabin setup for a fresh Raspberry Pi 5 (also works on a Linux laptop). Idempotent: re-run it any time.
#
#   ./setup.sh               full setup (asks before using sudo or installing uv)
#   ./setup.sh --yes         answer yes to every question (non-interactive)
#   ./setup.sh --no-apt      skip system packages (they are never installed without a yes)
#   ./setup.sh --check       only run the doctor (scripts/doctor.py) and exit
#
# Environment:
#   SETUP_DRY_RUN=1          print every step and command, execute nothing
#   SETUP_STEPS="a b"        run only the named steps (preflight apt uv venv requirements driveos driveauth models
#                            audio map env desktop check); default is all of them
#   SETUP_PREFIX=<dir>       keep the venv, models, caches and scratch under <dir>; touches nothing else
#                            (no .env, no desktop entry, no audio routing)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY="${SETUP_DRY_RUN:-0}"
PREFIX="${SETUP_PREFIX:-}"
YES=0
NO_APT=0
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=1 ;;
    --no-apt) NO_APT=1 ;;
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

DRIVEOS_REF="v1.2.0"
DRIVEOS_REPO="https://github.com/Senthi1Kumar/drive-os.git"
APT_PACKAGES="libportaudio2 espeak-ng gpsd gpsd-clients libspeexdsp1 python3-venv"
APT_BUILD_PACKAGES="git curl cmake patchelf build-essential"

if [[ -n "$PREFIX" ]]; then
  PREFIX="${PREFIX/#\~/$HOME}"
  VENV="$PREFIX/venv"
  SCRATCH="$PREFIX/scratch"
  export XDG_DATA_HOME="$PREFIX/data" XDG_CONFIG_HOME="$PREFIX/config" XDG_CACHE_HOME="$PREFIX/cache"
  export HF_HOME="$PREFIX/hf" UV_CACHE_DIR="$PREFIX/uv-cache"
else
  VENV="$ROOT/.venv"
  SCRATCH="$HOME/.cache/nova-cabin"
fi
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
MODELS="$DATA_HOME/drive-os/models"
HUB="${HF_HUB_CACHE:-${HF_HOME:-$HOME/.cache/huggingface}/hub}"
DRIVEOS_SRC="$SCRATCH/drive-os-src"
UV=""
FAILED=()

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ok   %s\n' "$*"; }
warn() { printf '  warn %s\n' "$*" >&2; }
bad()  { printf '  FAIL %s\n' "$*" >&2; FAILED+=("$*"); }

run() {
  if [[ "$DRY" == 1 ]]; then printf '  + %s\n' "$*"; else "$@"; fi
}

step() {
  if [[ "$DRY" == 1 ]]; then printf '[dry-run] STEP %s: %s\n' "$1" "$2"; else printf '\n==> %s: %s\n' "$1" "$2"; fi
}

ask() {
  # ask "question" -> 0 on yes. Never blocks without a terminal: no yes flag and no tty means no.
  [[ "$YES" == 1 ]] && return 0
  [[ "$DRY" == 1 ]] && { printf '  ? %s [would ask]\n' "$1"; return 0; }
  [[ -t 0 ]] || return 1
  local answer=""
  read -r -p "$1 [Y/n] " answer || answer=n
  case "${answer:-Y}" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

pip_install() {
  if [[ -n "$UV" ]]; then run "$UV" pip install --python "$VENV/bin/python" "$@"
  else run "$VENV/bin/python" -m pip install "$@"; fi
}

drive_src() {
  # Shallow clone of the pinned DriveOS tag: build_parakeet.sh and pi_audio_setup.sh live there.
  if [[ -f "$DRIVEOS_SRC/scripts/build_parakeet.sh" ]]; then return 0; fi
  run mkdir -p "$SCRATCH"
  run git clone --quiet --depth 1 --branch "$DRIVEOS_REF" "$DRIVEOS_REPO" "$DRIVEOS_SRC"
}

lock_rows() {
  grep -v '^[[:space:]]*#' "$ROOT/models.lock" | grep -v '^[[:space:]]*$' || true
}

lock_path() {
  # Resolve a lock path (with optional @hf/ prefix and glob) to an existing file, or print nothing.
  local rel="$1" pat f
  if [[ "$rel" == @hf/* ]]; then pat="$HUB/${rel#@hf/}"; else pat="$MODELS/$rel"; fi
  # newest first, so a newer Phonon snapshot wins over a stale one
  while IFS= read -r f; do [[ -f "$f" ]] && { printf '%s' "$f"; return 0; }; done \
    < <(compgen -G "$pat" | while IFS= read -r g; do printf '%s\t%s\n' "$(stat -c %Y "$g" 2>/dev/null || echo 0)" "$g"; done \
        | sort -rn | cut -f2-)
  return 0
}

# ── steps ───────────────────────────────────────────────────────────────────────────────────────────────────

step_preflight() {
  step preflight "check the machine (OS, git, curl, disk)"
  local missing=()
  for tool in git curl; do command -v "$tool" >/dev/null 2>&1 || missing+=("$tool"); done
  if ((${#missing[@]})); then warn "missing: ${missing[*]} (installed by the apt step, or install them yourself)"; fi
  [[ "$(uname -s)" == Linux ]] || warn "this script targets Linux; continuing anyway"
  local free_kb disk_dir="$HOME"
  [[ -n "$PREFIX" && -d "$PREFIX" ]] && disk_dir="$PREFIX"
  free_kb="$(df -Pk "$disk_dir" 2>/dev/null | awk 'NR==2{print $4}' || true)"
  if [[ -n "$free_kb" ]]; then
    ok "free disk: $((free_kb / 1024 / 1024)) GB (needs about 6 GB)"
    ((free_kb > 6 * 1024 * 1024)) || warn "less than 6 GB free; the install may fail"
  fi
  run mkdir -p "$SCRATCH"
}

step_apt() {
  step apt "system packages (audio, speech, gpsd, build tools)"
  if [[ "$NO_APT" == 1 ]]; then say "  skipped (--no-apt)"; return 0; fi
  if [[ "$DRY" != 1 ]] && command -v dpkg >/dev/null 2>&1; then
    local need=() p
    for p in $APT_PACKAGES $APT_BUILD_PACKAGES; do dpkg -s "$p" >/dev/null 2>&1 || need+=("$p"); done
    if ((${#need[@]} == 0)); then ok "all packages already installed"; return 0; fi
    say "  missing: ${need[*]}"
    if ask "Install them with sudo apt-get?"; then
      sudo apt-get update || bad "apt-get update failed (needs sudo)"
      sudo apt-get install -y "${need[@]}" || bad "apt-get install failed (needs sudo)"
    else
      say "  skipped. Install later with: sudo apt-get install -y ${need[*]}"
    fi
    return 0
  fi
  run sudo apt-get update || bad "apt-get update failed (needs sudo)"
  run sudo apt-get install -y $APT_PACKAGES $APT_BUILD_PACKAGES || bad "apt-get install failed (needs sudo)"
}

step_uv() {
  step uv "uv (fast installer); falls back to python -m venv and pip"
  if command -v uv >/dev/null 2>&1; then UV="$(command -v uv)"
  elif [[ -x "$HOME/.local/bin/uv" ]]; then UV="$HOME/.local/bin/uv"; fi
  if [[ -n "$UV" ]]; then ok "uv at $UV"; return 0; fi
  if ask "uv is not installed. Install it to ~/.local/bin (https://astral.sh/uv)?"; then
    run mkdir -p "$SCRATCH"
    run curl -LsSf -o "$SCRATCH/uv-install.sh" https://astral.sh/uv/install.sh
    run env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh "$SCRATCH/uv-install.sh"
    UV="$HOME/.local/bin/uv"
    [[ "$DRY" == 1 || -x "$UV" ]] || { warn "uv install failed; using pip"; UV=""; }
  else
    say "  skipped; using python -m venv and pip"
  fi
}

step_venv() {
  step venv "virtualenv at $VENV (Python 3.12)"
  if [[ -x "$VENV/bin/python" ]]; then ok "exists: $VENV"; return 0; fi
  if [[ -n "$UV" ]]; then
    run "$UV" venv --python 3.12 "$VENV"
  else
    local py=""
    for c in python3.12 python3.13 python3.11 python3; do
      if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)'; then
        py="$c"; break
      fi
    done
    [[ -n "$py" || "$DRY" == 1 ]] || { bad "Python 3.11-3.13 not found"; return 0; }
    run "${py:-python3}" -m venv "$VENV"
    run "$VENV/bin/python" -m pip install --quiet --upgrade pip wheel
  fi
}

step_requirements() {
  step requirements "base requirements (requirements.txt)"
  pip_install -r "$ROOT/requirements.txt"
}

step_driveos() {
  step driveos "DriveOS $DRIVEOS_REF with voice and biometrics (requirements-pi.txt)"
  pip_install -r "$ROOT/requirements-pi.txt"
}

step_driveauth() {
  step driveauth "DriveAuth wheel from packaging/vendor"
  local wheel
  wheel="$(ls "$ROOT"/packaging/vendor/driveauth_edge-*.whl 2>/dev/null | sort | tail -n1 || true)"
  if [[ -z "$wheel" ]]; then
    if [[ "$DRY" == 1 ]]; then wheel="$ROOT/packaging/vendor/driveauth_edge-<version>.whl"
    else bad "packaging/vendor/driveauth_edge-*.whl is missing"; return 0; fi
  fi
  pip_install "$wheel"
}

verify_lock() {
  # Print one line per lock row; record failures. Returns the number of missing or mismatched rows.
  local name rel sha src file got n=0
  while IFS='|' read -r name rel sha src; do
    file="$(lock_path "$rel")"
    if [[ -z "$file" ]]; then
      if [[ "$src" == build:* ]]; then warn "$name: not built (speech falls back to faster-whisper)"
      else bad "$name: missing ($rel)"; n=$((n + 1)); fi
    elif [[ "$sha" == "-" ]]; then ok "$name (not pinned)"
    elif [[ "$(sha256_of "$file")" == "$sha" ]]; then ok "$name sha256 verified"
    else bad "$name: sha256 mismatch ($rel)"; n=$((n + 1)); fi
  done < <(lock_rows)
  return 0
}

step_models() {
  step models "models from models.lock (Silero, Smart Turn, Parakeet, Phonon, speaker/anti-spoof)"
  run mkdir -p "$MODELS" "$SCRATCH"
  if [[ "$DRY" == 1 ]]; then
    local name rel sha src
    while IFS='|' read -r name rel sha src; do
      printf '  + ensure %s (%s) sha256=%s\n' "$name" "${src%%:*}" "${sha:0:12}"
    done < <(lock_rows)
    run "$VENV/bin/driveos" fetch-models
    return 0
  fi

  local name rel sha src file tmp need_cmd=0 need_build=0
  while IFS='|' read -r name rel sha src; do
    file="$(lock_path "$rel")"
    case "$src" in
      url:*)
        if [[ -n "$file" && "$(sha256_of "$file")" == "$sha" ]]; then ok "$name present"; continue; fi
        tmp="$SCRATCH/$name.part"
        say "  downloading $name"
        if curl -fL --retry 3 --silent --show-error -o "$tmp" "${src#url:}"; then
          if [[ "$(sha256_of "$tmp")" == "$sha" ]]; then mkdir -p "$(dirname "$MODELS/$rel")"; mv "$tmp" "$MODELS/$rel"
          else rm -f "$tmp"; bad "$name: downloaded file does not match the pinned sha256"; fi
        else rm -f "$tmp"; bad "$name: download failed"; fi ;;
      cmd:*)
        if [[ -z "$file" || ( "$sha" != "-" && "$(sha256_of "$file")" != "$sha" ) ]]; then need_cmd=1; fi ;;
      build:*)
        [[ -n "$file" ]] || need_build=1 ;;
    esac
  done < <(lock_rows)

  if [[ "$need_cmd" == 1 ]]; then
    if [[ -x "$VENV/bin/driveos" ]]; then
      "$VENV/bin/driveos" fetch-models || warn "driveos fetch-models reported failures"
    else
      bad "driveos is not installed in $VENV; cannot fetch Parakeet, Phonon and biometric models"
    fi
  fi
  if [[ "$need_build" == 1 ]]; then
    local missing=()
    for t in git cmake patchelf g++; do command -v "$t" >/dev/null 2>&1 || missing+=("$t"); done
    if ((${#missing[@]})); then
      warn "cannot build libparakeet.so (missing: ${missing[*]}); install them (apt step) and re-run. Speech uses faster-whisper until then"
    else
      drive_src && bash "$DRIVEOS_SRC/scripts/build_parakeet.sh" "$MODELS/parakeet" || warn "libparakeet.so build failed"
    fi
  fi
  say "  verifying against models.lock"
  verify_lock
}

step_audio() {
  step audio "audio routing (WM8960 HAT or XVF3800)"
  if [[ -n "$PREFIX" ]]; then say "  skipped (SETUP_PREFIX)"; return 0; fi
  if [[ "$DRY" == 1 ]]; then
    run bash "$DRIVEOS_SRC/scripts/pi_audio_setup.sh" --user
    return 0
  fi
  if { arecord -l 2>/dev/null; pactl list short sources 2>/dev/null; } | grep -qi 'xvf3800'; then
    drive_src && bash "$DRIVEOS_SRC/scripts/pi_audio_setup.sh" --user || warn "pi_audio_setup.sh reported a problem"
  elif aplay -l 2>/dev/null | grep -qi 'wm8960'; then
    ok "WM8960 sound card detected; the app uses PipeWire's default devices with software echo cancellation"
  else
    warn "no XVF3800 or WM8960 audio device found. For the WM8960 HAT add 'dtoverlay=wm8960-soundcard' to /boot/firmware/config.txt and reboot. Without a microphone the app uses the keyboard"
  fi
}

step_map() {
  step map "map service (prebuilt, if shipped)"
  local pkg
  pkg="$(ls "$ROOT"/packaging/vendor/map-service-*.tar.gz 2>/dev/null | sort | tail -n1 || true)"
  if [[ -z "$pkg" ]]; then say "  no prebuilt map service in packaging/vendor; the 2D map is used"; return 0; fi
  run mkdir -p "$ROOT/packaging/map-service"
  run tar -xzf "$pkg" -C "$ROOT/packaging/map-service"
}

fill_env_blank() {
  # fill_env_blank NAME VALUE: set NAME only when its line exists and is empty (a trailing comment is kept).
  local name="$1" value="$2"
  local line comment
  line="$(grep -E "^${name}=[[:space:]]*(#.*)?$" "$ROOT/.env" | head -n1 || true)"
  [[ -n "$line" ]] || return 0
  comment=""
  [[ "$line" == *"#"* ]] && comment="  #${line#*#}"
  NAME="$name" NEW="${name}=${value}${comment}" \
    awk -v n="^${name}=[[:space:]]*(#.*)?$" '$0 ~ n && !d { print ENVIRON["NEW"]; d=1; next } { print }' "$ROOT/.env" > "$ROOT/.env.tmp" \
    && mv "$ROOT/.env.tmp" "$ROOT/.env"
  chmod 600 "$ROOT/.env"
}

step_env() {
  step env ".env from .env.example (mode 600, only if missing)"
  if [[ -n "$PREFIX" ]]; then say "  skipped (SETUP_PREFIX)"; return 0; fi
  if [[ ! -f "$ROOT/.env" ]]; then
    run install -m 600 "$ROOT/.env.example" "$ROOT/.env"
  else
    run chmod 600 "$ROOT/.env"
    say "  .env exists; left as is"
  fi
  if [[ "$DRY" != 1 && -f "$ROOT/.env" ]]; then
    fill_env_blank SILERO_MODEL "$MODELS/silero_vad.onnx"
    fill_env_blank SMART_TURN_MODEL "$MODELS/smart-turn-v3.2-cpu.onnx"
    say "  add your API keys to $ROOT/.env (OPENROUTER_API_KEY at least)"
  else
    say "  + set SILERO_MODEL and SMART_TURN_MODEL when blank"
  fi
}

step_desktop() {
  step desktop "desktop entry"
  if [[ -n "$PREFIX" ]]; then say "  skipped (SETUP_PREFIX)"; return 0; fi
  local dir="$DATA_HOME/applications" file
  file="$dir/nova-cabin.desktop"
  if [[ "$DRY" == 1 ]]; then printf '  + write %s\n' "$file"; return 0; fi
  mkdir -p "$dir"
  cat > "$file" <<EOF
[Desktop Entry]
Type=Application
Name=Nova Cabin
Comment=In-car voice assistant
Exec=$VENV/bin/python $ROOT/main.py --fullscreen
Path=$ROOT
Terminal=false
Categories=Utility;
EOF
  ok "wrote $file"
}

step_check() {
  step check "doctor"
  if ! run_doctor; then
    warn "doctor reported a missing required item (see the table above)"
    FAILED+=("doctor reported a missing required item")
  fi
}

run_doctor() {
  local doctor="$ROOT/scripts/doctor.py" py="$VENV/bin/python"
  if [[ ! -f "$doctor" ]]; then
    say "  doctor not available yet (scripts/doctor.py is missing); skipping the check"
    return 0
  fi
  [[ -x "$py" ]] || py="$(command -v python3)"
  if [[ "$DRY" == 1 ]]; then printf '  + %s %s\n' "$py" "$doctor"; return 0; fi
  "$py" "$doctor"
}

# ── main ────────────────────────────────────────────────────────────────────────────────────────────────────

cd "$ROOT"

if [[ "$CHECK_ONLY" == 1 ]]; then
  say "Nova Cabin doctor"
  run_doctor
  exit $?
fi

ALL_STEPS="preflight apt uv venv requirements driveos driveauth models audio map env desktop check"
for s in ${SETUP_STEPS:-$ALL_STEPS}; do
  declare -F "step_$s" >/dev/null || { echo "unknown step: $s" >&2; exit 2; }
  "step_$s"
done

say
if ((${#FAILED[@]})); then
  say "Setup finished with problems:"
  printf '  - %s\n' "${FAILED[@]}"
  exit 1
fi
if [[ "$DRY" == 1 ]]; then say "Dry run complete; nothing was changed."; else say "Setup complete. Run: $VENV/bin/python $ROOT/main.py"; fi
