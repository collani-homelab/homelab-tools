#!/usr/bin/env python3
import typer
import os
import yaml
import time
import subprocess
import litellm
import asyncio
import re
import litellm
import asyncio
import base64
import requests
import json
import aiohttp
import glob

from eval_logger import setup_tracing, save_experiment_results
from deepeval.test_case import LLMTestCase, SingleTurnParams
from deepeval.metrics import GEval, BaseMetric
from deepeval.models.base_model import DeepEvalBaseLLM

app = typer.Typer(help="Agent Testing CLI to run experiments with OTEL tracing")

# Configure litellm to use otel
litellm.success_callback = ["otel"]
litellm.failure_callback = ["otel"]

def resolve_endpoint(model_name: str):
    """Parses prefix to determine api_base, actual model name, and if it's a direct connection."""
    api_base = os.getenv("LITELLM_API_BASE", "http://localhost:4000/v1")
    is_direct = False

    if model_name.startswith("direct_ws/"):
        api_base = os.getenv("OLLAMA_WS_URL", "http://localhost:11434") + "/api/chat"
        model_name = model_name[10:]
        is_direct = True
    elif model_name.startswith("direct_sre/"):
        api_base = os.getenv("OLLAMA_SRE_URL", "http://localhost:11434") + "/api/chat"
        model_name = model_name[11:]
        is_direct = True
    elif model_name.startswith("ws/"):
        api_base = os.getenv("OLLAMA_WS_URL", "http://localhost:11434") + "/v1"
        model_name = model_name[3:]
    elif model_name.startswith("sre/"):
        api_base = os.getenv("OLLAMA_SRE_URL", "http://localhost:11434") + "/v1"
        model_name = model_name[4:]
        
    if model_name.startswith("ollama/"):
        if is_direct or "11434" in api_base:
            model_name = model_name[7:]
        
    proxy_model = f"openai/{model_name}" if not model_name.startswith("openai/") and not is_direct else model_name
    return proxy_model, api_base, is_direct

class CustomLiteLLM(DeepEvalBaseLLM):
    """Wrapper to allow DeepEval to use LiteLLM configured endpoints."""
    def __init__(self, model_name):
        self.model_name = model_name

    def load_model(self):
        return self

    def generate(self, prompt: str) -> str:
        proxy_model, api_base, is_direct = resolve_endpoint(self.model_name)
        if is_direct:
            raise NotImplementedError("DeepEval Judge models currently do not support direct routing.")
        res = litellm.completion(
            model=proxy_model,
            messages=[{"role": "user", "content": prompt}],
            api_base=api_base,
            api_key="sk-dummy",
            response_format={"type": "json_object"},
            timeout=1200
        )
        return res.choices[0].message.content

    async def a_generate(self, prompt: str) -> str:
        proxy_model, api_base, is_direct = resolve_endpoint(self.model_name)
        if is_direct:
            raise NotImplementedError("DeepEval Judge models currently do not support direct routing.")
        res = await litellm.acompletion(
            model=proxy_model,
            messages=[{"role": "user", "content": prompt}],
            api_base=api_base,
            api_key="sk-dummy",
            response_format={"type": "json_object"},
            timeout=1200
        )
        return res.choices[0].message.content
        
    def get_model_name(self):
        return self.model_name

class ExecutionMetric(BaseMetric):
    def __init__(self, threshold: float = 1.0):
        self.threshold = threshold
        self.score = 0.0
        self.reason = None
        self.success = False

    def measure(self, test_case: LLMTestCase):
        code = test_case.actual_output.strip()
        if code.startswith("```python"):
            code = code[9:]
        elif code.startswith("```"):
            code = code[3:]
        if code.endswith("```"):
            code = code[:-3]
            
        with open("temp_output.py", "w") as f:
            f.write(code.strip())
        
        try:
            subprocess.run(["python3", "temp_output.py"], timeout=10, check=True, capture_output=True)
            self.score = 1.0
            self.success = True
            self.reason = "Code executed successfully with Exit Code 0."
        except subprocess.TimeoutExpired:
            self.score = 0.0
            self.success = False
            self.reason = "Code execution timed out."
        except subprocess.CalledProcessError as e:
            self.score = 0.0
            self.success = False
            self.reason = f"Code failed with exit code {e.returncode}. Stderr: {e.stderr.decode('utf-8')[:200]}"
            
        return self.score

    async def a_measure(self, test_case: LLMTestCase):
        return self.measure(test_case)

    def is_successful(self):
        return self.success

    @property
    def __name__(self):
        return "Programmatic Execution"

