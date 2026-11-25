"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  team: null,
  actor: null,
  lead: null,
  participants: [],
  messages: [],
  goals: [],
  tasks: [],
  openTask: null,
  taskFetch: 0,
  questions: [],
  approvals: [],
  approvalRequests: new Map(),
  approvalFetch: 0,
  peer: null,
  reply: null,
  stream: null,
  seq: 0,
  generation: 0,
  pending: null,
};
const notice = (text) => {
  $("notice").textContent = text || "";
};
const element = (tag, text, cls) => {
  const e = document.createElement(tag);
  if (text != null) e.textContent = text;
  if (cls) e.className = cls;
  return e;
};
const nameOf = (id) =>
  state.participants.find((p) => p.id === id)?.name || id || "未知成员";
const key = () => crypto.randomUUID();
async function api(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  if (response.status === 401) {
    showLogin();
    throw new Error("会话已失效，请重新登录");
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : "";
    } catch (_) {}
    throw new Error(detail || `请求失败 (${response.status})`);
  }
  return response.status === 204 ? null : response.json();
}
function clearDraft() {
  $("text").value = "";
  $("kind").value = "chat";
  $("mentions").replaceChildren();
  state.reply = null;
  state.pending = null;
}
function showLogin() {
  clearDraft();
  state.team = null;
  state.actor = null;
  state.messages = [];
  state.goals = [];
  state.questions = [];
  state.approvals = [];
  state.approvalRequests.clear();
  state.approvalFetch++;
  $("approvals").replaceChildren();
  $("approval-section").hidden = true;
  $("messages").replaceChildren();
  $("goals").replaceChildren();
  resetTaskResults();
  $("questions").replaceChildren();
  state.generation++;
  state.stream?.close();
  state.stream = null;
  $("workspace").hidden = true;
  $("login").hidden = false;
  $("connection").textContent = "未连接";
}
async function loadTeams() {
  const teams = await api("/v1/teams");
  $("login").hidden = true;
  $("workspace").hidden = false;
  const select = $("teams");
  select.replaceChildren();
  for (const team of teams) {
    const o = element("option", team.name);
    o.value = team.team_id;
    select.append(o);
  }
  if (!teams.length) {
    state.generation++;
    state.stream?.close();
    clearDraft();
    state.team = null;
    $("composer")
      .querySelectorAll("textarea, select, button")
      .forEach((e) => (e.disabled = true));
    $("connection").textContent = "暂无团队";
    $("conversations").replaceChildren();
    $("messages").replaceChildren(element("p", "暂无团队", "empty"));
    $("goals").replaceChildren();
    resetTaskResults();
    state.questions = [];
    state.approvals = [];
    state.approvalRequests.clear();
    state.approvalFetch++;
    $("approvals").replaceChildren();
    $("approval-section").hidden = true;
    $("questions").replaceChildren();
    return;
  }
  $("composer")
    .querySelectorAll("textarea, select, button")
    .forEach((e) => (e.disabled = false));
  const chosen = teams.some((t) => t.team_id === state.team)
    ? state.team
    : teams[0].team_id;
  select.value = chosen;
  await loadTeam(chosen);
}
function setComposerEnabled(enabled) {
  $("composer")
    .querySelectorAll("textarea, select, button")
    .forEach((control) => {
      control.disabled = !enabled;
    });
}
async function loadTeam(id, preserve = false) {
  setComposerEnabled(false);
  const oldTeam = state.team;
  const oldActor = state.actor;
  resetTaskResults();
  if (!preserve || oldTeam !== id) {
    clearDraft();
    state.peer = null;
    state.messages = [];
    state.goals = [];
    state.questions = [];
    $("messages").replaceChildren();
    $("goals").replaceChildren();
    $("questions").replaceChildren();
    $("conversations").replaceChildren();
    $("conversation-title").textContent = "群聊";
  }
  const generation = ++state.generation;
  state.stream?.close();
  state.stream = null;
  state.team = id;
  state.approvals = [];
  if (!preserve || oldTeam !== id) state.approvalRequests.clear();
  state.approvalFetch++;
  $("approvals").replaceChildren();
  $("approval-section").hidden = true;
  $("connection").textContent = "正在加载";
  try {
    const s = await api(`/v1/teams/${encodeURIComponent(id)}/snapshot`);
    if (generation !== state.generation) return;
    if (oldActor && oldActor !== s.actor_id) {
      clearDraft();
      state.peer = null;
    }
    state.actor = s.actor_id;
    state.lead = s.lead_id;
    state.participants = s.participants || [];
    state.messages = s.messages || [];
    state.goals = s.goals || [];
    state.tasks = s.tasks || [];
    renderTaskResults();
    state.questions = s.questions || [];
    state.seq = Math.max(0, ...state.messages.map((m) => Number(m.seq) || 0));
    render();
    setComposerEnabled(true);
    connect(s.cursor, generation);
    if (state.actor === "local-operator") refreshApprovals(id, generation);
  } catch (e) {
    if (generation === state.generation) {
      $("connection").textContent = "加载失败";
      notice(e.message);
    }
  }
}
function connect(cursor, generation) {
  const stream = new EventSource(
    `/v1/teams/${encodeURIComponent(state.team)}/events?after=${encodeURIComponent(cursor ?? "")}`,
  );
  state.stream = stream;
  stream.onopen = () => {
    if (generation === state.generation)
      $("connection").textContent = "实时连接中";
  };
  stream.onerror = () => {
    if (generation === state.generation)
      $("connection").textContent = "连接中断，正在重连";
  };
  for (const type of ["message.created", "goal.registered"])
    stream.addEventListener(type, (event) => {
      if (generation !== state.generation) return;
      try {
        const data = JSON.parse(event.data);
        if (type === "message.created" && data.message?.kind === "approval" && state.actor === "local-operator")
          refreshApprovals(state.team, generation);
        if (Number(data.seq) <= state.seq) return;
        state.seq = Number(data.seq);
        if (
          type === "message.created" &&
          !state.messages.some((m) => m.id === data.message.id)
        )
          state.messages.push(data.message);
        if (type === "message.created" && data.question) {
          const index = state.questions.findIndex(
            (q) => q.question_id === data.question.question_id,
          );
          if (index < 0) state.questions.push(data.question);
          else state.questions[index] = data.question;
        }
        if (
          type === "goal.registered" &&
          !state.goals.some((g) => g.id === data.goal.id)
        )
          state.goals.push(data.goal);
        render();
      } catch (_) {
        notice("无法读取实时事件，请刷新工作台");
      }
    });
  for (const type of ["goal.completed", "goal.failed", "goal.cancelled", "goal.timed_out"]) {
    stream.addEventListener(type, (event) => {
      if (generation !== state.generation) return;
      try {
        const data = JSON.parse(event.data);
        for (const status of data.supplement_statuses || []) {
          const existing = state.messages.find(m => m.id === status.id);
          if (existing) existing.active_input = status.active_input;
        }
        render();
      } catch (_) { notice("无法更新补充状态，请刷新工作台"); }
    });
  }
  stream.addEventListener("supplement.status", (event) => {
    if (generation !== state.generation) return;
    try {
      const incoming = JSON.parse(event.data).message;
      const existing = state.messages.find(m => m.id === incoming.id);
      if (existing) { existing.active_input = incoming.active_input; render(); }
    } catch (_) { notice("无法更新补充状态，请刷新工作台"); }
  });
  stream.addEventListener("stream.reset", () => {
    if (generation === state.generation) loadTeam(state.team, true);
  });
  for (const type of ["task.assigned", "task.updated", "task.submitted", "task.approved", "task.returned", "task.needs_recheck"]) {
    stream.addEventListener(type, (event) => {
      if (generation !== state.generation) return;
      try {
        const { task } = JSON.parse(event.data);
        const index = state.tasks.findIndex((item) => item.id === task.id);
        if (index < 0) state.tasks.push(task);
        else state.tasks[index] = task;
        const open = state.openTask;
        renderTaskResults();
        if (open) openTaskResult(open);
      } catch (_) {
        notice("无法读取任务更新，请刷新工作台");
      }
    });
  }
}

