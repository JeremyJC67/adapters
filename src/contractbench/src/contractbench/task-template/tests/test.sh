#!/bin/bash
# Verifier entrypoint, installed into every generated task by the adapter.
#
# Same scoring as the upstream tests/test.sh: run the task's pytest verifier,
# fall back to reward 0 when the suite writes nothing, and normalize reward.json
# to a flat {"reward": <float>} while keeping the rich diagnostics object in
# reward.diagnostics.json. Only the way pytest is launched differs:
#
# - pytest and pytest-json-ctrf are preinstalled in the image, so grading does
#   not depend on network access at verify time (installed here only if absent);
# - the interpreter is called by absolute path, PYTHONPATH (=/app, which the
#   agent can write) is dropped, and conftest discovery is confined to /tests,
#   so the grading helpers come from the trusted copy in /tests.

mkdir -p /logs/verifier
# Never trust an agent-written reward: clear any pre-existing reward files
# so the verifier always recomputes from the test suite.
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json /logs/verifier/reward.diagnostics.json

PY=/usr/local/bin/python3
"$PY" -m pip show -q pytest pytest-json-ctrf >/dev/null 2>&1 \
  || "$PY" -m pip install --no-cache-dir pytest pytest-json-ctrf >/dev/null 2>&1

cd /tests && env -u PYTHONPATH "$PY" -m pytest -p no:cacheprovider \
  --rootdir=/tests --confcutdir=/tests \
  --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA -v

# Ensure a reward file always exists (fallback to 0 if the suite did not write one).
test -f /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt

# Normalize reward.json to a flat numeric schema, preserving diagnostics.
"$PY" - <<'PY'
import json, os

VDIR = "/logs/verifier"
txt = os.path.join(VDIR, "reward.txt")
js = os.path.join(VDIR, "reward.json")
diag = os.path.join(VDIR, "reward.diagnostics.json")

reward = 0.0
try:
    with open(txt) as f:
        reward = float(f.read().strip())
except Exception:
    reward = 0.0

# Preserve the rich diagnostics object written by write_reward(), if any.
if os.path.exists(js):
    try:
        with open(js) as f:
            rich = json.load(f)
        if isinstance(rich, dict) and any(
            not isinstance(v, (int, float)) for v in rich.values()
        ):
            with open(diag, "w") as f:
                json.dump(rich, f, indent=2)
    except Exception:
        pass

with open(js, "w") as f:
    json.dump({"reward": reward}, f)
PY
