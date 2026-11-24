
"use strict";

// =========================================
// RESOLVEAI - ROLE-SPECIFIC DASHBOARD
// =========================================
//
// Admin:    global statistics (/api/analytics/summary), unassigned
//           count, recently created tickets.
// Agent:    personal workload (assigned_to=me), unassigned queue,
//           recently assigned tickets.
// Customer: own request statistics and recent requests. The API scopes
//           /api/tickets to the customer's own tickets; customers never
//           call staff-only endpoints.
//
// Counts use the ticket list endpoint with limit=1 and read the
// X-Total-Count header, so no ticket lists are downloaded for counting.

// Shared authentication, request and role helpers (common.js).
const {
    authenticatedFetch,
    apiRequest,
    checkApiResponse,
    readJsonResponse,
    requireAuthentication,
    initialiseUserInterface,
    bindLogoutButton,
    isAdmin,
    isAgent,
    isCustomer,
    isAuthenticationError
} = window.ResolveAI;

const RECENT_TICKET_LIMIT = 5;

// The API orders tickets by creation date; "recently assigned" is
// sorted client-side from the agent's newest assigned tickets
// (the API's maximum page size).
const AGENT_ASSIGNED_FETCH_LIMIT = 100;

const STATUS_OPEN = "Open";
const STATUS_IN_PROGRESS = "In Progress";
const STATUS_RESOLVED = "Resolved";

// Authenticated user, or null when the profile could not be loaded.
let currentUser = null;

// Ignore responses from superseded loads (e.g. after Retry).
let statsRequestId = 0;
let recentRequestId = 0;


// =========================================
// DOM ELEMENTS
// =========================================

const dashboardContent = document.querySelector(
    ".dashboard-content"
);

const statsAlert = document.getElementById(
    "statsAlert"
);

const recentTicketsHead = document.getElementById(
    "recentTicketsHead"
);

const recentTicketsBody = document.getElementById(
    "recentTickets"
);

const logoutButton = document.getElementById(
    "logoutButton"
);


// =========================================
// CREATE TICKET ELEMENTS
// =========================================

const createTicketButton = document.getElementById(
    "createTicketButton"
);

const createTicketForm = document.getElementById(
    "createTicketForm"
);

const createTicketModalElement = document.getElementById(
    "createTicketModal"
);

const createTicketAlert = document.getElementById(
    "createTicketAlert"
);

const saveTicketButton = document.getElementById(
    "saveTicketButton"
);

const requesterNameInput = document.getElementById(
    "requesterName"
);


// =========================================
// ROLE-SPECIFIC CONFIGURATION
// =========================================

// Staff wording is the HTML default (creating a ticket on behalf of a
// customer); customers get self-service wording.
const CREATE_FORM_COPY = Object.freeze({
    staff: Object.freeze({
        openButton: "+ Create Ticket",
        title: "Create Support Ticket",
        subtitle: "Enter the customer's issue details.",
        nameLabel: "Customer Name",
        namePlaceholder: "Enter customer name",
        titleLabel: "Ticket Title *",
        titlePlaceholder: "Briefly describe the issue",
        descriptionPlaceholder: "Describe the customer's issue in detail",
        help: "AI will automatically suggest a category after ticket submission.",
        submit: "Create Ticket",
        submitting: "Creating Ticket..."
    }),
    customer: Object.freeze({
        openButton: "+ New Support Request",
        title: "Submit a Support Request",
        subtitle: "Describe the issue you're experiencing and our support team will help.",
        nameLabel: "Your Name",
        namePlaceholder: "Enter your name",
        titleLabel: "Subject *",
        titlePlaceholder: "A short summary of the issue",
        descriptionPlaceholder: "Describe the issue you're experiencing",
        help: "Our support team will review your request and keep you updated.",
        submit: "Submit Support Request",
        submitting: "Submitting..."
    })
});


