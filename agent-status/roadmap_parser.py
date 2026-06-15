import os
import re

ROADMAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src", "meta", "ROADMAP.md")

def print_roadmap_status():
    if not os.path.exists(ROADMAP_PATH):
        print(f"Error: Could not find {ROADMAP_PATH}")
        return

    with open(ROADMAP_PATH, "r", encoding="utf-8") as f:
        content = f.read()

    print("\n\033[1;36m=== \U0001f5fa\ufe0f  Homelab Master Roadmap ===\033[0m\n")

    # Extract High-Level Status Dashboard table
    table_match = re.search(r"## \U0001f6a6 High-Level Status Dashboard\n\n(.*?)\n\n---", content, re.DOTALL)
    if table_match:
        print("\033[1;34m[ High-Level Status ]\033[0m")
        lines = table_match.group(1).strip().split('\n')
        for line in lines:
            # Format colors based on status dots
            line = line.replace("\U0001f7e2", "\033[1;32m\U0001f7e2\033[0m")
            line = line.replace("\U0001f7e1", "\033[1;33m\U0001f7e1\033[0m")
            line = line.replace("\U0001f534", "\033[1;31m\U0001f534\033[0m")
            line = line.replace("\U0001f535", "\033[1;34m\U0001f535\033[0m")
            print(line)
        print("")

    # Extract tasks
    sections = re.findall(r"### (.*?)\n(.*?)(?=\n### |\n---|$)", content, re.DOTALL)
    
    total_tasks = 0
    completed_tasks = 0

    for title, body in sections:
        tasks = re.findall(r"(\d+)\.\s*(?:\*\*)?\[(x| |/)\]\s*(.*?)(?:\*\*)?(?:\n|$)", body)
        if not tasks:
            continue
        
        # Colorize titles
        if "Critical" in title:
            print(f"\033[1;31m>>> {title}\033[0m")
        elif "High" in title:
            print(f"\033[1;33m>>> {title}\033[0m")
        elif "Completed" in title:
            print(f"\033[1;32m>>> {title}\033[0m")
        else:
            print(f"\033[1;36m>>> {title}\033[0m")

        for idx, status, name in tasks:
            name = name.replace("**", "").strip()
            total_tasks += 1
            if status == "x":
                completed_tasks += 1
                print(f"  [\033[1;32m✓\033[0m] {name}")
            elif status == "/":
                print(f"  [\033[1;33m/\033[0m] {name}")
            else:
                print(f"  [ ] {name}")
        print("")

    if total_tasks > 0:
        pct = int((completed_tasks / total_tasks) * 100)
        bar_len = 40
        filled = int(bar_len * completed_tasks / total_tasks)
        bar = "█" * filled + "░" * (bar_len - filled)
        
        print("\033[1;34m[ Overall Progress ]\033[0m")
        print(f"{bar} {pct}% ({completed_tasks}/{total_tasks} Tasks)\n")

def get_backlog_context() -> str:
    if not os.path.exists(ROADMAP_PATH):
        return ""
    with open(ROADMAP_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    
    sections = re.findall(r"### (.*?)\n(.*?)(?=\n### |\n---|$)", content, re.DOTALL)
    backlog_texts = []
    for title, body in sections:
        if "Backlog" in title:
            backlog_texts.append(body.strip())
    return "\n".join(backlog_texts)
