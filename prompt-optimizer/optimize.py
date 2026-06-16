#!/usr/bin/env python3
"""Hill-climbing prompt optimizer — uses llm-eval-kit's GEval scoring as the fitness function."""
import asyncio
import json
import re
from datetime import datetime

import litellm
import typer
import yaml
from cli import CustomLiteLLM, agent_task, resolve_endpoint
from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCase, SingleTurnParams
from eval_logger import push_metrics_to_prometheus, save_experiment_results

app = typer.Typer(help="Hill-climbing prompt optimizer on top of llm-eval-kit")


def mutate_prompt(base_prompt: str, task_description: str, mutator_model: str, n: int) -> list[str]:
    if n <= 0:
        return []
    proxy_model, api_base, is_direct = resolve_endpoint(mutator_model)
    if is_direct:
        raise NotImplementedError("Direct routing is not supported for the mutator model.")

    instruction = (
        f"Task the prompt is used for: {task_description}\n\n"
        f"Current prompt:\n---\n{base_prompt}\n---\n\n"
        f"Generate {n} alternative rewrites of this prompt that aim to perform the task better "
        "(clearer constraints, better phrasing, sharper instructions). Preserve any hard constraints "
        "(length limits, output format) unless the task description implies they should change.\n"
        f"Return ONLY a JSON array of {n} strings, no markdown, no explanation."
    )
    response = litellm.completion(
        model=proxy_model,
        messages=[{"role": "user", "content": instruction}],
        api_base=api_base,
        api_key="sk-dummy",
        timeout=120,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        variants = json.loads(raw)
        if isinstance(variants, list) and all(isinstance(v, str) for v in variants):
            return variants[:n]
    except json.JSONDecodeError:
        pass
    print("[!] Mutator returned unparseable output, reusing base prompt for this slot")
    return [base_prompt] * n


def evaluate_variant(variant_prompt, variant_id, test_cases, target_model, judge_model, experiment_name) -> float:
    scores = []
    for case in test_cases:
        actual_output, latency, _usage = agent_task(target_model, variant_prompt, case["input"])
        test_case = LLMTestCase(
            input=case["input"],
            actual_output=actual_output,
            expected_output=case["expected_output_criteria"],
        )
        geval = GEval(
            name="Prompt Quality",
            criteria=case["expected_output_criteria"],
            evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
            model=CustomLiteLLM(judge_model),
        )
        score = asyncio.run(geval.a_measure(test_case))
        scores.append(score)
        push_metrics_to_prometheus(experiment_name, variant_id, case["name"], {"GEval": score}, latency)
    return sum(scores) / len(scores) if scores else 0.0


@app.command()
def run(config_path: str = typer.Argument(..., help="Path to optimizer YAML spec")):
    """Run a prompt optimization loop defined in a YAML spec."""
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    name = cfg.get("name", "prompt-optimization")
    base_prompt = cfg["base_prompt"]
    task_description = cfg["task_description"]
    target_model = cfg["target_model"]
    mutator_model = cfg.get("mutator_model", target_model)
    judge_model = cfg["judge_model"]
    generations = cfg.get("generations", 3)
    variants_per_generation = cfg.get("variants_per_generation", 4)
    top_k = cfg.get("top_k", 2)
    test_cases = cfg["test_cases"]

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    experiment_name = f"{name}_{timestamp}"
    print(f"[*] Starting prompt optimization: {experiment_name}")

    population = [base_prompt]
    history = []
    best_prompt, best_score = base_prompt, float("-inf")

    for gen in range(generations):
        print(f"\n>> Generation {gen}")
        needed = variants_per_generation - len(population)
        if needed > 0:
            population += mutate_prompt(population[0], task_description, mutator_model, needed)

        scored = []
        for i, variant in enumerate(population):
            variant_id = f"gen{gen}_v{i}"
            avg_score = evaluate_variant(variant, variant_id, test_cases, target_model, judge_model, experiment_name)
            scored.append((avg_score, variant_id, variant))
            print(f"   {variant_id}: {avg_score:.3f}")

        scored.sort(key=lambda t: t[0], reverse=True)
        history.append({"generation": gen, "scores": [{"variant_id": vid, "score": s} for s, vid, _ in scored]})

        if scored[0][0] > best_score:
            best_score, best_prompt = scored[0][0], scored[0][2]

        survivors = [variant for _, _, variant in scored[:top_k]]
        next_population = list(survivors)
        per_survivor = max(1, variants_per_generation // max(1, top_k))
        for survivor in survivors:
            remaining = variants_per_generation - len(next_population)
            if remaining <= 0:
                break
            next_population += mutate_prompt(survivor, task_description, mutator_model, min(remaining, per_survivor))
        population = next_population[:variants_per_generation] or survivors

    print(f"\n[DONE] Best score: {best_score:.3f}")
    print(f"Winning prompt:\n{best_prompt}")

    save_experiment_results(experiment_name, {
        "experiment_name": experiment_name,
        "base_prompt": base_prompt,
        "best_prompt": best_prompt,
        "best_score": best_score,
        "history": history,
    })


if __name__ == "__main__":
    app()
