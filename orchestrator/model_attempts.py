"""Local model response history and shadow recommendations for jobs."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

HISTORY_BYTES = 256 * 1024
MIN_RESPONSES = 3
SCORED_RESULTS = {"response_accepted", "invalid_json"}


class AttemptAudit:
    def __init__(self, path: Path, role: str, job_type: str, candidates: list[str], *,
                 job_id: str | None = None, requested_model: str | None = None):
        self.path = path
        self.context = {"call_id": uuid4().hex, "role": role, "job_type": job_type}
        if job_id:
            self.context["job_id"] = job_id
        if requested_model:
            self.context["requested_model"] = requested_model
        self.evidence = self._evidence(candidates)
        self.recommended = candidates[0] if candidates else None
        self.basis = "insufficient_evidence"
        if self.recommended and self.evidence[self.recommended]["total"] >= MIN_RESPONSES:
            comparable = [model for model in candidates if self.evidence[model]["total"] >= MIN_RESPONSES]
            # Start with a neutral prior, and keep catalog order on ties.
            self.recommended = max(comparable, key=lambda model:
                                   (self.evidence[model]["accepted"] + 1) / (self.evidence[model]["total"] + 2))
            self.basis = "response_acceptance"

    def _evidence(self, candidates):
        counts = {model: {"accepted": 0, "total": 0} for model in candidates}
        try:
            with self.path.open("rb") as stream:
                start = max(0, stream.seek(0, os.SEEK_END) - HISTORY_BYTES)
                stream.seek(start)
                if start:
                    stream.readline()
                lines = stream.read(HISTORY_BYTES).splitlines()
        except OSError:
            return counts
        for line in lines:
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict) or event.get("kind") != "attempt":
                continue
            model, result = event.get("model"), event.get("result")
            if (event.get("role") == self.context["role"] and event.get("job_type") == self.context["job_type"]
                    and isinstance(model, str) and model in counts and isinstance(result, str) and result in SCORED_RESULTS):
                counts[model]["total"] += 1
                counts[model]["accepted"] += result == "response_accepted"
        return counts

    def _append(self, **fields):
        row = {**self.context, "at": datetime.now(timezone.utc).isoformat(), **fields}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(row) + "\n").encode())
            finally:
                os.close(fd)
        except OSError:
            pass  # A failure to record evidence must not stop a job.

    def record(self, model: str, result: str, elapsed: float, *, format_retry: bool = False):
        self._append(kind="attempt", model=model, result=result, elapsed_seconds=round(elapsed, 3),
                     format_retry=format_retry)

    def finish(self, actual: str | None):
        self._append(kind="shadow", recommended_model=self.recommended, actual_model=actual, basis=self.basis,
                     evidence={model: counts for model, counts in self.evidence.items() if counts["total"]})
