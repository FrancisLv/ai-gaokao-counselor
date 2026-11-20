package llmgw

import (
	"regexp"
	"sort"
)

var directIdentifierPatterns = []*regexp.Regexp{
	// 中国大陆 18 位居民身份证号。
	regexp.MustCompile(`(?:^|[^0-9])[1-9][0-9]{5}(?:18|19|20)[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])[0-9]{3}[0-9Xx](?:[^0-9Xx]|$)`),
	// 中国大陆手机号，允许 +86 前缀与分隔符。
	regexp.MustCompile(`(?:^|[^0-9])(?:\+?86[-\s]?)?1[3-9][0-9]{9}(?:[^0-9]|$)`),
	regexp.MustCompile(`(?i)[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}`),
	// 常见模型 key、访问 token 和私钥头。只用于拒绝，不回显匹配内容。
	regexp.MustCompile(`(?i)\b(?:sk|pk|api|token|secret)[-_][A-Z0-9_-]{8,}\b`),
	regexp.MustCompile(`(?i)\b(?:AKIA|ASIA)[A-Z0-9]{16}\b`),
	regexp.MustCompile(`(?i)\bBearer\s+[A-Z0-9._~+\-/]{16,}`),
	regexp.MustCompile(`(?i)\beyJ[A-Z0-9_-]{8,}\.[A-Z0-9_-]{8,}\.[A-Z0-9_-]{8,}`),
	regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----`),
}

var compactIdentifierSeparators = regexp.MustCompile(`[\s-]`)

var compactDirectIdentifierPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?:^|[^0-9])[1-9][0-9]{5}(?:18|19|20)[0-9]{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])[0-9]{3}[0-9Xx](?:[^0-9Xx]|$)`),
	regexp.MustCompile(`(?:^|[^0-9])(?:86)?1[3-9][0-9]{9}(?:[^0-9]|$)`),
}

var sanitizedSensitiveConclusionPattern = regexp.MustCompile(
	`(?i)(?:色觉|辨色(?:力)?|视力|健康(?:状况|情况|限制)?|性别|户籍|学籍|体检(?:结果|结论)?)` +
		`(?:要求|条件|限制|核验|规则|结论|结果|适用性|状态){0,3}` +
		`\s*(?:为|是|[:：=])?\s*(?:结论|结果|状态)?\s*(?:为|是|[:：=])?\s*` +
		`(?:PASS|FAIL|UNKNOWN)`,
)

var rawSensitiveFactPatterns = []*regexp.Regexp{
	regexp.MustCompile(`(?i)(?:色盲|色弱|红绿色盲|红绿色弱|近视|远视|散光|失明|残疾|乙肝|心脏病|哮喘|过敏|怀孕|病史)`),
	regexp.MustCompile(`(?i)(?:裸眼|矫正)?视力\s*(?:为|是|[:：=])?\s*[0-9]+(?:\.[0-9]+)?`),
	regexp.MustCompile(`(?i)(?:色觉|辨色力)\s*(?:为|是|[:：=])?\s*(?:正常|异常)`),
	regexp.MustCompile(`(?i)(?:健康|体检)(?:状况|情况|结果|报告)?\s*(?:为|是|[:：=])\s*[^，,。；;\n]{1,40}`),
	regexp.MustCompile(`(?i)(?:性别|户籍|学籍)\s*(?:为|是|[:：=])\s*[^，,。；;\n]{1,40}`),
}

func rejectUnsafeInput(req governedRequest) error {
	values := make([]string, 0, len(req.Messages)+len(req.PromptVariables)+2)
	values = append(values, req.TraceID)
	for _, message := range req.Messages {
		values = append(values, message.Content)
	}
	keys := make([]string, 0, len(req.PromptVariables))
	for key := range req.PromptVariables {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		values = append(values, key, req.PromptVariables[key])
	}
	if containsUnsafeStringValues(values) {
		return sensitiveInputNotAllowed()
	}
	return nil
}

func rejectUnsafeRenderedMessages(messages []ProviderMessage) error {
	values := make([]string, 0, len(messages))
	for _, message := range messages {
		values = append(values, message.Content)
	}
	if containsUnsafeStringValues(values) {
		return sensitiveInputNotAllowed()
	}
	return nil
}

func containsUnsafeStringValues(values []string) bool {
	for _, value := range values {
		if containsDirectIdentifier(value) || containsRawSensitiveFact(value) {
			return true
		}
	}
	return false
}

func containsDirectIdentifier(value string) bool {
	for _, pattern := range directIdentifierPatterns {
		if pattern.MatchString(value) {
			return true
		}
	}
	compact := compactIdentifierSeparators.ReplaceAllString(value, "")
	for _, pattern := range compactDirectIdentifierPatterns {
		if pattern.MatchString(compact) {
			return true
		}
	}
	return false
}

func containsRawSensitiveFact(value string) bool {
	withoutConclusions := sanitizedSensitiveConclusionPattern.ReplaceAllString(value, " ")
	for _, pattern := range rawSensitiveFactPatterns {
		if pattern.MatchString(withoutConclusions) {
			return true
		}
	}
	return false
}

func containsUnsafeProviderOutput(result providerResult) bool {
	if containsUnsafeStringValues([]string{
		result.Content,
		result.ProviderRequestID,
		result.FinishReason,
	}) {
		return true
	}
	return containsUnsafeStructuredValue(result.StructuredOutput)
}

func containsUnsafeStructuredValue(value any) bool {
	switch typed := value.(type) {
	case string:
		return containsDirectIdentifier(typed) || containsRawSensitiveFact(typed)
	case []any:
		for _, item := range typed {
			if containsUnsafeStructuredValue(item) {
				return true
			}
		}
	case map[string]any:
		for key, item := range typed {
			if containsDirectIdentifier(key) || containsRawSensitiveFact(key) ||
				containsUnsafeStructuredValue(item) {
				return true
			}
		}
	}
	return false
}
