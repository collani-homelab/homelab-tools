#!/bin/bash

cd "$(dirname "${BASH_SOURCE[0]}")"
go build

echo "[] Starting Phase 4 Overnight Matrix Benchmarks..."
mkdir -p results

run_config() {
    local name=$1
    local syn=$2
    local sre=$3
    local dev=$4
    local mgr=$5
    
    echo "Running $name..."
    ./agent-standup -syn "$syn" -sre "$sre" -dev "$dev" -mgr "$mgr" -out "results/${name}.md" > "results/${name}.log"
    
    local lat=$(grep "Latency: " results/${name}.log | awk '{print $2}')
    if [ -z "$lat" ]; then
        lat="0.0"
    fi
    
    ../agent-eval/.venv/bin/python judge.py "results/${name}.md" "$name" "$lat" > "results/${name}_score.json"
    echo "$name complete."
}

run_config_8() {
    local name=$1
    local syn=$2
    local sre=$3
    local dev=$4
    local mgr=$5
    local arch=$6
    local sec=$7
    local qa=$8
    local data=$9
    local ui=${10}
    
    echo "Running 8-Persona Config $name..."
    ./agent-standup -syn "$syn" -sre "$sre" -dev "$dev" -mgr "$mgr" -arch "$arch" -sec "$sec" -qa "$qa" -data "$data" -ui "$ui" -out "results/${name}.md" > "results/${name}.log"
    
    local lat=$(grep "Latency: " results/${name}.log | awk '{print $2}')
    if [ -z "$lat" ]; then
        lat="0.0"
    fi
    
    ../agent-eval/.venv/bin/python judge.py "results/${name}.md" "$name" "$lat" > "results/${name}_score.json"
    echo "$name complete."
}

run_config_8_seq() {
    local name=$1
    local syn=$2
    local sre=$3
    local dev=$4
    local mgr=$5
    local arch=$6
    local sec=$7
    local qa=$8
    local data=$9
    local ui=${10}
    
    echo "Running 8-Persona Sequential Config $name..."
    ./agent-standup -seq -syn "$syn" -sre "$sre" -dev "$dev" -mgr "$mgr" -arch "$arch" -sec "$sec" -qa "$qa" -data "$data" -ui "$ui" -out "results/${name}.md" > "results/${name}.log"
    
    local lat=$(grep "Latency: " results/${name}.log | awk '{print $2}')
    if [ -z "$lat" ]; then
        lat="0.0"
    fi
    
    ../agent-eval/.venv/bin/python judge.py "results/${name}.md" "$name" "$lat" > "results/${name}_score.json"
    echo "$name complete."
}

# Edge-Seq
run_config_8_seq "Edge_Seq" "ollama/sre/mistral-nemo:12b" "ollama/sre/mistral-nemo:12b" "ollama/sre/mistral-nemo:12b" "ollama/sre/hermes3:8b" "ollama/sre/mistral-nemo:12b" "ollama/sre/mistral-nemo:12b" "ollama/sre/mistral-nemo:12b" "ollama/sre/mistral-nemo:12b" "ollama/sre/hermes3:8b"

# Mono-Nemo (Concurrent mistral-nemo:12b on Workstation for comparison)
run_config_8 "Mono_Nemo" "ollama/ws/mistral-nemo:12b" "ollama/ws/mistral-nemo:12b" "ollama/ws/mistral-nemo:12b" "ollama/ws/mistral-nemo:12b" "ollama/ws/mistral-nemo:12b" "ollama/ws/mistral-nemo:12b" "ollama/ws/mistral-nemo:12b" "ollama/ws/mistral-nemo:12b" "ollama/ws/mistral-nemo:12b"

echo "[] All benchmarks finished!"
cat results/*_score.json

