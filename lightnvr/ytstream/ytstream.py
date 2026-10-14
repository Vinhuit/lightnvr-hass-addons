import os, sys
import pickle
import time
import google.auth
from google.auth.exceptions import RefreshError
from ytstream_accounts import (
    accounts, atomic_json, credential_path, current_account, owner, root,
    set_owner, use_account, validate_id,
)
from google_auth_oauthlib.flow import InstalledAppFlow, Flow
from googleapiclient.http import MediaFileUpload
import logging
import subprocess
from datetime import datetime, timedelta, timezone
from google.auth.transport.requests import Request
import json
import psutil
import threading
from flask import Flask, jsonify, request, redirect, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix
from ytstream_retry import (
    YouTubeRetryBlocked, YouTubeRetryGuard, build_youtube_service, credential_scope,
)

# Global process tracker
active_processes = {}
active_processes_lock = threading.RLock()
sync_playlist_name = "Camera San"

# YouTube only guarantees automatic archives for broadcasts shorter than 12
# hours. Keep a safety margin so API/ingest delays cannot push a broadcast
# over that limit. These values can be overridden through the environment.
DEFAULT_MAX_BROADCAST_DURATION_SECONDS = 11 * 3600 + 45 * 60
DEFAULT_SCHEDULE_LEAD_SECONDS = 30
DEFAULT_INGEST_POLL_SECONDS = 2
DEFAULT_INGEST_WAIT_SECONDS = 180


def get_positive_env_int(name, default, minimum=1):
    """Read a positive integer environment setting with a safe fallback."""
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        logging.warning("Invalid %s value; using default %s", name, default)
        return default


