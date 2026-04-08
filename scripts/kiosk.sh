#!/bin/bash
set -e
rm -rf ~/.config/chromium/* || true
xset -display :0 dpms force on
xset -display :0 -dpms
xset -display :0 s noblank
xset -display :0 s off
xrandr -o left
chromium-browser --no-memcheck --noerrdialogs --incognito --kiosk --no-default-browser-check --no-first-run --disable-translate --disable-cache --disk-cache-dir=/dev/null --disk-cache-size=1 --autoplay-policy=no-user-gesture-required $SERVER_URL &