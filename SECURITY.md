# Security Policy

## Scope

TubeLounge is a **local, single-user** app with no authentication. It is
designed to run bound to `127.0.0.1` only. Do not expose it on `0.0.0.0` or
forward its port to the internet. Doing so gives anyone on the network full
control of your TV and queue.

## Reporting a vulnerability

Please report vulnerabilities privately via GitHub's
[Report a vulnerability](https://github.com/cdbkk/tubelounge/security/advisories/new)
flow rather than opening a public issue.

Include what you can reproduce and the impact. Expect an initial response
within a few days. There is no bounty, this is a hobby project.

## Out of scope

- Attacks that require the server to already be exposed beyond `127.0.0.1`
  (that is a documented misconfiguration, not a vulnerability).
- The Samsung Tizen and YouTube Lounge APIs themselves, those are Samsung's
  and Google's, private and unversioned.
