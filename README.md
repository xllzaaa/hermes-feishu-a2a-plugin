# Hermes Feishu A2A Plugin

让多个 Hermes 实例在同一个飞书群里用原生 `@` 互相协作。

用户只需要在群里 `@` 一个 Hermes；该 Hermes 可以在回复中 `@` 另一个 Hermes。只要飞书应用权限和 registry 配置正确，被 `@` 的 Hermes 会像被真人 `@` 一样收到消息并直接介入，不需要人工逐个 `@` 每个机器人。

## 能做什么

- 把 `@开发Agent`、`@2号Hermes` 这类文本自动转换成飞书原生 mention。
- 放行“机器人 `@` 机器人”的飞书群聊事件，让目标 Hermes 自动处理。
- 过滤无关机器人消息，避免每个 Hermes 都被群里所有机器人输出打扰。
- 给接收方注入发起方信息，让它知道任务来自哪个 Hermes，并能 `@` 回去。
- 支持单机多 profile，也支持多台主机分别运行不同 Hermes 实例。
- 支持静态 registry，也支持用多个飞书 app 凭据自动发现 bot `open_id`。

## 前置条件

每个参与协作的飞书机器人应用都需要：

- 已加入同一个飞书群。
- Hermes gateway 能正常连接飞书。
- 开启飞书权限 `im:message.group_at_msg.include_bot:readonly`。
- 如果要自动读取群成员或动态修正 mention，还需要对应的群成员读取权限。

建议关闭 Hermes 流式输出。流式输出会先发送半截消息再编辑补全，飞书的 mention 投递会更不稳定。

```yaml
display:
  streaming: false

streaming:
  enabled: false
```

## 快速开始

克隆仓库后进入项目目录：

```bash
cd hermes-feishu-a2a-plugin
```

准备 registry：

```bash
cp agents.example.json agents.local.json
```

把 `agents.local.json` 改成真实机器人信息：

```json
{
  "agents": [
    {
      "name": "产品Agent",
      "open_id": "ou_xxx_product",
      "account_id": "product",
      "description": "需求理解、任务拆解、结果汇总",
      "aliases": ["产品", "1号"]
    },
    {
      "name": "开发Agent",
      "open_id": "ou_xxx_developer",
      "account_id": "developer",
      "description": "代码实现",
      "aliases": ["开发", "2号"]
    }
  ]
}
```

给当前 Hermes home 安装插件和 gateway hook：

```bash
HERMES_HOME="$HOME/.hermes/profiles/product" ./install.sh
```

给多个本机 profile 一次性安装：

```bash
PROFILES="product developer tester reviewer" ./install.sh
```

每个 Hermes 实例都要配置自己的身份。示例：

```bash
export FEISHU_APP_ID=cli_xxx_product
export FEISHU_APP_SECRET=xxx
export FEISHU_BOT_NAME=产品Agent
export HERMES_FEISHU_SELF_ACCOUNT_ID=product
export HERMES_FEISHU_A2A_REGISTRY_FILE=/absolute/path/to/agents.local.json
export HERMES_FEISHU_PATCH_ADAPTER=true
```

诊断配置：

```bash
./hermes-feishu-a2a doctor
```

启动 Hermes gateway：

```bash
hermes -p product gateway run
```

启动日志应包含：

```text
[hermes-feishu-a2a-gateway-patch] installed=True agents=2 self=产品Agent
```

## 多主机部署

多台主机部署时，每台机器都 clone 同一份代码并执行 `./install.sh`。所有机器可以使用同一份 registry 内容，但每台机器的当前身份必须不同。

主机 A：

```bash
export HERMES_FEISHU_SELF_ACCOUNT_ID=product
export HERMES_FEISHU_A2A_REGISTRY_FILE=/opt/hermes/agents.local.json
hermes gateway run
```

主机 B：

```bash
export HERMES_FEISHU_SELF_ACCOUNT_ID=developer
export HERMES_FEISHU_A2A_REGISTRY_FILE=/opt/hermes/agents.local.json
hermes gateway run
```

关键点：

