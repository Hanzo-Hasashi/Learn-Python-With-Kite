(function () {
  "use strict";

  var deferredInstallPrompt = null;
  var installDismissed = false;
  var installButtons = [];
  var installDialog = null;
  var isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);

  function isInstalled() {
    return Boolean(
      (window.matchMedia && window.matchMedia("(display-mode: standalone)").matches) ||
      navigator.standalone === true
    );
  }

  function updateInstallButtons() {
    var visible = !isInstalled() && !installDismissed && (deferredInstallPrompt || isIOS);
    installButtons.forEach(function (button) {
      button.hidden = !visible;
    });
  }

  function openIOSInstructions() {
    if (!installDialog) return;
    if (typeof installDialog.showModal === "function") installDialog.showModal();
    else installDialog.setAttribute("open", "");
  }

  function closeIOSInstructions() {
    if (!installDialog) return;
    if (typeof installDialog.close === "function") installDialog.close();
    else installDialog.removeAttribute("open");
  }

  function installKite() {
    if (isInstalled()) {
      updateInstallButtons();
      return;
    }
    if (isIOS) {
      openIOSInstructions();
      return;
    }
    if (!deferredInstallPrompt) return;

    var promptEvent = deferredInstallPrompt;
    deferredInstallPrompt = null;
    updateInstallButtons();
    try {
      promptEvent.prompt();
    } catch (error) {
      console.error("Kite's install prompt could not be opened.", error);
      return;
    }
    Promise.resolve(promptEvent.userChoice).then(function (choice) {
      if (!choice || choice.outcome !== "accepted") installDismissed = true;
      updateInstallButtons();
    }).catch(function (error) {
      console.error("Kite's install choice could not be read.", error);
      installDismissed = true;
      updateInstallButtons();
    });
  }

  function addInstallButton(container) {
    if (!container || container.querySelector("[data-kite-pwa-install]")) return;
    var button = document.createElement("button");
    button.type = "button";
    button.className = "kite-pwa-install";
    button.setAttribute("data-kite-pwa-install", "");
    button.textContent = "Install Kite";
    button.hidden = true;
    button.addEventListener("click", installKite);
    container.appendChild(button);
    installButtons.push(button);
  }

  function createIOSInstructions() {
    if (!isIOS) return;
    installDialog = document.createElement("dialog");
    installDialog.id = "kite-install-dialog";
    installDialog.setAttribute("aria-labelledby", "kite-install-title");
    installDialog.setAttribute("aria-describedby", "kite-install-description");

    var heading = document.createElement("h2");
    heading.id = "kite-install-title";
    heading.textContent = "Add Kite to your Home Screen";

    var description = document.createElement("p");
    description.id = "kite-install-description";
    description.textContent =
      "In Safari, tap Share, then choose Add to Home Screen. In other browsers, open the Share menu and choose Add to Home Screen if available.";

    var closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "kite-install-close";
    closeButton.textContent = "Got it";
    closeButton.addEventListener("click", closeIOSInstructions);

    installDialog.append(heading, description, closeButton);
    installDialog.addEventListener("click", function (event) {
      if (event.target === installDialog) closeIOSInstructions();
    });
    document.body.appendChild(installDialog);
  }

  addInstallButton(document.getElementById("pn"));
  addInstallButton(document.getElementById("desk-nav-more"));
  createIOSInstructions();
  updateInstallButtons();

  window.addEventListener("beforeinstallprompt", function (event) {
    event.preventDefault();
    deferredInstallPrompt = event;
    installDismissed = false;
    updateInstallButtons();
  });
  window.addEventListener("appinstalled", function () {
    deferredInstallPrompt = null;
    installDismissed = true;
    updateInstallButtons();
  });
  if (window.matchMedia) {
    var standaloneQuery = window.matchMedia("(display-mode: standalone)");
    if (standaloneQuery.addEventListener) {
      standaloneQuery.addEventListener("change", updateInstallButtons);
    } else if (standaloneQuery.addListener) {
      standaloneQuery.addListener(updateInstallButtons);
    }
  }

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js?v=pwa-3", { scope: "/" }).catch(function (error) {
      console.error("Kite's offline support could not be registered.", error);
    });
  }
}());
