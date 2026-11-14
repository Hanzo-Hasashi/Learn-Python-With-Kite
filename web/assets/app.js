(function () {
  "use strict";
  var frame = document.getElementById("f");
  var tokenKey = "kite.accessToken";
  var refreshKey = "kite.refreshToken";
  var pendingPlanKey = "kite.pendingSubscriptionPeriod";
  var subscriptionReturnKey = "kite.subscriptionReturn";
  var googleVerifierKey = "kite.googleCodeVerifier";
  var authDestinationKey = "kite.authDestination";
  var authModeKey = "kite.authMode";
  var guestProgressKey = "kite.guestProgress.v1";
  var guestNotificationsKey = "kite.guestNotifications.v1";
  var pendingPlaygroundCodeKey = "kite.pendingPlaygroundCode";
  var playgroundCodeKey = "kite.playgroundCode";
  var playgroundOutputKey = "kite.playgroundOutput";
  var savedPlaygroundCodeKey = "kite.playgroundSavedCode";
  var playgroundReturnContextKey = "kite.playgroundReturnContext";
  var pendingChallengeCodeKey = "kite.pendingChallengeCode";
  var pendingQuizAnswersKey = "kite.pendingQuizAnswers";
  var pendingQuizIndexKey = "kite.pendingQuizIndex";
  var pendingQuizDraftIdKey = "kite.pendingQuizDraftId";
  var pendingQuizSubmissionIdKey = "kite.pendingQuizSubmissionId";
  var quizEntryModeKey = "kite.quizEntryMode";
  var completedQuizResultKey = "kite.completedQuizResult";
  var guestQuizAttemptsKey = "kite.guestQuizAttempts.v1";
  var quizDraftsKey = "kite.quizDrafts.v1";
  var protectedRoutes = {
    leaderboard: true,
    profile: true
  };
  var knownRoutes = ["about", "challenge", "home", "launch", "leaderboard", "learn", "lesson",
    "notifications", "playground", "preferences", "profile", "progress", "quiz"];
  var refreshInFlight = null;
  var busy = false;
  var oauthCallbackStarted = false;
  var activePlaygroundRun = null;
  var routeUser = null;
  var shellUserRequest = null;

  function token() {
    return sessionStorage.getItem(tokenKey);
  }
  function setMoreMenuAuthState(authenticated) {
    var menu = document.getElementById("pn");
    if (!menu) return;
    var signIn = menu.querySelector(".kite-menu-sign-in");
    var exitGuest = menu.querySelector(".kite-menu-exit-guest");
    var logOut = menu.querySelector("button.out");
    if (signIn) signIn.style.display = authenticated ? "none" : "";
    if (exitGuest) exitGuest.style.display = authenticated ? "none" : "";
    if (logOut) logOut.style.display = authenticated ? "" : "none";
  }
  function ensureMoreMenuActions() {
    var menu = document.getElementById("pn");
    if (!menu) return;
    var icon = menu.querySelector("button:not(.out) svg");
    [
      { className: "kite-menu-sign-in", label: "Sign In" },
      { className: "kite-menu-exit-guest", label: "Exit Guest Mode" }
    ].forEach(function (action) {
      if (menu.querySelector("." + action.className)) return;
      var button = document.createElement("button");
      button.className = action.className;
      button.setAttribute("data-r", "launch");
      var label = document.createElement("span");
      label.textContent = action.label;
      button.appendChild(label);
      if (icon) button.appendChild(icon.cloneNode(true));
      var logOut = menu.querySelector("button.out");
      menu.insertBefore(button, logOut || null);
    });
  }
  function navigate(route, replace) {
    if (route !== "quiz" && route !== "challenge" && route !== "lesson") {
      sessionStorage.removeItem(quizEntryModeKey);
    }
    if (typeof window.kiteNavigate === "function") window.kiteNavigate(route, replace);
    else window.location.hash = "#" + route;
  }
  function clearAuthTokens() {
    sessionStorage.removeItem(tokenKey);
    sessionStorage.removeItem(refreshKey);
    setMoreMenuAuthState(false);
  }
  function signInForRoute(route) {
    if (knownRoutes.indexOf(route) >= 0) sessionStorage.setItem(authDestinationKey, route);
    sessionStorage.setItem(authModeKey, "login");
    navigate("launch", true);
  }
  function responseData(response) {
    return response.json().catch(function () { return {}; }).then(function (data) {
      var detail = data.detail;
      if (Array.isArray(detail)) {
        detail = detail.map(function (item) { return item && item.msg; }).filter(Boolean).join(" ");
      } else if (detail && typeof detail !== "string") {
        detail = detail.message;
      }
      if (!response.ok) {
        var error = new Error(detail || "Request failed.");
        error.status = response.status;
        throw error;
      }
      return data;
    });
  }
  function refreshSession() {
    if (refreshInFlight) return refreshInFlight;
    refreshInFlight = fetch((window.KITE_API_BASE || "") + "/api/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: sessionStorage.getItem(refreshKey) })
    }).then(responseData).then(function (data) {
      if (!data.access_token) throw new Error("Authentication service returned no access token.");
      sessionStorage.setItem(tokenKey, data.access_token);
      if (data.refresh_token) sessionStorage.setItem(refreshKey, data.refresh_token);
      else sessionStorage.removeItem(refreshKey);
    }).catch(function (error) {
      if (error.status === 400 || error.status === 401) clearAuthTokens();
      throw error;
    }).finally(function () { refreshInFlight = null; });
    return refreshInFlight;
  }
  function api(path, body, method) {
    function send(retried) {
      var headers = { "Content-Type": "application/json" };
      if (token()) headers.Authorization = "Bearer " + token();
      return fetch((window.KITE_API_BASE || "") + path, { method: method || (body ? "POST" : "GET"), headers: headers, body: body ? JSON.stringify(body) : undefined })
        .then(function (response) {
          if (response.status === 401 && !retried && sessionStorage.getItem(refreshKey) && path.indexOf("/api/auth/") !== 0) {
            return refreshSession().then(function () { return send(true); });
          }
          if (response.status === 401 && !retried && path.indexOf("/api/auth/") !== 0 && token()) {
            clearAuthTokens();
          }
          return responseData(response);
        });
    }
    return send(false);
  }
  function tell(doc, text, isError) {
    var target = doc.getElementById("kite-verification-status") ||
      doc.getElementById("err") || doc.getElementById("toast") || doc.getElementById("out");
    if (!target) {
      var body = doc.getElementById("sbody");
      if (!body) return;
      target = doc.createElement("p");
      target.className = "err";
      target.setAttribute("role", "alert");
      body.insertBefore(target, body.firstChild);
    }
    target.textContent = text;
    if (target.classList.contains("err")) target.className = "err" + (isError ? " on" : "");
  }
  function friendlyAuthMessage(error, flow) {
    var status = error && error.status;
    var message = String(error && error.message || "").toLowerCase();
    if (status === 429) return "Too many attempts. Please wait a little while and try again.";
    if (flow === "google") {
      if (message.indexOf("requires configured") >= 0 || message.indexOf("not configured") >= 0) {
        return "Google sign-in is not configured for this Kite deployment yet. You can use email sign-in instead.";
      }
      if (message.indexOf("cancel") >= 0 || message.indexOf("access_denied") >= 0) {
        return "Google sign-in was cancelled. You can try again whenever you're ready.";
      }
      return "Google sign-in couldn't be completed. Check your connection and try again.";
    }
    if (message.indexOf("confirm your email") >= 0) {
      return "Check your inbox and confirm your email address before signing in.";
    }
    if (flow === "in" && status === 401) return "Email or password is incorrect. Check your details and try again.";
    if (flow === "up" && (status === 400 || status === 409)) {
      return "We couldn't complete registration. If you already have an account, try signing in.";
    }
    if (flow === "up" && status === 422) {
      return "Choose a stronger password and review the registration details.";
    }
    if (flow === "rs" && status === 503) {
      return "Password recovery is temporarily unavailable. Please try again later.";
    }
    if ((flow === "verify" || flow === "reset") &&
        (message.indexOf("expired") >= 0 || message.indexOf("invalid") >= 0)) {
      return "This link is invalid or expired. Request a new email and try again.";
    }
    if (message.indexOf("too many attempts") >= 0) return "Too many attempts. Please wait and try again.";
    if (status === 503) return "Authentication is temporarily unavailable. Please try again later.";
    if (message.indexOf("choose a stronger password") >= 0 ||
        message.indexOf("mix of letters") >= 0 ||
        message.indexOf("at least 8 characters") >= 0 ||
        message.indexOf("passwords do not match") >= 0 ||
        message.indexOf("enter your") === 0 ||
        message.indexOf("enter a valid") === 0 ||
        message.indexOf("check your email") === 0) {
      return error.message;
    }
    return "We couldn't complete that request. Check your connection and try again.";
  }
  function passwordStrength(password) {
    var lower = password.toLowerCase();
    var common = ["password", "password1", "password123", "12345678", "123456789",
      "qwerty123", "letmein123", "admin1234"];
    var categories = [
      /[a-z]/.test(password), /[A-Z]/.test(password),
      /[0-9]/.test(password), /[^A-Za-z0-9]/.test(password)
    ].filter(Boolean).length;
    var weak = password.length < 8 || common.indexOf(lower) >= 0 || categories < 2;
    return { weak: weak, label: !password ? "Use at least 8 characters and a mix of character types."
      : weak ? "Weak — use at least 8 characters and mix letters, numbers, or symbols."
        : password.length >= 12 && categories >= 3 ? "Strong password" : "Fair password" };
  }
  function addPasswordStrengthFeedback(doc, password) {
    if (!password || password.dataset.kiteStrength === "ready") return;
    password.dataset.kiteStrength = "ready";
    var feedback = doc.createElement("small");
    feedback.className = "kite-password-strength";
    feedback.setAttribute("role", "status");
    feedback.setAttribute("aria-live", "polite");
    feedback.id = "kite-password-strength-" + (password.id || "reset");
    feedback.style.cssText = "display:block;margin:4px 0 8px;font-size:12px;color:var(--mut,#667085)";
    password.setAttribute("aria-describedby", feedback.id);
    if (password.parentElement) password.parentElement.insertAdjacentElement("afterend", feedback);
    function update() {
      var strength = passwordStrength(password.value);
      feedback.textContent = strength.label;
      feedback.style.color = strength.weak ? "#b42318" :
        strength.label === "Strong password" ? "#067647" : "var(--mut,#667085)";
    }
    password.addEventListener("input", update);
    update();
  }
  function readGuestProgress(doc) {
    var empty = { version: 1, started_lessons: [], completed_lessons: [], playground_runs: 0, xp: 0 };
    var raw;
    try { raw = window.localStorage.getItem(guestProgressKey); }
    catch (error) {
      console.error("Could not read guest progress from this device.", error);
      if (doc) tell(doc, "Guest progress could not be read from this device.", true);
      return empty;
    }
    if (raw === null) return empty;
    try {
      var progress = JSON.parse(raw);
      if (!progress || progress.version !== 1) throw new Error("Unsupported guest progress format.");
      function cleanIds(ids) {
        if (!Array.isArray(ids)) return [];
        return Array.from(new Set(ids.filter(function (id) {
          return typeof id === "string" && id.length > 0 && id.length <= 60;
        }).slice(0, 100)));
      }
      var completed = cleanIds(progress.completed_lessons);
      return {
        version: 1,
        started_lessons: cleanIds(progress.started_lessons).filter(function (id) {
          return completed.indexOf(id) < 0;
        }),
        completed_lessons: completed,
        playground_runs: Number.isSafeInteger(progress.playground_runs) && progress.playground_runs >= 0
          ? Math.min(progress.playground_runs, 10000) : 0,
        xp: Number.isSafeInteger(progress.xp) && progress.xp >= 0
          ? Math.min(progress.xp, 1000000000) : 0,
        last_lesson: typeof progress.last_lesson === "string" && progress.last_lesson.length <= 60
          ? progress.last_lesson : null
      };
    } catch (error) {
      console.error("Guest progress on this device is invalid.", error);
      if (doc) tell(doc, "Guest progress on this device could not be read.", true);
      return empty;
    }
  }
  function writeGuestProgress(doc, progress) {
    progress.version = 1;
    progress.updated_at = new Date().toISOString();
    try {
      window.localStorage.setItem(guestProgressKey, JSON.stringify(progress));
      return true;
    } catch (error) {
      console.error("Could not save guest progress on this device.", error);
      if (doc) tell(doc, "Guest progress could not be saved on this device.", true);
      return false;
    }
  }
  function readGuestNotifications() {
    try {
      var raw = window.localStorage.getItem(guestNotificationsKey);
      if (!raw) return [];
      var items = JSON.parse(raw);
      if (!Array.isArray(items)) throw new Error("Unsupported guest notification format.");
      return items.filter(function (item) {
        return item && typeof item.id === "string" && typeof item.title === "string" &&
          typeof item.body === "string" && typeof item.created_at === "string";
      }).slice(0, 100);
    } catch (error) {
      console.error("Could not read guest notifications from this device.", error);
      return [];
    }
  }
  function writeGuestNotifications(items) {
    try {
      window.localStorage.setItem(guestNotificationsKey, JSON.stringify(items.slice(0, 100)));
      return true;
    } catch (error) {
      console.error("Could not save guest notifications on this device.", error);
      return false;
    }
  }
  function recordGuestNotification(id, title, body, targetRoute) {
    var items = readGuestNotifications();
    if (items.some(function (item) { return item.id === id; })) return;
    items.unshift({
      id: id,
      title: title,
      body: body,
      category: "learning",
      read: false,
      target_route: targetRoute,
      created_at: new Date().toISOString()
    });
    writeGuestNotifications(items);
  }
  function recordGuestLesson(doc, lessonId, completed, xpReward) {
    if (token()) return;
    var progress = readGuestProgress(doc);
    if (progress.completed_lessons.indexOf(lessonId) >= 0) return;
    if (completed) {
      progress.started_lessons = progress.started_lessons.filter(function (id) { return id !== lessonId; });
      progress.completed_lessons.push(lessonId);
      if (Number.isSafeInteger(xpReward) && xpReward > 0) {
        progress.xp = Math.min(progress.xp + xpReward, 1000000000);
      }
    } else if (progress.started_lessons.indexOf(lessonId) < 0) {
      progress.started_lessons.push(lessonId);
    }
    progress.last_lesson = lessonId;
    if (writeGuestProgress(doc, progress) && completed) {
      recordGuestNotification(
        "lesson-completed:" + lessonId,
        "Free lesson completed",
        "Your lesson progress was saved on this device and is not synced to an account.",
        "learn"
      );
    }
  }
  function recordGuestPlaygroundRun(doc) {
    if (token()) return;
    var progress = readGuestProgress(doc);
    progress.playground_runs = Math.min(progress.playground_runs + 1, 10000);
    writeGuestProgress(doc, progress);
  }
  function addGuestProgressPrompt(doc, container, isHome) {
    if (!token() || !container || container.querySelector(".kite-guest-progress")) return;
    var progress = readGuestProgress(doc);
    var startedCount = progress.started_lessons.length;
    var completedCount = progress.completed_lessons.length;
    if (!startedCount && !completedCount) return;

    var section = doc.createElement(isHome ? "section" : "div");
    section.className = "kite-guest-progress" + (isHome ? " lc" : "");
    var content = doc.createElement("div");
    if (isHome) content.style.cssText = "flex:1;min-width:0";
    var heading = doc.createElement(isHome ? "h3" : "p");
    heading.textContent = "Guest progress on this device";
    var detail = doc.createElement("p");
    detail.textContent = completedCount + " completed and " + startedCount +
      " in-progress free lesson" + (completedCount + startedCount === 1 ? "" : "s") +
      " · not yet synced to your account";
    content.append(heading, detail);
    var button = doc.createElement("button");
    button.type = "button";
    button.className = isHome ? "go" : "b g";
    button.dataset.kiteImportGuest = "true";
    button.textContent = "Transfer progress to my account";
    if (isHome) {
      section.append(content, button);
      var quickAccess = container.querySelector(".qa");
      var quickAccessHeading = quickAccess && quickAccess.previousElementSibling;
      if (quickAccessHeading) container.insertBefore(section, quickAccessHeading);
      else container.appendChild(section);
    } else {
      detail.className = "sub";
      button.setAttribute("aria-label", "Transfer guest lesson progress to your Kite account");
      section.append(heading, detail, button);
      container.appendChild(section);
    }
  }
  function importGuestProgress(doc, button, route) {
    if (!token()) {
      tell(doc, "Sign in before transferring guest progress.", true);
      return;
    }
    var progress = readGuestProgress(doc);
    button.disabled = true;
    button.textContent = "Transferring progress…";
    api("/api/progress/import", {
      started_lessons: progress.started_lessons,
      completed_lessons: progress.completed_lessons
    }).then(function (result) {
      try { window.localStorage.removeItem(guestProgressKey); }
      catch (error) {
        console.error("Guest progress transferred but could not be cleared from this device.", error);
      }
      var card = button.closest(".kite-guest-progress");
      if (card) card.remove();
      var transferred = result.imported_completed + result.imported_started;
      tell(doc, transferred
        ? "Guest progress transferred to your Kite account and will now sync normally."
        : "Your account already has this guest progress; it is up to date.", false);
      if (route === "home") {
        api("/api/me").then(function (me) { hydrateHome(doc, me); })
          .catch(function (error) { tell(doc, error.message, true); });
      }
    }).catch(function (error) {
      button.disabled = false;
      button.textContent = "Transfer progress to my account";
      tell(doc, error.message, true);
    });
  }
  function showSignedIn(doc, name) {
    var body = doc.getElementById("sbody");
    if (!body) return;
    setMoreMenuAuthState(true);
    body.innerHTML = "<h2>You’re in, " + escapeHtml(name.split(" ")[0]) + "!</h2><p class=\"sub\">Your learning journey is saved to your Kite account.</p><button class=\"b p\" type=\"button\" data-kite-home>Continue learning</button><button class=\"b g\" type=\"button\" data-a=\"out\">Log out</button>";
    body.querySelector("[data-kite-home]").addEventListener("click", function () { navigate("home"); });
    addGuestProgressPrompt(doc, body, false);
  }
  function completeAuthentication(doc, data, destination) {
    sessionStorage.setItem(tokenKey, data.access_token);
    if (data.refresh_token) sessionStorage.setItem(refreshKey, data.refresh_token);
    else sessionStorage.removeItem(refreshKey);
    setMoreMenuAuthState(true);
    var requestedDestination = sessionStorage.getItem(authDestinationKey);
    var nextDestination = requestedDestination || destination;
    sessionStorage.removeItem(authDestinationKey);
    sessionStorage.removeItem(authModeKey);
    sessionStorage.removeItem("kite.pendingVerificationEmail");
    if (sessionStorage.getItem(subscriptionReturnKey) === "preferences") {
      sessionStorage.removeItem(subscriptionReturnKey);
      navigate("preferences", true);
      return;
    }
    if (nextDestination && nextDestination !== "launch" && knownRoutes.indexOf(nextDestination) >= 0) {
      navigate(nextDestination, Boolean(requestedDestination));
      return;
    }
    showSignedIn(doc, (data.user && data.user.name) || "Kite learner");
  }
  function logOut(doc) {
    function finish(error) {
      [tokenKey, refreshKey, pendingPlanKey, subscriptionReturnKey, authDestinationKey, authModeKey,
        pendingPlaygroundCodeKey, pendingChallengeCodeKey, pendingQuizAnswersKey, googleVerifierKey]
        .forEach(function (key) { sessionStorage.removeItem(key); });
      setMoreMenuAuthState(false);
      hydrateShellUser(null);
      if (doc && doc.getElementById("sbody")) {
        doc.defaultView.location.hash = "#welcome";
        if (error) setTimeout(function () { tell(doc, "Signed out on this device, but server session revocation failed.", true); }, 0);
      } else navigate("launch");
    }
    if (!token()) {
      finish();
      return;
    }
    api("/api/auth/logout", {}, "POST").then(function () {
      finish();
    }).catch(function (error) {
      console.error("Kite sign-out could not be revoked by the server.", error);
      finish(error);
    });
  }
  function cleanAuthCallbackUrl() {
    window.history.replaceState(null, "", window.location.pathname);
  }
  function handleAuthCallback(doc) {
    var hashParams = new URLSearchParams(window.location.hash.slice(1));
    var queryParams = new URLSearchParams(window.location.search);
    var type = hashParams.get("type") || queryParams.get("type");
    var oauthError = queryParams.get("error_description") || queryParams.get("error") ||
      hashParams.get("error_description") || hashParams.get("error");
    if (oauthError) {
      sessionStorage.removeItem(googleVerifierKey);
      cleanAuthCallbackUrl();
      if (type === "recovery") {
        doc.defaultView.location.hash = "#reset";
        setTimeout(function () {
          tell(doc, "This password-reset link is invalid or expired. Request a new one and try again.", true);
        }, 0);
      } else {
        var callbackFlow = type === "signup" || type === "email_change" ? "verify" : "google";
        tell(doc, friendlyAuthMessage(new Error(oauthError), callbackFlow), true);
      }
      return true;
    }
    if (type === "recovery") return false;
    var accessToken = hashParams.get("access_token");
    if (accessToken) {
      cleanAuthCallbackUrl();
      if (["signup", "email_change", "magiclink", "invite"].indexOf(type) < 0) {
        tell(doc, "This sign-in link could not be verified. Request a new link and try again.", true);
        return true;
      }
      api("/api/auth/session", {
        access_token: accessToken,
        refresh_token: hashParams.get("refresh_token")
      }).then(function (data) {
        completeAuthentication(doc, data, type === "signup" ? "home" : undefined);
      }).catch(function (error) {
        tell(doc, friendlyAuthMessage(error, "verify"), true);
      });
      return true;
    }
    var code = queryParams.get("code");
    var verifier = sessionStorage.getItem(googleVerifierKey);
    if (!code) return false;
    if (!verifier) {
      cleanAuthCallbackUrl();
      tell(doc, friendlyAuthMessage(new Error("Missing secure sign-in session."), "google"), true);
      return true;
    }
    if (oauthCallbackStarted) return true;
    oauthCallbackStarted = true;
    api("/api/auth/google/exchange", { code: code, code_verifier: verifier }).then(function (data) {
      sessionStorage.removeItem(googleVerifierKey);
      cleanAuthCallbackUrl();
      completeAuthentication(doc, data);
    }).catch(function (error) {
      sessionStorage.removeItem(googleVerifierKey);
      cleanAuthCallbackUrl();
      tell(doc, friendlyAuthMessage(error, "google"), true);
    });
    return true;
  }
  function requestSubscriptionSignIn(period) {
    if (period) sessionStorage.setItem(pendingPlanKey, period);
    sessionStorage.setItem(subscriptionReturnKey, "preferences");
    sessionStorage.setItem(authModeKey, "login");
    navigate("launch");
  }
  function base64UrlEncode(value) {
    var bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
    var binary = "";
    bytes.forEach(function (byte) { binary += String.fromCharCode(byte); });
    return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function startGoogleSignIn(doc) {
    if (!window.crypto || !window.crypto.subtle) {
      return Promise.reject(new Error("Secure Google sign-in is unavailable in this browser."));
    }
    if (doc.defaultView.location.hash === "#sign-up" && !sessionStorage.getItem(authDestinationKey)) {
      sessionStorage.setItem(authDestinationKey, "home");
    }
    var random = new Uint8Array(32);
    window.crypto.getRandomValues(random);
    var verifier = base64UrlEncode(random);
    sessionStorage.setItem(googleVerifierKey, verifier);
    return window.crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)).then(function (digest) {
      var challenge = base64UrlEncode(digest);
      return api("/api/auth/google/authorize?code_challenge=" + encodeURIComponent(challenge));
    }).then(function (data) {
      window.location.assign(data.authorization_url);
    }).catch(function (error) {
      sessionStorage.removeItem(googleVerifierKey);
      throw error;
    });
  }
  function enhanceRegistrationForm(doc) {
    var form = doc.querySelector('form[data-f="up"]');
    if (!form) return;
    var name = form.elements.namedItem("nm");
    var email = form.elements.namedItem("em");
    var password = form.elements.namedItem("pw");
    if (name) name.required = true;
    if (email) email.required = true;
    if (email && !email.value) {
      var pendingEmail = sessionStorage.getItem("kite.pendingVerificationEmail");
      if (pendingEmail) {
        email.value = pendingEmail;
        sessionStorage.removeItem("kite.pendingVerificationEmail");
      }
    }
    if (password) {
      password.required = true;
      password.minLength = 8;
      password.maxLength = 128;
      addPasswordStrengthFeedback(doc, password);
    }
    if (form.elements.namedItem("cpw")) return;
    var error = form.querySelector("#err");
    if (!error) return;
    var label = doc.createElement("label");
    label.htmlFor = "cpw";
    label.textContent = "Confirm password";
    var wrapper = doc.createElement("div");
    wrapper.className = "pw";
    var confirm = doc.createElement("input");
    confirm.className = "fi";
    confirm.id = "cpw";
    confirm.name = "cpw";
    confirm.type = "password";
    confirm.autocomplete = "new-password";
    confirm.placeholder = "Confirm your password";
    confirm.required = true;
    confirm.minLength = 8;
    confirm.maxLength = 128;
    var toggle = doc.createElement("button");
    toggle.type = "button";
    toggle.className = "tgl";
    toggle.dataset.a = "show";
    toggle.setAttribute("aria-label", "Show password");
    toggle.textContent = "Show";
    wrapper.append(confirm, toggle);
    form.insertBefore(label, error);
    form.insertBefore(wrapper, error);
  }
  function watchRegistrationForm(doc) {
    var body = doc.getElementById("sbody");
    if (!body || body.dataset.kiteRegistrationObserver) return;
    body.dataset.kiteRegistrationObserver = "ready";
    var observer = new doc.defaultView.MutationObserver(function () { enhanceRegistrationForm(doc); });
    observer.observe(body, { childList: true, subtree: true });
    enhanceRegistrationForm(doc);
  }
  function showVerificationPending(doc, email) {
    var body = doc.getElementById("sbody");
    if (!body) return;
    var previousContent = Array.prototype.slice.call(body.children).map(function (node) {
      return { node: node, hidden: node.hidden };
    });
    previousContent.forEach(function (item) { item.node.hidden = true; });
    var registration = body.querySelector('form[data-f="up"]');
    if (registration) {
      if (registration.elements.pw) registration.elements.pw.value = "";
      if (registration.elements.cpw) registration.elements.cpw.value = "";
    }
    var panel = doc.createElement("section");
    panel.setAttribute("aria-labelledby", "kite-verification-title");
    panel.innerHTML = "<h2 id=\"kite-verification-title\">Check your email</h2><p class=\"sub\">If this address can be registered, a verification link will arrive shortly. Follow it to finish creating your account.</p><p id=\"kite-verification-address\"></p><p id=\"kite-verification-status\" class=\"err\" role=\"status\" aria-live=\"polite\"></p><button class=\"b p\" type=\"button\" data-kite-resend-verification>Resend verification email</button><button class=\"b g\" type=\"button\" data-kite-change-registration>Change email</button><button class=\"b g\" type=\"button\" data-kite-back-sign-in>Back to Sign In</button>";
    panel.querySelector("#kite-verification-address").textContent = email;
    body.appendChild(panel);
    doc.documentElement.kiteVerificationView = {
      panel: panel,
      content: previousContent,
      email: email
    };
  }
  function restoreRegistrationView(doc) {
    var state = doc.documentElement.kiteVerificationView;
    if (!state) return;
    state.panel.remove();
    state.content.forEach(function (item) { item.node.hidden = item.hidden; });
    var email = doc.querySelector('form[data-f="up"] input[name="em"]');
    if (email) {
      email.value = state.email;
      email.focus();
    }
    sessionStorage.removeItem("kite.pendingVerificationEmail");
    doc.documentElement.kiteVerificationView = null;
  }
  function resetToken() {
    var query = new URLSearchParams(window.location.search);
    if (query.get("token")) return query.get("token");
    var hash = window.location.hash.slice(1);
    var queryIndex = hash.indexOf("?");
    if (queryIndex >= 0) {
      var fragmentQuery = new URLSearchParams(hash.slice(queryIndex + 1));
      if (fragmentQuery.get("token")) return fragmentQuery.get("token");
    }
    var fragment = new URLSearchParams(hash.slice(queryIndex + 1));
    if (fragment.get("type") === "recovery") return fragment.get("access_token") || fragment.get("token");
    return null;
  }
  function showPasswordReset(doc, reset) {
    var body = doc.getElementById("sbody");
    if (!body) return;
    body.innerHTML = "<h2>Choose a new password</h2><p class=\"sub\">Use at least 8 characters and a mix of character types.</p><form data-kite-reset><label for=\"kite-reset-password\">New password</label><div class=\"pw\"><input class=\"fi\" id=\"kite-reset-password\" type=\"password\" name=\"password\" minlength=\"8\" maxlength=\"128\" autocomplete=\"new-password\" placeholder=\"New password\" required><button type=\"button\" class=\"tgl\" data-a=\"show\" aria-label=\"Show password\">Show</button></div><label for=\"kite-reset-confirm\">Confirm password</label><div class=\"pw\"><input class=\"fi\" id=\"kite-reset-confirm\" type=\"password\" name=\"confirmation\" minlength=\"8\" maxlength=\"128\" autocomplete=\"new-password\" placeholder=\"Confirm your password\" required><button type=\"button\" class=\"tgl\" data-a=\"show\" aria-label=\"Show password\">Show</button></div><div class=\"err\" id=\"err\" role=\"alert\"></div><button class=\"b p\" type=\"submit\">Update password</button></form>";
    var form = body.querySelector("form[data-kite-reset]");
    var password = form.elements.password;
    var confirmation = form.elements.confirmation;
    addPasswordStrengthFeedback(doc, password);
    password.focus();
    body.addEventListener("submit", function (event) {
      if (!event.target.matches("form[data-kite-reset]")) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (busy) return;
      var strength = passwordStrength(password.value);
      if (password.value.length < 8 || password.value.length > 128 || strength.weak) {
        tell(doc, "Choose a stronger password with at least 8 characters.", true);
        return;
      }
      if (password.value !== confirmation.value) {
        tell(doc, "Passwords do not match.", true);
        confirmation.focus();
        return;
      }
      var submit = form.querySelector('button[type="submit"]');
      var label = submit.textContent;
      busy = true;
      submit.disabled = true;
      submit.textContent = "Updating password…";
      form.setAttribute("aria-busy", "true");
      api("/api/auth/password-reset/complete", {
        token: reset,
        password: password.value
      }).then(function () {
        body.innerHTML = "<h2>Password updated</h2><p class=\"sub\">You can now log in with your new password.</p><button class=\"b p\" type=\"button\" data-kite-login>Log in</button>";
        body.querySelector("[data-kite-login]").addEventListener("click", function () {
          window.history.replaceState(null, "", window.location.pathname);
          sessionStorage.setItem(authModeKey, "login");
          navigate("launch");
        });
      }).catch(function (error) { tell(doc, friendlyAuthMessage(error, "reset"), true); })
        .finally(function () {
          busy = false;
          if (submit.isConnected) {
            submit.disabled = false;
            submit.textContent = label;
            form.removeAttribute("aria-busy");
          }
        });
    }, true);
  }
  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[ch];
    });
  }
  function createPythonLogo(doc, size) {
    var image = doc.createElement("img");
    image.className = "kite-python-logo";
    image.src = "/assets/python_logo.webp";
    image.alt = "";
    image.width = size;
    image.height = size;
    image.decoding = "async";
    image.style.cssText = "display:block;flex:none;width:" + size + "px;height:" + size +
      "px;object-fit:contain;background:#fff;border-radius:" + Math.min(Math.round(size * 0.18), 10) + "px";
    return image;
  }
  function applyPythonBranding(doc, route) {
    if (route === "launch") {
      var launchLayout = doc.createElement("style");
      launchLayout.textContent = "@media(max-height:900px){.stage{height:75vh}.sheet{max-height:60vh}}";
      doc.head.appendChild(launchLayout);
    }

    function replaceMark(selector) {
      var mark = doc.querySelector(selector);
      if (!mark) return;
      var size = parseInt(mark.getAttribute("width"), 10) || 28;
      mark.parentNode.replaceChild(createPythonLogo(doc, size), mark);
    }
    function addToHeading(selector) {
      var heading = doc.querySelector(selector);
      if (!heading || heading.parentNode.querySelector(".kite-python-logo")) return;
      heading.parentNode.insertBefore(createPythonLogo(doc, 26), heading);
    }

    if (route === "home") replaceMark(".lc > svg");
    if (route === "learn") replaceMark(".hero > svg");
    if (route === "playground") {
      replaceMark("header.top > svg");
    }
    if (route === "challenge") {
      var run = doc.getElementById("run");
      if (run && !run.querySelector(".kite-python-logo")) run.insertBefore(createPythonLogo(doc, 16), run.firstChild);
    }
    if (route === "lesson" || route === "quiz" || route === "challenge") {
      addToHeading("header h1");
    }
  }
  function createPlaygroundExecutionId(win) {
    if (!win.crypto || typeof win.crypto.getRandomValues !== "function") {
      throw new Error("Secure random IDs are unavailable in this browser.");
    }
    var bytes = new Uint8Array(16);
    win.crypto.getRandomValues(bytes);
    return Array.prototype.map.call(bytes, function (value) {
      return value.toString(16).padStart(2, "0");
    }).join("");
  }
  function enhancePlayground(doc) {
    var editor = doc.getElementById("code");
    var output = doc.getElementById("out");
    var header = doc.querySelector("header.top");
    var gutter = doc.getElementById("gut");
    var highlight = doc.getElementById("hl");
    var outputSection = output && output.closest(".op");
    if (!editor || !output || !header || !outputSection || editor.dataset.kiteEnhanced) return;
    editor.dataset.kiteEnhanced = "true";

    var initialCode = editor.defaultValue || editor.value;
    var pendingCode = sessionStorage.getItem(pendingPlaygroundCodeKey);
    var sessionCode = sessionStorage.getItem(playgroundCodeKey);
    var savedCode = null;
    try { savedCode = window.localStorage.getItem(savedPlaygroundCodeKey); }
    catch (error) { console.error("Could not read the saved Playground code.", error); }
    editor.value = pendingCode !== null ? pendingCode :
      sessionCode !== null ? sessionCode : savedCode !== null ? savedCode : initialCode;
    if (pendingCode !== null) sessionStorage.removeItem(pendingPlaygroundCodeKey);

    var style = doc.createElement("style");
    style.textContent = ".kite-playground-exit{min-width:42px;padding:0 8px}.kite-playground-controls{display:flex;align-items:center;gap:5px;flex:0 0 auto}.kite-playground-actions{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}.kite-playground-actions b{flex:1}.kite-playground-actions .kite-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:6px}.kite-playground-actions .btn{height:30px;padding:0 8px;font-size:12px}.kite-playground-actions .btn:disabled{opacity:.55;cursor:default}@media(max-width:420px){header.top{display:grid;grid-template-columns:auto auto minmax(0,1fr);grid-template-areas:'exit logo title' 'controls controls controls';column-gap:5px;row-gap:8px}header.top>.kite-playground-exit{grid-area:exit}header.top>img.kite-python-logo{grid-area:logo}header.top>h1{grid-area:title;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}header.top>.kite-playground-controls{grid-area:controls;width:100%;justify-content:space-between}.kite-playground-actions .kite-actions{gap:4px}.kite-playground-actions .btn{padding:0 6px;font-size:11px}}";
    doc.head.appendChild(style);

    var exit = doc.createElement("button");
    exit.type = "button";
    exit.className = "btn kite-playground-exit";
    exit.id = "kite-exit-playground";
    exit.setAttribute("aria-label", "Exit Playground and return to the previous screen");
    exit.textContent = "Exit";
    header.insertBefore(exit, header.firstChild);

    var run = doc.getElementById("run");
    var resetButton = doc.getElementById("reset");
    var fullscreenButton = doc.getElementById("fs");
    var controls = doc.createElement("div");
    controls.className = "kite-playground-controls";
    var stop = doc.createElement("button");
    stop.type = "button";
    stop.className = "btn";
    stop.id = "kite-stop";
    stop.setAttribute("aria-label", "Stop Python execution");
    stop.textContent = "Stop";
    stop.hidden = true;
    stop.style.display = "none";
    if (run) controls.appendChild(run);
    controls.appendChild(stop);
    if (resetButton) controls.appendChild(resetButton);
    if (fullscreenButton) controls.appendChild(fullscreenButton);
    header.appendChild(controls);

    var label = outputSection.querySelector("b");
    var actions = doc.createElement("div");
    actions.className = "kite-playground-actions";
    var actionLabel = doc.createElement("b");
    actionLabel.textContent = label ? label.textContent : "Output:";
    var actionButtons = doc.createElement("div");
    actionButtons.className = "kite-actions";
    function addAction(id, text, ariaLabel) {
      var button = doc.createElement("button");
      button.type = "button";
      button.className = "btn";
      button.id = id;
      button.textContent = text;
      button.setAttribute("aria-label", ariaLabel);
      actionButtons.appendChild(button);
      return button;
    }
    var copy = addAction("kite-copy-code", "Copy code", "Copy code to clipboard");
    var save = addAction("kite-save-code", "Save code", "Save code on this device");
    var clear = addAction("kite-clear-console", "Clear console", "Clear console output");
    actions.append(actionLabel, actionButtons);
    if (label) label.remove();
    outputSection.insertBefore(actions, output);

    function persistCode() {
      try { sessionStorage.setItem(playgroundCodeKey, editor.value); }
      catch (error) { console.error("Could not preserve the current Playground code.", error); }
    }
    function persistOutput() {
      try { sessionStorage.setItem(playgroundOutputKey, output.textContent); }
      catch (error) { console.error("Could not preserve Playground output.", error); }
    }
    function syncEditor() {
      if (gutter) gutter.textContent = editor.value.split("\n").map(function (_, index) {
        return String(index + 1);
      }).join("\n");
      persistCode();
    }
    function dispatchEditorInput() {
      editor.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true }));
    }
    function setOutput(text) {
      output.textContent = text;
      persistOutput();
    }
    function saveCode() {
      try {
        window.localStorage.setItem(savedPlaygroundCodeKey, editor.value);
        persistCode();
        tell(doc, "Code saved on this device.", false);
      } catch (error) {
        console.error("Could not save Playground code on this device.", error);
        tell(doc, "Could not save code on this device.", true);
      }
    }
    function copyCode() {
      var clipboard = doc.defaultView.navigator.clipboard;
      if (clipboard && typeof clipboard.writeText === "function") {
        clipboard.writeText(editor.value).then(function () {
          tell(doc, "Code copied to clipboard.", false);
        }).catch(function (error) {
          console.error("Could not copy Playground code.", error);
          tell(doc, "Clipboard access was denied. Select and copy the code manually.", true);
        });
        return;
      }
      editor.focus();
      editor.select();
      var copied = doc.execCommand && doc.execCommand("copy");
      editor.setSelectionRange(editor.value.length, editor.value.length);
      tell(doc, copied ? "Code copied to clipboard." : "Select and copy the code manually.", !copied);
    }
    function indentSelection(outdent) {
      var value = editor.value;
      var start = editor.selectionStart;
      var end = editor.selectionEnd;
      if (start === end && !outdent) {
        editor.setRangeText("    ", start, end, "end");
        dispatchEditorInput();
        return;
      }
      var lineStart = value.lastIndexOf("\n", Math.max(0, start - 1)) + 1;
      var endLine = value.indexOf("\n", end);
      if (endLine < 0) endLine = value.length;
      var selected = value.slice(lineStart, endLine);
      var lines = selected.split("\n");
      var removedBeforeStart = 0;
      var changeBeforeEnd = 0;
      var updated = lines.map(function (line, index) {
        if (!outdent) {
          if (index === 0) changeBeforeEnd += 4;
          return "    " + line;
        }
        var match = line.match(/^( {1,4}|\t)/);
        if (!match) return line;
        if (index === 0) removedBeforeStart = match[0].length;
        changeBeforeEnd -= match[0].length;
        return line.slice(match[0].length);
      }).join("\n");
      editor.setRangeText(updated, lineStart, endLine, "preserve");
      var nextStart = Math.max(lineStart, start + (outdent ? -removedBeforeStart : 4));
      var nextEnd = Math.max(nextStart, end + changeBeforeEnd);
      editor.setSelectionRange(nextStart, nextEnd);
      dispatchEditorInput();
    }
    editor.addEventListener("input", syncEditor);
    editor.addEventListener("scroll", function () {
      if (gutter) gutter.scrollTop = editor.scrollTop;
      if (highlight) {
        highlight.scrollTop = editor.scrollTop;
        highlight.scrollLeft = editor.scrollLeft;
      }
    });
    doc.addEventListener("keydown", function (event) {
      if (event.target !== editor) return;
      var modifier = event.ctrlKey || event.metaKey;
      if (modifier && event.key === "Enter") {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (run && !run.disabled) run.click();
        return;
      }
      if (modifier && event.key.toLowerCase() === "s") {
        event.preventDefault();
        event.stopImmediatePropagation();
        saveCode();
        return;
      }
      if (event.key === "Tab") {
        event.preventDefault();
        event.stopImmediatePropagation();
        indentSelection(event.shiftKey);
        return;
      }
      if (event.key === "Enter") {
        var cursor = editor.selectionStart;
        var selectionEnd = editor.selectionEnd;
        var lineStart = editor.value.lastIndexOf("\n", cursor - 1) + 1;
        var currentLine = editor.value.slice(lineStart, cursor);
        var indentation = (currentLine.match(/^[ \t]*/) || [""])[0];
        var increase = /:\s*(#.*)?$/.test(currentLine.trimEnd());
        editor.setRangeText("\n" + indentation + (increase ? "    " : ""), cursor, selectionEnd, "end");
        event.preventDefault();
        event.stopImmediatePropagation();
        dispatchEditorInput();
      }
    }, true);
    copy.addEventListener("click", copyCode);
    save.addEventListener("click", saveCode);
    clear.addEventListener("click", function () {
      setOutput("");
      tell(doc, "Console cleared.", false);
    });
    exit.addEventListener("click", function () {
      if (typeof window.kiteGoBack === "function") window.kiteGoBack();
      else navigate("learn");
    });
    stop.addEventListener("click", function () {
      var active = activePlaygroundRun;
      if (!active || active.doc !== doc) return;
      stop.disabled = true;
      stop.textContent = "Stopping…";
      api("/api/execution/cancel", { execution_id: active.id }).then(function (result) {
        if (!result.cancelled) {
          stop.disabled = false;
          stop.textContent = "Stop";
          tell(doc, "Execution has already finished.", false);
          return;
        }
        active.cancelled = true;
        setOutput("Execution stopped.");
      }).catch(function (error) {
        stop.disabled = false;
        stop.textContent = "Stop";
        tell(doc, error.message, true);
      });
    });
    if (run) run.title = "Run code (Ctrl/Command+Enter)";
    save.title = "Save code (Ctrl/Command+S)";

    var savedOutput = sessionStorage.getItem(playgroundOutputKey);
    output.textContent = savedOutput === null ? "Run code to see output." : savedOutput;
    dispatchEditorInput();
  }
  var legalDocuments = {
    "b-terms": {
      title: "Terms of Service",
      sections: [
        {
          title: "1. About these Terms",
          paragraphs: [
            "These Terms govern your access to and use of Learn Python with Kite (the “Service”), including its lessons, quizzes, coding challenges, Playground, account features and subscriptions. By creating an account or using the Service, you agree to these Terms. If you do not agree, do not use the Service."
          ]
        },
        {
          title: "2. Accounts and security",
          paragraphs: [
            "Provide a name and email address that you are entitled to use and keep your sign-in details secure. You are responsible for activity performed through your account. Tell us promptly if you believe it has been accessed without permission. Google sign-in, when available, is also subject to the relevant provider’s terms."
          ]
        },
        {
          title: "3. Acceptable use",
          paragraphs: [
            "Use Kite for lawful learning and personal development. Do not use it to break the law, harm people or systems, distribute malware, attempt unauthorized access, interfere with service availability, evade account or premium-content controls, or copy or misuse another person’s content. Do not put passwords, API keys, personal information or other secrets into the Playground or a challenge."
          ]
        },
        {
          title: "4. Lessons, code and other content",
          paragraphs: [
            "Kite’s software, brand, lessons and interface are provided for your personal learning use. Rights in those materials remain with their respective owners; these Terms do not transfer ownership or permit you to republish or commercially exploit them.",
            "You retain any rights you have in original code or other content you submit. To provide a feature you choose to use, you allow Kite to receive, transmit, process and, where requested, execute that content and return its result. You are responsible for the content you submit and for having the rights to use it. Code execution depends on a configured runner and may be limited or unavailable."
          ]
        },
        {
          title: "5. Premium plans and payments",
          paragraphs: [
            "The current advertised Premium prices are K19.99 per month and K99.99 per year. Twelve monthly payments at the advertised rate total K239.88; the annual plan is K139.89 less (approximately 58.3%). The purchase screen and Google Play checkout, when available, show the terms that apply to your transaction.",
            "Purchases are processed through Google Play, not by Kite collecting your card details. A Premium entitlement is applied only after the purchase has been verified by the Service. Renewal, cancellation, payment method and refund arrangements are handled through Google Play and are subject to its purchase terms and applicable law. The app may be unable to offer checkout where its Google Play billing or product configuration is unavailable."
          ]
        },
        {
          title: "6. Availability and service limitations",
          paragraphs: [
            "Kite is an educational service. Lessons, quizzes and code examples may contain errors and are not a promise of a particular learning, examination, employment or other outcome. Features depend on internet access and on the availability and configuration of authentication, hosting, payment, email and code-execution providers. We may update, suspend or discontinue a feature, including for maintenance or security."
          ]
        },
        {
          title: "7. Suspension and ending use",
          paragraphs: [
            "You may stop using the Service at any time. We may restrict or suspend access when reasonably needed to address misuse, a security risk, a legal requirement or a material breach of these Terms. The current app does not provide an in-app account-deletion control; contact us to request account closure. Any outstanding payment or other obligation remains subject to the applicable purchase terms."
          ]
        },
        {
          title: "8. Disclaimers and liability",
          paragraphs: [
            "The Service is provided on an “as available” basis. To the extent permitted by applicable law, Kite does not guarantee uninterrupted or error-free operation and disclaims implied warranties that may lawfully be excluded. Nothing in these Terms excludes or limits a right, warranty or liability that applicable law does not allow to be excluded or limited."
          ]
        },
        {
          title: "9. Changes and contact",
          paragraphs: [
            "We may revise these Terms as the Service changes. The date shown with the Terms will be updated when the text changes; where a notice or renewed agreement is required by law, we will provide it. For questions, use the Contact Us section in the app."
          ]
        }
      ]
    },
    "b-priv": {
      title: "Privacy Policy",
      sections: [
        {
          title: "1. Scope and contact",
          paragraphs: [
            "This notice describes how the current Learn Python with Kite app handles information. Provider choices and storage locations can vary by deployment; where a provider is enabled, its own privacy terms also apply. Contact details for privacy requests are in the Contact Us section."
          ]
        },
        {
          title: "2. Information the Service processes",
          paragraphs: [
            "Account information: the name and email address you provide, an account identifier, and authentication information. With local authentication, the server stores a salted password hash rather than the plaintext password. If Supabase authentication is configured, Supabase handles sign-in credentials and the app receives account information needed to provide your Kite account.",
            "Learning and activity information: lesson completion and scores, quiz or challenge activity, XP, streaks, achievement progress and activity timestamps. Quiz answers are processed to calculate a result; the app records each quiz's best score and XP rather than keeping the submitted answer list. Challenge results may be recorded as activity.",
            "Profile and preferences: account name and settings such as theme, language and the notification preference. The browser may also retain display-only profile customizations, such as a nickname or avatar color.",
            "Subscription information: selected product, verification state, expiry and renewal state. When Google Play verification is configured, the server receives the purchase token for verification and stores a hash and an encrypted copy for subscription checks. Kite does not receive or store your Google Play payment-card number.",
            "Code and technical information: code is sent to the configured execution service when you choose Run or submit a coding challenge. The Kite database records challenge results and counts of Playground runs that fail and are later corrected, but not the submitted source code. The hosting or web server may log operational request information such as IP address, requested path, time and response status.",
            "Support information: if you contact us by email or telephone, the details and message you choose to provide are handled by the relevant communications provider. Contact Us does not submit a message to or store one in the Kite app."
          ]
        },
        {
          title: "3. How information is used",
          paragraphs: [
            "Information is used to create and secure accounts; authenticate users; provide lessons, progress, quizzes, challenges and leaderboards; save account settings and in-app notification state; verify Premium purchases and apply entitlements; respond to support or privacy requests; and operate, troubleshoot and protect the Service."
          ]
        },
        {
          title: "4. What is visible to other users",
          paragraphs: [
            "The leaderboard can show the first word of an account name and its XP to signed-in users. It does not show the account email address. Do not use a name you do not want displayed there."
          ]
        },
        {
          title: "5. Storage and service providers",
          paragraphs: [
            "Account and learning records are stored in the database configured for the deployment. Local development defaults to SQLite; a deployment may use a configured PostgreSQL database. The app can also use Supabase for authentication, Google for OAuth sign-in when enabled, Google Play for purchase verification, an SMTP provider for password-reset email, and a separate runner for code execution. The app also requests fonts from Google Fonts; that request can expose ordinary connection details such as your IP address to Google.",
            "Authentication tokens and short-lived sign-in/navigation state are kept in browser session storage. Some interface preferences and profile customizations are kept in browser local storage. These are browser storage mechanisms, not a Kite account export or backup. Other providers may use their own cookies or storage under their own policies.",
            "These providers receive information only as needed for the feature they provide, or where disclosure is required by law. Their processing, security practices and data locations are governed by their own terms and configuration. The current app code contains no advertising or analytics integration and does not sell personal information."
          ]
        },
        {
          title: "6. Retention and security",
          paragraphs: [
            "Account, progress, activity and subscription records remain in the configured database while the account is in use and until they are removed by the service operator or a retention requirement applies. The current app has no automatic account-retention schedule and no in-app data-export or account-deletion control. Operational-log retention depends on the hosting provider and deployment.",
            "Local-auth passwords are stored as salted, one-way hashes, and stored Google Play purchase tokens are encrypted by the app. Other safeguards, including transport security, database backups and provider access controls, depend on deployment configuration. No system can be guaranteed completely secure; use a unique password and do not submit secrets as code."
          ]
        },
        {
          title: "7. Your choices and privacy rights",
          paragraphs: [
            "You can update supported account details and settings in the app. Depending on the privacy law that applies to you, you may also have rights to request access, correction, deletion, restriction, portability or objection, or to withdraw consent where processing relies on consent. The app does not currently provide self-service export or account deletion. Send a request using the Contact Us details; we may need to verify your identity and may retain information where required or permitted by law."
          ]
        },
        {
          title: "8. Children and international processing",
          paragraphs: [
            "The current app does not verify age or provide a separate child-consent flow. A parent or guardian who believes a child has provided personal information may contact us to discuss it.",
            "The app does not fix a single hosting or storage country. If external providers are enabled, they may process information in locations determined by their service configuration. Contact us if you need details about a particular deployment."
          ]
        },
        {
          title: "9. Updates",
          paragraphs: [
            "We may update this notice when the Service or its providers change. We will update the date shown here; any additional notice required by applicable law will be provided."
          ]
        }
      ]
    }
  };
  function appendLegalParagraph(doc, parent, text) {
    var paragraph = doc.createElement("p");
    paragraph.textContent = text;
    paragraph.style.cssText = "margin:0 0 12px;line-height:1.55;color:var(--ink);font-size:14px";
    parent.appendChild(paragraph);
  }
  function renderLegalPage(doc, buttonId, trigger) {
    var overlay = doc.getElementById("ov");
    var sheet = doc.getElementById("sh");
    if (!overlay || !sheet) return;
    sheet.textContent = "";
    var title = buttonId === "b-con" ? "Contact Us" : legalDocuments[buttonId].title;
    var heading = doc.createElement("h4");
    heading.id = "st";
    heading.textContent = title;
    sheet.appendChild(heading);
    if (buttonId === "b-con") {
      var intro = doc.createElement("p");
      intro.className = "sub";
      intro.textContent = "For learning support or privacy requests, contact us directly.";
      sheet.appendChild(intro);
      var emailLabel = doc.createElement("p");
      emailLabel.className = "sub";
      emailLabel.textContent = "Email";
      sheet.appendChild(emailLabel);
      var email = doc.createElement("a");
      email.href = "mailto:learnwithkite.python@gmail.com";
      email.textContent = "learnwithkite.python@gmail.com";
      email.style.cssText = "display:block;margin:0 0 16px;color:var(--blue);font-weight:600;overflow-wrap:anywhere";
      sheet.appendChild(email);
      var phoneLabel = doc.createElement("p");
      phoneLabel.className = "sub";
      phoneLabel.textContent = "Phone";
      sheet.appendChild(phoneLabel);
      var phone = doc.createElement("a");
      phone.href = "tel:+260969830886";
      phone.textContent = "+260969830886";
      phone.style.cssText = "display:block;margin:0 0 16px;color:var(--blue);font-weight:600";
      sheet.appendChild(phone);
      appendLegalParagraph(doc, sheet, "The email link opens your email application. The phone link opens a dialer where your device supports it. The app does not store a Contact Us message.");
    } else {
      var updated = doc.createElement("p");
      updated.className = "note";
      updated.textContent = "Last updated: 14 November 2026";
      sheet.appendChild(updated);
      legalDocuments[buttonId].sections.forEach(function (section) {
        var subheading = doc.createElement("h5");
        subheading.textContent = section.title;
        subheading.style.cssText = "margin:18px 0 7px;font-size:15px;line-height:1.35;color:var(--ink)";
        sheet.appendChild(subheading);
        section.paragraphs.forEach(function (text) { appendLegalParagraph(doc, sheet, text); });
      });
    }
    var actions = doc.createElement("div");
    actions.className = "two";
    var close = doc.createElement("button");
    close.type = "button";
    close.className = "bt p";
    close.setAttribute("data-x", "");
    close.textContent = "Close";
    close.addEventListener("click", function () {
      if (trigger) setTimeout(function () { trigger.focus(); }, 0);
    });
    actions.appendChild(close);
    sheet.appendChild(actions);
    overlay.classList.add("on");
    close.focus();
  }
  function addSubscriptionSection(doc) {
    var main = doc.querySelector(".scroll");
    if (!main || doc.getElementById("kite-subscription")) return;
    var heading = doc.createElement("h2");
    heading.textContent = "Subscription";
    var group = doc.createElement("div");
    group.className = "grp";
    group.id = "kite-subscription";
    group.innerHTML = "<div class=\"row\"><span class=\"l\" id=\"kite-plan-state\">Kite Free</span><span class=\"val\">Current plan</span></div>" +
      "<button class=\"row\" type=\"button\" id=\"kite-monthly\"><span class=\"l\">Kite Premium · Monthly</span><span class=\"val\">Subscribe</span></button>" +
      "<button class=\"row\" type=\"button\" id=\"kite-annual\"><span class=\"l\">Kite Premium · Annual <span id=\"kite-best-value\" style=\"display:inline-block;margin-left:6px;padding:2px 7px;border-radius:10px;background:#e8f5ed;color:#267447;font-size:11px;font-weight:600;white-space:nowrap\">Best Value</span><span id=\"kite-annual-savings\" style=\"display:block;margin-top:3px;font-size:12px;font-weight:500\">Save K139.89 annually · 58.3% less</span></span><span class=\"val\">Subscribe</span></button>" +
      "<button class=\"row\" type=\"button\" id=\"kite-sign-in\"><span class=\"l\">Sign in to continue</span><span class=\"val\">Sign in</span></button>" +
      "<button class=\"row\" type=\"button\" id=\"kite-manage\"><span class=\"l\">Manage subscription</span><span class=\"val\">Google Play</span></button>" +
      "<p class=\"sub\" id=\"kite-billing-note\" style=\"padding:0 16px 12px\">Purchases are verified by Google Play.</p>";
    var appPreferences = Array.prototype.find.call(main.querySelectorAll("h2"), function (item) { return item.textContent.trim() === "App Preferences"; });
    if (appPreferences) main.insertBefore(heading, appPreferences);
    else main.appendChild(heading);
    if (appPreferences) main.insertBefore(group, appPreferences);
    else main.appendChild(group);
    var signIn = doc.getElementById("kite-sign-in");
    var manage = doc.getElementById("kite-manage");
    if (signIn) signIn.style.display = token() ? "none" : "flex";
    if (manage) manage.style.display = token() ? "flex" : "none";
    api("/api/plans").then(function (result) {
      ["monthly", "annual"].forEach(function (period) {
        var plan = result.plans.find(function (item) { return item.id === "premium_" + period; });
        var button = doc.getElementById("kite-" + period);
        if (plan && button) button.querySelector(".val").textContent = plan.price + (plan.period ? " / " + plan.period : "");
        if (period === "annual" && plan) {
          var bestValue = doc.getElementById("kite-best-value");
          var savings = doc.getElementById("kite-annual-savings");
          if (bestValue) bestValue.hidden = !plan.best_value;
          if (savings) savings.textContent = "Save " + plan.savings + " · " + plan.percentage_savings + " less";
        }
      });
    }).catch(function (error) {
      var note = doc.getElementById("kite-billing-note");
      if (/sign in|session has expired/i.test(error.message)) {
        if (signIn) signIn.style.display = "flex";
        if (manage) manage.style.display = "none";
      }
      if (note) note.textContent = error.message;
    });
    if (!token()) {
      var note = doc.getElementById("kite-billing-note");
      if (note) note.textContent = "Sign in to subscribe. Purchases are verified by Google Play.";
    } else api("/api/subscription").then(function (data) {
      var state = doc.getElementById("kite-plan-state");
      if (state) state.textContent = data.premium ? "Kite Premium" : "Kite Free";
      if (data.expires_at) {
        var note = doc.getElementById("kite-billing-note");
        note.textContent = (data.auto_renew ? "Renews" : "Access ends") + " " + new Date(data.expires_at).toLocaleDateString();
      }
    }).catch(function (error) {
      var note = doc.getElementById("kite-billing-note");
      if (note) note.textContent = error.message;
    });
    bindSubscriptionButtons(doc);
    if (signIn) signIn.addEventListener("click", function () { requestSubscriptionSignIn(); });
    var pendingPeriod = sessionStorage.getItem(pendingPlanKey);
    if (pendingPeriod === "monthly" || pendingPeriod === "annual") {
      doc.getElementById("kite-" + pendingPeriod).click();
    }
  }
      function setText(doc, selector, text) {
        var node = doc.querySelector(selector);
        if (node) node.textContent = text;
      }
      function updateNotificationBadge(doc, count) {
        var button = doc.getElementById("bell");
        if (!button) return;
        var badge = doc.getElementById("kite-notification-count");
        button.style.position = "relative";
        button.setAttribute("aria-label", count ? "Notifications, " + count + " unread" : "Notifications");
        if (!count) {
          if (badge) badge.remove();
          return;
        }
        if (!badge) {
          badge = doc.createElement("span");
          badge.id = "kite-notification-count";
          badge.setAttribute("aria-hidden", "true");
          badge.style.cssText = "position:absolute;top:0;right:0;min-width:17px;height:17px;padding:0 4px;border-radius:99px;background:#d64545;color:#fff;font:700 10px/17px system-ui;text-align:center";
          button.appendChild(badge);
        }
        badge.textContent = count > 99 ? "99+" : String(count);
      }
      function hydrateShellUser(me) {
        try {
          if (!me && token()) {
            setMoreMenuAuthState(true);
            if (!shellUserRequest) {
              shellUserRequest = api("/api/me").then(function (account) {
                hydrateShellUser(account);
              }).catch(function (error) {
                if (error.status === 401) {
                  clearAuthTokens();
                  hydrateShellUser(null);
                } else tell(doc, error.message, true);
              }).finally(function () {
                shellUserRequest = null;
              });
            }
            return;
          }
          setMoreMenuAuthState(Boolean(me));
          var menu = parent.document.getElementById("dr");
          var who = menu && menu.querySelector(".who");
          if (!who) return;
          var avatar = who.querySelector(".av");
          var name = who.querySelector("b");
          var detail = who.querySelector("small");
          if (!me) {
            if (avatar) avatar.textContent = "G";
            if (name) name.textContent = "Guest";
            if (detail) detail.textContent = "Progress is stored on this device";
            return;
          }
          if (avatar) avatar.textContent = me.name.trim().charAt(0).toUpperCase();
          if (name) name.textContent = me.name.split(" ")[0];
          if (detail) detail.textContent = Number(me.xp).toLocaleString() + " XP · " +
            (me.premium ? "Kite Premium" : "Kite Free");
        } catch (error) {
          console.error("Could not update the Kite navigation profile.", error);
        }
      }
      function hydrateHome(doc, me) {
        Promise.all([
          Promise.resolve(me),
          me ? api("/api/progress") : Promise.resolve(null),
          api("/api/lessons")
        ]).then(function (values) {
          var progress = values[1], lessons = values[2];
          var guestProgress = me ? null : readGuestProgress(doc);
          var freeLessons = lessons.filter(function (lesson) { return !lesson.premium; });
          var progressLessons = me ? lessons : freeLessons;
          var completedIds = me ? progress.completed_lessons : guestProgress.completed_lessons;
          var completed = progressLessons.filter(function (lesson) {
            return completedIds.indexOf(lesson.id) >= 0;
          }).length;
          var total = me ? lessons.length : freeLessons.length;
          setText(doc, "h1", me ? "Hello, " + me.name.split(" ")[0] + "!" : "Welcome, Guest");
          var percent = me
            ? (total ? Math.round(completed / total * 100) : 0)
            : (total ? Math.round(completed / total * 100) : 0);
          function applyProgress() {
            setText(doc, "#pct", percent + "%");
            var fill = doc.getElementById("fill");
            if (fill) fill.style.width = percent + "%";
            var bar = doc.querySelector('[role="progressbar"]');
            if (bar) bar.setAttribute("aria-valuenow", String(percent));
          }
          applyProgress();
          setTimeout(applyProgress, 1200);
          var next;
          if (me) {
            next = lessons.find(function (lesson) { return lesson.started && !lesson.locked; }) ||
              lessons.find(function (lesson) { return !lesson.completed && !lesson.locked; });
          } else {
            next = freeLessons.find(function (lesson) {
              return guestProgress.last_lesson === lesson.id &&
                guestProgress.completed_lessons.indexOf(lesson.id) < 0;
            }) || freeLessons.find(function (lesson) {
              return guestProgress.started_lessons.indexOf(lesson.id) >= 0;
            }) || freeLessons.find(function (lesson) {
              return guestProgress.completed_lessons.indexOf(lesson.id) < 0;
            });
          }
          var note = doc.querySelector(".pc small");
          if (note) note.textContent = me
            ? completed + " of " + total + " published lessons completed"
            : completed + " of " + total + " free lessons completed · progress is on this device";
          if (me) {
            api("/api/notifications/unread-count").then(function (value) {
              updateNotificationBadge(doc, Number(value.count) || 0);
            }).catch(function (error) { tell(doc, error.message, true); });
          } else {
            updateNotificationBadge(doc, readGuestNotifications().filter(function (item) { return !item.read; }).length);
          }
          if (next) sessionStorage.setItem("kite.lessonId", next.id);
          if (!me && next) {
            var guestLessonCard = doc.querySelector(".lc:not(.kite-guest-progress)");
            if (guestLessonCard) {
              setText(doc, ".lc:not(.kite-guest-progress) h3", next.title);
              setText(doc, ".lc:not(.kite-guest-progress) p", next.description);
            }
          }
          if (me) addGuestProgressPrompt(doc, doc.querySelector(".scroll"), true);
        }).catch(function (error) { tell(doc, error.message, true); });
      }
      function hydrateLearn(doc) {
        api("/api/modules").then(function (modules) {
          var list = doc.getElementById("mods");
          if (!list) return;
          var guestProgress = token() ? null : readGuestProgress(doc);
          list.textContent = "";
          modules.forEach(function (module) {
            var heading = doc.createElement("li");
            heading.className = "kite-module-heading";
            heading.style.cssText = "list-style:none;padding:18px 4px 8px;display:flex;justify-content:space-between;align-items:baseline;gap:8px";
            var title = doc.createElement("strong");
            title.textContent = module.order + ". " + module.title;
            var level = doc.createElement("small");
            var guestModuleCompletions = guestProgress
              ? module.lessons.filter(function (lesson) {
                return guestProgress.completed_lessons.indexOf(lesson.id) >= 0;
              }).length
              : 0;
            level.textContent = (module.completed_lessons + guestModuleCompletions) +
              " / " + module.lesson_count + " " + (module.lesson_count === 1 ? "lesson" : "lessons") +
              " · " + module.quizzes.filter(function (quiz) {
                return !quiz.locked && (quiz.completed ||
                  (guestProgress && guestQuizHistory(quiz).length > 0));
              }).length + " / " + module.quiz_count + " quizzes" +
              " · " + module.challenge_count + " " +
              (module.challenge_count === 1 ? "challenge" : "challenges") + " · " + module.level;
            heading.append(title, level);
            list.appendChild(heading);
            module.lessons.forEach(function (lesson, index) {
              var guestCompleted = guestProgress &&
                guestProgress.completed_lessons.indexOf(lesson.id) >= 0;
              var guestStarted = guestProgress &&
                guestProgress.started_lessons.indexOf(lesson.id) >= 0;
              var completed = lesson.completed || guestCompleted;
              var started = lesson.started || guestStarted;
              var item = doc.createElement("li");
              var row = doc.createElement("div");
              row.className = "m " + (lesson.locked ? "lock" : (completed ? "done" : (started ? "cur" : "")));
              var number = doc.createElement("span");
              number.className = "n";
              number.textContent = String(index + 1);
              var text = doc.createElement("span");
              text.className = "t";
              var lessonTitle = doc.createElement("b");
              lessonTitle.textContent = lesson.title;
              var description = doc.createElement("small");
              description.textContent = lesson.description;
              text.append(lessonTitle, description);
              var state = doc.createElement("span");
              state.className = "st";
              if (lesson.locked) {
                state.textContent = "Premium";
              } else {
                var button = doc.createElement("button");
                button.className = "go";
                button.textContent = completed ? "Review" : (started ? "Continue" : "Start");
                button.dataset.kiteLesson = lesson.id;
                state.appendChild(button);
              }
              row.append(number, text, state);
              item.appendChild(row);
              list.appendChild(item);
            });
          });
        }).catch(function (error) { tell(doc, error.message, true); });
      }
      function parseLessonSections(content) {
        var names = [
          "Learning objectives", "Core concept", "Why it matters", "How it works",
          "Syntax", "Read the syntax", "Worked example", "Step-by-step walkthrough",
          "Expected output", "Understand the result", "Practical application",
          "Common mistakes", "Practice exercises", "Knowledge check"
        ];
        var sections = {};
        var current = null;
        String(content || "").split("\n").forEach(function (line) {
          if (names.indexOf(line.trim()) >= 0) {
            current = line.trim();
            sections[current] = [];
          } else if (current) {
            sections[current].push(line);
          }
        });
        Object.keys(sections).forEach(function (name) {
          sections[name] = sections[name].join("\n").trim();
        });
        return sections;
      }
      function appendLessonSection(doc, panel, title, value) {
        if (!value) return;
        var heading = doc.createElement("h3");
        heading.textContent = title;
        panel.appendChild(heading);
        var lines = value.split("\n");
        if (title === "Syntax" || title === "Worked example" || title === "Expected output") {
          var code = doc.createElement("pre");
          code.className = "code";
          code.textContent = value;
          panel.appendChild(code);
          return;
        }
        var list = lines.length && lines.every(function (line) { return /^\s*(?:-\s+|\d+\.\s+)/.test(line); });
        if (list) {
          var items = doc.createElement(title === "Step-by-step walkthrough" ? "ol" : "ul");
          lines.forEach(function (line) {
            var item = doc.createElement("li");
            item.textContent = line.replace(/^\s*(?:-\s+|\d+\.\s+)/, "").replace(/`/g, "");
            items.appendChild(item);
          });
          panel.appendChild(items);
          return;
        }
        var paragraph = doc.createElement("p");
        paragraph.textContent = value;
        panel.appendChild(paragraph);
      }
      function renderLessonTab(doc, lesson, tab) {
        var panel = doc.getElementById("pn");
        if (!panel) return;
        var sections = parseLessonSections(lesson.content);
        panel.textContent = "";
        panel.setAttribute("aria-labelledby", "tb-" + tab);
        Array.prototype.forEach.call(doc.querySelectorAll("[data-tab]"), function (button) {
          var selected = button.getAttribute("data-tab") === tab;
          button.setAttribute("aria-selected", String(selected));
          button.tabIndex = selected ? 0 : -1;
        });
        if (tab === "example") {
          var title = doc.createElement("h2");
          title.textContent = "Worked example: " + lesson.title;
          panel.appendChild(title);
          var summary = doc.createElement("p");
          summary.textContent = lesson.description;
          panel.appendChild(summary);
          appendLessonSection(doc, panel, "Worked example", sections["Worked example"]);
          appendLessonSection(doc, panel, "Step-by-step walkthrough", sections["Step-by-step walkthrough"]);
          appendLessonSection(doc, panel, "Expected output", sections["Expected output"]);
          appendLessonSection(doc, panel, "Practice exercises", sections["Practice exercises"]);
          return;
        }
        if (tab === "quiz") {
          var quizTitle = doc.createElement("h2");
          quizTitle.textContent = "Check your understanding";
          panel.appendChild(quizTitle);
          var quizDescription = doc.createElement("p");
          quizDescription.textContent = "Choose a scored quiz from " + lesson.module_title +
            ". Your answers and explanations are handled by Kite's existing quiz flow.";
          panel.appendChild(quizDescription);
          api("/api/quizzes").then(function (catalog) {
            var matching = catalog.filter(function (item) {
              return item.module_id === lesson.module_id && !item.locked;
            });
            if (!matching.length) {
              var empty = doc.createElement("p");
              empty.textContent = "No quiz is currently available for this module.";
              panel.appendChild(empty);
              return;
            }
            matching.forEach(function (item) {
              var button = doc.createElement("button");
              button.type = "button";
              button.className = "go";
              button.style.cssText = "display:block;margin:10px 0";
              button.textContent = "Start quiz: " + item.title + " (" + item.question_count + " questions)";
              button.addEventListener("click", function () {
                sessionStorage.setItem("kite.quizId", item.id);
                sessionStorage.setItem(quizEntryModeKey, "runner");
                sessionStorage.setItem(pendingQuizDraftIdKey, item.id);
                sessionStorage.removeItem(pendingQuizSubmissionIdKey);
                sessionStorage.removeItem(pendingQuizAnswersKey);
                sessionStorage.removeItem(pendingQuizIndexKey);
                sessionStorage.removeItem(completedQuizResultKey);
                navigate("quiz");
              });
              panel.appendChild(button);
            });
          }).catch(function (error) { tell(doc, error.message, true); });
          return;
        }
        var heading = doc.createElement("h2");
        heading.textContent = lesson.title;
        panel.appendChild(heading);
        var description = doc.createElement("p");
        description.textContent = lesson.description;
        panel.appendChild(description);
        [
          "Learning objectives", "Core concept", "Why it matters", "How it works",
          "Syntax", "Read the syntax", "Practical application", "Common mistakes",
          "Practice exercises", "Knowledge check"
        ].forEach(function (name) {
          appendLessonSection(doc, panel, name, sections[name]);
        });
      }
      function hydrateLesson(doc) {
        var id = sessionStorage.getItem("kite.lessonId") || "python-variables";
        api("/api/lessons/" + encodeURIComponent(id)).then(function (lesson) {
          recordGuestLesson(doc, lesson.id, false);
          setText(doc, "header h1", lesson.title);
          doc.documentElement.kiteLesson = lesson;
          doc.addEventListener("click", function (event) {
            var tab = event.target.closest && event.target.closest("[data-tab]");
            if (!tab || !tab.closest(".tabs")) return;
            var tabName = tab.getAttribute("data-tab");
            if (["lesson", "example", "quiz"].indexOf(tabName) < 0) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            renderLessonTab(doc, lesson, tabName);
          }, true);
          var initialTab = "lesson";
          try {
            var returnContext = JSON.parse(sessionStorage.getItem(playgroundReturnContextKey) || "null");
            if (returnContext && returnContext.route === "lesson" &&
                ["lesson", "example", "quiz"].indexOf(returnContext.lessonTab) >= 0) {
              initialTab = returnContext.lessonTab;
            }
          } catch (error) {
            console.error("Could not restore the lesson section after leaving Playground.", error);
          }
          renderLessonTab(doc, lesson, initialTab);
        }).catch(function (error) {
          tell(doc, error.message, true);
          if (error.message.indexOf("Kite Premium") >= 0) navigate("preferences");
        });
      }
      function addContentPicker(doc, route, items, selectedId, onChange) {
        var id = route === "quiz" ? "kite-quiz-select" : "kite-challenge-select";
        var select = doc.getElementById(id);
        if (!select) {
          var wrapper = doc.createElement("label");
          wrapper.className = "kite-content-picker";
          wrapper.style.cssText = "display:block;margin:10px 16px;padding:0 12px";
          var caption = doc.createElement("span");
          caption.textContent = route === "quiz" ? "Choose a quiz" : "Choose a challenge";
          caption.style.cssText = "display:block;font-size:12px;margin:0 0 5px";
          select = doc.createElement("select");
          select.id = id;
          select.setAttribute("aria-label", caption.textContent);
          select.style.cssText = "box-sizing:border-box;width:100%;min-height:42px;padding:8px;border-radius:10px";
          wrapper.append(caption, select);
          var anchor = route === "quiz" ? doc.getElementById("pb") : doc.querySelector("main");
          if (route === "quiz" && anchor && anchor.parentNode) anchor.parentNode.insertBefore(wrapper, anchor);
          else if (anchor) anchor.insertBefore(wrapper, anchor.firstChild);
        }
        select.textContent = "";
        var grouped = {};
        items.forEach(function (item) {
          var groupName = item.module_title || "Python";
          if (!grouped[groupName]) {
            grouped[groupName] = doc.createElement("optgroup");
            grouped[groupName].label = groupName;
            select.appendChild(grouped[groupName]);
          }
          var option = doc.createElement("option");
          option.value = item.id;
          option.textContent = item.title + (item.locked ? " · Kite Premium" : "");
          option.disabled = Boolean(item.locked);
          grouped[groupName].appendChild(option);
        });
        if (selectedId && items.some(function (item) { return item.id === selectedId && !item.locked; })) {
          select.value = selectedId;
        }
        select.onchange = function () { onChange(select.value); };
        return select;
      }
      function ensureQuizStyles(doc) {
        if (doc.getElementById("kite-assessment-styles")) return;
        var style = doc.createElement("style");
        style.id = "kite-assessment-styles";
        style.textContent =
          ".kite-assessment-dashboard{width:min(100%,900px);margin:0 auto;padding:18px 16px 30px;color:var(--ink,#0e1b36)}" +
          ".kite-assessment-hero{padding:18px;border:1px solid var(--line,#e3eaf5);border-radius:20px;background:var(--card,#fff);box-shadow:0 8px 26px rgba(20,46,87,.06)}" +
          ".kite-assessment-hero h2{margin:0 0 6px;font-size:22px;letter-spacing:-.3px}.kite-assessment-hero p{margin:0;color:var(--mut,#66758d);line-height:1.5}" +
          ".kite-assessment-stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:16px}" +
          ".kite-assessment-stat{padding:11px;border-radius:13px;background:var(--soft,#e8f0fe)}.kite-assessment-stat b{display:block;font-size:18px}.kite-assessment-stat span{font-size:12px;color:var(--mut,#66758d)}" +
          ".kite-assessment-section{margin:24px 0}.kite-assessment-section h2{margin:0 0 12px;font-size:18px}.kite-assessment-grid{display:grid;gap:12px}" +
          ".kite-assessment-card{min-width:0;padding:16px;border:1px solid var(--line,#e3eaf5);border-radius:17px;background:var(--card,#fff)}" +
          ".kite-assessment-card h3{margin:0;font-size:17px}.kite-assessment-card p{margin:7px 0;color:var(--mut,#66758d);font-size:13px;line-height:1.5}" +
          ".kite-assessment-badges{display:flex;flex-wrap:wrap;gap:6px;margin:9px 0}.kite-assessment-badge{padding:4px 8px;border-radius:999px;background:var(--soft,#e8f0fe);font-size:11px;font-weight:700}" +
          ".kite-assessment-actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}.kite-assessment-button{min-height:44px;padding:10px 14px;border:1px solid var(--line,#e3eaf5);border-radius:12px;background:var(--card,#fff);color:var(--ink,#0e1b36);font:600 14px system-ui,sans-serif;cursor:pointer}" +
          ".kite-assessment-button.primary{background:var(--blue,#1d6bf0);border-color:var(--blue,#1d6bf0);color:#fff}.kite-assessment-button:disabled{opacity:.55;cursor:not-allowed}" +
          ".kite-assessment-details{margin-top:11px;border-top:1px solid var(--line,#e3eaf5);padding-top:10px;font-size:13px}.kite-assessment-details summary{cursor:pointer;font-weight:650;padding:5px 0}" +
          ".kite-assessment-details li{margin:6px 0;color:var(--mut,#66758d)}.kite-assessment-empty{padding:14px;border-radius:13px;background:var(--soft,#e8f0fe);color:var(--mut,#66758d)}" +
          ".kite-quiz-type{display:inline-block;margin:0 0 10px;padding:5px 9px;border-radius:999px;background:var(--soft,#e8f0fe);font-size:12px;font-weight:700}" +
          ".kite-quiz-code{overflow:auto;padding:14px;border-radius:12px;background:#101b2d;color:#eef4ff;white-space:pre-wrap;font:13px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}" +
          ".kite-quiz-intro{padding:12px;border-radius:13px;background:var(--soft,#e8f0fe);margin-bottom:16px}.kite-quiz-validation{min-height:20px;color:var(--red,#d64545);font-size:13px}" +
          ".kite-quiz-feedback{padding:13px 0;border-top:1px solid var(--line,#e3eaf5)}.kite-quiz-feedback h3{margin:8px 0;font-size:15px}" +
          ".kite-quiz-feedback p{margin:6px 0;line-height:1.5}.kite-quiz-result-pass{color:#16824b}.kite-quiz-result-fail{color:var(--red,#d64545)}" +
          ".kite-quiz-navigation{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:10px 14px calc(10px + env(safe-area-inset-bottom,0px))}" +
          ".kite-quiz-navigation button{min-height:46px}.kite-quiz-back{margin-bottom:14px}" +
          "@media(min-width:640px){.kite-assessment-stats{grid-template-columns:repeat(4,minmax(0,1fr))}.kite-assessment-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.kite-assessment-dashboard{padding:24px 24px 38px}}" +
          "@media(prefers-reduced-motion:reduce){.kite-assessment-button{scroll-behavior:auto}}";
        doc.head.appendChild(style);
      }
      function readGuestQuizAttempts() {
        try {
          var parsed = JSON.parse(localStorage.getItem(guestQuizAttemptsKey) || "{}");
          return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
        } catch (error) {
          console.warn("Could not read quiz history from this device.", error);
          return {};
        }
      }
      function readQuizDrafts() {
        try {
          var parsed = JSON.parse(localStorage.getItem(quizDraftsKey) || "{}");
          return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
        } catch (error) {
          console.warn("Could not read unfinished quiz work from this device.", error);
          return {};
        }
      }
      function quizHasDraft(item) {
        var drafts = readQuizDrafts();
        var draft = drafts[item.id];
        if (draft && Array.isArray(draft.answers) && draft.answers.length === item.question_count) return true;
        if (sessionStorage.getItem(pendingQuizDraftIdKey) !== item.id) return false;
        try {
          var answers = JSON.parse(sessionStorage.getItem(pendingQuizAnswersKey) || "null");
          return Array.isArray(answers) && answers.length === item.question_count;
        } catch (error) {
          console.warn("Could not read the current quiz draft.", error);
          return false;
        }
      }
      function saveQuizDraft(state, doc) {
        var drafts = readQuizDrafts();
        drafts[state.id] = {
          answers: state.answers,
          index: state.index,
          submission_id: sessionStorage.getItem(pendingQuizSubmissionIdKey),
          updated_at: new Date().toISOString()
        };
        try {
          localStorage.setItem(quizDraftsKey, JSON.stringify(drafts));
        } catch (error) {
          console.error("Could not save quiz progress on this device.", error);
          tell(doc, "Quiz progress could not be saved on this device. Keep this page open until you finish.", true);
        }
        sessionStorage.setItem(pendingQuizDraftIdKey, state.id);
        sessionStorage.setItem(pendingQuizAnswersKey, JSON.stringify(state.answers));
        sessionStorage.setItem(pendingQuizIndexKey, String(state.index));
      }
      function removeQuizDraft(quizId) {
        var drafts = readQuizDrafts();
        delete drafts[quizId];
        try {
          localStorage.setItem(quizDraftsKey, JSON.stringify(drafts));
        } catch (error) {
          console.error("Could not clear the completed quiz draft.", error);
        }
        if (sessionStorage.getItem(pendingQuizDraftIdKey) === quizId) {
          sessionStorage.removeItem(pendingQuizDraftIdKey);
          sessionStorage.removeItem(pendingQuizAnswersKey);
          sessionStorage.removeItem(pendingQuizIndexKey);
          sessionStorage.removeItem(pendingQuizSubmissionIdKey);
        }
      }
      function newQuizSubmissionId(doc) {
        var cryptoApi = doc.defaultView && doc.defaultView.crypto;
        if (cryptoApi && typeof cryptoApi.randomUUID === "function") return cryptoApi.randomUUID();
        if (cryptoApi && typeof cryptoApi.getRandomValues === "function") {
          var bytes = new Uint8Array(16);
          cryptoApi.getRandomValues(bytes);
          return Array.prototype.map.call(bytes, function (byte) {
            return ("0" + byte.toString(16)).slice(-2);
          }).join("");
        }
        return Date.now().toString(16) + "-" + Math.random().toString(16).slice(2) +
          Math.random().toString(16).slice(2);
      }
      function guestQuizHistory(item) {
        var history = readGuestQuizAttempts()[item.id];
        return Array.isArray(history) ? history : [];
      }
      function storeGuestQuizAttempt(quizId, result) {
        var attempts = readGuestQuizAttempts();
        if (!Array.isArray(attempts[quizId])) attempts[quizId] = [];
        if (result.submission_id && attempts[quizId].some(function (attempt) {
          return attempt.submission_id === result.submission_id;
        })) return;
        attempts[quizId].unshift({
          score: result.score,
          total: result.total,
          percentage: result.percentage,
          passed: result.passed,
          submission_id: result.submission_id || null,
          created_at: new Date().toISOString(),
          feedback: result.feedback
        });
        attempts[quizId] = attempts[quizId].slice(0, 50);
        try {
          localStorage.setItem(guestQuizAttemptsKey, JSON.stringify(attempts));
        } catch (error) {
          console.error("Could not save guest quiz history on this device.", error);
        }
      }
      function quizHistory(item, signedIn) {
        return signedIn ? (item.attempts || []) : guestQuizHistory(item);
      }
      function quizScore(item, signedIn) {
        if (signedIn) return item.best_percentage;
        var attempts = guestQuizHistory(item);
        return attempts.reduce(function (best, attempt) {
          return best === null || attempt.percentage > best ? attempt.percentage : best;
        }, null);
      }
      function makeQuizButton(doc, label, callback, primary, disabled) {
        var button = doc.createElement("button");
        button.type = "button";
        button.className = "kite-assessment-button" + (primary ? " primary" : "");
        button.textContent = label;
        button.disabled = Boolean(disabled);
        if (!disabled) button.addEventListener("click", callback);
        return button;
      }
      function quizDate(value) {
        if (!value) return "";
        var date = new Date(value);
        return isNaN(date.getTime()) ? "" : date.toLocaleDateString();
      }
      function renderQuizCard(doc, item, modulesById, challengesById, signedIn, draftId) {
        var card = doc.createElement("article");
        card.className = "kite-assessment-card";
        var heading = doc.createElement("h3");
        heading.textContent = item.title;
        var summary = doc.createElement("p");
        summary.textContent = item.question_count + " questions · " + item.difficulty +
          " · Pass mark " + item.pass_threshold + "%";
        card.append(heading, summary);

        var badges = doc.createElement("div");
        badges.className = "kite-assessment-badges";
        [
          item.locked ? "Premium · Locked" : "Available",
          item.completed || (!signedIn && guestQuizHistory(item).length) ? "Completed before" : "Not attempted"
        ].forEach(function (value) {
          var badge = doc.createElement("span");
          badge.className = "kite-assessment-badge";
          badge.textContent = value;
          badges.appendChild(badge);
        });
        card.appendChild(badges);

        var signedScore = quizScore(item, signedIn);
        var attempts = quizHistory(item, signedIn);
        var attemptCount = signedIn ? item.attempt_count : attempts.length;
        var savedScore = signedIn ? item.best_score : attempts.reduce(function (best, attempt) {
          return best === null || attempt.score > best ? attempt.score : best;
        }, null);
        var savedTotal = signedIn ? item.best_total : (attempts[0] && attempts[0].total);
        if (savedScore !== null && savedScore !== undefined && savedTotal) {
          var best = doc.createElement("p");
          best.textContent = "Best score: " + savedScore + " / " + savedTotal + " (" + signedScore + "%)" +
            (attemptCount ? " · " + attemptCount + (attemptCount === 1 ? " attempt" : " attempts") : "");
          card.appendChild(best);
        } else if (attemptCount) {
          var count = doc.createElement("p");
          count.textContent = attemptCount + (attemptCount === 1 ? " attempt" : " attempts");
          card.appendChild(count);
        }

        var actions = doc.createElement("div");
        actions.className = "kite-assessment-actions";
        if (item.locked) {
          actions.appendChild(makeQuizButton(doc, "View subscription options", function () {
            navigate("preferences");
          }, false, false));
        } else {
          var hasDraft = quizHasDraft(item);
          actions.appendChild(makeQuizButton(doc, hasDraft ? "Resume quiz" : "Start quiz", function () {
            sessionStorage.setItem("kite.quizId", item.id);
            sessionStorage.setItem(quizEntryModeKey, "runner");
            sessionStorage.removeItem(completedQuizResultKey);
            if (sessionStorage.getItem(pendingQuizDraftIdKey) !== item.id) {
              sessionStorage.removeItem(pendingQuizSubmissionIdKey);
            }
            if (!hasDraft) {
              removeQuizDraft(item.id);
              sessionStorage.removeItem(pendingQuizSubmissionIdKey);
              sessionStorage.removeItem(pendingQuizAnswersKey);
              sessionStorage.removeItem(pendingQuizIndexKey);
            }
            sessionStorage.setItem(pendingQuizDraftIdKey, item.id);
            hydrateQuiz(doc, item.id, true);
          }, true, false));
        }
        var challenge = item.practice_challenge_id && challengesById[item.practice_challenge_id];
        if (challenge) {
          actions.appendChild(makeQuizButton(doc,
            signedIn ? "Coding practice" : "Coding practice · sign in to run", function () {
            sessionStorage.setItem("kite.challengeId", challenge.id);
            navigate("challenge");
          }, false, challenge.locked));
        }
        card.appendChild(actions);

        if (attempts.length) {
          var historyDetails = doc.createElement("details");
          historyDetails.className = "kite-assessment-details";
          var historySummary = doc.createElement("summary");
          historySummary.textContent = "Recent attempts" +
            (attemptCount > attempts.length ? " (latest " + attempts.length + " of " + attemptCount + ")" : "");
          historyDetails.appendChild(historySummary);
          var historyList = doc.createElement("ul");
          attempts.forEach(function (attempt) {
            var row = doc.createElement("li");
            row.textContent = attempt.score + " / " + attempt.total + " (" + attempt.percentage + "%) · " +
              (attempt.passed ? "Passed" : "Review recommended") +
              (quizDate(attempt.created_at) ? " · " + quizDate(attempt.created_at) : "");
            historyList.appendChild(row);
          });
          historyDetails.appendChild(historyList);
          card.appendChild(historyDetails);
        }

        var module = modulesById[item.module_id];
        var lessons = (item.lesson_ids || []).map(function (id) {
          return module && (module.lessons || []).find(function (lesson) { return lesson.id === id; });
        }).filter(Boolean);
        if (lessons.length) {
          var review = doc.createElement("details");
          review.className = "kite-assessment-details";
          var reviewSummary = doc.createElement("summary");
          reviewSummary.textContent = "Review lessons for this topic";
          var lessonList = doc.createElement("ul");
          lessons.forEach(function (lesson) {
            var lessonItem = doc.createElement("li");
            lessonItem.textContent = lesson.title + (lesson.locked ? " · Premium" : "");
            lessonList.appendChild(lessonItem);
          });
          review.append(reviewSummary, lessonList);
          card.appendChild(review);
        }
        return card;
      }
      function renderQuizDashboard(doc, catalog, modules, challenges) {
        ensureQuizStyles(doc);
        var root = doc.getElementById("kite-quiz-dashboard");
        if (root) root.remove();
        root = doc.createElement("main");
        root.id = "kite-quiz-dashboard";
        root.className = "kite-assessment-dashboard";
        var signedIn = Boolean(token());
        var available = catalog.filter(function (item) { return !item.locked; });
        var completed = available.filter(function (item) {
          return signedIn ? item.completed : guestQuizHistory(item).length > 0;
        });
        var totalAttempts = available.reduce(function (total, item) {
          return total + (signedIn ? item.attempt_count : guestQuizHistory(item).length);
        }, 0);
        var bestScores = available.map(function (item) { return quizScore(item, signedIn); })
          .filter(function (value) { return Number.isFinite(value); });
        var average = bestScores.length ?
          Math.round(bestScores.reduce(function (sum, value) { return sum + value; }, 0) / bestScores.length) + "%" :
          "—";
        var hero = doc.createElement("section");
        hero.className = "kite-assessment-hero";
        var title = doc.createElement("h2");
        title.textContent = "Quiz and Assessment Centre";
        var description = doc.createElement("p");
        description.textContent = "Choose an assessment by Python module and topic. Results and explanations help you decide what to review next.";
        var stats = doc.createElement("div");
        stats.className = "kite-assessment-stats";
        [
          [available.length, "Available quizzes"],
          [completed.length + " / " + available.length, "Completed"],
          [totalAttempts, "Recorded attempts"],
          [average, "Average best score"]
        ].forEach(function (entry) {
          var stat = doc.createElement("div");
          stat.className = "kite-assessment-stat";
          var value = doc.createElement("b");
          value.textContent = entry[0];
          var label = doc.createElement("span");
          label.textContent = entry[1];
          stat.append(value, label);
          stats.appendChild(stat);
        });
        hero.append(title, description, stats);
        root.appendChild(hero);

        var modulesById = {};
        modules.forEach(function (module) { modulesById[module.id] = module; });
        var challengesById = {};
        challenges.forEach(function (challenge) { challengesById[challenge.id] = challenge; });
        var draftId = sessionStorage.getItem(pendingQuizDraftIdKey);
        if (draftId && !sessionStorage.getItem(pendingQuizAnswersKey)) draftId = null;
        var recommendations = available.slice().sort(function (left, right) {
          var leftScore = quizScore(left, signedIn);
          var rightScore = quizScore(right, signedIn);
          var leftPriority = leftScore === null || leftScore === undefined ? 1 : leftScore < left.pass_threshold ? 0 : 2;
          var rightPriority = rightScore === null || rightScore === undefined ? 1 : rightScore < right.pass_threshold ? 0 : 2;
          if (leftPriority !== rightPriority) return leftPriority - rightPriority;
          if (leftPriority === 1 && leftScore !== rightScore) return leftScore - rightScore;
          return left.module_order - right.module_order;
        }).slice(0, 3);
        var recommendationSection = doc.createElement("section");
        recommendationSection.className = "kite-assessment-section";
        var recommendationTitle = doc.createElement("h2");
        recommendationTitle.textContent = "Recommended next";
        recommendationSection.appendChild(recommendationTitle);
        if (recommendations.length) {
          var recommendationGrid = doc.createElement("div");
          recommendationGrid.className = "kite-assessment-grid";
          recommendations.forEach(function (item) {
            recommendationGrid.appendChild(renderQuizCard(doc, item, modulesById, challengesById, signedIn, draftId));
          });
          recommendationSection.appendChild(recommendationGrid);
        } else {
          var empty = doc.createElement("p");
          empty.className = "kite-assessment-empty";
          empty.textContent = "No unlocked quizzes are available right now.";
          recommendationSection.appendChild(empty);
        }
        root.appendChild(recommendationSection);

        var orderedModules = modules.slice().sort(function (a, b) { return a.order - b.order; });
        orderedModules.forEach(function (module) {
          var quizzes = catalog.filter(function (item) { return item.module_id === module.id; });
          if (!quizzes.length) return;
          var section = doc.createElement("section");
          section.className = "kite-assessment-section";
          var heading = doc.createElement("h2");
          heading.textContent = "Module " + module.order + " · " + module.title;
          section.appendChild(heading);
          var grid = doc.createElement("div");
          grid.className = "kite-assessment-grid";
          quizzes.forEach(function (item) {
            grid.appendChild(renderQuizCard(doc, item, modulesById, challengesById, signedIn, draftId));
          });
          section.appendChild(grid);
          root.appendChild(section);
        });

        var anchor = doc.getElementById("pb");
        if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(root, anchor);
        ["pb", "sc", "ft"].forEach(function (id) {
          var element = doc.getElementById(id);
          if (element) element.style.display = "none";
        });
        var picker = doc.getElementById("kite-quiz-select");
        if (picker && picker.closest(".kite-content-picker")) picker.closest(".kite-content-picker").style.display = "none";
        var count = doc.getElementById("cnt");
        if (count) count.style.display = "none";
        setText(doc, "#ttl", "Quizzes");
        doc.documentElement.kiteQuizCatalog = catalog;
        doc.documentElement.kiteQuizDashboard = true;
      }
      function hydrateQuizDashboard(doc) {
        Promise.all([api("/api/quizzes"), api("/api/modules"), api("/api/challenges")])
          .then(function (data) {
            renderQuizDashboard(doc, data[0], data[1], data[2]);
          }).catch(function (error) {
            tell(doc, error.message, true);
          });
      }
      function showQuizDashboard(doc) {
        sessionStorage.removeItem(quizEntryModeKey);
        sessionStorage.removeItem(completedQuizResultKey);
        doc.documentElement.kiteQuizState = null;
        hydrateQuizDashboard(doc);
      }
      function ensureQuizNavigation(doc, state) {
        var footer = doc.getElementById("ft");
        if (!footer) return;
        footer.classList.add("kite-quiz-navigation");
        var previous = doc.getElementById("pv");
        if (!previous) {
          previous = doc.createElement("button");
          previous.id = "pv";
          previous.type = "button";
          previous.className = "kite-assessment-button";
          previous.textContent = "Previous";
          footer.insertBefore(previous, footer.firstChild);
        }
        var next = doc.getElementById("nx");
        if (!next) {
          next = doc.createElement("button");
          next.id = "nx";
          next.type = "button";
          next.className = "kite-assessment-button primary";
          footer.appendChild(next);
        }
        previous.disabled = state.index === 0;
        previous.setAttribute("aria-label", "Previous question");
        var isFinalQuestion = state.index === state.questions.length - 1;
        next.textContent = isFinalQuestion ? "Submit quiz" : "Next question";
        next.disabled = !isFinalQuestion && !Number.isInteger(state.answers[state.index]);
        var validation = doc.getElementById("kite-quiz-validation");
        if (!validation) {
          validation = doc.createElement("div");
          validation.id = "kite-quiz-validation";
          validation.className = "kite-quiz-validation";
          validation.setAttribute("role", "status");
          footer.parentNode.insertBefore(validation, footer);
        }
        validation.textContent = next.disabled ? "Select an answer to continue." :
          (isFinalQuestion && !Number.isInteger(state.answers[state.index])
            ? "Unanswered questions will be marked incorrect if you submit."
            : "");
        footer.style.display = "";
      }
      function quizQuestionTypeLabel(type) {
        var labels = {
          multiple_choice: "Multiple choice",
          true_false: "True / false",
          output_prediction: "Output prediction",
          code_interpretation: "Code interpretation",
          debugging: "Debugging",
          error_identification: "Error identification"
        };
        return labels[type] || String(type || "multiple_choice").replace(/_/g, " ");
      }
      function renderQuizQuestion(doc, state) {
        var panel = doc.getElementById("sc");
        if (!panel || !state || !state.questions.length) return;
        ensureQuizStyles(doc);
        var dashboard = doc.getElementById("kite-quiz-dashboard");
        if (dashboard) dashboard.style.display = "none";
        ["pb", "sc", "ft"].forEach(function (id) {
          var element = doc.getElementById(id);
          if (element) element.style.display = "";
        });
        var picker = doc.getElementById("kite-quiz-select");
        if (picker && picker.closest(".kite-content-picker")) picker.closest(".kite-content-picker").style.display = "";
        var countLabel = doc.getElementById("cnt");
        if (countLabel) countLabel.style.display = "";
        var question = state.questions[state.index];
        panel.textContent = "";
        var back = doc.createElement("button");
        back.type = "button";
        back.className = "kite-assessment-button kite-quiz-back";
        back.textContent = "Back to quiz list";
        back.addEventListener("click", function () { showQuizDashboard(doc); });
        panel.appendChild(back);
        var intro = doc.createElement("div");
        intro.className = "kite-quiz-intro";
        intro.textContent = state.quiz.module_title + " · " + state.quiz.difficulty +
          " · Pass mark " + state.quiz.pass_threshold + "%";
        panel.appendChild(intro);
        var kind = doc.createElement("span");
        kind.className = "kite-quiz-type";
        kind.textContent = quizQuestionTypeLabel(question.type);
        panel.appendChild(kind);
        var heading = doc.createElement("h2");
        heading.id = "qt";
        heading.textContent = question.question;
        panel.appendChild(heading);
        if (question.code) {
          var code = doc.createElement("pre");
          code.className = "kite-quiz-code";
          code.setAttribute("aria-label", "Python code example");
          code.textContent = question.code;
          panel.appendChild(code);
        }
        var choices = doc.createElement("div");
        choices.setAttribute("role", "radiogroup");
        choices.setAttribute("aria-labelledby", "qt");
        question.options.forEach(function (optionText, index) {
          var option = doc.createElement("button");
          option.type = "button";
          option.className = "opt";
          option.setAttribute("role", "radio");
          option.setAttribute("aria-checked", String(state.answers[state.index] === index));
          option.dataset.k = String(index);
          var radio = doc.createElement("span");
          radio.className = "rd";
          var label = doc.createElement("span");
          label.textContent = optionText;
          option.append(radio, label);
          choices.appendChild(option);
        });
        panel.appendChild(choices);
        if (state.index < state.questions.length - 1 && !Number.isInteger(state.answers[state.index])) {
          var reminder = doc.createElement("p");
          reminder.className = "kite-quiz-validation";
          reminder.textContent = "Choose an answer before continuing. Your work is saved on this device.";
          panel.appendChild(reminder);
        }
        var count = doc.getElementById("cnt");
        if (count) count.textContent = (state.index + 1) + " / " + state.questions.length;
        var bar = doc.getElementById("bar");
        if (bar) bar.style.width = Math.round((state.index + 1) / state.questions.length * 100) + "%";
        var progress = doc.getElementById("pb");
        if (progress) {
          progress.setAttribute("aria-valuenow", String(state.index + 1));
          progress.setAttribute("aria-valuemax", String(state.questions.length));
        }
        ensureQuizNavigation(doc, state);
        doc.documentElement.kiteQuizDashboard = false;
      }
      function renderQuizResult(doc, state, result) {
        var panel = doc.getElementById("sc");
        if (!panel) return;
        panel.textContent = "";
        var heading = doc.createElement("h2");
        heading.textContent = result.score + " / " + result.total + " correct · " + result.percentage + "%";
        panel.appendChild(heading);
        var status = doc.createElement("p");
        status.className = result.passed ? "kite-quiz-result-pass" : "kite-quiz-result-fail";
        status.textContent = result.passed ? "Passed — you met the " + result.pass_threshold + "% pass mark." :
          "Not passed yet — review the lessons below and try again.";
        panel.appendChild(status);
        var summary = doc.createElement("p");
        summary.textContent = result.guest ?
          "This guest result is saved in this browser on this device." :
          "Attempt " + result.attempt_number + " is saved to your learning progress." +
            (result.xp_awarded ? " You earned " + result.xp_awarded + " XP." : "");
        panel.appendChild(summary);
        (result.feedback || []).forEach(function (item, index) {
          var section = doc.createElement("section");
          section.className = "kite-quiz-feedback";
          var question = doc.createElement("h3");
          question.textContent = "Question " + (index + 1) + " · " +
            (item.correct ? "Correct" : "Review");
          var prompt = doc.createElement("p");
          prompt.textContent = item.question;
          var selected = doc.createElement("p");
          selected.textContent = "Your answer: " + (item.your_answer === null ? "No answer" : item.your_answer);
          var correct = doc.createElement("p");
          correct.textContent = "Correct answer: " + item.correct_option;
          var explanation = doc.createElement("p");
          explanation.textContent = item.explanation;
          section.append(question, prompt, selected, correct, explanation);
          panel.appendChild(section);
        });
        var recommendations = doc.createElement("section");
        recommendations.className = "kite-assessment-section";
        var recommendationHeading = doc.createElement("h3");
        recommendationHeading.textContent = "Recommended revision";
        recommendations.appendChild(recommendationHeading);
        var lessonList = doc.createElement("ul");
        var ids = result.review_lesson_ids || state.quiz.lesson_ids || [];
        var module = (doc.documentElement.kiteModules || []).find(function (item) {
          return item.id === state.quiz.module_id;
        });
        ids.forEach(function (id) {
          var lesson = module && (module.lessons || []).find(function (item) { return item.id === id; });
          if (!lesson) return;
          var row = doc.createElement("li");
          if (lesson.locked) {
            row.textContent = lesson.title + " · Premium";
          } else {
            var link = doc.createElement("button");
            link.type = "button";
            link.className = "kite-assessment-button";
            link.textContent = "Review: " + lesson.title;
            link.addEventListener("click", function () {
              sessionStorage.setItem("kite.lessonId", lesson.id);
              navigate("lesson");
            });
            row.appendChild(link);
          }
          lessonList.appendChild(row);
        });
        if (lessonList.childNodes.length) recommendations.appendChild(lessonList);
        panel.appendChild(recommendations);
        var actions = doc.createElement("div");
        actions.className = "kite-assessment-actions";
        actions.appendChild(makeQuizButton(doc, "Try again", function () {
          sessionStorage.removeItem(completedQuizResultKey);
          removeQuizDraft(state.id);
          hydrateQuiz(doc, state.id, true, true);
        }, true, false));
        actions.appendChild(makeQuizButton(doc, "Back to quizzes", function () {
          showQuizDashboard(doc);
        }, false, false));
        if (result.practice_challenge_id) {
          actions.appendChild(makeQuizButton(doc,
            result.guest ? "Coding practice · sign in to run" : "Open coding practice", function () {
            sessionStorage.setItem("kite.challengeId", result.practice_challenge_id);
            navigate("challenge");
          }, false, false));
        }
        panel.appendChild(actions);
        var footer = doc.getElementById("ft");
        if (footer) footer.style.display = "none";
        var progress = doc.getElementById("pb");
        if (progress) progress.style.display = "none";
        var count = doc.getElementById("cnt");
        if (count) count.textContent = result.score + " / " + result.total;
      }
      function renderQuizQuestionOrResult(doc, state) {
        var saved = null;
        try {
          saved = JSON.parse(sessionStorage.getItem(completedQuizResultKey) || "null");
        } catch (error) {
          console.error("Could not restore the completed quiz result.", error);
          sessionStorage.removeItem(completedQuizResultKey);
        }
        if (saved && saved.id === state.id && saved.result) renderQuizResult(doc, state, saved.result);
        else renderQuizQuestion(doc, state);
      }
      function hydrateQuiz(doc, selectedId, forceRunner, resetDraft) {
        api("/api/quizzes").then(function (catalog) {
          var requested = selectedId || sessionStorage.getItem("kite.quizId") || "python-basics";
          var selected = catalog.find(function (item) { return item.id === requested; });
          if (!selected) selected = catalog.find(function (item) { return item.id === "python-basics"; }) ||
            catalog.find(function (item) { return !item.locked; });
          if (!selected) throw new Error("No quizzes are currently available.");
          doc.documentElement.kiteQuizCatalog = catalog;
          var runQuiz = forceRunner || sessionStorage.getItem(quizEntryModeKey) === "runner";
          if (!runQuiz) {
            hydrateQuizDashboard(doc);
            return;
          }
          if (selected.locked) throw new Error("This quiz is part of Kite Premium.");
          var sessionDraftMatches = !resetDraft &&
            sessionStorage.getItem(pendingQuizDraftIdKey) === selected.id;
          var sessionDraftAnswers = sessionDraftMatches ? sessionStorage.getItem(pendingQuizAnswersKey) : null;
          var sessionDraftIndex = sessionDraftMatches ? sessionStorage.getItem(pendingQuizIndexKey) : null;
          addContentPicker(doc, "quiz", catalog, selected.id, function (nextId) {
            sessionStorage.setItem("kite.quizId", nextId);
            sessionStorage.setItem(pendingQuizDraftIdKey, nextId);
            sessionStorage.removeItem(pendingQuizSubmissionIdKey);
            sessionStorage.removeItem(pendingQuizAnswersKey);
            sessionStorage.removeItem(pendingQuizIndexKey);
            sessionStorage.removeItem(completedQuizResultKey);
            hydrateQuiz(doc, nextId, true);
          });
          if (resetDraft) {
            removeQuizDraft(selected.id);
            sessionStorage.removeItem(pendingQuizAnswersKey);
            sessionStorage.removeItem(pendingQuizIndexKey);
            sessionStorage.removeItem(completedQuizResultKey);
          }
          sessionStorage.setItem("kite.quizId", selected.id);
          sessionStorage.setItem(quizEntryModeKey, "runner");
          sessionStorage.setItem(pendingQuizDraftIdKey, selected.id);
          setText(doc, "#ttl", selected.title);
          return api("/api/quizzes/" + encodeURIComponent(selected.id)).then(function (quiz) {
            doc.documentElement.kiteModules = doc.documentElement.kiteModules || [];
            return api("/api/modules").then(function (modules) {
              doc.documentElement.kiteModules = modules;
              var saved = [];
              for (var answerIndex = 0; answerIndex < quiz.questions.length; answerIndex++) {
                saved.push(null);
              }
              var drafts = readQuizDrafts();
              var storedDraft = drafts[selected.id];
              var storedAnswers = storedDraft && storedDraft.answers;
              if (storedDraft && storedDraft.submission_id &&
                  sessionStorage.getItem(pendingQuizSubmissionIdKey) !== storedDraft.submission_id) {
                sessionStorage.setItem(pendingQuizSubmissionIdKey, storedDraft.submission_id);
              }
              if (!Array.isArray(storedAnswers) && sessionDraftAnswers) {
                try {
                  storedAnswers = JSON.parse(sessionDraftAnswers);
                } catch (error) {
                  console.error("Could not restore saved quiz answers.", error);
                }
              }
              if (Array.isArray(storedAnswers) && storedAnswers.length === quiz.questions.length &&
                  storedAnswers.every(function (answer, index) {
                    return answer === null || (Number.isInteger(answer) &&
                      answer >= 0 && answer < quiz.questions[index].options.length);
                  })) saved = storedAnswers;
              else if (storedAnswers) {
                console.warn("An unfinished quiz draft no longer matches the current assessment.");
                removeQuizDraft(selected.id);
              }
              var savedIndex = Number(storedDraft ? storedDraft.index : sessionDraftIndex);
              if (!Number.isInteger(savedIndex) || savedIndex < 0 || savedIndex >= quiz.questions.length) savedIndex = 0;
              var state = {
                id: quiz.id, title: quiz.title, questions: quiz.questions, answers: saved,
                index: savedIndex, quiz: quiz
              };
              doc.documentElement.kiteQuizState = state;
              renderQuizQuestionOrResult(doc, state);
            });
          });
        }).catch(function (error) {
          tell(doc, error.message, true);
          if (error.message.indexOf("Kite Premium") >= 0) navigate("preferences");
        });
      }
      function hydrateChallenge(doc, selectedId) {
        api("/api/challenges").then(function (catalog) {
          var requested = selectedId || sessionStorage.getItem("kite.challengeId") || "hello-world";
          var selected = catalog.find(function (item) { return item.id === requested && !item.locked; }) ||
            catalog.find(function (item) { return item.id === "hello-world"; }) ||
            catalog.find(function (item) { return !item.locked; });
          if (!selected) throw new Error("No coding challenges are currently available.");
          doc.documentElement.kiteChallengeCatalog = catalog;
          addContentPicker(doc, "challenge", catalog, selected.id, function (nextId) {
            sessionStorage.setItem("kite.challengeId", nextId);
            hydrateChallenge(doc, nextId);
          });
          sessionStorage.setItem("kite.challengeId", selected.id);
          doc.documentElement.kiteChallenge = selected;
          setText(doc, "header h1", selected.title);
          setText(doc, "#q", selected.description);
          setText(doc, "#pts", selected.xp + " pts");
          setText(doc, "#tc", "0 / " + selected.test_count);
          var editor = doc.getElementById("ed");
          if (editor) {
            var savedCode = sessionStorage.getItem(pendingChallengeCodeKey);
            editor.value = savedCode !== null ? savedCode : selected.starter_code;
            if (savedCode !== null) sessionStorage.removeItem(pendingChallengeCodeKey);
          }
          var results = doc.getElementById("res");
          if (results) results.textContent = "";
          var win = doc.getElementById("win");
          if (win) win.classList.remove("on");
          var hint = doc.getElementById("hl");
          if (hint) hint.textContent = selected.hint;
        }).catch(function (error) {
          tell(doc, error.message, true);
          if (error.message.indexOf("Kite Premium") >= 0) navigate("preferences");
        });
      }
      function guestProgressSnapshot(doc, modules) {
        var local = readGuestProgress(doc);
        var lessons = modules.reduce(function (all, module) {
          return all.concat(module.lessons || []);
        }, []);
        var availableLessons = lessons.filter(function (lesson) { return !lesson.locked; });
        var availableIds = new Set(availableLessons.map(function (lesson) { return lesson.id; }));
        var completed = local.completed_lessons.filter(function (id) { return availableIds.has(id); });
        var started = local.started_lessons.filter(function (id) {
          return availableIds.has(id) && completed.indexOf(id) < 0;
        });
        var completedIds = new Set(completed);
        var availableModules = modules.filter(function (module) {
          return module.lessons && module.lessons.length &&
            module.lessons.every(function (lesson) { return !lesson.locked; });
        });
        var completedModules = availableModules.filter(function (module) {
          return module.lessons.every(function (lesson) { return completedIds.has(lesson.id); });
        }).length;
        var savedQuizAttempts = readGuestQuizAttempts();
        var availableQuizzes = modules.reduce(function (all, module) {
          return all.concat((module.quizzes || []).filter(function (quiz) { return !quiz.locked; }));
        }, []);
        var quizHistories = {};
        availableQuizzes.forEach(function (quiz) {
          quizHistories[quiz.id] = Array.isArray(savedQuizAttempts[quiz.id]) ? savedQuizAttempts[quiz.id] : [];
        });
        var quizCount = Object.keys(quizHistories).filter(function (id) {
          return quizHistories[id].length > 0;
        }).length;
        var quizAttemptCount = Object.keys(quizHistories).reduce(function (total, id) {
          return total + quizHistories[id].length;
        }, 0);
        var quizBestScores = Object.keys(quizHistories).map(function (id) {
          return quizHistories[id].reduce(function (best, attempt) {
            return best === null || attempt.percentage > best ? attempt.percentage : best;
          }, null);
        }).filter(function (score) { return Number.isFinite(score); });
        var quizRecommendations = availableQuizzes.map(function (quiz) {
          var attempts = quizHistories[quiz.id] || [];
          var best = attempts.reduce(function (score, attempt) {
            return score === null || attempt.percentage > score ? attempt.percentage : score;
          }, null);
          return { id: quiz.id, title: quiz.title, module_id: quiz.module_id,
            module_title: quiz.module_title, percentage: best };
        }).filter(function (quiz) {
          return quiz.percentage === null || quiz.percentage < 80;
        }).sort(function (left, right) {
          if (left.percentage === null && right.percentage !== null) return 1;
          if (right.percentage === null && left.percentage !== null) return -1;
          return (left.percentage || 0) - (right.percentage || 0);
        }).slice(0, 5);
        var quizMasterEarned = Object.keys(quizHistories).some(function (id) {
          return quizHistories[id].some(function (attempt) { return attempt.percentage >= 80; });
        });
        var achievements = [
          { id: "first-lesson", name: "First Lesson", description: "Complete a lesson.", earned: completed.length > 0 },
          { id: "quiz-master", name: "Quiz Master", description: "Score at least 80% on a quiz.", earned: quizMasterEarned },
          { id: "coding-star", name: "Coding Star", description: "Pass a coding challenge.", earned: false },
          { id: "python-pro", name: "Python Pro", description: "Complete every published lesson and challenge.", earned: false },
          { id: "12-day-streak", name: "12-Day Streak", description: "Maintain a 12-day learning streak.", earned: false },
          { id: "bug-hunter", name: "Bug Hunter", description: "Fix ten Playground errors.", earned: false },
          { id: "top-10", name: "Top 10", description: "Rank in the top ten for weekly XP.", earned: false },
          { id: "certified", name: "Certified", description: "Pass the comprehensive final assessment.", earned: false }
        ];
        var earned = achievements.filter(function (item) { return item.earned; })
          .map(function (item) { return item.name; });
        var totalQuizzes = availableQuizzes.length;
        var totalChallenges = modules.reduce(function (total, module) {
          return total + (module.challenges || []).filter(function (challenge) {
            return !challenge.locked;
          }).length;
        }, 0);
        return {
          guest: true,
          xp: local.xp,
          completed_lessons: completed,
          started_lessons: started,
          total_lessons: availableLessons.length,
          completion_percent: availableLessons.length
            ? Math.round(completed.length / availableLessons.length * 100) : 0,
          completed_modules: completedModules,
          total_modules: availableModules.length,
          quiz_count: quizCount,
          total_quizzes: totalQuizzes,
          quiz_attempt_count: quizAttemptCount,
          average_quiz_score: quizBestScores.length
            ? Math.round(quizBestScores.reduce(function (sum, score) { return sum + score; }, 0) / quizBestScores.length)
            : null,
          quiz_recommendations: quizRecommendations,
          challenge_count: 0,
          total_challenges: totalChallenges,
          active_days_7: null,
          streak: null,
          best_streak: null,
          weekly_activity: null,
          achievements: earned,
          achievement_catalog: achievements,
          achievement_count: earned.length,
          total_achievements: achievements.length
        };
      }

      function hydrateProgress(doc, me) {
        var dataRequest = me
          ? Promise.all([api("/api/progress"), api("/api/modules")]).then(function (values) {
            var data = values[0];
            var modules = values[1];
            data.completed_modules = modules.filter(function (module) {
              return module.lessons && module.lessons.length &&
                module.lessons.every(function (lesson) { return lesson.completed; });
            }).length;
            data.total_modules = modules.length;
            return data;
          }).catch(function (error) {
            if (error.status !== 401) throw error;
            clearAuthTokens();
            routeUser = null;
            hydrateShellUser(null);
            return api("/api/modules").then(function (modules) {
              return guestProgressSnapshot(doc, modules);
            });
          })
          : api("/api/modules").then(function (modules) { return guestProgressSnapshot(doc, modules); });
        dataRequest.then(function (data) {
          var percent = Number(data.completion_percent) || 0;
          var catalog = Array.isArray(data.achievement_catalog) ? data.achievement_catalog : [];
          var names = catalog.map(function (item) { return item.name; });
          var earned = new Set(data.achievements);
          doc.documentElement.dataset.kiteAchievements = JSON.stringify(data.achievements);
          function applyProgress() {
            setText(doc, "#xp", String(data.xp));
            setText(doc, "#pc", percent + "%");
            var progressLabel = doc.querySelector("#pc") &&
              doc.querySelector("#pc").parentElement.querySelector("small");
            if (progressLabel) progressLabel.textContent = data.guest
              ? "Accessible Progress" : "Overall Progress";
            var arc = doc.getElementById("arc");
            if (arc) arc.style.strokeDashoffset = String(314.16 * (1 - percent / 100));
            var skillLabel = doc.querySelector(".stk .cardx small");
            var skillValue = doc.querySelector(".stk .cardx b");
            if (skillLabel && skillValue) {
              skillLabel.textContent = data.guest ? "Accessible modules completed" : "Modules completed";
              skillValue.textContent = data.completed_modules + " / " + data.total_modules;
            }
            Array.prototype.forEach.call(doc.querySelectorAll("[data-b]"), function (badge) {
              var name = names[Number(badge.getAttribute("data-b"))];
              var on = earned.has(name);
              badge.style.opacity = on ? "1" : "0.5";
              badge.setAttribute("aria-label", name + (on ? ", earned" : ", locked"));
            });
            var summary = doc.querySelector(".lead");
            if (summary) summary.textContent = data.guest
              ? data.achievement_count + " of " + data.total_achievements +
                " badges earned from progress saved on this device."
              : data.achievement_count + " of " + data.total_achievements +
                " badges earned. Tap a badge to see how to earn it.";
            var guestNote = doc.getElementById("kite-guest-progress-note");
            if (data.guest && summary && !guestNote) {
              guestNote = doc.createElement("p");
              guestNote.id = "kite-guest-progress-note";
              guestNote.className = "sub";
              guestNote.textContent = "Guest lesson and quiz progress is stored on this device. XP reflects new lesson completions; challenge and streak history require sign-in.";
              summary.parentNode.insertBefore(guestNote, summary.nextSibling);
            } else if (!data.guest && guestNote) {
              guestNote.remove();
            }
            var stats = doc.querySelectorAll(".tiles .cardx");
            var values = [
              data.completed_lessons.length + " / " + data.total_lessons,
              data.quiz_count + " / " + data.total_quizzes,
              data.challenge_count + " / " + data.total_challenges,
              data.guest ? "Not tracked" : data.active_days_7 + " / 7 days",
              data.guest ? "Not tracked" : data.streak + " days",
              data.guest ? "Not tracked" : data.best_streak + " days"
            ];
            var labels = data.guest
              ? ["Accessible lessons", "Quizzes attempted", "Available challenges"]
              : ["Lessons completed", "Quizzes completed", "Challenges solved"];
            Array.prototype.forEach.call(stats, function (tile, index) {
              var value = tile.querySelector("b");
              if (value && values[index] !== undefined) value.textContent = String(values[index]);
              var label = tile.querySelector("small");
              if (label && labels[index]) label.textContent = labels[index];
            });
            var chart = doc.querySelector(".chart");
            if (chart && data.guest) {
              chart.textContent = "";
              chart.style.minHeight = "120px";
              chart.style.display = "flex";
              chart.style.alignItems = "center";
              chart.style.justifyContent = "center";
              chart.style.padding = "0 12px";
              chart.style.color = "var(--mut)";
              chart.style.textAlign = "center";
              chart.style.fontSize = "12px";
              chart.textContent = "Daily XP history is not recorded for guest activity.";
              chart.setAttribute("aria-label", "Daily XP history is not recorded for guest activity");
              setText(doc, ".sh .mut", "Guest XP history");
            } else if (chart && Array.isArray(data.weekly_activity)) {
              ["minHeight", "display", "alignItems", "justifyContent", "padding", "color",
                "textAlign", "fontSize"].forEach(function (property) {
                chart.style[property] = "";
              });
              var maxXp = data.weekly_activity.reduce(function (max, day) {
                return Math.max(max, day.xp);
              }, 0);
              chart.textContent = "";
              chart.setAttribute("aria-label", "XP earned each day during the last seven days");
              data.weekly_activity.forEach(function (day) {
                var column = doc.createElement("div");
                if (maxXp && day.xp === maxXp) column.className = "hl";
                var bar = doc.createElement("i");
                bar.setAttribute("role", "img");
                bar.setAttribute("aria-label", day.date + ": " + day.xp + " XP");
                bar.style.height = (maxXp ? Math.round(day.xp / maxXp * 100) : 0) + "%";
                column.appendChild(bar);
                column.appendChild(doc.createTextNode(day.weekday.slice(0, 1)));
                chart.appendChild(column);
              });
              setText(doc, ".sh .mut", "XP per day");
            }
            ensureQuizStyles(doc);
            var oldAssessmentSummary = doc.getElementById("kite-progress-assessments");
            if (oldAssessmentSummary) oldAssessmentSummary.remove();
            var assessmentSummary = doc.createElement("section");
            assessmentSummary.id = "kite-progress-assessments";
            assessmentSummary.className = "kite-assessment-section";
            var assessmentTitle = doc.createElement("h2");
            assessmentTitle.textContent = "Quiz performance";
            var assessmentText = doc.createElement("p");
            assessmentText.className = "kite-assessment-empty";
            assessmentText.textContent = data.quiz_count + " of " + data.total_quizzes +
              " accessible quizzes completed · " + (data.quiz_attempt_count || 0) +
              " recorded attempts · average best score " +
              (data.average_quiz_score === null || data.average_quiz_score === undefined
                ? "not available yet" : data.average_quiz_score + "%") + ".";
            assessmentSummary.append(assessmentTitle, assessmentText);
            var quizRecommendations = data.quiz_recommendations || [];
            if (quizRecommendations.length) {
              var recommendationHeading = doc.createElement("h3");
              recommendationHeading.textContent = "Topics to study next";
              assessmentSummary.appendChild(recommendationHeading);
              var recommendationList = doc.createElement("div");
              recommendationList.className = "kite-assessment-grid";
              quizRecommendations.forEach(function (item) {
                var button = makeQuizButton(doc,
                  item.title + (item.percentage === null || item.percentage === undefined
                    ? " · Not attempted" : " · Best " + item.percentage + "%"),
                  function () {
                    sessionStorage.setItem("kite.quizId", item.id);
                    sessionStorage.setItem(quizEntryModeKey, "runner");
                    sessionStorage.removeItem(pendingQuizAnswersKey);
                    sessionStorage.removeItem(pendingQuizIndexKey);
                    sessionStorage.removeItem(completedQuizResultKey);
                    navigate("quiz");
                  }, false, false);
                recommendationList.appendChild(button);
              });
              assessmentSummary.appendChild(recommendationList);
            } else {
              var noRecommendation = doc.createElement("p");
              noRecommendation.textContent = "No quiz revisions are recommended from your recorded results.";
              assessmentSummary.appendChild(noRecommendation);
            }
            var progressHost = doc.querySelector(".scroll") || doc.querySelector("main") || doc.body;
            progressHost.appendChild(assessmentSummary);
          }
          applyProgress();
          setTimeout(applyProgress, 1200);
        }).catch(function (error) { tell(doc, error.message, true); });
      }
      function hydrateLeaderboard(doc, period) {
        api("/api/leaderboard?period=" + encodeURIComponent(period || "week")).then(function (data) {
          var list = doc.getElementById("ls");
          if (!list) return;
          list.textContent = "";
          data.leaders.forEach(function (entry) {
            var row = doc.createElement("li");
            row.className = "r";
            var rank = doc.createElement("span");
            rank.className = "rk";
            rank.textContent = String(entry.rank);
            var avatar = doc.createElement("span");
            avatar.className = "av";
            avatar.textContent = entry.name.slice(0, 1).toUpperCase();
            var name = doc.createElement("span");
            name.className = "nm";
            name.textContent = entry.name;
            var xp = doc.createElement("span");
            xp.className = "xp";
            xp.textContent = entry.xp.toLocaleString() + " XP";
            row.append(rank, avatar, name, xp);
            list.appendChild(row);
          });
          var current = doc.getElementById("you");
          if (current) current.textContent = "You · Rank " + data.current.rank + " · " + data.current.xp.toLocaleString() + " XP";
        }).catch(function (error) { tell(doc, error.message, true); });
      }
      function renderNotificationItems(doc, items, category, unread, guest) {
        var panel = doc.getElementById("pn");
        if (!panel) return;
        var visible = items.filter(function (item) {
          if (!category || category === "all") return true;
          if (category === "updates") return item.category !== "reminders";
          return item.category === category;
        });
        setText(doc, "#cnt", unread
          ? unread + " unread"
          : (guest ? "No unread guest notifications" : "All caught up"));
        var markRead = doc.getElementById("mr");
        var clear = doc.getElementById("ca");
        if (markRead) {
          markRead.style.display = items.length ? "" : "none";
          markRead.disabled = unread === 0;
        }
        if (clear) {
          clear.style.display = items.length ? "" : "none";
          clear.disabled = items.length === 0;
        }
        var list = doc.createElement("ul");
        visible.forEach(function (item) {
          var li = doc.createElement("li");
          var button = doc.createElement("button");
          button.className = "n" + (item.read ? "" : " un");
          button.dataset.id = (guest ? "guest-" : "kite-") + item.id;
          var icon = doc.createElement("span");
          icon.className = "ic";
          icon.setAttribute("aria-hidden", "true");
          var content = doc.createElement("span");
          content.className = "c";
          var title = doc.createElement("b");
          title.textContent = item.title;
          var body = doc.createElement("span");
          body.className = "bd";
          body.textContent = item.body;
          var time = doc.createElement("span");
          time.className = "tm";
          var createdAt = new Date(item.created_at);
          time.textContent = Number.isNaN(createdAt.getTime()) ? "Time unavailable" : createdAt.toLocaleString();
          content.append(title, body);
          button.append(icon, content, time);
          if (!item.read) {
            var dot = doc.createElement("span");
            dot.className = "dot";
            dot.setAttribute("aria-hidden", "true");
            button.appendChild(dot);
          }
          li.appendChild(button);
          list.appendChild(li);
        });
        panel.textContent = "";
        if (visible.length) panel.appendChild(list);
        else {
          var empty = doc.createElement("div");
          empty.className = "empty";
          var message = doc.createElement("p");
          message.textContent = guest
            ? (items.length ? "No guest notifications in this category." : "Guest activity notifications will appear here on this device.")
            : "You’re all caught up. New Kite updates will appear here.";
          empty.appendChild(message);
          panel.appendChild(empty);
        }
      }
      function hydrateNotifications(doc, category) {
        Promise.all([
          api("/api/notifications"),
          api("/api/notifications/unread-count")
        ]).then(function (values) {
          var items = values[0];
          doc.documentElement.kiteNotices = items;
          renderNotificationItems(doc, items, category, Number(values[1].count) || 0, false);
        }).catch(function (error) { tell(doc, error.message, true); });
      }
      function addNotificationBackButton(doc) {
        var header = doc.querySelector("header.top");
        if (!header || doc.getElementById("kite-notification-back")) return;
        var style = doc.createElement("style");
        style.textContent = ".top{position:relative;min-height:58px;padding-left:58px;padding-right:58px}.kite-notification-back{position:absolute;left:14px;top:10px;width:40px;height:40px;display:grid;place-items:center;border:0;border-radius:12px;background:transparent;color:inherit}.kite-notification-back:active{background:var(--soft)}";
        doc.head.appendChild(style);
        var back = doc.createElement("button");
        back.type = "button";
        back.id = "kite-notification-back";
        back.className = "kite-notification-back";
        back.setAttribute("aria-label", "Back to the previous screen");
        back.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/><path d="M9 12h11"/></svg>';
        back.addEventListener("click", function () {
        if (typeof window.kiteGoBack === "function") window.kiteGoBack();
        else navigate("home");
        });
        header.insertBefore(back, header.firstChild);
      }
      function hydrateGuestNotifications(doc, category) {
        var panel = doc.getElementById("pn");
        if (!panel) return;
        var buttons = doc.querySelectorAll(".tabs [data-k]");
        Array.prototype.forEach.call(buttons, function (button) {
        var activeKey = category === "updates" ? "u" : category === "reminders" ? "r" : "all";
        var selected = button.dataset.k === activeKey;
        button.setAttribute("aria-selected", String(selected));
        button.tabIndex = selected ? 0 : -1;
        });
        var items = readGuestNotifications();
        doc.documentElement.kiteNotices = items;
        renderNotificationItems(
          doc, items, category,
          items.filter(function (item) { return !item.read; }).length,
          true
        );
      }
      function quizQuestionIndex(doc) {
        var match = (doc.getElementById("cnt") || {}).textContent.match(/(\d+)\s*\/\s*(\d+)/);
        return match ? Number(match[1]) - 1 : -1;
      }
      function hydratePreferences(doc) {
        if (!token()) return;
        api("/api/me").then(function (me) {
          setText(doc, "#a-mail .l", me.email);
          var notifications = doc.getElementById("sw");
          if (notifications && me.settings && typeof me.settings.notifications === "boolean") {
            notifications.setAttribute("aria-checked", String(me.settings.notifications));
            saveNotificationPreference(me.settings.notifications);
          }
          var theme = me.settings && me.settings.theme;
          if (theme) {
            try {
              var settings = JSON.parse(parent.localStorage.getItem("kiteset") || "{}");
              settings.theme = theme;
              parent.localStorage.setItem("kiteset", JSON.stringify(settings));
              parent.document.documentElement.setAttribute("data-theme", theme === "system" ? "" : theme);
              if (theme === "system") parent.document.documentElement.removeAttribute("data-theme");
              doc.documentElement.setAttribute("data-theme", theme === "system" ? "" : theme);
              if (theme === "system") doc.documentElement.removeAttribute("data-theme");
            } catch (error) { console.error("Could not apply saved Kite preferences.", error); }
          }
        }).catch(function (error) { tell(doc, error.message, true); });
      }
      function saveNotificationPreference(enabled) {
        try {
          var settings = JSON.parse(parent.localStorage.getItem("kiteset") || "{}");
          settings.notif = enabled;
          parent.localStorage.setItem("kiteset", JSON.stringify(settings));
        } catch (error) { console.error("Could not save Kite notification preferences locally.", error); }
      }
      function hydrateProfilePanel(doc, panelType) {
        api("/api/progress").then(function (data) {
          var sheet = doc.getElementById("sh");
          if (!sheet) return;
          if (panelType === "achievements") {
            var summary = sheet.querySelector(".sub");
            var chips = sheet.querySelector(".chips");
            if (summary) summary.textContent = data.achievement_count + " of " +
              data.total_achievements + " badges earned";
            if (chips) {
              chips.textContent = "";
              data.achievements.forEach(function (name) {
                var chip = doc.createElement("span");
                chip.className = "chip";
                chip.textContent = name;
                chips.appendChild(chip);
              });
            }
            return;
          }
          var list = sheet.querySelector(".act");
          if (!list) return;
          var titles = {
            "python-variables": "Variables",
            "python-types": "Data types",
            "python-conditions": "Make decisions",
            "python-loops": "Loops",
            "python-lists": "Lists",
            "python-functions": "Functions",
            "python-basics": "Python Basics",
            "hello-world": "Hello, World!",
            "sum-variables": "Sum of two numbers",
            "count-to-five": "Count to five"
          };
          var descriptions = { lesson: "Completed lesson", quiz: "Completed quiz", challenge: "Solved challenge" };
          list.textContent = "";
          if (!data.activities.length) {
            var empty = doc.createElement("li");
            empty.textContent = "No learning activity yet.";
            list.appendChild(empty);
            return;
          }
          data.activities.forEach(function (activity) {
            var item = doc.createElement("li");
            var icon = doc.createElement("span");
            icon.className = "dt";
            icon.setAttribute("aria-hidden", "true");
            icon.textContent = (activity.kind || "activity").charAt(0).toUpperCase();
            var details = doc.createElement("div");
            var title = doc.createElement("b");
            title.textContent = (descriptions[activity.kind] || "Learning activity") + " " +
              (titles[activity.item_id] || activity.item_id);
            var date = doc.createElement("small");
            date.textContent = new Date(activity.at).toLocaleDateString();
            details.append(title, date);
            var xp = doc.createElement("span");
            xp.className = "xp";
            xp.textContent = "+" + activity.xp + " XP";
            item.append(icon, details, xp);
            list.appendChild(item);
          });
        }).catch(function (error) { tell(doc, error.message, true); });
      }
  function bindSubscriptionButtons(doc) {
    ["monthly", "annual"].forEach(function (period) {
      doc.getElementById("kite-" + period).addEventListener("click", function () {
        if (!token()) {
          requestSubscriptionSignIn(period);
          return;
        }
        api("/api/plans").then(function (data) {
          var plan = data.plans.find(function (item) { return item.id === "premium_" + period; });
          var billing = window.Capacitor && window.Capacitor.Plugins && window.Capacitor.Plugins.KiteBilling;
          if (!billing || typeof billing.purchase !== "function" || !plan.product_id) {
            throw new Error("Google Play Billing must be configured in the Android build before you can subscribe.");
          }
          return billing.purchase({ productId: plan.product_id }).then(function (purchase) {
            return api("/api/subscription/google/verify", {
              product_id: plan.product_id,
              purchase_token: purchase.purchaseToken
            }).then(function (subscription) {
              sessionStorage.removeItem(pendingPlanKey);
              var state = doc.getElementById("kite-plan-state");
              if (state) state.textContent = subscription.premium ? "Kite Premium" : "Kite Free";
              tell(doc, "Kite Premium is active.", false);
            });
          });
        }).catch(function (error) {
          var note = doc.getElementById("kite-billing-note");
          if (note) note.textContent = error.message;
          else tell(doc, error.message, true);
        });
      });
    });
    doc.getElementById("kite-manage").addEventListener("click", function () {
      api("/api/subscription/refresh", {}, "POST").then(function (data) {
        var state = doc.getElementById("kite-plan-state");
        if (state) state.textContent = data.premium ? "Kite Premium" : "Kite Free";
        if (data.cancel_url) parent.open(data.cancel_url, "_blank", "noopener");
        else tell(doc, "You are on the free plan.", false);
      }).catch(function (error) { tell(doc, error.message, true); });
    });
  }
  function install(doc) {
    if (!doc || doc.documentElement.dataset.kiteBridge) return;
    doc.documentElement.dataset.kiteBridge = "ready";
    var route = (location.hash || "#launch").slice(1);
    if (route.indexOf("access_token=") === 0 || route.indexOf("error=") === 0) route = "launch";
    applyPythonBranding(doc, route);
    if (route === "notifications") addNotificationBackButton(doc);

    doc.defaultView.addEventListener("click", function (event) {
      var target = event.target.closest && event.target.closest("button, a");
      if (!target) return;
      if (route === "launch" && (target.getAttribute("href") === "#sign-up" || target.getAttribute("href") === "#log-in")) {
        event.preventDefault();
        doc.defaultView.location.hash = target.getAttribute("href");
        return;
      }
      if (route === "learn" && target.dataset.kiteLesson) {
        sessionStorage.setItem("kite.lessonId", target.dataset.kiteLesson);
        recordGuestLesson(doc, target.dataset.kiteLesson, false);
      }
      if (route === "home" && target.getAttribute("data-t") && target.getAttribute("data-t").indexOf("Lesson:") === 0) {
        if (!sessionStorage.getItem("kite.lessonId")) sessionStorage.setItem("kite.lessonId", "python-variables");
        recordGuestLesson(doc, sessionStorage.getItem("kite.lessonId"), false);
      }
      if (route === "lesson" && target.id === "next") {
        var activeLesson = sessionStorage.getItem("kite.lessonId") || "python-variables";
        if (token()) {
          api("/api/lessons/" + encodeURIComponent(activeLesson) + "/complete", {}, "POST")
            .catch(function (error) { tell(doc, error.message, true); });
        } else {
          var lessonData = doc.documentElement.kiteLesson;
          recordGuestLesson(doc, activeLesson, true, lessonData && lessonData.xp_reward);
          tell(doc, "Lesson progress is stored on this device only until you sign in.", false);
        }
      }
      if (route === "launch" && target.textContent.trim() === "Start with a free lesson") {
        event.preventDefault();
        event.stopImmediatePropagation();
        sessionStorage.setItem("kite.lessonId", "python-variables");
        navigate("lesson");
      }
      if (route === "notifications" && target.dataset.id) {
        event.stopImmediatePropagation();
        if (target.dataset.id.indexOf("guest-") === 0) {
          var guestNoticeId = target.dataset.id.slice(6);
          var guestItems = readGuestNotifications();
          var guestNotice = guestItems.find(function (item) { return item.id === guestNoticeId; });
          if (guestNotice) {
            guestNotice.read = true;
            writeGuestNotifications(guestItems);
            hydrateGuestNotifications(doc, "all");
            if (knownRoutes.indexOf(guestNotice.target_route) >= 0) navigate(guestNotice.target_route);
          }
          return;
        }
        var noticeId = target.dataset.id.replace(/^kite-/, "");
        var notice = (doc.documentElement.kiteNotices || []).find(function (item) {
          return String(item.id) === noticeId;
        });
        api("/api/notifications/" + encodeURIComponent(noticeId) + "/read", {}, "POST")
          .then(function () {
            hydrateNotifications(doc, "all");
            if (notice && knownRoutes.indexOf(notice.target_route) >= 0) navigate(notice.target_route);
          })
          .catch(function (error) { tell(doc, error.message, true); });
        return;
      }
      if (route === "notifications" && target.dataset.k) {
        event.stopImmediatePropagation();
        var category = target.dataset.k === "u" ? "updates" : target.dataset.k === "r" ? "reminders" : "all";
        if (token()) hydrateNotifications(doc, category);
        else hydrateGuestNotifications(doc, category);
      }
      if (route === "notifications" && target.id === "mr") {
        event.stopImmediatePropagation();
        if (!token()) {
          var guestItemsToRead = readGuestNotifications();
          guestItemsToRead.forEach(function (item) { item.read = true; });
          writeGuestNotifications(guestItemsToRead);
          hydrateGuestNotifications(doc, "all");
        } else {
          api("/api/notifications/read-all", {}, "POST").then(function () { hydrateNotifications(doc, "all"); })
            .catch(function (error) { tell(doc, error.message, true); });
        }
        return;
      }
      if (route === "notifications" && target.id === "ca") {
        event.stopImmediatePropagation();
        if (!token()) {
          writeGuestNotifications([]);
          hydrateGuestNotifications(doc, "all");
        } else {
          api("/api/notifications", null, "DELETE").then(function () { hydrateNotifications(doc, "all"); })
            .catch(function (error) { tell(doc, error.message, true); });
        }
        return;
      }
    }, true);

    doc.defaultView.addEventListener("change", function (event) {
      var target = event.target;
      if (route === "quiz" && target.id === "kite-quiz-select") {
        event.stopImmediatePropagation();
        sessionStorage.setItem("kite.quizId", target.value);
        sessionStorage.setItem(quizEntryModeKey, "runner");
        sessionStorage.setItem(pendingQuizDraftIdKey, target.value);
        sessionStorage.removeItem(pendingQuizSubmissionIdKey);
        sessionStorage.removeItem(pendingQuizAnswersKey);
        sessionStorage.removeItem(pendingQuizIndexKey);
        sessionStorage.removeItem(completedQuizResultKey);
        hydrateQuiz(doc, target.value, true, true);
      }
      if (route === "challenge" && target.id === "kite-challenge-select") {
        event.stopImmediatePropagation();
        sessionStorage.setItem("kite.challengeId", target.value);
        sessionStorage.removeItem(pendingChallengeCodeKey);
        hydrateChallenge(doc, target.value);
      }
    }, true);

    doc.addEventListener("submit", function (event) {
      var form = event.target;
      if (form.matches("form[data-kite-reset]")) return;
      if (form.id === "ef") {
        if (!form.checkValidity() || form.elements.f1.value.trim().length < 2 || !form.elements.f2.value.trim()) return;
        if (!token()) {
          event.preventDefault();
          event.stopImmediatePropagation();
          signInForRoute(route);
          return;
        }
        api("/api/me", { name: form.elements.f1.value.trim() }, "PATCH")
          .catch(function (error) { tell(doc, error.message, true); });
        return;
      }
      if (route === "preferences" && form.elements.e1 && form.elements.e2) {
        if (!form.checkValidity()) return;
        if (!token()) {
          event.preventDefault();
          event.stopImmediatePropagation();
          signInForRoute("preferences");
          return;
        }
        api("/api/me/email", {
          email: form.elements.e1.value.trim(),
          password: form.elements.e2.value
        }, "PATCH").then(function (result) {
          if (result.email) {
            setText(doc, "#a-mail .l", result.email);
            var overlay = doc.getElementById("ov");
            if (overlay) overlay.classList.remove("on");
            tell(doc, "Email address updated.", false);
          } else if (result.message) tell(doc, result.message, false);
        }).catch(function (error) { tell(doc, error.message, true); });
        return;
      }
      if (route === "preferences" && form.elements.p0 && form.elements.p1) {
        if (!form.checkValidity() || (form.elements.p2 && form.elements.p1.value !== form.elements.p2.value)) return;
        if (!token()) {
          event.preventDefault();
          event.stopImmediatePropagation();
          signInForRoute("preferences");
          return;
        }
        api("/api/me/password", {
          current_password: form.elements.p0.value,
          new_password: form.elements.p1.value
        }, "PATCH").then(function () {
          tell(doc, "Password updated.", false);
        }).catch(function (error) { tell(doc, error.message, true); });
        return;
      }
      if (!form.matches("form[data-f]")) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (busy) return;
      var kind = form.getAttribute("data-f");
      if (kind === "up") enhanceRegistrationForm(doc);
      var fields = form.elements;
      var error = doc.getElementById("err");
      if (error) error.textContent = "";
      var operation;
      var ownsAuthDestination = false;
      if (kind === "up") {
        var fullName = fields.nm.value.trim();
        var email = fields.em.value.trim();
        fields.em.value = email;
        var password = fields.pw.value;
        var confirmation = fields.cpw && fields.cpw.value;
        var validationMessage = "";
        if (!fullName) validationMessage = "Enter your full name.";
        else if (!fields.em.checkValidity()) validationMessage = "Enter a valid email address.";
        else if (password.length < 8) validationMessage = "Password must be at least 8 characters.";
        else if (password.length > 128) validationMessage = "Password must be 128 characters or fewer.";
        else if (passwordStrength(password).weak) validationMessage = "Choose a stronger password with a mix of character types.";
        else if (!confirmation) validationMessage = "Confirm your password.";
        else if (password !== confirmation) validationMessage = "Passwords do not match.";
        if (validationMessage) {
          tell(doc, validationMessage, true);
          return;
        }
        if (!sessionStorage.getItem(authDestinationKey)) {
          sessionStorage.setItem(authDestinationKey, "home");
          ownsAuthDestination = true;
        }
        operation = api("/api/auth/register", { name: fullName, email: email, password: password });
      }
      else if (kind === "in") {
        var loginEmail = fields.em.value.trim();
        if (!loginEmail) {
          tell(doc, "Enter your email address.", true);
          fields.em.focus();
          return;
        }
        if (!fields.em.checkValidity()) {
          tell(doc, "Enter a valid email address.", true);
          fields.em.focus();
          return;
        }
        if (!fields.pw.value) {
          tell(doc, "Enter your password.", true);
          fields.pw.focus();
          return;
        }
        operation = api("/api/auth/login", { email: loginEmail, password: fields.pw.value });
      }
      else if (kind === "rs") {
        if (!fields.em.value.trim()) {
          tell(doc, "Enter the email address for your account.", true);
          fields.em.focus();
          return;
        }
        if (!fields.em.checkValidity()) {
          tell(doc, "Enter a valid email address.", true);
          fields.em.focus();
          return;
        }
        operation = api("/api/auth/password-reset", { email: fields.em.value.trim() });
      }
      else return;
      var submit = form.querySelector('button[type="submit"]');
      var submitLabel = submit ? submit.textContent : "";
      busy = true;
      if (submit) {
        submit.disabled = true;
        submit.textContent = kind === "rs" ? "Sending email…" :
          kind === "up" ? "Creating account…" : "Signing in…";
      }
      form.setAttribute("aria-busy", "true");
      operation.then(function (data) {
        if (data.access_token) {
          completeAuthentication(doc, data, kind === "up" ? "home" : undefined);
        } else if (kind === "up") {
          sessionStorage.setItem("kite.pendingVerificationEmail", fields.em.value.trim());
          showVerificationPending(doc, fields.em.value.trim());
        } else tell(doc, data.message || "If that address has an account, you will receive instructions shortly.", false);
      }).catch(function (error) {
        if (ownsAuthDestination) sessionStorage.removeItem(authDestinationKey);
        tell(doc, friendlyAuthMessage(error, kind), true);
      })
        .finally(function () {
          busy = false;
          if (submit && submit.isConnected) {
            submit.disabled = false;
            submit.textContent = submitLabel;
            form.removeAttribute("aria-busy");
          }
        });
    }, true);

    doc.addEventListener("click", function (event) {
      var target = event.target.closest("button, a");
      if (!target) return;
      if (target.hasAttribute("data-kite-resend-verification")) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (busy) return;
        var verification = doc.documentElement.kiteVerificationView;
        if (!verification) return;
        var resendButton = target;
        var resendLabel = resendButton.textContent;
        busy = true;
        resendButton.disabled = true;
        resendButton.textContent = "Sending email…";
        api("/api/auth/resend-verification", { email: verification.email })
          .then(function (data) { tell(doc, data.message, false); })
          .catch(function (error) { tell(doc, friendlyAuthMessage(error, "verify"), true); })
          .finally(function () {
            busy = false;
            if (resendButton.isConnected) {
              resendButton.disabled = false;
              resendButton.textContent = resendLabel;
            }
          });
        return;
      }
      if (target.hasAttribute("data-kite-change-registration")) {
        event.preventDefault();
        event.stopImmediatePropagation();
        restoreRegistrationView(doc);
        return;
      }
      if (target.hasAttribute("data-kite-back-sign-in")) {
        event.preventDefault();
        event.stopImmediatePropagation();
        var pendingEmail = doc.documentElement.kiteVerificationView &&
          doc.documentElement.kiteVerificationView.email;
        restoreRegistrationView(doc);
        var fillLoginEmail = function () {
          var emailInput = doc.querySelector('form[data-f="in"] input[name="em"]');
          if (emailInput) {
            emailInput.value = pendingEmail;
            emailInput.focus();
          }
        };
        if (pendingEmail) doc.defaultView.addEventListener("hashchange", fillLoginEmail, { once: true });
        doc.defaultView.location.hash = "#log-in";
        if (pendingEmail) setTimeout(fillLoginEmail, 50);
        return;
      }
      if (target.hasAttribute("data-kite-import-guest")) {
        event.preventDefault();
        event.stopImmediatePropagation();
        importGuestProgress(doc, target, route);
        return;
      }
      if (doc.defaultView.parent.location.hash === "#about" && (target.id === "b-terms" || target.id === "b-priv" || target.id === "b-con")) {
        event.preventDefault();
        event.stopImmediatePropagation();
        renderLegalPage(doc, target.id, target);
        return;
      }
      if (target.getAttribute("data-a") === "google") {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (busy) return;
        var googleButton = target;
        var googleText = googleButton.querySelector("span:not(.ic)");
        var googleLabel = googleText ? googleText.textContent : "";
        busy = true;
        googleButton.disabled = true;
        googleButton.setAttribute("aria-busy", "true");
        if (googleText) googleText.textContent = "Connecting…";
        startGoogleSignIn(doc).catch(function (error) {
          tell(doc, friendlyAuthMessage(error, "google"), true);
        }).finally(function () {
          busy = false;
          if (googleButton.isConnected) {
            googleButton.disabled = false;
            googleButton.removeAttribute("aria-busy");
            if (googleText) googleText.textContent = googleLabel;
          }
        });
        return;
      }
      if (target.getAttribute("data-a") === "out") {
        event.preventDefault();
        event.stopImmediatePropagation();
        logOut(doc);
        return;
      }
      if (route === "profile" && (target.id === "lo" || target.id === "m-out")) {
        event.preventDefault();
        event.stopImmediatePropagation();
        logOut(doc);
        return;
      }
      if (route === "profile" && target.id === "m-edit") {
        setTimeout(function () {
          var input = doc.getElementById("f1");
          var name = doc.documentElement.dataset.kiteName;
          if (input && name) input.value = name;
        }, 0);
      }
      if (route === "profile" && target.id === "m-act") {
        setTimeout(function () { hydrateProfilePanel(doc, "activity"); }, 0);
      }
      if (route === "profile" && target.id === "m-ach") {
        setTimeout(function () { hydrateProfilePanel(doc, "achievements"); }, 0);
        return;
      }
      if (route === "preferences" && target.dataset.v && token()) {
        api("/api/settings", { theme: target.dataset.v }, "PATCH").catch(function (error) { tell(doc, error.message, true); });
      }
      if (route === "playground" && target.id === "run") {
        var editor = doc.getElementById("code");
        var output = doc.getElementById("out");
        if (!editor || !output) return;
        if (!token()) {
          recordGuestPlaygroundRun(doc);
          if (!sessionStorage.getItem("kite.guestPlaygroundNotice")) {
            sessionStorage.setItem("kite.guestPlaygroundNotice", "shown");
            tell(doc, "Guest mode runs code on this device; it is not synced to your Kite account.", false);
          }
          return;
        }
        event.preventDefault();
        event.stopImmediatePropagation();
        if (activePlaygroundRun) return;
        var executionId;
        try { executionId = createPlaygroundExecutionId(doc.defaultView); }
        catch (error) {
          tell(doc, error.message, true);
          return;
        }
        var runState = { id: executionId, doc: doc, cancelled: false };
        activePlaygroundRun = runState;
        var stopButton = doc.getElementById("kite-stop");
        if (stopButton) {
          stopButton.hidden = false;
          stopButton.style.display = "flex";
          stopButton.disabled = false;
          stopButton.textContent = "Stop";
        }
        target.hidden = true;
        target.style.display = "none";
        target.disabled = true;
        output.textContent = "Running…";
        sessionStorage.setItem(playgroundOutputKey, "Running…");
        api("/api/execution/run", { code: editor.value, execution_id: executionId }).then(function (result) {
          if (runState.cancelled) output.textContent = "Execution stopped.";
          else output.textContent = [result.stdout, result.stderr, result.error].filter(Boolean).join("\n");
          sessionStorage.setItem(playgroundOutputKey, output.textContent);
        }).catch(function (error) {
          if (error.status === 401) {
            sessionStorage.setItem(pendingPlaygroundCodeKey, editor.value);
            signInForRoute("playground");
            return;
          }
          output.textContent = error.message;
          sessionStorage.setItem(playgroundOutputKey, output.textContent);
        }).finally(function () {
          if (activePlaygroundRun === runState) activePlaygroundRun = null;
          target.hidden = false;
          target.style.display = "flex";
          target.disabled = false;
          if (stopButton) {
            stopButton.hidden = true;
            stopButton.style.display = "none";
            stopButton.disabled = false;
            stopButton.textContent = "Stop";
          }
        });
      }
      if (route === "playground" && target.id === "reset") {
        var resetEditor = doc.getElementById("code");
        if (resetEditor && resetEditor.value !== resetEditor.defaultValue &&
            !doc.defaultView.confirm("Reset the editor to its starter code?")) {
          event.preventDefault();
          event.stopImmediatePropagation();
          return;
        }
        setTimeout(function () {
          var output = doc.getElementById("out");
          var editor = doc.getElementById("code");
          if (output) {
            output.textContent = "Run code to see output.";
            sessionStorage.setItem(playgroundOutputKey, output.textContent);
          }
          if (editor) editor.dispatchEvent(new doc.defaultView.Event("input", { bubbles: true }));
        }, 0);
      }
      if (route === "challenge" && target.id === "run") {
        event.preventDefault();
        event.stopImmediatePropagation();
        var challenge = doc.documentElement.kiteChallenge;
        var challengeId = challenge ? challenge.id : (sessionStorage.getItem("kite.challengeId") || "hello-world");
        var editor = doc.getElementById("ed");
        var results = doc.getElementById("res");
        if (!editor || !results) return;
        if (!token()) {
          sessionStorage.setItem("kite.challengeId", challengeId);
          sessionStorage.setItem(pendingChallengeCodeKey, editor.value);
          signInForRoute("challenge");
          return;
        }
        results.textContent = "";
        setText(doc, "#tc", "Running tests...");
        api("/api/challenges/" + encodeURIComponent(challengeId) + "/submit", { code: editor.value }).then(function (result) {
          var passed = result.results.filter(function (item) { return item.passed; }).length;
          setText(doc, "#tc", passed + " / " + result.results.length);
          result.results.forEach(function (item, index) {
            var line = doc.createElement("li");
            line.className = item.passed ? "p" : "f";
            line.textContent = "Test " + (index + 1) + ": " + (item.passed ? "passed" : (item.error || "not yet"));
            results.appendChild(line);
          });
          if (result.passed) {
            setText(doc, "#wt", "Challenge complete!");
            setText(doc, "#wp", "+" + result.xp_awarded + " XP");
            setText(doc, "#wtot", "Total: " + result.total_xp + " XP");
            var win = doc.getElementById("win");
            if (win) win.classList.add("on");
          }
        }).catch(function (error) {
          if (error.status === 401) {
            sessionStorage.setItem(pendingChallengeCodeKey, editor.value);
            signInForRoute("challenge");
            return;
          }
          setText(doc, "#tc", error.message);
        });
        return;
      }
      if (route === "quiz" && target.classList.contains("opt")) {
        event.preventDefault();
        event.stopImmediatePropagation();
        var quizState = doc.documentElement.kiteQuizState;
        var choiceIndex = quizState ? quizState.index : quizQuestionIndex(doc);
        if (quizState && choiceIndex >= 0) {
          var selectedAnswer = Number(target.dataset.k);
          if (quizState.answers[choiceIndex] !== selectedAnswer) {
            sessionStorage.removeItem(pendingQuizSubmissionIdKey);
          }
          quizState.answers[choiceIndex] = selectedAnswer;
          sessionStorage.setItem(quizEntryModeKey, "runner");
          saveQuizDraft(quizState, doc);
          renderQuizQuestion(doc, quizState);
        }
        return;
      }
      if (route === "quiz" && target.id === "pv") {
        event.preventDefault();
        event.stopImmediatePropagation();
        var previousQuizState = doc.documentElement.kiteQuizState;
        if (previousQuizState && previousQuizState.index > 0) {
          previousQuizState.index -= 1;
          saveQuizDraft(previousQuizState, doc);
          renderQuizQuestion(doc, previousQuizState);
        }
        return;
      }
      if (route === "quiz" && target.id === "nx") {
        event.preventDefault();
        event.stopImmediatePropagation();
        var quizState = doc.documentElement.kiteQuizState;
        if (!quizState) return;
        if (quizState.index < quizState.questions.length - 1) {
          if (!Number.isInteger(quizState.answers[quizState.index])) return;
          quizState.index += 1;
          saveQuizDraft(quizState, doc);
          renderQuizQuestion(doc, quizState);
          return;
        }
        var unanswered = [];
        var invalidAnswer = quizState.answers.length !== quizState.questions.length;
        quizState.questions.forEach(function (question, index) {
          var answer = quizState.answers[index];
          if (answer === null || answer === undefined) {
            unanswered.push(index + 1);
          } else if (!Number.isInteger(answer) || answer < 0 ||
              answer >= question.options.length) {
            invalidAnswer = true;
          }
        });
        if (invalidAnswer) {
          tell(doc, "Some saved answers are invalid. Review the quiz before submitting.", true);
          return;
        }
        sessionStorage.setItem("kite.quizId", quizState.id);
        sessionStorage.setItem(pendingQuizDraftIdKey, quizState.id);
        sessionStorage.setItem(pendingQuizAnswersKey, JSON.stringify(quizState.answers));
        var win = doc.defaultView;
        var confirmation = unanswered.length
          ? "Questions " + unanswered.join(", ") +
            " are unanswered and will be marked incorrect. Submit anyway?"
          : "Submit this quiz and view your score and explanations?";
        if (win && typeof win.confirm === "function" && !win.confirm(confirmation)) return;
        var submissionId = sessionStorage.getItem(pendingQuizSubmissionIdKey) ||
          newQuizSubmissionId(doc);
        sessionStorage.setItem(pendingQuizSubmissionIdKey, submissionId);
        saveQuizDraft(quizState, doc);
        var submitLabel = target.textContent;
        target.disabled = true;
        target.textContent = "Submitting…";
        target.setAttribute("aria-busy", "true");
        var validation = doc.getElementById("kite-quiz-validation");
        if (validation) validation.textContent = "Submitting your answers…";
        api("/api/quizzes/" + encodeURIComponent(quizState.id) + "/submit", {
          answers: quizState.answers.map(function (answer) {
            return Number.isInteger(answer) ? answer : -1;
          }),
          submission_id: submissionId
        }).then(function (result) {
          if (result.guest) storeGuestQuizAttempt(quizState.id, result);
          removeQuizDraft(quizState.id);
          if (validation) validation.textContent = "";
          target.removeAttribute("aria-busy");
          sessionStorage.setItem(completedQuizResultKey, JSON.stringify({ id: quizState.id, result: result }));
          renderQuizResult(doc, quizState, result);
          tell(doc, result.guest ? "Quiz result saved on this device." :
            "Quiz score saved." + (result.xp_awarded ? " +" + result.xp_awarded + " XP." : ""), false);
        }).catch(function (error) {
          var submitError = error.status ? error.message :
            "A network problem interrupted submission. Check your connection and retry.";
          target.disabled = false;
          target.textContent = submitLabel;
          target.removeAttribute("aria-busy");
          if (validation) {
            validation.textContent = "Your answers were not submitted. " +
              submitError + " You can retry without losing your answers.";
          }
          tell(doc, submitError, true);
        });
        return;
      }
      if (route === "leaderboard" && /^t-[wma]$/.test(target.id)) {
        var period = target.id === "t-m" ? "month" : target.id === "t-a" ? "all" : "week";
        setTimeout(function () { hydrateLeaderboard(doc, period); }, 0);
      }
      if (route === "progress" && target.id === "t-progress") {
        setTimeout(function () { hydrateProgress(doc, routeUser); }, 0);
      }
      if (route === "progress" && target.id === "t-ach") {
        setTimeout(function () { hydrateProgress(doc, routeUser); }, 0);
      }
      if (route === "progress" && target.id === "t-stats") {
        setTimeout(function () { hydrateProgress(doc, routeUser); }, 0);
      }
      if (route === "progress" && target.id === "va") {
        setTimeout(function () { hydrateProgress(doc, routeUser); }, 0);
      }
      if (route === "progress" && target.hasAttribute("data-b")) {
        setTimeout(function () {
          var data = doc.documentElement.dataset.kiteAchievements;
          var earned = data ? JSON.parse(data) : [];
          setText(doc, "#bs", earned.indexOf(doc.getElementById("bn").textContent) >= 0 ? "Earned" : "Locked");
        }, 0);
      }
      if (route === "preferences" && target.id === "sw") {
        event.preventDefault();
        event.stopImmediatePropagation();
        var previous = target.getAttribute("aria-checked") === "true";
        var enabled = !previous;
        target.setAttribute("aria-checked", String(enabled));
        saveNotificationPreference(enabled);
        if (!token()) return;
        api("/api/settings", { notifications: enabled }, "PATCH").catch(function (error) {
          target.setAttribute("aria-checked", String(previous));
          saveNotificationPreference(previous);
          tell(doc, error.message, true);
        });
      }
    }, true);

    function hydrateRouteData(me) {
      routeUser = me;
      hydrateShellUser(me);
      if (route === "preferences") addSubscriptionSection(doc);
      if (route === "preferences") hydratePreferences(doc);
      if (route === "home") hydrateHome(doc, me);
      if (route === "learn") hydrateLearn(doc);
      if (route === "lesson") hydrateLesson(doc);
      if (route === "playground") {
        enhancePlayground(doc);
      }
      if (route === "challenge") hydrateChallenge(doc);
      if (route === "quiz") hydrateQuiz(doc);
      if (route === "progress") hydrateProgress(doc, me);
      if (route === "leaderboard") hydrateLeaderboard(doc, "week");
      if (route === "notifications") {
        if (token()) hydrateNotifications(doc, "all");
        else hydrateGuestNotifications(doc, "all");
      }
      if (route === "profile" && me) {
        var name = doc.getElementById("nm");
        if (name) name.textContent = me.name;
        var avatar = doc.getElementById("av");
        if (avatar) avatar.textContent = me.name.trim().charAt(0).toUpperCase();
        doc.documentElement.dataset.kiteName = me.name;
        var stats = doc.querySelector(".stats");
        if (stats) {
          Array.prototype.forEach.call(stats.children, function (tile) {
            var label = tile.querySelector("small");
            var value = tile.querySelector("b");
            if (!label || !value) return;
            if (label.textContent.trim() === "Level" || label.textContent.trim() === "Lessons") {
              label.textContent = "Lessons";
              value.textContent = "—";
            }
            if (label.textContent.trim() === "XP") value.textContent = Number(me.xp).toLocaleString();
            if (label.textContent.trim() === "Streak") {
              var icon = value.querySelector("svg");
              if (icon) {
                while (value.firstChild && value.firstChild !== icon) value.removeChild(value.firstChild);
                value.insertBefore(doc.createTextNode(String(me.streak) + " "), icon);
              } else value.textContent = String(me.streak);
            }
          });
          api("/api/progress").then(function (progress) {
            Array.prototype.forEach.call(stats.children, function (tile) {
              var label = tile.querySelector("small");
              var value = tile.querySelector("b");
              if (label && value && label.textContent.trim() === "Lessons") {
                value.textContent = progress.completed_lessons.length + " / " + progress.total_lessons;
              }
            });
          }).catch(function (error) { tell(doc, error.message, true); });
        }
        try {
          var profile = JSON.parse(parent.localStorage.getItem("kiteprof") || "{}");
          profile.name = me.name;
          if (!profile.nick) profile.nick = me.name.split(" ")[0];
          parent.localStorage.setItem("kiteprof", JSON.stringify(profile));
        } catch (error) { console.error("Could not synchronize the local Kite profile.", error); }
      }
    }

    if (route === "launch") {
      watchRegistrationForm(doc);
      if (handleAuthCallback(doc)) return;
      if (!resetToken() &&
          (sessionStorage.getItem(authModeKey) === "login" ||
           sessionStorage.getItem(subscriptionReturnKey) === "preferences")) {
        sessionStorage.removeItem(authModeKey);
        doc.defaultView.location.hash = "#log-in";
      }
      var passwordResetCredential = resetToken();
      var callbackType = new URLSearchParams(window.location.hash.slice(1)).get("type");
      if (passwordResetCredential) {
        cleanAuthCallbackUrl();
        showPasswordReset(doc, passwordResetCredential);
      } else if (callbackType === "recovery") {
        cleanAuthCallbackUrl();
        doc.defaultView.location.hash = "#reset";
        setTimeout(function () {
          tell(doc, "This password-reset link is invalid or expired. Request a new one and try again.", true);
        }, 0);
      } else if (token()) {
        api("/api/me").then(function (me) {
          hydrateShellUser(me);
          showSignedIn(doc, me.name);
        }).catch(function (error) {
          if (error.status !== 401) tell(doc, error.message, true);
        });
      }
    }

    if (route === "progress" && token()) {
      api("/api/me").then(function (me) {
        hydrateRouteData(me);
      }).catch(function (error) {
        if (error.status === 401) {
          clearAuthTokens();
          routeUser = null;
          hydrateRouteData(null);
          return;
        }
        tell(doc, error.message, true);
      });
      return;
    }

    if (protectedRoutes[route] || (route === "home" && token())) {
      if (!token()) {
        signInForRoute(route);
        return;
      }
      api("/api/me").then(function (me) {
        hydrateRouteData(me);
      }).catch(function (error) {
        if (error.status === 401) {
          clearAuthTokens();
          signInForRoute(route);
          return;
        }
        tell(doc, error.message, true);
      });
      return;
    }
    if (route !== "launch") hydrateRouteData(null);
    ["b-terms", "b-priv", "b-con"].forEach(function (id) {
      var button = doc.getElementById(id);
      if (!button) return;
      button.onclick = function (event) {
        event.preventDefault();
        event.stopPropagation();
        renderLegalPage(doc, id, button);
      };
    });
  }
  function bind() {
    if (!frame) return;
    frame.addEventListener("load", function () {
      try { install(frame.contentDocument); } catch (error) { console.error("Kite bridge could not attach.", error); }
    });
    try { if (frame.contentDocument && frame.contentDocument.readyState === "complete") install(frame.contentDocument); } catch (error) { console.error("Kite bridge could not attach.", error); }
  }
  ensureMoreMenuActions();
  setMoreMenuAuthState(Boolean(token()));
  document.addEventListener("click", function (event) {
    var signIn = event.target.closest && event.target.closest("#pn button.kite-menu-sign-in");
    if (signIn) {
      event.preventDefault();
      event.stopImmediatePropagation();
      sessionStorage.removeItem(authDestinationKey);
      sessionStorage.removeItem(subscriptionReturnKey);
      sessionStorage.setItem(authModeKey, "login");
      navigate("launch", true);
      return;
    }
    var exitGuest = event.target.closest && event.target.closest("#pn button.kite-menu-exit-guest");
    if (exitGuest) {
      event.preventDefault();
      event.stopImmediatePropagation();
      sessionStorage.removeItem(authModeKey);
      sessionStorage.removeItem(authDestinationKey);
      sessionStorage.removeItem(subscriptionReturnKey);
      clearAuthTokens();
      hydrateShellUser(null);
      navigate("launch", true);
      return;
    }
    var target = event.target.closest && event.target.closest("#pn button.out");
    if (!target) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    logOut(frame && frame.contentDocument);
  }, true);
  bind();
}());
