# 贡献指南 (Contributing Guidelines)

感谢你考虑为 **耗材智能管理助手 (Lab-Management-Copilot)** 做出贡献！

## 快速开始

1. **Fork 并克隆仓库**
   ```bash
   git clone https://github.com/你的用户名/Lab-Management-Copilot.git
   cd Lab-Management-Copilot
   ```

2. **安装依赖**
   建议使用 Python 3.11 或以上版本：
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # macOS/Linux
   # .venv\Scripts\activate   # Windows
   pip install -r requirements.txt
   ```

3. **运行服务**
   ```bash
   uvicorn consumable_assistant.server:app --reload --host 0.0.0.0 --port 8000
   ```

4. **配置环境变量 (可选)**
   复制 `.env.example` 为 `.env.local` 并填入你的 API Key，以测试大模型功能。

## 代码规范与测试

本项目使用 GitHub Actions 进行代码检查。提交代码前，请在本地运行：
```bash
pip install ruff
ruff check .
ruff format .
python scripts/smoke_test.py
```
确保 `smoke_test.py` 成功通过，并且没有 Ruff 报错。

## 提交 Pull Request
- 请确保你的 PR 描述清晰。
- 遵循本项目内置的 PR 模板。
- 提交前请删除测试代码或个人敏感信息。