const taskStatus = {
  ready: "就绪",
  waiting_dependencies: "等待依赖",
  pending_review: "待验",
  returned: "已退回",
  accepted: "已通过",
  needs_recheck: "待复核",
};

function resetTaskResults() {
  state.tasks = [];
  state.openTask = null;
  state.taskFetch++;
  $("task-results").replaceChildren();
}

function renderTaskResults() {
  const list = $("task-results");
  list.replaceChildren();
  if (!state.tasks.length) list.append(element("p", "暂无可读任务", "empty"));
  for (const task of state.tasks) {
    const row = element("div", null, "goal");
    const button = element("button", `${task.terms.deliverables[0]} · ${taskStatus[task.status] || task.status}`);
    button.type = "button";
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", `task-detail-${task.id}`);
    button.onclick = () => {
      if (state.openTask === task.id) {
        state.openTask = null;
        state.taskFetch++;
        renderTaskResults();
      } else {
        renderTaskResults();
        openTaskResult(task.id);
      }
    };
    const detail = element("div");
    detail.id = `task-detail-${task.id}`;
    row.append(button, detail);
    list.append(row);
  }
}

function addResultNotes(parent, title, notes) {
  if (!notes.length) return;
  const list = element("ul");
  for (const note of notes) list.append(element("li", note));
  parent.append(element("strong", title), list);
}