def parse_youtube_timestamp(value):
    """Parse an RFC3339 timestamp returned by the YouTube API."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def remaining_broadcast_seconds(actual_start_time, max_duration_seconds, now=None):
    """Return the safe remaining lifetime of a YouTube broadcast."""
    started_at = parse_youtube_timestamp(actual_start_time)
    if started_at is None:
        return float(max_duration_seconds)
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (now - started_at).total_seconds())
    return max(0.0, float(max_duration_seconds) - age_seconds)

def get_config_path(filename):
    """Get absolute path for configuration file based on config directory environment variable."""
    config_dir = os.environ.get("YTSTREAM_CONFIG_DIR")
    if config_dir:
        os.makedirs(config_dir, exist_ok=True)
        return os.path.join(config_dir, filename)
    return filename


def get_retry_guard():
    return YouTubeRetryGuard(get_config_path("youtube_retry_state.json"))


def get_retry_scope(account_id=None):
    token_path = credential_path("token.pickle", account_id)
    credentials = None
    if os.path.exists(token_path):
        with open(token_path, "rb") as token:
            credentials = pickle.load(token)
    return credential_scope(credentials)


def get_retry_status(stream_name=None):
    account_id = stream_account(stream_name) if stream_name else current_account.get()
    return get_retry_guard().status(get_retry_scope(account_id), stream_name)


def stream_config(stream_name):
    return next((s for s in get_streams_from_file() if s.get("stream_name") == stream_name), {})


def stream_account(stream_name):
    return owner(stream_name, stream_config(stream_name).get("account_id", "default"))


def authenticate_stream(stream_name):
    """Fallback only before starting broadcasts, and only for authentication failures."""
    config = stream_config(stream_name)
    primary = stream_account(stream_name)
    candidates = list(dict.fromkeys([primary] + config.get("fallback_account_ids", [])))
    last_error = None
    for account_id in candidates:
        current_account.set(account_id)
        # Quota and live permission errors must not trigger account rotation.
        get_retry_guard().require_available(get_retry_scope(account_id), stream_name)
        try:
            with open(credential_path("token.pickle"), "rb") as token:
                credentials = pickle.load(token)
            if not credentials or not credentials.valid:
                if credentials and credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                    save_credentials(credentials)
                else:
                    raise RefreshError("Account requires authorization")
            set_owner(stream_name, account_id)
            return credentials
        except (RefreshError, FileNotFoundError) as error:
            last_error = error
            logging.warning("YouTube account '%s' requires authorization for '%s'.", account_id, stream_name)
    raise last_error


def save_credentials(credentials):
    path = credential_path("token.pickle")
    import tempfile
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=os.path.dirname(os.path.abspath(path)), delete=False) as token:
            temp_path = token.name
            pickle.dump(credentials, token)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def guarded_youtube_service(credentials):
    return build_youtube_service(credentials, get_retry_guard())


def start_stream_worker(url, stream_name, playlist):
    """Serialize starts and honor persisted blocks before spawning a worker."""
    with active_processes_lock:
        previous = active_processes.get(stream_name)
        if previous is not None and previous.is_alive():
            return None
        blocked = get_retry_status(stream_name)
        if blocked:
            return blocked
        if previous is not None:
            previous.join(timeout=0)
        import multiprocessing
        process = multiprocessing.Process(target=run_stream, args=(url, stream_name, playlist))
        process.start()
        active_processes[stream_name] = process
        return None

# ==========================================
# Settings & Broadcast History Management
# ==========================================
DEFAULT_SETTINGS = {
    "history_days_limit": 30
}

def get_settings():
    """Retrieve YTStream configuration settings."""
    settings_path = get_config_path("ytstream_settings.json")
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    days = data.get("history_days_limit", 30)
                    try:
                        days = max(1, min(365, int(days)))
                    except (ValueError, TypeError):
                        days = 30
                    data["history_days_limit"] = days
                    return {**DEFAULT_SETTINGS, **data}
        except Exception as e:
            logging.error(f"Error loading ytstream_settings.json: {e}")
    return dict(DEFAULT_SETTINGS)

def save_settings(new_settings):
    """Persist updated configuration settings."""
    settings_path = get_config_path("ytstream_settings.json")
    current = get_settings()
    current.update(new_settings)
    try:
        current["history_days_limit"] = max(1, min(365, int(current.get("history_days_limit", 30))))
    except (ValueError, TypeError):
        current["history_days_limit"] = 30
    try:
        with open(settings_path, "w", encoding="utf-8") as f:
            json.dump(current, f, indent=4)
        return True, current
    except Exception as e:
        logging.error(f"Error saving ytstream_settings.json: {e}")
        return False, str(e)

def get_history_file_path():
    return get_config_path("stream_history.json")

def load_history():
    """Load stream broadcast history from stream_history.json."""
    path = get_history_file_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logging.error(f"Error loading stream_history.json: {e}")
    return {"streams": {}}

def save_history(history_data):
    """Persist broadcast history."""
    path = get_history_file_path()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history_data, f, indent=4)
        return True
    except Exception as e:
        logging.error(f"Error saving stream_history.json: {e}")
        return False

def record_broadcast_history(stream_name, broadcast_id, title=None, date_str=None, suffix=None, status="live"):
    """Record or update a broadcast entry in persistent history."""
    if not stream_name or not broadcast_id:
        return
    now = datetime.now()
    if not date_str:
        date_str = now.strftime("%Y-%m-%d")
    if not suffix:
        suffix = "_1" if now.hour < 12 else "_2"
    if not title:
        title = f"{stream_name} {date_str}{suffix}"
    watch_url = f"https://www.youtube.com/watch?v={broadcast_id}"

    history = load_history()
    streams = history.setdefault("streams", {})
    camera_entries = streams.setdefault(stream_name, [])

    found = False
    for entry in camera_entries:
        if entry.get("broadcast_id") == broadcast_id or (entry.get("date") == date_str and entry.get("session") == suffix and entry.get("account_id", "default") == current_account.get()):
            entry["broadcast_id"] = broadcast_id
            entry["account_id"] = current_account.get()
            entry["status"] = status
            entry["title"] = title
            entry["watch_url"] = watch_url
            entry["date"] = date_str
            entry["session"] = suffix
            entry["last_updated"] = datetime.now(timezone.utc).isoformat()
            found = True
            break

    if not found:
        camera_entries.append({
            "broadcast_id": broadcast_id,
            "account_id": current_account.get(),
            "title": title,
            "date": date_str,
            "session": suffix,
            "watch_url": watch_url,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_updated": datetime.now(timezone.utc).isoformat()
        })

    camera_entries.sort(key=lambda x: (x.get("date", ""), x.get("session", "")), reverse=True)
    save_history(history)

def get_camera_history(stream_name=None, days_limit=30, include_today=False, specific_date=None):
    """
    Retrieve previous days' broadcast history per camera up to days_limit or for a specific_date (YYYY-MM-DD).
    When include_today is False and specific_date is None, only streams from previous days (date < today) are returned.
    """
    history = load_history()
    streams = history.get("streams", {})
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    cutoff_date = (now - timedelta(days=days_limit)).strftime("%Y-%m-%d")

    configured_streams = get_streams_from_file()
    all_names = set(s.get("stream_name") for s in configured_streams if s.get("stream_name"))
    if stream_name:
        all_names = {stream_name}
    else:
        all_names.update(streams.keys())

    result = {}
    for name in all_names:
        entries = streams.get(name, [])
        filtered_entries = []
        for e in entries:
            entry_date = e.get("date", "")
            if not entry_date:
                continue
            if specific_date:
                if entry_date == specific_date:
                    filtered_entries.append(e)
            else:
                if (include_today or entry_date < today_str) and entry_date >= cutoff_date:
                    filtered_entries.append(e)

        filtered_entries.sort(key=lambda x: (x.get("date", ""), x.get("session", "")), reverse=True)

        grouped_by_date = {}
        for item in filtered_entries:
            d = item.get("date")
            if d not in grouped_by_date:
                grouped_by_date[d] = {
                    "date": d,
                    "sessions": []
                }
            sfx = item.get("session", "_1")
            session_label = "Part 1 (00:00 - 12:00)" if sfx == "_1" else "Part 2 (12:00 - 24:00)" if sfx == "_2" else f"Session {sfx}"
            grouped_by_date[d]["sessions"].append({
                "session": sfx,
                "session_label": session_label,
                "broadcast_id": item.get("broadcast_id"),
                "watch_url": item.get("watch_url"),
                "status": item.get("status"),
                "title": item.get("title")
            })

        date_list = []
        for d in sorted(grouped_by_date.keys(), reverse=True):
            entry_obj = grouped_by_date[d]
            entry_obj["sessions"].sort(key=lambda x: x.get("session", ""))
            date_list.append(entry_obj)

        result[name] = {
            "stream_name": name,
            "days_limit": days_limit,
            "specific_date": specific_date,
            "total_records": len(filtered_entries),
            "days": date_list,
            "records": filtered_entries
        }

    return result

def get_youtube_service(account_id=None):
    """Get authenticated YouTube v3 service instance using token.pickle."""
    token_path = credential_path("token.pickle", account_id)
    if not os.path.exists(token_path):
        return None
    try:
        with open(token_path, "rb") as token:
            credentials = pickle.load(token)
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                with open(token_path, "wb") as token:
                    pickle.dump(credentials, token)
            else:
                return None
        return guarded_youtube_service(credentials)
    except Exception as e:
        logging.error(f"Error authenticating YouTube service: {e}")
        return None

def set_broadcast_privacy(youtube, broadcast_id, privacy_status="private"):
    """Update privacy of a broadcast (and its associated video archive) to 'private' or 'unlisted'."""
    if not youtube:
        youtube = get_youtube_service()
    if not youtube or not broadcast_id:
        return False
    try:
        youtube.liveBroadcasts().update(
            part="id,status",
            body={
                "id": broadcast_id,
                "status": {"privacyStatus": privacy_status}
            }
        ).execute()
        logging.info(f"Broadcast {broadcast_id} privacy updated to {privacy_status}.")
        return True
    except Exception as e:
        logging.debug(f"liveBroadcasts().update to {privacy_status} for {broadcast_id} exception: {e}")
        try:
            youtube.videos().update(
                part="id,status",
                body={
                    "id": broadcast_id,
                    "status": {"privacyStatus": privacy_status}
                }
            ).execute()
            logging.info(f"Video {broadcast_id} privacy updated to {privacy_status}.")
            return True
        except Exception as e2:
            logging.warning(f"Could not update privacy to {privacy_status} for {broadcast_id}: {e2}")
            return False

def end_and_privatize_stream(stream_name):
    """End a broadcast and set it to private when stopped."""
    try:
        streams = get_streams_from_file()
        for s in streams:
            if s.get("stream_name") == stream_name:
                watch_url = s.get("watch_url", "")
                if "v=" in watch_url:
                    vid = watch_url.split("v=")[1].split("&")[0]
                    yt = get_youtube_service(stream_account(stream_name))
                    if yt:
                        try:
                            yt.liveBroadcasts().transition(
                                broadcastStatus="complete",
                                id=vid,
                                part="status"
                            ).execute()
                            logging.info(f"Stream {stream_name} ({vid}) transitioned to COMPLETE.")
                        except Exception as e:
                            logging.debug(f"Transition exception on stop for {vid}: {e}")
                        set_broadcast_privacy(yt, vid, "private")
    except Exception as e:
        logging.error(f"Failed to end and privatize stopped stream {stream_name}: {e}")

def sync_history_from_youtube(days_limit=90):
    """Query YouTube API for past broadcasts and merge into stream_history.json."""
    token_path = credential_path("token.pickle")
    if not os.path.exists(token_path):
        return 0, "No YouTube credentials found (token.pickle missing)."
    try:
        with open(token_path, "rb") as token:
            credentials = pickle.load(token)
        if not credentials or not credentials.valid:
            if credentials and credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                with open(token_path, "wb") as token:
                    pickle.dump(credentials, token)
            else:
                return 0, "YouTube credentials expired."
        youtube = guarded_youtube_service(credentials)
    except Exception as e:
        return 0, f"Authentication error: {e}"

    import re
    now = datetime.now()
    cutoff_date = (now - timedelta(days=days_limit)).strftime("%Y-%m-%d")
    synced_count = 0

    history = load_history()
    streams_dict = history.setdefault("streams", {})

    try:
        request = youtube.liveBroadcasts().list(
            part="id,snippet,status",
            mine=True,
            broadcastType="all",
            maxResults=50
        )
        while request:
            response = request.execute()
            for b in response.get("items", []):
                title = b.get("snippet", {}).get("title", "")
                broadcast_id = b.get("id")
                status = b.get("status", {}).get("lifeCycleStatus", "unknown")
                privacy = b.get("status", {}).get("privacyStatus", "private")
                if not title or not broadcast_id:
                    continue
                m = re.match(r"^(.*?)\s+(\d{4}-\d{2}-\d{2})(_\d)?$", title)
                if m:
                    raw_name = m.group(1).strip()
                    date_str = m.group(2)
                    suffix = m.group(3) or "_1"
                    if date_str >= cutoff_date:
                        cam_key = raw_name.lower()
                        cam_entries = streams_dict.setdefault(cam_key, [])
                        
                        existing_entry = None
                        for e in cam_entries:
                            if e.get("date") == date_str and e.get("session") == suffix and e.get("account_id", "default") == current_account.get():
                                existing_entry = e
                                break
                            elif e.get("broadcast_id") == broadcast_id:
                                existing_entry = e
                                break
                                
                        watch_url = f"https://www.youtube.com/watch?v={broadcast_id}"
                        if existing_entry:
                            existing_entry["broadcast_id"] = broadcast_id
                            existing_entry["account_id"] = current_account.get()
                            existing_entry["title"] = title
                            existing_entry["date"] = date_str
                            existing_entry["session"] = suffix
                            existing_entry["watch_url"] = watch_url
                            existing_entry["status"] = status
                            existing_entry["last_updated"] = datetime.now(timezone.utc).isoformat()
                        else:
                            cam_entries.append({
                                "broadcast_id": broadcast_id,
                                "account_id": current_account.get(),
                                "title": title,
                                "date": date_str,
                                "session": suffix,
                                "watch_url": watch_url,
                                "status": status,
                                "created_at": datetime.now(timezone.utc).isoformat(),
                                "last_updated": datetime.now(timezone.utc).isoformat()
                            })
                            synced_count += 1

                        # Automatically ensure completed broadcasts are private
                        if status in ("complete", "revoked") and privacy == "unlisted":
                            set_broadcast_privacy(youtube, broadcast_id, "private")
            request = youtube.liveBroadcasts().list_next(request, response)

        # Sort all cameras by date and session descending
        for cam, entries in streams_dict.items():
            entries.sort(key=lambda x: (x.get("date", ""), x.get("session", "")), reverse=True)
            
        save_history(history)
    except Exception as e:
        logging.error(f"Error syncing broadcasts from YouTube: {e}")
        return synced_count, f"Sync error: {e}"

    return synced_count, f"Successfully synced {synced_count} broadcasts from YouTube."

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')

def process_videos_in_directory(directory, playlist_name="Camera San"):
    """Process videos in directory based on size:
    - >500MB: Upload directly
    - 100KB-500MB: Merge then upload
    - <100KB: Skip
    """
    LARGE_SIZE = 500 * 1024 * 1024  # 500MB in bytes
    MIN_SIZE = 100 * 1024  # 100KB in bytes
    
    large_videos = []
    medium_videos = []
    skipped_videos = []
    
    # First pass: categorize videos by size
    for root, dirs, files in os.walk(directory):
        for video_file in files:
            if video_file.endswith((".mp4", ".avi")):
                file_path = os.path.join(root, video_file)
                file_size = os.path.getsize(file_path)
                
                if file_size > LARGE_SIZE:
                    large_videos.append(file_path)
                elif file_size > MIN_SIZE:
                    medium_videos.append(file_path)
                else:
                    skipped_videos.append(file_path)
    
    # Report found videos
    logging.info(f"Found {len(large_videos)} large videos (>500MB)")
    logging.info(f"Found {len(medium_videos)} medium videos (100KB-500MB)")
    logging.info(f"Found {len(skipped_videos)} small videos (<100KB) to skip")
    
    # Process large videos first
    for video_path in large_videos:
        file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
        video_name = os.path.basename(video_path)
        logging.info(f"Uploading large video directly: {video_name} ({file_size_mb:.2f}MB)")
        
        title = f"Large Video {os.path.splitext(video_name)[0]} {datetime.now().strftime('%Y-%m-%d')}"
        description = f"Large video file uploaded directly. Original file: {video_name}"
        upload_video(video_path, title, description, "private", playlist_name)
    
    # Then merge and upload medium videos if any exist
    if medium_videos:
        today = datetime.now().strftime("%Y-%m-%d")
        output_file = os.path.join(directory, f"merged_medium_{today}.mp4")
        
        if merge_videos(medium_videos, output_file):
            title = f"Merged Medium Videos {today}"
            description = "Merged video containing files between 100KB and 500MB"
            upload_video(output_file, title, description, "private", playlist_name)
    
    return len(large_videos), len(medium_videos), len(skipped_videos)

def merge_videos(video_files, output_file):
    """Merge a list of video files into one and remove input files after merging"""
    if not video_files:
        return False
        
    file_list_path = os.path.join(os.path.dirname(output_file), "file_list.txt")
    
    try:
        # Create file list for ffmpeg
        with open(file_list_path, "w") as file_list:
            for video_path in video_files:
                file_list.write(f"file '{video_path}'\n")
                file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
                logging.info(f"Adding to merge: {os.path.basename(video_path)} ({file_size_mb:.2f}MB)")
        
        # Run ffmpeg to merge videos
        logging.info(f"Merging videos into {output_file}")
        subprocess.run([
            "ffmpeg", "-f", "concat", "-safe", "0", "-i", file_list_path, "-c", "copy", output_file
        ], check=True)
        
        logging.info(f"Successfully merged videos into {output_file}")

        # Remove input files after merging
        for video_path in video_files:
            if os.path.exists(video_path):
                os.remove(video_path)
                logging.info(f"Deleted input file after merging: {video_path}")
        
        return True
        
    except subprocess.CalledProcessError as e:
        logging.error(f"Error during video merging: {e}")
        return False
    finally:
        if os.path.exists(file_list_path):
            os.remove(file_list_path)

def video_exists_on_youtube(youtube, title):
    request = youtube.search().list(
        part="snippet",
        q=title,
        type="video",
        maxResults=5,  # Increase the number of results to check
        forMine=True  # Restrict search to the authenticated user's videos
    )
    response = request.execute()
    for item in response.get('items', []):
        if item['snippet']['title'].strip().lower() == title.strip().lower():
            return True
    return False

def get_or_create_playlist(youtube, playlist_name):
    """Get existing playlist or create new one if it doesn't exist"""
    # Try to find existing playlist
    request = youtube.playlists().list(
        part="snippet",
        mine=True
    )
    response = request.execute()
    
    for item in response.get('items', []):
        if item['snippet']['title'] == playlist_name:
            return item['id']
    
    # Create new playlist if not found
    request = youtube.playlists().insert(
        part="snippet,status",
        body={
            "snippet": {
                "title": playlist_name,
                "description": "Automatically created playlist for camera videos"
            },
            "status": {
                "privacyStatus": "private"
            }
        }
    )
    response = request.execute()
    return response['id']

