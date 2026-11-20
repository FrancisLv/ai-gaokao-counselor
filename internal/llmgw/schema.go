package llmgw

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"strings"

	jsonschema "github.com/google/jsonschema-go/jsonschema"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"

	llmgwdb "github.com/choucisan/ai-gaokao-counselor/internal/llmgw/dbgen"
)

type responseSchema struct {
	key      string
	version  string
	digest   string
	document []byte
	compiled *jsonschema.Resolved
}

func (s *Service) loadResponseSchema(
	ctx context.Context,
	req governedRequest,
	route routeConfig,
) (*responseSchema, error) {
	key := strings.TrimSpace(req.SchemaKey)
	version := strings.TrimSpace(req.SchemaVersion)
	rawSupplied := strings.TrimSpace(req.RawSchema) != ""
	referenceSupplied := key != "" || version != ""
	if !route.requiresResponseSchema && !referenceSupplied && !rawSupplied {
		return nil, nil
	}
	if key == "" || version == "" {
		return nil, newStableError(
			codes.InvalidArgument, 400, ReasonSchemaNotAllowed,
			"结构化调用必须引用已注册的 response schema key/version",
		)
	}

	row, err := s.queries.GetResponseSchema(ctx, llmgwdb.GetResponseSchemaParams{
		ReleaseID:             req.ReleaseID,
		ResponseSchemaKey:     key,
		ResponseSchemaVersion: version,
	})
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, responseSchemaNotAllowed()
	}
	if err != nil {
		return nil, newStableError(codes.Internal, 500, "INTERNAL", "读取 response schema 失败")
	}
	if row.Status != "ACTIVE" {
		return nil, responseSchemaNotAllowed()
	}
	schema := responseSchema{
		key:      row.ResponseSchemaKey,
		version:  row.ResponseSchemaVersion,
		digest:   row.SchemaSha256,
		document: []byte(row.SchemaDocument),
	}

	var definition jsonschema.Schema
	if err := json.Unmarshal(schema.document, &definition); err != nil {
		return nil, responseSchemaNotAllowed()
	}
	compiled, err := definition.Resolve(nil)
	if err != nil {
		return nil, responseSchemaNotAllowed()
	}
	schema.compiled = compiled
	return &schema, nil
}

func responseSchemaNotAllowed() error {
	return newStableError(
		codes.InvalidArgument, 400, ReasonSchemaNotAllowed,
		"response schema 未注册、已撤销、版本不受支持或内容不可解析",
	)
}

func validateStructuredOutput(schema *responseSchema, content string) (map[string]any, error) {
	decoder := json.NewDecoder(strings.NewReader(content))
	var output map[string]any
	if err := decoder.Decode(&output); err != nil {
		return nil, err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err == nil {
		return nil, errors.New("multiple JSON values")
	} else if !errors.Is(err, io.EOF) {
		return nil, err
	}
	if err := schema.compiled.Validate(output); err != nil {
		return nil, err
	}
	return output, nil
}
