# Opt-in real-login fixture

Run only in a disposable macOS user profile, never against the developer's existing service. Automated tests use injected registration adapters and do not call launchctl or SMAppService registration.

Build a distinct application:

```
python3 packaging/macos/build.py --arch arm64 --output dist/login-fixture --build-number 1 --development --integration-fixture
```

The fixture uses bundle ID `com.orchestrator.desktop.integration`, agent label `com.orchestrator.desktop.integration.agent`, state `~/.orchestrator-desktop-test` and `Library/Application Support/Orchestrator/test/agent.sock`. It never targets `com.orchestrator.ui` or normal desktop state. Developer-ID signing is needed to exercise all approval behavior reliably; an ad-hoc build is not release acceptance.

Copy this fixture app into that profile's Applications folder. Launch it, explicitly enable Start at login, approve it in Login Items, log out/in and verify both the menu and independent agent return. Quit the menu during an active run and confirm work continues. With no work active, terminate only the fixture agent identified by its private socket/lease, verify launchd restarts it, and check Stop removes the fixture KeepAlive registration. Disable login during work, confirm the pending state, then finish the work and verify exactly one on-demand agent remains. Remove background components and verify its project/history files remain.

These real-login checks have not been run in the developer's profile. Record OS version, architecture, signing identity and observed transitions before approving a release.