def add_video_to_playlist(youtube, playlist_id, video_id, max_retries=5, retry_delay=5):
    """Add a video to a playlist, with retry on service unavailable errors. Only add if not already present."""
    # Check if video is already in playlist
    try:
        request = youtube.playlistItems().list(
            part="snippet",
            playlistId=playlist_id,
            maxResults=50
        )
        while request:
            response = request.execute()
            for item in response.get('items', []):
                resource = item['snippet'].get('resourceId', {})
                if resource.get('videoId') == video_id:
                    logging.info(f"Video {video_id} already in playlist {playlist_id}, skipping add.")
                    return None
            request = youtube.playlistItems().list_next(request, response)
    except Exception as e:
        logging.warning(f"Could not check if video is already in playlist: {e}")
    # Retry logic for adding video to playlist
    for attempt in range(max_retries):
        try:
            request = youtube.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": playlist_id,
                        "resourceId": {
                            "kind": "youtube#video",
                            "videoId": video_id
                        }
                    }
                }
            )
            return request.execute()
        except Exception as e:
            # Check for service unavailable or 409 error
            if hasattr(e, 'resp') and hasattr(e.resp, 'status') and e.resp.status in (409, 503):
                logging.warning(f"YouTube API service unavailable or 409 conflict when adding video to playlist. Attempt {attempt+1}/{max_retries}. Retrying in {retry_delay}s...")
                time.sleep(retry_delay)
            else:
                logging.error(f"Failed to add video to playlist: {e}")
                raise
    logging.error(f"Failed to add video to playlist after {max_retries} attempts.")
    return None

def upload_video(file_path, title, description, privacy_status="private", playlist_name="Camera San"):
    SCOPES = ['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.force-ssl']
    CHUNK_SIZE = 1024 * 1024 * 5  # 5MB chunks for upload

    # Authenticate
    credentials = None
    token_path = credential_path("token.pickle")
    oauth_path = credential_path("oauth.json")
    if os.path.exists(token_path):
        with open(token_path, "rb") as token:
            credentials = pickle.load(token)

    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                oauth_path, SCOPES
            )
            credentials = flow.run_local_server(port=0)

        with open(token_path, "wb") as token:
            pickle.dump(credentials, token)

    youtube = guarded_youtube_service(credentials)

    # Check if video already exists on YouTube
    if video_exists_on_youtube(youtube, title):
        logging.info(f"Video '{title}' already exists on YouTube. Skipping upload.")
        return

    # Get file size and prepare for chunked upload
    file_size = os.path.getsize(file_path)
    file_size_mb = file_size / (1024 * 1024)
    logging.info(f"Preparing to upload: {file_path} (Size: {file_size_mb:.2f}MB)")

    # Prepare video for upload with chunking
    media = MediaFileUpload(
        file_path, 
        mimetype="video/*", 
        chunksize=CHUNK_SIZE, 
        resumable=True
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=dict(
            snippet=dict(
                title=title,
                description=description,
                tags=["motioneye", "surveillance", "camera san"],
            ),
            status=dict(
                privacyStatus=privacy_status,
                madeForKids=False,
            ),
        ),
        media_body=media,
    )

    # Execute upload request with retry logic and progress monitoring
    response = None
    for attempt in range(3):
        try:
            logging.info("Starting upload with progress monitoring...")
            while response is None:
                status, response = request.next_chunk()
                if status:
                    uploaded_mb = (status.resumable_progress / 1024 / 1024)
                    percent = status.progress() * 100
                    logging.info(f"Uploaded {uploaded_mb:.1f}MB ({percent:.1f}%)")

            video_id = response['id']
            logging.info(f"Video uploaded successfully: {video_id}")

            # Add to playlist if playlist name is provided
            if playlist_name:
                playlist_id = get_or_create_playlist(youtube, playlist_name)
                add_video_to_playlist(youtube, playlist_id, video_id)
                logging.info(f"Added video to playlist: {playlist_name}")

            # Delete the file after successful upload
            if os.path.exists(file_path):
                os.remove(file_path)
                logging.info(f"Deleted file after upload: {file_path}")
            return
        except Exception as e:
            logging.error(f"Upload attempt {attempt + 1} failed: {e}")
            time.sleep(5)  # Wait before retrying
            response = None  # Reset response for retry

    logging.error(f"Failed to upload video after 3 attempts: {file_path}")

