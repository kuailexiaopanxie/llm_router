"""Offline dashboard asset contract tests."""

from importlib.resources import files


def test_dashboard_assets_are_packaged_without_external_dependencies() -> None:
    """All browser modules are package resources and avoid remote endpoints."""

    assets = files("llm_router.dashboard.assets")
    index = assets.joinpath("index.html").read_text()
    app = assets.joinpath("app.js").read_text()
    assert "https://" not in index
    assert "http://" not in app
    assert "localStorage" not in app
    assert assets.joinpath("styles.css").is_file()
