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
from functools import partial
from typing import Any, Callable, Optional

import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from aliyunsdkcore import client
from aliyunsdkalidns.request.v20150109 import UpdateDomainRecordRequest


# ======================================================
# 全局配置（从脚本所在目录的 config.yml 读取）
CUR_DIR = os.path.dirname(os.path.realpath(__file__))
CONFIG_FILE = os.path.join(CUR_DIR, "config.yml")


def load_config() -> dict[str, Any]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
    except FileNotFoundError as exc:
        raise RuntimeError(f"配置文件不存在：{CONFIG_FILE}") from exc
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeError(f"无法读取配置文件 {CONFIG_FILE}：{exc}") from exc
    if not isinstance(config, dict):
        raise RuntimeError("config.yml 顶层必须是对象")
    return config


CONFIG_ERROR: Optional[RuntimeError] = None
try:
    CONFIG = load_config()
    GENERAL_CONFIG = CONFIG.get("general", {})
    PROVIDERS_CONFIG = CONFIG.get("providers", {})
    if not isinstance(GENERAL_CONFIG, dict) or not isinstance(PROVIDERS_CONFIG, dict):
        raise RuntimeError("config.yml 的 general 和 providers 必须是对象")
except RuntimeError as exc:
    CONFIG_ERROR = exc
    CONFIG = {}
    GENERAL_CONFIG = {}
    PROVIDERS_CONFIG = {}

configured_home = str(GENERAL_CONFIG.get("home", "")).strip()
configured_home = os.path.expanduser(configured_home or CUR_DIR)
if not os.path.isabs(configured_home):
    configured_home = os.path.join(CUR_DIR, configured_home)
DDNS_HOME = os.path.abspath(configured_home)


def resolve_config_path(name: str, default_name: str) -> str:
    """Resolve relative runtime paths against DDNS_HOME."""
    value = str(GENERAL_CONFIG.get(name, default_name)).strip() or default_name
    value = os.path.expanduser(value)
    if not os.path.isabs(value):
        value = os.path.join(DDNS_HOME, value)
    return os.path.abspath(value)


GET_IP_URL = str(
    GENERAL_CONFIG.get("get_ip_url", "https://ipv4.ddnspod.com")
).strip()
STORE_IP_FILE_PATH = resolve_config_path("ip_history_file", "ip_history.txt")
DDNS_STATE_FILE = resolve_config_path("state_file", "ddns_state.json")
DDNS_LOG_FILE = resolve_config_path("log_file", "update_dns.log")

CF_API_BASE = "https://api.cloudflare.com/client/v4"
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


def aliyun_client(config: dict[str, str]) -> client.AcsClient:
    return client.AcsClient(
        required_config("access_key_id", config.get("access_key_id", "")),
        required_config("access_key_secret", config.get("access_key_secret", "")),
        config.get("region_id", "cn-shenzhen"),
    )


def update_aliyun(ip: str, config: dict[str, str]) -> None:
    record_id = required_config("record_id", config.get("record_id", ""))
    record_rr = required_config("record_rr", config.get("record_rr", ""))
    acs = aliyun_client(config)

    update = UpdateDomainRecordRequest.UpdateDomainRecordRequest()
    update.set_accept_format("json")
    update.set_RecordId(record_id)
    update.set_RR(record_rr)
    update.set_Type("A")
    update.set_Value(ip)
    acs.do_action_with_exception(update)
    LOG.info("Aliyun 写入成功：%s -> %s", record_rr, ip)


def cf_request(method: str, path: str, token: str, **kwargs: Any) -> dict[str, Any]:
    token = required_config("api_token", token)
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


def update_cloudflare(ip: str, config: dict[str, str]) -> None:
    zone_id = required_config("zone_id", config.get("zone_id", ""))
    record_id = required_config("record_id", config.get("record_id", ""))
    dns_name = required_config("dns_name", config.get("dns_name", ""))
    cf_request(
        "PATCH",
        f"/zones/{zone_id}/dns_records/{record_id}",
        token=config.get("api_token", ""),
        json={"content": ip, "proxied": False},
    )
    LOG.info("Cloudflare 写入成功：%s -> %s（DNS only）", dns_name, ip)


