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

env_value() {
  local key="$1"
  local line value
  line="$(grep -E "^${key}=" "$source_env" | tail -n 1 || true)"
  value="${line#*=}"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  printf '%s' "$value"
}

cotas_port="$(env_value COTAS_PORT)"
cotas_storage="$(env_value COTAS_STORAGE)"

if [[ ! "$cotas_port" =~ ^[0-9]+$ ]]; then
  echo "COTAS_PORT invalido en $source_env" >&2
  exit 4
fi

expected_storage="$app_root/storage/tenants/$slug"
if [[ "$cotas_storage" != "$expected_storage" ]]; then
  echo "COTAS_STORAGE no coincide con el tenant $slug." >&2
  echo "Esperado: $expected_storage" >&2
  echo "Actual: ${cotas_storage:-vacio}" >&2
  exit 5
fi

systemctl stop "cotas-tenant@$slug.service" 2>/dev/null || true

port_pid="$(ss -ltnp "sport = :$cotas_port" 2>/dev/null | sed -n 's/.*pid=\([0-9]\+\).*/\1/p' | head -n 1)"
if [[ -n "$port_pid" && "$port_pid" != "0" ]]; then
  echo "El puerto $cotas_port ya esta ocupado por PID $port_pid. No se activara $slug con un puerto usado." >&2
  exit 6
fi

install -d -m 0750 -o root -g www-data /etc/cotas-tenants
install -m 0600 -o root -g www-data "$source_env" "$target_env"
systemctl enable --now "cotas-tenant@$slug.service"
systemctl restart "cotas-tenant@$slug.service"
systemctl is-active --quiet "cotas-tenant@$slug.service"
echo "Tenant $slug activo."
