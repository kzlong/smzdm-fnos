"use strict";

const prefix = "/app/smzdm-checkin/api";
const add = document.querySelector("#add");
const accountList = document.querySelector("#accounts");
const historyList = document.querySelector("#history");
const emptyHistory = document.querySelector("#history-empty");
const status = document.querySelector("#status");
const editor = document.querySelector("#editor");
const form = document.querySelector("#account-form");
const fields = form.elements;
const formStatus = document.querySelector("#form-status");
const cookieInput = document.querySelector("#smzdm-cookie-value");
const showCookie = document.querySelector("#smzdm-cookie-visibility");
const clearHistory = document.querySelector("#clear-history");
let clearingHistory = false;
let editing = null;
let refreshing = false;

async function request(path, payload) {
  const options = { cache: "no-store" };
  if (payload) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-SMZDM-Request": "1" };
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(prefix + path, options);
  if (!response.headers.get("content-type")?.includes("application/json")) {
    throw new Error("无法连接签到服务，请检查应用是否已启动。");
  }
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求失败");
  return data;
}

function element(tag, text, className) {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}

function button(text, callback, disabled = false, className = "") {
  const item = element("button", text, className);
  item.type = "button";
  item.disabled = disabled;
  item.addEventListener("click", callback);
  return item;
}

function openEditor(account = null) {
  editing = account?.id ?? null;
  form.reset();
  setCookieVisibility(false);
  formStatus.textContent = "";
  document.querySelector("#editor-title").textContent = account ? "编辑账号" : "添加账号";
  fields.label.value = account?.label ?? "";
  fields.platform.value = account?.platform ?? "auto";
  fields.cookie.required = !account;
  fields.cookie.value = account?.cookie ?? "";
  fields.sk.value = account?.sk ?? "";
  fields.cookie.placeholder = "粘贴 App 请求的完整 Cookie";
  fields.sk.placeholder = account ? "同时更新 Cookie 时，留空自动生成 SK" : "同一设备的签到请求 SK";
  fields.scheduled.checked = Boolean(account?.daily_time);
  fields.daily_time.value = account?.daily_time || "09:30";
  fields.extra_rewards.checked = account?.task_options?.extra_rewards ?? true;
  fields.lottery_tasks.checked = account?.task_options?.lottery ?? false;
  fields.daily_tasks.checked = account?.task_options?.daily ?? false;
  fields.follow_tasks.checked = account?.task_options?.follow ?? true;
  updateDailyTasks();
  fields.bark_mode.value = account?.bark_mode ?? "off";
  fields.bark_push.value = account?.bark_push ?? "";
  fields.bark_push.placeholder = "多个目标用英文逗号分隔";
  updateTimeField();
  updatePlatformField();
  updateBarkField();
  editor.showModal();
}

function updateTimeField() {
  document.querySelector("#time-field").hidden = !fields.scheduled.checked;
}

function updatePlatformField() {
  document.querySelector("#sk-field").hidden = fields.platform.value === "iphone";
}

function updateBarkField() {
  document.querySelector("#bark-target-field").hidden = fields.bark_mode.value === "off";
}

function updateDailyTasks() {
  document.querySelector("#follow-field").hidden = !fields.daily_tasks.checked;
}

function setCookieVisibility(visible) {
  cookieInput.type = visible ? "text" : "password";
  showCookie.setAttribute("aria-pressed", String(visible));
  const label = visible ? "隐藏 Cookie" : "显示 Cookie";
  showCookie.setAttribute("aria-label", label);
  showCookie.title = label;
  showCookie.querySelector(".eye-slash").toggleAttribute("hidden", !visible);
}

async function action(path, account, item) {
  item.disabled = true;
  try {
    await request(path, { id: account.id });
    await refresh();
  } catch (error) {
    status.textContent = error.message;
    item.disabled = false;
  }
}

