package llm

import (
	"context"
	"fmt"
	"os"

	"github.com/cloudwego/eino/callbacks"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.17.0"
	"go.opentelemetry.io/otel/trace"
)

var tracer trace.Tracer

// InitTelemetry configures standard OpenTelemetry HTTP OTLP trace exporting.
// It returns a shutdown function to flush any pending spans on exit.
func InitTelemetry(ctx context.Context) (func(), error) {
	endpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
	if endpoint == "" {
		endpoint = "localhost:4319"
	}

	// Create OTLP HTTP exporter
	exporter, err := otlptracehttp.New(ctx,
		otlptracehttp.WithEndpoint(endpoint),
		otlptracehttp.WithInsecure(),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create OTLP trace exporter: %w", err)
	}

	// Create resource attributes
	res, err := resource.New(ctx,
		resource.WithAttributes(
			semconv.ServiceNameKey.String("agent-standup"),
		),
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create resource: %w", err)
	}

	// Set up TracerProvider
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(tp)

	tracer = otel.Tracer("agent-standup")
	fmt.Printf("[*] OpenTelemetry configured to export to http://%s/v1/traces\n", endpoint)

	shutdown := func() {
		if err := tp.Shutdown(context.Background()); err != nil {
			fmt.Printf("[*] Error shutting down TracerProvider: %v\n", err)
		}
	}

	return shutdown, nil
}

// NewEinoOTelHandler builds an Eino CallbackHandler using Eino's native callbacks builder.
// It intercepts component execution to record spans, conventional attributes, and errors.
func NewEinoOTelHandler() callbacks.Handler {
	if tracer == nil {
		tracer = otel.Tracer("agent-standup")
	}

	return callbacks.NewHandlerBuilder().
		OnStartFn(func(ctx context.Context, info *callbacks.RunInfo, input callbacks.CallbackInput) context.Context {
			spanName := fmt.Sprintf("eino.%s.%s", info.Component, info.Name)
			if info.Name == "" {
				spanName = fmt.Sprintf("eino.%s", info.Component)
			}
			ctx, span := tracer.Start(ctx, spanName)
			span.SetAttributes(
				attribute.String("eino.component.type", string(info.Component)),
				attribute.String("eino.component.name", info.Name),
			)
			return ctx
		}).
		OnEndFn(func(ctx context.Context, info *callbacks.RunInfo, output callbacks.CallbackOutput) context.Context {
			span := trace.SpanFromContext(ctx)
			if span.IsRecording() {
				span.End()
			}
			return ctx
		}).
		OnErrorFn(func(ctx context.Context, info *callbacks.RunInfo, err error) context.Context {
			span := trace.SpanFromContext(ctx)
			if span.IsRecording() {
				span.RecordError(err)
				span.SetStatus(codes.Error, err.Error())
				span.End()
			}
			return ctx
		}).
		Build()
}
