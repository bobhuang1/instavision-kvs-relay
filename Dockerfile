# InstaVision relay image.
#
# The only system dependency is ffmpeg: the relay copies H.264 access units into
# it as Annex-B on stdin and ffmpeg does the RTSP handshake and RTP packetisation
# with `-c copy`. Everything else (WebRTC, TURN, the KVS signalling client) comes
# from Python wheels.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg wget ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# aiortc brings aioice (ICE/TURN), pylibsrtp (SRTP) and av (H.264 parsing);
# websockets runs the KVS signalling socket.
RUN pip install aiortc==1.15.0 websockets==17.4

COPY probe.py kvs_viewer.py relay.py ./

# Not root: the relay only needs to write its own state directory.
RUN mkdir -p /app/state && useradd --uid 10001 --create-home relay \
 && chown -R relay:relay /app
USER relay

EXPOSE 8655
CMD ["python3", "relay.py"]
