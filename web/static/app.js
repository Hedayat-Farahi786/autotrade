/* ===========================================================================
   GTMO XAUUSD Dashboard — client
   Fetches initial state, then streams live updates over WebSocket.
   =========================================================================== */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const MAX_FEED = 60;
  let feedCount = 0;

  /* --------------------------------------------------------------- auth */
  const TOKEN_KEY = "gtmo_token";
  let token = localStorage.getItem(TOKEN_KEY) || "";

  const authedFetch = (url, opts = {}) => {
    const headers = Object.assign({}, opts.headers);
    if (token) headers["Authorization"] = "Bearer " + token;
    return fetch(url, Object.assign({}, opts, { headers }));
  };

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

  /* ----------------------------------------------- animated numbers */
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
  const _anim = new WeakMap();

  // Formatters
  const fMoney = (v) => Number(v).toLocaleString("en-US",
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fSigned = (v) => (v >= 0 ? "+" : "−") + fMoney(Math.abs(v));
  const fInt = (v) => String(Math.round(v));
  const fPct = (v) => Math.round(v) + "%";
  const fR = (v) => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(2) + "R";
  const fFloat = (v) => v.toFixed(2);

  function animateNumber(node, target, format) {
    if (!node) return;
    if (target == null || isNaN(target)) { node.textContent = "—"; _anim.delete(node); return; }
    const prev = _anim.get(node);
    const from = prev ? prev.value : target;
    if (reduceMotion || from === target) {
      node.textContent = format(target);
      _anim.set(node, { value: target });
      if (from !== target) flashDir(node, target >= from);
      return;
    }
    if (prev && prev.raf) cancelAnimationFrame(prev.raf);
    flashDir(node, target >= from);
    const dur = 650, t0 = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - t0) / dur);
      node.textContent = format(from + (target - from) * easeOutCubic(t));
      if (t < 1) {
        _anim.set(node, { value: target, raf: requestAnimationFrame(step) });
      } else {
        node.textContent = format(target);
        _anim.set(node, { value: target });
      }
    };
    _anim.set(node, { value: target, raf: requestAnimationFrame(step) });
  }

  function flashDir(node, up) {
    node.classList.remove("tick-up", "tick-down");
    void node.offsetWidth;
    node.classList.add(up ? "tick-up" : "tick-down");
  }

  const setStat = (el, target, format) => {
    if (!el) return;
    animateNumber(el.querySelector("[data-value]") || el, target, format);
  };
  const setPerf = (key, target, format) =>
    animateNumber(document.querySelector(`[data-perf="${key}"]`), target, format);

  /* --------------------------------------------------------------- status */
  function renderStatus(s) {
    // "up" = a bot is genuinely alive (fresh heartbeat, not stopped).
    const fresh = s && s.ts ? (Date.now() / 1000 - s.ts < 15) : !!(s && s.online);
    const up = !!(s && s.online && fresh && s.bot_running !== false);
    if (s) s = Object.assign({}, s, { online: up });

    document.body.dataset.state = up ? "ready" : "offline";

    const live = s && s.dry_run === false;
    const pill = $("#modePill");
    pill.dataset.mode = !up ? "" : live ? "live" : "dry";
    $("#modeLabel").textContent = !up
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

    // Drive the boot/connection screen + running indicator.
    updateConnection(s);

    if (!s || !s.online) return;

    setStat($("#statBalance"), s.balance, fMoney);
    setStat($("#statEquity"), s.equity, fMoney);
    $("#symbolTag").textContent = s.symbol || "";

    // Daily P/L (equity − start balance is unknown; use realized loss + float).
    const start = s.daily_start_balance || s.balance || 0;
    const pl = (s.equity != null && start) ? s.equity - start : -(s.daily_loss || 0);
    setStat($("#statPnl"), pl, fSigned);
    const sub = $("#pnlBar");
    // risk bar = how much of the daily loss budget is used
    const budget = (s.daily_start_balance || 0) * (s.max_daily_loss || 0.05);
    const used = budget ? Math.min(100, (s.daily_loss / budget) * 100) : 0;
    const bar = sub.querySelector(".risk-bar");
    sub.querySelector("i").style.width = used + "%";
    bar.classList.toggle("danger", s.halted);

    setStat($("#statSignals"), s.open_signals || 0, fInt);
    $("#positionsHint").textContent = `${s.open_positions || 0} position${(s.open_positions || 0) === 1 ? "" : "s"}`;

    if (s.performance) renderPerformance(s.performance);
    if (s.review_queue != null) renderReview(s.review_queue);
    if (s.intel_enabled != null) {
      $("#intelChip").classList.toggle("is-off", !s.intel_enabled);
    }

    // Emergency stop + pause buttons reflect live state.
    setEstop(!!s.emergency_stop);
    if (s.paused != null) setPaused(!!s.paused);
    if (s.halted) $("#equityHint").textContent = "Daily loss limit reached";
  }

  /* ----------------------------------------------------------- performance */
  function renderPerformance(p) {
    if (!p) return;
    const has = p.trades > 0;
    const tone = (key, on) => {
      const el = document.querySelector(`[data-perf="${key}"]`);
      if (el) { el.classList.toggle("up", on === 1); el.classList.toggle("down", on === -1); }
    };
    const dash = (key) => { const el = document.querySelector(`[data-perf="${key}"]`);
      if (el) { el.textContent = "—"; _anim.delete(el); } };

    setPerf("trades", p.trades ?? 0, fInt);
    if (has) setPerf("win_rate", (p.win_rate || 0) * 100, fPct); else dash("win_rate");

    if (p.profit_factor != null) { setPerf("profit_factor", p.profit_factor, fFloat);
      tone("profit_factor", p.profit_factor >= 1 ? 1 : -1); } else dash("profit_factor");

    if (has) { setPerf("expectancy", p.expectancy || 0, fSigned);
      tone("expectancy", (p.expectancy || 0) >= 0 ? 1 : -1); } else dash("expectancy");

    if (p.avg_r != null) { setPerf("avg_r", p.avg_r, fR);
      tone("avg_r", p.avg_r >= 0 ? 1 : -1); } else dash("avg_r");

    if (has) { setPerf("net_profit", p.net_profit || 0, fSigned);
      tone("net_profit", (p.net_profit || 0) >= 0 ? 1 : -1); } else dash("net_profit");

    if (has) setPerf("max_drawdown", p.max_drawdown || 0, fMoney); else dash("max_drawdown");

    const st = p.streak || 0;
    const stEl = document.querySelector('[data-perf="streak"]');
    if (stEl) {
      stEl.textContent = st === 0 ? "—" : (st > 0 ? `${st}W` : `${-st}L`);
      stEl.classList.toggle("up", st > 0); stEl.classList.toggle("down", st < 0);
    }
  }

  function renderReview(n) {
    const chip = $("#reviewChip");
    chip.textContent = `${n} to review`;
    chip.classList.toggle("is-off", !n);
  }

  /* --------------------------------------------------------- equity chart */
  function renderEquity(curve) {
    const canvas = $("#equityChart");
    if (!canvas) return;
    const net = curve && curve.length ? curve[curve.length - 1].equity : 0;
    $("#equityNet").textContent = curve && curve.length
      ? (net >= 0 ? "+" : "−") + fmtMoney(Math.abs(net)) : "—";

    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth || canvas.parentElement.clientWidth || 300;
    const h = 120;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);
    if (!curve || curve.length < 2) return;

    const vals = curve.map((p) => p.equity);
    let min = Math.min(0, ...vals), max = Math.max(0, ...vals);
    if (min === max) { max += 1; min -= 1; }
    const pad = 6;
    const x = (i) => pad + (i / (curve.length - 1)) * (w - pad * 2);
    const y = (v) => h - pad - ((v - min) / (max - min)) * (h - pad * 2);

    // Zero baseline.
    ctx.strokeStyle = "rgba(255,255,255,0.14)";
    ctx.lineWidth = 1; ctx.setLineDash([3, 4]);
    ctx.beginPath(); ctx.moveTo(pad, y(0)); ctx.lineTo(w - pad, y(0)); ctx.stroke();
    ctx.setLineDash([]);

    // Area fill.
    const grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, "rgba(255,255,255,0.18)");
    grad.addColorStop(1, "rgba(255,255,255,0)");
    ctx.beginPath();
    ctx.moveTo(x(0), y(vals[0]));
    curve.forEach((p, i) => ctx.lineTo(x(i), y(p.equity)));
    ctx.lineTo(x(curve.length - 1), y(0));
    ctx.lineTo(x(0), y(0));
    ctx.closePath(); ctx.fillStyle = grad; ctx.fill();

    // Line.
    ctx.beginPath();
    ctx.moveTo(x(0), y(vals[0]));
    curve.forEach((p, i) => ctx.lineTo(x(i), y(p.equity)));
    ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1.6;
    ctx.lineJoin = "round"; ctx.stroke();

    // End dot.
    ctx.beginPath();
    ctx.arc(x(curve.length - 1), y(net), 2.6, 0, Math.PI * 2);
    ctx.fillStyle = "#ffffff"; ctx.fill();
  }

  let lastCurve = null;
  window.addEventListener("resize", () => { if (lastCurve) renderEquity(lastCurve); });

  /* -------------------------------------------------------- trade history */
  function renderHistory(recent) {
    const list = $("#historyList");
    $("#historyCount").textContent = recent ? recent.length : 0;
    list.querySelectorAll(".histrow").forEach((n) => n.remove());
    if (!recent || !recent.length) {
      $("#historyEmpty").classList.remove("is-hidden");
      return;
    }
    $("#historyEmpty").classList.add("is-hidden");
    for (const t of recent.slice().reverse()) {
      const row = document.createElement("div");
      row.className = "histrow";
      const pl = t.profit || 0;
      const when = t.closed_at ? relTime(t.closed_at) : "";
      const r = t.r_multiple != null ? (t.r_multiple >= 0 ? "+" : "") + t.r_multiple + "R" : "";
      row.innerHTML = `
        <span class="histrow__time">${when}</span>
        <span class="histrow__sig"><b>#${esc(t.signal_id)}</b> ${esc(t.direction)} ${esc(t.label || "")} <span style="opacity:.5">${esc(t.reason || "")}</span></span>
        <span class="histrow__r">${esc(r)}</span>
        <span class="histrow__pl ${pl >= 0 ? "up" : "down"}">${pl >= 0 ? "+" : "−"}${fmtMoney(Math.abs(pl))}</span>`;
      list.appendChild(row);
    }
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

  /* --------------------------------------------------- connection / boot */
  let bootDismissed = false;
  let starting = false;

  function setConnRow(key, state) {
    const row = document.querySelector(`.connrow[data-key="${key}"]`);
    if (!row) return;
    row.classList.toggle("is-connecting", state === "connecting");
    row.classList.toggle("is-on", state === "on");
    const label = { idle: "Idle", connecting: "Connecting", on: "Connected" }[state];
    row.querySelector(".connrow__state").textContent = label;
  }

  function updateConnection(s) {
    const online = !!(s && s.online);
    const tg = !!(s && s.telegram_connected);
    const mt5 = !!(s && s.mt5_connected);
    const running = online || starting;
    const connectingPhase = starting || (s && s.connecting);

    setConnRow("telegram", tg ? "on" : (running ? "connecting" : "idle"));
    setConnRow("mt5", mt5 ? "on" : (running && tg ? "connecting" : (running ? "idle" : "idle")));
    setConnRow("engine", online ? "on" : (running ? "connecting" : "idle"));

    // Running chip in the top bar.
    const chip = $("#runChip");
    if (running) {
      chip.classList.remove("is-hidden");
      chip.dataset.mode = connectingPhase && !(tg && mt5) ? "connecting" : "live";
      $("#runLabel").textContent = (s && s.demo) ? "Demo live"
        : connectingPhase && !(tg && mt5) ? "Connecting" : "Running";
    } else {
      chip.classList.add("is-hidden");
    }
    $("#powerBtn").classList.toggle("is-hidden", !running);

    // Boot screen: hide once fully connected; show when nothing is running.
    const boot = $("#bootScreen");
    const fullyUp = online && tg && mt5;
    if (fullyUp) {
      if (!bootDismissed) { bootDismissed = true; boot.classList.add("is-gone"); }
    } else if (!running) {
      bootDismissed = false;
      boot.classList.remove("is-gone");
      $("#bootActions").style.display = "";
      $("#bootSub").textContent = "XAUUSD auto-execution";
      resetBootButtons();
    } else {
      // mid-connection: keep boot visible with the live checklist.
      boot.classList.remove("is-gone");
      $("#bootSub").textContent = "Establishing connections…";
      $("#bootActions").style.display = "none";
    }
  }

  function resetBootButtons() {
    starting = false;
    ["startDemoBtn", "startRealBtn"].forEach((id) => {
      const b = $("#" + id); b.classList.remove("is-busy"); b.disabled = false;
    });
  }

  async function startBot(mode) {
    if (starting) return;
    starting = true;
    const btn = mode === "demo" ? $("#startDemoBtn") : $("#startRealBtn");
    btn.classList.add("is-busy");
    $("#startDemoBtn").disabled = $("#startRealBtn").disabled = true;
    setConnRow("engine", "connecting");
    setConnRow("telegram", "connecting");
    try {
      const r = await authedFetch("/api/bot/start", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!r.ok) throw new Error("start failed");
      $("#bootActions").style.display = "none";
      $("#bootSub").textContent = "Establishing connections…";
      toast(mode === "demo" ? "Starting live demo…" : "Connecting to Telegram & MT5…");
    } catch (e) {
      resetBootButtons();
      toast("Could not start the bot");
    }
  }

  $("#startDemoBtn").addEventListener("click", () => startBot("demo"));
  $("#startRealBtn").addEventListener("click", () => startBot("real"));

  $("#powerBtn").addEventListener("click", async () => {
    if (!confirm("Stop the bot?")) return;
    try {
      await authedFetch("/api/bot/stop", { method: "POST" });
      toast("Bot stopped");
      starting = false; bootDismissed = false;
      renderStatus({ online: false });
    } catch (e) { toast("Action failed"); }
  });

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
      const r = await authedFetch("/api/control/emergency-stop", {
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

  /* ----------------------------------------------------------------- pause */
  let pausedOn = false;
  function setPaused(on) {
    pausedOn = on;
    const btn = $("#pauseBtn");
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    $("#pauseLabel").textContent = on ? "Paused" : "Pause";
    $("#pauseIcon").textContent = on ? "▶" : "⏸";
  }

  $("#pauseBtn").addEventListener("click", async () => {
    const next = !pausedOn;
    setPaused(next);
    try {
      const r = await authedFetch("/api/control/pause", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next }),
      });
      const data = await r.json();
      setPaused(!!data.paused);
      toast(data.paused ? "New entries paused" : "Entries resumed");
    } catch (e) { setPaused(!next); toast("Action failed"); }
  });

  $("#closeAllBtn").addEventListener("click", async () => {
    if (!confirm("Close ALL open positions now?")) return;
    try {
      await authedFetch("/api/control/close-all", { method: "POST" });
      toast("Close-all sent to bot");
    } catch (e) { toast("Action failed"); }
  });

  /* --------------------------------------------------------------- network */
  async function loadInitial() {
    try {
      const [st, sig, fd, perf] = await Promise.all([
        authedFetch("/api/status").then((r) => r.json()),
        authedFetch("/api/signals").then((r) => r.json()),
        authedFetch("/api/feed").then((r) => r.json()),
        authedFetch("/api/performance").then((r) => r.json()).catch(() => null),
      ]);
      renderStatus(st);
      renderSignals(sig.signals || []);
      renderFeedSnapshot(fd.feed || []);
      if (perf) {
        if (perf.performance) renderPerformance(perf.performance);
        renderReview(perf.review_queue || 0);
        lastCurve = perf.equity_curve || [];
        renderEquity(lastCurve);
        renderHistory(perf.recent || []);
      }
    } catch (e) {
      renderStatus({ online: false });
    }
  }

  async function refreshPerformance() {
    try {
      const perf = await authedFetch("/api/performance").then((r) => r.json());
      if (perf.performance) renderPerformance(perf.performance);
      renderReview(perf.review_queue || 0);
      lastCurve = perf.equity_curve || [];
      renderEquity(lastCurve);
      renderHistory(perf.recent || []);
    } catch (e) { /* ignore */ }
  }

  let ws, retry = 0;
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const q = token ? `?token=${encodeURIComponent(token)}` : "";
    ws = new WebSocket(`${proto}://${location.host}/ws${q}`);

    ws.onopen = () => { retry = 0; $("#liveDot").style.opacity = "1"; };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      switch (msg.type) {
        case "status": renderStatus(msg.status); break;
        case "signals":
          renderSignals(msg.signals || []);
          refreshPerformance();  // a position change likely closed a trade
          break;
        case "feed":
          addFeed(msg.item, true);
          if (msg.item && msg.item.kind === "action") refreshPerformance();
          break;
        case "feed_snapshot": renderFeedSnapshot(msg.feed || []); break;
        case "emergency": setEstop(!!msg.enabled); break;
        case "paused": setPaused(!!msg.enabled); break;
        case "bot": if (msg.state && !msg.state.running) { starting = false; bootDismissed = false; } break;
      }
    };
    ws.onclose = () => {
      $("#liveDot").style.opacity = "0.25";
      retry = Math.min(retry + 1, 6);
      setTimeout(connect, retry * 1000);
    };
    ws.onerror = () => ws.close();
  }

  /* ------------------------------------------------------------ auth gate */
  async function checkAuth() {
    try {
      const r = await authedFetch("/api/auth").then((x) => x.json());
      return !r.required || r.ok;
    } catch (e) { return true; }  // if unreachable, let the app try anyway
  }

  function showLogin(err) {
    const gate = $("#loginGate");
    gate.classList.remove("is-hidden");
    $("#loginError").textContent = err || "";
    $("#tokenInput").focus();
  }

  $("#loginForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    token = $("#tokenInput").value.trim();
    if (!(await checkAuth())) { showLogin("Invalid token"); return; }
    localStorage.setItem(TOKEN_KEY, token);
    $("#loginGate").classList.add("is-hidden");
    boot();
  });

  async function boot() {
    await loadInitial();
    connect();
  }

  (async () => {
    if (await checkAuth()) boot();
    else showLogin();
  })();
})();
