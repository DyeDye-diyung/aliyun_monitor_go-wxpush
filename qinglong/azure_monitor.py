# -*- coding: utf-8 -*-
"""
Azure for Students VM 流量 / Credit 自动监控 (修复货币自适应换算)
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
# 底层网络兼容：SNI + IPv4
# ============================================================

_orig_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4_res = [r for r in res if r[0] == socket.AF_INET]
    return ipv4_res if ipv4_res else res


socket.getaddrinfo = _getaddrinfo_ipv4_only

# ============================================================
# 全局路径与常量
# ============================================================

CURR_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(CURR_DIR, "config.json")
LOG_FILE = os.path.join(CURR_DIR, "azure_monitor.log")
STATE_FILE = os.path.join(CURR_DIR, "azure_monitor_state.json")
LOCK_FILE = os.path.join(CURR_DIR, "azure_monitor.lock")

NOTIFY_COOLDOWN = 3600              # 普通异常通知冷却：1 小时
OVERLIMIT_COOLDOWN = 86400          # 流量超标通知冷却：24 小时
START_WAIT_TIMEOUT = 120            # 开机后最多轮询等待 120 秒
START_POLL_INTERVAL = 10            # 每 10 秒查询一次开机状态
USER_CHECK_TIMEOUT = 150            # 单个 Azure 账号巡检硬超时：150 秒
MAX_START_FAILURES = 3              # 连续开机失败达到 3 次进入防爆破冷却
RESOURCE_RETRY_COOLDOWN = 1800      # 资源不足冷却时间：30 分钟
CHECK_FAILURE_ALERT_THRESHOLD = 3   # 连续巡检失败达 3 次发送"监控失明"告警

API_RETRIES = 3
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
# 配置 / 状态 / 汇率
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


def get_usd_to_cny_rate():
    try:
        url = "https://api.exchangerate-api.com/v4/latest/USD"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            rate = res.json().get("rates", {}).get("CNY")
            if rate:
                return float(rate)
    except Exception:
        pass
    return 7.0


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


def get_credit_summary_line(user, state):
    key = resource_key(user)
    item = state.get(key, {})
    used = item.get("last_credit_used")
    if used is not None:
        limit = float(user.get("credit_limit", 100))
        rem = max(limit - used, 0.0)
        return f"\n💳 Credit 已消耗: ${used:.2f} (剩余约 ${rem:.2f})"
    return ""


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
        url = wx_conf.get("wxpush_api_url", "https://push.hzz.cool/wxsend")
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
                f"Azure API {method} {url} 失败 (尝试 {attempt}/{retries}): {e}"
            )
            if attempt < retries:
                time.sleep(2 * attempt)

    logger.error(f"Azure API {method} {url} 最终失败，已重试 {retries} 次")
    raise last_error


# ============================================================
# VM 控制与轮询
# ============================================================


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
    logger.warning(f"[{user['name']}] 执行 Azure VM Deallocate (解除分配)...")
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
# Metrics & Cost
# ============================================================


def get_month_start_utc():
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def month_key(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def get_vm_resource_id(user):
    return (
        f"/subscriptions/{user['subscription_id'].strip()}"
        f"/resourceGroups/{user['resource_group'].strip()}"
        f"/providers/Microsoft.Compute/virtualMachines/{user['vm_name'].strip()}"
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
        f"{MANAGEMENT_ENDPOINT}{resource_id}/providers/microsoft.insights/metrics"
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
    url = f"{MANAGEMENT_ENDPOINT}{scope}/providers/Microsoft.CostManagement/query"

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
        return 0.0, "USD"

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

    currency = "USD"
    currency_index = names.index("Currency") if "Currency" in names else None
    if currency_index is not None:
        currencies = {
            str(row[currency_index]).upper()
            for row in rows
            if len(row) > currency_index and row[currency_index] is not None
        }
        if len(currencies) > 1:
            raise RuntimeError(f"Cost API 返回多个货币单位: {currencies}")
        if currencies:
            currency = next(iter(currencies))

    return total, currency


# ============================================================
# 月初自动恢复
# ============================================================


def auto_restore_if_needed(user, compute_client, state, wx_conf):
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

    if item.get("last_restore_month") == current_month:
        return

    status = get_vm_status(compute_client, user)
    if status == "running":
        item["deallocated_by_guard"] = False
        item["last_restore_month"] = current_month
        state[key] = item
        return

    if status not in ("deallocated", "stopped"):
        logger.warning(f"[{user['name']}] 月初恢复时状态为 {status}，跳过自动启动。")
        return

    failures = item.get("start_failures", 0)
    if failures >= MAX_START_FAILURES:
        last_retry = item.get("last_retry_ts", 0)
        if time.time() - last_retry < RESOURCE_RETRY_COOLDOWN:
            logger.info(f"[{user['name']}] 月初恢复处于资源不足冷却期中，本轮跳过。")
            return

    logger.warning(f"[{user['name']}] 检测到上月流量熔断，进入新月份 {current_month}，执行自动恢复...")
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
                cred_line = get_credit_summary_line(user, state)
                content = (
                    f"✅ [{sanitize_markdown(user['name'])}] Azure 月初自动恢复成功！\n"
                    f"原因：跨入新自然月 ({current_month})，流量额度已重置\n"
                    f"状态：已确认进入 Running 运行中{cred_line}"
                )
                if send_wxpush(wx_conf, "Azure 月初自动恢复", content):
                    mark_notified(state, key, "auto_restore")
                    save_state(state)
        else:
            item["start_failures"] = failures + 1
            state[key] = item
            save_state(state)
            logger.warning(f"[{user['name']}] 月初自动恢复：Start 指令完成，但在限时内未确认 Running。")

    except Exception as e:
        item["start_failures"] = failures + 1
        state[key] = item
        save_state(state)
        logger.error(f"[{user['name']}] 月初自动恢复失败: {e}")
        if can_notify(state, key, "restore_error"):
            content = (
                f"⚠️ [{sanitize_markdown(user['name'])}] Azure 月初自动恢复失败。\n"
                f"错误：{sanitize_markdown(str(e))}"
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

    # 1. 尝试月初自动恢复
    auto_restore_if_needed(user, compute_client, state, wx_conf)

    # 2. 查询状态与流量
    status = get_vm_status(compute_client, user)
    if status == "unknown":
        logger.warning(f"❓[{name}] Azure VM 状态未知")
        return

    traffic_bytes = get_monthly_network_out(user, credential)
    traffic_gb = bytes_to_gb(traffic_bytes)
    traffic_limit = float(user.get("traffic_limit", 110))

    logger.info(
        f"[{name}] Network Out {traffic_gb:.2f} GB / {traffic_limit:.2f} GB，VM={status}"
    )

    # 3. 成功获取数据，重置连续巡检失败计数（消除“监控失明”预警）
    item = state.setdefault(key, {})
    if "check_failures" in item:
        item.pop("check_failures", None)

    # 4. 定期查询 Student Credit (默认每小时一次，自适应汇率换算)
    last_cost_check = item.get("last_cost_check_ts", 0)
    cost_check_interval = int(user.get("cost_check_interval", 3600))
    if time.time() - last_cost_check >= cost_check_interval:
        item["last_cost_check_ts"] = time.time()
        try:
            raw_cost, cur = get_credit_usage(user, credential)
            current_rate = get_usd_to_cny_rate()
            if cur == "CNY":
                credit_used = raw_cost / current_rate
                logger.info(f"[{name}] Cost API 返回 ¥{raw_cost:.2f} CNY，已折算为 ${credit_used:.2f} USD")
            else:
                credit_used = raw_cost
                logger.info(f"[{name}] Cost API 返回 ${credit_used:.2f} USD")

            credit_limit = float(user.get("credit_limit", 100))
            credit_warning = float(user.get("credit_warning", 80))
            credit_emergency = float(user.get("credit_emergency", 95))

            item["last_credit_used"] = credit_used
            save_state(state)

            if credit_used >= credit_limit and status == "running":
                logger.warning(f"[{name}] Credit 达到保护上限，执行 Deallocate。")
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
                        f"🚨 [{sanitize_markdown(name)}] Azure Student Credit 已达到保护上限！\n"
                        f"累计 Cost：${credit_used:.2f}\n"
                        f"保护上限：${credit_limit:.2f}\n"
                        f"动作：已执行 VM Deallocate (解除分配停止计费)。\n"
                        f"⚠️ Cost Management 存在延迟，请同步确认 Azure Sponsorships。"
                    )
                    if send_wxpush(wx_conf, "Azure Credit 止损", content):
                        mark_notified(state, key, "credit_stop")
                        save_state(state)
                return

            if credit_used >= credit_emergency and can_notify(state, key, "credit_emergency"):
                if send_wxpush(
                    wx_conf,
                    "Azure Credit 高位预警",
                    f"⚠️ [{sanitize_markdown(name)}] Student Credit 已使用 ${credit_used:.2f}，接近 ${credit_limit:.2f} 保护线。"
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
                    f"错误：{sanitize_markdown(str(e))}"
                )
                if send_wxpush(wx_conf, "Azure Cost 查询异常", content):
                    mark_notified(state, key, "cost_query_error")
                    save_state(state)

    # 5. 流量安全状态
    if traffic_gb < traffic_limit:
        item = state.setdefault(key, {})
        item.setdefault("start_failures", 0)
        item.setdefault("notifications", {})
        save_state(state)
        return

    # 6. 流量超标止损
    cred_line = get_credit_summary_line(user, state)
    if status == "running":
        logger.warning(
            f"[{name}] 流量超标 ({traffic_gb:.2f} GB >= {traffic_limit:.2f} GB)，执行 Deallocate..."
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
                    f"🚨 [{sanitize_markdown(name)}] Azure 出站流量超标！\n"
                    f"当月流量：{traffic_gb:.2f} GB\n"
                    f"止损阈值：{traffic_limit:.2f} GB\n"
                    f"动作：已执行 VM Deallocate 关机熔断保护{cred_line}"
                )
                if send_wxpush(wx_conf, "Azure 流量超标止损", content):
                    mark_notified(state, key, "overlimit_stop")
                    save_state(state)
        except Exception as e:
            logger.error(f"[{name}] Azure Deallocate 失败: {e}")
            if can_notify(state, key, "deallocate_error"):
                content = (
                    f"❌ [{sanitize_markdown(name)}] Azure 流量超标，但 Deallocate 失败！\n"
                    f"当月流量：{traffic_gb:.2f} GB\n"
                    f"错误：{sanitize_markdown(str(e))}\n"
                    f"请立即手动前往 Portal 处理。"
                )
                if send_wxpush(wx_conf, "Azure 流量止损失败", content):
                    mark_notified(state, key, "deallocate_error")
                    save_state(state)

    else:
        logger.warning(f"🔴[{name}] Azure 流量超标，VM 处于 {status} 保护状态")
        if can_notify(state, key, "overlimit_remind", OVERLIMIT_COOLDOWN):
            content = (
                f"⚠️ [{sanitize_markdown(name)}] Azure 流量熔断提醒。\n"
                f"当月出站：{traffic_gb:.2f} GB (阈值: {traffic_limit:.2f} GB)\n"
                f"VM 状态：{status}\n"
                f"保持解除分配保护中，新月将自动恢复。{cred_line}"
            )
            if send_wxpush(wx_conf, "Azure 超限保护提醒", content):
                mark_notified(state, key, "overlimit_remind")
                save_state(state)


# ============================================================
# 超时与连续失败失明监控
# ============================================================


class AzureMonitorTimeout(Exception):
    pass


def timeout_handler(signum, frame):
    raise AzureMonitorTimeout("Azure 单实例巡检超时")


def check_with_timeout(user, wx_conf, state):
    name = user.get("name", user.get("vm_name", "Azure"))

    if not hasattr(signal, "SIGALRM"):
        try:
            check_and_act(user, wx_conf, state)
        except Exception as e:
            handle_check_exception(user, wx_conf, state, e)
        return

    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(USER_CHECK_TIMEOUT)
    try:
        check_and_act(user, wx_conf, state)
    except AzureMonitorTimeout:
        logger.error(f"[{name}] Azure 巡检超时({USER_CHECK_TIMEOUT}s)，已强行中断本台机器。")
        handle_check_exception(user, wx_conf, state, RuntimeError("巡检硬超时"))
    except Exception as e:
        handle_check_exception(user, wx_conf, state, e)
    finally:
        signal.alarm(0)


def handle_check_exception(user, wx_conf, state, error):
    name = user.get("name", user.get("vm_name", "Azure"))
    key = resource_key(user)
    logger.error(f"[{name}] 巡检异常: {error}")

    item = state.setdefault(key, {})
    failures = item.get("check_failures", 0) + 1
    item["check_failures"] = failures
    save_state(state)

    if failures >= CHECK_FAILURE_ALERT_THRESHOLD and can_notify(state, key, "monitor_blind"):
        content = (
            f"🚨 [{sanitize_markdown(name)}] 监控失明告警！\n"
            f"已连续 {failures} 次巡检失败。\n"
            f"最近错误：{sanitize_markdown(str(error))}\n"
            f"期间自动流量止损与保护已失效，请立即检查 Azure 凭证有效性。"
        )
        if send_wxpush(wx_conf, "Azure 监控失明告警", content):
            mark_notified(state, key, "monitor_blind")
            save_state(state)


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
        logger.exception(f"Azure monitor main 遇到致命错误: {e}")
        raise
    finally:
        if hasattr(lock, "close"):
            lock.close()


if __name__ == "__main__":
    main()
