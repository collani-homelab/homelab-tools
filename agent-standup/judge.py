import sys
import json
import requests
import os

PROXY_URL = os.getenv("OLLAMA_PROXY_URL", "http://localhost:4000/v1/chat/completions")
JUDGE_MODEL = "ollama/ws/qwen2.5-coder:14b"

def score_report(filepath):
    try:
        with open(filepath, 'r') as f:
            report = f.read()
            
        system_prompt = """You are an impartial Judge evaluating a Multi-Agent Standup Report. 
Score the report between 0.0 and 1.0 based on:
1. Persona Adherence (Did SRE, Dev, Manager stay in character?)
2. Roadmap Accuracy (Are the tasks actually from the Roadmap without hallucinations?)
3. Hardware Accuracy (No mention of AWS/Cloud/K8s; must be local Unraid/UniFi).
Return ONLY a valid JSON object: {"score": 0.8, "reason": "Explanation"}"""

        payload = {
            "model": JUDGE_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Here is the report:\n{report}"}
            ]
        }
        
        response = requests.post(PROXY_URL, json=payload)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        
        if content.startswith("```json"):
            content = content[7:-3]
        elif content.startswith("```"):
            content = content[3:-3]
            
        result = json.loads(content)
        return result
    except Exception as e:
        return {"score": 0.0, "reason": f"Judge failed: {str(e)}"}

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python judge.py <report_file> <config_name> <latency_sec>")
        sys.exit(1)
        
    report_file = sys.argv[1]
    config_name = sys.argv[2]
    latency = float(sys.argv[3])
    
    evaluation = score_report(report_file)
    evaluation["config"] = config_name
    evaluation["latency_sec"] = latency
    
    print(json.dumps(evaluation))
