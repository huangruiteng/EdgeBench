from types import SimpleNamespace
from unittest.mock import Mock

import sforge.cli as cli


def test_judge_server_disables_secret_bearing_access_log(monkeypatch):
    run = Mock()
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", SimpleNamespace(run=run))
    monkeypatch.setattr(cli, "_make_config", lambda args: object())
    monkeypatch.setattr(
        "sforge.harness.judge_server.create_app",
        lambda config, checkpoints: "judge-app",
    )
    args = SimpleNamespace(
        host="127.0.0.1", port=12345, checkpoints=False,
    )

    cli.cmd_serve(args)

    run.assert_called_once_with(
        "judge-app", host="127.0.0.1", port=12345, access_log=False,
    )
