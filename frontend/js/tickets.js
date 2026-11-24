
"use strict";

// =========================================
// RESOLVEAI - TICKET MANAGEMENT
// =========================================
//
// Role-aware ticket list, details, editing, assignment (admin),
// claiming (agent) and activity history (staff).
//
// The UI only offers actions the current role may perform; the API
// remains the authority and enforces every permission.

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
    isStaff,
    isAuthenticationError
} = window.ResolveAI;

const PAGE_SIZE = 10;

// Agent list for the admin assignment control.
const AGENT_PAGE_SIZE = 100;
const MAX_AGENT_PAGES = 50;

const STATUS_OPEN = "Open";
const STATUS_IN_PROGRESS = "In Progress";
const STATUS_RESOLVED = "Resolved";

// Mirrors the backend STATUS_TRANSITIONS. Used only to offer sensible
// options; the API validates every change.
const STATUS_TRANSITIONS = Object.freeze({
    [STATUS_OPEN]: [
        { to: STATUS_IN_PROGRESS, requiresAssignee: true },
        { to: STATUS_RESOLVED, requiresAssignee: true }
    ],
    [STATUS_IN_PROGRESS]: [
        { to: STATUS_OPEN, requiresAssignee: false },
        { to: STATUS_RESOLVED, requiresAssignee: false }
    ],
    [STATUS_RESOLVED]: [
        { to: STATUS_IN_PROGRESS, requiresAssignee: true }
    ]
});

const CLAIMABLE_STATUSES = Object.freeze([
    STATUS_OPEN,
    STATUS_IN_PROGRESS
]);

const EVENT_LABELS = Object.freeze({
    created: "Created",
    assigned: "Assigned",
    reassigned: "Reassigned",
    unassigned: "Unassigned",
    status_changed: "Status changed"
});

const UNASSIGNMENT_REASONS = Object.freeze({
    assignee_deactivated: "the agent's account was deactivated",
    assignee_role_changed: "the agent's role was changed"
});


// =========================================
// PAGE STATE
// =========================================

// Authenticated user from /api/auth/me, or null if it could not be
// loaded (in which case no privileged UI is shown).
let currentUser = null;

let currentPage = 0;
let latestRequestId = 0;
let searchDebounce;

// Currently selected ticket in the details modal.
let activeTicket = null;

// Snapshot of the ticket taken when the edit form was opened.
let editSnapshot = null;

// Guards against rendering stale history / agent responses.
let historyRequestId = 0;
let agentRequestId = 0;

// Active agents available for assignment (admin only).
let assignableAgents = [];


// =========================================
// DOM ELEMENTS
// =========================================

const ticketsTableBody = document.getElementById(
    "ticketsTableBody"
);

const ticketSearchInput = document.getElementById(
    "ticketSearchInput"
);

const statusFilter = document.getElementById(
    "statusFilter"
);

const priorityFilter = document.getElementById(
    "priorityFilter"
);

const categoryFilter = document.getElementById(
    "categoryFilter"
);

const assignmentFilter = document.getElementById(
    "assignmentFilter"
);

const filterGrid = document.querySelector(
    ".filter-grid"
);

const ticketResultCount = document.getElementById(
    "ticketResultCount"
);

const paginationInfo = document.getElementById(
    "paginationInfo"
);

const previousPageButton = document.getElementById(
    "previousPageButton"
);

const nextPageButton = document.getElementById(
    "nextPageButton"
);

const clearFiltersButton = document.getElementById(
    "clearFiltersButton"
);

const logoutButton = document.getElementById(
    "logoutButton"
);

const TABLE_COLUMN_COUNT = 8;


// =========================================
// ROLE-BASED PERMISSIONS (UI ONLY)
// =========================================

function hasAssignee(ticket) {

    return Boolean(
        ticket &&
        ticket.assigned_to_user_id !== null &&
        ticket.assigned_to_user_id !== undefined
    );

}


function isAssignedToCurrentUser(ticket) {

    return Boolean(
        currentUser &&
        hasAssignee(ticket) &&
        ticket.assigned_to_user_id === currentUser.id
    );

}


// Admin: any ticket. Agent: only tickets assigned to them.
function canEditTicket(ticket) {

    return isAdmin(currentUser) ||
        (isAgent(currentUser) && isAssignedToCurrentUser(ticket));

}


function canDeleteTicket() {

    return isAdmin(currentUser);

}


function canAssignTicket() {

    return isAdmin(currentUser);

}


// Active agent, unassigned ticket, Open or In Progress.
function canClaimTicket(ticket) {

    return Boolean(
        isAgent(currentUser) &&
        currentUser.is_active === true &&
        ticket &&
        !hasAssignee(ticket) &&
        ticket.is_assigned !== true &&
        CLAIMABLE_STATUSES.includes(ticket.status)
    );

}


function canViewHistory() {

    return isStaff(currentUser);

}


// =========================================
// DISPLAY PAGE MESSAGES
// =========================================

function showPageMessage(message, type = "danger") {

    let alertElement = document.getElementById(
        "ticketsPageMessage"
    );

    if (!alertElement) {

        alertElement = document.createElement("div");

        alertElement.id = "ticketsPageMessage";

        const content = document.querySelector(
            ".tickets-page-content"
        );

        content.prepend(alertElement);

    }

    alertElement.className = `alert alert-${type}`;

    alertElement.setAttribute("role", "alert");

    alertElement.textContent = message;

}


// Profile errors use their own alert so that reloading the ticket list
// (which clears page messages) does not hide them.
function showProfileMessage(message) {

    let alertElement = document.getElementById(
        "ticketsProfileMessage"
    );

    if (!alertElement) {

        alertElement = document.createElement("div");

        alertElement.id = "ticketsProfileMessage";

        alertElement.className = "alert alert-danger";

        alertElement.setAttribute("role", "alert");

        document.querySelector(".tickets-page-content")
            .prepend(alertElement);

    }

    alertElement.textContent = message;

}


function clearPageMessage() {

    const alertElement = document.getElementById(
        "ticketsPageMessage"
    );

    if (alertElement) {

        alertElement.remove();

    }

}


// Show a message (built with textContent) in an alert element, with an
// optional "Reload Ticket" action.
function setAlert(element, message, type = "danger", onReload = null) {

    if (!element) {
        return;
    }

    element.replaceChildren();

    element.className = `alert alert-${type} ticket-modal-alert`;

    const text = document.createElement("span");

    text.textContent = message;

    element.appendChild(text);

    if (onReload) {

        const reloadButton = document.createElement("button");

        reloadButton.type = "button";
        reloadButton.className =
            "btn btn-sm btn-outline-dark ticket-reload-button";
        reloadButton.textContent = "Reload Ticket";

        reloadButton.addEventListener("click", onReload);

        element.appendChild(reloadButton);

    }

}


function hideAlert(element) {

    if (!element) {
        return;
    }

    element.replaceChildren();

    element.className = "alert d-none";

}


// =========================================
// CREATE TABLE CELL
// =========================================

function createTableCell(content) {

    const cell = document.createElement("td");

    if (content instanceof HTMLElement) {

        cell.appendChild(content);

    } else {

        cell.textContent = content;

    }

    return cell;

}


// =========================================
// CREATE STATUS / PRIORITY BADGE
// =========================================

