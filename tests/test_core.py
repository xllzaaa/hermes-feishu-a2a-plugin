import json
from types import SimpleNamespace

from hermes_feishu_a2a.adapter_patch import inject_sender_info_into_message
from hermes_feishu_a2a.core import (
    AgentRegistry,
    AgentSpec,
    FeishuA2ACoordinator,
    extract_mentions_from_content,
)


def registry():
    return AgentRegistry(
        [
            AgentSpec(name="1号Hermes", open_id="ou_one", aliases=("1号",), self_agent=True),
            AgentSpec(name="2号Hermes", open_id="ou_two", aliases=("2号", "开发Hermes"), description="开发实现"),
            AgentSpec(name="3号Hermes", open_id="ou_three", aliases=("3号",), description="测试验收"),
        ]
    )


def test_plain_name_mentions_are_converted_to_native_feishu_tags():
    out = registry().replace_text_mentions("@2号 请处理 task_id: abc-1234，然后 @3号 验收。")

    assert '<at user_id="ou_two">2号Hermes</at> (2号Hermes)' in out
    assert '<at user_id="ou_three">3号Hermes</at> (3号Hermes)' in out


def test_pre_llm_call_injects_roster_and_rules():
    coordinator = FeishuA2ACoordinator(registry())

    result = coordinator.pre_llm_call(platform="feishu", chat_id="oc_demo")

    assert result
    assert '<at user_id="ou_two">2号Hermes</at>' in result["context"]
    assert "Mention at most one agent per message" in result["context"]


def test_non_feishu_platform_is_ignored():
    coordinator = FeishuA2ACoordinator(registry())

    assert coordinator.pre_llm_call(platform="cli") is None
    assert coordinator.post_llm_call(platform="cli", assistant_response="@2号 看看") is None


def test_known_bot_is_accepted_only_when_it_mentions_self():
    coordinator = FeishuA2ACoordinator(registry())

    assert coordinator.should_accept_bot_sender(sender_open_id="ou_two", mentioned_ids={"ou_one"}, chat_id="oc_demo")
    assert "oc_demo" in coordinator.native_a2a_chats
    assert not coordinator.should_accept_bot_sender(sender_open_id="ou_two", mentioned_ids={"ou_three"}, chat_id="oc_demo")
    assert not coordinator.should_accept_bot_sender(sender_open_id="ou_unknown", mentioned_ids={"ou_one"}, chat_id="oc_demo")


def test_inbound_text_gets_sender_return_instruction():
    coordinator = FeishuA2ACoordinator(registry())

    result = coordinator.process_inbound_text(text="处理完成", sender_open_id="ou_two", chat_id="oc_demo")

    assert result["action"] == "rewrite"
    assert "来自 Hermes 机器人「2号Hermes」" in result["text"]
    assert '<at user_id="ou_two">2号Hermes</at>' in result["text"]


def test_loop_guard_skips_repeated_task_id():
    coordinator = FeishuA2ACoordinator(registry(), max_task_events=1)

    first = coordinator.process_inbound_text(text="task_id: abc-1234 第一次", sender_open_id="ou_two", chat_id="oc_demo")
    second = coordinator.process_inbound_text(text="task_id: abc-1234 第二次", sender_open_id="ou_two", chat_id="oc_demo")

    assert first["action"] == "rewrite"
    assert second == {"action": "skip", "reason": "feishu_a2a_loop_guard"}


def test_extract_mentions_from_text_and_rich_payload():
    raw = json.dumps(
        {
            "text": '<at user_id="ou_one">1号Hermes</at>',
            "content": [[{"tag": "at", "user_id": "ou_two", "user_name": "2号Hermes"}]],
        },
        ensure_ascii=False,
    )

    assert extract_mentions_from_content(raw) == {"ou_one", "ou_two"}


def test_adapter_injection_mutates_text_message_json():
    coordinator = FeishuA2ACoordinator(registry())
    message = SimpleNamespace(message_type="text", content=json.dumps({"text": "结果如下"}, ensure_ascii=False))

    inject_sender_info_into_message(coordinator, message, "ou_two", "oc_demo")

    payload = json.loads(message.content)
    assert "来自 Hermes 机器人「2号Hermes」" in payload["text"]
