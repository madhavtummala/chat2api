from src.api.sessions import SessionStore
from src.core.types import ChatMessage


def test_create_and_get_roundtrip():
    store = SessionStore()
    entry = store.create([ChatMessage("user", "hi")], "m")
    assert entry.id.startswith("resp_")
    assert store.get(entry.id) is entry
    assert store.get("resp_missing") is None


def test_lru_eviction():
    store = SessionStore(max_entries=2)
    a = store.create([ChatMessage("user", "a")], "m")
    b = store.create([ChatMessage("user", "b")], "m")
    store.get(a.id)  # touch a so b becomes the oldest
    c = store.create([ChatMessage("user", "c")], "m")
    assert store.get(b.id) is None  # evicted
    assert store.get(a.id) is not None
    assert store.get(c.id) is not None
    assert len(store) == 2
