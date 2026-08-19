# update_dns

一个轻量级的多服务商 DDNS 脚本：获取当前公网 IPv4 地址，并按配置同步到阿里云 DNS、Cloudflare 或公云（3322）动态 DNS。

脚本会在本地保存每个服务商最后一次成功写入的地址。公网 IP 未变化时不会重复调用 DNS 更新接口；如果某一家更新失败，下次运行时仍会单独重试该服务商。

## 功能

- 从可配置的公网 IP 查询服务获取 IPv4 地址
- 同步阿里云 DNS A 记录
- 同步 Cloudflare DNS A 记录（DNS only，不启用代理）
- 通过 `members.3322.net` 兼容接口同步公云（3322）动态域名
- 在 `config.yml` 中分别启用更新方式并配置任意数量的账号
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

配置统一从项目目录下的 `config.yml` 读取。先复制示例文件，再填写真实值：

```bash
cp config.yml.example config.yml
```

Windows PowerShell：

```powershell
Copy-Item config.yml.example config.yml
```

`config.yml` 已加入 `.gitignore`，不要提交包含真实凭据的文件。完整结构见 `config.yml.example`：

```yaml
general:
  get_ip_url: https://ipv4.ddnspod.com
  home: /root/update_dns
  ip_history_file: ip_history.txt
  state_file: ddns_state.json
  log_file: update_dns.log

providers:
  aliyun:
    enabled: true
    accounts:
      - name: home
        access_key_id: key-1
        access_key_secret: secret-1
        region_id: cn-shenzhen
        record_id: record-1
        record_rr: home
      - name: office
        access_key_id: key-2
        access_key_secret: secret-2
        region_id: cn-shanghai
        record_id: record-2
        record_rr: office

  cloudflare:
    enabled: true
    accounts:
      - name: home
        api_token: token-1
        zone_id: zone-1
        record_id: record-1
        dns_name: home.example.com
      - name: office
        api_token: token-2
        zone_id: zone-2
        record_id: record-2
        dns_name: office.example.net

  "3322":
    enabled: false
    accounts: []
```

每种方式通过 `enabled` 开关控制。每个账号的 `name` 必须唯一，且不能包含空白或冒号；它也用于生成 `aliyun:home`、`cloudflare:office` 等独立状态键。某个账号失败后，下次只重试该账号。

阿里云记录 ID 可通过云解析 DNS API 的 `DescribeDomainRecords` 查询。Cloudflare 的 Zone ID 可在域名概览页查看，记录 ID 可通过“列出 DNS 记录”API 查询。

配置完成后运行：

```bash
python3 update_dns.py
```

Windows PowerShell：

```powershell
python .\update_dns.py
```

## 运行结果

首次运行会更新所有已启用实例，并在 `DDNS_HOME` 目录生成（未配置时即脚本目录）：

- `ddns_state.json`：保存每家服务商最后成功写入的 IP 和时间
- `ip_history.txt`：按 `IP<TAB>时间` 格式记录公网 IP 的变化历史
- `update_dns.log`：以 UTF-8 编码追加保存运行日志；日志同时输出到控制台

之后的行为如下：

- 当前 IP 与某服务商的成功状态一致：跳过该服务商
- 当前 IP 已变化：更新所有已启用的服务商
- 仅一个实例更新失败：保留其他实例的成功状态，下次只重试失败实例
- 状态文件不存在或损坏：重新同步全部已启用记录

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

确认项目目录下存在 `config.yml`，YAML 缩进正确，且错误消息所指的字段已填写。脚本始终按自身文件位置查找配置文件，不受启动时工作目录影响。

### 获取公网 IPv4 失败

确认 `GET_IP_URL` 返回的响应体只有一个公网 IPv4 地址。内网地址、IPv6 地址、HTML 页面或带额外文本的响应都会被拒绝。

### Cloudflare 更新失败

确认 API Token 至少拥有目标 Zone 的 `DNS:Edit` 权限，并核对 `zone_id` 和 `record_id`。脚本会把记录设置为 `proxied: false`。

### 阿里云更新失败

确认 AccessKey 对云解析 DNS 具有更新记录的权限，并核对 `record_id` 与 `record_rr` 是否属于同一条记录。

## 安全建议

- 为脚本创建仅具 DNS 更新权限的专用凭据
- 不要在日志、Shell 历史或仓库中保存密钥
- 限制状态文件、历史文件和定时任务启动脚本的访问权限
- 定期轮换 API Token 和 AccessKey

## License

本仓库当前未声明开源许可证。未经作者许可，不应假定拥有复制、修改或再分发代码的权利。
