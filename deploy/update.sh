#!/bin/bash
# Apply the checkout already updated by /usr/local/bin/plugin-deploy.
set -euo pipefail
cd /root/webapps/plugin
python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt
install -m 644 deploy/erudore-plugin.service /etc/systemd/system/erudore-plugin.service
install -m 644 deploy/erudore-plugin-worker.service /etc/systemd/system/erudore-plugin-worker.service
if [ -f /etc/letsencrypt/live/plugin.erudore.com/fullchain.pem ]; then
  install -m 644 deploy/plugin.erudore.com.nginx /etc/nginx/sites-available/plugin.erudore.com
else
  install -m 644 deploy/plugin.erudore.com.http.nginx /etc/nginx/sites-available/plugin.erudore.com
fi
ln -sfn /etc/nginx/sites-available/plugin.erudore.com /etc/nginx/sites-enabled/plugin.erudore.com
nginx -t
systemctl reload nginx
systemctl daemon-reload
systemctl enable erudore-plugin.service erudore-plugin-worker.service
systemctl restart erudore-plugin.service erudore-plugin-worker.service
systemctl is-active --quiet erudore-plugin.service
systemctl is-active --quiet erudore-plugin-worker.service
echo "deployed $(git rev-parse --short HEAD)"
