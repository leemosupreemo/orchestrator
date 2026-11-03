# Product requirements (the PRD)

Every project has one short document, `docs/product/prd.md`, that says what the product is. Every AI that plans, builds, reviews or checks work on the project reads it first, so it gets better answers than it can from the code alone. It is the person's document: they can write it, edit it, import it or have it drafted, and the app keeps it true as work finishes, tells them when it does, and lets them undo it.

This page describes how it works for the person using it, then what happens underneath.

## The document

Six sections, in the person's own words wherever possible. **Every section is optional**: write what you know and leave the rest.

| Section | What goes in it | Optional |
|---|---|---|
| **Pitch** | What it is, said the way you'd explain it to a friend. Also a `Built for:` line (iOS app, web app, "not decided yet"…). | Yes |
| **Who it's for** | An ideal user, or the situations it's for. "Not sure yet" is a fine answer. | Yes |
| **Core features** | The things it has to do, one per line. User-story form ("As a …, I can … so that …") is encouraged, not required. | Yes |
| **Look and feel** | A description ("feels like Google Docs"), plus designs, sketches, links and items from a connected app such as Figma. | Yes |
| **Not this** | Features to avoid, non-goals, or competitor patterns you don't want to copy. | Yes |
| **Success measures** | Project-level outcomes and targets. Event names and an analytics provider are optional. | Yes |

Each section starts with a line of italic guidance. Guidance and lines beginning `TBD:` don't count as content, so a section is "written" only when the person (or an accepted draft) has put something real in it.

## The flow

```mermaid
flowchart TD
    start([Open a project]) --> kind{What is there?}
    kind -->|New project wizard| wiz[PRD filled in from the wizard's answers]
    kind -->|Older project with a brief / use-case docs| mig[Folded into a PRD the first time it is opened]
    kind -->|Existing code, no PRD| draft[Draft it from my project]
    kind -->|They already have a PRD| imp[Import PRD]
    kind -->|Nothing yet| write[Write whichever sections you can]
    draft --> review{Read the proposal as a diff}
    imp --> review
    write --> doc
    review -->|Accept| doc[(docs/product/prd.md + a history version)]
    review -->|Discard| none[Nothing saved]
    wiz --> doc
    mig --> doc
    doc --> use[Planner, builder, debugger, reviewer and plan verifier are each handed it]
    use --> job[A job runs and finishes]
    job --> check{Switched on, and a feature or design job, or the person answered questions?}
    check -->|No| doc
    check -->|Yes| ask[Ask the model: does this job change the PRD?]
    ask -->|No change| doc
    ask -->|Safe change| upd[Saved as a version, person told: banner, toast, browser notification]
    ask -->|Would empty a section, drop a 'Not this' line or gut the doc| rej[Refused, nothing changes]
    upd --> doc
    upd --> undo[See what changed / Undo / Dismiss]
    undo --> doc
```

### 1. Getting a first version

The Product page (**Product** card on Home, then **Open**) offers these, most useful first for the situation:

