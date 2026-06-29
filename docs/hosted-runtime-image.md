# Hosted Runtime Image

The hosted runtime image packages `cruxible-core[server]` for private runtime
containers. It starts the daemon (`cruxible server start`) as a non-root
`cruxible` user and stores mutable server state under `/var/lib/cruxible/server`.

Build with any Docker-compatible backend. OrbStack works for local development:

```bash
docker build -f deploy/runtime/Dockerfile -t cruxible-core-runtime:test .
```

Run with a mounted state directory and a runtime-supplied bootstrap secret:

```bash
STATE_DIR="$(mktemp -d)"
chmod 0777 "${STATE_DIR}"
docker run --rm \
  -e CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=bootstrap-secret \
  -v "${STATE_DIR}":/var/lib/cruxible/server \
  -p 127.0.0.1:8100:8100 \
  cruxible-core-runtime:test
```

The image intentionally fails fast if `/var/lib/cruxible/server` is not an
external Docker mount, or if the non-root `cruxible` user cannot write to it.
This prevents hosted runtime state from being stored only in the container's
ephemeral filesystem layer.

The external Cloud control plane (the separate `cruxible-cloud-api` package, not
`cruxible-core`) is what prepares each per-instance host state directory before
starting the runtime container. By default it applies mode `0777`, matching the
local smoke-test pattern above so the non-root container user can write through
the bind mount on a normal Linux host. Tighter host-ownership modes are
configured on that control plane, not through any `cruxible-core` environment
variable an operator of this image sets directly.

Verify the server:

```bash
curl http://127.0.0.1:8100/health
```

Expected response:

```json
{"status":"ok"}
```

Do not bake bootstrap secrets or runtime credentials into the image. Provide
them at container runtime through environment variables or the future deployment
secret layer. See [Runtime Auth And Agent Roles](runtime-auth-and-agent-roles.md)
for the bootstrap and credential model.

## Shared Profile Customer Code Policy

Set `CRUXIBLE_HOSTED_SERVER_PROFILE=shared` for runtimes that may host
untrusted or multi-tenant material. In this profile, provider execution and
Python provider loading are denied unless
`CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND` is set to a supported isolated
backend. The current supported backend name is `docker`.

Unsupported or missing isolated backends fail with the public-safe error code
`customer_code_execution_unsupported`.

## Private Runtime Network

Hosted runtimes should not publish port `8100` on the public host interface.
Public traffic should enter through external/future Cloud components — the edge
proxy or `cruxible-cloud-api`, neither of which ships in this repo — and
Cloud/API should reach runtimes over a private Docker network.

For local development, create a writable state directory and run the private
network proof:

```bash
STATE_DIR="$(mktemp -d)"
chmod 0777 "${STATE_DIR}"
CRUXIBLE_RUNTIME_STATE_DIR="${STATE_DIR}" \
  docker compose -f deploy/local/private-runtime-network.compose.yml up \
  --build --abort-on-container-exit runtime-probe
```

The `runtime` service uses `expose: ["8100"]` for same-network discovery but
does not publish `8100` to the host. The `runtime-probe` service can reach
`http://runtime:8100/health` because it joins the same private Docker network.

On a future Droplet or VM deployment, this same boundary should be reinforced
with firewall/VPC rules: public ingress is limited to the edge proxy ports
(`80`/`443`) and SSH, while runtime port `8100` remains private to Cloud/API or
the runtime network.