function createBadge(text, cssClass) {

    const badge = document.createElement("span");

    badge.className = `badge ${cssClass}`;

    badge.textContent = text;

    return badge;

}


// =========================================
// DATE FORMATTING
// =========================================

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


function formatTicketDate(dateValue) {

    const date = parseApiDate(dateValue);

    if (!date) {

        return "—";

    }

    return date.toLocaleDateString("en-IN", {
        day: "2-digit",
        month: "short",
        year: "numeric"
    });

}


// Date and time in the user's local timezone.
function formatDateTime(dateValue) {

    const date = parseApiDate(dateValue);

    if (!date) {

        return "Unknown time";

    }

    return date.toLocaleString("en-IN", {
        day: "2-digit",
        month: "short",
        year: "numeric",
        hour: "2-digit",
        minute: "2-digit"
    });

}


function formatTicketId(id) {

    return `#${String(id).padStart(4, "0")}`;

}


// =========================================
// ASSIGNMENT LABELS
// =========================================

// Staff see the assigned agent's name. Customers only ever see a
// neutral label derived from `is_assigned` - never agent identity.
function getAssignmentLabel(ticket) {

    if (isStaff(currentUser)) {

        if (
            ticket.assignee &&
            typeof ticket.assignee.name === "string" &&
            ticket.assignee.name.trim()
        ) {

            return ticket.assignee.name;

        }

        if (hasAssignee(ticket)) {

            return `Agent #${ticket.assigned_to_user_id}`;

        }

        return "Unassigned";

    }

    return ticket.is_assigned === true
        ? "Assigned to support"
        : "Awaiting assignment";

}


function isUnassignedForDisplay(ticket) {

    return isStaff(currentUser)
        ? !hasAssignee(ticket)
        : ticket.is_assigned !== true;

}


function createAssignmentLabel(ticket) {

    const label = document.createElement("span");

    label.className = isUnassignedForDisplay(ticket)
        ? "assignee-label is-unassigned"
        : "assignee-label";

    label.textContent = getAssignmentLabel(ticket);

    return label;

}


// History labels: IDs only (the API does not return names here).
function userLabel(userId) {

    if (!Number.isSafeInteger(userId)) {

        return "Unknown user";

    }

    const suffix = currentUser && userId === currentUser.id
        ? " (you)"
        : "";

    return `User #${userId}${suffix}`;

}


function agentLabel(agentId) {

    if (!Number.isSafeInteger(agentId)) {

        return "an agent";

    }

    const suffix = currentUser && agentId === currentUser.id
        ? " (you)"
        : "";

    return `Agent #${agentId}${suffix}`;

}


// =========================================
// TICKET DETAILS MODAL
// =========================================