def agent_task(model_name: str, system_prompt: str, input_prompt: str, num_ctx: int = 4096):
    """Executes the agent task using litellm routing or direct ollama hit."""
    start_time = time.time()
    proxy_model, api_base, is_direct = resolve_endpoint(model_name)
    
    if is_direct:
        payload = {
            "model": proxy_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_prompt}
            ],
            "stream": False,
            "options": {
                "num_ctx": num_ctx
            }
        }
        res = requests.post(api_base, json=payload, timeout=1200)
        res.raise_for_status()
        data = res.json()
        latency = time.time() - start_time
        output = data.get("message", {}).get("content", "")
        # Map ollama usage keys to litellm keys for consistency
        usage = {
            "prompt_tokens": data.get("prompt_eval_count", 0),
            "completion_tokens": data.get("eval_count", 0),
            "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
        }
    else:
        response = litellm.completion(
            model=proxy_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_prompt}
            ],
            api_base=api_base,
            api_key="sk-dummy",
            num_ctx=num_ctx,
            timeout=1200
        )
        latency = time.time() - start_time
        output = response.choices[0].message.content
        usage = response.usage.model_dump() if response.usage else {}

    # Strip <think> tags from output (for reasoning models)
    output = re.sub(r'<think>.*?</think>', '', output, flags=re.DOTALL).strip()
    return output, latency, usage

def multi_agent_triage_task(orchestrator_model: str, subagent_model: str, system_prompt: str, input_prompt: str):
    """Executes a 3-phase multi-agent triage pipeline."""
    start_time = time.time()
    
    # Phase 1: Subagent generates PromQL
    promql_prompt = f"You are a metrics subagent. Generate a PromQL query for the container mentioned in this alert:\n{input_prompt}\nReturn ONLY the PromQL query. No markdown."
    promql_output, _, usage1 = agent_task(subagent_model, "You are a PromQL expert.", promql_prompt)
    
    # Phase 2: Subagent generates LogQL
    try:
        with open("src/scripts/benchmarks/sre_inputs/mock_promql_result.json", "r") as f:
            mock_metrics = f.read()
    except FileNotFoundError:
        mock_metrics = "{}"
        
    logql_prompt = f"You are a logging subagent. The alert is:\n{input_prompt}\nThe metrics show:\n{mock_metrics}\nGenerate a Loki LogQL query to find errors for this container. Return ONLY the LogQL query. No markdown."
    logql_output, _, usage2 = agent_task(subagent_model, "You are a LogQL expert.", logql_prompt)
    
    # Phase 3: Orchestrator synthesizes
    try:
        with open("src/scripts/benchmarks/sre_inputs/mock_logql_result.json", "r") as f:
            mock_logs = f.read()
    except FileNotFoundError:
        mock_logs = "{}"
        
    orchestrator_prompt = f"Alert Payload:\n{input_prompt}\n\nPromQL Query generated by subagent:\n{promql_output}\nMetrics Result:\n{mock_metrics}\n\nLogQL Query generated by subagent:\n{logql_output}\nLogs Result:\n{mock_logs}\n\nAnalyze this data and provide a remediation plan."
    final_output, _, usage3 = agent_task(orchestrator_model, system_prompt, orchestrator_prompt)
    
    latency = time.time() - start_time
    total_usage = {
        "completion_tokens": usage1.get("completion_tokens", 0) + usage2.get("completion_tokens", 0) + usage3.get("completion_tokens", 0),
        "prompt_tokens": usage1.get("prompt_tokens", 0) + usage2.get("prompt_tokens", 0) + usage3.get("prompt_tokens", 0),
        "total_tokens": usage1.get("total_tokens", 0) + usage2.get("total_tokens", 0) + usage3.get("total_tokens", 0)
    }
    
    combined_output = f"PromQL:\n{promql_output}\n\nLogQL:\n{logql_output}\n\nRemediation Plan:\n{final_output}"
    return combined_output, latency, total_usage

