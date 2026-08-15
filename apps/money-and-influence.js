/* Money & Influence — a reading room over the AEC Transparency Register.
 *
 * Every number on screen comes from a view in this realm, called through the host's view surface.
 * Nothing is computed here: a page that does its own arithmetic over fetched rows becomes a second
 * implementation of the realm, and the two disagree eventually.
 */

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => Array.from(root.querySelectorAll(s));
const esc = (v) => String(v ?? "").replace(/[&<>'"]/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
const unentity = (v) => String(v ?? "")
  .replace(/&nbsp;/g, " ").replace(/&#39;|&apos;/g, "'").replace(/&quot;/g, '"')
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
const money = (n) => n == null ? "—" : "$" + Number(n).toLocaleString("en-AU");
const num = (n) => n == null ? "—" : Number(n).toLocaleString("en-AU");

/* A view call, and the two failure modes that matter.
 *
 * A request that never settles shows nothing and says nothing, which on a page whose whole claim is
 * "this is what the register discloses" reads as "the register discloses nothing". And a producer
 * that could not reach its source returns rows plus a WARNING — treating that as an empty answer is
 * exactly the confusion this realm exists to refuse, so warnings are surfaced, never swallowed.
 */
const TIMEOUT_MS = 180000;

async function invoke(view, args) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  let response;
  try {
    response = await fetch(`/api/v1/views/${encodeURIComponent(view)}/invoke`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args }),
      signal: controller.signal,
    });
  } catch (e) {
    throw new Error(e && e.name === "AbortError"
      ? `${view} took longer than ${Math.round(TIMEOUT_MS / 1000)}s and was stopped.`
      : "The workspace could not be reached.");
  } finally {
    clearTimeout(timer);
  }
  if (response.status === 401) throw new Error("Your session has lapsed. Sign in to the workspace and try again.");
  if (!response.ok) throw new Error(`${view} failed (${response.status}).`);
  const body = await response.json();
  if (body.status === "FAILED") throw new Error(body.error?.message || `${view} failed.`);
  return { rows: body.data ?? [], warnings: body.warnings ?? [], metrics: body.metrics ?? {} };
}

/* ── State ──────────────────────────────────────────────────────────────────────────────────── */

const state = { family: "Liberal Party of Australia", sinceFy: "2024-25", limit: 8, open: null };

/* ── Panels ─────────────────────────────────────────────────────────────────────────────────── */

function warn(warnings) {
  if (!warnings.length) return "";
  return `<div class="warn"><b>The data could not be fully fetched.</b> ${
    warnings.map((w) => esc(String(w).replace(/^PRODUCER_ERROR:\s*/, ""))).join(" ")
  }</div>`;
}

function loading(el, what) {
  el.innerHTML = `<div class="loading">Reading ${esc(what)}…</div>`;
}

function failed(el, e) {
  el.innerHTML = `<div class="warn"><b>Could not read this.</b> ${esc(e.message)}</div>`;
}

async function renderScale() {
  const el = $("#scale");
  loading(el, "the register");
  try {
    const { rows, warnings } = await invoke("PartyFundingScale", { familyName: state.family, sinceFy: state.sinceFy });
    const s = rows[0] || {};
    el.innerHTML = warn(warnings) + `
      <div class="figs">
        <div class="fig"><b>${money(s.disclosedTotal)}</b><span>disclosed to ${esc(state.family)} since ${esc(state.sinceFy)}</span></div>
        <div class="fig"><b>${num(s.donors)}</b><span>donors disclosed it</span></div>
        <div class="fig"><b>${num(s.gifts)}</b><span>separate gifts</span></div>
        <div class="fig"><b>${num(s.mostEntitiesOneDonorReached)}</b><span>party entities the widest-spread donor reached</span></div>
      </div>
      <p class="narrative">These are the amounts entities <em>disclosed</em> under law, rolled up per donor across
      every branch and division that lodged a return. That rollup is the point: the register records
      ${num(s.donors)} donors against a few dozen political interests, and a reader with one return in front of
      them cannot put it back together. Nothing here says what any donor sought or received, and gifts below the
      disclosure threshold do not appear at all.</p>`;
  } catch (e) { failed(el, e); }
}

