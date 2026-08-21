# -*- coding: utf-8 -*-
"""
Azure for Students 每日报告

与原项目 report.py 完全独立。
青龙每天运行一次，负责：
1. 查询 VM 状态 / 规格 / Region
2. 查询当月 Network Out Total
3. 查询 Student Credit 周期累计成本
4. 展示 100GB 通用免费额度 + 15GB Student 免费额度
5. 展示流量止损阈值、Credit 预警线、Credit 剩余额度
6. 读取 azure_monitor_state.json，展示是否处于自动保护状态
7. 支持 paused / disabled
8. API 严格超时 + 3 次阶梯重试
9. IPv4 / SNI 兼容
10. 日志轮转、并发锁、Markdown 防炸、失败统计
"""

import json
import logging
import os
import signal
import socket
import sys
import time
import warnings
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

import requests
import urllib3
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient

try:
    import fcntl
except ImportError:
    fcntl = None

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 网络兼容
# ============================================================

_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4_res = [r for r in res if r[0] == socket.AF_INET]
    return ipv4_res if ipv4_res else res


socket.getaddrinfo = _getaddrinfo_ipv4_only

# ============================================================
# 路径 / 常量
# ============================================================

CURR_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CURR_DIR, "config.json")
STATE_FILE = os.path.join(CURR_DIR, "azure_monitor_state.json")
LOG_FILE = os.path.join(CURR_DIR, "azure_report.log")
LOCK_FILE = os.path.join(CURR_DIR, "azure_report.lock")

API_RETRIES = 3
API_CONNECT_TIMEOUT = 5
API_READ_TIMEOUT = 15
METRIC_READ_TIMEOUT = 30
COST_READ_TIMEOUT = 30
REPORT_TIMEOUT = 180

MANAGEMENT_ENDPOINT = "https://management.azure.com"
METRICS_API_VERSION = "2018-01-01"
COST_API_VERSION = "2025-03-01"
TOKEN_SCOPE = "https://management.azure.com/.default"

logger = logging.getLogger("azure_report")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = TimedRotatingFileHandler(
        LOG_FILE,
        when="D",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        logging.Formatter("%(asctime)s - %(message)s")
    )
    logger.addHandler(console)


# ============================================================
# Config / State
# ============================================================


def load_config():
    if not os.path.exists(CONFIG_FILE):
        raise FileNotFoundError(f"配置文件不存在: {CONFIG_FILE}")
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"读取 Azure 状态缓存失败: {e}")
        return {}


# ============================================================
# 推送 / Markdown
# ============================================================


def sanitize_markdown(text):
    text = str(text)
    for ch in ("_", "*", "`", "[", "]"):
        text = text.replace(ch, " ")
    return text.strip()


def send_wxpush(wx_conf, title, content):
    if not wx_conf:
        logger.warning("未配置 Go-WXPush，跳过推送")
        return False

    try:
        url = wx_conf.get(
            "wxpush_api_url",
            "https://push.hzz.cool/wxsend"
        )
        payload = {
            "title": title,
            "content": content,
            "appid": wx_conf.get("appid"),
            "secret": wx_conf.get("secret"),
            "userid": wx_conf.get("userid"),
            "template_id": wx_conf.get("template_id"),
        }
        response = requests.post(
            url,
            json=payload,
            timeout=30,
            verify=False,
        )
        data = response.json()
        if data.get("errcode") == 0:
            logger.info("Go-WXPush 推送成功")
            return True
        logger.error(f"Go-WXPush 返回错误: {data}")
        return False
    except Exception as e:
        logger.error(f"推送过程发生异常: {e}")
        return False


# ============================================================
# Azure API
# ============================================================


def build_credential(user):
    return ClientSecretCredential(
        tenant_id=user["tenant_id"].strip(),
        client_id=user["client_id"].strip(),
        client_secret=user["client_secret"].strip(),
    )


def build_compute_client(user, credential):
    return ComputeManagementClient(
        credential,
        user["subscription_id"].strip(),
    )


