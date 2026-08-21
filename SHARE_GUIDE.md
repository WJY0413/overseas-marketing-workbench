# 海外营销 Workbench 分享说明

Use this workflow when sharing 海外营销 Workbench with another person.

## Create The Package

Run from the project root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\create_shareable_package.ps1
```

The zip is created under:

```text
releases/
```

## What The Clean Package Excludes

The package must not include:

- `.env`
- `.venv/`
- `data/`
- `reports/`
- `backups/`
- `code_backups/`
- `releases/`
- internal maintenance docs
- `*.db`, `*.sqlite`, `*.log`, `*.pyc`
- `data/.secret_key`

This keeps customer data, sender passwords, send history, local backups, and machine-specific runtime files out of the shared copy.

## Recipient Startup

The recipient should:

1. Install Python 3.12. Python 3.11 and 3.13 are also supported; avoid Python 3.14 for this release.
2. Unzip the package into a normal Windows folder, for example:

   ```text
   C:\BD_Email_Workbench_Lite
   ```

3. Double-click:

   ```text
   start.cmd
   ```

4. Open this URL if the browser does not open automatically:

   ```text
   http://127.0.0.1:8001
   ```

On first launch, `start.cmd` creates `.env`, creates `.venv`, installs dependencies, creates a fresh SQLite database, and starts the local app.

## Safe Defaults

The clean package starts with:

```text
DRY_RUN_EMAIL=true
ENABLE_OPEN_TRACKING=false
```

No real email is sent until the recipient configures sender settings and switches out of dry-run mode.

## Sender Setup

Before real SMTP sending, the recipient should:

1. Open the sender settings page.
2. Add their own sender mailbox.
3. Save their SMTP app password in the page, or configure the password variable in `.env`.
4. Test with `DRY_RUN_EMAIL=true`.
5. Only switch to real sending after checking sender, template, recipient list, and queue status.

## Signature Setup

The clean package includes the signature module, but the recipient should replace the default identity before sending:

1. Open `Settings`.
2. Find `Signature module`.
3. Edit sender name, title prefix, company name, address, default display email, phone numbers, country rules, and sender-mailbox rules.
4. Optionally import a `.docx` signature block for fixed images or visual formatting.
5. Return to `Workbench` and use `Signature confirmation` in the draft-generation step to preview the final signature by recipient country and sender mailbox.

Country rules use this format:

```text
United Kingdom, UK, Ireland|UK & IE|+1 555 0100
France|FR|+1 555 0100
Thailand, Vietnam, Malaysia, Singapore, Indonesia, Philippines|Southeast Asia|+1 555 0101
```

Sender-mailbox rules use this format:

```text
sender@example.com|display@example.com|+1 555 0100|UK & IE|Sender Name
```

## Verify A Package

Run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_shareable_package.ps1 -Path .\releases\<package>.zip
```

If verification fails, do not share the package.
