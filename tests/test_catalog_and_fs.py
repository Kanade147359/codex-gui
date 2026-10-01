import json
import time

import pytest
from fastapi.testclient import TestClient

from app.catalog import ModelCatalog, config_value, parse_catalog
from app.fs_browser import BrowseError, list_dir
from app.main import create_app

from conftest import FakeRunner

CATALOG = {"models": [
    {"slug": "b", "display_name": "B", "visibility": "list", "priority": 2, "default_reasoning_level": "medium",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}]},
    {"slug": "hidden", "visibility": "hide", "priority": 0},
    {"slug": "a", "display_name": "A", "visibility": "list", "priority": 1, "default_reasoning_level": "low",
     "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "xhigh"}]},
]}


def test_parse_catalog_filters_hidden_and_sorts():
    models = parse_catalog(CATALOG)
    assert [m["slug"] for m in models] == ["a", "b"]
    assert models[0]["efforts"] == ["low", "high", "xhigh"] and models[0]["default_effort"] == "low"
    assert parse_catalog({}) == []


def test_config_value(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('model = "gpt-x"\nmodel_reasoning_effort = "high"\n[projects."/a"]\nmodel = "other"\n')
    assert config_value("model", cfg) == "gpt-x"
    assert config_value("model_reasoning_effort", cfg) == "high"
    cfg.write_text('[tables]\nmodel = "inside-table"\n')
    assert config_value("model", cfg) is None  # only top-level keys count
    assert config_value("model", tmp_path / "missing.toml") is None


def test_catalog_reports_error_when_codex_missing():
    result = __import__("asyncio").run(ModelCatalog("/definitely/not/codex").get())
    assert result["models"] == [] and "could not read model list" in result["error"]


def test_list_dir(tmp_path):
    (tmp_path / "plain").mkdir()
    (tmp_path / "Repo").mkdir()
    (tmp_path / "Repo" / ".git").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    d = list_dir(str(tmp_path))
    assert [(e["name"], e["is_git"]) for e in d["entries"]] == [("plain", False), ("Repo", True)]
    assert d["parent"] == str(tmp_path.parent.resolve()) and d["is_git"] is False
    assert ".hidden" in [e["name"] for e in list_dir(str(tmp_path), show_hidden=True)["entries"]]
    assert list_dir(str(tmp_path / "Repo"))["is_git"] is True
    assert list_dir("/")["parent"] is None
    for bad in (str(tmp_path / "nope"), str(tmp_path / "file.txt")):
        with pytest.raises(BrowseError):
            list_dir(bad)


@pytest.fixture
def client(settings):
    app = create_app(settings, FakeRunner())
    app.state.catalog._cached = {"models": parse_catalog(CATALOG), "default_model": "a", "default_effort": "low", "error": ""}
    app.state.catalog._fetched_at = time.monotonic()
    with TestClient(app) as c:
        yield c


def test_options_and_fs_endpoints(client, git_repo):
    opts = client.get("/api/options").json()
    assert [m["slug"] for m in opts["models"]] == ["a", "b"] and opts["default_model"] == "a" and opts["repos"] == []
    client.post("/api/tasks", json={"repository": str(git_repo), "prompt": "ok"})
    assert client.get("/api/options").json()["repos"] == [str(git_repo.resolve())]

    d = client.get("/api/fs", params={"path": str(git_repo.parent)}).json()
    assert ("repo", True) in [(e["name"], e["is_git"]) for e in d["entries"]]
    assert client.get("/api/fs", params={"path": str(git_repo / "missing")}).status_code == 400


def test_refs_endpoint(client, git_repo, tmp_path):
    r = client.get("/api/refs", params={"repository": str(git_repo)}).json()
    assert r["default"] == "main" and [b["name"] for b in r["branches"]] == ["main"]
    assert client.get("/api/refs", params={"repository": str(tmp_path)}).status_code == 400
