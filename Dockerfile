ARG ALPINE_VERSION=3.22

# ustreamer isn't packaged for Alpine, so build it from a pinned release.
FROM alpine:${ALPINE_VERSION} AS ustreamer

ARG USTREAMER_VERSION=6.67
ARG USTREAMER_COMMIT=0e64f1fedab4030c7bf58a287c462e21919c1d48

RUN apk add --no-cache build-base git libbsd-dev libevent-dev libjpeg-turbo-dev linux-headers

# -include fcntl.h: stream.c calls open() without including <fcntl.h>, which
# glibc provides indirectly but musl doesn't.
RUN git clone --quiet --depth 1 --branch "v${USTREAMER_VERSION}" https://github.com/pikvm/ustreamer /src \
    && test "$(git -C /src rev-parse HEAD)" = "${USTREAMER_COMMIT}" \
    && make -C /src apps -j"$(nproc)" CFLAGS="-O3 -include fcntl.h" \
    && install -D -s /src/src/ustreamer.bin /out/ustreamer

# Sipeed's official NanoKVM-USB browser build (static files).
FROM alpine:${ALPINE_VERSION} AS frontend

ARG NANOKVM_USB_VERSION=1.1.4

RUN wget -q -O /tmp/browser.zip \
      "https://github.com/sipeed/NanoKVM-USB/releases/download/v${NANOKVM_USB_VERSION}/nanokvm-usb-browser-v${NANOKVM_USB_VERSION}.zip" \
    && mkdir /www \
    && unzip -q /tmp/browser.zip -d /www \
    && printf '%s' "${NANOKVM_USB_VERSION}" > /www/.nanokvm-usb-version

FROM alpine:${ALPINE_VERSION}

# Audio needs alsa-utils; set to true (and AUDIO=on) to enable it.
ARG WITH_AUDIO=false

RUN apk add --no-cache libbsd libevent libjpeg-turbo python3 py3-aiohttp py3-pyserial \
    && if [ "$WITH_AUDIO" = "true" ]; then apk add --no-cache alsa-utils; fi

COPY --from=ustreamer /out/ustreamer /usr/local/bin/ustreamer
# Installed into the /data volume on startup; see updater.install_baked().
COPY --from=frontend /www /www-image
COPY server/bridge.py server/updater.py web/nanokvm-pi-shim.js dev/fake_mjpeg.py /app/

EXPOSE 80

ENTRYPOINT ["python3", "/app/bridge.py"]
