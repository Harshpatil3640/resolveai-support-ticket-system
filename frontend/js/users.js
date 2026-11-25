"use strict";

// =========================================
// RESOLVEAI - ADMIN USER MANAGEMENT
// =========================================
//
// Admin-only directory of user accounts built on the existing
// /api/admin/users endpoints:
//
//   GET    /api/admin/users               list (search, filters, paging)
//   GET    /api/admin/users/{id}          details
//   POST   /api/admin/users               create
//   PATCH  /api/admin/users/{id}/role     change role
//   PATCH  /api/admin/users/{id}/status   activate / deactivate
//
// The page is only shown once /api/auth/me confirms the admin role, but
// the API remains the authority: every endpoint requires an active
// administrator, and self-modification and last-administrator rules are
// enforced (and reported) by the backend. Every server value is written
// with textContent or DOM creation, never as HTML.

// Shared authentication, request and role helpers (common.js).
const {
    ROLES,
    MESSAGES,
    apiRequest,
    requireAuthentication,
    initialiseUserInterface,
    bindLogoutButton,
    isAdmin,
    isKnownRole,
    formatRole,
    isAuthenticationError,
    isForbiddenError
} = window.ResolveAI;


// =========================================
// CONSTANTS
// =========================================

const USERS_ENDPOINT = "/api/admin/users";

const PAGE_SIZES = Object.freeze([10, 25, 50]);
const DEFAULT_PAGE_SIZE = 10;

const SEARCH_DEBOUNCE_MS = 350;

// Mirrors app.services.user_service (the API validates again).
const MAX_NAME_LENGTH = 100;
const MAX_EMAIL_LENGTH = 255;
const MAX_SEARCH_LENGTH = 200;
const MIN_PASSWORD_LENGTH = 12;
const MAX_PASSWORD_LENGTH = 128;

const ROLE_ORDER = Object.freeze([
    ROLES.ADMIN,
    ROLES.AGENT,
    ROLES.CUSTOMER
]);

const ROLE_DESCRIPTIONS = Object.freeze({
    [ROLES.ADMIN]:
        "Full access, including all tickets, analytics and user management.",
    [ROLES.AGENT]:
        "Can claim, work on and resolve support tickets, and view analytics.",
    [ROLES.CUSTOMER]:
        "Can submit and track their own support requests."
});

const ROLE_BADGE_CLASSES = Object.freeze({
    [ROLES.ADMIN]: "users-role-badge is-admin",
    [ROLES.AGENT]: "users-role-badge is-agent",
    [ROLES.CUSTOMER]: "users-role-badge is-customer"
});

// Six global counts, each read from `total` of a one-row request.
const STAT_QUERIES = Object.freeze([
    { elementId: "statTotal", params: {} },
    { elementId: "statActive", params: { is_active: "true" } },
    { elementId: "statInactive", params: { is_active: "false" } },
    { elementId: "statAdmin", params: { role: ROLES.ADMIN } },
    { elementId: "statAgent", params: { role: ROLES.AGENT } },
    { elementId: "statCustomer", params: { role: ROLES.CUSTOMER } }
]);

const TABLE_COLUMN_COUNT = 6;

const NUMBER_FORMAT = new Intl.NumberFormat("en-IN");


// =========================================
// PAGE STATE
// =========================================

// Authenticated administrator from /api/auth/me.
let currentUser = null;

// Directory query (mirrored in the page URL).
const listState = {
    page: 0,
    pageSize: DEFAULT_PAGE_SIZE,
    search: "",
    role: "",
    isActive: ""
};

// Guards against rendering responses from superseded requests.
let listRequestId = 0;
let statsRequestId = 0;
let detailsRequestId = 0;

let searchDebounce = null;

// Users on the current page, by ID (row actions read from here).
const pageUsers = new Map();

// Row to highlight after the next render (a newly created user).
let highlightUserId = null;

// True while a create / role / status request is in flight. Only one
// change is submitted at a time, and modals cannot be closed meanwhile.
let mutationPending = false;

// User shown in the details modal.
let detailsUser = null;

// Pending role or status change:
// { mode: "role" | "status", user, step: "select" | "confirm",
//   newRole, newActive }
let accountAction = null;

// Once access is refused, no further admin requests are made.
let accessRevoked = false;


// =========================================
// DOM ELEMENTS
// =========================================

const byId = id => document.getElementById(id);

const pageAlert = byId("usersPageAlert");
const accessChecking = byId("accessChecking");
const accessDenied = byId("accessDenied");
const accessDeniedMessage = byId("accessDeniedMessage");
const userManagement = byId("userManagement");
const logoutButton = byId("logoutButton");

const statsAlert = byId("statsAlert");

const searchInput = byId("userSearchInput");
const roleFilter = byId("roleFilter");
const statusFilter = byId("statusFilter");
const clearFiltersButton = byId("clearFiltersButton");

const usersListAlert = byId("usersListAlert");
const usersTableBody = byId("usersTableBody");
const usersTable = usersTableBody.closest("table");
const userResultCount = byId("userResultCount");
const paginationInfo = byId("paginationInfo");
const pageSizeSelect = byId("pageSizeSelect");
const previousPageButton = byId("previousPageButton");
const nextPageButton = byId("nextPageButton");

// Create user.
const createUserButton = byId("createUserButton");
const createUserModalElement = byId("createUserModal");
const createUserForm = byId("createUserForm");
const createUserAlert = byId("createUserAlert");
const newUserName = byId("newUserName");
const newUserEmail = byId("newUserEmail");
const newUserPassword = byId("newUserPassword");
const newUserRole = byId("newUserRole");
const newUserRoleHelp = byId("newUserRoleHelp");
const togglePasswordButton = byId("togglePasswordButton");
const submitCreateUserButton = byId("submitCreateUserButton");

// User details.
const userDetailsModalElement = byId("userDetailsModal");
const userDetailsAlert = byId("userDetailsAlert");
const userDetailsLoading = byId("userDetailsLoading");
const userDetailsContent = byId("userDetailsContent");
const detailSelfNote = byId("detailSelfNote");
const detailChangeRoleButton = byId("detailChangeRoleButton");
const detailStatusButton = byId("detailStatusButton");

