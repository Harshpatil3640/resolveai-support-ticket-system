"use strict";

// Browser tests for frontend/js/notifications.js.
//
// Loaded by tests/test_frontend_notifications.py into a generated page
// that holds the real bell markup (copied from tickets.html). Runs in
// headless Chrome with a fake fetch (no server, no database), fake
// timers and a controllable document.visibilityState. The real
// common.js and notifications.js are loaded afresh for every case.
// Results are written as JSON into <pre id="results">.

(async function () {

    const { COMMON_URL, MODULE_URL, BELL_MARKUP } = window.HARNESS;

    const realSetTimeout = window.setTimeout.bind(window);
    const out = document.getElementById("results");
    const results = [];

    // ---------------------------------------------------------------
    // Fake timers
    // ---------------------------------------------------------------

    const timers = new Map();
    let nextTimerId = 1;
    let now = 0;

    window.setTimeout = (callback, ms) => {
        const id = nextTimerId++;
        timers.set(id, { callback, at: now + (ms || 0), ms: ms || 0 });
        return id;
    };

    window.clearTimeout = id => {
        timers.delete(id);
    };

    function pendingTimers() {
        return [...timers.values()];
    }

    async function flush() {
        for (let i = 0; i < 6; i++) {
            await new Promise(resolve => realSetTimeout(resolve, 0));
        }
    }

    async function advance(ms) {
        const target = now + ms;

        for (;;) {
            const due = [...timers.entries()]
                .filter(([, timer]) => timer.at <= target)
                .sort((a, b) => a[1].at - b[1].at)[0];

            if (!due) {
                break;
            }

            const [id, timer] = due;
            timers.delete(id);
            now = timer.at;
            timer.callback();
            await flush();
        }

        now = target;
        await flush();
    }

    // ---------------------------------------------------------------
    // Fake visibility
    // ---------------------------------------------------------------

    let visibility = "visible";

    Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => visibility
    });

    async function setVisibility(value) {
        visibility = value;
        document.dispatchEvent(new Event("visibilitychange"));
        await flush();
    }

    // ---------------------------------------------------------------
    // Fake server
    // ---------------------------------------------------------------

    let calls = [];
    let server = null;
    let navigations = [];

    function json(status, body, headers = {}) {
        return new Response(JSON.stringify(body), {
            status,
            headers: { "Content-Type": "application/json", ...headers }
        });
    }

    function item(id, overrides = {}) {
        return {
            id,
            notification_type: "ticket_message",
            ticket_id: 5,
            ticket_title: "Payment failed",
            message_id: id + 100,
            actor_name: "Cara Customer",
            actor_role: "customer",
            preview: `Message ${id}`,
            created_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
            read_at: null,
            is_read: false,
            ...overrides
        };
    }

    function defaultServer() {
        return {
            me: { id: 1, name: "Ada Admin", role: "admin", is_active: true },
            count: { unread_count: 0, latest_id: null },
            countStatus: 200,
            countGate: null,
            items: [],
            listStatus: 200,
            listGate: null,
            readAllResult: { marked_read: 0, unread_count: 0 },
            readAllStatus: 200
        };
    }

    window.fetch = async (url, options = {}) => {
        const method = (options.method || "GET").toUpperCase();
        const parsed = new URL(url, "http://harness.test");
        const call = {
            method,
            path: parsed.pathname,
            search: parsed.search,
            params: Object.fromEntries(parsed.searchParams),
            body: options.body === undefined ? undefined : JSON.parse(options.body),
            auth: new Headers(options.headers || {}).get("Authorization")
        };
        calls.push(call);

        if (call.path === "/api/auth/me") {
            return json(200, server.me);
        }

        if (call.path === "/api/notifications/unread-count") {
            if (server.countGate) {
                await server.countGate;
            }
            if (server.countStatus !== 200) {
                return json(server.countStatus, { detail: "Internal server error." });
            }
            return json(200, server.count);
        }

        if (call.path === "/api/notifications" && method === "GET") {
            if (server.listGate) {
                await server.listGate;
            }
            if (server.listStatus !== 200) {
                return json(server.listStatus, { detail: "SQL secret leaked" });
            }
            const before = call.params.before_id ? Number(call.params.before_id) : null;
            const limit = Number(call.params.limit || 20);
            const visible = server.items.filter(entry => before === null || entry.id < before);
            return json(200, visible.slice(0, limit), {
                "X-Total-Count": String(server.items.length)
            });
        }

        if (call.path === "/api/notifications/read-all" && method === "POST") {
            if (server.readAllStatus !== 200) {
                return json(server.readAllStatus, { detail: "Internal server error." });
            }
            return json(200, server.readAllResult);
        }

        const one = call.path.match(/^\/api\/notifications\/(\d+)\/read$/);

        if (one && method === "POST") {
            const found = server.items.find(entry => entry.id === Number(one[1]));
            return json(200, { ...found, is_read: true, read_at: new Date().toISOString() });
        }

        return json(404, { detail: "Not Found" });
    };

    function callsTo(path, method = "GET") {
        return calls.filter(call => call.path === path && call.method === method);
    }

    // ---------------------------------------------------------------
    // Loading the module afresh
    // ---------------------------------------------------------------

    function loadScript(src) {
        return new Promise((resolve, reject) => {
            const script = document.createElement("script");
            script.src = `${src}?load=${Math.random()}`;
            script.onload = resolve;
            script.onerror = () => reject(new Error(`could not load ${src}`));
            document.body.append(script);
        });
    }

    const mount = document.getElementById("mount");

    async function setup({ token = true, configure } = {}) {
        if (window.ResolveAINotifications) {
            window.ResolveAINotifications.stop();
            delete window.ResolveAINotifications;
        }

        delete window.ResolveAI;
        timers.clear();
        calls = [];
        navigations = [];
        visibility = "visible";
        server = defaultServer();

        if (configure) {
            configure(server);
        }

        // Setting up the fixture page only (the module never does this).
        mount.innerHTML = BELL_MARKUP;

        if (token) {
            sessionStorage.setItem("resolveai_access_token", "test-token");
        } else {
            sessionStorage.removeItem("resolveai_access_token");
        }

        window.ResolveAINotificationsConfig = {
            navigate: url => navigations.push(url)
        };

        await loadScript(COMMON_URL);
        await loadScript(MODULE_URL);
        await flush();
    }

    const $ = selector => document.querySelector(selector);

    function assert(condition, message) {
        if (!condition) {
            throw new Error(message);
        }
    }

    function equal(actual, expected, message) {
        const a = JSON.stringify(actual);
        const e = JSON.stringify(expected);

        if (a !== e) {
            throw new Error(`${message}: expected ${e}, got ${a}`);
        }
    }

    async function openPanel() {
        $("#notificationBell").click();
        await flush();
    }

    // ---------------------------------------------------------------
    // Cases
    // ---------------------------------------------------------------

    const cases = {

        async signed_out_visitors_see_no_bell_and_send_nothing() {
            await setup({ token: false });

            assert($("#notificationCenter").hidden, "bell is hidden");
            equal(calls.length, 0, "requests");
            equal(pendingTimers().length, 0, "timers");
        },

        async bell_is_shown_for_every_role() {
            for (const role of ["admin", "agent", "customer"]) {
                await setup({ configure: s => { s.me.role = role; } });

                assert(!$("#notificationCenter").hidden, `bell visible for ${role}`);
                equal(callsTo("/api/notifications/unread-count").length, 1, `count for ${role}`);
                assert(
                    callsTo("/api/notifications/unread-count")[0].auth === "Bearer test-token",
                    "uses the shared bearer token"
                );
            }
        },

        async unknown_role_sees_no_bell() {
            await setup({ configure: s => { s.me.role = "guest"; } });

            assert($("#notificationCenter").hidden, "bell is hidden");
            equal(callsTo("/api/notifications/unread-count").length, 0, "no polling");
        },

        async bell_has_accessible_markup() {
            await setup();

            const bell = $("#notificationBell");

            equal(bell.tagName, "BUTTON", "real button");
            equal(bell.getAttribute("aria-label"), "Notifications", "label");
            equal(bell.getAttribute("aria-controls"), "notificationPanel", "controls");
            equal(bell.getAttribute("aria-expanded"), "false", "collapsed");
            equal($("#notificationBadge").getAttribute("aria-live"), "polite", "live badge");
        },

        async badge_hides_at_zero_and_caps_at_99_plus() {
            await setup();

            const badge = $("#notificationBadge");
            assert(badge.hidden, "hidden at zero");

            for (const [count, text] of [[1, "1"], [5, "5"], [99, "99"], [100, "99+"], [2500, "99+"]]) {
                server.count = { unread_count: count, latest_id: 9 };
                await window.ResolveAINotifications.refresh();
                assert(!badge.hidden, `visible for ${count}`);
                equal(badge.textContent, text, `badge for ${count}`);
            }

            server.count = { unread_count: 0, latest_id: 9 };
            await window.ResolveAINotifications.refresh();
            assert(badge.hidden, "hidden again at zero");
            equal(window.ResolveAINotifications.formatBadge(100), "99+", "formatBadge");
        },

        async polls_every_30_seconds_while_visible() {
            await setup();

            const count = () => callsTo("/api/notifications/unread-count").length;

            equal(count(), 1, "first request on start");
            equal(pendingTimers().map(t => t.ms), [30000], "one 30 s timer");

            await advance(29999);
            equal(count(), 1, "not before 30 s");

            await advance(1);
            equal(count(), 2, "polled at 30 s");
            equal(pendingTimers().length, 1, "still one timer");

            await advance(30000 * 3);
            equal(count(), 5, "three more polls");
            equal(pendingTimers().length, 1, "never duplicate timers");

            // The list is never fetched by polling.
            equal(callsTo("/api/notifications").length, 0, "no list requests");
        },

        async polling_pauses_while_hidden_and_resumes_when_visible() {
            await setup();

            const count = () => callsTo("/api/notifications/unread-count").length;

            await setVisibility("hidden");
            equal(pendingTimers().length, 0, "timer cleared when hidden");

            await advance(10 * 60 * 1000);
            equal(count(), 1, "no polls while hidden");

            // Focus while hidden does not poll either.
            window.dispatchEvent(new Event("focus"));
            await flush();
            equal(count(), 1, "no poll on focus while hidden");

            await setVisibility("visible");
            equal(count(), 2, "immediate refresh when visible");
            equal(pendingTimers().map(t => t.ms), [30000], "polling resumed once");

            await setVisibility("visible");
            equal(pendingTimers().length, 1, "repeated visible events: still one timer");
        },

        async focus_refreshes_immediately() {
            await setup();

            window.dispatchEvent(new Event("focus"));
            await flush();

            equal(callsTo("/api/notifications/unread-count").length, 2, "refreshed on focus");
            equal(pendingTimers().length, 1, "one timer");
        },

        async only_one_count_request_in_flight() {
            let release;

            await setup();
            server.countGate = new Promise(resolve => { release = resolve; });

            const api = window.ResolveAINotifications;
            const pending = [api.refresh(), api.refresh(), api.refresh()];
            window.dispatchEvent(new Event("focus"));
            await setVisibility("visible");
            await flush();

            equal(callsTo("/api/notifications/unread-count").length, 2, "one request in flight");

            server.countGate = null;
            release();
            await Promise.all(pending);
            await flush();

            equal(pendingTimers().length, 1, "one timer afterwards");
        },

        async errors_back_off_exponentially_up_to_5_minutes() {
            await setup({ configure: s => { s.countStatus = 500; } });

            const delays = [pendingTimers()[0].ms];

            for (let i = 0; i < 5; i++) {
                await advance(pendingTimers()[0].ms);
                delays.push(pendingTimers()[0].ms);
            }

            equal(delays, [60000, 120000, 240000, 300000, 300000, 300000], "backoff");

            server.countStatus = 200;
            await advance(pendingTimers()[0].ms);

            equal(pendingTimers().map(t => t.ms), [30000], "reset after success");
        },

        async opening_fetches_ten_and_renders_items() {
            let release;

            await setup({
                configure: s => {
                    s.count = { unread_count: 1, latest_id: 3 };
                    s.items = [
                        item(3, { actor_name: "Alex Agent", actor_role: "agent", preview: "On it" }),
                        item(2, { is_read: true, read_at: new Date().toISOString() })
                    ];
                }
            });
            server.listGate = new Promise(resolve => { release = resolve; });

            await openPanel();

            equal($("#notificationBell").getAttribute("aria-expanded"), "true", "expanded");
            assert(!$("#notificationPanel").hidden, "panel visible");
            equal(callsTo("/api/notifications").map(c => c.params), [{ limit: "10" }], "list request");
            equal($("#notificationStatus").textContent.trim(), "Loading notifications...", "loading state");

            server.listGate = null;
            release();
            await flush();

            const items = [...document.querySelectorAll(".notification-item")];

            equal(items.length, 2, "two items");
            equal($(".notification-ticket-title").textContent, "Payment failed (2)", "ticket title");
            equal(items[0].querySelector(".notification-actor").textContent, "Alex Agent", "actor");
            equal(items[0].querySelector(".notification-role").textContent, "Agent", "role");
            equal(items[0].querySelector(".notification-preview").textContent, "On it", "preview");
            equal(items[0].querySelector("time").textContent, "5 min ago", "relative time");
            assert(items[0].querySelector("time").dateTime, "machine-readable time");
            assert(items[0].classList.contains("is-unread"), "unread item marked");
            assert(items[1].classList.contains("is-read"), "read item marked");
            equal(items[0].getAttribute("href"), "/tickets.html?ticket=5", "deep link");
            assert($("#notificationStatus").hidden, "status hidden when populated");
        },

        async empty_state() {
            await setup();
            await openPanel();

            equal(document.querySelectorAll(".notification-item").length, 0, "no items");
            equal($("#notificationStatus").textContent.trim(), "You have no notifications.", "empty");
            assert($("#notificationMarkAll").disabled, "mark all disabled");
            assert($("#notificationLoadMore").hidden, "no load more");
        },

        async error_state_with_safe_message_and_retry() {
            await setup({ configure: s => { s.listStatus = 500; } });
            await openPanel();

            const status = $("#notificationStatus");

            equal(status.dataset.kind, "error", "error state");
            assert(status.textContent.includes("Unable to load notifications."), "fixed message");
            assert(!status.textContent.includes("SQL"), "server detail not shown");

            server.listStatus = 200;
            server.items = [item(1)];
            status.querySelector(".notification-retry").click();
            await flush();

            equal(document.querySelectorAll(".notification-item").length, 1, "retried");
        },

        async server_strings_render_as_text() {
            window.__xss = undefined;

            await setup({
                configure: s => {
                    s.items = [
                        item(1, {
                            preview: "<img src=x onerror=\"window.__xss=1\"> <script>window.__xss=2</script>",
                            ticket_title: "<b>Bold</b> title",
                            actor_name: "<i>Eve</i>"
                        })
                    ];
                }
            });
            await openPanel();

            const panel = $("#notificationPanel");

            equal(panel.querySelectorAll("img, b, i, script").length, 0, "no injected elements");
            equal(panel.querySelector(".notification-preview").textContent,
                "<img src=x onerror=\"window.__xss=1\"> <script>window.__xss=2</script>", "preview text");
            assert(panel.querySelector(".notification-ticket-title").textContent.startsWith("<b>Bold</b> title"),
                "title text");
            equal(panel.querySelector(".notification-actor").textContent, "<i>Eve</i>", "actor text");
            await flush();
            equal(window.__xss, undefined, "nothing executed");
        },

        async repeated_ticket_notifications_are_grouped_in_order() {
            await setup({
                configure: s => {
                    s.items = [
                        item(10, { ticket_id: 5, ticket_title: "Unassigned A" }),
                        item(9, { ticket_id: 5, ticket_title: "Unassigned A" }),
                        item(8, { ticket_id: 7, ticket_title: "Other" }),
                        item(7, { ticket_id: 5, ticket_title: "Unassigned A" })
                    ];
                }
            });
            await openPanel();

            const groups = [...document.querySelectorAll(".notification-group")];

            equal(groups.map(g => g.querySelector(".notification-ticket-title").textContent),
                ["Unassigned A (2)", "Other", "Unassigned A"], "groups");
            equal([...document.querySelectorAll(".notification-item")].map(a => a.dataset.notificationId),
                ["10", "9", "8", "7"], "every notification, newest first");
        },

        async load_more_uses_before_id() {
            await setup({
                configure: s => {
                    s.items = Array.from({ length: 12 }, (_, i) => item(20 - i));
                }
            });
            await openPanel();

            const loadMore = $("#notificationLoadMore");

            equal(document.querySelectorAll(".notification-item").length, 10, "first page");
            assert(!loadMore.hidden, "load more offered");

            loadMore.click();
            await flush();

            equal(callsTo("/api/notifications").map(c => c.params),
                [{ limit: "10" }, { limit: "10", before_id: "11" }], "cursor request");
            equal([...document.querySelectorAll(".notification-item")].map(a => Number(a.dataset.notificationId)),
                Array.from({ length: 12 }, (_, i) => 20 - i), "appended in order");
            assert(loadMore.hidden, "no more pages");
        },

        async mark_all_displayed_sends_up_to_id() {
            await setup({
                configure: s => {
                    s.count = { unread_count: 12, latest_id: 20 };
                    s.items = Array.from({ length: 12 }, (_, i) => item(20 - i));
                    s.readAllResult = { marked_read: 10, unread_count: 2 };
                }
            });
            await openPanel();

            const markAll = $("#notificationMarkAll");
            assert(!markAll.disabled, "enabled with unread items");

            markAll.click();
            await flush();

            equal(callsTo("/api/notifications/read-all", "POST").map(c => c.body),
                [{ up_to_id: 20 }], "payload");
            equal($("#notificationBadge").textContent, "2", "badge updated");
            equal(callsTo("/api/notifications").length, 2, "list reloaded once");
        },

        async clicking_a_notification_marks_it_read_then_navigates() {
            await setup({
                configure: s => {
                    s.count = { unread_count: 1, latest_id: 9 };
                    s.items = [item(9, { ticket_id: 42 })];
                }
            });
            await openPanel();

            const before = callsTo("/api/notifications/unread-count").length;
            server.count = { unread_count: 0, latest_id: 9 };

            $(".notification-item").click();
            await flush();

            equal(callsTo("/api/notifications/9/read", "POST").length, 1, "marked read");
            equal(callsTo("/api/notifications/unread-count").length, before + 1, "count refreshed");
            equal(navigations, ["/tickets.html?ticket=42"], "navigated to the ticket");
            assert($("#notificationPanel").hidden, "panel closed");
            assert($("#notificationBadge").hidden, "badge cleared");

            const readIndex = calls.findIndex(c => c.path === "/api/notifications/9/read");
            assert(readIndex !== -1 && navigations.length === 1, "mark before navigate");
        },

        async mark_ticket_read_posts_ticket_id_and_dispatches() {
            await setup({ configure: s => { s.readAllResult = { marked_read: 2, unread_count: 3 }; } });

            const events = [];
            document.addEventListener("resolveai:notifications-changed", e => events.push(e.detail));

            const result = await window.ResolveAINotifications.markTicketRead(7);

            equal(callsTo("/api/notifications/read-all", "POST").map(c => c.body),
                [{ ticket_id: 7 }], "payload");
            equal(result, { marked_read: 2, unread_count: 3 }, "result");
            equal($("#notificationBadge").textContent, "3", "badge updated");
            equal(events.map(e => e.ticketId), [7], "event dispatched");

            for (const bad of [0, -1, 1.5, "abc", null, undefined]) {
                equal(await window.ResolveAINotifications.markTicketRead(bad), null, `ignored ${bad}`);
            }

            equal(callsTo("/api/notifications/read-all", "POST").length, 1, "no extra requests");

            server.readAllStatus = 500;
            equal(await window.ResolveAINotifications.markTicketRead(8), null, "failure resolves null");
            equal(events.length, 1, "no event on failure");
        },

        async list_refreshes_only_when_latest_id_changes() {
            await setup({
                configure: s => {
                    s.count = { unread_count: 1, latest_id: 20 };
                    s.items = [item(20)];
                }
            });
            await openPanel();

            equal(callsTo("/api/notifications").length, 1, "loaded on open");

            await window.ResolveAINotifications.refresh();
            equal(callsTo("/api/notifications").length, 1, "unchanged latest_id: no reload");

            server.items = [item(21), item(20)];
            server.count = { unread_count: 2, latest_id: 21 };
            await advance(30000);

            equal(callsTo("/api/notifications").length, 2, "new latest_id: reloaded");
            equal(document.querySelectorAll(".notification-item").length, 2, "new item shown");

            // Closed: polling never fetches the list.
            window.ResolveAINotifications.close();
            server.count = { unread_count: 3, latest_id: 22 };
            await advance(30000);
            equal(callsTo("/api/notifications").length, 2, "no reload while closed");
        },

        async escape_and_outside_click_close_the_panel() {
            await setup({ configure: s => { s.items = [item(1)]; } });
            await openPanel();

            $(".notification-item").dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
            await flush();

            assert($("#notificationPanel").hidden, "closed on Escape");
            equal($("#notificationBell").getAttribute("aria-expanded"), "false", "collapsed");
            equal(document.activeElement, $("#notificationBell"), "focus returned to bell");

            await openPanel();
            $("#outside").dispatchEvent(new PointerEvent("pointerdown", { bubbles: true }));
            await flush();

            assert($("#notificationPanel").hidden, "closed on outside click");
        },

        async arrow_keys_move_between_notifications() {
            await setup({ configure: s => { s.items = [item(2), item(1)]; } });
            await openPanel();

            const panel = $("#notificationPanel");
            const items = [...document.querySelectorAll(".notification-item")];

            panel.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
            equal(document.activeElement, items[0], "first item");

            items[0].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
            equal(document.activeElement, items[1], "second item");

            items[1].dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", bubbles: true }));
            equal(document.activeElement, items[0], "wraps around");
        },

        async loading_the_script_twice_keeps_one_instance_and_timer() {
            await setup();

            const first = window.ResolveAINotifications;
            await loadScript(MODULE_URL);
            await flush();

            assert(window.ResolveAINotifications === first, "same instance");
            equal(pendingTimers().length, 1, "one timer");
            equal(callsTo("/api/notifications/unread-count").length, 1, "no extra polling");
        },

        async external_changes_reload_an_open_list() {
            await setup({ configure: s => { s.items = [item(1)]; } });
            await openPanel();

            document.dispatchEvent(new CustomEvent("resolveai:notifications-changed", {
                detail: { source: "ticket", ticketId: 5 }
            }));
            await flush();

            equal(callsTo("/api/notifications").length, 2, "reloaded");
        },

        async stop_detaches_polling() {
            await setup();
            window.ResolveAINotifications.stop();

            equal(pendingTimers().length, 0, "timer cleared");

            window.dispatchEvent(new Event("focus"));
            await setVisibility("visible");
            equal(callsTo("/api/notifications/unread-count").length, 1, "no more requests");
        }
    };

    for (const [name, run] of Object.entries(cases)) {
        try {
            await run();
            results.push({ name, ok: true });
        } catch (error) {
            results.push({ name, ok: false, error: String(error && error.message || error) });
        }
    }

    if (window.ResolveAINotifications) {
        window.ResolveAINotifications.stop();
    }

    out.textContent = JSON.stringify(results);
    document.title = "done";

}());
