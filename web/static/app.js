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
  $("#startRealBtn").addEventListener("click", () => Setup.open());
  $("#settingsBtn").addEventListener("click", () => Setup.open());

  /* ----------------------------------------------- Telegram connect wizard */
  const Wizard = (() => {
    const wiz = $("#tgWizard");
    let dialogs = [], me = null, onDone = null;

    const err = (m) => {
      const e = $("#wizErr"); e.textContent = m || "";
      if (m) { e.classList.remove("show"); void e.offsetWidth; e.classList.add("show"); }
    };
    const busy = (btn, on) => { if (btn) { btn.classList.toggle("is-busy", on); btn.disabled = on; } };

    function dots(cur) {
      const order = ["creds", "code", "channel", "ready"];
      const key = cur === "password" ? "code" : cur;
      const idx = order.indexOf(key);
      $("#wizDots").innerHTML = order.map((s, i) =>
        `<i class="${i < idx ? "is-done" : ""} ${i === idx ? "is-active" : ""}"></i>`).join("");
    }
    function show(step) {
      wiz.querySelectorAll(".wizstep").forEach((s) =>
        s.classList.toggle("is-hidden", s.dataset.step !== step));
      dots(step); err("");
      const inp = wiz.querySelector(`.wizstep[data-step="${step}"] input`);
      if (inp) setTimeout(() => inp.focus(), 60);
    }

    async function open(doneCb) {
      onDone = doneCb || null;
      wiz.classList.remove("is-hidden");
      err(""); show("creds");
      try {
        const st = await authedFetch("/api/telegram/state").then((r) => r.json());
        if (!st.telethon) { err("telethon is not installed on the server — `pip install telethon`."); return; }
        if (st.phone) $("#wizPhone").value = st.phone;
        if (st.authorized) { me = st.me; st.channel ? gotoReady(st.channel) : gotoChannel(); }
      } catch (e) { /* stay on creds */ }
    }
    function close() { wiz.classList.add("is-hidden"); }

    // Step 1 — credentials → send code
    $("#wizCredsForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.submitter || $("#wizCredsForm button");
      const body = {
        api_id: $("#wizApiId").value.trim(),
        api_hash: $("#wizApiHash").value.trim(),
        phone: $("#wizPhone").value.trim(),
      };
      if (!body.api_id || !body.api_hash || !body.phone) { err("Fill in all three fields."); return; }
      busy(btn, true);
      try {
        const r = await post("/api/telegram/connect", body);
        if (r.authorized) { me = r.me; gotoChannel(); }
        else show("code");
      } catch (ex) { err(ex.message); } finally { busy(btn, false); }
    });

    // Step 2 — code
    $("#wizCodeForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.submitter; busy(btn, true);
      try {
        const r = await post("/api/telegram/code", { code: $("#wizCode").value });
        if (r.step === "password") show("password");
        else { me = r.me; gotoChannel(); }
      } catch (ex) { err(ex.message); } finally { busy(btn, false); }
    });

    // Step 3 — 2FA password
    $("#wizPwForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = e.submitter; busy(btn, true);
      try {
        const r = await post("/api/telegram/password", { password: $("#wizPw").value });
        me = r.me; gotoChannel();
      } catch (ex) { err(ex.message); } finally { busy(btn, false); }
    });

    // Step 4 — channel picker
    async function gotoChannel() {
      show("channel");
      $("#wizMe").textContent = me
        ? `Connected as @${me.username || me.first_name || "you"} — choose a channel:`
        : "Choose the channel to listen to:";
      $("#wizList").innerHTML = '<div class="wiz__loading">Loading your channels…</div>';
      try {
        const r = await authedFetch("/api/telegram/dialogs").then((x) => x.json());
        dialogs = r.dialogs || [];
        renderDialogs("");
      } catch (e) { $("#wizList").innerHTML = '<div class="wiz__loading">Could not load channels.</div>'; }
    }
    function renderDialogs(filter) {
      const f = (filter || "").toLowerCase();
      const items = dialogs.filter((d) => !f ||
        (d.title || "").toLowerCase().includes(f) || (d.username || "").toLowerCase().includes(f));
      const list = $("#wizList");
      if (!items.length) { list.innerHTML = '<div class="wiz__loading">No channels found.</div>'; return; }
      list.innerHTML = items.map((d) => `
        <div class="wizitem" data-peer="${esc(d.peer)}" data-title="${esc(d.title)}">
          <span class="wizitem__av">${esc((d.title || "?").slice(0, 1).toUpperCase())}</span>
          <span class="wizitem__main">
            <span class="wizitem__title">${esc(d.title)}</span>
            <span class="wizitem__meta">${d.username ? "@" + esc(d.username) : esc(d.type)}${d.participants ? " · " + fInt(d.participants) + " members" : ""}</span>
          </span>
        </div>`).join("");
      list.querySelectorAll(".wizitem").forEach((el) =>
        el.addEventListener("click", () => selectChannel(el.dataset.peer, el.dataset.title)));
    }
    $("#wizSearch").addEventListener("input", (e) => renderDialogs(e.target.value));

    async function selectChannel(peer, title) {
      try {
        await post("/api/telegram/select-channel", { channel: peer });
        gotoReady(title || peer);
      } catch (ex) { err(ex.message); }
    }

    // Step 5 — ready → start the real bot (or return to the Setup guide)
    function gotoReady(channel) {
      show("ready");
      $("#wizReady").innerHTML = me
        ? `Connected as <b>@${esc(me.username || me.first_name || "you")}</b><br>Listening to <b>${esc(channel)}</b>`
        : `Listening to <b>${esc(channel)}</b>`;
      $("#wizStart").querySelector("span:last-child").textContent =
        onDone ? "Done" : "Start the bot";
    }
    $("#wizChangeChan").addEventListener("click", gotoChannel);
    $("#wizStart").addEventListener("click", async (e) => {
      if (onDone) { close(); onDone(); return; }
      busy(e.currentTarget, true);
      close();
      await startBot("real");
    });

    // Helpers + close interactions
    async function post(url, body) {
      const r = await authedFetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || "Request failed");
      return data;
    }
    $("#wizClose").addEventListener("click", close);
    wiz.addEventListener("click", (e) => { if (e.target === wiz) close(); });

    return { open, close };
  })();

  /* --------------------------------------------------- First-time Setup guide */
  const Setup = (() => {
    const el = $("#setup");
    let state = null;

    const setBusy = (btn, on) => { if (btn) { btn.classList.toggle("is-busy", on); btn.disabled = on; } };
    async function post(url, body) {
      const r = await authedFetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "Request failed");
      return d;
    }

    function openConnector(key) {
      el.querySelectorAll(".connector").forEach((c) => c.classList.toggle("is-open", c.dataset.key === key));
    }
    el.querySelectorAll("[data-toggle]").forEach((btn) =>
      btn.addEventListener("click", () => {
        const c = btn.closest(".connector");
        const wasOpen = c.classList.contains("is-open");
        el.querySelectorAll(".connector").forEach((x) => x.classList.remove("is-open"));
        if (!wasOpen) c.classList.add("is-open");
      }));

    function status(key, text, st) {
      const node = el.querySelector(`.connector[data-key="${key}"] [data-status]`);
      if (node) { node.textContent = text; node.dataset.state = st || ""; }
      el.querySelector(`.connector[data-key="${key}"]`).classList.toggle("is-done", st === "ok");
    }
    function setMode(mode) {
      el.querySelectorAll("#suMode .seg__opt").forEach((o) => o.classList.toggle("is-active", o.dataset.mode === mode));
    }

    async function refresh() {
      try { state = await authedFetch("/api/setup/state").then((r) => r.json()); render(); }
      catch (e) { /* ignore */ }
    }
    function render() {
      if (!state) return;
      const tg = state.telegram, m = state.mt5, ai = state.ai;
      const dis = $("#suTgDisconnect");
      if (tg.connected && tg.channel) {
        status("telegram", "Connected", "ok");
        $("#suTgSum").innerHTML = `Connected as <b>@${esc((tg.me && tg.me.username) || "you")}</b> · listening to <b>${esc(tg.channel)}</b>`;
        $("#suTgBtn").textContent = "Change account / channel";
        dis.classList.remove("is-hidden");
      } else if (tg.connected) {
        status("telegram", "Pick channel", "warn");
        dis.classList.remove("is-hidden");
      } else {
        status("telegram", tg.available ? "Pending" : "Needs telethon", "");
        $("#suTgBtn").textContent = "Connect Telegram";
        dis.classList.add("is-hidden");
      }

      if (m.configured) status("mt5", "Configured", "ok");
      else status("mt5", m.available ? "Pending" : "Windows only", "");

      status("ai", ai.has_key ? "Key set" : "Regex", ai.has_key ? "ok" : "warn");
      if (ai.provider) $("#suAiProvider").value = ai.provider;
      if (ai.mode) $("#suAiMode").value = ai.mode;

      setMode(state.dry_run ? "dry" : "live");
      status("mode", state.dry_run ? "Dry-run" : "Live", state.dry_run ? "" : "warn");
      $("#suModeNote").textContent = state.dry_run
        ? "Dry-run simulates orders on the built-in simulator — no real money. Recommended until everything is verified."
        : "⚠ Live trading places REAL orders with real money on your MT5 account.";

      $("#setupStart").disabled = !state.ready;
      $("#setupReady").textContent = state.ready ? "Everything's ready — start the bot."
        : !tg.connected ? "Connect Telegram to continue"
        : !tg.channel ? "Choose a channel to listen to"
        : (!state.dry_run && !m.configured) ? "Connect MetaTrader 5 for live trading"
        : "Almost there…";
    }

    function noteResult(sel, text, ok) {
      const n = $(sel); n.textContent = text;
      n.classList.remove("ok", "bad"); n.classList.add(ok ? "ok" : "bad");
      n.classList.add("conn__result");
    }

    // Telegram
    $("#suTgBtn").addEventListener("click", () =>
      Wizard.open(() => { refresh(); openConnector("mt5"); }));
    $("#suTgDisconnect").addEventListener("click", async () => {
      if (!confirm("Disconnect this Telegram account? You'll need to sign in again.")) return;
      try { await post("/api/telegram/logout", {}); toast("Telegram disconnected"); await refresh(); }
      catch (e) { toast("Could not disconnect"); }
    });

    // MT5 — test & save
    $("#suMtBtn").addEventListener("click", async (e) => {
      setBusy(e.currentTarget, true);
      try {
        const r = await post("/api/mt5/test", {
          login: $("#suMtLogin").value.trim(), password: $("#suMtPass").value,
          server: $("#suMtServer").value.trim(), symbol: $("#suMtSymbol").value.trim(),
          terminal_path: $("#suMtPath").value.trim(),
        });
        noteResult("#suMtNote", r.ok
          ? `✓ Connected — balance ${fMoney(r.balance)} on ${r.symbol}`
          : "✗ " + (r.error || "Connection failed"), r.ok);
        await refresh();
        if (r.ok) openConnector("ai");
      } catch (ex) { noteResult("#suMtNote", "✗ " + ex.message, false); }
      finally { setBusy(e.currentTarget, false); }
    });

    // AI — save
    $("#suAiBtn").addEventListener("click", async () => {
      const body = { mode: $("#suAiMode").value, provider: $("#suAiProvider").value };
      const key = $("#suAiKey").value.trim();
      if (key) body[body.provider === "gemini" ? "gemini_api_key" : "anthropic_api_key"] = key;
      try { await post("/api/ai/save", body); toast("AI settings saved"); await refresh(); openConnector("mode"); }
      catch (e) { toast("Save failed"); }
    });

    // Mode toggle
    el.querySelectorAll("#suMode .seg__opt").forEach((o) =>
      o.addEventListener("click", async () => {
        const dry = o.dataset.mode === "dry";
        if (!dry && !confirm("Live trading places REAL orders with real money. Continue?")) return;
        setMode(o.dataset.mode);
        try { await post("/api/setup/dry-run", { dry_run: dry }); await refresh(); } catch (e) { /* ignore */ }
      }));

    // Start
    $("#setupStart").addEventListener("click", async (e) => {
      setBusy(e.currentTarget, true);
      close();
      await startBot("real");
    });
    $("#setupClose").addEventListener("click", close);
    el.addEventListener("click", (e) => { if (e.target === el) close(); });

    function open() {
      el.classList.remove("is-hidden");
      refresh().then(() => {
        const firstPending = ["telegram", "mt5", "ai", "mode"].find((k) => {
          const c = el.querySelector(`.connector[data-key="${k}"]`);
          return c && !c.classList.contains("is-done");
        });
        openConnector(firstPending || "telegram");
      });
    }
    function close() { el.classList.add("is-hidden"); }
    return { open, close, refresh };
  })();

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
