# Idea-first onboarding, AI setup, feature map and building the plan

## Why

Checking the app against the intended journey (describe, set up the essentials, map the features, build, react, repeat) found it mostly in place, with four gaps:

1. **Order.** First run asks for machine setup before anything about what the person wants to build. They should say at least the basic idea first, so they're invested before the chores.
2. **Getting an AI.** The checklist says when no AI is ready but not how to get one, which options are free, or which plugins help.
3. **From PRD to features.** Features, their dependencies and overlap checks exist, but the person creates every feature by hand. Nothing turns the product requirements into a feature map with stories and an order.
4. **Building the plan.** Jobs start one at a time. Nothing runs a whole feature list in dependency order, with independent features in parallel.

## A. Idea first

Every first-run entry point opens with **What do you want to build?**: one sentence, and an optional name. Beside it is **I already have code**. Setup comes after, and the idea is carried into the new-project form, so it's never asked twice.

- **Hosted app, signed in, no computers yet.** Step 1 is the idea, kept in this browser. Step 2 sets up a computer (the existing steps). When the person later opens their first computer and starts a project, the form is prefilled with the idea. "I already have code" skips to the computer steps, then to adding an existing project.
- **Mac app welcome window.** The idea field comes first. **Start** opens the new-project form prefilled through the one-time browser link, while **I have code already** opens the folder picker. Connecting the Mac to an account comes after both.
- **Browser home with no project.** The same card sits at the top.

The idea travels as `pitch` and `name` query parameters on `#/new-project`. The form only uses them when no draft exists, so a draft in progress is never overwritten. Browser-link routes must accept these parameters.

## B. Add an AI

A page, `#/config/ai`, linked from the setup checklist and the "no AI ready" notices. It lists each provider:

- what it is and what it costs (free, subscription or pay-per-use);
- whether it's installed and signed in on this computer, using the existing detection;
- the install command (copy button) and the sign-in step;
- the official link.

Free options come first, and the recommended plugins and MCP servers appear at the bottom. Provider facts live in one data module, so they can be updated without touching the page.

## C. Draft the feature map from the PRD

**Draft features from the product requirements** on the Features page. In the background, the model reads the PRD, the existing features and the project's layout. It proposes features, each with:

- a name and summary;
- user stories;
- the code paths it should own;
- what it depends on;
- which core feature it serves.

The proposal is validated before anyone sees it:

- dependencies must name proposed or existing features;
- there must be no dependency loops;
- names must be unique;
- path overlaps with existing or other proposed features are flagged.

The page shows it as layers in build order, with warnings, and the person accepts all of it, some of it, or none. Nothing is saved until they accept, the same pattern as PRD drafts. Stories are stored on the feature and handed to the planner with the feature's context.

## D. Build the plan

**Build the plan** on the Features page starts a *plan run* over the chosen features (by default, every planned feature with no job yet).

- A feature becomes **ready** when every feature it depends on is complete.
- Ready features get a feature job, created through the same path as **New job → Feature**, so they're planned, verified and built normally. Several ready features start in parallel, up to how much work the configured machines can take.
- Plans still wait for approval unless **Approve plans automatically** was chosen.
- When a job finishes, the next tick starts whatever became ready. The run is complete when every chosen feature is.
- **Pause** stops starting new work, without stopping anything running. A failed job pauses only the features that depend on it.

The run is stored in `<runtime>/plan-run.json` and advanced from the server's existing background loop. Choosing what to start is a pure function, so the rules are easy to test.

## Not in scope

- Tester feedback flowing in by itself (it's typed in today).
- A full repository browser.
- Distribution beyond Firebase App Distribution.