// The modal markup is static (no API data); every server value is
// written later with textContent.
function getTicketDetailsModal() {

    let modal = document.getElementById(
        "ticketDetailsModal"
    );

    if (modal) {
        return modal;
    }

    modal = document.createElement("div");

    modal.id = "ticketDetailsModal";
    modal.className = "modal fade";
    modal.tabIndex = -1;
    modal.setAttribute("aria-hidden", "true");
    modal.setAttribute("aria-labelledby", "ticketDetailsModalLabel");

    modal.innerHTML = `
        <div class="modal-dialog modal-dialog-centered modal-dialog-scrollable modal-lg">
            <div class="modal-content">

                <!-- Modal header -->
                <div class="modal-header">
                    <div>
                        <h5 class="modal-title" id="ticketDetailsModalLabel">
                            Ticket Details
                        </h5>

                        <p
                            class="text-secondary small mb-0"
                            id="detailTicketId"
                        ></p>
                    </div>

                    <button
                        type="button"
                        class="btn-close"
                        data-bs-dismiss="modal"
                        aria-label="Close"
                    ></button>
                </div>


                <!-- Modal body -->
                <div class="modal-body">

                    <div
                        id="ticketDetailsAlert"
                        class="alert d-none"
                        role="alert"
                        aria-live="polite"
                    ></div>

                    <!-- READ-ONLY VIEW -->
                    <div id="ticketReadOnlyView">

                        <h5 id="detailTitle" class="mb-3"></h5>

                        <p class="text-secondary small mb-1">
                            Description
                        </p>

                        <p
                            id="detailDescription"
                            class="ticket-detail-description"
                        ></p>

                        <hr>

                        <div class="row g-3">

                            <div class="col-6">
                                <small class="text-secondary">
                                    Customer
                                </small>
                                <p id="detailRequester" class="mb-0"></p>
                            </div>

                            <div class="col-6">
                                <small class="text-secondary">
                                    Priority
                                </small>
                                <p id="detailPriority" class="mb-0"></p>
                            </div>

                            <div class="col-6">
                                <small class="text-secondary">
                                    Status
                                </small>
                                <p id="detailStatus" class="mb-0"></p>
                            </div>

                            <div class="col-6">
                                <small class="text-secondary">
                                    Assigned To
                                </small>
                                <p id="detailAssignee" class="mb-0"></p>
                            </div>

                            <div class="col-6">
                                <small class="text-secondary">
                                    Final Category
                                </small>
                                <p id="detailCategory" class="mb-0"></p>
                            </div>

                            <div class="col-6">
                                <small class="text-secondary">
                                    AI Prediction
                                </small>
                                <p id="detailPrediction" class="mb-0"></p>
                            </div>

                            <div class="col-6">
                                <small class="text-secondary">
                                    Model Confidence
                                </small>
                                <p id="detailConfidence" class="mb-0"></p>
                            </div>

                            <div class="col-6">
                                <small class="text-secondary">
                                    Created
                                </small>
                                <p id="detailCreated" class="mb-0"></p>
                            </div>

                        </div>


                        <!-- ADMIN: ASSIGNMENT -->
                        <section
                            class="ticket-assignment-panel"
                            id="ticketAssignmentPanel"
                            aria-labelledby="ticketAssignmentHeading"
                        >

                            <div class="ticket-panel-heading">
                                <h6 id="ticketAssignmentHeading">
                                    Assignment
                                </h6>
                                <p>
                                    Assign, reassign or unassign this
                                    ticket. Only active agents can be
                                    selected.
                                </p>
                            </div>

                            <div class="assignment-controls">

                                <label
                                    for="assignmentSelect"
                                    class="visually-hidden"
                                >
                                    Assigned agent
                                </label>

                                <select
                                    class="form-select"
                                    id="assignmentSelect"
                                    disabled
                                ></select>

                                <button
                                    type="button"
                                    class="btn btn-primary"
                                    id="saveAssignmentButton"
                                    disabled
                                >
                                    Save Assignment
                                </button>

                            </div>

                            <p
                                class="assignment-status"
                                id="assignmentStatus"
                                aria-live="polite"
                            ></p>

                        </section>


                        <!-- STAFF: ACTIVITY HISTORY -->
                        <section
                            class="ticket-activity"
                            id="ticketActivitySection"
                            aria-labelledby="ticketActivityHeading"
                        >

                            <div class="ticket-panel-heading">
                                <h6 id="ticketActivityHeading">
                                    Ticket Activity
                                </h6>
                            </div>

                            <p
                                class="activity-state"
                                id="ticketActivityState"
                                aria-live="polite"
                            ></p>

                            <ol
                                class="activity-timeline"
                                id="ticketActivityList"
                            ></ol>

                        </section>

                    </div>


                    <!-- EDIT FORM -->
                    <div id="ticketEditView" class="d-none">

                        <form id="ticketEditForm" novalidate>

                            <div
                                id="ticketEditAlert"
                                class="alert d-none"
                                role="alert"
                                aria-live="polite"
                            ></div>

                            <div class="mb-3">

                                <label
                                    for="editTicketTitle"
                                    class="form-label"
                                >
                                    Ticket Title
                                </label>

                                <input
                                    type="text"
                                    class="form-control"
                                    id="editTicketTitle"
                                    minlength="3"
                                    maxlength="200"
                                    required
                                >

                            </div>


                            <div class="mb-3">

                                <label
                                    for="editTicketDescription"
                                    class="form-label"
                                >
                                    Description
                                </label>

                                <textarea
                                    class="form-control"
                                    id="editTicketDescription"
                                    rows="4"
                                    minlength="5"
                                    required
                                ></textarea>

                            </div>


                            <div class="mb-3">

                                <label
                                    for="editRequesterName"
                                    class="form-label"
                                >
                                    Customer Name
                                </label>

                                <input
                                    type="text"
                                    class="form-control"
                                    id="editRequesterName"
                                    maxlength="100"
                                >

                            </div>


                            <div class="mb-3">

                                <label
                                    for="editTicketCategory"
                                    class="form-label"
                                >
                                    Final Category
                                </label>

                                <input
                                    type="text"
                                    class="form-control"
                                    id="editTicketCategory"
                                    maxlength="100"
                                    placeholder="e.g. PAYMENT, REFUND, ORDER"
                                >

                            </div>


                            <div class="row g-3">

                                <div class="col-md-6">

                                    <label
                                        for="editTicketPriority"
                                        class="form-label"
                                    >
                                        Priority
                                    </label>

                                    <select
                                        class="form-select"
                                        id="editTicketPriority"
                                    >
                                        <option value="Low">Low</option>
                                        <option value="Medium">Medium</option>
                                        <option value="High">High</option>
                                    </select>

                                </div>


                                <div class="col-md-6">

                                    <label
                                        for="editTicketStatus"
                                        class="form-label"
                                    >
                                        Status
                                    </label>

                                    <select
                                        class="form-select"
                                        id="editTicketStatus"
                                    ></select>

                                    <div
                                        class="form-text"
                                        id="editStatusHelp"
                                    ></div>

                                </div>

                            </div>


                            <div class="alert alert-info mt-3 mb-0 small">
                                AI prediction and model confidence are
                                preserved automatically when editing a ticket.
                                Only the fields you change are saved.
                            </div>

                        </form>

                    </div>

                </div>


                <!-- DETAILS FOOTER -->
                <div
                    class="modal-footer"
                    id="ticketDetailsFooter"
                >

                    <button
                        type="button"
                        class="btn btn-light"
                        data-bs-dismiss="modal"
                    >
                        Close
                    </button>

                    <button
                        type="button"
                        class="btn btn-outline-danger d-none"
                        id="deleteTicketButton"
                    >
                        Delete Ticket
                    </button>

                    <button
                        type="button"
                        class="btn btn-success d-none"
                        id="claimTicketButton"
                    >
                        Claim Ticket
                    </button>

                    <button
                        type="button"
                        class="btn btn-primary d-none"
                        id="editTicketButton"
                    >
                        Edit Ticket
                    </button>

                </div>


                <!-- EDIT FOOTER -->
                <div
                    class="modal-footer d-none"
                    id="ticketEditFooter"
                >

                    <button
                        type="button"
                        class="btn btn-light"
                        id="cancelTicketEditButton"
                    >
                        Cancel
                    </button>

                    <button
                        type="submit"
                        form="ticketEditForm"
                        class="btn btn-primary"
                        id="saveTicketChangesButton"
                    >
                        Save Changes
                    </button>

                </div>

            </div>
        </div>
    `;

    // Remove every control the current role must never see. Without a
    // confirmed role (profile failed to load) nothing privileged remains.
    if (!canAssignTicket()) {
        modal.querySelector("#ticketAssignmentPanel").remove();
    }

    if (!canDeleteTicket()) {
        modal.querySelector("#deleteTicketButton").remove();
    }

    if (!isAgent(currentUser)) {
        modal.querySelector("#claimTicketButton").remove();
    }

    if (!canViewHistory()) {
        modal.querySelector("#ticketActivitySection").remove();
    }

    if (!isStaff(currentUser)) {
        modal.querySelector("#editTicketButton").remove();
        modal.querySelector("#ticketEditView").remove();
        modal.querySelector("#ticketEditFooter").remove();
    }

    document.body.appendChild(modal);

    // Register remaining actions.
    modal.querySelector("#editTicketButton")?.addEventListener(
        "click",
        enterTicketEditMode
    );

    modal.querySelector("#deleteTicketButton")?.addEventListener(
        "click",
        deleteTicket
    );

    modal.querySelector("#claimTicketButton")?.addEventListener(
        "click",
        claimTicket
    );

    modal.querySelector("#saveAssignmentButton")?.addEventListener(
        "click",
        saveAssignment
    );

    modal.querySelector("#cancelTicketEditButton")?.addEventListener(
        "click",
        cancelTicketEdit
    );

    modal.querySelector("#ticketEditForm")?.addEventListener(
        "submit",
        saveTicketChanges
    );

    // Restore read-only mode whenever the modal closes.
    modal.addEventListener(
        "hidden.bs.modal",
        () => {
            editSnapshot = null;
            historyRequestId++;
            agentRequestId++;
            showTicketReadOnlyMode();
        }
    );

    return modal;

}


function getModalElement(id) {

    return document.getElementById(id);

}


// =========================================
// POPULATE TICKET DETAILS
// =========================================

function populateTicketDetails(ticket) {

    const details = {

        detailTicketId: formatTicketId(ticket.id),
        detailTitle: ticket.title,
        detailDescription: ticket.description,
        detailRequester: ticket.requester_name || "Not provided",
        detailPriority: ticket.priority,
        detailStatus: ticket.status,
        detailAssignee: getAssignmentLabel(ticket),
        detailCategory: ticket.category || "Not assigned",
        detailPrediction: ticket.predicted_category || "Unavailable",

        detailConfidence:
            ticket.prediction_confidence !== null &&
            ticket.prediction_confidence !== undefined
                ? `${(ticket.prediction_confidence * 100).toFixed(1)}%`
                : "Unavailable",

        detailCreated: formatTicketDate(ticket.created_at)

    };

    Object.entries(details).forEach(([elementId, value]) => {

        const element = getModalElement(elementId);

        if (element) {

            element.textContent = value;

        }

    });

}


// Show or hide the per-ticket actions for the current role.
function updateTicketActions(ticket) {

    const toggles = [
        ["editTicketButton", canEditTicket(ticket)],
        ["claimTicketButton", canClaimTicket(ticket)],
        ["deleteTicketButton", canDeleteTicket()]
    ];

    toggles.forEach(([elementId, visible]) => {

        const element = getModalElement(elementId);

        if (element) {

            element.classList.toggle("d-none", !visible);

            element.disabled = false;

        }

    });

}


