# Bundled software

Exact versions, URLs, SHA-256 values and upstream license references are recorded in `dependencies.json`.

- CPython: Python Software Foundation License 2.0. The standalone distribution also contains OpenSSL, SQLite, libffi, zlib and other libraries with their own licenses. All upstream runtime license files are preserved in the bundle.
- python-build-standalone: build tooling maintained by Astral; runtime licenses are described in its licensing documentation.
- cloudflared: Cloudflare, Apache License 2.0. See the pinned upstream LICENSE.
- Mozilla CA certificate bundle, distributed by curl: Mozilla Public License 2.0.

Release builders must review the pinned dependencies for security updates before publishing. Development pins do not imply current security support.
