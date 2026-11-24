"""svc-algo gRPC 错误详情的稳定 reason 解析。"""
from __future__ import annotations

import unittest

import grpc
from google.protobuf import any_pb2
from google.rpc import error_details_pb2, status_pb2


class AlgoErrorMappingTests(unittest.TestCase):
    def test_reads_error_info_reason_from_status_details(self):
        from app.run_lifecycle.algo import grpc_error_reason

        info = error_details_pb2.ErrorInfo(
            reason="SCORE_BELOW_CONTROL_LINE",
            domain="algo",
            metadata={"http_code": "400"},
        )
        detail = any_pb2.Any()
        detail.Pack(info)
        status = status_pb2.Status(
            code=grpc.StatusCode.FAILED_PRECONDITION.value[0],
            message="分数低于报考年本科线",
            details=[detail],
        )

        class FakeRpcError(grpc.RpcError):
            @staticmethod
            def trailing_metadata():
                return ((
                    "grpc-status-details-bin",
                    status.SerializeToString(),
                ),)

        self.assertEqual(
            grpc_error_reason(FakeRpcError()),
            "SCORE_BELOW_CONTROL_LINE",
        )

    def test_missing_or_malformed_details_have_no_business_reason(self):
        from app.run_lifecycle.algo import grpc_error_reason

        class FakeRpcError(grpc.RpcError):
            def __init__(self, metadata):
                self.metadata = metadata

            def trailing_metadata(self):
                return self.metadata

        self.assertIsNone(grpc_error_reason(FakeRpcError(())))
        self.assertIsNone(
            grpc_error_reason(
                FakeRpcError(((
                    "grpc-status-details-bin",
                    b"not-a-status",
                ),))
            )
        )


if __name__ == "__main__":
    unittest.main()
