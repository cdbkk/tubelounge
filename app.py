import asyncio
import ipaddress
import json
import math
import os
import re
import socket
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, StrictFloat, StrictInt
from pyytlounge import YtLoungeApi
from samsungtvws import SamsungTVWS


AUTH_PATH = Path(
    os.environ.get("TVCC_AUTH_PATH", "~/.cache/ytcast/ytcast.json")
).expanduser()
STATIC_PATH = Path(__file__).with_name("static")
DESIGNS_PATH = Path(__file__).with_name("designs")
TV_CONFIG_PATH = Path(
    os.environ.get(
        "SAMSUNG_TV_CONFIG_PATH",
        "~/.config/tv-command-center/samsung.json",
    )
).expanduser()
TV_TOKEN_PATH = Path(
    os.environ.get(
        "SAMSUNG_TV_TOKEN_PATH",
        "~/.cache/tv-command-center/samsung-token.txt",
    )
).expanduser()
ORIGIN_PATTERN = re.compile(
    r"^(?:chrome-extension://[a-p]{32}|https?://(?:localhost|127\.0\.0\.1)(?::\d{1,5})?)$"
)
VIDEO_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
REMOTE_KEYS = {
    "up": "KEY_UP",
    "down": "KEY_DOWN",
    "left": "KEY_LEFT",
    "right": "KEY_RIGHT",
    "enter": "KEY_ENTER",
    "back": "KEY_RETURN",
    "home": "KEY_HOME",
    "source": "KEY_SOURCE",
    "volume_up": "KEY_VOLUP",
    "volume_down": "KEY_VOLDOWN",
    "mute": "KEY_MUTE",
    "channel_up": "KEY_CHUP",
    "channel_down": "KEY_CHDOWN",
    "play_pause": "KEY_PLAYPAUSE",
    "power_off": "KEY_POWEROFF",
}
TV_APPS = {
    "youtube": "111299001912",
    "netflix": "3201907018807",
    "max": "3202301029760",
}


def validate_tv_host(host):
    try:
        address = ipaddress.ip_address(host.strip())
    except ValueError:
        raise ValueError("TV IP must be a valid IP address") from None
    if address.version != 4 or not address.is_private:
        raise ValueError("TV IP must be a private IPv4 address")
    return str(address)


def validate_tv_mac(mac):
    compact = mac.strip().replace(":", "").replace("-", "")
    if len(compact) != 12 or any(char not in "0123456789abcdefABCDEF" for char in compact):
        raise ValueError("TV MAC must contain six hexadecimal bytes")
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2)).lower()


