# -*- coding: utf-8 -*-
import json
import logging
import os
import requests
import warnings
from logging.handlers import TimedRotatingFileHandler
from aliyunsdkcore.client import AcsClient
from aliyunsdkcore.request import CommonRequest

warnings.filterwarnings("ignore")

# CONFIG_FILE = '/opt/scripts/config.json'
# LOG_FILE = '/opt/scripts/monitor.log'

# --- 最小化修改：自动获取脚本所在目录 ---
curr_dir = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(curr_dir, 'config.json')
LOG_FILE = os.path.join(curr_dir, 'monitor.log')

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = TimedRotatingFileHandler(LOG_FILE, when='D', interval=1, backupCount=7, encoding='utf-8')
    handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    logger.addHandler(handler)

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def send_tg(tg_conf, text):
    if not tg_conf.get('bot_token'): return
    try:
        requests.post(f"https://api.telegram.org/bot{tg_conf['bot_token']}/sendMessage", 
                      json={"chat_id": tg_conf['chat_id'], "text": text, "parse_mode": "Markdown"}, timeout=5)
    except: pass

def send_wxpush(wx_conf, title, content):
    """Go-WXPush 统一推送"""
    if not wx_conf: return
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
        requests.post(wxpush_url, json=wx_payload, timeout=10, verify=False)
    except Exception as e:
        logger.error(f"Push failed: {e}")

def do_request(client, action, params=None):
    try:
        req = CommonRequest()
        req.set_domain('ecs.aliyuncs.com')
        req.set_version('2014-05-26')
        req.set_action_name(action)
        req.set_method('POST')
        if params:
            for k,v in params.items(): req.add_query_param(k, v)
        return client.do_action_with_exception(req)
    except Exception as e:
        logger.error(f"API {action} failed: {e}")
        return None

def check(user, wx_conf):
    try:
        # 数据清洗
        target_id = user['instance_id'].strip()
        region = user['region'].strip()
        resgroup = user['resgroup'].strip()
        client = AcsClient(user['ak'].strip(), user['sk'].strip(), region)
        
        # 1. CDT 流量
        req_cdt = CommonRequest()
        req_cdt.set_domain('cdt.aliyuncs.com')
        req_cdt.set_version('2021-08-13')
        req_cdt.set_action_name('ListCdtInternetTraffic')
        req_cdt.set_method('POST')
        resp_cdt = client.do_action_with_exception(req_cdt)
        data_cdt = json.loads(resp_cdt.decode('utf-8'))
        curr_gb = sum(d.get('Traffic',0) for d in data_cdt.get('TrafficDetails',[])) / (1024**3)
        
        # 2. ECS 状态 (本地匹配模式)
        ecs_params = {'PageSize': 50, 'RegionId': region}
        if resgroup:
            ecs_params['ResourceGroupId'] = resgroup
        resp_ecs = do_request(client, 'DescribeInstances', ecs_params)
        
        status = "Unknown"
        if resp_ecs:
            data_ecs = json.loads(resp_ecs.decode('utf-8'))
            for inst in data_ecs.get("Instances", {}).get("Instance", []):
                if inst['InstanceId'] == target_id:
                    status = inst.get("Status")
                    break
        
        if status == "Unknown":
            # 如果没找到机器，就不做任何操作，防止误判
            print(f"❓机器未找到")
            return

        limit = user.get('traffic_limit', 180)
        
        if curr_gb < limit:
            if status == "Stopped":
                logger.info(f"[{user['name']}] Start instance...")
                do_request(client, 'StartInstance', {'InstanceId': target_id})
                # send_tg(tg_conf, f"✅ *[{user['name']}]* 流量安全 ({curr_gb:.2f}GB)，已恢复运行。")
                send_wxpush(wx_conf, "CDT 流量止损恢复", f"✅ [{user['name']}] 流量安全 ({curr_gb:.2f}GB)，已恢复运行。")
            else:
                print(f"🟢[{user['name']}] 流量正常，机器运行中 (当前: {curr_gb:.2f}GB / 阈值: {limit}GB)，机器运行中")
        else:
            if status == "Running":
                logger.info(f"[{user['name']}] Stop instance...")
                do_request(client, 'StopInstance', {'InstanceId': target_id})
                # send_tg(tg_conf, f"🚨 *[{user['name']}]* 流量超标 ({curr_gb:.2f}GB)，已强制关机！")
                send_wxpush(wx_conf, "CDT 流量止损触发", f"🚨 [{user['name']}] 流量超标 ({curr_gb:.2f}GB)，已执行强制关机！")
            else:
                print(f"🔴[{user['name']}] 流量用满，机器停止中 (当前: {curr_gb:.2f}GB / 阈值: {limit}GB)，机器停止中")

    except Exception as e:
        logger.error(f"Check failed: {e}")

def main():
    cfg = load_config()
    # for u in cfg.get('users', []):
    #     check(u, cfg.get('telegram', {}))
    
    # 注意这里 key 改为了 wxpush
    wx_conf = cfg.get('wxpush', {})
    for u in cfg.get('users', []):
        check(u, wx_conf)

if __name__ == "__main__":
    main()
