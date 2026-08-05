#!/usr/bin/env bash
# Resolve the registry digest currently behind a GHCR tag.
#
# Usage: ghcr-tag-digest.sh <image> <tag>   (GHCR_TOKEN must be exported)
#
# Prints the `sha256:...` digest on stdout when the tag exists, prints nothing
# when the registry reports the tag as absent, and exits non-zero for every
# other response. The three outcomes are kept distinct on purpose: the publish
# workflow treats "absent" as permission to push an immutable tag, so an auth
# or transport failure must never be mistaken for "absent" — that would move an
# already-published tag.
#
# GHCR hides private packages behind 404 rather than 403, so a caller with no
# read access sees "absent". That does not open the moved-tag hole: moving a tag
# needs push access on the same package, and a caller who cannot read it cannot
# push to it either, so the mistake surfaces as a failed push, never as a
# silently overwritten tag.
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

: "${GHCR_TOKEN:?GHCR_TOKEN must be set}"

repository_path="${image#ghcr.io/}"
# GHCR accepts a base64-encoded GITHUB_TOKEN/PAT as the registry bearer token.
bearer="$(printf '%s' "${GHCR_TOKEN}" | base64 | tr -d '\n')"
header_file="$(mktemp)"
trap 'rm -f "${header_file}"' EXIT

status="$(
  curl \
    --silent \
    --show-error \
    --head \
    --location \
    --max-time 60 \
    --output /dev/null \
    --dump-header "${header_file}" \
    --write-out '%{http_code}' \
    --header "Authorization: Bearer ${bearer}" \
    --header 'Accept: application/vnd.oci.image.index.v1+json' \
    --header 'Accept: application/vnd.oci.image.manifest.v1+json' \
    --header 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' \
    --header 'Accept: application/vnd.docker.distribution.manifest.v2+json' \
    "https://ghcr.io/v2/${repository_path}/manifests/${tag}"
)"

case "${status}" in
  200) ;;
  404)
    # Absent tag (or absent package): nothing published under this name yet.
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
