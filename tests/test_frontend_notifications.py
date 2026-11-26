"""Frontend notification bell (Phase 10): markup, scripts and behaviour.

Static checks read the HTML, JavaScript and CSS files. Behaviour is
tested in a real browser: tests/frontend/notifications_harness.js runs
the real common.js and notifications.js in headless Chrome (or Edge)
against a fake fetch, with fake timers and visibility, on a generated
page holding the bell markup copied from tickets.html. Those tests are
skipped when no Chromium-based browser is installed. No server and no
database are used; resolveai.db is never touched.
"""

import html
import json
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
HARNESS_JS = Path(__file__).resolve().parent / "frontend" / "notifications_harness.js"

MODULE = FRONTEND / "js" / "notifications.js"
TICKETS_JS = FRONTEND / "js" / "tickets.js"
DASHBOARD_CSS = FRONTEND / "css" / "dashboard.css"

# Signed-in pages and their page script.
PAGES = {
    "dashboard.html": "./js/dashboard.js",
    "tickets.html": "./js/tickets.js",
    "users.html": "./js/users.js",
    "analytics.html": "./js/analytics.js",
}


# ---------------------------------------------------------------------------
# A small HTML reader
# ---------------------------------------------------------------------------


VOID_TAGS = {"meta", "link", "br", "img", "input", "hr", "path", "source"}


class _Element:
    def __init__(self, tag: str, attrs: dict, parent: "_Element | None") -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[_Element] = []

    def ancestors(self):
        node = self.parent

        while node is not None:
            yield node
            node = node.parent

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())


