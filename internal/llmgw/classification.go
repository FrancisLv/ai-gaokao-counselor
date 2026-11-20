package llmgw

import (
	"strings"

	"google.golang.org/grpc/codes"

	llmgwv1 "github.com/choucisan/ai-gaokao-counselor/api/llmgw/v1"
)

const (
	dataClassPublicData               = "PUBLIC_DATA"
	dataClassPrincipalData            = "PRINCIPAL_DATA"
	dataClassInquiryData              = "INQUIRY_DATA"
	dataClassSensitiveEligibilityData = "SENSITIVE_ELIGIBILITY_DATA"
	dataClassOperationalData          = "OPERATIONAL_DATA"
	dataClassSecret                   = "SECRET"
)

var cloudSafeDataClasses = map[string]struct{}{
	dataClassPublicData:      {},
	dataClassPrincipalData:   {},
	dataClassInquiryData:     {},
	dataClassOperationalData: {},
}

func resolveDataClassification(
	legacy string,
	typed llmgwv1.DataClassification,
) (string, error) {
	var typedClass string
	switch typed {
	case llmgwv1.DataClassification_DATA_CLASSIFICATION_UNSPECIFIED:
	case llmgwv1.DataClassification_PUBLIC_DATA:
		typedClass = dataClassPublicData
	case llmgwv1.DataClassification_PRINCIPAL_DATA:
		typedClass = dataClassPrincipalData
	case llmgwv1.DataClassification_INQUIRY_DATA:
		typedClass = dataClassInquiryData
	case llmgwv1.DataClassification_SENSITIVE_ELIGIBILITY_DATA:
		typedClass = dataClassSensitiveEligibilityData
	case llmgwv1.DataClassification_OPERATIONAL_DATA:
		typedClass = dataClassOperationalData
	case llmgwv1.DataClassification_SECRET:
		typedClass = dataClassSecret
	default:
		return "", invalidDataClassification()
	}

	var legacyClass string
	switch strings.TrimSpace(legacy) {
	case "":
	case "PUBLIC":
		legacyClass = dataClassPublicData
	case "INTERNAL":
		legacyClass = dataClassPrincipalData
	case "SENSITIVE":
		legacyClass = dataClassSensitiveEligibilityData
	case dataClassPublicData,
		dataClassPrincipalData,
		dataClassInquiryData,
		dataClassSensitiveEligibilityData,
		dataClassOperationalData,
		dataClassSecret:
		legacyClass = strings.TrimSpace(legacy)
	default:
		return "", invalidDataClassification()
	}

	if typedClass == "" {
		typedClass = legacyClass
	}
	if typedClass == "" {
		return "", invalidDataClassification()
	}
	if legacyClass != "" && legacyClass != typedClass {
		return "", invalidDataClassification()
	}
	return typedClass, nil
}

func validateRouteDataClassification(route routeConfig, dataClass string) error {
	if dataClass == dataClassSensitiveEligibilityData || dataClass == dataClassSecret {
		return sensitiveDataNotAllowed()
	}
	if _, safe := cloudSafeDataClasses[dataClass]; !safe {
		return invalidDataClassification()
	}
	for _, allowed := range route.allowedDataClasses {
		if allowed == dataClass {
			return nil
		}
	}
	return sensitiveDataNotAllowed()
}

func invalidDataClassification() error {
	return newStableError(
		codes.InvalidArgument, 400, ReasonRequestInvalid,
		"data classification 缺失、未知或新旧字段冲突",
	)
}

func sensitiveDataNotAllowed() error {
	return newStableError(
		codes.FailedPrecondition, 412, ReasonSensitiveDataNotAllowed,
		"请求数据分类或内容不允许进入云模型",
	)
}
