"""Account metadata and isolated credential paths (never expose token contents)."""
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

current_account = ContextVar("youtube_account", default="default")


def root():
    return Path(os.environ.get("YTSTREAM_CONFIG_DIR", "."))


def validate_id(account_id):
    if not isinstance(account_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", account_id):
        raise ValueError("Account ID must contain 1–64 letters, numbers, underscores or hyphens.")
    return account_id


def credential_path(filename, account_id=None):
    account_id = validate_id(account_id or current_account.get())
    directory = root() if account_id == "default" else root() / "accounts" / account_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    # Accounts can share the existing OAuth client, but never share tokens.
    if filename == "oauth.json" and not path.exists():
        return str(root() / filename)
    return str(path)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as f:
            temp = f.name
            json.dump(data, f, indent=2)
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)


def accounts():
    path = root() / "youtube_accounts.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    return [{"id": "default", "label": "Default"}] + saved


@contextmanager
def use_account(account_id):
    validate_id(account_id)
    if account_id not in {a["id"] for a in accounts()}:
        raise ValueError("Unknown YouTube account")
    token = current_account.set(account_id)
    try:
        yield
    finally:
        current_account.reset(token)


def owner_path(stream_name):
    return root() / "account_owners" / (hashlib.sha256(stream_name.encode()).hexdigest() + ".json")


def owner(stream_name, default="default"):
    path = owner_path(stream_name)
    return json.loads(path.read_text(encoding="utf-8"))["account_id"] if path.exists() else default


def set_owner(stream_name, account_id):
    atomic_json(owner_path(stream_name), {"account_id": account_id})
