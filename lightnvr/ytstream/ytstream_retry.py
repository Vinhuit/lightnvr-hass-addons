"""Persist YouTube retry limits across workers and service restarts."""

import hashlib
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest


_state_lock = threading.RLock()
QUOTA_ERRORS = {"quotaExceeded", "dailyLimitExceeded"}
PERMISSION_ERRORS = {
    "livePermissionBlocked", "liveStreamingNotEnabled", "insufficientLivePermissions",
}


def credential_scope(credentials):
    """Do not store credentials in the retry state. Quota belongs to a project."""
    client = getattr(credentials, "client_id", "") or "missing"
    project = client.split("-", 1)[0]
    account = getattr(credentials, "refresh_token", None) or client
    digest = lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest()
    return {"project": "project:" + digest(project), "account": "account:" + digest(account)}


def error_reason(error):
    if isinstance(error, YouTubeRetryBlocked):
        return error.status["reason"]
    if isinstance(error, HttpError):
        try:
            errors = json.loads(error.content).get("error", {}).get("errors", [])
            reasons = [item.get("reason") for item in errors]
            for reason in reasons:
                if reason in QUOTA_ERRORS or reason in PERMISSION_ERRORS:
                    return reason
            if reasons and reasons[0]:
                return reasons[0]
        except (ValueError, TypeError, AttributeError):
            pass
    return type(error).__name__


def next_quota_reset(now):
    local = datetime.fromtimestamp(now, ZoneInfo("America/Los_Angeles"))
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    # Allow a minute for Google's daily quota counter to reset.
    return midnight.timestamp() + 60


def public_status(record):
    reason = record["reason"]
    if reason in QUOTA_ERRORS:
        message = "YouTube's daily limit has been reached. New stream starts are paused until it resets."
    elif reason in PERMISSION_ERRORS:
        message = "Live streaming is restricted for this channel. Resolve it in YouTube Studio, then retry."
    else:
        message = "The stream could not start. Waiting before trying again."
    retry_after = record.get("retry_after")
    return {
        "blocked": True,
        "reason": reason,
        "message": message,
        "retry_at": datetime.fromtimestamp(retry_after, timezone.utc).isoformat() if retry_after else None,
        "manual_retry": reason in PERMISSION_ERRORS,
    }


class YouTubeRetryBlocked(Exception):
    def __init__(self, status):
        self.status = status
        super().__init__(status["message"])


class YouTubeRetryGuard:
    def __init__(self, state_path):
        self.path = Path(state_path)

    @contextmanager
    def _state(self):
        """Lock across threads/processes, then replace the state atomically."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _state_lock, self.path.with_suffix(".lock").open("a+b") as lock:
            if os.name == "nt":
                import msvcrt
                if lock.seek(0, os.SEEK_END) == 0:
                    lock.write(b"\0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    state = json.loads(self.path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    state = {"blocks": {}, "streams": {}}
                original = json.dumps(state, sort_keys=True)
                yield state
                if original != json.dumps(state, sort_keys=True):
                    temp_path = None
                    try:
                        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as temp:
                            temp_path = temp.name
                            json.dump(state, temp, indent=2)
                        os.chmod(temp_path, 0o600)
                        os.replace(temp_path, self.path)
                    finally:
                        if temp_path and os.path.exists(temp_path):
                            os.unlink(temp_path)
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _block(state, scope, now):
        for key in (scope["project"], scope["account"]):
            record = state["blocks"].get(key)
            if record:
                if record.get("retry_after") and record["retry_after"] <= now:
                    del state["blocks"][key]
                else:
                    status = public_status(record)
                    permission = state["blocks"].get(scope["account"])
                    if key == scope["project"] and permission and permission["reason"] in PERMISSION_ERRORS:
                        status["permission_blocked"] = True
                        status["message"] += " Live streaming is also restricted for this channel; resolve it in YouTube Studio."
                    return status
        return None

    def status(self, scope, stream_name=None, now=None):
        now = time.time() if now is None else now
        with self._state() as state:
            block = self._block(state, scope, now)
            if block:
                return block
            record = state["streams"].get(scope["account"], {}).get(stream_name)
            if record and record["retry_after"] > now:
                return public_status(record)
        return None

    def require_available(self, scope, stream_name=None):
        status = self.status(scope, stream_name)
        if status:
            raise YouTubeRetryBlocked(status)

    def record_api_error(self, scope, error, now=None):
        now = time.time() if now is None else now
        reason = error_reason(error)
        if reason not in QUOTA_ERRORS | PERMISSION_ERRORS:
            return
        key = scope["project"] if reason in QUOTA_ERRORS else scope["account"]
        with self._state() as state:
            state["blocks"][key] = {
                "reason": reason,
                "retry_after": next_quota_reset(now) if reason in QUOTA_ERRORS else None,
            }

    def record_failure(self, scope, stream_name, error, now=None):
        now = time.time() if now is None else now
        self.record_api_error(scope, error, now)
        with self._state() as state:
            block = self._block(state, scope, now)
            if block:
                return block
            streams = state["streams"].setdefault(scope["account"], {})
            failures = min(streams.get(stream_name, {}).get("failures", 0) + 1, 7)
            record = {
                "reason": error_reason(error),
                "failures": failures,
                "retry_after": now + min(60 * 2 ** (failures - 1), 3600),
            }
            streams[stream_name] = record
            return public_status(record)

    def clear_failure(self, scope, stream_name):
        with self._state() as state:
            state["streams"].get(scope["account"], {}).pop(stream_name, None)

    def reset_permissions(self, scope):
        """An explicit retry may clear channel restrictions, but not daily quota."""
        with self._state() as state:
            state["blocks"].pop(scope["account"], None)
            state["streams"].pop(scope["account"], None)


class GuardedYouTubeRequest(HttpRequest):
    def __init__(self, *args, retry_guard, retry_scope, **kwargs):
        super().__init__(*args, **kwargs)
        self.retry_guard = retry_guard
        self.retry_scope = retry_scope

    def execute(self, http=None, num_retries=0):
        self.retry_guard.require_available(self.retry_scope)
        try:
            return super().execute(http=http, num_retries=num_retries)
        except HttpError as error:
            self.retry_guard.record_api_error(self.retry_scope, error)
            raise

    def next_chunk(self, http=None, num_retries=0):
        self.retry_guard.require_available(self.retry_scope)
        try:
            return super().next_chunk(http=http, num_retries=num_retries)
        except HttpError as error:
            self.retry_guard.record_api_error(self.retry_scope, error)
            raise


def build_youtube_service(credentials, retry_guard):
    return build(
        "youtube", "v3", credentials=credentials,
        requestBuilder=partial(
            GuardedYouTubeRequest, retry_guard=retry_guard,
            retry_scope=credential_scope(credentials),
        ),
    )
