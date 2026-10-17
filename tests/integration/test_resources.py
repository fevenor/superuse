"""Installed source resources resolve without a repository checkout."""

from superuse import app


def test_installed_web_assets_are_selected(tmp_path, monkeypatch):
    package = tmp_path / "installed" / "superuse"
    web = package / "web"
    web.mkdir(parents=True)
    (web / "index.html").write_text("<title>test viewer</title>", encoding="utf-8")
    monkeypatch.delenv("SUPERUSE_WEB_DIR", raising=False)
    monkeypatch.setattr(app, "__file__", str(package / "app.py"))
    assert app.default_web_dir() == web


async def test_persisted_human_hold_is_a_healthy_startup(tmp_path):
    from superuse.config import load_config

    state = tmp_path / "state"
    state.mkdir()
    (state / "control.json").write_text('{"human_latched":true}', encoding="utf-8")
    broker = app.Broker(
        load_config(overrides={"device": {"mode": "simulated"}}), runtime_dir=tmp_path / "runtime"
    )
    try:
        await broker.start()
        await broker.wait_healthy(timeout_s=2)
        assert broker.manager.get_state().mode.value == "human_hold"
    finally:
        await broker.stop()
