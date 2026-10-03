"""mind-dj - a personal DJ that learns what you like, when you like it."""
__version__ = "0.1.0"

# AVG Web/Mail Shield MITMs TLS with its own root CA, which certifi does not
# trust. truststore makes Python use the Windows cert store instead, where
# AVG's root actually lives. Must run before any HTTPS client is constructed.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:  # pragma: no cover - non-fatal, plain certifi still works
    pass
