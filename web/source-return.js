function isOrdinaryClick(event) {
  return !event.defaultPrevented && event.button === 0 &&
    !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey;
}

function matchingLinks(section, href) {
  return [...section.querySelectorAll("a.ref")].filter((link) => link.getAttribute("href") === href);
}

export function rememberSourceOrigin(event, pendingDraft) {
  if (!isOrdinaryClick(event) || !(event.target instanceof Element)) return;
  const link = event.target.closest("a.ref");
  const section = link?.closest("#result, #messages, #ticket-panel");
  if (!section || link.target || link.origin !== location.origin) return;
  const href = link.getAttribute("href");
  history.replaceState({
    ...history.state,
    sourceOrigin: {
      section: section.id,
      href,
      index: matchingLinks(section, href).indexOf(link),
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      offset: link.getBoundingClientRect().top,
      pendingDraft,
      question: document.getElementById("question")?.value || "",
    },
  }, "");
  // Storage is optional: source links still work if browser storage is disabled.
  try {
    const card = link.closest("[data-source-context], .first-check, .answer-block");
    const content = card?.cloneNode(true);
    content?.querySelectorAll(".refs, button").forEach((node) => node.remove());
    sessionStorage.setItem("mechanic-source-context", JSON.stringify({
      href: new URL(href, location.origin).pathname,
      from: location.href,
      time: Date.now(),
      vehicle: new URL(location.href).searchParams.get("vehicle_id") || "",
      session: new URL(location.href).searchParams.get("session_id") || "",
      engine: document.querySelector("#vehicle-facts strong")?.textContent || "",
      recommendation: content ? [...content.querySelectorAll("h3, p, li")].map((node) => node.textContent.trim()).filter(Boolean).join("\n").slice(0, 4000) || content.textContent.trim().slice(0, 4000) : "",
    }));
  } catch { /* The source and browser Back remain available. */ }
}

export function restoreSourceOrigin() {
  const { sourceOrigin, ...state } = history.state || {};
  if (!sourceOrigin) return;
  history.replaceState(state, "");
  const question = document.getElementById("question");
  if (question && typeof sourceOrigin.question === "string") {
    question.value = sourceOrigin.question;
    question.dispatchEvent(new Event("input", { bubbles: true }));
  }
  const section = document.getElementById(sourceOrigin.section);
  const link = section && matchingLinks(section, sourceOrigin.href)[sourceOrigin.index];
  if (!link) {
    window.scrollTo(sourceOrigin.scrollX, sourceOrigin.scrollY);
    return;
  }
  for (let parent = link.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === "DETAILS") parent.open = true;
  }
  link.focus({ preventScroll: true });
  const top = window.scrollY + link.getBoundingClientRect().top - sourceOrigin.offset;
  window.scrollTo(sourceOrigin.scrollX, top);
}

const backLink = document.getElementById("source-return");
if (backLink && document.referrer) {
  const previous = new URL(document.referrer);
  if (previous.origin === location.origin && previous.pathname === "/") {
    backLink.href = previous.href;
    backLink.addEventListener("click", (event) => {
      if (!isOrdinaryClick(event) || history.length < 2) return;
      event.preventDefault();
      history.back();
    });
  }
}


const context = document.getElementById("source-context");
if (context) {
  try {
    const saved = JSON.parse(sessionStorage.getItem("mechanic-source-context") || "null");
    if (saved?.href === location.pathname && saved.from === document.referrer &&
        Date.now() - saved.time < 10 * 60 * 1000) {
      const title = document.createElement("h2");
      title.textContent = "Текущая проверка";
      const vehicle = document.createElement("p");
      vehicle.textContent = [saved.vehicle, saved.engine, saved.session].filter(Boolean).join(" · ");
      context.append(title, vehicle);
      if (saved.recommendation) {
        const details = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = "Рекомендация к этой странице";
        const recommendation = document.createElement("p");
        recommendation.textContent = saved.recommendation;
        details.append(summary, recommendation);
        context.append(details);
      }
      context.hidden = false;
    }
  } catch { /* Context supplements the PDF and is not required to open it. */ }
}

const preview = document.querySelector(".source-image");
if (preview) {
  const showPreviewFallback = () => {
    preview.hidden = true;
    document.getElementById("source-preview-note").hidden = false;
  };
  preview.addEventListener("error", showPreviewFallback);
  if (preview.complete && !preview.naturalWidth) showPreviewFallback();
}
