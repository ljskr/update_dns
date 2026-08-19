#!/usr/bin/env python3
"""Update enabled DDNS providers when the locally observed IPv4 changes."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
import tempfile
from datetime import datetime
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from dotenv import load_dotenv

from aliyunsdkcore import client
from aliyunsdkalidns.request.v20150109 import UpdateDomainRecordRequest


# ======================================================
# 全局配置（从脚本所在目录的 .env 读取，已有系统环境变量优先）
CUR_DIR = os.path.dirname(os.path.realpath(__file__))
load_dotenv(os.path.join(CUR_DIR, ".env"), override=False)


configured_home = os.environ.get("DDNS_HOME", "").strip()
configured_home = os.path.expanduser(configured_home or CUR_DIR)
if not os.path.isabs(configured_home):
    configured_home = os.path.join(CUR_DIR, configured_home)
DDNS_HOME = os.path.abspath(configured_home)


def resolve_config_path(name: str, default_name: str) -> str:
    """Resolve relative runtime paths against DDNS_HOME."""
    value = os.environ.get(name, default_name).strip() or default_name
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(DDNS_HOME, value)
    return os.path.abspath(value)


GET_IP_URL = os.environ.get("GET_IP_URL", "https://ipv4.ddnspod.com").strip()
DDNS_PROVIDERS = os.environ.get("DDNS_PROVIDERS", "aliyun,cloudflare").strip()
STORE_IP_FILE_PATH = resolve_config_path("STORE_IP_FILE_PATH", "ip_history.txt")
DDNS_STATE_FILE = resolve_config_path("DDNS_STATE_FILE", "ddns_state.json")
DDNS_LOG_FILE = resolve_config_path("DDNS_LOG_FILE", "update_dns.log")

# 阿里云配置
ALIYUN_ACCESS_KEY_ID = os.environ.get("ALIYUN_ACCESS_KEY_ID", "").strip()
ALIYUN_ACCESS_KEY_SECRET = os.environ.get("ALIYUN_ACCESS_KEY_SECRET", "").strip()
ALIYUN_REGION_ID = os.environ.get("ALIYUN_REGION_ID", "cn-shenzhen").strip()
ALIYUN_RECORD_ID = os.environ.get("ALIYUN_RECORD_ID", "").strip()
ALIYUN_RECORD_RR = os.environ.get("ALIYUN_RECORD_RR", "").strip()

# Cloudflare 配置
CF_API_BASE = "https://api.cloudflare.com/client/v4"
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
CF_ZONE_ID = os.environ.get("CF_ZONE_ID", "").strip()
CF_RECORD_ID = os.environ.get("CF_RECORD_ID", "").strip()
CF_DNS_NAME = os.environ.get("CF_DNS_NAME", "").strip().rstrip(".").lower()

# 公云（3322）动态域名配置
DYNDNS_3322_URL = os.environ.get(
    "DYNDNS_3322_URL", "https://members.3322.net/dyndns/update"
).strip()
DYNDNS_3322_USERNAME = os.environ.get("DYNDNS_3322_USERNAME", "").strip()
DYNDNS_3322_PASSWORD = os.environ.get("DYNDNS_3322_PASSWORD", "").strip()
DYNDNS_3322_HOSTNAME = (
    os.environ.get("DYNDNS_3322_HOSTNAME", "").strip().rstrip(".").lower()
)
# ======================================================


LOG = logging.getLogger("ddns")


def configure_logging() -> None:
    log_path = os.path.abspath(DDNS_LOG_FILE)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[console_handler, file_handler],
    )


def required_config(name: str, value: str) -> str:
    if not value:
        raise RuntimeError(f"缺少配置 {name}")
    return value


def make_http_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "PATCH"}),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.headers.update({"User-Agent": "multi-provider-ddns/1.0"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


HTTP = make_http_session()


def get_public_ipv4() -> str:
    response = HTTP.get(GET_IP_URL, timeout=(5, 10))
    response.raise_for_status()
    value = response.text.strip()
    address = ipaddress.ip_address(value)
    if address.version != 4 or not address.is_global:
        raise RuntimeError(f"公网 IP 服务返回了无效的 IPv4 地址：{value!r}")
    return str(address)


def aliyun_client() -> client.AcsClient:
    return client.AcsClient(
        required_config("ALIYUN_ACCESS_KEY_ID", ALIYUN_ACCESS_KEY_ID),
        required_config("ALIYUN_ACCESS_KEY_SECRET", ALIYUN_ACCESS_KEY_SECRET),
        ALIYUN_REGION_ID,
    )


def update_aliyun(ip: str) -> None:
    record_id = required_config("ALIYUN_RECORD_ID", ALIYUN_RECORD_ID)
    record_rr = required_config("ALIYUN_RECORD_RR", ALIYUN_RECORD_RR)
    acs = aliyun_client()

    update = UpdateDomainRecordRequest.UpdateDomainRecordRequest()
    update.set_accept_format("json")
    update.set_RecordId(record_id)
    update.set_RR(record_rr)
    update.set_Type("A")
    update.set_Value(ip)
    acs.do_action_with_exception(update)
    LOG.info("Aliyun 写入成功：%s -> %s", record_rr, ip)


def cf_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    token = required_config("CF_API_TOKEN", CF_API_TOKEN)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    response = HTTP.request(
        method,
        f"{CF_API_BASE}{path}",
        headers=headers,
        timeout=(5, 15),
        **kwargs,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Cloudflare API 失败：{payload.get('errors', payload)}")
    return payload


def update_cloudflare(ip: str) -> None:
    zone_id = required_config("CF_ZONE_ID", CF_ZONE_ID)
    record_id = required_config("CF_RECORD_ID", CF_RECORD_ID)
    dns_name = required_config("CF_DNS_NAME", CF_DNS_NAME)
    cf_request(
        "PATCH",
        f"/zones/{zone_id}/dns_records/{record_id}",
        json={"content": ip, "proxied": False},
    )
    LOG.info("Cloudflare 写入成功：%s -> %s（DNS only）", dns_name, ip)


def update_3322(ip: str) -> None:
    """通过 members.3322.net 兼容接口更新动态域名。"""
    url = required_config("DYNDNS_3322_URL", DYNDNS_3322_URL)
    username = required_config("DYNDNS_3322_USERNAME", DYNDNS_3322_USERNAME)
    password = required_config("DYNDNS_3322_PASSWORD", DYNDNS_3322_PASSWORD)
    hostname = required_config("DYNDNS_3322_HOSTNAME", DYNDNS_3322_HOSTNAME)
    response = HTTP.get(
        url,
        params={"hostname": hostname, "myip": ip},
        auth=(username, password),
        timeout=(5, 15),
    )
    response.raise_for_status()
    result = response.text.strip()
    if not (result.startswith("good ") or result.startswith("nochg ")):
        raise RuntimeError(f"3322 动态 DNS 更新失败：{result or '<empty response>'}")
    LOG.info("3322 动态 DNS 写入成功：%s -> %s（%s）", hostname, ip, result)


PROVIDER_OPERATIONS: dict[str, Callable[[str], None]] = {
    "aliyun": update_aliyun,
    "cloudflare": update_cloudflare,
    "3322": update_3322,
}


def enabled_providers(value: str = DDNS_PROVIDERS) -> list[str]:
    """解析逗号分隔的服务商列表，并拒绝拼写错误和空配置。"""
    names = list(dict.fromkeys(part.strip().lower() for part in value.split(",") if part.strip()))
    if not names:
        raise RuntimeError("DDNS_PROVIDERS 至少需要启用一种更新方式")
    unknown = [name for name in names if name not in PROVIDER_OPERATIONS]
    if unknown:
        supported = ", ".join(PROVIDER_OPERATIONS)
        raise RuntimeError(f"DDNS_PROVIDERS 包含未知方式：{', '.join(unknown)}；可选：{supported}")
    return names


def load_state() -> dict[str, Any]:
    try:
        with open(DDNS_STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
        if not isinstance(state, dict) or not isinstance(state.get("providers", {}), dict):
            raise ValueError("状态文件格式不正确")
        return state
    except FileNotFoundError:
        return {"providers": {}}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # 不信任损坏的状态：本次重新写入两家，避免错误跳过 DNS 更新。
        LOG.warning("无法读取状态文件 %s，将重新同步全部记录：%s", DDNS_STATE_FILE, exc)
        return {"providers": {}}


def save_state(state: dict[str, Any]) -> None:
    state_dir = os.path.dirname(os.path.abspath(DDNS_STATE_FILE))
    os.makedirs(state_dir, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".ddns-state-", suffix=".tmp", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(state, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, DDNS_STATE_FILE)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def sync_provider(
    name: str,
    operation: Callable[[str], None],
    ip: str,
    state: dict[str, Any],
) -> bool:
    providers = state.setdefault("providers", {})
    provider_state = providers.get(name, {})
    # 旧版字符串状态没有成功时间，强制写入一次以迁移为带时间的新格式。
    stored_ip = provider_state.get("ip") if isinstance(provider_state, dict) else None
    if stored_ip == ip:
        LOG.info("%s 跳过：本地状态显示已成功写入 %s", name, ip)
        return True
    try:
        operation(ip)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        providers[name] = {"ip": ip, "updated_at": now}
        state["updated_at"] = now
        save_state(state)
        return True
    except Exception:
        LOG.exception("%s 同步失败", name)
        return False


def store_ip_history(ip: str, state: dict[str, Any]) -> None:
    """仅当检测到的公网 IP 变化时，向历史文件追加一行。"""
    if state.get("public_ip") == ip:
        return

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    history_dir = os.path.dirname(os.path.abspath(STORE_IP_FILE_PATH))
    os.makedirs(history_dir, exist_ok=True)
    with open(STORE_IP_FILE_PATH, "a", encoding="utf-8") as file:
        file.write(f"{ip}\t{now}\n")
        file.flush()
        os.fsync(file.fileno())

    state["public_ip"] = ip
    state["public_ip_updated_at"] = now
    state["updated_at"] = now
    save_state(state)
    LOG.info("公网 IP 变化历史已写入 %s", STORE_IP_FILE_PATH)


def main() -> int:
    configure_logging()
    try:
        providers = enabled_providers()
    except RuntimeError:
        LOG.exception("更新方式配置无效")
        return 1
    LOG.info("已启用更新方式：%s", ", ".join(providers))
    try:
        ip = get_public_ipv4()
    except Exception:
        LOG.exception("获取公网 IPv4 失败")
        return 1

    LOG.info("当前公网 IPv4：%s", ip)
    state = load_state()
    try:
        store_ip_history(ip, state)
    except Exception:
        LOG.exception("保存公网 IP 历史失败")
        return 1
    results = [
        sync_provider(name, PROVIDER_OPERATIONS[name], ip, state) for name in providers
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
