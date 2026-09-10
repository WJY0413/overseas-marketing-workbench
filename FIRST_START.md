# BD Email Workbench：首次启动

## Python 自动配置

- 双击 `start.cmd`：自动读取本机 PATH、Python Launcher、注册表及常见安装目录，首次配置优先使用较新的稳定版 CPython 3.11+，自动创建 `.venv` 并安装依赖。v5.03 本次发布验证使用 Python 3.12；3.14 使用对应依赖版本，未来版本仍取决于依赖兼容性。
- 没有可用 Python 时，启动器会显示 python.org 官方最新版稳定安装包，输入 `y` 后下载、验证发布者签名、为当前用户安装并继续配置。无需手工设置 PATH。
- 想主动安装最新版：双击 `install_python.cmd`。使用官方下载，不包含 Python 安装文件，首次配置需要联网。仅下载正式稳定版，不选预览版。
- 已有可用 `.venv` 会复用；更换 Python 时原环境保存为 `.venv.previous-*`。依赖文件改变会自动重新安装依赖。备份环境不进入分享包。
- 自定义 Python 路径：在此目录执行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_python.ps1 -PythonPath "C:\你的目录\python.exe"`。
- 只检测、不安装：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/setup_python.ps1 -Mode Detect`。

只做四件事：确认唯一版本、安装 Skills、配置一个发件邮箱、保存邮件签名。

1. 双击 `install_skills.cmd`，安装或原目录更新随包的 10 个 Skills。
2. 重启 Codex，然后发送：

   `请使用 $workbench-first-run-check 检查并完成海外营销 Workbench 首次配置。`

3. 检查结果为 `new` 时，双击 `start.cmd` 创建本地环境。
4. 检查结果为 `upgrade` 时，不要再启动解压目录；先关闭唯一旧版本并确认其本地端口停止监听，再让 Codex 预览并使用 `scripts/update_existing_workbench.ps1` 原地更新。若端口仍在监听，脚本会在复制任何文件前停止。
5. 检查结果为 `blocked` 时，先指定唯一主版本。系统不会自动删除、合并或启动多个副本。
6. 在页面中启用至少一个 SMTP 发件邮箱，并在 `Settings` 保存、预览邮件签名。

内部种子包首次启动时只会写入永久邮箱级抑制和已确认客户域名级 NO-GO；不会创建 NO-GO 公司，也不会覆盖已有 CRM、联系人、发送记录或发件邮箱配置。对外干净包不含此种子数据。

达到以下状态即可结束首次配置：

```text
WORKBENCH_SETUP: READY
安装模式: new / upgrade / current
运行环境: PASS
发送邮箱: PASS
邮件签名: PASS
Skills: PASS
```

首次配置不导入客户、不创建队列、不扫描退信，也不发送测试邮件。实际发送仍需单独确认。

## LinkedIn 工作流

1. `$linkedin-mining-v3`：采集已提供的 LinkedIn People 搜索结果。
2. `$linkedin-greeting-workflow`：用户明确指定原始库和 Workbench LinkedIn 主库路径后，解析、生成草稿，并将可用草稿显式导入主库作为待建联联系人。
