"use strict";

// Responsive layout probe for the signed-in pages (Phase 10).
//
// Inserted (by tests/test_frontend_notifications.py) at the start of the
// <head> of a temporary copy of a real page, which is then shown in an
// <iframe> of an exact width: the iframe is the page's viewport, so the
// real CSS media queries apply. It installs a fake API (no server, no
// database), lets the page start, measures the header, opens the
// notification dropdown, measures it, and posts the result to the
// wrapper page.

(function () {

    const params = new URLSearchParams(window.location.search);
    const key = params.get("probe");

    sessionStorage.setItem("resolveai_access_token", "layout-probe");

    const notification = {
        id: 9,
        notification_type: "ticket_message",
        ticket_id: 3,
        ticket_title: "Refund not received for order #A-1002 placed last month",
        message_id: 1,
        actor_name: "Cara Customer",
        actor_role: "customer",
        preview: "It has been two weeks and the refund still has not arrived on my card.",
        created_at: new Date().toISOString(),
        read_at: null,
        is_read: false
    };

    window.fetch = async url => {
        const path = new URL(url, "http://probe.test").pathname;
        let body = {};

        if (path === "/api/auth/me") {
            // A long name: the widest profile the header must hold.
            body = { id: 1, name: "Alexandra Administrator", role: "admin" };
        } else if (path === "/api/notifications/unread-count") {
            body = { unread_count: 120, latest_id: 9 };
        } else if (path === "/api/notifications") {
            body = [notification];
        } else if (path.startsWith("/api/tickets") || path.startsWith("/api/admin/users")) {
            body = [];
        }

        return new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json", "X-Total-Count": "1" }
        });
    };

    const pause = ms => new Promise(resolve => setTimeout(resolve, ms));

    function describe(element) {
        const classes = typeof element.className === "string" ? element.className.trim() : "";

        return element.tagName.toLowerCase()
            + (element.id ? `#${element.id}` : "")
            + (classes ? `.${classes.split(/\s+/).join(".")}` : "");
    }

    function rect(element) {
        const r = element.getBoundingClientRect();

        return {
            left: Math.round(r.left),
            right: Math.round(r.right),
            top: Math.round(r.top),
            bottom: Math.round(r.bottom),
            width: Math.round(r.width),
            height: Math.round(r.height)
        };
    }

    // "ok" when the element is rendered inside the viewport and is what
    // a click at its centre would hit.
    function clickable(element, viewportWidth) {
        if (!element) {
            return "missing";
        }

        const r = element.getBoundingClientRect();

        if (r.width === 0 || r.height === 0) {
            return "not rendered";
        }

        if (r.left < -0.5 || r.right > viewportWidth + 0.5) {
            return "clipped";
        }

        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);

        return hit && (hit === element || element.contains(hit))
            ? "ok"
            : `covered by ${hit ? describe(hit) : "nothing"}`;
    }

    function overlapping(elements) {
        const pairs = [];

        for (let i = 0; i < elements.length; i++) {
            for (let j = i + 1; j < elements.length; j++) {
                const a = elements[i].getBoundingClientRect();
                const b = elements[j].getBoundingClientRect();

                if (a.left < b.right - 0.5 && b.left < a.right - 0.5
                    && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5) {
                    pairs.push(`${describe(elements[i])} / ${describe(elements[j])}`);
                }
            }
        }

        return pairs;
    }

    window.addEventListener("load", async () => {

        // Let the page script and the notification module start.
        await pause(600);

        const viewportWidth = document.documentElement.clientWidth;
        const header = document.querySelector(".dashboard-topbar");
        const bell = document.getElementById("notificationBell");
        const avatar = document.getElementById("agentAvatar");
        const logout = document.getElementById("logoutButton");

        const headerOutside = [...header.querySelectorAll("*")]
            .filter(element => {
                const r = element.getBoundingClientRect();
                return r.width > 0 && (r.right > viewportWidth + 0.5 || r.left < -0.5);
            })
            .map(describe);

        const navigation = [...document.querySelectorAll(".sidebar-navigation .nav-item")]
            .filter(element => !element.classList.contains("d-none"));

        const result = {
            key,
            viewportWidth,
            scrollWidth: document.documentElement.scrollWidth,
            headerScrollWidth: header.scrollWidth,
            headerClientWidth: header.clientWidth,
            headerOutside,
            controls: {
                bell: clickable(bell, viewportWidth),
                avatar: clickable(avatar, viewportWidth),
                logout: clickable(logout, viewportWidth)
            },
            navigation: navigation.map(element => [
                element.textContent.trim(),
                clickable(element, viewportWidth)
            ]),
            overlaps: overlapping([bell, avatar, logout, document.getElementById("agentName")]
                .filter(element => element && element.getBoundingClientRect().width > 0)),
            badge: document.getElementById("notificationBadge").textContent,
            rects: {
                header: rect(header),
                title: rect(header.querySelector("h1")),
                bell: rect(bell),
                avatar: rect(avatar),
                logout: rect(logout)
            }
        };

        bell.click();
        await pause(300);

        const panel = document.getElementById("notificationPanel");
        const item = panel.querySelector(".notification-item");
        const panelRect = panel.getBoundingClientRect();

        result.dropdown = {
            expanded: bell.getAttribute("aria-expanded"),
            visible: !panel.hidden && panelRect.width > 0,
            rect: rect(panel),
            fits: panelRect.left >= -0.5 && panelRect.right <= viewportWidth + 0.5,
            item: clickable(item, viewportWidth),
            markAll: clickable(document.getElementById("notificationMarkAll"), viewportWidth),
            scrollWidth: document.documentElement.scrollWidth
        };

        parent.postMessage(JSON.stringify(result), "*");
    });

}());
