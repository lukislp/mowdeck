"""Husqvarna Automower adapter for mowdeck-control (ADR 0065), untested on real hardware.

The package plugs into the Device-Gateway only through the entry point group
``mowdeck.cloud_providers`` (:data:`~mowdeck_control_husqvarna.plugin.SPEC`). Importing it does
not import aioautomower; the modules that do are loaded when the provider is enabled:

* :mod:`~mowdeck_control_husqvarna.descriptor`: the credential fields (application key and
  secret) and the guidance shown when linking an account;
* :mod:`~mowdeck_control_husqvarna.auth`: the OAuth2 client-credentials token and the
  request throttle (1 request per second per application key);
* :mod:`~mowdeck_control_husqvarna.account`: one shared Automower Connect session (token,
  websocket, rare REST refresh) per application key, :mod:`~mowdeck_control_husqvarna.pool`
  shares it between the discovery and the account's mowers;
* :mod:`~mowdeck_control_husqvarna.mapping`: vendor data to MowDeck state and capabilities;
* :mod:`~mowdeck_control_husqvarna.connection`: the ``cloud`` :class:`~mowdeck_control.\
MowerConnection` of one mower;
* :mod:`~mowdeck_control_husqvarna.fake_cloud`: a fake Husqvarna cloud for development and the
  browser tests (``ProviderContext.fake``).
"""

from mowdeck_control_husqvarna.descriptor import DESCRIPTOR, PROVIDER

__version__ = "0.1.0"

__all__ = ["DESCRIPTOR", "PROVIDER"]
