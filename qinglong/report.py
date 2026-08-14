# -*- coding: utf-8 -*-
import json
import requests
import datetime
import os
import sys
import warnings
import logging
from logging.handlers import TimedRotatingFileHandler
import time
import socket

# ================= 底层网络与兼容性修复 =================
# 1. 修正 urllib3 在 Python 3.12 下引发的 SNI 丢失问题 (防 SSL 报错)
try:
    from aliyunsdkcore.vendored.requests.packages.urllib3.util import ssl_
    ssl_.HAS_SNI = True
except Exception:
    pass

# 2. 强制使用 IPv4 避免 IPv6 黑洞 (防请求无限卡死)
_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4_res = [r for r in res if r[0] == socket.AF_INET]
    return ipv4_res if ipv4_res else res
socket.getaddrinfo = _getaddrinfo_ipv4_only

warnings.filterwarnings("ignore")

try:
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkcore.request import CommonRequest
except ImportError:
    print("❌ 缺少 aliyun-python-sdk-core 库，请执行: pip install aliyun-python-sdk-core")
    sys.exit(1)


# ================= 全局配置与日志初始化 =================
curr_dir = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(curr_dir, 'config.json')
LOG_FILE = os.path.join(curr_dir, 'report.log')  # 日志也放在同级目录

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    # 每天轮转一次日志，保留 7 天
    handler = TimedRotatingFileHandler(LOG_FILE, when='D', interval=1, backupCount=7, encoding='utf-8')
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(console_handler)

BALANCE_ENDPOINTS = ('business.aliyuncs.com', 'business.ap-southeast-1.aliyuncs.com')


# ================= 核心函数 =================
def load_config():
    if not os.path.exists(CONFIG_FILE):
        logger.error(f"配置文件不存在: {CONFIG_FILE}")
        sys.exit(1)
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def sanitize_markdown(text):
    """文本防炸处理：替换特殊字符，避免推送排版错乱导致失败"""
    text = str(text)
    for ch in ('_', '*', '`', '[', ']'):
        text = text.replace(ch, ' ')
    return text.strip()

def get_usd_to_cny_rate():
    """获取实时汇率 (USD/CNY)，失败则返回保底汇率 7.0"""
    try:
        url = "https://api.exchangerate-api.com/v4/latest/USD"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            rate = data.get('rates', {}).get('CNY')
            if rate:
                logger.info(f"成功获取实时汇率: 1 USD = {float(rate):.2f} CNY")
                return float(rate)
    except Exception as e:
        logger.warning(f"获取实时汇率失败，将使用保底汇率 7.0。原因: {e}")
    return 7.0

def send_wxpush(wx_conf, title, content):
    """Go-WXPush 统一推送"""
    if not wx_conf:
        logger.warning("未配置 Go-WXPush，跳过推送")
        return
    
    wxpush_url = wx_conf.get('wxpush_api_url', 'https://push.hzz.cool/wxsend')
    wx_payload = {
        "title": title,
        "content": content,
        "appid": wx_conf.get('appid'),
        "secret": wx_conf.get('secret'),
        "userid": wx_conf.get('userid'),
        "template_id": wx_conf.get('template_id')
    }
    
    try:
        response = requests.post(wxpush_url, json=wx_payload, timeout=30, verify=False)
        response_data = response.json()
        if response_data.get("errcode") == 0:
            logger.info("任务完成，go-wxpush 推送成功。")
        else:
            logger.error(f"go-wxpush 推送返回错误: {response_data}")
    except Exception as e:
        logger.error(f"推送过程发生异常: {e}")

