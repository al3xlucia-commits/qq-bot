import json
from unittest.mock import Mock

import pytest
from astrbot_plugin_meme_manager.backend.semantic import storage
from astrbot_plugin_meme_manager.backend.semantic.models import (
    PROMPT_VERSION,
    semantic_caption_is_complete,
)
from PIL import Image


@pytest.mark.parametrize(
    ("version", "category", "complete"),
    [
        (PROMPT_VERSION, "happy", True),
        ("old", "happy", False),
        (PROMPT_VERSION, "sad", False),
    ],
)
def test_collection_analysis_reuse_requires_matching_semantic_contract(
    tmp_path, version, category, complete
):
    pack = tmp_path / "pack"
    source = pack / "memes" / "happy" / "new.png"
    source.parent.mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(source)
    (pack / "memes_data.json").write_text(
        json.dumps({"happy": "Express happiness"}), encoding="utf-8"
    )
    decision = {
        "caption": "A cheerful reaction to good news.",
        "tags": ["celebration", "good news"],
        "visible_text": "yay",
        "semantic_prompt_version": version,
        "semantic_category": category,
        "vision_model": "collection-vision",
    }
    storage.save_collected_image_semantic(pack, source, decision, manual_confirmed=True)
    metadata = storage.load_metadata(pack)
    item = next(iter(metadata["images"].values()))
    assert semantic_caption_is_complete(item) is complete
    assert item["auto_caption"] == decision["caption"]
    assert item["auto_tags"] == decision["tags"]
    assert item["category_review_status"] == "manual_confirmed"
    assert item["embedding_status"] == "pending"
    assert metadata["requires_local_index_rebuild"] is True
    reconciled = storage.reconcile_metadata(pack)
    assert (
        semantic_caption_is_complete(next(iter(reconciled["images"].values())))
        is complete
    )


def test_collection_analysis_rejects_image_outside_pack(tmp_path):
    pack = tmp_path / "pack"
    (pack / "memes").mkdir(parents=True)
    source = tmp_path / "outside.png"
    Image.new("RGB", (8, 8), "blue").save(source)
    with pytest.raises(ValueError):
        storage.save_collected_image_semantic(pack, source, {})