// Role / status changes.
const accountActionModalElement = byId("accountActionModal");
const accountActionTitle = byId("accountActionModalLabel");
const accountActionAlert = byId("accountActionAlert");
const accountActionCloseButton = byId("accountActionCloseButton");
const accountActionBackButton = byId("accountActionBackButton");
const accountActionPrimaryButton = byId("accountActionPrimaryButton");
const actionAvatar = byId("actionAvatar");
const actionName = byId("actionName");
const actionEmail = byId("actionEmail");
const actionBadges = byId("actionBadges");
const roleSelectStep = byId("roleSelectStep");
const newRoleSelect = byId("newRoleSelect");
const confirmStep = byId("confirmStep");
const confirmSummary = byId("confirmSummary");
const confirmConsequences = byId("confirmConsequences");

const toastContainer = byId("toastContainer");


// =========================================
// GENERIC HELPERS
// =========================================

function getModal(element) {

    return bootstrap.Modal.getOrCreateInstance(element);

}


// Safe, user-facing text for a failed request. Server errors never
// show backend text; other API messages are the backend's fixed
// `detail` strings (common.js never includes submitted values).
function describeError(error, fallback) {

    if (error && error.status >= 500) {

        return MESSAGES.serverError;

    }

    return (error && error.message) || fallback || MESSAGES.generic;

}


// Fill an alert with text, plus an optional action button.
function setAlert(element, message, type = "danger", action = null) {

    element.replaceChildren();

    element.className = `alert alert-${type} dashboard-alert`;

    const text = document.createElement("span");

    text.textContent = message;

    element.appendChild(text);

    if (action) {

        const button = document.createElement("button");

        button.type = "button";
        button.className = "btn btn-sm btn-outline-dark dashboard-retry-button";
        button.textContent = action.label || "Retry";

        button.addEventListener("click", action.onClick);

        element.appendChild(button);

    }

}


function hideAlert(element) {

    element.replaceChildren();

    element.className = "alert d-none";

}


function getInitials(name) {

    return String(name || "")
        .trim()
        .split(/\s+/)
        .slice(0, 2)
        .map(part => [...part][0] || "")
        .join("")
        .toUpperCase() || "?";

}


// Length in Unicode code points (as counted by the Python backend).
function codePointLength(value) {

    return [...value].length;

}


// The API returns UTC timestamps; values without an explicit offset
// are treated as UTC so they display correctly in local time.
function parseApiDate(value) {

    if (typeof value !== "string" || !value) {

        return null;

    }

    const hasTimezone = /(?:[zZ]|[+-]\d{2}:?\d{2})$/.test(value);

    const date = new Date(hasTimezone ? value : `${value}Z`);

    return Number.isNaN(date.getTime()) ? null : date;

}


function formatDate(value) {

    const date = parseApiDate(value);

    return date
        ? date.toLocaleDateString("en-IN", {
            day: "2-digit",
            month: "short",
            year: "numeric"
        })
        : "—";

}


function formatDateTime(value) {

    const date = parseApiDate(value);

    return date
        ? date.toLocaleString("en-IN", {
            day: "2-digit",
            month: "short",
            year: "numeric",
            hour: "2-digit",
            minute: "2-digit"
        })
        : "Unknown";

}


function formatCount(value) {

    return NUMBER_FORMAT.format(value);

}


function pluralise(count, singular, plural = `${singular}s`) {

    return `${formatCount(count)} ${count === 1 ? singular : plural}`;

}


function roleLabel(role) {

    return isKnownRole(role) ? formatRole(role) : "Unknown role";

}


function isSelf(user) {

    return Boolean(
        user &&
        currentUser &&
        Number.isSafeInteger(currentUser.id) &&
        user.id === currentUser.id
    );

}


// =========================================
// RESPONSE VALIDATION
// =========================================

function isValidUser(user) {

    return Boolean(
        user &&
        typeof user === "object" &&
        Number.isSafeInteger(user.id) &&
        user.id > 0 &&
        typeof user.name === "string" &&
        typeof user.email === "string" &&
        typeof user.role === "string" &&
        typeof user.is_active === "boolean"
    );

}


// Keep only the public UserRead fields.
function toUserRecord(user) {

    if (!isValidUser(user)) {

        throw new Error("The server returned an invalid user record.");

    }

    return Object.freeze({
        id: user.id,
        name: user.name,
        email: user.email,
        role: user.role,
        is_active: user.is_active,
        created_at: typeof user.created_at === "string"
            ? user.created_at
            : null
    });

}


function parseUserList(data) {

    if (
        !data ||
        typeof data !== "object" ||
        !Array.isArray(data.items) ||
        !Number.isSafeInteger(data.total) ||
        data.total < 0
    ) {

        throw new Error("The server returned an invalid user list.");

    }

    return {
        items: data.items.map(toUserRecord),
        total: data.total
    };

}


// =========================================
// BADGES AND AVATARS
// =========================================

function createRoleBadge(role) {

    const badge = document.createElement("span");

    badge.className =
        ROLE_BADGE_CLASSES[role] || "users-role-badge is-unknown";

    badge.textContent = roleLabel(role);

    return badge;

}


function createStatusBadge(isActive) {

    const badge = document.createElement("span");

    badge.className = isActive
        ? "users-status-badge is-active"
        : "users-status-badge is-inactive";

    const dot = document.createElement("span");

    dot.className = "users-status-dot";
    dot.setAttribute("aria-hidden", "true");

    badge.append(dot, isActive ? "Active" : "Inactive");

    return badge;

}


function createSelfBadge() {

    const badge = document.createElement("span");

    badge.className = "users-self-badge";
    badge.textContent = "You";

    return badge;

}


function fillAvatar(element, user) {

    element.textContent = getInitials(user.name);

    element.className = element.className
        .split(/\s+/)
        .filter(name => name && !name.startsWith("role-"))
        .concat(`role-${isKnownRole(user.role) ? user.role : "unknown"}`)
        .join(" ");

}


// =========================================
// TOAST NOTIFICATIONS
// =========================================

