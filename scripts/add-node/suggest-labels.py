#!/usr/bin/env python3
"""
suggest-labels.py - Analyze a ComfyUI custom node repository and suggest labels.

Scans Python source files for code patterns that match the 18 labels defined in
supported_nodes.yaml. Outputs a YAML-formatted suggestion that can be copy-pasted
into the supported_nodes.yaml file.

This script is fully standalone and requires no external dependencies beyond the
Python standard library. It is intended for open-source contributors who want to
self-check their nodes before submitting a PR.

Usage:
    python suggest-labels.py /path/to/custom-node-repo
    python suggest-labels.py /path/to/custom-node-repo --json
    python suggest-labels.py --help

Labels detected (15 of 18 -- 3 require human judgment):
    ReadsArbitraryFile, WritesToDisk, CreatesLargeOutputs, NetworkAccess,
    Stateful, HasCustomEndpoints, RequiresExternalAPI, PathParsing,
    RequiresWebcam, RequiresDisplay, RequiresClipboard, RequiresGPU,
    ExecutesArbitraryCode, RuntimeModelDownload, RuntimePipInstall

Skipped (require human judgment or runtime testing):
    DuplicateOfCoreNode, Incompatible, BrokenNode
"""

import argparse
import ast
import json
import os
import re
import sys
import textwrap
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Label detection rules
# ---------------------------------------------------------------------------
# Each rule is a tuple of (label, description, list_of_pattern_checks).
# A pattern check is a function that takes (file_path, source_text, line_texts)
# and returns a list of (line_number, evidence_snippet) tuples.

Evidence = Tuple[int, str]  # (line_number, snippet)


def _regex_scanner(pattern: str, exclude_pattern: Optional[str] = None):
    """Return a scanner function that finds regex matches line by line."""
    compiled = re.compile(pattern)
    compiled_exclude = re.compile(exclude_pattern) if exclude_pattern else None

    def scan(filepath: str, source: str, lines: List[str]) -> List[Evidence]:
        results = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if compiled.search(line):
                if compiled_exclude and compiled_exclude.search(line):
                    continue
                snippet = stripped[:120]
                results.append((i, snippet))
        return results

    return scan


def _multi_regex_scanner(*patterns: str, exclude: Optional[str] = None):
    """Return a scanner that matches any of the given patterns."""
    compiled = [re.compile(p) for p in patterns]
    compiled_exclude = re.compile(exclude) if exclude else None

    def scan(filepath: str, source: str, lines: List[str]) -> List[Evidence]:
        results = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pat in compiled:
                if pat.search(line):
                    if compiled_exclude and compiled_exclude.search(line):
                        continue
                    results.append((i, stripped[:120]))
                    break
        return results

    return scan


# --- Individual label scanners ---

def scan_reads_arbitrary_file(filepath: str, source: str, lines: List[str]) -> List[Evidence]:
    """Detect STRING inputs named path/file/directory that feed into open()/read()."""
    results = []
    # Look for INPUT_TYPES patterns with path/file/directory string inputs
    input_pat = re.compile(
        r"""["'](?:path|file|directory|folder|filepath|file_path|dir|"""
        r"""input_dir|input_path|image_path|video_path|audio_path|load_path)["']"""
        r"""\s*:\s*\(\s*["']STRING["']""",
        re.IGNORECASE,
    )
    # Also look for open() reading from variable paths
    open_read_pat = re.compile(r'open\s*\([^)]*["\']r["\']|open\s*\([^)]*\)\s*\.read')
    # And direct reads with pathlib
    pathlib_read = re.compile(r'Path\s*\(.*\)\.read_')
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if input_pat.search(line):
            results.append((i, stripped[:120]))
        elif open_read_pat.search(line) and not re.search(r'open\s*\(\s*["\']', line):
            # open() with a variable (not a hardcoded string literal)
            results.append((i, stripped[:120]))
        elif pathlib_read.search(line):
            results.append((i, stripped[:120]))
    return results


