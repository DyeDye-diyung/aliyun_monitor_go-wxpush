# -*- coding: utf-8 -*-
import json
import logging
import os
import requests
import warnings
import time
import signal
import socket
from logging.handlers import TimedRotatingFileHandler
try:
    import fcntl  # Linux 文件锁，防止青龙面板 cron 并发堆积
except ImportError:
    fcntl = None

from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest

# ================= 底层网络修复 (适配青龙 Docker 环境) =================
try:
    from aliyunsdkcore.vendored.requests.packages.urllib3.util import ssl_
    ssl_.HAS_SNI = True
except Exception:
    pass

_orig_getaddrinfo = socket.getaddrinfo
def _getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    res = _orig_getaddrinfo(host, port, family, type, proto, flags)
    ipv4_res = [r for r in res if r[0] == socket.AF_INET]
    return ipv4_res if ipv4_res else res
socket.getaddrinfo = _getaddrinfo_ipv4_only

warnings.filterwarnings("ignore")

# ================= 全局路径与配置 =================
curr_dir = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(curr_dir, 'config.json')
LOG_FILE = os.path.join(curr_dir, 'monitor.log')
STATE_FILE = os.path.join(curr_dir, 'monitor_state.json') # 状态缓存(防频繁推送)
LOCK_FILE = os.path.join(curr_dir, 'monitor.lock')        # 运行锁(防进程堆积)

# 配置常量
NOTIFY_COOLDOWN = 3600         # 普通异常推送冷却：1小时
OVERLIMIT_COOLDOWN = 86400     # 流量超标推送冷却：24小时
START_WAIT_TIMEOUT = 120       # 等待开机成功的最大轮询时间(秒)
START_POLL_INTERVAL = 10       # 开机状态轮询间隔(秒)
USER_CHECK_TIMEOUT = 150       # 单台机器最大检查时间，超时直接切断(防死锁)
MAX_START_FAILURES = 3         # 最大连续开机失败次数
RESOURCE_RETRY_COOLDOWN = 1800 # 资源不足时的重试冷却(半小时)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = TimedRotatingFileHandler(LOG_FILE, when='D', interval=1, backupCount=7, encoding='utf-8')
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    logger.addHandler(console)

# ================= 状态管理与推送 =================
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except: pass
    return {}