function showToast(message, type = "success") {

    const toast = document.createElement("div");

    toast.className =
        `toast align-items-center border-0 users-toast is-${type}`;
    toast.setAttribute("role", type === "danger" ? "alert" : "status");
    toast.setAttribute("aria-live", type === "danger" ? "assertive" : "polite");
    toast.setAttribute("aria-atomic", "true");

    const wrapper = document.createElement("div");

    wrapper.className = "d-flex";

    const body = document.createElement("div");

    body.className = "toast-body";
    body.textContent = message;

    const close = document.createElement("button");

    close.type = "button";
    close.className = "btn-close me-2 m-auto";
    close.setAttribute("data-bs-dismiss", "toast");
    close.setAttribute("aria-label", "Close");

    wrapper.append(body, close);
    toast.appendChild(wrapper);
    toastContainer.appendChild(toast);

    toast.addEventListener("hidden.bs.toast", () => toast.remove());

    bootstrap.Toast.getOrCreateInstance(toast, { delay: 6000 }).show();

}


// =========================================
// ACCESS STATES
// =========================================

function showPageAlert(message, type = "danger", action = null) {

    setAlert(pageAlert, message, type, action);

}


// Show the access-denied panel and stop all admin requests. Used for
// non-admin users and whenever the API answers 403 (e.g. the admin's
// role was changed in another session).
function showAccessDenied(message) {

    accessRevoked = true;

    clearTimeout(searchDebounce);

    accessChecking.classList.add("d-none");
    userManagement.classList.add("d-none");
    accessDenied.classList.remove("d-none");

    accessDeniedMessage.textContent = message ||
        "User Management is available to administrators only.";

    // Close any open dialog: its actions are no longer permitted.
    mutationPending = false;

    [createUserModalElement, userDetailsModalElement, accountActionModalElement]
        .forEach(element => bootstrap.Modal.getInstance(element)?.hide());

}


// Handle errors every request shares. Returns true when handled.
function handleAccessError(error) {

    if (isAuthenticationError(error)) {

        // common.js is already redirecting to the login page.
        return true;

    }

    if (isForbiddenError(error)) {

        showAccessDenied(
            "Your account no longer has administrator access. " +
            "User Management is available to administrators only."
        );

        return true;

    }

    return false;

}


// =========================================
// URL STATE
// =========================================

// Restore search, filters and page from the URL (validated).
function applyStateFromUrl() {

    const params = new URLSearchParams(window.location.search);

    const search = (params.get("search") || "").trim();

    if (search && search.length <= MAX_SEARCH_LENGTH) {
        listState.search = search;
    }

    const role = params.get("role");

    if (isKnownRole(role)) {
        listState.role = role;
    }

    const status = params.get("is_active");

    if (status === "true" || status === "false") {
        listState.isActive = status;
    }

    const size = Number(params.get("limit"));

    if (PAGE_SIZES.includes(size)) {
        listState.pageSize = size;
    }

    const page = params.get("page");

    if (page && /^[1-9][0-9]{0,5}$/.test(page)) {
        listState.page = Number(page) - 1;
    }

    searchInput.value = listState.search;
    roleFilter.value = listState.role;
    statusFilter.value = listState.isActive;
    pageSizeSelect.value = String(listState.pageSize);

}


function syncUrl() {

    const params = new URLSearchParams();

    if (listState.search) {
        params.set("search", listState.search);
    }

    if (listState.role) {
        params.set("role", listState.role);
    }

    if (listState.isActive) {
        params.set("is_active", listState.isActive);
    }

    if (listState.pageSize !== DEFAULT_PAGE_SIZE) {
        params.set("limit", String(listState.pageSize));
    }

    if (listState.page > 0) {
        params.set("page", String(listState.page + 1));
    }

    const query = params.toString();

    const url = `${window.location.pathname}${query ? `?${query}` : ""}`;

    try {

        window.history.replaceState(null, "", url);

    } catch {

        // URL state is a convenience only.

    }

}


// =========================================
// STATISTICS
// =========================================

function setStatsLoading(loading) {

    STAT_QUERIES.forEach(({ elementId }) => {

        byId(elementId).closest(".stat-card")
            .classList.toggle("is-loading", loading);

    });

}


async function loadStats() {

    if (accessRevoked) {
        return;
    }

    const requestId = ++statsRequestId;

    setStatsLoading(true);

    try {

        const totals = await Promise.all(
            STAT_QUERIES.map(async ({ params }) => {

                const query = new URLSearchParams({
                    ...params,
                    skip: "0",
                    limit: "1"
                });

                const data = await apiRequest(
                    `${USERS_ENDPOINT}?${query.toString()}`,
                    {},
                    "Unable to load account statistics."
                );

                return parseUserList(data).total;

            })
        );

        if (requestId !== statsRequestId) {
            return;
        }

        hideAlert(statsAlert);

        STAT_QUERIES.forEach(({ elementId }, index) => {

            byId(elementId).textContent = formatCount(totals[index]);

        });

    } catch (error) {

        if (requestId !== statsRequestId || handleAccessError(error)) {
            return;
        }

        STAT_QUERIES.forEach(({ elementId }) => {

            byId(elementId).textContent = "--";

        });

        setAlert(
            statsAlert,
            describeError(error, "Unable to load account statistics."),
            "warning",
            { label: "Retry", onClick: loadStats }
        );

    } finally {

        if (requestId === statsRequestId) {
            setStatsLoading(false);
        }

    }

}


// =========================================
// USER DIRECTORY
// =========================================

function hasActiveFilters() {

    return Boolean(
        listState.search ||
        listState.role ||
        listState.isActive
    );

}


function buildListQuery() {

    const params = new URLSearchParams({
        skip: String(listState.page * listState.pageSize),
        limit: String(listState.pageSize)
    });

    // The API rejects an empty search, so a blank one is omitted.
    if (listState.search) {
        params.set("search", listState.search);
    }

    if (listState.role) {
        params.set("role", listState.role);
    }

    if (listState.isActive) {
        params.set("is_active", listState.isActive);
    }

    return params.toString();

}


