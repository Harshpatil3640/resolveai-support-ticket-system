"use strict";

// =========================================
// RESOLVEAI - ANALYTICS FUNCTIONALITY
// =========================================

// Shared authentication, API error and role helpers (common.js).
const {
    authenticatedFetch,
    checkApiResponse,
    getAccessToken,
    requireAuthentication,
    initialiseUserInterface,
    bindLogoutButton,
    isStaff
} = window.ResolveAI;

const logoutButton = document.getElementById("logoutButton");
const analyticsPageAlert = document.getElementById("analyticsPageAlert");

const SVG_NAMESPACE = "http://www.w3.org/2000/svg";


// =========================================
// PAGE MESSAGE
// =========================================

function showAnalyticsMessage(message, type = "danger") {
    analyticsPageAlert.textContent = message;

    analyticsPageAlert.className = `alert alert-${type}`;

    analyticsPageAlert.setAttribute("role", "alert");
}


function clearAnalyticsMessage() {
    analyticsPageAlert.textContent = "";
    analyticsPageAlert.className = "alert d-none";
}


// =========================================
// SAFE DOM HELPERS
// =========================================

function setElementText(elementId, value) {
    const element = document.getElementById(elementId);

    if (element) {
        element.textContent = value;
    }
}


function setContainerMessage(containerId, message) {
    const container = document.getElementById(containerId);

    if (!container) {
        return;
    }

    const paragraph = document.createElement("p");

    paragraph.className = "analytics-empty-state";
    paragraph.textContent = message;

    container.replaceChildren(paragraph);
}


function createSvgElement(tagName, attributes = {}) {
    const element = document.createElementNS(
        SVG_NAMESPACE,
        tagName
    );

    Object.entries(attributes).forEach(([key, value]) => {
        element.setAttribute(key, value);
    });

    return element;
}


// =========================================
// FORMAT VALUES
// =========================================

function formatNumber(value) {
    return Number(value).toLocaleString("en-IN");
}


function formatPercentage(value) {
    return `${(Number(value) * 100).toFixed(2)}%`;
}


function formatDateLabel(dateString) {
    const date = new Date(`${dateString}T00:00:00`);

    if (Number.isNaN(date.getTime())) {
        return dateString;
    }

    return new Intl.DateTimeFormat("en-IN", {
        day: "2-digit",
        month: "short"
    }).format(date);
}


// =========================================
// GENERIC BAR CHART
// =========================================

function renderBarChart(containerId, items, labelKey) {
    const container = document.getElementById(containerId);

    if (!container) {
        return;
    }

    container.replaceChildren();

    if (!Array.isArray(items) || items.length === 0) {
        setContainerMessage(
            containerId,
            "No ticket data available yet."
        );
        return;
    }

    const validItems = items.filter(item =>
        typeof item[labelKey] === "string" &&
        Number.isFinite(Number(item.count)) &&
        Number(item.count) >= 0
    );

    if (validItems.length === 0) {
        setContainerMessage(
            containerId,
            "No valid distribution data available."
        );
        return;
    }

    const maximum = Math.max(
        ...validItems.map(item => Number(item.count)),
        1
    );

    const list = document.createElement("div");
    list.className = "analytics-bar-list";

    validItems.forEach(item => {
        const count = Number(item.count);

        const row = document.createElement("div");
        row.className = "analytics-bar-row";

        const label = document.createElement("span");
        label.className = "analytics-bar-label";
        label.textContent = item[labelKey];

        const track = document.createElement("div");
        track.className = "analytics-bar-track";

        const fill = document.createElement("div");
        fill.className = "analytics-bar-fill";

        const percentage = (count / maximum) * 100;

        fill.style.width = `${percentage}%`;

        track.appendChild(fill);

        const value = document.createElement("span");
        value.className = "analytics-bar-value";
        value.textContent = formatNumber(count);

        row.append(
            label,
            track,
            value
        );

        list.appendChild(row);
    });

    container.appendChild(list);
}


