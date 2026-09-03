# Hosted Runtime Image

The hosted runtime image packages `cruxible` (daemon included) for private runtime
containers. It starts the daemon (`cruxible server start`) as a non-root
`cruxible` user and uses `/var/lib/cruxible` as its explicit state root.

Build with any Docker-compatible backend. OrbStack works for local development:

```bash
docker build -f deploy/runtime/Dockerfile -t cruxible-core-runtime:test .
```

Run with a mounted state root and a runtime-supplied bootstrap secret:

```bash
STATE_DIR="$(mktemp -d)"
chmod 0777 "${STATE_DIR}"
docker run --rm \
  -e CRUXIBLE_RUNTIME_BOOTSTRAP_SECRET=bootstrap-secret \
  -v "${STATE_DIR}":/var/lib/cruxible \
  -p 127.0.0.1:8100:8100 \
  cruxible-core-runtime:test
```

The image intentionally fails fast if `/var/lib/cruxible` is not an
external Docker mount, or if the non-root `cruxible` user cannot write to it.
This prevents hosted runtime state from being stored only in the container's
ephemeral filesystem layer.

The external Cloud control plane (the separate `cruxible-cloud-api` package, not
`cruxible`) is what prepares each runtime state root before
starting the runtime container. By default it applies mode `0777`, matching the
local smoke-test pattern above so the non-root container user can write through
the bind mount on a normal Linux host. Tighter host-ownership modes are
configured on that control plane, not through any `cruxible` environment
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

## Publishing the image

`.github/workflows/publish-runtime-image.yml` is the supported way to publish
this image. It builds `deploy/runtime/Dockerfile` for `linux/amd64` and pushes
it to GHCR under the repository namespace:

```
ghcr.io/<owner>/<repo>/runtime:runtime-<version>-<sha12>
```

`<version>` is the `pyproject.toml` package version and `<sha12>` the first 12
characters of the built commit. The workflow never publishes or moves `latest`.

### Dispatch from a reviewed SHA

1. Pick the reviewed commit and copy its **full 40-character SHA**. A short SHA
   is rejected by the verification step, and a branch name builds whatever the
   branch points at today.
2. Run the workflow: Actions -> *Publish runtime image* -> *Run workflow*, with
   `source_ref` set to that SHA.
3. The build job checks out exactly that ref and refuses to continue unless
   `git rev-parse HEAD` equals the requested SHA.
4. Read the run summary. It reports source SHA, version, tag, digest, and
   whether the image was rebuilt, and prints the digest-pinned reference to copy
   into a deployment. The same values are job outputs (`image`, `tag`,
   `version`, `source_sha`, `digest`, `image_ref`, `rebuilt`).

Pushing a `v*` release tag runs the same publish for the tagged commit,
alongside the PyPI publish in `publish.yml`. On that trigger the tag must equal
`v<package version>`.

### Immutability

A published tag is never overwritten:

- Before building, the workflow asks the registry for the tag. Only an explicit
  "absent" response authorizes a push; an auth or transport failure fails the
  job instead of being read as "unpublished". The lookup exchanges its
  credential for a registry token first (`.github/scripts/ghcr-tag-digest.sh`),
  because `ghcr.io/v2` does not reject an unusable bearer token — it downgrades
  the request to anonymous access, which would make a private published tag
  look absent.
- If the tag exists, nothing is built and nothing is pushed. The workflow reuses
  that digest, so re-dispatching the same commit is a cheap no-op that
  re-reports the same digest.
- Before reusing a tag, its `org.opencontainers.image.revision` and
  `org.opencontainers.image.version` labels must match the requested source. A
  tag that was moved onto some other build fails the job; it is not adopted.
- After pushing, the tag must still resolve to the digest this run produced,
  which closes the window between the existence check and the push.

Every image carries `org.opencontainers.image.source`, `.revision`, `.version`,
and `.created`.

### Verification

