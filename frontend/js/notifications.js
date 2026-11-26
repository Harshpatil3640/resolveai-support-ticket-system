"use strict";

// =========================================
// RESOLVEAI - NOTIFICATIONS (PHASE 10)
// =========================================
//
// The notification bell, its dropdown and unread-count polling, shared
// by every signed-in page. Loaded as a classic deferred script AFTER
// common.js and BEFORE the page script. Exposed as the single global
// `window.ResolveAINotifications`.
//
// - Authentication, tokens and the current user come from common.js
//   (window.ResolveAI); nothing is duplicated here. Without a stored
//   token the module does nothing at all.
// - The bell stays hidden until the user's profile has loaded with a
//   known role, so signed-out visitors never see it.
// - Every server-provided string is inserted with textContent: message
//   previews and ticket titles are always shown as plain text.
// - Only the unread count is polled (every 30 seconds while the page is
//   visible, one request at a time, backing off after errors). The list
//   is fetched when the dropdown opens, or again while it is open when
//   a new notification has arrived.
//
// Pages without the bell markup can still call markTicketRead().

(function (window, document) {

    // Loaded twice? Keep the first instance (one timer per page).
    if (window.ResolveAINotifications) {
        return;
    }

    const ResolveAI = window.ResolveAI;

    if (!ResolveAI) {
        return;
    }

    const API_URL = "/api/notifications";
    const UNREAD_COUNT_URL = `${API_URL}/unread-count`;
    const READ_ALL_URL = `${API_URL}/read-all`;

    const CHANGED_EVENT = "resolveai:notifications-changed";

    const LIST_LIMIT = 10;
    const MAX_BADGE_COUNT = 99;
    const MAX_DATABASE_ID = Number.MAX_SAFE_INTEGER;

    // Test hooks only (set before this script loads): a navigation
    // function and the polling interval.
    const config = window.ResolveAINotificationsConfig || {};

    const POLL_INTERVAL_MS = config.pollIntervalMs || 30000;
    const MAX_BACKOFF_MS = 5 * 60 * 1000;

    const navigate = typeof config.navigate === "function"
        ? config.navigate
        : url => window.location.assign(url);

    const MESSAGES = Object.freeze({
        loading: "Loading notifications...",
        empty: "You have no notifications.",
        listError: "Unable to load notifications.",
        markError: "Unable to update notifications. Please try again."
    });

    const state = {
        user: null,
        started: false,
        stopped: false,
        open: false,
        unreadCount: 0,
        latestId: null,
        // The latest_id the displayed list was loaded for.
        listLatestId: undefined,
        items: [],
        total: 0,
        listRequestId: 0,
        loadingMore: false,
        countRequest: null,
        timer: null,
        failures: 0,
        ticketRequests: new Map()
    };

    let elements = null;

    // Listeners added by this module, removed again by stop().
    const listeners = [];


    // =========================================
    // SMALL HELPERS
    // =========================================

    function isPositiveId(value) {

        return Number.isInteger(value) && value >= 1 && value <= MAX_DATABASE_ID;

    }


    function listen(target, type, handler, options) {

        target.addEventListener(type, handler, options);

        listeners.push(() => target.removeEventListener(type, handler, options));

    }


    function element(tag, className, text) {

        const node = document.createElement(tag);

        if (className) {
            node.className = className;
        }

        if (text !== undefined && text !== null) {
            node.textContent = String(text);
        }

        return node;

    }


    function dispatchChanged(detail) {

        document.dispatchEvent(
            new CustomEvent(CHANGED_EVENT, { detail })
        );

    }


    function formatBadge(count) {

        return count > MAX_BADGE_COUNT ? `${MAX_BADGE_COUNT}+` : String(count);

    }


    function formatRelativeTime(isoValue, now = Date.now()) {

        const time = Date.parse(isoValue);

        if (Number.isNaN(time)) {
            return "";
        }

        const seconds = Math.max(0, Math.round((now - time) / 1000));

        if (seconds < 60) {
            return "Just now";
        }

        const minutes = Math.floor(seconds / 60);

        if (minutes < 60) {
            return `${minutes} min ago`;
        }

        const hours = Math.floor(minutes / 60);

        if (hours < 24) {
            return `${hours} h ago`;
        }

        const days = Math.floor(hours / 24);

        if (days < 7) {
            return `${days} d ago`;
        }

        return new Date(time).toLocaleDateString();

    }


    function ticketUrl(ticketId) {

        return `/tickets.html?ticket=${encodeURIComponent(ticketId)}`;

    }


    // =========================================
    // API
    // =========================================

    async function postJson(url, body, fallbackMessage) {

        return ResolveAI.apiRequest(
            url,
            {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: body === undefined ? undefined : JSON.stringify(body)
            },
            fallbackMessage
        );

    }


    // One page of notifications, with the X-Total-Count header.
    async function fetchPage(beforeId) {

        const params = new URLSearchParams({ limit: String(LIST_LIMIT) });

        if (beforeId !== undefined && beforeId !== null) {
            params.set("before_id", String(beforeId));
        }

        const response = await ResolveAI.authenticatedFetch(
            `${API_URL}?${params}`
        );

        await ResolveAI.checkApiResponse(response, MESSAGES.listError);

        const items = await ResolveAI.readJsonResponse(response);

        if (!Array.isArray(items)) {
            throw new ResolveAI.ApiError(MESSAGES.listError, { kind: "parse" });
        }

        const total = Number.parseInt(response.headers.get("X-Total-Count"), 10);

        return {
            items,
            total: Number.isNaN(total) ? items.length : total
        };

    }


    // =========================================
    // BADGE
    // =========================================

    function setUnreadCount(count) {

        state.unreadCount = Number.isInteger(count) && count > 0 ? count : 0;

        if (!elements) {
            return;
        }

        const { badge } = elements;

        if (state.unreadCount === 0) {

            badge.hidden = true;
            badge.textContent = "";

        } else {

            badge.textContent = formatBadge(state.unreadCount);
            badge.hidden = false;

        }

        badge.title = `${state.unreadCount} unread`;

        elements.markAll.disabled = state.unreadCount === 0 || state.items.length === 0;

    }


    // =========================================
    // UNREAD COUNT AND POLLING
    // =========================================

    // Fetch the unread summary; at most one request at a time.
    function refresh() {

        if (state.countRequest) {
            return state.countRequest;
        }

        const request = ResolveAI.apiRequest(
            UNREAD_COUNT_URL,
            {},
            MESSAGES.listError
        ).then(summary => {

            state.failures = 0;
            setUnreadCount(summary && summary.unread_count);

            const latestId = summary && isPositiveId(summary.latest_id)
                ? summary.latest_id
                : null;

            state.latestId = latestId;

            // Refresh an open list only when something new arrived.
            if (state.open && latestId !== state.listLatestId) {
                loadNotifications();
            }

            return summary;

        }).catch(error => {

            state.failures += 1;

            if (ResolveAI.isAuthenticationError(error)) {
                // common.js is already redirecting to the login page.
                stop();
            }

            return null;

        }).finally(() => {

            state.countRequest = null;

        });

        state.countRequest = request;

        return request;

    }


    function nextDelay() {

        if (state.failures === 0) {
            return POLL_INTERVAL_MS;
        }

        return Math.min(
            POLL_INTERVAL_MS * 2 ** state.failures,
            MAX_BACKOFF_MS
        );

    }


    function clearTimer() {

        if (state.timer !== null) {
            window.clearTimeout(state.timer);
            state.timer = null;
        }

    }


    function schedule() {

        clearTimer();

        if (state.stopped || document.visibilityState !== "visible") {
            return;
        }

        state.timer = window.setTimeout(poll, nextDelay());

    }


    async function poll() {

        state.timer = null;

        if (state.stopped || document.visibilityState !== "visible") {
            return;
        }

        await refresh();

        schedule();

    }


    // Refresh now (if visible) and restart the polling interval.
    async function refreshNow() {

        if (state.stopped || document.visibilityState !== "visible") {
            clearTimer();
            return;
        }

        clearTimer();

        await refresh();

        schedule();

    }


    function onVisibilityChange() {

        if (document.visibilityState === "visible") {
            refreshNow();
        } else {
            // Paused while hidden.
            clearTimer();
        }

    }


    // =========================================
    // DROPDOWN LIST
    // =========================================

    function setStatus(message, kind = "info", withRetry = false) {

        const { status } = elements;

        status.replaceChildren();
        status.dataset.kind = kind;
        status.hidden = !message;

        if (!message) {
            return;
        }

        status.append(element("span", "notification-status-text", message));

        if (withRetry) {

            const retry = element(
                "button",
                "btn btn-link btn-sm notification-retry",
                "Retry"
            );
            retry.type = "button";
            retry.addEventListener("click", () => loadNotifications());
            status.append(retry);

        }

    }


    function renderItem(item) {

        const listItem = element("li", "notification-entry");
        const link = element(
            "a",
            `notification-item ${item.is_read ? "is-read" : "is-unread"}`
        );

        link.href = ticketUrl(item.ticket_id);
        link.dataset.notificationId = String(item.id);
        link.dataset.ticketId = String(item.ticket_id);

        const meta = element("span", "notification-meta");
        const actor = element("span", "notification-actor", item.actor_name);
        const role = element(
            "span",
            "notification-role",
            item.actor_role ? ResolveAI.formatRole(item.actor_role) : ""
        );
        const time = element(
            "time",
            "notification-time",
            formatRelativeTime(item.created_at)
        );

        time.dateTime = item.created_at;
        time.title = new Date(item.created_at).toLocaleString();
        meta.append(actor, role, time);

        const preview = element("span", "notification-preview", item.preview);

        link.append(meta, preview);

        // Read state, for screen readers as well as visually.
        link.append(
            element(
                "span",
                "visually-hidden",
                item.is_read ? " (read)" : " (unread)"
            )
        );

        link.addEventListener("click", event => {
            event.preventDefault();
            openNotification(item);
        });

        listItem.append(link);

        return listItem;

    }


    // Consecutive notifications on the same ticket share one heading
    // (so repeated alerts on one ticket are recognisable); the order and
    // every individual notification are kept.
    function renderList() {

        const { list, loadMore } = elements;

        list.replaceChildren();

        let group = null;
        let groupTicketId = null;

        for (const item of state.items) {

            if (group === null || item.ticket_id !== groupTicketId) {

                groupTicketId = item.ticket_id;
                group = element("li", "notification-group");

                const heading = element(
                    "p",
                    "notification-ticket-title",
                    item.ticket_title
                );
                const groupList = element("ul", "notification-group-items");

                group.append(heading, groupList);
                list.append(group);

            }

            group.querySelector(".notification-group-items").append(renderItem(item));

        }

        // Count per group, shown when a ticket repeats.
        list.querySelectorAll(".notification-group").forEach(groupNode => {

            const count = groupNode.querySelectorAll(".notification-item").length;

            if (count > 1) {
                groupNode.querySelector(".notification-ticket-title").append(
                    element("span", "notification-group-count", ` (${count})`)
                );
            }

        });

        loadMore.hidden = !(state.items.length < state.total);
        loadMore.disabled = state.loadingMore;

        elements.markAll.disabled = state.unreadCount === 0 || state.items.length === 0;

        if (state.items.length === 0) {
            setStatus(MESSAGES.empty);
        } else {
            setStatus("");
        }

    }


    // Load (or reload) the newest page.
    async function loadNotifications() {

        if (!elements) {
            return null;
        }

        const requestId = ++state.listRequestId;
        const latestIdAtRequest = state.latestId;

        state.loadingMore = false;

        if (state.items.length === 0) {
            elements.list.replaceChildren();
            elements.loadMore.hidden = true;
        }

        setStatus(MESSAGES.loading, "loading");

        try {

            const page = await fetchPage();

            if (requestId !== state.listRequestId) {
                return null;
            }

            state.items = page.items;
            state.total = page.total;
            state.listLatestId = page.items.length
                ? Math.max(...page.items.map(item => item.id))
                : latestIdAtRequest;

            renderList();

            return page;

        } catch (error) {

            if (requestId !== state.listRequestId || ResolveAI.isAuthenticationError(error)) {
                return null;
            }

            state.items = [];
            state.total = 0;
            elements.list.replaceChildren();
            elements.loadMore.hidden = true;
            setStatus(MESSAGES.listError, "error", true);

            return null;

        }

    }


    // Reload the list if the dropdown is open.
    function refreshList() {

        return state.open ? loadNotifications() : Promise.resolve(null);

    }


    async function loadMore() {

        if (state.loadingMore || state.items.length === 0) {
            return;
        }

        const requestId = state.listRequestId;
        const oldest = state.items[state.items.length - 1];

        state.loadingMore = true;
        elements.loadMore.disabled = true;

        try {

            const page = await fetchPage(oldest.id);

            if (requestId !== state.listRequestId) {
                return;
            }

            const known = new Set(state.items.map(item => item.id));

            state.items = state.items.concat(
                page.items.filter(item => !known.has(item.id))
            );
            state.total = page.total;

        } catch (error) {

            if (requestId !== state.listRequestId || ResolveAI.isAuthenticationError(error)) {
                return;
            }

            setStatus(MESSAGES.listError, "error");

        } finally {

            if (requestId === state.listRequestId) {
                state.loadingMore = false;
                renderList();
            }

        }

    }


    // =========================================
    // MARKING READ
    // =========================================

    async function markAllDisplayedRead() {

        if (state.items.length === 0) {
            return null;
        }

        const newestDisplayed = Math.max(...state.items.map(item => item.id));

        elements.markAll.disabled = true;

        try {

            const result = await postJson(
                READ_ALL_URL,
                { up_to_id: newestDisplayed },
                MESSAGES.markError
            );

            setUnreadCount(result && result.unread_count);
            // This module reloads its own list below.
            dispatchChanged({ source: "mark-all", internal: true });
            await loadNotifications();

            return result;

        } catch (error) {

            if (!ResolveAI.isAuthenticationError(error)) {
                setStatus(MESSAGES.markError, "error");
                elements.markAll.disabled = false;
            }

            return null;

        }

    }


    // Mark one notification read, then open its ticket. Navigation
    // happens even if marking fails (opening the conversation marks
    // the ticket's notifications read anyway).
    async function openNotification(item) {

        try {

            // Idempotent: an already-read notification keeps its read_at.
            await postJson(
                `${API_URL}/${encodeURIComponent(item.id)}/read`,
                undefined,
                MESSAGES.markError
            );

            dispatchChanged({
                source: "mark-one",
                notificationId: item.id,
                internal: true
            });
            await refresh();

        } catch (error) {

            if (ResolveAI.isAuthenticationError(error)) {
                return;
            }

        }

        close({ restoreFocus: false });
        navigate(ticketUrl(item.ticket_id));

    }


    // Mark every notification on one ticket read (the conversation was
    // opened). Never throws or rejects; resolves to the API result, or
    // null when nothing was sent or the request failed.
    function markTicketRead(ticketId) {

        const id = typeof ticketId === "string" && /^[1-9][0-9]{0,15}$/.test(ticketId)
            ? Number(ticketId)
            : ticketId;

        if (!isPositiveId(id) || !ResolveAI.getAccessToken()) {
            return Promise.resolve(null);
        }

        // One request per ticket at a time.
        if (state.ticketRequests.has(id)) {
            return state.ticketRequests.get(id);
        }

        const request = postJson(
            READ_ALL_URL,
            { ticket_id: id },
            MESSAGES.markError
        ).then(result => {

            if (result && Number.isInteger(result.unread_count)) {
                setUnreadCount(result.unread_count);
            }

            dispatchChanged({ source: "ticket", ticketId: id });

            return result;

        }).catch(() => null).finally(() => {

            state.ticketRequests.delete(id);

        });

        state.ticketRequests.set(id, request);

        return request;

    }


    // =========================================
    // OPENING AND CLOSING
    // =========================================

    function open() {

        if (!elements || state.open) {
            return;
        }

        state.open = true;
        elements.panel.hidden = false;
        elements.bell.setAttribute("aria-expanded", "true");
        elements.root.classList.add("is-open");

        loadNotifications();

    }


    function close({ restoreFocus = true } = {}) {

        if (!elements || !state.open) {
            return;
        }

        state.open = false;
        state.listRequestId++;
        elements.panel.hidden = true;
        elements.bell.setAttribute("aria-expanded", "false");
        elements.root.classList.remove("is-open");

        if (restoreFocus) {
            elements.bell.focus();
        }

    }


    function toggle() {

        if (state.open) {
            close();
        } else {
            open();
        }

    }


    function focusableItems() {

        return [...elements.panel.querySelectorAll(".notification-item")];

    }


    function onPanelKeydown(event) {

        if (event.key === "Escape") {
            event.preventDefault();
            close();
            return;
        }

        if (event.key !== "ArrowDown" && event.key !== "ArrowUp") {
            return;
        }

        const items = focusableItems();

        if (items.length === 0) {
            return;
        }

        event.preventDefault();

        const index = items.indexOf(document.activeElement);
        const step = event.key === "ArrowDown" ? 1 : -1;
        const next = index === -1
            ? (step === 1 ? 0 : items.length - 1)
            : (index + step + items.length) % items.length;

        items[next].focus();

    }


    function onDocumentPointerDown(event) {

        if (state.open && !elements.root.contains(event.target)) {
            close({ restoreFocus: false });
        }

    }


    function onFocusOut(event) {

        const next = event.relatedTarget;

        if (state.open && next && !elements.root.contains(next)) {
            close({ restoreFocus: false });
        }

    }


    // =========================================
    // START AND STOP
    // =========================================

    function findElements() {

        const root = document.getElementById("notificationCenter");

        if (!root) {
            return null;
        }

        const found = {
            root,
            bell: root.querySelector("#notificationBell"),
            badge: root.querySelector("#notificationBadge"),
            panel: root.querySelector("#notificationPanel"),
            status: root.querySelector("#notificationStatus"),
            list: root.querySelector("#notificationList"),
            markAll: root.querySelector("#notificationMarkAll"),
            loadMore: root.querySelector("#notificationLoadMore")
        };

        return Object.values(found).every(Boolean) ? found : null;

    }


    function start(user) {

        if (state.started || state.stopped) {
            return;
        }

        state.started = true;
        state.user = user;
        elements = findElements();

        if (elements) {

            listen(elements.bell, "click", toggle);
            listen(elements.panel, "keydown", onPanelKeydown);
            listen(elements.bell, "keydown", event => {
                if (event.key === "Escape" && state.open) {
                    event.preventDefault();
                    close();
                }
            });
            listen(elements.root, "focusout", onFocusOut);
            listen(elements.markAll, "click", markAllDisplayedRead);
            listen(elements.loadMore, "click", loadMore);
            listen(document, "pointerdown", onDocumentPointerDown);

            elements.root.hidden = false;
            setUnreadCount(0);

        }

        // Read state changed elsewhere (e.g. a conversation was opened):
        // reload an open list. The module's own list actions reload it
        // themselves.
        listen(document, CHANGED_EVENT, event => {
            if (!(event.detail && event.detail.internal)) {
                refreshList();
            }
        });

        listen(document, "visibilitychange", onVisibilityChange);
        listen(window, "focus", () => refreshNow());

        refreshNow();

    }


    // Stop polling and detach every listener (e.g. when signing out).
    function stop() {

        state.stopped = true;
        clearTimer();

        while (listeners.length) {
            listeners.pop()();
        }

    }


    function initialise() {

        if (!ResolveAI.getAccessToken()) {
            return;
        }

        ResolveAI.loadCurrentUser()
            .then(user => {
                if (user && ResolveAI.isKnownRole(user.role)) {
                    start(user);
                }
            })
            .catch(() => {
                // Profile unavailable: no bell. Authentication failures
                // are handled (redirect) by common.js.
            });

    }


    window.ResolveAINotifications = Object.freeze({
        CHANGED_EVENT,
        refresh: refreshNow,
        refreshList,
        loadNotifications,
        loadMore,
        markTicketRead,
        markAllDisplayedRead,
        open,
        close,
        stop,
        formatBadge,
        formatRelativeTime
    });

    initialise();

}(window, document));
