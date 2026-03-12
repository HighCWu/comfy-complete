#!/bin/bash
set -e

# Comfy Complete - Install to Directory
# This script installs ComfyUI with Comfy Complete environment to a target directory.
# Uses uv by default for fast package installation.
#
# Usage: ./install-to-dir.sh <target-directory> [options]
#
# Options:
#   --no-deps    Skip installing Python dependencies (for custom setups)
#   --no-uv      Use pip instead of uv for package installation
#   --help       Show this help message

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# UV environment variables for reliability
export UV_HTTP_TIMEOUT=300
export UV_HTTP_RETRIES=5
export UV_LINK_MODE=copy

# Parse arguments
TARGET_DIR=""
NO_DEPS=false
USE_UV=true

show_help() {
    echo "Comfy Complete - Install to Directory"
    echo ""
    echo "Usage: $0 <target-directory> [options]"
    echo ""
    echo "Arguments:"
    echo "  target-directory   Directory to install ComfyUI into"
    echo ""
    echo "Options:"
    echo "  --no-deps          Skip installing Python dependencies"
    echo "  --no-uv            Use pip instead of uv (uv is used by default)"
    echo "  --help             Show this help message"
    echo ""
    echo "Example:"
    echo "  $0 ~/ComfyUI"
    echo "  $0 /opt/comfyui --no-uv"
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --no-deps)
            NO_DEPS=true
            shift
            ;;
        --no-uv)
            USE_UV=false
            shift
            ;;
        --help|-h)
            show_help
            exit 0
            ;;
        -*)
            echo "Unknown option: $1"
            show_help
            exit 1
            ;;
        *)
            if [[ -z "$TARGET_DIR" ]]; then
                TARGET_DIR="$1"
            else
                echo "Error: Multiple target directories specified"
                show_help
                exit 1
            fi
            shift
            ;;
    esac
done

if [[ -z "$TARGET_DIR" ]]; then
    echo "Error: Target directory is required"
    echo ""
    show_help
    exit 1
fi

# Convert to absolute path
TARGET_DIR="$(cd "$(dirname "${TARGET_DIR}")" 2>/dev/null && pwd)/$(basename "${TARGET_DIR}")" || TARGET_DIR="$(pwd)/${TARGET_DIR}"

echo "=== Comfy Complete - Installation ==="
echo "Repository root: ${REPO_ROOT}"
echo "Target directory: ${TARGET_DIR}"
echo ""

# Check for required tools
echo "Checking dependencies..."

if ! command -v git &> /dev/null; then
    echo "Error: git is required but not installed"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is required but not installed"
    exit 1
fi

# Set up virtual environment path
VENV_PATH="${TARGET_DIR}/.venv"

# Install uv if needed (venv creation happens after git clone)
if [[ "$USE_UV" == true ]]; then
    if ! command -v uv &> /dev/null; then
        echo "uv not found. Installing..."
        pip install uv
    fi
fi

# Check for pyyaml (use system python for initial parsing)
if ! python3 -c "import yaml" &> /dev/null; then
    echo "PyYAML not found in system Python. Installing..."
    pip install pyyaml
fi

# Read ComfyUI version from version_lock.yaml
COMFYUI_VERSION=$(python3 -c "import yaml; print(yaml.safe_load(open('${REPO_ROOT}/version_lock.yaml'))['pinned']['comfyui']['version'])")
COMFYUI_SOURCE=$(python3 -c "import yaml; print(yaml.safe_load(open('${REPO_ROOT}/version_lock.yaml'))['pinned']['comfyui']['source'])")

echo "ComfyUI version: ${COMFYUI_VERSION}"
echo ""

# Create target directory if it doesn't exist
if [[ ! -d "$TARGET_DIR" ]]; then
    echo "Creating target directory..."
    mkdir -p "$TARGET_DIR"
fi

# Clone or update ComfyUI (using shallow clone for speed)
COMFY_PATH="${TARGET_DIR}"

if [[ -d "${COMFY_PATH}/.git" ]]; then
    echo "ComfyUI already cloned. Fetching version ${COMFYUI_VERSION}..."
    cd "$COMFY_PATH"
    git fetch --depth 1 origin "$COMFYUI_VERSION"
    echo "Checking out version ${COMFYUI_VERSION}..."
    git checkout FETCH_HEAD
else
    echo "Cloning ComfyUI (shallow clone)..."
    git clone --depth 1 "$COMFYUI_SOURCE" "$COMFY_PATH"
    cd "$COMFY_PATH"
    echo "Fetching version ${COMFYUI_VERSION}..."
    git fetch --depth 1 origin "$COMFYUI_VERSION"
    echo "Checking out version ${COMFYUI_VERSION}..."
    git checkout FETCH_HEAD
fi

# Create virtual environment after cloning (so target dir exists and has ComfyUI)
if [[ "$USE_UV" == true ]]; then
    if [[ ! -d "$VENV_PATH" ]]; then
        echo ""
        echo "Creating virtual environment at ${VENV_PATH}..."
        uv venv "$VENV_PATH"
    fi

    # Set UV_PYTHON to use the venv
    export UV_PYTHON="${VENV_PATH}/bin/python"
    PIP_CMD="uv pip"
    PYTHON_CMD="${VENV_PATH}/bin/python"
else
    PIP_CMD="pip"
    PYTHON_CMD="python3"
fi

