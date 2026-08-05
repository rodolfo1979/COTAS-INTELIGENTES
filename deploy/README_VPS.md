# Despliegue en VPS Ubuntu 24.04

Estos pasos instalan COTAS-INTELIGENTES en un VPS con Ubuntu 24.04.

## 1. Paquetes del sistema

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip poppler-utils nginx
```

## 2. Descargar el sistema

```bash
sudo mkdir -p /var/www
sudo chown -R $USER:$USER /var/www
cd /var/www
git clone https://github.com/rodolfo1979/COTAS-INTELIGENTES.git cotas-inteligentes
cd cotas-inteligentes
```

## 3. Crear entorno Python

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
mkdir -p storage/uploads storage/jobs
```

## 4. Probar localmente en el VPS

```bash
.venv/bin/python tools/start_server.py 8088
```

En otra terminal:

```bash
curl http://127.0.0.1:8088/version
```

## 5. Servicio systemd

```bash
sudo cp deploy/cotas-inteligentes.service /etc/systemd/system/cotas-inteligentes.service
sudo chown -R www-data:www-data /var/www/cotas-inteligentes
sudo systemctl daemon-reload
sudo systemctl enable --now cotas-inteligentes
sudo systemctl status cotas-inteligentes
```

## 6. Nginx

```bash
sudo cp deploy/nginx-cotas-inteligentes.conf /etc/nginx/sites-available/cotas-inteligentes
sudo ln -s /etc/nginx/sites-available/cotas-inteligentes /etc/nginx/sites-enabled/cotas-inteligentes
sudo nginx -t
sudo systemctl reload nginx
```

## 7. Actualizar despues

```bash
cd /var/www/cotas-inteligentes
sudo -u www-data git pull
sudo systemctl restart cotas-inteligentes
```