def test_collection_acceptance_does_not_rescan_existing_library(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    category = pack / "memes" / "happy"
    category.mkdir(parents=True)
    (pack / "memes_data.json").write_text('{"happy":"Joy"}', encoding="utf-8")
    scan = Mock(side_effect=AssertionError("Collection must not rehash the library"))
    monkeypatch.setattr(storage, "scan_images", scan)
    for name, color in (("first", "red"), ("second", "blue")):
        source = category / f"{name}.png"
        Image.new("RGB", (8, 8), color).save(source)
        storage.save_collected_image_semantic(pack, source, {})
    assert len(storage.load_metadata(pack)["images"]) == 2
    scan.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_index", ["unreadable", "missing_manifest", "invalid_manifest", "unreadable_vector"])
async def test_incremental_collection_reuses_vectors_and_keeps_existing_search(tmp_path, monkeypatch, broken_index):
    from unittest.mock import AsyncMock

    from astrbot_plugin_meme_manager.backend.semantic.index import (
        EmbeddingAdapter,
        build_index,
        index_is_ready,
        search_index,
    )
    from astrbot_plugin_meme_manager.backend.semantic.task import SemanticTaskManager

    class Provider:
        provider_config = {"id": "embedding", "embedding_model": "test-model"}
        get_dim = staticmethod(lambda: 2)
        get_embedding = AsyncMock(return_value=[1.0, 0.0])
        get_embeddings = AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts])

    pack = tmp_path / "pack"
    category = pack / "memes" / "happy"
    category.mkdir(parents=True)
    (pack / "memes_data.json").write_text('{"happy":"Joy"}', encoding="utf-8")
    decision = {
        "caption": "A happy reaction",
        "tags": ["joy"],
        "semantic_prompt_version": PROMPT_VERSION,
        "semantic_category": "happy",
    }
    first = category / "first.png"
    Image.new("RGB", (8, 8), "red").save(first)
    storage.save_collected_image_semantic(pack, first, decision)
    provider = Provider()
    embedding = EmbeddingAdapter(provider)
    runtime = tmp_path / "runtime"
    await build_index(pack, runtime, "pack", embedding)
    provider.get_embeddings.reset_mock()

    second = category / "second.png"
    Image.new("RGB", (8, 8), "blue").save(second)
    storage.save_collected_image_semantic(pack, second, {**decision, "caption": "New joy"})
    unfinished = category / "unfinished.png"
    Image.new("RGB", (8, 8), "green").save(unfinished)
    storage.save_collected_image_semantic(pack, unfinished, {})
    metadata = storage.load_metadata(pack)
    assert index_is_ready(runtime, "pack", metadata, allow_partial=True)
    assert not index_is_ready(runtime, "pack", metadata)
    before = await search_index(runtime, "pack", "joy", embedding, metadata)
    assert len(before) == 1
    assert before[0]["caption"] == decision["caption"]

    manager = SemanticTaskManager(runtime)
    manager._pack_dir = lambda pack_id: pack
    manager._require_embedding_provider = lambda *args: embedding
    manager._verify_embedding_dimension = AsyncMock()
    await manager.rebuild_index("pack", force=False)
    provider.get_embeddings.assert_awaited_once()
    assert len(provider.get_embeddings.await_args.args[0]) == 1
    metadata = storage.load_metadata(pack)
    after = await search_index(runtime, "pack", "joy", embedding, metadata)
    assert len(after) == 2
    assert index_is_ready(runtime, "pack", metadata, allow_partial=True)
    with pytest.raises(RuntimeError, match="描述未完成"):
        await manager.rebuild_index("pack", force=True)
    provider.provider_config = {"id": "other", "embedding_model": "test-model"}
    embedding = EmbeddingAdapter(provider)
    with pytest.raises(RuntimeError, match="全量重建"):
        await manager.rebuild_index("pack", force=False)
    provider.provider_config = {"id": "embedding", "embedding_model": "test-model"}
    embedding = EmbeddingAdapter(provider)
    next(item for item in metadata["images"].values() if item.get("caption"))["caption"] = "Stale entry"
    # A stale top-ranked vector must not hide the remaining valid entry at top_k=1.
    remaining = await search_index(runtime, "pack", "joy", embedding, metadata, top_k=1)
    assert len(remaining) == 1
    assert remaining[0]["caption"] != "Stale entry"
    # Changing semantic text must prevent stale vectors from returning that entry.
    for item in metadata["images"].values():
        if item.get("caption"):
            item["caption"] = "Changed text"
    assert not index_is_ready(runtime, "pack", metadata, allow_partial=True)
    provider.provider_config = {"id": "embedding", "embedding_model": "test-model"}
    embedding = EmbeddingAdapter(provider)
    assert await search_index(runtime, "pack", "joy", embedding, metadata) == []

    from astrbot_plugin_meme_manager.backend.semantic import index as index_module

    provider.get_embeddings.reset_mock()
    if broken_index == "unreadable":
        monkeypatch.setattr(index_module, "_read_faiss_index", lambda *args: None)
    elif broken_index == "missing_manifest":
        index_module.index_manifest_path(runtime, "pack").unlink()
    elif broken_index == "invalid_manifest":
        index_module.index_manifest_path(runtime, "pack").write_text("invalid", encoding="utf-8")
    else:
        monkeypatch.setattr(index_module, "_reconstruct_reusable_vectors", lambda *args: {})
    with pytest.raises(RuntimeError, match="全量重建"):
        await manager.rebuild_index("pack", force=False)
    provider.get_embeddings.assert_not_awaited()
