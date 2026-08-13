"""config.load_env_local: .env.local parsing + precedence."""
import os

from echoecho_app import config


def test_env_local_loads_and_env_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env.local"
    env.write_text(
        "# comment\n"
        "\n"
        "OPENAI_API_KEY='sk-from-file'\n"
        "ECHOECHO_REALTIME_MODEL=gpt-realtime-2.1\n"
        "EMPTY_VALUE=\n"
        "not a kv line\n")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ECHOECHO_REALTIME_MODEL", "from-real-env")
    monkeypatch.delenv("EMPTY_VALUE", raising=False)

    config.load_env_local(env)

    assert os.environ["OPENAI_API_KEY"] == "sk-from-file"  # quotes stripped
    assert os.environ["ECHOECHO_REALTIME_MODEL"] == "from-real-env"  # env wins
    assert "EMPTY_VALUE" not in os.environ  # blank values are skipped


def test_env_local_missing_file_is_noop(tmp_path):
    config.load_env_local(tmp_path / "nope.env")  # must not raise
