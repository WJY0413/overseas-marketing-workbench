"""Native LinkedIn storage and read-only validation of operator-selected libraries."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
from threading import RLock

from app.services.linkedin_connections import LinkedInConnectionError
from app.services.linkedin_master_seed import _create_schema


LIBRARY_LOCK = RLock()


def library_token(path: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(path.resolve())).encode('utf-8')).hexdigest()


def library_info(settings) -> dict:
    path = settings.linkedin_master_path.resolve()
    native = (getattr(settings, 'data_dir', Path('data')) / 'linkedin_people_master.sqlite').resolve()
    mode = 'custom' if getattr(settings, 'linkedin_master_sqlite_path', None) or path != native else 'native'
    return {'mode': mode, 'label': '指定路径库' if mode == 'custom' else '内置库',
            'path': str(path), 'native_path': str(native), 'exists': path.is_file(),
            'token': library_token(path), 'locked': 'LINKEDIN_MASTER_SQLITE_PATH' in os.environ}


def ensure_native_library(path: Path) -> None:
    """Initialize only a new native file; an existing library is never replaced."""
    with LIBRARY_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(path)) as connection:
                _create_schema(connection)
                connection.commit()
        except Exception:
            path.unlink()
            raise


def validate_library(path: Path) -> None:
    """Check compatibility without creating, migrating or writing the chosen file."""
    if not path.is_file():
        raise LinkedInConnectionError('库文件不存在；请选择已有的 Workbench LinkedIn SQLite 库。')
    try:
        with closing(sqlite3.connect(':memory:')) as schema, closing(
            sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
        ) as connection:
            connection.execute('PRAGMA query_only=ON')
            _create_schema(schema)
            for (table,) in schema.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                expected = {r[1] for r in schema.execute(f'PRAGMA table_info("{table}")')}
                actual = {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
                if not expected.issubset(actual):
                    raise LinkedInConnectionError(f'库结构不兼容：{table} 缺少所需字段，当前库未切换。')
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise LinkedInConnectionError('库完整性检查未通过，当前库未切换。')
    except sqlite3.Error as exc:
        raise LinkedInConnectionError('无法读取该 SQLite 库，当前库未切换。') from exc