def get_token(credential):
    return credential.get_token(TOKEN_SCOPE).token


def azure_request(method, url, *, params=None, json_body=None, token=None,
                  retries=API_RETRIES, timeout=None):
    last_error = None
    timeout = timeout or (API_CONNECT_TIMEOUT, API_READ_TIMEOUT)

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    for attempt in range(1, retries + 1):
        try:
            response = requests.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
            )

            if response.status_code in (429, 500, 502, 503, 504):
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = min(float(retry_after), 30.0)
                    except ValueError:
                        wait = 2 * attempt
                else:
                    wait = 2 * attempt
                raise requests.HTTPError(
                    f"HTTP {response.status_code}; retry after {wait}s",
                    response=response,
                )

            response.raise_for_status()
            return response.json() if response.content else {}

        except Exception as e:
            last_error = e
            logger.warning(
                f"Azure API {method} {url} 失败 "
                f"(尝试 {attempt}/{retries}): {e}"
            )
            if attempt < retries:
                time.sleep(2 * attempt)

    raise last_error


def get_vm_status(compute_client, user):
    view = compute_client.virtual_machines.instance_view(
        user["resource_group"].strip(),
        user["vm_name"].strip(),
    )
    for status in view.statuses:
        code = getattr(status, "code", "") or ""
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1].lower()
    return "unknown"


def get_vm_info(compute_client, user):
    vm = compute_client.virtual_machines.get(
        user["resource_group"].strip(),
        user["vm_name"].strip(),
    )
    return {
        "size": getattr(vm.hardware_profile, "vm_size", "N/A"),
        "location": getattr(vm, "location", "N/A"),
    }


def get_vm_resource_id(user):
    return (
        f"/subscriptions/{user['subscription_id'].strip()}"
        f"/resourceGroups/{user['resource_group'].strip()}"
        f"/providers/Microsoft.Compute/virtualMachines/{user['vm_name'].strip()}"
    )


def get_month_start_utc():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def get_monthly_network_out(user, credential):
    token = get_token(credential)
    resource_id = get_vm_resource_id(user)
    start = get_month_start_utc()
    end = datetime.now(timezone.utc)

    url = (
        f"{MANAGEMENT_ENDPOINT}{resource_id}"
        f"/providers/microsoft.insights/metrics"
    )
    params = {
        "api-version": METRICS_API_VERSION,
        "metricnames": "Network Out Total",
        "timespan": (
            f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/"
            f"{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        ),
        "interval": "PT1H",
        "aggregation": "Total",
    }

    data = azure_request(
        "GET",
        url,
        params=params,
        token=token,
        retries=API_RETRIES,
        timeout=(API_CONNECT_TIMEOUT, METRIC_READ_TIMEOUT),
    )

    total_bytes = 0.0
    for metric in data.get("value", []):
        for timeseries in metric.get("timeseries", []):
            for point in timeseries.get("data", []):
                value = point.get("total")
                if value is not None:
                    total_bytes += float(value)
    return total_bytes


def bytes_to_gb(value):
    return value / (1024 ** 3)


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def get_credit_start_date(user):
    value = user.get("credit_start_date", "").strip()
    if not value:
        raise ValueError(f"[{user['name']}] 缺少 credit_start_date")
    return parse_date(value)


def get_credit_usage(user, credential):
    token = get_token(credential)
    subscription_id = user["subscription_id"].strip()
    url = (
        f"{MANAGEMENT_ENDPOINT}/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CostManagement/query"
    )

    start = get_credit_start_date(user)
    end = datetime.now(timezone.utc)

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": start.strftime("%Y-%m-%dT00:00:00Z"),
            "to": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "dataset": {
            "granularity": "None",
            "aggregation": {
                "totalCost": {
                    "name": "PreTaxCost",
                    "function": "Sum",
                }
            },
        },
    }

    data = azure_request(
        "POST",
        url,
        params={"api-version": COST_API_VERSION},
        json_body=body,
        token=token,
        retries=API_RETRIES,
        timeout=(API_CONNECT_TIMEOUT, COST_READ_TIMEOUT),
    )

    properties = data.get("properties", {})
    columns = properties.get("columns", [])
    rows = properties.get("rows", [])
    if not rows:
        return 0.0

    names = [c.get("name", "") for c in columns]
    index = names.index("PreTaxCost") if "PreTaxCost" in names else None
    if index is None and "totalCost" in names:
        index = names.index("totalCost")
    if index is None:
        raise RuntimeError(f"Cost API 返回中找不到成本列: {names}")

    total = 0.0
    for row in rows:
        if len(row) > index and row[index] is not None:
            total += float(row[index])

    currency_index = names.index("Currency") if "Currency" in names else None
    if currency_index is not None:
        currencies = {
            str(row[currency_index]).upper()
            for row in rows
            if len(row) > currency_index and row[currency_index] is not None
        }
        if len(currencies) > 1:
            raise RuntimeError(f"Cost API 返回多个货币单位: {currencies}")
        if currencies and next(iter(currencies)) != "USD":
            raise RuntimeError(
                f"Cost API 返回货币为 {next(iter(currencies))}，当前脚本按 USD 管理。"
            )

    return total


