# -*- coding: utf-8 -*-
"""
Azure for Students VM 流量 / Credit 自动监控

与原项目 monitor.py 完全独立，不读取/修改阿里云 users 配置。

核心功能：
1. 每分钟检查 Azure VM 状态与当月 Network Out Total。
2. 流量达到 traffic_limit 后执行 Deallocate 止损。
3. 上个月因“流量熔断”而 Deallocate 的 VM，在新月份首次巡检时自动恢复。
4. VM 启动后原地轮询，确认真实进入 running 才发送恢复成功通知。
5. Azure API 请求带连接/读取超时和阶梯重试。
6. 连续启动失败达到阈值后进入冷却期，避免资源不足时高频重试。
7. 普通异常 / 流量超限通知带冷却，避免 Go-WXPush 推送轰炸。
8. 单个 Azure 账号巡检带 SIGALRM 硬超时，避免青龙任务长期卡死。
9. 使用文件锁防止 cron 并发堆积。
10. Linux 青龙 Docker 环境的 SNI/IPv4 兼容处理。
11. 日志按天轮转，保留 7 天。
"""

import json
import logging
import os
import signal
import socket
import sys
import time
import warnings
from datetime import datetime, timezone, timedelta
from logging.handlers import TimedRotatingFileHandler

import requests
import urllib3
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient

try:
    import fcntl  # Linux 文件锁
except ImportError:
    fcntl = None

warnings.filterwarnings("ignore")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 底层网络兼容：SNI + IPv4
# ============================================================

_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4_res = [r for r in res if r[0] == socket.AF_INET]
    return ipv4_res if ipv4_res else res


socket.getaddrinfo = _getaddrinfo_ipv4_only

# ============================================================
# 全局路径
# ============================================================

CURR_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CURR_DIR, "config.json")
LOG_FILE = os.path.join(CURR_DIR, "azure_monitor.log")
STATE_FILE = os.path.join(CURR_DIR, "azure_monitor_state.json")
LOCK_FILE = os.path.join(CURR_DIR, "azure_monitor.lock")

# ============================================================
# 配置常量
# ============================================================

NOTIFY_COOLDOWN = 3600           # 普通异常 1 小时
OVERLIMIT_COOLDOWN = 86400       # 流量超限 24 小时
START_WAIT_TIMEOUT = 120         # Azure start 后最多等待 120 秒
START_POLL_INTERVAL = 10         # 每 10 秒查询一次状态
USER_CHECK_TIMEOUT = 150         # 单个 Azure 账号最多检查 150 秒
MAX_START_FAILURES = 3           # 连续启动失败达到 3 次进入冷却
RESOURCE_RETRY_COOLDOWN = 1800   # 冷却 30 分钟
API_RETRIES = 3                  # 一般 API 最多 3 次
API_CONNECT_TIMEOUT = 5
API_READ_TIMEOUT = 15
METRIC_READ_TIMEOUT = 30
COST_READ_TIMEOUT = 30

MANAGEMENT_ENDPOINT = "https://management.azure.com"
METRICS_API_VERSION = "2018-01-01"
COST_API_VERSION = "2025-03-01"
TOKEN_SCOPE = "https://management.azure.com/.default"

logger = logging.getLogger("azure_monitor")
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
# 配置 / 状态
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
        logger.warning(f"读取 Azure 状态文件失败，将使用空状态: {e}")
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.error(f"保存 Azure 状态失败: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def resource_key(user):
    return (
        f"{user.get('subscription_id', '').strip()}::"
        f"{user.get('resource_group', '').strip()}::"
        f"{user.get('vm_name', '').strip()}"
    )


def can_notify(state, key, event_key, cooldown=NOTIFY_COOLDOWN):
    last_ts = state.get(key, {}).get("notifications", {}).get(event_key, 0)
    return (time.time() - last_ts) >= cooldown


def mark_notified(state, key, event_key):
    state.setdefault(key, {}).setdefault("notifications", {})[event_key] = time.time()


# ============================================================
# Markdown / 推送
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
            timeout=10,
            verify=False,
        )
        data = response.json()
        if data.get("errcode") == 0:
            logger.info("Go-WXPush 推送成功")
            return True
        logger.error(f"Go-WXPush 推送返回错误: {data}")
        return False
    except Exception as e:
        logger.error(f"Go-WXPush 推送失败: {e}")
        return False