function resultVersionList(versions) {
  const list = element("ul");
  for (const version of versions) {
    const link = element("a", `${version.source_path} · v${version.version}`);
    link.href = `/v1/artifacts/${encodeURIComponent(version.artifact_id)}/versions/${encodeURIComponent(version.version)}`;
    link.download = version.source_path.split(/[\\/]/).pop();
    const item = element("li");
    item.append(link, element("div", `版本 ID：${version.version_id}`));
    list.append(item);
  }
  return list;
}

async function openTaskResult(taskId) {
  const detail = $(`task-detail-${taskId}`);
  if (!detail) return;
  state.openTask = taskId;
  const request = ++state.taskFetch;
  detail.previousElementSibling.setAttribute("aria-expanded", "true");
  detail.textContent = "正在读取交付详情";
  try {
    const task = await api(`/v1/teams/${encodeURIComponent(state.team)}/tasks/${encodeURIComponent(taskId)}`);
    if (request !== state.taskFetch) return;
    const result = task.result;
    detail.replaceChildren(element("p", `任务修订 ${task.revision} · ${taskStatus[task.status] || task.status}`));
    if (result.stale_input_versions.length)
      detail.append(element("strong", "需复核的旧输入版本"), resultVersionList(result.stale_input_versions));
    if (!result.submissions.length) detail.append(element("p", "暂无可读提交", "empty"));
    for (const submission of result.submissions) {
      const entry = element("details");
      const current = submission.id === result.current_submission_id;
      entry.open = current;
      entry.append(element("summary", `${current ? "当前提交" : "历史提交"} · 修订 ${submission.task_revision} · ${formatTime(submission.created_at)}`));
      entry.append(element("p", `提交 ${submission.id} · 负责人 ${nameOf(submission.owner_id)}`));
      if (submission.previous_return_id) entry.append(element("p", `关联退回决定 ${submission.previous_return_id}`));
      entry.append(resultVersionList(submission.versions));
      addResultNotes(entry, "验证证据", submission.evidence);
      addResultNotes(entry, "遗留问题", submission.open_issues);
      const review = submission.review;
      if (review) {
        entry.append(element("p", `${review.decision === "approved" ? "通过" : "退回"} · 主审 ${nameOf(review.reviewer_id)} · ${formatTime(review.created_at)}`));
        entry.append(element("p", `审查决定 ${review.id}`));
        addResultNotes(entry, "审查依据", review.findings.map((finding) =>
          `${finding.requirement}：${finding.met ? "通过" : "未通过"}；${finding.evidence}`));
        if (review.expected) entry.append(element("p", `退回预期：${review.expected}`));
        addResultNotes(entry, "复验项", review.recheck_items);
      } else {
        entry.append(element("p", "尚无审查决定"));
      }
      detail.append(entry);
    }
  } catch (error) {
    if (request === state.taskFetch) detail.textContent = error.message;
  }
}
async function refreshApprovals(team, generation) {
  const request = ++state.approvalFetch;
  try {
    const approvals = await api(`/v1/teams/${encodeURIComponent(team)}/approvals`);
    if (generation !== state.generation || team !== state.team || request !== state.approvalFetch || state.actor !== "local-operator") return;
    state.approvals = approvals.filter((a) => a.status === "awaiting_approval");
    renderApprovals();
  } catch (e) {
    if (generation === state.generation && team === state.team && request === state.approvalFetch) notice(`审批列表加载失败：${e.message}`);
  }
}
function safeRepository(value) {
  // Display only a sanitized address; the original remains in the signed decision body.
  return String(value).replace(/(\w+:\/\/)[^/@\s]+@/g, "$1[已隐藏]@").replace(/^[^/@\s]+@/, "[已隐藏]@").replace(/[?#].*$/, "?[已隐藏]");
}
function renderApprovals() {
  const section = $("approval-section");
  section.hidden = state.actor !== "local-operator";
  const list = $("approvals");
  list.replaceChildren();
  if (section.hidden) return;
  if (!state.approvals.length) list.append(element("p", "暂无待审批推送", "empty"));
  for (const approval of state.approvals) {
    const row = element("div", null, "approval");
    row.append(element("strong", "推送请求"),
      element("div", `仓库：${safeRepository(approval.remote_repository ?? approval.repository)}`),
      element("div", `目标 ref：${approval.ref}`),
      element("div", `提交 SHA：${approval.sha}`),
      element("div", `摘要：${approval.digest}`));
    if (approval.member_id) row.append(element("div", `申请人：${nameOf(approval.member_id)}`));
    for (const [decision, label] of [["approve", "批准此提交"], ["reject", "拒绝"]]) {
      const button = element("button", label);
      button.type = "button";
      button.disabled = !!state.approvalRequests.get(approval.approval_id)?.busy;
      button.onclick = () => decideApproval(approval, decision);
      row.append(button);
    }
    list.append(row);
  }
}
async function decideApproval(approval, decision) {
  const team = state.team, generation = state.generation;
  let pending = state.approvalRequests.get(approval.approval_id);
  if (pending?.busy) return;
  if (pending && pending.decision !== decision) {
    notice("上次决定的结果尚未确认，请重试相同决定或刷新审批列表。");
    return;
  }
  if (!pending) pending = {request_id: key(), decision, busy: false};
  pending.busy = true;
  state.approvalRequests.set(approval.approval_id, pending);
  renderApprovals();
  try {
    await api(`/v1/approvals/${encodeURIComponent(approval.approval_id)}/decision`, {
      method: "POST",
      body: JSON.stringify({request_id: pending.request_id, decision, action: approval.action,
        repository: approval.repository, ref: approval.ref, sha: approval.sha, digest: approval.digest}),
    });
    if (generation !== state.generation || team !== state.team) return;
    state.approvals = state.approvals.filter((a) => a.approval_id !== approval.approval_id);
    state.approvalRequests.delete(approval.approval_id);
    notice(decision === "approve" ? "已批准该提交的推送" : "已拒绝该推送");
    await refreshApprovals(team, generation);
  } catch (e) {
    if (generation === state.generation && team === state.team) notice(`审批结果未确认：${e.message}；可重试相同决定`);
  } finally {
    pending.busy = false;
    if (generation === state.generation && team === state.team) renderApprovals();
  }
}
function messageSummary(id) {
  const message = state.messages.find((m) => m.id === id);
  return message ? message.text.slice(0, 120) : `消息 ${id}`;
}
function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}
function render() {
  const nav = $("conversations");
  nav.replaceChildren();
  const add = (label, peer) => {
    const b = element("button", label, state.peer === peer ? "active" : "");
    b.type = "button";
    b.onclick = () => {
      state.peer = peer;
      state.reply = null;
      render();
    };
    nav.append(b);
  };
  add("群聊", null);
  state.participants
    .filter((p) => p.id !== state.actor)
    .forEach((p) => add(p.name, p.id));
  $("kind")
    .querySelectorAll("option")
    .forEach((o) => {
      o.hidden =
        state.actor !== "local-operator" &&
        ["assignment", "supplement"].includes(o.value);
    });
  if (
    state.actor !== "local-operator" &&
    ["assignment", "supplement"].includes($("kind").value)
  )
    $("kind").value = "chat";
  const canSteer = state.actor === "local-operator" && !state.peer && $("kind").value === "supplement" && state.messages.some(m => m.id === state.reply && m.kind === "assignment");
  $("steer-active").disabled = !canSteer;
  if (!canSteer) $("steer-active").checked = false;
  $("conversation-title").textContent = state.peer
    ? `与 ${nameOf(state.peer)} 的私聊`
    : "群聊";
  const mentions = $("mentions");
  const selected = new Set([...mentions.selectedOptions].map((o) => o.value));
  mentions.replaceChildren();
  state.participants
    .filter(
      (p) =>
        p.id !== state.actor &&
        p.id !== "local-operator" &&
        (!state.peer || p.id === state.peer),
    )
    .forEach((p) => {
      const o = element("option", p.name);
      o.value = p.id;
      o.selected = selected.has(p.id);
      mentions.append(o);
    });
  const list = $("messages");
  list.replaceChildren();
  const visible = state.messages.filter((m) =>
    state.peer
      ? (m.sender_id === state.actor && m.recipient_id === state.peer) ||
        (m.sender_id === state.peer && m.recipient_id === state.actor)
      : !m.recipient_id,
  );
  if (!visible.length) list.append(element("p", "这里还没有消息", "empty"));
  for (const m of visible) {
    const item = element("article", null, "message");
    const meta = element("div", null, "meta");
    meta.append(
      element("strong", nameOf(m.sender_id)),
      element("span", m.kind || "chat"),
      element("time", formatTime(m.created_at)),
    );
    item.append(meta);
    if (m.reply_to)
      item.append(
        element("div", `引用：${messageSummary(m.reply_to)}`, "quote"),
      );
    if (m.mentions?.length)
      item.append(
        element("div", `点名：${m.mentions.map(nameOf).join("、")}`, "quote"),
      );
    item.append(element("p", m.text));
    if (m.active_input?.length) item.append(element("div", m.active_input.map(x => `${nameOf(x.member_id)}：${x.status === "applied" ? "已应用" : x.status === "fallback" ? "转普通投递" : x.status === "cancelled" ? "已取消" : "已排队"}`).join("、"), "quote"));
    const question = state.questions.find((q) => q.question_id === m.id);
    if (m.kind !== "question" || (state.actor === "local-operator" && question && !question.answer_message_id)) {
      const reply = element("button", m.kind === "question" ? "答复" : "引用");
      reply.type = "button";
      reply.onclick = () => {
        state.reply = m.id;
        if (m.kind === "question") $("kind").value = "chat";
        renderReply();
        $("text").focus();
      };
      item.append(reply);
    }
    if (
      state.actor === state.lead &&
      m.sender_id === "local-operator" &&
      m.kind === "assignment" &&
      !state.goals.some((g) => g.source_message_id === m.id)
    ) {
      const goal = element("button", "登记目标");
      goal.type = "button";
      goal.onclick = () => registerGoal(m.id, goal);
      item.append(goal);
    }
    list.append(item);
  }
  renderReply();
  renderQuestions();
  const goals = $("goals");
  goals.replaceChildren();
  if (!state.goals.length) goals.append(element("p", "暂无目标", "empty"));
  for (const g of state.goals) {
    const row = element("div", null, "goal");
    row.append(
      element(
        "strong",
        g.status === "active"
          ? "活动中"
          : g.status === "queued"
            ? "排队中"
            : g.status,
      ),
      element("div", `来源：${messageSummary(g.source_message_id)}`),
      element("div", `修订 ${g.revision}`),
    );
    goals.append(row);
  }
}
function renderQuestions() {
  const list = $("questions");
  list.replaceChildren();
  const pending = state.questions.filter((q) => !q.answer_message_id);
  if (!pending.length) list.append(element("p", "暂无待答问题", "empty"));
  for (const q of pending) {
    const message = state.messages.find((m) => m.id === q.question_id);
    if (!message) continue;
    const row = element("div", null, "question");
    row.append(
      element("strong", nameOf(q.asker_id)),
      element("div", message.text.slice(0, 120)),
      element("small", message.recipient_id ? `私聊 · ${nameOf(q.asker_id)}` : "群聊"),
    );
    if (state.actor === "local-operator") {
      const answer = element("button", "答复");
      answer.type = "button";
      answer.onclick = () => {
        state.peer = message.recipient_id
          ? (message.sender_id === state.actor ? message.recipient_id : message.sender_id)
          : null;
        state.reply = q.question_id;
        $("kind").value = "chat";
        render();
        $("text").focus();
      };
      row.append(answer);
    }
    list.append(row);
  }
}
function renderReply() {
  const box = $("reply");
  box.replaceChildren();
  box.hidden = !state.reply;
  if (state.reply) {
    box.append(
      document.createTextNode(`引用：${messageSummary(state.reply)} `),
    );
    const cancel = element("button", "取消");
    cancel.type = "button";
    cancel.onclick = () => {
      state.reply = null;
      renderReply();
    };
    box.append(cancel);
  }
}
async function registerGoal(id, button) {
  button.disabled = true;
  try {
    await api(`/v1/teams/${encodeURIComponent(state.team)}/goals`, {
      method: "POST",
      body: JSON.stringify({ request_id: key(), source_message_id: id }),
    });
    notice("目标登记请求已提交，等待实时更新");
  } catch (e) {
    notice(e.message);
  } finally {
    button.disabled = false;
  }
}
$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    await api("/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ token: $("token").value }),
    });
    $("token").value = "";
    notice("");
    await loadTeams();
  } catch (err) {
    notice(err.message);
  }
};
$("logout").onclick = async () => {
  try {
    await api("/v1/auth/session", { method: "DELETE" });
    showLogin();
    notice("");
  } catch (e) {
    notice(e.message);
  }
};
$("teams").onchange = (e) => {
  notice("");
  loadTeam(e.target.value);
};
$("refresh").onclick = () => {
  notice("");
  (state.team ? loadTeam(state.team, true) : loadTeams()).catch((e) =>
    notice(e.message),
  );
};
$("composer").onsubmit = async (e) => {
  e.preventDefault();
  const text = $("text").value.trim();
  if (!text || !state.team) return;
  const question = state.questions.find((q) => q.question_id === state.reply);
  if (question?.answer_message_id) {
    notice("该问题已有答复，请取消引用后发送其他消息");
    return;
  }
  const body = {
    text,
    kind: $("kind").value,
    ...($("steer-active").checked ? { steer_active: true } : {}),
    mentions: [...$("mentions").selectedOptions].map((o) => o.value),
    ...(state.peer ? { recipient_id: state.peer } : {}),
    ...(state.reply ? { reply_to: state.reply } : {}),
  };
  const signature = JSON.stringify([state.team, body]);
  if (!state.pending || state.pending.signature !== signature)
    state.pending = { signature, request_id: key() };
  const team = state.team;
  const generation = state.generation;
  $("send").disabled = true;
  try {
    const sent = await api(`/v1/teams/${encodeURIComponent(team)}/messages`, {
      method: "POST",
      body: JSON.stringify({ ...body, request_id: state.pending.request_id }),
    });
    if (state.team === team && state.generation === generation) {
      $("text").value = "";
      state.reply = null;
      state.pending = null;
      renderReply();
      if (sent.active_input?.length) {
        const existing = state.messages.find(m => m.id === sent.message_id);
        if (existing) existing.active_input = sent.active_input;
        $("steer-active").checked = false;
        render();
      }
      notice(sent.active_input?.map(x => `${nameOf(x.member_id)}：${x.status === "queued" ? "已排队" : "普通投递"}`).join("、") || "");
    }
  } catch (err) {
    notice(`${err.message}；可再次发送以重试`);
  } finally {
    if (state.team && state.generation === generation)
      $("send").disabled = false;
  }
};
$("kind").addEventListener("change", render);
loadTeams().catch((e) => notice(e.message));
