"""Offline SQLite restore, with a validated source and automatic pre-restore backup."""
import argparse
import sqlite3
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from launch import acquire_lock
from modules.storage import backup_database


def restore(source,target):
    if source.resolve()==target.resolve(): raise ValueError('备份和目标不能为同一文件')
    if not source.is_file(): raise ValueError('备份文件不存在')
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as incoming:
        if incoming.execute('PRAGMA quick_check').fetchone()[0]!='ok': raise ValueError('备份完整性检查失败')
        tables={r[0] for r in incoming.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'review_records','game_profiles'}<=tables: raise ValueError('这不是本工具的备份文件')
    incoming.close()
    prior=backup_database(target) if target.exists() else None
    incoming=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    destination=sqlite3.connect(target)
    try: incoming.backup(destination)
    finally: incoming.close(); destination.close()
    return prior


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='先退出工具，再恢复备份；当前库会先自动备份。')
    parser.add_argument('backup',type=Path)
    args=parser.parse_args()
    data=ROOT/'data'; data.mkdir(exist_ok=True)
    lock=acquire_lock(data/'application.lock')
    if lock is None: raise SystemExit('工具仍在运行，请退出后恢复。')
    try:
        prior=restore(args.backup.resolve(),data/'reviews.sqlite3')
        print('恢复完成；恢复前备份：',prior)
    finally: lock.close()
