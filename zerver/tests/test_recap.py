import json
from unittest import mock

from zerver.lib.llm import LLMError, LLMNotConfiguredError
from zerver.lib.recap import build_recap_prompt, fetch_recap_messages
from zerver.lib.test_classes import ZulipTestCase
from zerver.models.streams import get_stream


class MessageRecapTest(ZulipTestCase):
    def test_recap_links_cited_messages(self) -> None:
        hamlet = self.example_user("hamlet")
        cordelia = self.example_user("cordelia")
        self.subscribe(cordelia, "Denmark")
        stream_message_id = self.send_stream_message(
            cordelia, "Denmark", topic_name="castle repairs", content="The moat needs work."
        )
        dm_message_id = self.send_personal_message(cordelia, hamlet, "Are you free tomorrow?")

        llm_response = json.dumps(
            {
                "sections": [
                    {
                        "heading": "Denmark",
                        "points": [
                            {
                                "text": "Cordelia flagged moat repairs.",
                                "message_ids": [stream_message_id, 999999999],
                            }
                        ],
                    },
                    {
                        "heading": "Direct messages",
                        "points": [
                            {
                                "text": "Cordelia asked about your availability.",
                                "message_ids": [dm_message_id],
                            }
                        ],
                    },
                ]
            }
        )

        self.login_user(hamlet)
        with mock.patch("zerver.lib.recap.generate_text", return_value=llm_response) as m:
            result = self.client_get("/json/messages/recap")
        data = self.assert_json_success(result)

        # Both unread messages were sent to the model with their id tags.
        prompt = m.call_args.args[0]
        self.assertIn(f"[#{stream_message_id}]", prompt)
        self.assertIn(f"[#{dm_message_id}]", prompt)

        self.assertEqual(data["unread_count"], 2)
        self.assertEqual(data["included_count"], 2)
        self.assertFalse(data["truncated"])
        self.assert_length(data["sections"], 2)

        denmark = get_stream("Denmark", hamlet.realm)
        stream_point = data["sections"][0]["points"][0]
        # The hallucinated id 999999999 must not produce a reference.
        self.assert_length(stream_point["references"], 1)
        self.assertEqual(
            stream_point["references"][0]["url"],
            f"#narrow/channel/{denmark.id}-Denmark/topic/castle.20repairs/near/{stream_message_id}",
        )

        dm_point = data["sections"][1]["points"][0]
        self.assertEqual(
            dm_point["references"][0]["url"],
            f"#narrow/dm/{cordelia.id},{hamlet.id}/near/{dm_message_id}",
        )

    def test_recap_with_no_unreads_skips_llm(self) -> None:
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)
        with mock.patch("zerver.lib.recap.generate_text") as m:
            result = self.client_get("/json/messages/recap")
        data = self.assert_json_success(result)
        m.assert_not_called()
        self.assertEqual(data["sections"], [])
        self.assertEqual(data["unread_count"], 0)

    def test_recap_errors(self) -> None:
        hamlet = self.example_user("hamlet")
        cordelia = self.example_user("cordelia")
        self.send_personal_message(cordelia, hamlet, "ping")
        self.login_user(hamlet)

        with mock.patch("zerver.lib.recap.generate_text", side_effect=LLMNotConfiguredError):
            result = self.client_get("/json/messages/recap")
        self.assert_json_error(result, "Message recap is not configured on this server.")

        with mock.patch("zerver.lib.recap.generate_text", side_effect=LLMError):
            result = self.client_get("/json/messages/recap")
        self.assert_json_error(result, "Could not generate a recap right now. Please try again.")

        with mock.patch("zerver.lib.recap.generate_text", return_value="not json"):
            result = self.client_get("/json/messages/recap")
        data = self.assert_json_success(result)
        self.assertEqual(data["sections"], [])

    def test_prompt_groups_by_conversation(self) -> None:
        hamlet = self.example_user("hamlet")
        cordelia = self.example_user("cordelia")
        self.subscribe(cordelia, "Denmark")
        self.subscribe(cordelia, "Verona")
        first = self.send_stream_message(cordelia, "Denmark", topic_name="a", content="one")
        second = self.send_stream_message(cordelia, "Denmark", topic_name="a", content="two")
        third = self.send_stream_message(cordelia, "Verona", topic_name="b", content="three")

        prompt = build_recap_prompt(fetch_recap_messages(hamlet, [first, second, third]))
        self.assertEqual(
            prompt,
            f"## #Denmark > a\n[#{first}] Cordelia, Lear's daughter: one\n"
            f"[#{second}] Cordelia, Lear's daughter: two\n\n"
            f"## #Verona > b\n[#{third}] Cordelia, Lear's daughter: three",
        )