def update_3322(ip: str, config: dict[str, str]) -> None:
    """通过 members.3322.net 兼容接口更新动态域名。"""
    url = config.get("url", "https://members.3322.net/dyndns/update")
    username = required_config("username", config.get("username", ""))
    password = required_config("password", config.get("password", ""))
    hostname = required_config("hostname", config.get("hostname", ""))
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


PROVIDER_UPDATERS: dict[str, Callable[..., None]] = {
    "aliyun": update_aliyun,
    "cloudflare": update_cloudflare,
    "3322": update_3322,
}


def parse_accounts(
    provider: str,
    value: Any,
    required_fields: tuple[str, ...],
) -> list[dict[str, str]]:
    """验证账号数组；账号名用于生成独立且稳定的状态键。"""
    if not isinstance(value, list) or not value:
        raise RuntimeError(f"providers.{provider}.accounts 必须是非空数组")

    result: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for index, raw_account in enumerate(value, start=1):
        if not isinstance(raw_account, dict):
            raise RuntimeError(f"providers.{provider}.accounts 第 {index} 项必须是对象")
        account = {
            str(key): str(value).strip()
            for key, value in raw_account.items()
            if value is not None
        }
        name = account.get("name", "")
        if not name or ":" in name or any(character.isspace() for character in name):
            raise RuntimeError(
                f"providers.{provider}.accounts 第 {index} 项的 name 必须非空，"
                "且不能包含空白或冒号"
            )
        if name in seen_names:
            raise RuntimeError(f"providers.{provider}.accounts 包含重复账号名：{name}")
        missing = [field for field in required_fields if not account.get(field)]
        if missing:
            raise RuntimeError(
                f"providers.{provider}.accounts 账号 {name} 缺少字段："
                f"{', '.join(missing)}"
            )
        seen_names.add(name)
        result.append(account)
    return result


def build_provider_operations() -> dict[str, Callable[[str], None]]:
    """从 YAML 创建已启用的更新实例，使用 provider:name 状态键。"""
    unknown = [name for name in PROVIDERS_CONFIG if name not in PROVIDER_UPDATERS]
    if unknown:
        raise RuntimeError(f"providers 包含未知方式：{', '.join(unknown)}")

    required_fields = {
        "aliyun": ("name", "access_key_id", "access_key_secret", "record_id", "record_rr"),
        "cloudflare": ("name", "api_token", "zone_id", "record_id", "dns_name"),
        "3322": ("name", "username", "password", "hostname"),
    }
    operations: dict[str, Callable[[str], None]] = {}
    for provider, raw_config in PROVIDERS_CONFIG.items():
        if not isinstance(raw_config, dict):
            raise RuntimeError(f"providers.{provider} 必须是对象")
        enabled = raw_config.get("enabled", False)
        if not isinstance(enabled, bool):
            raise RuntimeError(f"providers.{provider}.enabled 必须是 true 或 false")
        if not enabled:
            continue
        accounts = parse_accounts(
            provider, raw_config.get("accounts"), required_fields[provider]
        )
        operations.update(
            {
                f"{provider}:{account['name']}": partial(
                    PROVIDER_UPDATERS[provider], config=account
                )
                for account in accounts
            }
        )
    if not operations:
        raise RuntimeError("config.yml 至少需要启用一个账号")
    return operations


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
        # 不信任损坏的状态：本次重新写入全部实例，避免错误跳过 DNS 更新。
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
    if CONFIG_ERROR is not None:
        LOG.error("配置加载失败：%s", CONFIG_ERROR)
        return 1
    try:
        operations = build_provider_operations()
    except RuntimeError:
        LOG.exception("更新方式配置无效")
        return 1
    LOG.info("已启用更新实例：%s", ", ".join(operations))
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
        sync_provider(name, operation, ip, state)
        for name, operation in operations.items()
    ]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
