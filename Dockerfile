# Stage 1: Sipeed's NanoKVM-USB browser build, plus the tag that loads the shim.
FROM debian:bookworm-slim AS fetch

ARG NANOKVM_USB_VERSION=1.1.4

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/browser.zip \
      "https://github.com/sipeed/NanoKVM-USB/releases/download/v${NANOKVM_USB_VERSION}/nanokvm-usb-browser-v${NANOKVM_USB_VERSION}.zip" \
    && unzip -q /tmp/browser.zip -d /www \
    && printf '%s' "${NANOKVM_USB_VERSION}" > /www/.nanokvm-usb-version \
    && sed -i 's#<head>#<head>\n    <script src="/nanokvm-pi-shim.js"></script>#' /www/index.html \
    && grep -q nanokvm-pi-shim.js /www/index.html

# Stage 2: runtime - ustreamer for video, Python/aiohttp for everything else.
FROM debian:bookworm-slim

# Audio needs alsa-utils; set to true (and AUDIO=on) to enable it.
ARG WITH_AUDIO=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      python3 \
      python3-aiohttp \
      python3-serial \
      ustreamer \
    && if [ "$WITH_AUDIO" = "true" ]; then \
         apt-get install -y --no-install-recommends alsa-utils; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Copied into the /data volume by entrypoint.sh; see there for when.
COPY --from=fetch /www /www-image

COPY server/bridge.py server/updater.py web/nanokvm-pi-shim.js dev/fake_mjpeg.py /app/
COPY entrypoint.sh /entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
