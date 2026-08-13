# update_dns

一个轻量级的多服务商 DDNS 脚本：获取当前公网 IPv4 地址，并将其同步到阿里云 DNS 和 Cloudflare 的 A 记录。

脚本会在本地保存每个服务商最后一次成功写入的地址。公网 IP 未变化时不会重复调用 DNS 更新接口；如果某一家更新失败，下次运行时仍会单独重试该服务商。

## 功能

- 从可配置的公网 IP 查询服务获取 IPv4 地址
- 同步阿里云 DNS A 记录
- 同步 Cloudflare DNS A 记录（DNS only，不启用代理）
- 对临时网络错误和 `429`、`5xx` 响应自动重试
- 原子写入状态文件，避免中途退出破坏状态
- 仅在公网 IP 变化时追加一条历史记录
- 任一服务商更新失败时返回非零退出码，便于定时任务监控

## 环境要求

- Python 3.9 或更高版本
- 可访问公网 IP 查询服务、阿里云 API 和 Cloudflare API 的网络
- 已在阿里云 DNS 和 Cloudflare 中创建需要更新的 A 记录

## 安装

### Linux / macOS

```bash
git clone https://github.com/ljskr/update_dns.git
cd update_dns
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

### Windows PowerShell

```powershell
git clone https://github.com/ljskr/update_dns.git
Set-Location update_dns
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 配置

配置从项目目录下的 `.env` 文件读取。先复制示例文件，再填写真实值：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

`.env` 已加入 `.gitignore`，不要强制提交该文件。系统环境变量的优先级高于 `.env` 中的同名配置，便于在容器或 CI 中覆盖配置。

| 环境变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ALIYUN_ACCESS_KEY_ID` | 是 | - | 阿里云 AccessKey ID |
| `ALIYUN_ACCESS_KEY_SECRET` | 是 | - | 阿里云 AccessKey Secret |
| `ALIYUN_REGION_ID` | 否 | `cn-shenzhen` | 阿里云 API Region ID |
| `ALIYUN_RECORD_ID` | 是 | - | 要更新的阿里云解析记录 ID，不是域名 |
| `ALIYUN_RECORD_RR` | 是 | - | 主机记录，例如 `home`；根域名通常为 `@` |
| `CF_API_TOKEN` | 是 | - | Cloudflare API Token，需要对应 Zone 的 DNS 编辑权限 |
| `CF_ZONE_ID` | 是 | - | Cloudflare Zone ID |
| `CF_RECORD_ID` | 是 | - | 要更新的 Cloudflare DNS 记录 ID |
| `CF_DNS_NAME` | 是 | - | 完整记录名，例如 `home.example.com` |
| `GET_IP_URL` | 否 | `https://ipv4.ddnspod.com` | 返回纯文本 IPv4 地址的查询 URL |
| `DDNS_HOME` | 否 | 脚本目录 | 运行数据的基础目录；相对路径基于脚本目录，不受启动时工作目录影响 |
| `DDNS_STATE_FILE` | 否 | `ddns_state.json` | 同步状态文件；相对路径基于 `DDNS_HOME` |
| `STORE_IP_FILE_PATH` | 否 | `ip_history.txt` | 公网 IP 变更历史；相对路径基于 `DDNS_HOME` |
| `DDNS_LOG_FILE` | 否 | `update_dns.log` | 运行日志；相对路径基于 `DDNS_HOME`，父目录不存在时自动创建 |

阿里云的记录 ID 可通过云解析 DNS API 的 `DescribeDomainRecords` 查询。Cloudflare 的 Zone ID 可在域名概览页查看，记录 ID 可通过“列出 DNS 记录”API 查询。

### `.env` 示例

```dotenv
DDNS_HOME=/root/update_dns
STORE_IP_FILE_PATH=ip_history.txt
DDNS_STATE_FILE=ddns_state.json
DDNS_LOG_FILE=update_dns.log

ALIYUN_ACCESS_KEY_ID=your-access-key-id
ALIYUN_ACCESS_KEY_SECRET=your-access-key-secret
ALIYUN_REGION_ID=cn-shenzhen
ALIYUN_RECORD_ID=1234567890
ALIYUN_RECORD_RR=home

CF_API_TOKEN=your-cloudflare-api-token
CF_ZONE_ID=your-zone-id
CF_RECORD_ID=your-record-id
CF_DNS_NAME=home.example.com
```

