---
name: a2a-identity-diagnostics
description: |
  A2A 身份和 open_id 诊断指南。**仅在用户询问 bot 身份、ouid/open_id、@ 不生效、某个 Agent 不回复时使用。**

  适用场景：
  (1) 用户问“你的 ouid/open_id 是什么”
  (2) 用户说“@ 变蓝但对方不回复”
  (3) 用户怀疑 registry、profile、飞书 app 凭据配错
  (4) 需要解释为什么不能相信 LLM 自报 open_id
---

# A2A 身份和 open_id 诊断

## 原则

- 不要猜测或编造 `open_id` / `ouid`。
- LLM 回复里的 “我的 ouid 是 ...” 不能作为配置依据。
- 可信来源只有飞书开放平台 API、Hermes profile 环境变量、插件 registry 文件、插件日志。

## 推荐诊断命令

在插件仓库目录运行：

```bash
./hermes-feishu-a2a doctor --profile product
./hermes-feishu-a2a doctor --profiles product developer tester reviewer
```

如果已经通过 `uv tool install .` 安装 CLI：

```bash
hermes-feishu-a2a doctor --profiles product developer tester reviewer
```

检查重点：

- `status` 是否为 `ok`
- `self_agent.open_id` 是否等于 `feishu_identity.bot_open_id`
- `feishu_identity.match` 是否为 `true`
- 每个 profile 的 `HERMES_FEISHU_SELF_ACCOUNT_ID` 是否不同

## 常见判断

| 现象 | 更可能的原因 |
|------|--------------|
| `@` 没变蓝 | registry 的目标 `open_id` 不对，或出站消息没经过插件 |
| `@` 变蓝但对方不回复 | 目标 bot 没收到 bot-to-bot at 事件、权限缺失、gateway 未运行、消息作为回复投递被飞书吞掉 |
| 人工 `@` 能回复，机器人 `@` 不回复 | 飞书应用缺 `im:message.group_at_msg.include_bot:readonly`，或 bot-to-bot mention 投递路径受限 |
| 多个 bot 都说出不同 ouid | 它们在根据上下文猜，不是权威来源 |

## 回复用户时

- 先说明“不能信机器人自报 open_id”。
- 引导使用 `doctor` 查看飞书 API 返回值。
- 不要在聊天里暴露 `FEISHU_APP_SECRET`。
