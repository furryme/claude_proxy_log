#!/usr/bin/env python3
"""
archive_cron.py — Schedule periodic archival via cron.

Generates a crontab entry and installs it. The archival runs on a schedule
you choose (default: every 4 hours).

Usage:
    # Show the cron command that would be installed
    python archive_cron.py --show

    # Install the cron job (default: every 4 hours)
    python archive_cron.py --install

    # Install with custom interval
    python archive_cron.py --install --minute 0 --hour "*/6"

    # Remove the cron job
    python archive_cron.py --remove

    # Run a one-shot archive immediately (for testing)
    python archive_cron.py --run-now

Default behavior:
    - Archives the oldest non-archived time window each run
    - Keeps cleaned data (use --no-keep-source to delete after archive)
    - Logs to a cron log file
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ARCHIVER = SCRIPT_DIR / "dataset_archiver.py"
PYTHON = sys.executable or "python3"
LOG_FILE = SCRIPT_DIR / "logs" / "archive_cron.log"

CRON_IDENTIFIER = "# dataset-archiver"


def _build_command(extra_args: str = "") -> str:
    LOGS_ROOT = SCRIPT_DIR / "logs"
    return (
        f"cd {SCRIPT_DIR} && "
        f"{PYTHON} {ARCHIVER} {extra_args} "
        f"1>>{LOG_FILE} 2>&1"
    )


def _build_cron_line(minute: str, hour: str, extra_args: str = "") -> str:
    cmd = _build_command(extra_args)
    return f"{minute} {hour} * * * {cmd}  # {CRON_IDENTIFIER}"


def show(minute: str = "0", hour: str = "*/4"):
    """Show the cron line that would be installed."""
    line = _build_cron_line(minute, hour)
    print(f"Cron line (every 4 hours by default):")
    print(f"  {line}")
    print()
    print(f"Command:")
    print(f"  {_build_command()}")


def install(minute: str = "0", hour: str = "*/4", extra_args: str = ""):
    """Install the cron job."""
    line = _build_cron_line(minute, hour, extra_args)

    # Ensure log directory exists
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Get current crontab
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        body = result.stdout if result.returncode == 0 else ""
    except FileNotFoundError:
        print("Error: `crontab` not found. Install it manually:")
        print(f"  {line}")
        return

    # Remove old entry if exists
    lines = body.split("\n")
    lines = [l for l in lines if CRON_IDENTIFIER not in l]
    new_crontab = "\n".join(lines) + "\n" + line + "\n"

    # Install
    subprocess.run(
        ["crontab", "-"],
        input=new_crontab, text=True, timeout=10,
    )
    print(f"✓ Cron job installed:")
    print(f"  {line}")
    print()
    print(f"Log file: {LOG_FILE}")
    print(f"View crontab: crontab -l")
    print(f"Remove:       python archive_cron.py --remove")


def remove():
    """Remove the cron job."""
    try:
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True, text=True, timeout=10,
        )
        body = result.stdout if result.returncode == 0 else ""
    except FileNotFoundError:
        print("Error: `crontab` not found.")
        return

    lines = body.split("\n")
    lines = [l for l in lines if CRON_IDENTIFIER not in l]
    new_crontab = "\n".join(lines) + "\n"

    subprocess.run(
        ["crontab", "-"],
        input=new_crontab, text=True, timeout=10,
    )
    print("✓ Cron job removed")


def run_now():
    """Run the archiver immediately (for testing)."""
    print(f"Running: {_build_command()}", file=sys.stderr)
    subprocess.run([PYTHON, str(ARCHIVER)])


def main():
    parser = argparse.ArgumentParser(description="Schedule periodic dataset archival")
    parser.add_argument("--show", action="store_true", help="Show the cron line")
    parser.add_argument("--install", action="store_true", help="Install the cron job")
    parser.add_argument("--remove", action="store_true", help="Remove the cron job")
    parser.add_argument("--run-now", action="store_true", help="Run archival immediately")
    parser.add_argument("--minute", default="0", help="Cron minute (default: 0)")
    parser.add_argument("--hour", default="*/4", help="Cron hour (default: */4 = every 4 hours)")
    parser.add_argument("--extra-args", default="", help="Extra args for archiver")
    args = parser.parse_args()

    if args.show:
        show(args.minute, args.hour)
    elif args.install:
        install(args.minute, args.hour, args.extra_args)
    elif args.remove:
        remove()
    elif args.run_now:
        run_now()
    else:
        # Default: show
        show(args.minute, args.hour)
        print("\nUse --install to set up the cron job.")


if __name__ == "__main__":
    main()
