package main

import (
	"flag"
	"fmt"
	"homelab/agent-standup/llm"
	"homelab/agent-standup/patterns"
	"os"
	"time"
)

func main() {
	sreModel := flag.String("sre", "homelab-auto", "Model for SRE agent")
	devModel := flag.String("dev", "homelab-auto", "Model for Dev agent")
	mgrModel := flag.String("mgr", "homelab-auto", "Model for Manager agent")
	archModel := flag.String("arch", "homelab-auto", "Model for Architect agent")
	secModel := flag.String("sec", "homelab-auto", "Model for Security agent")
	qaModel := flag.String("qa", "homelab-auto", "Model for QA agent")
	dataModel := flag.String("data", "homelab-auto", "Model for Data agent")
	uiModel := flag.String("ui", "homelab-auto", "Model for UI/UX agent")
	synModel := flag.String("syn", "homelab-auto", "Model for Synthesizer agent")
	outPath := flag.String("out", "report.md", "Output path for the report")
	roadmapPath := flag.String("roadmap", "", "Path to roadmap markdown file (required)")
	hardwarePath := flag.String("hardware", "", "Path to hardware/context markdown file (required)")
	seqFlag := flag.Bool("seq", false, "Run sequentially (Option B) instead of concurrently (Option A)")
	strictLocalFlag := flag.Bool("strict-local", false, "Force all models to execute on the local SRE node, overriding heavy model routing")
	flag.Parse()

	if *roadmapPath == "" || *hardwarePath == "" {
		fmt.Fprintln(os.Stderr, "error: --roadmap and --hardware flags are required")
		flag.Usage()
		os.Exit(1)
	}

	llm.IsSequential = *seqFlag
	llm.IsStrictLocal = *strictLocalFlag

	roadmap, err := os.ReadFile(*roadmapPath)
	if err != nil {
		fmt.Printf("Failed to read ROADMAP: %v\n", err)
		os.Exit(1)
	}

	hardware, err := os.ReadFile(*hardwarePath)
	if err != nil {
		fmt.Printf("Failed to read hardware context: %v\n", err)
		os.Exit(1)
	}

	roadmapStr := string(roadmap)
	hardwareStr := string(hardware)

	models := map[string]string{
		"sre":  *sreModel,
		"dev":  *devModel,
		"mgr":  *mgrModel,
		"arch": *archModel,
		"sec":  *secModel,
		"qa":   *qaModel,
		"data": *dataModel,
		"ui":   *uiModel,
		"syn":  *synModel,
	}

	var report string
	var latency time.Duration

	if *seqFlag {
		report, latency = patterns.RunOptionB(roadmapStr, hardwareStr, models)
	} else {
		report, latency = patterns.RunOptionA(roadmapStr, hardwareStr, models)
	}

	if report != "" {
		err = os.WriteFile(*outPath, []byte(report), 0644)
		if err != nil {
			fmt.Printf("Failed to write report: %v\n", err)
		}
		fmt.Printf("Latency: %f seconds\n", latency.Seconds())
	}
}