function getCreateFormCopy() {

    return isCustomer(currentUser)
        ? CREATE_FORM_COPY.customer
        : CREATE_FORM_COPY.staff;

}


// Recent-ticket table columns per role. Customers never get an
// assignment column.
const RECENT_COLUMNS = Object.freeze({
    admin: ["Ticket", "Subject", "Category", "Priority", "Status", "Assigned", "Action"],
    agent: ["Ticket", "Subject", "Priority", "Status", "Created", "Action"],
    customer: ["Request", "Subject", "Priority", "Status", "Submitted", "Action"]
});


function getRoleKey() {

    if (isAdmin(currentUser)) {
        return "admin";
    }

    if (isAgent(currentUser)) {
        return "agent";
    }

    if (isCustomer(currentUser)) {
        return "customer";
    }

    return null;

}


// =========================================
// MESSAGES
// =========================================

// Page-level message (profile errors), optionally with a Retry action.
function showDashboardMessage(message, onRetry = null) {

    let alertElement = document.getElementById(
        "dashboardMessage"
    );

    if (!alertElement) {

        alertElement = document.createElement("div");

        alertElement.id = "dashboardMessage";

        dashboardContent.prepend(alertElement);

    }

    setAlert(alertElement, message, "danger", onRetry);

}


function clearDashboardMessage() {

    document.getElementById("dashboardMessage")?.remove();

}


// Fill an alert using textContent, with an optional Retry button.
function setAlert(element, message, type, onRetry = null) {

    element.replaceChildren();

    element.className = `alert alert-${type} dashboard-alert`;

    element.setAttribute("role", "alert");

    const text = document.createElement("span");

    text.textContent = message;

    element.appendChild(text);

    if (onRetry) {

        element.appendChild(createRetryButton(onRetry));

    }

}


function hideAlert(element) {

    element.replaceChildren();

    element.className = "alert d-none";

}


function createRetryButton(onRetry) {

    const button = document.createElement("button");

    button.type = "button";
    button.className = "btn btn-sm btn-outline-dark dashboard-retry-button";
    button.textContent = "Retry";

    button.addEventListener("click", onRetry);

    return button;

}


function errorMessageOf(error, fallback) {

    return (error && error.message) || fallback;

}


// =========================================
// SHARED HELPERS
// =========================================

function createBadge(text, cssClass) {

    const badge = document.createElement("span");

    badge.className = `badge ${cssClass}`;

    badge.textContent = text;

    return badge;

}


function createTableCell(content) {

    const cell = document.createElement("td");

    if (content instanceof Node) {

        cell.appendChild(content);

    } else {

        cell.textContent = content;

    }

    return cell;

}


function formatTicketId(id) {

    return `#${String(id).padStart(4, "0")}`;

}


// The API returns UTC timestamps; values without an explicit offset
// are treated as UTC.
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

    if (!date) {
        return "—";
    }

    return date.toLocaleDateString("en-IN", {
        day: "2-digit",
        month: "short",
        year: "numeric"
    });

}


function formatCount(value) {

    return Number(value).toLocaleString();

}


function setText(elementId, value) {

    const element = document.getElementById(elementId);

    if (element) {
        element.textContent = value;
    }

}


// Parse X-Total-Count; null when missing or invalid.
function parseTotalCount(response) {

    const raw = response.headers.get("X-Total-Count");

    if (raw === null || !/^\d+$/.test(raw.trim())) {
        return null;
    }

    const total = Number(raw.trim());

    return Number.isSafeInteger(total) ? total : null;

}


// One page of tickets plus the total matching count.
async function fetchTickets(filters, limit) {

    const params = new URLSearchParams({
        ...filters,
        skip: "0",
        limit: String(limit)
    });

    const response = await authenticatedFetch(
        `/api/tickets?${params.toString()}`
    );

    await checkApiResponse(
        response,
        "Unable to retrieve support tickets."
    );

    const tickets = await readJsonResponse(response);

    if (!Array.isArray(tickets)) {

        throw new Error(
            "The server returned an invalid ticket response."
        );

    }

    return {
        tickets,
        total: parseTotalCount(response)
    };

}


