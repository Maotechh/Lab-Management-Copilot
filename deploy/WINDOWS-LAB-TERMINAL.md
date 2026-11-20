# Windows AccessClient 与 lab-agent

像平时一样从学校的 AccessClient 进入实验室服务器。等它打开的 PowerShell 窗口显示 `root@hxsy-zngl:~#` 后，保留这个窗口。在另一个 PowerShell 中运行 `lab-agent`，就能在同一服务器会话里发送命令。

## 首次安装

需要 Windows 版 Python 3.11 或更新版本，以及 Git。在普通 PowerShell 执行以下命令。

```powershell
git clone --single-branch --branch codex/windows-lab-terminal https://github.com/Maotechh/Lab-Management-Copilot.git "$HOME\lab-terminal-tools"
Set-Location "$HOME\lab-terminal-tools"
py -3 -m pip install --user .\scripts\lab-agent-windows
if ($LASTEXITCODE -ne 0) { throw 'lab-agent installation failed' }
$labAgentBin = (py -3 -c "import sysconfig; print(sysconfig.get_path('scripts', scheme='nt_user'))").Trim()
$labAgentUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($labAgentUserPath -split ';') -notcontains $labAgentBin) {
    $labAgentPaths = @($labAgentUserPath, $labAgentBin) | Where-Object { $_ }
    [Environment]::SetEnvironmentVariable('Path', ($labAgentPaths -join ';'), 'User')
}
if (($env:Path -split ';') -notcontains $labAgentBin) { $env:Path += ";$labAgentBin" }
lab-agent --help
```

如果 AI 工具已经运行，请重新启动它，以便它读取更新后的 `PATH`。已经有仓库的同学可以在自己的仓库中切换到 `codex/windows-lab-terminal`，再从 `py -3 -m pip install` 开始执行。更新分支以后重新运行安装命令。

## 使用

```powershell
lab-agent
```

进入后，在 `hxsy-zngl>` 输入 `hostname`、`date` 等命令。输入 `exit` 退出工具，AccessClient 会话继续保持。

AI 工具可以在自己的 PowerShell 中执行以下命令。

```powershell
lab-agent check
lab-agent run 'hostname; date'
lab-agent run 'systemctl is-active consumable-assistant.service consumable-dsh.service'
lab-agent run 'journalctl -u consumable-assistant.service -n 10 --no-pager'
$LASTEXITCODE
```

Windows PowerShell 5.1 在传递给外部程序时可能改变命令里的双引号。含有双引号或多行内容的命令可以通过 `run --stdin` 以 UTF-8 传入。

```powershell
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
@'
printf '%s\n' "Hello from the lab"
date
'@ | lab-agent run --stdin
```

运行时将原 AccessClient 窗口留在提示符。每次命令在自己的 Bash 进程执行，可以用 `cd /目录 && 命令` 指定工作目录。标准输出和错误输出会合并，在第二个 PowerShell 中显示，退出状态可通过 `$LASTEXITCODE` 读取。原窗口通过 Base64 传递输出文本，以便工具还原换行和中文。如果日志超过控制台的容量，可以用 `-n` 或 `tail` 分段查询。如果超时，先核对实际执行结果，再决定是否重试。

## 控制台连接错误

在第二个 PowerShell 运行 `lab-agent check --diagnose`，工具会列出候选进程和 Windows 错误编号。`Windows error 6` 表示所选进程没有可连接的控制台。使用 `check --diagnose` 自动选择进程。只在结果显示多个实验室会话时，才用工具列出的 PID 明确选择，例如 `lab-agent --pid 1234 run 'hostname'`。

若自动查找仍找不到会话，确认 AccessClient 的窗口还显示 Linux 提示符。如果该窗口由 Windows Terminal 承载，在 Windows Terminal 的「设置 → 启动 → 默认终端应用程序」选择 **Windows Console Host**。从 AccessClient 重新连接后，再运行 `lab-agent check --diagnose`。`Windows error 5` 则应核对两个窗口是否由同一 Windows 用户、以相同的管理员权限运行。

可以把下一段交给 AI。

> 我已通过 AccessClient 连接实验室服务器。请在 Windows PowerShell 用 `lab-agent check` 检查会话，再用 `lab-agent run '命令'` 执行并读取输出和退出状态。遇到长日志，请限制输出行数。出现超时先核对实际执行结果。
