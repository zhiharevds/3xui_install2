#!/bin/bash

red='\033[0;31m'
green='\033[0;32m'
yellow='\033[0;33m'
plain='\033[0m'

UI_PORT=$(shuf -i 50000-65535 -n1)
HTTP_PORT=$(shuf -i 50000-65535 -n1)
MAIL=$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 10 | head -n 1)
DOMAIN=$(cat /dev/urandom | tr -dc 'a-zA-Z0-9' | fold -w 10 | head -n 1)
# Исправлено: hostname --ip-address может вернуть локальный адрес
MAIN_IP=$(curl -s ifconfig.me)

echo y | ufw reset

ufw allow ${UI_PORT}/tcp
ufw allow ${HTTP_PORT}/tcp
ufw allow 443/tcp
ufw allow 80/tcp
ufw allow 22/tcp

echo y | ufw enable
ufw status verbose

echo n | bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)
/usr/local/x-ui/x-ui setting -username admin -password admin -port ${UI_PORT}
WEB_PATH=$(x-ui settings | grep webBasePath | cut -d " " -f 2-)

# Исправлено: ждём освобождения apt перед установкой Caddy
echo -e "${yellow}Waiting for apt to be available...${plain}"
systemctl stop unattended-upgrades 2>/dev/null || true
while fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
    echo "Waiting for apt lock..."
    sleep 3
done

# Исправлено: устанавливаем Caddy и sqlite3, ждём освобождения apt
apt-get install -y caddy sqlite3
if ! command -v caddy &>/dev/null; then
    echo -e "${red}Caddy installation failed! Aborting.${plain}"
    exit 1
fi

# Исправлено: копируем сертификат в папку доступную пользователю caddy
mkdir -p /etc/caddy/certs
cp /root/cert/ip/fullchain.pem /etc/caddy/certs/
cp /root/cert/ip/privkey.pem /etc/caddy/certs/
chown -R caddy:caddy /etc/caddy/certs
chmod 600 /etc/caddy/certs/privkey.pem

# Исправлено: настраиваем хук для автообновления сертификата
cat > /etc/caddy/renew-hook.sh << 'HOOK'
#!/bin/bash
cp /root/cert/ip/fullchain.pem /etc/caddy/certs/
cp /root/cert/ip/privkey.pem /etc/caddy/certs/
chown caddy:caddy /etc/caddy/certs/*.pem
chmod 600 /etc/caddy/certs/privkey.pem
systemctl restart caddy
HOOK
chmod +x /etc/caddy/renew-hook.sh

/root/.acme.sh/acme.sh --install-cert -d ${MAIN_IP} --ecc \
  --key-file /root/cert/ip/privkey.pem \
  --fullchain-file /root/cert/ip/fullchain.pem \
  --reloadcmd "/etc/caddy/renew-hook.sh"

# Исправлено: убираем SSL из x-ui — TLS терминирует Caddy
sqlite3 /etc/x-ui/x-ui.db "UPDATE settings SET value='' WHERE key='webCertFile' OR key='webKeyFile';" 2>/dev/null || true
x-ui restart 2>/dev/null || true

rm -rf /etc/caddy/Caddyfile
cat << EOF | tee "/etc/caddy/Caddyfile"
{
    auto_https disable_redirects
    https_port ${HTTP_PORT}
    http_port  10087
    https_port 443
    http_port 80
    log {
        level ERROR
    }
    on_demand_tls {
        ask http://localhost:10087/
        interval 3600s
        burst 4
    }
}

# Исправлено: используем реальный сертификат вместо tls internal
https://${MAIN_IP}:${HTTP_PORT} {
  reverse_proxy localhost:${UI_PORT}
  tls /etc/caddy/certs/fullchain.pem /etc/caddy/certs/privkey.pem
}

# Match only host names and not ip-addresses:
https://*.*:${HTTP_PORT},
https://*.*.*:${HTTP_PORT} {
    reverse_proxy localhost:${UI_PORT}
    tls {
        on_demand
        issuer acme {
            email ${MAIL}@${DOMAIN}.com
        }
    }
}

http://:10087 {
  respond "allowed" 200 {
    close
  }
}
EOF

systemctl restart caddy

echo -e "${green}x-ui ${plain} installation finished, it is running now..."
echo -e "###############################################"
echo -e "${green}username: admin${plain}"
echo -e "${green}password: admin${plain}"
echo -e "###############################################"
echo -e "The panel is available at ${red}https://${MAIN_IP}:${HTTP_PORT}${plain}${WEB_PATH}"
echo -e "###############################################"
