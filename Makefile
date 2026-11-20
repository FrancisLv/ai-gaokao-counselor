# ai-gaokao-counselor —— 顶层构建入口
#
# 用法:
#   make api     由 api/*.proto 生成 Go + Python 代码（单一事实来源）
#   make tidy    拉取 Go 依赖
#   make build   编译三个 Go 服务
#   make test    跑 Go 单测
#   make up/down 起停全栈（docker compose）
#   make run-*   本地直跑某个服务（L0 开发）

MODULE   := github.com/choucisan/ai-gaokao-counselor
PY       ?= python3
PROTO_INCLUDE ?= $(shell go list -m -f '{{.Dir}}' github.com/go-kratos/kratos/v2)/third_party
export PATH := $(shell go env GOPATH)/bin:$(PATH)
export PYTHONPATH := $(CURDIR)/services/orchestrator:$(CURDIR)/services/orchestrator/pb
PY_SVC   := services/orchestrator
COMPOSE  := docker compose -f deploy/docker-compose.yml
# 镜像前缀仅用于本次构建；例如 IMAGE_MIRROR=m.daocloud.io/，不修改全局配置。
IMAGE_MIRROR ?=
GO_IMAGE ?= $(IMAGE_MIRROR)docker.io/library/golang:1.25
GO_RUNTIME_IMAGE ?= $(IMAGE_MIRROR)gcr.io/distroless/static-debian12
PYTHON_IMAGE ?= $(IMAGE_MIRROR)docker.io/library/python:3.11-slim
DOCKER_SERVICES ?= algo gateway llm-gateway orchestrator
BUILD_GOPROXY ?= https://proxy.golang.org,direct
TORCH_INDEX_URL ?= https://download.pytorch.org/whl/cpu
# sqlc 只在修改 SQL/schema 后使用；固定版本工具装在项目 bin 下，不进入服务依赖。
SQLC_VERSION := v1.30.0
SQLC := $(CURDIR)/bin/tools/sqlc-$(SQLC_VERSION)/sqlc

.PHONY: help api api-go api-py conf tidy build build-gateway build-llm-gateway build-algo \
        test py-check m4a-acceptance-seed m4a-real-policy-import up down logs run-gateway run-llm-gateway run-algo run-orchestrator

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ---- 契约生成 ----
api: api-go api-py conf ## 由 api/*.proto + conf/conf.proto 生成 Go + Python 代码

conf: ## 生成 conf/conf.pb.go（Kratos 配置 proto，无 service）
	protoc -I. --go_out=. --go_opt=module=$(MODULE) conf/conf.proto

api-go: ## 生成 Go（Kratos：gRPC + HTTP + 结构体）
	@set -e; for f in $$(find api -name '*.proto' | sort); do kratos proto client $$f; done

api-py: ## 生成 Python stub（grpcio-tools）
	$(PY) -m grpc_tools.protoc -I. -I$(PROTO_INCLUDE) \
		--python_out=$(PY_SVC)/pb --grpc_python_out=$(PY_SVC)/pb \
		$$(find api -name '*.proto' | sort)
	@touch $(PY_SVC)/pb/__init__.py

## ---- SQL 查询生成 ----
.PHONY: sqlc-install sqlc-generate sqlc-check

sqlc-install: $(SQLC) ## 安装固定版本 sqlc（可通过环境变量 GOPROXY 指定下载源）

$(SQLC):
	GOBIN="$(dir $(SQLC))" go install github.com/sqlc-dev/sqlc/cmd/sqlc@$(SQLC_VERSION)

sqlc-generate: $(SQLC) ## 由 queries/*.sql 与现有 schema 生成 dbgen；不连接数据库
	$(SQLC) generate -f sqlc.yaml

sqlc-check: $(SQLC) ## 校验 SQL 与已保存的生成代码一致，不改生成文件
	$(SQLC) diff -f sqlc.yaml

## ---- Go ----
tidy: ## 拉取/整理 Go 依赖
	go mod tidy

build: build-gateway build-llm-gateway build-algo ## 编译三个 Go 服务

build-gateway: ## 编译 svc-gateway
	go build -o bin/gateway ./cmd/gateway
build-llm-gateway: ## 编译 svc-llm-gateway
	go build -o bin/llm-gateway ./cmd/llm-gateway
build-algo: ## 编译 svc-algo
	go build -o bin/algo ./cmd/algo

.PHONY: test-integration test-integration-llmgw test-race coverage bench test-fuzz py-test m5b-release-gate tcp-smoke perf check compose-check docker-build

test: ## Go 默认单测（不依赖数据库）
	go test ./... -count=1

test-integration: ## 真实库测试；必须显式设置只读 ALGO_TEST_DSN
	@test -n "$$ALGO_TEST_DSN" || (echo '需设置 ALGO_TEST_DSN'; exit 1)
	go test -tags integration ./internal/algo/... ./cmd/algo -count=1

test-integration-llmgw: ## LLM 治理真实库测试；必须显式设置隔离可写 LLMGW_TEST_DSN
	@test -n "$$LLMGW_TEST_DSN" || (echo '需设置 LLMGW_TEST_DSN'; exit 1)
	go test -tags integration ./internal/llmgw/... ./cmd/llm-gateway -count=1

test-race: ## 真实库并发/竞态三轮 shuffle 回归
	@test -n "$$ALGO_TEST_DSN" || (echo '需设置 ALGO_TEST_DSN'; exit 1)
	go test -race -tags integration ./internal/algo/... -count=3 -shuffle=on -timeout=180s