# Install Python dependencies
if [[ "$NO_DEPS" == false ]]; then
    echo ""
    echo "Installing comfy-cli (with dependencies)..."
    # Install comfy-cli first WITH dependencies (matches Dockerfile behavior)
    $PIP_CMD install comfy-cli

    echo ""
    echo "Installing Python dependencies..."
    # Use --no-deps to avoid dependency conflicts (requirements.txt has pre-resolved versions)
    $PIP_CMD install --no-deps -r "${REPO_ROOT}/requirements.txt"

    # Install frontend and workflow templates from version_lock.yaml
    echo ""
    echo "Installing frontend and workflow templates..."
    FRONTEND_VERSION=$(python3 -c "import yaml; print(yaml.safe_load(open('${REPO_ROOT}/version_lock.yaml'))['pinned']['comfyui_frontend_package']['version'])")
    TEMPLATES_VERSION=$(python3 -c "import yaml; print(yaml.safe_load(open('${REPO_ROOT}/version_lock.yaml'))['pinned']['comfyui_workflow_templates']['version'])")

    echo "  Frontend package: ${FRONTEND_VERSION}"
    echo "  Workflow templates: ${TEMPLATES_VERSION}"
    $PIP_CMD install --no-deps "comfyui_frontend_package==${FRONTEND_VERSION}" "comfyui_workflow_templates==${TEMPLATES_VERSION}"
else
    echo ""
    echo "Skipping Python dependencies (--no-deps specified)"
fi

# Set comfy-cli path (already installed in deps block, but handle --no-deps case)
if [[ "$USE_UV" == true ]]; then
    if [[ ! -f "${VENV_PATH}/bin/comfy" ]]; then
        echo ""
        echo "Installing comfy-cli to venv..."
        $PIP_CMD install comfy-cli
    fi
    COMFY_CLI="${VENV_PATH}/bin/comfy"
else
    if ! command -v comfy &> /dev/null; then
        echo ""
        echo "Installing comfy-cli..."
        $PIP_CMD install comfy-cli
    fi
    COMFY_CLI="comfy"
fi

# Install PyTorch (required for ComfyUI and cm-cli.py)
# Must be installed before custom nodes since ComfyUI-Manager's cm-cli.py requires torchvision
echo ""
echo "Installing PyTorch..."
if command -v nvcc &> /dev/null; then
    CUDA_VERSION=$(nvcc --version | grep "release" | sed -n 's/.*release \([0-9]*\.[0-9]*\).*/\1/p')
    echo "CUDA ${CUDA_VERSION} detected"
    case "$CUDA_VERSION" in
        12.*)
            $PIP_CMD install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
            ;;
        11.8*)
            $PIP_CMD install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
            ;;
        *)
            echo "CUDA $CUDA_VERSION detected but no matching PyTorch build. Using default..."
            $PIP_CMD install torch torchvision torchaudio
            ;;
    esac
else
    echo "No CUDA detected. Installing CPU-only PyTorch..."
    $PIP_CMD install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
fi

# Create necessary directories
echo ""
echo "Creating directories..."
mkdir -p models/checkpoints \
    models/clip \
    models/controlnet \
    models/diffusers \
    models/embeddings \
    models/loras \
    models/upscale_models \
    models/vae \
    models/sams \
    models/insightface \
    models/detection \
    output \
    temp \
    custom_nodes

# Attempt SAM2 build if CUDA toolkit is available
SAM2_COMMIT="2b90b9f5ceec907a1c18123530e92e794ad901a4"

if command -v nvcc &> /dev/null; then
    echo ""
    echo "CUDA toolkit found. Attempting SAM2 build..."
    # Install setuptools if needed for building
    $PIP_CMD install setuptools wheel
    $PIP_CMD install --no-build-isolation --no-deps \
        "SAM-2 @ https://github.com/facebookresearch/sam2/archive/${SAM2_COMMIT}.tar.gz" || {
        echo "Warning: SAM2 build failed. Some nodes may not work."
        echo "You can try building manually with CUDA toolkit."
    }
else
    echo ""
    echo "Note: CUDA toolkit (nvcc) not found. Skipping SAM2 build."
    echo "SAM2-dependent nodes won't work without manual compilation."
fi

# Install custom nodes
echo ""
echo "Installing custom nodes..."

INSTALL_ARGS="--comfy-path ${COMFY_PATH} --config ${REPO_ROOT}/supported_nodes.yaml"
if [[ "$USE_UV" == false ]]; then
    INSTALL_ARGS="${INSTALL_ARGS} --no-uv"
fi

# Add venv bin to PATH so install_custom_nodes.py can find comfy-cli
if [[ "$USE_UV" == true ]]; then
    export PATH="${VENV_PATH}/bin:${PATH}"
fi

$PYTHON_CMD "${REPO_ROOT}/scripts/install_custom_nodes.py" $INSTALL_ARGS

echo ""
echo "=== Installation Complete ==="
echo ""
echo "ComfyUI installed to: ${COMFY_PATH}"
if [[ "$USE_UV" == true ]]; then
    echo "Virtual environment: ${VENV_PATH}"
fi
echo ""
echo "To start ComfyUI:"
echo "  cd ${COMFY_PATH}"
if [[ "$USE_UV" == true ]]; then
    echo "  source ${VENV_PATH}/bin/activate"
fi
echo "  python main.py"
echo ""
echo "To start with the web UI accessible from other machines:"
echo "  python main.py --listen 0.0.0.0"
