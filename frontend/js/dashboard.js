
"use strict";

// =========================================
// RESOLVEAI - DASHBOARD FUNCTIONALITY
// =========================================

const TOKEN_KEY = "resolveai_access_token";

const dashboardContent = document.querySelector(
    ".dashboard-content"
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


// =========================================
// CHECK AUTHENTICATION
// =========================================

function getAccessToken() {

    return sessionStorage.getItem(TOKEN_KEY);

}


function redirectToLogin() {

    sessionStorage.removeItem(TOKEN_KEY);

    window.location.replace("/");

}


// =========================================
// AUTHENTICATED API REQUEST
// =========================================

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

    // Handle expired or invalid JWT tokens.
    if (response.status === 401) {

        redirectToLogin();

        throw new Error("Your session has expired.");

    }

    return response;

}


// =========================================
// DASHBOARD ERROR MESSAGE
// =========================================

function showDashboardMessage(message) {

    let alertElement = document.getElementById(
        "dashboardMessage"
    );

    if (!alertElement) {

        alertElement = document.createElement("div");

        alertElement.id = "dashboardMessage";

        dashboardContent.prepend(alertElement);

    }

    alertElement.className = "alert alert-danger";

    alertElement.setAttribute("role", "alert");

    alertElement.textContent = message;

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
                "Unable to retrieve your profile."
            );

        }

        const user = await response.json();

        // Display agent's full name.
        document.getElementById("agentName").textContent =
            user.name;

        // Display agent's role.
        const formattedRole = user.role
            .replace(/_/g, " ")
            .replace(/\b\w/g, character =>
                character.toUpperCase()
            );

        document.getElementById("agentRole").textContent =
            formattedRole;

        // Display first name in welcome message.
        const firstName = user.name.trim().split(/\s+/)[0];

        document.getElementById("welcomeName").textContent =
            firstName;

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

            showDashboardMessage(error.message);

        }

    }

}


// =========================================
// CREATE A BADGE ELEMENT
// =========================================

function createBadge(text, cssClass) {

    const badge = document.createElement("span");

    badge.className = `badge ${cssClass}`;

    badge.textContent = text;

    return badge;

}


// =========================================
// CREATE TICKET TABLE CELL
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
// CREATE A TICKET TABLE ROW
// =========================================

function createTicketRow(ticket) {

    const row = document.createElement("tr");

    // Ticket ID.
    const ticketId = `#${String(ticket.id).padStart(4, "0")}`;

    row.appendChild(
        createTableCell(ticketId)
    );


    // Ticket title.
    row.appendChild(
        createTableCell(ticket.title)
    );


    // Final category or AI suggestion.
    let categoryBadge;

    if (ticket.category) {

        categoryBadge = createBadge(
            ticket.category,
            "bg-light text-dark"
        );

    } else if (ticket.predicted_category) {

        categoryBadge = createBadge(
            `AI: ${ticket.predicted_category}`,
            "bg-info-subtle text-info-emphasis"
        );

    } else {

        categoryBadge = createBadge(
            "Unassigned",
            "bg-secondary-subtle text-secondary-emphasis"
        );

    }

    row.appendChild(
        createTableCell(categoryBadge)
    );


    // Ticket priority.
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


    // Ticket status.
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

    return row;

}


// =========================================
// DISPLAY RECENT TICKETS
// =========================================

function renderRecentTickets(tickets) {

    recentTicketsBody.replaceChildren();

    if (!tickets.length) {

        const row = document.createElement("tr");

        const cell = document.createElement("td");

        cell.colSpan = 5;

        cell.className = "empty-state";

        cell.textContent =
            "No support tickets have been created yet.";

        row.appendChild(cell);

        recentTicketsBody.appendChild(row);

        return;

    }

    tickets.forEach(ticket => {

        const row = createTicketRow(ticket);

        recentTicketsBody.appendChild(row);

    });

}


// =========================================
// LOAD RECENT TICKETS FROM API
// =========================================

async function loadRecentTickets() {

    try {

        const response = await authenticatedFetch(
            "/api/tickets?skip=0&limit=5"
        );

        if (!response.ok) {

            throw new Error(
                "Unable to retrieve support tickets."
            );

        }

        const tickets = await response.json();

        if (!Array.isArray(tickets)) {

            throw new Error(
                "The server returned an invalid ticket response."
            );

        }

        renderRecentTickets(tickets);

    } catch (error) {

        if (getAccessToken()) {

            showDashboardMessage(error.message);

        }

    }

}

 // =========================================
 // CREATE TICKET MESSAGE HANDLING
 // =========================================

