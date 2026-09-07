#!/usr/bin/env bash
# Check a deployed backend from outside the browser, where the real error is visible.
#
#   ./check-deploy.sh https://your-app.up.railway.app https://your-app.vercel.app
#
# Arg 1: the Railway public URL, no /api and no trailing slash.
# Arg 2: the Vercel origin the browser sends, no path and no trailing slash.

set -u

API="${1:-}"
ORIGIN="${2:-}"

if [ -z "$API" ] || [ -z "$ORIGIN" ]; then
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
fi

API="${API%/}"
ORIGIN="${ORIGIN%/}"
# Accept the URL either way: the checks below append /api themselves.
case "$API" in
  */api) API="${API%/api}"; echo "(dropped the /api suffix; checking the host itself)" ;;
esac
fail=0

echo "backend: $API"
echo "origin:  $ORIGIN"
echo

echo "1. is the backend up?"
code=$(curl -s -o /tmp/_health.$$ -w '%{http_code}' --max-time 20 "$API/health")
body=$(cat /tmp/_health.$$ 2>/dev/null | head -c 200); rm -f /tmp/_health.$$
if [ "$code" = "200" ]; then
  echo "   ok   $code $body"
else
  echo "   FAIL $code ${body:-no response}"
  echo "        The container is not serving. Check Railway's deploy logs for a boot error,"
  echo "        and that the service has a public domain generated."
  fail=1
fi
echo

echo "2. does /api exist at that URL?"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$API/api/auth/login" -X POST \
  -H 'Content-Type: application/json' -d '{}')
if [ "$code" = "400" ] || [ "$code" = "401" ] || [ "$code" = "422" ]; then
  echo "   ok   $code (route exists and rejected the empty body, as it should)"
elif [ "$code" = "404" ]; then
  echo "   FAIL 404 - no route there."
  echo "        NEXT_PUBLIC_API_URL must be $API/api  (with the /api suffix)."
  fail=1
else
  echo "   ??   $code - unexpected; see Railway's logs."
  fail=1
fi
echo

echo "3. will the browser be allowed to call it? (CORS preflight)"
allow=$(curl -s -D - -o /dev/null --max-time 20 -X OPTIONS "$API/api/auth/login" \
  -H "Origin: $ORIGIN" \
  -H 'Access-Control-Request-Method: POST' \
  -H 'Access-Control-Request-Headers: content-type,authorization' \
  | tr -d '\r' | grep -i '^access-control-allow-origin:' | head -1)
if [ -n "$allow" ]; then
  echo "   ok   $allow"
else
  echo "   FAIL no Access-Control-Allow-Origin for $ORIGIN"
  echo "        Set CORS_ORIGINS on Railway to include exactly: $ORIGIN"
  echo "        (no trailing slash; add https://your-app-*.vercel.app to cover previews)"
  fail=1
fi
echo

[ "$fail" -eq 0 ] && echo "all checks passed - the browser should be able to reach the API." \
                  || echo "see the FAIL lines above."
exit "$fail"
