#!/bin/bash
set -e

# Comfy Complete - Run All Tests
# This script runs the test suite regardless of the current directory.

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=== Comfy Complete - Running Tests ==="
echo "Repository root: ${REPO_ROOT}"
echo ""

# Check for uv
if ! command -v uv &> /dev/null; then
    echo "uv not found. Installing..."
    pip install uv
fi

# Check for pytest
if ! command -v pytest &> /dev/null; then
    echo "pytest not found. Installing..."
    uv pip install pytest
fi

# Check for pyyaml (required for tests)
if ! python3 -c "import yaml" &> /dev/null; then
    echo "PyYAML not found. Installing..."
    uv pip install pyyaml
fi

# Run tests
echo "Running tests..."
echo ""

cd "${REPO_ROOT}"
pytest tests/ -v "$@"

echo ""
echo "=== All tests completed ==="
