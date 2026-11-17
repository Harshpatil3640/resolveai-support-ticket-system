
"use strict";

// =========================================
// RESOLVEAI - TICKET MANAGEMENT
// =========================================

const TOKEN_KEY = "resolveai_access_token";
const PAGE_SIZE = 10;

let currentPage = 0;
let latestRequestId = 0;
let searchDebounce;
// Currently selected ticket in the details modal.
let activeTicket = null;


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


// =========================================
// AUTHENTICATION
// =========================================

function getAccessToken() {

    return sessionStorage.getItem(TOKEN_KEY);

}


function redirectToLogin() {

    sessionStorage.removeItem(TOKEN_KEY);

    window.location.replace("/");

}


async function authenticatedFetch(url, options = {}) {

    const token = getAccessToken();

    if (!token) {

        redirectToLogin();

        throw new Error("Authentication required.");

    }

    const headers = new Headers(options.headers || {});

    headers.set("Authorization", `Bearer ${token}`);

    const response = await fetch(url, {
        ...options,
        headers
    });

    if (response.status === 401) {

        redirectToLogin();

        throw new Error("Your session has expired.");

    }

    return response;

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

        alertElement.className = `alert alert-${type}`;
        const content = document.querySelector(
            ".tickets-page-content"
        );

        content.prepend(alertElement);

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


// =========================================
// LOAD AUTHENTICATED AGENT PROFILE
// =========================================

async function loadAgentProfile() {

    try {

        const response = await authenticatedFetch(
            "/api/auth/me"
        );

        if (!response.ok) {

            throw new Error(
                "Unable to retrieve agent profile."
            );

        }

        const user = await response.json();

        // Display agent name.
        document.getElementById("agentName").textContent =
            user.name;

        // Format agent role.
        const formattedRole = user.role
            .replace(/_/g, " ")
            .replace(/\b\w/g, character =>
                character.toUpperCase()
            );

        document.getElementById("agentRole").textContent =
            formattedRole;

        // Generate agent initials.
        const initials = user.name
            .trim()
            .split(/\s+/)
            .slice(0, 2)
            .map(name => name.charAt(0))
            .join("")
            .toUpperCase();

        document.getElementById("agentAvatar").textContent =
            initials || "AG";

    } catch (error) {

        if (getAccessToken()) {

            showPageMessage(error.message);

        }

    }

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

function formatTicketDate(dateValue) {

    const date = new Date(dateValue);

    if (Number.isNaN(date.getTime())) {

        return "—";

    }

    return date.toLocaleDateString("en-IN", {
        day: "2-digit",
        month: "short",
        year: "numeric"
    });

}


// =========================================
// TICKET DETAILS MODAL
// =========================================


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

    modal.innerHTML = `
        <div class="modal-dialog modal-dialog-centered modal-lg">
            <div class="modal-content">

                <!-- Modal header -->
                <div class="modal-header">
                    <div>
                        <h5 class="modal-title">
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
                    class="alert alert-success d-none"
                    role="alert"
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

                    </div>


                    <!-- EDIT FORM -->
                    <div id="ticketEditView" class="d-none">

                        <form id="ticketEditForm">

                            <div
                                id="ticketEditAlert"
                                class="alert d-none"
                                role="alert"
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
                                    >
                                        <option value="Open">Open</option>
                                        <option value="In Progress">
                                            In Progress
                                        </option>
                                        <option value="Resolved">
                                            Resolved
                                        </option>
                                    </select>

                                </div>

                            </div>


                            <div class="alert alert-info mt-3 mb-0 small">
                                AI prediction and model confidence are
                                preserved automatically when editing a ticket.
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
                        class="btn btn-outline-danger"
                        id="deleteTicketButton"
                    >
                        Delete Ticket
                    </button>

                    <button
                        type="button"
                        class="btn btn-primary"
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

    document.body.appendChild(modal);

// Register Edit Ticket button.
modal.querySelector("#editTicketButton").addEventListener(
    "click",
    enterTicketEditMode
);

// Register Delete Ticket functionality.
modal.querySelector("#deleteTicketButton").addEventListener(
    "click",
    deleteTicket
);
// Register Cancel button.
modal.querySelector("#cancelTicketEditButton").addEventListener(
    "click",
    showTicketReadOnlyMode
);

// Register edit form submission.
modal.querySelector("#ticketEditForm").addEventListener(
    "submit",
    saveTicketChanges
);

// Restore read-only mode whenever the modal closes.
modal.addEventListener(
    "hidden.bs.modal",
    showTicketReadOnlyMode
);

return modal;

}



// =========================================
// POPULATE TICKET DETAILS
// =========================================

function populateTicketDetails(ticket) {

    const ticketId =
        `#${String(ticket.id).padStart(4, "0")}`;

    const details = {

        detailTicketId: ticketId,
        detailTitle: ticket.title,
        detailDescription: ticket.description,
        detailRequester: ticket.requester_name || "Not provided",
        detailPriority: ticket.priority,
        detailStatus: ticket.status,
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

        document.getElementById(elementId).textContent = value;

    });

}


