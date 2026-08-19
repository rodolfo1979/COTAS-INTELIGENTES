#!/usr/bin/env bash
set -euo pipefail

slug="${1:-}"
app_root="/var/www/cotas-inteligentes"

if [[ ! "$slug" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]]; then
  echo "Codigo cliente invalido: $slug" >&2
  exit 2
fi

source_env="$app_root/deploy/generated/$slug.env"
target_env="/etc/cotas-tenants/$slug.env"

if [[ ! -f "$source_env" ]]; then
  echo "No existe $source_env" >&2
  exit 3
fi

install -d -m 0750 -o root -g www-data /etc/cotas-tenants
install -m 0600 -o root -g www-data "$source_env" "$target_env"
systemctl enable --now "cotas-tenant@$slug.service"
systemctl restart "cotas-tenant@$slug.service"
systemctl is-active --quiet "cotas-tenant@$slug.service"
echo "Tenant $slug activo."
