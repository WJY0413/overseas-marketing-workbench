# 海外营销 Workbench：首次启动

只做四件事：确认唯一版本、安装 Skills、配置一个发件邮箱、保存邮件签名。

1. 双击 `install_skills.cmd`，安装或原目录更新随包的 7 个 Skills。
2. 重启 Codex，然后发送：

   `请使用 $workbench-first-run-check 检查并完成海外营销 Workbench 首次配置。`

3. 检查结果为 `new` 时，双击 `start.cmd` 创建本地环境。
4. 检查结果为 `upgrade` 时，不要再启动解压目录；让 Codex 先预览并使用 `scripts/update_existing_workbench.ps1` 原地更新唯一旧版本。
5. 检查结果为 `blocked` 时，先指定唯一主版本。系统不会自动删除、合并或启动多个副本。
6. 在页面中启用至少一个 SMTP 发件邮箱，并在 `Settings` 保存、预览邮件签名。

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
