"""Automated local rotating SQLite backups with WAL-safe snapshot & restore verification."""

import argparse
import datetime
import gzip
import hashlib
import os
import shutil
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ops.sqlite_utils import (
    create_verified_snapshot,
    get_db_path,
    verify_gzip_restore,
)
from ops.telegram_ops import send_alert

DEFAULT_BACKUP_ROOT = '/root/st_jerome_backups'
RETENTION = {
    'hourly': 24,
    'daily': 30,
    'snapshot': 0,  # Keep tagged snapshots indefinitely
}


def get_backup_root(override=None):
    if override:
        return os.path.abspath(override)
    env_root = os.environ.get('STJEROME_BACKUP_DIR', '').strip()
    return os.path.abspath(env_root) if env_root else DEFAULT_BACKUP_ROOT


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _prune_old(target_dir, keep_count):
    if not keep_count or keep_count <= 0:
        return []
    entries = sorted(
        f for f in os.listdir(target_dir) if f.endswith('.db.gz')
    )
    removed = []
    while len(entries) > keep_count:
        oldest = entries.pop(0)
        gz_path = os.path.join(target_dir, oldest)
        sha_path = gz_path + '.sha256'
        for p in (gz_path, sha_path):
            if os.path.exists(p):
                os.remove(p)
        removed.append(oldest)
    return removed


def run_backup(mode='hourly', tag=None, verify_restore=True, backup_root=None, db_path=None):
    """Create a compressed WAL-safe backup, verify restore integrity, and prune old copies."""
    if mode not in RETENTION:
        raise ValueError(f"Unsupported backup mode: {mode}")

    src_db = db_path or get_db_path()
    root_dir = get_backup_root(backup_root)
    sub_dir = os.path.join(root_dir, 'snapshots' if mode == 'snapshot' else mode)
    os.makedirs(sub_dir, exist_ok=True)

    now = datetime.datetime.now()
    if mode == 'daily':
        stamp = now.strftime('%Y%m%d_%H%M%S')
        base_name = f"katalog_daily_{stamp}.db.gz"
    elif mode == 'snapshot':
        safe_tag = ''.join(ch if (ch.isalnum() or ch in '-_') else '_' for ch in (tag or 'manual'))
        stamp = now.strftime('%Y%m%d_%H%M%S')
        base_name = f"katalog_{safe_tag}_{stamp}.db.gz"
    else:
        stamp = now.strftime('%Y%m%d_%H%M%S')
        base_name = f"katalog_hourly_{stamp}.db.gz"

    final_gz = os.path.join(sub_dir, base_name)
    final_sha = final_gz + '.sha256'

    try:
        with tempfile.TemporaryDirectory(prefix='stjerome_local_bak_') as tmpdir:
            snap_db = os.path.join(tmpdir, 'katalog.db')
            tmp_gz = os.path.join(tmpdir, base_name)

            raw_bytes, counts = create_verified_snapshot(src_db, snap_db)
            with open(snap_db, 'rb') as sf, gzip.open(tmp_gz, 'wb', compresslevel=6) as gz:
                shutil.copyfileobj(sf, gz)

            if verify_restore:
                restored_counts = verify_gzip_restore(tmp_gz)
                if restored_counts != counts:
                    raise RuntimeError(
                        f"Restore count mismatch: {restored_counts} != {counts}"
                    )

            digest = _sha256_file(tmp_gz)
            gz_bytes = os.path.getsize(tmp_gz)

            # Atomically move compressed archive and checksum into target directory
            stage_gz = final_gz + '.tmp'
            shutil.copy2(tmp_gz, stage_gz)
            os.replace(stage_gz, final_gz)
            with open(final_sha, 'w', encoding='utf-8') as sf:
                sf.write(f"{digest}  {base_name}\n")

        pruned = _prune_old(sub_dir, RETENTION[mode])
        return {
            'status': 'ok',
            'mode': mode,
            'path': final_gz,
            'sha256': digest,
            'raw_bytes': raw_bytes,
            'gz_bytes': gz_bytes,
            'counts': counts,
            'pruned': pruned,
        }
    except Exception as e:
        send_alert(
            f"Local DB Backup Failed ({mode})",
            f"Source: {src_db}\nTarget: {final_gz}\nError: {e}",
            level='CRITICAL',
            dedup_key=f"local_backup_fail:{mode}",
            cooldown_sec=300,
        )
        raise


def get_latest_backup_info(backup_root=None):
    """Return path, age_seconds, and size of the newest local verified .db.gz backup."""
    root_dir = get_backup_root(backup_root)
    candidates = []
    for sub in ('hourly', 'daily', 'snapshots'):
        d = os.path.join(root_dir, sub)
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            if fname.endswith('.db.gz'):
                fpath = os.path.join(d, fname)
                candidates.append((os.path.getmtime(fpath), fpath, os.path.getsize(fpath)))

    if not candidates:
        return None
    mtime, fpath, size = max(candidates, key=lambda x: x[0])
    import time
    return {
        'path': fpath,
        'mtime': mtime,
        'age_seconds': max(0.0, time.time() - mtime),
        'size_bytes': size,
    }


def main():
    parser = argparse.ArgumentParser(description='St. Jerome local rotating database backup')
    parser.add_argument('--mode', choices=['hourly', 'daily', 'snapshot'], default='hourly')
    parser.add_argument('--tag', default=None, help='Tag name when --mode snapshot')
    parser.add_argument('--no-verify-restore', action='store_true')
    args = parser.parse_args()

    res = run_backup(
        mode=args.mode,
        tag=args.tag,
        verify_restore=not args.no_verify_restore,
    )
    print(
        f"[OK] mode={res['mode']} file={res['path']} "
        f"raw={res['raw_bytes']} gz={res['gz_bytes']} "
        f"counts={res['counts']} pruned={len(res['pruned'])}",
        flush=True,
    )


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f"[FAILED] {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