function renderTableMessage(message, { loading = false, action = null } = {}) {

    const row = document.createElement("tr");

    const cell = document.createElement("td");

    cell.colSpan = TABLE_COLUMN_COUNT;
    cell.className = "empty-state users-table-message";

    if (loading) {

        const spinner = document.createElement("span");

        spinner.className = "spinner-border spinner-border-sm me-2";
        spinner.setAttribute("aria-hidden", "true");

        cell.appendChild(spinner);

    }

    const text = document.createElement("span");

    text.textContent = message;

    cell.appendChild(text);

    if (action) {

        const button = document.createElement("button");

        button.type = "button";
        button.className = "btn btn-sm btn-outline-primary users-table-message-action";
        button.textContent = action.label;
        button.addEventListener("click", action.onClick);

        const wrapper = document.createElement("div");

        wrapper.appendChild(button);

        cell.appendChild(wrapper);

    }

    row.appendChild(cell);

    usersTableBody.replaceChildren(row);

}


function createCell(label, content, className = "") {

    const cell = document.createElement("td");

    cell.dataset.label = label;

    if (className) {
        cell.className = className;
    }

    if (content instanceof Node) {

        cell.appendChild(content);

    } else {

        cell.textContent = content;

    }

    return cell;

}


function createActionButton(label, className, ariaLabel, onClick) {

    const button = document.createElement("button");

    button.type = "button";
    button.className = `btn btn-sm ${className}`;
    button.textContent = label;
    button.setAttribute("aria-label", ariaLabel);

    button.addEventListener("click", onClick);

    return button;

}


function createUserRow(user) {

    const row = document.createElement("tr");

    row.dataset.userId = String(user.id);

    if (!user.is_active) {
        row.classList.add("is-inactive-user");
    }

    if (user.id === highlightUserId) {
        row.classList.add("users-row-highlight");
    }

    const self = isSelf(user);


    // User: avatar + name (+ "You").
    const identity = document.createElement("div");

    identity.className = "users-identity";

    const avatar = document.createElement("div");

    avatar.className = "users-avatar";
    avatar.setAttribute("aria-hidden", "true");
    fillAvatar(avatar, user);

    const nameWrapper = document.createElement("div");

    nameWrapper.className = "users-identity-text";

    const name = document.createElement("span");

    name.className = "users-name";
    name.textContent = user.name;

    nameWrapper.appendChild(name);

    if (self) {
        nameWrapper.appendChild(createSelfBadge());
    }

    identity.append(avatar, nameWrapper);

    row.appendChild(createCell("User", identity, "users-cell-identity"));


    // Email.
    const email = document.createElement("span");

    email.className = "users-email";
    email.textContent = user.email;

    row.appendChild(createCell("Email", email));


    // Role and status.
    row.appendChild(createCell("Role", createRoleBadge(user.role)));
    row.appendChild(createCell("Status", createStatusBadge(user.is_active)));


    // Created.
    row.appendChild(createCell("Created", formatDate(user.created_at)));


    // Actions.
    const actions = document.createElement("div");

    actions.className = "users-row-actions";

    actions.appendChild(
        createActionButton(
            "View",
            "btn-outline-primary",
            `View details for ${user.name}`,
            () => openUserDetails(user.id)
        )
    );

    const roleButton = createActionButton(
        "Role",
        "btn-outline-secondary",
        `Change role for ${user.name}`,
        () => openRoleChange(user)
    );

    const statusButton = createActionButton(
        user.is_active ? "Deactivate" : "Activate",
        user.is_active ? "btn-outline-danger" : "btn-outline-success",
        `${user.is_active ? "Deactivate" : "Activate"} account for ${user.name}`,
        () => openStatusChange(user)
    );

    if (self) {

        // The backend also refuses these (409); the UI does not offer them.
        roleButton.disabled = true;
        statusButton.disabled = true;

        const note = "You cannot change your own role or account status.";

        const roleWrapper = document.createElement("span");
        roleWrapper.title = note;
        roleWrapper.className = "d-inline-block";
        roleWrapper.appendChild(roleButton);

        const statusWrapper = document.createElement("span");
        statusWrapper.title = note;
        statusWrapper.className = "d-inline-block";
        statusWrapper.appendChild(statusButton);

        actions.append(roleWrapper, statusWrapper);

    } else {

        actions.append(roleButton, statusButton);

    }

    row.appendChild(createCell("Actions", actions, "text-end users-cell-actions"));

    return row;

}


function renderUsers(users) {

    pageUsers.clear();

    users.forEach(user => pageUsers.set(user.id, user));

    if (!users.length) {

        if (hasActiveFilters()) {

            renderTableMessage(
                "No users match your search or filters.",
                {
                    action: {
                        label: "Clear filters",
                        onClick: clearFilters
                    }
                }
            );

        } else {

            renderTableMessage("No user accounts have been created yet.");

        }

        highlightUserId = null;

        return;

    }

    usersTableBody.replaceChildren(...users.map(createUserRow));

    if (highlightUserId !== null) {

        usersTableBody
            .querySelector(".users-row-highlight")
            ?.scrollIntoView({ block: "nearest", behavior: "smooth" });

        highlightUserId = null;

    }

}


function setPaginationDisabled() {

    previousPageButton.disabled = true;
    nextPageButton.disabled = true;

}


function updatePagination(count, total) {

    const { page, pageSize } = listState;

    const totalPages = Math.max(1, Math.ceil(total / pageSize));

    const start = count ? page * pageSize + 1 : 0;
    const end = page * pageSize + count;

    userResultCount.textContent = total
        ? `Showing ${formatCount(start)}–${formatCount(end)} of ` +
          `${pluralise(total, hasActiveFilters() ? "matching user" : "user")}`
        : hasActiveFilters()
            ? "No matching users"
            : "No users";

    paginationInfo.textContent =
        `Page ${formatCount(page + 1)} of ${formatCount(totalPages)}`;

    previousPageButton.disabled = page === 0;
    nextPageButton.disabled = page >= totalPages - 1;

}