// =========================================
// SWITCH TO READ-ONLY MODE
// =========================================

function showTicketReadOnlyMode() {

    document.getElementById("ticketReadOnlyView")
        .classList.remove("d-none");

    document.getElementById("ticketEditView")
        .classList.add("d-none");

    document.getElementById("ticketDetailsFooter")
        .classList.remove("d-none");

    document.getElementById("ticketEditFooter")
        .classList.add("d-none");

    document.getElementById("ticketEditAlert").className =
        "alert d-none";

}


// =========================================
// SWITCH TO EDIT MODE
// =========================================

function enterTicketEditMode() {

    if (!activeTicket) {
        return;
    }

    // Populate form with existing ticket information.
    document.getElementById("editTicketTitle").value =
        activeTicket.title;

    document.getElementById("editTicketDescription").value =
        activeTicket.description;

    document.getElementById("editRequesterName").value =
        activeTicket.requester_name || "";

    document.getElementById("editTicketCategory").value =
        activeTicket.category || "";

    document.getElementById("editTicketPriority").value =
        activeTicket.priority;

    document.getElementById("editTicketStatus").value =
        activeTicket.status;


    // Hide any previous success message.
    document.getElementById("ticketDetailsAlert").className =
        "alert alert-success d-none";


    // Switch from read-only to edit mode.
    document.getElementById("ticketReadOnlyView")
        .classList.add("d-none");

    document.getElementById("ticketEditView")
        .classList.remove("d-none");

    document.getElementById("ticketDetailsFooter")
        .classList.add("d-none");

    document.getElementById("ticketEditFooter")
        .classList.remove("d-none");

}


// =========================================
// DISPLAY EDIT FORM MESSAGE
// =========================================

function showTicketEditMessage(message, type = "danger") {

    const alertElement = document.getElementById(
        "ticketEditAlert"
    );

    alertElement.textContent = message;

    alertElement.className = `alert alert-${type}`;

}


// =========================================
// SAVE TICKET CHANGES
// =========================================