def multi_agent_blog_task(generator_model: str, critic_model: str, refiner_model: str, system_prompt: str, input_prompt: str):
    """Executes a Generator-Critic-Refiner pipeline for blog creation."""
    start_time = time.time()
    
    # Phase 1: Generator
    draft, _, usage1 = agent_task(generator_model, system_prompt, input_prompt)
    
    # Phase 2: Critic
    critic_prompt = f"Original Source:\n{input_prompt}\n\nDraft Blog Post:\n{draft}\n\nReview this draft and provide a bulleted list of critiques or missing information based on the source."
    critique, _, usage2 = agent_task(critic_model, "You are a strict blog editor.", critic_prompt)
    
    # Phase 3: Refiner
    refiner_prompt = f"Original Source:\n{input_prompt}\n\nDraft:\n{draft}\n\nCritiques:\n{critique}\n\nRewrite the final blog post incorporating these critiques."
    final_output, _, usage3 = agent_task(refiner_model, system_prompt, refiner_prompt)
    
    latency = time.time() - start_time
    total_usage = {
        "completion_tokens": usage1.get("completion_tokens", 0) + usage2.get("completion_tokens", 0) + usage3.get("completion_tokens", 0),
        "prompt_tokens": usage1.get("prompt_tokens", 0) + usage2.get("prompt_tokens", 0) + usage3.get("prompt_tokens", 0),
        "total_tokens": usage1.get("total_tokens", 0) + usage2.get("total_tokens", 0) + usage3.get("total_tokens", 0)
    }
    
    return final_output, latency, total_usage, draft, critique

def mob_of_experts_task(orchestrator_model: str, generator_model: str, critic_model: str, refiner_model: str, system_prompt: str, input_prompt: str):
    """Executes a Mob of Experts architecture: Orchestrator -> Fan-Out (Sequential) -> Synthesize."""
    start_time = time.time()
    total_usage = {"completion_tokens": 0, "prompt_tokens": 0, "total_tokens": 0}
    
    def update_usage(u):
        total_usage["completion_tokens"] += u.get("completion_tokens", 0)
        total_usage["prompt_tokens"] += u.get("prompt_tokens", 0)
        total_usage["total_tokens"] += u.get("total_tokens", 0)

    # Phase 1: Orchestrator Sub-prompt Generation
    orch_prompt = f"Original Source/Instructions:\n{input_prompt}\n\nYou are the Orchestrator. Create TWO distinct 'Expert System Prompts' tailored to the requested persona. Expert A should focus on one aspect (e.g. structure/tone) and Expert B on another (e.g. depth/storytelling).\nFormat your output strictly as:\nEXPERT_A_PROMPT: <prompt>\nEXPERT_B_PROMPT: <prompt>"
    orch_out, _, u = agent_task(orchestrator_model, "You are a master AI Orchestrator.", orch_prompt)
    update_usage(u)
    
    expert_a_prompt = "You are an expert technical writer."
    expert_b_prompt = "You are an expert technical writer."
    if "EXPERT_A_PROMPT:" in orch_out and "EXPERT_B_PROMPT:" in orch_out:
        parts = orch_out.split("EXPERT_B_PROMPT:")
        expert_a_prompt = parts[0].replace("EXPERT_A_PROMPT:", "").strip()
        expert_b_prompt = parts[1].strip()

    # Phase 2: Sequential Fan-Out
    # Expert A Pipeline
    draft_a, _, u_ga = agent_task(generator_model, expert_a_prompt, input_prompt)
    update_usage(u_ga)
    critic_a_prompt = f"Original Source:\n{input_prompt}\n\nDraft:\n{draft_a}\n\nProvide critiques based on this persona: {expert_a_prompt}"
    critique_a, _, u_ca = agent_task(critic_model, "You are a strict editor.", critic_a_prompt)
    update_usage(u_ca)
    refiner_a_prompt = f"Original Source:\n{input_prompt}\n\nDraft:\n{draft_a}\n\nCritiques:\n{critique_a}\n\nRewrite."
    final_a, _, u_ra = agent_task(refiner_model, expert_a_prompt, refiner_a_prompt)
    update_usage(u_ra)
    
    # Expert B Pipeline
    draft_b, _, u_gb = agent_task(generator_model, expert_b_prompt, input_prompt)
    update_usage(u_gb)
    critic_b_prompt = f"Original Source:\n{input_prompt}\n\nDraft:\n{draft_b}\n\nProvide critiques based on this persona: {expert_b_prompt}"
    critique_b, _, u_cb = agent_task(critic_model, "You are a strict editor.", critic_b_prompt)
    update_usage(u_cb)
    refiner_b_prompt = f"Original Source:\n{input_prompt}\n\nDraft:\n{draft_b}\n\nCritiques:\n{critique_b}\n\nRewrite."
    final_b, _, u_rb = agent_task(refiner_model, expert_b_prompt, refiner_b_prompt)
    update_usage(u_rb)

    # Phase 3: Synthesis
    synth_prompt = f"Original Request:\n{input_prompt}\n\n--- EXPERT A DRAFT ---\n{final_a}\n\n--- EXPERT B DRAFT ---\n{final_b}\n\nSynthesize these two drafts into the ultimate final blog post that perfectly captures the requested persona."
    final_output, _, u_synth = agent_task(orchestrator_model, system_prompt, synth_prompt)
    update_usage(u_synth)
    
    latency = time.time() - start_time
    return final_output, latency, total_usage, final_a, final_b, orch_out

