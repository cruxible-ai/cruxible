# Isolated Deployment

This guide is the advanced path for users who want a real runtime boundary between the agent and the Cruxible graph.

It is not the default onboarding flow. The normal local setup is still the fastest way to try Cruxible, but local same-user setups are a convenience mode, not a strong isolation boundary.

## What This Guide Is For

Use this guide if you want the agent to interact with Cruxible without being able to:

- import `cruxible-core` directly
- read the graph files directly
- bypass daemon permission modes by reaching into the runtime

If that is not a requirement, stick with the standard [Quickstart](quickstart.md).

## What Creates a Real Boundary

At minimum, all of these need to be true:

- `cruxible-core` runs as a different principal or on a different host
- the graph state directory is readable only by that runtime principal
- the agent environment installs only `cruxible-client`
- the agent talks to Cruxible over HTTP or a Unix socket, not through shared filesystem access
- the agent cannot escalate into the runtime environment through `sudo`, Docker, SSH, or shared source checkout access

If the agent can read the instance files, import the runtime source, or control Docker on the runtime host, the boundary is not real.

## What Does Not Create a Real Boundary

These are useful for convenience, but they are not sufficient for graph isolation by themselves:

- separate `uv` environments only
- Docker on the same machine if the agent can run `docker`
- a named Docker volume if the agent can mount or inspect it
- keeping `cruxible-core` installed in the agent environment
- a shared repo checkout visible to both the agent and the runtime
- running the agent and the daemon as the same Unix user

## Recommended Patterns

There are two practical ways to isolate the runtime:

- same machine, separate Unix user
- separate host or VM

The second is stronger. The first is often enough for local or internal setups.

## Option 1: Same Machine, Separate Unix User

This is the smallest setup that creates a meaningful local wall.

### 1. Create a dedicated runtime user

```bash
sudo useradd --system --create-home --home-dir /var/lib/cruxible cruxd
sudo mkdir -p /var/lib/cruxible
sudo chown -R cruxd:cruxd /var/lib/cruxible
sudo chmod 700 /var/lib/cruxible
```

Use a state directory the agent user cannot read. This example uses `/var/lib/cruxible`, but any directory owned only by the runtime user is fine.

### 2. Install the daemon runtime for that user

```bash
sudo -u cruxd python3 -m venv /opt/cruxible-core
sudo -u cruxd /opt/cruxible-core/bin/pip install "cruxible-core[server]"
```

### 3. Start the server as the runtime user

```bash
sudo -u cruxd env \
  CRUXIBLE_SERVER_STATE_DIR=/var/lib/cruxible \
  CRUXIBLE_SERVER_AUTH=true \
  CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=change-me-once \
  CRUXIBLE_HOST=127.0.0.1 \
  CRUXIBLE_PORT=8100 \
  /opt/cruxible-core/bin/cruxible-server
```

Notes:

- `CRUXIBLE_HOST=127.0.0.1` keeps the daemon local to the machine.
- `CRUXIBLE_SERVER_AUTH=true` and `CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET` enable
  runtime credential bootstrapping.
- `CRUXIBLE_SERVER_STATE_DIR` ensures the server-owned state stays under the runtime user's directory.
- Claim the bootstrap secret, create role-specific runtime credentials, and use
  those tokens from the agent environment. See
  [Runtime Auth And Agent Roles](runtime-auth-and-agent-roles.md).

### 4. Install only the client in the agent environment

```bash
python3 -m venv .venv-agent
. .venv-agent/bin/activate
pip install cruxible-client
```

The agent environment should not have `cruxible-core` installed.

### 5. Connect to the isolated daemon

```python
from cruxible_client import CruxibleClient

with CruxibleClient(
    base_url="http://127.0.0.1:8100",
    token="<runtime-credential-token>",
) as client:
    result = client.validate(config_path="config.yaml")
    print(result.valid)
```

### 6. Lock down the agent user

This setup only works as a real boundary if the agent user does not also have a privilege-escalation path into the runtime environment.

At minimum, the agent user should not have:

- `sudo` access to become `cruxd`
- membership in the `docker` group
- read access to `/var/lib/cruxible`
- access to the `cruxible-core` source tree or runtime virtualenv

## Option 2: Separate Host or VM

This is the stronger version of the same pattern.

Run `cruxible-core` on a different machine and keep the state directory there. The agent machine installs only `cruxible-client` and connects over HTTP.

Daemon host:

```bash
python3 -m venv /opt/cruxible-core
/opt/cruxible-core/bin/pip install "cruxible-core[server]"
env \
  CRUXIBLE_SERVER_STATE_DIR=/var/lib/cruxible \
  CRUXIBLE_SERVER_AUTH=true \
  CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=change-me-once \
  CRUXIBLE_HOST=0.0.0.0 \
  CRUXIBLE_PORT=8100 \
  /opt/cruxible-core/bin/cruxible-server
```

Agent host:

```bash
python3 -m venv .venv-agent
. .venv-agent/bin/activate
pip install cruxible-client
```

Python client:

```python
from cruxible_client import CruxibleClient

with CruxibleClient(
    base_url="https://your-cruxible-host.example.com",
    token="<runtime-credential-token>",
) as client:
    result = client.validate(config_path="config.yaml")
    print(result.valid)
```

For real deployments, put the HTTP service behind normal operational controls such as TLS, firewall rules, and standard secret handling.

## MCP Caveat

Today, `cruxible-mcp` is part of `cruxible-core`.

That means the easiest local MCP setup still installs the full runtime package on the machine where the agent runs. It is convenient, but it is not the strongest isolation story.

If you need a real boundary today, the cleanest path is:

- `cruxible-core` on the isolated runtime
- `cruxible-client` in the agent environment
- HTTP-based access from the agent side

Use local MCP for convenience and development. Use client-to-daemon separation when isolation matters.

## Docker Caveat

Docker is useful for packaging and reproducibility. It is not automatically a security boundary.

Docker only helps with isolation if the agent cannot:

- run `docker`
- access the Docker socket
- mount the runtime volume
- inspect the runtime container filesystem

If the agent can do those things, it can usually recover the graph data anyway.

## Practical Recommendation

Choose one of these modes deliberately:

- default local mode: fastest onboarding, advisory permissions only
- isolated mode: more setup, meaningful runtime boundary

If you only need convenience, use the standard local setup.

If you need the graph to be genuinely inaccessible to the agent, use one of the isolated patterns above.