// Accurate count for a filter, via X-Total-Count (limit=1).
async function fetchTicketCount(filters) {

    const { total } = await fetchTickets(filters, 1);

    if (total === null) {

        throw new Error(
            "The server returned an invalid ticket count."
        );

    }

    return total;

}


function ticketDetailsUrl(ticketId) {

    return `/tickets.html?ticket=${encodeURIComponent(ticketId)}`;

}


// =========================================
// STATISTICS
// =========================================

const STAT_ELEMENT_IDS = Object.freeze({
    admin: [
        "totalTickets",
        "openTickets",
        "progressTickets",
        "resolvedTickets",
        "unassignedTickets"
    ],
    agent: [
        "myOpenTickets",
        "myProgressTickets",
        "myAssignedTickets",
        "queueUnassignedTickets"
    ],
    customer: [
        "customerTotalTickets",
        "customerOpenTickets",
        "customerProgressTickets",
        "customerResolvedTickets"
    ]
});


function setStatValues(role, text) {

    STAT_ELEMENT_IDS[role].forEach(elementId => setText(elementId, text));

}


async function loadAdminStats() {

    const [summary, unassigned] = await Promise.all([
        apiRequest(
            "/api/analytics/summary",
            {},
            "Unable to retrieve dashboard analytics."
        ),
        fetchTicketCount({ assigned_to: "none" })
    ]);

    const values = [
        summary && summary.total_tickets,
        summary && summary.open_tickets,
        summary && summary.in_progress_tickets,
        summary && summary.resolved_tickets
    ];

    if (!values.every(Number.isSafeInteger)) {

        throw new Error(
            "The server returned an invalid analytics response."
        );

    }

    return [...values, unassigned];

}


// "My" cards only ever use assigned_to=me, so tickets assigned to
// other agents can never be counted.
function loadAgentStats() {

    return Promise.all([
        fetchTicketCount({ assigned_to: "me", status: STATUS_OPEN }),
        fetchTicketCount({ assigned_to: "me", status: STATUS_IN_PROGRESS }),
        fetchTicketCount({ assigned_to: "me" }),
        fetchTicketCount({ assigned_to: "none" })
    ]);

}


function loadCustomerStats() {

    return Promise.all([
        fetchTicketCount({}),
        fetchTicketCount({ status: STATUS_OPEN }),
        fetchTicketCount({ status: STATUS_IN_PROGRESS }),
        fetchTicketCount({ status: STATUS_RESOLVED })
    ]);

}


const STAT_LOADERS = Object.freeze({
    admin: loadAdminStats,
    agent: loadAgentStats,
    customer: loadCustomerStats
});


async function loadStats() {

    const role = getRoleKey();

    if (!role) {
        return;
    }

    const requestId = ++statsRequestId;

    hideAlert(statsAlert);

    setStatValues(role, "--");

    try {

        const values = await STAT_LOADERS[role]();

        if (requestId !== statsRequestId) {
            return;
        }

        STAT_ELEMENT_IDS[role].forEach((elementId, index) => {
            setText(elementId, formatCount(values[index]));
        });

    } catch (error) {

        if (requestId !== statsRequestId || isAuthenticationError(error)) {
            return;
        }

        setStatValues(role, "—");

        setAlert(
            statsAlert,
            errorMessageOf(error, "Unable to load your dashboard statistics."),
            "danger",
            loadStats
        );

    }

}


// =========================================
// RECENT TICKETS TABLE
// =========================================

function renderRecentHead(role) {

    const row = document.createElement("tr");

    RECENT_COLUMNS[role].forEach(label => {

        const heading = document.createElement("th");

        heading.scope = "col";
        heading.textContent = label;

        row.appendChild(heading);

    });

    recentTicketsHead.replaceChildren(row);

}