// Load the current page. With keepRows, the existing rows stay visible
// (dimmed) while refreshing after a change, instead of a loading row.
async function loadUsers({ keepRows = false } = {}) {

    if (accessRevoked) {
        return;
    }

    const requestId = ++listRequestId;

    syncUrl();

    hideAlert(usersListAlert);

    setPaginationDisabled();

    usersTable.setAttribute("aria-busy", "true");

    if (keepRows && pageUsers.size) {

        usersTable.classList.add("is-refreshing");

    } else {

        userResultCount.textContent = "Loading users...";

        renderTableMessage("Loading users...", { loading: true });

    }

    try {

        const data = await apiRequest(
            `${USERS_ENDPOINT}?${buildListQuery()}`,
            {},
            "Unable to load users."
        );

        // A newer search, filter or page request has started.
        if (requestId !== listRequestId) {
            return;
        }

        const { items, total } = parseUserList(data);

        // The page no longer exists (e.g. the last user on it was
        // filtered out by a status change): show the last page instead.
        const lastPage = Math.max(0, Math.ceil(total / listState.pageSize) - 1);

        if (listState.page > lastPage) {

            listState.page = lastPage;

            await loadUsers({ keepRows });

            return;

        }

        const users = items.slice(0, listState.pageSize);

        renderUsers(users);

        updatePagination(users.length, total);

    } catch (error) {

        if (requestId !== listRequestId || handleAccessError(error)) {
            return;
        }

        pageUsers.clear();

        const message = describeError(error, "Unable to load users.");

        setAlert(usersListAlert, message, "danger", {
            label: "Retry",
            onClick: () => loadUsers()
        });

        renderTableMessage("Users could not be loaded.");

        userResultCount.textContent = "Unable to load users.";

        paginationInfo.textContent = `Page ${formatCount(listState.page + 1)}`;

        previousPageButton.disabled = listState.page === 0;
        nextPageButton.disabled = true;

    } finally {

        if (requestId === listRequestId) {

            usersTable.removeAttribute("aria-busy");
            usersTable.classList.remove("is-refreshing");

        }

    }

}


// Refresh the list and statistics after a successful change.
function refreshAfterChange() {

    loadUsers({ keepRows: true });

    loadStats();

}


// =========================================
// SEARCH, FILTERS AND PAGINATION EVENTS
// =========================================

function applySearch() {

    clearTimeout(searchDebounce);

    const search = searchInput.value.trim().slice(0, MAX_SEARCH_LENGTH);

    // Unchanged (e.g. only whitespace was typed): no request.
    if (search === listState.search) {
        return;
    }

    listState.search = search;
    listState.page = 0;

    loadUsers();

}


function clearFilters() {

    clearTimeout(searchDebounce);

    const changed = hasActiveFilters() || listState.page !== 0;

    searchInput.value = "";
    roleFilter.value = "";
    statusFilter.value = "";

    listState.search = "";
    listState.role = "";
    listState.isActive = "";
    listState.page = 0;

    if (changed) {
        loadUsers();
    }

}


searchInput.addEventListener("input", () => {

    clearTimeout(searchDebounce);

    searchDebounce = setTimeout(applySearch, SEARCH_DEBOUNCE_MS);

});


// Enter searches immediately.
searchInput.addEventListener("keydown", event => {

    if (event.key === "Enter") {

        event.preventDefault();

        applySearch();

    }

});


roleFilter.addEventListener("change", () => {

    listState.role = isKnownRole(roleFilter.value) ? roleFilter.value : "";
    listState.page = 0;

    loadUsers();

});


statusFilter.addEventListener("change", () => {

    listState.isActive = ["true", "false"].includes(statusFilter.value)
        ? statusFilter.value
        : "";
    listState.page = 0;

    loadUsers();

});


pageSizeSelect.addEventListener("change", () => {

    const size = Number(pageSizeSelect.value);

    listState.pageSize = PAGE_SIZES.includes(size) ? size : DEFAULT_PAGE_SIZE;
    listState.page = 0;

    loadUsers();

});


clearFiltersButton.addEventListener("click", clearFilters);


previousPageButton.addEventListener("click", () => {

    if (listState.page > 0) {

        listState.page--;

        loadUsers();

    }

});


nextPageButton.addEventListener("click", () => {

    if (!nextPageButton.disabled) {

        listState.page++;

        loadUsers();

    }

});


// =========================================
// CREATE USER
// =========================================

const createFields = Object.freeze({
    name: newUserName,
    email: newUserEmail,
    password: newUserPassword,
    role: newUserRole
});


function setFieldError(field, message) {

    const feedback = byId(`${field.id}Feedback`);

    field.classList.toggle("is-invalid", Boolean(message));
    field.setAttribute("aria-invalid", message ? "true" : "false");

    if (feedback) {
        feedback.textContent = message || "";
    }

}


function clearCreateErrors() {

    Object.values(createFields).forEach(field => setFieldError(field, ""));

    hideAlert(createUserAlert);

}


// Same rule as the backend: exactly one "@" with text on both sides
// and no whitespace.
function isValidEmail(email) {

    return /^[^\s@]+@[^\s@]+$/.test(email);

}


// Validate the form. Returns the request body, or null (with field
// errors shown) when invalid. The password is never trimmed.
function collectCreatePayload() {

    const name = newUserName.value.trim();
    const email = newUserEmail.value.trim();
    const password = newUserPassword.value;
    const role = newUserRole.value;

    const errors = new Map();

    if (!name) {
        errors.set(newUserName, "Full name is required.");
    } else if (codePointLength(name) > MAX_NAME_LENGTH) {
        errors.set(newUserName, `Name must be at most ${MAX_NAME_LENGTH} characters.`);
    }

    if (!email) {
        errors.set(newUserEmail, "Email is required.");
    } else if (codePointLength(email) > MAX_EMAIL_LENGTH) {
        errors.set(newUserEmail, `Email must be at most ${MAX_EMAIL_LENGTH} characters.`);
    } else if (!isValidEmail(email)) {
        errors.set(newUserEmail, "Enter a valid email address, e.g. name@example.com.");
    }

    const passwordLength = codePointLength(password);

    if (!password) {
        errors.set(newUserPassword, "Password is required.");
    } else if (passwordLength < MIN_PASSWORD_LENGTH) {
        errors.set(
            newUserPassword,
            `Password must contain at least ${MIN_PASSWORD_LENGTH} characters.`
        );
    } else if (passwordLength > MAX_PASSWORD_LENGTH) {
        errors.set(
            newUserPassword,
            `Password must contain at most ${MAX_PASSWORD_LENGTH} characters.`
        );
    }

    if (!role) {
        errors.set(newUserRole, "Select a role.");
    } else if (!ROLE_ORDER.includes(role)) {
        errors.set(newUserRole, "Select a valid role.");
    }

    Object.values(createFields).forEach(field => {
        setFieldError(field, errors.get(field) || "");
    });

    if (errors.size) {

        errors.keys().next().value.focus();

        return null;

    }

    return { name, email, password, role };

}