def stream_to_youtube(rtsp_url, stream_name, playlist_name="Camera San", privacy_status="unlisted"): 
    """
    Start a YouTube live stream using an RTSP input, reuse or create stream and playlist, and add the stream to the playlist.
    Rotates below YouTube's 12-hour archive limit, closes the current
    broadcast, creates a replacement, and continues streaming.
    Each day, only two streams are created: streamname YYYY-MM-DD_1 and streamname YYYY-MM-DD_2.
    """
    from googleapiclient.errors import HttpError
    SCOPES = ['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.force-ssl']
    max_broadcast_duration = get_positive_env_int(
        "YTSTREAM_MAX_BROADCAST_SECONDS",
        DEFAULT_MAX_BROADCAST_DURATION_SECONDS,
        minimum=60,
    )
    schedule_lead_seconds = get_positive_env_int(
        "YTSTREAM_SCHEDULE_LEAD_SECONDS",
        DEFAULT_SCHEDULE_LEAD_SECONDS,
    )
    ingest_poll_seconds = get_positive_env_int(
        "YTSTREAM_INGEST_POLL_SECONDS",
        DEFAULT_INGEST_POLL_SECONDS,
    )
    ingest_wait_seconds = get_positive_env_int(
        "YTSTREAM_INGEST_WAIT_SECONDS",
        DEFAULT_INGEST_WAIT_SECONDS,
        minimum=30,
    )
    
    credentials = authenticate_stream(stream_name)
    youtube = guarded_youtube_service(credentials)

    def get_stream_suffix():
        now = datetime.now()
        # 0:00-11:59 = _1, 12:00-23:59 = _2
        return "_1" if now.hour < 12 else "_2"

    def get_or_create_broadcast_and_stream(youtube, stream_name, privacy_status, broadcasts):
        today_str = datetime.now().strftime("%Y-%m-%d")
        suffix = get_stream_suffix()
        dated_stream_name = f"{stream_name} {today_str}{suffix}"
        # Keep the scheduled time close to now. The worker waits for YouTube
        # to report an active ingest before transitioning instead of sleeping
        # for a fixed five minutes.
        scheduled_start = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(seconds=schedule_lead_seconds)
        ).isoformat("T") + "Z"
        logging.info("Available broadcasts:")
        for b in broadcasts.get('items', []):
            logging.info(f"  - Title: {b['snippet']['title']}, Status: {b['status']['lifeCycleStatus']}, ID: {b['id']}")
            if b['snippet']['title'] == dated_stream_name:
                if b['status']['lifeCycleStatus'] == "live":
                    # While stream is living, ensure it is unlisted so user can view live stream
                    if b.get('status', {}).get('privacyStatus') != 'unlisted':
                        set_broadcast_privacy(youtube, b['id'], 'unlisted')
                    stream_id = b.get('contentDetails', {}).get('boundStreamId')
                    if stream_id:
                        s = youtube.liveStreams().list(
                            part="id,snippet,cdn",
                            id=stream_id
                        ).execute()
                        if s['items']:
                            ingestion_info = s['items'][0]['cdn']['ingestionInfo']
                            return (
                                b['id'],
                                stream_id,
                                ingestion_info,
                                False,
                                b.get('snippet', {}).get('actualStartTime'),
                            )
                    logging.warning(f"Live broadcast found but no bound stream. Will create new.")
                    break
                elif b['status']['lifeCycleStatus'] in ["created", "ready"]:
                    return b['id'], None, None, True, None
        broadcast = youtube.liveBroadcasts().insert(
            part="snippet,status,contentDetails",
            body={
                "snippet": {
                    "title": dated_stream_name,
                    "scheduledStartTime": scheduled_start
                },
                "status": {
                    "privacyStatus": privacy_status,
                    "selfDeclaredMadeForKids": False,
                    "lifeCycleStatus": "created"
                },
                "contentDetails": {"monitorStream": {"enableMonitorStream": False}}
            }
        ).execute()
        logging.info(f"Created new broadcast: {broadcast['id']}")
        return broadcast['id'], None, None, True, None

    def get_or_create_stream(youtube, stream_name):
        today_str = datetime.now().strftime("%Y-%m-%d")
        suffix = get_stream_suffix()
        dated_stream_name = f"{stream_name} {today_str}{suffix}"
        streams = youtube.liveStreams().list(
            part="id,snippet,cdn",
            mine=True,
            maxResults=50
        ).execute()
        
        found_stream_id = None
        found_ingestion_info = None
        
        for s in streams.get('items', []):
            title = s['snippet']['title']
            if title == dated_stream_name:
                found_stream_id = s['id']
                found_ingestion_info = s['cdn']['ingestionInfo']
                break

        # Do not delete historical liveStream resources on the handoff path.
        # Those API calls are slow, frequently rejected for streams already
        # used by an archive, and used to delay each replacement broadcast.
        # Explicit cleanup remains available through the cleanup endpoint.
                        
        if found_stream_id and found_ingestion_info:
            return found_stream_id, found_ingestion_info

        stream = youtube.liveStreams().insert(
            part="snippet,cdn",
            body={
                "snippet": {"title": dated_stream_name},
                "cdn": {
                    "frameRate": "30fps",
                    "ingestionType": "rtmp",
                    "resolution": "1080p"
                }
            }
        ).execute()
        return stream['id'], stream['cdn']['ingestionInfo']

    def cleanup_broadcast(youtube, broadcast_id, title, status):
        if status in ("complete", "revoked"):
            set_broadcast_privacy(youtube, broadcast_id, "private")
            return
        if status in ("created", "ready", "testing", "testStarting"):
            logging.info(f"Deleting old/other broadcast {broadcast_id} ({title}) with status '{status}' (never went live) before starting new one.")
            for attempt in range(3):
                try:
                    youtube.liveBroadcasts().delete(id=broadcast_id).execute()
                    logging.info(f"Old/other broadcast {broadcast_id} deleted successfully.")
                    return
                except Exception as e:
                    logging.error(f"Failed to delete old/other broadcast {broadcast_id} (attempt {attempt+1}): {e}")
                    time.sleep(2)
        else:
            logging.info(f"Transitioning old/other broadcast {broadcast_id} ({title}) with status '{status}' to COMPLETE before starting new one.")
            for attempt in range(3):
                try:
                    youtube.liveBroadcasts().transition(
                        broadcastStatus="complete",
                        id=broadcast_id,
                        part="status"
                    ).execute()
                    set_broadcast_privacy(youtube, broadcast_id, "private")
                    # Check if it is now complete
                    b_check = youtube.liveBroadcasts().list(
                        part="id,status",
                        id=broadcast_id
                    ).execute()
                    if b_check.get('items'):
                        new_status = b_check['items'][0]['status']['lifeCycleStatus']
                        if new_status == "complete":
                            logging.info(f"Old/other broadcast {broadcast_id} transitioned to COMPLETE.")
                            return
                        else:
                            logging.warning(f"Broadcast {broadcast_id} still in status '{new_status}' after transition attempt {attempt+1}.")
                except Exception as e:
                    logging.error(f"Failed to transition old/other broadcast {broadcast_id} to COMPLETE (attempt {attempt+1}): {e}")
                    time.sleep(2)
            set_broadcast_privacy(youtube, broadcast_id, "private")
            # Final check after retries
            try:
                b_check = youtube.liveBroadcasts().list(
                    part="id,status",
                    id=broadcast_id
                ).execute()
                if b_check.get('items'):
                    final_status = b_check['items'][0]['status']['lifeCycleStatus']
                    if final_status == "live":
                        logging.error(f"Broadcast {broadcast_id} ({title}) is STILL LIVE after transition attempts. This usually means ffmpeg or another RTMP stream is still pushing. Please ensure the RTMP stream is stopped before transitioning to COMPLETE.")
            except Exception as e:
                logging.warning(f"Failed to run final status check on broadcast {broadcast_id}: {e}")

    # 12-hour rotation loop
    prev_ffmpeg_proc = None
    last_stream_date = None
    pid_dir = "ffmpeg_pids"
    os.makedirs(pid_dir, exist_ok=True)
    safe_stream_name = ''.join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in stream_name)
    pid_file_path = os.path.join(pid_dir, f"ffmpeg_{safe_stream_name}.pid")
    while True:
        # Check for existing ffmpeg PID file and process
        if os.path.exists(pid_file_path):
            try:
                with open(pid_file_path, "r") as pf:
                    pid = int(pf.read().strip())
                if psutil.pid_exists(pid):
                    p = psutil.Process(pid)
                    if p.is_running() and p.name().lower().startswith("ffmpeg"):
                        logging.error(f"ffmpeg process already running for stream '{stream_name}' (PID {pid}). Skipping duplicate start.")
                        print(f"ffmpeg already running for stream '{stream_name}' (PID {pid}). Exiting.")
                        return
            except Exception as e:
                logging.warning(f"Could not check existing ffmpeg PID for stream '{stream_name}': {e}")
                # Remove corrupt PID file
                try:
                    os.remove(pid_file_path)
                except Exception:
                    pass
        # Use dated stream name for this session
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        suffix = get_stream_suffix()
        # If the date has changed since the last stream, always start with _1
        if last_stream_date is not None and today_str != last_stream_date:
            suffix = "_1"
        dated_stream_name = f"{stream_name} {today_str}{suffix}"
        last_stream_date = today_str
        import re
        try:
            broadcasts = youtube.liveBroadcasts().list(
                part="id,snippet,status,contentDetails",
                mine=True,
                broadcastType="all",
                maxResults=50,
            ).execute()
            for b in broadcasts.get('items', []):
                title = b['snippet']['title']
                status = b['status']['lifeCycleStatus']
                logging.debug(f"Found broadcast: {title} (status: {status}, id: {b['id']})")
                # Match titles like 'stream_name YYYY-MM-DD_1' or 'stream_name YYYY-MM-DD_2'
                m = re.match(rf"^{re.escape(stream_name)} (\d{{4}}-\d{{2}}-\d{{2}})(_\d)$", title)
                if m:
                    b_date, b_sfx = m.group(1), m.group(2)
                    # If not today or not the current suffix, and not complete, close it
                    if (b_date != today_str or b_sfx != suffix) and status not in ("complete", "revoked"):
                        cleanup_broadcast(youtube, b['id'], title, status)
                else:
                    # If the title starts with the stream_name but doesn't match the expected pattern, close it as well
                    if title.startswith(stream_name + " ") and status not in ("complete", "revoked"):
                        cleanup_broadcast(youtube, b['id'], title, status)
        except Exception as e:
            logging.warning(f"Could not check/close old/other broadcasts: {e}")
            raise
        (
            broadcast_id,
            bound_stream_id,
            bound_ingestion_info,
            need_to_bind,
            broadcast_actual_start,
        ) = get_or_create_broadcast_and_stream(youtube, stream_name, privacy_status, broadcasts)

        remaining_seconds = remaining_broadcast_seconds(
            broadcast_actual_start,
            max_broadcast_duration,
        )
        if broadcast_actual_start and remaining_seconds <= 0:
            logging.warning(
                "Existing broadcast %s for '%s' is already at or beyond the "
                "safe archive duration; completing it before reconnecting.",
                broadcast_id,
                stream_name,
            )
            try:
                youtube.liveBroadcasts().transition(
                    broadcastStatus="complete",
                    id=broadcast_id,
                    part="status",
                ).execute()
                set_broadcast_privacy(youtube, broadcast_id, "private")
            except Exception as e:
                logging.error(
                    "Failed to complete over-age broadcast %s: %s",
                    broadcast_id,
                    e,
                )
            continue

        # This deadline is created once per YouTube broadcast and retained
        # across every FFmpeg retry. For a reused live broadcast it is based
        # on YouTube's actualStartTime, so a service/container restart cannot
        # reset the archive clock.
        rotation_deadline = time.monotonic() + remaining_seconds
        logging.info(
            "Broadcast %s safe rotation deadline is in %.1f minutes.",
            broadcast_id,
            remaining_seconds / 60.0,
        )
        if bound_stream_id and bound_ingestion_info:
            stream_id = bound_stream_id
            ingestion_info = bound_ingestion_info
            logging.info(f"Using existing live broadcast and stream: {broadcast_id}, {stream_id}")
        else:
            stream_id, ingestion_info = get_or_create_stream(youtube, stream_name)
            if need_to_bind:
                logging.debug(f"Binding broadcast {broadcast_id} to stream {stream_id}")
                youtube.liveBroadcasts().bind(
                    part="id,contentDetails",
                    id=broadcast_id,
                    streamId=stream_id
                ).execute()
                # Wait for YouTube to confirm that it is receiving RTMP data,
                # then transition immediately. The API requires the bound
                # liveStream to be active before a live transition.
                def transition_to_live():
                    logging.info(
                        "Waiting for active YouTube ingest before starting "
                        "broadcast %s...",
                        broadcast_id,
                    )
                    wait_deadline = time.monotonic() + ingest_wait_seconds
                    last_stream_status = None
                    while time.monotonic() < wait_deadline:
                        try:
                            stream_response = youtube.liveStreams().list(
                                part="status",
                                id=stream_id,
                            ).execute()
                            if stream_response.get('items'):
                                last_stream_status = stream_response['items'][0].get(
                                    'status', {}
                                ).get('streamStatus')

                            if last_stream_status == "active":
                                try:
                                    youtube.liveBroadcasts().transition(
                                        broadcastStatus="live",
                                        id=broadcast_id,
                                        part="status",
                                    ).execute()
                                    logging.info(
                                        "Broadcast %s transitioned to LIVE "
                                        "after ingest became active.",
                                        broadcast_id,
                                    )
                                    get_retry_guard().clear_failure(credential_scope(credentials), stream_name)
                                    set_broadcast_privacy(youtube, broadcast_id, "unlisted")
                                    return
                                except Exception as e:
                                    # A scheduled start may still be a few
                                    # seconds away. Keep retrying until the
                                    # bounded deadline instead of sleeping for
                                    # several minutes.
                                    if "redundantTransition" in str(e):
                                        logging.info(
                                            "Broadcast %s is already LIVE.",
                                            broadcast_id,
                                        )
                                        get_retry_guard().clear_failure(credential_scope(credentials), stream_name)
                                        return
                                    logging.warning(
                                        "Active ingest for broadcast %s but "
                                        "LIVE transition is not ready yet: %s",
                                        broadcast_id,
                                        e,
                                    )
                        except Exception as e:
                            logging.warning(
                                "Could not check ingest status for broadcast "
                                "%s: %s",
                                broadcast_id,
                                e,
                            )
                        time.sleep(ingest_poll_seconds)
                    logging.error(
                        "Timed out after %ss waiting to transition broadcast "
                        "%s to LIVE (last stream status: %s).",
                        ingest_wait_seconds,
                        broadcast_id,
                        last_stream_status,
                    )
                threading.Thread(target=transition_to_live, daemon=True).start()

        def update_playlist_membership(target_date=today_str):
            """Update playlist membership off the latency-sensitive path."""
            try:
                playlist_youtube = guarded_youtube_service(credentials)
                playlist_id = get_or_create_playlist(
                    playlist_youtube,
                    playlist_name,
                )
                logging.debug(
                    "Playlist name: %s, Playlist ID: %s",
                    playlist_name,
                    playlist_id,
                )
                # Add both _1 and _2 broadcasts for the segment date.
                broadcasts = playlist_youtube.liveBroadcasts().list(
                    part="id,snippet,status,contentDetails",
                    mine=True,
                    broadcastType="all",
                    maxResults=50,
                ).execute()
                by_title = {
                    b['snippet']['title']: b['id']
                    for b in broadcasts.get('items', [])
                }
                for sfx in ["_1", "_2"]:
                    alt_stream_name = f"{stream_name} {target_date}{sfx}"
                    video_id = by_title.get(alt_stream_name)
                    if video_id:
                        add_video_to_playlist(
                            playlist_youtube,
                            playlist_id,
                            video_id,
                        )
            except Exception as e:
                logging.warning(
                    "Could not update playlist membership for '%s': %s",
                    stream_name,
                    e,
                )

        rtmp_url = f"{ingestion_info['ingestionAddress']}/{ingestion_info['streamName']}"
        logging.info(f"RTMP URL for ffmpeg: {rtmp_url}")
        logging.info(f"RTMP ingest address: {ingestion_info['ingestionAddress']}")
        logging.info(f"RTMP stream key: {ingestion_info['streamName']}")
        logging.info(f"RTSP input URL: {rtsp_url}")
        logging.info(f"Ready to start ffmpeg streaming command.")

        watch_url = f"https://www.youtube.com/watch?v={broadcast_id}"
        logging.info(f"YouTube Live Watch URL: {watch_url} (stream is set to private)")
        print(f"YouTube Live Watch URL: {watch_url} (private)")

        # Record in persistent stream_history.json
        try:
            record_broadcast_history(
                stream_name=stream_name,
                broadcast_id=broadcast_id,
                title=dated_stream_name,
                date_str=today_str,
                suffix=suffix,
                status="live"
            )
        except Exception as e:
            logging.warning(f"Failed to record broadcast history: {e}")

        # Update stream.json with the new watch_url for the matching stream_name (ignore suffix)
        streams = get_streams_from_file()
        updated = False
        for s in streams:
            if s.get("stream_name") == stream_name:
                s["watch_url"] = watch_url
                updated = True
                break
        if updated:
            try:
                with open(get_config_path("stream.json"), "w", encoding="utf-8") as f:
                    json.dump(streams, f, indent=4)
                logging.info(f"Updated watch_url for stream '{stream_name}' in stream.json.")
            except Exception as e:
                logging.error(f"Failed to update stream.json: {e}")
        else:
            logging.warning(f"No entry with stream_name '{stream_name}' found in stream.json to update watch_url.")

        protocol = "tcp"
        streams = get_streams_from_file()
        for s in streams:
            if s.get("url") == rtsp_url:
                protocol = s.get("protocol", "tcp")
                break
        logging.info(f"Using RTSP protocol: {protocol}")
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner",
            "-fflags", "+genpts+discardcorrupt", "-use_wallclock_as_timestamps", "1",
            "-flags", "nobuffer", "-flags", "low_delay", "-analyzeduration", "20000000", "-probesize", "20000000",
            "-rtsp_transport", protocol,
            "-i", rtsp_url,
            "-map", "0:v:0", "-c:v", "copy",
            "-map", "0:a:0?", "-c:a", "aac", "-ar", "44100", "-b:a", "128k",
            "-f", "flv", rtmp_url
        ]
        logging.info(f"FFmpeg command: {' '.join(ffmpeg_cmd)}")

        log_dir = "ffmpeg_logs"
        os.makedirs(log_dir, exist_ok=True)
        pid_dir = "ffmpeg_pids"
        os.makedirs(pid_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d")
        safe_stream_name = ''.join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in stream_name)
        log_file_path = os.path.join(log_dir, f"ffmpeg_{safe_stream_name}_{timestamp}.log")
        pid_file_path = os.path.join(pid_dir, f"{safe_stream_name}.pid")
        logging.info(f"FFmpeg output will be logged to: {log_file_path}")

        # Check if a PID file exists and if the process is running
        if os.path.exists(pid_file_path):
            try:
                with open(pid_file_path, "r") as pf:
                    old_pid = int(pf.read().strip())
                # Check if process is running (Windows: tasklist, POSIX: os.kill)
                if psutil.pid_exists(old_pid):
                    p = psutil.Process(old_pid)
                    if p.is_running() and p.name().lower().startswith("ffmpeg"):
                        logging.error(f"ffmpeg process already running for stream '{stream_name}' (PID {old_pid}). Not starting another.")
                        print(f"ffmpeg already running for stream '{stream_name}' (PID {old_pid}). Exiting.")
                        return
                # If not running, remove stale PID file
                os.remove(pid_file_path)
            except Exception as e:
                logging.warning(f"Could not check/remove old PID file: {e}")
                try:
                    os.remove(pid_file_path)
                except Exception:
                    pass

        # Start FFmpeg and rotate at the deadline derived from YouTube's
        # actual broadcast age. The deadline must survive FFmpeg retries.
        error_triggered = False
        ffmpeg_should_restart = True
        playlist_update_started = False
        while ffmpeg_should_restart:
            ffmpeg_should_restart = False
            ffmpeg_proc = None
            error_triggered = False
            rotation_completed = False
            try:
                with open(log_file_path, "a", encoding="utf-8") as logf:
                    ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdout=logf, stderr=logf)
                    prev_ffmpeg_proc = ffmpeg_proc
                    # Save PID to file
                    with open(pid_file_path, "w") as pf:
                        pf.write(str(ffmpeg_proc.pid))
                    if not playlist_update_started:
                        threading.Thread(
                            target=update_playlist_membership,
                            daemon=True,
                        ).start()
                        playlist_update_started = True
                    while True:
                        if ffmpeg_proc.poll() is not None:
                            # Remove PID file on exit
                            try:
                                if os.path.exists(pid_file_path):
                                    os.remove(pid_file_path)
                            except Exception as e:
                                logging.warning(f"Could not remove PID file: {e}")
                            logging.warning(f"ffmpeg exited with code {ffmpeg_proc.returncode}, checking for error in log...")
                            # Check for specific ffmpeg error in the last 100 lines of the log
                            try:
                                with open(log_file_path, "r", encoding="utf-8", errors="ignore") as lf:
                                    lines = lf.readlines()[-100:]
                                    for line in lines:
                                        if ("Error writing trailer: End of file" in line or
                                            "Error closing file: Broken pipe" in line):
                                            logging.warning("Detected ffmpeg output error, will rerun stream immediately.")
                                            error_triggered = True
                                            break
                                        # Detect SIGINT exit
                                        if "Exiting normally, received signal 2" in line:
                                            logging.warning("ffmpeg exited due to SIGINT (signal 2). Will restart stream unless main script is exiting.")
                                            error_triggered = True
                                            break
                            except Exception as e:
                                logging.warning(f"Could not read ffmpeg log for error detection: {e}")
                            # Also check return code for SIGINT (130 or 2)
                            if ffmpeg_proc.returncode in (2, 130):
                                logging.warning("ffmpeg exited with SIGINT (return code 2 or 130). Will restart stream unless main script is exiting.")
                                error_triggered = True

                            # Any premature FFmpeg exit must retry the same
                            # YouTube broadcast. Previously, unrecognised
                            # return codes escaped to the outer loop, reused
                            # the live broadcast, and reset its archive timer.
                            if time.monotonic() < rotation_deadline:
                                if not error_triggered:
                                    logging.warning(
                                        "ffmpeg exited before the safe rotation "
                                        "deadline; restarting the current "
                                        "broadcast without resetting its timer."
                                    )
                                error_triggered = True
                            else:
                                # The archive deadline always wins over stale
                                # error text found in the shared daily log.
                                error_triggered = False
                            break
                        if time.monotonic() >= rotation_deadline:
                            logging.info(
                                "Safe YouTube archive duration reached, "
                                "rotating stream..."
                            )
                            ffmpeg_proc.terminate()
                            try:
                                ffmpeg_proc.wait(timeout=10)
                            except Exception:
                                ffmpeg_proc.kill()
                            # Wait for YouTube to detect stream end before transitioning
                            logging.info("Waiting 10 seconds for YouTube to detect stream end before transitioning broadcast to COMPLETE...")
                            time.sleep(10)
                            # Explicitly end the current broadcast before creating a new one
                            try:
                                youtube.liveBroadcasts().transition(
                                    broadcastStatus="complete",
                                    id=broadcast_id,
                                    part="status"
                                ).execute()
                                logging.info(
                                    "Broadcast %s transitioned to COMPLETE at "
                                    "the safe archive limit.",
                                    broadcast_id,
                                )
                                set_broadcast_privacy(youtube, broadcast_id, "private")
                                record_broadcast_history(
                                    stream_name=stream_name,
                                    broadcast_id=broadcast_id,
                                    title=dated_stream_name,
                                    date_str=today_str,
                                    suffix=suffix,
                                    status="complete"
                                )
                            except Exception as e:
                                logging.error(f"Failed to transition broadcast {broadcast_id} to COMPLETE: {e}")
                            rotation_completed = True
                            break
                        time.sleep(10)
                # Clean log if >5MB
                try:
                    if os.path.getsize(log_file_path) > 5 * 1024 * 1024:
                        logging.info(f"Truncating ffmpeg log file (>5MB): {log_file_path}")
                        with open(log_file_path, "w", encoding="utf-8") as logf:
                            logf.write("")
                except Exception as e:
                    logging.warning(f"Could not check/truncate ffmpeg log file: {e}")
            except Exception as e:
                # Remove PID file on error
                try:
                    if os.path.exists(pid_file_path):
                        os.remove(pid_file_path)
                except Exception as e2:
                    logging.warning(f"Could not remove PID file after error: {e2}")
                logging.error(f"ffmpeg process error: {e}, restarting in 5 seconds...")
                if ffmpeg_proc:
                    try:
                        ffmpeg_proc.terminate()
                    except Exception:
                        pass
                time.sleep(5)
                error_triggered = time.monotonic() < rotation_deadline

            # Handle the edge case where FFmpeg exits just as the deadline is
            # reached, before the polling branch above can complete YouTube.
            if (
                not error_triggered
                and not rotation_completed
                and time.monotonic() >= rotation_deadline
            ):
                logging.info(
                    "FFmpeg ended at the safe archive deadline; completing "
                    "broadcast %s.",
                    broadcast_id,
                )
                time.sleep(10)
                try:
                    youtube.liveBroadcasts().transition(
                        broadcastStatus="complete",
                        id=broadcast_id,
                        part="status",
                    ).execute()
                    set_broadcast_privacy(youtube, broadcast_id, "private")
                    record_broadcast_history(
                        stream_name=stream_name,
                        broadcast_id=broadcast_id,
                        title=dated_stream_name,
                        date_str=today_str,
                        suffix=suffix,
                        status="complete"
                    )
                    rotation_completed = True
                except Exception as e:
                    logging.error(
                        "Failed to complete broadcast %s at its deadline: %s",
                        broadcast_id,
                        e,
                    )
            # Retry the same broadcast on errors; only the safe archive
            # deadline is allowed to return to the outer broadcast loop.
            if error_triggered:
                logging.info("Restarting ffmpeg for this stream due to ffmpeg output error or SIGINT...")
                ffmpeg_should_restart = True
            else:
                logging.info("Restarting stream with new broadcast/stream...")