function renderTicketDetails(ticket) {

    populateTicketDetails(ticket);

    updateTicketActions(ticket);

    if (canAssignTicket()) {

        renderAssignmentOptions(ticket);

    }

}


// =========================================
// SWITCH TO READ-ONLY MODE
// =========================================

function showTicketReadOnlyMode() {

    getModalElement("ticketReadOnlyView")
        ?.classList.remove("d-none");

    getModalElement("ticketEditView")
        ?.classList.add("d-none");

    getModalElement("ticketDetailsFooter")
        ?.classList.remove("d-none");

    getModalElement("ticketEditFooter")
        ?.classList.add("d-none");

    hideAlert(getModalElement("ticketEditAlert"));

}


function cancelTicketEdit() {

    editSnapshot = null;

    showTicketReadOnlyMode();

}


// =========================================
// STATUS OPTIONS (WORKFLOW-AWARE)
// =========================================

function populateStatusOptions(currentStatus, ticketHasAssignee) {

    const select = getModalElement("editTicketStatus");
    const help = getModalElement("editStatusHelp");

    select.replaceChildren();

    const current = document.createElement("option");

    current.value = currentStatus;
    current.textContent = currentStatus;

    select.appendChild(current);

    let blocked = false;

    (STATUS_TRANSITIONS[currentStatus] || []).forEach(rule => {

        const option = document.createElement("option");

        option.value = rule.to;
        option.textContent = rule.to;

        if (rule.requiresAssignee && !ticketHasAssignee) {

            option.disabled = true;
            option.textContent = `${rule.to} (assign first)`;
            blocked = true;

        }

        select.appendChild(option);

    });

    select.value = currentStatus;

    help.textContent = blocked
        ? "Assign the ticket to an agent before moving it forward."
        : (currentStatus === STATUS_RESOLVED
            ? "A resolved ticket can only be reopened as In Progress."
            : "");

}


// =========================================
// SWITCH TO EDIT MODE
// =========================================

function createEditSnapshot(ticket) {

    return Object.freeze({
        id: ticket.id,
        version: ticket.version,
        hasAssignee: hasAssignee(ticket),
        title: ticket.title,
        description: ticket.description,
        requester_name: ticket.requester_name ?? null,
        category: ticket.category ?? null,
        priority: ticket.priority,
        status: ticket.status
    });

}


function enterTicketEditMode() {

    if (!activeTicket || !canEditTicket(activeTicket)) {
        return;
    }

    editSnapshot = createEditSnapshot(activeTicket);

    // Populate form with the snapshot.
    getModalElement("editTicketTitle").value =
        editSnapshot.title;

    getModalElement("editTicketDescription").value =
        editSnapshot.description;

    getModalElement("editRequesterName").value =
        editSnapshot.requester_name || "";

    getModalElement("editTicketCategory").value =
        editSnapshot.category || "";

    getModalElement("editTicketPriority").value =
        editSnapshot.priority;

    populateStatusOptions(
        editSnapshot.status,
        editSnapshot.hasAssignee
    );

    // Clear previous messages.
    hideAlert(getModalElement("ticketDetailsAlert"));
    hideAlert(getModalElement("ticketEditAlert"));

    // Switch from read-only to edit mode.
    getModalElement("ticketReadOnlyView").classList.add("d-none");
    getModalElement("ticketEditView").classList.remove("d-none");
    getModalElement("ticketDetailsFooter").classList.add("d-none");
    getModalElement("ticketEditFooter").classList.remove("d-none");

}


// =========================================
// DISPLAY EDIT FORM MESSAGE
// =========================================

function showTicketEditMessage(message, type = "danger", onReload = null) {

    setAlert(
        getModalElement("ticketEditAlert"),
        message,
        type,
        onReload
    );

}


function showTicketDetailsMessage(message, type = "success", onReload = null) {

    setAlert(
        getModalElement("ticketDetailsAlert"),
        message,
        type,
        onReload
    );

}


// =========================================
// COLLECT CHANGED FIELDS
// =========================================

function normaliseText(value) {

    return typeof value === "string" ? value.trim() : "";

}


function normaliseOptionalText(value) {

    const text = normaliseText(value);

    return text || null;

}


// Compare the form against the snapshot; return only changed fields.
function collectTicketChanges(snapshot) {

    const formValues = {
        title: normaliseText(getModalElement("editTicketTitle").value),
        description: normaliseText(
            getModalElement("editTicketDescription").value
        ),
        requester_name: normaliseOptionalText(
            getModalElement("editRequesterName").value
        ),
        category: normaliseOptionalText(
            getModalElement("editTicketCategory").value
        ),
        priority: getModalElement("editTicketPriority").value,
        status: getModalElement("editTicketStatus").value
    };

    const originalValues = {
        title: normaliseText(snapshot.title),
        description: normaliseText(snapshot.description),
        requester_name: normaliseOptionalText(snapshot.requester_name),
        category: normaliseOptionalText(snapshot.category),
        priority: snapshot.priority,
        status: snapshot.status
    };

    const changes = {};

    Object.keys(formValues).forEach(field => {

        if (formValues[field] !== originalValues[field]) {

            changes[field] = formValues[field];

        }

    });

    return changes;

}


function validateTicketChanges(changes) {

    if ("title" in changes) {

        if (changes.title.length < 3) {
            return "Ticket title must contain at least 3 characters.";
        }

        if (changes.title.length > 200) {
            return "Ticket title must contain at most 200 characters.";
        }

    }

    if ("description" in changes && changes.description.length < 5) {
        return "Description must contain at least 5 characters.";
    }

    if (
        changes.requester_name &&
        changes.requester_name.length > 100
    ) {
        return "Customer name must contain at most 100 characters.";
    }

    if (changes.category && changes.category.length > 100) {
        return "Category must contain at most 100 characters.";
    }

    return null;

}


// =========================================
// SAVE TICKET CHANGES
// =========================================

async function saveTicketChanges(event) {

    event.preventDefault();

    if (!activeTicket || !editSnapshot || !canEditTicket(activeTicket)) {
        return;
    }

    const snapshot = editSnapshot;

    const changes = collectTicketChanges(snapshot);

    if (Object.keys(changes).length === 0) {

        showTicketEditMessage("No changes to save.", "info");

        return;

    }

    const validationError = validateTicketChanges(changes);

    if (validationError) {

        showTicketEditMessage(validationError);

        return;

    }

    const saveButton = getModalElement("saveTicketChangesButton");

    saveButton.disabled = true;
    saveButton.textContent = "Saving...";

    try {

        const updated = await apiRequest(
            `/api/tickets/${snapshot.id}`,
            {
                method: "PATCH",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    ...changes,
                    expected_version: snapshot.version
                })
            },
            "Unable to update ticket."
        );

        assertTicketResponse(updated);

        // The modal may have moved on to another ticket meanwhile.
        if (!activeTicket || activeTicket.id !== snapshot.id) {
            await loadTickets();
            return;
        }

        activeTicket = updated;
        editSnapshot = null;

        renderTicketDetails(activeTicket);

        showTicketReadOnlyMode();

        showTicketDetailsMessage(
            `Ticket #${activeTicket.id} updated successfully.`
        );

        await Promise.all([
            loadTickets(),
            loadTicketHistory(activeTicket.id)
        ]);

    } catch (error) {

        if (isAuthenticationError(error)) {
            return;
        }

        if (error.status === 409) {

            // Keep the user's form values; never retry automatically.
            showTicketEditMessage(
                `${error.message} Your unsaved changes are still ` +
                "in the form.",
                "warning",
                reloadActiveTicket
            );

            return;

        }

        showTicketEditMessage(
            error.message || "Unable to update ticket."
        );

    } finally {

        saveButton.disabled = false;
        saveButton.textContent = "Save Changes";

    }

}


