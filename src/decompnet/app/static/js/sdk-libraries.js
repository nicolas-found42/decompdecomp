"use strict";

const sdkRecentDecisions = 10;

async function loadSdkLibraries(overview) {
  const panel = $("sdkLibrariesPanel"), slug = S.slug;
  panel.hidden = !overview.can_manage_sdk_libraries;
  if (panel.hidden || panel._working) return;
  if (panel._slug !== slug) {
    panel._slug = slug;
    $("sdkLibraryLog").hidden = true;
  }
  try {
    const listing = await api(`/api/sdk-libraries?slug=${encodeURIComponent(slug)}`);
    if (S.slug !== slug || panel._working) return;
    const choices = listing.choices.map(copy => `<label class="check"><input type="checkbox" value="${esc(copy.id)}" ${listing.selected.includes(copy.id) ? "checked" : ""}><span>${esc(copy.path)}</span></label>`).join("");
    const missing = listing.missing ? `<p class="muted small">${listing.missing} selected SDK folders are unavailable.</p>` : "";
    setHtml($("sdkLibraryCopies"), missing + (choices || '<p class="muted small">No SDK libraries registered.</p>'));
    $("sdkLibrarySelect").disabled = !listing.choices.length && !listing.selected.length;
    $("sdkLibraryScan").disabled = !listing.selected.length;
    renderSdkReview(listing);
  } catch (error) {
    if (S.slug === slug) showJobError($("sdkLibraryLog"), error.message);
  }
}

function renderSdkReview({ report, reviews }) {
  const decisions = Object.values(reviews).flatMap(review =>
    [review, ...(review.history || []).map(previous => ({ ...review, ...previous, active: false, history: [] }))]);
  decisions.sort((a, b) => (b.decided_at || 0) - (a.decided_at || 0));
  const applied = Object.values(reviews).filter(review => review.active).length;
  const dismissed = Object.values(reviews).filter(review => review.decision === "dismissed").length;
  const summary = report
    ? `<p class="muted small">${report.functions} functions compared with ${report.archives} library files. ${report.pending} possible names still need evidence.</p>`
    : "";
  const cards = decisions.map(review => {
    const item = review.evidence;
    const action = review.decision === "approved" ? (review.active ? "Applied" : "Previously applied") : "Dismissed";
    const name = review.decision === "approved" && review.original_name !== item.name
      ? `${esc(review.original_name)} → ${esc(item.name)}` : esc(item.name);
    const witness = review.witness === undefined ? "" : ` · Reference checked at 0x${review.witness.toString(16).toUpperCase()}`;
    return `<details class="more"><summary>${action} ${name}</summary>
      <p>${review.reason ? esc(review.reason) : "Decision saved before reasons were recorded."}</p>
      <p class="muted small">${esc(review.by)} · Function at 0x${review.address.toString(16).toUpperCase()}${witness}<br>
      ${esc(item.archive)} (${esc(item.member)}) · ${item.resolved_relocations} of ${item.relocations} code references confirmed</p></details>`;
  });
  const earlier = cards.length > sdkRecentDecisions
    ? `<details class="more"><summary>Earlier decisions</summary>${cards.slice(sdkRecentDecisions).join("")}</details>`
    : "";
  setHtml($("sdkLibraryReview"), summary + (cards.length
    ? `<h3>Recent name decisions</h3><p class="muted small">${applied} applied · ${dismissed} dismissed</p>`
      + cards.slice(0, sdkRecentDecisions).join("") + earlier
    : '<p class="muted small">Agent decisions will appear here.</p>'));
}

async function runSdkLibraryAction(action, extras = {}) {
  const panel = $("sdkLibrariesPanel"), slug = S.slug, log = $("sdkLibraryLog");
  if (panel._working || panel._slug !== slug) return;
  panel._working = true;
  log.textContent = "";
  log.hidden = false;
  showJobError(log, "");
  const finish = beginWork(log, "Scanning SDK libraries", [...panel.querySelectorAll("button, input")],
                           "SDK scan finished.");
  let refresh = false;
  try {
    const { job } = await api("/api/sdk-libraries", { method: "POST", body: { slug, action, ...extras } });
    const result = await followJob(job, log);
    if (S.slug === slug && result?.message) log.textContent += `\n${result.message}`;
    refresh = true;
  } catch (error) {
    if (S.slug === slug) showJobError(log, error.message);
  } finally {
    finish();
    panel._working = false;
  }
  if ((refresh || S.slug !== slug) && S.ov) await loadSdkLibraries(S.ov);
}

$("sdkLibrarySelect").onclick = () => runSdkLibraryAction("select", {
  copies: [...$("sdkLibraryCopies").querySelectorAll("input:checked")].map(input => input.value),
});
$("sdkLibraryScan").onclick = () => runSdkLibraryAction("scan");