def get_streams_from_file(json_path=None):
    """Load list of streams from a JSON file."""
    if json_path is None:
        json_path = get_config_path("stream.json")
    if not os.path.exists(json_path):
        logging.error(f"Stream list file not found: {json_path}")
        return []
    with open(json_path, "r", encoding="utf-8") as f:
        try:
            streams = json.load(f)
            return streams
        except Exception as e:
            logging.error(f"Failed to load stream list: {e}")
            return []

def run_stream(url, stream_name, playlist):
    guard = get_retry_guard()
    context_token = current_account.set(stream_account(stream_name))
    scope = get_retry_scope()
    try:
        guard.require_available(scope, stream_name)
        stream_to_youtube(url, stream_name, playlist)
    except YouTubeRetryBlocked as error:
        guard.record_api_error(scope, error)
        logging.warning("Stream '%s' paused: %s", stream_name, error)
    except Exception as error:
        status = guard.record_failure(get_retry_scope(stream_account(stream_name)), stream_name, error)
        logging.error("Stream '%s' paused (%s): %s", stream_name, status["reason"], status["message"])
    finally:
        current_account.reset(context_token)

def delete_old_streams():
    """Find and delete all live stream configurations on YouTube that are older than today."""
    from googleapiclient.errors import HttpError
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow
    import pickle
    import re
    
    SCOPES = ['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.force-ssl']
    
    # Authenticate
    credentials = None
    token_path = credential_path("token.pickle")
    oauth_path = credential_path("oauth.json")
    if os.path.exists(token_path):
        with open(token_path, "rb") as token:
            credentials = pickle.load(token)
    if not credentials or not credentials.valid:
        if credentials and credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                oauth_path, SCOPES
            )
            credentials = flow.run_local_server(port=0)
        with open(token_path, "wb") as token:
            pickle.dump(credentials, token)
    youtube = guarded_youtube_service(credentials)

    today_str = datetime.now().strftime("%Y-%m-%d")
    logging.info(f"Scanning for old live streams to delete (older than today: {today_str})...")

    request = youtube.liveStreams().list(
        part="id,snippet",
        mine=True,
        maxResults=50
    )
    
    total_found = 0
    deleted_count = 0
    skipped_count = 0
    quota_exceeded = False
    
    while request and not quota_exceeded:
        try:
            response = request.execute()
        except HttpError as e:
            logging.error(f"Error fetching live streams: {e}")
            break
            
        items = response.get('items', [])
        for s in items:
            title = s['snippet']['title']
            # Match YYYY-MM-DD pattern in title
            m = re.search(r"(\d{4}-\d{2}-\d{2})", title)
            if m:
                s_date = m.group(1)
                if s_date < today_str:
                    total_found += 1
                    logging.info(f"Deleting old live stream: '{title}' (ID: {s['id']})")
                    try:
                        youtube.liveStreams().delete(id=s['id']).execute()
                        deleted_count += 1
                        time.sleep(0.5)  # Avoid rate limits
                    except HttpError as e:
                        if e.resp.status == 403 and "quota" in str(e).lower():
                            logging.warning("YouTube API Quota exceeded! Stopping deletion process.")
                            quota_exceeded = True
                            break
                        else:
                            logging.error(f"Failed to delete '{title}': {e}")
                            skipped_count += 1
                    except Exception as e:
                        logging.error(f"Failed to delete '{title}': {e}")
                        skipped_count += 1
            else:
                logging.debug(f"Skipping non-dated live stream: '{title}'")
                
        if quota_exceeded:
            break
            
        request = youtube.liveStreams().list_next(request, response)
        
    logging.info(f"Scan and cleanup complete.")
    logging.info(f"- Identified {total_found} streams matching the old date pattern.")
    logging.info(f"- Successfully deleted: {deleted_count}")
    if skipped_count > 0:
        logging.info(f"- Failed to delete: {skipped_count}")
    if quota_exceeded:
        logging.warning("Deletion stopped because your daily YouTube API quota was exceeded.")
        print("\n[WARNING] YouTube API Quota exceeded! Stopped after deleting some streams.")
        print(f"Deleted: {deleted_count} streams. Run the script again tomorrow when quota resets.")
