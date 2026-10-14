# BD Email Workbench

## 按平台下载安装

| 平台 | 版本 | 下载 |
| --- | --- | --- |
| Windows | 5.05 | [正式安装包](https://github.com/WJY0413/overseas-marketing-workbench/releases/download/v5.05/Overseas_Marketing_Workbench_v5.05_Production_Clean.zip) · [发布说明](https://github.com/WJY0413/overseas-marketing-workbench/releases/tag/v5.05) |
| Linux / dot | 5.05 / linux-dot.1 | [Linux 安装包](https://github.com/WJY0413/overseas-marketing-workbench/releases/download/v5.05-linux-dot.1/Overseas_Marketing_Workbench_v5.05_Linux_Dot_v1.tar.gz) · [发布说明](https://github.com/WJY0413/overseas-marketing-workbench/releases/tag/v5.05-linux-dot.1) |

Linux / dot 用户请使用对应安装包和 [Linux 安装指南](https://github.com/WJY0413/overseas-marketing-workbench/releases/download/v5.05-linux-dot.1/LINUX_DOT.md)，也可把 [部署提示词](https://github.com/WJY0413/overseas-marketing-workbench/releases/download/v5.05-linux-dot.1/DOT_DEPLOY_PROMPT.txt) 交给 dot 执行独立空库安装测试。Linux 版已退役内置飞书原生回复、统一发件设置、折叠旧规则，并将 NO-GO 反馈改为 JSON 下载。实际 dot 环境仍需验证持久化和常驻能力；初始安装仅允许模拟发送。

[全部历史版本](https://github.com/WJY0413/overseas-marketing-workbench/releases)继续保留。请下载对应发布资产，不要用 GitHub 自动生成的 Source code 压缩包替代安装包。本分支及下方说明对应 Windows 版；Linux 源码位于 `v5.05-linux-dot.1` 标签。

## LinkedIn 库选择（5.05）

在「设置 → LinkedIn 联系人库」选择「内置库」或「指定库路径」。内置库位于当前数据目录的 `linkedin_people_master.sqlite`；新安装会自动创建空库，已有库完整保留。内部种子包已有的联系人库仍可继续使用。

指定库路径时，填写运行 Workbench 的电脑上的绝对路径，目标必须是兼容 Workbench 的已有 LinkedIn SQLite 库。保存前只读检查结构及完整性，成功后立即生效并持久保存。页面显示当前来源和路径，可随时切回内置库；切换不会复制、合并或覆盖任一库。确认和撤销会写入当前选择的库，已打开的旧建联页面须刷新后再操作。

若启动脚本通过 `LINKEDIN_MASTER_SQLITE_PATH` 环境变量固定了路径，界面会标明并禁用切换；需先调整该实例启动配置。`.env` 中配置的路径则可从设置页更改。

这是可在 Windows 本地运行的干净生产分享包。它通过本地队列完成 SMTP 发信、发送节奏控制、记录、邮箱风险检查、退信抑制和 NO-GO 管理；不需要 AI 会话持续运行。

## v5.03 发布包说明

2026-09-10 更新：修复收件人和 CC 保持、队列暂停/取消、每日额度、发送结果未明、退信历史保留、SQLite 备份和 LinkedIn 确认还原；首页预览按需加载。公开包不含客户数据或运营者身份；NO-GO 反馈邮件需先配置 `WORKBENCH_NOGO_FEEDBACK_TO`，内部包沿用内部反馈目标。


- `Overseas_Marketing_Workbench_v5.03_Production_Clean.zip`：对外干净分享包，不含任何 SQLite 数据库。
- `Overseas_Marketing_Workbench_v5.03_Internal_LinkedIn_NoGo_Seed.zip`：内部种子包，含默认 LinkedIn 联系人主库（100 家脱敏的初始已建联联系人）、永久邮箱级抑制和已确认客户域名级 NO-GO；不得对外分发。
- 两个包均附带 10 个 Workbench Skills，其中包含 LinkedIn 采集与招呼语处理流程。

## 计划器安全规则

内置 `/planner` 只读预排：不会创建草稿、入队或发送邮件。模板轮换前会排除公司成功发送后 48 小时冷却、所选精确主收件人成功发送后 7 天冷却，以及来源为 `success_locked` 的联系人；三个原因分别返回，不能被“可入队”总数掩盖。

## 第一次使用

1. 无需预先手动配置 Python。首次启动自动检测 PATH、Python Launcher 和 Windows 注册表中的稳定版 CPython 3.11+，优先选择本机较新版本，支持 Python 3.14。
2. 解压后先阅读 `FIRST_START.md`，双击 `install_skills.cmd`。
3. 重启 Codex，发送：`请使用 $workbench-first-run-check 检查并完成海外营销 Workbench 首次配置。`
4. 未发现旧版本时双击 `start.cmd`；发现一个旧版本时按检查结果原地升级；发现多个版本时先指定唯一主版本。
5. 在页面中配置至少一个可用 SMTP 邮箱，并保存、预览个性化签名。

`start.cmd` 会创建 `.env`、`.venv` 和新的本地 SQLite 数据库，然后启动 `http://127.0.0.1:8001`。

## 安全默认值

- `DRY_RUN_EMAIL=true`
- `ENABLE_OPEN_TRACKING=false`
- 不含真实 SMTP 密码、发送记录、日志、备份或真实签名；对外干净包不含数据库，内部种子包仅含脱敏 LinkedIn 主库与 NO-GO 种子库。
- 实际发送前仍需检查发件人、收件人、模板、签名、抑制状态、NO-GO 和队列。

## 已有版本升级

不要把新版作为第二套软件启动。先让 `$workbench-first-run-check` 确认唯一旧版本，关闭该版本并确认它的本地端口已经停止监听，再预览：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\update_existing_workbench.ps1 -TargetRoot "<existing-workbench-root>"
```

确认目标后增加 `-Apply`。若目标目录声明的 Workbench 端口仍在监听，脚本会在复制任何文件前拒绝更新，防止旧进程加载新模板形成混合版本。脚本保留 `.env`、`.venv`、`data/`、数据库、邮箱凭据、签名、发送记录、报告和备份，并把被替换的程序文件备份到旧版本的 `code_backups/`；更新完成后再从目标目录启动 Workbench。

## 随包 Skills

压缩包包含 10 个 Workbench Skills：首次启动检查、批次计划器、每日路由、邮件发送、收件箱检查、退信抑制、队列构建、参数调整、LinkedIn 采集与 LinkedIn 招呼语处理。安装器只更新同名目录并先备份；检测到 `copy`、`old`、`(1)` 等重复目录时会停止。
