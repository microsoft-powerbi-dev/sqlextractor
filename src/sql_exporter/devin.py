"""Minimal Devin REST client (v1 sessions). The API key is read from the environment per request."""
import json
import os
import urllib.error
import urllib.request

from .common import ExportError, require

TERMINAL = {"finished", "expired"}


def base_url():
    return os.environ.get("DEVIN_API_BASE", "https://api.devin.ai/v1").rstrip("/")


def request(method, path, body=None, timeout=60):
    key = os.environ.get("DEVIN_API_KEY")
    require(key, "Set DEVIN_API_KEY to a Devin API key to use the devin provider")
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base_url() + path, data=data, method=method,
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = response.read(10 * 1024 * 1024 + 1)
    except urllib.error.HTTPError as exc:
        raise ExportError(f"Devin API returned HTTP {exc.code}; check DEVIN_API_KEY, permissions, and usage limits") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ExportError("Devin API is unreachable; check network and proxy settings") from exc
    require(len(payload) <= 10 * 1024 * 1024, "Devin API response is too large")
    try:
        return json.loads(payload) if payload.strip() else {}
    except ValueError as exc:
        raise ExportError("Devin API returned invalid JSON") from exc


def create_session(prompt, title):
    result = request("POST", "/sessions", {"prompt": prompt, "title": title, "idempotent": False})
    require(isinstance(result, dict) and isinstance(result.get("session_id"), str), "Devin API did not return a session_id")
    return result


def get_session(session_id):
    return request("GET", "/sessions/" + session_id)


def terminate(session_id):
    try:
        request("DELETE", "/sessions/" + session_id, timeout=30)
    except ExportError:
        pass  # Already finished sessions cannot be terminated; nothing else to clean up.


def structured(session):
    value = session.get("structured_output")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) and value else None