app = Flask(__name__, static_folder='ytstream_web', static_url_path='')
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "ytstream_secret_key_12345")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=1)
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024

@app.route('/health')
def health():
    return jsonify({'status': 'ok'})

@app.route('/api/oauth/client', methods=['GET', 'POST'])
def oauth_client():
    if request.method == 'POST':
        data = request.get_json(silent=True)
        client = data.get('web') if isinstance(data, dict) else None
        if not isinstance(client, dict) or not client.get('client_id') or not client.get('client_secret'):
            return jsonify({'error': 'Upload a Google OAuth Web application client JSON.'}), 400
        if client.get('auth_uri') not in ('https://accounts.google.com/o/oauth2/auth', 'https://accounts.google.com/o/oauth2/v2/auth') or client.get('token_uri') not in ('https://oauth2.googleapis.com/token', 'https://accounts.google.com/o/oauth2/token'):
            return jsonify({'error': 'Use the client JSON downloaded from Google Cloud.'}), 400
        atomic_json(get_config_path('oauth.json'), {'web': client})
    return jsonify({'configured': os.path.exists(credential_path('oauth.json')),
                    'redirect_uri': url_for('oauth2callback', _external=True)})

@app.before_request
def account_request_context():
    from flask import g
    account_id = request.args.get("account_id", "default")
    if account_id not in {a["id"] for a in accounts()}:
        return jsonify({"error": "Unknown account"}), 400
    g.account_context_token = current_account.set(account_id)


