# AI tool removal and Gemini migration

Add an owner-only Uninstall button for installed AI tools, with a confirmation
dialog naming the connected computer and explaining that settings, histories,
API keys and downloaded models are preserved. Uninstall only recognized native,
npm or Homebrew installations; never run a user-provided command or remove an
unrecognized path. Refuse removal while work is active and block new work until
removal completes. Use the existing asynchronous task and error UI.

Google's May 19, 2026 announcement retires personal Google-account Gemini CLI
access on June 18, 2026, while retaining API-key and enterprise access:
https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/
Show a legacy notice with this distinction and a direct Antigravity setup action.

- [ ] Add mocked uninstall tests covering installation methods, unsupported paths,
  failed commands, concurrent work and confirmation/owner requirements.
- [ ] Implement installation-aware removal and exclusive task admission.
- [ ] Add confirmation UI, refresh installed status, and Gemini migration notice.
- [ ] Verify loading screens, Configure refresh, AI tool UI and server behavior.

No installed tool is to be uninstalled as part of implementing or testing this feature.