_ASYNC_WORKER_URL = os.getenv("ASYNC_WORKER_URL", "http://localhost:8080")

async def submit_async_job(session, payload, timeout=1200):
    async with session.post(f"{_ASYNC_WORKER_URL}/jobs/submit", json=payload, timeout=timeout) as response:
        response.raise_for_status()
        data = await response.json()
        return data["job_id"]

async def poll_async_job(session, job_id, timeout=1200):
    start_time = time.time()
    while time.time() - start_time < timeout:
        async with session.get(f"{_ASYNC_WORKER_URL}/jobs/status/{job_id}", timeout=10) as response:
            if response.status == 200:
                data = await response.json()
                if data["status"] == "Complete":
                    return data, time.time() - start_time
                elif data["status"] == "Failed":
                    return data, time.time() - start_time
        await asyncio.sleep(2)
    return {"status": "Timeout"}, time.time() - start_time

async def run_async_task(session, model, prompt, input_text):
    proxy_model, _, _ = resolve_endpoint(model)
    payload = {
        "model": "homelab-auto",
        "payload": {
            "model": proxy_model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": f"{prompt}\n\n{input_text}"}
            ]
        }
    }
    try:
        job_id = await submit_async_job(session, payload)
        result, latency = await poll_async_job(session, job_id)
        if result["status"] == "Complete":
            output = result.get("result", {}).get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = result.get("result", {}).get("usage", {})
            return output, latency, usage
        else:
            return f"Failed: {result}", latency, {}
    except Exception as e:
        return f"Exception: {str(e)}", 0, {}

