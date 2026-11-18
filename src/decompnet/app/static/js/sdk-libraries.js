"use strict";

const sdkReviewPageSize = 30;
let sdkReviewPage = 0;
let sdkLibraryListing = null;

async function loadSdkLibraries(overview) {
  const panel = $("sdkLibrariesPanel"), slug = S.slug;
  panel.hidden = !overview.can_manage_sdk_libraries;
  if (panel.hidden || panel._working) return;
  if (panel._slug !== slug) {
    panel._slug = slug;
    sdkLibraryListing = null;
    sdkReviewPage = 0;
    $("sdkLibraryLog").hidden = true;
  }
  try {
    const listing = await api(`/api/sdk-libraries?slug=${encodeURIComponent(slug)}`);
    if (S.slug !== slug || panel._working) return;
    sdkLibraryListing = listing;
    const choices = listing.choices.map(copy => `<label class="check"><input type="checkbox" value="${esc(copy.id)}" ${listing.selected.includes(copy.id) ? "checked" : ""}><span>${esc(copy.path)}</span></label>`).join("");
    const missing = listing.missing ? `<p class="muted small">${listing.missing} selected SDK folders are unavailable.</p>` : "";
    setHtml($("sdkLibraryCopies"), missing + (choices || '<p class="muted small">No SDK libraries registered.</p>'));
    $("sdkLibrarySelect").disabled = !listing.choices.length && !listing.selected.length;
    $("sdkLibraryScan").disabled = !listing.selected.length;
    renderSdkReview();
  } catch (error) {
    if (S.slug === slug) showJobError($("sdkLibraryLog"), error.message);
  }
}

function renderSdkReview() {
  const { report, reviews } = sdkLibraryListing;
  const saved = Object.values(reviews).filter(review => review.decision === "approved");
  const rows = (report?.hits || []).flatMap(hit => hit.evidence.map(item => ({ hit, item, review: reviews[item.id] })))
    .filter(row => !["approved", "dismissed"].includes(row.review?.decision));
  rows.sort((a, b) => Number(b.review?.decision === "proposed") - Number(a.review?.decision === "proposed") || Number(b.item.bulk) - Number(a.item.bulk));
  const pages = Math.max(1, Math.ceil(rows.length / sdkReviewPageSize));
  sdkReviewPage = Math.min(sdkReviewPage, pages - 1);
  const cards = rows.slice(sdkReviewPage * sdkReviewPageSize, (sdkReviewPage + 1) * sdkReviewPageSize).map(({ hit, item, review }) => `
    <article class="pad">
      <b>${esc(hit.function)} → ${esc(item.name)}</b>${review?.decision === "proposed" ? ' <span class="muted small">Proposed by ' + esc(review.by) + '</span>' : ""}
      <p class="muted small">0x${hit.address.toString(16).toUpperCase()} · ${esc(item.archive)}(${esc(item.member)}) · SDK ${esc(item.sdk.slice(0, 12))}<br>${item.extent} bytes · ${item.resolved_relocations}/${item.relocations} relocations confirmed${item.layout_agrees ? "" : " · Relocation layout needs review"}</p>
      <div class="row">
        ${item.bulk ? `<label class="check"><input type="checkbox" data-sdk-bulk="${esc(item.id)}"><span>Select for bulk approval</span></label>` : ""}
        <button class="ghost small-btn" type="button" data-sdk-approve="${esc(item.id)}">Approve</button>
        <button class="ghost small-btn" type="button" data-sdk-dismiss="${esc(item.id)}">Dismiss</button>
      </div>
    </article>`).join("");
  const receipts = saved.length ? `<details class="more"><summary>Approved names (${saved.length})</summary><ul>${saved.map(review => `<li>${esc(review.evidence.name)} · ${esc(review.evidence.archive)}(${esc(review.evidence.member)}) · SDK ${esc(review.evidence.sdk.slice(0, 12))}</li>`).join("")}</ul></details>` : "";
  const summary = report ? `<p class="muted small">${report.archives} archives · ${report.functions} functions compared · ${rows.length} candidate names to review</p>` : "";
  const navigation = rows.length ? `<div class="row"><button class="ghost small-btn" type="button" data-sdk-prev ${sdkReviewPage === 0 ? "disabled" : ""}>Previous</button><span class="small">${sdkReviewPage + 1} / ${pages}</span><button class="ghost small-btn" type="button" data-sdk-next ${sdkReviewPage + 1 === pages ? "disabled" : ""}>Next</button><button class="primary small-btn" type="button" data-sdk-apply>Approve selected</button></div>` : "";
  setHtml($("sdkLibraryReview"), summary + receipts + navigation + cards);
}

async function runSdkLibraryAction(action, extras = {}) {
  const panel = $("sdkLibrariesPanel"), slug = S.slug, log = $("sdkLibraryLog");
  if (panel._working || panel._slug !== slug) return;
  panel._working = true;
  log.textContent = "";
  log.hidden = false;
  showJobError(log, "");
  const scanning = action === "select" || action === "scan";
  const finish = beginWork(log, scanning ? "Scanning SDK libraries" : "Saving SDK name decisions",
                           [...panel.querySelectorAll("button, input")], scanning ? "SDK scan finished." : "SDK review saved.");
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
$("sdkLibraryReview").onclick = event => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.hasAttribute("data-sdk-next") || button.hasAttribute("data-sdk-prev")) {
    sdkReviewPage += button.hasAttribute("data-sdk-next") ? 1 : -1;
    renderSdkReview();
  } else if (button.dataset.sdkApprove || button.dataset.sdkDismiss) {
    runSdkLibraryAction(button.dataset.sdkApprove ? "approve" : "dismiss", { ids: [button.dataset.sdkApprove || button.dataset.sdkDismiss] });
  } else if (button.hasAttribute("data-sdk-apply")) {
    const ids = [...$("sdkLibraryReview").querySelectorAll("input[data-sdk-bulk]:checked")].map(input => input.dataset.sdkBulk);
    if (ids.length) runSdkLibraryAction("approve", { ids });
  }
};
