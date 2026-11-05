# Verification

Verified with Python 3.12 against the dependencies in `requirements.lock`.

- `pytest -q`: 31 passed
- `ruff check .`: passed
- `ruff format --check .`: passed
- `pip check`: no broken requirements
- Actual localhost HTTP streaming: first event 0.024 s; total 0.328 s
- Actual localhost primary timeout and backup completion: 3.015 s

Timings are one local mock run, not a production performance guarantee. Tests use mocked upstreams and a real stdio subprocess. Paid providers were not contacted.
