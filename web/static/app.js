/* ===========================================================================
   GTMO XAUUSD Dashboard — client
   Fetches initial state, then streams live updates over WebSocket.
   =========================================================================== */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const MAX_FEED = 60;
  let feedCount = 0;

  /* ----------------------------------------------------------------- utils */
  const fmtMoney = (v) => {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  };
  const fmtNum = (v) => (v == null || isNaN(v) ? "—" : Number(v));
  const esc = (s) =>
    String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const relTime = (ts) => {
    if (!ts) return "";
    const d = (Date.now() / 1000) - ts;
    if (d < 5) return "now";
    if (d < 60) return `${Math.floor(d)}s`;
    if (d < 3600) return `${Math.floor(d / 60)}m`;
    if (d < 86400) return `${Math.floor(d / 3600)}h`;
    return `${Math.floor(d / 86400)}d`;
  };

  let toastTimer;
  const toast = (msg) => {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
  };

  const setValue = (el, text) => {
    if (!el) return;
    const node = el.querySelector("[data-value]") || el;
    if (node.textContent === String(text)) return;
    node.textContent = text;
    node.classList.remove("flash");
    void node.offsetWidth; // reflow to restart animation
    node.classList.add("flash");
  };

  /* --------------------------------------------------------------- status */
  function renderStatus(s) {
    document.body.dataset.state = s && s.online ? "ready" : "offline";

    const live = s && s.dry_run === false;
    const pill = $("#modePill");
    pill.dataset.mode = !s || !s.online ? "" : live ? "live" : "dry";
    $("#modeLabel").textContent = !s || !s.online
      ? "Offline" : live ? "Live" : "Dry-run";

    const dry = $("#dryFlag");
    dry.textContent = !s || !s.online
      ? "Bot offline" : live ? "Live trading" : "Dry-run · simulated";
    dry.dataset.live = live ? "1" : "0";

    toggleConn($("#connTg"), s && s.telegram_connected);
    toggleConn($("#connMt5"), s && s.mt5_connected);
    $("#connState").textContent = !s || !s.online
      ? "No heartbeat — start the bot"
      : `${s.parser_mode || "?"} · ${s.provider || ""} · ${s.symbol || "XAUUSD"}`;

    if (!s || !s.online) return;

    setValue($("#statBalance"), fmtMoney(s.balance));
    setValue($("#statEquity"), fmtMoney(s.equity));
    $("#symbolTag").textContent = s.symbol || "";

    // Daily P/L (equity − start balance is unknown; use realized loss + float).
    const start = s.daily_start_balance || s.balance || 0;
    const pl = (s.equity != null && start) ? s.equity - start : -(s.daily_loss || 0);
    const plEl = $("#statPnl");
    setValue(plEl, (pl >= 0 ? "+" : "−") + fmtMoney(Math.abs(pl)));
    const sub = $("#pnlBar");
    // risk bar = how much of the daily loss budget is used
    const budget = (s.daily_start_balance || 0) * (s.max_daily_loss || 0.05);
    const used = budget ? Math.min(100, (s.daily_loss / budget) * 100) : 0;
    const bar = sub.querySelector(".risk-bar");
    sub.querySelector("i").style.width = used + "%";
    bar.classList.toggle("danger", s.halted);

    setValue($("#statSignals"), fmtNum(s.open_signals || 0));
    $("#positionsHint").textContent = `${s.open_positions || 0} position${(s.open_positions || 0) === 1 ? "" : "s"}`;

    if (s.performance) renderPerformance(s.performance);
    if (s.review_queue != null) renderReview(s.review_queue);
    if (s.intel_enabled != null) {
      $("#intelChip").classList.toggle("is-off", !s.intel_enabled);
    }

    // Emergency stop button reflects live state.
    setEstop(!!s.emergency_stop);
    if (s.halted) $("#equityHint").textContent = "Daily loss limit reached";
  }

  /* ----------------------------------------------------------- performance */
  function renderPerformance(p) {
    if (!p) return;
    const set = (key, text, dir) => {
      const el = document.querySelector(`[data-perf="${key}"]`);
      if (!el) return;
      if (el.textContent !== String(text)) {
        el.textContent = text;
        el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
      }
      el.classList.toggle("up", dir === 1);
      el.classList.toggle("down", dir === -1);
    };
    set("trades", p.trades ?? 0);
    set("win_rate", p.trades ? Math.round((p.win_rate || 0) * 100) + "%" : "—");
    set("profit_factor", p.profit_factor != null ? p.profit_factor : "—",
        p.profit_factor >= 1 ? 1 : (p.trades ? -1 : 0));
    const exp = p.expectancy || 0;
    set("expectancy", p.trades ? (exp >= 0 ? "+" : "−") + fmtMoney(Math.abs(exp)) : "—",
        p.trades ? (exp >= 0 ? 1 : -1) : 0);
    set("avg_r", p.avg_r != null ? (p.avg_r >= 0 ? "+" : "") + p.avg_r + "R" : "—",
        p.avg_r > 0 ? 1 : (p.avg_r < 0 ? -1 : 0));
    const net = p.net_profit || 0;
    set("net_profit", p.trades ? (net >= 0 ? "+" : "−") + fmtMoney(Math.abs(net)) : "—",
        p.trades ? (net >= 0 ? 1 : -1) : 0);
    set("max_drawdown", p.trades ? fmtMoney(p.max_drawdown || 0) : "—");
    const st = p.streak || 0;
    set("streak", st === 0 ? "—" : (st > 0 ? `${st}W` : `${-st}L`),
        st > 0 ? 1 : (st < 0 ? -1 : 0));
  }

  function renderReview(n) {
    const chip = $("#reviewChip");
    chip.textContent = `${n} to review`;
    chip.classList.toggle("is-off", !n);
  }

  function toggleConn(el, on) {
    if (!el) return;
    el.classList.toggle("is-on", !!on);
  }

  /* ------------------------------------------------------------- positions */
  function renderSignals(signals) {
    const list = $("#positionsList");
    const empty = $("#positionsEmpty");
    $("#positionsCount").textContent = signals.length;

    // remove previous cards
    list.querySelectorAll(".sigcard").forEach((n) => n.remove());

    if (!signals.length) {
      empty.classList.remove("is-hidden");
      return;
    }
    empty.classList.add("is-hidden");

    for (const s of signals) {
      list.appendChild(signalCard(s));
    }
  }

  function signalCard(s) {
    const card = document.createElement("article");
    card.className = "sigcard";
    const dirClass = s.direction === "BUY" ? "dir--buy" : "dir--sell";
    const zone = s.entry_low === s.entry_high
      ? fmtNum(s.entry_low)
      : `${fmtNum(s.entry_low)}–${fmtNum(s.entry_high)}`;

    const legs = (s.positions || []).map((p) => {
      const cls = ["leg"];
      if (p.breakeven) cls.push("is-be");
      if (p.is_open_runner) cls.push("is-runner");
      const tp = p.tp_price ? fmtNum(p.tp_price) : "open";
      return `<span class="${cls.join(" ")}"><b>${esc(p.tp_label || "TP")}</b> ${esc(tp)} · ${esc(p.volume)}</span>`;
    }).join("");

    card.innerHTML = `
      <div class="sigcard__top">
        <div class="sigcard__id">
          <span class="dir ${dirClass}">${esc(s.direction || "")}</span>
          <span class="sigcard__tag">#${esc(s.signal_id)} · ${esc(s.symbol || "XAUUSD")}</span>
        </div>
        <span class="sigcard__kind">${esc(s.order_kind || "")}</span>
      </div>
      <div class="sigcard__row">
        <div class="kv"><span>Zone</span><b>${esc(zone)}</b></div>
        <div class="kv"><span>Stop</span><b>${esc(fmtNum(s.sl))}</b></div>
        <div class="kv"><span>Legs</span><b>${(s.positions || []).length}</b></div>
        <div class="kv"><span>TP hit</span><b>${(s.tps_hit || []).join(",") || "—"}</b></div>
      </div>
      <div class="legs">${legs}</div>
    `;
    return card;
  }

  /* ------------------------------------------------------------------ feed */
  const ICONS = { message: "✉", intent: "→", action: "⚡", warn: "!", system: "◆" };
  const TYPE_BADGE = {
    ENTRY: "entry", MODIFY_SL: "", BREAKEVEN: "", PARTIAL_CLOSE: "",
    TP_HIT: "", CLOSE_ALL: "", NOISE: "noise",
  };

  function feedRow(it) {
    const row = document.createElement("div");
    row.className = `feeditem feeditem--${it.kind}`;
    const time = relTime(it.ts);

    let head = "", body = "";
    if (it.kind === "message") {
      head = `<span class="badge">Message</span>`;
      body = `<div class="feeditem__text">${esc(it.text) || "<i>media</i>"}</div>`;
    } else if (it.kind === "intent") {
      const variant = TYPE_BADGE[it.type] || "";
      head = `<span class="badge ${variant ? "badge--" + variant : ""}">${esc(it.type)}</span>` +
             (it.rule ? `<span class="feeditem__meta">${esc(it.rule)}</span>` : "");
      body = it.type !== "NOISE"
        ? `<div class="feeditem__meta">${esc(cleanDetail(it.detail))}</div>` : "";
    } else if (it.kind === "action") {
      head = `<span class="badge">${esc(it.action)}</span>` +
             (it.sim ? `<span class="feeditem__meta">sim</span>` : "");
      const bits = [];
      if (it.direction) bits.push(it.direction);
      if (it.ticket) bits.push("#" + it.ticket);
      if (it.volume != null) bits.push(it.volume + " lot");
      if (it.price != null) bits.push("@" + it.price);
      if (it.sl != null) bits.push("SL " + it.sl);
      if (it.tp != null) bits.push("TP " + it.tp);
      body = `<div class="feeditem__meta">${esc(bits.join(" · "))}</div>`;
    } else if (it.kind === "warn") {
      head = `<span class="badge badge--warn">Rejected</span>`;
      body = `<div class="feeditem__text dim">${esc(it.reason || "")}</div>`;
    } else {
      head = `<span class="badge">${esc(it.event)}</span>`;
    }

    row.innerHTML = `
      <div class="feeditem__icon">${ICONS[it.kind] || "·"}</div>
      <div class="feeditem__body">
        <div class="feeditem__head">${head}<span class="feeditem__time">${time}</span></div>
        ${body}
      </div>`;
    return row;
  }

  function cleanDetail(d) {
    if (!d) return "";
    return String(d).replace(/^<Intent\s+/, "").replace(/>$/, "")
      .replace(/type=\w+\s*/, "").replace(/rule=\w+\s*/, "").trim();
  }

  function addFeed(it, prepend = true) {
    const list = $("#feedList");
    $("#feedEmpty").classList.add("is-hidden");
    const row = feedRow(it);
    if (prepend) list.insertBefore(row, list.firstChild);
    else list.appendChild(row);
    feedCount++;
    while (list.querySelectorAll(".feeditem").length > MAX_FEED) {
      list.querySelector(".feeditem:last-child")?.remove();
    }
  }

  function renderFeedSnapshot(items) {
    const list = $("#feedList");
    list.querySelectorAll(".feeditem").forEach((n) => n.remove());
    if (!items.length) { $("#feedEmpty").classList.remove("is-hidden"); return; }
    $("#feedEmpty").classList.add("is-hidden");
    // newest first
    items.slice(-MAX_FEED).forEach((it) => addFeed(it, true));
  }

  /* -------------------------------------------------------- emergency stop */
  let estopOn = false;
  function setEstop(on) {
    estopOn = on;
    const btn = $("#estopBtn");
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    $("#estopLabel").textContent = on ? "Stopped · tap to resume" : "Emergency stop";
  }

  $("#estopBtn").addEventListener("click", async () => {
    const next = !estopOn;
    setEstop(next); // optimistic
    try {
      const r = await fetch("/api/control/emergency-stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      const data = await r.json();
      setEstop(!!data.emergency_stop);
      toast(data.emergency_stop ? "Emergency stop ENGAGED" : "Trading resumed");
    } catch (e) {
      setEstop(!next);
      toast("Action failed");
    }
  });

  /* --------------------------------------------------------------- network */
  async function loadInitial() {
    try {
      const [st, sig, fd, perf] = await Promise.all([
        fetch("/api/status").then((r) => r.json()),
        fetch("/api/signals").then((r) => r.json()),
        fetch("/api/feed").then((r) => r.json()),
        fetch("/api/performance").then((r) => r.json()).catch(() => null),
      ]);
      renderStatus(st);
      renderSignals(sig.signals || []);
      renderFeedSnapshot(fd.feed || []);
      if (perf && perf.performance) { renderPerformance(perf.performance); renderReview(perf.review_queue || 0); }
    } catch (e) {
      renderStatus({ online: false });
    }
  }

  let ws, retry = 0;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);

    ws.onopen = () => { retry = 0; };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      switch (msg.type) {
        case "status": renderStatus(msg.status); break;
        case "signals": renderSignals(msg.signals || []); break;
        case "feed": addFeed(msg.item, true); break;
        case "feed_snapshot": renderFeedSnapshot(msg.feed || []); break;
        case "emergency": setEstop(!!msg.enabled); break;
      }
    };
    ws.onclose = () => {
      $("#liveDot").style.opacity = "0.25";
      retry = Math.min(retry + 1, 6);
      setTimeout(connect, retry * 1000);
    };
    ws.onerror = () => ws.close();
  }

  // Keep relative times fresh.
  setInterval(() => {
    document.querySelectorAll(".feeditem__time").forEach((el) => {});
  }, 15000);

  loadInitial().then(connect);
})();
