#!/usr/bin/env bash
# Publish the page on the tailnet over HTTPS. Only needs running once — tailscale persists the
# mapping across reboots.
#
# HTTPS rather than plain http over the LAN, for two reasons that only show up on the phone:
# Safari treats an insecure origin as second-class and the Media Session API — the lock-screen
# artwork and the skip buttons — is one of the things it withholds. And a tailnet address works
# from anywhere, not only from the sofa.
#
# 8444 because speech-webui already has 8443 on this tailnet, and 443 was taken before that.
set -e
tailscale serve --bg --https=8444 "http://127.0.0.1:${AUDIOBOOK_PORT:-8610}"
echo
tailscale serve status