def _load_tv_config():
    config = {}
    try:
        config.update(json.loads(TV_CONFIG_PATH.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        pass
    if os.environ.get("SAMSUNG_TV_IP"):
        config["host"] = os.environ["SAMSUNG_TV_IP"]
    if os.environ.get("SAMSUNG_TV_MAC"):
        config["mac"] = os.environ["SAMSUNG_TV_MAC"]
    try:
        return {
            "host": validate_tv_host(config["host"]),
            "mac": validate_tv_mac(config["mac"]),
        }
    except (KeyError, TypeError, ValueError):
        return {"host": "", "mac": ""}


tv_config = _load_tv_config()


def _initial_state():
    return {
        "connected": False,
        "error": None,
        "playback": {
            "video_id": "",
            "state": "stopped",
            "current_time": 0.0,
            "duration": 0.0,
        },
        "remote": {
            "configured": bool(tv_config["host"] and tv_config["mac"]),
            "approved": TV_TOKEN_PATH.exists(),
            "host": tv_config["host"],
            "mac": tv_config["mac"],
            "status": (
                "unconfigured"
                if not (tv_config["host"] and tv_config["mac"])
                else ("paired" if TV_TOKEN_PATH.exists() else "approval_required")
            ),
            "error": None,
        },
        "queue": [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


state = _initial_state()
lounge = None
subscription = None
sockets = set()
command_lock = asyncio.Lock()
remote_lock = asyncio.Lock()
tv_remote = None


class PairingError(Exception):
    pass


class UrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str


class QueueItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    video_id: str


class ControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    value: StrictFloat | StrictInt | None = None


class RemoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str


class RemoteAppRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app: str


class RemoteConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    mac: str


def extract_video_id(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower().rstrip(".")
    except (AttributeError, ValueError):
        raise ValueError("Invalid YouTube URL") from None

    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Invalid YouTube URL")

    parts = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be" and parts:
        video_id = parts[0]
    elif host in YOUTUBE_HOSTS and parsed.path.rstrip("/") == "/watch":
        video_id = parse_qs(parsed.query).get("v", [""])[0]
    elif host in YOUTUBE_HOSTS and len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        video_id = parts[1]
    else:
        raise ValueError("Invalid YouTube URL")

    return validate_video_id(video_id)


def validate_video_id(video_id: str) -> str:
    if len(video_id) != 11 or any(char not in VIDEO_ID_CHARS for char in video_id):
        raise ValueError("Invalid YouTube video ID")
    return video_id


def _queue_item(video_id):
    return {
        "video_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "thumbnail": f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
    }


def _touch(error=None):
    state["error"] = error
    state["updated_at"] = datetime.now(timezone.utc).isoformat()


async def _broadcast():
    if not sockets:
        return
    peers = tuple(sockets)
    results = await asyncio.gather(
        *(socket.send_json(state) for socket in peers), return_exceptions=True
    )
    for socket, result in zip(peers, results):
        if isinstance(result, Exception):
            sockets.discard(socket)


async def _on_playback(playback):
    video_id = playback.videoId
    state["playback"].update(
        video_id=video_id,
        state=getattr(playback.state, "name", str(playback.state)).lower(),
        current_time=playback.currentTime,
        duration=playback.duration,
    )
    if video_id:
        ids = [item["video_id"] for item in state["queue"]]
        if video_id in ids:
            state["queue"] = state["queue"][ids.index(video_id) :]
        else:
            state["queue"] = [_queue_item(video_id)]
    _touch()
    await _broadcast()


async def _subscribe(client):
    error = "Live updates stopped"
    try:
        await client.subscribe(_on_playback)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = f"Live updates failed ({type(exc).__name__})"
    state["connected"] = False
    _touch(error)
    await _broadcast()


def _load_auth(client):
    try:
        cache = json.loads(AUTH_PATH.read_text())
        remote = cache[0]["Remote"]
        screen_id = remote["ScreenId"]
        lounge_token = remote["LoungeToken"]
    except FileNotFoundError:
        raise PairingError(
            f"Pairing cache not found at {AUTH_PATH}. Pair the TV with ytcast first."
        ) from None
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        raise PairingError(
            f"Pairing cache at {AUTH_PATH} is invalid. Pair the TV with ytcast again."
        ) from None
    client.load_auth_state(
        {
            "version": 0,
            "screen_id": screen_id,
            "lounge_id_token": lounge_token,
            "refresh_token": None,
            "expiry": remote.get("Expiration"),
        }
    )


async def _connect(client):
    if not client.linked() and client.paired():
        await client.refresh_auth()
    connected = await client.connect()
    if not connected and client.paired():
        await client.refresh_auth()
        connected = await client.connect()
    return connected


async def _restart_subscription():
    global subscription
    if subscription:
        subscription.cancel()
        with suppress(asyncio.CancelledError):
            await subscription
    subscription = asyncio.create_task(_subscribe(lounge))


async def _ensure_connected():
    if lounge is None:
        return False
    if state["connected"] and lounge.connected():
        return True
    if not await _connect(lounge):
        return False
    state["connected"] = True
    _touch()
    await _restart_subscription()
    return True


def _wake_tv():
    if not tv_config["mac"]:
        raise ValueError("Configure the Samsung TV first")
    mac = bytes.fromhex(tv_config["mac"].replace(":", ""))
    packet = b"\xff" * 6 + mac * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wake_socket:
        wake_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        wake_socket.sendto(packet, ("255.255.255.255", 9))


def _close_tv_remote():
    global tv_remote
    if tv_remote:
        tv_remote.close()
        tv_remote = None


def _get_tv_remote():
    global tv_remote
    if not tv_config["host"]:
        raise ValueError("Configure the Samsung TV first")
    TV_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    if tv_remote is None:
        tv_remote = SamsungTVWS(
            host=tv_config["host"],
            port=8002,
            token_file=str(TV_TOKEN_PATH),
            timeout=15,
            key_press_delay=0.08,
            name="TV Command Center",
        )
    return tv_remote


def _call_tv_remote(method, *args):
    try:
        return getattr(_get_tv_remote(), method)(*args)
    except Exception:
        _close_tv_remote()
        raise


async def _send_remote_action(action):
    action = action.strip().lower()
    if action == "power_on":
        await asyncio.to_thread(_wake_tv)
        state["remote"].update(status="waking", error=None)
        return
    key = REMOTE_KEYS.get(action)
    if key is None:
        raise ValueError("Unknown remote action")
    await asyncio.to_thread(_call_tv_remote, "send_key", key)
    state["remote"].update(
        status="ready",
        approved=TV_TOKEN_PATH.exists(),
        error=None,
    )


async def _launch_tv_app(app_name):
    app_id = TV_APPS.get(app_name.strip().lower())
    if app_id is None:
        raise ValueError("Unknown Samsung TV app")
    await asyncio.to_thread(_call_tv_remote, "run_app", app_id)
    state["remote"].update(
        status="ready",
        approved=TV_TOKEN_PATH.exists(),
        error=None,
    )


async def _test_tv_remote():
    await asyncio.to_thread(_call_tv_remote, "open")
    state["remote"].update(
        status="ready",
        approved=TV_TOKEN_PATH.exists(),
        error=None,
    )


def _save_tv_config(host, mac):
    TV_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    pending = TV_CONFIG_PATH.with_suffix(".tmp")
    pending.write_text(json.dumps({"host": host, "mac": mac}, indent=2) + "\n")
    pending.replace(TV_CONFIG_PATH)
    tv_config.update(host=host, mac=mac)


@asynccontextmanager
async def lifespan(_app):
    global lounge, subscription, tv_remote
    state.clear()
    state.update(_initial_state())
    sockets.clear()
    lounge = YtLoungeApi("TV Command Center")
    entered = False
    try:
        await lounge.__aenter__()
        entered = True
        _load_auth(lounge)
        connected = await _connect(lounge)
        if not connected:
            raise RuntimeError("TV unavailable")
        state["connected"] = True
        _touch()
        subscription = asyncio.create_task(_subscribe(lounge))
    except Exception as exc:
        error = str(exc) if isinstance(exc, PairingError) else f"Connection failed ({type(exc).__name__})"
        _touch(error)

    yield

    if subscription:
        subscription.cancel()
        with suppress(asyncio.CancelledError):
            await subscription
        subscription = None
    if entered:
        with suppress(Exception):
            await lounge.__aexit__(None, None, None)
    lounge = None
    if tv_remote:
        with suppress(Exception):
            await asyncio.to_thread(tv_remote.close)
        tv_remote = None


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=ORIGIN_PATTERN.pattern,
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)


async def _send(method, *args):
    error = "TV rejected the command"
    for _ in range(2):
        try:
            if not await _ensure_connected():
                break
            if await getattr(lounge, method)(*args):
                return
        except Exception as exc:
            error = f"Command failed ({type(exc).__name__})"
        state["connected"] = False
    _touch(error)
    await _broadcast()
    raise HTTPException(503, error)


@app.get("/state")
async def get_state():
    return state


@app.websocket("/ws")
async def websocket_state(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    if origin and not ORIGIN_PATTERN.fullmatch(origin):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    sockets.add(websocket)
    await websocket.send_json(state)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        sockets.discard(websocket)


@app.post("/queue")
async def queue_video(request: UrlRequest):
    try:
        video_id = extract_video_id(request.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    async with command_lock:
        await _send("_command", "addVideo", {"videoId": video_id})
        state["queue"].append(_queue_item(video_id))
        _touch()
        await _broadcast()
        return state


@app.post("/play")
async def play_video(request: UrlRequest):
    try:
        video_id = extract_video_id(request.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    async with command_lock:
        await _send("play_video", video_id)
        state["queue"] = [_queue_item(video_id)]
        state["playback"].update(
            video_id=video_id,
            state="starting",
            current_time=0.0,
            duration=0.0,
        )
        _touch()
        await _broadcast()
        return state


@app.post("/queue/remove")
async def remove_queue_video(request: QueueItemRequest):
    try:
        validate_video_id(request.video_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    async with command_lock:
        current_id = state["playback"]["video_id"]
        if not current_id:
            raise HTTPException(409, "Cannot rebuild the TV queue without a current video")

        queue = state["queue"]
        start = 1 if queue and queue[0]["video_id"] == current_id else 0
        index = next(
            (
                i
                for i in range(start, len(queue))
                if queue[i]["video_id"] == request.video_id
            ),
            None,
        )
        if index is None:
            raise HTTPException(404, "Video is not in the upcoming queue")

        remaining = queue[:index] + queue[index + 1 :]
        video_ids = [item["video_id"] for item in remaining]
        if not video_ids or video_ids[0] != current_id:
            video_ids.insert(0, current_id)
        await _send(
            "_command",
            "setPlaylist",
            {
                "videoId": current_id,
                "videoIds": ",".join(video_ids),
                "currentIndex": 0,
                "currentTime": str(int(state["playback"]["current_time"])),
            },
        )
        state["queue"] = [_queue_item(video_id) for video_id in video_ids]
        _touch()
        await _broadcast()
        if state["playback"]["state"] == "paused":
            await _send("pause")
        return state


@app.post("/control")
async def control(request: ControlRequest):
    action = request.action.strip().lower()
    value = request.value
    if action == "prev":
        action = "previous"

    async with command_lock:
        if action in {"play", "pause", "next", "previous", "skip_ad"}:
            if value is not None:
                raise HTTPException(422, f"{action} does not accept a value")
            await _send(action)
            if action in {"play", "pause"}:
                state["playback"]["state"] = "playing" if action == "play" else "paused"
        elif action == "seek":
            if value is None or not math.isfinite(value) or value < 0:
                raise HTTPException(422, "seek requires non-negative seconds")
            await _send("seek_to", float(value))
            state["playback"]["current_time"] = float(value)
        elif action == "volume":
            if value is None or not math.isfinite(value) or not float(value).is_integer() or not 0 <= value <= 100:
                raise HTTPException(422, "volume requires an integer from 0 to 100")
            await _send("set_volume", int(value))
            state["playback"]["volume"] = int(value)
        else:
            raise HTTPException(422, "Unknown control action")

        _touch()
        await _broadcast()
        return state


@app.post("/remote")
async def remote_control(request: RemoteRequest):
    async with remote_lock:
        try:
            await _send_remote_action(request.action)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            error = f"Samsung TV unavailable or awaiting approval ({type(exc).__name__})"
            state["remote"].update(status="offline", error=error)
            _touch()
            await _broadcast()
            raise HTTPException(
                503,
                error,
            ) from None
        _touch()
        await _broadcast()
        return state


@app.post("/remote/app")
async def remote_app(request: RemoteAppRequest):
    async with remote_lock:
        try:
            await _launch_tv_app(request.app)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            error = f"Could not launch the Samsung TV app ({type(exc).__name__})"
            state["remote"].update(status="offline", error=error)
            _touch()
            await _broadcast()
            raise HTTPException(503, error) from None
        _touch()
        await _broadcast()
        return state


@app.post("/remote/test")
async def remote_test():
    async with remote_lock:
        try:
            await _test_tv_remote()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            error = f"Samsung TV unavailable or awaiting approval ({type(exc).__name__})"
            state["remote"].update(status="offline", error=error)
            _touch()
            await _broadcast()
            raise HTTPException(503, error) from None
        _touch()
        await _broadcast()
        return state


@app.post("/remote/config")
async def remote_config(request: RemoteConfigRequest):
    try:
        host = validate_tv_host(request.host)
        mac = validate_tv_mac(request.mac)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    async with remote_lock:
        changed_tv = host != tv_config["host"]
        await asyncio.to_thread(_close_tv_remote)
        if changed_tv:
            TV_TOKEN_PATH.unlink(missing_ok=True)
        try:
            _save_tv_config(host, mac)
        except OSError as exc:
            raise HTTPException(500, f"Could not save TV settings ({type(exc).__name__})") from None
        state["remote"].update(
            configured=True,
            approved=TV_TOKEN_PATH.exists(),
            host=host,
            mac=mac,
            status="paired" if TV_TOKEN_PATH.exists() else "approval_required",
            error=None,
        )
        _touch()
        await _broadcast()
        return state


@app.post("/remote/forget")
async def remote_forget():
    async with remote_lock:
        await asyncio.to_thread(_close_tv_remote)
        TV_TOKEN_PATH.unlink(missing_ok=True)
        state["remote"].update(
            approved=False,
            status="approval_required" if state["remote"]["configured"] else "unconfigured",
            error=None,
        )
        _touch()
        await _broadcast()
        return state


app.mount(
    "/designs",
    StaticFiles(directory=DESIGNS_PATH, html=True, check_dir=False),
    name="designs",
)
app.mount("/", StaticFiles(directory=STATIC_PATH, html=True, check_dir=False), name="static")
