from src.core.messages import estimate_tokens, flatten_messages
from src.core.types import ChatMessage, ChatRequest


def test_flatten_single_user_message_is_verbatim():
    assert flatten_messages([ChatMessage("user", "hi there")]) == "hi there"


def test_flatten_multi_turn_is_labelled():
    out = flatten_messages(
        [
            ChatMessage("system", "Be terse."),
            ChatMessage("user", "Hello"),
            ChatMessage("assistant", "Hi"),
            ChatMessage("user", "Bye"),
        ]
    )
    assert out.startswith("System: Be terse.")
    assert "User: Hello" in out
    assert out.rstrip().endswith("Assistant:")


def test_last_user_message_picks_latest_user_turn():
    req = ChatRequest(
        messages=[
            ChatMessage("user", "first"),
            ChatMessage("assistant", "ok"),
            ChatMessage("user", "second"),
        ],
        model="m",
    )
    assert req.last_user_message == "second"


def test_estimate_tokens_is_positive():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd" * 10) >= 10
