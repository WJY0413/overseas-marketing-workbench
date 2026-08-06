# 海外营销 Workbench 分享与安装说明

公开版本通过 GitHub Release 提供。下载版本号对应的 ZIP，解压到普通 Windows 文件夹，例如：

```text
C:\Overseas_Marketing_Workbench
```

## 首次启动

1. 安装 Python 3，并确保 Windows 可以使用 `py -3`、`python` 或 `python3` 命令。
2. 双击 `install_skills.cmd` 安装随包 Codex Skills。
3. 重启 Codex，发送：`请使用 $workbench-first-run-check 检查并完成海外营销 Workbench 首次配置。`
4. 未发现旧版本时双击 `start.cmd`；如果发现旧版本，优先按检查结果原地升级，避免同时运行两套软件。
5. 浏览器未自动打开时访问 `http://127.0.0.1:8001`。

首次启动会在本机创建 `.env`、`.venv` 和新的 SQLite 数据库。公开包不包含真实邮箱密码、客户数据、发送记录、日志、备份或真实签名。

## 最小配置

开始实际发送前，只需先完成四项：

- 至少一个可正常连接的 SMTP 发件邮箱。
- 已替换为本人信息并预览过的签名。
- Workbench 能正常启动，`/health` 检查通过。
- 7 个随包 Skills 已安装，Codex 重启后可以识别。

邮箱密码应只保存在使用者自己的电脑上，不要提交到 GitHub，也不要发到聊天中。

## 安全默认值

```text
DRY_RUN_EMAIL=true
ENABLE_OPEN_TRACKING=false
```

先在 dry-run 模式检查发件人、收件人、模板、签名、NO-GO、抑制状态和队列。准备实际发送时，再由使用者明确切换发送设置；SMTP 接受不代表最终投递成功。

## 签名与区域规则

在 `Settings` 的签名模块中配置姓名、职位、公司、地址、展示邮箱、电话、国家规则和发件邮箱规则。也可以导入 `.docx` 签名块保留图片和富文本格式。

国家规则示例：

```text
United Kingdom, UK, Ireland|UK & IE|+1 555 0100
France|FR|+1 555 0100
Thailand, Vietnam, Malaysia, Singapore, Indonesia, Philippines|Southeast Asia|+1 555 0101
```

发件邮箱规则示例：

```text
sender@example.com|display@example.com|+1 555 0100|UK & IE|Sender Name
```

以上信息均为虚构占位示例，使用前必须替换。

## 已有版本升级

先用 `$workbench-first-run-check` 找到唯一主版本，再预览更新：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\update_existing_workbench.ps1 -TargetRoot "<existing-workbench-root>"
```

确认目标后增加 `-Apply`。更新脚本会保留本机 `.env`、`.venv`、`data/`、邮箱凭据、签名、发送记录、报告和备份。

## 包校验

维护者可以运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify_shareable_package.ps1 -Path "<release-zip>"
```

校验失败时不要分享或安装该 ZIP。
