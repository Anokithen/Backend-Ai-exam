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
  shape=$(curl -s --max-time 20 "$API/api/auth/login" -X POST \
    -H 'Content-Type: application/json' -d '{}' | head -c 120)
  case "$shape" in
    *NOT_FOUND*)
      echo "   FAIL 404 from Flask - the request reached the app, but nothing serves that path."
      echo "        Body: $shape" ;;
    *)
      echo "   FAIL 404, and NOT from Flask - the request never reached the app."
      echo "        Body: ${shape:-empty}"
      echo "        That is the platform's own 404: wrong domain, or the domain points at"
      echo "        a different service. Check the public domain on the Railway service." ;;
  esac
  echo "        Whatever the frontend calls must be exactly: $API/api"
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

echo "4. what URL is the deployed frontend actually calling?"
page=$(curl -s --max-time 20 "$ORIGIN/login")
if [ -z "$page" ]; then
  echo "   ??   could not fetch $ORIGIN/login - skipping."
else
  chunks=$(printf '%s' "$page" | grep -o '/_next/static/[^"]*\.js' | sort -u | head -25)
  # Bundles also contain URLs from error-message text and framework docs; drop those, and
  # the placeholder host used in this repo's own build-time hint, so only real bases remain.
  found=$(for c in $chunks; do curl -s --max-time 20 "$ORIGIN$c"; done \
    | grep -oE 'https?://[A-Za-z0-9._-]+(:[0-9]+)?(/[A-Za-z0-9._-]+)*/api' | sort -u \
    | grep -v -e googleapis -e gstatic -e cloudinary -e nextjs\.org -e vercel\.com \
              -e 'your-app\.up\.railway\.app' -e 'your-service')
  if [ -z "$found" ]; then
    echo "   ??   no API base found in the bundle. If the build is stale or the page is"
    echo "        behind auth, read the request URL from the Network tab instead."
  elif printf '%s\n' "$found" | grep -qx "$API/api"; then
    echo "   ok   built with $API/api - matches this backend."
  else
    echo "   FAIL the bundle calls:"
    printf '%s\n' "$found" | sed 's/^/          /'
    echo "        none of which is $API/api"
    echo "        Set NEXT_PUBLIC_API_URL=$API/api in Vercel, then REDEPLOY -"
    echo "        NEXT_PUBLIC_* is baked in at build time, so editing it alone changes nothing."
    fail=1
  fi
fi
echo

[ "$fail" -eq 0 ] && echo "all checks passed - the browser should be able to reach the API." \
                  || echo "see the FAIL lines above."
exit "$fail"