@app.teardown_request
def reset_account_context(error=None):
    from flask import g
    token = getattr(g, "account_context_token", None)
    if token is not None:
        current_account.reset(token)


def public_accounts():
    return [{**a, "authenticated": os.path.exists(credential_path("token.pickle", a["id"])),
             "retry": get_retry_guard().status(get_retry_scope(a["id"]))} for a in accounts()]


@app.route('/api/accounts', methods=['GET', 'POST'])
def api_accounts():
    if request.method == 'GET':
        return jsonify({"accounts": public_accounts()})
    data = request.get_json(silent=True) or {}
    try:
        account_id = validate_id(data.get("id"))
        label = data.get("label", account_id)
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise ValueError("Account label must contain 1–100 characters")
        with active_processes_lock:
            saved = accounts()
            if account_id in {a["id"] for a in saved}:
                raise ValueError("Account ID already exists")
            atomic_json(root() / "youtube_accounts.json", saved[1:] + [{"id": account_id, "label": label.strip()}])
        return jsonify({"accounts": public_accounts()}), 201
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

@app.before_request
def handle_preflight():
    if request.method == "OPTIONS":
        response = app.make_default_options_response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return response

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/api/status')
def api_status():
    streams = get_streams_from_file()
    auth_status = any(os.path.exists(credential_path("token.pickle", a["id"])) for a in accounts())
    settings = get_settings()
    days_limit = settings.get("history_days_limit", 30)
    history_data = get_camera_history(days_limit=days_limit, include_today=False)
    
    stream_statuses = []
    for s in streams:
        name = s.get("stream_name")
        p = active_processes.get(name)
        is_active = p is not None and p.is_alive()
        
        cam_hist = history_data.get(name, {})
        stream_statuses.append({
            "stream_name": name,
            "url": s.get("url"),
            "protocol": s.get("protocol", "tcp"),
            "account_id": s.get("account_id", "default"),
            "active_account_id": stream_account(name),
            "fallback_account_ids": s.get("fallback_account_ids", []),
            "watch_url": s.get("watch_url"),
            "status": "Active" if is_active else "Inactive",
            "retry": None if is_active else get_retry_status(name),
            "previous_days": cam_hist.get("days", []),
            "history_count": cam_hist.get("total_records", 0)
        })
        
    return jsonify({
        "streams": stream_statuses,
        "settings": settings,
        "accounts": public_accounts(),
        "authenticated": auth_status,
        "youtube": get_retry_status() or {"blocked": False},
        "system": {
            "cpu": psutil.cpu_percent(),
            "ram": psutil.virtual_memory().percent
        }
    })

@app.route('/api/settings', methods=['GET', 'POST'])
def api_settings():
    if request.method == 'POST':
        data = request.json or {}
        success, res = save_settings(data)
        if success:
            return jsonify({"status": "success", "settings": res, "message": "Settings updated successfully."})
        return jsonify({"status": "error", "message": res}), 500
    return jsonify(get_settings())

@app.route('/api/history', methods=['GET'])
def api_history():
    settings = get_settings()
    days_limit = request.args.get("days", default=settings.get("history_days_limit", 30), type=int)
    stream_name = request.args.get("stream_name", default=None, type=str)
    specific_date = request.args.get("date", default=None, type=str)
    include_today = request.args.get("include_today", default="false", type=str).lower() in ("true", "1")
    history_data = get_camera_history(
        stream_name=stream_name,
        days_limit=days_limit,
        include_today=include_today,
        specific_date=specific_date
    )
    return jsonify({
        "days_limit": days_limit,
        "specific_date": specific_date,
        "history": history_data
    })

@app.route('/api/history/sync', methods=['POST'])
def api_history_sync():
    settings = get_settings()
    days_limit = settings.get("history_days_limit", 30)
    results = []
    selected = [current_account.get()] if "account_id" in request.args else [a["id"] for a in accounts()]
    for account_id in selected:
        with use_account(account_id):
            count, msg = sync_history_from_youtube(days_limit=days_limit)
            results.append({"account_id": account_id, "synced_count": count, "message": msg})
    return jsonify({
        "synced_count": sum(r["synced_count"] for r in results),
        "message": " ".join(r["account_id"] + ": " + r["message"] for r in results),
        "accounts": results,
    })

@app.route('/api/stream/start', methods=['POST'])
def api_start_stream():
    data = request.json or {}
    stream_name = data.get("stream_name")
    if not stream_name:
        return jsonify({"error": "Missing stream_name"}), 400
        
    if stream_name in active_processes and active_processes[stream_name].is_alive():
        return jsonify({"message": f"Stream '{stream_name}' is already running."})
        
    streams = get_streams_from_file()
    target_stream = None
    for s in streams:
        if s.get("stream_name") == stream_name:
            target_stream = s
            break
            
    if not target_stream:
        return jsonify({"error": f"Stream '{stream_name}' not found"}), 404
        
    url = target_stream.get("url")
    playlist = "Camera San"
    
    blocked = start_stream_worker(url, stream_name, playlist)
    if blocked:
        return jsonify({"error": blocked["message"], "retry": blocked}), 429 if blocked["retry_at"] else 409
    
    return jsonify({"message": f"Stream '{stream_name}' started successfully."})


@app.route('/api/youtube/retry', methods=['POST'])
def api_youtube_retry():
    blocked = get_retry_status()
    if blocked and not blocked["manual_retry"]:
        return jsonify({"error": blocked["message"], "retry": blocked}), 429
    get_retry_guard().reset_permissions(get_retry_scope())
    return jsonify({"message": "YouTube retry enabled. Streams will resume on the next sync."})


@app.route('/api/sync', methods=['POST'])
def api_sync():
    data = request.get_json(silent=True)
    if not isinstance(data, list):
        return jsonify({"error": "Expected a list of streams"}), 400
    enabled_streams = {
        s["name"]: s for s in data
        if isinstance(s, dict) and isinstance(s.get("name"), str)
        and s["name"] and s.get("ytstream_enabled", False)
    }
    paused = []
    try:
        with active_processes_lock:
            for stream_name in list(active_processes):
                if stream_name not in enabled_streams:
                    process = active_processes.pop(stream_name)
                    was_alive = process.is_alive()
                    if was_alive:
                        process.terminate()
                    process.join(timeout=5)
                    if process.is_alive():
                        process.kill()
                        process.join(timeout=5)
                    if was_alive:
                        threading.Thread(target=end_and_privatize_stream, args=(stream_name,), daemon=True).start()

            for stream_name in enabled_streams:
                process = active_processes.get(stream_name)
                if process is not None and process.is_alive():
                    continue
                blocked = get_retry_status(stream_name)
                if blocked:
                    paused.append({"stream_name": stream_name, **blocked})
                    continue
                url = os.environ.get('LIGHTNVR_RTSP_URL', 'rtsp://127.0.0.1:8554').rstrip('/') + '/' + stream_name
                current_streams = get_streams_from_file()
                existing = next((s for s in current_streams if s.get("stream_name") == stream_name), None)
                if existing is None:
                    current_streams.append({"stream_name": stream_name, "url": url, "protocol": "tcp", "watch_url": ""})
                else:
                    existing.update({"url": url, "protocol": "tcp"})
                with open(get_config_path("stream.json"), "w", encoding="utf-8") as config:
                    json.dump(current_streams, config, indent=4)
                blocked = start_stream_worker(url, stream_name, sync_playlist_name)
                if blocked:
                    paused.append({"stream_name": stream_name, **blocked})
                else:
                    logging.info("Started stream '%s' after LightNVR sync.", stream_name)
        return jsonify({"status": "success", "synced_streams": len(enabled_streams), "paused_streams": paused})
    except Exception as error:
        logging.error("Failed to sync with LightNVR: %s", error)
        return jsonify({"status": "error", "message": str(error)}), 500

@app.route('/api/stream/stop', methods=['POST'])
def api_stop_stream():
    data = request.json or {}
    stream_name = data.get("stream_name")
    if not stream_name:
        return jsonify({"error": "Missing stream_name"}), 400
        
    p = active_processes.get(stream_name)
    if p and p.is_alive():
        p.terminate()
        p.join()
        del active_processes[stream_name]
        threading.Thread(target=end_and_privatize_stream, args=(stream_name,), daemon=True).start()
        
        # Clean up PID file if any exists
        safe_name = ''.join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in stream_name)
        pid_file = os.path.join("ffmpeg_pids", f"{safe_name}.pid")
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r") as pf:
                    pid = int(pf.read().strip())
                if psutil.pid_exists(pid):
                    proc = psutil.Process(pid)
                    if proc.name().lower().startswith("ffmpeg"):
                        proc.terminate()
                os.remove(pid_file)
            except Exception:
                pass
                
        return jsonify({"message": f"Stream '{stream_name}' stopped."})
    else:
        return jsonify({"message": f"Stream '{stream_name}' is not running."})

