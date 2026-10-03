# Bundled software

Exact versions, URLs, SHA-256 values and upstream license references are recorded in `dependencies.json`.

- CPython: Python Software Foundation License 2.0. The standalone distribution also contains OpenSSL, SQLite, libffi, zlib and other libraries with their own licenses. All upstream runtime license files are preserved in the bundle.
- python-build-standalone: build tooling maintained by Astral; runtime licenses are described in its licensing documentation.
- cloudflared: Cloudflare, Apache License 2.0. See the pinned upstream LICENSE.
- Mozilla CA certificate bundle, distributed by curl: Mozilla Public License 2.0.
- Sparkle 2.7.3: MIT license, pinned to commit `06beff60e3b609a485290e4d5d2d1e2eedb1e55d` and archive SHA-256 `2e0bf15ae74c13e7b16b76e34ba56fe3a44683e35473298225f4a2a2c274329a`. Its framework and helpers are preserved together. https://github.com/sparkle-project/Sparkle/tree/2.7.3

Release builders must review the pinned dependencies for security updates before publishing. Development pins do not imply current security support.