async def execute_async_batch(tasks):
    results = []
    async with aiohttp.ClientSession() as session:
        for task in tasks:
            concurrency = task.get("concurrency", 1)
            model = task.get("model", "homelab-auto")
            prompt = task.get("prompt", "")
            
            inputs = []
            if "input_dir" in task:
                pattern = task.get("file_pattern", "*")
                search_path = os.path.join(task["input_dir"], pattern)
                for fpath in glob.glob(search_path):
                    with open(fpath, "r") as f:
                        inputs.append((fpath, f.read()))
            elif "input_file" in task:
                try:
                    with open(task["input_file"], "r") as f:
                        content = f.read()
                        for _ in range(concurrency):
                            inputs.append((task["input_file"], content))
                except FileNotFoundError:
                    print(f"[!] Could not find input file: {task['input_file']}")
            
            if not inputs:
                for _ in range(concurrency):
                    inputs.append(("", ""))
            
            semaphore = asyncio.Semaphore(concurrency)
            
            async def bounded_task(fpath, content):
                async with semaphore:
                    out, lat, u = await run_async_task(session, model, prompt, content)
                    return {"file": fpath, "output": out, "latency": lat, "usage": u}
            
            print(f"   Dispatching {len(inputs)} jobs for task {task['id']} at concurrency {concurrency}...")
            if len(inputs) > 200:
                print(f"   Truncating {len(inputs)} to 200 to prevent test timeout.")
                inputs = inputs[:200]

            task_coros = [bounded_task(f, c) for f, c in inputs]
            batch_results = await asyncio.gather(*task_coros)
            
            avg_latency = sum(r['latency'] for r in batch_results) / len(batch_results) if batch_results else 0
            success_count = sum(1 for r in batch_results if not r['output'].startswith("Failed") and not r['output'].startswith("Exception"))
            
            print(f"   [DONE] Task {task['id']} | Avg Latency: {avg_latency:.2f}s | Success: {success_count}/{len(batch_results)}")
            
            results.append({
                "task_id": task['id'],
                "avg_latency": avg_latency,
                "success_rate": f"{success_count}/{len(batch_results)}",
                "sample_output": batch_results[0]['output'] if batch_results else ""
            })
    return results

