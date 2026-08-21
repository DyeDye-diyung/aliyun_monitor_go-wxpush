# Azure for Students 监控与自动止损

本目录是在原阿里云监控项目基础上**新增的 Azure 独立模块**。

> **重要：Azure 模块不会修改原有阿里云 `monitor.py`、`report.py` 或 `users` 配置逻辑。**
>
> 青龙中只需要额外添加 `azure_monitor.py` 与 `azure_report.py` 两个任务即可。

---

## 1. Azure 模块做什么？

Azure 模块针对的是 Azure for Students 学生优惠账户，重点不是像阿里云一样高频监控普通账户余额，而是防止：

1. Azure VM 出站流量异常，意外消耗免费额度后继续产生带宽费用；
2. Student Credit 累计使用接近或达到 `$100`；
3. Azure VM 因流量保护被释放后，跨自然月无法自动恢复。

当前实现包含：

- **每分钟流量监控**：查询 Azure VM 的 `Network Out Total`；
- **流量自动止损**：默认达到 `110 GB/月` 后执行 VM `Deallocate`；
- **月初自动恢复**：如果上个月确实由本程序因为“流量超限”执行了 Deallocate，本月第一次巡检时自动 Start；
- **开机结果确认**：Start 后轮询 VM 状态，确认真正进入 `running` 后才发送成功通知；
- **启动失败降频**：连续启动失败 3 次后，进入 30 分钟冷却，避免持续重试；
- **Cost 监控**：默认每小时查询一次 Student Credit 累计 Cost；
- **Cost 预警**：默认 `$80` 预警、`$95` 高位预警、`$100` 自动保护；
- **每日 Azure 报告**：每天汇报 VM 状态、流量、免费额度、Credit 使用量和保护状态；
- **通知防刷**：普通异常 1 小时冷却，流量熔断相关提醒 24 小时冷却；
- **任务并发锁**：避免青龙上一轮任务卡住后下一轮继续堆积；
- **单实例硬超时**：避免 Azure API 网络异常导致任务无限挂起；
- **日志轮转**：日志按天切割，默认保留 7 天；
- **IPv4 / SNI 兼容**：针对部分青龙 Docker 网络环境提供兼容处理。

Azure Monitor 当前的 VM 指标包含 `Network Out Total`，单位为 Bytes，默认聚合为 Total，最小采样粒度为 1 分钟。

---

# 2. 为什么 Azure 不使用阿里云 AK/SK？

阿里云使用的是：

```text
AccessKey ID
AccessKey Secret
```

Azure 没有完全对应的 `AK/SK` 模型。

本项目采用 Azure 推荐的 **Microsoft Entra Service Principal** 方式。

可以把它理解成：

```text
阿里云
AccessKey ID      ≈ Azure Client ID
AccessKey Secret  ≈ Azure Client Secret
                    +
                  Tenant ID
                    +
               Subscription ID
```

程序最终需要四个身份/订阅信息：

| 配置项 | Azure 中对应什么 | 用途 |
|---|---|---|
| `tenant_id` | Microsoft Entra Tenant ID | 指定身份目录 |
| `client_id` | App Registration 的 Application (client) ID | 标识脚本身份 |
| `client_secret` | App Registration 创建的 Client Secret Value | 脚本登录凭证 |
| `subscription_id` | Azure Subscription ID | 指定需要监控的 Azure 订阅 |

Azure 官方文档中的 Cost Management Python 示例也使用 `AZURE_CLIENT_ID`、`AZURE_TENANT_ID` 和 `AZURE_CLIENT_SECRET` 作为 Service Principal 凭证。

---

# 3. 在 Azure 创建类似 AccessKey 的脚本身份

## 3.1 打开 App Registrations

进入 Azure Portal：

**Microsoft Entra ID → App registrations → New registration**

创建一个专门给监控脚本使用的应用，例如：

```text
azure-vm-monitor
```

账号类型保持默认的 **Accounts in this organizational directory only** 即可。

Redirect URI 不需要填写。