// =========================================
// RELOAD TICKET (AFTER A CONFLICT)
// =========================================

async function reloadActiveTicket() {

    if (!activeTicket) {
        return;
    }

    const ticketId = activeTicket.id;

    try {

        const fresh = await apiRequest(
            `/api/tickets/${ticketId}`,
            {},
            "Unable to reload the ticket."
        );

        assertTicketResponse(fresh);

        if (!activeTicket || activeTicket.id !== ticketId) {
            return;
        }

        activeTicket = fresh;
        editSnapshot = null;

        renderTicketDetails(activeTicket);

        showTicketReadOnlyMode();

        showTicketDetailsMessage(
            "Ticket reloaded with the latest changes.",
            "info"
        );

        await Promise.all([
            loadTickets(),
            loadTicketHistory(ticketId),
            canAssignTicket() ? loadAssignableAgents() : null
        ]);

    } catch (error) {

        if (isAuthenticationError(error)) {
            return;
        }

        const message = error.status === 404
            ? "This ticket no longer exists."
            : (error.message || "Unable to reload the ticket.");

        if (!getModalElement("ticketEditView")?.classList.contains("d-none")) {

            showTicketEditMessage(message);

        } else {

            showTicketDetailsMessage(message, "danger");

        }

        if (error.status === 404) {
            await loadTickets();
        }

    }

}


// =========================================
// VALIDATE TICKET RESPONSES
// =========================================

function assertTicketResponse(ticket) {

    if (
        !ticket ||
        typeof ticket !== "object" ||
        !Number.isSafeInteger(ticket.id)
    ) {

        throw new Error(
            "The server returned an invalid ticket response."
        );

    }

}


// =========================================
// SHOW TICKET DETAILS
// =========================================

function showTicketDetails(ticket) {

    // Store the currently selected ticket.
    activeTicket = ticket;
    editSnapshot = null;

    const modalElement = getTicketDetailsModal();

    renderTicketDetails(ticket);

    // Clear previous messages.
    hideAlert(getModalElement("ticketDetailsAlert"));

    // Always open in read-only mode.
    showTicketReadOnlyMode();

    bootstrap.Modal.getOrCreateInstance(modalElement).show();

    // Staff-only content loads after the modal is visible.
    if (canViewHistory()) {
        loadTicketHistory(ticket.id);
    }

    if (canAssignTicket()) {
        loadAssignableAgents();
    }

}


// =========================================
// ADMIN: LOAD ACTIVE AGENTS
// =========================================

function isAssignableAgent(user) {

    return Boolean(
        user &&
        Number.isSafeInteger(user.id) &&
        typeof user.name === "string" &&
        user.role === "agent" &&
        user.is_active === true
    );

}


// Fetch every active agent, following `total` across pages.
async function fetchActiveAgents() {

    const agents = new Map();

    let skip = 0;
    let total = Infinity;

    for (let page = 0; page < MAX_AGENT_PAGES && skip < total; page++) {

        const params = new URLSearchParams({
            role: "agent",
            is_active: "true",
            limit: String(AGENT_PAGE_SIZE),
            skip: String(skip)
        });

        const data = await apiRequest(
            `/api/admin/users?${params.toString()}`,
            {},
            "Unable to load the list of agents."
        );

        if (
            !data ||
            !Array.isArray(data.items) ||
            !Number.isSafeInteger(data.total) ||
            data.total < 0
        ) {

            throw new Error(
                "The server returned an invalid agent list."
            );

        }

        total = data.total;

        // Defensive filter: only active agents are ever offered.
        data.items
            .filter(isAssignableAgent)
            .forEach(agent => agents.set(agent.id, agent));

        if (data.items.length === 0) {
            break;
        }

        skip += data.items.length;

    }

    return [...agents.values()].sort((first, second) =>
        first.name.localeCompare(second.name) || first.id - second.id
    );

}


function setAssignmentStatus(message, type = "muted") {

    const status = getModalElement("assignmentStatus");

    if (!status) {
        return;
    }

    status.textContent = message;

    status.className = `assignment-status text-${type}`;

}


function setAssignmentControlsDisabled(disabled) {

    const select = getModalElement("assignmentSelect");
    const button = getModalElement("saveAssignmentButton");

    if (select) {
        select.disabled = disabled;
    }

    if (button) {
        button.disabled = disabled;
    }

}


async function loadAssignableAgents() {

    if (!canAssignTicket()) {
        return;
    }

    const requestId = ++agentRequestId;

    setAssignmentControlsDisabled(true);

    setAssignmentStatus("Loading agents...");

    try {

        const agents = await fetchActiveAgents();

        if (requestId !== agentRequestId) {
            return;
        }

        assignableAgents = agents;

        if (activeTicket) {
            renderAssignmentOptions(activeTicket);
        }

        setAssignmentControlsDisabled(false);

        setAssignmentStatus(
            agents.length
                ? ""
                : "There are no active agents to assign."
        );

    } catch (error) {

        if (requestId !== agentRequestId || isAuthenticationError(error)) {
            return;
        }

        assignableAgents = [];

        setAssignmentControlsDisabled(true);

        setAssignmentStatus(
            error.message || "Unable to load the list of agents.",
            "danger"
        );

    }

}


// Build the agent dropdown (DOM + textContent only).
function renderAssignmentOptions(ticket) {

    const select = getModalElement("assignmentSelect");

    if (!select) {
        return;
    }

    select.replaceChildren();

    const unassigned = document.createElement("option");

    unassigned.value = "";
    unassigned.textContent = "Unassigned";

    select.appendChild(unassigned);

    // Disambiguate agents sharing a name.
    const nameCounts = new Map();

    assignableAgents.forEach(agent => {
        nameCounts.set(agent.name, (nameCounts.get(agent.name) || 0) + 1);
    });

    assignableAgents.forEach(agent => {

        const option = document.createElement("option");

        option.value = String(agent.id);
        option.textContent = nameCounts.get(agent.name) > 1
            ? `${agent.name} (#${agent.id})`
            : agent.name;

        select.appendChild(option);

    });

    // Keep showing a current assignee who is no longer assignable,
    // but never allow selecting them.
    if (
        hasAssignee(ticket) &&
        !assignableAgents.some(agent => agent.id === ticket.assigned_to_user_id)
    ) {

        const option = document.createElement("option");

        option.value = String(ticket.assigned_to_user_id);
        option.textContent =
            `${getAssignmentLabel(ticket)} (not an active agent)`;
        option.disabled = true;

        select.appendChild(option);

    }

    select.value = hasAssignee(ticket)
        ? String(ticket.assigned_to_user_id)
        : "";

}


// =========================================
// ADMIN: SAVE ASSIGNMENT
// =========================================

