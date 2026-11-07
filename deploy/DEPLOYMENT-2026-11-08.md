# 2026 年 11 月 8 日发布记录

## 运行版本

- 正式入口：`https://10.15.21.115`。校园 HTTP 页面返回 200，用于证书安装说明。访问仍限制在校园网及学校 VPN，无登录。
- 应用分支：`codex/dsh-lab-delivery`，发布提交：`323c239ee4cdc488e6f30a333fd32300fd18179f`。
- 发布目录：`/opt/lab-management/releases/20261108-323c239ee4`，通过该版本的 `deploy/install.sh` 安装。
- DSH 分支：`Maotechh/deepseek-harness:codex/lab-copilot`，提交：`013eea6a51f8b1d6bf279c83dfd142c19d775d44`，保留官方基础提交 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` 的历史。
- Node.js：`24.22.2`；pnpm：`11.28.5`。发布清单验证 36,247 个文件。
- DSH 源依赖锁文件 SHA-256：`d65a6deb59aaaa61424ea8c832874b8745d09085c8c2ed37a1e8f1aeaa01017e`。部署锁文件 SHA-256：`91931d6d75c9a669ef95abf0f6ef41af830ac4ba08e63f5972df19d47dba5c35`。
- 网页 `app.js` SHA-256：`bc96f06de5d9ce31262f96b34110d1bd233b389628f43c33c8b933eb4cbd6f75`。完整构建校验值保存在发布目录的 `release-manifest.json`。

## 实际服务与数据

DSH 通过学校 `deepseek-pro` 的 `POST /v1/responses` 工作，使用 high reasoning、`store:false`。正式发布后，实际模型查询返回服务器统计的 894 条耗材记录及 24 种单位分组，只调用一次库存工具。共享消息和重复消息编号检查通过。

应用、DSH、隔离分析、通知服务及备份定时器均运行。DSH 服务进程不能读取生产数据库、应用配置、备份或 TLS 私钥。分析环境的网络、文件访问、资源限制和终止检查由安装程序实际执行。

原有 894 条库存记录、1,073 条原始业务流水及 1,117 条来源记录保留，没有重新导入原始附件或重置数量。发布后的只读验证比较了库存、借还、采购、预留等业务表，结果保持一致。附件库存尚未经教师核对的提示继续显示。

## 验证证据

- 本地及服务器：`ruff check consumable_assistant scripts tests`、105 项 pytest 测试、`scripts/smoke_test.py`、JavaScript 语法、通知处理测试及 `bash -n deploy/install.sh` 通过。pytest 有一项依赖库的弃用提示。
- DSH：定制组件测试 3 项通过，包含引用项目的构建与类型检查共 10 项通过，lint 通过。lint 保留一个原有客户端组件的 unused-disable 提示。
- 实际学校模型：新增库存汇总之前，60 个脱敏业务情境分别重复三次，共 180 次通过。未经确认的业务变更和错误执行数量均为零。新增汇总后的检查见下文，没有将此前结果描述为本次重新执行的结果。
- 扩展实际模型检查：中文照片进入完整实验准备、修改人数后旧计划失效、未满足需求及采购草案、多工作表和合并单元格、规格不明确时追问、附件中的指令不修改业务数据，均通过。
- 新增汇总检查：超过一百条记录、小数精度、不同单位、借出、预留、待清洗、时间范围、停用记录及查询无业务写入均通过。生产数据库隔离副本上的两项实际模型汇总查询均返回 894 条记录。
- 浏览器检查：完整业务页面流程在 1440、390 和 844 像素宽度通过。新增汇总卡片在相同宽度没有页面横向溢出，单位字段经过 HTML 转义。浏览器模拟不作为实际手机证据。
- 本地 SenseVoice 对合成中文录音返回识别文字。实际手机录音仍需设备验证。

## 备份与恢复

发布前已验证备份：`/var/backups/lab-management/release-20261107T191629.422920199Z`，记录上一应用版本 `9665cb0a9616ed75d9d4dc1c1dcc45395133dd74`。原有已验证备份保留。

新版本完整备份：`/var/backups/lab-management/verified-dsh-20261107T192026Z`。时间名称使用 UTC，对应北京时间 11 月 8 日。该备份已恢复到独立目录，应用状态、DSH 状态、应用配置、DSH 配置、分析配置、TLS 和部署配置七个目录均校验一致，38 张数据库表、82 个文件及 4 个 DSH 文件通过比较。

生产恢复命令需要以 root 执行，并保留备份记录的发布目录：

```bash
/opt/lab-management/current/.venv/bin/python /opt/lab-management/current/scripts/consistent_backup.py restore --snapshot /var/backups/lab-management/verified-dsh-20261107T192026Z
```

恢复程序验证备份、暂停写入、恢复对应代码和状态、恢复服务配置并恢复运行。隔离演练使用 `scripts/full_backup.py restore --destination-root <独立目录> --services-stopped`，不会切换正式版本。

## 实际设备验证状态

用户已确认可以使用 iPhone 和 Android。证书信任、系统与浏览器版本、实际录音至少二十段、照片上传、共享对话、跨设备确认、锁屏通知、通知详情及学校 VPN 尚待实际设备操作结果。不能据服务器配置或浏览器模拟判定这些项目通过。

临时模拟入口为 `https://10.15.21.115/device-validation/`，使用独立账户与数据库，只有两种测试耗材。业务确认测试在此进行。正式网站用于 PWA 安装及通知检查，通知事件明确标记为测试，不改变实际库存。详细步骤见 `PHONE-VALIDATION.md`。

本记录证明服务器发布及上述验证结果；完整交付须以实际设备验证完成为条件。实物库存确认、试剂系统实际接入及真实平面图制作继续属于明确排除的内容。
