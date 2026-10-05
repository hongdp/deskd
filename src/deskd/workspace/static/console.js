"use strict";

// All shared work is untrusted text. Build nodes explicitly; never parse it as HTML.
(() => {
  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = String(text);
    return element;
  };
  const state = {
    paired: false, connected: false, snapshot: null, view: "overview", compose: "task",
    pending: false, refreshing: false, lastUpdated: null, timer: null, toastTimer: null,
    intent: null, rendered: null, proof: null, epoch: 0, extended: null,
    goalIntent: null, goalRequests: new Map(), knowledgeResults: null,
  };
  const labels = {
    queued: "排队中", delivering: "正在送达", delivered: "已送达", handled: "已处理",
    unknown: "结果待核对", idle: "待命", busy: "处理中", active: "进行中",
    running: "处理中", paused: "已暂停", revoked: "已停用", blocked: "等待协助",
    done: "已完成", cancelled: "已取消", completed: "本轮结束", failed: "本轮未完成",
    pending: "待独立复核", approved: "已授权，待执行", executed: "已发布",
    expired: "已过期", consumed: "已使用", superseded: "已失效", unread: "未读", read: "已读",
  };
  const views = {
    overview: ["总览", "每件事，都有着落", "工作台总览", "交办新任务，看看团队的进展。"],
    tasks: ["任务", "从交办到完成", "团队任务", "明确负责人，跟进每一项交付。"],
    messages: ["消息", "保持沟通", "我的消息", "与你的团队直接对话，补充信息或调整方向。"],
    reviews: ["待复核", "让决策有所依据", "提案与独立复核", "先看清提案，再交给有权限的另一角色复核。"],
    results: ["成果", "把工作留在这里", "团队成果", "查看共享备忘录与已完成任务。"],
    activity: ["动态", "进展清晰可见", "工作动态", "任务、消息和调度的重要变化。"],
    goals: ["持续目标", "交代目标，持续推进", "持续目标", "让团队完成调研、独立复核与交付，并在需要时主动找你。"],
    notifications: ["提醒", "只在需要时打扰", "需要你关注", "重要变化集中在这里，相同状态保持安静。"],
    sources: ["信息来源", "让工作有据可查", "允许读取的来源", "明确团队可以获取哪些公开信息。"],
    knowledge: ["共享知识", "积累可以复用的经验", "团队共享知识", "查找角色主动分享的结论与工作笔记。"],
    operations: ["运行维护", "持续、可控地运行", "运行与恢复", "查看健康状态、额度与恢复边界。"],
  };
  const eventLabels = {
    "task.created": "创建了任务", "task.updated": "更新了任务进度", "task.cancelled": "取消了任务",
    "mail.sent": "发送了消息", "message.queued": "消息已进入队列", "message.handled": "已处理消息",
    "inbox.ack": "已确认处理消息", "inbox.acked": "已确认处理消息", "timer.fired": "定时工作已进入队列",
    "timer.scheduled": "设置了定时工作", "timer.cancelled": "取消了定时工作",
    "seat.paused": "暂停了后续自动唤醒", "seat.resumed": "恢复了自动唤醒",
    "seat.revoked": "停用了角色", "seat.budget": "更新了自动唤醒额度",
    "dispatch.claimed": "开始投递工作", "dispatch.delivered": "工作已送达",
    "dispatch.completed": "本轮工作已结束", "dispatch.unknown": "投递结果需要核对",
    "dispatch.reconciled": "核对了投递结果", "turn.completed": "本轮工作已结束",
    "service.started": "工作台已启动", "service.activated": "自动调度已启用", "service.fenced": "自动调度已暂停",
    "operator.task": "你交办了任务", "operator.message": "你发送了消息", "operator.read": "你已读消息",
    "supervisor.message": "发来了消息", "console.task": "你交办了任务", "console.message": "你发送了消息",
    "console.read": "你已读消息", "console.cancel": "你取消了任务",
    "memo.proposed": "提交了共享备忘录提案", "memo.approved": "独立授权了提案", "memo.published": "发布了共享备忘录",
    "review.requested": "请求了独立复核",
    "goal.created": "创建了持续目标", "goal.active": "恢复了目标推进", "goal.paused": "暂停了目标推进",
    "goal.completed": "完成了目标交付", "goal.cancelled": "取消了持续目标", "goal.waiting_human": "目标需要你的决定",
    "source.configured": "配置了允许读取的信息来源", "source.disabled": "停用了信息来源",
    "memory.published": "分享了工作笔记", "memory.revised": "更新了角色笔记", "memory.forgotten": "删除了角色笔记",
  };
  const errors = {
    session_required: "访问已到期，请重新连接工作台。",
    pairing_busy: "当前连接请求较多，请稍后再试。",
    version_conflict: "这项内容刚刚发生变化。已刷新记录，请查看最新状态后再操作。",
    task_terminal: "任务已经结束，不能再次取消。", task_closed: "任务已经结束，不能再次修改。",
    principal_revoked: "该角色已停用，请选择其他角色。", seat_revoked: "该角色已停用，请选择其他角色。",
    recipient_not_found: "接收角色已不存在，请刷新并重新选择。",
    invalid_body: "请填写有效的内容。", invalid_title: "请填写较短的任务标题。",
    inbox_full: "该角色的待处理消息已满，请等待处理后再试。",
    request_id_conflict: "这次提交与之前的内容不一致，请刷新后核对记录。",
    request_conflict: "这次提交与之前的内容不一致，请刷新后核对记录。",
    proposal_not_found: "该提案已不可用，请刷新提案列表。",
    proposal_content_mismatch: "提案内容与当前记录不一致，请刷新并重新查看原文。",
    proposal_already_executed: "这项提案已经执行，无需再次请求复核。",
    independent_reviewer_required: "复核角色必须不同于指定执行人，请重新选择。",
    reviewer_not_authorized: "该角色目前没有复核权限，请刷新后重新选择。",
    goal_requires_independent_participants: "调研、复核与交付须由三位不同角色负责。",
    invalid_source_ids: "请至少选择一个已配置的信息来源。", goal_interval_required: "重复执行的目标需要指定间隔。",
    goal_state_conflict: "目标状态已经变化，请查看最新进展后再操作。", goal_closed: "目标已经结束，不能继续修改。",
    source_url_rejected: "请输入不含登录信息、查询参数或片段的公开 HTTPS 地址。",
    invalid_source_name: "来源名称须以小写字母开头，只包含小写字母、数字、短横线或下划线。",
    source_address_rejected: "该地址不属于允许读取的公开网络。", unknown_goal: "该目标已不可用，请刷新。",
    source_configuration_limit: "信息来源已达到 100 个配置的上限，请使用已有来源。",
    goal_participant_not_authorized: "所选角色没有完成对应阶段所需的权限，请选择其他角色或请管理员调整权限。",
    task_not_owned: "这里只能取消你交办的任务。",
    forbidden_origin: "无法从当前页面操作，请使用启动终端给出的本地工作台地址。",
    outcome_unknown: "连接中断，尚不能确认操作是否完成。请先核对最新记录，避免重复操作。",
    status_unavailable: "暂时无法获取工作台状态。保留的内容可能已过时。",
    console_unavailable: "工作台暂时不可用，请检查启动终端。",
  };
  const blockedReasons = {
    participant_revoked: "参与角色已停用，需要管理员调整安排。",
    seat_budget_exhausted: "负责角色的自动工作次数已用完，需要补充额度。",
    task_cancelled: "当前阶段的任务已取消，需要重新确认目标安排。",
    followup_budget_exhausted: "已达到自动跟进次数上限，需要你查看进展后决定下一步。",
    approval_unavailable: "缺少可用的独立授权，需要复核角色确认。",
    approval_revoked: "独立授权已被撤销，当前不能继续交付。",
    approval_expired: "独立授权已过期，需要重新复核后才能交付。",
    delivery_mismatch: "交付记录与本轮复核凭据不一致，需要人工核实。",
  };

  function randomId() {
    return Array.from(crypto.getRandomValues(new Uint8Array(32)), (value) => value.toString(16).padStart(2, "0")).join("");
  }

  function sessionProof(reset = false) {
    // This per-tab application proof is generated locally. It is never shown or put in a URL/cookie.
    let proof = null;
    try {
      if (!reset) proof = sessionStorage.getItem("deskd.console.session");
      if (reset) sessionStorage.removeItem("deskd.console.session");
    } catch (_) { /* Private browser storage may be unavailable; memory still works. */ }
    if (!proof || !/^[0-9a-f]{64}$/.test(proof)) proof = randomId();
    try { sessionStorage.setItem("deskd.console.session", proof); } catch (_) { /* Keep in memory only. */ }
    state.proof = proof;
  }

  async function api(path, body) {
    const epoch = state.epoch;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    const options = {
      method: body === undefined ? "GET" : "POST", credentials: "omit", cache: "no-store",
      signal: controller.signal, headers: { "X-Deskd-Session": state.proof },
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.headers["X-Deskd-Console"] = "1";
      options.body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, options);
      if (epoch !== state.epoch) throw { code: "session_changed" };
      let value;
      try { value = await response.json(); } catch (_) { throw { code: "console_unavailable", status: response.status }; }
      if (epoch !== state.epoch) throw { code: "session_changed" };
      if (!response.ok || value.ok === false) throw { code: value.error?.code || "console_unavailable", status: response.status };
      return value.ok === true ? value.result : value;
    } catch (error) {
      if (epoch !== state.epoch) throw { code: "session_changed" };
      if (error.status === 401 && state.paired) clearAccess("访问已到期，请在终端重新确认连接。");
      throw error.code ? error : { code: body === undefined ? "status_unavailable" : "outcome_unknown" };
    } finally {
      clearTimeout(timeout);
    }
  }

  function errorText(error) { return errors[error.code] || "这次操作未完成。请刷新状态后再试。"; }
  function roleName(principal) {
    if (principal === "@supervisor") return "我";
    const name = String(principal || "").split("/").pop();
    return ({ analyst: "分析师", trader: "执行员", engineer: "工程师", reviewer: "复核员", operator: "执行员" })[name] || name || "工作台";
  }
  function timeText(value, full = false) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    const date = new Date(Number(value) * 1000);
    if (Number.isNaN(date.getTime())) return "—";
    return new Intl.DateTimeFormat("zh-CN", full
      ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }
      : { hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }
  function statusBadge(value) {
    const badge = node("span", "badge", labels[value] || "状态待核对");
    badge.dataset.tone = ["active", "handled", "done", "approved", "executed", "idle", "read", "consumed"].includes(value) ? "good"
      : ["unknown", "blocked", "paused", "pending", "expired"].includes(value) ? "warn"
      : ["revoked", "failed"].includes(value) ? "danger" : "neutral";
    return badge;
  }
  function button(text, callback, className = "text-button", key) {
    const result = node("button", className, text);
    result.type = "button";
    if (key) result.dataset.focusKey = key;
    result.addEventListener("click", callback);
    return result;
  }
  function commandButton(text, callback, className, key) {
    const result = button(text, callback, className, key);
    result.dataset.requiresConnection = "true";
    result.disabled = state.pending || !state.connected;
    return result;
  }
  function empty(title, description, symbol = "◌") {
    const result = node("div", "empty");
    const glyph = node("span", "empty-symbol", symbol);
    glyph.setAttribute("aria-hidden", "true");
    result.append(glyph, node("strong", "", title), node("p", "", description));
    return result;
  }
  function replaceList(selector, children) {
    const root = $(selector);
    const active = root.contains(document.activeElement) ? document.activeElement.dataset.focusKey : null;
    const expanded = new Set(Array.from(root.querySelectorAll("details[open][data-expand-key]")).map((item) => item.dataset.expandKey));
    root.replaceChildren(...children);
    root.querySelectorAll("details[data-expand-key]").forEach((item) => { item.open = expanded.has(item.dataset.expandKey); });
    if (active) Array.from(root.querySelectorAll("[data-focus-key]")).find((item) => item.dataset.focusKey === active)?.focus({ preventScroll: true });
  }
  function toast(text, isError = false) {
    clearTimeout(state.toastTimer);
    $("#toast").textContent = text;
    $("#toast").dataset.error = String(isError);
    $("#toast").hidden = false;
    state.toastTimer = setTimeout(() => { $("#toast").hidden = true; }, isError ? 10000 : 6000);
  }
  function feedback(selector, text, isError = false) {
    $(selector).textContent = text;
    $(selector).dataset.error = String(isError);
  }
  function setConnection(text, connected = false) {
    $("#connection").textContent = text;
    $("#connection").dataset.state = connected ? "connected" : "disconnected";
  }
  function updateButtons() {
    $("#composer").setAttribute("aria-busy", String(state.pending));
    $$('[data-requires-connection="true"]').forEach((item) => { item.disabled = state.pending || !state.connected; });
    const recipient = state.snapshot?.seats.find((seat) => seat.principal === $("#recipient").value);
    $("#send").disabled = state.pending || !state.connected || !recipient || Boolean(recipient.revoked);
    $("#send").textContent = state.pending ? "正在提交…" : state.compose === "task" ? "交办任务 ↗" : "发送消息 ↗";
  }

  function clearAccess(message = "") {
    state.epoch += 1;
    state.paired = false;
    state.connected = false;
    state.snapshot = null;
    state.rendered = null;
    state.intent = null;
    state.extended = null;
    state.goalIntent = null;
    state.goalRequests.clear();
    state.knowledgeResults = null;
    state.pending = false;
    $("#console-content").hidden = true;
    $("#pairing").hidden = false;
    $("#pairing-description").textContent = message || "先在本机终端确认访问，再查看团队的任务、消息与成果。";
    $("#logout").hidden = true;
    $("#pair").hidden = false;
    $("#pairing-code-wrap").hidden = true;
    $("#detail-dialog").close();
    $("#detail-title").textContent = "";
    $("#detail-body").replaceChildren();
    $("#detail-actions").replaceChildren();
    clearTimeout(state.toastTimer);
    $("#toast").hidden = true;
    $("#toast").textContent = "";
    ["#metrics", "#attention", "#roles", "#recent-tasks", "#tasks-list", "#messages-list", "#reviews-list", "#approvals-list", "#results-list", "#activity-list", "#goals-list", "#sources-list", "#knowledge-list", "#notifications-list", "#health-metrics", "#health-detail", "#goal-sources"].forEach((selector) => $(selector).replaceChildren());
    ["#nav-tasks", "#nav-messages", "#nav-reviews", "#nav-goals", "#nav-notifications"].forEach((selector) => { $(selector).textContent = "0"; });
    $("#recipient").replaceChildren();
    delete $("#recipient").dataset.signature;
    const allMessages = node("option", "", "全部角色"); allMessages.value = "all";
    $("#message-filter").replaceChildren(allMessages);
    $("#task-title").value = "";
    $("#compose-body").value = "";
    ["#goal-title", "#goal-objective", "#source-name", "#source-url", "#knowledge-query"].forEach((selector) => { $(selector).value = ""; });
    ["#goal-researcher", "#goal-reviewer", "#goal-executor"].forEach((selector) => { $(selector).replaceChildren(); delete $(selector).dataset.signature; });
    delete $("#goal-sources").dataset.signature;
    ["#goal-feedback", "#source-feedback", "#knowledge-feedback"].forEach((selector) => feedback(selector, ""));
    document.title = "deskd · 工作台";
    feedback("#compose-feedback", "");
    feedback("#pairing-feedback", message, Boolean(message));
    setConnection("尚未连接");
    updateButtons();
  }

  function showSession(session) {
    if (session.state === "paired") {
      state.paired = true;
      $("#pairing").hidden = true;
      $("#console-content").hidden = false;
      $("#logout").hidden = false;
      feedback("#pairing-feedback", "");
      return;
    }
    if (state.paired) clearAccess();
    const pending = session.state === "pending";
    $("#pairing-code-wrap").hidden = !pending;
    $("#pair").hidden = pending;
    $("#pairing-code").textContent = pending ? session.pairing_id : "";
    feedback("#pairing-feedback", pending ? "等待终端确认… 编号在两分钟内有效。" : "");
    setConnection(pending ? "等待确认" : "尚未连接");
  }

  async function refresh() {
    if (state.refreshing) return;
    state.refreshing = true;
    const epoch = state.epoch;
    clearTimeout(state.timer);
    if (state.paired && (!state.lastUpdated || Date.now() - state.lastUpdated > 15000)) {
      state.connected = false;
      setConnection("正在核对最新状态");
      updateButtons();
    }
    try {
      if (!state.paired) showSession(await api("/api/session"));
      if (state.paired) {
        const [snapshot, extended] = await Promise.all([api("/api/snapshot"), api("/api/workspace")]);
        if (!snapshot || !Array.isArray(snapshot.seats) || !Array.isArray(snapshot.tasks) || !Array.isArray(snapshot.messages)) throw { code: "status_unavailable" };
        if (!extended || !Array.isArray(extended.goals) || !Array.isArray(extended.sources)) throw { code: "status_unavailable" };
        state.snapshot = snapshot;
        state.extended = extended;
        state.connected = true;
        state.lastUpdated = Date.now();
        render(snapshot);
      }
    } catch (error) {
      if (epoch !== state.epoch || error.code === "session_changed") return;
      state.connected = false;
      setConnection(state.paired ? "连接中断 · 内容可能过时" : "连接不可用");
      if (state.paired) {
        $("#banner").hidden = false;
        $("#banner").dataset.tone = "warn";
        $("#banner").textContent = "暂时无法连接工作台。以下是上次获取的记录，当前状态尚未确认；操作已暂时停用。";
      } else feedback("#pairing-feedback", errorText(error), true);
    } finally {
      updateButtons();
      state.refreshing = false;
      state.timer = setTimeout(refresh, state.paired ? 4000 : 2000);
    }
  }

  async function command(name, params, success, onSuccess) {
    if (!state.connected || state.pending) return false;
    state.pending = true;
    const epoch = state.epoch;
    updateButtons();
    try {
      const result = await api("/api/commands", { command: name, params });
      clearTimeout(state.toastTimer);
      $("#toast").hidden = true;
      $("#toast").textContent = "";
      if (onSuccess) onSuccess(result);
      if (success) toast(success);
      return true;
    } catch (error) {
      if (epoch !== state.epoch || error.code === "session_changed") throw { code: "session_changed" };
      toast(errorText(error), true);
      throw error;
    } finally {
      if (epoch === state.epoch) {
        state.pending = false;
        await refresh();
        updateButtons();
      }
    }
  }

  function switchView(view, moveFocus = false) {
    if (!views[view]) view = "overview";
    state.view = view;
    const [name, eyebrow, title, description] = views[view];
    $("#page-name").textContent = name;
    $("#page-eyebrow").textContent = eyebrow;
    $("#page-title").textContent = title;
    $("#page-description").textContent = description;
    $$("[data-view]").forEach((item) => {
      if (item.dataset.view === view) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
    $$(".view").forEach((item) => { item.hidden = item.id !== "view-" + view; });
    if (location.hash !== "#" + view) history.replaceState(null, "", "#" + view);
    if (moveFocus) $("#main").focus({ preventScroll: true });
  }

  function setCompose(mode) {
    state.compose = mode;
    $$("[data-compose]").forEach((item) => item.setAttribute("aria-pressed", String(item.dataset.compose === mode)));
    $("#title-field").hidden = mode !== "task";
    $("#task-title").required = mode === "task";
    $("#compose-body").required = mode === "message";
    $("#compose-body").placeholder = mode === "task" ? "补充背景、交付要求或截止时间…" : "补充信息、询问进展，或告诉这个角色你的新想法…";
    $("#body-label").textContent = mode === "task" ? "任务要求" : "消息内容";
    $("#compose-hint").textContent = mode === "task" ? "提交后进入任务队列，完成进展以任务状态为准。" : "消息进入队列；已送达不等于已处理。";
    feedback("#compose-feedback", "");
    updateButtons();
  }

  function populateRoles(seats) {
    const signature = JSON.stringify(seats.map((seat) => [seat.principal, seat.revoked]));
    if ($("#recipient").dataset.signature === signature) return;
    const previous = $("#recipient").value;
    const messageRole = $("#message-filter").value;
    const recipients = seats.filter((seat) => !seat.revoked).map((seat) => {
      const option = node("option", "", roleName(seat.principal));
      option.value = seat.principal;
      return option;
    });
    $("#recipient").replaceChildren(...recipients);
    if (recipients.some((item) => item.value === previous)) $("#recipient").value = previous;
    if (!recipients.length) {
      const option = node("option", "", "暂无可用角色"); option.value = "";
      $("#recipient").append(option);
    }
    const all = node("option", "", "全部角色"); all.value = "all";
    $("#message-filter").replaceChildren(all, ...seats.map((seat) => {
      const option = node("option", "", roleName(seat.principal)); option.value = seat.principal; return option;
    }));
    if (seats.some((seat) => seat.principal === messageRole)) $("#message-filter").value = messageRole;
    $("#recipient").dataset.signature = signature;
  }

  function render(snapshot) {
    const demo = snapshot.mode === "demo";
    setConnection(demo ? "本地演示已连接" : "工作台已连接", true);
    $("#last-updated").textContent = "最近更新 " + timeText(state.lastUpdated / 1000) + " · 每 4 秒刷新";
    const notices = [];
    if (demo) notices.push("本地模拟演示：已有数据与回复为演示内容。新请求只入账，不会自动运行模型，也不会调用外部服务。");
    else if (!snapshot.live_observation) notices.push("当前仅能查看账本记录，运行状态尚未确认。");
    if (snapshot.fenced) notices.push("自动调度当前已暂停，新工作会等待恢复；已在运行的工作不一定停止。");
    $("#banner").textContent = notices.join(" ");
    $("#banner").hidden = !notices.length;
    $("#banner").dataset.tone = snapshot.fenced ? "warn" : "info";
    populateRoles(snapshot.seats);
    const signature = JSON.stringify([{ ...snapshot, as_of: undefined }, state.extended]);
    if (state.rendered === signature) return;
    state.rendered = signature;
    const openTasks = snapshot.tasks.filter((task) => !["done", "cancelled"].includes(task.status));
    const proposals = (snapshot.proposals || []).filter((proposal) => proposal.status === "pending");
    const metrics = [[snapshot.truncated?.tasks ? "当前显示的进行中任务" : "进行中的任务", openTasks.length, "☷"], [snapshot.truncated?.proposals ? "当前显示的待复核提案" : "待独立复核", proposals.length, "◇"], ["未读消息", snapshot.unread_count || 0, "▤"], [snapshot.truncated?.memos ? "当前显示的共享备忘录" : "共享备忘录", (snapshot.memos || []).length, "▱"]];
    $("#metrics").replaceChildren(...metrics.map(([label, value, glyph]) => {
      const card = node("div", "metric"); const text = node("div");
      text.append(node("p", "metric-label", label), node("p", "metric-value", value));
      const icon = node("span", "metric-glyph", glyph); icon.setAttribute("aria-hidden", "true");
      card.append(text, icon); return card;
    }));
    $("#nav-tasks").textContent = openTasks.length || "";
    $("#nav-messages").textContent = snapshot.unread_count || "";
    document.title = snapshot.unread_count ? "(" + snapshot.unread_count + " 条未读) deskd · 工作台" : "deskd · 工作台";
    $("#nav-reviews").textContent = proposals.length || "";
    renderAttention(snapshot, proposals);
    renderSeats(snapshot.seats);
    replaceList("#recent-tasks", snapshot.tasks.slice(0, 4).map(taskRow));
    if (!snapshot.tasks.length) $("#recent-tasks").append(empty("还没有任务", "从上方交办第一项任务，进展会显示在这里。"));
    renderTasks(); renderMessages(); renderReviews(); renderResults(); renderActivity();
    renderExtended();
    const truncated = snapshot.truncated || {};
    $("#tasks-limit").hidden = !truncated.tasks;
    $("#messages-limit").hidden = !truncated.messages;
    $("#reviews-limit").hidden = !truncated.proposals && !truncated.approvals;
    $("#results-limit").hidden = !truncated.memos && !truncated.tasks;
    $("#activity-limit").hidden = !truncated.events;
  }

  function renderAttention(snapshot, proposals) {
    const items = [];
    function attention(title, description, view, warn = false) {
      const item = button("", () => switchView(view, true), "attention-item");
      const content = node("div"); content.append(node("strong", "", title), node("p", "", description));
      item.append(node("span", "attention-dot" + (warn ? " warn" : "")), content);
      items.push(item);
    }
    if (snapshot.unread_count) attention(snapshot.unread_count + " 条消息等你查看", "团队的新回复，集中放在消息里。", "messages");
    if (proposals.length) attention((snapshot.truncated?.proposals ? "当前显示 " : "") + proposals.length + " 项提案等待独立复核", "查看原文，选择有权限的另一角色。", "reviews", true);
    const blocked = snapshot.tasks.filter((task) => task.status === "blocked");
    if (blocked.length) attention(blocked.length + " 项任务需要协助", "查看任务详情，补充信息或调整安排。", "tasks", true);
    const unknown = snapshot.seats.reduce((sum, seat) => sum + (seat.inbox?.unknown || 0), 0);
    if (unknown) attention(unknown + " 条消息的处理结果待核对", "请在管理终端核对后，再决定是否重新提交。", "messages", true);
    const notices = state.extended?.notifications?.unread_count || 0;
    if (notices) attention(notices + " 项重要变化需要关注", "查看目标决定、交付与运行提醒。", "notifications", true);
    $("#attention").replaceChildren(...(items.length ? items : [empty("目前无需你介入", "有新回复或待复核提案时，会显示在这里。", "✓")]));
  }

  function renderSeats(seats) {
    replaceList("#roles", seats.map((seat) => {
      const card = node("article", "role-card");
      const header = node("div", "role-header");
      const name = node("div"); name.append(node("h3", "role-name", roleName(seat.principal)), node("span", "principal", seat.principal));
      header.append(node("span", "role-avatar", roleName(seat.principal).slice(0, 1)), name, statusBadge(seat.status));
      const stats = node("div", "role-stats");
      for (const [value, label] of [[seat.inbox?.queued || 0, "待接收"], [seat.inbox?.delivered || 0, "已送达"], [seat.inbox?.handled || 0, "已处理"]]) {
        const item = node("span"); item.append(node("strong", "", value), document.createTextNode(label)); stats.append(item);
      }
      const footer = node("div", "role-footer");
      footer.append(node("span", "", seat.next_trigger_at ? "下次定时 " + timeText(seat.next_trigger_at, true) : "有新工作时唤醒"));
      if (!seat.revoked) footer.append(commandButton(seat.paused ? "恢复唤醒" : "暂停唤醒", async () => {
        try { await command("pause", { principal: seat.principal, paused: !seat.paused, expected_version: seat.version }, seat.paused ? roleName(seat.principal) + "已恢复后续自动唤醒。" : roleName(seat.principal) + "已暂停后续自动唤醒；当前运行不会被强行停止。"); } catch (_) { /* Message shown by command. */ }
      }, "text-button", "pause-" + seat.principal));
      card.append(header, stats, footer); return card;
    }));
  }

  function taskRow(task) {
    const row = node("article", "task-row");
    row.append(node("span", "task-symbol", task.status === "done" ? "✓" : task.status === "cancelled" ? "−" : ""));
    const content = node("div", "task-main");
    content.append(button(taskTitle(task), () => openTask(task), "task-title", "task-" + task.id), node("p", "task-meta", roleName(task.assignee) + " · " + timeText(task.updated_at, true)));
    row.append(content, statusBadge(task.status));
    row.append(button("查看", () => openTask(task), "text-button", "task-view-" + task.id));
    return row;
  }

  function goalAssignment(task) {
    if (task.creator !== "@supervisor" || typeof task.detail !== "string") return null;
    try {
      const value = JSON.parse(task.detail);
      if (value.type === "goal_assignment" && typeof value.objective === "string" && ["research", "review", "delivery"].includes(value.stage)) return value;
    } catch (_) { /* Ordinary task prose is displayed as text. */ }
    return null;
  }
  function taskTitle(task) {
    const assignment = goalAssignment(task);
    if (!assignment) return task.title;
    const goal = state.extended?.goals?.find((item) => item.id === assignment.goal_id);
    return goal ? goal.title + " · " + ({ research: "调研", review: "独立复核", delivery: "交付" })[assignment.stage] + " · 第 " + assignment.cycle + " 轮" : task.title;
  }

  function renderTasks() {
    if (!state.snapshot) return;
    const filter = $("#task-filter").value;
    const tasks = state.snapshot.tasks.filter((task) => filter === "all" || (filter === "open" ? !["done", "cancelled"].includes(task.status) : task.status === filter));
    replaceList("#tasks-list", tasks.length ? tasks.map(taskRow) : [empty("这里还没有任务", filter === "open" ? "没有待推进的任务，可以交办一项新工作。" : "符合当前筛选的任务会显示在这里。")]);
  }

  function metadata(entries) {
    const dl = node("dl");
    entries.forEach(([key, value]) => dl.append(node("dt", "", key), node("dd", "", value)));
    return dl;
  }
  function openDialog(title, children, actions = []) {
    $("#detail-title").textContent = title;
    $("#detail-body").replaceChildren(...children);
    $("#detail-actions").replaceChildren(...actions);
    if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
  }
  function openTask(task) {
    const current = state.snapshot.tasks.find((item) => item.id === task.id) || task;
    const assignment = goalAssignment(current);
    const elements = [metadata([["负责人", roleName(current.assignee)], ["交办人", roleName(current.creator)], ["状态", labels[current.status] || "待核对"], ["最近更新", timeText(current.updated_at, true)]]), node("p", "body-text", assignment ? assignment.objective : current.detail || "没有补充要求。")];
    if (assignment) elements.push(node("p", "context-note section-spaced", "这是持续目标中的" + ({ research: "调研", review: "独立复核", delivery: "交付" })[assignment.stage] + "任务。相关来源、复核与交付进展可在「持续目标」中查看。"));
    if (current.depends_on?.length) {
      elements.push(node("p", "context-note section-spaced", "依赖任务：" + current.depends_on.map((id) => state.snapshot.tasks.find((item) => item.id === id)?.title || "更早的关联任务").join("、")));
    }
    const actions = [button("发送跟进消息", () => {
      $("#detail-dialog").close(); startMessage(current.assignee, "关于任务「" + current.title + "」：\n");
    }, "secondary-button")];
    if (current.creator === "@supervisor" && !["done", "cancelled"].includes(current.status)) actions.push(commandButton("取消任务", () => {
      openDialog("确认取消这项任务？", [node("p", "body-text", current.title), node("p", "context-note section-spaced", "取消会更新任务记录，不会强行停止角色已经开始的工作。若需要调整当前工作，请同时发送跟进消息。")], [button("保留任务", () => openTask(current), "secondary-button"), commandButton("确认取消", async () => {
        try { await command("cancel", { task_id: current.id, expected_version: current.version }, "任务已标记为取消。", () => $("#detail-dialog").close()); } catch (_) { /* Keep the confirmation visible for a new explicit choice. */ }
      }, "danger-button")]);
    }, "danger-button"));
    openDialog(taskTitle(current), elements, actions);
  }

  function startMessage(principal, body = "") {
    switchView("overview"); setCompose("message");
    if (principal && Array.from($("#recipient").options).some((item) => item.value === principal)) $("#recipient").value = principal;
    if (body) $("#compose-body").value = body;
    $("#compose-body").focus();
  }

  function renderMessages() {
    if (!state.snapshot) return;
    const selected = $("#message-filter").value;
    const messages = state.snapshot.messages.filter((item) => selected === "all" || item.sender === selected || item.recipient === selected);
    replaceList("#messages-list", messages.length ? messages.map((message) => {
      const incoming = message.recipient === "@supervisor";
      const card = node("article", "message-card");
      card.dataset.direction = incoming ? "incoming" : "outgoing";
      card.dataset.unread = String(incoming && !message.read);
      const header = node("div", "message-heading");
      header.append(node("strong", "", incoming ? roleName(message.sender) + " 发给我" : "我 → " + roleName(message.recipient)), node("span", "", timeText(message.created_at, true)));
      const footer = node("div", "message-footer");
      footer.append(statusBadge(message.state));
      if (incoming && !message.read && Number.isInteger(message.reply_id)) footer.append(commandButton("标为已读", async () => {
        try { await command("ack", { message_ids: [message.reply_id] }, "已标为已读。"); } catch (_) { /* The message remains unread until confirmed. */ }
      }, "text-button", "read-" + message.id));
      else footer.append(button(incoming ? "回复" : "继续沟通", () => startMessage(incoming ? message.sender : message.recipient), "text-button", "reply-" + message.id));
      card.append(header, messageContent(message), footer); return card;
    }) : [empty("还没有往来消息", "选择一个角色发送消息，回复会集中显示在这里。", "▤")]);
  }

  function messageContent(message) {
    if (message.sender === "@supervisor" && message.kind === "review_request") {
      const content = node("div");
      content.append(node("p", "body-text", "已请求" + roleName(message.recipient) + "独立复核提案。完整原文已一并入队，复核请求本身不授予执行权限。"));
      try {
        const value = JSON.parse(message.body);
        if (value.type !== "independent_review_request" || typeof value.proposal_id !== "string" || !/^[0-9a-f]{64}$/.test(value.body_sha256)) throw new Error();
        const details = node("details", "message-details"); details.dataset.expandKey = message.id;
        const summary = node("summary", "", "查看提案编号与内容指纹"); summary.dataset.focusKey = "review-meta-" + message.id;
        details.append(summary, node("code", "", value.proposal_id), node("code", "", value.body_sha256)); content.append(details);
      } catch (_) { content.append(node("p", "context-note", "关联信息暂时无法识别，请在提案列表中核对原文。")); }
      return content;
    }
    if (message.sender === "@supervisor" && message.kind === "review_body") {
      const content = node("div"); content.append(node("p", "content-card-meta", "待复核原文"));
      if (String(message.body).length <= 600) content.append(node("p", "body-text", message.body));
      else {
        const details = node("details", "message-details"); details.dataset.expandKey = message.id;
        const summary = node("summary", "", "阅读完整原文"); summary.dataset.focusKey = "review-body-" + message.id;
        details.append(summary, node("p", "body-text", message.body));
        content.append(node("p", "body-text body-preview", message.body), details);
      }
      return content;
    }
    return node("p", "body-text", message.body);
  }

  function proposalTitle(proposal) {
    const first = String(proposal.body || "").trim().split("\n")[0];
    return first.length > 60 ? first.slice(0, 60) + "…" : first || "共享备忘录提案";
  }
  function renderReviews() {
    const snapshot = state.snapshot;
    if (!snapshot) return;
    const proposals = (snapshot.proposals || []).filter((proposal) => proposal.status !== "executed");
    replaceList("#reviews-list", proposals.length ? proposals.map((proposal) => {
      const card = node("article", "content-card");
      const header = node("div", "content-card-header"); header.append(node("h3", "", proposalTitle(proposal)), statusBadge(proposal.status));
      card.append(header, node("p", "content-card-meta", roleName(proposal.author_principal) + " 提案 · " + roleName(proposal.executor_principal) + " 执行 · " + timeText(proposal.created_at, true)), node("p", "body-text body-preview", proposal.body));
      const actions = node("div", "content-card-actions");
      actions.append(button("查看与请求复核", () => openProposal(proposal), "secondary-button", "proposal-" + proposal.proposal_id));
      card.append(actions); return card;
    }) : [empty("没有待复核的提案", "团队提交共享备忘录提案后，你可以在这里查看并请求独立复核。", "◇")]);
    const approvals = snapshot.approvals || [];
    replaceList("#approvals-list", approvals.length ? [node("h2", "", "授权记录"), ...approvals.map((approval) => {
      const row = node("article", "approval-item"); const text = node("div");
      const proposal = (snapshot.proposals || []).find((item) => item.proposal_id === approval.proposal_id);
      text.append(node("strong", "", proposal ? proposalTitle(proposal) : "共享备忘录授权"), node("p", "", roleName(approval.issuer_principal) + " 独立授权 · 指定 " + roleName(approval.executor_principal) + " 执行 · 有效至 " + timeText(approval.expires_at, true)));
      const badge = statusBadge(approval.status);
      if (approval.status === "active") badge.textContent = "有效授权";
      row.append(text, badge); return row;
    })] : []);
  }

  function openProposal(proposal) {
    const current = (state.snapshot.proposals || []).find((item) => item.proposal_id === proposal.proposal_id) || proposal;
    const meta = metadata([["提案人", roleName(current.author_principal)], ["指定执行人", roleName(current.executor_principal)], ["状态", labels[current.status] || "待核对"]]);
    const proof = node("details"); proof.append(node("summary", "", "查看提案编号与内容指纹"), node("code", "", current.proposal_id), node("code", "", current.body_sha256));
    const children = [meta, node("p", "body-text", current.body), proof];
    const candidates = state.snapshot.seats.filter((seat) => seat.principal !== current.executor_principal && !seat.revoked && seat.binding_status === "bound" && seat.capabilities?.includes("approval.issue"));
    const actions = [];
    if (current.status !== "executed" && candidates.length) {
      const field = node("div", "review-field"); const label = node("label", "", "交给谁复核"); label.htmlFor = "review-recipient";
      const select = node("select"); select.id = "review-recipient";
      candidates.forEach((seat) => { const option = node("option", "", roleName(seat.principal)); option.value = seat.principal; select.append(option); });
      field.append(label, select); children.push(field, node("p", "context-note section-spaced", "将原文、提案编号与内容指纹一起发给该角色，由其独立判断。请求送达不代表已获授权。"));
      const requestIds = new Map();
      actions.push(commandButton("请求独立复核", async () => {
        if (!requestIds.has(select.value)) requestIds.set(select.value, randomId());
        try { await command("review", { proposal_id: current.proposal_id, body_sha256: current.body_sha256, reviewer: select.value, request_id: requestIds.get(select.value) }, "独立复核请求已提交；等待角色处理与判断。", () => $("#detail-dialog").close()); } catch (_) { /* Never auto-retry a decision request. */ }
      }, "primary-button"));
    } else if (current.status !== "executed") children.push(node("p", "context-note section-spaced", "目前没有可用的独立复核角色，需要在管理端配置具有复核权限、且不同于执行人的角色。"));
    openDialog("共享备忘录提案", children, actions);
  }

  function renderResults() {
    const snapshot = state.snapshot;
    if (!snapshot) return;
    const results = (snapshot.memos || []).map((memo) => {
      const card = node("article", "content-card"); const header = node("div", "content-card-header");
      header.append(node("h3", "", proposalTitle(memo)), statusBadge("executed"));
      const details = node("details"); details.dataset.expandKey = memo.memo_id;
      const summary = node("summary", "", "阅读完整备忘录"); summary.dataset.focusKey = "memo-summary-" + memo.memo_id;
      details.append(summary, node("p", "body-text", memo.body));
      card.append(header, node("p", "content-card-meta", roleName(memo.issuer_principal) + " 独立授权 · " + roleName(memo.executor_principal) + " 发布 · " + timeText(memo.published_at, true)), node("p", "body-text body-preview", memo.body), details);
      return card;
    });
    const completed = snapshot.tasks.filter((task) => task.status === "done");
    if (completed.length) {
      const panel = node("section", "panel"); panel.append(node("h2", "", "已完成的任务"), node("p", "context-note", "以下状态由负责角色记录；任务详情不等同于独立验收结论。"), ...completed.map(taskRow)); results.push(panel);
    }
    replaceList("#results-list", results.length ? results : [empty("成果会在这里汇集", "共享备忘录发布或任务完成后，就可以在这里查看。", "▱")]);
  }

  function renderActivity() {
    if (!state.snapshot) return;
    const events = state.snapshot.events || [];
    replaceList("#activity-list", events.length ? events.map((event) => {
      const row = node("article", "activity-item"); const content = node("div", "activity-content");
      const task = state.snapshot.tasks.find((item) => item.id === event.ref);
      content.append(node("strong", "", eventLabels[event.kind] || "工作状态已更新"), node("p", "", roleName(event.actor) + (task ? " · " + task.title : "")));
      row.append(node("span", "activity-dot"), content, node("time", "", timeText(event.created_at, true))); return row;
    }) : [empty("还没有工作动态", "交办任务后，关键进展会留在这里。", "↗")]);
  }

  function goalBadge(value) {
    const badge = statusBadge(value);
    const names = { active: "推进中", scheduled: "等待下一轮", waiting_human: "需要你决定", blocked: "遇到阻碍", completed: "已完成", paused: "已暂停", cancelled: "已取消" };
    badge.textContent = names[value] || "状态待核对";
    if (["waiting_human", "blocked"].includes(value)) badge.dataset.tone = "warn";
    return badge;
  }

  function requestFor(key) {
    if (!state.goalRequests.has(key)) state.goalRequests.set(key, randomId());
    return state.goalRequests.get(key);
  }

  function renderExtended() {
    if (!state.extended) return;
    const extended = state.extended;
    const openGoals = extended.goals.filter((goal) => !["completed", "cancelled"].includes(goal.state));
    $("#nav-goals").textContent = openGoals.length || "";
    $("#nav-notifications").textContent = extended.notifications?.unread_count || "";
    const attentionCount = (state.snapshot?.unread_count || 0) + (extended.notifications?.unread_count || 0);
    document.title = attentionCount ? "(" + attentionCount + " 条待查看) deskd · 工作台" : "deskd · 工作台";
    populateGoalForm(); renderGoals(); renderSources(); renderNotifications(); renderKnowledge(); renderHealth();
  }

  function populateGoalForm() {
    const seats = state.snapshot.seats.filter((seat) => !seat.revoked);
    const signature = JSON.stringify(seats.map((seat) => seat.principal));
    [["#goal-researcher", "engineer"], ["#goal-reviewer", "analyst"], ["#goal-executor", "trader"]].forEach(([selector, preferred], index) => {
      const select = $(selector);
      if (select.dataset.signature === signature) return;
      const previous = select.value;
      select.replaceChildren(...seats.map((seat) => { const option = node("option", "", roleName(seat.principal)); option.value = seat.principal; return option; }));
      const desired = seats.some((seat) => seat.principal === previous) ? previous : seats.find((seat) => seat.principal.split("/").pop() === preferred)?.principal || seats[index]?.principal;
      if (desired) select.value = desired;
      select.dataset.signature = signature;
    });
    const sources = state.extended.sources.filter((source) => source.enabled);
    const sourceSignature = JSON.stringify(sources.map((source) => [source.name, source.version]));
    if ($("#goal-sources").dataset.signature !== sourceSignature) {
      const checked = new Set($$("#goal-sources input:checked").map((item) => item.value));
      $("#goal-sources").replaceChildren(...sources.map((source) => {
        const label = node("label", "source-option"); const input = node("input"); input.type = "checkbox"; input.value = source.name; input.checked = checked.has(source.name);
        label.append(input, document.createTextNode(source.name)); return label;
      }));
      if (!sources.length) $("#goal-sources").append(node("p", "subtle", "还没有可用来源，请先添加一个公开信息来源。"));
      $("#goal-sources").dataset.signature = sourceSignature;
    }
  }

  function renderGoals() {
    const goals = state.extended.goals;
    replaceList("#goals-list", goals.length ? goals.map((goal) => {
      const card = node("article", "content-card"); const header = node("div", "content-card-header");
      header.append(node("h3", "", goal.title), goalBadge(goal.state));
      const cycle = goal.cycles?.find((item) => item.number === goal.cycle) || goal.cycles?.[0];
      const progress = node("div", "goal-progress");
      [["research", "调研", goal.researcher], ["review", "独立复核", goal.reviewer], ["delivery", "交付", goal.executor]].forEach(([stage, label, principal], index) => {
        if (index) progress.append(node("span", "", "→"));
        const item = node("span", "goal-stage", label + " · " + roleName(principal)); item.dataset.current = String(cycle?.phase === stage); progress.append(item);
      });
      card.append(header, node("p", "body-text body-preview", goal.objective), progress, node("p", "content-card-meta", "第 " + goal.cycle + " / " + goal.max_cycles + " 轮" + (goal.next_due ? " · 下次推进 " + timeText(goal.next_due, true) : "")));
      if (goal.question) card.append(node("p", "goal-question", "需要你决定：" + goal.question));
      else if (goal.blocked_reason && blockedReasons[goal.blocked_reason]) card.append(node("p", "goal-question", blockedReasons[goal.blocked_reason]));
      const actions = node("div", "content-card-actions"); actions.append(button(goal.state === "waiting_human" ? "查看并回复" : "查看进展", () => openGoal(goal), "secondary-button", "goal-" + goal.id));
      card.append(actions); return card;
    }) : [empty("还没有持续目标", "交代一次完整目标，团队会按调研、复核、交付的顺序推进。", "◎")]);
  }

  function openGoal(goal) {
    const current = state.extended.goals.find((item) => item.id === goal.id) || goal;
    const cycle = current.cycles?.find((item) => item.number === current.cycle) || current.cycles?.[0];
    const stages = { research: "调研中", review: "独立复核中", delivery: "交付中", completed: "本轮已交付" };
    const children = [metadata([["分工", [current.researcher, current.reviewer, current.executor].map(roleName).join(" → ")], ["当前阶段", stages[cycle?.phase] || "等待推进"], ["执行轮次", current.cycle + " / " + current.max_cycles], ["信息来源", (current.source_ids || []).join("、")], ["本阶段已跟进", String(cycle?.followups || 0) + " 次"]]), node("p", "body-text", current.objective)];
    if (current.blocked_reason && blockedReasons[current.blocked_reason]) children.push(node("p", "goal-question", blockedReasons[current.blocked_reason]));
    const actions = [];
    if (current.state === "waiting_human") {
      children.push(node("p", "goal-question", current.question || "团队需要你的补充信息。"));
      const field = node("label", "field"); const answer = node("textarea"); answer.maxLength = 1000; answer.rows = 4;
      field.append(node("span", "", "你的回复"), answer); children.push(field, node("p", "context-note section-spaced", "回复用于继续推进目标，不会替代独立执行授权。"));
      actions.push(commandButton("回复并继续", async () => {
        const body = answer.value.trim(); if (!body) { answer.focus(); return; }
        const params = { goal_id: current.id, body, expected_version: current.version, request_id: requestFor(JSON.stringify(["answer", current.id, current.version, body])) };
        try { await command("goal_answer", params, "回复已记录，目标可以继续推进。", () => $("#detail-dialog").close()); } catch (_) { /* Preserve the answer on failure. */ }
      }, "primary-button"));
    }
    const mutate = (action) => async () => {
      const params = { goal_id: current.id, action, expected_version: current.version, request_id: requestFor(JSON.stringify(["update", current.id, current.version, action])) };
      try { await command("goal_update", params, action === "pause" ? "已暂停后续分派与跟进；已经开始的工作不会被强行停止。" : action === "resume" ? "已恢复目标推进。" : "已取消后续安排，并阻止已关联提案继续执行；已经完成的交付不会撤回。", () => $("#detail-dialog").close()); } catch (_) { /* A fresh decision is required after a version conflict. */ }
    };
    if (["active", "scheduled"].includes(current.state)) actions.push(commandButton("暂停后续推进", mutate("pause"), "secondary-button"));
    if (["paused", "blocked"].includes(current.state)) actions.push(commandButton("恢复推进", mutate("resume"), "primary-button"));
    if (!["completed", "cancelled"].includes(current.state)) actions.push(commandButton("取消目标", () => openDialog("确认取消这个目标？", [node("p", "body-text", current.title), node("p", "context-note section-spaced", "取消会停止后续分派与跟进，并阻止本目标已关联提案继续获批或执行。已经完成的交付不会撤回；正在运行的回合不会被强行停止。")], [button("保留目标", () => openGoal(current), "secondary-button"), commandButton("确认取消", mutate("cancel"), "danger-button")]), "danger-button"));
    openDialog(current.title, children, actions);
  }

  function renderSources() {
    const sources = state.extended.sources;
    replaceList("#sources-list", sources.length ? sources.map((source) => {
      const card = node("article", "content-card"); const header = node("div", "content-card-header");
      header.append(node("h3", "", source.name), node("span", "badge", source.enabled ? "允许读取" : "已停用"));
      card.append(header, node("p", "source-url", source.url), node("p", "content-card-meta", "单次最多 " + Math.round(source.max_bytes / 1024) + " KB · 超时 " + source.timeout_seconds + " 秒 · 第 " + source.version + " 版"));
      const actions = node("div", "content-card-actions");
      if (source.enabled) actions.append(commandButton("停用来源", async () => {
        try { await command("source_disable", { name: source.name, expected_version: source.version }, "已停用该来源，后续读取请求将被拒绝。"); } catch (_) { /* Do not repeat a stale mutation. */ }
      }, "text-button", "source-disable-" + source.name));
      actions.append(button(source.enabled ? "修改地址" : "重新配置", () => {
        $("#source-name").value = source.name; $("#source-url").value = source.url; $("#source-url").focus();
      }, "text-button", "source-edit-" + source.name));
      card.append(actions); return card;
    }) : [empty("先允许一个信息来源", "团队只会读取你配置的公开来源，不会任意浏览其他地址。", "⊕")]);
  }

  function renderNotifications() {
    const values = state.extended.notifications || { notifications: [], unread_count: 0 };
    const names = { decision: "需要你决定", completion: "已完成交付", stalled: "进展需要协助", error: "运行遇到问题", budget: "额度需要关注" };
    replaceList("#notifications-list", values.notifications?.length ? values.notifications.map((notice) => {
      const card = node("article", "content-card"); const header = node("div", "content-card-header");
      header.append(node("h3", "", notice.title), statusBadge(notice.acknowledged_at === null ? "unread" : "read"));
      let body = notice.body || "请查看相关工作进展。";
      body = blockedReasons[body] || (body === "A goal participant was revoked; the next cycle requires an operator decision." ? blockedReasons.participant_revoked : body);
      if (notice.kind === "completion") {
        try {
          const detail = JSON.parse(body);
          if (typeof detail.memo_id === "string" && typeof detail.body_sha256 === "string") body = detail.all_cycles_complete ? "目标的全部轮次已完成，成果已通过独立授权发布。可前往「成果」查看共享备忘录。" : "本轮成果已通过独立授权发布，目标会按设定间隔继续下一轮。";
        } catch (_) { /* Plain-language notices stay plain text. */ }
      }
      card.append(node("p", "notification-kind", names[notice.kind] || "工作提醒"), header, node("p", "body-text", body), node("p", "content-card-meta", timeText(notice.created_at, true)));
      const actions = node("div", "content-card-actions");
      if (notice.acknowledged_at === null) actions.append(commandButton("我已查看", async () => {
        try { await command("notification_ack", { notification_ids: [notice.id] }, "已确认查看这条提醒。"); } catch (_) { /* Keep unread until confirmed. */ }
      }, "secondary-button", "notice-" + notice.id));
      const destination = notice.kind === "completion" ? "results" : notice.source_id?.startsWith("message:") ? "messages" : notice.source_id?.startsWith("source:") ? "sources" : notice.kind === "budget" && notice.source_id?.startsWith("seat:") ? "operations" : "goals";
      actions.append(button(({ results: "查看成果", messages: "查看消息", sources: "查看来源", operations: "查看额度", goals: "查看目标" })[destination], () => {
        switchView(destination, true);
        const goal = state.extended.goals.find((item) => item.id === notice.source_id);
        if (destination === "goals" && goal) openGoal(goal);
      }, "text-button")); card.append(actions); return card;
    }) : [empty("暂时没有重要提醒", "需要你决定或工作有重要变化时，会在这里留下记录。", "◉")]);
    $("#notifications-limit").hidden = !values.has_more;
  }

  function renderKnowledge() {
    if (state.knowledgeResults === null) {
      replaceList("#knowledge-list", [empty("搜索团队已经共享的知识", "输入关键词，或留空搜索近期共享笔记。私人记忆不会出现在结果中。", "▥")]); return;
    }
    const notes = state.knowledgeResults.memories || [];
    replaceList("#knowledge-list", notes.length ? notes.map((note) => {
      const card = node("article", "content-card"); const header = node("div", "content-card-header");
      header.append(node("h3", "", note.title || "共享笔记"), node("span", "badge", "主动共享"));
      const detail = node("details", "message-details"); detail.dataset.expandKey = "memory-" + note.id;
      const summary = node("summary", "", "阅读完整笔记"); summary.dataset.focusKey = "memory-" + note.id;
      detail.append(summary, node("p", "body-text", note.body));
      card.append(header, node("p", "content-card-meta", roleName(note.owner) + " · 第 " + note.version + " 版"), node("p", "body-text body-preview", note.body), detail);
      if (Array.isArray(note.sources) && note.sources.length) card.append(node("p", "note-provenance", "附有 " + note.sources.length + " 项来源记录，可由参与角色核对原始内容与指纹。"));
      return card;
    }) : [empty("没有找到匹配的共享笔记", "试试其他关键词，或等待角色明确分享新的工作笔记。", "▥")]);
  }

  function renderHealth() {
    const health = state.extended.health || {};
    const metrics = [["角色", health.seats ?? "—"], ["暂停唤醒", health.paused_seats ?? "—"], ["待核对投递", health.unknown_deliveries ?? "—"], ["额度耗尽", health.exhausted_turn_budgets ?? "—"]];
    $("#health-metrics").replaceChildren(...metrics.map(([label, value]) => {
      const card = node("div", "metric"); const content = node("div"); content.append(node("p", "metric-label", label), node("p", "metric-value", value)); card.append(content); return card;
    }));
    const title = node("h2", "", health.health === "attention" ? "有运行状态需要关注" : health.observation === "live" ? "已获取运行状态" : "当前为账本记录");
    const facts = metadata([["自动调度", health.fenced ? "当前已暂停" : "当前未暂停"], ["状态来源", health.observation === "live" ? "刚刚观察到的服务状态" : "保存的记录，不能证明服务在线"], ["已使用自动轮次", String(health.automatic_turns_used ?? "—")], ["费用统计", "当前统计自动唤醒轮次，不代表模型 token 或实际费用"]]); facts.className = "health-facts";
    $("#health-detail").replaceChildren(title, facts);
  }

  $("#pair").addEventListener("click", async () => {
    $("#pair").disabled = true;
    try { showSession(await api("/api/pair", {})); await refresh(); }
    catch (error) { if (error.code !== "session_changed") feedback("#pairing-feedback", errorText(error), true); }
    finally { $("#pair").disabled = false; }
  });
  $("#logout").addEventListener("click", async () => {
    // Invalidate displayed authority immediately; late responses from the previous proof are ignored.
    const logout = api("/api/logout", {});
    clearAccess(); sessionProof(true);
    feedback("#pairing-feedback", "已断开此页面的访问。重新连接需要在终端再次确认。");
    try { await logout; } catch (_) { /* Local access has already been removed. */ }
  });
  $("#refresh").addEventListener("click", refresh);
  $$("[data-view]").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.view, true)));
  $$("[data-go]").forEach((item) => item.addEventListener("click", () => switchView(item.dataset.go, true)));
  $$("[data-compose]").forEach((item) => item.addEventListener("click", () => setCompose(item.dataset.compose)));
  $("#new-task").addEventListener("click", () => { switchView("overview"); setCompose("task"); $("#task-title").focus(); });
  $("#compose-message").addEventListener("click", () => startMessage($("#message-filter").value));
  $("#recipient").addEventListener("change", updateButtons);
  $("#task-filter").addEventListener("change", renderTasks);
  $("#message-filter").addEventListener("change", renderMessages);
  $("#close-dialog").addEventListener("click", () => $("#detail-dialog").close());
  $("#composer").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.connected || state.pending) return;
    const recipient = $("#recipient").value;
    const body = $("#compose-body").value.trim();
    const title = $("#task-title").value.trim();
    if (!recipient || (state.compose === "task" ? !title : !body)) {
      feedback("#compose-feedback", "请选择接收角色，并填写要交办或发送的内容。", true); return;
    }
    const params = state.compose === "task" ? { assignee: recipient, title, body } : { recipient, body };
    const fingerprint = JSON.stringify([state.compose, params]);
    if (!state.intent || state.intent.fingerprint !== fingerprint) state.intent = { fingerprint, id: randomId() };
    params.request_id = state.intent.id;
    feedback("#compose-feedback", "正在提交…");
    try {
      const wasTask = state.compose === "task";
      await command(state.compose, params, "", () => {
        // Clear only the exact submitted content; typing while a request is in flight is preserved.
        if ($("#compose-body").value.trim() === body) $("#compose-body").value = "";
        if (wasTask && $("#task-title").value.trim() === title) $("#task-title").value = "";
        state.intent = null;
        feedback("#compose-feedback", wasTask ? "任务已提交给" + roleName(recipient) + "，可在任务中跟进。" : "消息已提交给" + roleName(recipient) + "，处理进展见消息记录。");
      });
    } catch (error) {
      if (error.code === "session_changed") return;
      feedback("#compose-feedback", error.code === "outcome_unknown" ? "提交结果尚未确认。恢复连接后，请先查看记录；相同内容再次提交会沿用本次请求编号。" : errorText(error), true);
    }
  });
  $("#theme").addEventListener("click", () => {
    const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = theme;
    try { localStorage.setItem("deskd.console.theme", theme); } catch (_) { /* Theme preference is optional. */ }
  });
  $("#goal-interval").addEventListener("change", () => {
    const once = $("#goal-interval").value === "once";
    $("#goal-cycles").disabled = once;
    $("#goal-cycles").value = once ? "1" : "7";
  });
  $("#goal-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.connected || state.pending) return;
    const params = {
      title: $("#goal-title").value.trim(), objective: $("#goal-objective").value.trim(),
      researcher: $("#goal-researcher").value, reviewer: $("#goal-reviewer").value, executor: $("#goal-executor").value,
      source_ids: $$("#goal-sources input:checked").map((input) => input.value).sort(),
      interval_seconds: $("#goal-interval").value === "once" ? null : Number($("#goal-interval").value),
      max_cycles: Number($("#goal-cycles").value), followup_seconds: Number($("#goal-followup").value), max_followups: 2,
    };
    if (!params.title || !params.objective || !params.source_ids.length) {
      feedback("#goal-feedback", "请填写目标与成果要求，并至少选择一个允许读取的信息来源。", true); return;
    }
    if (new Set([params.researcher, params.reviewer, params.executor]).size !== 3) {
      feedback("#goal-feedback", "调研、复核与交付须选择三位不同的角色。", true); return;
    }
    const fingerprint = JSON.stringify(params);
    if (!state.goalIntent || state.goalIntent.fingerprint !== fingerprint) state.goalIntent = { fingerprint, id: randomId() };
    params.request_id = state.goalIntent.id;
    feedback("#goal-feedback", "正在创建目标…");
    try {
      await command("goal_create", params, "", () => {
        if ($("#goal-title").value.trim() === params.title) $("#goal-title").value = "";
        if ($("#goal-objective").value.trim() === params.objective) $("#goal-objective").value = "";
        state.goalIntent = null;
        feedback("#goal-feedback", state.snapshot?.mode === "demo" ? "目标已记入演示账本。当前不会自动调用模型。" : "目标已创建，首项调研任务已进入队列。独立复核与交付进展会显示在下方。");
      });
    } catch (error) {
      if (error.code !== "session_changed") feedback("#goal-feedback", error.code === "outcome_unknown" ? "尚不能确认是否创建成功。请先查看目标列表；相同内容重试会沿用本次请求编号。" : errorText(error), true);
    }
  });
  $("#source-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.connected || state.pending) return;
    const name = $("#source-name").value.trim(); const url = $("#source-url").value.trim();
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash || url.includes("?") || url.includes("#")) throw new Error();
    } catch (_) { feedback("#source-feedback", errors.source_url_rejected, true); return; }
    const existing = state.extended.sources.find((source) => source.name === name);
    const submit = async () => {
      feedback("#source-feedback", "正在保存来源…");
      try {
        await command("source_configure", { name, url, max_bytes: existing?.max_bytes || 65536, timeout_seconds: existing?.timeout_seconds || 5 }, "", () => {
          if ($("#source-name").value.trim() === name) $("#source-name").value = "";
          if ($("#source-url").value.trim() === url) $("#source-url").value = "";
          feedback("#source-feedback", "已允许团队读取该来源。配置本身不会立刻读取网页。");
          $("#detail-dialog").close();
        });
      } catch (error) { if (error.code !== "session_changed") feedback("#source-feedback", errorText(error), true); }
    };
    if (existing) openDialog("确认更新这个信息来源？", [metadata([["来源", name], ["原地址", existing.url], ["新地址", url]]), node("p", "context-note", "同名来源将更新为新版本并启用。依赖旧版本、尚未完成的读取可能需要重新发起。")], [button("暂不修改", () => $("#detail-dialog").close(), "secondary-button"), commandButton("确认更新来源", submit, "primary-button")]);
    else await submit();
  });
  $("#knowledge-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!state.connected || state.pending) return;
    const query = $("#knowledge-query").value.trim();
    feedback("#knowledge-feedback", "正在查找已共享的笔记…");
    try {
      await command("memory_search", { query, limit: 20 }, "", (result) => {
        state.knowledgeResults = result;
        renderKnowledge();
        feedback("#knowledge-feedback", result.has_more ? "找到部分匹配笔记，可用更具体的关键词缩小范围。" : "找到 " + (result.memories?.length || 0) + " 篇共享笔记。");
      });
    } catch (error) { if (error.code !== "session_changed") feedback("#knowledge-feedback", errorText(error), true); }
  });
  window.addEventListener("hashchange", () => switchView(location.hash.slice(1)));
  window.addEventListener("online", refresh);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  try {
    let theme;
    try { theme = localStorage.getItem("deskd.console.theme"); } catch (_) { /* Use system preference. */ }
    document.documentElement.dataset.theme = ["light", "dark"].includes(theme) ? theme : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    sessionProof();
    switchView(location.hash.slice(1));
    updateButtons();
    refresh();
  } catch (_) {
    setConnection("浏览器暂不支持");
    feedback("#pairing-feedback", "请使用支持安全随机数与本地会话的现代浏览器打开工作台。", true);
    $("#pair").disabled = true;
  }
})();
