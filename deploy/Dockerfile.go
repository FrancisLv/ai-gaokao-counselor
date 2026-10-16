# 通用 Go 服务镜像（三个角色共用）
#   docker build --build-arg SERVICE=gateway -f deploy/Dockerfile.go .
FROM golang:1.22 AS build
WORKDIR /src
ARG SERVICE
COPY go.mod go.sum* ./
RUN go mod download || true
COPY . .
RUN CGO_ENABLED=0 go build -o /out/app ./cmd/${SERVICE}

FROM gcr.io/distroless/static-debian12
COPY --from=build /out/app /app
ENTRYPOINT ["/app"]
