#!/usr/bin/env python3
"""Check licenses for a ComfyUI custom node, its pip dependencies, and models.

Exhaustive license check: node repo license + all pip deps (transitive via PyPI)
+ model licenses (HuggingFace). Blocklist enforcement for insightface, deepface,
AGPL, and other restrictive licenses. Hard fail on blockers found.

Examples:
    # Check a cloned node repo
    python scripts/add-node/check-license.py /path/to/node/repo

    # Also check models declared in supported_nodes.yaml
    python scripts/add-node/check-license.py /path/to/node/repo \\
        --yaml supported_nodes.yaml --name comfyui-example

    # Machine-readable output
    python scripts/add-node/check-license.py /path/to/node/repo --json
"""

import argparse
import json
import os
import re
import sys
import textwrap
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    import yaml
except ImportError:
    yaml = None

# Ensure stdout can handle unicode (needed on Windows with cp1252)
if hasattr(sys.stdout, 'reconfigure'):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if hasattr(sys.stderr, 'reconfigure'):
    try:
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

# ---------------------------------------------------------------------------
# License classification
# ---------------------------------------------------------------------------

# Licenses considered permissive and safe for inclusion
PERMISSIVE_LICENSES = {
    "mit",
    "apache-2.0", "apache 2.0", "apache license 2.0", "apache software license",
    "apache license, version 2.0",
    "bsd", "bsd-2-clause", "bsd-3-clause", "bsd 2-clause", "bsd 3-clause",
    "bsd license", "bsd-2-clause license", "bsd-3-clause license",
    "simplified bsd", "new bsd", "new bsd license", "revised bsd",
    "isc", "isc license",
    "unlicense", "the unlicense",
    "public domain", "cc0", "cc0-1.0", "cc0 1.0",
    "wtfpl",
    "zlib", "zlib license",
    "boost software license", "bsl-1.0",
    "python software foundation license", "psf", "psf license", "psfl",
    "python-2.0",
    "mpl-2.0", "mozilla public license 2.0",
    "artistic-2.0", "artistic license 2.0",
    "0bsd",
    "openrail", "creativeml openrail-m", "openrail-m", "openrail++",
    "mit license", "mit no attribution",
    "historical permission notice and disclaimer",
    "hpnd",
}

# Licenses that are restrictive and must be blocked
BLOCKED_LICENSE_PATTERNS = [
    (re.compile(r'\bagpl\b', re.IGNORECASE), "AGPL"),
    (re.compile(r'\bgnu affero\b', re.IGNORECASE), "AGPL"),
    (re.compile(r'\bgpl\b(?!.*exception)', re.IGNORECASE), "GPL"),
    (re.compile(r'\bgnu general public\b', re.IGNORECASE), "GPL"),
    (re.compile(r'\bsspl\b', re.IGNORECASE), "SSPL"),
    (re.compile(r'\bserver side public\b', re.IGNORECASE), "SSPL"),
    (re.compile(r'\bbusl\b', re.IGNORECASE), "BUSL"),
    (re.compile(r'\bbusiness source\b', re.IGNORECASE), "BUSL"),
    (re.compile(r'\bcc[- ]?by[- ]?nc\b', re.IGNORECASE), "CC-BY-NC"),
    (re.compile(r'\bcreative commons.*non[- ]?commercial\b', re.IGNORECASE), "CC-BY-NC"),
    (re.compile(r'\bcc[- ]?by[- ]?nd\b', re.IGNORECASE), "CC-BY-ND"),
    (re.compile(r'\bcreative commons.*no[- ]?deriv\b', re.IGNORECASE), "CC-BY-ND"),
]

# Licenses that warrant a warning (usable but with caveats)
WARN_LICENSE_PATTERNS = [
    (re.compile(r'\blgpl\b', re.IGNORECASE), "LGPL (requires dynamic linking)"),
    (re.compile(r'\bgnu lesser\b', re.IGNORECASE), "LGPL (requires dynamic linking)"),
]

