const storageKey = "mechanic-ui-language";
export const languages = {
  ru: "Русский", be: "Беларуская", az: "Azərbaycanca", kk: "Қазақша", ky: "Кыргызча",
  tk: "Türkmençe", uz: "O‘zbekcha", hy: "Հայերեն", tg: "Тоҷикӣ", hi: "हिन्दी",
};
const localizedAttributes = ["aria-label", "placeholder", "title", "alt", "label", "aria-description"];
const excluded = '[translate="no"], script, style';
const records = new WeakMap();
const dictionaries = new Map();
let current = "ru";
let dictionary = {};
let patterns = [];
let revision = 0;

export function uiLanguage() {
  return current;
}

export function savedLanguage() {
  try {
    const code = localStorage.getItem(storageKey);
    return Object.hasOwn(languages, code) ? code : "ru";
  } catch {
    return "ru";
  }
}

function escapePattern(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function composePatterns(entries) {
  return Object.keys(entries)
    .filter((key) => /\{[\w.]+\}/u.test(key))
    .map((key) => {
      const slots = [];
      const pattern = key.split(/(\{[\w.]+\})/g).map((token) => {
        if (!/^\{[\w.]+\}$/.test(token)) return escapePattern(token);
        slots.push(token);
        return "(.+?)";
      }).join("");
      return { key, slots, match: new RegExp("^" + pattern + "$", "u") };
    })
    .sort((a, b) => b.key.length - a.key.length);
}

export function interfaceText(text) {
  if (current === "ru" || !text || !/[А-Яа-яЁё]/.test(text)) return text;
  const value = text.trim().replace(/\s+/gu, " ");
  let translated = dictionary[value];
  if (!translated) {
    for (const pattern of patterns) {
      const match = value.match(pattern.match);
      if (!match) continue;
      translated = pattern.slots.reduce((result, slot, index) =>
        result.replace(slot, () => interfaceText(match[index + 1])), dictionary[pattern.key]);
      break;
    }
  }
  if (!translated && / · | — |\n/u.test(text)) {
    const parts = text.split(/( · | — |\n)/u);
    return parts.map((part, index) => index % 2 ? part : interfaceText(part)).join("");
  }
  if (!translated) return text;
  return text.match(/^\s*/u)[0] + translated + text.match(/\s*$/u)[0];
}

function localize(node, field, text, apply) {
  let fields = records.get(node);
  if (!fields) records.set(node, fields = new Map());
  const previous = fields.get(field);
  const original = previous && previous.localized === text ? previous.original : text;
  const localized = interfaceText(original);
  fields.set(field, { original, localized });
  if (localized !== text) apply(localized);
}

function localizeTree(root) {
  const element = root.nodeType === Node.TEXT_NODE ? root.parentElement : root;
  if (!element || element.nodeType !== Node.ELEMENT_NODE || element.closest(excluded)) return;
  if (root.nodeType === Node.TEXT_NODE) {
    if (element.tagName !== "TEXTAREA" && root.data.trim()) localize(root, "text", root.data, (value) => { root.data = value; });
    return;
  }
  for (const field of localizedAttributes) {
    const value = element.getAttribute(field);
    if (value) localize(element, field, value, (translation) => element.setAttribute(field, translation));
  }
  for (const child of element.childNodes) localizeTree(child);
}

const observer = new MutationObserver((changes) => {
  if (current === "ru") return;
  for (const change of changes) {
    if (change.type === "childList") {
      for (const node of change.addedNodes) localizeTree(node);
    } else {
      localizeTree(change.target);
    }
  }
});
observer.observe(document.documentElement, {
  childList: true, subtree: true, characterData: true, attributes: true,
  attributeFilter: localizedAttributes,
});

export async function setUiLanguage(code) {
  if (!Object.hasOwn(languages, code)) return false;
  const requested = ++revision;
  let next = dictionaries.get(code);
  if (!next && code !== "ru") {
    try {
      const result = await fetch("/assets/locales/" + code + ".json");
      if (!result.ok) throw new Error("Missing interface dictionary");
      next = await result.json();
      dictionaries.set(code, next);
    } catch {
      if (requested !== revision) return false;
      throw new Error(interfaceText("Изменить язык не удалось. Повторите попытку."));
    }
  }
  if (requested !== revision) return false;
  current = code;
  dictionary = next || {};
  patterns = composePatterns(dictionary);
  document.documentElement.lang = code;
  localizeTree(document.head.querySelector("title"));
  localizeTree(document.body);
  try { localStorage.setItem(storageKey, code); } catch { /* Storage is optional. */ }
  return true;
}
