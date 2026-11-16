# Architecture step

How Orchestrator chooses, describes, records and changes a project's architecture: the language, frameworks, structure and
packages it is built with, and why. The short version for people using the app is in the user guide under **Architecture**;
this page is the complete reference. Code: `orchestrator/architecture.py` (the logic), the `architecture_*` helpers and
routes in `orchestrator/web/server.py`, and the Architecture section of the Product page in `orchestrator/web/static/app.js`.
Tests: `tests/test_architecture.py`, which also checks that this page covers every mode, status, style and route.

## Why it exists

Without it, a new project's first feature job picks the stack as a side effect of a few small tasks, with no alternatives
weighed and nothing written down, and every AI re-guesses an imported project's architecture. With it there is a current
architecture that plans, reviews and builds follow, tied to the product requirements, and changed deliberately.

## The step is soft

Nothing blocks on it. A person can choose, describe, skip or ignore it:

- **Choose or describe, then approve.** The architecture becomes the *approved decision* (status `approved`, version 1.0 and up).
- **Skip for now / Decide later.** The recommended option of a waiting proposal is kept as a *working assumption*
  (status `provisional`, versions 0.1, 0.2 and so on). With no proposal, the working assumption is inferred from the
  repository without a model (`infer_record`). Skipping never replaces an approved decision; skipping a proposed change to
  one just discards the proposal.
- **Ignore it.** A codeless project with nothing recorded gets a short warning in its plans: don't pick a stack silently,
  state assumptions, and suggest deciding the architecture. Nothing else changes.

A working assumption can be approved as it is (**Approve as it is**), replaced by approving any proposal, refreshed with
**Refresh from the code** when it was inferred and the code has changed since (`inferred_is_stale`), or tweaked and
overhauled like an approved decision. Approving anything over a working assumption makes it version 1.0.

## Kinds of proposal

| Mode | When | What the model is asked for |
|---|---|---|
| `initial` | A project with no code (`is_codeless`) | Two or three genuinely different options, a recommendation, plain pros and cons and a fit score each |
| `describe` | A project with code | The architecture as it is now, from the repository digest, as one option named "Current architecture", and where it misses the requirements |
| `tweak` | Something is recorded and a note says what to add or adjust | The full architecture with only that change; the language, framework and system shape stay |
| `overhaul` | Something is recorded and a note says why to rethink it | Two or three options, the current architecture among them, so staying put is a real choice |

`default_mode` picks `initial` or `describe` for a first proposal. Feedback on a waiting proposal (**Ask for changes**)
revises it in the same mode, with the previous proposal in the prompt and every note kept in its history.

## What every option carries

- **best_for**: one plain sentence on the kind of product it suits.
- **fit**: 0 to 100 against this product's requirements (clamped).
- **pros** and **cons**: everyday words, shown as "Good" and "Watch out".
- **profile**: the weighted style mix (below).
- **diagram**: a very simple diagram (below).

The chosen option's profile and diagram become the record's. The record also holds the stack (primary language,
framework, build and test commands), platforms, layers, folder structure, packages, decisions (each with alternatives,
reason and the requirements it serves), risks, open questions, `revisit_when` triggers, notes, and the pages the model
checked (https only). A proposal is checked before it is shown (`parse_proposal`): at least one option, a chosen option
that matches one of them, and a primary language Orchestrator can build. An unreadable answer is asked for once more.

## Style catalog and weighted profiles

Architectures are mixtures, so each option is described with weights over a fixed catalog. Categories are independent:
each one's weights are normalised to whole percents summing to exactly 100 (`normalize_profile`, largest remainder).
Unknown styles are dropped, and a style given without its category finds its own.

**System shape** (`shape`: what runs where):

| Style id | Name | In plain words |
|---|---|---|
| `client_only` | App only | Everything runs on the device or in the browser; no server of your own. |
| `client_server` | App with its own backend | An app talks to a server you write and run. |
| `hosted_backend` | App with hosted backend services | An app uses ready-made services (Firebase, Supabase, CloudKit) instead of your own server. |
| `serverless` | Serverless functions | Small pieces of server code that run on demand in the cloud. |
| `monolith` | Single server app | One server program that does everything, including its web pages. |
| `services` | Several services | Separate server programs that each own one job and talk over the network. |
| `static_site` | Static site | Pages built ahead of time and served as files. |
| `tool_or_library` | Tool or library | A command-line tool or a package other code uses. |

**Code organisation** (`code`: how the code is divided up):

| Style id | Name | In plain words |
|---|---|---|
| `layered` | Layers | Screens, logic and data access each live in their own layer. |
| `clean` | Clean core | Business rules sit in the middle and don't depend on frameworks or the database. |
| `feature_modules` | Feature modules | Code is grouped by feature, each folder holding everything that feature needs. |
| `framework_conventions` | Framework conventions | The framework decides where things go (Rails, Django, Next.js). |
| `flat` | Flat / scripts | A few files without much structure; fine while it is small. |

**UI pattern** (`ui`: how screens get and change their data):

