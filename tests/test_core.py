from src.core.markdown import html_to_markdown
from src.core.messages import estimate_tokens, flatten_messages
from src.core.types import ChatMessage


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


def test_estimate_tokens_is_positive():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd" * 10) >= 10


# ---- markdown conversion / image stripping -------------------------------
def test_markdown_keeps_images_by_default():
    md = html_to_markdown('<p>see <img src="a.png" alt="chart"></p>')
    assert md == "see ![chart](a.png)"


def test_drop_images_removes_standalone_images():
    md = html_to_markdown('<p><img src="a.png" alt="image">tail</p>', drop_images=True)
    assert "a.png" not in md and md == "tail"


def test_drop_images_discards_image_only_citation_chip():
    # ExpressAI's online mode renders a bare thumbnail chip per source. Nothing
    # is left of it once the image goes — the chip only repeats a URL the prose
    # already cites, so dropping it whole is the point.
    md = html_to_markdown(
        '<a href="https://www.federalregister.gov/d/1"><img src="t.png" alt="image"></a>',
        drop_images=True,
    )
    assert md == ""


def test_drop_images_keeps_inline_reference_links_that_have_text():
    md = html_to_markdown(
        '<p>news. <a href="https://r.ex/x"><img src="f.png" alt="image">reddit.com</a>'
        ' <a href="https://r.ex/x"><img src="t.png" alt="image"></a></p>',
        drop_images=True,
    )
    assert md == "news. [reddit.com](https://r.ex/x)"


def test_drop_images_leaves_text_and_code_intact():
    md = html_to_markdown(
        '<h2>Wrap</h2><ul><li><strong>Alert:</strong> bad bug</li></ul>'
        '<pre><code class="language-python">x = 1</code></pre>'
        '<figure><img src="c.png" alt="image"><figcaption>Trend</figcaption></figure>',
        drop_images=True,
    )
    assert "## Wrap" in md
    assert "- **Alert:** bad bug" in md
    assert "```python\nx = 1" in md
    assert "Trend" in md and "c.png" not in md
