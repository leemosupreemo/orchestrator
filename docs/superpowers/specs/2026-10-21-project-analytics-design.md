# Project analytics and KPI workflow

## Goal

Make measurement part of normal feature planning and give every project one clear place to understand its analytics setup and feature outcomes. A project may have no KPIs or analytics provider configured; these remain optional. The first version uses results entered by the project owner and does not query analytics vendors for product usage data.

## Existing foundation

- Feature records already own KPI definitions and up to 200 manually logged measurements per KPI. A KPI has a name, event, direction, optional target and unit, plus a learning decision and note for each measurement.
- The existing Measure page manages Mixpanel, Amplitude and PostHog connections, feature KPIs, KPI suggestions, result logs and tracking-plan export. Provider test events verify connection setup, not product event delivery.
- A feature-linked job receives KPI instrumentation instructions only when its selected feature already has KPIs. The instructions ask the builder to reuse the project's analytics wrapper and add a test for every event.
- The product requirements document (PRD) is optional, has five optional sections, and its context is supplied to planning, building, review and plan verification.

## User experience

Promote the existing Measure page to the project's **Analytics** page. Keep the existing page and data model as the source of truth rather than creating a second KPI store. The page summarizes the configured provider and its dashboard link, KPI count, measurements available, and the latest value/status for each KPI, grouped by feature. Existing KPI editing and manual result logging remain available. Empty states explain that KPIs and provider setup are optional and provide direct actions to add them.

Feature creation and feature-linked job planning surface a measurement prompt: how will the team know the feature worked? The user can add a KPI, accept a suggested KPI, or skip. This must not block job creation or planning. The existing Check-up continues to call out features with work and no KPI, with a route to Analytics.

## PRD success measures

Add an optional **Success measures** section to every generated PRD template. It records project-level outcomes and targets in the person's own words; it does not require analytics event names or a connected provider. Feature-level KPIs remain the structured, event-oriented measurement records attached to features.

PRD parsing, editing, importing, drafting, normalization, history and automatic updates must recognize and preserve the new optional section. Existing PRDs without it remain valid and render an empty optional section when normalized. PRD context includes the section for all existing audiences, so jobs without a feature can still use the project's success criteria. Prompts that rewrite or summarize the PRD must retain the section and its contents without inventing metrics.

## Job instrumentation and reporting

All jobs continue to receive the PRD context when available. Feature-linked jobs also receive that feature's KPI event names, targets and directions. When feature KPIs exist, the implementation and review workflow requires an automated test for each event and reports which tests verify the events and whether they passed. A successful instrumentation test means the event was sent through the project analytics layer in the tested code path; it does not establish that users triggered it or that an outcome target was achieved.

Jobs whose feature has no KPI remain runnable. Their planning context should make the missing measurement visible and offer a KPI suggestion or allow the person to continue without one. Do not infer product success from build status, test pass status, or the analytics provider's connection test.

## Data and provider boundaries

Continue storing KPI records with their features and provider configuration in the project's existing settings. The Analytics summary is derived from these records and does not maintain separate copies. Keep credentials private and expose only provider identity, region, whether a key is configured, and public dashboard links. The first version does not fetch event counts or KPI values from provider APIs, synchronize dashboards, or add another SDK to project code.

The tracking-plan export remains a generated Markdown artifact. Editing KPI definitions updates the next export; generated summaries must not overwrite manually maintained project documents.

## Failure behavior

An unavailable provider, missing key, or failed test event must leave Analytics and manual KPI editing usable. Provider test failure is shown as a connection problem and does not block job creation. A KPI with no measurement is shown as having no result rather than as zero or as meeting its target. Invalid KPI values and event names continue to be rejected by the existing validators.

If a project has no PRD, jobs continue as they do now and no PRD is created just to store success measures. If a feature has no KPI, instrumentation requirements are omitted for that feature; the feature can still be worked on and its omission remains visible in Analytics and Check-up.

## Validation

- Unit coverage for the new optional PRD section: template, parse/split, normalization, replace/edit, import/draft/update preservation, and context for each audience.
- API and UI coverage for project analytics summary, provider identity/region/key secrecy, empty states, existing manual KPI lifecycle, tracking-plan export, and optional KPI creation flow.
- Job-context and pipeline coverage showing PRD success measures reach jobs with and without a feature, feature KPIs reach linked jobs, and required event tests are reported accurately.
- Regression coverage that failed or unavailable provider checks do not block Analytics or jobs, and no result is never represented as a zero measurement.
- Use canonical commands in `docs/build-test-commands.md`; run `orchestrator check-config` only if `.orchestrator/project.json` is changed.

## Out of scope for the first version

- Pulling measurements from Mixpanel, Amplitude or PostHog APIs.
- A/B testing, experimentation assignment, cohort analysis or event-query builders.
- Blocking feature jobs when no KPI is defined.
- Treating code coverage, test pass status, or provider test events as evidence of product adoption.