- **Write it.** Click **Write** on a section and type. Every section is optional: write what you know and leave the rest. Anything written is enough for the document to start doing its job.
- **Draft it from my project.** For a project that already has code (a README, notes, a manifest or source files), the app reads a bounded summary of it: the README, `AGENTS.md`/`CLAUDE.md`, a few files in `docs/`, manifests (`package.json`, `pyproject.toml`, `Package.swift`, …), the top-level layout, source files by type and the last 30 commit subjects. A model proposes a first PRD from that. It describes what the product does today, says what it inferred and what it couldn't tell, and writes `TBD:` questions for the rest. **It never writes "Not this"**: only the person can say what the product shouldn't be, and builders and reviewers treat that section as law.
- **Import PRD.** Drag a file onto the drop zone (on a computer) or browse files. A markdown, text or Word (`.docx`) file, a PDF (needs the `pdftotext` tool, `brew install poppler`), or pasted text. A model reads it and writes a short summary for each of the six sections, leaving Success measures empty unless the source states outcomes or targets; the proposal says what it summarised or left out. **The full file is kept** as a [source document](#source-documents) and handed to the AI helpers alongside the summaries, so the nuance isn't lost.
- **Reverse engineer with REA MCP.** For existing applications, compiled binaries, APKs, IPAs, Electron bundles, or network captures, use the REA (Reverse Engineer Anything) MCP server (`rea-agents`) to decompile, inspect, and reconstruct product requirements and workflows.
- **New project wizard.** After the name and platforms (the only required answers), it asks one question per section: pitch, who it's for, core features, look and feel, what to exclude, and optional project success measures. Each answer is written into its section as given and any of them can be skipped. "Not sure: recommend for me" for platforms becomes a note telling the first plan to recommend platforms, with reasons. The last step offers **Break it down** into epics, and it can be skipped.

Drafts and imports are always shown as a diff with **Accept and save** / **Discard**. Nothing is written until the person accepts.

**With no PRD at all** nothing breaks. Jobs run as before with no product context, the automatic update never runs (there is nothing to keep true) and never creates a document, and Home, Check-up and the New job form point at the Product page. Before the first feature or design in a project with no Pitch, New job suggests defining the product first (it can be skipped).

### Source documents

When a PRD is imported, the original file is saved under `docs/product/source/` and listed in the PRD under a `## Source documents` heading. The six section summaries say what matters; the source holds the detail behind them. Every reader in the table below is told the source documents exist and where, so a planner or builder that needs the detail can read the file. The Product page lists them with a download link. Only the first 40,000 characters of an import are sent to the model to summarise; the saved file is always whole. Saving a section by hand, a draft or an update keeps the source list.

### 2. Editing by hand

Each section has an **Edit** button (**Write** when empty). Saving replaces only that section; headings the person added to the file elsewhere are kept. The file is ordinary markdown in the repo, so it can also be edited in any editor and committed; the Product page picks the change up. An edit made outside the app is kept and is included in the next version the app saves, but it doesn't get a version of its own, so restoring an older version would drop it unless it was committed to git. Commit outside edits.

### 3. Look and feel: designs and links

On the Look and feel section:

- **Add a design or sketch.** Images, PDFs, `.fig` and HTML files up to 25 MB. They're saved in the repo under `docs/product/designs/` and listed as bullets in the section; images also show as thumbnails. Logs are refused (they belong on a job).
- **Add a link.** A label and an `http(s)` URL, for example a Figma file.
- **Pick from a connected app.** Items from Figma (and the other connected apps) are added as a link, plus their image when the app provides one.

Only plain images are shown in place. Any other file type is served as a download, so an uploaded file can never run inside the app.

### 4. Keeping it true

When a job finishes building, the worker checks whether the job changes what the PRD says:

- **Which jobs:** feature and design jobs, and any job where the person answered the AI's questions. Quick fixes and plain bug fixes are skipped. Each job is checked once.
- **What the model sees:** the current document and a digest of the job: what it did, the builder's summary, the assumptions it made, what it was built to do, and the questions the person answered with their answers.
- **The rules it's given:** change the document only where the job clearly adds to it or contradicts it; keep the person's own words (add or adjust a line, never rewrite their wording); never remove anything under "Not this"; no implementation detail (files, libraries).
- **A safety check on the result.** An edit is refused, and nothing changes, if it empties a section that had content, removes any line from "Not this", or cuts the document below 60% of its length. A model failure or an unreadable answer is also a no-op: it never stops the job.
- **Which model:** the job's own reviewer, restricted to the job's allowed models, so a free-models-only job stays free-only.
- **Where it runs:** in an empty scratch folder, not the project. An agentic model (one with file tools) can't write the document directly and skip the history.

If the edit is applied it is saved as a version, and the person is told:

- a **banner** on Home and on the Product page ("The product requirements were updated after "Add rematch". Added the rematch feature this job built.") with **See what changed**, **Undo** and **Dismiss**;
- a **toast** the next time the page polls (not for an update that was already waiting when the page loaded: that shows as the banner only);
- a **browser notification**, if the person has turned those on.

The **switch** "Update this automatically when jobs finish" on the Product page turns all of this off (on by default). When it is off the model is never asked.

### 5. History and undo

Every change is a version: the person's edits, accepted drafts, imports and Help proposals, automatic updates, restores, and a one-time "before changes were tracked" version of a document that existed first. The History list shows when, who (You, Updated automatically after a job, Imported, AI help, Restored an earlier version…) and a one-line summary, with **See changes** (a diff against the version before) and **Restore**.

**Undo** on an automatic update restores the version before it. A restore is itself a new version, so nothing is ever lost; going back and forward is always possible. History keeps the latest 60 versions.

### 6. Who reads it

The filled-in sections are put in front of each role in a form that suits it, headed `## Product context (source of truth)`:

| Reader | How much it gets | Its rule |
|---|---|---|
| Planner | Most of each section (pitch and "who" ~1,500 characters each, features ~3,000) | Serve the people and features described, consider project success measures, stay inside "Not this", and say in the plan's assumptions or risks if it would contradict the document. |
| Builder, debugger | Less (~600 to 1,200 per section) | Build only what the task asks, consider the project success measures, match the look and feel, stay inside "Not this", and use `clarification_needed` instead of guessing if the task can't be done without contradicting it. |
| Reviewer | Medium | Flag work that serves none of the features, conflicts with project success measures, builds something under "Not this", or departs from the look and feel, even if the code is correct. |
| Plan verifier | Medium | Reject or flag a plan that conflicts with project success measures, builds something under "Not this", serves no listed feature, or departs from the look and feel. |

Sections are trimmed one at a time, so a long feature list never squeezes out "Not this". Empty sections add nothing; a project with no written PRD adds no context at all. Project-level Success measures reach jobs whether or not they are linked to a feature.

### 7. Check-up

Check-up has one **Product requirements** item: *todo* with nothing in the Pitch, *todo* naming what's missing if "Who it's for" or "Core features" is empty, *ok* once those three are written (and it says if automatic updates are off). The `Built for:` line also feeds the **Platforms** item.

### 8. Breaking it down into epics

Once the PRD has something in it, **Break down** splits it into epics: the big pieces of the product, for the person to approve or decline. It is one option in three places, and it runs only when asked, never in the background:

- **In the new project wizard**, as the last, optional step (**Break it down** or **Skip for now**).
- **On the Product page**, the first time the PRD has something in it, a dialog offers it once (**Break it down** or **Not now**; it isn't asked again, on any device). After that, the **Epics** section at the bottom of the Product page is where it happens.
- **In the Epics section** at the bottom of the Product page, any time.

How the breakdown is made:

- **Technique follows the information, or the person's choice.** By default the model picks, and names, the technique that fits what the PRD says: a **story map** (the flows people follow, grouped into release slices: MVP, Next, Later), a **work breakdown** (WBS: top-down deliverables by component, for technical products), or a **short list**. The Technique menu in the Epics section lets the person choose one instead.
- **Detail follows the detail you gave.** The app measures how much the PRD (and any attached source document) says. A short PRD gets a few epics (3 to 6) with one sentence each and *questions* instead of invented detail; a fuller one gets up to about 10 epics with requirements and user stories; a detailed one gets up to 15 epics with 3 to 6 testable requirements each, drawn from the text.
- **Run again whenever.** Breaking down again replaces the suggestions nobody has decided on. Approved and declined epics stay, and nothing already there is suggested twice.

#### The Epics section

Each epic is **Suggested**, **Approved** or **Declined**:

- **Approve** (or **Decline**) the ones ticked. Ticking an epic also ticks what it builds on, and approving brings those along. Declined epics stay in a list and can be brought back.
- **An approved epic becomes a feature and a job marked Epic.** The feature is how the planner, the plan run and the feature pages already work. The job waits in the **Backlog** (a filter on Home appears once there is one) and does nothing until you press **Make a plan**. Planning runs the usual feature job, with the epic's requirements and stories in its brief, and that job takes the waiting one's place, still marked Epic. **Build the plan** does the same for epics it starts.
- Waiting jobs are never counted as "needs you", so a dozen approved epics don't fill Home or send notifications.

#### Jira

With Jira connected (Connections) and a project key set (in the Epics section, or in the Jira connection), the Epics section can work both ways. Both are owner-only, and nothing is sent unless a button is pressed:

- **Send approved epics to Jira** creates each approved epic that isn't there yet as an Epic issue in the project (with its requirements and stories as the description). It then shows a Jira icon with the key and a link, in the Epics section, on Home, and on the epic's job. The job is linked to the Jira epic, so the pull request and merge notes that Orchestrator already posts to linked tickets go to it.
- **Import epics from Jira** lists the project's open epics that aren't here yet. The ones ticked become approved epics, each with a feature and a waiting Epic job, linked to the Jira epic. Orchestrator reads them from Jira again when importing; it doesn't take their text from the page.

## What is stored where

| What | Where | Committed? |
|---|---|---|
| The document | `docs/product/prd.md` | Yes, with the project |
| Designs and sketches | `docs/product/designs/` | Yes |
| Imported source documents | `docs/product/source/` | Yes |
| Features (including approved epics) | `.orchestrator/features.json` | No (local runtime state) |
| Epics: suggestions, decisions, Jira links | `.orchestrator/epics.json` | No (local runtime state) |
| A waiting or planned epic's job | `.orchestrator/jobs/` (`status: backlog` until planned) | No |
| Version history | `.orchestrator/prd-history.json` (latest 60, full text of each) | No (local runtime state) |
| Auto-update switch and the latest unseen update notice | `.orchestrator/prd.json` | No |
| "This job has been checked" | `prd_checked` on the job file | No |

History is local to the machine running Orchestrator. The document itself is the thing to commit and share; to see an older version on another machine, use `git log` on the file.

## Earlier projects

A project that has the older product documents (`docs/product-brief.md`, `docs/product/use-cases.md`) gets a PRD built from them the first time the Product page, Check-up or an update looks at it: the brief becomes the Pitch, the users and use cases become "Who it's for", the version-1 list becomes "Core features" and the non-goals become "Not this". It is recorded in the history as "Built from the earlier product documents". The old files are left where they are and are no longer read. The other old documents (journey, screens, technical decisions, current plan) and the periodic product-review job were retired: a document that keeps itself true makes them redundant.

## Developer reference

- **Module:** `orchestrator/prd.py`: the section model, parsing, `context_block`, the `Prd` class (document, history, settings, notice, migration), the prompts and reply parsers (`questions_prompt`, `refine_prompt`, `import_prompt`, `draft_prompt`, `update_prompt`), `extract_text`, `project_digest`, `unsafe_update` and `run_update`, plus the source-document helpers (`sources`, `attach_source`). It never calls a model itself; callers pass one in. `orchestrator/feature_map.py` builds the breakdown prompt (`detail_level`, `prompt`) and checks the reply (`parse`, `validate`, `accept`). `orchestrator/epics.py` is the epic store: suggestions, `approve` (feature plus waiting job), `attach_job` (what `new_job.py --epic` calls so a planned job replaces the waiting one), and the Jira link and import helpers. The Jira calls (`create_epic`, `list_epics`) are on the `Jira` provider in `orchestrator/integrations.py`.
- **Worker hook:** `update_product_requirements` in `orchestrator/scripts/worker_run.py`, run after a job's review and before its status report. Failures print a note and never fail the job.
- **Prompt wiring:** `new_job.py` (planner and verifier), `run_builder.py`, `debug_job.py`, `review_ready.py` call `prd.context_block(ROOT, audience)`.
- **API** (all under `/api/product`; writes need the UI header and a signed-in session):

| Request | Does |
|---|---|
| `GET /product` | The document, its six sections, history, settings, unseen notice and whether a draft is possible. Never creates a file. |
| `POST /product` `{section, body}` or `{text, source, summary}` | Save one section, or the whole document. `source` is `you`, `import` or `draft`. |
| `POST /product/settings` `{auto_update}` | The automatic-update switch. |
| `POST /product/dismiss` | Mark the update notice seen. |
| `GET /product/history/<id>` | One version, with a diff against the one before. |
| `POST /product/revert` `{id}` | Restore a version (recorded as a new version). |
| `POST /product/import` | A file (raw body, `?name=`) or `{text}`. Starts the work and returns `{task}` at once (202). |
| `POST /product/draft` | Draft from the project. Starts the work and returns `{task}` at once (202). |
| `GET /product/task/<id>` | How a started import or draft is getting on: `running`, `done` with the proposal and its diff (saves nothing), or `error` with why. 404 once it has expired (30 minutes). |
| `DELETE /product/task/<id>` | Stop or cancel an in-flight background task (or `POST /product/task/<id>/cancel`). |
| `POST /product/design?name=` | Upload a design (raw body, up to 25 MB). |
| `GET /product/design/<name>` | A saved design: images inline, anything else as a download. |
| `POST /product/reference` `{label, url}` or `{links}` | Add a link, or items picked from a connected app. |
| `GET /product/source/<name>` | Download an attached source document. |
| `POST /product/settings` `{auto_update}` or `{breakdown_offered}` | The automatic-update switch, and whether the one-time breakdown offer has been made. |
| `GET /epics` | Every epic with where its job is up to, how the breakdown was made, and whether Jira can be used. |
| `POST /epics/breakdown` `{technique?}` | Break the PRD (and any source document) down. Starts the work and returns `{task}` at once (202); poll `GET /product/task/<id>`. The result is saved when the model finishes, unless the task was cancelled. |
| `POST /epics/decide` `{ids, decision}` | `approve` (prerequisites included; creates the features and waiting jobs), `decline` or `restore`. |
| `POST /epics/jira/project` `{project}` | The Jira project key epics go to. Owner only. |
| `GET /epics/jira` | The project's open Jira epics not yet here. Owner only. |
| `POST /epics/jira/send` `{ids?}` | Create approved epics in Jira. Owner only. |
| `POST /epics/jira/import` `{keys}` | Import Jira epics as approved epics. Owner only. |

  `GET /api/state` also carries `product_notice` (the latest unseen automatic update), which the page polls to announce it.
- **Slow model calls run in the background.** Import and Draft return a task id straight away and the page polls `GET /product/task/<id>` every 1.5 s, so no single request stays open (a quick tunnel closes one after about 100 s, which made a slow model look like a failure). If the model's reply isn't in the expected format it is asked once more, with the format restated, before an error is shown.
- **Answer-only model calls** (Import, Draft, job chat) go through `_model_call` in `orchestrator/web/server.py`, which runs `job_chat_run.py` in an empty scratch folder.

## Tests

`tests/test_prd.py` (the module), `tests/test_web_ui.py` (`ProductEndpointTests` and the static page checks), `tests/test_product_context_wiring.py` and `tests/test_e2e_workflow.py` (what each role is handed), and `tests/e2e/test_pipeline_offline.py` (`LivingPrdTests`, `ReadOnlyModelCallTests`: the real worker through a finished job, with a scripted model). See [build-test-commands.md](build-test-commands.md).

## Limits worth knowing

- How closely any given model follows the product context, and how good its automatic edits are, has not been measured. The safety check refuses damaging edits; it can't judge whether a good-looking edit is right. That is what the banner and Undo are for.
- A draft is built from what the project contains. A thin project (no README, few files) gives a thin draft, and a model can still misread what a project is for; the proposal states what it inferred so the person can correct it.
- The history is local runtime state, not committed.
- An import sends the model at most its first 40,000 characters to summarise (the whole file is still saved), and a source document is read for the breakdown up to 30,000 characters.
- An approved epic's feature keeps the requirements and slice it was suggested with. Its name, summary, files and dependencies can be edited later, or the feature deleted, but its requirements and slice can't yet.
- Epics are created in Jira once and linked by key. Later edits to an epic here aren't pushed to Jira, and changes in Jira aren't pulled back; import brings in new epics only.
- Drift between the document and the code is only caught when a job's own information contradicts the document. There is no separate periodic review.
