#!/usr/bin/env bash
# Runtime-neutral container/Compose helpers for Podman and Docker.
# Does not weaken security: never invents image digests; never opens Docker/Podman
# at application runtime — only build/attestation and Compose orchestration use this.
#
# shellcheck shell=bash

orch_detect_container_runtime() {
  if [[ -n "${ORCH_CONTAINER_RUNTIME:-}" ]]; then
    case "${ORCH_CONTAINER_RUNTIME}" in
      podman|docker) echo "${ORCH_CONTAINER_RUNTIME}"; return 0 ;;
      *)
        echo "ERROR: ORCH_CONTAINER_RUNTIME must be podman or docker (got: ${ORCH_CONTAINER_RUNTIME})" >&2
        return 1
        ;;
    esac
  fi

  # Prefer Podman when Docker socket is inaccessible (rootless / no dockerd).
  if command -v podman >/dev/null 2>&1; then
    if ! command -v docker >/dev/null 2>&1; then
      echo "podman"
      return 0
    fi
    if docker info >/dev/null 2>&1; then
      echo "docker"
      return 0
    fi
    echo "podman"
    return 0
  fi

  if command -v docker >/dev/null 2>&1; then
    echo "docker"
    return 0
  fi

  echo "ERROR: neither podman nor docker is available" >&2
  return 1
}

orch_container_cli() {
  orch_detect_container_runtime
}

orch_compose_cmd() {
  # Prints space-separated argv base for compose (no trailing args).
  local runtime
  runtime="$(orch_detect_container_runtime)" || return 1
  if [[ "${runtime}" == "podman" ]]; then
    if command -v podman-compose >/dev/null 2>&1; then
      echo "podman-compose"
      return 0
    fi
    if podman compose version >/dev/null 2>&1; then
      echo "podman compose"
      return 0
    fi
    echo "ERROR: podman found but neither podman-compose nor 'podman compose' works" >&2
    return 1
  fi
  if docker compose version >/dev/null 2>&1; then
    echo "docker compose"
    return 0
  fi
  if command -v docker-compose >/dev/null 2>&1; then
    echo "docker-compose"
    return 0
  fi
  echo "ERROR: docker found but Compose plugin/binary unavailable" >&2
  return 1
}

orch_compose() {
  # Usage: orch_compose [compose args...]
  local -a base
  # shellcheck disable=SC2207
  base=($(orch_compose_cmd)) || return 1
  "${base[@]}" "$@"
}

orch_image_build() {
  # Usage: orch_image_build <tag> --target <stage> [extra docker/podman build args...]
  local runtime tag
  runtime="$(orch_detect_container_runtime)" || return 1
  tag="$1"
  shift
  "${runtime}" build -t "${tag}" "$@"
}

orch_image_inspect() {
  # Usage: orch_image_inspect <image-ref>
  local runtime
  runtime="$(orch_detect_container_runtime)" || return 1
  "${runtime}" image inspect "$1"
}

orch_attestation_source_label() {
  # Production attestation source for HMAC documents.
  # container_inspect = OCI image Id/RepoDigest from build-time inspect (podman or docker).
  # docker_inspect remains accepted for back-compat.
  echo "container_inspect"
}
