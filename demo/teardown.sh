#!/usr/bin/env bash
# =============================================================================
# Splice — Demo Teardown
#
# Kills all bridge instances started by run_demo.sh
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE_DIR="${REPO_ROOT}/demo/.pids"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

info()  { echo -e "${GREEN}[INFO]${RESET}  $1"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET}  $1"; }

echo ""
echo -e "${BOLD}Tearing down demo bridge instances...${RESET}"
echo ""

stopped=0

if [ -d "$PIDFILE_DIR" ] && ls "${PIDFILE_DIR}"/*.pid &>/dev/null; then
    for pidfile in "${PIDFILE_DIR}"/*.pid; do
        name=$(basename "$pidfile" .pid)
        pid=$(cat "$pidfile" 2>/dev/null || echo "")

        if [ -z "$pid" ]; then
            warn "${name}: empty pidfile, removing"
            rm -f "$pidfile"
            continue
        fi

        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            # Wait up to 5 seconds for graceful shutdown
            waited=0
            while kill -0 "$pid" 2>/dev/null && [ $waited -lt 5 ]; do
                sleep 1
                waited=$((waited + 1))
            done

            if kill -0 "$pid" 2>/dev/null; then
                warn "${name} (PID ${pid}) did not exit gracefully, sending SIGKILL"
                kill -9 "$pid" 2>/dev/null || true
            fi

            info "${name} ${DIM}(PID ${pid})${RESET} stopped"
            stopped=$((stopped + 1))
        else
            info "${name} ${DIM}(PID ${pid})${RESET} was already stopped"
        fi

        rm -f "$pidfile"
    done
else
    info "No pidfiles found — nothing to clean up."
fi

# Clean up the pidfile directory
rmdir "$PIDFILE_DIR" 2>/dev/null || true

echo ""
if [ $stopped -gt 0 ]; then
    info "${BOLD}${stopped} bridge instance(s) stopped.${RESET}"
else
    info "No running instances found."
fi
echo ""
