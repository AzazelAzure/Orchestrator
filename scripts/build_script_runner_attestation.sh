#!/usr/bin/env bash
# Build script-runner image and persist a signed local attestation from
# podman/docker image inspect (Id / RepoDigest). Runtime-neutral.
# Does NOT invent digests when the container CLI is unavailable — fails closed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/container_runtime.sh"

RUNTIME="$(orch_detect_container_runtime)"
IMAGE_TAG="${ORCH_SCRIPT_RUNNER_IMAGE_TAG:-orchestrator-script-runner:local}"
OUT_PATH="${ORCH_SCRIPT_RUNNER_ATTESTATION_OUT:-$ROOT/deploy/attestations/script-runner.attestation.json}"
SOURCE_LABEL="$(orch_attestation_source_label)"

if [[ -z "${ORCH_ATTESTATION_HMAC_KEY:-}" ]]; then
  echo "ERROR: ORCH_ATTESTATION_HMAC_KEY is required to sign attestation" >&2
  exit 1
fi

echo "Using container runtime: ${RUNTIME}"
echo "Building script-runner image: ${IMAGE_TAG}"
orch_image_build "${IMAGE_TAG}" --target script-runner .

echo "Inspecting immutable image Id / RepoDigest via ${RUNTIME}"
export ORCH_ATTESTATION_HMAC_KEY
export IMAGE_TAG
export OUT_PATH
export SOURCE_LABEL
export ORCH_CONTAINER_RUNTIME="${RUNTIME}"
python3 - <<'PY'
import json, os, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(os.environ.get("PWD", ".")) / "src"))

from flow_engine.script_sandbox.attestation import (
    SOURCE_CONTAINER_INSPECT,
    SOURCE_DOCKER_INSPECT,
    build_attestation_document,
    write_attestation,
)
from flow_engine.script_sandbox.pins import assert_valid_sha256_digest, orch_script_source_digest

runtime = os.environ["ORCH_CONTAINER_RUNTIME"]
image_tag = os.environ["IMAGE_TAG"]
source_label = os.environ.get("SOURCE_LABEL", SOURCE_CONTAINER_INSPECT)
if source_label not in {SOURCE_CONTAINER_INSPECT, SOURCE_DOCKER_INSPECT}:
    raise SystemExit(f"unsupported attestation source label: {source_label}")

raw = subprocess.check_output([runtime, "image", "inspect", image_tag], text=True)
docs = json.loads(raw)
if not docs:
    raise SystemExit(f"{runtime} inspect returned empty")
info = docs[0]
image_id = str(info.get("Id") or "")
if not image_id:
    raise SystemExit(f"{runtime} inspect missing Id")
if image_id.startswith("sha256:"):
    image_id_digest = image_id
else:
    image_id_digest = assert_valid_sha256_digest(
        image_id if image_id.startswith("sha256:") else f"sha256:{image_id}",
        what="image Id",
    )

repo_digests = list(info.get("RepoDigests") or [])
image_digest = None
for item in repo_digests:
    if "@sha256:" in item:
        image_digest = "sha256:" + item.split("@sha256:", 1)[1]
        break
# Local builds often lack RepoDigests; Id is the immutable content address.
if image_digest is None:
    image_digest = image_id_digest

doc = build_attestation_document(
    image_digest=image_digest,
    image_id=image_id_digest,
    executable_digest=orch_script_source_digest(),
    built_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    source=source_label,
)
out = Path(os.environ["OUT_PATH"])
write_attestation(out, doc)
print(f"Wrote attestation: {out}")
print(f"runtime={runtime}")
print(f"source={doc['source']}")
print(f"ORCH_SCRIPT_IMAGE_DIGEST={doc['image_digest']}")
PY
