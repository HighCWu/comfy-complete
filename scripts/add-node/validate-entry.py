#!/usr/bin/env python3
"""Validate a supported_nodes.yaml entry for correctness.

Run this BEFORE submitting a PR to ensure your node pack entry is well-formed.

Examples:
    # Validate a registry node pack
    python scripts/add-node/validate-entry.py --yaml supported_nodes.yaml --name comfyui-kjnodes

    # Validate a GitHub-pinned node pack
    python scripts/add-node/validate-entry.py --yaml supported_nodes.yaml \\
        --name 'https://github.com/kijai/ComfyUI-KwaiKolorsWrapper@6fc1cd9d20bb7537facf180e5494b486b9710e24'

    # Machine-readable output
    python scripts/add-node/validate-entry.py --yaml supported_nodes.yaml --name comfyui-kjnodes --json
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error

try:
    import yaml
except ImportError:
    print("Error: pyyaml is required. Install with: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

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


# A GitHub URL name must match: https://github.com/org/repo@<40-hex-char-sha>
# We also allow tag refs like @v2.0.0 for flexibility (some entries use them).
GITHUB_URL_RE = re.compile(
    r'^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+@.+$'
)
GITHUB_SHA_RE = re.compile(
    r'^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+@[0-9a-f]{40}$'
)
GITHUB_TAG_RE = re.compile(
    r'^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+@v?[0-9].*$'
)

# PEP 508 simplified: package name with optional extras and version specifiers
PIP_REQ_RE = re.compile(
    r'^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?(\[[A-Za-z0-9,._-]+\])?\s*'
    r'([<>=!~]+\s*[A-Za-z0-9.*+!-]+(\s*,\s*[<>=!~]+\s*[A-Za-z0-9.*+!-]+)*)?$'
)


def load_yaml(path):
    """Load and return the YAML data from the given path."""
    with open(path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def find_pack(node_packs, name):
    """Find a node pack by name. Returns the pack dict or None."""
    for pack in node_packs:
        if pack.get('name') == name:
            return pack
    return None


def check_registry_name(name, errors, warnings):
    """Check if a registry name resolves via the Comfy API."""
    url = f"https://api.comfy.org/nodes/{name}"
    try:
        req = urllib.request.Request(url, method='GET')
        req.add_header('User-Agent', 'comfy-complete-validate/1.0')
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            # The API returns a JSON object; a valid node has an id or name field
            if not data or (isinstance(data, dict) and data.get('error')):
                errors.append(
                    f"Registry name '{name}' returned an error from api.comfy.org: "
                    f"{data.get('error', 'unknown error')}"
                )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            errors.append(
                f"Registry name '{name}' not found on api.comfy.org (HTTP 404). "
                f"Check the name matches the Comfy Registry."
            )
        else:
            warnings.append(
                f"Could not verify registry name '{name}': HTTP {e.code}. "
                f"The registry may be temporarily unavailable."
            )
    except (urllib.error.URLError, OSError) as e:
        warnings.append(
            f"Could not reach api.comfy.org to verify '{name}': {e}. "
            f"Skipping registry validation (network unavailable)."
        )


def check_github_url(name, errors, warnings):
    """Validate a GitHub URL format."""
    if not GITHUB_URL_RE.match(name):
        errors.append(
            f"GitHub URL '{name}' has invalid format. "
            f"Expected: https://github.com/org/repo@<40-char-sha> or @<tag>"
        )
        return

    if GITHUB_SHA_RE.match(name):
        return  # Valid SHA pinning

    if GITHUB_TAG_RE.match(name):
        warnings.append(
            f"GitHub URL '{name}' uses a tag ref instead of a commit SHA. "
            f"Tags can be moved; prefer pinning to a 40-character commit SHA."
        )
        return

    # Has @ but not a valid SHA or tag
    ref = name.split('@', 1)[1]
    errors.append(
        f"GitHub URL ref '{ref}' is not a 40-character hex SHA. "
        f"Expected format: https://github.com/org/repo@<40-hex-chars>"
    )


def is_github_url(name):
    """Check if a name looks like a GitHub URL."""
    return name.startswith('https://github.com/')


def validate_pack(pack, declared_labels, errors, warnings):
    """Validate a single node pack entry."""
    name = pack.get('name')
    if not name:
        errors.append("Pack is missing a 'name' field.")
        return

    # --- Name validation ---
    if name == 'core':
        pass  # Built-in core pack, no external validation needed
    elif is_github_url(name):
        check_github_url(name, errors, warnings)
    else:
        check_registry_name(name, errors, warnings)

    # --- Version field ---
    # version must be present as a key (value can be empty string)
    # Exception: 'core' pack and packs without version key that use GitHub URLs
    # are acceptable (GitHub URL includes the pin)
    if name != 'core':
        if 'version' not in pack and not is_github_url(name):
            errors.append(
                f"Pack '{name}' is missing the 'version' field. "
                f"Add version: \"<version>\" (can be empty string \"\" for GitHub-pinned packs)."
            )

    # --- node_labels validation ---
    node_labels = pack.get('node_labels')
    if node_labels is not None:
        if not isinstance(node_labels, dict):
            errors.append(
                f"Pack '{name}': 'node_labels' must be a mapping of node_name -> list of labels."
            )
        else:
            for node_name, labels in node_labels.items():
                # Labels must be a list
                if not isinstance(labels, list):
                    errors.append(
                        f"Pack '{name}', node '{node_name}': labels must be a list, "
                        f"got {type(labels).__name__}."
                    )
                    continue

                # Each label must be a string
                for label in labels:
                    if not isinstance(label, str):
                        errors.append(
                            f"Pack '{name}', node '{node_name}': label {label!r} "
                            f"is not a string."
                        )
                        continue

                    # Label must be declared
                    if label not in declared_labels:
                        errors.append(
                            f"Pack '{name}', node '{node_name}': label '{label}' "
                            f"is not in the declared labels list. "
                            f"Valid labels: {', '.join(sorted(declared_labels))}"
                        )

                # Check for duplicate labels
                seen = set()
                for label in labels:
                    if not isinstance(label, str):
                        continue
                    if label in seen:
                        errors.append(
                            f"Pack '{name}', node '{node_name}': duplicate label '{label}'."
                        )
                    seen.add(label)

    # --- dependency_overrides validation ---
    dep_overrides = pack.get('dependency_overrides')
    if dep_overrides is not None:
        if not isinstance(dep_overrides, list):
            errors.append(
                f"Pack '{name}': 'dependency_overrides' must be a list of pip requirements."
            )
        else:
            for dep in dep_overrides:
                if not isinstance(dep, str):
                    errors.append(
                        f"Pack '{name}': dependency_override {dep!r} is not a string."
                    )
                    continue
                # Allow common pip formats: name, name==ver, name>=ver, name[extra]>=ver
                # Also allow URLs and git+ prefixes
                if dep.startswith(('git+', 'http://', 'https://')):
                    continue  # URL-based requirements are fine
                if not PIP_REQ_RE.match(dep.strip()):
                    errors.append(
                        f"Pack '{name}': dependency_override '{dep}' does not look like "
                        f"a valid pip requirement (e.g. 'torch>=2.0', 'numpy==1.24.0')."
                    )

    # --- models validation ---
    models = pack.get('models')
    if models is not None:
        if not isinstance(models, list):
            errors.append(
                f"Pack '{name}': 'models' must be a list of model definitions."
            )
        else:
            required_model_fields = {'name', 'url', 'directory'}
            for i, model in enumerate(models):
                if not isinstance(model, dict):
                    errors.append(
                        f"Pack '{name}': models[{i}] must be a mapping with "
                        f"name, url, and directory fields."
                    )
                    continue
                missing = required_model_fields - set(model.keys())
                if missing:
                    errors.append(
                        f"Pack '{name}': models[{i}] is missing required fields: "
                        f"{', '.join(sorted(missing))}."
                    )


def main():
    parser = argparse.ArgumentParser(
        description='Validate a supported_nodes.yaml entry for correctness.',
        epilog='''Examples:
  %(prog)s --yaml supported_nodes.yaml --name comfyui-kjnodes
  %(prog)s --yaml supported_nodes.yaml --name comfyui-kjnodes --json
  %(prog)s --yaml supported_nodes.yaml --name 'https://github.com/org/repo@abc123...'
        ''',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--yaml', required=True,
        help='Path to supported_nodes.yaml',
    )
    parser.add_argument(
        '--name', required=True,
        help='Node pack name to validate (registry name or GitHub URL)',
    )
    parser.add_argument(
        '--json', action='store_true', dest='json_output',
        help='Output results as JSON for machine consumption',
    )

    args = parser.parse_args()

    # Load YAML
    try:
        data = load_yaml(args.yaml)
    except FileNotFoundError:
        msg = f"File not found: {args.yaml}"
        if args.json_output:
            print(json.dumps({'pass': False, 'errors': [msg], 'warnings': []}, indent=2))
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)
    except yaml.YAMLError as e:
        msg = f"Invalid YAML in {args.yaml}: {e}"
        if args.json_output:
            print(json.dumps({'pass': False, 'errors': [msg], 'warnings': []}, indent=2))
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)

    # Extract declared labels
    declared_labels = set(data.get('labels', []))
    if not declared_labels:
        msg = "No 'labels' list found at top level of YAML file."
        if args.json_output:
            print(json.dumps({'pass': False, 'errors': [msg], 'warnings': []}, indent=2))
        else:
            print(f"FAIL: {msg}", file=sys.stderr)
        sys.exit(1)

    # Find the pack
    node_packs = data.get('node_packs', [])
    pack = find_pack(node_packs, args.name)

    if pack is None:
        msg = (
            f"Pack '{args.name}' not found in node_packs list. "
            f"Make sure the name matches exactly (including quotes for GitHub URLs)."
        )
        if args.json_output:
            print(json.dumps({'pass': False, 'errors': [msg], 'warnings': []}, indent=2))
        else:
            print(f"FAIL: {msg}")
        sys.exit(1)

    errors = []
    warnings = []
    validate_pack(pack, declared_labels, errors, warnings)

    passed = len(errors) == 0

    if args.json_output:
        result = {
            'pass': passed,
            'name': args.name,
            'errors': errors,
            'warnings': warnings,
        }
        print(json.dumps(result, indent=2))
    else:
        print(f"Validating: {args.name}")
        print()

        if errors:
            print("ERRORS:")
            for err in errors:
                print(f"  \u2717 {err}")
            print()

        if warnings:
            print("WARNINGS:")
            for warn in warnings:
                print(f"  \u26a0 {warn}")
            print()

        if passed:
            if warnings:
                print(f"PASS (with {len(warnings)} warning(s))")
            else:
                print("PASS")
        else:
            print(f"FAIL ({len(errors)} error(s), {len(warnings)} warning(s))")

    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
