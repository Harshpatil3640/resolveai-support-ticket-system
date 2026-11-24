"use strict";

// =========================================
// RESOLVEAI - SHARED FRONTEND UTILITIES
// =========================================
//
// Loaded as a classic script before each page-specific script.
// Everything is exposed through the single global `window.ResolveAI`
// namespace so page scripts can keep their own top-level names
// without colliding with the shared helpers.

(function (window) {

    const TOKEN_KEY = "resolveai_access_token";

    const LOGIN_PATH = "/";

    const ROLES = Object.freeze({
        ADMIN: "admin",
        AGENT: "agent",
        CUSTOMER: "customer"
    });

    const STAFF_ROLES = Object.freeze([
        ROLES.ADMIN,
        ROLES.AGENT
    ]);

    const KNOWN_ROLES = Object.freeze(Object.values(ROLES));

    // Attribute listing the roles allowed to see an element,
    // e.g. data-visible-roles="admin agent".
    const VISIBLE_ROLES_ATTRIBUTE = "data-visible-roles";

    const MAX_ERROR_MESSAGE_LENGTH = 300;

    // Shared in-flight/resolved /api/auth/me request for this page load.
    let currentUserPromise = null;

    const MESSAGES = Object.freeze({
        authenticationRequired: "Please sign in to continue.",
        sessionExpired: "Your session has expired. Please sign in again.",
        forbidden: "You do not have permission to perform this action.",
        network:
            "Unable to connect to the ResolveAI server. " +
            "Please check your connection and try again.",
        unreadableResponse: "Unable to read the server response.",
        serverError:
            "The server encountered an error. Please try again later.",
        generic: "Something went wrong. Please try again."
    });


    // =========================================
    // API ERROR TYPE
    // =========================================

    class ApiError extends Error {

        constructor(message, { status = 0, kind = "api" } = {}) {

            super(message);

            this.name = "ApiError";

            // HTTP status code, or 0 when no response was received.
            this.status = status;

            // One of: "api", "auth", "forbidden", "network", "parse".
            this.kind = kind;

        }

    }


    function isAuthenticationError(error) {

        return error instanceof ApiError && error.kind === "auth";

    }


    function isForbiddenError(error) {

        return error instanceof ApiError && error.kind === "forbidden";

    }


    function isNetworkError(error) {

        return error instanceof ApiError && error.kind === "network";

    }


    // =========================================
    // ACCESS TOKEN STORAGE
    // =========================================

    function getAccessToken() {

        try {

            return window.sessionStorage.getItem(TOKEN_KEY);

        } catch {

            // Storage can be unavailable (e.g. blocked site data).
            return null;

        }

    }


    function setAccessToken(token) {

        window.sessionStorage.setItem(TOKEN_KEY, token);

    }


    function clearAccessToken() {

        try {

            window.sessionStorage.removeItem(TOKEN_KEY);

        } catch {

            // Nothing to clear if storage is unavailable.

        }

        // The cached profile belongs to the cleared session.
        currentUserPromise = null;

    }


    function redirectToLogin() {

        clearAccessToken();

        window.location.replace(LOGIN_PATH);

    }


    // =========================================
    // SAFE RESPONSE PARSING
    // =========================================

    function truncateMessage(message) {

        const text = String(message).trim();

        if (text.length <= MAX_ERROR_MESSAGE_LENGTH) {

            return text;

        }

        return `${text.slice(0, MAX_ERROR_MESSAGE_LENGTH - 1)}…`;

    }


    // Read a response body as JSON without ever throwing.
    // Returns undefined when the body is empty or not valid JSON.
    async function readJsonSafely(response) {

        let text;

        try {

            text = await response.text();

        } catch {

            return undefined;

        }

        if (!text) {

            return undefined;

        }

        try {

            return JSON.parse(text);

        } catch {

            return undefined;

        }

    }


    // Extract a user-facing message from a FastAPI error body.
    // Only `detail` strings and validation `msg` values are used;
    // submitted input echoed back by validation errors is ignored.
    function extractErrorDetail(data) {

        if (!data || typeof data !== "object") {

            return null;

        }

        const detail = data.detail;

        if (typeof detail === "string" && detail.trim()) {

            return truncateMessage(detail);

        }

        if (Array.isArray(detail)) {

            const messages = detail
                .map(item =>
                    item && typeof item.msg === "string"
                        ? item.msg.trim()
                        : ""
                )
                .filter(Boolean)
                .slice(0, 5);

            if (messages.length) {

                return truncateMessage(messages.join(". "));

            }

        }

        return null;

    }


    // Build a safe, user-facing error message for a failed response.
    async function getApiErrorMessage(response, fallbackMessage) {

        const fallback = fallbackMessage || MESSAGES.generic;

        const detail = extractErrorDetail(
            await readJsonSafely(response)
        );

        if (detail) {

            return detail;

        }

        if (response.status === 403) {

            return MESSAGES.forbidden;

        }

        if (response.status >= 500) {

            return fallbackMessage || MESSAGES.serverError;

        }

        return fallback;

    }


    // Throw an ApiError when a response is unsuccessful.
    async function checkApiResponse(response, fallbackMessage) {

        if (response.ok) {

            return;

        }

        const message = await getApiErrorMessage(
            response,
            fallbackMessage
        );

        throw new ApiError(message, {
            status: response.status,
            kind: response.status === 403 ? "forbidden" : "api"
        });

    }


    // Parse a successful response body as JSON.
    // Returns null for empty bodies (e.g. HTTP 204).
    async function readJsonResponse(response) {

        let text;

        try {

            text = await response.text();

        } catch {

            throw new ApiError(MESSAGES.unreadableResponse, {
                status: response.status,
                kind: "parse"
            });

        }

        if (!text) {

            return null;

        }

        try {

            return JSON.parse(text);

        } catch {

            throw new ApiError(MESSAGES.unreadableResponse, {
                status: response.status,
                kind: "parse"
            });

        }

    }


    // =========================================
    // NETWORK REQUESTS
    // =========================================

    // fetch() wrapper that turns network failures into a readable
    // ApiError. Used for both public and authenticated requests.
    async function safeFetch(url, options = {}) {

        try {

            return await window.fetch(url, options);

        } catch {

            throw new ApiError(MESSAGES.network, {
                status: 0,
                kind: "network"
            });

        }

    }


    // Perform a bearer-authenticated request.
    //
    // - No stored token: redirect to login.
    // - 401: clear the invalid session and redirect to login.
    // - 403: returned to the caller unchanged. The user is still
    //   authenticated, so the session is kept.
    async function authenticatedFetch(url, options = {}) {

        const token = getAccessToken();

        if (!token) {

            redirectToLogin();

            throw new ApiError(MESSAGES.authenticationRequired, {
                status: 401,
                kind: "auth"
            });

        }

        const headers = new Headers(options.headers || {});

        headers.set("Authorization", `Bearer ${token}`);

        const response = await safeFetch(url, {
            ...options,
            headers
        });

        if (response.status === 401) {

            redirectToLogin();

            throw new ApiError(MESSAGES.sessionExpired, {
                status: 401,
                kind: "auth"
            });

        }

        return response;

    }


    // Authenticated request that checks the status and parses JSON.
    async function apiRequest(url, options = {}, fallbackMessage) {

        const response = await authenticatedFetch(url, options);

        await checkApiResponse(response, fallbackMessage);

        return readJsonResponse(response);

    }


    // =========================================
    // CURRENT USER
    // =========================================

    function validateUser(user) {

        if (
            !user ||
            typeof user !== "object" ||
            typeof user.name !== "string" ||
            typeof user.role !== "string"
        ) {

            throw new ApiError(
                "The server returned an invalid profile response.",
                { kind: "parse" }
            );

        }

        return Object.freeze({ ...user });

    }


    // Load the authenticated user's profile from /api/auth/me.
    //
    // The request is made at most once per page load; concurrent
    // callers share the same promise. A failed request is not cached,
    // so a later call can retry. Pass { force: true } to refetch.
    function loadCurrentUser({ force = false } = {}) {

        if (currentUserPromise && !force) {

            return currentUserPromise;

        }

        const request = apiRequest(
            "/api/auth/me",
            {},
            "Unable to retrieve your profile."
        ).then(validateUser);

        currentUserPromise = request;

        request.catch(() => {

            if (currentUserPromise === request) {

                currentUserPromise = null;

            }

        });

        return request;

    }


    // =========================================
    // ROLE HELPERS
    // =========================================

    function hasRole(user, ...roles) {

        return Boolean(
            user &&
            typeof user.role === "string" &&
            roles.includes(user.role)
        );

    }


    function isAdmin(user) {

        return hasRole(user, ROLES.ADMIN);

    }


    function isAgent(user) {

        return hasRole(user, ROLES.AGENT);

    }


    function isCustomer(user) {

        return hasRole(user, ROLES.CUSTOMER);

    }


    function isStaff(user) {

        return hasRole(user, ...STAFF_ROLES);

    }


    function isKnownRole(role) {

        return KNOWN_ROLES.includes(role);

    }


    function formatRole(role) {

        return String(role || "")
            .replace(/_/g, " ")
            .replace(/\b\w/g, character => character.toUpperCase());

    }


    // =========================================
    // ROLE-AWARE VISIBILITY
    // =========================================

    // Show elements marked with data-visible-roles only when the
    // user's role is listed. Elements start hidden in the HTML
    // (class "d-none"), so nothing privileged is shown before the
    // role is confirmed, or when the profile cannot be loaded.
    function applyRoleVisibility(user, root = document) {

        root.querySelectorAll(`[${VISIBLE_ROLES_ATTRIBUTE}]`)
            .forEach(element => {

                const allowedRoles = element
                    .getAttribute(VISIBLE_ROLES_ATTRIBUTE)
                    .split(/\s+/)
                    .filter(Boolean);

                const visible = hasRole(user, ...allowedRoles);

                element.classList.toggle("d-none", !visible);

            });

    }


    // =========================================
    // PROFILE DISPLAY
    // =========================================

    function setTextById(elementId, value) {

        const element = document.getElementById(elementId);

        if (element) {

            element.textContent = value;

        }

    }


    function getInitials(name) {

        return String(name || "")
            .trim()
            .split(/\s+/)
            .slice(0, 2)
            .map(part => part.charAt(0))
            .join("")
            .toUpperCase();

    }


    // Fill the shared top-bar profile elements.
    function renderUserProfile(user) {

        const name = user.name.trim();

        setTextById("agentName", name);

        setTextById("agentRole", formatRole(user.role));

        setTextById("agentAvatar", getInitials(name) || "--");

    }


    // Replace the loading placeholders when the profile is unavailable,
    // without implying any particular role.
    function renderProfileUnavailable() {

        setTextById("agentName", "Profile unavailable");

        setTextById("agentRole", "");

        setTextById("agentAvatar", "--");

    }


    // Load the current user, render the profile and apply role-aware
    // navigation. Resolves to the user, or null when the profile could
    // not be loaded (in which case onError receives a safe message,
    // except for authentication failures, which redirect to login).
    async function initialiseUserInterface({ onError } = {}) {

        try {

            const user = await loadCurrentUser();

            renderUserProfile(user);

            applyRoleVisibility(user);

            return user;

        } catch (error) {

            applyRoleVisibility(null);

            if (!isAuthenticationError(error)) {

                renderProfileUnavailable();

                if (typeof onError === "function") {

                    onError(error.message || MESSAGES.generic);

                }

            }

            return null;

        }

    }


    // =========================================
    // LOGOUT
    // =========================================

    // Revoke the current token on the server, then clear the session.
    // Throws an ApiError if the server does not confirm the logout,
    // leaving the session in place (existing behaviour).
    async function logout() {

        const response = await authenticatedFetch(
            "/api/auth/logout",
            { method: "POST" }
        );

        await checkApiResponse(
            response,
            "Unable to complete logout. Please try again."
        );

        redirectToLogin();

    }


    // Wire a logout button with loading state and error reporting.
    function bindLogoutButton(button, onError) {

        if (!button) {

            return;

        }

        const originalLabel = button.textContent.trim() || "Logout";

        button.addEventListener("click", async () => {

            button.disabled = true;

            button.textContent = "Logging out...";

            try {

                await logout();

            } catch (error) {

                if (isAuthenticationError(error)) {

                    // Already redirecting to the login page.
                    return;

                }

                if (typeof onError === "function") {

                    onError(
                        error.message ||
                        "Unable to complete logout. Please try again."
                    );

                }

                button.disabled = false;

                button.textContent = originalLabel;

            }

        });

    }


    // Redirect to login when there is no stored token.
    // Returns true when the page may continue loading.
    function requireAuthentication() {

        if (getAccessToken()) {

            return true;

        }

        redirectToLogin();

        return false;

    }


    // =========================================
    // PUBLIC API
    // =========================================

    window.ResolveAI = Object.freeze({
        TOKEN_KEY,
        ROLES,
        STAFF_ROLES,
        MESSAGES,

        ApiError,
        isAuthenticationError,
        isForbiddenError,
        isNetworkError,

        getAccessToken,
        setAccessToken,
        clearAccessToken,
        redirectToLogin,
        requireAuthentication,

        safeFetch,
        authenticatedFetch,
        apiRequest,
        checkApiResponse,
        readJsonResponse,
        readJsonSafely,
        getApiErrorMessage,
        extractErrorDetail,

        loadCurrentUser,
        hasRole,
        isAdmin,
        isAgent,
        isCustomer,
        isStaff,
        isKnownRole,
        formatRole,

        applyRoleVisibility,
        renderUserProfile,
        initialiseUserInterface,

        logout,
        bindLogoutButton
    });

})(window);
