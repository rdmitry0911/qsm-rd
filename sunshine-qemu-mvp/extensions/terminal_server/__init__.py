"""Server-side building blocks for the single q-sunshine terminal service.

The package intentionally has no import-time side effects.  In particular it
never reads a PVE credential or a TLS private key merely because a unit-test
imports it.
"""