def save_state(state):
    try:
        with open(STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存状态失败: {e}")

def can_notify(state, instance_id, event_key, cooldown=NOTIFY_COOLDOWN):
    """判断是否已过推送冷却期"""
    last_ts = state.get(instance_id, {}).get(event_key, 0)
    return (time.time() - last_ts) >= cooldown

def mark_notified(state, instance_id, event_key):
    """记录推送时间"""
    state.setdefault(instance_id, {})[event_key] = time.time()

def send_wxpush(wx_conf, title, content):
    """Go-WXPush 推送，返回是否成功"""
    if not wx_conf: return False
    try:
        wxpush_url = wx_conf.get('wxpush_api_url', 'https://push.hzz.cool/wxsend')
        wx_payload = {
            "title": title,
            "content": content,
            "appid": wx_conf.get('appid'),
            "secret": wx_conf.get('secret'),
            "userid": wx_conf.get('userid'),
            "template_id": wx_conf.get('template_id')
        }
        res = requests.post(wxpush_url, json=wx_payload, timeout=10, verify=False)
        return res.json().get("errcode") == 0
    except Exception as e:
        logger.error(f"Push failed: {e}")
        return False

# ================= API 请求与查询 =================
def do_request(client, action, params=None):
    """轻量级通用请求"""
    try:
        req = CommonRequest()
        req.set_domain('ecs.aliyuncs.com')
        req.set_version('2014-05-26')
        req.set_action_name(action)
        req.set_method('POST')
        req.set_protocol_type('https')
        req.set_connect_timeout(5000)
        req.set_read_timeout(10000)
        if params:
            for k, v in params.items(): req.add_query_param(k, v)
        return json.loads(client.do_action_with_exception(req).decode('utf-8'))
    except Exception as e:
        logger.error(f"API {action} failed: {e}")
        return None

def get_status(client, instance_id, region):
    resp = do_request(client, 'DescribeInstances', {'RegionId': region, 'InstanceIds': json.dumps([instance_id])})
    if resp and "Instances" in resp:
        instances = resp.get("Instances", {}).get("Instance", [])
        if instances:
            return instances[0].get("Status")
    return "Unknown"

# ================= 核心巡检逻辑 =================
def check_and_act(user, wx_conf, state):
    target_id = user['instance_id'].strip()
    region = user['region'].strip()
    name = user.get('name', target_id)
    
    if user.get('paused') or user.get('disabled'):
        return  # 机器已暂停，跳过
    
    try:
        client = AcsClient(user['ak'].strip(), user['sk'].strip(), region)
        
        # 1. 查询 CDT 流量 (强制用杭州节点防报错)
        cdt_client = AcsClient(user['ak'].strip(), user['sk'].strip(), 'cn-hangzhou')
        req_cdt = CommonRequest()
        req_cdt.set_domain('cdt.aliyuncs.com')
        req_cdt.set_version('2021-08-13')
        req_cdt.set_action_name('ListCdtInternetTraffic')
        req_cdt.set_method('POST')
        req_cdt.set_protocol_type('https')
        req_cdt.set_connect_timeout(5000)
        req_cdt.set_read_timeout(10000)
        
        resp_cdt = json.loads(cdt_client.do_action_with_exception(req_cdt).decode('utf-8'))
        curr_gb = sum(d.get('Traffic', 0) for d in resp_cdt.get('TrafficDetails', [])) / (1024**3)
        
        # 2. 查询实例状态
        status = get_status(client, target_id, region)
        if status == "Unknown":
            logger.warning(f"❓[{name}] 机器状态未知或未找到")
            return
            
        limit = user.get('traffic_limit', 180)
        
        # 3. 决策逻辑
        if curr_gb < limit:
            # --- 流量安全 ---
            if status == "Stopped":
                failures = state.setdefault(target_id, {}).get('start_failures', 0)
                
                # 检查是否处于“资源不足”的冷却期中
                if failures >= MAX_START_FAILURES:
                    last_retry = state[target_id].get('last_retry_ts', 0)
                    if time.time() - last_retry < RESOURCE_RETRY_COOLDOWN:
                        return # 冷却中，静默跳过
                
                logger.info(f"[{name}] 流量安全 ({curr_gb:.2f}GB)，尝试启动实例...")
                state[target_id]['last_retry_ts'] = time.time()
                
                start_resp = do_request(client, 'StartInstance', {'InstanceId': target_id})
                if start_resp is None:
                    # 开机 API 失败
                    state[target_id]['start_failures'] = failures + 1
                    if can_notify(state, target_id, 'start_err'):
                        send_wxpush(wx_conf, "开机请求失败", f"⚠️ [{name}] 尝试开机失败，可能是由于阿里云资源售罄。脚本将在30分钟后重试。")
                        mark_notified(state, target_id, 'start_err')
                    return
                
                # 原地轮询等待开机成功 (取代原先的只发请求不管结果)
                waited = 0
                started = False
                while waited < START_WAIT_TIMEOUT:
                    time.sleep(START_POLL_INTERVAL)
                    waited += START_POLL_INTERVAL
                    real_status = get_status(client, target_id, region)
                    logger.info(f"[{name}] 等待开机... 当前: {real_status} ({waited}s)")
                    
                    if real_status == "Running":
                        started = True
                        break
                    elif real_status == "Stopped":
                        break # 被阿里云打回关机状态(没资源)
                
                if started:
                    state[target_id]['start_failures'] = 0
                    if can_notify(state, target_id, 'resumed'):
                        send_wxpush(wx_conf, "CDT 流量安全恢复", f"✅ [{name}] 流量正常 ({curr_gb:.2f}GB)，已成功恢复运行！")
                        mark_notified(state, target_id, 'resumed')
                else:
                    state[target_id]['start_failures'] = failures + 1
                    logger.warning(f"[{name}] 开机超时或被拒绝")
            
            elif status == "Running":
                state.setdefault(target_id, {})['start_failures'] = 0
                logger.info(f"🟢[{name}] 流量正常 ({curr_gb:.2f}GB / {limit}GB)，运行中")
        
        else:
            # --- 流量超标 ---
            if status == "Running":
                logger.info(f"[{name}] 流量超标，执行强制关机...")
                do_request(client, 'StopInstance', {'InstanceId': target_id})
                
                if can_notify(state, target_id, 'overlimit_stop', OVERLIMIT_COOLDOWN):
                    send_wxpush(wx_conf, "🚨 流量超标止损", f"🚨 [{name}] 当前流量 ({curr_gb:.2f}GB) 已超限额 ({limit}GB)，已触发强制关机保护！")
                    mark_notified(state, target_id, 'overlimit_stop')
            else:
                logger.info(f"🔴[{name}] 流量用满已关机 ({curr_gb:.2f}GB / {limit}GB)")
                # 即使已经在关机状态，每天也发一次提醒，防止被遗忘
                if can_notify(state, target_id, 'overlimit_remind', OVERLIMIT_COOLDOWN):
                    send_wxpush(wx_conf, "⚠️ 超标关机提醒", f"[{name}] 本月流量已用满，已处于关机保护状态。")
                    mark_notified(state, target_id, 'overlimit_remind')
                    
    except Exception as e:
        logger.error(f"[{name}] 巡检异常: {e}")

# ================= 超时中断守护 =================
class MonitorTimeout(Exception): pass

def timeout_handler(signum, frame):
    raise MonitorTimeout("巡检超时")

def check_with_timeout(user, wx_conf, state):
    """设置单台机器的巡检超时时间，防止某一台机器网络故障卡死整个脚本"""
    if not hasattr(signal, 'SIGALRM'):
        check_and_act(user, wx_conf, state)
        return
        
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(USER_CHECK_TIMEOUT)
    try:
        check_and_act(user, wx_conf, state)
    except MonitorTimeout:
        name = user.get('name', user.get('instance_id', 'Unknown'))
        logger.error(f"[{name}] 查询超时({USER_CHECK_TIMEOUT}s)，已强行跳过。")
    finally:
        signal.alarm(0)

# ================= 并发运行锁 =================
def acquire_lock():
    """获取进程锁，防止上一分钟的 cron 没跑完，下一分钟又启动导致内存溢出"""
    if fcntl is None: return True
    try:
        lock_file = open(LOCK_FILE, 'w')
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except (IOError, OSError):
        return None

def main():
    lock = acquire_lock()
    if lock is None:
        logger.warning("⚠️ 上一轮监控尚未结束，本轮任务跳过执行。")
        return
        
    try:
        if not os.path.exists(CONFIG_FILE):
            logger.error("配置文件 config.json 不存在！")
            return
            
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            
        wx_conf = cfg.get('wxpush', {})
        state = load_state()
        
        for u in cfg.get('users', []):
            check_with_timeout(u, wx_conf, state)
            
        save_state(state)
    finally:
        if hasattr(lock, 'close'):
            lock.close()

if __name__ == "__main__":
    main()
    
