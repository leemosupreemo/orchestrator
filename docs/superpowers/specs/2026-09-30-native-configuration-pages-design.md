# Native Configuration Pages Design

## Purpose

Replace the Web UI's flat Configuration screen and terminal-based submenu handoffs with focused native web pages that a non-technical user can understand and operate safely.

The Configuration navigation item becomes the entry point to a context menu. Each usable submenu opens a dedicated, bookmarkable page. Configuration tools that do not yet have native web APIs remain visible but disabled until a later phase; the Web UI must not send users into terminal screens.

## Scope

This design establishes the complete navigation architecture and delivers the first set of native pages using capabilities already exposed by the Web UI backend.

Phase one includes:

- API Keys
- Base Branch
- Projects, through the existing Projects page
- Archived Jobs
- Email Notifications
- Documentation

Later phases will add native APIs and pages for:

- Models
- AI Instructions
- Machine Fleet
- Firebase App Distribution
- Xcode Cloud
- Prerequisite Audit
- Orchestrator Self-Tests
- Updates
- Setup Wizard, once its interactive workflow has a native web replacement

The flat all-settings page, terminal-launching configuration actions, and the full console entry are not part of the new configuration navigation.

## Navigation Architecture

### Configuration entry point

Clicking the Configuration item in the primary navigation opens its context menu. The adjacent submenu-arrow button is removed so there is one obvious interaction target.

The menu groups entries under plain-language headings. Enabled items navigate to native pages. Unavailable items are disabled and labeled `Coming next`; they do not start a terminal session.

The menu closes after navigation, on an outside click, or on Escape. It supports keyboard focus and communicates disabled state through both HTML semantics and visible text.

### Routes

Native configuration pages use stable routes:

- `#/config/api-keys`
- `#/config/base-branch`
- `#/config/archived-jobs`
- `#/config/email`
- `#/config/documentation`

Projects continues to use `#/projects` because it is already a first-class application area.

`#/config` does not render a flat overview. Navigating to it opens the context menu when initiated through the UI; a direct URL visit renders a small configuration chooser using the same registry so bookmarks and reloads never produce an empty screen.

### Shared registry

A single configuration registry defines every submenu's:

- stable identifier;
- label and short description;
- group;
- route;
- availability;
- optional `Coming next` status.

The navigation context menu and direct configuration chooser both render from this registry. Route handlers are kept separately so display metadata cannot execute actions by itself.

## Shared Page Experience

Every native configuration page uses a consistent structure:

- a Configuration breadcrumb or back action;
- a plain-language title and one-sentence explanation;
- a concise current-status summary;
- controls for one configuration responsibility only;
- inline progress, validation, success, and error feedback.

Pages must not assume command-line knowledge. Labels describe the user outcome, and technical details appear only as supporting text when needed.

Submitting controls are disabled while a mutation is pending. Failed mutations retain the user's entered values and show an actionable message. Successful mutations refresh only the active page rather than navigating back to a configuration overview.

## Phase-One Pages

### API Keys

Show each supported provider and whether its key is saved, inherited from the environment, or missing. Users can set, replace, or clear saved keys. Ollama also exposes its optional host URL. Secret values are never returned by the server or redisplayed after submission.

### Base Branch

Explain that the base branch is the comparison point for jobs. Present only local branches supplied by the backend, identify the current selection, and save through the existing validated endpoint.

### Projects

The context menu links to the existing Projects page. That page remains responsible for adding, forgetting, and switching projects.

### Archived Jobs

List archived jobs with their identifier, title, and final status. Valid entries have a Restore action. Corrupt entries are labeled and cannot be restored.

### Email Notifications

Show the selected delivery provider, sender readiness, and recipient list. Users can add or remove recipients, configure Gmail or Resend credentials, and send a test email when at least one recipient exists. Password and API-key fields never display saved secret values.

### Documentation

Group Orchestrator documentation separately from project documentation. Selecting a document opens its content in the existing web dialog. The server remains responsible for resolving only allowlisted document identifiers.

## Backend Boundaries

Phase one reuses the existing `GET /api/config` response and validated `POST /api/config/{part}` mutations. No terminal process is required for these pages.

Later native pages must add explicit state and mutation endpoints rather than wrapping interactive console menus. Endpoints will return structured JSON, validate identifiers and paths server-side, avoid exposing secrets, and report errors suitable for plain-language display.

The existing `config_menu` action may remain for backward compatibility outside the new configuration UI, but no new page or menu entry will call it.

## Error Handling and Concurrency

- A page that cannot load configuration state shows a focused retry message.
- A failed mutation leaves the page and input values intact.
- Repeated submissions are blocked until the current request completes.
- A response received after the user leaves a page must not overwrite the new page.
- A `404` from an older backend produces an update-required explanation rather than exposing raw errors.
- Secret inputs are cleared after successful submission and never placed in URLs, logs, or page HTML.

## Accessibility and Responsive Behavior

- The Configuration trigger exposes menu state and relationship with `aria-expanded`, `aria-haspopup`, and `aria-controls`.
- The menu is keyboard navigable and returns focus to the trigger when closed.
- Disabled entries use native disabled controls and visible `Coming next` text.
- Each page has a unique heading and descriptive form labels.
- Status is not conveyed by color alone.
- Pages use existing responsive card and form patterns and remain usable at phone widths.

## Testing

Tests will verify:

- the registry generates the expected enabled and disabled entries;
- clicking Configuration opens the context menu instead of navigating to the old flat page;
- direct configuration routes resolve to the correct page;
- each phase-one page renders its relevant state;
- mutations send literal, validated payloads and refresh only the active page;
- pending controls cannot submit twice;
- error responses preserve inputs and display feedback;
- secret values never appear in API responses or rendered output;
- unavailable entries cannot launch terminal actions;
- existing Projects behavior remains intact.

Canonical repository compilation and test commands remain those in `docs/build-test-commands.md`.

## Delivery Phases

1. Introduce the shared registry, context-menu behavior, dedicated routing, and the six phase-one native pages.
2. Add native Models and AI Instructions APIs and pages.
3. Add native Machine Fleet management.
4. Add native delivery pages for Firebase and Xcode Cloud.
5. Add native Audit, Self-Tests, Update, and Setup Wizard workflows.

Each phase must leave every visible enabled menu item fully functional. A later phase enables its entries only after its native page and backend operations are complete.
