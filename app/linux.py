"""Single-process Linux entrypoint; run from the Workbench installation directory."""

import argparse
import atexit
import os
from pathlib import Path, PureWindowsPath
import sys

from app.config import get_settings


def database_path(settings) -> Path:
    from sqlalchemy.engine import make_url

    url = make_url(settings.resolved_database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise ValueError("Linux runtime requires a persistent SQLite database file")
    if url.query.get("uri", "").lower() == "true" or url.database.startswith("file:"):
        raise ValueError("Use a normal SQLite file path, not SQLite URI mode")
    for value in (url.database, settings.workbench_data_dir,
                  settings.linkedin_master_sqlite_path, settings.bd_database_sqlite_path,
                  settings.bd_database_json_path):
        if value and (PureWindowsPath(value).drive or "\\" in value):
            raise ValueError("Replace Windows paths with Linux paths before starting Workbench")
    # SQLAlchemy treats '~' literally in a database URL. Lock the same file it opens.
    return Path(url.database).resolve()


def acquire_instance_lock(database: Path):
    import fcntl

    database.parent.mkdir(parents=True, exist_ok=True)
    handle = database.with_name(database.name + ".runtime.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("Another Workbench Linux process already owns this database") from None
    # Never unlink this file: that would allow two processes to lock different inodes.
    return handle


def main() -> None:
    if not sys.platform.startswith("linux"):
        raise SystemExit("Use the Windows launcher on Windows; this entrypoint requires Linux")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.umask(0o077)
    settings = get_settings()
    try:
        path = database_path(settings)
        handle = acquire_instance_lock(path)
    except (ValueError, OSError, RuntimeError) as exc:
        raise SystemExit(str(exc)) from exc
    # Keep the lock until interpreter exit, including any finishing SMTP worker.
    atexit.register(handle.close)
    print(f"Workbench Linux: database={path}; dry_run_email={settings.dry_run_email}", flush=True)
    import uvicorn

    uvicorn.run("app.main:app", host=args.host, port=args.port, workers=1, reload=False)


if __name__ == "__main__":
    main()
