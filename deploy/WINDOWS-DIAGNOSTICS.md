# Windows 开发与故障诊断

从 GitHub 上的 `codex/dsh-lab-delivery` 获取 `scripts/lab_terminal.py`。Windows PowerShell 需要 Python 3.11 或更新版本，脚本只用 Python 自带的模块。使用 WSL 或 macOS 时，将示例中的 `py -3` 换成 `python3`。

## 从 Windows 读取网站错误

连接校园网或学校 VPN，在仓库目录运行以下命令。PowerShell 会显示可复制给 AI 的 JSON 报告。

```powershell
py -3 scripts/lab_terminal.py check
```

如果报告显示 `certificate_verification`，请向维护者获取实验室根证书并核对指纹。可以只为这条命令指定该证书。

```powershell
py -3 scripts/lab_terminal.py check --ca .\lab-root-ca.crt
```

遇到聊天失败，在网页的失败状态旁选择“复制运行编号”。将编号交给 AI，或将它传给下方命令。结果包含这一轮的状态和错误代码。命令只读取已有的记录。

```powershell
$runId = (Get-Clipboard -Raw).Trim()
py -3 scripts/lab_terminal.py check --run-id $runId
```

本地开发可以使用 `check --url http://127.0.0.1:8000`。报告里的 `model_health` 是应用最近一次模型检查的结果。即使它显示 `available`，实际聊天仍可能失败，应继续核对本轮的 `chat`。

## 从学校堡垒机读取服务器诊断

同学使用自己的学校账号，在资产页面进入已获授权的实验室服务器，并等待服务器终端显示命令提示符。由维护者确定可访问的服务器及账号权限。原有 Mac 的 `lab-terminal` 依赖该机器的 SSH 连接，Windows 版本通过已登录的终端运行诊断。

如果服务器已发布此工具，在已登录的服务器终端执行以下命令。诊断具体聊天时，可以加上 `--run-id` 和复制的运行编号。

```bash
python3 /opt/lab-management/current/scripts/lab_terminal.py server
```

如果服务器还没有这个文件，或需要使用同学本地的版本，可在 PowerShell 将诊断命令复制到剪贴板，再将整段内容粘贴到已登录的服务器终端，按回车执行。命令使用 Python 标准库，可以先去掉 `| Set-Clipboard` 查看将要执行的内容。

```powershell
py -3 scripts/lab_terminal.py server-command | Set-Clipboard
# 已从网页复制运行编号时，改用以下两行。
$runId = (Get-Clipboard -Raw).Trim()
py -3 scripts/lab_terminal.py server-command --run-id $runId | Set-Clipboard
```

把终端中的 JSON 报告转给 AI。服务器报告包括应用及 DSH 本机状态、系统服务、最近的日志错误类型、磁盘余量和当前发布版本。对指定的失败运行，程序尝试在对应时间内识别 DSH 的模型错误代码和 HTTP 状态，例如 `AUTH` 与 `401`。若同一秒内有多个可能的记录，会标明无法唯一匹配。读取权限取决于服务器账号，部分字段可能为空。

### 可以直接发给同学的 AI

> 请先使用 `scripts/lab_terminal.py check` 检查网站。若我提供了失败运行编号，请用 `--run-id` 检查该轮。若要区分网站、DSH 和发布服务的故障，请生成 `server-command`，我会在自己已登录的服务器终端执行，再把 JSON 报告发给你。请依据报告解释报错及下一步，并在独立开发分支修改代码。
