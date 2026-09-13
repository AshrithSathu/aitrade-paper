const $ = (id) => document.getElementById(id);
const money = (v) =>
  v == null
    ? "—"
    : Number(v).toLocaleString("en-US", {
        style: "currency",
        currency: "USD",
        maximumFractionDigits: 2,
      });
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const marketLink = (ticker, label = "Open on Polymarket") =>
  /^btc-updown-(5|15)m-\d+$/.test(ticker || "")
    ? `<a href="https://polymarket.com/event/${encodeURIComponent(ticker)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`
    : "";
const time = (v) =>
  new Date(v).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const durations = ["5", "15"];
const durationFromUrl = () => {
  const value = new URL(location.href).searchParams.get("minutes");
  return durations.includes(value) ? value : "5";
};
let selectedMinutes = durationFromUrl(),
  actionBusy = false;
const loadedSettings = new Set();
const loadedModes = new Set();
const emptyHistory = () => ({
  decisions: [],
  entries: [],
  results: [],
  rejections: {},
  outcomes: {},
  trades: [],
  page: 1,
  pages: 1,
  total: 0,
});
const histories = { 5: emptyHistory(), 15: emptyHistory() };
const historyVersions = { 5: null, 15: null };
const historyLoading = new Set();
let latest = null;
function tabsDisabled(value) {
  $("tab5").disabled = value;
  $("tab15").disabled = value;
}
function selectDuration(minutes, historyMethod) {
  selectedMinutes = minutes;
  $("saved").textContent = "";
  for (const m of durations) {
    $("tab" + m).setAttribute("aria-selected", String(m === minutes));
    $("tab" + m).className = m === minutes ? "" : "secondary";
  }
  if (historyMethod) {
    const url = new URL(location.href);
    url.searchParams.set("minutes", minutes);
    history[historyMethod](null, "", url);
  }
  if (latest) render(latest);
}
for (const minutes of durations)
  $("tab" + minutes).onclick = () => {
    if (!actionBusy) selectDuration(minutes, "pushState");
  };
