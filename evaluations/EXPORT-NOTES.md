# Evidence export

The raw requests, SSE events, answers, tool calls, scores and timing values are
preserved. Local output-directory strings were normalized to `.` in JSON metadata.
No model result was changed, omitted, retried or rescored using a different rule.
Transport and scorer copies have path/import adaptations for this repository;
the scoring rules are unchanged. `verify-scores.py` recalculates all 16 official-
subset scores offline and does not contact the model.

```powershell
python -m venv .venv-eval
.\.venv-eval\Scripts\python.exe -m pip install -r evaluations/requirements-locked.txt
.\.venv-eval\Scripts\python.exe evaluations/verify-scores.py evaluations/instruction-and-tools
```

`run-sample.py` is a live rerun and requires explicit resource/GPU authorization.
It expects the existing Barq server at :8081/:8082 and the documented Windows
model path. It never starts another server, executes fixture tools or retries.
The exact selected cases and source revisions remain frozen in selection.json.
The venv/cache/model/runtime binaries and original local publication tools are
not included. Third-party source and tokenizer notices accompany the scorers.
