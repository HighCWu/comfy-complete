#!/bin/bash
# Security Scan for Custom Nodes
# Ported from cloud-custom-node-automation
#
# Usage: ./security-scan.sh <repo_dir>
#
# Outputs findings that Claude interprets for PR review.
# Exit code: 0 if no blockers, 1 if blockers found

set -e

REPO_DIR="${1:-.}"

# Find ripgrep - try common locations
RG=""
for path in \
    "$(command -v rg 2>/dev/null)" \
    "/usr/bin/rg" \
    "/usr/local/bin/rg" \
    "/opt/homebrew/bin/rg" \
    "$HOME/.nvm/versions/node/"*/lib/node_modules/@anthropic-ai/claude-code/vendor/ripgrep/*/rg \
    ; do
    if [ -x "$path" ] 2>/dev/null; then
        RG="$path"
        break
    fi
done

if [ -z "$RG" ]; then
    echo "Error: ripgrep (rg) not found. Install with: brew install ripgrep (macOS) or apt install ripgrep (Ubuntu)"
    exit 1
fi

if [ ! -d "$REPO_DIR" ]; then
    echo "Usage: ./security-scan.sh <repo_dir>"
    exit 1
fi

cd "$REPO_DIR"

BLOCKER_COUNT=0
WARNING_COUNT=0

echo "Security Scan Results"
echo "====================="
echo "Repository: $REPO_DIR"
echo ""

# ============================================================================
# BLOCKERS - must reject or get explicit approval
# ============================================================================

echo "## BLOCKERS"
echo ""

# eval() - exclude method calls like model.eval(), self.eval()
COUNT=$($RG -c '(?<!\.)eval\(' --type py -P 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "BLOCKER: eval() - $COUNT occurrences (arbitrary code execution)"
    $RG -n '(?<!\.)eval\(' --type py -P 2>/dev/null | head -5
    echo ""
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# exec() - the builtin, not subprocess_exec or create_subprocess_exec
COUNT=$($RG -c '(?<![_a-zA-Z])exec\s*\(' --type py -P 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "BLOCKER: exec() - $COUNT occurrences (arbitrary code execution)"
    $RG -n '(?<![_a-zA-Z])exec\s*\(' --type py -P 2>/dev/null | head -5
    echo ""
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# os.system()
COUNT=$($RG -c 'os\.system\(' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "BLOCKER: os.system() - $COUNT occurrences (command execution)"
    $RG -n 'os\.system\(' --type py 2>/dev/null | head -5
    echo ""
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# subprocess with shell=True
COUNT=$($RG -c 'shell\s*=\s*True' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "BLOCKER: shell=True - $COUNT occurrences (shell injection risk)"
    $RG -n 'shell\s*=\s*True' --type py 2>/dev/null | head -5
    echo ""
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# torch.load without weights_only
TOTAL=$($RG -c 'torch\.load\(' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
SAFE=$($RG -c 'torch\.load.*weights_only\s*=\s*True' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
UNSAFE=$((TOTAL - SAFE))
if [ "$UNSAFE" -gt 0 ]; then
    echo "BLOCKER: unsafe torch.load() - $UNSAFE occurrences (pickle deserialization vulnerability)"
    $RG -n 'torch\.load\(' --type py 2>/dev/null | grep -v 'weights_only' | head -5
    echo ""
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

# Pickle serialization
COUNT=$($RG -c 'pickle\.(dumps|loads)|cloudpickle\.|dill\.' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "BLOCKER: pickle serialization - $COUNT occurrences (arbitrary code execution via unpickle)"
    $RG -n 'pickle\.(dumps|loads)|cloudpickle\.|dill\.' --type py 2>/dev/null | head -5
    echo ""
    BLOCKER_COUNT=$((BLOCKER_COUNT + 1))
fi

if [ "$BLOCKER_COUNT" -eq 0 ]; then
    echo "None found"
    echo ""
fi

# ============================================================================
# WARNINGS - note but can proceed
# ============================================================================

echo "## WARNINGS"
echo ""

# Dynamic imports
COUNT=$($RG -c '__import__|importlib\.(import_module|reload)' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: dynamic imports - $COUNT occurrences (may load arbitrary modules)"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# Network requests
COUNT=$($RG -c 'requests\.(get|post|put|delete)|urllib\.(request|urlopen)|httpx\.' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: network requests - $COUNT occurrences (makes external HTTP requests)"
    echo "  NOTE: If these are model downloads, consider RuntimeModelDownload label. If general API calls, consider NetworkAccess label."
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# File writes
COUNT=$($RG -c "open\([^)]*['\"][wa]" --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: file writes - $COUNT occurrences (writes to filesystem)"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# Model/tensor file saves
COUNT=$($RG -c 'safetensors\.torch\.save|torch\.save\(|\.save_pretrained\(|imageio\.imwrite|cv2\.imwrite|sf\.write' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: model/media saves - $COUNT occurrences (saves models or media to disk - consider WritesToDisk label)"
    $RG -n 'safetensors\.torch\.save|torch\.save\(|\.save_pretrained\(' --type py 2>/dev/null | head -5
    echo ""
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# System information exposure
COUNT=$($RG -c 'psutil\.|platform\.(uname|system|node|processor)|os\.uname|socket\.gethostname|getpass\.getuser' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: system info exposure - $COUNT occurrences (exposes system information)"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# GPU/Hardware monitoring
COUNT=$($RG -c 'nvidia-smi|torch\.cuda\.get_device_properties|pynvml|GPUtil' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: hardware monitoring - $COUNT occurrences (accesses GPU/hardware info)"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# Arbitrary file path inputs (potential for ReadsArbitraryFile label)
COUNT=$($RG -c -i 'STRING.*default.*[./~]|path.*STRING|file.*STRING|directory.*STRING|folder.*STRING' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: file path inputs - $COUNT occurrences (has file/path string inputs - consider ReadsArbitraryFile label)"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

if [ "$WARNING_COUNT" -eq 0 ]; then
    echo "None found"
fi

echo ""

# ============================================================================
# LABEL DETECTION CHECKS
# ============================================================================

echo "## LABEL DETECTION"
echo ""

# Runtime model downloads (RuntimeModelDownload label)
COUNT=$($RG -c 'hf_hub_download|snapshot_download|download_url_to_file|urlretrieve.*model|from_pretrained' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: runtime model downloads - $COUNT occurrences (consider RuntimeModelDownload label)"
    $RG -n 'hf_hub_download|snapshot_download|download_url_to_file' --type py 2>/dev/null | head -5
    echo ""
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# Runtime pip install (RuntimePipInstall label)
COUNT=$($RG -c 'pip.*install|ensure_package|install_if_missing' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: runtime pip install - $COUNT occurrences (consider RuntimePipInstall label)"
    $RG -n 'pip.*install|ensure_package' --type py 2>/dev/null | head -5
    echo ""
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# Hardcoded CUDA (RequiresGPU label)
COUNT=$($RG -c "\.to\(['\"]cuda['\"]|\.cuda\(\)|torch\.device\(['\"]cuda" --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: hardcoded CUDA - $COUNT occurrences (check for CPU fallback; consider RequiresGPU label)"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# Webcam access (RequiresWebcam label)
COUNT=$($RG -c 'VideoCapture\(\s*[0-9]|VideoCapture\(\s*camera|VideoCapture\(\s*device' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: webcam access - $COUNT occurrences (consider RequiresWebcam label)"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# Clipboard access (RequiresClipboard label)
COUNT=$($RG -c 'grabclipboard|pyperclip|clipboard' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: clipboard access - $COUNT occurrences (consider RequiresClipboard label)"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# Interactive display (RequiresDisplay label)
COUNT=$($RG -c 'cv2\.imshow|cv2\.waitKey|plt\.show|send_sync.*prompt_user' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: interactive display - $COUNT occurrences (consider RequiresDisplay label)"
    WARNING_COUNT=$((WARNING_COUNT + 1))
fi

# ============================================================================
# DEPLOYMENT COMPATIBILITY CHECKS
# ============================================================================

echo "## DEPLOYMENT COMPATIBILITY"
echo ""

CLOUD_WARNING_COUNT=0

# Check for web/js directories (custom UI)
if [ -d "web" ] || [ -d "js" ]; then
    WEB_DIR=""
    [ -d "web" ] && WEB_DIR="web/"
    [ -d "js" ] && WEB_DIR="js/"
    echo "INFO: Custom UI directory found - $WEB_DIR (may modify the default interface)"
    CLOUD_WARNING_COUNT=$((CLOUD_WARNING_COUNT + 1))
fi

# Custom server endpoints (@routes decorator)
COUNT=$($RG -c '@.*routes' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: custom endpoints - $COUNT occurrences (registers custom HTTP routes)"
    $RG -n '@.*routes' --type py 2>/dev/null | head -5
    echo ""
    CLOUD_WARNING_COUNT=$((CLOUD_WARNING_COUNT + 1))
fi

# Stateful patterns - global/class-level state
COUNT=$($RG -c '^\s*[A-Z_]+\s*=\s*(\{|\[|dict\(|list\()|^[a-z_]+\s*=\s*(\{|\[)(?!.*def )' --type py -P 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "INFO: module-level state - $COUNT potential occurrences (may persist between runs)"
    CLOUD_WARNING_COUNT=$((CLOUD_WARNING_COUNT + 1))
fi

# In-memory caches (common patterns)
COUNT=$($RG -c '@lru_cache|@cache|@functools\.cache|_cache\s*=|cache\s*=\s*\{|_CACHE|CACHE\s*=' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: caching patterns - $COUNT occurrences (in-memory state between runs)"
    $RG -n '@lru_cache|@cache|_cache\s*=|CACHE' --type py 2>/dev/null | head -5
    echo ""
    CLOUD_WARNING_COUNT=$((CLOUD_WARNING_COUNT + 1))
fi

# External API keys
COUNT=$($RG -c 'OPENAI_API_KEY|ANTHROPIC_API_KEY|openai\.api_key|anthropic\.' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: external API usage - $COUNT occurrences (requires user API keys)"
    $RG -n 'OPENAI_API_KEY|ANTHROPIC_API_KEY' --type py 2>/dev/null | head -5
    echo ""
    CLOUD_WARNING_COUNT=$((CLOUD_WARNING_COUNT + 1))
fi

# System package dependencies (not available in base image)
COUNT=$($RG -c 'subprocess.*\b(espeak|tesseract|imagemagick|convert)\b|os\.system.*\b(espeak|tesseract)' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "WARNING: system packages - $COUNT occurrences (may require packages not in base image)"
    $RG -n 'espeak|tesseract|imagemagick' --type py 2>/dev/null | head -5
    echo ""
    CLOUD_WARNING_COUNT=$((CLOUD_WARNING_COUNT + 1))
fi

# Singleton patterns
COUNT=$($RG -c '_instance\s*=|__instance|@singleton|Singleton' --type py 2>/dev/null | awk -F: '{sum += $2} END {print sum+0}')
if [ "$COUNT" -gt 0 ]; then
    echo "INFO: singleton patterns - $COUNT occurrences (stateful across runs)"
    CLOUD_WARNING_COUNT=$((CLOUD_WARNING_COUNT + 1))
fi

if [ "$CLOUD_WARNING_COUNT" -eq 0 ]; then
    echo "No deployment compatibility issues found"
fi

echo ""
echo "====================="
echo "Summary: $BLOCKER_COUNT blockers, $WARNING_COUNT warnings"

# Exit with error if blockers found
if [ "$BLOCKER_COUNT" -gt 0 ]; then
    exit 1
else
    exit 0
fi