# Hard-blocked packages regardless of their declared license
BLOCKED_PACKAGES = {
    "insightface": "Contains proprietary model code with restrictive license",
    "deepface": "Bundles models with non-commercial restrictions",
    "gfpgan": "GPL-licensed (inherits from BasicSR)",
    "basicsr": "GPL-3.0 licensed",
    "realesrgan": "GPL-3.0 licensed (depends on BasicSR)",
    "facexlib": "GPL-3.0 licensed (depends on BasicSR)",
}

# Packages known to be safe that may have ambiguous metadata
KNOWN_SAFE_PACKAGES = {
    "torch", "torchvision", "torchaudio",
    "numpy", "scipy", "pillow", "opencv-python", "opencv-python-headless",
    "requests", "pyyaml", "packaging", "setuptools", "pip", "wheel",
    "certifi", "charset-normalizer", "idna", "urllib3",
    "six", "typing-extensions", "filelock", "jinja2", "markupsafe",
    "sympy", "networkx", "mpmath", "fsspec",
    "safetensors", "huggingface-hub", "tokenizers", "transformers",
    "accelerate", "diffusers", "einops", "kornia",
    "tqdm", "colorama", "click", "rich",
    "aiohttp", "aiosignal", "frozenlist", "multidict", "yarl", "attrs",
}

# Non-commercial model license identifiers
NON_COMMERCIAL_MODEL_LICENSES = {
    "cc-by-nc-4.0", "cc-by-nc-sa-4.0", "cc-by-nc-nd-4.0",
    "cc-by-nc-3.0", "cc-by-nc-sa-3.0", "cc-by-nc-nd-3.0",
    "cc-by-nc-2.0", "cc-by-nc-sa-2.0",
    "other",  # "other" is often restrictive on HuggingFace
}

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

PASS = "PASS"
BLOCK = "BLOCK"
WARN = "WARN"
SKIP = "SKIP"


class CheckResult:
    """A single license check result."""

    def __init__(self, status: str, name: str, license_str: str, reason: str = ""):
        self.status = status
        self.name = name
        self.license = license_str
        self.reason = reason

    def to_dict(self) -> dict:
        d = {"status": self.status, "name": self.name, "license": self.license}
        if self.reason:
            d["reason"] = self.reason
        return d


# ---------------------------------------------------------------------------
# PyPI response cache
# ---------------------------------------------------------------------------

_pypi_cache: Dict[str, Optional[dict]] = {}


def _fetch_json(url: str, timeout: int = 10) -> Optional[dict]:
    """Fetch JSON from a URL, returning None on failure."""
    try:
        req = urllib.request.Request(url, method='GET')
        req.add_header('User-Agent', 'comfy-complete-license-check/1.0')
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None


def get_pypi_info(package_name: str) -> Optional[dict]:
    """Fetch package info from PyPI JSON API with caching."""
    normalized = re.sub(r'[-_.]+', '-', package_name).lower()
    if normalized in _pypi_cache:
        return _pypi_cache[normalized]

    data = _fetch_json(f"https://pypi.org/pypi/{normalized}/json")
    _pypi_cache[normalized] = data
    return data


# ---------------------------------------------------------------------------
# License classification helpers
# ---------------------------------------------------------------------------


def classify_license(license_text: str) -> Tuple[str, str]:
    """Classify a license string.

    Returns (status, detail) where status is PASS/BLOCK/WARN and detail
    is a human-readable explanation.
    """
    if not license_text or license_text.strip().lower() in ("", "unknown", "none"):
        return WARN, "Unknown license"

    text = license_text.strip()
    text_lower = text.lower()

    # Check blocked patterns first (order matters: AGPL before GPL)
    for pattern, label in BLOCKED_LICENSE_PATTERNS:
        if pattern.search(text):
            # Exception: LGPL is a warning, not a block. But our blocked list
            # has GPL which would also match LGPL, so check LGPL first.
            is_lgpl = re.search(r'\blgpl\b|\bgnu lesser\b', text, re.IGNORECASE)
            if is_lgpl and label == "GPL":
                continue  # Let the WARN patterns handle LGPL
            return BLOCK, f"{label} license"

    # Check warn patterns
    for pattern, label in WARN_LICENSE_PATTERNS:
        if pattern.search(text):
            return WARN, label

    # Check permissive
    if text_lower in PERMISSIVE_LICENSES:
        return PASS, text

    # Try partial matching for common permissive patterns
    permissive_partials = [
        (r'\bmit\b', "MIT"),
        (r'\bapache\b.*\b2', "Apache-2.0"),
        (r'\bbsd\b', "BSD"),
        (r'\bisc\b', "ISC"),
        (r'\bunlicense\b', "Unlicense"),
        (r'\bcc0\b', "CC0"),
        (r'\bpublic\s+domain\b', "Public Domain"),
        (r'\bpsf\b', "PSF"),
        (r'\bpython\b', "Python"),
        (r'\bmpl[- ]?2', "MPL-2.0"),
        (r'\bopenrail\b', "OpenRAIL"),
    ]
    for pattern, label in permissive_partials:
        if re.search(pattern, text, re.IGNORECASE):
            return PASS, label

    return WARN, f"Unknown license: {text[:80]}"


