"""``vision-capture``: still frames from a live camera without an open browser (ADR 0035).

The Device-Gateway asks this worker for one frame when an adapter offers a live camera session
(the Mammotion cloud's Agora channel) but cannot take a photo itself. The worker joins the
channel in a headless Chromium with the Agora Web SDK, subscribes to the requested camera,
waits for the first decoded frame, draws it into a canvas and answers with a JPEG. It runs as a
separate process from the Vision image built with ``--build-arg WITH_CAPTURE=true``
(Playwright and Chromium); the normal Vision image stays without a browser.

* :mod:`vision.capture.settings`: ``MOWDECK_CAPTURE_*``.
* :mod:`vision.capture.grabber`: the ``FrameGrabber`` port, the Playwright grabber and a fake.
* :mod:`vision.capture.app`: ``POST /api/v1/capture`` with a shared token, a concurrency limit
  and timeouts; ``python -m vision.capture`` serves it.
"""
