#!/usr/bin/env bash
# Install a shell shortcut; dependencies and the server are managed separately.
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

# Prefer the invoking shell; SHELL is the fallback for launchers and subprocesses.
CURRENT_SHELL="$(ps -p "$PPID" -o comm= 2>/dev/null || true)"
CURRENT_SHELL="${CURRENT_SHELL##*/}"
CURRENT_SHELL="${CURRENT_SHELL#-}"
case "$CURRENT_SHELL" in
  bash|zsh) ;;
  *) CURRENT_SHELL="${SHELL:-unknown}"; CURRENT_SHELL="${CURRENT_SHELL##*/}" ;;
esac

case "$CURRENT_SHELL" in
  zsh) CONFIG_FILE="${ZDOTDIR:-$HOME}/.zshrc" ;;
  bash)
    CONFIG_FILE="$HOME/.bashrc"
    # macOS Terminal starts Bash as a login shell.
    if [[ "$(uname -s)" == Darwin ]]; then
      CONFIG_FILE="$HOME/.bash_profile"
      if [[ ! -e "$CONFIG_FILE" && -e "$HOME/.bash_login" ]]; then
        CONFIG_FILE="$HOME/.bash_login"
      elif [[ ! -e "$CONFIG_FILE" && -e "$HOME/.profile" ]]; then
        CONFIG_FILE="$HOME/.profile"
      fi
    fi
    ;;
  *) printf 'Unsupported shell: %s (supported: bash, zsh)\n' "$CURRENT_SHELL" >&2; exit 1 ;;
esac

# Quote twice: once for the command path, then for the alias definition.
shell_quote() {
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}
ALIAS_COMMAND="make -C $(shell_quote "$PROJECT_DIR") open"
ALIAS_LINE="alias token-tracker=$(shell_quote "$ALIAS_COMMAND")"
BLOCK_START='# >>> token-tracker alias >>>'
BLOCK_END='# <<< token-tracker alias <<<'

mkdir -p -- "$(dirname -- "$CONFIG_FILE")"
touch -- "$CONFIG_FILE"
TEMP_FILE="$(mktemp "${CONFIG_FILE}.token-tracker.XXXXXX")"
trap 'rm -f -- "$TEMP_FILE"' EXIT

# Replace our own block on repeated installs, preserving other configuration.
awk -v start="$BLOCK_START" -v end="$BLOCK_END" '
  $0 == start { managed = 1; next }
  $0 == end && managed { managed = 0; next }
  !managed { print }
  END { if (managed) exit 1 }
' "$CONFIG_FILE" > "$TEMP_FILE"
printf '%s\n%s\n%s\n' "$BLOCK_START" "$ALIAS_LINE" "$BLOCK_END" >> "$TEMP_FILE"
cat "$TEMP_FILE" > "$CONFIG_FILE"

printf 'Installed token-tracker alias in %s\n' "$CONFIG_FILE"
printf 'Run: source %s\n' "$(shell_quote "$CONFIG_FILE")"
printf 'Then run: token-tracker (start the tracker server first).\n'
