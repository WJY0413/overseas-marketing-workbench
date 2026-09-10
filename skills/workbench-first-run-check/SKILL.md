---
name: workbench-first-run-check
description: "Prepare or verify a Windows computer for 海外营销 Workbench. Use when Codex needs to perform first-run setup, detect an older Workbench installation, choose new-install versus in-place-upgrade mode, prevent duplicate installations, or check the minimum ready state: runnable environment, one usable SMTP sender, a personalized signature, and the bundled Workbench Skills."
---

# Workbench First Run Check

Keep the first-run flow to four checks. Show details only for failed items.

## Run the read-only check

Run:

```powershell
python "<codex-skills-root>\workbench-first-run-check\scripts\preflight_check.py" --package-dir <clean-package-root>
```

Add `--installation <path>` for a known installation or `--search-root <path>` for a narrowly scoped search. Use `--json` only when another script needs structured output.

The checker must not start the app, connect to SMTP, send email, reveal a password, modify a database, install a Skill, or update program files.

## Decide the installation mode

- No stateful installation: use `new` mode.
- Exactly one installation older than the package: use `upgrade` mode and keep that directory as the only active copy.
- Exactly one installation at the package version: use `current` mode.
- More than one active installation, or duplicate Skill folders: return `blocked`; list the paths and ask which installation is authoritative. Do not merge, rename, or delete automatically.
- Ignore clearly archived copies under `backup`, `code_backups`, `history`, `output`, `releases`, or `staging` when counting active installations.
- Treat a deliberately isolated test database such as `workbench_test.db` as a test workspace, not a second production installation. Use `--include-test` only when the user asks to audit test copies too.

Before an in-place upgrade, preview the exact target and preserve `.env`, `.venv`, `data/`, databases, sender credentials, templates, signatures, send records, reports, and backups. Back up the replaced program files. Apply the update only after the user approves that exact target.

When the clean package contains `scripts/update_existing_workbench.ps1`, run it once without `-Apply` to preview the update. After approval, rerun it with `-Apply` against the same exact target. Never extract and start a second stateful copy merely to perform an upgrade.

## Check only four readiness items

1. **Environment**: supported Python, `start.cmd` or `start_production.cmd`, `requirements.txt`, and application files exist. If the service is already running, `/health` may be checked read-only.
2. **Sender**: at least one active, non-placeholder SMTP sender has host, username, and a stored credential reference. Never print the credential.
3. **Signature**: a saved signature configuration or active signature template exists and does not contain placeholder identity values.
4. **Skills**: the 10 bundled Workbench Skills exist under the configured Codex skills root with no copy/old/numbered duplicates.

Do not expand into campaign settings, customer imports, templates, queues, NO-GO policy, bounce processing, or test sends during first-run setup. Route those tasks to their specialist Skills after readiness is complete.

## Finish concisely

Return one block:

```text
WORKBENCH_SETUP: READY | NEEDS_SETUP | MULTIPLE_INSTALLS
安装模式: new | upgrade | current | blocked
运行环境: PASS | FAIL
发送邮箱: PASS | FAIL
邮件签名: PASS | FAIL
Skills: PASS | WARN | FAIL
下一步: <one concrete action>
```

When Skills are missing, point to `install_skills.cmd` in the clean package and give this first prompt after installation:

`请使用 $workbench-first-run-check 检查并完成海外营销 Workbench 首次配置。`
