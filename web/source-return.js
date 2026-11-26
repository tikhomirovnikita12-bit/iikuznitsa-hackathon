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
    },
  }, "");
}

export function restoreSourceOrigin() {
  const { sourceOrigin, ...state } = history.state || {};
  if (!sourceOrigin) return;
  history.replaceState(state, "");
  const section = document.getElementById(sourceOrigin.section);
  const link = section && matchingLinks(section, sourceOrigin.href)[sourceOrigin.index];
  if (!link) {
    window.scrollTo(sourceOrigin.scrollX, sourceOrigin.scrollY);
    return;
  }
  const details = link.closest("details");
  if (details) details.open = true;
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
