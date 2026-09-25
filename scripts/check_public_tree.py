"""Fail CI if tracked files contain runtime state or recognizable credentials."""

from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
DENIED_NAMES = {
    ".env",
    ".DS_Store",
    "CLAUDE.md",
    "CODEX.md",
    "GEMINI.md",
}
DENIED_SUFFIXES = {
    ".db", ".sqlite", ".sqlite3", ".key", ".pem", ".p12",
    ".log", ".zip", ".tar", ".blend", ".blend1",
}
DENIED_DIRS = {"data", ".scratch", "__pycache__", ".venv"}
SECRET_PATTERNS = {
    "Telegram bot token": re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "GitHub token": re.compile(rb"\b(?:ghp_|gho_|ghu_|github_pat_)[A-Za-z0-9_]{20,}"),
    "OpenAI key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    "private key": re.compile(b"-----BEGIN " + b"PRIVATE KEY-----"),
    "local home path": re.compile(b"/" + rb"Users/[^/\s]+/"),
}


def tracked_files():
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [Path(p.decode()) for p in output.split(b"\0") if p]


def main():
    errors = []
    for relative in tracked_files():
        path = ROOT / relative
        if not path.is_file():
            continue
        if (relative.name in DENIED_NAMES or relative.suffix in DENIED_SUFFIXES
                or any(part in DENIED_DIRS for part in relative.parts)):
            errors.append(f"{relative}: forbidden file")
            continue
        data = path.read_bytes()
        if data.startswith(b"SQLite format 3"):
            errors.append(f"{relative}: SQLite database")
        for description, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                errors.append(f"{relative}: {description}")
    if errors:
        print("Public tree check failed:\n" + "\n".join(errors), file=sys.stderr)
        return 1
    print(f"Public tree check passed ({len(tracked_files())} tracked files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
