# Setting up a Mac mini (or any Mac nobody sits at)

Orchestrator can run on a Mac you only reach over SSH, such as a Mac mini in a closet. It's the same app as the download. It's just installed and connected from the command line, and it keeps itself updated.

## Before you start (once, at the Mac or over Screen Sharing)

The agent runs as a logged-in user, because builds, simulators and code signing need a user session. So the Mac has to log in by itself and stay up.

1. **Automatic login** for the user Orchestrator will work as: System Settings › Users & Groups › Automatically log in as. macOS only allows this with FileVault off, so keep the Mac somewhere physically safe.
2. **Never sleep, and restart after a power cut:**
   ```bash
   sudo pmset -a sleep 0 disksleep 0 autorestart 1
   ```
3. **Remote Login** (SSH): System Settings › General › Sharing.
4. **Xcode**, if the project needs it: install it, open it once, and accept the license (`sudo xcodebuild -license accept`). Sign in to your Apple developer account in Xcode if builds need signing.
5. **Git access** for the user: an SSH key added to GitHub, or `gh auth login`, so the project can be cloned without a password prompt.
6. **macOS updates**: decide whether they install automatically. If they do, the Mac restarts, logs in by itself and Orchestrator comes back on its own.

## Add it to your account

In the web app, open your computers, choose **Add a Mac nobody sits at**, enter the project (a git URL, or a folder already on that Mac) and choose **Make a command**. Run the command over SSH as that user. The command works once, within 15 minutes:

```bash
curl -fsSL https://<your hosted app>/install-mac.sh | sh -s -- --token enroll_… --project git@github.com:you/app.git
```

It downloads the app for the Mac's chip, checks the checksum and Apple's signature, installs it in Applications, connects the Mac to your account, sets up the project and starts Orchestrator in the background at login. You get a notification that a Mac was added. If you didn't add it, remove it from your computers.

If Orchestrator is already installed, the script uses it as it is. If the Mac is already connected to an account, the token isn't used.

Prefer not to have the token in the shell history? Pipe it in instead: `echo enroll_… | sh -s -- --token-stdin --project …`, with the script saved locally.

## If macOS asks for approval

On some setups macOS wants a one-time click before a background item may start at login. The command tells you if so. Connect with Screen Sharing and allow Orchestrator in System Settings › General › Login Items, then run the command again.

## Keeping it ready

The computer list in the web app warns when one of the settings above is off: automatic login, sleep, restart after a power cut, the Xcode license or the login item. Each warning shows the fix. These are only checked, never changed: changing them needs an administrator.

## Updates

**Install updates automatically** is on by default. The app checks every six hours and installs an update only after ten minutes with no work running. It then relaunches by itself. You can also choose **Update** next to the Mac in the web app; that still waits for running work to finish.