# ============================================================
# Azure Credential / API
# ============================================================


def build_credential(user):
    return ClientSecretCredential(
        tenant_id=user["tenant_id"].strip(),
        client_id=user["client_id"].strip(),
        client_secret=user["client_secret"].strip(),
    )


def get_token(credential):
    return credential.get_token(TOKEN_SCOPE).token


def build_compute_client(user, credential):
    return ComputeManagementClient(
        credential,
        user["subscription_id"].strip(),
    )


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
                        sleep_seconds = min(float(retry_after), 30.0)
                    except ValueError:
                        sleep_seconds = 2 * attempt
                else:
                    sleep_seconds = 2 * attempt
                raise requests.HTTPError(
                    f"HTTP {response.status_code}; retry after {sleep_seconds}s",
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

    logger.error(
        f"Azure API {method} {url} 最终失败，已重试 {retries} 次"
    )
    raise last_error


# ============================================================
# VM
# ============================================================


def get_vm_status(compute_client, user):
    resource_group = user["resource_group"].strip()
    vm_name = user["vm_name"].strip()

    view = compute_client.virtual_machines.instance_view(
        resource_group,
        vm_name,
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
        "id": getattr(vm, "id", ""),
    }


def start_vm(compute_client, user):
    resource_group = user["resource_group"].strip()
    vm_name = user["vm_name"].strip()
    logger.info(f"[{user['name']}] 执行 Azure VM Start...")
    operation = compute_client.virtual_machines.begin_start(
        resource_group,
        vm_name,
    )
    operation.result()


def deallocate_vm(compute_client, user):
    resource_group = user["resource_group"].strip()
    vm_name = user["vm_name"].strip()
    logger.warning(f"[{user['name']}] 执行 Azure VM Deallocate...")
    operation = compute_client.virtual_machines.begin_deallocate(
        resource_group,
        vm_name,
    )
    operation.result()


def wait_for_running(compute_client, user):
    waited = 0
    while waited < START_WAIT_TIMEOUT:
        time.sleep(START_POLL_INTERVAL)
        waited += START_POLL_INTERVAL
        status = get_vm_status(compute_client, user)
        logger.info(
            f"[{user['name']}] 等待 Azure VM 开机... 当前={status} ({waited}s)"
        )
        if status == "running":
            return True
        if status in ("stopped", "deallocated"):
            return False
    return False


# ============================================================
# Metrics
# ============================================================


def get_month_start_utc():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def month_key(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def get_vm_resource_id(user):
    subscription_id = user["subscription_id"].strip()
    resource_group = user["resource_group"].strip()
    vm_name = user["vm_name"].strip()
    return (
        f"/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
    )


def get_monthly_network_out(user, credential):
    token = get_token(credential)
    resource_id = get_vm_resource_id(user)
    start = get_month_start_utc()
    end = datetime.now(timezone.utc)

    timespan = (
        f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/"
        f"{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )

    url = (
        f"{MANAGEMENT_ENDPOINT}"
        f"{resource_id}/providers/microsoft.insights/metrics"
    )

    params = {
        "api-version": METRICS_API_VERSION,
        "metricnames": "Network Out Total",
        "timespan": timespan,
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


# ============================================================
# Cost Management
# ============================================================


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
    scope = f"/subscriptions/{subscription_id}"
    url = (
        f"{MANAGEMENT_ENDPOINT}{scope}"
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

    params = {"api-version": COST_API_VERSION}

    data = azure_request(
        "POST",
        url,
        params=params,
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
    index = None
    for candidate in ("PreTaxCost", "totalCost"):
        if candidate in names:
            index = names.index(candidate)
            break
    if index is None:
        raise RuntimeError(f"Cost API 返回中找不到成本列: {names}")

    total = 0.0
    for row in rows:
        if len(row) > index and row[index] is not None:
            total += float(row[index])

    # 本配置默认按 USD 管理；如果 Cost API 返回的货币不是 USD，
    # 这里不做汇率转换，避免错误地把非 USD 原值当作 USD。
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
                f"Cost API 返回货币为 {next(iter(currencies))}，"
                f"当前 Student Credit 配置要求 USD。"
            )

    return total


# ============================================================
# 自动恢复
# ============================================================


def auto_restore_if_needed(user, compute_client, state, wx_conf):
    """
    只恢复“上个月由本程序因流量触发的熔断”。
    Credit 熔断不会因为自然月切换而自动恢复。
    """
    key = resource_key(user)
    item = state.get(key, {})

    if not item.get("deallocated_by_guard", False):
        return
    if item.get("guard_reason") != "traffic":
        return

    guarded_month = item.get("guarded_month")
    current_month = month_key()
    if not guarded_month or guarded_month == current_month:
        return

    # 防止同一个新月份重复执行“月初恢复”
    if item.get("last_restore_month") == current_month:
        return

    status = get_vm_status(compute_client, user)

    # 如果用户已经手动启动，不再重复 start。
    if status == "running":
        item["deallocated_by_guard"] = False
        item["last_restore_month"] = current_month
        state[key] = item
        return

    if status not in ("deallocated", "stopped"):
        logger.warning(
            f"[{user['name']}] 月初恢复时状态为 {status}，跳过自动启动。"
        )
        return

    failures = item.get("start_failures", 0)
    if failures >= MAX_START_FAILURES:
        last_retry = item.get("last_retry_ts", 0)
        if time.time() - last_retry < RESOURCE_RETRY_COOLDOWN:
            logger.info(f"[{user['name']}] 月初自动恢复仍处于资源重试冷却期，跳过。")
            return

    logger.warning(
        f"[{user['name']}] 检测到上月流量熔断，"
        f"进入新月份 {current_month}，准备自动恢复。"
    )

    item["last_retry_ts"] = time.time()
    state[key] = item

    try:
        start_vm(compute_client, user)
        started = wait_for_running(compute_client, user)

        if started:
            item["deallocated_by_guard"] = False
            item["guard_reason"] = None
            item["guarded_month"] = None
            item["last_restore_month"] = current_month
            item["start_failures"] = 0
            state[key] = item
            save_state(state)

            if can_notify(state, key, "auto_restore"):
                content = (
                    f"✅ [{sanitize_markdown(user['name'])}] Azure 月初自动恢复成功。\n"
                    f"原因：上月流量熔断\n"
                    f"当前月份：{current_month}\n"
                    f"VM 已确认进入 Running。"
                )
                if send_wxpush(wx_conf, "Azure 月初自动恢复", content):
                    mark_notified(state, key, "auto_restore")
                    save_state(state)
        else:
            item["start_failures"] = failures + 1
            state[key] = item
            save_state(state)
            logger.warning(f"[{user['name']}] 月初自动恢复：Start 请求完成，但未确认 Running。")

    except Exception as e:
        item["start_failures"] = failures + 1
        state[key] = item
        save_state(state)
        logger.error(f"[{user['name']}] 月初自动恢复失败: {e}")
        if can_notify(state, key, "restore_error"):
            content = (
                f"⚠️ [{sanitize_markdown(user['name'])}] Azure 月初自动恢复失败。\n"
                f"错误：{sanitize_markdown(e)}"
            )
            if send_wxpush(wx_conf, "Azure 月初恢复失败", content):
                mark_notified(state, key, "restore_error")
                save_state(state)


# ============================================================
# 账号巡检
# ============================================================


def check_and_act(user, wx_conf, state):
    name = user.get("name", user.get("vm_name", "Azure"))
    key = resource_key(user)

    if user.get("paused") or user.get("disabled"):
        logger.info(f"[{name}] Azure 监控已暂停，跳过接口请求")
        return

    required = [
        "tenant_id",
        "client_id",
        "client_secret",
        "subscription_id",
        "resource_group",
        "vm_name",
    ]
    for field in required:
        if not user.get(field):
            raise ValueError(f"[{name}] 缺少 Azure 配置项: {field}")

    credential = build_credential(user)
    compute_client = build_compute_client(user, credential)

    # 1. 新月自动恢复
    auto_restore_if_needed(user, compute_client, state, wx_conf)

    # 2. 查询状态
    status = get_vm_status(compute_client, user)
    if status == "unknown":
        logger.warning(f"❓[{name}] Azure VM 状态未知")
        return

    # 3. 查询当月流量
    traffic_bytes = get_monthly_network_out(user, credential)
    traffic_gb = bytes_to_gb(traffic_bytes)
    traffic_limit = float(user.get("traffic_limit", 110))

    logger.info(
        f"[{name}] Network Out {traffic_gb:.2f} GB / {traffic_limit:.2f} GB，"
        f"VM={status}"
    )

    # 4. Cost 不需要像流量一样每分钟严格监控，默认每小时检查一次。
    #    仍然保留在 monitor 中作为最后一道自动保护；日报会每天强制查询。
    item = state.setdefault(key, {})
    last_cost_check = item.get("last_cost_check_ts", 0)
    cost_check_interval = int(user.get("cost_check_interval", 3600))
    if time.time() - last_cost_check >= cost_check_interval:
        item["last_cost_check_ts"] = time.time()
        try:
            credit_used = get_credit_usage(user, credential)
            credit_limit = float(user.get("credit_limit", 100))
            credit_warning = float(user.get("credit_warning", 80))
            credit_emergency = float(user.get("credit_emergency", 95))

            item["last_credit_used"] = credit_used
            save_state(state)

            logger.info(
                f"[{name}] Student Credit 累计使用 ${credit_used:.2f} / "
                f"${credit_limit:.2f}"
            )

            if credit_used >= credit_limit and status == "running":
                logger.warning(
                    f"[{name}] Credit 达到保护上限，执行 Deallocate。"
                )
                deallocate_vm(compute_client, user)
                item.update({
                    "deallocated_by_guard": True,
                    "guard_reason": "credit",
                    "guarded_month": month_key(),
                    "guard_time": datetime.now(timezone.utc).isoformat(),
                })
                save_state(state)

                if can_notify(state, key, "credit_stop", OVERLIMIT_COOLDOWN):
                    content = (
                        f"🚨 [{sanitize_markdown(name)}] Azure Student Credit 已达到保护上限。\n"
                        f"累计 Cost：${credit_used:.2f}\n"
                        f"保护上限：${credit_limit:.2f}\n"
                        f"已执行 VM Deallocate。\n"
                        f"⚠️ Cost Management 数据可能存在延迟，请同步检查 Azure Sponsorships。"
                    )
                    if send_wxpush(wx_conf, "Azure Credit 止损", content):
                        mark_notified(state, key, "credit_stop")
                        save_state(state)
                return

            if credit_used >= credit_emergency and can_notify(state, key, "credit_emergency"):
                if send_wxpush(
                    wx_conf,
                    "Azure Credit 高位预警",
                    f"⚠️ [{sanitize_markdown(name)}] Student Credit 已使用 ${credit_used:.2f}，接近 ${credit_limit:.2f} 上限。"
                ):
                    mark_notified(state, key, "credit_emergency")
                    save_state(state)
            elif credit_used >= credit_warning and can_notify(state, key, "credit_warning"):
                if send_wxpush(
                    wx_conf,
                    "Azure Credit 使用预警",
                    f"⚠️ [{sanitize_markdown(name)}] Student Credit 已使用 ${credit_used:.2f}，超过 ${credit_warning:.2f} 预警线。"
                ):
                    mark_notified(state, key, "credit_warning")
                    save_state(state)

        except Exception as e:
            logger.warning(f"[{name}] Azure Cost 查询失败：{e}")
            if can_notify(state, key, "cost_query_error"):
                content = (
                    f"⚠️ [{sanitize_markdown(name)}] Azure Cost Management 查询失败。\n"
                    f"错误：{sanitize_markdown(e)}"
                )
                if send_wxpush(wx_conf, "Azure Cost 查询异常", content):
                    mark_notified(state, key, "cost_query_error")
                    save_state(state)

    # 5. 流量安全：只有“本程序流量熔断后”才允许自动恢复。
    #    正常情况下，不会因为 VM 是 deallocated/stopped 就擅自启动。
    if traffic_gb < traffic_limit:
        item = state.setdefault(key, {})
        item.setdefault("start_failures", 0)
        item.setdefault("notifications", {})
        save_state(state)
        return

    # 5. 流量超标
    if status == "running":
        logger.warning(
            f"[{name}] 流量超限，当前 {traffic_gb:.2f} GB >= {traffic_limit:.2f} GB，"
            f"执行 Deallocate。"
        )
        try:
            deallocate_vm(compute_client, user)
            state[key] = {
                **state.get(key, {}),
                "deallocated_by_guard": True,
                "guard_reason": "traffic",
                "guarded_month": month_key(),
                "guard_time": datetime.now(timezone.utc).isoformat(),
                "start_failures": 0,
            }
            save_state(state)

            if can_notify(state, key, "overlimit_stop", OVERLIMIT_COOLDOWN):
                content = (
                    f"🚨 [{sanitize_markdown(name)}] Azure 流量超限止损。\n"
                    f"本月出站：{traffic_gb:.2f} GB\n"
                    f"安全阈值：{traffic_limit:.2f} GB\n"
                    f"已执行 VM Deallocate。"
                )
                if send_wxpush(wx_conf, "Azure 流量超限止损", content):
                    mark_notified(state, key, "overlimit_stop")
                    save_state(state)
        except Exception as e:
            logger.error(f"[{name}] Azure Deallocate 失败: {e}")
            if can_notify(state, key, "deallocate_error"):
                content = (
                    f"❌ [{sanitize_markdown(name)}] Azure 流量已超限，但 Deallocate 失败。\n"
                    f"当前：{traffic_gb:.2f} GB\n"
                    f"阈值：{traffic_limit:.2f} GB\n"
                    f"错误：{sanitize_markdown(e)}"
                )
                if send_wxpush(wx_conf, "Azure 流量止损失败", content):
                    mark_notified(state, key, "deallocate_error")
                    save_state(state)

    else:
        logger.warning(
            f"🔴[{name}] Azure 流量已超限，VM 当前={status}"
        )
        if can_notify(state, key, "overlimit_remind", OVERLIMIT_COOLDOWN):
            content = (
                f"⚠️ [{sanitize_markdown(name)}] Azure 本月流量已达到止损阈值。\n"
                f"当前：{traffic_gb:.2f} GB\n"
                f"阈值：{traffic_limit:.2f} GB\n"
                f"VM 当前状态：{status}\n"
                f"目前保持保护状态。"
            )
            if send_wxpush(wx_conf, "Azure 超限保护提醒", content):
                mark_notified(state, key, "overlimit_remind")
                save_state(state)


# ============================================================
# 超时守护
# ============================================================


class AzureMonitorTimeout(Exception):
    pass


def timeout_handler(signum, frame):
    raise AzureMonitorTimeout("Azure 单实例巡检超时")


def check_with_timeout(user, wx_conf, state):
    if not hasattr(signal, "SIGALRM"):
        check_and_act(user, wx_conf, state)
        return

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(USER_CHECK_TIMEOUT)
    try:
        check_and_act(user, wx_conf, state)
    except AzureMonitorTimeout:
        name = user.get("name", user.get("vm_name", "Azure"))
        logger.error(
            f"[{name}] Azure 巡检超时({USER_CHECK_TIMEOUT}s)，已强行跳过。"
        )
    finally:
        signal.alarm(0)


# ============================================================
# 并发锁
# ============================================================


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
# main
# ============================================================


def main():
    lock = acquire_lock()
    if lock is None:
        logger.warning("⚠️ 上一轮 Azure 监控尚未结束，本轮任务跳过执行。")
        return

    try:
        config = load_config()
        wx_conf = config.get("wxpush", {})
        azure_users = config.get("azure", [])

        if not azure_users:
            logger.info("config.json 中没有 Azure 配置，任务结束。")
            return

        state = load_state()

        for user in azure_users:
            check_with_timeout(user, wx_conf, state)

        save_state(state)

    except Exception as e:
        logger.exception(f"Azure monitor main failed: {e}")
        raise
    finally:
        if hasattr(lock, "close"):
            lock.close()


if __name__ == "__main__":
    main()