function setCreatePending(pending) {

    mutationPending = pending;

    submitCreateUserButton.disabled = pending;
    submitCreateUserButton.textContent = pending ? "Creating User..." : "Create User";

    createUserForm
        .querySelectorAll("input, select, button")
        .forEach(element => {

            if (element !== submitCreateUserButton) {
                element.disabled = pending;
            }

        });

}


function resetCreateForm() {

    createUserForm.reset();

    newUserPassword.type = "password";
    togglePasswordButton.textContent = "Show";
    togglePasswordButton.setAttribute("aria-pressed", "false");

    newUserRoleHelp.textContent = "Select a role to see what it can access.";

    clearCreateErrors();

}


// Whether a user appears under the current search and filters.
function matchesCurrentFilters(user) {

    if (listState.role && user.role !== listState.role) {
        return false;
    }

    if (listState.isActive && String(user.is_active) !== listState.isActive) {
        return false;
    }

    if (listState.search) {

        const term = listState.search.toLowerCase();

        return user.name.toLowerCase().includes(term) ||
            user.email.toLowerCase().includes(term);

    }

    return true;

}


async function submitCreateUser(event) {

    event.preventDefault();

    if (mutationPending || accessRevoked) {
        return;
    }

    hideAlert(createUserAlert);

    const payload = collectCreatePayload();

    if (!payload) {
        return;
    }

    setCreatePending(true);

    let created;

    try {

        created = toUserRecord(
            await apiRequest(
                USERS_ENDPOINT,
                {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                },
                "Unable to create the user."
            )
        );

    } catch (error) {

        setCreatePending(false);

        if (handleAccessError(error)) {
            return;
        }

        if (error.status === 409) {

            setFieldError(newUserEmail, error.message ||
                "An account with this email already exists.");

            newUserEmail.focus();

            return;

        }

        setAlert(
            createUserAlert,
            error.status === 422
                ? `Please check the details: ${error.message}`
                : describeError(error, "Unable to create the user."),
            error.status === 422 ? "warning" : "danger"
        );

        return;

    }

    setCreatePending(false);

    // The modal's hidden handler clears the form, including the password.
    getModal(createUserModalElement).hide();

    // Newest users are listed first: show page 1, clearing filters that
    // would hide the new account.
    const visible = matchesCurrentFilters(created);

    if (!visible) {

        searchInput.value = "";
        roleFilter.value = "";
        statusFilter.value = "";

        listState.search = "";
        listState.role = "";
        listState.isActive = "";

    }

    listState.page = 0;
    highlightUserId = created.id;

    showToast(
        `Account created for ${created.name} (${roleLabel(created.role)}).` +
        (visible ? "" : " Filters were cleared so the new account is visible.")
    );

    loadUsers();
    loadStats();

}


createUserButton.addEventListener("click", () => {

    if (mutationPending || accessRevoked) {
        return;
    }

    resetCreateForm();

    getModal(createUserModalElement).show();

});


createUserForm.addEventListener("submit", submitCreateUser);


// Clear a field's error as soon as it is edited.
Object.values(createFields).forEach(field => {

    field.addEventListener(
        field.tagName === "SELECT" ? "change" : "input",
        () => setFieldError(field, "")
    );

});


newUserRole.addEventListener("change", () => {

    newUserRoleHelp.textContent = ROLE_DESCRIPTIONS[newUserRole.value] ||
        "Select a role to see what it can access.";

});


togglePasswordButton.addEventListener("click", () => {

    const show = newUserPassword.type === "password";

    newUserPassword.type = show ? "text" : "password";

    togglePasswordButton.textContent = show ? "Hide" : "Show";
    togglePasswordButton.setAttribute("aria-pressed", String(show));

});


createUserModalElement.addEventListener("shown.bs.modal", () => {

    newUserName.focus();

});


// Never leave the submitted password in the page once the dialog closes.
createUserModalElement.addEventListener("hidden.bs.modal", resetCreateForm);


// =========================================
// USER DETAILS
// =========================================

function setDetailsActions(user) {

    const manageable = Boolean(user) && !isSelf(user);

    detailChangeRoleButton.disabled = !manageable;
    detailStatusButton.disabled = !manageable;

    const active = user ? user.is_active : true;

    detailStatusButton.textContent = active ? "Deactivate" : "Activate";
    detailStatusButton.className = active
        ? "btn btn-outline-danger"
        : "btn btn-outline-success";

    detailSelfNote.classList.toggle("d-none", !isSelf(user));

}


function populateUserDetails(user) {

    fillAvatar(byId("detailAvatar"), user);

    byId("detailName").textContent = user.name;
    byId("detailEmailHeader").textContent = user.email;
    byId("detailId").textContent = `#${user.id}`;
    byId("detailEmail").textContent = user.email;
    byId("detailRole").replaceChildren(createRoleBadge(user.role));
    byId("detailStatus").replaceChildren(createStatusBadge(user.is_active));
    byId("detailCreated").textContent = formatDateTime(user.created_at);

}


