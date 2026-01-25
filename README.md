
#  阿里云国际版 CDT 流量监控 & 自动止损脚本

![OS](https://img.shields.io/badge/OS-Linux-blue?logo=linux)
![Python](https://img.shields.io/badge/Python-3.x-yellow?logo=python)
![Alibaba Cloud](https://img.shields.io/badge/Alibaba%20Cloud-International-orange?logo=alibabacloud)

一个仅为自定义 **Alpine** 系统准备的 **阿里云国际版（Alibaba Cloud International）** 设计的 **CDT 公网流量监控 + 自动止损工具**，  
在流量或账单即将失控前 **强制关机**，真正帮你守住钱包 💰。

---

## ✨ 核心特性

- 🛡️ **流量熔断**：每分钟检测 CDT 使用量，超过阈值立即关机  
- 💵 **真实账单校验**：绕过国际版 API 限制，读取当月实时账单金额  
- 🔄 **自动恢复**：次月流量重置后自动开机恢复业务  
- 📊 **多账号支持**：同时监控多个阿里云账号 / 实例  
- 📩 **Telegram 通知**：异常告警 + 每日汇总日报  
- 🚀 **一键部署**：全自动安装，交互式配置，零基础可用  

---
## ⭐ 运行截图

<div align="center">
  <img src="https://github.com/user-attachments/assets/381e346d-604b-47c7-9970-e4e29c87bfb0" width="320" alt="运行截图" />
  <br>
  <p><i>运行效果预览</i></p>
</div>

---

## 🛠️ 前置准备

### 1️⃣ Telegram 通知

- 创建机器人并获取Token：[@BotFather](https://t.me/BotFather)
- 获取 Chat ID：[@userinfobot](https://t.me/userinfobot)

### 2️⃣ 阿里云 RAM 权限（⚠️不要使用主账号）

* 👉 **[创建用户并赋权](https://ram.console.alibabacloud.com/users)**

需要授予以下权限：

- `AliyunECSFullAccess`（开关机）
- `AliyunCDTFullAccess`（查询流量）
- `AliyunBSSReadOnlyAccess`（查询账单）

---

## （一） Alpine Linux（VNC）初始化（首次必做）

> ⚠️ **仅适用于使用 VNC 界面安装的 Alpine Linux 系统**

### 初始化步骤

1. 在本仓库中找到并打开 * 👉 **[`vnc.sh`](https://github.com/10000ge10000/aliyun_monitor/blob/main/vnc.sh)**
2. **复制 `vnc.sh` 中的全部内容**
3. 登录阿里云实例的 **VNC 控制台**
4. 将代码 **完整粘贴到 VNC 界面并回车执行**
5. 等待脚本自动完成初始化

### 脚本会自动完成

- Alpine 基础环境初始化
- Python 运行环境安装
- 常用工具配置
- SSH 登录环境准备

### 🔑 默认登录信息

- **用户名**：`root`
- **初始化后的密码**：`yiwan123`

> ✅ 初始化完成并可正常 SSH 登录后，再继续执行下面的 **一键安装** 步骤。

---

## （二） Alpine 修复 GRUB 引导并重装 Debian 13 指南

> 适用于 **系统无法启动 / GRUB 损坏 / Debian 无法进入** 等场景  
> 通过 **Alpine Linux + chroot** 的方式修复引导并重装 Debian 13

### 使用方法

1. 通过 **VNC 控制台** 或 **ssh 工具** 启动并进入 Alpine Linux  
2. 使用 **root 用户** 登录 Alpine 后，下载并执行脚本：

```bash
wget -O install2.sh https://raw.githubusercontent.com/10000ge10000/aliyun_monitor/main/install2.sh
chmod +x install2.sh
./install2.sh
````

3. 按脚本提示选择磁盘与分区，等待自动完成

### 脚本会自动完成

* 磁盘与分区检测
* 挂载原系统
* GRUB 修复 / 重装
* Debian 13 基础系统恢复

---

## （三）一键保活并监控

使用 **root 用户** 执行：

```bash
wget -N https://raw.githubusercontent.com/10000ge10000/aliyun_monitor/main/install.sh \
&& chmod +x install.sh \
&& ./install.sh
```

脚本会自动：

* 安装 Python 运行环境
* 下载程序源码
* 引导填写 AK / SK / Telegram
* 配置定时任务（cron）

---

## 🗑️ 卸载

```bash
wget -N https://raw.githubusercontent.com/10000ge10000/aliyun_monitor/main/uninstall.sh \
&& chmod +x uninstall.sh \
&& ./uninstall.sh
```

---

## ⚠️ 免责声明

本项目仅供学习与技术交流使用。
作者不对因 **脚本异常、API 变更或配置错误** 导致的任何费用损失负责。
**强烈建议同时在阿里云控制台设置「预算告警」作为最后防线。**

---

## ⭐ Star 支持

如果这个项目帮你避免了一次爆账单，欢迎点个 ⭐
你的支持是继续维护和优化的动力 🙏