@app.command()
def run(
    config_path: str = typer.Argument(..., help="Path to experiment YAML spec"),
    litellm_config: str = typer.Option("Tools/litellm_config.yaml", help="Path to litellm config")
):
    """Run an agent experiment defined in a YAML spec."""
    
    # 1. Setup OTEL Tracing to point to Alloy
    setup_tracing()
    
    # We load router config in code or rely on litellm to load it if needed
    # But litellm config loading might require setting os.environ if using litellm proxy, 
    # or we can just ensure endpoints are registered. For local scripts, litellm uses standard syntax.

    with open(config_path, "r") as f:
        exp = yaml.safe_load(f)
        
    experiment_name = exp.get('name', exp.get('experiment_name', 'Unnamed Experiment'))
    print(f"[*] Starting Experiment: {experiment_name}")
    
    judge_model = exp.get('judge_model', "judge-model")
    models_to_test = exp.get('orchestrator_models', []) # Updated to use the correct key from your yaml
    if not models_to_test:
        models_to_test = exp.get('models_to_test', [])

    results = {
        "experiment_name": experiment_name,
        "runs": []
    }

    workflow = exp.get("workflow", "single_agent")
    
    if "tasks" in exp:
        # Custom Async Batch Schema
        print(f"\n>> Evaluating Async Batch Tasks via Worker Queue")
        batch_results = asyncio.run(execute_async_batch(exp["tasks"]))
        results["runs"] = batch_results
        save_experiment_results(experiment_name, results)
        return

    combinations = []
    if "pipeline_combinations" in exp:
        combinations = exp["pipeline_combinations"]
    elif "mob_combinations" in exp:
        combinations = exp["mob_combinations"]
    else:
        for model in models_to_test:
            combinations.append({"model": model})

    # 3. Iterate through combinations and test cases
    for combo in combinations:
        model_name = combo.get('model', combo.get('orchestrator', combo.get('generator', 'pipeline')))
        combo_id = "_".join([v.replace("/", "-").replace(":", "-") for k, v in combo.items()])
        print(f"\n>> Evaluating Pipeline/Model: {combo_id}")
        
        for case in exp.get('test_cases', []):
            try:
                if case['input_file'].lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                    with open(case['input_file'], "rb") as f:
                        base64_image = base64.b64encode(f.read()).decode('utf-8')
                    input_prompt = [
                        {"type": "text", "text": case.get('task_prompt', '')},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                else:
                    with open(case['input_file'], "r") as f:
                        input_content = f.read()
                    input_prompt = f"{case.get('task_prompt', '')}\n\nCode/Input:\n{input_content}"
            except FileNotFoundError:
                print(f"[!] Could not read input file {case['input_file']}")
                continue
                
            expected_output = case.get('expected_output_criteria', '')
            
            print(f"   Running case: {case['name']}...")
            
            # Execute Model Task
            if workflow == "multi_agent_blog_gen":
                gen_m = combo['generator']
                crit_m = combo['critic']
                ref_m = combo['refiner']
                actual_output, latency, usage, draft, critique = multi_agent_blog_task(gen_m, crit_m, ref_m, exp['system_prompt'], input_prompt)
                
                # Save human artifact
                artifact_dir = "results/artifacts"
                os.makedirs(artifact_dir, exist_ok=True)
                safe_case = case['name'].replace(' ', '_').replace('/', '-')
                artifact_path = os.path.join(artifact_dir, f"Blog_{combo_id}_{safe_case}.md")
                with open(artifact_path, "w") as af:
                    af.write(f"# Pipeline: {combo_id}\n\n## Final V2 Blog Post\n\n{actual_output}\n\n---\n## Critic Feedback on V1\n\n{critique}")
                print(f"   [+] Saved artifact to {artifact_path}")
            elif workflow == "mob_of_experts":
                orch_m = combo['orchestrator']
                gen_m = combo['generator']
                crit_m = combo['critic']
                ref_m = combo['refiner']
                actual_output, latency, usage, draft_a, draft_b, orch_out = mob_of_experts_task(
                    orch_m, gen_m, crit_m, ref_m, exp['system_prompt'], input_prompt
                )
                
                # Save human artifact
                artifact_dir = "results/artifacts"
                os.makedirs(artifact_dir, exist_ok=True)
                safe_case = case['name'].replace(' ', '_').replace('/', '-')
                artifact_path = os.path.join(artifact_dir, f"Mob_{combo_id}_{safe_case}.md")
                with open(artifact_path, "w") as af:
                    af.write(f"# Pipeline: {combo_id}\n\n## Final Synthesis\n\n{actual_output}\n\n---\n## Orchestrator Prompts\n\n{orch_out}\n\n---\n## Expert A Draft\n\n{draft_a}\n\n---\n## Expert B Draft\n\n{draft_b}")
                print(f"   [+] Saved artifact to {artifact_path}")
            elif workflow == "multi_agent_triage":
                subagent_model = exp.get("subagent_model", "ollama/qwen2.5-coder:7b")
                actual_output, latency, usage = multi_agent_triage_task(model_name, subagent_model, exp['system_prompt'], input_prompt)
            else:
                num_ctx = exp.get("num_ctx", 4096)
                actual_output, latency, usage = agent_task(model_name, exp['system_prompt'], input_prompt, num_ctx=num_ctx)
            
            # DeepEval Scoring
            test_case_input = input_prompt if isinstance(input_prompt, str) else case.get('task_prompt', '')
            test_case = LLMTestCase(
                input=test_case_input,
                actual_output=actual_output,
                expected_output=expected_output
            )
            
            print(f"   Grading Output...")
            # 1. Execution Metric
            exec_metric = ExecutionMetric()
            exec_score = exec_metric.measure(test_case)
            
            # 2. GEval Metric
            geval = GEval(
                name="Code Requirements Checklist",
                criteria=expected_output,
                evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
                model=CustomLiteLLM(judge_model)
            )
            
            geval_score = asyncio.run(geval.a_measure(test_case))
            
            results["runs"].append({
                "pipeline": combo,
                "case_name": case['name'],
                "latency_sec": round(latency, 3),
                "tokens": usage,
                "actual_output": actual_output,
                "scores": {
                    "ExecutionMetric": exec_score,
                    "ExecutionReason": getattr(exec_metric, 'reason', ''),
                    "GEval": geval_score,
                    "GEvalReason": getattr(geval, 'reason', '')
                }
            })
            print(f"   [DONE] Latency: {latency:.2f}s | GEval: {geval_score} | Exec: {exec_score}")

    # 4. Save standardized output
    save_experiment_results(experiment_name, results)

if __name__ == "__main__":
    app()
