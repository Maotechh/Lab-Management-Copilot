# 2026 年 11 月 8 日发布记录

## 正式版本

- 正式网址：`https://10.15.21.115`，限制在校园网及学校 VPN 内访问，不要求登录。HTTP 根页面提供证书安装说明。指定实验室 CA 后，HTTPS 正式配置检查通过；电脑 Chrome 的系统证书信任仍未完成。
- 应用分支和提交：`codex/dsh-lab-delivery`，`1c924fa69dd0f3ca2a83248998ce14f7394ccf3d`。
- DSH 分支和提交：`Maotechh/deepseek-harness:codex/lab-copilot`，`a51d6a6df84930fb8d4aa0a9daf26523984b7560`，保留官方提交 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` 的历史。两个提交均已推送至各自远程仓库，没有强制推送。
- 应用发布目录：`/opt/lab-management/releases/20261108-1c924fa69d`，通过该版本的 `deploy/install.sh` 安装。
- DSH 使用 Node.js `24.22.2` 和 pnpm `11.28.5`。发布清单核对了 36,250 个文件；源依赖锁 SHA-256 为 `d65a6deb59aaaa61424ea8c832874b8745d09085c8c2ed37a1e8f1aeaa01017e`，部署依赖锁 SHA-256 为 `91931d6d75c9a669ef95abf0f6ef41af830ac4ba08e63f5972df19d47dba5c35`。
- DSH 调用学校 `deepseek-pro` 的 `POST /v1/responses`，使用 high reasoning 和 `store: false`。未配置校外模型备用服务。

## 服务和业务数据

正式应用、DSH、隔离分析、通知进程、定时完整备份和 Nginx 均处于运行状态。DSH 以独立低权限账户运行，不能读取正式数据库、配置、TLS 私钥或备份。分析程序在 rootless Podman 中运行。生产网站保留三份原始附件库存尚未经教师核对的提示。

正式数据在发布后的只读比较中保持一致：894 条库存、1,073 条原始业务流水和 1,117 条来源记录，没有重新导入附件或重置数量。发布后的查询通过实际学校 Responses 接口返回 894 条库存记录；第二设备可见共享对话，重复消息编号没有重复执行。查询前后生产业务表的记录数和校验值一致。

独立设备验证网页位于 `https://10.15.21.115/device-validation/`，使用独立模拟数据库，其中有两种测试耗材。其应用与 DSH 现在由持久 systemd 服务管理；更新后模型查询、第二设备读取共享对话和合成语音识别均通过，业务数据比较结果未变化。该模拟服务不能写入正式数据库。

## 自动和学校模型验证

- 应用检查：`ruff check consumable_assistant scripts tests`、111 项 pytest、`python scripts/smoke_test.py`、JavaScript 语法检查、通知处理检查和 `bash -n deploy/install.sh` 全部通过。pytest 有一项依赖库弃用提示。
- DSH 定制组件：构建与类型检查十项通过，单元测试三项通过，lint 通过。lint 保留一个既有客户端组件的 unused-disable 提示；构建输出另有关于 Vite 原生 tsconfig 路径解析的建议。
- Linux 服务器隔离数据库上的学校模型验证：历史导出 3／3，生命周期情境 60／60，常用操作情境 180／180。合计 243 项实际学校模型验证任务。要求重复执行的情境均连续通过三次；未确认的业务变更和错误执行数量均为零。报告另行保留了先前失败结果。
- 生产只读检查使用实际学校模型查询库存汇总，并检查服务器本地 SenseVoice 对合成音频的识别。合成音频检查仅证明服务器识别服务可用。

完整结果、构建日志和文件校验清单保存在服务器的 `/var/backups/lab-management/verification-20261108-1c924fa69d`，共 28 个证据文件。该目录仅维护者可访问。

## 备份和恢复验证

- 安装新版本前的完整备份：`/var/backups/lab-management/release-20261108T154344.206968349Z`，安装程序同时在隔离目录恢复并校验该版本备份。
- 新版本验证备份：`/var/backups/lab-management/verified-dsh-20261108T155649Z`。已恢复到其 `restoration-rehearsal` 独立目录，没有替换正在使用的正式数据。应用、DSH、应用配置、DSH 配置、分析配置、TLS 和部署配置七个目录均通过比较；38 张数据库表、86 个文件及 8 个 DSH 会话文件一致。
- 独立设备验证数据备份：`/var/backups/lab-management/device-validation-20261108T155633Z`。

可在服务器上查看完整备份清单及恢复结果：

```text
/var/backups/lab-management/verification-20261108-1c924fa69d/lab-final-version-restoration-result.json
/var/backups/lab-management/verification-20261108-1c924fa69d/evidence-manifest.json
```

恢复到本次验证快照时，以 root 执行协调恢复程序：

```bash
/opt/lab-management/current/.venv/bin/python /opt/lab-management/current/scripts/consistent_backup.py restore --snapshot /var/backups/lab-management/verified-dsh-20261108T155649Z
```

恢复前须确认对应的不可变应用发布目录仍存在。协调程序会校验快照、停止写入服务、恢复七类数据和配置、选择匹配的应用版本，再恢复备份中运行的服务。

## 设备验证状态

用户报告 iPhone 与 Android 均可正常打开模拟网页并完成基本录音、识别文字进入可编辑输入框及共享消息检查。证书无警告也来自用户报告。尚未记录两部手机的系统与浏览器版本，也没有完成二十段录音、照片上传、跨设备业务确认、PWA 安装、锁屏通知和通知点击检查。用户要求通知部分稍后处理，因此没有安排测试通知。学校 VPN 使用情况尚未确认。

电脑使用 macOS 15.6、Chrome 157.0.8103.38。指定实验室根证书的命令行 HTTPS 检查通过；Chrome 仍显示 `ERR_CERT_AUTHORITY_INVALID`。没有绕过警告或改变系统证书信任。浏览器信任和实际校园网／学校 VPN 访问仍待验证。证书指纹、手机步骤和录音清单见 [PHONE-VALIDATION.md](PHONE-VALIDATION.md)。

实物库存核对、试剂系统实际接入和真实平面图制作不属于本次发布范围。手机后台通知验证仍未完成，因此本记录证明正式部署及服务器验证完成，不表示所有设备验收项目均已完成。
