import json
from unittest import mock

from typing_extensions import override

from zerver.lib.llm import LLMError
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.topic_drift import (
    DRIFT_CHECK_EVERY_N_MESSAGES,
    DRIFT_MIN_MESSAGES,
    clear_topic_title_suggestion,
    get_topic_title_suggestion,
    parse_drift_response,
)
from zerver.models.streams import get_stream

DRIFTED_RESPONSE = json.dumps(
    {
        "drifted": True,
        "suggested_title": "Staging deploy Postgres timeout",
        "reason": "The discussion moved from lunch to a deploy failure.",
    }
)
NOT_DRIFTED_RESPONSE = json.dumps({"drifted": False, "suggested_title": "", "reason": "On topic."})


class TopicDriftTest(ZulipTestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        self.hamlet = self.example_user("hamlet")
        self.stream = get_stream("Verona", self.hamlet.realm)
        self.topic = "lunch plans"
        clear_topic_title_suggestion(self.stream.id, self.topic)

    def send_topic_messages(self, count: int) -> list[int]:
        return [
            self.send_stream_message(self.hamlet, "Verona", f"message {i}", topic_name=self.topic)
            for i in range(count)
        ]

    def test_check_is_gated_by_message_count(self) -> None:
        with (
            mock.patch("zerver.lib.topic_drift.get_gemini_api_key", return_value="key"),
            mock.patch(
                "zerver.lib.topic_drift.generate_text", return_value=NOT_DRIFTED_RESPONSE
            ) as m,
        ):
            self.send_topic_messages(DRIFT_MIN_MESSAGES - 1)
            m.assert_not_called()

            # The first check happens at the first multiple of N at or
            # past the minimum, and the cooldown suppresses the rest.
            self.send_topic_messages(2 * DRIFT_CHECK_EVERY_N_MESSAGES)
            self.assertEqual(m.call_count, 1)
            prompt = m.call_args.args[0]
            self.assertIn(f"Current title: {self.topic}", prompt)
            self.assertIn("King Hamlet: message 0", prompt)

    def test_no_check_without_api_key(self) -> None:
        with (
            mock.patch("zerver.lib.topic_drift.get_gemini_api_key", return_value=None),
            mock.patch("zerver.lib.topic_drift.generate_text") as m,
        ):
            self.send_topic_messages(DRIFT_MIN_MESSAGES + DRIFT_CHECK_EVERY_N_MESSAGES)
            m.assert_not_called()

    def test_drift_stores_suggestion_and_notifies_sender(self) -> None:
        with (
            mock.patch("zerver.lib.topic_drift.get_gemini_api_key", return_value="key"),
            mock.patch("zerver.lib.topic_drift.generate_text", return_value=DRIFTED_RESPONSE),
            mock.patch("zerver.lib.topic_drift.send_event_on_commit") as send_event,
        ):
            message_ids = self.send_topic_messages(6)

        send_event.assert_called_once()
        realm, event, user_ids = send_event.call_args.args
        self.assertEqual(realm, self.hamlet.realm)
        self.assertEqual(user_ids, [self.hamlet.id])
        self.assertEqual(event["type"], "topic_title_suggestion")
        self.assertEqual(event["suggested_title"], "Staging deploy Postgres timeout")
        self.assertEqual(event["stream_id"], self.stream.id)
        self.assertEqual(event["topic_name"], self.topic)
        self.assertEqual(event["message_id"], message_ids[-1])

        suggestion = get_topic_title_suggestion(self.stream.id, self.topic)
        assert suggestion is not None
        self.assertEqual(suggestion["suggested_title"], "Staging deploy Postgres timeout")

        self.login_user(self.hamlet)
        result = self.client_get(
            "/json/topics/title_suggestion", {"stream_id": self.stream.id, "topic": self.topic}
        )
        data = self.assert_json_success(result)
        self.assertEqual(data["suggestion"]["suggested_title"], "Staging deploy Postgres timeout")

    def test_pending_suggestion_is_resent_to_next_sender(self) -> None:
        with (
            mock.patch("zerver.lib.topic_drift.get_gemini_api_key", return_value="key"),
            mock.patch(
                "zerver.lib.topic_drift.generate_text", return_value=DRIFTED_RESPONSE
            ) as llm,
            mock.patch("zerver.lib.topic_drift.send_event_on_commit") as send_event,
        ):
            self.send_topic_messages(6)
            self.assertEqual(llm.call_count, 1)
            self.assertEqual(send_event.call_count, 1)

            # Another participant posting in the topic gets the pending
            # suggestion without a second LLM call.
            cordelia = self.example_user("cordelia")
            self.send_stream_message(cordelia, "Verona", "one more", topic_name=self.topic)
            self.assertEqual(llm.call_count, 1)
            self.assertEqual(send_event.call_count, 2)
            _realm, event, user_ids = send_event.call_args.args
            self.assertEqual(user_ids, [cordelia.id])
            self.assertEqual(event["suggested_title"], "Staging deploy Postgres timeout")

    def test_llm_failure_is_swallowed(self) -> None:
        with (
            mock.patch("zerver.lib.topic_drift.get_gemini_api_key", return_value="key"),
            mock.patch("zerver.lib.topic_drift.generate_text", side_effect=LLMError),
            self.assertLogs("zerver.lib.topic_drift", level="WARNING") as logs,
        ):
            self.send_topic_messages(6)
        self.assertEqual(
            logs.output,
            [
                f"WARNING:zerver.lib.topic_drift:Topic drift check skipped for stream {self.stream.id}: LLM unavailable"
            ],
        )
        self.assertIsNone(get_topic_title_suggestion(self.stream.id, self.topic))

    def test_force_check_endpoint(self) -> None:
        self.send_topic_messages(2)
        self.login_user(self.hamlet)
        params = {"stream_id": self.stream.id, "topic": self.topic}

        with mock.patch("zerver.lib.topic_drift.generate_text", return_value=NOT_DRIFTED_RESPONSE):
            result = self.client_post("/json/topics/title_suggestion", params)
        self.assertIsNone(self.assert_json_success(result)["suggestion"])

        with mock.patch("zerver.lib.topic_drift.generate_text", return_value=DRIFTED_RESPONSE):
            result = self.client_post("/json/topics/title_suggestion", params)
        data = self.assert_json_success(result)
        self.assertEqual(data["suggestion"]["suggested_title"], "Staging deploy Postgres timeout")

        result = self.client_post(
            "/json/topics/title_suggestion", {"stream_id": self.stream.id, "topic": "no such topic"}
        )
        self.assert_json_error(result, "No messages in this topic.")

    def test_parse_drift_response(self) -> None:
        self.assertIsNone(parse_drift_response("not json", "t"))
        self.assertIsNone(parse_drift_response(NOT_DRIFTED_RESPONSE, "t"))
        self.assertIsNone(
            parse_drift_response(
                json.dumps({"drifted": True, "suggested_title": "Lunch Plans", "reason": ""}),
                "lunch plans",
            )
        )
        self.assertEqual(
            parse_drift_response(
                json.dumps({"drifted": True, "suggested_title": '"Deploy"', "reason": "r"}), "t"
            ),
            ("Deploy", "r"),
        )
        long_title = "x" * 100
        parsed = parse_drift_response(
            json.dumps({"drifted": True, "suggested_title": long_title, "reason": ""}), "t"
        )
        assert parsed is not None
        self.assert_length(parsed[0], 60)