A second job runs with `packages: read`, pulls the image **by digest**, checks
its revision and version labels, and runs `tests/test_image` against it — the
same suite CI runs against a locally built image. Setting
`CRUXIBLE_RUNTIME_IMAGE_REF` to an already-pulled reference makes the module
fixture use that image instead of building one, so the published artifact is
what gets checked for the non-root user, the bundled `oras` version, `/health`,
and the external-state-mount contract. The variable requires the image to be in
the local store already; the tests never pull it.

The same override works locally:

```bash
docker pull ghcr.io/<owner>/<repo>/runtime@sha256:<digest>
CRUXIBLE_RUN_DOCKER_TESTS=1 \
CRUXIBLE_RUNTIME_IMAGE_REF=ghcr.io/<owner>/<repo>/runtime@sha256:<digest> \
  uv run pytest tests/test_image -m docker
```

### Repository linkage and package visibility

These are two different settings on the package, changed in two different
places, and neither one implies the other. Both are one-time setup after the
first publish.

**1. Repository linkage** decides *which repository's permissions govern the
package* and makes the package show up on the repository page. The workflow
labels every image with
`org.opencontainers.image.source=<server_url>/<owner>/<repo>`, which is what
GHCR uses to link the package to this repository automatically. If a package
ever shows as unlinked, connect it by hand: GitHub -> the package page ->
*Package settings* -> *Manage Actions access* / *Connect repository*. Linking
grants the repository's collaborators and its workflows access to the package;
it does not change who outside the repository can pull it.

**2. Package visibility** (private / internal / public) decides *whether a
credential is needed to pull at all*. A newly published container package is
**private**, regardless of whether the repository that built it is public — a
public repository does not publish public packages, and linking a package to a
public repository does not change the package's visibility either. Change it
on the package's own settings page: *Package settings* -> *Danger Zone* ->
*Change visibility* -> *Public*. Anonymous `docker pull` then works with no
`docker login`.

Publishing always requires the workflow's `packages: write` token, whichever
visibility the package has.

### Pull auth for a private package

While the package is private, every consumer needs a credential, including a
deployment host:

```bash
echo "<personal-access-token>" \
  | docker login ghcr.io -u <github-username> --password-stdin
docker pull ghcr.io/<owner>/<repo>/runtime@sha256:<digest>
```

The token must be a classic PAT with the `read:packages` scope, and its owner
must have access to the package — either through the linked repository or
directly (GitHub -> the package page -> *Package settings* -> *Manage access*).
`read:packages` is pull-only; never give a deployment host a token with
`write:packages`.

### Pin the digest

Deployments reference `.../runtime@sha256:<digest>`, not the tag. The immutable
tag is a human-readable handle for finding the digest; the digest is what proves
which bytes are running. `latest` is never published, so nothing on a host can
drift by pulling it. Record the digest wherever the deployment ref is
configured, and re-run the workflow dispatch — not a manual `docker build` — to
obtain a new one.

## Shared Profile Customer Code Policy

Set `CRUXIBLE_HOSTED_SERVER_PROFILE=shared` for runtimes that may host
untrusted or multi-tenant material. In this profile the daemon executes no
Provider code at all: `procedure run`, `line run` and `provider seed` refuse
with the public-safe error code `customer_code_execution_unsupported` and the
detail `isolation backend not implemented`, and the refusal happens before any
tenant secret is resolved and before any child process exists.

Execution is permitted only when an isolated executor is REGISTERED in the
running build, not when one is merely named in the environment. This repository
registers none, so `CRUXIBLE_HOSTED_ISOLATED_EXECUTION_BACKEND` cannot re-enable
execution today; setting it to `docker` previously unlocked spawning the
Provider directly on the host, which is the opposite of what the name promised
(maintainer ruling 2026-09-03). The variable stays as the selector an executor
will be chosen by once one ships.

A non-empty `CRUXIBLE_HOSTED_SERVER_PROFILE` this build does not declare —
a misspelling, or a profile from a newer image — refuses typed with
`hosted_profile_unknown` rather than being read as "not shared". Both refusals
reach HTTP as `403` and reconstruct as their typed client classes.

Runtimes that must execute Provider code run without
`CRUXIBLE_HOSTED_SERVER_PROFILE` set: that is the ordinary single-tenant
profile, and it is unchanged.

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
