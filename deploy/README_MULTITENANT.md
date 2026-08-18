# Multi-tenant sin tocar la logica

Este modo mantiene intacta la app actual de cotas. Cada cliente corre como una
instancia separada, con su propio puerto, login y carpeta `COTAS_STORAGE`.

## Estructura

```text
admin.tudominio.com      -> panel super admin -> 127.0.0.1:8099
cliente1.tudominio.com   -> tenant cliente1  -> 127.0.0.1:8088
cliente2.tudominio.com   -> tenant cliente2  -> 127.0.0.1:8089
```

Cada tenant guarda sus archivos en:

```text
/var/www/cotas-inteligentes/storage/tenants/{slug}/
  uploads/
  jobs/
  history.sqlite
```

## Instalar plantillas en Ubuntu

Desde `/var/www/cotas-inteligentes`:

```bash
sudo mkdir -p /etc/cotas-tenants
sudo cp deploy/cotas-tenant@.service /etc/systemd/system/cotas-tenant@.service
sudo cp deploy/cotas-super-admin.service /etc/systemd/system/cotas-super-admin.service
sudo systemctl daemon-reload
```

## Configurar panel super admin

```bash
sudo tee /etc/cotas-super-admin.env >/dev/null <<'EOF'
COTAS_SUPERADMIN_USER=superadmin
COTAS_SUPERADMIN_PASSWORD=cambie-esta-clave
COTAS_SUPERADMIN_SECRET=cambie-este-secreto-largo
COTAS_TENANT_ROOT=/var/www/cotas-inteligentes/storage/tenants
COTAS_GENERATED_DIR=/var/www/cotas-inteligentes/deploy/generated
EOF
sudo chmod 600 /etc/cotas-super-admin.env
sudo systemctl enable --now cotas-super-admin
```

Configure `deploy/nginx-super-admin.conf`, cambie `admin.tudominio.com` por su
subdominio real y copie el archivo:

```bash
sudo cp deploy/nginx-super-admin.conf /etc/nginx/sites-available/cotas-super-admin
sudo ln -s /etc/nginx/sites-available/cotas-super-admin /etc/nginx/sites-enabled/cotas-super-admin
sudo nginx -t
sudo systemctl reload nginx
```

## Crear tenants

Entre al panel:

```text
http://admin.tudominio.com
```

Cuando crea un tenant, el panel genera archivos en:

```text
deploy/generated/
```

Por cada tenant, copie el `.env` generado a `/etc/cotas-tenants/{slug}.env` y el
nginx generado a `/etc/nginx/sites-available/{slug}`. Luego active el servicio:

```bash
sudo cp deploy/generated/cliente1.env /etc/cotas-tenants/cliente1.env
sudo chmod 600 /etc/cotas-tenants/cliente1.env
sudo cp deploy/generated/nginx-cliente1.conf /etc/nginx/sites-available/cliente1
sudo ln -s /etc/nginx/sites-available/cliente1 /etc/nginx/sites-enabled/cliente1
sudo systemctl enable --now cotas-tenant@cliente1
sudo nginx -t
sudo systemctl reload nginx
```

## Suspender un cliente

El panel permite marcar el cliente como `suspendido` para control administrativo.
Para bloquear el acceso sin tocar la logica de la app, apague su servicio:

```bash
sudo systemctl stop cotas-tenant@cliente1
sudo systemctl disable cotas-tenant@cliente1
```

Para reactivarlo:

```bash
sudo systemctl enable --now cotas-tenant@cliente1
```

## Actualizar el sistema

Como todos los tenants comparten el mismo codigo, una actualizacion se hace una
vez y luego se reinician los servicios:

```bash
cd /var/www/cotas-inteligentes
sudo -u www-data git pull
sudo systemctl restart 'cotas-tenant@*'
sudo systemctl restart cotas-super-admin
```
