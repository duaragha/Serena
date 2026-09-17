# Serena's phone line: liblinphone + a private PulseAudio, bridged to /ws/desk.
#   build:  docker build -f voice/phone/Dockerfile -t serena-phone:local .
# The context is the repo root; only voice/ is copied in.
FROM ubuntu:24.04

ARG LINPHONE_WHEEL=https://download.linphone.org/releases/linphone-python/5.5/linux-x86_64/linphone-5.5.21-cp312-cp312-linux_x86_64.whl
ARG LINPHONE_APPIMAGE=https://download.linphone.org/releases/linux/app/Linphone-6.2.2-x86_64.AppImage

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       python3 libpython3.12t64 python3-numpy python3-websocket ca-certificates curl unzip \
       pulseaudio pulseaudio-utils libasound2t64 \
       libssl3t64 libldap2 libmysqlclient21 libjsoncpp25 libudev1 libxml2 libzstd1 libcap2 \
       libgl1 libegl1 libx11-6 libxext6 libv4l-0t64 libglib2.0-0t64 \
    && rm -rf /var/lib/apt/lists/*

# The wheel's own mediastreamer has no sound cards, so its Python wrapper is
# paired with the desktop AppImage's libraries, which are the same SDK line
# built with PulseAudio.
RUN set -eux; mkdir -p /tmp/lp /opt/linphone/linphone; cd /tmp/lp; \
    curl -fsSLo lp.whl "$LINPHONE_WHEEL"; unzip -q lp.whl -d wheel; \
    curl -fsSLo lp.AppImage "$LINPHONE_APPIMAGE"; chmod +x lp.AppImage; \
    ./lp.AppImage --appimage-extract >/dev/null; \
    cp -a squashfs-root/usr/lib/*.so* /opt/linphone/linphone/; \
    cp -a wheel/linphone/pylinphone.so wheel/linphone/__init__.py wheel/linphone/share /opt/linphone/linphone/; \
    for lib in libjsoncpp.so.25 libsqlite3.so liblime.so.0 libbelr.so.1 libxml2.so.2; do \
      [ -e "/opt/linphone/linphone/$lib" ] || cp -a wheel/linphone/$lib* /opt/linphone/linphone/; \
    done; \
    cd /; rm -rf /tmp/lp; \
    ! ldd /opt/linphone/linphone/pylinphone.so | grep "not found"

RUN useradd --create-home --uid 1500 phone && mkdir -p /data /run/phone && chown phone /data
COPY voice/__init__.py /app/voice/__init__.py
COPY voice/call /app/voice/call
COPY voice/desk /app/voice/desk
COPY voice/phone /app/voice/phone
RUN chmod +x /app/voice/phone/entrypoint.sh

ENV PYTHONPATH=/opt/linphone:/app \
    PYTHONUNBUFFERED=1 \
    HOME=/home/phone
USER phone
WORKDIR /app
RUN python3 -c "import linphone, voice.phone.bridge"
ENTRYPOINT ["/app/voice/phone/entrypoint.sh"]
