# prompt-optimizer

A hill-climbing prompt optimizer. Generates rewrites of a base prompt (LLM-as-mutator),
scores each variant with [llm-eval-kit](https://github.com/wcollani/llm-eval-kit)'s `GEval`
scoring (LLM-as-fitness-function), keeps the top performers, and repeats for N generations.

Depends on `llm-eval-kit` as a real pip dependency (`requirements.txt`) — no vendored eval logic.
Each variant's score and latency is pushed to the same Prometheus Pushgateway metrics
(`llm_eval_score`, `llm_eval_latency_ms`) that llm-eval-kit's own experiments use, so a run shows
up live in the existing `eval-metrics-dashboard.json` Grafana dashboard with no dashboard changes —
just pick the new `experiment` value from the template variable dropdown.

## Install

```bash
pip install -r requirements.txt
```

During local development, swap the `requirements.txt` line for an editable install of a local
checkout instead of pulling from GitHub each time:

```bash
pip install -e ~/repos/llm-eval-kit
```

## Config

```yaml
name: my-prompt-opt              # used as the experiment_name prefix (timestamp appended)
task_description: "..."          # what the prompt is for — guides the mutator's rewrites
base_prompt: "..."                # the prompt to optimize
target_model: "sre/..."           # model the prompt is actually used with
mutator_model: "ws/..."           # model used to generate prompt rewrites (defaults to target_model)
judge_model: "ws/..."             # GEval judge model
generations: 3
variants_per_generation: 4
top_k: 2                          # survivors carried into the next generation
test_cases:
  - name: "..."
    input: "..."                          # user-turn input the target model sees
    expected_output_criteria: "..."       # GEval criteria text
```

Model names use llm-eval-kit's direct-routing prefix convention (`ws/<model>` → `OLLAMA_WS_URL`,
`sre/<model>` → `OLLAMA_SRE_URL` — see `cli.py`'s `resolve_endpoint`), matching how the live agents
(e.g. `agent-sre-patrol`) call Ollama directly rather than through LiteLLM.

## Run

Create a `.env` file (gitignored) with:
```
OLLAMA_SRE_URL=http://192.168.99.178:11434
OLLAMA_WS_URL=http://192.168.99.247:11434
PROMETHEUS_PUSHGATEWAY_URL=http://localhost:9092
```

Then:
```bash
python optimize.py experiments/sre-patrol-summary.yaml
```

Prints per-generation scores and the winning prompt, and writes full results JSON via
llm-eval-kit's `save_experiment_results` (same `results/` convention llm-eval-kit itself uses).

The bundled `experiments/sre-patrol-summary.yaml` optimizes the summary prompt from
`agent-sre-patrol/patrol.py`'s `SUMMARY_PROMPT` — if a run produces a clearly better prompt, copy
it back into `patrol.py` by hand (not automated).