async function loadUserDetails(userId) {

    const requestId = ++detailsRequestId;

    detailsUser = null;

    hideAlert(userDetailsAlert);
    setDetailsActions(null);

    userDetailsContent.classList.add("d-none");
    userDetailsLoading.classList.remove("d-none");

    try {

        const user = toUserRecord(
            await apiRequest(
                `${USERS_ENDPOINT}/${encodeURIComponent(userId)}`,
                {},
                "Unable to load the user's details."
            )
        );

        if (requestId !== detailsRequestId) {
            return;
        }

        detailsUser = user;

        populateUserDetails(user);
        setDetailsActions(user);

        userDetailsContent.classList.remove("d-none");

    } catch (error) {

        if (requestId !== detailsRequestId || handleAccessError(error)) {
            return;
        }

        if (error.status === 404) {

            setAlert(
                userDetailsAlert,
                "This user account no longer exists. The directory has been refreshed.",
                "warning"
            );

            refreshAfterChange();

            return;

        }

        setAlert(
            userDetailsAlert,
            describeError(error, "Unable to load the user's details."),
            "danger",
            { label: "Retry", onClick: () => loadUserDetails(userId) }
        );

    } finally {

        if (requestId === detailsRequestId) {
            userDetailsLoading.classList.add("d-none");
        }

    }

}


function openUserDetails(userId) {

    if (mutationPending || accessRevoked) {
        return;
    }

    getModal(userDetailsModalElement).show();

    loadUserDetails(userId);

}


// Switch from the details dialog to a role / status dialog once the
// details dialog has fully closed (Bootstrap does not stack modals).
function switchFromDetails(openNext) {

    const user = detailsUser;

    if (!user || isSelf(user)) {
        return;
    }

    userDetailsModalElement.addEventListener(
        "hidden.bs.modal",
        () => openNext(user),
        { once: true }
    );

    getModal(userDetailsModalElement).hide();

}


detailChangeRoleButton.addEventListener("click", () => {

    switchFromDetails(openRoleChange);

});


detailStatusButton.addEventListener("click", () => {

    switchFromDetails(openStatusChange);

});


userDetailsModalElement.addEventListener("hidden.bs.modal", () => {

    // Ignore a details response that arrives after closing.
    detailsRequestId++;

});


// =========================================
// ROLE AND STATUS CHANGES
// =========================================

function roleChangeConsequences(user, newRole) {

    const items = [
        "The change takes effect on their next authenticated request. " +
        "They remain signed in."
    ];

    if (user.role === ROLES.AGENT && newRole !== ROLES.AGENT) {

        items.push(
            "Their Open and In Progress tickets will be unassigned and " +
            "returned to the Open queue. Resolved tickets keep their assignee."
        );

        items.push(
            "Unassigned tickets are not reassigned automatically."
        );

    }

    if (newRole === ROLES.ADMIN) {

        items.push(
            "They will gain full administrative access, including user management."
        );

    } else if (user.role === ROLES.ADMIN) {

        items.push(
            "They will lose administrative access. At least one active " +
            "administrator must always remain."
        );

    }

    if (newRole === ROLES.AGENT) {

        items.push("They will be able to claim and work on support tickets.");

    }

    if (newRole === ROLES.CUSTOMER) {

        items.push("They will only see their own support requests.");

    }

    return items;

}


function statusChangeConsequences(user, newActive) {

    if (!newActive) {

        const items = [
            "They will be signed out of every active session immediately " +
            "and will not be able to sign in."
        ];

        if (user.role === ROLES.AGENT) {

            items.push(
                "Their Open and In Progress tickets will be unassigned and " +
                "returned to the Open queue. Resolved tickets keep their assignee."
            );

            items.push(
                "Tickets are not reassigned automatically, and reactivating " +
                "the account later does not restore them."
            );

        }

        if (user.role === ROLES.ADMIN) {

            items.push(
                "They will lose administrative access. At least one active " +
                "administrator must always remain."
            );

        }

        return items;

    }

    const items = [
        "They will be able to sign in again with their existing password.",
        "Any sessions from before the change remain invalid; they will need to sign in again."
    ];

    if (user.role === ROLES.AGENT) {

        items.push(
            "Tickets unassigned when the account was deactivated are not " +
            "reassigned automatically."
        );

    }

    return items;

}


function renderActionTarget(user) {

    fillAvatar(actionAvatar, user);

    actionName.textContent = user.name;
    actionEmail.textContent = user.email;

    actionBadges.replaceChildren(
        createRoleBadge(user.role),
        createStatusBadge(user.is_active)
    );

}


function renderConfirmList(items) {

    confirmConsequences.replaceChildren(
        ...items.map(text => {

            const item = document.createElement("li");

            item.textContent = text;

            return item;

        })
    );

}


// Render the action dialog for the current accountAction.
function renderAccountAction() {

    const action = accountAction;

    if (!action) {
        return;
    }

    const { user } = action;

    renderActionTarget(user);

    if (action.mode === "role") {

        accountActionTitle.textContent = "Change Role";

        if (action.step === "select") {

            roleSelectStep.classList.remove("d-none");
            confirmStep.classList.add("d-none");

            accountActionBackButton.textContent = "Cancel";
            accountActionPrimaryButton.textContent = "Review Change";
            accountActionPrimaryButton.className = "btn btn-primary";
            accountActionPrimaryButton.disabled =
                !ROLE_ORDER.includes(newRoleSelect.value) ||
                newRoleSelect.value === user.role;

            return;

        }

        roleSelectStep.classList.add("d-none");
        confirmStep.classList.remove("d-none");

        confirmSummary.replaceChildren(
            "Change ",
            strong(user.name),
            "'s role from ",
            strong(roleLabel(user.role)),
            " to ",
            strong(roleLabel(action.newRole)),
            "?"
        );

        renderConfirmList(roleChangeConsequences(user, action.newRole));

        accountActionBackButton.textContent = "Back";
        accountActionPrimaryButton.textContent = "Confirm Role Change";
        accountActionPrimaryButton.className = "btn btn-primary";
        accountActionPrimaryButton.disabled = false;

        return;

    }

    // Status change: confirmation only.
    const activating = action.newActive;

    accountActionTitle.textContent = activating
        ? "Activate Account"
        : "Deactivate Account";

    roleSelectStep.classList.add("d-none");
    confirmStep.classList.remove("d-none");

    confirmSummary.replaceChildren(
        activating ? "Activate " : "Deactivate ",
        strong(user.name),
        "'s account?"
    );

    renderConfirmList(statusChangeConsequences(user, activating));

    accountActionBackButton.textContent = "Cancel";
    accountActionPrimaryButton.textContent = activating
        ? "Activate Account"
        : "Deactivate Account";
    accountActionPrimaryButton.className = activating
        ? "btn btn-success"
        : "btn btn-danger";
    accountActionPrimaryButton.disabled = false;

}