// =========================================
// RENDER DAILY TICKET TREND
// =========================================

function renderTicketTrends(trends) {
    const container = document.getElementById(
        "ticketTrendChart"
    );

    if (!container) {
        return;
    }

    container.replaceChildren();

    if (!Array.isArray(trends) || trends.length === 0) {
        setContainerMessage(
            "ticketTrendChart",
            "No ticket creation history is available yet."
        );
        return;
    }

    const validTrends = trends.filter(item =>
        typeof item.date === "string" &&
        Number.isFinite(Number(item.count)) &&
        Number(item.count) >= 0
    );

    if (validTrends.length === 0) {
        setContainerMessage(
            "ticketTrendChart",
            "No valid ticket trend data is available."
        );
        return;
    }

    const svg = createSvgElement("svg", {
        viewBox: "0 0 640 250",
        class: "analytics-trend-chart",
        role: "img",
        "aria-label": "Daily ticket creation trend"
    });

    const chartWidth = 640;
    const chartHeight = 250;

    const left = 48;
    const right = 18;
    const top = 22;
    const bottom = 48;

    const plotWidth = chartWidth - left - right;
    const plotHeight = chartHeight - top - bottom;

    const maximum = Math.max(
        ...validTrends.map(item => Number(item.count)),
        1
    );

    const yMaximum = Math.max(
        4,
        Math.ceil(maximum * 1.15)
    );

    const points = validTrends.map((item, index) => {
        const x = validTrends.length === 1
            ? left + plotWidth / 2
            : left + (
                index / (validTrends.length - 1)
            ) * plotWidth;

        const y = top + plotHeight - (
            Number(item.count) / yMaximum
        ) * plotHeight;

        return {
            x,
            y,
            date: item.date,
            count: Number(item.count)
        };
    });


    // Horizontal grid lines and Y-axis labels.
    for (let tick = 0; tick <= 4; tick++) {
        const value = Math.round(
            (yMaximum / 4) * tick
        );

        const y = top + plotHeight - (
            value / yMaximum
        ) * plotHeight;

        const gridLine = createSvgElement("line", {
            x1: left,
            y1: y,
            x2: chartWidth - right,
            y2: y,
            stroke: "#e9edf3",
            "stroke-width": "1"
        });

        svg.appendChild(gridLine);

        const label = createSvgElement("text", {
            x: left - 12,
            y: y + 4,
            "text-anchor": "end"
        });

        label.textContent = String(value);
        svg.appendChild(label);
    }


    // Line chart path.
    const linePath = points.map((point, index) =>
        `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`
    ).join(" ");

    // Filled area beneath the line.
    const areaPath = [
        linePath,
        `L ${points[points.length - 1].x} ${top + plotHeight}`,
        `L ${points[0].x} ${top + plotHeight}`,
        "Z"
    ].join(" ");

    const area = createSvgElement("path", {
        d: areaPath,
        class: "analytics-trend-area"
    });

    svg.appendChild(area);

    const line = createSvgElement("path", {
        d: linePath,
        class: "analytics-trend-line"
    });

    svg.appendChild(line);


    // Data points and their labels.
    points.forEach(point => {
        const circle = createSvgElement("circle", {
            cx: point.x,
            cy: point.y,
            r: "5",
            class: "analytics-trend-point"
        });

        const tooltip = createSvgElement("title");
        tooltip.textContent =
            `${point.date}: ${point.count} tickets`;

        circle.appendChild(tooltip);
        svg.appendChild(circle);
    });


    // X-axis date labels.
    const labelInterval = Math.max(
        1,
        Math.ceil(points.length / 6)
    );

    points.forEach((point, index) => {
        const isLast = index === points.length - 1;

        if (
            index % labelInterval !== 0 &&
            !isLast
        ) {
            return;
        }

        const dateLabel = createSvgElement("text", {
            x: point.x,
            y: chartHeight - 15,
            "text-anchor": "middle"
        });

        dateLabel.textContent = formatDateLabel(
            point.date
        );

        svg.appendChild(dateLabel);
    });

    container.appendChild(svg);
}


