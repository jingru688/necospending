#!/bin/bash
# Share the spending tracker with someone on a different network — FREE, no install.
# Uses an SSH reverse tunnel (localhost.run). Parsing runs on THIS Mac with your
# Claude subscription, so there is no API cost.
#
# Usage:
#   APP_PASSWORD="pick-a-password" ./share.sh
#
# It prints a https://....lhr.life URL. Give that URL + the password to your
# boyfriend; he opens it in any browser, on any network. Keep this terminal open
# (and your Mac awake) while sharing. Ctrl+C stops sharing.

set -e
cd "$(dirname "$0")"

if [ -z "$APP_PASSWORD" ]; then
  echo "ERROR: set a password first so your financial data is protected, e.g.:"
  echo '  APP_PASSWORD="our-secret" ./share.sh'
  exit 1
fi

PORT=8501

# Start Streamlit (headless, password-protected) if it isn't already running.
if ! curl -s -o /dev/null "http://localhost:$PORT/"; then
  echo "Starting the app on port $PORT ..."
  APP_PASSWORD="$APP_PASSWORD" nohup streamlit run app.py \
    --server.headless true --server.port "$PORT" \
    --server.enableCORS false --server.enableXsrfProtection false \
    >/tmp/spending_app.log 2>&1 &
  sleep 6
else
  echo "App already running on port $PORT."
fi

echo ""
echo "Password to give your boyfriend: $APP_PASSWORD"
echo "Opening public tunnel — share the https://....lhr.life URL printed below."
echo "Keep this window open and your Mac awake. Ctrl+C stops sharing."
echo ""

exec ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=60 \
  -R 80:localhost:"$PORT" nokey@localhost.run