# ============================================================
# 超时 / 锁
# ============================================================


class AzureReportTimeout(Exception):
    pass


def timeout_handler(signum, frame):
    raise AzureReportTimeout("Azure 报告执行超时")


def acquire_lock():
    if fcntl is None:
        return True
    try:
        lock_file = open(LOCK_FILE, "w")
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except (IOError, OSError):
        return None


# ============================================================
# 主流程
# ============================================================


def main():
    lock = acquire_lock()
    if lock is None:
        logger.warning("⚠️ 上一轮 Azure 日报尚未结束，本轮任务跳过执行。")
        return

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(REPORT_TIMEOUT)

    try:
        config = load_config()
        wx_conf = config.get("wxpush", {})
        azure_users = config.get("azure", [])
        state = load_state()

        if not azure_users:
            logger.info("config.json 中没有 Azure 配置，任务结束。")
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        success_count = 0
        fail_count = 0
        report_lines = []

        header = (
            "📊 [Azure Student - 每日财报]\n"
            f"📅 日期: {today}\n"
            f"✅ 成功机器：{{success}}，❌ 失败机器：{{fail}}\n"
            "--------------------------------\n"
        )

        for user in azure_users:
            name = user.get("name", user.get("vm_name", "Azure"))

            try:
                if user.get("paused") or user.get("disabled"):
                    logger.info(f"[{name}] 监控已暂停，跳过接口请求")
                    report_lines.append(
                        f"👤 *{sanitize_markdown(name)}* (已暂停)\n"
                        "   ⏸️ 监控: 配置文件设定为暂停\n"
                    )
                    success_count += 1
                    continue

                credential = build_credential(user)
                compute_client = build_compute_client(user, credential)

                status = get_vm_status(compute_client, user)
                info = get_vm_info(compute_client, user)

                traffic_bytes = get_monthly_network_out(
                    user,
                    credential
                )
                traffic_gb = bytes_to_gb(traffic_bytes)

                generic_free = float(
                    user.get("generic_free_gb", 100)
                )
                student_free = float(
                    user.get("student_free_gb", 15)
                )
                traffic_limit = float(
                    user.get("traffic_limit", 110)
                )

                credit_limit = float(
                    user.get("credit_limit", 100)
                )

                credit_used = None
                credit_error = None
                try:
                    credit_used = get_credit_usage(
                        user,
                        credential
                    )
                except Exception as e:
                    credit_error = e
                    logger.error(
                        f"[{name}] Cost 查询失败: {e}"
                    )

                key = (
                    f"{user.get('subscription_id', '').strip()}::"
                    f"{user.get('resource_group', '').strip()}::"
                    f"{user.get('vm_name', '').strip()}"
                )
                guard = state.get(key, {})
                guarded = guard.get("deallocated_by_guard", False)
                guard_reason = guard.get("guard_reason")

                # 状态图标
                if status == "running":
                    run_icon = "🟢"
                elif status == "deallocated":
                    run_icon = "⚫"
                elif status == "stopped":
                    run_icon = "🔴"
                else:
                    run_icon = "❓"

                # 流量评价
                if traffic_gb >= traffic_limit:
                    traffic_status = "🚨 已达到自动止损阈值"
                elif traffic_gb >= generic_free:
                    traffic_status = "⚠️ 已超过 100GB 通用免费额度"
                else:
                    traffic_status = "✅ 低于 100GB 通用免费额度"

                # Credit 评价
                if credit_error is not None:
                    credit_status = "❓ 查询失败"
                    credit_text = "查询失败"
                    remaining_text = "查询失败"
                else:
                    remaining = max(credit_limit - credit_used, 0)
                    if credit_used >= credit_limit:
                        credit_status = "🚨 已达到保护上限"
                    elif credit_used >= float(user.get("credit_emergency", 95)):
                        credit_status = "🔴 接近 $100 上限"
                    elif credit_used >= float(user.get("credit_warning", 80)):
                        credit_status = "⚠️ Credit 使用较高"
                    else:
                        credit_status = "✅ Credit 正常"
                    credit_text = f"${credit_used:.2f}"
                    remaining_text = f"${remaining:.2f}"

                if guarded:
                    if guard_reason == "traffic":
                        guard_status = "🚨 流量熔断状态"
                    elif guard_reason == "credit":
                        guard_status = "🚨 Credit 熔断状态"
                    else:
                        guard_status = "🚨 自动保护状态"
                else:
                    guard_status = "✅ 未处于自动保护状态"

                report_lines.append(
                    f"👤 *{sanitize_markdown(name)}* ({sanitize_markdown(info['size'])})\n"
                    f"   🖥️ 状态: {run_icon} {sanitize_markdown(status)}\n"
                    f"   📍 Region: {sanitize_markdown(info['location'])}\n"
                    f"   🌐 本月出站: {traffic_gb:.2f} GB\n"
                    f"      ├─ 通用免费额度: {generic_free:.0f} GB/月\n"
                    f"      ├─ Student 免费额度: {student_free:.0f} GB/月\n"
                    f"      ├─ 自动止损阈值: {traffic_limit:.0f} GB\n"
                    f"      └─ 评价: {traffic_status}\n"
                    f"   💰 Student Credit: {credit_text}\n"
                    f"      ├─ 总额度: ${credit_limit:.2f}\n"
                    f"      ├─ 剩余估算: {remaining_text}\n"
                    f"      └─ 评价: {credit_status}\n"
                    f"   🛡️ 自动保护: {guard_status}\n"
                )

                success_count += 1
                logger.info(f"[{name}] Azure 日报数据获取成功")

            except Exception as e:
                fail_count += 1
                report_lines.append(
                    f"❌ *{sanitize_markdown(name)}* Error: "
                    f"{sanitize_markdown(e)}\n"
                )
                logger.exception(
                    f"处理 Azure 用户 {name} 时出错"
                )

        final_summary = (
            header.format(
                success=success_count,
                fail=fail_count
            )
            + "\n".join(report_lines)
            + "--------------------------------\n"
            + "⚠️ 说明：Cost Management 数据存在刷新延迟，"
              "Student Credit 剩余额度请以 Azure Sponsorships 为最终依据。"
        )

        title = (
            f"Azure 日报: 成功{success_count}, 失败{fail_count}"
        )

        send_wxpush(
            wx_conf,
            title,
            final_summary
        )

        logger.info(
            "=== 本次 Azure 日报结束 ===\n" + final_summary
        )
        print(final_summary)

    except AzureReportTimeout:
        logger.error(
            f"Azure 日报执行超过 {REPORT_TIMEOUT}s，已强制终止。"
        )
        raise
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        if hasattr(lock, "close"):
            lock.close()


if __name__ == "__main__":
    main()
