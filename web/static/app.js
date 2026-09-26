// Character counter for the "I want to…" field.
const t = document.getElementById("wish"), c = document.getElementById("count");
if (t && c) {
  const upd = () => { c.textContent = t.value.length ? `(${t.value.length}/5000)` : ""; };
  t.addEventListener("input", upd); upd();
}

// "describe your job" button: jump into the text field, ready to type.
document.querySelectorAll("a.cta").forEach((a) =>
  a.addEventListener("click", (e) => {
    if (!t) return;
    e.preventDefault();
    const smooth = !matchMedia("(prefers-reduced-motion: reduce)").matches;
    t.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "center" });
    t.focus({ preventScroll: true });
  }),
);

// Sample email: grow the frame to the email's height, so the page scrolls instead of the frame.
document.querySelectorAll(".mail iframe").forEach((f) => {
  const fit = () => {
    const doc = f.contentDocument;
    if (doc && doc.body) f.style.height = `${doc.body.scrollHeight + 32}px`; // + the body's 16px margins
  };
  f.addEventListener("load", fit);
  if (f.contentDocument && f.contentDocument.readyState === "complete") fit();
});