def do_common_request(client, domain, version, action, params=None, method='POST', retries=3):
    """带重试机制与严格超时的 API 请求函数，有效防卡死"""
    for attempt in range(1, retries + 1):
        try:
            request = CommonRequest()
            request.set_domain(domain)
            request.set_version(version)
            request.set_action_name(action)
            request.set_method(method)
            request.set_protocol_type('https')
            request.set_connect_timeout(5000)  # 连接 5 秒必须成功
            request.set_read_timeout(15000)    # 读取 15 秒超时
            
            if params:
                for k, v in params.items():
                    request.add_query_param(k, v)
            
            response = client.do_action_with_exception(request)
            return json.loads(response.decode('utf-8'))
        
        except Exception as e:
            logger.warning(f"请求 {action} 失败 (尝试 {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            logger.error(f"请求 {action} 最终失败，已重试 {retries} 次")
            return None

def get_account_balance(client, bill_endpoint):
    """查询账户可用余额并进行节点容灾"""
    endpoints = [bill_endpoint] + [ep for ep in BALANCE_ENDPOINTS if ep != bill_endpoint]
    for endpoint in endpoints:
        data = do_common_request(client, endpoint, '2017-12-14', 'QueryAccountBalance', retries=1)
        if not data or not data.get('Success'):
            continue
        info = data.get('Data') or {}
        raw_amount = info.get('AvailableAmount')
        if raw_amount is None:
            continue
        try:
            amount = float(str(raw_amount).replace(',', ''))
            return amount, info.get('Currency', '')
        except ValueError:
            continue
    return None, None

def convert_currency(amount, from_currency, target_rate):
    """统一汇率转换工具函数，输出美金和人民币"""
    if from_currency == "CNY":
        return amount / target_rate, amount
    else:
        return amount, amount * target_rate


# ================= 主流程 =================
def main():
    config = load_config()
    users = config.get('users', [])
    wx_conf = config.get('wxpush', {})
    
    current_rate = get_usd_to_cny_rate()
    
    success_count = 0
    fail_count = 0
    report_lines = []
    balance_cache = {}  # 缓存同一个账号(AK)的余额，避免重复查询
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    for user in users:
        try:
            target_id = user.get('instance_id', '').strip()
            target_region = user.get('region', '').strip()
            resgroup = user.get('resgroup', '').strip()
            bill_endpoint = user.get('bill_endpoint', 'business.ap-southeast-1.aliyuncs.com').strip()
            
            # [名字获取及防炸处理]
            user_name = sanitize_markdown(user.get('name', '').strip() or target_id or "Unknown_Device")

            # [监控开关检查] 允许跳过停机/弃用的实例
            if user.get('paused') or user.get('disabled'):
                logger.info(f"[{user_name}] 监控已暂停，跳过接口请求")
                report_lines.append(
                    f"👤 *{user_name}* (已暂停)\n"
                    f"   ⏸️ 监控: 配置文件设定为暂停\n"
                )
                success_count += 1
                continue
            
            client = AcsClient(user['ak'].strip(), user['sk'].strip(), target_region)
            
            # 1. CDT 流量 (强制指向 cn-hangzhou 全局节点，防止因为海外地域导致 API 报错)
            cdt_client = AcsClient(user['ak'].strip(), user['sk'].strip(), 'cn-hangzhou')
            traffic_data = do_common_request(cdt_client, 'cdt.aliyuncs.com', '2021-08-13', 'ListCdtInternetTraffic')
            traffic_gb = -1
            if traffic_data:
                traffic_gb = sum(d.get('Traffic', 0) for d in traffic_data.get('TrafficDetails', [])) / (1024**3)

            # 2. BSS 账单 (引入重试与金额全量累加)
            bill_amount = -1
            bill_currency = "USD"
            
            # 尝试1: 查国内实例账单
            bill_params = {'BillingCycle': datetime.datetime.now().strftime("%Y-%m"), 'InstanceID': target_id}
            bill_data = do_common_request(client, 'business.aliyuncs.com', '2017-12-14', 'DescribeInstanceBill', bill_params, retries=1)
            
            if bill_data and bill_data.get('Success'):
                items = bill_data.get('Data', {}).get('Items', [])
                bill_amount = sum(float(item.get('PretaxAmount', 0)) for item in items)
                if items:
                    bill_currency = items[0].get('Currency', 'USD')
            else:
                # 尝试2: 回退查国际站总账单
                bill_params2 = {'BillingCycle': datetime.datetime.now().strftime("%Y-%m")}
                bill_data2 = do_common_request(client, bill_endpoint, '2017-12-14', 'QueryBillOverview', bill_params2, retries=1)
                
                if bill_data2 and bill_data2.get('Success'):
                    items2 = bill_data2.get('Data', {}).get('Items', {}).get('Item', [])
                    bill_amount = sum(float(item.get('PretaxAmount', 0)) for item in items2)
                    if items2:
                        bill_currency = items2[0].get('Currency', 'USD')

            # 2.5 获取账户余额 (利用缓存机制防刷接口)
            ak_key = user['ak'].strip()
            if ak_key in balance_cache:
                bal_amount, bal_currency = balance_cache[ak_key]
            else:
                bal_amount, bal_currency = get_account_balance(client, bill_endpoint)
                balance_cache[ak_key] = (bal_amount, bal_currency)

            # 3. ECS 状态查询
            ecs_params = {'PageSize': 50, 'RegionId': target_region}
            if resgroup:
                ecs_params['ResourceGroupId'] = resgroup
            ecs_data = do_common_request(client, 'ecs.aliyuncs.com', '2014-05-26', 'DescribeInstances', ecs_params)
            
            status, ip, spec = "NotFound", "N/A", "N/A"
            if ecs_data and 'Instances' in ecs_data:
                for inst in ecs_data['Instances'].get('Instance', []):
                    if inst['InstanceId'] == target_id:
                        status = inst.get('Status', 'Unknown')
                        pub = inst.get('PublicIpAddress', {}).get('IpAddress', [])
                        eip = inst.get('EipAddress', {}).get('IpAddress', "")
                        ip = eip if eip else (pub[0] if pub else "无公网IP")
                        
                        cpu = inst.get('Cpu', 0)
                        mem_mb = inst.get('Memory', 0)
                        mem_str = f"{int(mem_mb/1024)}" if (mem_mb > 0 and mem_mb % 1024 == 0) else f"{mem_mb/1024:.1f}"
                        spec = f"{cpu}C{mem_str}G"
                        break 

            # 4. 数据换算与评价逻辑
            quota = user.get('traffic_limit', 180)
            bill_limit_usd = user.get('bill_threshold', 1.0)
            
            # 流量格式化
            if traffic_gb >= 0:
                percent = (traffic_gb / quota) * 100 if quota > 0 else 0
                traffic_str = f"{traffic_gb:.2f} GB ({percent:.1f}%)"
            else:
                traffic_str = "⚠️ 查询失败"
            
            # 账单格式化 (引入自动汇率)
            if bill_amount == -1:
                bill_str = "查询失败"
                usd_val = 0.0
            else:
                usd_val, cny_val = convert_currency(bill_amount, bill_currency, current_rate)
                bill_str = f"${usd_val:.2f} (预估¥{cny_val:.2f})"

            # 余额格式化 (引入自动汇率)
            if bal_amount is not None:
                bal_usd, bal_cny = convert_currency(bal_amount, bal_currency, current_rate)
                balance_str = f"${bal_usd:.2f} (预估¥{bal_cny:.2f})"
                if bal_usd < 0:
                    balance_str += " ⚠️ 欠费"
            else:
                balance_str = "查询失败"

            # 状态评价表情
            status_icon = "✅"
            if traffic_gb < 0 or bill_amount == -1: status_icon = "⚠️ 接口查询异常"
            elif traffic_gb > quota: status_icon = "⚠️ 流量超标"
            elif usd_val > bill_limit_usd: status_icon = "💸 扣费预警"
            
            run_icon = "🟢" if status == "Running" else "🔴"
            if status == "Stopped": run_icon = "⚫"
            if status == "NotFound": run_icon = "❓"

            # 构建个人卡片
            user_report = (
                f"👤 *{user_name}* ({spec})\n"
                f"   🖥️ 状态: {run_icon} {status}\n"
                f"   🌐 IP: `{ip}`\n"
                f"   📉 流量: {traffic_str}\n"
                f"   💰 账单: *{bill_str}*\n"
                f"   💳 余额: *{balance_str}*\n"
                f"   📝 评价: {status_icon}\n"
            )
            report_lines.append(user_report)
            success_count += 1
            logger.info(f"[{user_name}] 数据获取成功")

        except Exception as e:
            err_msg = f"❌ *{sanitize_markdown(user.get('name', 'Unknown'))}* Error: {sanitize_markdown(str(e))}\n"
            report_lines.append(err_msg)
            fail_count += 1
            logger.exception(f"处理用户 {user.get('name', 'Unknown')} 时出错")

    # ================= 拼接与推送 =================
    push_title = f"阿里云财报: 成功{success_count}, 失败{fail_count}"
    
    header = f"📊 [阿里云多账号 - 每日财报]\n"
    header += f"📅 日期: {today} (实时汇率: {current_rate:.2f})\n"
    header += f"✅ 成功机器：{success_count}，❌ 失败机器：{fail_count}\n"
    header += "--------------------------------\n"
    
    final_summary = header + "\n".join(report_lines)

    # 发送推送
    send_wxpush(wx_conf, push_title, final_summary)
    logger.info("=== 本次巡检结束，最终推送内容如下 ===\n" + final_summary)

if __name__ == "__main__":
    main()
    
