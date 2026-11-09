# 2026 年 11 月 9 日 DSH 网页重构发布

正式入口为 [https://10.15.21.115](https://10.15.21.115)。应用提交为 `392ce4c2af7227e7de02ac849c23c471117ad0c7`，分支为 `codex/dsh-lab-delivery`。DSH 继续使用 `a51d6a6df84930fb8d4aa0a9daf26523984b7560`，Node.js 为 `v24.22.2`，pnpm 为 `11.28.5`。

## 实现与来源

直接检查服务器 `127.0.0.1:3080` 的 DSH 网页，并确认其 client 目录与官方提交 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` 相同。直接复用主题、字体、阴影、滚动条和圆角文件，导出原始 SVG 图标，并适配原始 Markdown、代码与表格样式及中文强调解析。来源文件、校验值和许可保存在 `consumable_assistant/static/dsh/`，构建说明见 `frontend/README.md`。

导航、共享对话、空白对话、底部输入区域、按钮、用户消息和工具过程按照 DSH 网页的结构及尺寸重建。支持电脑折叠导航、手机抽屉导航、深色配色、复制、查看历史及返回最新消息。实际查询结果直接显示，处理过程可以展开。数量不在小数中间换行，较宽的表格可横向滚动。库存和表单共用原有服务器确认规则。

服务器仅返回已经保存的工具活动摘要。消息、任务和事件编号使用同一个 SQLite 读取事务，避免其他设备并发发送时遗漏事件。浏览器拒绝较旧的响应，发送期间保护附件，取消状态取自服务器。离线、重连或存在未发送修改时不能确认原计划。

## 验证

本地和服务器均通过 Ruff、114 项测试、smoke test、JavaScript 语法、通知处理检查、安装脚本语法及前端构建一致性检查。隔离业务浏览器检查覆盖借还、清洗、采购到货、实验预留、表格导入、清单执行和确认，电脑与手机尺寸均通过。聊天界面检查覆盖 Markdown 安全、实际数据展示、草稿、取消、重复新建、迟到响应、重连、深色主题、抽屉导航及触摸录音控件。历史检查保留 201 条消息，分页一次请求，原有展开状态保留。

安装脚本修正了定时备份并发问题。systemd 的 Type=oneshot 服务在执行期间处于 activating，安装程序现在等待备份结束后再暂停业务写入。等待超过十五分钟则结束安装，不切换版本。模拟 activating、active、deactivating、inactive 的状态检查通过。此前失败的安装日志继续保留。


## 正式发布结果

通过本版本的 `deploy/install.sh` 安装，任务正常结束，学校模型 `deepseek-pro` 实际状态为 `available`，使用学校 `POST /v1/responses`。安装期间检查时间为上海时间 2026 年 11 月 9 日 14:34:19。发布目录为 `/opt/lab-management/releases/20261109-392ce4c2af`，当前目录链接和 `/etc/lab-management/release.json` 均记录上述提交。发布清单校验 36,274 个文件。

正式应用、DSH、分析服务、通知服务、Nginx 和完整备份定时器均为 `active`。分析容器实际检查通过，包括禁止读取生产数据与凭据、禁止网络访问、只读输入、单并发，以及 CPU、内存、超时和输出限制。

发布前后全部业务和聊天表的行内容校验值一致，保留 894 条库存、1,073 条 `inventory_transactions` 历史业务记录、1,117 条来源记录和 10 条聊天消息。新业务表 `stock_events` 为 0 条。本次没有库存操作，也没有重新导入附件。仅 `service_health` 和 `ai_provider_checks` 的健康检查时间、耗时等记录更新；模型配置身份与状态另行比较一致。

完整发布前备份为 `/var/backups/lab-management/release-20261109T063246.861576694Z`，包含数据库、附件、位置照片、DSH 会话、任务和配置。安装程序验证了该备份，并在独立目录恢复演练。发布后完整快照为同一目录中的 `installed-snapshot`，其记录的提交与正式版本一致，文件与数据库校验通过。原有已验证备份和恢复需要的历史发布目录继续保留。

正式 HTTPS 首页及十个静态文件的实际内容校验值与发布目录一致。校园网络访问 HTTP 证书说明页和显式指定实验室 CA 的 HTTPS 首页均返回 200。正式网页只读浏览器检查通过，屏幕宽度为 390、844 和 1,440 像素，验证消息数量、实际统计数据、长小数完整显示、导航、深色配色及横向溢出，没有 JavaScript 错误，也没有修改请求。

安装日志为 `/opt/lab-management/build/install-392ce4c2af.log`，完整检查摘要为 `/opt/lab-management/build/verify-392ce4c2af.json`。此前备份锁冲突的失败日志保留为 `/opt/lab-management/build/install-6d6606d252.log`。正式检查在上海时间 2026 年 11 月 9 日 14:37:12 完成。

两部手机此前由用户确认受信任页面、录音和共享消息正常。本次重构后尚未重复实际 iPhone 和 Android 操作，系统版本、浏览器版本和网络类型仍未记录。学校 VPN 访问及锁屏通知没有在本次发布中验证，通知检查按用户安排暂缓。浏览器屏幕尺寸模拟和显式 CA 检查不作为手机或操作系统信任验证。

## 恢复本次发布前状态

以下命令需要服务器 root。先暂停定时备份并等待正在执行的 Type=oneshot 备份完成，再恢复完整状态：

```bash
systemctl stop consumable-backup.timer
while [[ "$(systemctl show --property=ActiveState --value consumable-backup.service)" =~ ^(active|activating|reloading|deactivating)$ ]]; do
    sleep 1
done
/opt/lab-management/current/.venv/bin/python /opt/lab-management/current/scripts/consistent_backup.py restore --snapshot /var/backups/lab-management/release-20261109T063246.861576694Z
systemctl restart consumable-analysis
systemctl start consumable-backup.timer
```

恢复前一版本 `3c31509bf4901ece5a9360530bb2a52be8ccfe77` 时，恢复程序同时还原匹配的代码选择、数据库、共享对话、附件和配置。完成后再次检查服务、学校模型及 HTTPS。操作说明参见 `deploy/README.md`，不要单独修改发布目录链接。
