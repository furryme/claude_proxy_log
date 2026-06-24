"""
dataset_archiver.py — Archive and compress cleaned datasets by time window.

Uses SQLite (logs/raw.db) as the data source.

Usage:
    python dataset_archiver.py                  # archive oldest window
    python dataset_archiver.py --date 2026-06-16 --hour 14
    python dataset_archiver.py --dry-run
    python dataset_archiver.py --keep-source
    python dataset_archiver.py --archive-all
    python dataset_archiver.py --before 2026-06-20_14    # archive everything before this hour
"""

from __future__ import annotations

import argparse
import io
import os
import shutil
import sys
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import zstandard as zstd

from log_store import DB_PATH, get_connection
from dataset_cleaner import clean_time_window

LOGS_ROOT = Path(os.environ.get("LOGS_ROOT", "logs"))
CLEANED_DIR = LOGS_ROOT / "cleaned"
ARCHIVES_DIR = LOGS_ROOT / "archives"


def _find_time_windows() -> list[str]:
    """Find all hour_keys in the DB that have entries."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT hour_key FROM entries ORDER BY hour_key"
        ).fetchall()
        return [r["hour_key"] for r in rows]
    finally:
        conn.close()


def _is_already_archived(hour_key: str) -> bool:
    archive_file = ARCHIVES_DIR / f"dataset_{hour_key}.tar.zst"
    return archive_file.exists()


def _clean(hour_key: str) -> Path | None:
    output_dir = CLEANED_DIR / hour_key
    print(f"[archiver] Cleaning {hour_key} -> {output_dir}", file=sys.stderr)
    try:
        stats = clean_time_window(DB_PATH, hour_key, output_dir)
        if stats["records_kept"] == 0:
            print("[archiver] No records kept, skipping", file=sys.stderr)
            if output_dir.exists():
                shutil.rmtree(output_dir, ignore_errors=True)
            return None
        return output_dir
    except Exception as e:
        print(f"[archiver] Cleaning failed: {e}", file=sys.stderr)
        return None


def _compress(cleaned_dir: Path) -> Path | None:
    key = cleaned_dir.name
    archive_file = ARCHIVES_DIR / f"dataset_{key}.tar.zst"
    ARCHIVES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[archiver] Compressing -> {archive_file}", file=sys.stderr)
    try:
        # Build tar into a buffer first
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            dataset_file = cleaned_dir / "dataset.jsonl"
            if dataset_file.exists():
                tar.add(dataset_file, arcname="dataset.jsonl")
            for f in cleaned_dir.glob("*.json"):
                tar.add(f, arcname=f.name)
        tar_data = buf.getvalue()
        # Compress with zstd
        cctx = zstd.ZstdCompressor(level=19)
        compressed = cctx.compress(tar_data)
        archive_file.write_bytes(compressed)
        # Verify by decompressing and checking tar contents
        dctx = zstd.ZstdDecompressor()
        decompressed = dctx.decompress(compressed)
        with tarfile.open(fileobj=io.BytesIO(decompressed), mode="r") as tar:
            members = tar.getnames()
        print(f"[archiver] Archive verified: {len(members)} files", file=sys.stderr)
        return archive_file
    except Exception as e:
        print(f"[archiver] Compression failed: {e}", file=sys.stderr)
        if archive_file.exists():
            archive_file.unlink(missing_ok=True)
        return None


def _delete_source(hour_key: str, cleaned_dir: Path, keep: bool = False):
    if keep:
        print("[archiver] Keeping source (requested)", file=sys.stderr)
        return
    # Delete from DB
    conn = get_connection()
    try:
        cur = conn.execute("DELETE FROM entries WHERE hour_key = ?", (hour_key,))
        deleted = cur.rowcount
        conn.commit()
        # VACUUM only if we deleted a significant number of rows
        if deleted > 1000:
            conn.execute("VACUUM")
        print(f"[archiver] Deleted {deleted} entries from DB for {hour_key}", file=sys.stderr)
    finally:
        conn.close()
    # Delete cleaned dir
    if cleaned_dir.exists():
        shutil.rmtree(cleaned_dir, ignore_errors=True)
        print(f"[archiver] Deleted cleaned dir: {cleaned_dir}", file=sys.stderr)


def _retain_archives(days: int = 0):
    if days <= 0 or not ARCHIVES_DIR.exists():
        return
    cutoff = datetime.now() - timedelta(days=days)
    for archive in ARCHIVES_DIR.glob("dataset_*.tar.zst"):
        try:
            name = archive.stem.replace("dataset_", "")
            date_str = name[:-3]
            archive_date = datetime.strptime(date_str, "%Y-%m-%d")
            if archive_date < cutoff:
                archive.unlink()
                print(f"[archiver] Deleted old archive: {archive}", file=sys.stderr)
        except (ValueError, IndexError):
            continue


def archive_window(
    date_str: str | None = None,
    hour_str: str | None = None,
    dry_run: bool = False,
    keep_source: bool = False,
    archive_oldest: bool = True,
    retain_days: int = 0,
    before: str | None = None,
):
    windows = _find_time_windows()
    if not windows:
        print("[archiver] No time windows found", file=sys.stderr)
        return

    if date_str and hour_str:
        targets = [f"{date_str}_{hour_str}"]
    elif before:
        targets = [w for w in windows if w < before]
        if not targets:
            print(f"[archiver] No windows before {before}", file=sys.stderr)
            return
    elif archive_oldest:
        targets = [w for w in windows if not _is_already_archived(w)]
        if not targets:
            print("[archiver] All windows already archived", file=sys.stderr)
            return
    else:
        targets = windows

    print(f"[archiver] Will process {len(targets)} window(s)", file=sys.stderr)

    for hour_key in targets:
        print(f"\n{'='*60}", file=sys.stderr)
        print(f"[archiver] Window: {hour_key}", file=sys.stderr)
        print(f"{'='*60}", file=sys.stderr)

        if dry_run:
            print(f"[archiver] [DRY RUN] Would archive {hour_key}", file=sys.stderr)
            continue

        cleaned_dir = _clean(hour_key)
        if cleaned_dir is None:
            continue

        archive_file = _compress(cleaned_dir)
        if archive_file is None:
            print("[archiver] Skipping deletion due to compression failure", file=sys.stderr)
            continue

        _delete_source(hour_key, cleaned_dir, keep_source)
        print(f"[archiver] ✓ Archived: {archive_file}", file=sys.stderr)

    _retain_archives(retain_days)


def main():
    parser = argparse.ArgumentParser(description="Archive cleaned datasets by time window")
    parser.add_argument("--date", help="Specific date (YYYY-MM-DD)")
    parser.add_argument("--hour", help="Specific hour (00-23)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-source", action="store_true")
    parser.add_argument("--archive-all", action="store_true")
    parser.add_argument("--before", help="Archive all windows before this hour_key (e.g. 2026-06-20_14)")
    parser.add_argument("--retain-days", type=int, default=0)
    args = parser.parse_args()

    keep_source = args.keep_source or os.environ.get("KEEP_SOURCE", "").lower() in ("1", "true", "yes")
    retain_days = args.retain_days or int(os.environ.get("RETAIN_DAYS", "0"))

    archive_window(
        date_str=args.date, hour_str=args.hour, dry_run=args.dry_run,
        keep_source=keep_source, archive_oldest=not args.archive_all and not args.before,
        retain_days=retain_days, before=args.before,
    )


if __name__ == "__main__":
    main()
