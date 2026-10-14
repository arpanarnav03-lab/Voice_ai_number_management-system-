"""Pure helpers for scripts/share.sh (standard library only, so any python3 will do).

Nothing here prints a secret. `.env` is read only through an exact-name lookup of three names, and
only derived yes/no values come out (the password itself is never output)."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

TUNNEL_URL = re.compile(r"https://([a-z0-9]+(?:-[a-z0-9]+)*)\.trycloudflare\.com(?!\.?[a-z0-9-])", re.IGNORECASE)
TRUE_WORDS = {"1", "true", "yes", "on"}


def extract_tunnel_url(text: str) -> str | None:
    """The public https://<words>.trycloudflare.com link in cloudflared's output, or None."""
    for m in TUNNEL_URL.finditer(text or ""):
        if m.group(1).lower() != "api":  # cloudflared's own API host shows up in error lines
            return m.group(0)
    return None


def lookup_env_names(env_text: str, names: tuple[str, ...]) -> dict[str, str]:
    """Exact-match lookup of just `names` in the text of a .env file. Every other line is ignored."""
    found: dict[str, str] = {}
    for line in (env_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key in names:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            found[key] = value
    return found


def resolve_flags(environ: dict, env_text: str = "", api_status: dict | None = None,
                  api_delete_enabled: bool | None = None) -> dict:
    """What the link exposes: whether a password is required, the Bolna mode, and whether deletion is on.
    The running backend's own answer wins; otherwise the process environment; otherwise `.env`."""
    names = ("BOLNA_MODE", "APP_PASSWORD", "ALLOW_BOLNA_NUMBER_DELETE")
    file_values = lookup_env_names(env_text, names)

    def pick(name: str) -> str | None:
        return environ[name] if name in environ else file_values.get(name)

    password_set = bool((pick("APP_PASSWORD") or "").strip())
    mode = (pick("BOLNA_MODE") or "").strip().lower() or "fake"
    delete_on = (pick("ALLOW_BOLNA_NUMBER_DELETE") or "").strip().lower() in TRUE_WORDS
    if api_status:
        password_set = bool(api_status.get("required", password_set))
        mode = str(api_status.get("mode") or mode).lower()
    if api_delete_enabled is not None:
        delete_on = bool(api_delete_enabled)
    return {"password_set": password_set, "mode": "real" if mode == "real" else "fake", "delete_on": delete_on}


def build_banner(password_set: bool, mode: str, delete_on: bool) -> str:
    if password_set:
        who = "This link is PUBLIC. A password is required to use the app (APP_PASSWORD is set)."
    else:
        who = "This link is PUBLIC. Anyone who has it can use the app. There is NO login (APP_PASSWORD is empty)."
    live = mode == "real"
    lines = [
        who,
        "Bolna mode: LIVE (calls are real)" if live else "Bolna mode: FAKE (no calls)",
        "Bolna deletion: ON (releasing numbers is permanent)" if delete_on else "Bolna deletion: OFF",
        "Stop sharing with Ctrl+C. The link stops working immediately and the next one will be different.",
    ]
    width = max(len(x) for x in lines) + 4
    bar = "!" * width if (live or not password_set) else "-" * width
    return "\n".join([bar, *[f"  {x}" for x in lines], bar])


def _get_json(url: str, timeout: float = 3.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except (urllib.error.URLError, ValueError, OSError):
        return None


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "url":  # stdin -> the link, exit 1 when there is none
        link = extract_tunnel_url(sys.stdin.read())
        if link:
            print(link)
        return 0 if link else 1
    if cmd == "flags":  # flags <backend-base-url> <env-file>  -> password_set= mode= delete=
        base, env_file = argv[2], argv[3]
        try:
            env_text = open(env_file, encoding="utf-8").read()
        except OSError:
            env_text = ""
        status = _get_json(f"{base}/api/auth/status")
        spam = _get_json(f"{base}/api/spam/list")  # needs no login when there is no password
        flags = resolve_flags(dict(os.environ), env_text, status,
                              spam.get("delete_enabled") if isinstance(spam, dict) else None)
        print(f"password_set={int(flags['password_set'])}\nmode={flags['mode']}\ndelete={int(flags['delete_on'])}")
        return 0
    if cmd == "banner":  # banner <0|1 password_set> <real|fake> <0|1 delete>
        print(build_banner(argv[2] == "1", argv[3], argv[4] == "1"))
        return 0
    if cmd == "mode":  # mode <backend-base-url> -> the running backend's mode, or nothing
        status = _get_json(f"{argv[2]}/api/auth/status")
        if status:
            print(status.get("mode", ""))
        return 0
    print("usage: share_helpers.py url | flags | banner | mode", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
