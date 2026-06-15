package llm

import (
	"context"
	"fmt"
	"os"
	"strings"

	"github.com/cloudwego/eino-ext/components/model/openai"
	"github.com/cloudwego/eino/components/model"
	"github.com/cloudwego/eino/schema"
)

func getProxyURL() string {
	if u := os.Getenv("OLLAMA_PROXY_URL"); u != "" {
		return u
	}
	return "http://localhost:4000/v1"
}

// IsSequential indicates if the execution is running in sequential mode (Option B).
// This is used to route standard models to the SRE node or the Workstation.
var IsSequential bool

// IsStrictLocal forces all models to route to the SRE node regardless of size.
var IsStrictLocal bool

// RouteModel intercepts the model name and prefixes it with the workstation or SRE
// routing prefix if it doesn't already have one.
func RouteModel(modelName string) string {
	// If already explicitly routed or prefixed, do not modify
	if strings.HasPrefix(modelName, "ollama/") || strings.HasPrefix(modelName, "openai/") {
		return modelName
	}
	if strings.Contains(modelName, "/") {
		return modelName
	}

	if IsStrictLocal {
		routed := "ollama/sre/" + modelName
		fmt.Printf("[Routing] Strict Local mode: %q -> Prefixed to route to SRE Node: %q\n", modelName, routed)
		return routed
	}

	lower := strings.ToLower(modelName)
	isHeavy := strings.Contains(lower, "14b") ||
		strings.Contains(lower, "12b") ||
		strings.Contains(lower, "deepseek-r1") ||
		strings.Contains(lower, "phi4") ||
		strings.Contains(lower, "mistral-nemo")

	if isHeavy {
		// Route heavy model to the Workstation by prefixing it
		routed := "ollama/ws/" + modelName
		fmt.Printf("[Routing] Heavy model detected: %q -> Prefixed to route to Workstation: %q\n", modelName, routed)
		return routed
	}
	
	// Route based on execution mode
	var routed string
	if IsSequential {
		routed = "ollama/sre/" + modelName
		fmt.Printf("[Routing] Sequential mode: %q -> Prefixed to route to SRE Node: %q\n", modelName, routed)
	} else {
		routed = "ollama/ws/" + modelName
		fmt.Printf("[Routing] Concurrent mode: %q -> Prefixed to route to Workstation: %q\n", modelName, routed)
	}
	return routed
}

// NewChatModel creates an Eino ChatModel component with prefix routing.
func NewChatModel(ctx context.Context, modelName string) (model.ChatModel, error) {
	routed := RouteModel(modelName)
	return openai.NewChatModel(ctx, &openai.ChatModelConfig{
		Model:   routed,
		BaseURL: getProxyURL(),
		APIKey:  "sk-local",
	})
}

// Generate implements the legacy high-level Generate function using Eino.
func Generate(modelName string, systemPrompt string, userPrompt string) (string, error) {
	ctx := context.Background()
	chatModel, err := NewChatModel(ctx, modelName)
	if err != nil {
		return "", fmt.Errorf("failed to create chat model: %w", err)
	}

	resp, err := chatModel.Generate(ctx, []*schema.Message{
		schema.SystemMessage(systemPrompt),
		schema.UserMessage(userPrompt),
	})
	if err != nil {
		return "", fmt.Errorf("failed to generate response: %w", err)
	}

	return resp.Content, nil
}