async function renderBackers() {
  const el = $("#backers");
  loading(el, "the largest donors");
  try {
    const { rows, warnings } = await invoke("FamilyBackers", {
      familyName: state.family, sinceFy: state.sinceFy, limit: state.limit,
    });
    if (!rows.length) {
      el.innerHTML = `<p class="empty">No disclosures for ${esc(state.family)} since ${esc(state.sinceFy)}.</p>`;
      return;
    }
    el.innerHTML = warn(warnings) + `
      <p class="narrative">Read the <b>entities</b> column before the total. A figure spanning eight legal entities is
      a different fact from one cheque: each entity lodges its own return, and only the sum — which appears on none
      of them — shows what the donor gave the party as a whole. Select a donor to see who they are and what
      published sources say.</p>
      <div class="scroll"><table>
        <thead><tr><th>Donor, as lodged</th><th class="r">Disclosed</th><th class="r">Gifts</th><th class="r">Entities</th><th>Year</th></tr></thead>
        <tbody>${rows.map((r) => `
          <tr class="pick" data-donor="${esc(r.donor)}" tabindex="0" role="button"
              aria-label="Show sources for ${esc(r.donor)}">
            <td class="name">${esc(r.donor)}</td>
            <td class="r amt">${money(r.total)}</td>
            <td class="r">${num(r.gifts)}</td>
            <td class="r ${r.branches > 1 ? "spread" : ""}">${num(r.branches)}</td>
            <td class="fy">${esc(r.financialYear)}</td>
          </tr>`).join("")}
        </tbody>
      </table></div>`;
    $$(".pick", el).forEach((row) => {
      const open = () => drill(row.dataset.donor);
      row.addEventListener("click", open);
      row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    });
  } catch (e) { failed(el, e); }
}

async function renderSpread() {
  const el = $("#spread");
  loading(el, "branch spreading");
  try {
    const { rows, warnings } = await invoke("BranchSpreading", {
      familyName: state.family, sinceFy: state.sinceFy, minBranches: 3, limit: 8,
    });
    if (!rows.length) {
      el.innerHTML = `<p class="empty">No donor reached three or more separate entities of this family in the window.</p>`;
      return;
    }
    el.innerHTML = warn(warnings) + `
      <p class="narrative">Nothing here is irregular: parties lodge as separate divisions and a donor may deal with
      each. But money split eight ways is eight modest disclosures and one large one, and only the second is the
      fact. These are the rows where reading a single return understates most.</p>
      <div class="scroll"><table>
        <thead><tr><th>Donor</th><th class="r">Entities</th><th class="r">Disclosed</th><th>Which entities</th></tr></thead>
        <tbody>${rows.map((r) => `
          <tr class="pick" data-donor="${esc(r.donor)}" tabindex="0" role="button">
            <td class="name">${esc(r.donor)}</td>
            <td class="r spread">${num(r.branches)}</td>
            <td class="r amt">${money(r.total)}</td>
            <td class="lodged">${esc(r.branchesLodged || "").split(" | ").map((b) => `<span>${esc(b)}</span>`).join("")}</td>
          </tr>`).join("")}
        </tbody>
      </table></div>`;
    $$(".pick", el).forEach((row) => row.addEventListener("click", () => drill(row.dataset.donor)));
  } catch (e) { failed(el, e); }
}

async function renderDossier() {
  const el = $("#dossier");
  loading(el, "who the donors are — this reads live pages and takes a moment");
  try {
    const { rows, warnings } = await invoke("PartyFundingDossier", {
      familyName: state.family, sinceFy: state.sinceFy, limit: Math.min(state.limit, 5),
    });
    if (!rows.length) { el.innerHTML = `<p class="empty">Nothing to report for this selection.</p>`; return; }
    el.innerHTML = warn(warnings) + `
      <p class="narrative">Each paragraph is written from that donor's own sources and nothing else. Every statement
      about who a donor <em>is</em> is reported as the source's, never as this realm's, and the kind of source is
      named so a filing lodged under law is never mistaken for a newspaper.</p>
      ${rows.map((r) => `
        <article class="para">
          <h4>${esc(r.donor)}<span class="amt">${money(r.disclosedTotal)}</span></h4>
          ${r.searchedAs && r.searchedAs !== r.donor
            ? `<p class="alias">searched as <b>${esc(r.searchedAs)}</b> — the lodged name is printed as lodged</p>` : ""}
          <p>${esc(r.prose)}</p>
          <p class="sources">${(r.sources || []).map((s) => `
            <a href="${esc(s.url)}" target="_blank" rel="noopener" class="chip ${gradeClass(s.kind)}"
               title="${esc(s.title)}">${esc(gradeLabel(s.kind))}</a>`).join("")}</p>
        </article>`).join("")}
      <p class="caveat">${esc(rows[0].caveats || "")}</p>`;
  } catch (e) { failed(el, e); }
}

const gradeClass = (k) => k === "a disclosure lodged under law" ? "filing"
  : k === "a government register" ? "register"
  : k === "an encyclopaedia entry" ? "encyc" : "press";
const gradeLabel = (k) => k === "a disclosure lodged under law" ? "Lodged under law"
  : k === "a government register" ? "Government register"
  : k === "an encyclopaedia entry" ? "Encyclopaedia" : "Named publication";

