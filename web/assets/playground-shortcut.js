(function () {
  "use strict";

  var frame = document.getElementById("f");
  var contextKey = "kite.playgroundReturnContext";
  var learningRoutes = {
    learn: true,
    lesson: true,
    challenge: true,
    quiz: true
  };
  var lastRoute = (window.location.hash || "#launch").slice(1);
  var restoreOnNextFrameLoad = false;
  var root = null;
  var toggle = null;
  var panel = null;
  var close = null;
  var open = null;
  var scrollTimer = null;

  function currentRoute() {
    return (window.location.hash || "#launch").slice(1);
  }

  function setExpanded(expanded, restoreFocus) {
    if (!root || !panel || !toggle) return;
    root.classList.toggle("is-expanded", expanded);
    panel.hidden = !expanded;
    toggle.setAttribute("aria-expanded", String(expanded));
    if (expanded) open.focus();
    else if (restoreFocus) toggle.focus();
  }

  function mountShortcut() {
    if (root) return;

    root = document.createElement("div");
    root.id = "kite-playground-shortcut";
    root.setAttribute("role", "group");
    root.setAttribute("aria-label", "Playground shortcut");

    toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "kite-playground-toggle";
    toggle.setAttribute("aria-label", "Open Playground shortcut");
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-controls", "kite-playground-panel");
    toggle.title = "Playground";

    var logo = document.createElement("img");
    logo.className = "kite-python-mark";
    logo.src = "/assets/python_logo.webp";
    logo.alt = "";
    logo.width = 28;
    logo.height = 28;

    var label = document.createElement("span");
    label.className = "kite-playground-label";
    label.textContent = "Playground";

    var chevron = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    chevron.classList.add("kite-playground-chevron");
    chevron.setAttribute("viewBox", "0 0 20 20");
    chevron.setAttribute("fill", "none");
    chevron.setAttribute("aria-hidden", "true");
    var chevronPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    chevronPath.setAttribute("d", "m5 7.5 5 5 5-5");
    chevronPath.setAttribute("stroke", "currentColor");
    chevronPath.setAttribute("stroke-width", "1.8");
    chevronPath.setAttribute("stroke-linecap", "round");
    chevronPath.setAttribute("stroke-linejoin", "round");
    chevron.appendChild(chevronPath);
    toggle.append(logo, label, chevron);

    panel = document.createElement("section");
    panel.id = "kite-playground-panel";
    panel.className = "kite-playground-panel";
    panel.setAttribute("aria-label", "Open the Python Playground");
    panel.hidden = true;

    close = document.createElement("button");
    close.type = "button";
    close.className = "kite-playground-close";
    close.setAttribute("aria-label", "Collapse Playground shortcut");
    close.textContent = "\u00d7";

    var message = document.createElement("p");
    var heading = document.createElement("strong");
    heading.textContent = "Ready to code?";
    message.append(heading, document.createTextNode("Try an idea in Kite's Python Playground."));

    open = document.createElement("button");
    open.type = "button";
    open.className = "kite-playground-open";
    open.textContent = "Open Playground";

    panel.append(close, message, open);
    root.append(toggle, panel);
    document.body.appendChild(root);

    toggle.addEventListener("click", function () {
      setExpanded(panel.hidden, false);
    });
    close.addEventListener("click", function () {
      setExpanded(false, true);
    });
    open.addEventListener("click", function () {
      captureContext();
      setExpanded(false, false);
      if (typeof window.kiteNavigate === "function") window.kiteNavigate("playground");
      else console.error("Kite navigation is unavailable; Playground could not be opened.");
    });
  }

  function unmountShortcut() {
    if (scrollTimer !== null) {
      window.clearTimeout(scrollTimer);
      scrollTimer = null;
    }
    if (root && root.parentNode) root.parentNode.removeChild(root);
    root = null;
    toggle = null;
    panel = null;
    close = null;
    open = null;
  }

  function captureContext() {
    var route = currentRoute();
    var context = {
      route: route,
      lessonId: sessionStorage.getItem("kite.lessonId"),
      lessonTab: null,
      scrolls: [],
      createdAt: Date.now()
    };

    try {
      var doc = frame.contentDocument;
      if (doc) {
        var activeTab = doc.querySelector(".tabs [data-tab][aria-selected='true']");
        context.lessonTab = activeTab ? activeTab.getAttribute("data-tab") : null;
        Array.prototype.forEach.call(doc.querySelectorAll(".scroll"), function (element) {
          if (element.scrollHeight <= element.clientHeight + 1 && element.scrollTop <= 0) return;
          context.scrolls.push({
            top: element.scrollTop,
            left: element.scrollLeft,
            height: element.scrollHeight,
            clientHeight: element.clientHeight
          });
        });
        var scrollingElement = doc.scrollingElement;
        if (scrollingElement &&
            (scrollingElement.scrollHeight > scrollingElement.clientHeight + 1 ||
             scrollingElement.scrollTop > 0)) {
          context.documentScroll = {
            top: scrollingElement.scrollTop,
            left: scrollingElement.scrollLeft
          };
        }
      }
      sessionStorage.setItem(contextKey, JSON.stringify(context));
    } catch (error) {
      console.error("Could not preserve the current screen before opening Playground.", error);
    }
  }

  function restoreContext() {
    var context;
    try {
      context = JSON.parse(sessionStorage.getItem(contextKey) || "null");
    } catch (error) {
      console.error("Could not read the saved screen context after Playground.", error);
      sessionStorage.removeItem(contextKey);
      return;
    }
    if (!context || context.route !== currentRoute()) return;

    var startedAt = Date.now();
    var timer = window.setInterval(function () {
      if (context.route !== currentRoute()) {
        window.clearInterval(timer);
        return;
      }
      var doc = frame.contentDocument;
      if (!doc || !doc.body) return;

      var lessonReady = context.route !== "lesson" || Boolean(doc.documentElement.kiteLesson);
      var modules = doc.getElementById("mods");
      var learnReady = context.route !== "learn" || !modules || modules.children.length > 0;
      var scrollers = doc.querySelectorAll(".scroll");
      var scrollReady = context.scrolls.every(function (saved, index) {
        var element = scrollers[index];
        return element && element.scrollHeight >= Math.min(
          saved.top + element.clientHeight,
          saved.height
        );
      });
      var ready = lessonReady && learnReady && scrollReady;

      if (ready || Date.now() - startedAt > 5000) {
        context.scrolls.forEach(function (saved, index) {
          var element = scrollers[index];
          if (!element) return;
          element.scrollTop = Math.min(saved.top, element.scrollHeight - element.clientHeight);
          element.scrollLeft = Math.min(saved.left, element.scrollWidth - element.clientWidth);
        });
        if (context.documentScroll && doc.scrollingElement) {
          doc.scrollingElement.scrollTop = context.documentScroll.top;
          doc.scrollingElement.scrollLeft = context.documentScroll.left;
        }
        if (!ready) console.warn("Restored Playground return context after lesson content timed out.");
        sessionStorage.removeItem(contextKey);
        window.clearInterval(timer);
      }
    }, 80);
  }

  function syncRoute() {
    var route = currentRoute();
    if (lastRoute === "playground" && route !== "playground") {
      try {
        var context = JSON.parse(sessionStorage.getItem(contextKey) || "null");
        restoreOnNextFrameLoad = Boolean(context && context.route === route);
        if (!restoreOnNextFrameLoad) sessionStorage.removeItem(contextKey);
      } catch (error) {
        console.error("Could not check the saved screen context after Playground.", error);
        sessionStorage.removeItem(contextKey);
      }
    }
    lastRoute = route;
    if (learningRoutes[route]) mountShortcut();
    else unmountShortcut();
  }

  function isEditable(element) {
    return element && (element.matches("input, textarea, select") ||
      element.isContentEditable);
  }

  function onFrameScroll() {
    if (!root) return;
    root.classList.add("is-scrolling");
    if (scrollTimer !== null) window.clearTimeout(scrollTimer);
    scrollTimer = window.setTimeout(function () {
      if (root) root.classList.remove("is-scrolling");
      scrollTimer = null;
    }, 750);
  }

  function onFrameFocusIn(event) {
    if (root && isEditable(event.target)) root.classList.add("is-input-focused");
  }

  function onFrameFocusOut(event) {
    var doc = event.currentTarget;
    window.setTimeout(function () {
      if (root && !isEditable(doc.activeElement)) root.classList.remove("is-input-focused");
    }, 0);
  }

  function onFrameLoad() {
    var doc = frame.contentDocument;
    if (root && doc) {
      doc.addEventListener("scroll", onFrameScroll, true);
      doc.addEventListener("focusin", onFrameFocusIn, true);
      doc.addEventListener("focusout", onFrameFocusOut, true);
      if (isEditable(doc.activeElement)) {
        onFrameFocusIn({ target: doc.activeElement });
      }
    }
    if (restoreOnNextFrameLoad) {
      restoreOnNextFrameLoad = false;
      restoreContext();
    }
  }

  function watchHistoryMethod(method) {
    var original = window.history[method];
    window.history[method] = function () {
      var result = original.apply(this, arguments);
      window.setTimeout(syncRoute, 0);
      return result;
    };
  }

  watchHistoryMethod("pushState");
  watchHistoryMethod("replaceState");
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && panel && !panel.hidden) setExpanded(false, true);
  });
  window.addEventListener("hashchange", syncRoute);
  window.addEventListener("popstate", syncRoute);
  frame.addEventListener("load", onFrameLoad);
  syncRoute();
  onFrameLoad();
})();