def extract_license_from_classifiers(classifiers: List[str]) -> Optional[str]:
    """Extract license from PyPI trove classifiers."""
    for c in classifiers:
        if c.startswith("License :: OSI Approved ::"):
            return c.split("::")[-1].strip()
        if c.startswith("License ::"):
            return c.split("::")[-1].strip()
    return None


# ---------------------------------------------------------------------------
# Node repo license checking
# ---------------------------------------------------------------------------

LICENSE_FILENAMES = [
    "LICENSE", "LICENSE.md", "LICENSE.txt", "LICENSE.rst",
    "LICENCE", "LICENCE.md", "LICENCE.txt",
    "COPYING", "COPYING.md", "COPYING.txt",
]


def check_node_license(repo_dir: str) -> CheckResult:
    """Check the license of a node repository."""
    # 1. Look for LICENSE files
    license_text = None
    license_file = None
    for fname in LICENSE_FILENAMES:
        fpath = os.path.join(repo_dir, fname)
        if os.path.isfile(fpath):
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    license_text = f.read()
                    license_file = fname
                    break
            except OSError:
                continue

    # 2. Check pyproject.toml
    pyproject_license = None
    pyproject_path = os.path.join(repo_dir, "pyproject.toml")
    if os.path.isfile(pyproject_path):
        pyproject_license = _extract_license_from_pyproject(pyproject_path)

    # 3. Check setup.py
    setup_license = None
    setup_path = os.path.join(repo_dir, "setup.py")
    if os.path.isfile(setup_path):
        setup_license = _extract_license_from_setup_py(setup_path)

    # 4. Check setup.cfg
    setupcfg_license = None
    setupcfg_path = os.path.join(repo_dir, "setup.cfg")
    if os.path.isfile(setupcfg_path):
        setupcfg_license = _extract_license_from_setup_cfg(setupcfg_path)

    # Determine the best license identifier
    declared = pyproject_license or setup_license or setupcfg_license

    if declared:
        status, detail = classify_license(declared)
        return CheckResult(status, "node", declared, detail)

    if license_text:
        # Try to identify from license file content
        identified = _identify_license_from_text(license_text)
        if identified:
            status, detail = classify_license(identified)
            return CheckResult(status, "node", identified,
                               f"{detail} (from {license_file})")
        return CheckResult(WARN, "node", f"Unrecognized (from {license_file})",
                           "License file found but could not identify license type")

    return CheckResult(WARN, "node", "No license found",
                       "No LICENSE file or license metadata found in repo")


def _extract_license_from_pyproject(path: str) -> Optional[str]:
    """Extract license from pyproject.toml without a TOML parser."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    # Match license = "MIT" or license = {text = "MIT"}
    m = re.search(r'^\s*license\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if m:
        return m.group(1)

    m = re.search(r'^\s*license\s*=\s*\{[^}]*text\s*=\s*"([^"]+)"', content, re.MULTILINE)
    if m:
        return m.group(1)

    # SPDX style: license = {file = "LICENSE"} -- not useful for classification
    # Check classifiers
    classifier_pat = re.compile(r'"(License\s*::[^"]+)"')
    classifiers = classifier_pat.findall(content)
    if classifiers:
        return extract_license_from_classifiers(classifiers)

    return None


def _extract_license_from_setup_py(path: str) -> Optional[str]:
    """Extract license from setup.py."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    m = re.search(r'license\s*=\s*["\']([^"\']+)["\']', content)
    if m:
        return m.group(1)
    return None