coverage: ## biz/service 默认覆盖率＋data 真实库覆盖率
	go test ./internal/algo/biz ./internal/algo/service -cover -count=1
	@test -n "$$ALGO_TEST_DSN" || (echo 'data 覆盖率需 ALGO_TEST_DSN'; exit 1)
	go test -tags integration ./internal/algo/data -cover -count=1

bench: ## 固定夹具＋真实库性能（不启用 race）
	@test -n "$$ALGO_TEST_DSN" || (echo '需设置 ALGO_TEST_DSN'; exit 1)
	go test -tags integration ./internal/algo/... -run '^$$' -bench . -benchmem -count=3

test-fuzz: ## 两个解析器各 fuzz 10 秒
	go test ./internal/algo/biz -run '^$$' -fuzz FuzzSubjectReq -fuzztime=10s
	go test ./internal/algo/data -run '^$$' -fuzz FuzzParseRange -fuzztime=10s

tcp-smoke: api-py ## 新建独立 algo 进程，Go/Python 四 RPC；需 PY 与只读 ALGO_TEST_DSN
	@test -n "$$ALGO_TEST_DSN" || (echo '需设置 ALGO_TEST_DSN'; exit 1)
	ALGO_PYTHON=$(PY) go test -tags integration ./cmd/algo -run TestIntegration_TCP -v -count=1 -timeout=240s

perf: ## TCP 单并发/8并发各100请求，输出 p50/p95/max/错误率/响应大小
	ALGO_LOAD=1 $(MAKE) tcp-smoke PY=$(PY)

check: sqlc-check ## SQL 生成一致性、全仓构建、vet 及构建标签边界
	go build ./...
	go vet ./...
	go vet -tags integration ./...
	go vet -tags wireinject ./cmd/algo
	go test -tags wireinject ./cmd/algo -count=1

## ---- Python ----
py-check: api-py ## 语法与模块导入检查
	$(PY) -m compileall -q $(PY_SVC) scripts
	$(PY) -c 'import app.main, app.server, graph.build, rag.etl, rag.embedder; from api.algo.v1 import algo_pb2_grpc'

py-test: api-py ## Python 离线逻辑与导入测试，不下载模型、不入库
	HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 $(PY) -m unittest discover -s $(PY_SVC)/tests -p 'test_*.py' -v

m5b-release-gate: ## 执行不可变 EvalRun 的 M5b 聚合发布门禁；需 DSN / EVAL_RUN_ID / SECURITY_EVIDENCE
	@test -n "$(DSN)" || (echo '需设置 DSN'; exit 1)
	@test -n "$(EVAL_RUN_ID)" || (echo '需设置 EVAL_RUN_ID'; exit 1)
	@test -n "$(SECURITY_EVIDENCE)" || (echo '需设置 SECURITY_EVIDENCE'; exit 1)
	$(PY) scripts/run_m5b_release_gate.py \
		--dsn "$(DSN)" \
		--dataset-dir "$(if $(DATASET_DIR),$(DATASET_DIR),data/golden/evaluation-hunan-2025-v0.2.0-draft)" \
		--eval-run-id "$(EVAL_RUN_ID)" \
		--security-evidence "$(SECURITY_EVIDENCE)"

m4a-acceptance-seed: ## 为 M4a 前端验收 seed policy release；需 DSN
	@test -n "$(DSN)" || (echo '需设置 DSN'; exit 1)
	$(PY) scripts/seed_m4a_frontend_acceptance.py --dsn "$(DSN)" $(if $(RESET_SCHEMA),--reset-schema,)

m4a-real-policy-import: ## 将主库真实 2025 章程复制到隔离测试库并发布 release
	@test -n "$(SOURCE_DSN)" || (echo '需设置 SOURCE_DSN'; exit 1)
	@test -n "$(TARGET_DSN)" || (echo '需设置 TARGET_DSN'; exit 1)
	$(PY) scripts/load_real_policy_release.py \
		--source-dsn "$(SOURCE_DSN)" \
		--target-dsn "$(TARGET_DSN)"

## ---- 运行 ----
run-gateway: ## 本地直跑 svc-gateway
	go run ./cmd/gateway
run-llm-gateway: ## 本地直跑 svc-llm-gateway
	go run ./cmd/llm-gateway
run-algo: ## 本地直跑 svc-algo
	go run ./cmd/algo
run-orchestrator: ## 本地直跑 svc-orchestrator
	$(PY) -m app.main

## ---- compose ----
compose-check: ## 仅校验 Compose 配置，不构建、不启动
	$(COMPOSE) config --quiet

docker-build: compose-check ## 仅构建业务镜像；可选 IMAGE_MIRROR=m.daocloud.io/，不启动服务
	GO_IMAGE="$(GO_IMAGE)" GO_RUNTIME_IMAGE="$(GO_RUNTIME_IMAGE)" PYTHON_IMAGE="$(PYTHON_IMAGE)" \
	BUILD_GOPROXY="$(BUILD_GOPROXY)" TORCH_INDEX_URL="$(TORCH_INDEX_URL)" \
	$(COMPOSE) --progress plain build $(DOCKER_SERVICES)

up: ## 起全栈
	$(COMPOSE) up -d --build
down: ## 停全栈
	$(COMPOSE) down
logs: ## 跟踪日志
	$(COMPOSE) logs -f
