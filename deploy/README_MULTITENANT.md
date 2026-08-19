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

## Opcion recomendada: dominio unico para clientes

Para evitar crear un subdominio por cada cliente, use un portal unico:

```text
https://cotasinteligentes.morpho3d.com
```

El cliente ingresa:

```text
Codigo cliente: tagosa
Usuario: admin
Contrasena: la clave del tenant
```

El portal busca el tenant en la base del super admin y lo enruta al puerto
interno correcto. La app de cotas sigue corriendo igual por tenant.

Instale el servicio:

```bash
sudo cp deploy/cotas-tenant-portal.service /etc/systemd/system/cotas-tenant-portal.service
sudo systemctl daemon-reload
sudo systemctl enable --now cotas-tenant-portal
```

Agregue al env del super admin un secreto para el portal. Puede usar el mismo
secreto del super admin o uno nuevo:

```bash
sudo nano /etc/cotas-super-admin.env
```

```text
COTAS_PORTAL_SECRET=cambie-este-secreto-largo
```

Luego reinicie:

```bash
sudo systemctl restart cotas-super-admin
sudo systemctl restart cotas-tenant-portal
```

Configure una sola entrada DNS:

```text
A    cotasinteligentes    IP_IPV4_DEL_VPS
AAAA cotasinteligentes    IP_IPV6_DEL_VPS
```

Instale SSL antes de publicar el portal:

```bash
sudo apt update
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d cotasinteligentes.morpho3d.com
```

Despues de tener el certificado, copie la configuracion HTTPS del portal:

```bash
sudo cp deploy/nginx-tenant-portal-https.conf /etc/nginx/sites-available/cotas-tenant-portal
sudo ln -s /etc/nginx/sites-available/cotas-tenant-portal /etc/nginx/sites-enabled/cotas-tenant-portal
sudo nginx -t
sudo systemctl reload nginx
```

Con esta opcion ya no hace falta crear Nginx ni DNS por cada tenant nuevo. Solo
asegure que cada tenant tenga su servicio activo con un puerto unico.

## Activacion automatica de tenants

Para que el super admin active/desactive tenants sin entrar al VPS por cada
cliente, instale los scripts root limitados:

```bash
cd /var/www/cotas-inteligentes
sudo install -m 0750 -o root -g root deploy/cotas-provision-tenant.sh /usr/local/sbin/cotas-provision-tenant
sudo install -m 0750 -o root -g root deploy/cotas-deprovision-tenant.sh /usr/local/sbin/cotas-deprovision-tenant
sudo install -m 0440 -o root -g root deploy/sudoers-cotas-tenants /etc/sudoers.d/cotas-tenants
sudo visudo -cf /etc/sudoers.d/cotas-tenants
```

Active el modo automatico:

```bash
sudo nano /etc/cotas-super-admin.env
```

Agregue:

```text
COTAS_AUTO_PROVISION=1
COTAS_PROVISION_SCRIPT=/usr/local/sbin/cotas-provision-tenant
COTAS_DEPROVISION_SCRIPT=/usr/local/sbin/cotas-deprovision-tenant
```

Reinicie:

```bash
sudo systemctl restart cotas-super-admin
```

Desde ese momento, al crear un tenant el panel copia su `.env` a
`/etc/cotas-tenants/` y arranca `cotas-tenant@codigo`. Al eliminarlo del panel,
desactiva el servicio y conserva el storage del cliente.
