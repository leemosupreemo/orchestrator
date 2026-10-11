#!/usr/bin/env python3
"""A scripted stand-in for the `opencode run --model <id>` CLI, so the pipeline can run end to end offline.

It reads the prompt on stdin, works out which role is asking, records the prompt (so a test can see exactly what each role was
told), and answers the way a model would: a plan, a verification, an implementation (it really edits files in the working
directory) or a review. Behaviour switches come from $FAKE_LLM_BEHAVIOUR, a comma list:
  prose_first    the first planner reply is prose, not JSON (the orchestrator should ask again)
  full_suite     the builder runs the project's whole test command, as its prompt asks
  narrow_tests   the builder runs only a test that has nothing to do with the plan
  two_tasks      the plan has two tasks (the bonus, then a README note), each built separately
  writes_files   behave like an agent with file tools: write docs/product/prd.md into the folder it was started in
  prd_update     when asked whether a finished job changes the product requirements, add a feature to them
  prd_gut        ...answer with a document whose pitch has been emptied (which must be refused)
  concerns_once  the first verification raises concerns (the orchestrator should accept the suggestions and carry on)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_LLM_STATE"])
BEHAVIOUR = set(filter(None, os.environ.get("FAKE_LLM_BEHAVIOUR", "").split(",")))

BONUS_TEST = '''

class LongWordBonusTests(unittest.TestCase):
    def test_seven_letters_get_the_bonus(self):
        self.assertEqual(score("abcdefg") - score("abcdef"), score("g") + 10)

    def test_six_letters_get_no_bonus(self):
        self.assertEqual(score("abcdef"), sum(score(c) for c in "abcdef"))

    def test_punctuation_does_not_count_towards_the_seven(self):
        self.assertEqual(score("abcdef-!"), score("abcdef"))
'''


def load() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {"calls": []}


def role_of(prompt: str) -> str:
    if "PRODUCT DOCUMENT UPDATE" in prompt:
        return "prd"
    if "### GENERATED PLAN ###" in prompt:
        return "verifier"
    if "Vertical slices, not layers" in prompt or "Required JSON schema" in prompt and "tasks" in prompt:
        return "planner"
    if "MANDATORY TEST-DRIVEN DEVELOPMENT" in prompt or "implementation agent" in prompt:
        return "builder"
    if "reviewer agent" in prompt:
        return "reviewer"
    return "other"


def plan() -> dict:
    p = _plan()
    p.pop("TWO", None)
    if "two_tasks" in BEHAVIOUR:
        p["tasks"].append({"title": "Document the bonus", "description": "Document the bonus", "acceptance_criteria": ["README mentions the bonus"], "likely_files": ["README.md"],
                           "tests": ["test_seven_letters_get_the_bonus"], "complexity": "small"})
    return p


def _plan() -> dict:
    return {
        "title": "[Scoring] Long-word bonus", "summary": "Serves the use case 'score a word': words of 7+ letters get 10 extra points.",
        "impact_analysis": "Only score(); the existing three tests must still pass.", "research_findings": "wordgame/scoring.py holds score().",
        "assumptions": ["Only letters a-z count towards the seven", "The bonus is a flat 10 points"], "constraints": ["Standard library only"], "risks": [],
        "tasks": [{"title": "A seven-letter word scores the bonus, end to end", "description": "Write the failing tests, then add the bonus to score().",
                   "acceptance_criteria": ["score('abcdefg') includes 10 extra points", "score('abcdef') is unchanged"], "likely_files": ["wordgame/scoring.py", "tests/test_scoring.py"],
                   "tests": ["test_seven_letters_get_the_bonus", "test_six_letters_get_no_bonus"], "complexity": "small"}],
        "TWO": None,
        "test_cases": [
            {"title": "Seven letters get the bonus", "type": "unit", "expected": "10 extra points", "covers": ["score('abcdefg') includes 10 extra points"], "tests": ["test_seven_letters_get_the_bonus"]},
            {"title": "Six letters are unchanged", "type": "unit", "expected": "no bonus", "covers": ["score('abcdef') is unchanged"], "tests": ["test_six_letters_get_no_bonus"]}],
    }


def build(prompt: str = "") -> dict:
    if "two_tasks" in BEHAVIOUR and "Document the bonus" in prompt.split("Brief:")[-1][:600]:
        readme = Path("README.md")
        readme.write_text(readme.read_text() + "\nWords of seven or more letters score a 10 point bonus.\n")
        return {"test_command": "python3 -m unittest discover -s tests", "summary": "Documented the bonus.", "files_changed": ["README.md"]}
    tests = Path("tests/test_scoring.py")
    text = tests.read_text()
    if "LongWordBonusTests" not in text:
        marker = '\n\nif __name__ == "__main__":'
        tests.write_text(text.replace(marker, BONUS_TEST + marker) if marker in text else text + BONUS_TEST)
    src = Path("wordgame/scoring.py")
    code = src.read_text()
    if "BONUS" not in code:
        code = code.replace('    return sum(LETTER_POINTS.get(c, 0) for c in word.lower())',
                            '    letters = [c for c in word.lower() if c in LETTER_POINTS]\n    bonus = LONG_WORD_BONUS if len(letters) >= LONG_WORD_LETTERS else 0\n    return sum(LETTER_POINTS[c] for c in letters) + bonus')
        code = code.replace('\n\ndef score', '\nLONG_WORD_LETTERS = 7\nLONG_WORD_BONUS = 10  # BONUS for words of seven or more letters\n\n\ndef score', 1)
        src.write_text(code)
    command = "python3 -m unittest discover -s tests -k test_seven_letters_get_the_bonus -k test_six_letters_get_no_bonus"
    if "full_suite" in BEHAVIOUR:
        command = "python3 -m unittest discover -s tests"
    if "narrow_tests" in BEHAVIOUR:
        command = "python3 -m unittest discover -s tests -k test_case_does_not_matter"
    return {"test_command": command, "summary": "Added the long-word bonus and three tests; all tests pass.", "files_changed": ["wordgame/scoring.py", "tests/test_scoring.py"]}


def prd_reply(prompt: str) -> str:
    """The answer to "does this finished job change the product requirements?"."""
    current = prompt.split("Current document:\n", 1)[1].split("\n\nWhat the job showed:", 1)[0]
    if "prd_gut" in BEHAVIOUR:
        import re
        gutted = re.sub(r"(## Pitch\n\n).*?(?=\n## )", r"\1", current, count=1, flags=re.S)
        return json.dumps({"changed": True, "summary": "Rewrote the pitch", "markdown": gutted})
    if "prd_update" in BEHAVIOUR:
        updated = current.replace("## Look and feel", "- Players can ask for a rematch after any game\n\n## Look and feel", 1)
        return json.dumps({"changed": True, "summary": "Added the rematch feature this job built", "markdown": updated})
    return json.dumps({"changed": False, "summary": "", "markdown": ""})


def main() -> int:
    prompt = sys.stdin.read()
    state = load()
    role = role_of(prompt)
    state["calls"].append({"role": role, "model": sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else None, "prompt": prompt, "cwd": os.getcwd()})
    planner_calls = sum(1 for c in state["calls"] if c["role"] == "planner")
    verifier_calls = sum(1 for c in state["calls"] if c["role"] == "verifier")
    STATE.write_text(json.dumps(state))
    if "writes_files" in BEHAVIOUR and role in ("prd", "other"):  # the read-only questions; builders and planners legitimately work in the project
        target = Path("docs/product/prd.md")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# written by an agentic model\n")
    if role == "planner":
        if "prose_first" in BEHAVIOUR and planner_calls == 1:
            print("I looked through the code and wrote my plan to plan.json. It adds the bonus in score().")
        else:
            print("```json\n" + json.dumps(plan()) + "\n```")
    elif role == "verifier":
        if "concerns_once" in BEHAVIOUR and verifier_calls == 1:
            print(json.dumps({"status": "concerns", "comments": "Say whether punctuation counts.", "suggested_additions": ["Add a test with punctuation"], "risks_identified": []}))
        else:
            print(json.dumps({"status": "approved", "comments": "Grounded in wordgame/scoring.py.", "suggested_additions": [], "risks_identified": []}))
    elif role == "builder":
        print("```json\n" + json.dumps(build(prompt)) + "\n```")
    elif role == "prd":
        print(prd_reply(prompt))
    elif role == "reviewer":
        print("## Review\n\nThe change matches the brief: the bonus applies at seven letters and tests cover both sides of the boundary. No blocking issues.\n\n**Verdict: approve**")
    else:
        print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
