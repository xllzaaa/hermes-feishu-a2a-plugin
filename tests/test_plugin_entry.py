import importlib


class FakeContext:
    def __init__(self):
        self.hooks = []

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))


def test_register_adds_hermes_hooks(monkeypatch):
    monkeypatch.setenv(
        "HERMES_FEISHU_A2A_REGISTRY_JSON",
        '{"agents":[{"name":"1号Hermes","open_id":"ou_one","self":true},{"name":"2号Hermes","open_id":"ou_two"}]}',
    )
    monkeypatch.setenv("HERMES_FEISHU_PATCH_ADAPTER", "false")

    plugin = importlib.import_module("hermes_feishu_a2a")
    plugin._coordinator = None
    ctx = FakeContext()

    plugin.register(ctx)

    assert [name for name, _ in ctx.hooks] == ["pre_llm_call", "post_llm_call"]
