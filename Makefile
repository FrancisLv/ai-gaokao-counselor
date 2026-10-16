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
PY       := python3
PY_SVC   := services/orchestrator
COMPOSE  := docker compose -f deploy/docker-compose.yml

.PHONY: help api api-go api-py tidy build build-gateway build-llm-gateway build-algo \
        test py-check up down logs run-gateway run-llm-gateway run-algo run-orchestrator

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ---- 契约生成 ----
api: api-go api-py ## 由 api/*.proto 生成 Go + Python 代码

api-go: ## 生成 Go（Kratos：gRPC + HTTP + 结构体）
	@for f in $$(find api -name '*.proto' | sort); do \
		echo "kratos proto client $$f"; \
		kratos proto client $$f; \
	done

api-py: ## 生成 Python stub（grpcio-tools）
	$(PY) -m grpc_tools.protoc -I. \
		--python_out=$(PY_SVC)/pb --grpc_python_out=$(PY_SVC)/pb \
		$$(find api -name '*.proto' | sort)
	@touch $(PY_SVC)/pb/__init__.py

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

test: ## Go 单测
	go test ./...

## ---- Python ----
py-check: ## 语法检查 Python 服务
	$(PY) -m compileall -q $(PY_SVC)

## ---- 运行 ----
run-gateway: ## 本地直跑 svc-gateway
	go run ./cmd/gateway
run-llm-gateway: ## 本地直跑 svc-llm-gateway
	go run ./cmd/llm-gateway
run-algo: ## 本地直跑 svc-algo
	go run ./cmd/algo
run-orchestrator: ## 本地直跑 svc-orchestrator
	cd $(PY_SVC) && $(PY) -m app.main

## ---- compose ----
up: ## 起全栈
	$(COMPOSE) up -d --build
down: ## 停全栈
	$(COMPOSE) down
logs: ## 跟踪日志
	$(COMPOSE) logs -f
