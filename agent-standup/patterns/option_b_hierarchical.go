package patterns

import (
	"context"
	"fmt"
	"time"

	"github.com/cloudwego/eino/components/prompt"
	"github.com/cloudwego/eino/compose"
	"github.com/cloudwego/eino/schema"
	"homelab/agent-standup/llm"
)

// RunOptionB executes the Hierarchical pattern sequentially using Eino.
func RunOptionB(roadmap string, hardware string, models map[string]string) (string, time.Duration) {
	fmt.Println("=== Starting Option B (Eino Sequential Delegation) ===")
	startTime := time.Now()

	ctx := context.Background()

	// Initialize the telemetry if not already done
	shutdown, err := llm.InitTelemetry(ctx)
	if err == nil && shutdown != nil {
		defer shutdown()
	}

	fmt.Println("[Coordinator] Dispatching specialized tasks sequentially for 8 personas via Eino Chains...")

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

	buildPersonaChain := func(sysPrompt, modelName string) (compose.Runnable[string, string], error) {
		chatModel, err := llm.NewChatModel(ctx, modelName)
		if err != nil {
			return nil, err
		}

		tpl := prompt.FromMessages(schema.FString,
			schema.SystemMessage(sysPrompt),
			schema.UserMessage("{input}"),
		)

		chain := compose.NewChain[string, string]()

		// Map string input to map[string]any expected by tpl
		mapper := compose.InvokableLambda(func(ctx context.Context, input string) (map[string]any, error) {
			return map[string]any{"input": input}, nil
		})

		chain.
			AppendLambda(mapper).
			AppendChatTemplate(tpl).
			AppendChatModel(chatModel).
			AppendLambda(compose.InvokableLambda(func(ctx context.Context, msg *schema.Message) (string, error) {
				return msg.Content, nil
			}))

		return chain.Compile(ctx)
	}

	otelHandler := llm.NewEinoOTelHandler()
	reports := make([]string, len(personas))

	for i, config := range personas {
		fmt.Printf("[%s Agent] Generating report sequentially via Eino...\n", config.Name)
		modelName := models[config.ModelKey]
		runner, err := buildPersonaChain(config.SystemPrompt, modelName)
		if err != nil {
			fmt.Printf("[Error] Failed to build Eino chain for %s: %v\n", config.Name, err)
			return "", 0
		}

		report, err := runner.Invoke(ctx, config.TaskPrompt, compose.WithCallbacks(otelHandler))
		if err != nil {
			fmt.Printf("[Error] %s Agent failed: %v\n", config.Name, err)
			return "", 0
		}

		reports[i] = fmt.Sprintf("### %s Report\n%s", config.Name, report)
		fmt.Printf("  <- [%s Agent] Responded.\n", config.Name)
	}

	fmt.Println("[Synthesizer] Compiling final standup report via Eino...")
	synthesisPrompt := "Combine the following reports into a single cohesive Standup Document.\n\n"
	for _, r := range reports {
		synthesisPrompt += r + "\n\n"
	}

	synthRunner, err := buildPersonaChain("You are the Standup Synthesizer. Output clean markdown.", models["syn"])
	if err != nil {
		fmt.Printf("[Error] Failed to build Eino synthesizer chain: %v\n", err)
		return "", 0
	}

	finalReport, err := synthRunner.Invoke(ctx, synthesisPrompt, compose.WithCallbacks(otelHandler))
	if err != nil {
		fmt.Printf("[Error] Synthesizer compilation failed: %v\n", err)
		return "", 0
	}

	elapsed := time.Since(startTime)
	fmt.Printf("\n=== FINAL STANDUP REPORT (Took %v) ===\n%s\n====================================\n\n", elapsed, finalReport)
	return finalReport, elapsed
}
