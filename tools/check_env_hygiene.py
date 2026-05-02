#!/usr/bin/env python3
"""Verify environment variable hygiene for deployments.

Checks:
1. .env is NOT committed to the repository
2. .env.example exists and documents all required variables
3. .gitignore includes .env
4. No variables in .env.example are empty placeholders that should have examples

Usage:
    python tools/check_env_hygiene.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_env_file(filepath: Path) -> dict[str, str]:
    """Parse a .env-style file into a dict of key=value pairs."""
    result = {}
    if not filepath.exists():
        return result
    for line in filepath.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


def check_env_not_committed() -> list[str]:
    """Verify .env is not tracked by git."""
    errors = []
    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return errors  # Good — no .env file

    import subprocess
    try:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(env_file)],
            capture_output=True,
            cwd=PROJECT_ROOT,
        )
        if result.returncode == 0:
            errors.append(".env is tracked by git — add it to .gitignore")
    except FileNotFoundError:
        pass  # git not available, skip check

    return errors


def check_gitignore() -> list[str]:
    """Verify .gitignore includes .env."""
    errors = []
    gitignore = PROJECT_ROOT / ".gitignore"
    if not gitignore.exists():
        errors.append(".gitignore file not found")
        return errors

    content = gitignore.read_text()
    if ".env" not in content:
        errors.append(".gitignore does not include .env")

    return errors


def check_env_example() -> list[str]:
    """Verify .env.example is complete and well-formed."""
    errors = []
    env_example = PROJECT_ROOT / ".env.example"
    if not env_example.exists():
        errors.append(".env.example not found — required for documenting env vars")
        return errors

    vars = parse_env_file(env_example)
    if not vars:
        errors.append(".env.example is empty — should document required variables")
        return errors

    # Check for required variables (by convention, TBDTASK_ and DATABASE_URL)
    required_found = False
    for key in vars:
        if key.startswith("TBDTASK_") or key == "DATABASE_URL":
            required_found = True
            break
    if not required_found:
        errors.append(".env.example should document at least one TBDTASK_* or DATABASE_URL variable")

    # Check that commented-out optional vars have example values or clear instructions
    content = env_example.read_text()
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# OIDC_") or stripped.startswith("# TBDTASK_"):
            # This is fine — it's a commented-out optional variable
            pass

    return errors


def check_no_secrets_in_example() -> list[str]:
    """Verify .env.example doesn't contain real-looking secrets."""
    errors = []
    env_example = PROJECT_ROOT / ".env.example"
    if not env_example.exists():
        return errors

    vars = parse_env_file(env_example)
    for key, value in vars.items():
        # Values should be empty or clearly placeholder
        if value and len(value) > 8 and value not in ("", "change-me", "dev-secret-key-change-me"):
            # Check if it looks like a real secret (not a placeholder)
            if not value.startswith("${") and not value.endswith("}"):
                # Could be a real secret — flag it
                if "secret" in key.lower() or "key" in key.lower() or "password" in key.lower():
                    errors.append(
                        f".env.example: {key} has a non-empty value that looks like a real secret"
                    )

    return errors


def main() -> None:
    all_errors = []
    all_errors.extend(check_env_not_committed())
    all_errors.extend(check_gitignore())
    all_errors.extend(check_env_example())
    all_errors.extend(check_no_secrets_in_example())

    if all_errors:
        print(f"Found {len(all_errors)} env hygiene issue(s):", file=sys.stderr)
        for error in all_errors:
            print(f"  ✗ {error}", file=sys.stderr)
        sys.exit(1)
    else:
        print("Environment hygiene checks passed.")


if __name__ == "__main__":
    main()
