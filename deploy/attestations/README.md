# Script-runner image attestations

Runtime never calls Docker or Podman. Authorized digests come from a signed local
attestation produced at build/deploy time via container image inspect
(`Id` / `RepoDigest`).

## Production / Compose (podman or docker)

```bash
export ORCH_ATTESTATION_HMAC_KEY=...
# Optional: ORCH_CONTAINER_RUNTIME=podman|docker
bash scripts/build_script_runner_attestation.sh
# prints ORCH_SCRIPT_IMAGE_DIGEST=sha256:...
# writes deploy/attestations/script-runner.attestation.json (gitignored)
```

`source` must be `container_inspect` (preferred, runtime-neutral) or legacy
`docker_inspect`. Missing attestation fails closed outside `ORCH_TESTING`.

## Testing fixture

```bash
ORCH_TESTING=1 python scripts/write_testing_attestation.py
```

Writes `script-runner.testing.attestation.json` (`source=orch_testing_fixture`),
rejected outside `ORCH_TESTING`.
