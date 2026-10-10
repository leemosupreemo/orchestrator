Create visual design mockups for this job. Inspect the existing design system and
product requirements, then show the requested screens as self-contained HTML or
SVG mockups. Include useful states and responsive layouts where relevant.

The project is read-only. Do not implement app components, change production
code, run builds, commit, or start an implementation job. Return files as JSON;
Orchestrator saves them as design artifacts under docs/product/designs/<job-id>.
HTML must include its own styles and use mock data; do not depend on the live app.

Return ONLY JSON:
{"summary": "What the mockups show", "files": [{"path": "screen.html", "content": "complete HTML or SVG text"}]}

If the request cannot be represented as visual mockups, return
{"clarification_needed": "What needs to be clarified?"}.
