from hermes_feishu_a2a.cli import _read_env_file


def test_read_env_file_supports_export_and_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
# comment
export FEISHU_APP_ID="cli_demo"
HERMES_FEISHU_SELF_AGENT_NAME='产品Agent'
EMPTY=
""".strip(),
        encoding="utf-8",
    )

    env = _read_env_file(env_file)

    assert env["FEISHU_APP_ID"] == "cli_demo"
    assert env["HERMES_FEISHU_SELF_AGENT_NAME"] == "产品Agent"
    assert env["EMPTY"] == ""
