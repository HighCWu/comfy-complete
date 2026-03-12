#!/usr/bin/env bash
# check-node.sh - Main entry point for contributors adding custom nodes.
#
# Runs all checks in sequence: security scan, label suggestions, and entry validation.
#
# Usage:
#   ./scripts/add-node/check-node.sh <github-url-or-repo-dir> [--name pack-name]
#
# Examples:
#   ./scripts/add-node/check-node.sh https://github.com/org/ComfyUI-MyNode
#   ./scripts/add-node/check-node.sh https://github.com/org/ComfyUI-MyNode --name comfyui-mynode
#   ./scripts/add-node/check-node.sh ./path/to/local/repo --name comfyui-mynode
#
# Requirements: Python 3, git, pyyaml (pip install pyyaml)

set -euo pipefail

# ---------------------------------------------------------------------------
# Color helpers (disabled if not a terminal or NO_COLOR is set)
# ---------------------------------------------------------------------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    BOLD=$'\033[1m'
    RED=$'\033[0;31m'
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[0;33m'
    CYAN=$'\033[0;36m'
    RESET=$'\033[0m'
else
    BOLD='' RED='' GREEN='' YELLOW='' CYAN='' RESET=''
fi

# Use ASCII fallbacks that work everywhere; terminals that support UTF-8 will
# show the nice glyphs, others get readable ASCII.
PASS_MARK="${GREEN}OK${RESET}"
FAIL_MARK="${RED}FAIL${RESET}"
WARN_MARK="${YELLOW}WARN${RESET}"

# Try to use Unicode symbols if the locale supports it
if printf '\xe2\x9c\x93' >/dev/null 2>&1; then
    _check=$(printf '\xe2\x9c\x93')   # checkmark
    _cross=$(printf '\xe2\x9c\x97')   # cross
    _warn=$(printf '\xe2\x9a\xa0')    # warning
    PASS_MARK="${GREEN}${_check}${RESET}"
    FAIL_MARK="${RED}${_cross}${RESET}"
    WARN_MARK="${YELLOW}${_warn}${RESET}"
fi

# ---------------------------------------------------------------------------
# Portable printf wrapper (echo -e is unreliable across platforms)
# ---------------------------------------------------------------------------
say() {
    printf '%b\n' "$*"
}

# ---------------------------------------------------------------------------
# Resolve script directory (works on macOS, Linux, WSL)
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
SECURITY_SCAN="$REPO_ROOT/scripts/pr-review/security-scan.sh"
SUGGEST_LABELS="$SCRIPT_DIR/suggest-labels.py"
VALIDATE_ENTRY="$SCRIPT_DIR/validate-entry.py"
SUPPORTED_NODES="$REPO_ROOT/supported_nodes.yaml"

# ---------------------------------------------------------------------------
# Usage / argument parsing
# ---------------------------------------------------------------------------
usage() {
    echo "Usage: $0 <github-url-or-repo-dir> [--name pack-name]"
    echo ""
    echo "Arguments:"
    echo "  <github-url-or-repo-dir>  GitHub clone URL or path to a local repo directory"
    echo "  --name <pack-name>        Node pack name in supported_nodes.yaml to validate"
    echo ""
    echo "Examples:"
    echo "  $0 https://github.com/org/ComfyUI-MyNode"
    echo "  $0 ./local-repo --name comfyui-mynode"
    exit 1
}

if [ $# -lt 1 ]; then
    usage
fi

TARGET="$1"
shift
PACK_NAME=""

while [ $# -gt 0 ]; do
    case "$1" in
        --name)
            if [ $# -lt 2 ]; then
                echo "Error: --name requires a value" >&2
                exit 1
            fi
            PACK_NAME="$2"
            shift 2
            ;;
        --help|-h)
            usage
            ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            usage
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------
PYTHON=""
for py in python3 python; do
    if command -v "$py" >/dev/null 2>&1; then
        PYTHON="$py"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "Error: Python 3 is required but not found in PATH." >&2
    exit 1
fi

if ! "$PYTHON" -c "import yaml" 2>/dev/null; then
    echo "Error: pyyaml is required. Install with: pip install pyyaml" >&2
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo "Error: git is required but not found in PATH." >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Resolve the repo directory
# ---------------------------------------------------------------------------
CLEANUP_DIR=""
cleanup() {
    if [ -n "$CLEANUP_DIR" ] && [ -d "$CLEANUP_DIR" ]; then
        rm -rf "$CLEANUP_DIR"
    fi
}
trap cleanup EXIT

if [[ "$TARGET" == https://github.com/* ]] || [[ "$TARGET" == git@github.com:* ]]; then
    TMPDIR_BASE="${TMPDIR:-/tmp}"
    CLEANUP_DIR="$(mktemp -d "${TMPDIR_BASE}/comfy-check-node-XXXXXX")"
    say "${CYAN}Cloning ${TARGET} ...${RESET}"
    if ! git clone --depth 1 --quiet "$TARGET" "$CLEANUP_DIR/repo" 2>&1; then
        say "${RED}Error: Failed to clone $TARGET${RESET}" >&2
        exit 1
    fi
    REPO_DIR="$CLEANUP_DIR/repo"
    echo ""
else
    if [ ! -d "$TARGET" ]; then
        echo "Error: '$TARGET' is not a valid directory or GitHub URL." >&2
        exit 1
    fi
    REPO_DIR="$(cd "$TARGET" && pwd)"
fi

# ---------------------------------------------------------------------------
# Determine how many steps we will run
# ---------------------------------------------------------------------------
HAS_SUGGEST_LABELS=false
HAS_VALIDATE=false
HAS_LICENSE_CHECK=false
CHECK_LICENSE="$SCRIPT_DIR/check-license.py"

if [ -f "$SUGGEST_LABELS" ]; then
    HAS_SUGGEST_LABELS=true
fi

if [ -n "$PACK_NAME" ]; then
    HAS_VALIDATE=true
fi

if [ -f "$CHECK_LICENSE" ]; then
    HAS_LICENSE_CHECK=true
fi

TOTAL_STEPS=1
if [ "$HAS_LICENSE_CHECK" = true ]; then
    TOTAL_STEPS=$((TOTAL_STEPS + 1))
fi
if [ "$HAS_SUGGEST_LABELS" = true ]; then
    TOTAL_STEPS=$((TOTAL_STEPS + 1))
fi
if [ "$HAS_VALIDATE" = true ]; then
    TOTAL_STEPS=$((TOTAL_STEPS + 1))
fi

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
say "${BOLD}"
echo "================================================"
echo "  Comfy-Complete Node Check"
echo "================================================"
say "${RESET}"
say "  Repository: ${CYAN}${REPO_DIR}${RESET}"
echo ""

STEP=0
SUMMARY_BLOCKERS=0
SUMMARY_WARNINGS=0
SUMMARY_ERRORS=0

# ---------------------------------------------------------------------------
# Helper: extract a number from "N blockers" or "N warnings" in scan output
# Uses portable sed instead of grep -P.
# ---------------------------------------------------------------------------
extract_count() {
    local pattern="$1"
    local text="$2"
    # Match lines like "Summary: 3 blockers, 5 warnings"
    echo "$text" | sed -n "s/.*\([0-9][0-9]*\) ${pattern}.*/\1/p" | tail -1
}

# ---------------------------------------------------------------------------
# Step: Security Scan
# ---------------------------------------------------------------------------
STEP=$((STEP + 1))
say "${BOLD}[${STEP}/${TOTAL_STEPS}] Security Scan${RESET}"

if [ ! -f "$SECURITY_SCAN" ]; then
    say "  ${WARN_MARK} security-scan.sh not found at $SECURITY_SCAN (skipped)"
    SUMMARY_WARNINGS=$((SUMMARY_WARNINGS + 1))
else
    SCAN_OUTPUT=""
    SCAN_EXIT=0
    SCAN_OUTPUT=$(bash "$SECURITY_SCAN" "$REPO_DIR" 2>&1) || SCAN_EXIT=$?

    # Check if the scan itself failed to run (missing deps, etc.)
    if echo "$SCAN_OUTPUT" | grep -q "^Error:"; then
        say "  ${WARN_MARK} Security scan could not run:"
        echo "$SCAN_OUTPUT" | grep "^Error:" | while IFS= read -r line; do
            echo "    $line"
        done
        SUMMARY_WARNINGS=$((SUMMARY_WARNINGS + 1))
    else
        # Parse the summary line from security-scan.sh output
        SCAN_BLOCKERS=$(extract_count "blocker" "$SCAN_OUTPUT")
        SCAN_WARNINGS=$(extract_count "warning" "$SCAN_OUTPUT")
        SCAN_BLOCKERS="${SCAN_BLOCKERS:-0}"
        SCAN_WARNINGS="${SCAN_WARNINGS:-0}"

        if [ "$SCAN_EXIT" -ne 0 ]; then
            say "  ${FAIL_MARK} ${RED}${SCAN_BLOCKERS} blocker(s) found${RESET}"
            SUMMARY_BLOCKERS=$((SUMMARY_BLOCKERS + SCAN_BLOCKERS))
        else
            say "  ${PASS_MARK} No blockers found"
        fi

        if [ "$SCAN_WARNINGS" -gt 0 ] 2>/dev/null; then
            say "  ${WARN_MARK} ${SCAN_WARNINGS} warning(s)"
            SUMMARY_WARNINGS=$((SUMMARY_WARNINGS + SCAN_WARNINGS))
        fi

        # Show blocker details if any
        if [ "$SCAN_EXIT" -ne 0 ]; then
            echo ""
            echo "$SCAN_OUTPUT" | grep -E '^BLOCKER:' | while IFS= read -r line; do
                say "    ${RED}${line}${RESET}"
            done
        fi

        # Show warning details
        if [ "$SCAN_WARNINGS" -gt 0 ] 2>/dev/null; then
            echo "$SCAN_OUTPUT" | grep -E '^WARNING:' | while IFS= read -r line; do
                say "    ${YELLOW}${line}${RESET}"
            done
        fi
    fi
fi
echo ""

# ---------------------------------------------------------------------------
# Step: License Check (if check-license.py exists)
# ---------------------------------------------------------------------------
if [ "$HAS_LICENSE_CHECK" = true ]; then
    STEP=$((STEP + 1))
    say "${BOLD}[${STEP}/${TOTAL_STEPS}] License Check${RESET}"

    LICENSE_OUTPUT=""
    LICENSE_EXIT=0
    LICENSE_OUTPUT=$("$PYTHON" "$CHECK_LICENSE" "$REPO_DIR" 2>&1) || LICENSE_EXIT=$?

    if [ "$LICENSE_EXIT" -ne 0 ]; then
        # Parse blocker/warning counts from output
        LIC_BLOCKERS=$(extract_count "blocked" "$LICENSE_OUTPUT")
        LIC_BLOCKERS="${LIC_BLOCKERS:-0}"
        LIC_UNKNOWN=$(extract_count "unknown" "$LICENSE_OUTPUT")
        LIC_UNKNOWN="${LIC_UNKNOWN:-0}"

        say "  ${FAIL_MARK} License blockers found"
        SUMMARY_BLOCKERS=$((SUMMARY_BLOCKERS + LIC_BLOCKERS))
        if [ "$LIC_UNKNOWN" -gt 0 ] 2>/dev/null; then
            say "  ${WARN_MARK} ${LIC_UNKNOWN} unknown license(s)"
            SUMMARY_WARNINGS=$((SUMMARY_WARNINGS + LIC_UNKNOWN))
        fi
    else
        # Extract license name and dep count from output
        NODE_LICENSE=$(echo "$LICENSE_OUTPUT" | sed -n 's/.*Node license: \(.*\)/\1/p' | head -1)
        DEPS_CHECKED=$(echo "$LICENSE_OUTPUT" | sed -n 's/.*Dependencies: \([0-9]*\) checked.*/\1/p' | head -1)
        DEPS_BLOCKED=$(echo "$LICENSE_OUTPUT" | sed -n 's/.*\([0-9][0-9]*\) blocked.*/\1/p' | head -1)
        LIC_UNKNOWN=$(echo "$LICENSE_OUTPUT" | sed -n 's/.*\([0-9][0-9]*\) unknown.*/\1/p' | head -1)
        NODE_LICENSE="${NODE_LICENSE:-unknown}"
        DEPS_CHECKED="${DEPS_CHECKED:-0}"
        DEPS_BLOCKED="${DEPS_BLOCKED:-0}"
        LIC_UNKNOWN="${LIC_UNKNOWN:-0}"

        say "  ${PASS_MARK} Node license: ${NODE_LICENSE}"
        say "  ${PASS_MARK} Dependencies: ${DEPS_CHECKED} checked, ${DEPS_BLOCKED} blocked"
        if [ "$LIC_UNKNOWN" -gt 0 ] 2>/dev/null; then
            say "  ${WARN_MARK} ${LIC_UNKNOWN} unknown license(s)"
            SUMMARY_WARNINGS=$((SUMMARY_WARNINGS + LIC_UNKNOWN))
        fi
    fi

    # Show details from output
    if [ -n "$LICENSE_OUTPUT" ]; then
        echo "$LICENSE_OUTPUT" | grep -E '^BLOCKER:' | while IFS= read -r line; do
            say "    ${RED}${line}${RESET}"
        done
        echo "$LICENSE_OUTPUT" | grep -E '^WARNING:' | while IFS= read -r line; do
            say "    ${YELLOW}${line}${RESET}"
        done
    fi
    echo ""
fi

# ---------------------------------------------------------------------------
# Step: Label Suggestions (if suggest-labels.py exists)
# ---------------------------------------------------------------------------
if [ "$HAS_SUGGEST_LABELS" = true ]; then
    STEP=$((STEP + 1))
    say "${BOLD}[${STEP}/${TOTAL_STEPS}] Label Suggestions${RESET}"

    LABEL_OUTPUT=""
    LABEL_EXIT=0
    LABEL_OUTPUT=$("$PYTHON" "$SUGGEST_LABELS" "$REPO_DIR" 2>&1) || LABEL_EXIT=$?

    if [ "$LABEL_EXIT" -ne 0 ]; then
        say "  ${WARN_MARK} suggest-labels.py exited with code $LABEL_EXIT"
        SUMMARY_WARNINGS=$((SUMMARY_WARNINGS + 1))
    fi

    if [ -n "$LABEL_OUTPUT" ]; then
        echo "$LABEL_OUTPUT" | while IFS= read -r line; do
            echo "  $line"
        done
    else
        say "  ${PASS_MARK} No label suggestions"
    fi
    echo ""
fi

# ---------------------------------------------------------------------------
# Step: Entry Validation (if --name given)
# ---------------------------------------------------------------------------
if [ "$HAS_VALIDATE" = true ]; then
    STEP=$((STEP + 1))
    say "${BOLD}[${STEP}/${TOTAL_STEPS}] Entry Validation (--name ${PACK_NAME})${RESET}"

    if [ ! -f "$SUPPORTED_NODES" ]; then
        say "  ${FAIL_MARK} supported_nodes.yaml not found at $SUPPORTED_NODES"
        SUMMARY_ERRORS=$((SUMMARY_ERRORS + 1))
    elif [ ! -f "$VALIDATE_ENTRY" ]; then
        say "  ${FAIL_MARK} validate-entry.py not found at $VALIDATE_ENTRY"
        SUMMARY_ERRORS=$((SUMMARY_ERRORS + 1))
    else
        VALIDATE_OUTPUT=""
        VALIDATE_EXIT=0
        VALIDATE_OUTPUT=$("$PYTHON" "$VALIDATE_ENTRY" --yaml "$SUPPORTED_NODES" --name "$PACK_NAME" --json 2>&1) || VALIDATE_EXIT=$?

        if [ "$VALIDATE_EXIT" -eq 0 ]; then
            V_WARNINGS=$(echo "$VALIDATE_OUTPUT" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('warnings',[])))" 2>/dev/null || echo "0")
            say "  ${PASS_MARK} Entry is valid"
            if [ "$V_WARNINGS" -gt 0 ] 2>/dev/null; then
                say "  ${WARN_MARK} ${V_WARNINGS} warning(s):"
                echo "$VALIDATE_OUTPUT" | "$PYTHON" -c "
import sys, json
d = json.load(sys.stdin)
for w in d.get('warnings', []):
    print('    ! ' + w)
" 2>/dev/null
                SUMMARY_WARNINGS=$((SUMMARY_WARNINGS + V_WARNINGS))
            fi
        else
            V_ERRORS=$(echo "$VALIDATE_OUTPUT" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('errors',[])))" 2>/dev/null || echo "1")
            V_WARNINGS=$(echo "$VALIDATE_OUTPUT" | "$PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('warnings',[])))" 2>/dev/null || echo "0")
            say "  ${FAIL_MARK} Validation failed"

            # Print errors and warnings
            echo "$VALIDATE_OUTPUT" | "$PYTHON" -c "
import sys, json
d = json.load(sys.stdin)
for e in d.get('errors', []):
    print('    x ' + e)
for w in d.get('warnings', []):
    print('    ! ' + w)
" 2>/dev/null

            # Update summary counts
            SUMMARY_ERRORS=$((SUMMARY_ERRORS + V_ERRORS))
            if [ "$V_WARNINGS" -gt 0 ] 2>/dev/null; then
                SUMMARY_WARNINGS=$((SUMMARY_WARNINGS + V_WARNINGS))
            fi
        fi
    fi
    echo ""
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
say "${BOLD}Summary:${RESET} ${SUMMARY_BLOCKERS} blocker(s), ${SUMMARY_ERRORS} error(s), ${SUMMARY_WARNINGS} warning(s)"

if [ "$SUMMARY_BLOCKERS" -gt 0 ] || [ "$SUMMARY_ERRORS" -gt 0 ]; then
    say "${RED}Some checks failed. Please fix the issues above before submitting your PR.${RESET}"
    exit 1
else
    if [ "$SUMMARY_WARNINGS" -gt 0 ]; then
        say "${YELLOW}All checks passed with warnings. Review the warnings above.${RESET}"
    else
        say "${GREEN}All checks passed!${RESET}"
    fi
    exit 0
fi
