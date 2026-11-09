# Home project status container

The approved request replaces Home's single-line uncommitted-items link with a highlighted container at the top of Home. Short bullets describe the project, latest commit, uncommitted file count, latest work and overall status. Below these facts, three suggestions open New job with the type, summary and detail filled in; they never start work automatically.

Facts come from the existing project, Git, product and job endpoints. AI suggestions use a read-only, bounded model call and a disk cache keyed by requirements and job changes. Only an owner can request generation. Home stays usable while generation runs, with three project-based suggestions if no model is available. Older computer servers still show the container using existing endpoints and project-based suggestions. Do not expose model diagnostics in the card.

Use the existing card, colour, spacing and link styles; a subtle accent background distinguishes the container. Stack suggestions on small screens. Escape all project and model content. Preserve the branch selector and health indicator; remove only the old uncommitted-items line.

Approach: a focused Python suggestion/cache module, small authenticated endpoints using existing background tasks, and a focused JS renderer. This keeps model calls off the main page request and avoids expanding the large app file with rendering logic. No new dependencies.