def scan_writes_to_disk(filepath: str, source: str, lines: List[str]) -> List[Evidence]:
    """Detect writes to user-specified paths (not standard output/temp dirs)."""
    results = []
    patterns = [
        # open() in write mode with variable path
        re.compile(r"""open\s*\([^)]*["'][wa]b?["']\s*\)"""),
        re.compile(r"""open\s*\([^)]*,\s*["'][wa]"""),
        # Saving files
        re.compile(r'\.save\s*\('),
        re.compile(r'safetensors\.torch\.save'),
        re.compile(r'torch\.save\s*\('),
        re.compile(r'\.save_pretrained\s*\('),
        re.compile(r'imageio\.imwrite|cv2\.imwrite|sf\.write'),
        re.compile(r'shutil\.(copy|move|copytree)'),
        re.compile(r'\.write\s*\('),
    ]
    # Exclude patterns that write to standard comfy output dirs
    exclude = re.compile(r'folder_paths\.(get_output_directory|get_temp_directory)|output_dir|temp_dir')
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pat in patterns:
            if pat.search(line) and not exclude.search(line):
                results.append((i, stripped[:120]))
                break
    return results


def scan_creates_large_outputs(filepath: str, source: str, lines: List[str]) -> List[Evidence]:
    """Detect OUTPUT_NODE with video/audio/large batch output."""
    results = []
    has_output_node = False
    video_audio_patterns = re.compile(
        r'VIDEO|AUDIO|video_combine|imageio\.mimwrite|write_video|'
        r'VideoCombine|ffmpeg|\.mp4|\.avi|\.mov|\.mkv|\.wav|\.mp3|\.flac'
    )
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if 'OUTPUT_NODE' in line and 'True' in line:
            has_output_node = True
            results.append((i, stripped[:120]))
        elif video_audio_patterns.search(line) and has_output_node:
            results.append((i, stripped[:120]))
    return results


scan_network_access = _multi_regex_scanner(
    r'requests\.(get|post|put|delete|head|patch|session)\s*\(',
    r'urllib\.(request\.urlopen|request\.urlretrieve|request\.Request)',
    r'urllib\.request\.build_opener',
    r'httpx\.(get|post|put|delete|Client|AsyncClient)',
    r'aiohttp\.(ClientSession|request)',
    r'urlopen\s*\(',
    r'http\.client\.HTTP',
)

scan_stateful = _multi_regex_scanner(
    r'@lru_cache|@functools\.cache|@cache\b',
    r'_instance\s*=\s*None|__instance\s*=',
    r'@singleton|class\s+\w*[Ss]ingleton',
    # Module-level mutable state (dicts/lists used as caches)
    r'^[A-Z_]+\s*[:=]\s*(\{|\[|dict\(\)|list\(\))',
)


def scan_has_custom_endpoints(filepath: str, source: str, lines: List[str]) -> List[Evidence]:
    """Detect @PromptServer.instance.routes or @routes decorators."""
    results = []
    patterns = [
        re.compile(r'PromptServer\.instance\.routes'),
        re.compile(r'@\s*routes\.(get|post|put|delete|patch)\s*\('),
        re.compile(r'@\s*server\.routes'),
        re.compile(r'web\.RouteTableDef'),
    ]
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pat in patterns:
            if pat.search(line):
                results.append((i, stripped[:120]))
                break
    return results


scan_requires_external_api = _multi_regex_scanner(
    r'os\.environ\s*\[\s*["\'].*API[_-]?KEY',
    r'os\.getenv\s*\(\s*["\'].*API[_-]?KEY',
    r'OPENAI_API_KEY|ANTHROPIC_API_KEY|GOOGLE_API_KEY|HF_TOKEN|HUGGING_FACE_TOKEN',
    r'REPLICATE_API_TOKEN|STABILITY_API_KEY|FAL_KEY|RUNWAY_API',
    r'openai\.api_key|anthropic\.Anthropic|openai\.OpenAI',
)

scan_path_parsing = _multi_regex_scanner(
    r'os\.path\.(abspath|realpath|expanduser|expandvars|relpath|commonpath|commonprefix)',
    r'os\.path\.(split|splitext|dirname|basename|join)\s*\(',
    r'pathlib\.Path\s*\(',
    r'os\.getcwd\s*\(',
    r'os\.listdir\s*\(',
    r'os\.walk\s*\(',
    r'glob\.glob\s*\(',
)

scan_requires_webcam = _multi_regex_scanner(
    r'VideoCapture\s*\(\s*[0-9]',
    r'VideoCapture\s*\(\s*camera',
    r'VideoCapture\s*\(\s*device',
    r'picamera|libcamera',
)