// =========================================
// LOAD OPERATIONS ANALYTICS
// =========================================

async function loadOperationsAnalytics() {
    try {
        const response = await authenticatedFetch(
            "/api/analytics/operations"
        );

        await checkApiResponse(
            response,
            "Unable to retrieve ticket operations analytics."
        );

        const data = await response.json();

        if (!data.summary) {
            throw new Error(
                "The server returned an invalid analytics response."
            );
        }

        const summary = data.summary;

        // Update operational summary cards.
        setElementText(
            "analyticsTotalTickets",
            formatNumber(summary.total_tickets)
        );

        setElementText(
            "analyticsOpenTickets",
            formatNumber(summary.open_tickets)
        );

        setElementText(
            "analyticsProgressTickets",
            formatNumber(summary.in_progress_tickets)
        );

        setElementText(
            "analyticsResolvedTickets",
            formatNumber(summary.resolved_tickets)
        );


        // Render confirmed category distribution.
        renderBarChart(
            "confirmedCategoryChart",
            data.category_distribution,
            "category"
        );


        // Render AI prediction distribution.
        renderBarChart(
            "aiPredictionChart",
            data.ai_prediction_distribution,
            "category"
        );


        // Render priority distribution.
        renderBarChart(
            "priorityChart",
            data.priority_distribution,
            "priority"
        );


        // Render daily ticket trends.
        renderTicketTrends(data.ticket_trends);

    } catch (error) {
        if (getAccessToken()) {
            showAnalyticsMessage(error.message);

            const chartIds = [
                "confirmedCategoryChart",
                "aiPredictionChart",
                "priorityChart",
                "ticketTrendChart"
            ];

            chartIds.forEach(id => {
                setContainerMessage(
                    id,
                    "Unable to load analytics data."
                );
            });
        }
    }
}


// =========================================
// RENDER CATEGORY-LEVEL MODEL METRICS
// =========================================

function renderCategoryMetrics(metrics) {
    const tableBody = document.getElementById(
        "categoryMetricsTableBody"
    );

    tableBody.replaceChildren();

    if (!Array.isArray(metrics) || metrics.length === 0) {
        const row = document.createElement("tr");
        const cell = document.createElement("td");

        cell.colSpan = 5;
        cell.className = "analytics-empty-state";
        cell.textContent = "No classification results available.";

        row.appendChild(cell);
        tableBody.appendChild(row);

        return;
    }

    metrics.forEach(metric => {
        const row = document.createElement("tr");

        const values = [
            metric.category,
            formatPercentage(metric.precision),
            formatPercentage(metric.recall),
            formatPercentage(metric.f1_score),
            formatNumber(metric.support)
        ];

        values.forEach(value => {
            const cell = document.createElement("td");
            cell.textContent = value;
            row.appendChild(cell);
        });

        tableBody.appendChild(row);
    });
}


// =========================================
// RENDER CONFUSION MATRIX
// =========================================