function columnCount() {

    const role = getRoleKey();

    return role ? RECENT_COLUMNS[role].length : 5;

}


// A full-width table row containing a message and optional action.
function renderTableState(message, action = null) {

    const row = document.createElement("tr");

    const cell = document.createElement("td");

    cell.colSpan = columnCount();
    cell.className = "empty-state";

    const text = document.createElement("p");

    text.className = "empty-state-text";
    text.textContent = message;

    cell.appendChild(text);

    if (action) {
        cell.appendChild(action);
    }

    row.appendChild(cell);

    recentTicketsBody.replaceChildren(row);

}


function createCategoryBadge(ticket) {

    if (ticket.category) {

        return createBadge(ticket.category, "bg-light text-dark");

    }

    if (ticket.predicted_category) {

        return createBadge(
            `AI: ${ticket.predicted_category}`,
            "bg-info-subtle text-info-emphasis"
        );

    }

    return createBadge(
        "Unassigned",
        "bg-secondary-subtle text-secondary-emphasis"
    );

}


function createPriorityBadge(ticket) {

    const priorityClasses = {
        Low: "bg-success-subtle text-success-emphasis",
        Medium: "bg-warning-subtle text-warning-emphasis",
        High: "bg-danger-subtle text-danger-emphasis"
    };

    return createBadge(
        ticket.priority,
        priorityClasses[ticket.priority] ||
            "bg-secondary-subtle text-secondary-emphasis"
    );

}


function createStatusBadge(ticket) {

    const statusClasses = {
        "Open": "bg-warning-subtle text-warning-emphasis",
        "In Progress": "bg-info-subtle text-info-emphasis",
        "Resolved": "bg-success-subtle text-success-emphasis"
    };

    return createBadge(
        ticket.status,
        statusClasses[ticket.status] ||
            "bg-secondary-subtle text-secondary-emphasis"
    );

}


// Staff only: assigned agent's name, or "Unassigned".
function createAssigneeLabel(ticket) {

    const label = document.createElement("span");

    const hasAssignee =
        ticket.assigned_to_user_id !== null &&
        ticket.assigned_to_user_id !== undefined;

    if (ticket.assignee && typeof ticket.assignee.name === "string") {

        label.textContent = ticket.assignee.name;
        label.className = "assignee-label";

    } else if (hasAssignee) {

        label.textContent = `Agent #${ticket.assigned_to_user_id}`;
        label.className = "assignee-label";

    } else {

        label.textContent = "Unassigned";
        label.className = "assignee-label is-unassigned";

    }

    return label;

}


function createOpenLink(ticket) {

    const link = document.createElement("a");

    link.className = "btn btn-outline-primary btn-sm";
    link.href = ticketDetailsUrl(ticket.id);
    link.textContent = isCustomer(currentUser) ? "View" : "Open";
    link.setAttribute(
        "aria-label",
        `Open ticket ${formatTicketId(ticket.id)}`
    );

    return link;

}


function createTicketRow(ticket) {

    const role = getRoleKey();

    const row = document.createElement("tr");

    const cells = [
        formatTicketId(ticket.id),
        ticket.title
    ];

    if (role === "admin") {

        cells.push(
            createCategoryBadge(ticket),
            createPriorityBadge(ticket),
            createStatusBadge(ticket),
            createAssigneeLabel(ticket)
        );

    } else {

        // Agent and customer rows: no category or assignee columns.
        cells.push(
            createPriorityBadge(ticket),
            createStatusBadge(ticket),
            formatDate(ticket.created_at)
        );

    }

    cells.push(createOpenLink(ticket));

    cells.forEach(content => row.appendChild(createTableCell(content)));

    return row;

}


