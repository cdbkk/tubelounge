import asyncio
import json

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

import app


VIDEO_A = "dQw4w9WgXcQ"
VIDEO_B = "M7lc1UVf-VE"


class FakeLounge:
    def __init__(self):
        self.calls = []
        self.fail_once = False
        self.raise_once = False
        self.is_linked = True
        self.is_connected = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def load_auth_state(self, auth):
        self.auth = auth

    async def connect(self):
        self.calls.append(("connect",))
        self.is_connected = True
        return True

    def connected(self):
        return self.is_connected

    def paired(self):
        return True

    def linked(self):
        return self.is_linked

    async def refresh_auth(self):
        self.calls.append(("refresh_auth",))
        self.is_linked = True
        return True

    async def subscribe(self, _callback):
        await asyncio.Event().wait()

    async def _command(self, command, parameters):
        self.calls.append((command, parameters))
        if self.raise_once:
            self.raise_once = False
            self.is_connected = False
            raise RuntimeError("stale session")
        if self.fail_once:
            self.fail_once = False
            self.is_linked = False
            self.is_connected = False
            return False
        return True

    async def play_video(self, video_id):
        self.calls.append(("play_video", video_id))
        return True

    def __getattr__(self, name):
        async def command(*args):
            self.calls.append((name, *args))
            return True

        return command


@pytest.fixture
def client(tmp_path, monkeypatch):
    fake = FakeLounge()
    auth_path = tmp_path / "ytcast.json"
    auth_path.write_text(
        json.dumps([{"Remote": {"ScreenId": "screen", "LoungeToken": "token"}}])
    )
    monkeypatch.setattr(app, "AUTH_PATH", auth_path)
    monkeypatch.setattr(app, "YtLoungeApi", lambda _name: fake)
    with TestClient(app.app) as test_client:
        yield test_client, fake


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://youtu.be/{VIDEO_A}?t=3", VIDEO_A),
        (f"https://www.youtube.com/watch?v={VIDEO_A}&list=RD{VIDEO_A}", VIDEO_A),
        (f"https://youtube.com/shorts/{VIDEO_A}", VIDEO_A),
        (f"https://youtube.com/embed/{VIDEO_A}", VIDEO_A),
        (f"https://youtube.com/live/{VIDEO_A}?feature=share", VIDEO_A),
    ],
)
def test_extract_video_id(url, expected):
    assert app.extract_video_id(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        f"https://example.com/watch?v={VIDEO_A}",
        "https://youtube.com/watch?v=too-short",
        f"javascript:https://youtube.com/watch?v={VIDEO_A}",
    ],
)
def test_extract_video_id_rejects_invalid_urls(url):
    with pytest.raises(ValueError):
        app.extract_video_id(url)


def test_queue_adds_without_replacing(client):
    test_client, fake = client
    response = test_client.post("/queue", json={"url": f"https://youtu.be/{VIDEO_A}"})

    assert response.status_code == 200
    assert fake.calls == [("connect",), ("addVideo", {"videoId": VIDEO_A})]
    assert response.json()["queue"][0]["video_id"] == VIDEO_A


def test_play_replaces_queue(client):
    test_client, fake = client
    test_client.post("/queue", json={"url": f"https://youtu.be/{VIDEO_A}"})
    response = test_client.post("/play", json={"url": f"https://youtu.be/{VIDEO_B}"})

    assert response.status_code == 200
    assert fake.calls[-1] == ("play_video", VIDEO_B)
    assert [item["video_id"] for item in response.json()["queue"]] == [VIDEO_B]
    assert response.json()["playback"]["video_id"] == VIDEO_B
    assert response.json()["playback"]["state"] == "starting"


def test_remove_rebuilds_tv_queue_and_preserves_pause(client):
    test_client, fake = client
    app.state["playback"].update(
        video_id=VIDEO_A,
        state="paused",
        current_time=12.8,
        duration=100,
    )
    app.state["queue"] = [app._queue_item(VIDEO_B)]
    fake.calls.clear()

    response = test_client.post("/queue/remove", json={"video_id": VIDEO_B})

    assert response.status_code == 200
    assert fake.calls == [
        (
            "setPlaylist",
            {
                "videoId": VIDEO_A,
                "videoIds": VIDEO_A,
                "currentIndex": 0,
                "currentTime": "12",
            },
        ),
        ("pause",),
    ]
    assert [item["video_id"] for item in response.json()["queue"]] == [VIDEO_A]


def test_remove_rejects_unknown_upcoming_video(client):
    test_client, fake = client
    app.state["playback"]["video_id"] = VIDEO_A
    app.state["queue"] = [app._queue_item(VIDEO_A)]
    fake.calls.clear()

    response = test_client.post("/queue/remove", json={"video_id": VIDEO_B})

    assert response.status_code == 404
    assert fake.calls == []


def test_remove_rejects_invalid_video_id(client):
    test_client, fake = client
    fake.calls.clear()

    response = test_client.post("/queue/remove", json={"video_id": "bad"})

    assert response.status_code == 422
    assert fake.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "unknown"},
        {"action": "play", "value": 1},
        {"action": "seek"},
        {"action": "seek", "value": -1},
        {"action": "volume"},
        {"action": "volume", "value": 12.5},
        {"action": "volume", "value": 101},
    ],
)
def test_control_validation(client, payload):
    test_client, fake = client

    assert test_client.post("/control", json=payload).status_code == 422
    assert fake.calls == [("connect",)]


