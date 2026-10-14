"""The share tooling: link extraction, banner, flags, the script's safety, the action log, loopback rule."""
import importlib.util
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import actionlog
from app.config import Settings
from app.fake_bolna import FakeBolnaClient
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = ROOT / "scripts" / "share.sh"

spec = importlib.util.spec_from_file_location("share_helpers", ROOT / "scripts" / "share_helpers.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)

CLOUDFLARED_OUTPUT = """\
2026-10-12T10:00:00Z INF Thank you for trying Cloudflare Tunnel. Doing so, without a Cloudflare account, is a quick way to experiment and try it out.
2026-10-12T10:00:01Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-10-12T10:00:03Z INF +--------------------------------------------------------------------------------------------+
2026-10-12T10:00:03Z INF |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
2026-10-12T10:00:03Z INF |  https://quiet-river-demo-1234.trycloudflare.com                                           |
2026-10-12T10:00:03Z INF +--------------------------------------------------------------------------------------------+
"""


# 1 ------------------------------------------------------------------ link extraction
def test_the_tunnel_link_is_found_in_cloudflared_output():
    assert helpers.extract_tunnel_url(CLOUDFLARED_OUTPUT) == "https://quiet-river-demo-1234.trycloudflare.com"


@pytest.mark.parametrize("text", [
    "", None, "INF Requesting new quick Tunnel on trycloudflare.com...",
    "ERR Post \"https://api.trycloudflare.com/tunnel\": dial tcp: lookup api.trycloudflare.com: no such host",
    "visit https://evil.example.com/trycloudflare.com now", "http://plain-http.trycloudflare.com",
    "https://lookalike.trycloudflare.com.evil.org/path",
])
def test_no_link_is_returned_when_the_output_has_none(text):
    assert helpers.extract_tunnel_url(text) is None


def test_an_error_line_does_not_hide_the_real_link():
    out = 'ERR Post "https://api.trycloudflare.com/tunnel": timeout\n' + CLOUDFLARED_OUTPUT
    assert helpers.extract_tunnel_url(out) == "https://quiet-river-demo-1234.trycloudflare.com"


# 5 ------------------------------------------------------------------ banner
@pytest.mark.parametrize("password", [False, True])
@pytest.mark.parametrize("mode", ["real", "fake"])
@pytest.mark.parametrize("delete", [False, True])
def test_the_banner_says_plainly_what_the_link_exposes(password, mode, delete):
    text = helpers.build_banner(password, mode, delete)
    assert ("A password is required" in text) == password
    assert ("There is NO login (APP_PASSWORD is empty)" in text) == (not password)
    assert "This link is PUBLIC." in text
    assert ("Bolna mode: LIVE (calls are real)" in text) == (mode == "real")
    assert ("Bolna mode: FAKE (no calls)" in text) == (mode == "fake")
    assert ("Bolna deletion: ON (releasing numbers is permanent)" in text) == delete
    assert ("Bolna deletion: OFF" in text) == (not delete)
    assert "Stop sharing with Ctrl+C. The link stops working immediately and the next one will be different." in text


def test_the_flags_come_from_the_live_backend_then_the_environment_then_dotenv():
    env_text = "BOLNA_API_KEY=super-secret-key\nBOLNA_MODE=real\nALLOW_BOLNA_NUMBER_DELETE=true\nAPP_PASSWORD=\n"
    from_file = helpers.resolve_flags({}, env_text)
    assert from_file == {"password_set": False, "mode": "real", "delete_on": True}
    assert helpers.resolve_flags({"BOLNA_MODE": "fake", "ALLOW_BOLNA_NUMBER_DELETE": "false"}, env_text) == \
        {"password_set": False, "mode": "fake", "delete_on": False}  # the process environment wins
    live = helpers.resolve_flags({}, env_text, {"required": True, "mode": "fake"}, False)
    assert live == {"password_set": True, "mode": "fake", "delete_on": False}  # the running backend wins
    assert helpers.resolve_flags({}, "") == {"password_set": False, "mode": "fake", "delete_on": False}  # defaults


def test_dotenv_is_read_by_exact_name_and_nothing_else_comes_out():
    text = ("# BOLNA_MODE=real\nBOLNA_API_KEY=super-secret-key\nBOLNA_MODE_EXTRA=real\n  BOLNA_MODE = \"fake\" \n"
            "APP_PASSWORD=hunter2\nOTHER=1\n")
    found = helpers.lookup_env_names(text, ("BOLNA_MODE", "APP_PASSWORD", "ALLOW_BOLNA_NUMBER_DELETE"))
    assert found == {"BOLNA_MODE": "fake", "APP_PASSWORD": "hunter2"}
    assert "super-secret-key" not in str(helpers.resolve_flags({}, text))
    assert "hunter2" not in str(helpers.resolve_flags({}, text))  # only "a password is set" comes out
    assert "hunter2" not in helpers.build_banner(True, "fake", False)


# 4 ------------------------------------------------------------------ the script
def test_the_script_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_script_never_prints_dotenv_as_a_whole():
    lines = [line.split(" #", 1)[0].rstrip() for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#")]
    touching = [l.strip() for l in lines if ".env" in l.replace(".env.example", "") or "ENV_FILE" in l]
    # the only things that may mention the file: its path, and handing the path to the helper
    # (which looks up three variable names and prints derived yes/no values only)
    assert touching == ['ENV_FILE="$ROOT/backend/.env"',
                        'eval "$("$PY" "$HELPERS" flags "$BACKEND_URL" "$ENV_FILE" | sed \'s/^/FLAG_/\')"'], touching
    code = "\n".join(lines)
    for forbidden in ("printenv", "env |", "set -x", "xtrace", "BOLNA_API_KEY", "APP_PASSWORD="):
        assert forbidden not in code, forbidden


def test_the_script_cleans_up_and_never_writes_the_link_to_a_file():
    text = SCRIPT.read_text()
    assert "trap cleanup EXIT" in text and "Sharing stopped. The old link no longer works." in text
    assert "caffeinate -dims" in text and "--no-autoupdate" in text
    assert '"$LINK" >' not in text and '$LINK" >>' not in text and "> \"$SHARE_DIR/link" not in text
    gitignore = (ROOT / ".gitignore").read_text().splitlines()
    assert ".share/" in gitignore


# 6 ------------------------------------------------------------------ action log
def client_with_log(tmp_path):
    log_file = tmp_path / ".share" / "actions.log"
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", run_migrations=False, start_poller=False,
                        action_log_path=str(log_file))
    return TestClient(create_app(settings, FakeBolnaClient())), log_file


def test_a_changing_request_is_logged_with_path_status_and_the_tunnel_address(tmp_path):
    c, log_file = client_with_log(tmp_path)
    with c:
        r = c.put("/api/settings/threshold?secret=hunter2&key=abc", json={"threshold": 7, "note": "body-secret"},
                  headers={"CF-Connecting-IP": "203.0.113.9"})
        assert r.status_code == 200 and r.json() == {"threshold": 7}  # the response is untouched
        c.get("/api/settings")  # reads are not recorded
    lines = log_file.read_text().splitlines()
    assert len(lines) == 1
    stamp, method, path, status, ip = lines[0].split(" ")
    assert method == "PUT" and path == "/api/settings/threshold" and status == "200" and ip == "ip=203.0.113.9"
    assert stamp.endswith("Z") and "T" in stamp
    text = log_file.read_text()
    for never in ("hunter2", "secret", "abc", "body-secret", "?"):
        assert never not in text


def test_without_the_tunnel_header_the_direct_address_is_used(tmp_path):
    c, log_file = client_with_log(tmp_path)
    with c:
        c.post("/api/numbers/1/reset")
    line = log_file.read_text().splitlines()[0]
    assert line.split(" ")[1:4] == ["POST", "/api/numbers/1/reset", "404"] and line.split(" ")[4].startswith("ip=")
    assert line.split(" ")[4] != "ip=-"


def test_phone_numbers_in_a_path_are_masked_and_every_changing_method_is_recorded(tmp_path):
    c, log_file = client_with_log(tmp_path)
    with c:
        c.post("/api/numbers/+918000000001/mark-spam")
        c.delete("/api/numbers/918000000002")
        c.patch("/api/numbers/3")
        c.get("/api/numbers/+918000000003")
    text = log_file.read_text()
    assert "918000000001" not in text and "918000000002" not in text and "8000000003" not in text
    assert "+********0001" in text and "********0002" in text
    assert [x.split(" ")[1] for x in text.splitlines()] == ["POST", "DELETE", "PATCH"]


def test_the_log_line_formatter_masks_and_cannot_be_injected_into():
    from datetime import datetime, timezone
    when = datetime(2026, 10, 12, 10, 0, 0, tzinfo=timezone.utc)
    line = actionlog.format_action_line(when, "POST", "/api/x/+918000000001\nFAKE LINE 200", 200, "1.2.3.4\r\nevil")
    assert "\n" not in line and "\r" not in line and "918000000001" not in line
    assert line.startswith("2026-10-12T10:00:00Z POST /api/x/+********0001")
    assert actionlog.mask_digits("/api/numbers/42/mark-spam") == "/api/numbers/42/mark-spam"  # short ids stay


def test_the_log_is_off_unless_configured(tmp_path):
    assert Settings().action_log_path == ""
    settings = Settings(database_url=f"sqlite:///{tmp_path}/t.db", run_migrations=False, start_poller=False)
    with TestClient(create_app(settings, FakeBolnaClient())) as c:
        assert c.put("/api/settings/threshold", json={"threshold": 3}).status_code == 200
    assert not (tmp_path / ".share").exists()


def test_the_environment_can_move_or_switch_off_the_log(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("BOLNA_MODE", "fake")
    monkeypatch.delenv("ACTION_LOG_FILE", raising=False)
    assert Settings.from_env().action_log_path.endswith("/.share/actions.log")
    monkeypatch.setenv("ACTION_LOG_FILE", "/tmp/somewhere.log")
    assert Settings.from_env().action_log_path == "/tmp/somewhere.log"
    monkeypatch.setenv("ACTION_LOG_FILE", "")
    assert Settings.from_env().action_log_path == ""


# 7 ------------------------------------------------------------------ the backend stays private
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "::"])
def test_the_backend_refuses_to_listen_beyond_loopback_without_a_password(monkeypatch, host):
    import run
    started = []
    monkeypatch.setattr(run.Settings, "from_env", classmethod(lambda cls: Settings(host=host, app_password="")))
    monkeypatch.setattr(run.uvicorn, "run", lambda *a, **k: started.append(k))
    with pytest.raises(SystemExit) as e:
        run.main()
    assert "APP_PASSWORD" in str(e.value) and started == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_the_backend_still_starts_on_loopback_without_a_password(monkeypatch, host):
    import run
    started = []
    monkeypatch.setattr(run.Settings, "from_env", classmethod(lambda cls: Settings(host=host, app_password="")))
    monkeypatch.setattr(run.uvicorn, "run", lambda *a, **k: started.append(k))
    run.main()
    assert started and started[0]["host"] == host


def test_a_password_allows_another_host(monkeypatch):
    import run
    started = []
    monkeypatch.setattr(run.Settings, "from_env", classmethod(lambda cls: Settings(host="0.0.0.0", app_password="x")))
    monkeypatch.setattr(run.uvicorn, "run", lambda *a, **k: started.append(k))
    run.main()
    assert started
