import unittest
import uuid
from unittest.mock import Mock

import app


class LegacySearchTests(unittest.TestCase):
    def test_legacy_session_can_be_committed_without_losing_criteria(self):
        for previous in (
            {"state": {"criteria": {"operation": "compra", "districts": ["Lince"]}}},
            {"state": {"active_search_id": "legacy", "searches": {
                "legacy": {"criteria": {"operation": "compra", "districts": ["Lince"]}}
            }}},
        ):
            with self.subTest(previous=previous):
                state, criteria = app.search_state_for_turn(previous, None, "hola")
                self.assertEqual(criteria["districts"], ["Lince"])
                uuid.UUID(state["active_search_id"])
                memory = Mock()
                app.commit_consistent_turn(memory, "claim", "51979300318", "test-id",
                    "hola", "respuesta", None, criteria, "qualification", "resumen",
                    previous, state)
                payload = memory.commit_work.call_args.args[0]
                uuid.UUID(payload["p_search"]["search_id"])
                self.assertIsNone(payload["p_previous_search_id"])
                self.assertEqual(payload["p_search"]["state"]["criteria"], criteria)

    def test_existing_uuid_is_preserved(self):
        search_id = str(uuid.uuid4())
        previous = {"state": {"active_search_id": search_id,
            "searches": {search_id: {"criteria": {"operation": "compra"}}}}}
        state, _ = app.search_state_for_turn(previous, None, "hola")
        self.assertEqual(state["active_search_id"], search_id)
        self.assertEqual(app.active_search_id(previous), search_id)