创建完成后进入该应用。

---

## 3.2 获取 Client ID

进入：

```text
App registrations
→ azure-vm-monitor
→ Overview
```

找到：

```text
Application (client) ID
```

复制到：

```json
"client_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

---

## 3.3 获取 Tenant ID

同一个 Overview 页面找到：

```text
Directory (tenant) ID
```

复制到：

```json
"tenant_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

---

# 4. 创建 Client Secret

进入：

```text
Certificates & secrets
→ Client secrets
→ New client secret
```

填写：

```text
Description: qinglong-azure-monitor
Expires: 建议选择较长但可接受的期限
```

点击创建。

### ⚠️ 非常重要

创建后页面会显示两列：

```text
Secret ID
Value
```

**配置文件需要的是 `Value`，不是 `Secret ID`。**

`Value` 通常只在创建后显示一次，建议立即复制并安全保存。

然后填入：

```json
"client_secret": "这里填写 Secret Value"
```

不要把真实 Secret 提交到 GitHub。

---

# 5. 获取 Subscription ID

进入：

```text
Azure Portal
→ Subscriptions
→ 你的 Azure for Students 订阅
```

找到：

```text
Subscription ID
```

复制到：

```json
"subscription_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

Azure Cost Management Query API 在 subscription scope 下使用的路径就是 `/subscriptions/{subscriptionId}/providers/Microsoft.CostManagement/query`。

---

# 6. 获取 Resource Group 和 VM 名称

进入：

```text
Virtual machines
→ 你的 VM
```

记录：

### Resource group

例如：

```text
student-vm-rg
```

填入：

```json
"resource_group": "student-vm-rg"
```

### VM Name

例如：

```text
student-vm
```

填入：

```json
"vm_name": "student-vm"
```

---

# 7. 给 Service Principal 分配权限

这是整个 Azure 部署中最关键的一步。

Service Principal 创建完成后，它默认**没有权限操作你的 VM**。

本项目建议至少配置以下三个角色：

| 角色 | 建议 Scope | 用途 |
|---|---|---|
| `Virtual Machine Contributor` | 目标 Resource Group | 查询、启动、Deallocate VM |
| `Monitoring Reader` | 目标 Resource Group / Subscription | 读取 Azure Monitor Metrics |
| `Cost Management Reader` | Subscription | 查询 Cost Management |

Azure RBAC 允许直接给 Service Principal 分配这些 built-in roles。citeturn645835search1

---

# 8. 分配 Virtual Machine Contributor

进入：

```text
Resource groups
→ 你的 Resource Group
→ Access control (IAM)
→ Add
→ Add role assignment
```

选择：

```text
Virtual Machine Contributor
```

然后：

```text
Members
→ Assign access to: User, group, or service principal
→ Select members
→ 搜索 azure-vm-monitor
```

完成授权。

`Virtual Machine Contributor` 允许执行 VM 的创建、更新、删除、启动、重启和关机等操作，并且本身不会授予 Azure RBAC 角色分配能力。citeturn645835search3

### 为什么这里建议限定到 Resource Group？

因为脚本只需要控制你的那台学生 VM，不应该拥有整个 Azure 账户所有 VM 的控制权。

---

# 9. 分配 Monitoring Reader

仍然可以在目标 Resource Group：

```text
Access control (IAM)
→ Add role assignment
```

选择：

```text
Monitoring Reader
```

选择：

```text
azure-vm-monitor
```

确认。

`Monitoring Reader` 提供读取监控数据的权限，包括 Metrics。citeturn645835search1

---

# 10. 分配 Cost Management Reader

Cost Management 与普通 VM Resource Group 的权限范围不同，建议在** Subscription** 层授权。

进入：

```text
Subscriptions
→ 你的 Azure for Students Subscription
→ Access control (IAM)
→ Add role assignment
```

选择：

```text
Cost Management Reader
```

把 `azure-vm-monitor` 指定为成员。

微软当前官方说明中，`Cost Management Reader` 是只读成本数据和成本配置的角色，并包含 `Microsoft.CostManagement/*/read`。

### 不要给：

```text
Owner
Contributor
Cost Management Contributor
```

这个脚本不需要修改预算、导出任务或 Azure RBAC。

---

# 11. 推荐的权限结构

最终建议：

```text
Subscription
│
├── Cost Management Reader
│      └── azure-vm-monitor
│
└── Resource Group: student-vm-rg
       │
       ├── Virtual Machine Contributor
       │      └── azure-vm-monitor
       │
       └── Monitoring Reader
              └── azure-vm-monitor
```

这样脚本：

```text
可以：
✓ 读取 VM 状态
✓ 启动 VM
✓ Deallocate VM
✓ 读取 Network Out Total
✓ 查询 Cost Management

不能：
✗ 创建其它 VM
✗ 删除整个 Resource Group
✗ 修改 Azure RBAC
✗ 给别人授予权限
✗ 修改 Subscription 权限
```

注意：`Virtual Machine Contributor` 是一个相对宽的 VM 管理角色。如果你未来希望继续收敛权限，可以创建 Custom Role，只授予 VM read/start/deallocate 以及必要的 Metrics read。Azure 官方 RBAC 支持创建自定义角色。

---

# 12. Azure for Students 的流量逻辑

本项目按照你的实际 Student 账户使用场景，把流量保护设计为：

```text
100 GB/月
+
15 GB/月 Student outbound
```

代码中分别使用：

```json
"generic_free_gb": 100,
"student_free_gb": 15
```

但程序不会简单把 `100 + 15` 写成绝对保护线，而是默认设置一个略低的安全阈值：

```json
"traffic_limit": 110
```

因此：

```text
0 ~ 100 GB
    ✅ 正常

100 ~ 110 GB
    ⚠️ 超过第一层通用免费额度
    但暂不自动关机

>= 110 GB
    🚨 执行 VM Deallocate
```

Azure VM 的 `Network Out Total` 是所有网络接口的出站字节数，当前 Azure Monitor 文档将它定义为 VM 的 Outgoing Traffic，并支持 Total/Sum 聚合。

因此本项目只监控：

```text
Network Out Total
```

而不是：

```text
Network In + Network Out
```

---

# 13. Azure Student `$100` Credit 的逻辑

学生 Credit 与“每月免费流量”不是一回事。

```text
流量：按月重新计算

Student Credit：整个优惠周期累计
```

因此 config 使用：

```json
"credit_start_date": "2026-08-05"
```

它应该填写你这一次 `$100 Student Credit` 开始生效的日期，而不是每个月第一天。

脚本从该日期开始，通过 Cost Management Query API 累计 `PreTaxCost`。

当前 Cost Management Query API 版本为 `2025-03-01`，支持 subscription scope 的 Query 请求。

默认：

```text
$80
    ⚠️ 使用预警

$95
    🔴 高位预警

$100
    🚨 自动 Deallocate
```

Cost Management 数据可能存在刷新延迟，所以 `$100` 保护属于脚本层的最后防线，并不能等价于 Azure Sponsorships 门户中的实时余额。

---

# 14. config.json

如果原项目已经存在：

```json
"wxpush": {...},
"users": [...]
```

**不要修改原来的阿里云 `users`。**

只增加新的 `azure` 字段。

完整示例：

```json
{
    "wxpush": {
        "wxpush_api_url": "https://push.hzz.cool/wxsend",
        "appid": "你的_APPID",
        "secret": "你的_SECRET",
        "userid": "你的_USERID",
        "template_id": "你的_TEMPLATE_ID"
    },

    "users": [
        {
            "name": "阿里云香港",
            "ak": "LTAI5t9xxxxxxxxxxxxxx",
            "sk": "abcdef1234567890xxxxxxx",
            "region": "cn-hongkong",
            "resgroup": "rg-aek2xxxxxxx",
            "instance_id": "i-j6cxxxxxxxxxxxxxx",
            "traffic_limit": 180,
            "bill_threshold": 1.0
        }
    ],

    "azure": [
        {
            "name": "Azure Student VM",

            "tenant_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            "client_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            "client_secret": "你的_CLIENT_SECRET_VALUE",

            "subscription_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",

            "resource_group": "student-vm-rg",
            "vm_name": "student-vm",

            "credit_start_date": "2026-08-05",

            "generic_free_gb": 100,
            "student_free_gb": 15,

            "traffic_limit": 110,

            "credit_limit": 100,
            "credit_warning": 80,
            "credit_emergency": 95,

            "cost_check_interval": 3600
        }
    ]
}
```

---

# 15. Azure config 字段说明

| 字段 | 必填 | 含义 |
|---|---:|---|
| `name` | ✅ | 推送显示名称 |
| `tenant_id` | ✅ | Entra Directory/Tenant ID |
| `client_id` | ✅ | App Registration 的 Application ID |
| `client_secret` | ✅ | Client Secret 的 **Value** |
| `subscription_id` | ✅ | Azure Subscription ID |
| `resource_group` | ✅ | VM 所属 Resource Group |
| `vm_name` | ✅ | Azure VM 名称 |
| `credit_start_date` | ✅ | `$100 Student Credit` 开始日期，`YYYY-MM-DD` |
| `generic_free_gb` | ❌ | 通用免费出站额度，默认 100 |
| `student_free_gb` | ❌ | Student 出站额度，默认 15 |
| `traffic_limit` | ❌ | 自动 Deallocate 阈值，默认 110 GB |
| `credit_limit` | ❌ | Credit 保护上限，默认 `$100` |
| `credit_warning` | ❌ | Credit 普通预警，默认 `$80` |
| `credit_emergency` | ❌ | Credit 高位预警，默认 `$95` |
| `cost_check_interval` | ❌ | `azure_monitor.py` 查询 Cost 的最短间隔，默认 3600 秒 |
| `paused` | ❌ | `true` 时跳过该 Azure VM |
| `disabled` | ❌ | `true` 时跳过该 Azure VM |

---

# 16. 为什么 `cost_check_interval` 默认 1 小时？

Azure 流量是你的主要实时保护对象，所以：

```text
azure_monitor.py
每分钟
    ↓
Network Out Total
```

而 Student Credit 并不需要每分钟查询，因此：

```text
Cost Management
每小时检查一次
```

这样可以减少不必要的 Cost Management API 请求。

每天的 `azure_report.py` 会无条件查询一次最新的 Cost 数据。

---

# 17. 安装 Python 依赖

青龙 Python 环境执行：

```bash
pip install azure-identity azure-mgmt-compute requests
```

当前脚本直接使用 Azure Monitor REST API 查询 Metrics，因此不依赖额外的 `azure-monitor-query` Metrics 客户端。

Cost Management 同样直接调用 REST API，不需要单独安装 `azure-mgmt-costmanagement`。

---

# 18. 青龙面板配置

本模块新增两个定时任务。

## 18.1 Azure 实时监控

任务名称：

```text
Azure VM Monitor
```

命令：

```bash
python3 /你的脚本目录/azure_monitor.py
```

建议执行频率：

```text
每分钟
```

例如使用青龙 Cron：

```text
* * * * *
```

---

## 18.2 Azure 每日报告

任务名称：

```text
Azure Daily Report
```

命令：

```bash
python3 /你的脚本目录/azure_report.py
```

建议每天 09:30 执行：

```text
30 9 * * *
```

---

# 19. 与原阿里云青龙任务的关系

不要删除原来的：

```text
monitor.py
report.py
```

保留：

```text
阿里云 Monitor
阿里云 Report
```

新增：

```text
Azure Monitor
Azure Report
```

最终结构：

```text
青龙
│
├── monitor.py
│     └── 阿里云，每分钟
│
├── report.py
│     └── 阿里云，每天 09:30
│
├── azure_monitor.py
│     └── Azure，每分钟
│
└── azure_report.py
      └── Azure，每天 09:30
```

Azure 模块不会调用阿里云 API，也不会读取阿里云 `users` 数组。

---

# 20. Azure 自动恢复机制

这是 Azure 版本的重要设计。

假设：

```text
2026-08-20
本月 Network Out = 110GB
```

程序：

```text
Azure VM Running
      ↓
Network Out >= 110GB
      ↓
Deallocate
      ↓
azure_monitor_state.json 记录：
    deallocated_by_guard = true
    guard_reason = traffic
    guarded_month = 2026-08
```

第二天任务仍然执行，但 VM 已经处于保护状态。

到了：

```text
2026-09-01
```

程序第一次发现：

```text
guard_reason = traffic
        ↓
上次熔断月份 = 2026-08
        ↓
当前月份 = 2026-09
```

然后：

```text
Start VM
   ↓
每 10 秒查询一次 VM 状态
   ↓
确认 running
   ↓
发送“月初自动恢复成功”
   ↓
清除流量熔断状态
```

如果 Start 因容量/资源问题失败：

```text
连续失败 3 次
        ↓
30 分钟冷却
        ↓
之后再次尝试
```

这与新版阿里云 `monitor.py` 的 Anti-OOS 设计思路保持一致。

---

# 21. 为什么不会把普通手动关机的 Azure VM 自动启动？

非常重要。

脚本不会简单写成：

```python
if status != "running":
    start_vm()
```

它只有在状态文件中确认：

```text
deallocated_by_guard = true
```

并且：

```text
guard_reason = traffic
```

才会在新月份自动恢复。

因此如果你自己手动：

```text
Stop / Deallocate Azure VM
```

脚本不会因为“不是 Running”就偷偷把它启动。

---

# 22. `azure_monitor_state.json`

该文件由脚本自动生成，不需要手动创建。

典型结构：

```json
{
    "subscription-id::resource-group::student-vm": {
        "deallocated_by_guard": true,
        "guard_reason": "traffic",
        "guarded_month": "2026-08",
        "guard_time": "2026-08-20T13:25:00+00:00",
        "start_failures": 0,
        "last_cost_check_ts": 178xxxxxxx,
        "last_credit_used": 17.42,
        "notifications": {}
    }
}
```

不要把该文件提交到公开 Git 仓库，因为其中会包含监控运行状态和资源标识。

---

# 23. 日志文件

Azure 模块单独保存日志：

```text
azure_monitor.log
azure_report.log
```

日志按天轮转，并保留 7 天。

示例：

```text
azure_monitor.log
azure_monitor.log.2026-08-20
azure_monitor.log.2026-08-19
...
```

---

# 24. 常见问题

## 24.1 `AuthenticationFailed`

通常检查：

```text
tenant_id
client_id
client_secret
```

尤其确认 `client_secret` 填的是：

```text
Value
```

而不是：

```text
Secret ID
```

---

## 24.2 `AuthorizationFailed`

检查 Service Principal 是否被授予：

```text
Virtual Machine Contributor
Monitoring Reader
Cost Management Reader
```

并确认 Scope 正确：

```text
VM 操作 → Resource Group
Metrics → Resource Group / Subscription
Cost → Subscription
```

---

## 24.3 流量查询失败

检查：

```text
VM 是否已经运行过一段时间
Azure Monitor Metrics 是否可用
Service Principal 是否具有 Monitoring Reader
```

同时检查：

```text
azure_monitor.log
```

---

## 24.4 Cost 查询失败

Cost Management 查询需要对应 Subscription scope 的访问权限。

首先确认：

```text
Cost Management Reader
```

已经授予 Service Principal。

然后手动在 Azure Portal 的 Cost Management 中确认该订阅可以正常查看成本数据。

---

## 24.5 为什么日报里的 Student Credit 与门户余额可能不完全一致？

因为脚本读取的是：

```text
Cost Management ActualCost / PreTaxCost
```

而不是直接读取 Azure Sponsorships 余额。

Cost Management 数据可能存在刷新延迟，所以脚本报告中的：

```text
已使用 $X
剩余 $Y
```

应当理解为**程序估算值**。

最终的 Student Credit 余额，以 Azure Sponsorships 页面为准。

---

# 25. 安全建议

## 25.1 Client Secret 不要提交 Git

建议在 `.gitignore` 中加入：

```text
config.json
azure_monitor_state.json
azure_monitor.lock
```

如果你的项目必须提交一个示例配置，使用：

```text
config.example.json
```

不要提交真实 Secret。

---

## 25.2 不要给 Service Principal Owner

本项目只需要：

```text
Virtual Machine Contributor
Monitoring Reader
Cost Management Reader
```

不需要：

```text
Owner
Contributor
User Access Administrator
```

---

## 25.3 最好把 VM 放到专用 Resource Group

例如：

```text
azure-student-rg
```

然后把 VM 控制权限收敛到这个 Resource Group。

这样即使监控脚本凭证泄露，它能操作的范围仍然有限。

---

# 26. 推荐目录结构

最终目录可以是：

```text
project/
│
├── config.json
│
├── monitor.py
├── report.py
│
├── azure_monitor.py
├── azure_report.py
│
├── monitor.log
├── report.log
├── azure_monitor.log
├── azure_report.log
│
├── monitor_state.json
└── azure_monitor_state.json
```

原阿里云文件继续负责阿里云。

Azure 文件独立负责 Azure。

---

# 27. 最小部署流程

按以下顺序执行即可：

```text
1. Azure for Students 创建 VM
        ↓
2. 确认 Resource Group / VM Name / Subscription ID
        ↓
3. Microsoft Entra 创建 App Registration
        ↓
4. 创建 Client Secret
        ↓
5. 获取 Tenant ID / Client ID / Secret Value
        ↓
6. 给 Service Principal 授予 RBAC
        ↓
7. 修改 config.json 的 azure 数组
        ↓
8. 青龙安装 Python 依赖
        ↓
9. 手动测试 azure_report.py
        ↓
10. 手动测试 azure_monitor.py
        ↓
11. 青龙配置每分钟 azure_monitor.py
        ↓
12. 青龙配置每天 09:30 azure_report.py
```

---

# 28. 建议首次上线时先做手动测试

先不要立即让它自动运行。

在青龙终端：

```bash
python3 azure_report.py
```

确认：

```text
✅ 身份认证成功
✅ VM 状态正常
✅ Network Out 查询成功
✅ Cost 查询成功
✅ 微信推送成功
```

再执行：

```bash
python3 azure_monitor.py
```

确认没有：

```text
AuthenticationFailed
AuthorizationFailed
ResourceNotFound
```

最后再建立定时任务。

---

# 29. 重要说明

本模块用于：

```text
Azure for Students
VM 状态监控
流量保护
Student Credit 消耗监控
```

它不是 Azure 官方账单系统，也不是实时余额保证机制。

尤其是：

```text
Cost Management
```

存在数据刷新延迟。

因此建议同时在 Azure Portal 设置官方预算/通知作为额外防线。

本项目不会保证 API 异常、Azure 平台故障、数据延迟、权限配置错误或青龙容器异常时不会产生费用。

---

## 30. 相关 Azure 官方资料

- Azure Monitor VM metrics：
  https://learn.microsoft.com/azure/azure-monitor/reference/supported-metrics/microsoft-compute-virtualmachines-metrics
- Azure Cost Management Query API：
  https://learn.microsoft.com/rest/api/cost-management/query/usage
- Azure RBAC built-in roles：
  https://learn.microsoft.com/azure/role-based-access-control/built-in-roles
- Virtual Machine Contributor：
  https://learn.microsoft.com/azure/role-based-access-control/built-in-roles/compute
- Cost Management scope 与权限：
  https://learn.microsoft.com/azure/cost-management-billing/costs/understand-work-scopes
- Microsoft Entra Service Principal / App Registration：
  https://learn.microsoft.com/entra/identity-platform/howto-create-service-principal-portal
