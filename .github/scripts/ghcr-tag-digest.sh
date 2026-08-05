#!/usr/bin/env bash
# Resolve the registry digest currently behind a GHCR tag.
#
# Usage: ghcr-tag-digest.sh <image> <tag>
#
# Credentials: export GHCR_TOKEN with a GITHUB_TOKEN or PAT that can read the
# package. Export GHCR_ANONYMOUS=1 instead to look a public package up with no
# credential at all; GHCR_USERNAME overrides the basic-auth username sent to the
# token service (GHCR ignores it, any non-empty value works).
#
# Prints the `sha256:...` digest on stdout when the tag exists, prints nothing
# when the registry reports the tag as absent, and exits non-zero for every
# other response. The three outcomes are kept distinct on purpose: the publish
# workflow treats "absent" as permission to push an immutable tag, so an auth
# or transport failure must never be mistaken for "absent" — that would move an
# already-published tag.
#
# Auth is the registry's two-step flow, not the credential itself: ghcr.io/v2
# answers 401 with a `WWW-Authenticate: Bearer realm="https://ghcr.io/token"`
# challenge, and the bearer token for /v2 is the one that token service issues
# in exchange for the credential (sent as basic auth). Pushing the credential
# straight at /v2 is not equivalent, and the difference is the whole point of
# this script: ghcr.io/v2 does not reject a bearer token it cannot use, it
# silently downgrades the request to anonymous access. A wrong or expired
# credential therefore reads as 404 — "absent" — on a private tag that really
# exists, while the workflow's separate `docker login` session is still able to
# push over it. Exchanging the credential first turns that into an explicit
# rejection (HTTP 401/403) from the token service, which hard fails below.
#
# What "absent" can still hide: GHCR answers manifest requests for packages the
# credential is not entitled to read with 404 rather than 403, so a caller
# holding a valid credential without read access on that package sees "absent".
# That does not open the moved-tag hole: moving a tag needs push access on the
# same package, the workflow looks up with the same credential it pushes with,
# and a caller who cannot read a package cannot push over it either — so the
# mistake surfaces as a failed push, never as a silently overwritten tag.
#
# GHCR_ANONYMOUS=1 is narrower: the token service refuses (403) to issue a token
# for any package it will not serve publicly, so an anonymous lookup can prove
# "absent tag in a public package" but never "absent package" — the latter hard
# fails here rather than being reported as absent. The publish workflow always
# passes a credential.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: ghcr-tag-digest.sh <image> <tag>" >&2
  exit 2
fi

image="$1"
tag="$2"

case "${image}" in
  ghcr.io/*) ;;
  *)
    echo "ghcr-tag-digest: expected a ghcr.io image, got '${image}'" >&2
    exit 2
    ;;
esac

repository_path="${image#ghcr.io/}"

# Both values land in a URL. Keep them to the character sets the registry spec
# allows so neither can smuggle a query string or a path segment into it.
if ! printf '%s' "${repository_path}" \
  | grep -Eq '^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)+$'; then
  echo "ghcr-tag-digest: '${image}' is not a valid GHCR repository path" >&2
  exit 2
fi

if ! printf '%s' "${tag}" | grep -Eq '^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$'; then
  echo "ghcr-tag-digest: '${tag}' is not a valid image tag" >&2
  exit 2
fi

anonymous="${GHCR_ANONYMOUS:-}"
if [ -z "${anonymous}" ]; then
  # Deliberately required rather than optional: silently falling back to an
  # anonymous lookup would make a missing credential look like an absent tag.
  : "${GHCR_TOKEN:?GHCR_TOKEN must be set (or set GHCR_ANONYMOUS=1)}"
fi

token_file="$(mktemp)"
header_file="$(mktemp)"
trap 'rm -f "${token_file}" "${header_file}"' EXIT

# Step 1: exchange the credential for a registry bearer token scoped to a pull
# on this repository. A valid credential is granted a token even when the
# package does not exist yet, so first publishes still reach step 2.
token_args=(
  --silent
  --show-error
  --max-time 60
  --output "${token_file}"
  --write-out '%{http_code}'
)
if [ -z "${anonymous}" ]; then
  token_args+=(--user "${GHCR_USERNAME:-x-access-token}:${GHCR_TOKEN}")
fi

if ! token_status="$(
  curl "${token_args[@]}" \
    "https://ghcr.io/token?service=ghcr.io&scope=repository:${repository_path}:pull"
)"; then
  echo "ghcr-tag-digest: could not reach the ghcr.io token service" >&2
  echo "ghcr-tag-digest: refusing to treat this as an unpublished tag" >&2
  exit 1
fi

if [ "${token_status}" != "200" ]; then
  echo "ghcr-tag-digest: token service returned HTTP ${token_status} for ${image}" >&2
  if [ -n "${anonymous}" ]; then
    echo "ghcr-tag-digest: anonymous lookup; the package may be private or absent" >&2
  else
    echo "ghcr-tag-digest: the credential in GHCR_TOKEN was rejected" >&2
  fi
  echo "ghcr-tag-digest: refusing to treat this as an unpublished tag" >&2
  exit 1
fi

# GHCR answers {"token": "..."}; the spec also allows "access_token".
registry_token="$(
  tr -d '\n' < "${token_file}" \
    | sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
)"
if [ -z "${registry_token}" ]; then
  registry_token="$(
    tr -d '\n' < "${token_file}" \
      | sed -n 's/.*"access_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
  )"
fi
if [ -z "${registry_token}" ]; then
  echo "ghcr-tag-digest: token service returned no token for ${image}" >&2
  echo "ghcr-tag-digest: refusing to treat this as an unpublished tag" >&2
  exit 1
fi

# Step 2: the manifest lookup itself, carrying the issued token.
if ! status="$(
  curl \
    --silent \
    --show-error \
    --head \
    --location \
    --max-time 60 \
    --output /dev/null \
    --dump-header "${header_file}" \
    --write-out '%{http_code}' \
    --header "Authorization: Bearer ${registry_token}" \
    --header 'Accept: application/vnd.oci.image.index.v1+json' \
    --header 'Accept: application/vnd.oci.image.manifest.v1+json' \
    --header 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
    --header 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    "https://ghcr.io/v2/${repository_path}/manifests/${tag}"
)"; then
  echo "ghcr-tag-digest: could not reach ghcr.io for ${image}:${tag}" >&2
  echo "ghcr-tag-digest: refusing to treat this as an unpublished tag" >&2
  exit 1
fi

case "${status}" in
  200) ;;
  404)
    # Absent tag (or absent package): nothing published under this name yet.
    # The credential was accepted by the token service above, so this is a
    # statement about the tag and not about the credential.
    exit 0
    ;;
  *)
    echo "ghcr-tag-digest: unexpected HTTP ${status} for ${image}:${tag}" >&2
    echo "ghcr-tag-digest: refusing to treat this as an unpublished tag" >&2
    exit 1
    ;;
esac

digest="$(
  grep -i '^docker-content-digest:' "${header_file}" \
    | tail -n 1 \
    | awk '{print $2}' \
    | tr -d '\r\n'
)"

if [ -z "${digest}" ]; then
  echo "ghcr-tag-digest: ${image}:${tag} returned 200 without a content digest" >&2
  exit 1
fi

printf '%s\n' "${digest}"