@app.route('/api/streams/save', methods=['POST'])
def api_save_streams():
    data = request.json or {}
    new_streams = data.get("streams")
    if new_streams is None:
        return jsonify({"error": "Missing streams data"}), 400
        
    try:
        if not isinstance(new_streams, list) or any(not isinstance(s, dict) for s in new_streams):
            raise ValueError("Expected a list of streams")
        known = {a["id"] for a in accounts()}
        old = {s["stream_name"]: s for s in get_streams_from_file()}
        merged = []
        routing_changes = []
        names = set()
        for item in new_streams:
            name = item.get("stream_name")
            if not isinstance(name, str) or not name or name in names or not isinstance(item.get("url"), str) or not item["url"]:
                raise ValueError("Unique stream names and URLs are required")
            names.add(name)
            account_id = item.get("account_id", "default")
            fallbacks = item.get("fallback_account_ids", [])
            if not isinstance(account_id, str) or account_id not in known or not isinstance(fallbacks, list) or any(not isinstance(a, str) or a not in known for a in fallbacks):
                raise ValueError("Unknown account or invalid fallback list")
            previous = old.get(name, {})
            changed = account_id != previous.get("account_id", "default") or fallbacks != previous.get("fallback_account_ids", [])
            process = active_processes.get(name)
            if changed and process is not None and process.is_alive():
                return jsonify({"error": "Stop the stream before changing its accounts"}), 409
            if changed:
                routing_changes.append((name, account_id))
                item = {**item, "watch_url": ""}
            merged.append({**previous, **item})
        for name, account_id in routing_changes:
            set_owner(name, account_id)
        atomic_json(get_config_path("stream.json"), merged)
        return jsonify({"message": "Streams configuration saved successfully."})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to save stream.json: {e}"}), 500

@app.route('/api/logs/<stream_name>')
def api_logs(stream_name):
    safe_name = ''.join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in stream_name)
    log_dir = "ffmpeg_logs"
    if not os.path.exists(log_dir):
        return jsonify({"logs": ["No logs directory found."]})
        
    log_files = [f for f in os.listdir(log_dir) if f.startswith(f"ffmpeg_{safe_name}_") and f.endswith(".log")]
    if not log_files:
        return jsonify({"logs": [f"No logs found for stream '{stream_name}'."]})
        
    log_files.sort()
    latest_log = os.path.join(log_dir, log_files[-1])
    
    try:
        with open(latest_log, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            return jsonify({"logs": lines[-100:]})
    except Exception as e:
        return jsonify({"logs": [f"Error reading log file: {e}"]})

@app.route('/api/cleanup', methods=['POST'])
def api_cleanup():
    account_id = current_account.get()
    def cleanup():
        with use_account(account_id):
            delete_old_streams()
    thread = threading.Thread(target=cleanup)
    thread.start()
    return jsonify({"message": "Cleanup process started in background."})

@app.route('/login')
def login():
    account_id = request.args.get("account_id", "default")
    if account_id not in {a["id"] for a in accounts()}:
        return jsonify({"error": "Unknown account"}), 400
    session['youtube_account'] = account_id
    current_account.set(account_id)
    oauth_path = credential_path("oauth.json")
    if not os.path.exists(oauth_path):
        return "<h3>Error: oauth.json not found. Please place client secrets in config folder.</h3>", 400
        
    flow = Flow.from_client_secrets_file(
        oauth_path,
        scopes=['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.force-ssl'],
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    session['state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    state = session.get('state')
    if not state or request.args.get('state') != state:
        return jsonify({'error': 'Invalid OAuth state'}), 400
    current_account.set(session.get('youtube_account', 'default'))
    oauth_path = credential_path("oauth.json")
    flow = Flow.from_client_secrets_file(
        oauth_path,
        scopes=['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.force-ssl'],
        state=state,
        redirect_uri=url_for('oauth2callback', _external=True)
    )
    flow.fetch_token(authorization_response=request.url)
    credentials = flow.credentials
    save_credentials(credentials)
    get_retry_guard().reset_permissions(get_retry_scope())
    session.pop("state", None)
    session.pop("youtube_account", None)
    return redirect(url_for('index'))

if __name__ == "__main__":
    # Check if delete flag is present before usage print
    if len(sys.argv) > 1 and sys.argv[1] == "--delete-old-streams":
        delete_old_streams()
        sys.exit(0)

    logging.info("Starting the video processing and upload process.")
    
    # Parse command line arguments
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python uytstream.py [directory_path] [playlist_name]")
        print("  python uytstream.py --stream [rtsp_url] [stream_name] [playlist_name]")
        print("  python uytstream.py --all-streams-from-json-- [playlist_name]")
        print("  python uytstream.py --stream-from-json [stream_name] [playlist_name]")
        print("  python uytstream.py --delete-old-streams")
        print("Examples:")
        print("  python uytstream.py . 'Custom Playlist'")
        print("  python uytstream.py --stream rtsp://your_camera_url 'My Stream Name' 'My Playlist'")
        print("  python uytstream.py --all-streams-from-json-- 'My Playlist'")
        print("  python uytstream.py --stream-from-json 'My Stream Name' 'My Playlist'")
        print("  python uytstream.py --delete-old-streams")
        print("Note: If playlist_name is not provided, 'Camera San' will be used by default")
        sys.exit(1)

    if sys.argv[1] == "--stream":
        if len(sys.argv) < 4:
            print("Usage: python uytstream.py --stream [rtsp_url] [stream_name] [playlist_name]")
            sys.exit(1)
        rtsp_url = sys.argv[2]
        stream_name = sys.argv[3]
        playlist_name = sys.argv[4] if len(sys.argv) > 4 else "Camera San"
        logging.info(f"Starting YouTube live stream from RTSP: {rtsp_url} as '{stream_name}' in playlist '{playlist_name}'")
        stream_to_youtube(rtsp_url, stream_name, playlist_name)
        sys.exit(0)

    # If called with special flag, run all streams in stream.json and start Web UI
    if sys.argv[1] == "--all-streams-from-json--":
        playlist_name = "Camera San"
        if "--playlist" in sys.argv:
            idx = sys.argv.index("--playlist")
            if len(sys.argv) > idx + 1:
                playlist_name = sys.argv[idx + 1]
        elif len(sys.argv) > 2:
            playlist_name = sys.argv[2]
        sync_playlist_name = playlist_name
        import multiprocessing
        import threading
        
        port = int(os.environ.get("PORT", 8081))
        logging.info(f"Starting Web UI server on port {port}...")
        import signal
        def shutdown_server(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, shutdown_server)
        try:
            app.run(host=os.environ.get('YTSTREAM_BIND_HOST', '127.0.0.1'), port=port)
        except KeyboardInterrupt:
            logging.info("Shutting down Flask server. Terminating child processes...")
        finally:
            for p in list(active_processes.values()):
                if p.is_alive():
                    p.terminate()
            for p in list(active_processes.values()):
                p.join(timeout=5)
                if p.is_alive():
                    p.kill()
                    p.join(timeout=5)
        sys.exit(0)

    if sys.argv[1] == "--stream-from-json":
        if len(sys.argv) < 3:
            print("Usage: python uytstream.py --stream-from-json [stream_name ...] [--playlist playlist_name]")
            sys.exit(1)
        # Accept multiple stream names, optionally followed by --playlist playlist_name
        streams = get_streams_from_file()
        all_stream_names = [s.get("stream_name") for s in streams if s.get("stream_name")]
        args = sys.argv[2:]
        playlist_name = "Camera San"
        if "--playlist" in args:
            idx = args.index("--playlist")
            playlist_name = args[idx + 1] if len(args) > idx + 1 else "Camera San"
            stream_names = args[:idx]
        else:
            stream_names = args
        if not stream_names:
            print("No valid stream names provided.")
            sys.exit(1)
        import multiprocessing
        processes = []
        found_any = False
        for target_stream_name in stream_names:
            found = False
            for s in streams:
                if s.get("stream_name") == target_stream_name:
                    url = s.get("url")
                    if not url:
                        print(f"No URL found for stream_name '{target_stream_name}' in stream.json")
                        continue
                    logging.info(f"Starting YouTube live stream from stream.json: {url} as '{target_stream_name}' in playlist '{playlist_name}'")
                    p = multiprocessing.Process(target=run_stream, args=(url, target_stream_name, playlist_name))
                    p.start()
                    processes.append(p)
                    found = True
                    found_any = True
                    break
            if not found:
                print(f"stream_name '{target_stream_name}' not found in stream.json")
        if not found_any:
            sys.exit(1)
        try:
            for p in processes:
                p.join()
        except KeyboardInterrupt:
            logging.info("Main script interrupted by KeyboardInterrupt. Terminating child processes...")
            for p in processes:
                p.terminate()
            for p in processes:
                p.join()
        sys.exit(0)

    video_directory = sys.argv[1]
    playlist_name = sys.argv[2] if len(sys.argv) > 2 else "Camera San"
    
    logging.info(f"Video directory set to: {video_directory}")
    logging.info(f"Videos will be added to playlist: {playlist_name}")
    
    large_count, medium_count, skipped_count = process_videos_in_directory(video_directory, playlist_name)
    
    logging.info("Processing complete:")
    logging.info(f"- {large_count} large videos uploaded directly")
    logging.info(f"- {medium_count} medium videos merged and uploaded")
    logging.info(f"- {skipped_count} small videos skipped")