async function saveTicketChanges(event) {

    event.preventDefault();

    if (!activeTicket) {
        return;
    }

    const saveButton = document.getElementById(
        "saveTicketChangesButton"
    );

    saveButton.disabled = true;
    saveButton.textContent = "Saving...";


    try {

        // Collect and clean the editable fields.
        const title = document.getElementById(
            "editTicketTitle"
        ).value.trim();

        const description = document.getElementById(
            "editTicketDescription"
        ).value.trim();

        const requesterName = document.getElementById(
            "editRequesterName"
        ).value.trim();

        const category = document.getElementById(
            "editTicketCategory"
        ).value.trim();


        // Validate trimmed values.
        if (title.length < 3) {

            throw new Error(
                "Ticket title must contain at least 3 characters."
            );

        }

        if (description.length < 5) {

            throw new Error(
                "Description must contain at least 5 characters."
            );

        }


        // Construct the PATCH request payload.
        const updates = {

            title: title,

            description: description,

            requester_name: requesterName || null,

            category: category || null,

            priority: document.getElementById(
                "editTicketPriority"
            ).value,

            status: document.getElementById(
                "editTicketStatus"
            ).value

        };


        // Send updates to FastAPI.
        const response = await authenticatedFetch(
            `/api/tickets/${activeTicket.id}`,
            {
                method: "PATCH",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify(updates)
            }
        );


        // Read server response.
        let responseData = {};

        try {

            responseData = await response.json();

        } catch {

            throw new Error(
                "Unable to read the server response."
            );

        }


        // Handle API validation errors.
        if (!response.ok) {

            let errorMessage = "Unable to update ticket.";

            if (typeof responseData.detail === "string") {

                errorMessage = responseData.detail;

            } else if (Array.isArray(responseData.detail)) {

                errorMessage = responseData.detail
                    .map(error => error.msg)
                    .join(". ");

            }

            throw new Error(errorMessage);

        }


        // Update the currently selected ticket.
        activeTicket = responseData;


        // Refresh the read-only details.
        populateTicketDetails(activeTicket);

        showTicketReadOnlyMode();


        // Display successful update message.
        const successAlert = document.getElementById(
            "ticketDetailsAlert"
        );

        successAlert.textContent =
            `Ticket #${activeTicket.id} updated successfully.`;

        successAlert.className = "alert alert-success";


        // Refresh ticket listing.
        await loadTickets();


    } catch (error) {

        if (getAccessToken()) {

            showTicketEditMessage(
                error.message || "Unable to update ticket."
            );

        }

    } finally {

        saveButton.disabled = false;

        saveButton.textContent = "Save Changes";

    }

}


// =========================================
// SHOW TICKET DETAILS
// =========================================

function showTicketDetails(ticket) {

    // Store the currently selected ticket.
    activeTicket = ticket;

    const modalElement = getTicketDetailsModal();


    // Populate read-only details.
    populateTicketDetails(ticket);


    // Clear previous success message.
    document.getElementById("ticketDetailsAlert").className =
        "alert alert-success d-none";


    // Always open in read-only mode.
    showTicketReadOnlyMode();


    // Display modal.
    const modal = bootstrap.Modal.getOrCreateInstance(
        modalElement
    );

    modal.show();

}


// =========================================
// DELETE TICKET
// =========================================

