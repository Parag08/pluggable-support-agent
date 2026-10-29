"""Ports: the only ways the agent core reaches anything outside itself.

Each port is a `typing.Protocol`. A company plugs in an adapter (see `app/adapters/`)
that implements it against its own systems. `tests/conformance/` holds the tests an
adapter must pass.
"""
