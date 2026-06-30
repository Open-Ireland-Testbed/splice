#!/usr/bin/env bash
# =============================================================================
# Splice — Multi-Vendor Demo (7-Act Structure)
#
# Demonstrates the bridge across three vendors:
#   1. Lumentum ROADM     (192.0.2.17)  → gNMI :9341
#   2. Adtran Teraflex    (192.0.2.92)  → gNMI :9340
#   3. Juniper ILA        (192.0.2.20)  → gNMI :9342
#
# Usage:  ./demo/run_demo.sh
# Cleanup: ./demo/teardown.sh
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
BRIDGE="${REPO_ROOT}/.venv/bin/splice"
PIDFILE_DIR="${REPO_ROOT}/demo/.pids"

ROADM_CONFIG="${REPO_ROOT}/devices/roadm_17/config.yaml"
TERAFLEX_CONFIG="${REPO_ROOT}/devices/teraflex/config.yaml"
JUNIPER_CONFIG="${REPO_ROOT}/devices/juniper_ila/config.yaml"

ROADM_PORT=9341
TERAFLEX_PORT=9340
JUNIPER_PORT=9342

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
# Helpers
# ---------------------------------------------------------------------------
banner() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}${BLUE}  $1${RESET}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
}

section() {
    echo ""
    echo -e "${CYAN}── $1 ──${RESET}"
    echo ""
}

