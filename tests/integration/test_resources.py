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
