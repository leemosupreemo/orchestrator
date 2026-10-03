# Control plane

Orchestrator does the work on each person's own computer. The hosted app (`https://swift-orch-web-20260923.web.app`) is how they reach it from anywhere: they sign in with Google, pick one of their computers, and use it as if it were local. The control plane is the small service behind that. It knows which computers belong to which account, whether each is online, and where browsers reach it. It never sees code, model subscriptions or integration tokens.

```
Browser ── hosted app (static UI + Firebase sign-in)
   │          └─ /cp/** ── Cloud Function `cp` ── Firestore (machines, pairings, machine secrets)
   │                                   ▲
   │                                   │ pairing, heartbeat every 60 s
   └── https ──► the computer's address ── `orchestrator ui` (the runner)
```

## Pieces

| Where | What |
|---|---|
| `cloud/functions/control_plane.py` | All the logic, over a small storage interface (tested in `tests/test_control_plane.py`). |
| `cloud/functions/main.py` | The Cloud Function: Firestore storage and Firebase ID token checks. |
| `firestore.rules` | Deny everything: browsers only reach Firestore through the function. |
| `firebase.json` | Hosting rewrites `/cp/**` to the function, so the hosted page calls it on its own origin. |
| `orchestrator/account.py` | The computer's side: `orchestrator connect`, the heartbeat, checking tickets. Standard library only. |
| `orchestrator/web/static/account.js` | The hosted app's screens: your computers, adding one, opening one. |

## Adding a computer

1. `orchestrator connect` asks the control plane for an 8-character code and prints it with a link (`/#/connect?code=…`).
2. The person opens the link (or types the code), signs in, sees the computer's name and confirms.
3. The computer, polling with a secret only it has, receives its **machine secret** once, saved to `~/.orchestrator/machine.json` (mode 600). The account's email is now allowed to sign in to it (shown as *account* under **Configuration → Who can sign in**).

Codes expire after 10 minutes and can be claimed once. An account can have up to 20 computers.

## Opening a computer

1. The hosted app lists the account's computers (`GET /cp/machines`). One is online when it reported in the last 3 minutes, and reachable when it also has an `https://` address.
2. It asks for a **ticket** for one of them. The ticket names the computer, the person and a nonce, expires in 2 minutes, and is signed (HMAC-SHA256) with that computer's secret.
3. It sends the ticket to the computer's `/api/auth`. The computer checks the signature, that the ticket is for it, the expiry, and that the nonce hasn't been used, then issues its own sign-in (see [user guide](user-guide.md#web-ui)).

A ticket for one computer is useless on another. This is why the hosted app doesn't hand the computer the Firebase ID token: an ID token is valid for an hour on any computer that accepts it.

## Heartbeat

While `orchestrator ui` runs on a paired computer, it reports every 60 seconds with its version and its `https://` address (from `--tunnel`, `--tunnel-token` with `--public-url`, or `--public-url`). If the account removed the computer, the reply says so and the computer forgets its pairing.

## Notifications

Alerts reach a person's phones and browsers even with Orchestrator closed. They turn them on with **Notify me when done** in the hosted app (on iPhone and iPad, after adding it to the Home Screen: iOS only allows web push for home-screen apps).

- **From the computer.** The UI server already notices when something starts waiting on you or a run finishes or fails (`notifier.Tracker`). On a paired computer it also sends those events to `/cp/machine/events`, and the control plane pushes them through Firebase Cloud Messaging to every device the owner turned alerts on in. Tapping one opens the hosted app on that computer (`?machine=…`) at the page it is about.
- **From the control plane.** The heartbeat includes how many runs are going. A scheduled function (`sweep`, every 5 minutes) alerts the owner once when a computer stops reporting while runs were going. An idle computer going to sleep is not news.
- **What passes through.** The event's title (with the project name) and one line of text, on their way to the push service. Only each device's push token is stored, and tokens a push service reports as gone are dropped.
- Pushes are data-only and shown by the app's service worker (`firebase-messaging-sw.js`). Each carries the same tag as the alert an open tab shows for that event, so the two replace each other instead of doubling up.
- Slack webhooks are still sent by the computer itself (Configuration → Slack & chat alerts).

## Deploying

Needs the Blaze plan (Cloud Functions) and Firestore enabled on the project.

```bash
cd cloud/functions && python3.12 -m venv venv && ./venv/bin/pip install -r requirements.txt && cd ../..
firebase deploy --only firestore:rules,functions:control-plane,hosting
```

The Python venv must be built with Python 3.12 (the function's runtime): the CLI runs `python3.12` from it.

```bash
# once per project: delete old function images so they don't accrue storage charges
firebase functions:artifacts:setpolicy --location us-central1 --days 1 --force
```

Optional: Firestore TTL policies on the `pairings` and `enrollments` collections' `expire_at` field sweep old codes and enrollment tokens. Expired ones are refused either way.

## Not yet

- Computers are reachable only with an `https://` address the person sets up (`--tunnel`). Next: the control plane provisions a stable tunnel for each computer during pairing.
- Only the owner can open a computer from the hosted app. Sharing one with teammates comes later.
- `pair/start` has no rate limit beyond the function's instance cap.