async function saveAssignment() {

    if (!activeTicket || !canAssignTicket()) {
        return;
    }

    const ticket = activeTicket;
    const select = getModalElement("assignmentSelect");
    const button = getModalElement("saveAssignmentButton");

    const selectedValue = select.value;

    const assigneeId = selectedValue === ""
        ? null
        : Number(selectedValue);

    if (
        assigneeId !== null &&
        !assignableAgents.some(agent => agent.id === assigneeId)
    ) {

        showTicketDetailsMessage(
            "Select an active agent, or Unassigned.",
            "danger"
        );

        return;

    }

    const currentAssigneeId = hasAssignee(ticket)
        ? ticket.assigned_to_user_id
        : null;

    if (assigneeId === currentAssigneeId) {

        showTicketDetailsMessage(
            "The assignment has not changed.",
            "info"
        );

        return;

    }

    hideAlert(getModalElement("ticketDetailsAlert"));

    setAssignmentControlsDisabled(true);

    button.textContent = "Saving...";

    try {

        // Unassignment explicitly sends assigned_to_user_id: null.
        const updated = await apiRequest(
            `/api/tickets/${ticket.id}/assignment`,
            {
                method: "PUT",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    assigned_to_user_id: assigneeId,
                    expected_version: ticket.version
                })
            },
            "Unable to update the assignment."
        );

        assertTicketResponse(updated);

        if (!activeTicket || activeTicket.id !== ticket.id) {
            await loadTickets();
            return;
        }

        activeTicket = updated;

        renderTicketDetails(activeTicket);

        let message;

        if (assigneeId === null) {
            message = `Ticket #${updated.id} is now unassigned.`;
        } else if (currentAssigneeId === null) {
            message =
                `Ticket #${updated.id} assigned to ${getAssignmentLabel(updated)}.`;
        } else {
            message =
                `Ticket #${updated.id} reassigned to ${getAssignmentLabel(updated)}.`;
        }

        showTicketDetailsMessage(message);

        await Promise.all([
            loadTickets(),
            loadTicketHistory(updated.id)
        ]);

    } catch (error) {

        if (isAuthenticationError(error)) {
            return;
        }

        if (error.status === 409) {

            showTicketDetailsMessage(
                error.message ||
                "The ticket was changed by someone else.",
                "warning",
                reloadActiveTicket
            );

        } else {

            showTicketDetailsMessage(
                error.message || "Unable to update the assignment.",
                "danger"
            );

        }

        // Restore the dropdown to the ticket's actual assignee.
        if (activeTicket) {
            renderAssignmentOptions(activeTicket);
        }

    } finally {

        button.textContent = "Save Assignment";

        setAssignmentControlsDisabled(false);

    }

}


// =========================================
// AGENT: CLAIM TICKET
// =========================================

async function claimTicket() {

    if (!activeTicket || !canClaimTicket(activeTicket)) {
        return;
    }

    const ticket = activeTicket;
    const button = getModalElement("claimTicketButton");

    hideAlert(getModalElement("ticketDetailsAlert"));

    button.disabled = true;
    button.textContent = "Claiming...";

    try {

        // Single attempt using the version the API last returned.
        const updated = await apiRequest(
            `/api/tickets/${ticket.id}/claim`,
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/json"
                },
                body: JSON.stringify({
                    expected_version: ticket.version
                })
            },
            "Unable to claim the ticket."
        );

        assertTicketResponse(updated);

        if (!activeTicket || activeTicket.id !== ticket.id) {
            await loadTickets();
            return;
        }

        activeTicket = updated;

        renderTicketDetails(activeTicket);

        showTicketDetailsMessage(
            `You claimed ticket #${updated.id}. It is now assigned to you.`
        );

        await Promise.all([
            loadTickets(),
            loadTicketHistory(updated.id)
        ]);

    } catch (error) {

        if (isAuthenticationError(error)) {
            return;
        }

        if (error.status === 409) {

            // e.g. "Ticket is already assigned." - offer a reload,
            // never retry the claim.
            showTicketDetailsMessage(
                `${error.message} Reload the ticket to see its ` +
                "latest state.",
                "warning",
                reloadActiveTicket
            );

        } else {

            showTicketDetailsMessage(
                error.message || "Unable to claim the ticket.",
                "danger"
            );

        }

    } finally {

        button.textContent = "Claim Ticket";

        button.disabled = false;

    }

}


// =========================================
// STAFF: TICKET ACTIVITY HISTORY
// =========================================

function setActivityState(message, type = "muted") {

    const state = getModalElement("ticketActivityState");

    if (!state) {
        return;
    }

    state.textContent = message;

    state.className = message
        ? `activity-state text-${type}`
        : "activity-state d-none";

}


function describeEvent(event) {

    const actor = userLabel(event.actor_user_id);

    switch (event.event_type) {

        case "created":
            return `Ticket created by ${actor}.`;

        case "assigned":
            return event.to_assignee_id === event.actor_user_id
                ? `${agentLabel(event.to_assignee_id)} claimed the ticket.`
                : `${actor} assigned the ticket to ` +
                    `${agentLabel(event.to_assignee_id)}.`;

        case "reassigned":
            return `${actor} reassigned the ticket from ` +
                `${agentLabel(event.from_assignee_id)} to ` +
                `${agentLabel(event.to_assignee_id)}.`;

        case "unassigned": {

            const base = Number.isSafeInteger(event.from_assignee_id)
                ? `${actor} unassigned ${agentLabel(event.from_assignee_id)}`
                : `${actor} unassigned the ticket`;

            const reason = UNASSIGNMENT_REASONS[event.reason];

            let description = reason ? `${base} because ${reason}.` : `${base}.`;

            // Unassigning an "In Progress" ticket also returns it to
            // "Open"; the API records that on this same event.
            if (event.from_status && event.to_status) {
                description +=
                    ` The status changed from ${event.from_status} ` +
                    `to ${event.to_status}.`;
            }

            return description;

        }

        case "status_changed":
            return event.from_status
                ? `${actor} changed the status from ` +
                    `${event.from_status} to ${event.to_status}.`
                : `${actor} set the status to ${event.to_status}.`;

        default:
            return "Ticket activity recorded.";

    }

}


function sortEventsChronologically(events) {

    return [...events].sort((first, second) => {

        const firstTime = parseApiDate(first.created_at)?.getTime() ?? 0;
        const secondTime = parseApiDate(second.created_at)?.getTime() ?? 0;

        return firstTime - secondTime ||
            (Number(first.id) || 0) - (Number(second.id) || 0);

    });

}


function renderActivity(events) {

    const list = getModalElement("ticketActivityList");

    if (!list) {
        return;
    }

    list.replaceChildren();

    if (events.length === 0) {

        setActivityState("No activity history available yet.");

        return;

    }

    setActivityState("");

    sortEventsChronologically(events).forEach(event => {

        const item = document.createElement("li");
        item.className = "activity-item";

        const marker = document.createElement("span");
        marker.className = "activity-marker";
        marker.setAttribute("aria-hidden", "true");

        const body = document.createElement("div");
        body.className = "activity-body";

        const heading = document.createElement("div");
        heading.className = "activity-heading";

        const type = document.createElement("span");
        type.className = "activity-type";
        type.textContent =
            EVENT_LABELS[event.event_type] || "Activity";

        const time = document.createElement("time");
        time.className = "activity-time";

        const date = parseApiDate(event.created_at);

        if (date) {
            time.dateTime = date.toISOString();
        }

        time.textContent = formatDateTime(event.created_at);

        heading.append(type, time);

        const description = document.createElement("p");
        description.className = "activity-description";
        description.textContent = describeEvent(event);

        body.append(heading, description);

        item.append(marker, body);

        list.appendChild(item);

    });

}


