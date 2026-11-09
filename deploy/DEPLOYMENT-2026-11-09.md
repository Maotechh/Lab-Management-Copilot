# 2026 年 11 月 9 日界面发布记录

## 正式版本

- 正式网址：[https://10.15.21.115](https://10.15.21.115)，限制在校园网和学校 VPN 内访问，不要求登录。HTTP 根页面返回证书安装说明。
- 应用分支和提交：`codex/dsh-lab-delivery`，`cb7a74607ad7cdf33110bce939814ae822679d91`，已推送到 `origin`。
- 服务器发布目录：`/opt/lab-management/releases/20261109-cb7a74607a`，`/opt/lab-management/current` 已指向该目录。通过此版本的 `deploy/install.sh` 完成安装，安装任务退出状态为 0。
- DSH 提交：`a51d6a6df84930fb8d4aa0a9daf26523984b7560`。Node.js 为 `v24.22.2`，pnpm 为 `11.28.5`。发布清单核对 36,253 个文件，DSH 和语音运行文件与前次已验证版本完全一致。
- 学校模型实际检查结果为 `available`，模型是 `deepseek-pro`，接口使用学校 `POST /v1/responses`。本次检查时间为上海时间 2026 年 11 月 9 日 12:40:22。

## 界面改动

- 重新安排桌面侧边导航、共享对话列表、手机底部导航、业务事务分类和更多工具入口。
- 聊天草稿按对话分别保存。库存详情可将物品、位置和数量带入聊天输入框。
- 手机附件支持多文件预览、移除和拖放。录音状态、输入区和手机键盘布局已调整。
- 进一步参照服务器 DSH 网页的按钮、颜色、间距和聊天布局调整界面，保留实验室专用功能。
- 聊天消息按消息编号更新，保留已加载的历史、已展开的结果详情和阅读位置。修正初次显示时消息节点未插入页面的问题；历史分页一次点击只请求一次。
- 确认区域中的实际变更详情默认展开，老师可以检查完整变化后确认。
- 库存操作和清单扣减均通过服务器生成完整预览，再统一确认。共享事务页面可以查看不同对话中的待处理计划。
- 保留“附件库存尚未经老师核对”提示。

## 验证和数据

- `ruff check consumable_assistant scripts tests`、`python -m pytest -q tests`（112 项）、`python scripts/smoke_test.py`、全部静态 JavaScript 语法检查、通知处理检查及 `bash -n deploy/install.sh` 均通过。
- 隔离浏览器检查覆盖桌面、手机竖屏和手机横屏，检查共享对话、表单流程、附件处理、库存确认预览及页面横向溢出。模拟服务没有 DSH，因此本地检查记录了模型不可用时的消息保存结果。正式服务器的学校模型检查通过。
- 新增的隔离聊天历史检查通过：初次显示 100 条消息，加载历史后为 200 条，刷新新增消息后为 201 条；已加载历史和已展开详情保留，历史分页请求次数为 1。检查宽度为 375、844 和 1,440 像素。
- 正式网站只读浏览器检查通过：已保存的消息与页面显示数量一致，375、844 和 1,440 像素宽度均无页面横向溢出，没有 JavaScript 运行错误，也没有发送修改请求。完整业务浏览器测试仅在隔离环境执行。
- 部署前后均为 894 条库存、1,073 条业务流水和 1,117 条来源记录。安装过程没有重新导入附件。
- 迁移报告确认原有业务表、聊天及附件记录的数量和内容校验值均保留，数据库完整性检查为 `ok`。原有 10 条聊天消息保留。
- 安装脚本生成并验证完整发布快照：`/var/backups/lab-management/release-20261109T043852.486944495Z`。在隔离目录完成该备份的恢复演练；发布后快照位于该目录下的 `installed-snapshot`，校验结果为 `verified: true`，其中记录的新版本提交与正式服务一致。
- 正式应用、DSH、分析服务、通知服务、Nginx 和完整备份定时器均为 `active`。分析容器实际验证了生产数据与配置不可读、网络不可访问、输入不可写，以及单并发、CPU、内存、输出大小和执行超时限制；限制测试后服务恢复正常。
- 服务器使用实验室 CA 验证 HTTPS 首页、`app.js`、`mobile.js`、`styles.css` 和 `interface.js`，均返回 HTTP 200，内容校验值与发布清单完全一致。本机通过校园网络访问 HTTP 根页面及显式指定实验室 CA 的 HTTPS 首页均返回 HTTP 200；下载的 CA 指纹与 SSH 中读取的服务器 CA 指纹相同。

## 设备状态

两部手机此前已由用户确认可以访问受信任页面并完成录音识别和共享消息检查。本次界面发布后尚未重复进行手机操作测试；系统版本、浏览器版本和网络类型未记录。锁屏通知测试按用户安排暂缓。

本机 macOS 的系统信任库尚未确认信任实验室 CA。本次命令检查显式指定该 CA；正式网站的浏览器布局检查允许实验室证书，不作为操作系统信任完成的证据。电脑浏览器仍需安装并信任实验室根证书后再验证。HTTP 根页面可查看安装说明。学校 VPN 和手机锁屏通知没有在本次发布中重复验证。

完整发布日志保存在服务器 `/opt/lab-management/build/install-cb7a74607a.log`，发布后检查摘要保存在 `/opt/lab-management/build/verify-cb7a74607a.json`。实物库存核对、试剂系统实际接入和真实平面图制作不属于本次工作范围。

## 本日前次版本与恢复

前次版本 `4a53b359d3200e199e761debe1704b6eb1a4ff4c` 及目录 `/opt/lab-management/releases/20261109-4a53b359d3` 继续保留。其发布日志为 `/opt/lab-management/build/install-4a53b359d3.log`，此前验证的备份 `/var/backups/lab-management/release-20261108T171354.433751505Z` 也继续保留。

需要恢复本次发布前的完整状态时，先暂停备份定时器并确认当前备份任务已经结束，再按 `deploy/README.md` 的恢复步骤执行：

```sh
systemctl stop consumable-backup.timer
/opt/lab-management/current/.venv/bin/python /opt/lab-management/current/scripts/consistent_backup.py restore --snapshot /var/backups/lab-management/release-20261109T043852.486944495Z
systemctl restart consumable-analysis
systemctl start consumable-backup.timer
```

恢复命令同时还原数据库、共享对话、附件、配置和发布目录选择。操作完成后核对服务、学校模型及 HTTPS 访问；不要只修改 `current` 链接而保留不一致的数据状态。
