# Azure for Students 监控与自动止损 (增强版)

本模块专为 **Azure for Students** 学生开发者优惠设计，针对学生账户的两大核心风险：
1. **流量穿透扣费**：意外超出每月 100GB 通用额度 + 15GB Student 额度后，产生昂贵的出站流量费用；
2. **Student Credit 耗尽**：周期内消耗达到 `$100` 上限。

本模块在青龙面板中与原阿里云监控脚本**完全解耦并列运行**，互不干扰。

---

## 1. 核心特性清单

- **实时汇率双币种展示**：每日财报调用公共汇率接口，Student Credit 的总额度、已消耗额度、剩余估算额度均同时显示 USD 和预估 CNY。
- **免装库公网 IP 侦测**：直接利用 Azure ARM REST API 穿透网卡与关联的公共 IP，无需额外安装 `azure-mgmt-network`，卡片即可完整显示公网 IP。
- **订阅级 Cost 缓存（防 429）**：同一 Subscription 下配置多台 VM 时，单轮巡检只调用一次 Cost Management API，彻底避免触发 Azure API 速率限制。
- **“监控失明”紧急告警**：若因 Secret 过期、权限篡改或网络故障导致连续 3 次巡检失败，自动触发微信告警，防止因监控挂起导致流量偷跑。
- **告警携带财务明细**：在触发流量熔断止损、月初开机恢复时，推送消息自动附带当前的 Student Credit 累计消耗与剩余估算。
- **开机 Anti-OOS 退避**：执行开机后原地轮询 120 秒确认真实 `Running` 状态；连续 3 次开机失败（如可用区容量不足）自动退避至 30 分钟重试一次，保护 API 配额。
- **月初安全自动复机**：仅针对“上月由本程序流量熔断”的 VM 在新月进行安全恢复；手动关机的机器绝不越权启动。
- **青龙容器级稳定防护**：`fcntl` 文件并发锁（防定时任务并发堆叠）、`SIGALRM` 150 秒单机巡检硬超时、消息防骚扰冷却（超标提醒 24 小时 1 次，普通错误 1 小时 1 次）。

---

## 2. 身份凭证说明 (Microsoft Entra Service Principal)

Azure 采用企业级 Service Principal（服务主体）鉴权，程序最终需要四个身份/订阅信息：

| 配置项 | Azure 概念 | 作用 |
|---|---|---|
| `tenant_id` | Directory (tenant) ID / 目录(租户) ID | 标识身份租户目录 |
| `client_id` | Application (client) ID / 应用程序(客户端) ID | 标识监控脚本的应用身份 |
| `client_secret` | Client Secret Value / 客户端密码(值) | 脚本登录凭据（**填值而非机密ID**） |
| `subscription_id` | Subscription ID / 订阅 ID | 指定要监控的 Azure 订阅 |

---

## 3. 在 Azure 控制台创建脚本身份（中文/英文对照教程）

