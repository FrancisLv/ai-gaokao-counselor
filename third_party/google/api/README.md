# third_party/google/api

Kratos 的 HTTP 注解（`option (google.api.http) = {...}`）依赖这些 proto：

- `google/api/annotations.proto`
- `google/api/http.proto`

请在生成前把它们 vendor 到这里（二选一）：

```bash
# 方式 A：从 kratos 仓库拷贝
cp -r $(go env GOPATH)/pkg/mod/github.com/go-kratos/kratos/v2@*/third_party/google third_party/

# 方式 B：从 googleapis 拉取
curl -sSL -o third_party/google/api/annotations.proto \
  https://raw.githubusercontent.com/googleapis/googleapis/master/google/api/annotations.proto
curl -sSL -o third_party/google/api/http.proto \
  https://raw.githubusercontent.com/googleapis/googleapis/master/google/api/http.proto
```

`make api` 会用 `-I.`（含 `third_party/`）解析 import。
