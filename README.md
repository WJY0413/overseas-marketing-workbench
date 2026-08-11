# 海外营销 Workbench

这是可在 Windows 本地运行的干净生产分享包。它通过本地队列完成 SMTP 发信、发送节奏控制、记录、邮箱风险检查、退信抑制和 NO-GO 管理；不需要 AI 会话持续运行。

## 第一次使用

1. 安装 Python 3.12；同时支持 3.11 和 3.13，本版不建议 Python 3.14。
2. 解压后先阅读 `FIRST_START.md`，双击 `install_skills.cmd`。
3. 重启 Codex，发送：`请使用 $workbench-first-run-check 检查并完成海外营销 Workbench 首次配置。`
4. 未发现旧版本时双击 `start.cmd`；发现一个旧版本时按检查结果原地升级；发现多个版本时先指定唯一主版本。
5. 在页面中配置至少一个可用 SMTP 邮箱，并保存、预览个性化签名。

`start.cmd` 会创建 `.env`、`.venv` 和新的本地 SQLite 数据库，然后启动 `http://127.0.0.1:8001`。

## 安全默认值

- `DRY_RUN_EMAIL=true`
- `ENABLE_OPEN_TRACKING=false`
- 不含真实 SMTP 密码、客户数据、发送记录、数据库、日志、备份或真实签名。
- 实际发送前仍需检查发件人、收件人、模板、签名、抑制状态、NO-GO 和队列。

## 已有版本升级

不要把新版作为第二套软件启动。先让 `$workbench-first-run-check` 确认唯一旧版本，再预览：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\update_existing_workbench.ps1 -TargetRoot "<existing-workbench-root>"
```

确认目标后增加 `-Apply`。脚本保留 `.env`、`.venv`、`data/`、数据库、邮箱凭据、签名、发送记录、报告和备份，并把被替换的程序文件备份到旧版本的 `code_backups/`。

## 随包 Skills

压缩包包含 7 个 Workbench Skills：首次启动检查、每日路由、邮件发送、收件箱检查、退信抑制、队列构建和参数调整。安装器只更新同名目录并先备份；检测到 `copy`、`old`、`(1)` 等重复目录时会停止。
