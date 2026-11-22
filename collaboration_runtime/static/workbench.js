"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  team: null,
  actor: null,
  lead: null,
  participants: [],
  messages: [],
  goals: [],
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
  $("messages").replaceChildren();
  $("goals").replaceChildren();
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
  if (!preserve || oldTeam !== id) {
    clearDraft();
    state.peer = null;
    state.messages = [];
    state.goals = [];
    $("messages").replaceChildren();
    $("goals").replaceChildren();
    $("conversations").replaceChildren();
    $("conversation-title").textContent = "群聊";
  }
  const generation = ++state.generation;
  state.stream?.close();
  state.stream = null;
  state.team = id;
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
    state.seq = Math.max(0, ...state.messages.map((m) => Number(m.seq) || 0));
    render();
    setComposerEnabled(true);
    connect(s.cursor, generation);
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
        if (Number(data.seq) <= state.seq) return;
        state.seq = Number(data.seq);
        if (
          type === "message.created" &&
          !state.messages.some((m) => m.id === data.message.id)
        )
          state.messages.push(data.message);
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
  stream.addEventListener("stream.reset", () => {
    if (generation === state.generation) loadTeam(state.team, true);
  });
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
    const reply = element("button", "引用");
    reply.type = "button";
    reply.onclick = () => {
      state.reply = m.id;
      renderReply();
      $("text").focus();
    };
    item.append(reply);
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
  const body = {
    text,
    kind: $("kind").value,
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
    await api(`/v1/teams/${encodeURIComponent(team)}/messages`, {
      method: "POST",
      body: JSON.stringify({ ...body, request_id: state.pending.request_id }),
    });
    if (state.team === team && state.generation === generation) {
      $("text").value = "";
      state.reply = null;
      state.pending = null;
      renderReply();
      notice("");
    }
  } catch (err) {
    notice(`${err.message}；可再次发送以重试`);
  } finally {
    if (state.team && state.generation === generation)
      $("send").disabled = false;
  }
};
loadTeams().catch((e) => notice(e.message));