async function loadTicketHistory(ticketId) {

    // Customers never request history.
    if (!canViewHistory() || !getModalElement("ticketActivityList")) {
        return;
    }

    const requestId = ++historyRequestId;

    getModalElement("ticketActivityList").replaceChildren();

    setActivityState("Loading activity...");

    try {

        const events = await apiRequest(
            `/api/tickets/${ticketId}/history`,
            {},
            "Unable to load ticket activity."
        );

        if (
            requestId !== historyRequestId ||
            !activeTicket ||
            activeTicket.id !== ticketId
        ) {
            return;
        }

        if (!Array.isArray(events)) {

            throw new Error(
                "The server returned an invalid activity response."
            );

        }

        renderActivity(events);

    } catch (error) {

        if (requestId !== historyRequestId || isAuthenticationError(error)) {
            return;
        }

        // Contained error: the rest of the modal keeps working.
        setActivityState(
            error.message || "Unable to load ticket activity.",
            "danger"
        );

    }

}


// =========================================
// DELETE TICKET (ADMIN)
// =========================================

async function deleteTicket() {

    if (!activeTicket || !canDeleteTicket()) {
        return;
    }

    const confirmed = window.confirm(
        `Are you sure you want to delete ticket #${activeTicket.id}? This action cannot be undone.`
    );

    if (!confirmed) {
        return;
    }

    const deleteButton = getModalElement("deleteTicketButton");

    deleteButton.disabled = true;
    deleteButton.textContent = "Deleting...";

    try {

        const ticketId = activeTicket.id;

        // The API returns HTTP 204 when deletion succeeds.
        await apiRequest(
            `/api/tickets/${ticketId}`,
            { method: "DELETE" },
            "Unable to delete the ticket."
        );

        // Close the details modal.
        const modal = bootstrap.Modal.getInstance(
            getModalElement("ticketDetailsModal")
        );

        if (modal) {
            modal.hide();
        }

        // Clear the selected ticket.
        activeTicket = null;

        // Refresh the ticket list.
        await loadTickets();

        // Display success message.
        showPageMessage(
            `Ticket #${ticketId} deleted successfully.`,
            "success"
        );

    } catch (error) {

        if (isAuthenticationError(error)) {
            return;
        }

        showTicketDetailsMessage(
            error.message || "Unable to delete the ticket.",
            error.status === 409 ? "warning" : "danger",
            error.status === 409 ? reloadActiveTicket : null
        );

    } finally {

        deleteButton.disabled = false;
        deleteButton.textContent = "Delete Ticket";

    }

}


// =========================================
// CREATE TICKET TABLE ROW
// =========================================

function createTicketRow(ticket) {

    const row = document.createElement("tr");


    // Ticket ID.
    row.appendChild(
        createTableCell(formatTicketId(ticket.id))
    );


    // Ticket title.
    row.appendChild(
        createTableCell(ticket.title)
    );


    // Final category or AI prediction.
    const categoryLabel = ticket.category
        ? ticket.category
        : ticket.predicted_category
            ? `AI: ${ticket.predicted_category}`
            : "Unassigned";

    row.appendChild(
        createTableCell(categoryLabel)
    );


    // Priority.
    const priorityClasses = {
        Low: "bg-success-subtle text-success-emphasis",
        Medium: "bg-warning-subtle text-warning-emphasis",
        High: "bg-danger-subtle text-danger-emphasis"
    };

    const priorityBadge = createBadge(
        ticket.priority,
        priorityClasses[ticket.priority] ||
            "bg-secondary-subtle text-secondary-emphasis"
    );

    row.appendChild(
        createTableCell(priorityBadge)
    );


    // Status.
    const statusClasses = {
        "Open": "bg-warning-subtle text-warning-emphasis",
        "In Progress": "bg-info-subtle text-info-emphasis",
        "Resolved": "bg-success-subtle text-success-emphasis"
    };

    const statusBadge = createBadge(
        ticket.status,
        statusClasses[ticket.status] ||
            "bg-secondary-subtle text-secondary-emphasis"
    );

    row.appendChild(
        createTableCell(statusBadge)
    );


    // Assignment (agent name for staff, neutral label for customers).
    row.appendChild(
        createTableCell(createAssignmentLabel(ticket))
    );


    // Creation date.
    row.appendChild(
        createTableCell(
            formatTicketDate(ticket.created_at)
        )
    );


    // View details.
    const viewButton = document.createElement("button");

    viewButton.type = "button";

    viewButton.className =
        "btn btn-outline-primary btn-sm";

    viewButton.textContent = "View";

    viewButton.setAttribute(
        "aria-label",
        `View ticket ${formatTicketId(ticket.id)}`
    );

    viewButton.addEventListener("click", () => {

        showTicketDetails(ticket);

    });

    row.appendChild(
        createTableCell(viewButton)
    );


    return row;

}


// =========================================
// RENDER TICKETS
// =========================================

function renderTableMessage(message) {

    const row = document.createElement("tr");

    const cell = document.createElement("td");

    cell.colSpan = TABLE_COLUMN_COUNT;

    cell.className = "empty-state";

    cell.textContent = message;

    row.appendChild(cell);

    ticketsTableBody.replaceChildren(row);

}


function hasActiveFilters() {

    return Boolean(
        ticketSearchInput.value.trim() ||
        statusFilter.value ||
        priorityFilter.value ||
        categoryFilter.value ||
        (isStaff(currentUser) && assignmentFilter && assignmentFilter.value)
    );

}


function renderTickets(tickets) {

    if (!tickets.length) {

        renderTableMessage(
            hasActiveFilters()
                ? "No tickets found matching your filters."
                : "No support tickets have been created yet."
        );

        return;

    }

    ticketsTableBody.replaceChildren(
        ...tickets.map(createTicketRow)
    );

}


// =========================================
// BUILD TICKET API QUERY
// =========================================

function buildTicketQuery() {

    const params = new URLSearchParams();

    params.set("skip", String(currentPage * PAGE_SIZE));

    params.set("limit", String(PAGE_SIZE));

    const search = ticketSearchInput.value.trim();

    if (search) {

        params.set("search", search);

    }

    if (statusFilter.value) {

        params.set("status", statusFilter.value);

    }

    if (priorityFilter.value) {

        params.set("priority", priorityFilter.value);

    }

    if (categoryFilter.value) {

        params.set("category", categoryFilter.value);

    }

    // Staff only; customers never send assigned_to.
    if (
        isStaff(currentUser) &&
        assignmentFilter &&
        ["me", "none"].includes(assignmentFilter.value)
    ) {

        params.set("assigned_to", assignmentFilter.value);

    }

    return params.toString();

}


// =========================================
// PAGINATION
// =========================================

