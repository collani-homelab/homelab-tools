# Agent Eval & Benchmarking Sprint

## Objective
Run lightweight model evaluations using `agent-eval` while tracking the parallel development of the Asynchronous Queuing Worker. We are pausing heavy multi-model experiments until the queue is ready to avoid HTTP timeouts.

## Tasks
- [x] Verify SRE telemetry endpoints (Alloy OTLP, LiteLLM, Arize Phoenix, Loki).
- [ ] Define lightweight YAML experiments in `experiments/` directory.
- [ ] Run initial lightweight experiments and monitor Arize Phoenix for trace capture.
- [ ] Monitor SRE node (LiteLLM) for any HTTP timeouts or memory spikes.
- [ ] Pause eval track and pivot to Asynchronous Queuing Worker if timeouts occur.
- [ ] Ensure `uv` is managing python projects

## Parallel Track (Async Worker)
- [ ] Scaffold the Async Queuing Worker in the orchestrator repo.
- [ ] Initialize SQLite schema for task persistence (Pending, Running, Failed, Complete).
- [ ] Build basic queue daemon to mitigate Ollama loading timeouts.
