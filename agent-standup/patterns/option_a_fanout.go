package patterns

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/cloudwego/eino/components/prompt"
	"github.com/cloudwego/eino/compose"
	"github.com/cloudwego/eino/schema"
	"homelab/agent-standup/llm"
)

type PersonaConfig struct {
	Name         string
	SystemPrompt string
	TaskPrompt   string
	ModelKey     string
}

func RunOptionA(roadmap string, hardware string, models map[string]string) (string, time.Duration) {
	fmt.Println("=== Starting Option A (Eino Parallel Fan-Out/Gather) ===")
	startTime := time.Now()

	ctx := context.Background()

	// Initialize the telemetry if not already done
	shutdown, err := llm.InitTelemetry(ctx)
	if err == nil && shutdown != nil {
		defer shutdown()
	}

	fmt.Println("[Coordinator] Dispatching specialized tasks concurrently for 8 personas via Eino Parallel...")

	personas := []PersonaConfig{
		{
			Name:         "SRE",
			SystemPrompt: "You are the strict SRE Agent.",
			TaskPrompt:   fmt.Sprintf("You are the SRE Agent. Here is the Hardware Topology: %s\nHere is the Roadmap: %s\nWrite a brief 3-sentence status report focusing ONLY on infrastructure and stability tasks.", hardware, roadmap),
			ModelKey:     "sre",
		},
		{
			Name:         "Dev",
			SystemPrompt: "You are the enthusiastic Dev Agent.",
			TaskPrompt:   fmt.Sprintf("You are the Dev Agent. Here is the Roadmap: %s\nWrite a brief 3-sentence status report focusing ONLY on tooling, asynchronous workers, and feature tasks.", roadmap),
			ModelKey:     "dev",
		},
		{
			Name:         "Manager",
			SystemPrompt: "You are the organized Manager Agent.",
			TaskPrompt:   fmt.Sprintf("You are the Manager Agent. Here is the Roadmap: %s\nWrite a brief 3-sentence status report focusing ONLY on overall project health, tech debt progress, and unblocking the team.", roadmap),
			ModelKey:     "mgr",
		},
		{
			Name:         "Architect",
			SystemPrompt: "You are the pragmatic Architect Agent.",
			TaskPrompt:   fmt.Sprintf("You are the Architect Agent. Here is the Roadmap: %s\nWrite a brief 3-sentence status report focusing ONLY on system design, boundary isolation, and global rules.", roadmap),
			ModelKey:     "arch",
		},
		{
			Name:         "Security",
			SystemPrompt: "You are the paranoid Security Agent.",
			TaskPrompt:   fmt.Sprintf("You are the Security Agent. Here is the Hardware Topology: %s\nHere is the Roadmap: %s\nWrite a brief 3-sentence status report focusing ONLY on hardening, secrets management, and network boundaries.", hardware, roadmap),
			ModelKey:     "sec",
		},
		{
			Name:         "QA",
			SystemPrompt: "You are the skeptical QA Agent.",
			TaskPrompt:   fmt.Sprintf("You are the QA Agent. Here is the Roadmap: %s\nWrite a brief 3-sentence status report focusing ONLY on reproducible benchmarks, test coverage, and LLM evaluation metrics.", roadmap),
			ModelKey:     "qa",
		},
		{
			Name:         "Data",
			SystemPrompt: "You are the conservative Data Agent.",
			TaskPrompt:   fmt.Sprintf("You are the Data Agent. Here is the Hardware Topology: %s\nHere is the Roadmap: %s\nWrite a brief 3-sentence status report focusing ONLY on backup strategies, data resilience, volume persistence, and schema migrations.", hardware, roadmap),
			ModelKey:     "data",
		},
		{
			Name:         "UI/UX",
			SystemPrompt: "You are the user-centric UI/UX Agent.",
			TaskPrompt:   fmt.Sprintf("You are the UI/UX Agent. Here is the Roadmap: %s\nWrite a brief 3-sentence status report focusing ONLY on aesthetics, mobile responsiveness, accessibility, and low-friction interactions.", roadmap),
			ModelKey:     "ui",
		},
	}

	buildPersonaGraph := func(key string, sysPrompt, modelName string) (compose.AnyGraph, error) {
		chatModel, err := llm.NewChatModel(ctx, modelName)
		if err != nil {
			return nil, err
		}

		tpl := prompt.FromMessages(schema.FString,
			schema.SystemMessage(sysPrompt),
			schema.UserMessage("{input}"),
		)

		chain := compose.NewChain[map[string]any, string]()

		// Lambda to extract the specific key's value for this persona from the multi-graph input
		extractor := compose.InvokableLambda(func(ctx context.Context, input map[string]any) (map[string]any, error) {
			val, _ := input[key].(string)
			return map[string]any{"input": val}, nil
		})

		chain.
			AppendLambda(extractor).
			AppendChatTemplate(tpl).
			AppendChatModel(chatModel).
			AppendLambda(compose.InvokableLambda(func(ctx context.Context, msg *schema.Message) (string, error) {
				return msg.Content, nil
			}))

		return chain, nil
	}

	parallel := compose.NewParallel()
	for _, p := range personas {
		modelName := models[p.ModelKey]
		graph, err := buildPersonaGraph(strings.ToLower(p.Name), p.SystemPrompt, modelName)
		if err != nil {
			fmt.Printf("[Error] Failed to build graph for %s: %v\n", p.Name, err)
			return "", 0
		}
		parallel.AddGraph(strings.ToLower(p.Name), graph)
	}

	// Main pipeline chain
	mainChain := compose.NewChain[map[string]any, string]()
	mainChain.AppendParallel(parallel)

	// Aggregator Lambda to convert map outputs from parallel step into a synthesized final prompt
	aggregator := compose.InvokableLambda(func(ctx context.Context, results map[string]any) (map[string]any, error) {
		synthesisPrompt := "Combine the following reports into a single cohesive Standup Document.\n\n"
		for _, p := range personas {
			reportKey := strings.ToLower(p.Name)
			reportVal, ok := results[reportKey].(string)
			if ok && reportVal != "" {
				synthesisPrompt += fmt.Sprintf("### %s Report\n%s\n\n", p.Name, reportVal)
			}
		}
		return map[string]any{"input": synthesisPrompt}, nil
	})
	mainChain.AppendLambda(aggregator)

	// Synthesizer prompts template
	synthTpl := prompt.FromMessages(schema.FString,
		schema.SystemMessage("You are the Standup Synthesizer. Output clean markdown."),
		schema.UserMessage("{input}"),
	)
	mainChain.AppendChatTemplate(synthTpl)

	// Synthesizer model
	synthModel, err := llm.NewChatModel(ctx, models["syn"])
	if err != nil {
		fmt.Printf("[Error] Failed to create synthesizer model: %v\n", err)
		return "", 0
	}
	mainChain.AppendChatModel(synthModel)

	// Extract final response content
	mainChain.AppendLambda(compose.InvokableLambda(func(ctx context.Context, msg *schema.Message) (string, error) {
		return msg.Content, nil
	}))

	runner, err := mainChain.Compile(ctx)
	if err != nil {
		fmt.Printf("[Error] Failed to compile Eino chain: %v\n", err)
		return "", 0
	}

	// Prepare multi-graph input mapping
	inputMap := map[string]any{}
	for _, p := range personas {
		inputMap[strings.ToLower(p.Name)] = p.TaskPrompt
	}

	// Run with our OTel telemetry handler callbacks
	otelHandler := llm.NewEinoOTelHandler()
	finalReport, err := runner.Invoke(ctx, inputMap, compose.WithCallbacks(otelHandler))
	if err != nil {
		fmt.Printf("[Error] Eino runner invocation failed: %v\n", err)
		return "", 0
	}

	elapsed := time.Since(startTime)
	fmt.Printf("\n=== FINAL STANDUP REPORT (Took %v) ===\n%s\n====================================\n\n", elapsed, finalReport)
	return finalReport, elapsed
}