### 3.1 创建应用注册 (App Registration)
1. 登录 [Azure 门户 (Azure Portal)](https://portal.azure.com/)。
2. 在顶部搜索框搜索并进入：  
   `Microsoft Entra ID`（在部分中文界面中显示为：**Microsoft Entra ID** 或 原 **Azure Active Directory**）。
3. 在左侧菜单栏点击：  
   `App registrations`（**应用注册**） → 页面上方点击 `+ New registration`（**+ 新注册**）。
4. 填写应用信息：
   - **Name (名称)**：填入自定义名称，如 `azure-vm-monitor`。
   - **Supported account types (支持的帐户类型)**：保持默认的 `Accounts in this organizational directory only (仅此组织目录中的帐户 - 单一租户)`。
   - **Redirect URI (重定向 URI)**：留空不填。
5. 点击页面底部的 `Register`（**注册**）。

---

### 3.2 获取 Client ID 和 Tenant ID
创建完成后会自动跳转到应用的概览页面：
1. 在左侧菜单点击 `Overview`（**概述**）。
2. 在右侧属性面板中复制以下两项：
   - `Application (client) ID`（**应用程序(客户端) ID**） → 填入配置文件的 `client_id`。
   - `Directory (tenant) ID`（**目录(租户) ID**） → 填入配置文件的 `tenant_id`。

---

### 3.3 创建并获取 Client Secret (客户端密码)
1. 在应用的左侧菜单点击：  
   `Certificates & secrets`（**证书和密码** / 部分翻译为 **证书和机密**）。
2. 点击中间的 `Client secrets`（**客户端密码** / **客户端机密**）标签页。
3. 点击 `+ New client secret`（**+ 新客户端密码** / **+ 新建客户端机密**）。
4. 在弹出的侧边栏中：
   - **Description (说明)**：输入备注，例如 `qinglong-monitor`。
   - **Expires (截止期限)**：建议选择较长的时间（如 24 个月）。
5. 点击 `Add`（**添加**）。
6. **⚠️ 极其重要**：  
   添加成功后列表中会出现 `Secret ID`（**机密 ID**）和 `Value`（**值**）两列。  
   **必须复制 `Value`（值）这一列的内容**（它仅显示一次，离开页面后将隐藏），填入配置文件的 `client_secret`。切勿误复制 `Secret ID`。

---

### 3.4 获取 Subscription ID (订阅 ID)
1. 在 Azure 门户顶部搜索框输入并点击 `Subscriptions`（**订阅**）。
2. 点击你正在使用的 **Azure for Students** 订阅。
3. 在打开的 `Overview`（**概述**）页面中，找到并复制 `Subscription ID`（**订阅 ID**） → 填入配置文件的 `subscription_id`。

---

### 3.5 获取 Resource Group (资源组) 和 VM Name (虚拟机名称)
1. 在顶部搜索框搜索并进入 `Virtual machines`（**虚拟机**）。
2. 点击你要监控的虚拟机：
   - 虚拟机名称即为 `vm_name`。
   - 在其 `Overview`（**概述**）页面中，找到 `Resource group`（**资源组**）名称 → 填入配置文件的 `resource_group`。

---

## 4. 给脚本身份分配 RBAC 角色权限

新建的 Service Principal 默认没有任何云资源权限，必须在控制台中赋予以下 3 个角色：

### 4.1 分配虚拟机管理权限 (Virtual Machine Contributor)
1. 在 Azure 门户中进入 `Resource groups`（**资源组**） → 点击目标虚拟机所在的资源组。
2. 在左侧菜单点击 `Access control (IAM)`（**访问控制 (IAM)**）。
3. 依次点击：顶部 `+ Add`（**+ 添加**） → `Add role assignment`（**添加角色分配**）。
4. 在角色列表中搜索并选中 `Virtual Machine Contributor`（**虚拟机参与者**），点击下方的 `Next`（**下一步**）。
5. 在 `Members`（**成员**）选项卡中：
   - `Assign access to`（**分配访问权限至**）保持选择：`User, group, or service principal`（**用户、组或服务主体**）。
   - 点击 `+ Select members`（**+ 选择成员**）。
   - 在右侧侧边栏搜索你在 3.1 节创建的应用名称（如 `azure-vm-monitor`），选中后点击 `Select`（**选择**）。
6. 点击 `Review + assign`（**查看并分配**）完成授权。

---

### 4.2 分配监控读取权限 (Monitoring Reader)
在**同一个资源组**的 `Access control (IAM)`（**访问控制 (IAM)**）界面下：
1. 再次点击 `+ Add`（**+ 添加**） → `Add role assignment`（**添加角色分配**）。
2. 搜索并选择 `Monitoring Reader`（**监视读取者**），点击 `Next`（**下一步**）。
3. 同样在 `Members`（**成员**）中选择 `azure-vm-monitor` 应用主体。
4. 点击 `Review + assign`（**查看并分配**）完成授权。

---

### 4.3 分配成本管理读取权限 (Cost Management Reader)
**注意**：Cost Management API 的权限必须在 **Subscription (订阅)** 层级授予，不能只配在资源组。
1. 在 Azure 门户进入 `Subscriptions`（**订阅**） → 点击你的学生订阅。
2. 在左侧菜单点击 `Access control (IAM)`（**访问控制 (IAM)**）。
3. 依次点击：`+ Add`（**+ 添加**） → `Add role assignment`（**添加角色分配**）。
4. 搜索并选择 `Cost Management Reader`（**成本管理读取者**），点击 `Next`（**下一步**）。
5. 在 `Members`（**成员**）中选择 `azure-vm-monitor` 应用主体。
6. 点击 `Review + assign`（**查看并分配**）完成授权。

---

## 5. 配置文件 (`config.json`) 说明

保持原有的 `wxpush` 和阿里云 `users` 配置不变，直接追加 `azure` 列表：

```json
{
    "wxpush": {
        "wxpush_api_url": "[https://push.hzz.cool/wxsend](https://push.hzz.cool/wxsend)",
        "appid": "你的_APPID",
        "secret": "你的_SECRET",
        "userid": "你的_USERID",
        "template_id": "你的_TEMPLATE_ID"
    },
    "users": [
        // 保持原阿里云账号列表不变
    ],
    "azure": [
        {
            "name": "Azure 学生机",
            "tenant_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            "client_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            "client_secret": "你的_CLIENT_SECRET_VALUE",
            "subscription_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
            "resource_group": "student-rg",
            "vm_name": "student-vm",

            "credit_start_date": "2026-08-05",  // $100 Credit 权益生效起始日期 (YYYY-MM-DD)
            "generic_free_gb": 100,             // 全球通用月免费额度 (GB)
            "student_free_gb": 15,              // 学生专属月出站额度 (GB)
            "traffic_limit": 110,               // 触发自动止损关机的出站阈值 (GB)

            "credit_limit": 100,                // Student Credit 保护上限金额 ($)
            "credit_warning": 80,               // Credit 普通预警阈值 ($)
            "credit_emergency": 95,             // Credit 紧急预警阈值 ($)
            "cost_check_interval": 3600         // monitor 检查 Cost 的周期 (秒，默认 1 小时)
        }
    ]
}
