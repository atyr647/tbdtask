#!/usr/bin/env python3
"""Scan source directories for hardcoded secrets and credentials.

Checks for:
- AWS keys, tokens, passwords
- Hardcoded API keys and secrets
- Private key material
- Connection strings with embedded credentials

Usage:
    python tools/scan_secrets.py app/ tools/ alembic/
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Patterns that indicate hardcoded secrets.
# Each tuple: (compiled regex, human-readable description)
SECRET_PATTERNS = [
    (
        re.compile(r"(?i)(aws_access_key_id|aws_secret_access_key)\s*=\s*['\"][A-Z0-9]{16,}['\"]"),
        "AWS credentials",
    ),
    (
        re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"][^'\"]{8,}['\"]"),
        "Hardcoded password",
    ),
    (
        re.compile(r"(?i)(api_key|apikey|api_secret)\s*[:=]\s*['\"][a-zA-Z0-9_\-]{16,}['\"]"),
        "Hardcoded API key",
    ),
    (
        re.compile(r"(?i)(secret_key|secret)\s*[:=]\s*['\"][a-zA-Z0-9_\-]{16,}['\"]"),
        "Hardcoded secret key",
    ),
    (
        re.compile(r"(?i)postgresql://\w+:\w+@"),
        "Connection string with embedded credentials",
    ),
    (
        re.compile(r"-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----"),
        "Private key material",
    ),
    (
        re.compile(r"(?i)(token|bearer)\s*[:=]\s*['\"][a-zA-Z0-9_\-\.]{20,}['\"]"),
        "Hardcoded token",
    ),
]

# Files/directories to skip.
SKIP_PATTERNS = {
    "__pycache__",
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "*.pyc",
    "*.egg-info",
    "data/",
    "dist/",
    "build/",
}

# Known false positives — patterns that look like secrets but are safe.
# These are regex patterns matched against the full line.
FALSE_POSITIVES = [
    re.compile(r"# .*"),  # Comments
    re.compile(r"['\"]placeholder['\"]"),  # Placeholder strings
    re.compile(r"['\"]change[-_]me['\"]", re.IGNORECASE),  # "change-me" markers
    re.compile(r"['\"]dev[-_]secret['\"]", re.IGNORECASE),  # Dev-only markers
    re.compile(r"os\.environ"),  # Environment variable lookups
    re.compile(r"getenv"),  # Environment variable lookups
    re.compile(r"Form\("),  # FastAPI Form parameters
    re.compile(r"request\.form"),  # Form data access
]


def should_skip(path: Path) -> bool:
    """Check if a path should be skipped."""
    for pattern in SKIP_PATTERNS:
        if pattern.endswith("/"):
            if pattern.rstrip("/") in path.parts:
                return True
        elif path.name.endswith(pattern.lstrip("*")):
            return True
    return False


def is_false_positive(line: str) -> bool:
    """Check if a line is a known false positive."""
    for pattern in FALSE_POSITIVES:
        if pattern.search(line):
            return True
    return False


def scan_file(filepath: Path) -> list[tuple[str, int, str]]:
    """Scan a single file for secrets. Returns list of (filepath, line_num, description)."""
    findings = []
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
    except (PermissionError, UnicodeDecodeError):
        return findings

    for line_num, line in enumerate(content.splitlines(), 1):
        if is_false_positive(line):
            continue
        for pattern, description in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append((str(filepath), line_num, description))
                break  # One finding per line max

    return findings


def scan_directories(directories: list[str]) -> list[tuple[str, int, str]]:
    """Scan multiple directories for secrets."""
    all_findings = []
    for directory in directories:
        root = Path(directory)
        if not root.exists():
            print(f"[warn] Directory not found: {directory}", file=sys.stderr)
            continue

        for filepath in root.rglob("*"):
            if filepath.is_file() and not should_skip(filepath):
                findings = scan_file(filepath)
                all_findings.extend(findings)

    return all_findings


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python tools/scan_secrets.py <dir1> [dir2] ...", file=sys.stderr)
        sys.exit(1)

    directories = sys.argv[1:]
    findings = scan_directories(directories)

    if findings:
        print(f"Found {len(findings)} potential secret(s):", file=sys.stderr)
        for filepath, line_num, description in findings:
            print(f"  {filepath}:{line_num} — {description}", file=sys.stderr)
        sys.exit(1)
    else:
        print("No hardcoded secrets detected.")


if __name__ == "__main__":
    main()
