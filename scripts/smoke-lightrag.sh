#!/usr/bin/env bash
set -euo pipefail
set +x

: "${GRAPHRAG_GATEWAY_URL:?Set GRAPHRAG_GATEWAY_URL, including /api/v1}"

auth_args=()
if [[ -n "${LIGHTRAG_OPS_AUTH_TOKEN:-}" ]]; then
  auth_header="${LIGHTRAG_OPS_AUTH_HEADER:-Authorization}"
  auth_value="${LIGHTRAG_OPS_AUTH_TOKEN}"
  case "${auth_header}:${auth_value}" in
    [Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn]:[Bb][Ee][Aa][Rr][Ee][Rr]\ *) ;;
    [Aa][Uu][Tt][Hh][Oo][Rr][Ii][Zz][Aa][Tt][Ii][Oo][Nn]:*) auth_value="Bearer ${auth_value}" ;;
  esac
  auth_args=(-H "${auth_header}: ${auth_value}")
fi

gateway="${GRAPHRAG_GATEWAY_URL%/}"
ready_path="${LIGHTRAG_SMOKE_READY_PATH:-/health/ready}"
status_path="${LIGHTRAG_SMOKE_STATUS_PATH:-/health}"

curl --fail --silent --show-error "${auth_args[@]}" "${gateway}${ready_path}" >/dev/null
curl --fail --silent --show-error "${auth_args[@]}" "${gateway}${status_path}" >/dev/null

if [[ -n "${LIGHTRAG_BASE_URL:-}" ]]; then
  curl --fail --silent --show-error "${LIGHTRAG_BASE_URL%/}/live" >/dev/null
fi

echo "LightRAG smoke checks passed (gateway readiness, integration status, Railway health)."
