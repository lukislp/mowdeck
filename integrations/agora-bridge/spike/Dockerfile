# SPDX-License-Identifier: Apache-2.0
# Throwaway image for the step 0 spike only; never published. The Agora native libraries are
# downloaded by the SDK wrapper while this image is built on the operator's own machine.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir agora-python-server-sdk==2.4.9 nats-py==2.11.0 \
    && python -c "import agora.rtc.agora_service" \
    && cat /usr/local/lib/python3.12/site-packages/agora/version.txt
WORKDIR /work
COPY bridge_spike.py get_session.py analyze.py /code/
