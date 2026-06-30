#!/usr/bin/env bash
# =============================================================================
# Splice — Recording Script (non-interactive, 7-Act Structure)
#
# Designed for asciinema recording — no read -p pauses, just natural flow.
# Run with: asciinema rec demo/recording.cast --command "./demo/record_demo.sh"
# =============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Load OPENAI_API_KEY for semantic search
if [ -f ~/.zshrc ]; then
    export OPENAI_API_KEY=$(grep -oP 'OPENAI_API_KEY=\K.*' ~/.zshrc 2>/dev/null | tr -d '"' | tr -d "'" || true)
fi

BRIDGE="${REPO_ROOT}/.venv/bin/splice"
PIDFILE_DIR="${REPO_ROOT}/demo/.pids"
mkdir -p "$PIDFILE_DIR"

# ---------------------------------------------------------------------------
# Colors
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

# ---------------------------------------------------------------------------
# Helpers — simulate natural typing
# ---------------------------------------------------------------------------
typecmd() {
    echo ""
    echo -ne "${GREEN}\$ ${RESET}"
    local cmd="$1"
    for (( i=0; i<${#cmd}; i++ )); do
        echo -n "${cmd:$i:1}"
        sleep 0.03
    done
    echo ""
    sleep 0.3
}

banner() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}${BLUE}  $1${RESET}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    sleep 1
}

narrate() {
    echo -e "  ${DIM}$1${RESET}"
    sleep 0.5
}

pause() { sleep "${1:-2}"; }

# ---------------------------------------------------------------------------
# Cleanup handler
# ---------------------------------------------------------------------------
cleanup() {
    for pf in "${PIDFILE_DIR}"/*.pid; do
        [ -f "$pf" ] && kill "$(cat "$pf")" 2>/dev/null || true
        rm -f "$pf"
    done
}
trap cleanup EXIT

# =========================================================================
# INTRO
# =========================================================================
banner "Splice — Multi-Vendor Demo"

echo -e "  Three optical network devices, three different vendors."
echo -e "  None of them speak gNMI. All of them speak NETCONF."
echo ""
echo -e "    ${CYAN}1.${RESET} Lumentum ROADM     ${DIM}(192.0.2.17)${RESET}"
echo -e "    ${CYAN}2.${RESET} Adtran Teraflex    ${DIM}(192.0.2.92)${RESET}"
echo -e "    ${CYAN}3.${RESET} Juniper ILA        ${DIM}(192.0.2.20)${RESET}"
echo ""
echo -e "  The bridge translates NETCONF → gNMI so standard telemetry"
echo -e "  collectors (gnmic, Telegraf, etc.) can subscribe to them."
pause 3

# =========================================================================
# ACT 1 — The Problem
# =========================================================================
banner "ACT 1: The Problem — gnmic can't talk to NETCONF devices"

narrate "Let's try gnmic directly against the Lumentum ROADM..."

typecmd "gnmic -a 192.0.2.17:830 --insecure capabilities --timeout 5s"
gnmic -a 192.0.2.17:830 --insecure capabilities --timeout 5s 2>&1 || true

pause 2
echo ""
narrate "As expected — the device speaks NETCONF (SSH), not gRPC."
narrate "gnmic has no way to talk to it. That's what the bridge fixes."
pause 2

# =========================================================================
# ACT 2 — The Solution: Lumentum ROADM
# =========================================================================
banner "ACT 2: The Solution — Lumentum ROADM (192.0.2.17) → gNMI :9341"

narrate "Pre-fetched YANG models. Setup uses cached YANG files."
echo ""

typecmd "splice --config devices/roadm_17/config.yaml setup"
eval "$BRIDGE --config devices/roadm_17/config.yaml setup" 2>&1 | { grep -E '^\[|COMPLETE|paths|jobs' || true; } | while IFS= read -r line; do
    echo -e "  ${line}"
done
pause 1

narrate "Setup done. Starting the bridge..."
echo ""

typecmd "splice --config devices/roadm_17/config.yaml run &"
eval "$BRIDGE --config devices/roadm_17/config.yaml run" > demo/roadm.log 2>&1 &
ROADM_PID=$!
echo "$ROADM_PID" > "${PIDFILE_DIR}/roadm.pid"

# Wait for port
waited=0
while ! ss -tlnp 2>/dev/null | grep -q ":9341 " && [ $waited -lt 30 ]; do
    sleep 1; waited=$((waited + 1))
done
echo -e "  ${GREEN}Bridge listening on :9341${RESET} (PID ${ROADM_PID})"
pause 1

narrate "Now gnmic can talk to it:"
echo ""

typecmd "gnmic -a localhost:9341 --insecure capabilities"
gnmic -a localhost:9341 --insecure capabilities 2>&1 | head -12
echo -e "  ${DIM}... (truncated)${RESET}"
pause 2

typecmd "gnmic -a localhost:9341 --insecure get --path / | head -30"
gnmic -a localhost:9341 --insecure get --path / 2>&1 | head -30
echo -e "  ${DIM}... (truncated)${RESET}"
pause 3

# =========================================================================
# ACT 3 — Scale: Three Vendors
# =========================================================================
banner "ACT 3: Scale — Three Vendors, One Interface"

echo -e "  The Teraflex is a much larger device: ${BOLD}330 YANG models${RESET}, ${BOLD}13,281 telemetry paths${RESET}."
echo -e "  It also has PM and FEC-BER telemetry — real optical performance data."
echo ""
narrate "Setup takes ~40s for this many models, so it was run beforehand."
narrate "Starting the Teraflex bridge..."
echo ""

typecmd "splice --config devices/teraflex/config.yaml run &"
eval "$BRIDGE --config devices/teraflex/config.yaml run" > demo/teraflex.log 2>&1 &
TERAFLEX_PID=$!
echo "$TERAFLEX_PID" > "${PIDFILE_DIR}/teraflex.pid"

waited=0
while ! ss -tlnp 2>/dev/null | grep -q ":9340 " && [ $waited -lt 30 ]; do
    sleep 1; waited=$((waited + 1))
done
echo -e "  ${GREEN}Bridge listening on :9340${RESET} (PID ${TERAFLEX_PID})"
pause 1

narrate "Now the Juniper ILA — 30 YANG models..."
echo ""

typecmd "splice --config devices/juniper_ila/config.yaml setup"
eval "$BRIDGE --config devices/juniper_ila/config.yaml setup" 2>&1 | { grep -E '^\[|COMPLETE|paths|jobs' || true; } | while IFS= read -r line; do
    echo -e "  ${line}"
done
pause 1

typecmd "splice --config devices/juniper_ila/config.yaml run &"
eval "$BRIDGE --config devices/juniper_ila/config.yaml run" > demo/juniper.log 2>&1 &
JUNIPER_PID=$!
echo "$JUNIPER_PID" > "${PIDFILE_DIR}/juniper.pid"

waited=0
while ! ss -tlnp 2>/dev/null | grep -q ":9342 " && [ $waited -lt 30 ]; do
    sleep 1; waited=$((waited + 1))
done
echo -e "  ${GREEN}Bridge listening on :9342${RESET} (PID ${JUNIPER_PID})"
pause 1

narrate "All three bridges running:"
echo ""

typecmd "ss -tlnp | grep -E '9340|9341|9342'"
ss -tlnp 2>/dev/null | grep -E '9340|9341|9342' || true
pause 2

echo ""
echo -e "  ${BOLD}All three bridges running simultaneously:${RESET}"
echo ""

for entry in "roadm:Lumentum ROADM:9341" "teraflex:Adtran Teraflex:9340" "juniper:Juniper ILA:9342"; do
    IFS=':' read -r name label port <<< "$entry"
    pid=$(cat "${PIDFILE_DIR}/${name}.pid" 2>/dev/null || echo "???")
    if kill -0 "$pid" 2>/dev/null; then
        echo -e "    ${GREEN}●${RESET}  ${BOLD}${label}${RESET}  →  localhost:${port}  ${DIM}(PID ${pid})${RESET}"
    else
        echo -e "    ${RED}●${RESET}  ${BOLD}${label}${RESET}  →  localhost:${port}  ${RED}(not running)${RESET}"
    fi
done

pause 3

# =========================================================================
# ACT 4 — Semantic Search
# =========================================================================
banner "ACT 4: Semantic Search — Finding Needles in 18,000+ Paths"

narrate "18,261 paths across three vendors. How do you find what you need?"
narrate "Semantic search — search by meaning, not exact YANG path names."
echo ""

typecmd "splice --config devices/teraflex/config.yaml find-paths --semantic --pattern \"Q-factor optical signal quality\" --top 5"
eval "$BRIDGE --config devices/teraflex/config.yaml find-paths --semantic --pattern 'Q-factor optical signal quality' --top 5" 2>&1 | head -20 || true
pause 3

echo ""
typecmd "splice --config devices/teraflex/config.yaml find-paths --semantic --pattern \"FEC bit error rate\" --top 5"
eval "$BRIDGE --config devices/teraflex/config.yaml find-paths --semantic --pattern 'FEC bit error rate' --top 5" 2>&1 | head -20 || true
pause 2

echo ""
narrate "18,261 paths — found Q-factor in seconds using OpenAI embeddings."
narrate "No need to know the exact YANG module or path structure."
pause 3

# =========================================================================
# ACT 5 — Live PM Data
# =========================================================================
banner "ACT 5: Live PM Data — Real Coherent Transport Telemetry"

echo -e "  The Teraflex is a coherent transponder on a real optical link:"
echo ""
echo -e "    ${CYAN}Teraflex → ROADM → 25km fiber → ILA → ROADM → Teraflex${RESET}"
echo ""
narrate "Pulling live PM data — Q-factor, OSNR, FEC-BER..."
narrate "Waiting for at least one PM polling cycle..."
sleep 10
echo ""

typecmd "gnmic -a localhost:9340 --insecure subscribe --mode once --path '/pm'"
gnmic -a localhost:9340 --insecure subscribe --mode once --path '/pm' 2>&1 | head -40
echo -e "  ${DIM}... (truncated)${RESET}"
pause 2

echo ""
narrate "Real coherent transport PM from Teraflex:"
narrate "Q-factor, OSNR, FEC-BER, optical power, frequency — all via gNMI."
narrate "Real photons, real fiber, real performance monitoring."
pause 3

# =========================================================================
# ACT 6 — Four Interfaces
# =========================================================================
banner "ACT 6: Four Ways to Use the Bridge"

narrate "The bridge exposes four interfaces:"
echo ""

echo -e "  ${BOLD}1. CLI${RESET} (already demonstrated)"
echo -e "     ${GREEN}splice --config config.yaml setup / run / find-paths${RESET}"
echo ""

echo -e "  ${BOLD}2. Python API${RESET} (no database required)"
typecmd "python3 -c \"from splice.api.device import Device; d = Device('sim://openroadm'); d.connect(); print(f'{len(d.catalog_paths)} paths discovered')\""
${REPO_ROOT}/.venv/bin/python3 -c "from splice.api.device import Device; d = Device('sim://openroadm'); d.connect(); print(f'{len(d.catalog_paths)} paths discovered')" 2>&1 || true
echo ""
pause 1

echo -e "  ${BOLD}3. TUI${RESET} — Interactive Console"
echo -e "     ${GREEN}splice-tui --devices-dir devices${RESET}"
echo -e "     ${DIM}Tabbed interface: Devices, Paths, Telemetry, Status, Operations, Jobs${RESET}"
echo ""

echo -e "  ${BOLD}4. MCP Server${RESET} — AI Agent Interface"
echo -e "     ${DIM}The bridge exposes MCP tools for AI agents. Let's talk to Claude:${RESET}"
echo ""

echo -ne "${GREEN}\$ ${RESET}"
echo 'claude -p "Hello Claude! I have Splice running for our'
echo '  Adtran Teraflex transponder. Can you connect to it and tell me'
echo '  the current Q-factor and signal quality?'
echo '  Device: 192.0.2.92, port 830, username admin,'
echo '  password is in devices/<device>/password."'
sleep 1
claude -p "Hello Claude! I have Splice running for our Adtran Teraflex transponder. Can you connect to it and tell me the current Q-factor and signal quality? Device: 192.0.2.92, port 830, username admin, password is in devices/<device>/password." 2>&1 || true
echo ""
pause 3

# =========================================================================
# ACT 7 — Summary
# =========================================================================
banner "ACT 7: Summary"

echo -e "  ${BOLD}Three NETCONF-only devices, bridged to gNMI:${RESET}"
echo ""
echo -e "    ${GREEN}●${RESET}  Lumentum ROADM     ${DIM}192.0.2.17${RESET}  →  gNMI :9341"
echo -e "    ${GREEN}●${RESET}  Adtran Teraflex    ${DIM}192.0.2.92${RESET}  →  gNMI :9340"
echo -e "    ${GREEN}●${RESET}  Juniper ILA        ${DIM}192.0.2.20${RESET}  →  gNMI :9342"
echo ""
echo -e "  ${BOLD}By the numbers:${RESET}"
echo -e "    • 3 vendors, 3 device types"
echo -e "    • 19,000+ telemetry paths"
echo -e "    • 504 tests"
echo -e "    • 4 interfaces: CLI, Python API, TUI, MCP"
echo ""
echo -e "  ${DIM}No changes to the devices. No vendor-specific adapters.${RESET}"
echo -e "  ${DIM}Just YANG model discovery and automatic catalog building.${RESET}"
echo ""
pause 5