async function deleteTicket() {

    if (!activeTicket) {
        return;
    }

    const confirmed = window.confirm(
        `Are you sure you want to delete ticket #${activeTicket.id}? This action cannot be undone.`
    );

    if (!confirmed) {
        return;
    }

    const deleteButton = document.getElementById(
        "deleteTicketButton"
    );

    deleteButton.disabled = true;
    deleteButton.textContent = "Deleting...";

    try {

        const ticketId = activeTicket.id;

        // Send DELETE request to FastAPI.
        const response = await authenticatedFetch(
            `/api/tickets/${ticketId}`,
            {
                method: "DELETE"
            }
        );

        // The API returns HTTP 204 when deletion succeeds.
        if (!response.ok) {

            let message = "Unable to delete the ticket.";

            try {
                const errorData = await response.json();

                if (typeof errorData.detail === "string") {
                    message = errorData.detail;
                }
            } catch {
                // Keep the default error message.
            }

            throw new Error(message);
        }

        // Close the details modal.
        const modalElement = document.getElementById(
            "ticketDetailsModal"
        );

        const modal = bootstrap.Modal.getInstance(modalElement);

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

        if (getAccessToken()) {

            showPageMessage(error.message);

        }

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

    const ticketId = `#${String(ticket.id).padStart(4, "0")}`;


    // Ticket ID.
    row.appendChild(
        createTableCell(ticketId)
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

function renderTickets(tickets) {

    ticketsTableBody.replaceChildren();

    if (!tickets.length) {

        const row = document.createElement("tr");

        const cell = document.createElement("td");

        cell.colSpan = 7;

        cell.className = "empty-state";

        cell.textContent =
            "No tickets found matching your filters.";

        row.appendChild(cell);

        ticketsTableBody.appendChild(row);

        return;

    }

    tickets.forEach(ticket => {

        ticketsTableBody.appendChild(
            createTicketRow(ticket)
        );

    });

}


// =========================================
// BUILD TICKET API QUERY
// =========================================

function buildTicketQuery() {

    const params = new URLSearchParams();

    params.set("skip", String(currentPage * PAGE_SIZE));

    // Fetch one additional record to determine whether
    // another page exists.
    params.set("limit", String(PAGE_SIZE + 1));

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

    return params.toString();

}


// =========================================
// LOAD TICKETS FROM FASTAPI
// =========================================

async function loadTickets() {

    const requestId = ++latestRequestId;

    clearPageMessage();

    ticketsTableBody.innerHTML = `
        <tr>
            <td colspan="7" class="empty-state">
                Loading support tickets...
            </td>
        </tr>
    `;

    try {

        const query = buildTicketQuery();

        const response = await authenticatedFetch(
            `/api/tickets?${query}`
        );

        if (!response.ok) {

            throw new Error(
                "Unable to retrieve support tickets."
            );

        }

        const results = await response.json();

        // Ignore an older response if the user changed filters.
        if (requestId !== latestRequestId) {

            return;

        }

        if (!Array.isArray(results)) {

            throw new Error(
                "The server returned an invalid ticket response."
            );

        }

        // Detect whether another page exists.
        const hasNextPage = results.length > PAGE_SIZE;

        const tickets = results.slice(0, PAGE_SIZE);

        renderTickets(tickets);


        // Update result information.
        const start = tickets.length
            ? currentPage * PAGE_SIZE + 1
            : 0;

        const end = currentPage * PAGE_SIZE + tickets.length;

        ticketResultCount.textContent = tickets.length
            ? `Showing ${start}-${end} tickets on this page`
            : "No tickets found";


        // Update pagination controls.
        paginationInfo.textContent =
            `Page ${currentPage + 1}`;

        previousPageButton.disabled =
            currentPage === 0;

        nextPageButton.disabled =
            !hasNextPage;


    } catch (error) {

        if (
            requestId === latestRequestId &&
            getAccessToken()
        ) {

            showPageMessage(error.message);

            renderTickets([]);

            ticketResultCount.textContent = "Unable to load tickets.";

        }

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


statusFilter.addEventListener(
    "change",
    resetPageAndLoadTickets
);


priorityFilter.addEventListener(
    "change",
    resetPageAndLoadTickets
);


categoryFilter.addEventListener(
    "change",
    resetPageAndLoadTickets
);


// =========================================
// CLEAR FILTERS
// =========================================

clearFiltersButton.addEventListener("click", () => {

    ticketSearchInput.value = "";

    statusFilter.value = "";

    priorityFilter.value = "";

    categoryFilter.value = "";

    clearTimeout(searchDebounce);

    resetPageAndLoadTickets();

});


// =========================================
// PAGINATION
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
// LOGOUT
// =========================================

async function logout() {

    logoutButton.disabled = true;

    logoutButton.textContent = "Logging out...";

    try {

        const response = await authenticatedFetch(
            "/api/auth/logout",
            {
                method: "POST"
            }
        );

        if (!response.ok) {

            throw new Error(
                "Unable to complete logout. Please try again."
            );

        }

        sessionStorage.removeItem(TOKEN_KEY);

        window.location.replace("/");

    } catch (error) {

        if (getAccessToken()) {

            showPageMessage(error.message);

            logoutButton.disabled = false;

            logoutButton.textContent = "Logout";

        }

    }

}


// =========================================
// INITIALISE TICKETS PAGE
// =========================================

async function initialiseTicketsPage() {

    if (!getAccessToken()) {

        redirectToLogin();

        return;

    }

    logoutButton.addEventListener(
        "click",
        logout
    );

    await Promise.all([
        loadAgentProfile(),
        loadTickets()
    ]);

}


// Start the tickets page.
initialiseTicketsPage();
