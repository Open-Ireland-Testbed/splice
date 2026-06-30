#!/usr/bin/env bash
# =============================================================================
# Splice — Pre-Demo Setup
#
# Run this 5 minutes before the demo. It:
#   1. Runs splice setup for all three devices
#   2. Verifies each setup completed successfully
#   3. Verifies NETCONF connectivity to all three devices
#   4. Optionally pre-warms semantic search embeddings
#   5. Prints READY / NOT READY
#
# Does NOT start bridges or Docker stack — that happens during the demo.
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE="${REPO_ROOT}/.venv/bin/splice"

# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
declare -A CONFIGS HOSTS NAMES
DEVICES=("roadm" "teraflex" "juniper")

CONFIGS[roadm]="${REPO_ROOT}/devices/roadm_17/config.yaml"
CONFIGS[teraflex]="${REPO_ROOT}/devices/teraflex/config.yaml"
CONFIGS[juniper]="${REPO_ROOT}/devices/juniper_ila/config.yaml"

HOSTS[roadm]="192.0.2.17"
HOSTS[teraflex]="192.0.2.92"
HOSTS[juniper]="192.0.2.20"

NAMES[roadm]="Lumentum ROADM"
NAMES[teraflex]="Adtran Teraflex"
NAMES[juniper]="Juniper ILA"

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

info()  { echo -e "${GREEN}[  OK  ]${RESET}  $1"; }
fail()  { echo -e "${RED}[ FAIL ]${RESET}  $1"; }
step()  { echo -e "${BLUE}[  ..  ]${RESET}  $1"; }

# ---------------------------------------------------------------------------
# Track overall status
# ---------------------------------------------------------------------------
FAILURES=0

echo ""
echo -e "${BOLD}${BLUE}Pre-Demo Setup${RESET}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""

# ---------------------------------------------------------------------------
# 1. Verify configs exist
# ---------------------------------------------------------------------------
for dev in "${DEVICES[@]}"; do
    if [ ! -f "${CONFIGS[$dev]}" ]; then
        fail "${NAMES[$dev]}: config not found at ${CONFIGS[$dev]}"
        FAILURES=$((FAILURES + 1))
    fi
done

# ---------------------------------------------------------------------------
# 2. Verify NETCONF connectivity (TCP check on port 830)
# ---------------------------------------------------------------------------
echo -e "${BOLD}Checking NETCONF connectivity...${RESET}"
echo ""

for dev in "${DEVICES[@]}"; do
    host="${HOSTS[$dev]}"
    step "${NAMES[$dev]} (${host}:830)..."
    if timeout 5 bash -c "echo >/dev/tcp/${host}/830" 2>/dev/null; then
        info "${NAMES[$dev]} — NETCONF reachable at ${host}:830"
    else
        fail "${NAMES[$dev]} — cannot reach ${host}:830"
        FAILURES=$((FAILURES + 1))
    fi
done

echo ""

# ---------------------------------------------------------------------------
# 3. Run setup for each device
# ---------------------------------------------------------------------------
echo -e "${BOLD}Running splice setup for each device...${RESET}"
echo ""

for dev in "${DEVICES[@]}"; do
    config="${CONFIGS[$dev]}"
    name="${NAMES[$dev]}"
    logfile="${REPO_ROOT}/demo/.pre-demo-${dev}.log"

    step "${name}: running setup..."
    start_time=$SECONDS

    if eval "$BRIDGE --config $config setup" > "$logfile" 2>&1; then
        elapsed=$(( SECONDS - start_time ))

        # Extract key metrics from setup output
        paths=$(grep -oP '\d+(?= paths)' "$logfile" | tail -1 || echo "?")
        jobs=$(grep -oP '\d+(?= jobs)' "$logfile" | tail -1 || echo "?")

        info "${name}: setup complete in ${elapsed}s — ${paths} paths, ${jobs} jobs"
    else
        elapsed=$(( SECONDS - start_time ))
        fail "${name}: setup failed after ${elapsed}s — see ${logfile}"
        FAILURES=$((FAILURES + 1))
    fi
done

echo ""

# ---------------------------------------------------------------------------
# 4. Verify no stale bridge processes on demo ports
# ---------------------------------------------------------------------------
echo -e "${BOLD}Checking for port conflicts...${RESET}"
echo ""

for port in 9340 9341 9342; do
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        fail "Port ${port} is already in use — kill the process before the demo"
        FAILURES=$((FAILURES + 1))
    fi
done

if ! ss -tlnp 2>/dev/null | grep -qE ':934[012] '; then
    info "Ports 9340, 9341, 9342 are free"
fi

echo ""

# ---------------------------------------------------------------------------
# 5. Verify gnmic is available
# ---------------------------------------------------------------------------
if command -v gnmic &>/dev/null; then
    info "gnmic is installed: $(gnmic version 2>&1 | head -1)"
else
    fail "gnmic is not installed"
    FAILURES=$((FAILURES + 1))
fi

# ---------------------------------------------------------------------------
# 6. Verify Docker is available
# ---------------------------------------------------------------------------
if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    info "Docker is running"
else
    fail "Docker is not available or not running"
    FAILURES=$((FAILURES + 1))
fi

# ---------------------------------------------------------------------------
# 7. Pre-warm semantic search embeddings (optional)
# ---------------------------------------------------------------------------
echo ""
echo -e "${BOLD}Pre-warming semantic search...${RESET}"
echo ""

if [ -n "${OPENAI_API_KEY:-}" ]; then
    step "Running semantic search to build embedding cache..."
    if eval "$BRIDGE --config ${CONFIGS[teraflex]} find-paths --semantic --pattern 'Q-factor' --top 1" > /dev/null 2>&1; then
        info "Semantic search embeddings cached for Teraflex"
    else
        echo -e "${YELLOW}[SKIP]${RESET}  Semantic search pre-warm failed (non-critical)"
    fi
else
    echo -e "${YELLOW}[SKIP]${RESET}  OPENAI_API_KEY not set — semantic search will not be available"
fi

echo ""
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"

if [ $FAILURES -eq 0 ]; then
    echo ""
    echo -e "  ${GREEN}${BOLD}READY${RESET}  — All checks passed. You're good to demo."
    echo ""
    echo -e "  Next steps:"
    echo -e "    ${BOLD}./demo/run_demo.sh${RESET}                              ${DIM}# interactive demo${RESET}"
    echo -e "    ${BOLD}./demo/record_demo.sh${RESET}                           ${DIM}# asciinema recording${RESET}"
    echo ""
else
    echo ""
    echo -e "  ${RED}${BOLD}NOT READY${RESET}  — ${FAILURES} check(s) failed. Fix the issues above."
    echo ""
    exit 1
fi