scan_requires_display = _multi_regex_scanner(
    r'cv2\.imshow\s*\(',
    r'cv2\.waitKey\s*\(',
    r'plt\.show\s*\(',
    r'tkinter|Tkinter',
    r'QApplication|QMainWindow|QWidget',
    r'send_sync.*prompt_user',
    r'webbrowser\.open',
)

scan_requires_clipboard = _multi_regex_scanner(
    r'pyperclip',
    r'grabclipboard',
    r'clipboard\.(copy|paste)',
    r'QtGui\.QClipboard|QApplication\.clipboard',
)


def scan_requires_gpu(filepath: str, source: str, lines: List[str]) -> List[Evidence]:
    """Detect hardcoded .cuda() / .to('cuda') without fallback."""
    results = []
    cuda_pat = re.compile(r"""\.to\s*\(\s*["']cuda["']\s*\)|\.cuda\s*\(\s*\)|torch\.device\s*\(\s*["']cuda""")
    # Check if there's any fallback pattern in the file
    fallback_pat = re.compile(
        r'torch\.cuda\.is_available|comfy\.model_management|'
        r'if\s+.*cuda.*else.*cpu|device\s*=.*cpu'
    )
    has_fallback = fallback_pat.search(source)
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if cuda_pat.search(line):
            if not has_fallback:
                results.append((i, stripped[:120]))
    return results


def scan_executes_arbitrary_code(filepath: str, source: str, lines: List[str]) -> List[Evidence]:
    """Detect eval()/exec() on non-constant input."""
    results = []
    # eval() but not model.eval() or self.eval()
    eval_pat = re.compile(r'(?<!\.)eval\s*\(')
    # exec() but not subprocess_exec, create_subprocess_exec, etc.
    exec_pat = re.compile(r'(?<![_a-zA-Z])exec\s*\(')
    # compile() with exec/eval mode
    compile_pat = re.compile(r'compile\s*\(.*["\']exec["\']')
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if eval_pat.search(line):
            # Exclude safe patterns like json-safe eval with literal_eval
            if 'literal_eval' not in line:
                results.append((i, stripped[:120]))
        elif exec_pat.search(line):
            results.append((i, stripped[:120]))
        elif compile_pat.search(line):
            results.append((i, stripped[:120]))
    return results


scan_runtime_model_download = _multi_regex_scanner(
    r'hf_hub_download\s*\(',
    r'snapshot_download\s*\(',
    r'from_pretrained\s*\(',
    r'download_url_to_file\s*\(',
    r'urlretrieve\s*\(.*model',
    r'huggingface_hub\.(hf_hub_download|snapshot_download)',
    r'load_file_from_url\s*\(',
    r'wget\.download\s*\(',
    r'modelscope\.snapshot_download',
)

scan_runtime_pip_install = _multi_regex_scanner(
    r'subprocess.*pip\s+install',
    r'pip\s+install',
    r'ensure_package\s*\(',
    r'install_if_missing\s*\(',
    r'pkg_resources.*require',
    r'importlib\.metadata.*requires',
    # Direct subprocess calls that run pip
    r"""subprocess\.(run|call|check_call|Popen)\s*\(\s*\[.*['"]pip['"]""",
)


# Master list of (label_name, scanner_function)
LABEL_SCANNERS: List[Tuple[str, callable]] = [
    ("ReadsArbitraryFile", scan_reads_arbitrary_file),
    ("WritesToDisk", scan_writes_to_disk),
    ("CreatesLargeOutputs", scan_creates_large_outputs),
    ("NetworkAccess", scan_network_access),
    ("Stateful", scan_stateful),
    ("HasCustomEndpoints", scan_has_custom_endpoints),
    ("RequiresExternalAPI", scan_requires_external_api),
    ("PathParsing", scan_path_parsing),
    ("RequiresWebcam", scan_requires_webcam),
    ("RequiresDisplay", scan_requires_display),
    ("RequiresClipboard", scan_requires_clipboard),
    ("RequiresGPU", scan_requires_gpu),
    ("ExecutesArbitraryCode", scan_executes_arbitrary_code),
    ("RuntimeModelDownload", scan_runtime_model_download),
    ("RuntimePipInstall", scan_runtime_pip_install),
]

SKIPPED_LABELS = ["DuplicateOfCoreNode", "Incompatible", "BrokenNode"]

# ---------------------------------------------------------------------------
# Node class discovery
# ---------------------------------------------------------------------------


