# 耗材智能管理助手

这是一个面向化学实验教学平台的校内耗材管理 Web 应用。当前版本包含库存查询、聊天改数、库存预警、实验准备清单解析和 Excel 导出。

## 功能

- 自动导入 `Attachment` 目录下的普通化学、有机化学、物理化学耗材数据。
- 按名称、别名、规格、实验室、位置查询库存。
- 用文本识别入库、消耗、借出、归还、修正和预警设置。
- 保留原始行、当前库存和操作记录。
- 低库存条目在页面内显示，并支持浏览器通知。
- 上传实验准备 Excel，生成库存核对和采购清单。

## 运行

```bash
python -m pip install -r requirements.txt
python scripts/seed_data.py
uvicorn consumable_assistant.server:app --reload --host 0.0.0.0 --port 8000
```

如果要启用校内 GenAI 接口，在项目根目录放一个 `.env.local`，写入 `GENAI_API_KEY`、`GENAI_MODEL=deepseek-pro` 和 `GENAI_MODE=completion` 即可。

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

## 检查

```bash
python scripts/smoke_test.py
```

该脚本会在临时数据库中导入三份数据，检查搜索、文本识别、库存变更和实验清单解析。
