# Agent-Eval (ARCHIVED — DEPRECATED 2026-06-16)

**This is the extraction source for `llm-eval-kit`, kept for history only. Do not use it.**

This was the direct source extracted into the standalone OSS package
[`llm-eval-kit`](https://github.com/wcollani/llm-eval-kit) (also checked out locally at
`~/repos/llm-eval-kit`). Use that instead — it's pip-installable
(`pip install -e ~/repos/llm-eval-kit` or `git+https://github.com/wcollani/llm-eval-kit.git`) and
provides the same CLI/`GEval` scoring plus env-var-based Ollama routing, Prometheus Pushgateway
metrics, and a console script (`llm-eval <experiment.yaml>`).

`homelab-tools/prompt-optimizer` is the active dogfooding consumer of `llm-eval-kit`. Production
experiment configs that used to live in `experiments/` here now live at
`homelab-platform/services/dagu/dags/experiments-config/`.

---

`agent-eval` is a CLI evaluation framework built for the homelab to benchmark agent performance using DeepEval and litellm. Traces are emitted over OTLP directly to your telemetry backend (e.g. Arize Phoenix & Loki via Grafana Alloy).

## Quickstart

1. **Install dependencies:**
   ```bash
   cd agent-eval
   pip install -r requirements.txt
   ```
2. **Execute an experiment:**
   ```bash
   python cli.py experiments/refactor_bash.yaml
   ```

## Configuration

By default the CLI routes through a LiteLLM proxy. Override with environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `LITELLM_API_BASE` | `http://localhost:4000/v1` | LiteLLM proxy or direct Ollama endpoint |
| `OLLAMA_WS_URL` | `http://localhost:11434` | Target for `ws/` model prefix (e.g. a second GPU node) |
| `OLLAMA_SRE_URL` | `http://localhost:11434` | Target for `sre/` model prefix |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4319/v1/traces` | OTLP trace export |
| `ASYNC_WORKER_URL` | `http://localhost:8080` | Async job worker (used by `tasks:` batch experiments) |

To hit Ollama directly without a proxy:
```bash
export LITELLM_API_BASE="http://localhost:11434/v1"
```

## Writing an Experiment YAML

Create a new YAML file in the `experiments/` directory to define your test criteria:

```yaml
experiment_name: "My Evaluation"
system_prompt: "You are an expert agent..."
models_to_test:
  - "ollama/qwen2.5-coder:1.5b-base"
  - "ollama/llama3.1:8b"
judge_model: "ollama/qwen2.5-coder:14b"
test_cases:
  - name: "Refactor feature X"
    input_file: "path/to/local/file.py"
    task_prompt: "Refactor this file to..."
    expected_output_criteria: "The script must use X and Y."
```
> **Note:** Make sure model names are prefixed (e.g., `ollama/`, `vllm/`) to route correctly through the LiteLLM proxy!

---

## 🛠️ OpenTelemetry & Tracing Troubleshooting

If you see the warning `LiteLLM:WARNING: opentelemetry.py - Proxy Server is not installed` when running `agent-eval`, it indicates that the local Python environment does not have the necessary dependencies for local client-side OpenTelemetry logging. While LLM requests are successfully processed and tracked by the remote proxy server, **local spans from the client CLI will be skipped.**

### Option A: Enable Client-Side Tracing (Recommended for local logs)
To generate and export traces from the client script directly to your OTLP backend:
1. **Modify `Tools/agent-eval/requirements.txt`**:
   Change `litellm` to `litellm[proxy]`.
2. **Re-install dependencies**:
   ```bash
   pip install -r Tools/agent-eval/requirements.txt
   ```
3. **Verify**:
   Run `agent-eval` and confirm the warning is resolved and spans are visible at your configured OTLP endpoint.

### Option B: Bypassing Client-Side Tracing
If you only care about tracing on the remote LiteLLM Proxy Server and want to silence the client warning:
* Comment out the success and failure callbacks in `Tools/agent-eval/cli.py`:
  ```python
  # litellm.success_callback = ["otel"]
  # litellm.failure_callback = ["otel"]
  ```