window.onpopstate = () => selectDuration(durationFromUrl());
selectDuration(selectedMinutes, "replaceState");
async function action(path, body = {}, minutes = selectedMinutes) {
  if (actionBusy) return;
  actionBusy = true;
  tabsDisabled(true);
  try {
    const r = await fetch("/api/" + path + "?minutes=" + minutes, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
      d = await r.json();
    if (!r.ok) throw Error(d.error);
    $("saved").textContent = path === "settings" ? "Limits saved." : "Done.";
  } catch (e) {
    $(path === "settings" ? "saved" + minutes : "saved").textContent =
      e.message;
  } finally {
    actionBusy = false;
    tabsDisabled(false);
  }
}
for (const m of ["5", "15"]) {
  $("start" + m).onclick = () => {
    if (
      $("runhours" + m).reportValidity() &&
      $("profittarget" + m).reportValidity()
    )
      action(
        "start",
        {
          duration_hours: $("runhours" + m).value,
          profit_target_percent: $("profittarget" + m).value,
        },
        m,
      );
  };
  $("pause" + m).onclick = () => action("pause", {}, m);
}
$("codexlogin").onclick = () => action("codex/login");
for (const minutes of ["5", "15"]) {
  const form = $("settings" + minutes);
  form.onsubmit = (e) => {
    e.preventDefault();
    if (!latest) return;
    const body = {
      ...latest.accounts[minutes].settings,
      ...Object.fromEntries(new FormData(form)),
      assets: ["BTC"],
      market_minutes: minutes,
    };
    body.daily_loss = String(-Math.abs(Number(body.daily_loss)));
    action("settings", body, minutes);
  };
}

function render(update) {
  latest = update;
  for (const minutes of ["5", "15"])
    $("limits" + minutes).hidden = minutes !== selectedMinutes;
  const d = update.accounts[selectedMinutes],
    s = d.state,
    a = d.account,
    m = d.markets.BTC,
    u = m?.underlying;
  const settings = d.settings;
  $("markettitle").textContent = "Bitcoin · " + selectedMinutes + " minutes";
  $("marketlink").innerHTML = marketLink(
    m?.ticker,
    "View this market on Polymarket",
  );
  $("reviewpolicy").textContent =
    "One conditional review starting 15 seconds after opening. The live market must still match the AI plan before entry.";
  for (const [mode, info] of Object.entries(d.modes)) {
    $("mode" + mode).textContent = info.halted
      ? "Limit reached"
      : info.paused
        ? "Stopped"
        : "Running";
    $("start" + mode).disabled = !info.paused || !!info.halted;
    $("runhours" + mode).disabled = !info.paused;
    $("profittarget" + mode).disabled = !info.paused;
    if (!loadedModes.has(mode)) {
      $("runhours" + mode).value = info.run_hours || 12;
      $("profittarget" + mode).value = info.profit_target_percent || 0;
      loadedModes.add(mode);
    }
  }
  for (const minutes of ["5", "15"]) {
    const account = update.accounts[minutes],
      form = $("settings" + minutes),
      limits = account.settings;
    if (!loadedSettings.has(minutes)) {
      for (const k of ["balance", "max_trade", "size", "daily_loss"])
        form.elements[k].value =
          k === "daily_loss" ? Math.abs(Number(limits[k])) : limits[k];
      loadedSettings.add(minutes);
    }
    const used = Math.max(0, -Number(account.account.realized_pnl)),
      limit = Math.abs(Number(limits.daily_loss));
    $("lossbudget" + minutes).textContent = limit
      ? money(used) + " used of " + money(limit)
      : "No loss limit";
    $("lossbar" + minutes).style.width =
      (limit ? Math.min(100, (used / limit) * 100) : 0) + "%";
  }
  $("badge").textContent = s.halted
    ? "Limit reached"
    : d.paused
      ? "Paused"
      : "Running";
  $("notice").textContent = d.error
    ? "Trading paused: " + d.error
    : d.paused
      ? s.stop_reason
        ? s.stop_reason + ". AI is stopped."
        : "Paused. AI is stopped."
      : (d.waiting_for
          ? "Session started. " +
            d.waiting_for +
            ". No entry until data is ready. "
          : "Paper trading is running. ") +
        (s.run_until
          ? "Stops at " + new Date(s.run_until).toLocaleString() + ". "
          : "") +
        "AI reviews each market once.";
  ["cash", "equity"].forEach((k) => ($(k).textContent = money(a[k])));
  $("pnl").textContent = money(a.realized_pnl);
  $("pnl").className = Number(a.realized_pnl) < 0 ? "bad" : "good";
  $("unrealized").textContent = money(a.unrealized_pnl);
  $("window").textContent = m
    ? time(m.open_time) + " – " + time(m.close_time)
    : "Waiting for market";
  $("up").textContent = money(m?.yes_ask_dollars);
  $("down").textContent = money(m?.no_ask_dollars);
  $("current").textContent = "Bitcoin: " + money(u?.price);
  $("opening").textContent = "Opening: " + money(u?.opening_price);
  $("change").textContent = "Change: " + money(u?.delta);
  $("countdown").textContent = m
    ? Math.max(0, (Date.parse(m.close_time) - Date.now()) / 60000).toFixed(1) +
      " min left"
    : "—";
  const error = d.errors.BTC || u?.error,
    stale =
      !m ||
      Date.now() - Date.parse(m.received_at) > 5000 ||
      !u?.source_at ||
      Date.now() - Date.parse(u.source_at) > 5000;
  $("feed").textContent = error
    ? "Prices unavailable: " + error
    : stale
      ? "Waiting for fresh prices"
      : u.opening_price == null
        ? "Waiting for opening price"
        : "Prices live · Updated " + time(u.source_at);
  const positions = [
    ...Object.values(s.positions).map((p) => ({ ...p, status: "Open" })),
    ...Object.values(s.pending).map((p) => ({
      ...p,
      status: "Waiting for settlement",
    })),
  ];
  $("position").innerHTML = positions.length
    ? positions
        .map(
          (p) =>
            `<div class="event"><strong>${esc(p.side)} · ${esc(p.status)}</strong><br>${esc(p.size)} contracts · Entry ${money(p.entry)} · Current value per contract ${money(p.last_mark)}<br>${marketLink(p.ticker)}</div>`,
        )
        .join("")
    : "No active or settling trade.";
  $("performance").textContent =
    `${a.trades} closed trades · ${a.wins} wins · ${a.losses} losses`;
  const review = d.codex;
  const reviewTicker = review?.payload?.markets?.BTC?.ticker;
  const currentReview = reviewTicker && reviewTicker === m?.ticker;
  $("reviewstatus").textContent =
    currentReview && review?.status === "running"
      ? "AI is reviewing this market…"
      : currentReview && review?.response?.reason
        ? review.response.reason
        : d.state.phases?.BTC === "SKIPPED_WINDOW"
          ? "No decision: required live data was unavailable during this market's review window."
          : d.waiting_for
            ? "Waiting for data before this market can be reviewed."
            : "No decision yet for this market.";
  $("reviewtime").textContent = review?.payload?.at
    ? (currentReview ? "Current review: " : "Previous review: ") +
      new Date(review.payload.at).toLocaleString()
    : "";
  renderHistory(selectedMinutes);
  if (historyVersions[selectedMinutes] !== d.event_version)
    loadHistory(selectedMinutes);
  const login = update.login;
  $("loginstatus").textContent = login.status;
  $("codexlogin").hidden = login.authenticated;
  $("codexlogin").disabled =
    login.running || Object.values(update.accounts).some((a) => !a.paused);
  $("loginoutput").textContent = login.authenticated ? "" : login.output;
}

function renderHistory(minutes) {
  const history = histories[minutes];
  const exits = history.trades;
  $("trades").innerHTML = exits.length
    ? exits
        .slice()
        .reverse()
        .map(
          (e) =>
            `<tr><td>${esc(new Date(e.at).toLocaleString())}</td><td>${marketLink(e.ticker, e.side)}</td><td>${esc(e.size)}</td><td>${money(e.entry)}</td><td>${money(e.exit)}</td><td class="${Number(e.pnl) < 0 ? "bad" : "good"}">${money(e.pnl)}</td></tr>`,
        )
        .join("")
    : '<tr><td colspan="6">No closed trades yet.</td></tr>';
  const outcomes = Object.fromEntries(
    Object.entries(history.outcomes).map(([ticker, payouts]) => [
      ticker,
      payouts.UP === "1"
        ? "Up won"
        : payouts.DOWN === "1"
          ? "Down won"
          : "Split result",
    ]),
  );
  const entries = new Map(
    history.entries.map((entry) => [entry.ticker, entry]),
  );
  const results = new Map(
    history.results.map((result) => [result.ticker, result]),
  );
  const decisions = history.decisions
    .flatMap((e) => e.decisions.map((decision) => ({ ...decision, at: e.at })))
    .slice(0, 20);
  $("decisionhistory").innerHTML = decisions.length
    ? decisions
        .map((decision) => {
          const action =
            decision.action === "WAIT"
              ? "Skipped"
              : decision.action === "ENTER_UP"
                ? "Enter Up"
                : "Enter Down";
          const entry = entries.get(decision.ticker),
            result = results.get(decision.ticker),
            pnl = Number(result?.pnl),
            pill = result
              ? `<span class="result-pill ${pnl >= 0 ? "result-profit" : "result-loss"}">${pnl >= 0 ? "+" : ""}${money(pnl)}</span>`
              : entry
                ? '<span class="result-pill result-pending">Settlement pending</span>'
                : `<span class="result-pill">${decision.action === "WAIT" ? "Skipped" : "Not filled"}</span>`,
            invested = entry ? `Invested ${money(entry.cost)} · ` : "",
            rejection = history.rejections[decision.ticker];
          const outcome = outcomes[decision.ticker] || "Outcome pending";
          return `<div class="event"><div class="row"><strong>${esc(action)}</strong>${pill}</div><div class="sub">${esc(new Date(decision.at).toLocaleString())} · ${invested}${esc(outcome)} · ${marketLink(decision.ticker)}</div>${rejection ? `<div class="bad">Not filled: ${esc(rejection)}</div>` : ""}<div>${esc(decision.reason)}</div></div>`;
        })
        .join("")
    : "No decisions yet.";
  $("decisionpage").textContent = history.total
    ? `Page ${history.page} of ${history.pages} · ${history.total} decisions`
    : "No decisions";
  $("newerdecisions").disabled =
    history.page <= 1 || historyLoading.has(minutes);
  $("olderdecisions").disabled =
    history.page >= history.pages || historyLoading.has(minutes);
}

async function loadHistory(minutes, page = 1) {
  if (historyLoading.has(minutes)) return;
  historyLoading.add(minutes);
  if (selectedMinutes === minutes) renderHistory(minutes);
  try {
    const response = await fetch(
        "/api/history?minutes=" + minutes + "&page=" + page,
      ),
      data = await response.json();
    if (!response.ok) throw Error(data.error);
    histories[minutes] = data;
    historyVersions[minutes] = data.version;
    if (selectedMinutes === minutes) renderHistory(minutes);
  } catch (_) {
    // Keep the last successful history; the live connection will retry later.
  } finally {
    historyLoading.delete(minutes);
    if (selectedMinutes === minutes) renderHistory(minutes);
  }
}
$("newerdecisions").onclick = () =>
  loadHistory(selectedMinutes, histories[selectedMinutes].page - 1);
$("olderdecisions").onclick = () =>
  loadHistory(selectedMinutes, histories[selectedMinutes].page + 1);
let stream;
function connectStream() {
  if (stream || document.hidden) return;
  stream = new EventSource("/api/events");
  stream.onmessage = (event) => render(JSON.parse(event.data));
  stream.onerror = () => {
    $("badge").textContent = "Reconnecting";
    $("notice").textContent =
      "Live connection interrupted. Reconnecting automatically; displayed values may be out of date.";
    for (const m of ["5", "15"]) $("start" + m).disabled = true;
  };
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stream?.close();
    stream = null;
  } else {
    connectStream();
  }
});
connectStream();
