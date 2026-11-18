# 通用 Go 服务镜像（三个角色共用）
#   docker build --build-arg SERVICE=gateway -f deploy/go.Dockerfile .
# ⚠️ 构建镜像版本必须 >= go.mod 声明的 go 1.25.0，否则 go build 报 "go.mod requires go >= 1.25.0"。
# 可通过构建参数指定镜像加速地址或固定 digest，默认仍使用官方镜像。
ARG GO_IMAGE=golang:1.25
ARG GO_RUNTIME_IMAGE=gcr.io/distroless/static-debian12
FROM ${GO_IMAGE} AS build
WORKDIR /src
# 仅作用于镜像构建，保留 Go 校验和验证，不修改宿主 go env。
ARG GOPROXY=https://proxy.golang.org,direct
COPY go.mod go.sum* ./
RUN go mod download
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler
RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.11 && \
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.2 && \
    go install github.com/go-kratos/kratos/cmd/protoc-gen-go-http/v2@v2.0.0-20260404020628-f149714c1d54
COPY . .
# 镜像内从 proto 生成，不依赖宿主机被 gitignore 的生成物。
RUN protoc -I. -I$(go list -m -f '{{.Dir}}' github.com/go-kratos/kratos/v2)/third_party \
    --go_out=paths=source_relative:. --go-grpc_out=paths=source_relative:. \
    --go-http_out=paths=source_relative:. api/*/v1/*.proto && \
    protoc -I. --go_out=paths=source_relative:. conf/conf.proto
ARG SERVICE
RUN CGO_ENABLED=0 go build -o /out/app ./cmd/${SERVICE}

FROM ${GO_RUNTIME_IMAGE}
COPY --from=build /out/app /app
ENTRYPOINT ["/app"]
