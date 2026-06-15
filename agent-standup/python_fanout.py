import concurrent.futures
import time
import requests
import json
import os

PROXY_URL = os.getenv("OLLAMA_PROXY_URL", "http://localhost:4000/v1/chat/completions")
MODEL = "homelab-auto"

def generate(system_prompt, user_prompt):
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    }
    response = requests.post(PROXY_URL, json=payload)
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"]

def main():
    print("=== Starting Python Fan-Out/Gather ===")
    start_time = time.time()
    
    with open("../../src/meta/ROADMAP.md", "r") as f:
        roadmap = f.read()
    with open("../../src/meta/ARCH_HARDWARE.md", "r") as f:
        hardware = f.read()
        
    sre_prompt = f"You are the SRE Agent. Here is the Hardware Topology: {hardware}\nHere is the Roadmap: {roadmap}\nWrite a brief 3-sentence status report focusing ONLY on infrastructure and stability tasks."
    dev_prompt = f"You are the Dev Agent. Here is the Roadmap: {roadmap}\nWrite a brief 3-sentence status report focusing ONLY on tooling, asynchronous workers, and feature tasks."
    mgr_prompt = f"You are the Manager Agent. Here is the Roadmap: {roadmap}\nWrite a brief 3-sentence status report focusing ONLY on overall project health, tech debt progress, and unblocking the team."
    
    print("[Coordinator] Dispatching specialized tasks concurrently...")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        print("  -> [SRE Agent] Started...")
        future_sre = executor.submit(generate, "You are the strict SRE Agent.", sre_prompt)
        print("  -> [Dev Agent] Started...")
        future_dev = executor.submit(generate, "You are the enthusiastic Dev Agent.", dev_prompt)
        print("  -> [Manager Agent] Started...")
        future_mgr = executor.submit(generate, "You are the organized Manager Agent.", mgr_prompt)
        
        sre_report = future_sre.result()
        print("  <- [SRE Agent] Completed.")
        dev_report = future_dev.result()
        print("  <- [Dev Agent] Completed.")
        mgr_report = future_mgr.result()
        print("  <- [Manager Agent] Completed.")
        
    print("[Synthesizer] Gathering reports and synthesizing final output...")
    synthesis_prompt = f"Combine the following reports into a single cohesive Standup Document.\nSRE Report: {sre_report}\nDev Report: {dev_report}\nManager Report: {mgr_report}"
    
    final_report = generate("You are the Standup Synthesizer. Output clean markdown.", synthesis_prompt)
    
    elapsed = time.time() - start_time
    print(f"\n=== FINAL STANDUP REPORT (Took {elapsed:.2f}s) ===\n{final_report}\n====================================\n")

if __name__ == "__main__":
    main()