def _extract_license_from_setup_cfg(path: str) -> Optional[str]:
    """Extract license from setup.cfg."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return None

    m = re.search(r'^\s*license\s*=\s*(.+)$', content, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


def _identify_license_from_text(text: str) -> Optional[str]:
    """Try to identify a license from its full text."""
    text_lower = text.lower()

    patterns = [
        (r'mit license|permission is hereby granted, free of charge', "MIT"),
        (r'apache license.*version 2\.0', "Apache-2.0"),
        (r'redistribution and use in source and binary forms.*3 conditions',
         "BSD-3-Clause"),
        (r'redistribution and use in source and binary forms.*2 conditions',
         "BSD-2-Clause"),
        (r'gnu affero general public license', "AGPL-3.0"),
        (r'gnu general public license.*version 3', "GPL-3.0"),
        (r'gnu general public license.*version 2', "GPL-2.0"),
        (r'gnu lesser general public license', "LGPL"),
        (r'isc license', "ISC"),
        (r'the unlicense|this is free and unencumbered software', "Unlicense"),
        (r'creative commons.*attribution.*noncommercial', "CC-BY-NC"),
        (r'creative commons.*attribution.*noderivs', "CC-BY-ND"),
        (r'mozilla public license.*2\.0', "MPL-2.0"),
        (r'boost software license', "BSL-1.0"),
    ]
    for pattern, name in patterns:
        if re.search(pattern, text_lower):
            return name
    return None


# ---------------------------------------------------------------------------
# Pip dependency parsing
# ---------------------------------------------------------------------------

def parse_requirements_txt(path: str) -> List[str]:
    """Parse a requirements.txt file and return package names."""
    packages = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                # Handle -r includes (skip, not following for simplicity)
                if line.startswith("-r ") or line.startswith("--requirement"):
                    continue
                # Strip environment markers
                line = line.split(";")[0].strip()
                # Strip version specifiers
                name = re.split(r'[><=!~\[]', line)[0].strip()
                if name and not name.startswith(("git+", "http://", "https://")):
                    packages.append(name)
    except OSError:
        pass
    return packages


def parse_pyproject_deps(path: str) -> List[str]:
    """Extract dependency names from pyproject.toml (best-effort, no TOML parser)."""
    packages = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return packages

    # Look for dependencies = [...] section
    # This is a simplified parser that handles the common case
    m = re.search(r'dependencies\s*=\s*\[(.*?)\]', content, re.DOTALL)
    if m:
        deps_block = m.group(1)
        for dep in re.findall(r'"([^"]+)"', deps_block):
            dep = dep.split(";")[0].strip()
            name = re.split(r'[><=!~\[]', dep)[0].strip()
            if name and not name.startswith(("git+", "http://", "https://")):
                packages.append(name)
    return packages


def parse_setup_cfg_deps(path: str) -> List[str]:
    """Extract dependency names from setup.cfg."""
    packages = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return packages

    m = re.search(r'\[options\].*?install_requires\s*=\s*(.*?)(?:\n\[|\Z)',
                  content, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            line = line.split(";")[0].strip()
            name = re.split(r'[><=!~\[]', line)[0].strip()
            if name and not name.startswith(("git+", "http://", "https://")):
                packages.append(name)
    return packages


def collect_node_dependencies(repo_dir: str) -> List[str]:
    """Collect all declared pip dependencies from a node repo."""
    packages: List[str] = []

    # requirements.txt (common for ComfyUI nodes)
    for fname in ["requirements.txt", "requirements_base.txt"]:
        fpath = os.path.join(repo_dir, fname)
        if os.path.isfile(fpath):
            packages.extend(parse_requirements_txt(fpath))

    # pyproject.toml
    pyproject = os.path.join(repo_dir, "pyproject.toml")
    if os.path.isfile(pyproject):
        packages.extend(parse_pyproject_deps(pyproject))

    # setup.cfg
    setupcfg = os.path.join(repo_dir, "setup.cfg")
    if os.path.isfile(setupcfg):
        packages.extend(parse_setup_cfg_deps(setupcfg))

    # Deduplicate (preserve order)
    seen: Set[str] = set()
    unique: List[str] = []
    for p in packages:
        normalized = re.sub(r'[-_.]+', '-', p).lower()
        if normalized not in seen:
            seen.add(normalized)
            unique.append(p)
    return unique


# ---------------------------------------------------------------------------
# Dependency license checking (via PyPI API)
# ---------------------------------------------------------------------------


def check_dependency_license(package_name: str) -> CheckResult:
    """Check the license of a pip package via PyPI JSON API."""
    normalized = re.sub(r'[-_.]+', '-', package_name).lower()

    # Check blocklist first
    if normalized in BLOCKED_PACKAGES:
        reason = BLOCKED_PACKAGES[normalized]
        return CheckResult(BLOCK, package_name, "BLOCKLISTED PACKAGE", reason)

    # Skip known-safe packages
    if normalized in KNOWN_SAFE_PACKAGES:
        return CheckResult(SKIP, package_name, "known-safe", "")

    # Query PyPI
    data = get_pypi_info(package_name)
    if data is None:
        return CheckResult(WARN, package_name, "Unknown",
                           "Could not fetch package info from PyPI")

    info = data.get("info", {})

    # Try classifiers first (more structured)
    classifiers = info.get("classifiers", [])
    classifier_license = extract_license_from_classifiers(classifiers)

    # Then try the license field
    license_field = info.get("license", "")
    # Some packages put the entire license text in the license field;
    # if it's very long, it's probably the full text
    if license_field and len(license_field) > 200:
        identified = _identify_license_from_text(license_field)
        if identified:
            license_field = identified
        else:
            license_field = ""

    license_str = classifier_license or license_field or ""
    if not license_str:
        return CheckResult(WARN, package_name, "Unknown",
                           "No license metadata on PyPI")

    status, detail = classify_license(license_str)
    return CheckResult(status, package_name, license_str, detail)


def get_transitive_deps(package_name: str, visited: Set[str],
                        depth: int = 0, max_depth: int = 3) -> List[str]:
    """Get transitive dependencies from PyPI metadata (best-effort).

    Limits recursion depth to avoid excessive API calls.
    """
    normalized = re.sub(r'[-_.]+', '-', package_name).lower()
    if normalized in visited or depth > max_depth:
        return []

    visited.add(normalized)

    data = get_pypi_info(package_name)
    if data is None:
        return []

    info = data.get("info", {})
    requires_dist = info.get("requires_dist") or []

    transitive: List[str] = []
    for req in requires_dist:
        # Skip extras and conditional deps (e.g., '; extra == "dev"')
        if "extra ==" in req:
            continue

        # Extract package name
        name = re.split(r'[><=!~\[;\s]', req)[0].strip()
        if not name:
            continue

        dep_normalized = re.sub(r'[-_.]+', '-', name).lower()
        if dep_normalized not in visited:
            transitive.append(name)
            transitive.extend(
                get_transitive_deps(name, visited, depth + 1, max_depth)
            )

    return transitive


def check_all_dependencies(direct_deps: List[str],
                           check_transitive: bool = True) -> List[CheckResult]:
    """Check licenses for all dependencies (direct and transitive)."""
    results: List[CheckResult] = []
    visited: Set[str] = set()

    # Check direct deps
    all_deps = list(direct_deps)

    if check_transitive:
        # Collect transitive deps
        transitive_visited: Set[str] = set()
        for dep in direct_deps:
            transitive = get_transitive_deps(dep, transitive_visited)
            for t in transitive:
                t_normalized = re.sub(r'[-_.]+', '-', t).lower()
                if t_normalized not in visited:
                    all_deps.append(t)

    # Check each unique dep
    for dep in all_deps:
        normalized = re.sub(r'[-_.]+', '-', dep).lower()
        if normalized in visited:
            continue
        visited.add(normalized)

        result = check_dependency_license(dep)
        if result.status != SKIP:
            results.append(result)

    return results


# ---------------------------------------------------------------------------
# Model license checking
# ---------------------------------------------------------------------------


def check_model_license(model_url: str) -> Optional[CheckResult]:
    """Check the license of a model given its URL.

    Supports HuggingFace model URLs. Returns None if URL is not recognized.
    """
    # Extract HuggingFace repo ID
    hf_match = re.match(
        r'https?://huggingface\.co/([^/]+/[^/]+)(?:/|$)', model_url
    )
    if not hf_match:
        # Try direct repo ID format (org/model)
        if re.match(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', model_url):
            repo_id = model_url
        else:
            return None
    else:
        repo_id = hf_match.group(1)

    data = _fetch_json(f"https://huggingface.co/api/models/{repo_id}")
    if data is None:
        return CheckResult(WARN, repo_id, "Unknown",
                           "Could not fetch model info from HuggingFace")

    # HuggingFace returns license as a tag or in cardData
    license_tag = None
    tags = data.get("tags", [])
    for tag in tags:
        if tag.startswith("license:"):
            license_tag = tag.split(":", 1)[1]
            break

    if not license_tag:
        card_data = data.get("cardData", {})
        if isinstance(card_data, dict):
            license_tag = card_data.get("license", "")

    if not license_tag:
        return CheckResult(WARN, repo_id, "No license",
                           "No license tag found on HuggingFace model card")

    # Check for non-commercial
    if license_tag.lower() in NON_COMMERCIAL_MODEL_LICENSES:
        return CheckResult(BLOCK, repo_id, license_tag,
                           "Non-commercial license")

    status, detail = classify_license(license_tag)
    return CheckResult(status, repo_id, license_tag, detail)


def collect_model_urls_from_yaml(yaml_path: str, pack_name: str) -> List[str]:
    """Extract model URLs from a supported_nodes.yaml entry."""
    if yaml is None:
        return []

    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, Exception):
        return []

    node_packs = data.get("node_packs", [])
    for pack in node_packs:
        if pack.get("name") == pack_name:
            models = pack.get("models", [])
            if not isinstance(models, list):
                return []
            urls = []
            for model in models:
                if isinstance(model, dict):
                    url = model.get("url", "")
                    if url:
                        urls.append(url)
            return urls
    return []


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_status_symbol(status: str) -> str:
    """Return a text label for a status."""
    return {
        PASS: "PASS ",
        BLOCK: "BLOCK",
        WARN: "WARN ",
        SKIP: "SKIP ",
    }.get(status, "???? ")


def print_text_report(node_result: CheckResult,
                      dep_results: List[CheckResult],
                      model_results: List[CheckResult],
                      direct_dep_names: List[str]):
    """Print a human-readable report."""
    print()
    print("License Check Results")
    print("=====================")
    print()

    # Node license
    reason = f" -- {node_result.reason}" if node_result.reason else ""
    print(f"Node License: {node_result.license} ({node_result.status}){reason}")
    print()

    # Dependencies
    if dep_results:
        print("Dependency Licenses:")
        # Show direct deps first, then transitive
        direct_normalized = {re.sub(r'[-_.]+', '-', d).lower() for d in direct_dep_names}
        direct_results = []
        transitive_results = []
        for r in dep_results:
            r_normalized = re.sub(r'[-_.]+', '-', r.name).lower()
            if r_normalized in direct_normalized:
                direct_results.append(r)
            else:
                transitive_results.append(r)

        for r in direct_results:
            reason = f" -- {r.reason}" if r.reason else ""
            print(f"  {format_status_symbol(r.status)} {r.name} ({r.license}){reason}")

        if transitive_results:
            # Only show transitive deps that have issues
            transitive_issues = [r for r in transitive_results
                                 if r.status in (BLOCK, WARN)]
            if transitive_issues:
                print(f"  --- transitive dependencies ({len(transitive_results)} checked) ---")
                for r in transitive_issues:
                    reason = f" -- {r.reason}" if r.reason else ""
                    print(f"  {format_status_symbol(r.status)} {r.name} ({r.license}){reason}")
            else:
                print(f"  ({len(transitive_results)} transitive dependencies checked, all OK)")
        print()
    elif direct_dep_names:
        print("Dependency Licenses: (no dependencies to check)")
        print()
    else:
        print("Dependency Licenses: no requirements found")
        print()

    # Models
    if model_results:
        print("Model Licenses:")
        for r in model_results:
            reason = f" -- {r.reason}" if r.reason else ""
            print(f"  {format_status_symbol(r.status)} {r.name} ({r.license}){reason}")
        print()

    # Summary
    all_results = [node_result] + dep_results + model_results
    blockers = sum(1 for r in all_results if r.status == BLOCK)
    warnings = sum(1 for r in all_results if r.status == WARN)

    if blockers:
        print(f"FAIL: {blockers} blocker(s), {warnings} warning(s)")
    elif warnings:
        print(f"PASS (with {warnings} warning(s))")
    else:
        print("PASS")


def build_json_report(node_result: CheckResult,
                      dep_results: List[CheckResult],
                      model_results: List[CheckResult]) -> dict:
    """Build a JSON-serializable report."""
    all_results = [node_result] + dep_results + model_results
    blockers = sum(1 for r in all_results if r.status == BLOCK)
    warnings = sum(1 for r in all_results if r.status == WARN)

    return {
        "pass": blockers == 0,
        "blockers": blockers,
        "warnings": warnings,
        "node_license": node_result.to_dict(),
        "dependency_licenses": [r.to_dict() for r in dep_results],
        "model_licenses": [r.to_dict() for r in model_results],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Check licenses for a ComfyUI custom node, its pip dependencies, "
            "and associated models. Enforces a blocklist of restrictive licenses "
            "and known-problematic packages."
        ),
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s /path/to/node/repo
              %(prog)s /path/to/node/repo --yaml supported_nodes.yaml --name comfyui-example
              %(prog)s /path/to/node/repo --json
              %(prog)s /path/to/node/repo --no-transitive
        """),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "repo_dir",
        help="Path to the cloned custom node repository to check.",
    )
    parser.add_argument(
        "--yaml",
        help="Path to supported_nodes.yaml (for model license checking).",
    )
    parser.add_argument(
        "--name",
        help="Node pack name in supported_nodes.yaml (required with --yaml).",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Output results as JSON for machine consumption.",
    )
    parser.add_argument(
        "--no-transitive", action="store_true",
        help="Skip transitive dependency checking (faster, less thorough).",
    )

    args = parser.parse_args()
    repo_dir = os.path.abspath(args.repo_dir)

    if not os.path.isdir(repo_dir):
        msg = f"Not a directory: {repo_dir}"
        if args.json_output:
            print(json.dumps({"pass": False, "errors": [msg]}, indent=2))
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)

    if args.yaml and not args.name:
        parser.error("--name is required when --yaml is provided")

    if args.yaml and yaml is None:
        msg = "pyyaml is required for --yaml support. Install with: pip install pyyaml"
        if args.json_output:
            print(json.dumps({"pass": False, "errors": [msg]}, indent=2))
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)

    # 1. Check node repo license
    node_result = check_node_license(repo_dir)

    # 2. Check dependency licenses
    direct_deps = collect_node_dependencies(repo_dir)
    dep_results = check_all_dependencies(
        direct_deps, check_transitive=not args.no_transitive
    )

    # 3. Check model licenses (if YAML provided)
    model_results: List[CheckResult] = []
    if args.yaml and args.name:
        model_urls = collect_model_urls_from_yaml(args.yaml, args.name)
        for url in model_urls:
            result = check_model_license(url)
            if result is not None:
                model_results.append(result)

    # 4. Output
    if args.json_output:
        report = build_json_report(node_result, dep_results, model_results)
        print(json.dumps(report, indent=2))
    else:
        print_text_report(node_result, dep_results, model_results, direct_deps)

    # 5. Exit code
    all_results = [node_result] + dep_results + model_results
    has_blockers = any(r.status == BLOCK for r in all_results)
    sys.exit(1 if has_blockers else 0)


if __name__ == "__main__":
    main()
