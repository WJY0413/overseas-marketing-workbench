from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


EXPECTED_SKILLS = (
    "workbench-first-run-check",
    "workbench-email-daily-runbook",
    "workbench-send-mail",
    "workbench-inbox-check",
    "email-bounce-suppression",
    "workbench-queue-builder",
    "workbench-parameter-tuner",
)
ARCHIVE_PARTS = {
    "backup",
    "backups",
    "code_backups",
    "history",
    "old",
    "output",
    "outputs",
    "release",
    "releases",
    "staging",
}
PLACEHOLDER_EMAILS = {"sender@example.com", "your@email.com"}
PLACEHOLDER_TEXT = {
    "your name",
    "your company",
    "your company address",
    "+1 555 0100",
    "+1 555 0101",
}


@dataclass(frozen=True)
class Installation:
    path: str
    version: str
    stateful: bool
    role: str


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return ""


def version_tuple(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", value or "")
    return tuple(int(part) for part in parts[:4]) or (0,)


def is_workbench_root(path: Path) -> bool:
    return (
        path.is_dir()
        and (path / "VERSION").is_file()
        and (path / "app" / "main.py").is_file()
        and ((path / "start.cmd").is_file() or (path / "start_production.cmd").is_file())
    )


def is_archived(path: Path) -> bool:
    return any(part.lower() in ARCHIVE_PARTS for part in path.parts)


def has_local_state(path: Path) -> bool:
    if (path / ".env").is_file() or (path / ".venv").is_dir():
        return True
    data_dir = path / "data"
    if not data_dir.is_dir():
        return False
    return any(data_dir.glob("*.db")) or any(data_dir.glob("*.sqlite"))


def installation_role(path: Path) -> str:
    database_url = parse_env_file(path / ".env").get("DATABASE_URL", "").lower()
    lowered_name = path.name.lower()
    if "test" in database_url or lowered_name in {"test", "testing", "dev", "development"}:
        return "test"
    return "production"


def walk_candidates(root: Path, max_depth: int) -> Iterable[Path]:
    if not root.is_dir():
        return
    root = root.resolve()
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if is_workbench_root(current):
            yield current
            continue
        if depth >= max_depth:
            continue
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir():
                continue
            lowered = child.name.lower()
            if lowered in {".git", ".venv", "appdata", "node_modules", "data", "__pycache__"}:
                continue
            if lowered in ARCHIVE_PARTS:
                continue
            stack.append((child, depth + 1))


def default_candidates() -> list[Path]:
    user_profile = Path(os.environ.get("USERPROFILE", str(Path.home())))
    values = [
        Path.cwd(),
        Path(r"C:\BD_Email_Workbench\production"),
        Path(r"C:\BD_Email_Workbench_Lite"),
        Path(r"<detected-workbench-root>"),
        user_profile / "BD_Email_Workbench" / "production",
        user_profile / "BD_Email_Workbench_Lite",
        user_profile / "Documents" / "bd-email-workbench-lite",
    ]
    workbench_home = os.environ.get("WORKBENCH_HOME", "").strip()
    if workbench_home:
        values.insert(0, Path(workbench_home))
    return values


def discover_installations(
    explicit: Iterable[Path], search_roots: Iterable[Path], max_depth: int, include_defaults: bool = True
) -> list[Installation]:
    candidates: dict[str, Path] = {}
    paths = [*explicit, *(default_candidates() if include_defaults else [])]
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if is_workbench_root(resolved) and not is_archived(resolved):
            candidates[str(resolved).lower()] = resolved
    for root in search_roots:
        for path in walk_candidates(root, max_depth):
            if not is_archived(path):
                candidates[str(path.resolve()).lower()] = path.resolve()
    return sorted(
        [
            Installation(
                path=str(path),
                version=read_text(path / "VERSION") or "unknown",
                stateful=has_local_state(path),
                role=installation_role(path),
            )
            for path in candidates.values()
            if has_local_state(path)
        ],
        key=lambda item: item.path.lower(),
    )


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in read_text(path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def find_database(root: Path) -> Path | None:
    env_values = parse_env_file(root / ".env")
    database_url = env_values.get("DATABASE_URL", "")
    match = re.match(r"sqlite:///([^?]+)", database_url)
    if match:
        candidate = Path(match.group(1))
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            return candidate.resolve()
    data_dir = root / "data"
    for name in ("workbench.db", "workbench_prod.db", "workbench_test.db"):
        candidate = data_dir / name
        if candidate.is_file():
            return candidate.resolve()
    if data_dir.is_dir():
        candidates = sorted(
            [*data_dir.glob("*.db"), *data_dir.glob("*.sqlite")],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0].resolve()
    return None


def open_read_only(database: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def sender_status(root: Path) -> tuple[bool, int, str]:
    database = find_database(root)
    if database is None:
        return False, 0, "database not initialized"
    env_values = parse_env_file(root / ".env")
    try:
        connection = open_read_only(database)
        connection.row_factory = sqlite3.Row
        if not table_exists(connection, "senderaccount"):
            return False, 0, "senderaccount table missing"
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(senderaccount)").fetchall()
        }
        encrypted_expr = (
            "COALESCE(smtp_password_encrypted, '')" if "smtp_password_encrypted" in columns else "''"
        )
        rows = connection.execute(
            "SELECT email, smtp_host, smtp_username, password_env, "
            f"{encrypted_expr} AS encrypted_password "
            "FROM senderaccount WHERE COALESCE(is_active, 1)=1"
        ).fetchall()
    except sqlite3.Error as exc:
        return False, 0, f"database read failed: {exc}"
    finally:
        if "connection" in locals():
            connection.close()

    ready = 0
    for row in rows:
        email = str(row["email"] or "").strip().lower()
        password_env = str(row["password_env"] or "").strip()
        credential = bool(str(row["encrypted_password"] or "").strip())
        if password_env:
            credential = credential or bool(os.environ.get(password_env) or env_values.get(password_env))
        if (
            email
            and email not in PLACEHOLDER_EMAILS
            and not email.endswith("@example.com")
            and str(row["smtp_host"] or "").strip().lower() not in {"", "localhost"}
            and str(row["smtp_username"] or "").strip()
            and credential
        ):
            ready += 1
    return ready > 0, ready, "ready sender count" if ready else "no usable active SMTP sender"


def signature_status(root: Path) -> tuple[bool, str]:
    database = find_database(root)
    if database is None:
        return False, "database not initialized"
    try:
        connection = open_read_only(database)
        if not table_exists(connection, "appsetting"):
            return False, "signature settings table missing"
        row = connection.execute(
            "SELECT value FROM appsetting WHERE key='email_signature_config' LIMIT 1"
        ).fetchone()
        active_template = False
        if table_exists(connection, "emailtemplate"):
            active_template = connection.execute(
                "SELECT 1 FROM emailtemplate "
                "WHERE template_type='signature' AND COALESCE(is_active, 1)=1 "
                "AND LENGTH(TRIM(COALESCE(body_html, ''))) > 0 LIMIT 1"
            ).fetchone() is not None
    except sqlite3.Error as exc:
        return False, f"database read failed: {exc}"
    finally:
        if "connection" in locals():
            connection.close()
    if row is None:
        return False, "personalized signature not saved"
    try:
        config = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return False, "signature configuration is invalid"
    required = [
        str(config.get("sender_name") or "").strip(),
        str(config.get("company_name") or "").strip(),
        str(config.get("default_email") or "").strip(),
    ]
    lowered = {item.lower() for item in required}
    if not all(required) or lowered.intersection(PLACEHOLDER_TEXT | PLACEHOLDER_EMAILS):
        return False, "signature still contains placeholder identity"
    return True, "personalized signature saved" + (" with template" if active_template else "")


def environment_status(root: Path, health_url: str) -> tuple[bool, str]:
    python_version = sys.version_info[:2]
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    if venv_python.is_file():
        try:
            probe = subprocess.run(
                [str(venv_python), "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            match = re.search(r"Python\s+(\d+)\.(\d+)", probe.stdout + probe.stderr)
            if match:
                python_version = (int(match.group(1)), int(match.group(2)))
        except (OSError, subprocess.SubprocessError):
            return False, "existing virtual-environment Python is not runnable"
    python_ok = python_version[0] == 3
    files_ok = (
        (root / "requirements.txt").is_file()
        and (root / "app" / "main.py").is_file()
        and ((root / "start.cmd").is_file() or (root / "start_production.cmd").is_file())
    )
    if not python_ok:
        return False, f"Python 3 is required; found {python_version[0]}.{python_version[1]}"
    if not files_ok:
        return False, "required launcher or application files missing"
    if health_url:
        try:
            with urllib.request.urlopen(health_url.rstrip("/") + "/health", timeout=3) as response:
                if response.status != 200:
                    return False, f"health check returned HTTP {response.status}"
        except Exception as exc:  # noqa: BLE001 - concise readiness result is intentional.
            return False, f"health check failed: {exc}"
    return True, "launch files and runnable Python 3 found"


def duplicate_skill_dirs(skills_root: Path, skill_name: str) -> list[str]:
    if not skills_root.is_dir():
        return []
    pattern = re.compile(
        rf"^{re.escape(skill_name)}(?:[ _.-]*(?:copy|old|backup)|\s*\(\d+\)|[ _.-]+\d+)$",
        flags=re.IGNORECASE,
    )
    return sorted(
        str(item)
        for item in skills_root.iterdir()
        if item.is_dir() and item.name != skill_name and pattern.match(item.name)
    )


def skills_status(skills_root: Path) -> tuple[str, list[str], list[str]]:
    missing = [
        name for name in EXPECTED_SKILLS if not (skills_root / name / "SKILL.md").is_file()
    ]
    duplicates = [
        path for name in EXPECTED_SKILLS for path in duplicate_skill_dirs(skills_root, name)
    ]
    if duplicates:
        return "FAIL", missing, duplicates
    if missing:
        return "WARN", missing, []
    return "PASS", [], []


def build_result(args: argparse.Namespace) -> dict:
    package_dir = Path(args.package_dir).resolve() if args.package_dir else Path.cwd().resolve()
    installations = discover_installations(
        [Path(item) for item in args.installation],
        [Path(item) for item in args.search_root],
        args.max_depth,
        not args.no_default_locations,
    )
    active_installations = [
        item for item in installations if args.include_test or item.role != "test"
    ]
    package_version = read_text(package_dir / "VERSION") or "unknown"
    skills_root = Path(args.skills_root).resolve()
    skill_state, missing_skills, duplicate_skills = skills_status(skills_root)

    if len(active_installations) > 1 or duplicate_skills:
        mode = "blocked"
        target = Path(active_installations[0].path) if len(active_installations) == 1 else package_dir
    elif len(active_installations) == 1:
        target = Path(active_installations[0].path)
        mode = (
            "upgrade"
            if version_tuple(active_installations[0].version) < version_tuple(package_version)
            else "current"
        )
    else:
        target = package_dir
        mode = "new"

    environment_ok, environment_note = environment_status(target, args.health_url)
    sender_ok, sender_count, sender_note = sender_status(target)
    signature_ok, signature_note = signature_status(target)

    if len(active_installations) > 1 or duplicate_skills:
        overall = "MULTIPLE_INSTALLS"
        next_action = "选择唯一主安装或移除重复 Skill 后重新检查；不要自动合并或删除。"
    elif not environment_ok:
        overall = "NEEDS_SETUP"
        next_action = "先修复 Python 或启动文件，再重新检查。"
    elif skill_state != "PASS":
        overall = "NEEDS_SETUP"
        next_action = "运行安装包中的 install_skills.cmd，然后重启 Codex。"
    elif not sender_ok:
        overall = "NEEDS_SETUP"
        next_action = "在发件邮箱页面配置并启用至少一个 SMTP 邮箱。"
    elif not signature_ok:
        overall = "NEEDS_SETUP"
        next_action = "在 Settings 中保存并预览个性化邮件签名。"
    else:
        overall = "READY"
        next_action = "可以开始创建营销任务；实际发送仍需单独确认。"

    return {
        "workbench_setup": overall,
        "install_mode": mode,
        "package_version": package_version,
        "target": str(target),
        "installations": [asdict(item) for item in installations],
        "active_installations": [asdict(item) for item in active_installations],
        "environment": {"status": "PASS" if environment_ok else "FAIL", "note": environment_note},
        "sender": {
            "status": "PASS" if sender_ok else "FAIL",
            "ready_count": sender_count,
            "note": sender_note,
        },
        "signature": {"status": "PASS" if signature_ok else "FAIL", "note": signature_note},
        "skills": {
            "status": skill_state,
            "root": str(skills_root),
            "missing": missing_skills,
            "duplicates": duplicate_skills,
        },
        "next_action": next_action,
    }


def print_summary(result: dict) -> None:
    print(f"WORKBENCH_SETUP: {result['workbench_setup']}")
    print(f"安装模式: {result['install_mode']}")
    print(f"运行环境: {result['environment']['status']}")
    print(f"发送邮箱: {result['sender']['status']}")
    print(f"邮件签名: {result['signature']['status']}")
    print(f"Skills: {result['skills']['status']}")
    print(f"下一步: {result['next_action']}")
    if result["workbench_setup"] == "MULTIPLE_INSTALLS":
        for item in result["active_installations"]:
            print(f"安装: {item['path']} | version={item['version']} | role={item['role']}")
        for path in result["skills"]["duplicates"]:
            print(f"重复 Skill: {path}")
    else:
        if result["environment"]["status"] == "FAIL":
            print(f"环境详情: {result['environment']['note']}")
        if result["sender"]["status"] == "FAIL":
            print(f"邮箱详情: {result['sender']['note']}")
        if result["signature"]["status"] == "FAIL":
            print(f"签名详情: {result['signature']['note']}")
        if result["skills"]["status"] != "PASS":
            print("缺少 Skills: " + ", ".join(result["skills"]["missing"]))


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only first-run check for 海外营销 Workbench.")
    parser.add_argument("--package-dir", default="")
    parser.add_argument("--installation", action="append", default=[])
    parser.add_argument("--search-root", action="append", default=[])
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--include-test", action="store_true")
    parser.add_argument("--no-default-locations", action="store_true")
    parser.add_argument(
        "--skills-root",
        default=str(Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".codex" / "skills"),
    )
    parser.add_argument("--health-url", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = build_result(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_summary(result)
    if result["workbench_setup"] == "READY":
        return 0
    if result["workbench_setup"] == "MULTIPLE_INSTALLS":
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
