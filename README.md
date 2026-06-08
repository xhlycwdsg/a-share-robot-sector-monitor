# A股机器人板块监控助手

一个用 Python + AkShare 搭建的 A 股题材监控工具。它会按配置文件监控股票池、机器人板块、CPO、半导体和上证指数，并在触发规则时通过 Bark 或 Server酱推送到手机。

> 本项目只做提醒，不做自动交易。  
> 本项目仅用于个人研究、复盘和经验交流，不构成投资建议。

## 它适合做什么

- 盘中每隔几分钟检查一次行情
- 监控机器人方向是否强于大盘或其他科技线
- 观察个股是否强于板块、弱于板块、异常拉升、冲高回落、深水拉起
- 用手机通知提醒自己“看一眼”，减少手动盯盘
- 收盘后生成 Markdown 复盘摘要

它不适合做秒级盯盘，也不建议用于自动下单。免费行情接口可能延迟、限频或临时不可用。

## 功能

- 使用 AkShare 获取 A 股实时行情和东方财富板块数据
- 股票池、板块、规则阈值全部可配置
- 支持 Bark 推送到 iPhone
- 支持 Server酱推送到微信，适合 Android/Windows 用户
- 同一条规则每天只提醒一次，避免刷屏
- 支持 dry-run 测试，不实际推送
- 支持生成每日复盘文件

## 项目结构

```text
.
├── main.py              # 主程序
├── config.example.yaml  # 示例配置，可提交到 GitHub
├── requirements.txt     # Python 依赖
├── README.md            # 项目说明
└── .gitignore           # 忽略个人配置和运行输出
```

本地使用时需要复制一份个人配置：

```bash
cp config.example.yaml config.yaml
```

`config.yaml` 里会保存你的推送 key，已经被 `.gitignore` 忽略，不要提交到 GitHub。

## 快速开始

建议使用 Python 3.10 或以上版本。

macOS / Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python3 main.py --once --dry-run
```

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.yaml config.yaml
python main.py --once --dry-run
```

如果 PowerShell 不允许激活虚拟环境，可以先执行：

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

## 手机推送

### iPhone: Bark

在 iPhone 安装 Bark，复制 Bark 给你的推送地址，例如：

```text
https://api.day.app/你的BarkKey/这里改成你自己的推送内容
```

把中间那段 key 填到 `config.yaml`：

```yaml
push:
  provider: bark
  bark:
    key: "你的BarkKey"
    server: "https://api.day.app"
```

测试推送：

```bash
python3 main.py --test-push
```

### Android / OPPO / Windows: Server酱

Android 用户可以用 Server酱推送到微信。

Server酱地址：

```text
https://sct.ftqq.com/
```

在 `config.yaml` 里把推送通道改成：

```yaml
push:
  provider: server_chan
  server_chan:
    key: "你的Server酱SendKey"
```

测试推送：

```bash
python3 main.py --test-push
```

## 运行方式

只检查一次，并且不真正推送：

```bash
python3 main.py --once --dry-run
```

测试手机推送：

```bash
python3 main.py --test-push
```

交易时段循环运行：

```bash
python3 main.py
```

暂停程序：

```text
Ctrl + C
```

生成收盘复盘：

```bash
python3 main.py --review
```

复盘会保存到：

```text
reviews/YYYY-MM-DD.md
```

## 当前股票池

机器人方向：

- 方正电机 `002196`
- 万向钱潮 `000559`
- 绿的谐波 `688017`
- 三花智控 `002050`
- 拓普集团 `601689`
- 中大力德 `002896`
- 鸣志电器 `603728`
- 模塑科技 `000700`
- 巨轮智能 `002031`
- 柯力传感 `603662`

CPO / 半导体观察：

- 中际旭创 `300308`
- 新易盛 `300502`
- 工业富联 `601138`

你可以在 `config.yaml` 的 `stocks` 里自由增删。

## 当前提醒规则

持仓和核心票：

- 方正电机强于机器人板块
- 万向钱潮强于机器人板块
- 持仓弱于机器人板块
- 方正电机、万向钱潮、绿的谐波先锋 + 中军共振

板块强弱：

- 机器人板块强于上证指数
- 机器人强于 CPO / 半导体
- CPO / 半导体下跌但机器人上涨
- 机器人股票池广度转强

日内结构：

- 冲高回落
- 异常拉升
- 深水拉起
- 方正电机成交额放大
- 股票池个股放量上涨

情绪和风险：

- 绿的谐波接近涨停或深跌
- 模塑科技等高标负反馈
- 核心锚负反馈
- 利润保护提醒
- 尾盘仍保持强势

每条提醒会尽量说明：

```text
原因：为什么这件事值得注意
数据：具体触发数据
看点：接下来应该观察什么
```

## 修改规则

所有阈值都在 `config.yaml` 的 `rules` 下方。

例如，把检查间隔改成 5 分钟：

```yaml
app:
  poll_interval_seconds: 300
```

如果免费行情接口偶发断开，可以调高重试次数或重试等待：

```yaml
app:
  fetch_retries: 3
  fetch_retry_delay_seconds: 5
```

把方正电机相对机器人板块强 3% 才提醒：

```yaml
founder_vs_robot:
  outperform_pct: 3
```

增加一只股票：

```yaml
stocks:
  - code: "000000"
    name: 股票名称
    tags: [robot]
```

不建议把检查间隔改成秒级。免费接口可能出现超时、限频、返回空数据等问题。通常 3 到 5 分钟更稳。

## 常见问题

### 为什么显示“非交易时间，等待下一轮”？

说明程序已经正常启动，只是当前不在 A 股交易时间。它会按配置间隔继续等待。

### 为什么提示行情接口访问失败？

常见原因包括网络代理、DNS、东方财富接口临时不可用、AkShare 接口变动。可以稍后再试，或降低运行频率。

### 可以自动交易吗？

不建议。本项目定位是提醒和复盘，不负责下单。

### 可以上传自己的配置吗？

不要上传 `config.yaml`。里面可能有 Bark key、Server酱 SendKey 等私人信息。公开仓库只提交 `config.example.yaml`。

## 风险说明

- AkShare 和东方财富免费接口可能有延迟、限频或字段变化
- Tushare 的部分数据需要积分或权限，本项目未默认接入
- 手机推送 token/key 请自己保管，不要提交到 GitHub
- 本工具只负责提醒你看一眼，最终判断和执行仍由你决定

## 交流方向

欢迎围绕这些方向交流：

- 更稳定的数据源
- 更合理的题材强弱规则
- 更少噪音的提醒阈值
- Android / 企业微信 / Telegram 推送
- 收盘复盘模板
- Docker、云服务器或 NAS 部署
