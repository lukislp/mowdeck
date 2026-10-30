"""MowDeck adapter for Worx Landroid, Kress and LandXcape mowers on pyworxcloud. GPL-3.0-only.

This package is GPL-3.0 because it imports pyworxcloud, whose sdist ships a GPL-3.0 licence
(its PyPI metadata says MIT; the file wins), see ADR 0011 and ADR 0066. It plugs into
``mowdeck-control`` as a cloud provider (entry point group ``mowdeck.cloud_providers``:
``worx``, :data:`~mowdeck_control_worx.plugin.SPEC`) and ships its error lexicon
(``mowdeck.error_lexicons``: ``worx``). Untested on real hardware and off by default.

Importing this package does not import pyworxcloud: only
:mod:`~mowdeck_control_worx.pyworx_session` does, when a session at the real cloud opens.
"""

from mowdeck_control_worx.descriptor import DESCRIPTOR, PROVIDER
from mowdeck_control_worx.snapshot import WorxMower

__all__ = ["DESCRIPTOR", "PROVIDER", "WorxMower"]