def find_python_files(repo_dir: str) -> List[str]:
    """Find all .py files in the repo, excluding common non-node directories."""
    exclude_dirs = {
        ".git", "__pycache__", ".venv", "venv", "env", "node_modules",
        ".eggs", "*.egg-info", "dist", "build", ".tox", ".mypy_cache",
    }
    py_files = []
    for root, dirs, files in os.walk(repo_dir):
        # Filter out excluded directories
        dirs[:] = [d for d in dirs if d not in exclude_dirs and not d.endswith(".egg-info")]
        for f in files:
            if f.endswith(".py"):
                py_files.append(os.path.join(root, f))
    return py_files


def extract_node_class_mappings(py_files: List[str]) -> Dict[str, str]:
    """
    Find NODE_CLASS_MAPPINGS entries across all Python files.

    Returns a dict of {node_class_name: defining_file_path}.
    """
    mappings: Dict[str, str] = {}
    # Pattern 1: NODE_CLASS_MAPPINGS = { "Name": ClassName, ... }
    # Pattern 2: NODE_CLASS_MAPPINGS.update({ "Name": ClassName, ... })
    # Pattern 3: NODE_CLASS_MAPPINGS["Name"] = ClassName
    dict_entry_pat = re.compile(r"""["']([^"']+)["']\s*:\s*\w+""")
    bracket_assign_pat = re.compile(r"""NODE_CLASS_MAPPINGS\s*\[\s*["']([^"']+)["']\s*\]""")
    in_mapping_block = False
    brace_depth = 0

    for filepath in py_files:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except (OSError, IOError):
            continue

        if "NODE_CLASS_MAPPINGS" not in source:
            continue

        lines = source.splitlines()
        in_mapping_block = False
        brace_depth = 0

        for line in lines:
            stripped = line.strip()

            # Detect start of NODE_CLASS_MAPPINGS dict
            if re.search(r'NODE_CLASS_MAPPINGS\s*=\s*\{', line) or \
               re.search(r'NODE_CLASS_MAPPINGS\s*\.update\s*\(\s*\{', line):
                in_mapping_block = True
                brace_depth = line.count("{") - line.count("}")
                # Also check this line for entries
                for m in dict_entry_pat.finditer(line):
                    mappings[m.group(1)] = filepath
                if brace_depth <= 0:
                    in_mapping_block = False
                continue

            if in_mapping_block:
                brace_depth += line.count("{") - line.count("}")
                for m in dict_entry_pat.finditer(line):
                    mappings[m.group(1)] = filepath
                if brace_depth <= 0:
                    in_mapping_block = False
                continue

            # Pattern 3: direct bracket assignment
            m = bracket_assign_pat.search(line)
            if m:
                mappings[m.group(1)] = filepath

    return mappings


def find_class_source_files(
    node_class_name: str,
    mappings_file: str,
    py_files: List[str],
) -> List[str]:
    """
    Given a node class name and the file where it appears in NODE_CLASS_MAPPINGS,
    find all source files that are relevant (the mapping file + any file defining
    the actual class).
    """
    relevant = {mappings_file}

    # Try to find which Python class is mapped to this node name
    try:
        with open(mappings_file, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, IOError):
        return list(relevant)

    # Extract the Python class name from the mapping
    pat = re.compile(
        rf"""["']{re.escape(node_class_name)}["']\s*:\s*(\w+)"""
    )
    match = pat.search(source)
    if not match:
        return list(relevant)

    python_class_name = match.group(1)

    # If the class is defined in the same file, we're done
    class_def_pat = re.compile(rf"class\s+{re.escape(python_class_name)}\s*[\(:]")
    if class_def_pat.search(source):
        return list(relevant)

    # Search other files for the class definition
    for filepath in py_files:
        if filepath == mappings_file:
            continue
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except (OSError, IOError):
            continue
        if class_def_pat.search(content):
            relevant.add(filepath)
            break

    return list(relevant)


# ---------------------------------------------------------------------------
# Per-node analysis
# ---------------------------------------------------------------------------


