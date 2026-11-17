
"use strict";

// =========================================
// RESOLVEAI - LOGIN FUNCTIONALITY
// =========================================

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
        const response = await fetch("/api/auth/login", {

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


        // Read the API response.
        let responseData = {};

        try {
            responseData = await response.json();
        } catch {
            throw new Error("Unable to read the server response.");
        }


        // Handle unsuccessful authentication.
        if (!response.ok) {

            if (response.status === 401) {
                throw new Error("Incorrect email or password.");
            }

            throw new Error(
                responseData.detail || "Login failed. Please try again."
            );

        }


        // Verify that the response contains an access token.
        if (!responseData.access_token) {
            throw new Error("The server did not return an access token.");
        }


        // Store the token for this browser tab's session.
        sessionStorage.setItem(
            "resolveai_access_token",
            responseData.access_token
        );


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