- `account_id` 是你自己定义的稳定 ID，用来告诉插件“当前进程是哪一个 Agent”。
- `open_id` 必须是飞书机器人真实 open_id。
- 每个 Hermes 使用自己的 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`。

## 自动发现 open_id

如果还不知道 bot `open_id`，可以复制并编辑账号配置：

```bash
cp accounts.example.json accounts.local.json
export HERMES_FEISHU_ACCOUNTS_FILE=/absolute/path/to/accounts.local.json
export HERMES_FEISHU_SELF_ACCOUNT_ID=product
./hermes-feishu-a2a discover
```

发现结果会缓存到：

```text
$HERMES_HOME/feishu-a2a/registry.json
```

生产环境更推荐使用显式的 `agents.local.json`，便于审查和分发。

## 验证联动

在飞书群里人工 `@产品Agent`：

```text
任务ID：A2A-001
请 @开发Agent 回复“收到”，并说明你是谁。
```

成功标准：

1. 产品 Agent 发出的 `@开发Agent` 是蓝色飞书 mention。
2. 开发 Agent 的 gateway 日志能看到收到消息并开始处理。
3. 不需要人工再 `@开发Agent`。
4. 开发 Agent 可以 `@产品Agent` 回传结果。

如果 mention 没变蓝，通常是 registry 的 `open_id` 不对，或者出站消息没有经过插件 hook。

## 常用命令

```bash
./install.sh --help
./hermes-feishu-a2a doctor
./hermes-feishu-a2a discover
uv run --python 3.11 --with pytest python -m pytest
```

如果希望在任意目录运行命令：

```bash
uv tool install .
hermes-feishu-a2a doctor
```

## 配置项

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HERMES_FEISHU_A2A_ENABLED` | `true` | 是否启用插件 |
| `HERMES_FEISHU_PATCH_ADAPTER` | `true` | 是否 patch Hermes Feishu adapter |
| `HERMES_FEISHU_A2A_REGISTRY_FILE` | 空 | 静态 Agent registry 文件 |
| `HERMES_FEISHU_A2A_REGISTRY_JSON` | 空 | 静态 Agent registry JSON |
| `HERMES_FEISHU_ACCOUNTS_FILE` | 空 | 自动发现用 Feishu app 凭据 |
| `HERMES_FEISHU_SELF_ACCOUNT_ID` | 空 | 当前 Hermes 对应的 `account_id` |
| `HERMES_FEISHU_SELF_AGENT_NAME` | `FEISHU_BOT_NAME` | 当前 Agent 名称 |
| `HERMES_FEISHU_SELF_OPEN_ID` | `FEISHU_BOT_OPEN_ID` | 当前 Agent open_id |
| `HERMES_FEISHU_MAX_TASK_EVENTS` | `8` | 同一 task_id 的循环保护阈值 |
| `FEISHU_DOMAIN` | `feishu` | 国内飞书用 `feishu`，海外 Lark 用 `lark` |

## 故障排查

`doctor` 报 `No agents loaded`：

确认设置了 `HERMES_FEISHU_A2A_REGISTRY_FILE`，并且文件路径是绝对路径。

`self=null`：

确认当前实例的 `HERMES_FEISHU_SELF_ACCOUNT_ID` 能匹配 registry 中某个 agent 的 `account_id`。

`@` 没变蓝：

确认目标 agent 的 `open_id` 正确，目标 bot 已在群里，启动日志里看到的是 `hermes-feishu-a2a-gateway-patch`。

目标 bot 不回复：

确认目标飞书应用开启 `im:message.group_at_msg.include_bot:readonly`，目标 gateway 正在运行，且 Hermes 已关闭流式输出。

消息偶尔截断：

优先关闭流式输出。A2A 场景下完整消息一次性发送更稳定。

## 开发

```bash
uv run --python 3.11 --with pytest python -m pytest
uv run --python 3.11 python -m py_compile hermes_feishu_a2a/*.py
```

本项目使用 MIT License。请勿提交真实的 `agents.local.json`、`accounts.local.json`、`.env` 或飞书密钥。
