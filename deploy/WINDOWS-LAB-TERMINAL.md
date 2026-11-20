# Windows 堡垒机终端

同学完成学校认证后，他的 AI 可以用 `lab-terminal run` 直接在已授权的实验室服务器上执行命令，并读取输出和退出状态。请把本说明发给同学，并确认他的学校账号在资产页面中有对应服务器的 SSH 权限。

## 安装

在 Windows Terminal 中打开 WSL2 的 Ubuntu。登录窗口和 AI 执行命令的环境需要使用同一个 WSL 发行版及 Linux 用户。首次获取工具时，在 Ubuntu 中执行以下命令：

```bash
sudo apt-get update
sudo apt-get install -y git openssh-client python3
git clone --single-branch --branch codex/windows-lab-terminal https://github.com/Maotechh/Lab-Management-Copilot.git ~/lab-terminal-tools
cd ~/lab-terminal-tools
install -D -m 0755 scripts/lab-ssh "$HOME/.local/bin/lab-terminal"
export PATH="$HOME/.local/bin:$PATH"
```

使用 Bash 时，可以把上述 `export PATH` 写入自己的 `~/.bashrc`，供以后打开的终端使用。也可直接调用 `~/.local/bin/lab-terminal`。

## 建立连接

1. 在[学校资源访问页面](https://10.15.66.32/shterm/#/business/resourceaccess)用自己的学校账号登录，找到已获授权的实验室资产。按学校提供的 OpenSSH 或透传访问说明取得 SSH 目标、端口及所需的连接选项。若学校页面只提供 Windows AccessClient，需先确认学校是否为该账号提供可以在 WSL 中使用的 OpenSSH 入口。Windows 客户端窗口里的会话无法直接由 WSL 的 OpenSSH 复用。
2. 在一个专用的 Ubuntu 窗口运行下面的命令。示例中的端口和 SSH 目标需要替换为学校实际提供的值。若学校提供 OpenSSH 配置文件或身份文件，可以分别用 `-F 文件路径` 或 `-i 文件路径`。登录时按学校提示完成认证，并在命令提示符出现后保留该窗口。

```bash
lab-terminal login -p 端口 '学校提供的SSH目标'
```

退出登录窗口后，共享连接随之关闭。学校要求重新认证时，在专用窗口重新运行 `login`。工具只保存本地连接参数，由 OpenSSH 管理会话和认证。

## 让 AI 执行命令

把下面的命令交给 AI。Windows 原生 AI 的命令执行环境可以用 `wsl.exe -d Ubuntu --exec /home/你的Linux用户名/.local/bin/lab-terminal` 替换 `lab-terminal`。发行版名称以 `wsl.exe -l -q` 的输出为准。

```bash
lab-terminal --check
lab-terminal run 'hostname; date'
lab-terminal run 'systemctl is-active consumable-assistant.service consumable-dsh.service'
lab-terminal run 'journalctl -u consumable-assistant.service -n 25 --no-pager'
lab-terminal run 'python3 /opt/lab-management/current/scripts/lab_terminal.py server'
```

`run` 每次打开一个新的服务器 shell，将标准输出和错误输出合在一起返回，并传回远端命令的退出状态。适合无需键盘输入的命令。要进入交互终端，用 `lab-terminal --shell`。要延长执行时间，用 `lab-terminal run --timeout 120 '命令'`。遇到断线或超时时，先核对服务器上的实际执行结果，再决定是否重试。

可以把这段话发给同学的 AI：

> 我会在 WSL 中用学校账号完成堡垒机认证并保留登录窗口。请在同一个 WSL 发行版和 Linux 用户下先运行 `lab-terminal --check`，随后用 `lab-terminal run '命令'` 读取服务器状态、日志和退出状态。程序提示连接不可用时，请把原始错误交给我，由我重新登录。根据实际授权范围操作服务器。
