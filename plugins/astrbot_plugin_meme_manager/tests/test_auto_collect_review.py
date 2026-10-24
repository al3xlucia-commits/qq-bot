import asyncio
import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from astrbot_plugin_meme_manager.backend import auto_collect
from test_auto_collect import DummyPlugin, png_bytes


class AutoCollectReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.packs = self.root / "packs"
        for pack_id in ("pack-a", "pack-b"):
            (self.packs / pack_id / "memes" / "happy").mkdir(parents=True)
        for name, value in {
            "PACKS_DIR": self.packs,
            "AUTO_COLLECT_INBOX_IMAGES_DIR": self.root / "inbox" / "images",
            "AUTO_COLLECT_INBOX_METADATA_PATH": self.root / "inbox" / "metadata.json",
            "AUTO_COLLECT_STATE_PATH": self.root / "state.json",
        }.items():
            self.stack.enter_context(patch.object(auto_collect, name, value))
        self.stack.enter_context(
            patch.object(
                auto_collect,
                "get_pack_paths",
                side_effect=lambda pack_id: {
                    "memes_dir": self.packs / pack_id / "memes"
                },
            )
        )
        self.stack.enter_context(
            patch.object(
                auto_collect,
                "load_pack_category_mapping",
                return_value={"happy": "happy", "sad": "sad"},
            )
        )
        self.plugin = DummyPlugin()
        self.plugin.semantic_enabled = False
        self.manager = auto_collect.AutoCollectManager(self.plugin, {"enabled": True})

    async def add_candidate(self, color=(255, 0, 0), confidence=0.9, pack_id="pack-a"):
        content = png_bytes(color)
        digest = hashlib.sha256(content).hexdigest()
        source = self.root / f"{digest}.png"
        source.write_bytes(content)
        job = auto_collect.AutoCollectJob(
            source, pack_id, {"happy": "happy"}, "group", "1"
        )
        decision = {
            "is_meme": True,
            "meme_confidence": 0.95,
            "category": "happy",
            "category_confidence": confidence,
        }
        self.manager._classify = AsyncMock(return_value=decision)
        await self.manager._process_job(job)
        return f"{pack_id}:{digest}", job

    async def test_override_category_and_failed_semantic_write_retains_pending(self):
        record_id, _ = await self.add_candidate()
        with patch.object(
            auto_collect,
            "save_collected_image_semantic",
            side_effect=OSError("disk unavailable"),
        ):
            result = await self.manager.accept_pending(
                "pack-a", [{"id": record_id, "category": "sad"}]
            )
        self.assertEqual(result["failed"], 1)
        self.assertTrue(self.manager.pending_image_path("pack-a", record_id).exists())
        self.assertFalse(list((self.packs / "pack-a" / "memes").rglob("*.png")))
        result = await self.manager.accept_pending(
            "pack-a", [{"id": record_id, "category": "sad"}]
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(
            len(list((self.packs / "pack-a" / "memes" / "sad").glob("*.png"))), 1
        )

    async def test_regular_discard_can_be_collected_again(self):
        record_id, job = await self.add_candidate()
        await self.manager.discard_pending("pack-a", [record_id], False)
        await self.manager._process_job(job)
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 1)

    async def test_capacity_and_existing_pending_skip_recognition(self):
        self.manager.pending_limit = 1
        record_id, job = await self.add_candidate()
        self.manager._classify.reset_mock()
        await self.manager._process_job(job)
        self.manager._classify.assert_not_awaited()
        await self.add_candidate((0, 255, 0))
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 1)
        self.assertEqual(self.manager.counters["inbox_full"], 1)

    async def test_cross_pack_and_traversal_rejected_before_mutation(self):
        record_id, _ = await self.add_candidate()
        with self.assertRaises(ValueError):
            await self.manager.accept_pending("pack-b", [{"id": record_id}])
        with self.assertRaises(ValueError):
            await self.manager.discard_pending("pack-b", [record_id], True)
        metadata = json.loads(auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH.read_text())
        metadata["items"][record_id]["filename"] = "../../outside.png"
        auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH.write_text(json.dumps(metadata))
        with self.assertRaises(ValueError):
            self.manager.pending_image_path("pack-a", record_id)

    async def test_auto_accept_does_not_start_embedding(self):
        self.manager.manual_review = False
        await self.add_candidate()
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 0)
        self.assertEqual(len(list((self.packs / "pack-a" / "memes").rglob("*.png"))), 1)

    async def test_acceptance_batches_automatic_vectors(self):
        self.plugin.semantic_enabled = True
        self.plugin.semantic_task_manager.rebuild_index = AsyncMock()
        first, _ = await self.add_candidate()
        second, _ = await self.add_candidate((0, 255, 0))
        await self.manager.accept_pending("pack-a", [{"id": first}])
        task = self.manager._embedding_tasks["pack-a"]
        await self.manager.accept_pending("pack-a", [{"id": second}])
        self.assertIs(self.manager._embedding_tasks["pack-a"], task)
        await task
        self.plugin.semantic_task_manager.rebuild_index.assert_awaited_once_with(
            "pack-a", force=False
        )
        self.assertFalse(self.manager._embedding_tasks)

    async def test_vector_setting_can_disable_automatic_generation(self):
        self.plugin.semantic_enabled = True
        self.manager.auto_embedding = False
        record_id, _ = await self.add_candidate()
        await self.manager.accept_pending("pack-a", [{"id": record_id}])
        self.assertFalse(self.manager._embedding_tasks)

    async def test_auto_accept_generates_vectors_and_close_cancels_pending_batch(self):
        self.plugin.semantic_enabled = True
        self.plugin.semantic_task_manager.rebuild_index = AsyncMock()
        self.manager.manual_review = False
        await self.add_candidate()
        task = self.manager._embedding_tasks["pack-a"]
        await task
        self.plugin.semantic_task_manager.rebuild_index.assert_awaited_once()
        await self.add_candidate((0, 255, 0))
        task = self.manager._embedding_tasks["pack-a"]
        await asyncio.sleep(0)
        await self.manager.close()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.manager._embedding_tasks)

    async def test_embedding_trigger_waits_for_busy_pack_without_retrying_model_errors(
        self,
    ):
        self.plugin.semantic_enabled = True
        checked = asyncio.Event()
        busy = True

        def check_pack(_pack_id, _operation):
            if busy:
                checked.set()
                raise RuntimeError("Pack is busy")

        tasks = self.plugin.semantic_task_manager
        tasks.assert_pack_mutation_allowed = Mock(side_effect=check_pack)
        tasks.rebuild_index = AsyncMock(side_effect=RuntimeError("Provider failed"))
        record_id, _ = await self.add_candidate()
        await self.manager.accept_pending("pack-a", [{"id": record_id}])
        task = self.manager._embedding_tasks["pack-a"]
        await asyncio.wait_for(checked.wait(), 3)
        self.assertFalse(task.done())
        tasks.rebuild_index.assert_not_awaited()
        busy = False
        with patch.object(auto_collect.logger, "warning") as warning:
            await asyncio.wait_for(task, 3)
        tasks.rebuild_index.assert_awaited_once_with("pack-a", force=False)
        warning.assert_called_once()
        self.assertFalse(self.manager._embedding_tasks)

    async def test_busy_auto_accept_retries_only_new_ids_and_does_not_block_other_packs(
        self,
    ):
        original, _ = await self.add_candidate()
        self.manager.manual_review = False
        busy = True

        def check_pack(pack_id, _operation):
            if pack_id == "pack-a" and busy:
                raise RuntimeError("Pack is busy")

        tasks = self.plugin.semantic_task_manager
        tasks.assert_pack_mutation_allowed = Mock(side_effect=check_pack)
        tasks.begin_external_pack_operation = Mock(side_effect=check_pack)
        first, _ = await self.add_candidate((0, 255, 0))
        retry = self.manager._auto_accept_tasks["pack-a"]
        await asyncio.sleep(0)
        second, _ = await self.add_candidate((0, 0, 255))
        self.assertIs(self.manager._auto_accept_tasks["pack-a"], retry)
        self.assertEqual(self.manager._pending_auto_accept["pack-a"], {first, second})
        await self.add_candidate((255, 255, 0), pack_id="pack-b")
        self.assertEqual((await self.manager.pending_status("pack-b"))["count"], 0)
        self.assertFalse(retry.done())
        busy = False
        await asyncio.wait_for(retry, 3)
        remaining = (await self.manager.pending_status("pack-a"))["items"]
        self.assertEqual([item["id"] for item in remaining], [original])
        self.assertFalse(self.manager._auto_accept_tasks)
        self.assertFalse(self.manager._pending_auto_accept)

    async def test_embedding_trigger_survives_pack_becoming_busy_while_waiting_for_lock(
        self,
    ):
        self.plugin.semantic_enabled = True
        checked = asyncio.Event()
        busy = False
        attempts = 0

        def check_pack(_pack_id, _operation):
            if busy:
                checked.set()
                raise RuntimeError("Another semantic task is active")

        async def rebuild(_pack_id, *, force):
            nonlocal busy, attempts
            self.assertFalse(force)
            attempts += 1
            if attempts == 1:
                busy = True
                raise RuntimeError("Pack became busy while acquiring its lock")

        tasks = self.plugin.semantic_task_manager
        tasks.assert_pack_mutation_allowed = Mock(side_effect=check_pack)
        tasks.rebuild_index = AsyncMock(side_effect=rebuild)
        record_id, _ = await self.add_candidate()
        await self.manager.accept_pending("pack-a", [{"id": record_id}])
        task = self.manager._embedding_tasks["pack-a"]
        await asyncio.wait_for(checked.wait(), 3)
        self.assertFalse(task.done())
        self.assertEqual(attempts, 1)
        busy = False
        await asyncio.wait_for(task, 3)
        self.assertEqual(attempts, 2)
        self.assertFalse(self.manager._embedding_tasks)

    async def test_new_images_wait_for_own_vector_task_then_generate_their_vectors(
        self,
    ):
        self.manager.manual_review = False
        self.plugin.semantic_enabled = True
        started = asyncio.Event()
        release = asyncio.Event()
        busy = False

        def check_pack(_pack_id, _operation):
            if busy:
                raise RuntimeError("Index generation is busy")

        async def rebuild(_pack_id, *, force):
            nonlocal busy
            self.assertFalse(force)
            busy = True
            started.set()
            await release.wait()
            busy = False

        tasks = self.plugin.semantic_task_manager
        tasks.assert_pack_mutation_allowed = Mock(side_effect=check_pack)
        tasks.begin_external_pack_operation = Mock(side_effect=check_pack)
        tasks.rebuild_index = AsyncMock(side_effect=rebuild)
        await self.add_candidate()
        first = self.manager._embedding_tasks["pack-a"]
        await asyncio.wait_for(started.wait(), 3)
        await self.add_candidate((0, 255, 0))
        retry = self.manager._auto_accept_tasks["pack-a"]
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 1)
        release.set()
        await asyncio.wait_for(first, 3)
        await asyncio.wait_for(retry, 3)
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 0)
        second = self.manager._embedding_tasks["pack-a"]
        await asyncio.wait_for(second, 3)
        self.assertEqual(tasks.rebuild_index.await_count, 2)

    async def test_close_cancels_busy_auto_accept_and_vector_waits(self):
        self.manager.manual_review = False
        self.plugin.semantic_enabled = True
        tasks = self.plugin.semantic_task_manager
        tasks.rebuild_index = AsyncMock()
        await self.add_candidate()
        vector = self.manager._embedding_tasks["pack-a"]
        tasks.assert_pack_mutation_allowed = Mock(side_effect=RuntimeError("Busy"))
        tasks.begin_external_pack_operation = Mock(side_effect=RuntimeError("Busy"))
        await self.add_candidate((0, 255, 0))
        retry = self.manager._auto_accept_tasks["pack-a"]
        await asyncio.sleep(0)
        await self.manager.close()
        self.assertTrue(vector.cancelled())
        self.assertTrue(retry.cancelled())
        self.assertFalse(self.manager._embedding_tasks)
        self.assertFalse(self.manager._auto_accept_tasks)
        self.assertFalse(self.manager._pending_auto_accept)
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 1)
        tasks.rebuild_index.assert_not_awaited()

    async def test_close_waits_for_started_acceptance_without_spawning_vector_task(
        self,
    ):
        self.manager.manual_review = False
        self.plugin.semantic_enabled = True
        tasks = self.plugin.semantic_task_manager
        tasks.rebuild_index = AsyncMock()
        tasks.begin_external_pack_operation = Mock(side_effect=RuntimeError("Busy"))
        await self.add_candidate()
        started = asyncio.Event()
        release = asyncio.Event()

        async def reload_emotions():
            started.set()
            await release.wait()

        self.plugin.reload_emotions = reload_emotions
        tasks.begin_external_pack_operation.side_effect = None
        await asyncio.wait_for(started.wait(), 3)
        closing = asyncio.create_task(self.manager.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await asyncio.wait_for(closing, 3)
        self.assertFalse(self.manager._embedding_tasks)
        self.assertFalse(self.manager._auto_accept_tasks)
        tasks.rebuild_index.assert_not_awaited()
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 0)

    async def test_deferred_acceptance_does_not_retry_nonbusy_runtime_errors(self):
        self.manager.manual_review = False
        tasks = self.plugin.semantic_task_manager
        tasks.begin_external_pack_operation = Mock(side_effect=RuntimeError("Busy"))
        await self.add_candidate()
        retry = self.manager._auto_accept_tasks["pack-a"]
        tasks.begin_external_pack_operation.side_effect = RuntimeError("Invalid state")
        with patch.object(auto_collect.logger, "warning") as warning:
            await asyncio.wait_for(retry, 3)
        warning.assert_called_once()
        self.assertEqual(tasks.begin_external_pack_operation.call_count, 2)
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 1)
        self.assertFalse(self.manager._auto_accept_tasks)

    async def test_legacy_bulk_import_cannot_bypass_manual_review(self):
        await self.add_candidate()
        with self.assertRaises(RuntimeError):
            await self.manager.import_pending("pack-a")
        self.assertEqual((await self.manager.pending_status("pack-a"))["count"], 1)

    async def test_uncertain_category_kept_for_human_but_auto_accept_uses_review(self):
        record_id, _ = await self.add_candidate(confidence=0.2)
        item = (await self.manager.pending_status("pack-a"))["items"][0]
        self.assertEqual(item["suggested_category"], "happy")
        self.assertEqual(item["category_confidence"], 0.2)
        await self.manager.discard_pending("pack-a", [record_id])
        self.manager.manual_review = False
        await self.add_candidate(confidence=0.2)
        review_dir = self.packs / "pack-a" / "memes" / auto_collect.REVIEW_CATEGORY
        self.assertEqual(len(list(review_dir.glob("*.png"))), 1)

    async def test_selection_limit_matches_maximum_pending_configuration(self):
        record_id, _ = await self.add_candidate()
        result = await self.manager.accept_pending("pack-a", [{"id": record_id}] * 2000)
        self.assertEqual(result["imported"], 1)
        record_id, _ = await self.add_candidate((0, 255, 0))
        result = await self.manager.discard_pending("pack-a", [record_id] * 2000)
        self.assertEqual(result["discarded"], 1)
        with self.assertRaises(ValueError):
            await self.manager.discard_pending("pack-a", [record_id] * 2001)

    async def test_batch_validation_reads_inbox_metadata_once(self):
        for action in ("accept", "discard"):
            with self.subTest(action=action):
                ids = []
                for index in range(3):
                    color = (index * 50, 10 if action == "accept" else 20, 255)
                    record_id, _ = await self.add_candidate(color)
                    ids.append(record_id)
                with patch.object(
                    self.manager, "_load_json", wraps=self.manager._load_json
                ) as loader:
                    if action == "accept":
                        result = await self.manager.accept_pending(
                            "pack-a", [{"id": record_id} for record_id in ids]
                        )
                        self.assertEqual(result["imported"], 3)
                    else:
                        result = await self.manager.discard_pending("pack-a", ids)
                        self.assertEqual(result["discarded"], 3)
                loader.assert_called_once_with(
                    auto_collect.AUTO_COLLECT_INBOX_METADATA_PATH, {"items": {}}
                )

    async def test_nonfinite_model_confidences_never_become_high_confidence(self):
        self.plugin.context = SimpleNamespace(llm_generate=AsyncMock())
        with patch.object(auto_collect, "AUTO_COLLECT_TEMP_DIR", self.root):
            for value in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(value=value):
                    self.plugin.context.llm_generate.return_value = SimpleNamespace(
                        completion_text=json.dumps(
                            {
                                "is_meme": True,
                                "category": "happy",
                                "meme_confidence": value,
                                "category_confidence": value,
                            }
                        )
                    )
                    result = await self.manager._classify(
                        png_bytes(), ".png", {"happy": "happy"}
                    )
                    self.assertEqual(result["meme_confidence"], 0.0)
                    self.assertEqual(result["category_confidence"], 0.0)

    async def test_plain_text_messages_do_not_increment_collection_skip_counters(self):
        self.manager._ready = True
        self.manager.scope = {"unmatched"}
        event = SimpleNamespace(message_obj=SimpleNamespace(message=[]))
        self.assertFalse(await self.manager.submit(event))
        self.assertEqual(self.manager.counters, {})