def test_control_dispatches_alias_and_values(client):
    test_client, fake = client

    assert test_client.post("/control", json={"action": "prev"}).status_code == 200
    assert test_client.post("/control", json={"action": "seek", "value": 12.5}).status_code == 200
    assert test_client.post("/control", json={"action": "volume", "value": 42}).status_code == 200
    assert fake.calls == [
        ("connect",),
        ("previous",),
        ("seek_to", 12.5),
        ("set_volume", 42),
    ]


def test_remote_dispatches_tv_action(client, monkeypatch):
    test_client, _fake = client
    actions = []

    async def fake_remote_action(action):
        actions.append(action)

    monkeypatch.setattr(app, "_send_remote_action", fake_remote_action)

    response = test_client.post("/remote", json={"action": "home"})

    assert response.status_code == 200
    assert actions == ["home"]


def test_remote_rejects_unknown_action(client):
    test_client, _fake = client

    response = test_client.post("/remote", json={"action": "launch_missiles"})

    assert response.status_code == 422
    assert response.json()["detail"] == "Unknown remote action"


def test_remote_launches_named_tv_app(client, monkeypatch):
    test_client, _fake = client
    launched = []

    async def fake_launch(app_name):
        launched.append(app_name)

    monkeypatch.setattr(app, "_launch_tv_app", fake_launch)

    response = test_client.post("/remote/app", json={"app": "max"})

    assert response.status_code == 200
    assert launched == ["max"]


def test_remote_rejects_unknown_tv_app(client):
    test_client, _fake = client

    response = test_client.post("/remote/app", json={"app": "not-installed"})

    assert response.status_code == 422
    assert response.json()["detail"] == "Unknown Samsung TV app"


def test_remote_config_validates_and_saves(client, monkeypatch, tmp_path):
    test_client, _fake = client
    token_path = tmp_path / "token.txt"
    saved = {}

    def fake_save(host, mac):
        saved.update(host=host, mac=mac)

    monkeypatch.setattr(app, "TV_TOKEN_PATH", token_path)
    monkeypatch.setattr(app, "_save_tv_config", fake_save)

    response = test_client.post(
        "/remote/config",
        json={"host": "192.168.1.88", "mac": "AA-BB-CC-DD-EE-FF"},
    )

    assert response.status_code == 200
    assert saved == {"host": "192.168.1.88", "mac": "aa:bb:cc:dd:ee:ff"}
    assert response.json()["remote"]["status"] == "approval_required"


def test_remote_config_rejects_public_ip(client):
    test_client, _fake = client

    response = test_client.post(
        "/remote/config",
        json={"host": "8.8.8.8", "mac": "aa:bb:cc:dd:ee:ff"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "TV IP must be a private IPv4 address"


def test_command_reconnects_and_retries_once(client):
    test_client, fake = client
    fake.fail_once = True

    response = test_client.post("/queue", json={"url": f"https://youtu.be/{VIDEO_A}"})

    assert response.status_code == 200
    assert fake.calls == [
        ("connect",),
        ("addVideo", {"videoId": VIDEO_A}),
        ("refresh_auth",),
        ("connect",),
        ("addVideo", {"videoId": VIDEO_A}),
    ]


def test_command_recovers_after_disconnected_state(client):
    test_client, fake = client
    fake.calls.clear()
    fake.is_connected = False
    app.state["connected"] = False

    response = test_client.post("/queue", json={"url": f"https://youtu.be/{VIDEO_A}"})

    assert response.status_code == 200
    assert fake.calls == [("connect",), ("addVideo", {"videoId": VIDEO_A})]


def test_command_reconnects_after_exception(client):
    test_client, fake = client
    fake.calls.clear()
    fake.raise_once = True

    response = test_client.post("/queue", json={"url": f"https://youtu.be/{VIDEO_A}"})

    assert response.status_code == 200
    assert fake.calls == [
        ("addVideo", {"videoId": VIDEO_A}),
        ("connect",),
        ("addVideo", {"videoId": VIDEO_A}),
    ]


def test_subscribe_clean_exit_marks_disconnected():
    class EndingLounge:
        async def subscribe(self, _callback):
            pass

    app.state.update(app._initial_state())
    app.state["connected"] = True

    asyncio.run(app._subscribe(EndingLounge()))

    assert app.state["connected"] is False
    assert app.state["error"] == "Live updates stopped"


def test_websocket_rejects_untrusted_origin(client):
    test_client, _fake = client

    with pytest.raises(WebSocketDisconnect) as exc:
        with test_client.websocket_connect(
            "/ws", headers={"origin": "https://example.com"}
        ):
            pass

    assert exc.value.code == 1008


def test_missing_pairing_cache_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "AUTH_PATH", tmp_path / "missing.json")

    with pytest.raises(app.PairingError, match="Pair the TV with ytcast first"):
        app._load_auth(FakeLounge())
