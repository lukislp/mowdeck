# SPDX-License-Identifier: Apache-2.0
# Throwaway image for the step 0 spike only; never published. The Agora native libraries are
# downloaded by the SDK wrapper while this image is built on the operator's own machine.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && python -c "import agora.rtc.agora_service" \
    && cat /usr/local/lib/python3.12/site-packages/agora/version.txt
WORKDIR /work
COPY bridge_spike.py get_session.py analyze.py /code/