配置完成后运行：

```bash
python3 update_dns.py
```

Windows PowerShell：

```powershell
python .\update_dns.py
```

## 运行结果

首次运行会更新两家服务商，并在 `DDNS_HOME` 目录生成（未配置时即脚本目录）：

- `ddns_state.json`：保存每家服务商最后成功写入的 IP 和时间
- `ip_history.txt`：按 `IP<TAB>时间` 格式记录公网 IP 的变化历史
- `update_dns.log`：以 UTF-8 编码追加保存运行日志；日志同时输出到控制台

之后的行为如下：

- 当前 IP 与某服务商的成功状态一致：跳过该服务商
- 当前 IP 已变化：更新两家服务商
- 仅一家更新失败：保留另一家的成功状态，下次只重试失败的一家
- 状态文件不存在或损坏：重新同步全部记录

进程在全部同步成功或无需更新时返回 `0`；公网 IP 获取、状态保存或任一 DNS 更新失败时返回 `1`。

## 定时运行

### cron（每 5 分钟）

创建启动脚本，切换到仓库目录并执行虚拟环境中的 Python，然后添加 cron：

```cron
*/5 * * * * /absolute/path/to/run-update-dns.sh
```

脚本本身会写入 `DDNS_LOG_FILE`，无需再通过 cron 重定向输出。若仍需收集 cron 启动层面的错误，可按运行环境另行配置重定向。

也可以直接使用 Python 命令；相对运行文件路径会基于 `DDNS_HOME`，不会写入 cron 的当前工作目录：

```cron
*/5 * * * * /root/update_dns/venv/bin/python3 /root/update_dns/update_dns.py
```

确保启动脚本权限足够严格，例如：

```bash
chmod 700 /absolute/path/to/run-update-dns.sh
```

### Windows 任务计划程序

可创建一个 PowerShell 启动脚本，切换到仓库目录后执行：

```powershell
& 'C:\absolute\path\to\update_dns\.venv\Scripts\python.exe' `
  'C:\absolute\path\to\update_dns\update_dns.py'
exit $LASTEXITCODE
```

在任务计划程序中将“程序或脚本”设为 `powershell.exe`，“添加参数”设为：

```text
-NoProfile -ExecutionPolicy Bypass -File "C:\absolute\path\to\run-update-dns.ps1"
```

建议使用最小权限账户运行，并限制包含 API 凭据的启动脚本只有该账户可读。

## 常见问题

### 提示缺少配置

确认项目目录下存在 `.env`，且错误消息所指的配置项已填写。脚本始终按自身文件位置查找 `.env`，不受当前工作目录影响；系统环境变量如有同名配置则会覆盖文件值。

### 获取公网 IPv4 失败

确认 `GET_IP_URL` 返回的响应体只有一个公网 IPv4 地址。内网地址、IPv6 地址、HTML 页面或带额外文本的响应都会被拒绝。

### Cloudflare 更新失败

确认 API Token 至少拥有目标 Zone 的 `DNS:Edit` 权限，并核对 `CF_ZONE_ID` 和 `CF_RECORD_ID`。脚本会把记录设置为 `proxied: false`。

### 阿里云更新失败

确认 AccessKey 对云解析 DNS 具有更新记录的权限，并核对 `ALIYUN_RECORD_ID` 与 `ALIYUN_RECORD_RR` 是否属于同一条记录。

## 安全建议

- 为脚本创建仅具 DNS 更新权限的专用凭据
- 不要在日志、Shell 历史或仓库中保存密钥
- 限制状态文件、历史文件和定时任务启动脚本的访问权限
- 定期轮换 API Token 和 AccessKey

## License

本仓库当前未声明开源许可证。未经作者许可，不应假定拥有复制、修改或再分发代码的权利。