function renderAccounts(accounts) {
  const cards = accounts.map((account) => {
    const card = element("article", "", "card");
    const info = element("div", "");
    const schedule = account.daily_time ? "每天 " + account.daily_time + "（北京时间）" : "仅手动签到";
    const platform = account.detected_platform === "iphone" ? "iPhone / iPad" : "Android";
    info.append(element("strong", account.label), element("p", platform + " · " + schedule, "meta"));
    const controls = element("div", "", "actions");
    controls.append(
      button(account.running ? "签到中…" : "立即签到", (event) => action("/checkin", account, event.currentTarget), account.running, "primary"),
      button("编辑", () => openEditor(account), account.running),
      button("删除", (event) => {
        if (confirm("删除「" + account.label + "」及其签到记录？")) action("/accounts/remove", account, event.currentTarget);
      }, account.running, "danger")
    );
    card.append(info, controls);
    return card;
  });
  accountList.replaceChildren(...(cards.length ? cards : [element("p", "添加账号后即可在飞牛上发起签到。", "empty")]));
}

function renderHistory(history) {
  clearHistory.disabled = clearingHistory || history.length === 0;
  emptyHistory.hidden = history.length > 0;
  historyList.replaceChildren(...history.map((run) => {
    const entry = element("li", "");
    const date = element("time", run.started.slice(0, 19).replace("T", " ") + " · " + (run.source === "auto" ? "自动" : "手动"));
    date.dateTime = run.started;
    entry.append(date, element("span", run.label + " · " + run.message, "message " + (run.success ? "ok" : "failed")));
    return entry;
  }));
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const data = await request("/state");
    status.textContent = "";
    add.disabled = false;
    renderAccounts(data.accounts);
    renderHistory(data.history);
  } catch (error) {
    add.disabled = true;
    clearHistory.disabled = true;
    status.textContent = error.message;
    if (!accountList.querySelector(".card")) accountList.replaceChildren();
  } finally {
    refreshing = false;
  }
}

add.addEventListener("click", () => openEditor());
document.querySelector("#cancel").addEventListener("click", () => editor.close());
editor.addEventListener("close", () => {
  form.reset();
  setCookieVisibility(false);
});
showCookie.addEventListener("click", () => {
  setCookieVisibility(cookieInput.type === "password");
});
clearHistory.addEventListener("click", async () => {
  clearingHistory = true;
  clearHistory.disabled = true;
  try {
    await request("/history/clear", {});
    historyList.replaceChildren();
    emptyHistory.hidden = false;
  } catch (error) {
    status.textContent = error.message;
  } finally {
    clearingHistory = false;
    clearHistory.disabled = historyList.children.length === 0;
  }
});
fields.scheduled.addEventListener("change", updateTimeField);
fields.platform.addEventListener("change", updatePlatformField);
fields.bark_mode.addEventListener("change", updateBarkField);
fields.daily_tasks.addEventListener("change", updateDailyTasks);
form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (fields.scheduled.checked && !fields.daily_time.value) {
    formStatus.textContent = "请选择签到时间。";
    return;
  }
  formStatus.textContent = "";
  const submit = event.submitter;
  submit.disabled = true;
  try {
    await request("/accounts/save", {
      id: editing, label: fields.label.value, cookie: fields.cookie.value,
      platform: fields.platform.value, sk: fields.platform.value === "iphone" ? "" : fields.sk.value,
      bark_mode: fields.bark_mode.value, bark_push: fields.bark_mode.value === "off" ? "" : fields.bark_push.value,
      task_options: { extra_rewards: fields.extra_rewards.checked, lottery: fields.lottery_tasks.checked,
        daily: fields.daily_tasks.checked, follow: fields.follow_tasks.checked },
      daily_time: fields.scheduled.checked ? fields.daily_time.value : ""
    });
    editor.close();
    await refresh();
  } catch (error) {
    formStatus.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
refresh();
setInterval(() => { if (!editor.open) refresh(); }, 4000);