info()    { echo -e "${GREEN}[INFO]${RESET}  $1"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $1"; }
error()   { echo -e "${RED}[FAIL]${RESET}  $1"; }
cmd()     { echo -e "${DIM}\$ $1${RESET}"; }
prompt()  { echo ""; read -rp $'\033[1;33m'"[Press Enter to continue]"$'\033[0m '; echo ""; }

start_bridge() {
    local name="$1" config="$2" port="$3"
    info "Starting bridge for ${BOLD}${name}${RESET} on gNMI port ${BOLD}${port}${RESET} ..."
    cmd "splice --config $config run &"
    eval "$BRIDGE --config $config run" > "${REPO_ROOT}/demo/${name}.log" 2>&1 &
    local pid=$!
    echo "$pid" > "${PIDFILE_DIR}/${name}.pid"
    info "PID ${pid} — log at demo/${name}.log"

    # Wait for gRPC port to open (up to 30 sec)
    local waited=0
    while ! ss -tlnp 2>/dev/null | grep -q ":${port} " && [ $waited -lt 30 ]; do
        sleep 1
        waited=$((waited + 1))
    done

    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        info "${GREEN}${name} is listening on :${port}${RESET}"
    else
        warn "${name} did not bind to :${port} within 30s — check demo/${name}.log"
    fi
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
banner "Splice — Multi-Vendor Demo"

echo -e "  This demo shows the bridge working across ${BOLD}three vendors${RESET}:"
echo ""
echo -e "    ${CYAN}1.${RESET} Lumentum ROADM     ${DIM}(192.0.2.17)${RESET}  → gNMI :${ROADM_PORT}"
echo -e "    ${CYAN}2.${RESET} Adtran Teraflex    ${DIM}(192.0.2.92)${RESET}  → gNMI :${TERAFLEX_PORT}"
echo -e "    ${CYAN}3.${RESET} Juniper ILA        ${DIM}(192.0.2.20)${RESET}  → gNMI :${JUNIPER_PORT}"
echo ""
echo -e "  Each device speaks NETCONF only — no native gNMI support."
echo -e "  The bridge discovers YANG models, builds a telemetry catalog,"
echo -e "  and serves standard gNMI to any client."
echo ""

# Verify configs exist
for cfg in "$ROADM_CONFIG" "$TERAFLEX_CONFIG" "$JUNIPER_CONFIG"; do
    if [ ! -f "$cfg" ]; then
        error "Config not found: $cfg"
        exit 1
    fi
done
info "All device configs found."

# Verify gnmic is installed
if ! command -v gnmic &>/dev/null; then
    error "gnmic not found. Install: https://gnmic.openconfig.net/install/"
    exit 1
fi
info "gnmic is available."

# Cleanup handler — only on abnormal exit (INT/TERM), not normal completion
cleanup() {
    echo ""
    warn "Interrupted — stopping bridge processes..."
    for pf in "${PIDFILE_DIR}"/*.pid; do
        [ -f "$pf" ] && kill "$(cat "$pf")" 2>/dev/null || true
        rm -f "$pf"
    done
}
trap cleanup INT TERM

# Clean up any leftover PIDs
mkdir -p "$PIDFILE_DIR"
if ls "${PIDFILE_DIR}"/*.pid &>/dev/null; then
    warn "Cleaning up leftover bridge processes from a previous run..."
    "${REPO_ROOT}/demo/teardown.sh" 2>/dev/null || true
fi

prompt

# =========================================================================
# ACT 1 — The Problem: gnmic can't talk to a NETCONF-only device
# =========================================================================
banner "ACT 1: The Problem"

echo -e "  These devices only speak ${BOLD}NETCONF${RESET} (SSH, port 830)."
echo -e "  Modern telemetry collectors expect ${BOLD}gNMI${RESET} (gRPC, port 9339+)."
echo ""
echo -e "  Let's prove it — try gnmic directly against the Lumentum ROADM:"
echo ""

cmd "gnmic -a 192.0.2.17:830 --insecure capabilities"
echo ""
echo -e "  ${YELLOW}^^^ This will fail. The device doesn't speak gRPC.${RESET}"
echo ""
echo -e "  ${DIM}Try it yourself:${RESET}"
echo -e "  ${BOLD}gnmic -a 192.0.2.17:830 --insecure capabilities${RESET}"

prompt

# =========================================================================
# ACT 2 — Lumentum ROADM
# =========================================================================
banner "ACT 2: The Solution — Lumentum ROADM (192.0.2.17)"

section "Step 1: Setup (using cached YANG — ~2 seconds)"

info "The ROADM has pre-fetched YANG models in devices/roadm_17/yang/"
info "Setup will: migrate DB → build catalog from cached YANG → init polling jobs"
echo ""
cmd "splice --config devices/roadm_17/config.yaml setup"

eval "$BRIDGE --config $ROADM_CONFIG setup" 2>&1 | while IFS= read -r line; do
    echo -e "  ${DIM}${line}${RESET}"
done

info "Setup complete."
prompt

section "Step 2: Start the bridge"

start_bridge "roadm" "$ROADM_CONFIG" "$ROADM_PORT"
prompt

section "Step 3: Query via gnmic"

echo -e "  The Lumentum ROADM is now accessible over gNMI on port ${BOLD}${ROADM_PORT}${RESET}."
echo ""
echo -e "  ${BOLD}Try these commands:${RESET}"
echo ""
echo -e "    ${GREEN}gnmic -a localhost:${ROADM_PORT} --insecure capabilities${RESET}"
echo -e "    ${GREEN}gnmic -a localhost:${ROADM_PORT} --insecure get --path /${RESET}"
echo -e "    ${GREEN}gnmic -a localhost:${ROADM_PORT} --insecure subscribe --mode once --path '/'${RESET}"
echo ""
echo -e "  ${DIM}(type commands in another terminal, or press Enter when done)${RESET}"

prompt

# =========================================================================
# ACT 3 — Scale: Three Vendors
# =========================================================================
banner "ACT 3: Scale — Three Vendors, One Interface"

section "Teraflex (pre-setup, 330 YANG models, 13,281 paths)"

echo -e "  The Teraflex is a much larger device: ${BOLD}330 YANG models${RESET}, ${BOLD}13,281 telemetry paths${RESET}."
echo -e "  Setup was run beforehand. Starting the bridge directly..."
echo ""

start_bridge "teraflex" "$TERAFLEX_CONFIG" "$TERAFLEX_PORT"
prompt

section "Juniper ILA (30 YANG models)"

info "Running setup for Juniper ILA..."
echo ""
cmd "splice --config devices/juniper_ila/config.yaml setup"

eval "$BRIDGE --config $JUNIPER_CONFIG setup" 2>&1 | while IFS= read -r line; do
    echo -e "  ${DIM}${line}${RESET}"
done

info "Setup complete."
echo ""

start_bridge "juniper" "$JUNIPER_CONFIG" "$JUNIPER_PORT"
prompt

section "All three bridges running"

echo -e "  All three bridges are running simultaneously:"
echo ""

for entry in "roadm:Lumentum ROADM:${ROADM_PORT}" "teraflex:Adtran Teraflex:${TERAFLEX_PORT}" "juniper:Juniper ILA:${JUNIPER_PORT}"; do
    IFS=':' read -r name label port <<< "$entry"
    pid=$(cat "${PIDFILE_DIR}/${name}.pid" 2>/dev/null || echo "???")
    if kill -0 "$pid" 2>/dev/null; then
        echo -e "    ${GREEN}●${RESET}  ${BOLD}${label}${RESET}  →  localhost:${port}  ${DIM}(PID ${pid})${RESET}"
    else
        echo -e "    ${RED}●${RESET}  ${BOLD}${label}${RESET}  →  localhost:${port}  ${RED}(not running)${RESET}"
    fi
done

echo ""
echo -e "  ${BOLD}Same gnmic commands work against all three — only the port changes:${RESET}"
echo ""
echo -e "    ${GREEN}gnmic -a localhost:${ROADM_PORT} --insecure get --path /${RESET}    ${DIM}# Lumentum ROADM${RESET}"
echo -e "    ${GREEN}gnmic -a localhost:${TERAFLEX_PORT} --insecure get --path /${RESET}    ${DIM}# Adtran Teraflex${RESET}"
echo -e "    ${GREEN}gnmic -a localhost:${JUNIPER_PORT} --insecure get --path /${RESET}    ${DIM}# Juniper ILA${RESET}"
echo ""
echo -e "  ${DIM}Any gNMI client (gnmic, Telegraf, custom) can subscribe to all three.${RESET}"

prompt

# =========================================================================
# ACT 4 — Semantic Search
# =========================================================================
banner "ACT 4: Semantic Search — Finding Needles in 18,000+ Paths"

echo -e "  With 18,000+ telemetry paths across three vendors, finding the right"
echo -e "  path is like searching for a needle in a haystack."
echo ""
echo -e "  The bridge supports ${BOLD}semantic search${RESET} using OpenAI embeddings —"
echo -e "  search by meaning, not exact YANG path names."
echo ""

section "Example 1: Q-factor optical signal quality"

cmd "splice --config devices/teraflex/config.yaml find-paths --semantic --pattern \"Q-factor optical signal quality\" --top 5"
echo ""
eval "$BRIDGE --config $TERAFLEX_CONFIG find-paths --semantic --pattern 'Q-factor optical signal quality' --top 5" 2>&1 || true
echo ""

prompt

section "Example 2: FEC bit error rate"

cmd "splice --config devices/teraflex/config.yaml find-paths --semantic --pattern \"FEC bit error rate\" --top 5"
echo ""
eval "$BRIDGE --config $TERAFLEX_CONFIG find-paths --semantic --pattern 'FEC bit error rate' --top 5" 2>&1 || true
echo ""

echo -e "  ${BOLD}18,261 paths — found Q-factor in seconds using OpenAI embeddings.${RESET}"
echo -e "  ${DIM}No need to know the exact YANG module or path structure.${RESET}"

prompt

# =========================================================================
# ACT 5 — Live PM Data
# =========================================================================
banner "ACT 5: Live PM Data — Real Coherent Transport Telemetry"

echo -e "  The Teraflex is a coherent transponder connected to a real optical link:"
echo ""
echo -e "    ${CYAN}Teraflex → ROADM → 25km fiber → ILA → ROADM → Teraflex${RESET}"
echo ""
echo -e "  Let's pull live PM data — Q-factor, OSNR, FEC-BER — from the bridge."
echo ""

section "Subscribe to PM data"

info "Waiting for at least one PM polling cycle to populate data..."
sleep 10

cmd "gnmic -a localhost:${TERAFLEX_PORT} --insecure subscribe --mode once --path '/pm'"
echo ""
gnmic -a localhost:${TERAFLEX_PORT} --insecure subscribe --mode once --path '/pm' 2>&1 | head -50
echo -e "  ${DIM}... (truncated)${RESET}"
echo ""

echo -e "  ${BOLD}Real coherent transport PM from Teraflex:${RESET}"
echo -e "    Q-factor, OSNR, FEC-BER, optical power, frequency — all via gNMI."
echo -e "  ${DIM}Real photons, real fiber, real performance monitoring.${RESET}"

prompt

# =========================================================================
# ACT 6 — Four Interfaces
# =========================================================================
banner "ACT 6: Four Ways to Use the Bridge"

echo -e "  The bridge exposes ${BOLD}four interfaces${RESET}:"
echo ""

section "1. CLI (already demonstrated)"
echo -e "    ${GREEN}splice --config config.yaml setup${RESET}"
echo -e "    ${GREEN}splice --config config.yaml run${RESET}"
echo -e "    ${GREEN}splice --config config.yaml find-paths --pattern '...'${RESET}"
echo ""

section "2. Python API (no database required)"
echo -e "    ${DIM}>>> from splice.api.device import Device${RESET}"
echo -e "    ${DIM}>>> dev = Device('sim://openroadm')${RESET}"
echo -e "    ${DIM}>>> dev.connect()   # discovers + builds catalog automatically${RESET}"
echo -e "    ${DIM}>>> print(len(dev.catalog_paths), 'paths')${RESET}"
echo ""
cmd "python3 -c \"from splice.api.device import Device; d = Device('sim://openroadm'); d.connect(); print(f'{len(d.catalog_paths)} paths discovered')\""
${REPO_ROOT}/.venv/bin/python3 -c "from splice.api.device import Device; d = Device('sim://openroadm'); d.connect(); print(f'{len(d.catalog_paths)} paths discovered')" 2>&1 || true
echo ""

section "3. TUI — Interactive Console"
echo -e "    ${GREEN}splice-tui --devices-dir devices${RESET}"
echo -e "    ${DIM}Tabbed interface: Devices, Paths, Telemetry, Status, Operations, Jobs${RESET}"
echo ""

section "4. MCP Server — AI Agent Interface"
echo -e "    ${GREEN}splice mcp${RESET}"
echo -e "    ${DIM}Exposes connect_device, get_telemetry, find_paths, set_config, etc.${RESET}"
echo -e "    ${DIM}Works with Claude Code, any MCP-compatible AI agent.${RESET}"
echo ""

prompt

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
echo -e "  The bridges are still running. You can keep querying them."
echo ""
echo -e "  When you're done, clean up with:"
echo -e "    ${BOLD}./demo/teardown.sh${RESET}"
echo ""
echo -e "  Logs are at:"
echo -e "    demo/roadm.log"
echo -e "    demo/teraflex.log"
echo -e "    demo/juniper.log"
echo ""