| Style id | Name | In plain words |
|---|---|---|
| `mvvm` | View models (MVVM) | Each screen has a helper object holding its data and actions. |
| `mvc` | Controllers (MVC) | Controllers sit between screens and data. |
| `single_state` | Single state store | All app state lives in one place and changes through named actions (Redux, TCA, MVI). |
| `component_state` | Component state | Each component keeps its own state and passes data down (React hooks, SwiftUI @State). |
| `server_rendered` | Server-rendered pages | The server builds each page; the browser mostly displays it. |
| `no_ui` | No user interface | Nothing for people to look at: an API, a tool or a library. |

## Diagrams

Each option gets a deliberately simple diagram: at most 10 boxes and 14 arrows (`normalize_diagram`). Box kinds are
`user` (who uses it), `app` (what they open), `service` (code that runs behind it), `data` (where data is kept) and
`external` (someone else's service). An unknown kind becomes `service`, and arrows to missing boxes are dropped. For
`describe`, the diagram is the model's best approximation from the repository digest. The Product page draws it as plain
SVG in columns by kind, themed with the page; `docs/architecture.md` carries it as a Mermaid flowchart (`mermaid`).

## Tied to the product requirements

- The full requirements go into every prompt. `initial` needs them filled in; `describe` works without them.
- Each decision lists the requirements it **serves**. **prd_links** maps each core need to how the architecture meets it,
  and **prd_gaps** lists needs it doesn't meet yet. The planner also sees the gaps.
- Approving and skipping store the requirements' fingerprint (`prd_fingerprint`, whitespace-insensitive). When they change
  afterwards (`prd_changed`), the Product page and the Home card say so, and **Check it against the requirements**
  proposes a tweak limited to what the new requirements need.
- `revisit_reasons` flags `revisit_when` triggers that the current requirements now mention. It only flags.
- Platforms and technology preferences chosen in the new-project wizard are read back from the pitch (`platforms_from_prd`).
  When no platform was chosen, the `initial` proposal recommends platforms with reasons.

## Existing projects: the repository digest

The model that proposes can't open the repository, so `evidence` gathers what a person would skim first: the detected
stack, source files by language, folders holding source, manifests (including one level down, and Xcode projects),
packages in use, likely entry points, and the start of the README and agent instruction files. Dependency, build and
generated folders are skipped, and the walk is capped. `infer_record` uses the same digest, without a model, when the
step is skipped. The build tools installed on the Mac (`available_tools`) are listed in every prompt.

## Tweaks, overhauls and versions

`change_kind` decides from content, not from the model's label. A change to the primary language, the framework, the
chosen option (unless it is the same stack renamed) or the dominant system shape is an **overhaul**; anything else is a
**tweak**. Approving a tweak bumps the minor version (1.3 to 1.4), and an overhaul bumps the major version (1.3 to 2.0).
Before approval, `what_changed` lists every difference in plain lines. Each approval or skip appends a history entry
(version, kind, date, the person's note, the changes), keeping the last 20.

## Files

| Path | Tracked | Holds |
|---|---|---|
| `.orchestrator/architecture-proposal.json` | No (runtime) | The latest proposal waiting for a person |
| `docs/architecture.json` | Yes | The current record: approved decision or working assumption |
| `docs/architecture.md` | Yes | The same record written out, with the diagram, for people and prompts |

Approving or skipping commits only the two `docs/` files, so every job's worktree sees them; other changes in the working
tree are left alone. If the commit can't be made, the page says to commit them.

## How jobs use it

- **Planner, verifier, builder**: `context_block` adds the record with a rule for each reader. An approved decision is a
  constraint: plan within it, raise a concern when a plan quietly departs from it, don't add a second framework, and say
  so instead of working around a decision. A working assumption is followed by default, and a plan may choose better if it
  says why. A codeless project with nothing recorded, or only a "Not decided" placeholder, gets the warning described above.
- **Stack detection**: before any manifest exists, a chosen record's language and build and test commands are used
  (`detected_stack_fields`). Real manifests always win, and an inferred record never overrides detection, since it came from it.

## Where people see it

- **New project**: after creation, "Next: choose the architecture", with **Skip for now**.
- **Product page**: the Architecture section at the bottom. It shows options side by side with diagrams, fit, Good and
  Watch out, and style bars; then the current architecture with its diagram, style mix, requirement links, technical
  details and history; then **Add or adjust** (tweak) and **Rethink the architecture** (overhaul). `#/architecture` links here.
- **Home**: a card with the chosen option, its style mix and version, or the one step it needs, with flags for a waiting
  change, changed requirements, gaps, or code that changed since an inference.
- Proposing, editing, approving, skipping and discarding are for the project's owner; members can read everything.

## API

| Route | Does |
|---|---|
| `GET /api/architecture` | The page's view: the record, a waiting proposal with its change kind and changes, the catalog, and flags |
| `GET /api/architecture/summary` | The Home card's view |
| `POST /api/architecture/propose` | Starts a background proposal; body `mode` (optional) and `feedback` |
| `POST /api/architecture/edit` | Changes the waiting proposal's chosen option, language, framework, commands or summary |
| `POST /api/architecture/approve` | Approves the waiting proposal, or the working assumption as it is, and commits |
| `POST /api/architecture/skip` | Keeps a working assumption (or refreshes an inferred one) and commits |
| `POST /api/architecture/discard` | Drops the waiting proposal |
