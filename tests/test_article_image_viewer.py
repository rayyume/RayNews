from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def between(start: str, end: str) -> str:
    left = HTML.index(start)
    right = HTML.index(end, left)
    return HTML[left:right]


def test_viewer_has_accessible_stage_close_button_and_single_delegated_entry():
    assert 'id="lbStage"' in HTML
    assert 'id="lbCloseBtn"' in HTML
    assert 'role="dialog"' in HTML
    assert 'aria-modal="true"' in HTML
    assert "function openImageViewer(image)" in HTML
    assert "articleWrap.addEventListener('click'" in HTML
    delegated = between(
        "articleWrap.addEventListener('click'",
        "\n});",
    )
    assert "closest('img')" in delegated
    assert "openImageViewer(image)" in delegated


def test_dynamic_translation_paths_do_not_bind_image_click_handlers():
    translate = between("async function aiTranslate(", "async function autoDisplaySummary(")
    auto_display = between("async function autoDisplaySummary(", "function showAIActions(")
    render_body = between("function renderArticleBody(", "function formatTime(")
    for block in (translate, auto_display, render_body):
        assert "querySelectorAll('img').forEach" not in block
        assert "img.addEventListener('click'" not in block


def test_viewer_no_longer_mutates_viewport_meta_for_zoom():
    close_block = between("function closeLightbox()", "function shareArticle()")
    assert "meta[name=viewport]" not in close_block
    assert "maximum-scale" not in close_block
