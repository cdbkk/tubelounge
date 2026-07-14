# TV Command Center

Local web remote and queue mirror for the paired Samsung YouTube TV app.

## Pair and run

Pair the TV once using the code shown in the YouTube TV app:

```bash
ytcast -pair "TV-CODE"
```

The server reuses the pairing in `~/.cache/ytcast/ytcast.json`. Set
`TVCC_AUTH_PATH=/path/to/ytcast.json` if your cache lives elsewhere.

```bash
cd ~/code/tv-command-center
uv run --with-requirements requirements.txt uvicorn app:app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>.

Keep the server bound to `127.0.0.1`. It intentionally has no login because it
is a single-user local remote; any process that can reach it can control the TV.
Do not expose it on `0.0.0.0` or forward port `8765` to another machine.

The UI supports play/pause, previous/next, seek, volume, play-now, queue-add,
queue removal, live playback state, and an app-side queue mirror. Removing an
item rebuilds the mirrored TV playlist while preserving playback position.
The mirror remains best-effort because Lounge does not expose the full playlist.

## Samsung Wi-Fi remote

The remote panel controls a modern Samsung Tizen TV directly over its local
WebSocket API. Open **TV setup**, enter the TV's IP and Wi-Fi MAC, save, then
choose **Test / approve**. Settings are stored outside the repository at
`~/.config/tv-command-center/samsung.json`.

Environment variables are also supported for unattended installs:

```bash
SAMSUNG_TV_IP=192.168.1.50 \
SAMSUNG_TV_MAC=aa:bb:cc:dd:ee:ff \
uv run --with-requirements requirements.txt uvicorn app:app --host 127.0.0.1 --port 8765
```

The first button press may show an approval prompt on the TV. Accept it once;
the token is saved outside the repository at
`~/.cache/tv-command-center/samsung-token.txt`. **Wake** uses Wake-on-LAN;
the other buttons use Samsung's TLS WebSocket API on port `8002`. Both devices
must be on the same LAN.

YouTube, Netflix, and Max launcher buttons use their current Samsung app IDs.
Samsung notes that IDs can vary by TV year and firmware, so a button can fail
if that app is not installed or uses a different regional ID.

## Chrome extension

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Click **Load unpacked** and choose `~/code/tv-command-center/extension`.
4. Right-click a YouTube link or page and choose **Add to TV queue**.

The local server must be running on port `8765`.

## Check

```bash
uv run --with-requirements requirements.txt --with pytest --with httpx python -m pytest -q
node --check static/app.js
node --check extension/background.js
python3 -m json.tool extension/manifest.json >/dev/null
```

YouTube Lounge is private and unversioned. If Google changes it, the UI will
surface the failure instead of crashing. A missing, expired, or invalid cache
produces a pairing error; link the TV again with `ytcast -pair`.
