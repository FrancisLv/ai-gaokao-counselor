from __future__ import annotations

from typing import Protocol

import grpc
from google.protobuf.message import DecodeError
from google.rpc import error_details_pb2, status_pb2

from api.algo.v1 import algo_pb2_grpc


def grpc_error_reason(exc: grpc.RpcError) -> str | None:
    """从 gRPC status details 中提取唯一稳定业务 reason。"""
    try:
        metadata = dict(exc.trailing_metadata() or ())
    except (AttributeError, TypeError, ValueError):
        return None
    raw = metadata.get("grpc-status-details-bin")
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        return None
    try:
        status = status_pb2.Status.FromString(raw)
    except (DecodeError, TypeError, ValueError):
        return None
    for detail in status.details:
        info = error_details_pb2.ErrorInfo()
        try:
            unpacked = detail.Unpack(info)
        except (DecodeError, TypeError, ValueError):
            continue
        if unpacked and info.reason.strip():
            return info.reason.strip()
    return None


class AlgoClient(Protocol):
    """RunLifecycle 内部使用的 svc-algo client；不是公开 seam。"""

    async def RankConvert(self, request):
        ...

    async def EstimateProb(self, request):
        ...

    async def CheckConstraints(self, request):
        ...

    async def EvaluateEligibility(self, request):
        ...

    async def OptimizeWishlist(self, request):
        ...

    async def SummarizeWishlistDraft(self, request):
        ...


class GrpcAlgoClient:
    def __init__(
        self,
        *,
        channel: grpc.aio.Channel | None = None,
        address: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if channel is None and not address:
            raise ValueError("GrpcAlgoClient 需要 channel 或 address")
        self._owns_channel = channel is None
        self._channel = channel or grpc.aio.insecure_channel(address)
        self._stub = algo_pb2_grpc.AlgoStub(self._channel)
        self._timeout = timeout

    async def Close(self) -> None:
        if self._owns_channel:
            await self._channel.close()

    async def RankConvert(self, request):
        return await self._stub.RankConvert(request, timeout=self._timeout)

    async def EstimateProb(self, request):
        return await self._stub.EstimateProb(request, timeout=self._timeout)

    async def CheckConstraints(self, request):
        return await self._stub.CheckConstraints(request, timeout=self._timeout)

    async def EvaluateEligibility(self, request):
        return await self._stub.EvaluateEligibility(request, timeout=self._timeout)

    async def OptimizeWishlist(self, request):
        return await self._stub.OptimizeWishlist(request, timeout=self._timeout)

    async def SummarizeWishlistDraft(self, request):
        return await self._stub.SummarizeWishlistDraft(
            request,
            timeout=self._timeout,
        )