// Parse X-Total-Count; null when missing or invalid.
function parseTotalCount(response) {

    const raw = response.headers.get("X-Total-Count");

    if (raw === null) {
        return null;
    }

    const text = raw.trim();

    if (!/^\d+$/.test(text)) {
        return null;
    }

    const total = Number(text);

    return Number.isSafeInteger(total) ? total : null;

}


function setPaginationLoading() {

    previousPageButton.disabled = true;

    nextPageButton.disabled = true;

}


function updatePagination(ticketCount, total) {

    const start = ticketCount ? currentPage * PAGE_SIZE + 1 : 0;
    const end = currentPage * PAGE_SIZE + ticketCount;

    previousPageButton.disabled = currentPage === 0;

    if (total !== null) {

        const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

        ticketResultCount.textContent = total
            ? `Showing ${start}-${end} of ${total} tickets`
            : "No tickets found";

        paginationInfo.textContent =
            `Page ${currentPage + 1} of ${totalPages}`;

        nextPageButton.disabled = currentPage >= totalPages - 1;

        return;

    }

    // Header missing or invalid: a full page may have a successor.
    ticketResultCount.textContent = ticketCount
        ? `Showing ${start}-${end} tickets on this page`
        : "No tickets found";

    paginationInfo.textContent = `Page ${currentPage + 1}`;

    nextPageButton.disabled = ticketCount < PAGE_SIZE;

}


// =========================================
// LOAD TICKETS FROM FASTAPI
// =========================================

async function loadTickets() {

    const requestId = ++latestRequestId;

    clearPageMessage();

    setPaginationLoading();

    renderTableMessage("Loading support tickets...");

    try {

        const response = await authenticatedFetch(
            `/api/tickets?${buildTicketQuery()}`
        );

        await checkApiResponse(
            response,
            "Unable to retrieve support tickets."
        );

        const results = await readJsonResponse(response);

        // Ignore an older response if the user changed filters.
        if (requestId !== latestRequestId) {
            return;
        }

        if (!Array.isArray(results)) {

            throw new Error(
                "The server returned an invalid ticket response."
            );

        }

        const total = parseTotalCount(response);

        // The current page no longer exists (e.g. after a deletion):
        // move to the last available page.
        if (total !== null && currentPage > 0) {

            const lastPage = Math.max(
                0,
                Math.ceil(total / PAGE_SIZE) - 1
            );

            if (currentPage > lastPage) {

                currentPage = lastPage;

                await loadTickets();

                return;

            }

        }

        // Preserve the API's ordering.
        const tickets = results.slice(0, PAGE_SIZE);

        renderTickets(tickets);

        updatePagination(tickets.length, total);

    } catch (error) {

        if (
            requestId !== latestRequestId ||
            isAuthenticationError(error)
        ) {
            return;
        }

        showPageMessage(
            error.message || "Unable to retrieve support tickets."
        );

        renderTableMessage("Unable to load tickets.");

        ticketResultCount.textContent = "Unable to load tickets.";

        paginationInfo.textContent = `Page ${currentPage + 1}`;

        // Allow moving back; the next page is unknown.
        previousPageButton.disabled = currentPage === 0;

        nextPageButton.disabled = true;

    }

}


// =========================================
// SEARCH AND FILTER EVENTS
// =========================================

function resetPageAndLoadTickets() {

    currentPage = 0;

    loadTickets();

}


ticketSearchInput.addEventListener("input", () => {

    clearTimeout(searchDebounce);

    searchDebounce = setTimeout(() => {

        resetPageAndLoadTickets();

    }, 350);

});


[statusFilter, priorityFilter, categoryFilter, assignmentFilter]
    .filter(Boolean)
    .forEach(filter => {

        filter.addEventListener(
            "change",
            resetPageAndLoadTickets
        );

    });


// =========================================
// CLEAR FILTERS
// =========================================

clearFiltersButton.addEventListener("click", () => {

    ticketSearchInput.value = "";

    statusFilter.value = "";

    priorityFilter.value = "";

    categoryFilter.value = "";

    if (assignmentFilter) {
        assignmentFilter.value = "";
    }

    clearTimeout(searchDebounce);

    resetPageAndLoadTickets();

});


// =========================================
// PAGINATION EVENTS
// =========================================

previousPageButton.addEventListener("click", () => {

    if (currentPage > 0) {

        currentPage--;

        loadTickets();

    }

});


nextPageButton.addEventListener("click", () => {

    if (!nextPageButton.disabled) {

        currentPage++;

        loadTickets();

    }

});


// =========================================
// INITIALISE TICKETS PAGE
// =========================================

async function initialiseTicketsPage() {

    if (!requireAuthentication()) {

        return;

    }

    bindLogoutButton(
        logoutButton,
        showPageMessage
    );

    setPaginationLoading();

    // The role decides which columns, filters and actions are shown,
    // so it is confirmed before tickets are rendered. If it cannot be
    // loaded, no role is assumed (customer-safe view, no actions).
    currentUser = await initialiseUserInterface({
        onError: showProfileMessage
    });

    if (isStaff(currentUser)) {

        filterGrid.classList.add("has-assignment-filter");

    }

    configureAssignmentFilter();

    const ticketToOpen = applyFiltersFromUrl();

    await loadTickets();

    if (ticketToOpen !== null) {

        await openTicketById(ticketToOpen);

    }

}


// =========================================
// ROLE-SPECIFIC ASSIGNMENT FILTER
// =========================================

// Admins manage tickets but are never assignees, so "Assigned to Me"
// is offered to agents only. Customers never see the filter.
function configureAssignmentFilter() {

    if (!assignmentFilter || isAgent(currentUser)) {
        return;
    }

    assignmentFilter
        .querySelector('option[value="me"]')
        ?.remove();

}


// =========================================
// DEEP LINKS FROM THE DASHBOARD
// =========================================

function hasOption(select, value) {

    return Boolean(
        select &&
        value &&
        [...select.options].some(option => option.value === value)
    );

}


// Apply ?status= and ?assigned_to= presets (only values the current
// role can actually select) and return a ?ticket= ID to open, if any.
function applyFiltersFromUrl() {

    const params = new URLSearchParams(window.location.search);

    const status = params.get("status");

    if (hasOption(statusFilter, status)) {
        statusFilter.value = status;
    }

    const assignedTo = params.get("assigned_to");

    if (isStaff(currentUser) && hasOption(assignmentFilter, assignedTo)) {
        assignmentFilter.value = assignedTo;
    }

    const ticket = params.get("ticket");

    return ticket && /^[1-9][0-9]{0,15}$/.test(ticket)
        ? Number(ticket)
        : null;

}


// Open a ticket's details by ID (the API enforces who may read it).
async function openTicketById(ticketId) {

    try {

        const ticket = await apiRequest(
            `/api/tickets/${ticketId}`,
            {},
            "Unable to open the ticket."
        );

        assertTicketResponse(ticket);

        showTicketDetails(ticket);

    } catch (error) {

        if (isAuthenticationError(error)) {
            return;
        }

        showPageMessage(
            error.status === 404
                ? `Ticket #${ticketId} was not found.`
                : errorMessageFrom(error),
            "warning"
        );

    }

}


function errorMessageFrom(error) {

    return (error && error.message) || "Unable to open the ticket.";

}


// Start the tickets page.
initialiseTicketsPage();
