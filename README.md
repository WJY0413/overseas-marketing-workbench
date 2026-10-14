# Workbench Linux / dot 1

基于正式版 5.05，Linux 交付版标记 `linux-dot.1`。本包已完成 Linux 测试；实际 dot 部署与真实发信迁移需另行执行。

## 安装与运行

需要 Linux、Python 3.10+、venv/pip 和可访问 Python 包源的网络。验收环境使用 Ubuntu 24.04 / Python 3.12。如系统提示缺少 venv，由工作区管理员提供 Python 虚拟环境支持；安装脚本不修改系统软件、服务或账号权限。

将整个包解压到 dot **明确持久化**的独立目录。不要放在临时目录。进入解压目录：

```sh
sh setup.sh
# 检查 .env 中的数据目录、时区及路径
sh start.sh --port 8000
```

`setup.sh` 只向本目录的 `.venv` 安装依赖，首次创建 `.env`，不覆盖已有设置。`start.sh` 在前台运行，默认只监听 `127.0.0.1`。dot 必须通过已有的私有端口访问方式打开页面；此应用没有账号登录层，不应直接暴露到公网。若 dot 的私有转发要求监听 `0.0.0.0`，应先核实入口的身份校验，再显式传入 `--host 0.0.0.0`。

Python API 使用相同入口：在安装目录执行 `.venv/bin/python -m app.linux`。不要用多个 Uvicorn worker、`--reload` 或另一个进程同时启动同一数据库。Linux 入口固定单 worker，并对实际 SQLite 路径加进程锁。锁文件保留是正常状态，进程退出即释放锁；运行时不要删除锁文件。

## 功能与持久化

- 保留 CRM、模板、批次计划、审核、SMTP 队列、发送记录、抑制/退信、领英内置库与指定库路径。
- 发件设置统一在“设置”页；旧 `/senders` 地址自动跳转，原有发件配置 API 保留。
- 旧固定延迟规则仅在有历史记录时折叠展示，新空库不再创建旧规则示例。双计数器跟进保持默认关闭。
- NO-GO 反馈改为下载 JSON 快照，不需要发件邮箱、不发送邮件、不修改抑制或历史。旧反馈 POST 地址同样只返回下载文件。
- **Workbench 内置的飞书原生回复已经退役。** 旧原生回复草稿在审核/入队/发送前会被阻止，不会转成 SMTP 发信；历史内容与回执继续保留。回复由 dot 已有飞书能力处理，本应用无需安装或登录 lark-cli。
- Windows 启动/安装脚本、个人路径绑定的历史查询 MCP、旧领英测试库工具不放入此包。Linux 的系统防休眠 API 保持不执行。
- `.env` 默认模拟发送、关闭打开追踪和退信自动扫描。首次运行创建空业务库和空领英原生库；不携带客户库、密码、历史数据或本机绝对路径。
- 默认数据目录 `./data`。持久化时同时保留 `workbench.db`、`linkedin_people_master.sqlite`、`.secret_key`、报告/附件目录和 `.env`。`.secret_key` 是已存 SMTP 密码的解密密钥，迁移已有库时不能遗漏或重新生成。
- 指定领英库可在设置页切换。Linux 路径如 `/workspace/persistent/linkedin_people_master.sqlite`；同理 BD 源库可通过 `.env` 指定，源库应保持只读。使用本机文件系统；不要多主机共享一个 SQLite 库。

## dot 上线前的实际验收

本机 Linux 测试不能证明 dot 的工作区在聊天结束、休眠或重建后仍能运行。正式发信前需在实际 dot 中确认：

1. 代码、`.env`、数据库、密钥及附件目录可以持久化；断开聊天再进入后数据仍在。
2. dot 有支持常驻进程的机制。单纯后台 shell 或 `nohup` 不保证跨休眠/重建运行；如只有临时执行能力，dot 可以准备批次，定时发信需放在常驻服务。
3. 私有 UI 入口可用，所选 SMTP/IMAP 端口和 DNS 可用。现有飞书授权不能替代 SMTP 授权；禁止复制 Windows 的 lark-cli 登录缓存。
4. 迁移经确认的业务快照、密钥及附件，修正所有 Windows 附件/库路径；核对抑制、NO-GO、成功发送和未决发送记录。不能把旧队列复制到两端并同时运行。
5. 切换时先停止原端对该批次的调度，再在 dot 核验。`scheduled`、`sending`、`send_unknown` 和旧原生回复必须逐项处理，不能因重启而重试未知结果。
6. 明确授权后才切换真实发送、做受控试发并核验回执；本包安装和启动不代表已获该授权。

## 复现构建

开发者在 Windows 从候选源码执行既有 `scripts/create_shareable_package.ps1 -PureEmpty`，再运行：

```text
python scripts/build_linux_package.py --clean-zip releases/Overseas_Marketing_Workbench_v5.05_Pure_Empty.zip
```

Linux 包复用已验证的公开包脱敏流程，仅选择 Linux 所需文件；不会携带 Windows 安装程序、业务数据库、凭据或虚拟环境。

调度器单实例的背景：[APScheduler FAQ](https://apscheduler.readthedocs.io/en/3.x/faq.html)；启动方式：[Uvicorn programmatic deployment](https://www.uvicorn.org/deployment/)。