function createEmptyStateAction(role) {

    if (role === "customer") {

        const button = document.createElement("button");

        button.type = "button";
        button.className = "btn btn-primary btn-sm";
        button.textContent = "Submit Your First Request";
        button.addEventListener("click", openCreateTicketModal);

        return button;

    }

    if (role === "agent") {

        const link = document.createElement("a");

        link.className = "btn btn-outline-primary btn-sm";
        link.href = "/tickets.html?assigned_to=none";
        link.textContent = "Browse Unassigned Queue";

        return link;

    }

    return null;

}


const EMPTY_MESSAGES = Object.freeze({
    admin: "No support tickets have been created yet.",
    agent: "No tickets are assigned to you yet. Claim one from the unassigned queue.",
    customer: "You haven't submitted any support requests yet."
});


function renderRecentTickets(tickets) {

    const role = getRoleKey();

    if (!tickets.length) {

        renderTableState(
            EMPTY_MESSAGES[role],
            createEmptyStateAction(role)
        );

        return;

    }

    recentTicketsBody.replaceChildren(
        ...tickets.map(createTicketRow)
    );

}


// Newest assignment first (assigned_at), then newest ticket.
function sortByAssignment(tickets) {

    return [...tickets].sort((first, second) => {

        const firstTime = parseApiDate(first.assigned_at)?.getTime() ?? 0;
        const secondTime = parseApiDate(second.assigned_at)?.getTime() ?? 0;

        return secondTime - firstTime || second.id - first.id;

    });

}


async function fetchRecentTickets(role) {

    if (role === "agent") {

        const { tickets } = await fetchTickets(
            { assigned_to: "me" },
            AGENT_ASSIGNED_FETCH_LIMIT
        );

        return sortByAssignment(tickets).slice(0, RECENT_TICKET_LIMIT);

    }

    // Admin: all tickets. Customer: the API returns only their own.
    const { tickets } = await fetchTickets({}, RECENT_TICKET_LIMIT);

    return tickets;

}


async function loadRecentTickets() {

    const role = getRoleKey();

    if (!role) {
        return;
    }

    const requestId = ++recentRequestId;

    renderTableState("Loading tickets...");

    try {

        const tickets = await fetchRecentTickets(role);

        if (requestId !== recentRequestId) {
            return;
        }

        renderRecentTickets(tickets);

    } catch (error) {

        if (requestId !== recentRequestId || isAuthenticationError(error)) {
            return;
        }

        renderTableState(
            errorMessageOf(error, "Unable to retrieve support tickets."),
            createRetryButton(loadRecentTickets)
        );

    }

}


// =========================================
// CREATE TICKET FORM (ROLE-AWARE)
// =========================================

function applyCreateFormCopy() {

    const copy = getCreateFormCopy();

    createTicketButton.textContent = copy.openButton;

    setText("createTicketModalLabel", copy.title);
    setText("createTicketSubtitle", copy.subtitle);
    setText("requesterNameLabel", copy.nameLabel);
    setText("ticketTitleLabel", copy.titleLabel);
    setText("createTicketHelp", copy.help);

    requesterNameInput.placeholder = copy.namePlaceholder;

    document.getElementById("ticketTitle").placeholder =
        copy.titlePlaceholder;

    document.getElementById("ticketDescription").placeholder =
        copy.descriptionPlaceholder;

    saveTicketButton.textContent = copy.submit;

}


// Reset the form; customers get their own name prefilled (editable).
function resetCreateTicketForm() {

    createTicketForm.reset();

    if (isCustomer(currentUser)) {

        requesterNameInput.value = currentUser.name.trim();

    }

}


function showCreateTicketMessage(message, type = "danger") {

    createTicketAlert.textContent = message;

    createTicketAlert.className = `alert alert-${type}`;

}


function hideCreateTicketMessage() {

    createTicketAlert.textContent = "";

    createTicketAlert.className = "alert d-none";

}


