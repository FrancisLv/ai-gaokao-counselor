# svc-orchestrator 镜像（Python · LangGraph）
# 可通过构建参数指定镜像加速地址或固定 digest，默认仍使用官方镜像。
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE}
WORKDIR /app
ENV PYTHONUNBUFFERED=1

# 先安装固定生成器，再从 proto 生成；不依赖宿主机生成目录。
RUN pip install --no-cache-dir grpcio-tools==1.84.0 "googleapis-common-protos>=1.72"
# 默认用于 CPU 环境，避免 PyPI 默认 torch 引入无用的 CUDA 运行库。
# 有 GPU 部署需求时可显式改用匹配驱动的官方索引，并重新验证。
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir "torch>=2.2" --index-url "${TORCH_INDEX_URL}"
COPY api/ /proto/api/
COPY services/orchestrator/ ./
RUN python -m grpc_tools.protoc -I/proto \
    -I$(python -c 'from pathlib import Path; from google.api import annotations_pb2; print(Path(annotations_pb2.__file__).parents[2])') \
    --python_out=pb --grpc_python_out=pb /proto/api/*/v1/*.proto
RUN pip install --no-cache-dir .
ENV PYTHONPATH=/app:/app/pb
CMD ["python", "-m", "app.main"]