function showCreateTicketMessage(message, type = "danger") {

    createTicketAlert.textContent = message;

    createTicketAlert.className = `alert alert-${type}`;

}


function hideCreateTicketMessage() {

    createTicketAlert.textContent = "";

    createTicketAlert.className = "alert d-none";

}


// =========================================
// HANDLE CREATE TICKET SUBMISSION
// =========================================

async function createTicket(event) {

    event.preventDefault();

    hideCreateTicketMessage();

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

    saveTicketButton.textContent = "Creating Ticket...";


    try {

        // Send ticket creation request to FastAPI.
        const response = await authenticatedFetch(
            "/api/tickets",
            {
                method: "POST",

                headers: {
                    "Content-Type": "application/json"
                },

                body: JSON.stringify(ticketData)
            }
        );


        // Read the server response.
        let responseData = {};

        try {

            responseData = await response.json();

        } catch {

            throw new Error(
                "Unable to read the server response."
            );

        }


        // Handle unsuccessful ticket creation.
        if (!response.ok) {

            let errorMessage =
                "Unable to create the support ticket.";

            if (typeof responseData.detail === "string") {

                errorMessage = responseData.detail;

            } else if (Array.isArray(responseData.detail)) {

                errorMessage = responseData.detail
                    .map(error => error.msg)
                    .join(". ");

            }

            throw new Error(errorMessage);

        }


        // Build the AI prediction message.
        let resultMessage =
            `Ticket #${responseData.id} created successfully. `;

        if (responseData.predicted_category) {

            resultMessage +=
                `AI suggested category: ${responseData.predicted_category}. `;

            if (
                typeof responseData.prediction_confidence === "number"
            ) {

                const confidence = (
                    responseData.prediction_confidence * 100
                ).toFixed(1);

                resultMessage +=
                    `Model confidence: ${confidence}%.`;

            }

        } else {

            resultMessage +=
                "AI prediction was unavailable, but the ticket was saved.";

        }


        // Reset form after successful creation.
        createTicketForm.reset();


        // Display success message.
        showCreateTicketMessage(
            resultMessage,
            "success"
        );


        // Refresh the recent tickets table.
        await loadRecentTickets();


    } catch (error) {

        if (getAccessToken()) {

            showCreateTicketMessage(
                error.message ||
                "Something went wrong while creating the ticket."
            );

        }

    } finally {

        // Restore the submit button.
        saveTicketButton.disabled = false;

        saveTicketButton.textContent = "Create Ticket";

    }

}


 // =========================================
 // LOAD DASHBOARD ANALYTICS
 // =========================================

async function loadTicketSummary() {

    try {

        const response = await authenticatedFetch(
            "/api/analytics/summary"
        );

        if (!response.ok) {

            throw new Error(
                "Unable to retrieve dashboard analytics."
            );

        }

        const summary = await response.json();

        // Map API response fields to dashboard elements.
        const statistics = [
            ["totalTickets", summary.total_tickets],
            ["openTickets", summary.open_tickets],
            ["progressTickets", summary.in_progress_tickets],
            ["resolvedTickets", summary.resolved_tickets]
        ];

        // Update the dashboard cards.
        statistics.forEach(([elementId, value]) => {

            const element = document.getElementById(elementId);

            if (element) {

                element.textContent = value.toLocaleString();

            }

        });

    } catch (error) {

        if (getAccessToken()) {

            showDashboardMessage(error.message);

        }

    }

}

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

        // Clear the browser session after successful revocation.
        sessionStorage.removeItem(TOKEN_KEY);

        window.location.replace("/");

    } catch (error) {

        if (getAccessToken()) {

            showDashboardMessage(error.message);

            logoutButton.disabled = false;

            logoutButton.textContent = "Logout";

        }

    }

}


// =========================================
// INITIALISE DASHBOARD
// =========================================

async function initialiseDashboard() {

    const token = getAccessToken();

    if (!token) {

        redirectToLogin();

        return;

    }

    // Register logout functionality.
    logoutButton.addEventListener(
        "click",
        logout
    );
    // Register Create Ticket functionality.
createTicketButton.addEventListener(
    "click",
    function () {

        createTicketForm.reset();

        hideCreateTicketMessage();

        const modal = bootstrap.Modal.getOrCreateInstance(
            createTicketModalElement
        );

        modal.show();

    }
);


// Register ticket form submission.
createTicketForm.addEventListener(
    "submit",
    createTicket
);

    // Load profile and tickets independently.
    await Promise.all([
        loadAgentProfile(),
        loadRecentTickets(),
        loadTicketSummary()
    ]);

}


// Start the dashboard.
initialiseDashboard();