function openCreateTicketModal() {

    resetCreateTicketForm();

    hideCreateTicketMessage();

    bootstrap.Modal.getOrCreateInstance(
        createTicketModalElement
    ).show();

}


function buildSuccessMessage(ticket) {

    if (isCustomer(currentUser)) {

        return `Your support request ${formatTicketId(ticket.id)} has ` +
            "been submitted. Our support team will review it shortly.";

    }

    // Staff: existing message including the AI prediction.
    let message = `Ticket #${ticket.id} created successfully. `;

    if (ticket.predicted_category) {

        message += `AI suggested category: ${ticket.predicted_category}. `;

        if (typeof ticket.prediction_confidence === "number") {

            const confidence = (
                ticket.prediction_confidence * 100
            ).toFixed(1);

            message += `Model confidence: ${confidence}%.`;

        }

    } else {

        message +=
            "AI prediction was unavailable, but the ticket was saved.";

    }

    return message;

}


async function createTicket(event) {

    event.preventDefault();

    hideCreateTicketMessage();

    const copy = getCreateFormCopy();

    // Collect form data.
    const formData = new FormData(createTicketForm);

    const ticketData = {
        requester_name:
            formData.get("requester_name").trim() || null,

        title: formData.get("title").trim(),

        description: formData.get("description").trim(),

        priority: formData.get("priority")
    };

    // Display loading state.
    saveTicketButton.disabled = true;

    saveTicketButton.textContent = copy.submitting;

    try {

        const ticket = await apiRequest(
            "/api/tickets",
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify(ticketData)
            },
            isCustomer(currentUser)
                ? "Unable to submit your support request."
                : "Unable to create the support ticket."
        );

        if (!ticket || !Number.isSafeInteger(ticket.id)) {

            throw new Error(
                "The server returned an invalid ticket response."
            );

        }

        // Reset form (customer name is prefilled again).
        resetCreateTicketForm();

        showCreateTicketMessage(
            buildSuccessMessage(ticket),
            "success"
        );

        // Refresh counts and the recent list.
        await Promise.all([
            loadStats(),
            loadRecentTickets()
        ]);

    } catch (error) {

        if (isAuthenticationError(error)) {
            return;
        }

        showCreateTicketMessage(
            errorMessageOf(
                error,
                "Something went wrong while creating the ticket."
            )
        );

    } finally {

        // Restore the submit button.
        saveTicketButton.disabled = false;

        saveTicketButton.textContent = copy.submit;

    }

}


// =========================================
// LOAD DASHBOARD
// =========================================

// Confirms the role, then loads that role's sections. If the profile
// cannot be loaded, no role-specific section is shown and the user can
// retry; a 401 redirects to login through the shared helpers.
async function loadDashboard() {

    clearDashboardMessage();

    currentUser = await initialiseUserInterface({
        onError: message => showDashboardMessage(message, loadDashboard)
    });

    const role = getRoleKey();

    if (!role) {

        if (currentUser) {

            // Authenticated, but with a role this page does not support.
            showDashboardMessage(
                "Your account does not have access to a dashboard."
            );

        }

        return;

    }

    // Display first name in welcome message.
    setText(
        "welcomeName",
        currentUser.name.trim().split(/\s+/)[0]
    );

    applyCreateFormCopy();

    renderRecentHead(role);

    await Promise.all([
        loadStats(),
        loadRecentTickets()
    ]);

}


// =========================================
// INITIALISE DASHBOARD
// =========================================

async function initialiseDashboard() {

    if (!requireAuthentication()) {

        return;

    }

    // Register logout functionality.
    bindLogoutButton(
        logoutButton,
        message => showDashboardMessage(message)
    );

    // Register Create Ticket functionality.
    createTicketButton.addEventListener(
        "click",
        openCreateTicketModal
    );

    createTicketForm.addEventListener(
        "submit",
        createTicket
    );

    await loadDashboard();

}


// Start the dashboard.
initialiseDashboard();