function renderConfusionMatrix(confusionMatrix) {
    const container = document.getElementById(
        "confusionMatrixContainer"
    );

    container.replaceChildren();

    const labels = confusionMatrix.labels;
    const values = confusionMatrix.values;

    if (
        !Array.isArray(labels) ||
        !Array.isArray(values) ||
        labels.length === 0 ||
        values.length !== labels.length ||
        values.some(row =>
            !Array.isArray(row) ||
            row.length !== labels.length
        )
    ) {
        setContainerMessage(
            "confusionMatrixContainer",
            "The confusion matrix data is invalid."
        );

        return;
    }

    const table = document.createElement("table");
    table.className = "confusion-matrix";

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");

    const corner = document.createElement("th");
    corner.className = "matrix-axis-label";
    corner.scope = "col";
    corner.textContent = "Actual / Predicted";

    headerRow.appendChild(corner);

    labels.forEach(label => {
        const heading = document.createElement("th");

        heading.scope = "col";
        heading.textContent = label;

        headerRow.appendChild(heading);
    });

    thead.appendChild(headerRow);
    table.appendChild(thead);


    // Matrix data rows.
    const tbody = document.createElement("tbody");

    values.forEach((rowValues, rowIndex) => {
        const row = document.createElement("tr");

        const rowHeading = document.createElement("th");

        rowHeading.scope = "row";
        rowHeading.className = "matrix-row-label";
        rowHeading.textContent = labels[rowIndex];

        row.appendChild(rowHeading);

        rowValues.forEach((value, columnIndex) => {
            const cell = document.createElement("td");
            const numericValue = Number(value);

            cell.textContent = formatNumber(numericValue);

            if (rowIndex === columnIndex && numericValue > 0) {
                cell.className = "matrix-correct";
            } else if (numericValue > 0) {
                cell.className = "matrix-error";
            } else {
                cell.className = "matrix-zero";
            }

            cell.title =
                `Actual: ${labels[rowIndex]}, ` +
                `Predicted: ${labels[columnIndex]}, ` +
                `Count: ${numericValue}`;

            row.appendChild(cell);
        });

        tbody.appendChild(row);
    });

    table.appendChild(tbody);
    container.appendChild(table);
}


// =========================================
// LOAD ML MODEL PERFORMANCE
// =========================================

async function loadModelPerformance() {
    try {
        const response = await authenticatedFetch(
            "/api/analytics/model-performance"
        );

        await checkApiResponse(
            response,
            "Unable to retrieve model performance."
        );

        const data = await response.json();

        // Model information.
        setElementText("modelName", data.model);

        setElementText(
            "trainingRecords",
            formatNumber(data.training_records)
        );

        setElementText(
            "testRecords",
            formatNumber(data.test_records)
        );


        // Overall metrics.
        setElementText(
            "modelAccuracy",
            formatPercentage(data.accuracy)
        );

        setElementText(
            "modelMacroF1",
            formatPercentage(data.macro_f1)
        );

        setElementText(
            "modelWeightedF1",
            formatPercentage(data.weighted_f1)
        );


        // Category-level classification report.
        renderCategoryMetrics(data.category_metrics);


        // Actual versus predicted confusion matrix.
        renderConfusionMatrix(data.confusion_matrix);

    } catch (error) {
        if (getAccessToken()) {
            showAnalyticsMessage(error.message);

            setContainerMessage(
                "confusionMatrixContainer",
                "Unable to load confusion matrix."
            );

            const tableBody = document.getElementById(
                "categoryMetricsTableBody"
            );

            tableBody.replaceChildren();

            const row = document.createElement("tr");
            const cell = document.createElement("td");

            cell.colSpan = 5;
            cell.className = "analytics-empty-state";
            cell.textContent =
                "Unable to load classification report.";

            row.appendChild(cell);
            tableBody.appendChild(row);
        }
    }
}


// =========================================
// INITIALISE ANALYTICS PAGE
// =========================================

async function initialiseAnalytics() {
    if (!requireAuthentication()) {
        return;
    }

    // Register logout functionality.
    bindLogoutButton(logoutButton, showAnalyticsMessage);

    // Confirm the role before requesting the staff-only analytics
    // endpoints. The analytics sections stay hidden unless the user
    // is an admin or agent.
    const user = await initialiseUserInterface({
        onError: showAnalyticsMessage
    });

    if (!user) {
        return;
    }

    if (!isStaff(user)) {
        showAnalyticsMessage(
            "Analytics is available to support staff only.",
            "warning"
        );
        return;
    }

    // Load analytics independently.
    await Promise.all([
        loadOperationsAnalytics(),
        loadModelPerformance()
    ]);
}


// =========================================
// START ANALYTICS
// =========================================

initialiseAnalytics();