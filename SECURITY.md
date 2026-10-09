# Security

PickBench is a local tool. `server.py` listens on `127.0.0.1` only, has no login and is not meant to be reachable from a network.

## What the server protects against

- Requests from other web pages: the `Host` and `Origin` headers must be local, and the page cannot be shown in a frame.
- Leaking provider keys: keys are read from the environment only and are never returned in a response, an error message, a log line or the database. Tests guard this.

## What it does not protect against

- Other programs and users on the same computer. Anything that can open a connection to the port can call the server, including `POST /decide`, which
  sends hosted requests (and spends money at the provider) up to the cap set by `PICKBENCH_MAX_USD` and `PICKBENCH_MAX_HOSTED`.
- A port that you forward or expose yourself.

## Reporting a problem

Please report a vulnerability privately through GitHub: the **Security** tab of the repository, **Report a vulnerability**. Do not include real provider
keys in a report.
