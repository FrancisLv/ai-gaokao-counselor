import unittest

from api.llmgw.v1 import llmgw_pb2


class LLMGatewayPythonCompatibilityTest(unittest.TestCase):
    def test_legacy_chat_delta_remains_wire_readable(self):
        delta = llmgw_pb2.ChatDelta.FromString(
            llmgw_pb2.ChatDelta(content="旧客户端增量", done=True).SerializeToString()
        )

        self.assertEqual(delta.content, "旧客户端增量")
        self.assertTrue(delta.done)

    def test_additive_typed_event_is_readable(self):
        delta = llmgw_pb2.ChatDelta(
            event=llmgw_pb2.ChatStreamEvent(
                sequence=1,
                type=llmgw_pb2.CHAT_STREAM_EVENT_DONE,
                logical_call_id="logical-python-contract",
                attempt_id="attempt-python-contract",
                ledger_entry_id="ledger-python-contract",
                content="最终内容",
            )
        )

        decoded = llmgw_pb2.ChatDelta.FromString(delta.SerializeToString())
        self.assertTrue(decoded.HasField("event"))
        self.assertEqual(decoded.content, "")
        self.assertFalse(decoded.done)
        self.assertEqual(
            decoded.event.type,
            llmgw_pb2.CHAT_STREAM_EVENT_DONE,
        )
        self.assertEqual(decoded.event.logical_call_id, "logical-python-contract")
        self.assertEqual(decoded.event.content, "最终内容")

    def test_existing_field_numbers_are_not_reused(self):
        fields = {field.name: field.number for field in llmgw_pb2.ChatDelta.DESCRIPTOR.fields}
        self.assertEqual(
            fields,
            {
                "content": 1,
                "done": 2,
                "event": 3,
            },
        )


if __name__ == "__main__":
    unittest.main()
