
"use strict";

// =========================================
// RESOLVEAI - LOGIN FUNCTIONALITY
// =========================================

// Shared request, error and token helpers (common.js).
const {
    safeFetch,
    getApiErrorMessage,
    readJsonResponse,
    setAccessToken
} = window.ResolveAI;

// Select login form elements.
const loginForm = document.getElementById("loginForm");

const emailInput = document.getElementById("emailInput");
const passwordInput = document.getElementById("passwordInput");

const loginAlert = document.getElementById("loginAlert");
const loginButton = document.getElementById("loginButton");


// =========================================
// DISPLAY LOGIN MESSAGES
// =========================================

function showLoginMessage(message, type = "danger") {

    loginAlert.textContent = message;

    loginAlert.classList.remove(
        "d-none",
        "alert-danger",
        "alert-success"
    );

    loginAlert.classList.add(`alert-${type}`);
}


// =========================================
// HIDE LOGIN MESSAGES
// =========================================

function hideLoginMessage() {

    loginAlert.textContent = "";

    loginAlert.classList.add("d-none");

    loginAlert.classList.remove(
        "alert-danger",
        "alert-success"
    );

}


// =========================================
// HANDLE LOGIN FORM SUBMISSION
// =========================================

loginForm.addEventListener("submit", async function (event) {

    // Prevent the browser's default form submission.
    event.preventDefault();

    hideLoginMessage();

    // Read the form values.
    const email = emailInput.value.trim();
    const password = passwordInput.value;

    // Show loading state.
    loginButton.disabled = true;
    loginButton.textContent = "Signing in...";

    try {

        // Send login request to FastAPI.
        // safeFetch reports an unreachable server with a readable message.
        const response = await safeFetch("/api/auth/login", {

            method: "POST",

            headers: {
                "Content-Type": "application/x-www-form-urlencoded"
            },

            // FastAPI OAuth2 expects the field name "username".
            body: new URLSearchParams({
                username: email,
                password: password
            })

        });


        // Handle unsuccessful authentication.
        if (!response.ok) {

            if (response.status === 401) {
                throw new Error("Incorrect email or password.");
            }

            throw new Error(
                await getApiErrorMessage(
                    response,
                    "Login failed. Please try again."
                )
            );

        }


        // Read the API response (throws a readable error if invalid).
        const responseData = await readJsonResponse(response);


        // Verify that the response contains an access token.
        if (!responseData || !responseData.access_token) {
            throw new Error("The server did not return an access token.");
        }


        // Store the token for this browser tab's session.
        setAccessToken(responseData.access_token);


        // Clear the password field.
        passwordInput.value = "";

        // Redirect to the authenticated dashboard.
        window.location.assign("/dashboard.html");


    } catch (error) {

        // Display the error to the user.
        showLoginMessage(
            error.message || "Unable to connect to the server."
        );

    } finally {

        // Restore the login button.
        loginButton.disabled = false;
        loginButton.textContent = "Sign In";

    }

});
