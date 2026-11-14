"""Review screen: launcher, offline settings, and the Streamlit app itself."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

import pytest

from workbench import cli
from workbench.pipeline import run_workbench

ROOT = Path(__file__).resolve().parents[1]
OFFLINE = {"--browser.gatherUsageStats=false", "--server.address=localhost", "--server.headless=true"}


@pytest.fixture(scope="module")
def run_out(tmp_path_factory):
    out = tmp_path_factory.mktemp("retail")
    run_workbench(ROOT / "mappings/example_retail.yaml", ROOT / "config.yaml", out, engagement="Review test")
    return out


@pytest.fixture
def out(run_out, tmp_path):
    d = tmp_path / "out"
    shutil.copytree(run_out, d)
    return d


def test_launcher_forces_offline_settings(out):
    cmd, env = cli.review_command(out, port=8765)
    assert cmd[:4] == [sys.executable, "-m", "streamlit", "run"]
    assert Path(cmd[4]).name == "review_app.py"
    assert OFFLINE <= set(cmd)
    assert "--server.port=8765" in cmd
    assert env["WORKBENCH_REVIEW_DIR"] == str(Path(out).resolve())
    assert env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] == "false"


def test_streamlit_config_file_is_offline():
    cfg = tomllib.loads((ROOT / ".streamlit/config.toml").read_text())
    assert cfg["browser"]["gatherUsageStats"] is False
    assert cfg["server"]["address"] == "localhost"
    assert cfg["server"]["headless"] is True


def test_review_command_rejects_folder_without_workpaper(tmp_path, capsys):
    assert cli.main(["review", str(tmp_path)]) == 1
    assert "No workpaper.xlsx" in capsys.readouterr().out


def test_review_command_explains_missing_extra(out, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_streamlit_available", lambda: False)
    assert cli.main(["review", str(out)]) == 1
    assert "pip install .[review]" in capsys.readouterr().out


streamlit = pytest.importorskip("streamlit")


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network call attempted")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


def _app(out, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("WORKBENCH_REVIEW_DIR", str(out))
    at = AppTest.from_file(str(ROOT / "workbench/review_app.py"), default_timeout=60)
    return at.run()


def test_app_renders_without_network_and_proposes_nothing(out, monkeypatch, no_network):
    at = _app(out, monkeypatch)
    assert not at.exception, at.exception
    disp = [s for s in at.selectbox if s.label == "Disposition"]
    assert len(disp) == 1
    assert disp[0].value is None, "the screen must not pre-select a disposition"
    assert list(disp[0].options) == ["Error", "Irregularity — inquire", "Needs client query", "False positive",
                                     "Accepted"]
    assert not (out / "workpaper_reviewed.xlsx").exists(), "opening the screen must not write anything"


def test_app_saves_reviewer_choice_to_copy(out, monkeypatch, no_network):
    at = _app(out, monkeypatch)
    at.sidebar.text_input(key="reviewer").set_value("A. Reviewer")
    at.selectbox(key="run").set_value("ITGC-02")
    at.run()
    at.checkbox(key="apply_all").check()
    at.selectbox(key="disposition").set_value("False positive")
    at.text_area(key="note").set_value("Delegation on file")
    at.run()
    at.button(key="save").click()
    at.run()
    assert not at.exception, at.exception
    from workbench import review
    df = review.load_workpaper(out).runs["ITGC-02"]
    assert df["Disposition"].tolist() == ["False positive"]
    assert df["Reviewed by"].tolist() == ["A. Reviewer"]
    assert any("Saved" in s.value for s in at.success)


def test_save_is_blocked_without_reviewer_name(out, monkeypatch, no_network):
    at = _app(out, monkeypatch)
    at.selectbox(key="run").set_value("ITGC-02")
    at.run()
    at.checkbox(key="apply_all").check()
    at.selectbox(key="disposition").set_value("Error")
    at.run()
    assert at.button(key="save").disabled
    assert not (out / "workpaper_reviewed.xlsx").exists()


AUDIT_HOOK = """
import sys, os
_log = os.environ["WB_SOCKET_LOG"]
def _hook(event, args):
    if event == "socket.connect":
        addr = args[1]
        host = addr[0] if isinstance(addr, tuple) else str(addr)
        if host not in ("127.0.0.1", "::1", "localhost") and not str(host).startswith("/"):
            with open(_log, "a") as f:
                f.write(f"{host}\\n")
    elif event == "socket.getaddrinfo":
        host = args[0]
        if host not in (None, "127.0.0.1", "::1", "localhost", "0.0.0.0", ""):
            with open(_log, "a") as f:
                f.write(f"dns:{host}\\n")
sys.addaudithook(_hook)
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


import contextlib


@contextlib.contextmanager
def review_server(out, tmp_path):
    """Start the real review server with a socket audit hook; yield (url, log_path)."""
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(AUDIT_HOOK)
    log = tmp_path / "sockets.log"
    log.write_text("")
    port = _free_port()
    cmd, env = cli.review_command(out, port=port)
    env = env | {"PYTHONPATH": os.pathsep.join([str(hook_dir), str(ROOT)]), "WB_SOCKET_LOG": str(log)}
    for proxy in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        env.pop(proxy, None)
    proc = subprocess.Popen(cmd, env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    url = f"http://localhost:{port}"
    try:
        deadline, healthy = time.time() + 60, False
        while time.time() < deadline and not healthy:
            try:
                healthy = opener.open(f"{url}/_stcore/health", timeout=2).read() == b"ok"
            except OSError:
                time.sleep(0.5)
        assert healthy, "review server did not start"
        yield url, log, opener
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_real_server_makes_no_external_connections(out, tmp_path):
    with review_server(out, tmp_path) as (url, log, opener):
        page = opener.open(f"{url}/", timeout=5).read()
        assert b"<html" in page.lower()
        time.sleep(2)
    assert log.read_text() == "", f"external network activity: {log.read_text()}"


def test_browser_page_requests_only_localhost(out, tmp_path):
    """Streamlit's usage statistics are sent by the browser page, so check the browser too."""
    playwright = pytest.importorskip("playwright.sync_api")
    from urllib.parse import urlparse
    with review_server(out, tmp_path) as (url, log, _):
        with playwright.sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # no browser installed on this machine
                pytest.skip(f"no headless browser: {exc}")
            page = browser.new_page()
            hosts = []
            page.on("request", lambda r: hosts.append(urlparse(r.url).hostname))
            page.goto(url, wait_until="networkidle")
            page.get_by_text("Exception review").first.wait_for(timeout=30000)
            page.wait_for_timeout(3000)
            browser.close()
    assert log.read_text() == ""
    external = sorted({h for h in hosts if h not in ("localhost", "127.0.0.1", None)})
    assert hosts, "browser made no requests at all"
    assert external == [], f"browser contacted: {external}"