class _Page(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.root = _Element("#root", {}, None)
        self.current = self.root
        self.elements: list[_Element] = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        node = _Element(tag, dict(attrs), self.current)
        self.current.children.append(node)
        self.elements.append(node)

        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        node = _Element(tag, dict(attrs), self.current)
        self.current.children.append(node)
        self.elements.append(node)

    def handle_endtag(self, tag):
        node = self.current

        while node is not self.root and node.tag != tag:
            node = node.parent

        if node is not self.root:
            self.current = node.parent

    def by_id(self, element_id: str) -> list[_Element]:
        return [e for e in self.elements if e.attrs.get("id") == element_id]


def _page(name: str) -> _Page:
    return _Page((FRONTEND / name).read_text(encoding="utf-8"))


def _bell_markup(name: str) -> str:
    """The notification-center block of a page, verbatim."""

    text = (FRONTEND / name).read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index('<div class="notification-center"')
    depth = 0

    for match in re.finditer(r"<div\b|</div>", text[start:]):
        depth += 1 if match.group() == "<div" else -1

        if depth == 0:
            return text[start : start + match.end()]

    raise AssertionError(f"unbalanced notification markup in {name}")


# ---------------------------------------------------------------------------
# Markup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PAGES)
def test_bell_sits_in_the_profile_before_the_avatar(name: str) -> None:
    page = _page(name)
    [center] = page.by_id("notificationCenter")
    [avatar] = page.by_id("agentAvatar")

    profile = center.parent

    assert "agent-profile" in profile.classes()
    assert avatar.parent is profile
    assert profile.children.index(center) < profile.children.index(avatar)
    # Hidden until notifications.js confirms a signed-in user.
    assert "hidden" in center.attrs


@pytest.mark.parametrize("name", PAGES)
def test_bell_button_and_panel_are_accessible(name: str) -> None:
    page = _page(name)
    [bell] = page.by_id("notificationBell")
    [badge] = page.by_id("notificationBadge")
    [panel] = page.by_id("notificationPanel")
    [title] = page.by_id("notificationPanelTitle")

    assert bell.tag == "button"
    assert bell.attrs["type"] == "button"
    assert bell.attrs["aria-label"] == "Notifications"
    assert bell.attrs["aria-expanded"] == "false"
    assert bell.attrs["aria-controls"] == "notificationPanel"
    assert badge.parent is bell
    assert badge.attrs["aria-live"] == "polite"
    assert "hidden" in badge.attrs
    assert "hidden" in panel.attrs
    assert panel.attrs["aria-labelledby"] == title.attrs["id"]

    for element_id in (
        "notificationStatus",
        "notificationList",
        "notificationMarkAll",
        "notificationLoadMore",
    ):
        [node] = page.by_id(element_id)
        assert bell not in list(node.ancestors())
        assert panel in list(node.ancestors())

    [mark_all] = page.by_id("notificationMarkAll")
    [load_more] = page.by_id("notificationLoadMore")

    assert mark_all.tag == load_more.tag == "button"
    # Each ID appears once.
    for element_id in ("notificationCenter", "notificationBell", "notificationPanel"):
        assert len(page.by_id(element_id)) == 1


def test_bell_markup_is_identical_on_every_page() -> None:
    reference = _bell_markup("tickets.html")

    for name in PAGES:
        assert _bell_markup(name) == reference, name


def test_login_page_has_no_bell() -> None:
    text = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "notificationCenter" not in text
    assert "notifications.js" not in text


@pytest.mark.parametrize(("name", "page_script"), PAGES.items())
def test_script_loads_after_common_and_before_the_page_script(
    name: str,
    page_script: str,
) -> None:
    scripts = [
        element
        for element in _page(name).elements
        if element.tag == "script" and "src" in element.attrs
    ]
    sources = [element.attrs["src"] for element in scripts]

    assert sources.count("./js/notifications.js") == 1
    assert (
        sources.index("./js/common.js")
        < sources.index("./js/notifications.js")
        < sources.index(page_script)
    )

    for element in scripts:
        if element.attrs["src"].startswith("./js/"):
            assert "defer" in element.attrs


# ---------------------------------------------------------------------------
# Code safety and API contracts
# ---------------------------------------------------------------------------


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_module_never_injects_html() -> None:
    source = _source(MODULE)

    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert unsafe not in source

    assert "textContent" in source


def test_module_uses_the_shared_authentication_helpers() -> None:
    source = _source(MODULE)

    for helper in (
        "ResolveAI.loadCurrentUser()",
        "ResolveAI.authenticatedFetch(",
        "ResolveAI.apiRequest(",
        "ResolveAI.getAccessToken()",
    ):
        assert helper in source

    # No token handling of its own.
    for forbidden in ("sessionStorage", "localStorage", "Authorization", "/api/auth/"):
        assert forbidden not in source


def test_module_uses_the_documented_endpoints() -> None:
    source = _source(MODULE)

    assert 'const API_URL = "/api/notifications";' in source
    assert "const UNREAD_COUNT_URL = `${API_URL}/unread-count`;" in source
    assert "const READ_ALL_URL = `${API_URL}/read-all`;" in source
    assert "`${API_URL}/${encodeURIComponent(item.id)}/read`" in source
    assert "const LIST_LIMIT = 10;" in source
    assert 'params.set("before_id", String(beforeId));' in source
    assert "{ up_to_id: newestDisplayed }" in source
    assert "{ ticket_id: id }" in source
    assert 'method: "POST"' in source
    # Never sends a recipient, actor or role.
    for field in ("recipient_user_id", "actor_user_id", '"role"'):
        assert field not in source


def test_opening_a_conversation_marks_its_ticket_read() -> None:
    source = _source(TICKETS_JS).replace("\r\n", "\n")
    start = source.index("function openConversation(ticketId) {")
    end = source.index("\n}\n", start)
    body = source[start:end]

    call = "window.ResolveAINotifications?.markTicketRead(ticketId);"

    assert call in body
    # After the conversation has started loading, and never awaited.
    assert body.index("loadConversation(ticketId);") < body.index(call)
    assert "await window.ResolveAINotifications" not in source
    # The Phase 9 steps are all still there, in order.
    steps = [
        "saveConversationDraft();",
        "conversationDrafts.get(ticketId)",
        'hideAlert(getModalElement("conversationAlert"));',
        "updateConversationComposer(activeTicket);",
        "loadConversation(ticketId);",
    ]
    positions = [body.index(step) for step in steps]

    assert positions == sorted(positions)
    # The notification module is the only addition to tickets.js.
    assert source.count("ResolveAINotifications") == 1


def test_styles_exist_and_hidden_always_wins() -> None:
    css = _source(DASHBOARD_CSS)

    assert ".notification-center[hidden],\n.notification-center [hidden] {" in css.replace(
        "\r\n", "\n"
    )

    for selector in (
        ".notification-bell",
        ".notification-bell:focus-visible",
        ".notification-badge",
        ".notification-panel",
        ".notification-list",
        ".notification-item.is-unread",
        ".notification-item:focus-visible",
    ):
        assert f"{selector} {{" in css or f"{selector}," in css

    # Read and unread items are styled differently.
    assert ".notification-item.is-read .notification-preview {" in css
    assert ".notification-item.is-unread::before {" in css

    # The list scrolls instead of growing without bound.
    assert "max-height: min(420px, 60vh);" in css
    assert "overflow-y: auto;" in css


# ---------------------------------------------------------------------------
# Behaviour in a real browser
# ---------------------------------------------------------------------------


BROWSER_CANDIDATES = [
    os.environ.get("RESOLVEAI_TEST_BROWSER", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def _find_browser() -> str | None:
    for candidate in BROWSER_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate

    for name in ("google-chrome", "chromium", "chromium-browser", "chrome", "msedge"):
        found = shutil.which(name)

        if found:
            return found

    return None


# Every case in notifications_harness.js.
BROWSER_CASES = [
    "signed_out_visitors_see_no_bell_and_send_nothing",
    "bell_is_shown_for_every_role",
    "unknown_role_sees_no_bell",
    "bell_has_accessible_markup",
    "badge_hides_at_zero_and_caps_at_99_plus",
    "polls_every_30_seconds_while_visible",
    "polling_pauses_while_hidden_and_resumes_when_visible",
    "focus_refreshes_immediately",
    "only_one_count_request_in_flight",
    "errors_back_off_exponentially_up_to_5_minutes",
    "opening_fetches_ten_and_renders_items",
    "empty_state",
    "error_state_with_safe_message_and_retry",
    "server_strings_render_as_text",
    "repeated_ticket_notifications_are_grouped_in_order",
    "load_more_uses_before_id",
    "mark_all_displayed_sends_up_to_id",
    "clicking_a_notification_marks_it_read_then_navigates",
    "mark_ticket_read_posts_ticket_id_and_dispatches",
    "list_refreshes_only_when_latest_id_changes",
    "escape_and_outside_click_close_the_panel",
    "arrow_keys_move_between_notifications",
    "loading_the_script_twice_keeps_one_instance_and_timer",
    "external_changes_reload_an_open_list",
    "stop_detaches_polling",
]


@pytest.fixture(scope="module")
def browser_results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    browser = _find_browser()

    if browser is None:
        pytest.skip("No Chrome or Edge browser is installed for the frontend tests.")

    work = tmp_path_factory.mktemp("notification_browser")
    page = work / "harness.html"
    settings = {
        "COMMON_URL": (FRONTEND / "js" / "common.js").as_uri(),
        "MODULE_URL": MODULE.as_uri(),
        "BELL_MARKUP": _bell_markup("tickets.html"),
    }
    page.write_text(
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        f"<title>Notification harness</title>"
        f"<link rel=\"stylesheet\" href=\"{DASHBOARD_CSS.as_uri()}\"></head><body>"
        "<header class=\"dashboard-topbar\"><div class=\"agent-profile\" id=\"mount\"></div></header>"
        "<button type=\"button\" id=\"outside\">Outside</button>"
        "<pre id=\"results\">pending</pre>"
        f"<script>window.HARNESS = {json.dumps(settings)};</script>"
        f"<script src=\"{HARNESS_JS.as_uri()}\"></script>"
        "</body></html>",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--allow-file-access-from-files",
            f"--user-data-dir={work / 'profile'}",
            "--virtual-time-budget=120000",
            "--dump-dom",
            page.as_uri(),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    match = re.search(r'<pre id="results">(.*?)</pre>', completed.stdout, re.DOTALL)

    assert match, f"harness produced no results:\n{completed.stdout[-2000:]}"

    raw = html.unescape(match.group(1))

    assert raw != "pending", "the harness did not finish"

    return {result["name"]: result for result in json.loads(raw)}


def test_browser_harness_runs_every_case(browser_results: dict[str, dict]) -> None:
    assert sorted(browser_results) == sorted(BROWSER_CASES)


@pytest.mark.parametrize("case", BROWSER_CASES)
def test_browser_behaviour(browser_results: dict[str, dict], case: str) -> None:
    result = browser_results[case]

    assert result["ok"], result.get("error")


# ---------------------------------------------------------------------------
# Responsive header layout in a real browser
# ---------------------------------------------------------------------------

LAYOUT_PROBE_JS = Path(__file__).resolve().parent / "frontend" / "layout_probe.js"

LAYOUT_WIDTHS = [320, 375, 390, 768, 1366]

# The desktop dropdown width (and the most it may ever be).
PANEL_WIDTH = 380


def _page_copy(name: str, target: Path) -> Path:
    """A copy of a real page with absolute asset URLs and the probe first."""

    text = (FRONTEND / name).read_text(encoding="utf-8")
    base = FRONTEND.as_uri() + "/"
    text = text.replace('href="./', f'href="{base}').replace('src="./', f'src="{base}')
    text = text.replace(
        "<head>",
        f'<head>\n<script src="{LAYOUT_PROBE_JS.as_uri()}"></script>',
        1,
    )
    copy = target / name
    copy.write_text(text, encoding="utf-8")

    return copy


@pytest.fixture(scope="module")
def layout_results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    """Measurements for every page at every width, keyed "page@width".

    Headless Chrome will not make its window narrower than about 500px,
    so each page is shown in an <iframe> of the exact width (the iframe
    is the page's viewport). One browser run per page, with the widths
    side by side.
    """

    browser = _find_browser()

    if browser is None:
        pytest.skip("No Chrome or Edge browser is installed for the frontend tests.")

    work = tmp_path_factory.mktemp("notification_layout")
    results: dict[str, dict] = {}

    for name in PAGES:
        copy = _page_copy(name, work)
        frames = "".join(
            f'<iframe src="{copy.as_uri()}?probe={name}@{width}" '
            f'style="width:{width}px;height:900px;border:0;flex:none"></iframe>'
            for width in LAYOUT_WIDTHS
        )
        wrapper = work / f"layout-{name}"
        wrapper.write_text(
            "<!doctype html><html><head><meta charset=\"utf-8\"></head>"
            "<body style=\"margin:0\"><div style=\"display:flex;gap:10px\">"
            f"{frames}</div><pre id=\"results\">pending</pre><script>"
            "const results = {};"
            "window.addEventListener('message', event => {"
            "  const data = JSON.parse(event.data); results[data.key] = data;"
            f"  if (Object.keys(results).length === {len(LAYOUT_WIDTHS)}) {{"
            "    document.getElementById('results').textContent = JSON.stringify(results);"
            "  }"
            "});</script></body></html>",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-extensions",
                "--hide-scrollbars",
                "--allow-file-access-from-files",
                f"--user-data-dir={work / 'profile'}",
                f"--window-size={sum(LAYOUT_WIDTHS) + 100},1000",
                "--virtual-time-budget=20000",
                "--dump-dom",
                wrapper.as_uri(),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        match = re.search(r'<pre id="results">(.*?)</pre>', completed.stdout, re.DOTALL)

        assert match, f"no layout results for {name}"

        raw = html.unescape(match.group(1))

        assert raw != "pending", f"the layout probe did not finish for {name}"

        results.update(json.loads(raw))

    return results


LAYOUT_CASES = [(name, width) for name in PAGES for width in LAYOUT_WIDTHS]


def test_layout_probe_measured_every_page_and_width(
    layout_results: dict[str, dict],
) -> None:
    assert sorted(layout_results) == sorted(f"{n}@{w}" for n, w in LAYOUT_CASES)

    for result in layout_results.values():
        assert result["viewportWidth"] == int(result["key"].split("@")[1])


@pytest.mark.parametrize(("name", "width"), LAYOUT_CASES)
def test_header_never_overflows_and_controls_stay_usable(
    layout_results: dict[str, dict],
    name: str,
    width: int,
) -> None:
    result = layout_results[f"{name}@{width}"]

    # No horizontal page overflow, and nothing in the header outside it.
    assert result["scrollWidth"] <= result["viewportWidth"]
    assert result["headerScrollWidth"] <= result["headerClientWidth"]
    assert result["headerOutside"] == []

    # Bell, avatar and logout visible and clickable; no overlap.
    assert result["controls"] == {"bell": "ok", "avatar": "ok", "logout": "ok"}
    assert result["overlaps"] == []
    assert result["badge"] == "99+"

    # The navigation is still reachable.
    assert result["navigation"]
    assert all(state == "ok" for _label, state in result["navigation"]), result["navigation"]


@pytest.mark.parametrize(("name", "width"), LAYOUT_CASES)
def test_dropdown_opens_and_fits_the_viewport(
    layout_results: dict[str, dict],
    name: str,
    width: int,
) -> None:
    dropdown = layout_results[f"{name}@{width}"]["dropdown"]

    assert dropdown["expanded"] == "true"
    assert dropdown["visible"]
    assert dropdown["fits"], dropdown["rect"]
    assert dropdown["item"] == "ok"
    assert dropdown["markAll"] == "ok"
    assert dropdown["scrollWidth"] <= width
    # Never wider than on desktop; on narrow phones as wide as allowed
    # (16px margins).
    assert dropdown["rect"]["width"] == min(PANEL_WIDTH, width - 32)


@pytest.mark.parametrize("name", PAGES)
def test_desktop_header_layout_is_unchanged(
    layout_results: dict[str, dict],
    name: str,
) -> None:
    result = layout_results[f"{name}@1366"]
    rects = result["rects"]
    dropdown = result["dropdown"]["rect"]

    # One row: the title on the left; bell, avatar and logout on the
    # right, vertically centred together.
    assert rects["title"]["left"] < 400
    assert rects["bell"]["left"] > 900
    assert rects["bell"]["left"] < rects["avatar"]["left"] < rects["logout"]["left"]
    assert rects["logout"]["right"] >= result["viewportWidth"] - 60

    centres = {
        part: (rects[part]["top"] + rects[part]["bottom"]) / 2
        for part in ("bell", "avatar", "logout")
    }

    assert max(centres.values()) - min(centres.values()) <= 2
    assert rects["bell"]["width"] == rects["bell"]["height"] == 42
    assert rects["header"]["height"] == 105

    # The dropdown hangs from the bell, right-aligned, 10px below it.
    assert dropdown["right"] == rects["bell"]["right"]
    assert dropdown["top"] == rects["bell"]["bottom"] + 10
    assert dropdown["width"] == PANEL_WIDTH
