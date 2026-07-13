# 耗材智能管理助手 (Lab-Management-Copilot)

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![CI](https://github.com/Maotechh/Lab-Management-Copilot/actions/workflows/ci.yml/badge.svg)

这是一个面向化学实验教学平台的校内耗材管理 Web 应用。提供现代化的极简界面，包含**库存查询、自然语言多目标识别改数、库存预警、实验准备清单自动解析、以及一键 Excel 导出**等功能。

## 功能

- 自动导入 `Attachment` 目录下的普通化学、有机化学、物理化学耗材数据。
- 按名称、别名、规格、实验室、位置查询库存。
- 用文本识别入库、消耗、借出、归还、修正和预警设置。
- 保留原始行、当前库存和操作记录。
- 低库存条目在页面内显示，并支持浏览器通知。
- 上传实验准备 Excel，生成库存核对和采购清单。

## 🚀 快速运行

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
uvicorn consumable_assistant.server:app --reload --host 0.0.0.0 --port 8000
```

> **🔑 高级特性：大语言模型支持**  
> 如果要启用基于大模型（GenAI）的复杂自然语言识别功能，请复制项目根目录下的 `.env.example` 为 `.env.local`，并填入相应的 `GENAI_API_KEY` 等参数。

浏览器打开：

```text
http://127.0.0.1:8000
```

第一次启动服务时，如果 `data/consumables.db` 不存在或没有库存条目，应用会自动导入 `Attachment` 目录里的三份数据。

读取 `.xls` 文件需要系统可以运行 `soffice`，也就是 LibreOffice 的命令行程序。若服务器没有安装 LibreOffice，可以先把 `.xls` 转成 `.xlsx` 后再导入。

## 数据规则

- 完全相同的原始行只导入一次。
- 同名、同位置、同规格但数量不同的行按批次相加。
- 当前库存由初始导入和后续操作记录共同得到。
- 借出和归还只保留记录，不改变当前库存数量。
- 消耗会减少当前库存，入库会增加当前库存。

## ✅ 代码检查与测试

本项目配置了自动化测试，如果你修改了核心逻辑，可以通过以下命令快速自测：

```bash
python scripts/smoke_test.py
```

该脚本会在内存临时数据库中导入示例数据，检查搜索逻辑、文本解析引擎、并发库存变更以及实验清单比对算法的准确性。

## 🤝 参与贡献

欢迎提交 Issue 和 Pull Request！参与开发前请查阅 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 📄 许可协议

本项目基于 [MIT License](./LICENSE) 协议开源。
