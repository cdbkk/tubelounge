# Contributing

Thanks for your interest. This is a small single-purpose local app, so the bar
is simple: keep it lean, keep it working.

## Setup

```
uv run --with-requirements requirements.txt uvicorn app:app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>. Keep the server bound to `127.0.0.1` — it is a
single-user local remote and has no auth. Do not expose it on `0.0.0.0` or
forward the port.

## Checks

Run these before opening a PR:

```
uv run --with-requirements requirements.txt --with pytest --with httpx python -m pytest -q
node --check static/app.js
node --check extension/background.js
python3 -m json.tool extension/manifest.json >/dev/null
```

## Pull requests

- One change per PR. Small diffs get merged; large refactors stall.
- Add or update a test if you change backend behavior.
- No new dependencies unless a few lines can't do the job.
- The YouTube Lounge API is private and unversioned; if you touch that path,
  make failures surface in the UI rather than crash.

## Reporting bugs / ideas

Open an issue using the templates. For anything security-sensitive, see
[SECURITY.md](SECURITY.md).
