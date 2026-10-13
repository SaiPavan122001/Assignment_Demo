# samples/

**Every file here was produced with the `mock` LLM provider only.** No Ollama or Gemini run has been
made: no API key was provided and no local Ollama server was used. The mock is a deterministic heuristic
(see `app/llm/providers.py`), so these outputs demonstrate the pipeline plumbing, routing and
bookkeeping, not the grading quality of a real model.

| file | produced by | what it is |
|---|---|---|
| `mock_tests.txt` | `python tasks.py test -v` (fresh clone, 2026-10-13) | full verbose pytest output |
| `mock_demo.txt` | `python tasks.py demo` | end-to-end API + worker + review + publish flow |
| `mock_calibrate.txt` | `python tasks.py calibrate` | golden-set LLM-vs-expected score deltas |
| `mock_cost.txt` | `python tasks.py cost` | measured tokens/cost and 30 / 10,000 per day projection |

To add a real-provider sample later: set `LLM_PROVIDER=ollama` (with `ollama serve` and a pulled model) or
`LLM_PROVIDER=gemini` with `GEMINI_API_KEY`, then run `python tasks.py calibrate` and save the output as
`samples/<provider>_calibrate.txt`.