function strong(text) {

    const element = document.createElement("strong");

    element.textContent = text;

    return element;

}


function openAccountAction(action) {

    if (mutationPending || accessRevoked || isSelf(action.user)) {
        return;
    }

    accountAction = action;

    hideAlert(accountActionAlert);

    renderAccountAction();

    getModal(accountActionModalElement).show();

}


function openRoleChange(user) {

    // Preselect a different role so the select is ready to review.
    newRoleSelect.value = ROLE_ORDER.find(role => role !== user.role);

    [...newRoleSelect.options].forEach(option => {

        option.textContent = option.value === user.role
            ? `${formatRole(option.value)} (current)`
            : formatRole(option.value);

        option.disabled = option.value === user.role;

    });

    openAccountAction({
        mode: "role",
        user,
        step: "select",
        newRole: null,
        newActive: null
    });

}


function openStatusChange(user) {

    openAccountAction({
        mode: "status",
        user,
        step: "confirm",
        newRole: null,
        newActive: !user.is_active
    });

}


function setActionPending(pending) {

    mutationPending = pending;

    accountActionPrimaryButton.disabled = pending;
    accountActionBackButton.disabled = pending;
    accountActionCloseButton.disabled = pending;
    newRoleSelect.disabled = pending;

    if (pending) {

        accountActionPrimaryButton.textContent = "Saving...";

    } else {

        renderAccountAction();

    }

}


async function submitAccountAction() {

    const action = accountAction;

    if (!action || mutationPending || accessRevoked) {
        return;
    }

    const { user } = action;

    const isRoleChange = action.mode === "role";

    const url = `${USERS_ENDPOINT}/${encodeURIComponent(user.id)}/` +
        (isRoleChange ? "role" : "status");

    const body = isRoleChange
        ? { role: action.newRole }
        : { is_active: action.newActive === true };

    hideAlert(accountActionAlert);

    setActionPending(true);

    let updated;

    try {

        updated = toUserRecord(
            await apiRequest(
                url,
                {
                    method: "PATCH",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(body)
                },
                isRoleChange
                    ? "Unable to change the user's role."
                    : "Unable to change the account status."
            )
        );

    } catch (error) {

        if (handleAccessError(error)) {
            return;
        }

        setActionPending(false);

        if (error.status === 404) {

            setAlert(
                accountActionAlert,
                "This user account no longer exists. The directory has been refreshed.",
                "warning"
            );

            accountActionPrimaryButton.disabled = true;

            refreshAfterChange();

            return;

        }

        if (error.status === 409) {

            // Self-modification or last active administrator: the
            // backend's fixed explanation is shown as-is.
            setAlert(
                accountActionAlert,
                `${error.message} No changes were made.`,
                "warning"
            );

            refreshAfterChange();

            return;

        }

        // Not retried automatically: the administrator decides.
        setAlert(
            accountActionAlert,
            describeError(
                error,
                isRoleChange
                    ? "Unable to change the user's role."
                    : "Unable to change the account status."
            ),
            "danger"
        );

        return;

    }

    setActionPending(false);

    getModal(accountActionModalElement).hide();

    if (isRoleChange) {

        showToast(
            `${updated.name} is now ${roleLabel(updated.role)}. ` +
            "The change applies on their next request."
        );

    } else {

        showToast(
            `${updated.name}'s account is now ${updated.is_active ? "Active" : "Inactive"}.` +
            (updated.is_active
                ? " They can sign in again."
                : " They have been signed out of all sessions."),
            updated.is_active ? "success" : "info"
        );

    }

    refreshAfterChange();

}


newRoleSelect.addEventListener("change", renderAccountAction);


accountActionPrimaryButton.addEventListener("click", () => {

    const action = accountAction;

    if (!action || mutationPending) {
        return;
    }

    if (action.mode === "role" && action.step === "select") {

        const role = newRoleSelect.value;

        if (!ROLE_ORDER.includes(role) || role === action.user.role) {
            return;
        }

        action.newRole = role;
        action.step = "confirm";

        hideAlert(accountActionAlert);

        renderAccountAction();

        accountActionPrimaryButton.focus();

        return;

    }

    submitAccountAction();

});


accountActionBackButton.addEventListener("click", () => {

    const action = accountAction;

    if (mutationPending) {
        return;
    }

    if (action && action.mode === "role" && action.step === "confirm") {

        action.step = "select";

        hideAlert(accountActionAlert);

        renderAccountAction();

        newRoleSelect.focus();

        return;

    }

    getModal(accountActionModalElement).hide();

});


accountActionModalElement.addEventListener("hidden.bs.modal", () => {

    accountAction = null;

    hideAlert(accountActionAlert);

});


// Dialogs cannot be dismissed while their request is in flight.
[createUserModalElement, accountActionModalElement].forEach(element => {

    element.addEventListener("hide.bs.modal", event => {

        if (mutationPending && !accessRevoked) {
            event.preventDefault();
        }

    });

});


// =========================================
// INITIALISE PAGE
// =========================================

async function initialiseUsersPage() {

    if (!requireAuthentication()) {
        return;
    }

    bindLogoutButton(logoutButton, message => showPageAlert(message));

    // The admin role is confirmed before anything is requested from
    // /api/admin. If the profile cannot be loaded, no role is assumed.
    currentUser = await initialiseUserInterface({
        onError: message => showPageAlert(message, "danger", {
            label: "Reload",
            onClick: () => window.location.reload()
        })
    });

    accessChecking.classList.add("d-none");

    if (!currentUser) {
        return;
    }

    if (!isAdmin(currentUser)) {

        showAccessDenied(
            "User Management is available to administrators only. " +
            "Your account does not have permission to manage users."
        );

        return;

    }

    applyStateFromUrl();

    await Promise.all([
        loadStats(),
        loadUsers()
    ]);

}


initialiseUsersPage();