/* ── Drill-in ───────────────────────────────────────────────────────────────────────────────── */

async function drill(donor) {
  state.open = donor;
  const panel = $("#drill");
  panel.hidden = false;
  $("#drill-name").textContent = donor;
  const body = $("#drill-body");
  body.innerHTML = `<div class="loading">Reading everything the register holds on this donor…</div>`;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });

  const [identity, sameDay, sources] = await Promise.allSettled([
    invoke("DonorIdentity", { entityName: donor }),
    invoke("SameDayGiving", { entityName: donor, limit: 6 }),
    invoke("PartyFundingSources", { familyName: state.family, sinceFy: state.sinceFy, limit: state.limit }),
  ]);

  const blocks = [];

  if (identity.status === "fulfilled" && identity.value.rows.length) {
    const i = identity.value.rows[0];
    blocks.push(`<section><h4>Spellings of this donor</h4>
      <p class="narrative">${i.variants > 1
        ? `The register spelled this donor <b>${num(i.variants)}</b> ways, splitting a combined ${money(i.combinedTotal)} between them — a single spelling understates it.`
        : "The register used one spelling for this donor throughout, which is the common case and not a failure to resolve anything."}</p>
      <dl class="kv">
        <dt>Combined across spellings</dt><dd class="amt">${money(i.combinedTotal)}</dd>
        <dt>This spelling alone</dt><dd class="amt">${money(i.thisSpellingTotal)}</dd>
        <dt>Spellings</dt><dd>${esc(i.variantNames || i.canonicalName)}</dd>
      </dl></section>`);
  }

  if (sameDay.status === "fulfilled" && sameDay.value.rows.length) {
    blocks.push(`<section><h4>Days this donor paid more than one political family</h4>
      <p class="narrative">Say the innocent reading first, because it is usually right: paying several parties is
      ordinary access buying, disclosed exactly as required, and the date is the one the <em>donor</em> recorded — four
      payments sharing a date may be one finance run. What a batch run does not explain is a habit sustained over years.</p>
      <div class="scroll"><table>
        <thead><tr><th>Date</th><th class="r">Families</th><th class="r">Paid that day</th><th>Which</th></tr></thead>
        <tbody>${sameDay.value.rows.map((r) => `<tr>
          <td class="fy">${esc(r.madeOn)}</td><td class="r spread">${num(r.familiesPaid)}</td>
          <td class="r amt">${money(r.paidThatDay)}</td>
          <td class="lodged">${esc(r.whichFamilies || "").split(" | ").map((f) => `<span>${esc(f)}</span>`).join("")}</td>
        </tr>`).join("")}</tbody></table></div></section>`);
  }

  if (sources.status === "fulfilled") {
    const mine = sources.value.rows.filter((r) => r.donorAsLodged === donor && r.url);
    if (mine.length) {
      blocks.push(`<section><h4>Sources read for this donor</h4>
        <p class="narrative">The text below is what was actually read from each page — the evidence any sentence about
        this donor has to trace back to.</p>
        ${mine.map((s) => `<div class="src">
          <a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>
          <span class="chip ${gradeClass(s.sourceKind)}">${esc(gradeLabel(s.sourceKind))}</span>
          <p class="quote">${esc(unentity(s.text || "").slice(0, 400))}${(s.text || "").length > 400 ? "…" : ""}</p>
        </div>`).join("")}</section>`);
    }
  }

  body.innerHTML = blocks.length ? blocks.join("")
    : `<p class="empty">The register holds this donor's gifts, and nothing further was resolved for it here.</p>`;
}

/* ── Controls ───────────────────────────────────────────────────────────────────────────────── */

async function populateFamilies() {
  const select = $("#family");
  try {
    const families = await (await fetch("families.json", { credentials: "same-origin" })).json();
    select.innerHTML = families.map((f) =>
      `<option value="${esc(f.family)}"${f.family === state.family ? " selected" : ""}>${esc(f.family)}${
        f.lodgers > 1 ? ` — ${f.lodgers} lodging entities` : ""}</option>`).join("");
  } catch {
    select.innerHTML = `<option value="${esc(state.family)}">${esc(state.family)}</option>`;
  }
}

function refresh() {
  renderScale();
  renderBackers();
  renderSpread();
  renderDossier();
  $("#drill").hidden = true;
}

document.addEventListener("DOMContentLoaded", async () => {
  await populateFamilies();
  $("#family").addEventListener("change", (e) => { state.family = e.target.value; refresh(); });
  $("#since").addEventListener("change", (e) => { state.sinceFy = e.target.value; refresh(); });
  $("#limit").addEventListener("change", (e) => { state.limit = Number(e.target.value); refresh(); });
  $("#drill-close").addEventListener("click", () => { $("#drill").hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#drill").hidden = true; });
  refresh();
});
