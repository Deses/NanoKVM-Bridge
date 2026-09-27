# Stage 1: Sipeed's official NanoKVM-USB browser build, unmodified.
FROM debian:bookworm-slim AS fetch

ARG NANOKVM_USB_VERSION=1.1.4

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl unzip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /src
RUN curl -fsSL -o browser.zip \
      "https://github.com/sipeed/NanoKVM-USB/releases/download/v${NANOKVM_USB_VERSION}/nanokvm-usb-browser-v${NANOKVM_USB_VERSION}.zip" && \
    mkdir -p /www && \
    unzip -q browser.zip -d /www && \
    rm browser.zip && \
    echo -n "${NANOKVM_USB_VERSION}" > /www/.nanokvm-usb-version

# Classic script, so the shim runs before the app's module bundle.
COPY web/nanokvm-pi-shim.js /www/nanokvm-pi-shim.js
RUN sed -i 's#<head>#<head>\n    <script src="/nanokvm-pi-shim.js"></script>#' /www/index.html && \
    grep -q nanokvm-pi-shim.js /www/index.html

# Stage 2: runtime. bridge.py serves HTTP itself: no nginx, htpasswd or tini.
FROM debian:bookworm-slim

# Audio needs alsa-utils; set to true (and AUDIO=on) to enable it.
ARG WITH_AUDIO=false

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      ustreamer \
      python3 \
      python3-aiohttp \
      python3-serial \
    && if [ "$WITH_AUDIO" = "true" ]; then \
         apt-get install -y --no-install-recommends alsa-utils; \
       fi \
    && rm -rf /var/lib/apt/lists/*

# Baked-in build; entrypoint.sh seeds the /data volume from it on first boot.
COPY --from=fetch /www /www-image

COPY server/bridge.py /app/bridge.py
COPY server/updater.py /app/updater.py
COPY dev/fake_mjpeg.py /app/fake_mjpeg.py
# Reference copy of the shim, injected into downloaded releases.
COPY web/nanokvm-pi-shim.js /app/nanokvm-pi-shim.js
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