def analyze_file(filepath: str) -> Dict[str, List[Evidence]]:
    """Run all label scanners on a single file. Returns {label: [evidence]}."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            source = f.read()
    except (OSError, IOError):
        return {}

    lines = source.splitlines()
    results: Dict[str, List[Evidence]] = {}
    for label, scanner in LABEL_SCANNERS:
        evidence = scanner(filepath, source, lines)
        if evidence:
            results[label] = evidence
    return results


def analyze_node(
    node_name: str,
    mappings_file: str,
    py_files: List[str],
    repo_dir: str,
) -> Dict[str, List[Tuple[str, int, str]]]:
    """
    Analyze a single node class.

    Returns {label: [(relative_filepath, line_number, snippet), ...]}.
    """
    source_files = find_class_source_files(node_name, mappings_file, py_files)
    combined: Dict[str, List[Tuple[str, int, str]]] = defaultdict(list)

    for filepath in source_files:
        file_results = analyze_file(filepath)
        rel_path = os.path.relpath(filepath, repo_dir).replace("\\", "/")
        for label, evidences in file_results.items():
            for line_no, snippet in evidences:
                combined[label].append((rel_path, line_no, snippet))

    return dict(combined)


# ---------------------------------------------------------------------------
# Repository-level analysis
# ---------------------------------------------------------------------------


def analyze_repo_wide(
    py_files: List[str], repo_dir: str
) -> Dict[str, List[Tuple[str, int, str]]]:
    """
    Run label scanners across ALL Python files in the repo.

    This catches patterns that live in utility/helper files not directly
    tied to a specific node class. Returns {label: [(rel_path, line, snippet)]}.
    """
    combined: Dict[str, List[Tuple[str, int, str]]] = defaultdict(list)
    for filepath in py_files:
        file_results = analyze_file(filepath)
        rel_path = os.path.relpath(filepath, repo_dir).replace("\\", "/")
        for label, evidences in file_results.items():
            for line_no, snippet in evidences:
                combined[label].append((rel_path, line_no, snippet))
    return dict(combined)


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_evidence(evidence_list: List[Tuple[str, int, str]], max_items: int = 3) -> str:
    """Format evidence items into comment strings."""
    items = evidence_list[:max_items]
    parts = []
    for rel_path, line_no, snippet in items:
        # Truncate snippet for readability
        short = snippet[:80] + ("..." if len(snippet) > 80 else "")
        parts.append(f"{rel_path}:{line_no} - {short}")
    if len(evidence_list) > max_items:
        parts.append(f"... and {len(evidence_list) - max_items} more")
    return "; ".join(parts)


def get_pack_name(repo_dir: str) -> str:
    """Guess a pack name from the repo directory."""
    return os.path.basename(os.path.abspath(repo_dir))


def output_text(
    pack_name: str,
    node_labels: Dict[str, Dict[str, List[Tuple[str, int, str]]]],
    all_nodes: List[str],
    repo_wide: Dict[str, List[Tuple[str, int, str]]],
):
    """Print human-readable output with YAML copy-paste block."""
    print(f"=== Label Suggestions for {pack_name} ===")
    print()

    nodes_with_labels = {n for n, labels in node_labels.items() if labels}
    nodes_without_labels = [n for n in sorted(all_nodes) if n not in nodes_with_labels]

    # --- Per-node suggestions ---
    if nodes_with_labels:
        print("Suggested node_labels:")
        for node_name in sorted(nodes_with_labels):
            labels = node_labels[node_name]
            print(f"  {node_name}:")
            for label in sorted(labels.keys()):
                evidence = labels[label]
                comment = format_evidence(evidence, max_items=2)
                print(f"    - {label:<25s} # {comment}")
        print()

    # --- Clean nodes ---
    if nodes_without_labels:
        print("No labels needed:")
        for node_name in nodes_without_labels:
            print(f"  - {node_name} (no concerning patterns detected)")
        print()

    # --- Repo-wide findings not tied to specific nodes ---
    if repo_wide:
        print("Repo-wide patterns (may apply to nodes called from these files):")
        for label in sorted(repo_wide.keys()):
            evidence = repo_wide[label]
            comment = format_evidence(evidence, max_items=3)
            print(f"  {label}: {comment}")
        print()

    # --- Skipped labels ---
    print(f"Skipped labels (require human judgment): {', '.join(SKIPPED_LABELS)}")
    print()

    # --- Copy-paste YAML ---
    if nodes_with_labels:
        print("Copy-paste YAML:")
        print("    node_labels:")
        for node_name in sorted(nodes_with_labels):
            labels = node_labels[node_name]
            # Quote names that contain special YAML characters
            if any(c in node_name for c in " :{}[],'\"!@#$%^&*()+-"):
                quoted = f'"{node_name}"'
            else:
                quoted = node_name
            print(f"      {quoted}:")
            for label in sorted(labels.keys()):
                print(f"        - {label}")
    else:
        print("No labels to suggest -- this node pack looks clean!")
    print()


def output_json(
    pack_name: str,
    node_labels: Dict[str, Dict[str, List[Tuple[str, int, str]]]],
    all_nodes: List[str],
    repo_wide: Dict[str, List[Tuple[str, int, str]]],
):
    """Print machine-readable JSON output."""
    result = {
        "pack_name": pack_name,
        "node_labels": {},
        "clean_nodes": [],
        "repo_wide_patterns": {},
        "skipped_labels": SKIPPED_LABELS,
    }

    for node_name in sorted(all_nodes):
        labels = node_labels.get(node_name, {})
        if labels:
            result["node_labels"][node_name] = {}
            for label, evidence in sorted(labels.items()):
                result["node_labels"][node_name][label] = [
                    {"file": rel_path, "line": line_no, "snippet": snippet}
                    for rel_path, line_no, snippet in evidence
                ]
        else:
            result["clean_nodes"].append(node_name)

    for label, evidence in sorted(repo_wide.items()):
        result["repo_wide_patterns"][label] = [
            {"file": rel_path, "line": line_no, "snippet": snippet}
            for rel_path, line_no, snippet in evidence
        ]

    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Analyze a ComfyUI custom node repository and suggest labels for supported_nodes.yaml.",
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s /path/to/ComfyUI-MyNodes
              %(prog)s /path/to/ComfyUI-MyNodes --json
              %(prog)s /path/to/ComfyUI-MyNodes --json > labels.json

            This tool scans for 15 of the 18 defined labels. Three labels
            (DuplicateOfCoreNode, Incompatible, BrokenNode) require human
            judgment and are skipped.

            The output includes a copy-paste YAML block that can be added
            directly to supported_nodes.yaml under your node pack entry.
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "repo_dir",
        help="Path to the custom node repository to analyze.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON for machine consumption.",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show all evidence lines (not just top matches).",
    )
    args = parser.parse_args()

    repo_dir = os.path.abspath(args.repo_dir)

    if not os.path.isdir(repo_dir):
        print(f"Error: '{repo_dir}' is not a directory.", file=sys.stderr)
        sys.exit(0)  # Exit 0 -- suggestions, not enforcement

    # 1. Find all Python files
    py_files = find_python_files(repo_dir)
    if not py_files:
        print(f"Error: No Python files found in '{repo_dir}'.", file=sys.stderr)
        sys.exit(0)

    # 2. Extract NODE_CLASS_MAPPINGS
    mappings = extract_node_class_mappings(py_files)
    if not mappings:
        print(
            f"Warning: No NODE_CLASS_MAPPINGS found in '{repo_dir}'.\n"
            f"This may not be a ComfyUI custom node repository, or the node\n"
            f"registration pattern is non-standard.\n"
            f"\n"
            f"Checked {len(py_files)} Python file(s).",
            file=sys.stderr,
        )
        # Still run repo-wide analysis
        repo_wide = analyze_repo_wide(py_files, repo_dir)
        pack_name = get_pack_name(repo_dir)
        if args.json_output:
            output_json(pack_name, {}, [], repo_wide)
        else:
            output_text(pack_name, {}, [], repo_wide)
        sys.exit(0)

    pack_name = get_pack_name(repo_dir)
    all_nodes = sorted(mappings.keys())

    print(
        f"Found {len(all_nodes)} node(s) in {len(set(mappings.values()))} file(s).",
        file=sys.stderr,
    )

    # 3. Analyze each node
    node_labels: Dict[str, Dict[str, List[Tuple[str, int, str]]]] = {}
    for node_name in all_nodes:
        mappings_file = mappings[node_name]
        node_results = analyze_node(node_name, mappings_file, py_files, repo_dir)
        node_labels[node_name] = node_results

    # 4. Repo-wide scan for patterns in files not tied to specific nodes
    node_files = set(mappings.values())
    non_node_files = [f for f in py_files if f not in node_files]
    repo_wide = analyze_repo_wide(non_node_files, repo_dir) if non_node_files else {}

    # 5. Output
    if args.json_output:
        output_json(pack_name, node_labels, all_nodes, repo_wide)
    else:
        output_text(pack_name, node_labels, all_nodes, repo_wide)

    sys.exit(0)


if __name__ == "__main__":
    main()
