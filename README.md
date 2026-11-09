# 耗材智能管理助手 (Lab-Management-Copilot)

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![CI](https://github.com/Maotechh/Lab-Management-Copilot/actions/workflows/ci.yml/badge.svg)

这是一个面向化学实验教学的校内耗材管理应用，使用 FastAPI、SQLite、定制 DSH 和实验室专用网页。DSH 是负责持续对话和工具调用的框架。系统无需登录，首次写入需要填写操作人姓名，姓名未经身份核验。正式访问范围为校园网和学校 VPN。

## 功能

- 正式库存保留既有普通化学、有机化学、物理化学数据及其原始来源，附件数量仍等待教师核对。
- 按名称、别名、规格、实验室、位置查询库存。
- 使用实际 AI 模型识别入库、消耗、借用、归还、修正和预警设置。否定、未来计划、条件和更正必须经过校验，采购计划不会直接增加库存。
- 保留原始行、当前库存和操作记录。
- 可用、借出、待清洗、维修和待核对数量分别显示，支持低库存提醒及通用浏览器后台通知。
- 所有设备共享对话、任务和完整业务预览，业务变更经一次确认后执行。
- 支持 XLS、XLSX、CSV、清单照片和直接录音。语音在校内识别，文字可修改后发送。
- 完整实验准备按人数与组数计算、检查各位置数量、预留已有库存，并为未满足的需求建立采购草案。
- 借用记录包含人员、用途、目标位置和预计归还时间，支持部分归还、逾期查询、清洗、维修、破损及遗失处理。
- 实验模板按人数、小组数、演示和备用数量计算需求，支持时间预留、整份安排分配及执行记录。
- 采购记录支持负责人、预计到货、部分到货和取消，未到货数量不会成为可用库存。
- 库存上传先预览后确认，重复文件需要明确确认。支持实物清点、部分转移、包装换算、位置说明及照片。
- 写入使用请求编号、版本校验和数据库事务。电脑和手机保留表单草稿，并支持不确定请求的结果查询。

## 本地运行

1. 安装依赖：
```bash
python -m pip install -r requirements.txt
```

2. 导入种子数据（可选，第一次启动时也会自动检测导入）：
```bash
python scripts/seed_data.py
```

3. 启动服务：
```bash
uvicorn consumable_assistant.server:app --reload --host 127.0.0.1 --port 8000
```

本地库存表单可以独立运行；完整聊天和录音还需要[部署说明](deploy/README.md)中的 DSH lab 配置与语音模型。正式模型统一使用学校 `deepseek-pro` 的 `POST /v1/responses`，推理等级为 `high`，不使用未经能力验证的备用模型。模型故障会显示错误并保留输入。仅在隔离测试中设置 `GENAI_REQUIRED=0`。

浏览器访问：

```text
http://127.0.0.1:8000
```

本地默认使用 simulation 配置，空库存时可以导入三份示例附件。正式配置使用 `CONSUMABLE_PROFILE=production` 和 `CONSUMABLE_AUTO_SEED=0`，禁止因部署或迁移重新导入附件。

XLS 使用 xlrd 读取，XLSX 使用 openpyxl 读取，不执行宏、外部链接或公式代码。

## 数据规则

- 数量计算使用 `Decimal`，新增数量字段保存十进制文本，单位换算必须经过人工确认。
- 迁移前建立备份，保留原始数据和历史记录。负数量及不确定的历史借还记录需要实物核对。
- 总拥有量包含各库存状态，预留不会增加总拥有量。
- 借出减少可用数量，归还按物品状态处理；消耗、破损和遗失减少总拥有量。
- 撤销使用关联的反向记录，校验数量依赖并保留后续变更。

## 代码检查与测试

测试使用隔离数据库，不修改正式库存：

```bash
python -m pip install -r requirements-dev.txt
ruff check consumable_assistant scripts tests
python -m pytest -q tests
python scripts/smoke_test.py
node --check consumable_assistant/static/app.js
node --check consumable_assistant/static/operations.js
node --check consumable_assistant/static/mobile.js
node --check consumable_assistant/static/interface.js
node --check consumable_assistant/static/theme.js
bash -n deploy/install.sh
```

网页直接复用指定 DSH 网页版本的主题、图标与 Markdown 排版，构建和来源参见 [frontend/README.md](frontend/README.md)。

`scripts/check_dsh_interface.cjs` 检查聊天界面状态和手机导航，`scripts/check_chat_history.cjs` 检查历史分页。`scripts/browser_check.cjs` 使用 Playwright 检查电脑和手机流程，应仅连接独立测试环境。正式 AI 接口的实际请求需要另行验证。

## 部署与维护

生产部署使用 Nginx、systemd、独立 DSH、临时分析容器和通知进程。每小时暂停持久数据写入并保存完整备份，既有备份继续保留。模型健康每五分钟检查。浏览器后台通知需要可信 HTTPS、有效推送配置和设备授权，只发送通用文字。

教师操作参见[教师说明](deploy/TEACHER-GUIDE.md)，维护参见[部署说明](deploy/README.md)。各次部署记录分别保存当时的实际验证结果。校园网、学校 VPN、可信 HTTPS 和实际手机通知分别验证。

管理系统的修改在验证后同步到此 GitHub 仓库，部署版本记录对应提交。密钥、正式数据库、备份、上传照片和无关报告不进入仓库。

## 参与贡献

欢迎提交 Issue 和 Pull Request！参与开发前请查阅 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 许可协议

本项目基于 [MIT License](./LICENSE) 协议开源。
