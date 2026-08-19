#!/usr/bin/env bash
set -euo pipefail

slug="${1:-}"

if [[ ! "$slug" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
  echo "Codigo cliente invalido: $slug" >&2
  exit 2
fi

systemctl disable --now "cotas-tenant@$slug.service" || true
rm -f "/etc/cotas-tenants/$slug.env"
echo "Tenant $slug desactivado. Storage conservado."
