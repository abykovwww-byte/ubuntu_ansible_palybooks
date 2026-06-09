const state = {
  tasks: [],
  adminTasks: [],
  admin: false,
  editingId: "",
};

const elements = {
  publicView: document.querySelector("#publicView"),
  adminView: document.querySelector("#adminView"),
  taskList: document.querySelector("#taskList"),
  taskCount: document.querySelector("#taskCount"),
  notifyPermission: document.querySelector("#notifyPermission"),
  loginForm: document.querySelector("#loginForm"),
  logoutButton: document.querySelector("#logoutButton"),
  adminPanel: document.querySelector("#adminPanel"),
  taskForm: document.querySelector("#taskForm"),
  resetForm: document.querySelector("#resetForm"),
  adminTaskList: document.querySelector("#adminTaskList"),
  toastStack: document.querySelector("#toastStack"),
};

const isAdminRoute = () => window.location.pathname === "/admin";

function formatTime(value) {
  if (!value) return "без времени";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "без времени";
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function api(path, options = {}) {
  return fetch(path, {
    headers: { "content-type": "application/json", ...(options.headers || {}) },
    credentials: "same-origin",
    ...options,
  }).then(async (response) => {
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || "Ошибка запроса.");
    return body;
  });
}

function taskTemplate(task, admin = false) {
  const card = document.createElement("article");
  card.className = admin ? "task-card admin-card" : "task-card";
  card.innerHTML = `
    <div>
      <h3 class="task-title"></h3>
      <p class="task-notes"></p>
    </div>
    <span class="task-time"></span>
  `;
  card.querySelector(".task-title").textContent = task.title;
  card.querySelector(".task-notes").textContent = task.notes || "Нет деталей";
  card.querySelector(".task-time").textContent = formatTime(task.triggerAt);

  if (admin) {
    const footer = document.createElement("footer");
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "secondary";
    edit.textContent = "Редактировать";
    edit.addEventListener("click", () => editTask(task));

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger";
    remove.textContent = "Удалить";
    remove.addEventListener("click", () => deleteTask(task.id));

    footer.append(edit, remove);
    card.append(footer);
  }

  return card;
}

function renderPublicTasks() {
  elements.taskList.replaceChildren();
  elements.taskCount.textContent = state.tasks.length;

  if (state.tasks.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Активных задач пока нет.";
    elements.taskList.append(empty);
    return;
  }

  state.tasks.forEach((task) => elements.taskList.append(taskTemplate(task)));
}

function renderAdminTasks() {
  elements.adminTaskList.replaceChildren();

  if (state.adminTasks.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Создай первую задачу.";
    elements.adminTaskList.append(empty);
    return;
  }

  state.adminTasks.forEach((task) => elements.adminTaskList.append(taskTemplate(task, true)));
}

async function loadTasks() {
  state.tasks = await api("/api/tasks");
  renderPublicTasks();
  checkTriggers();
}

async function loadAdminTasks() {
  state.adminTasks = await api("/api/admin/tasks");
  renderAdminTasks();
}

function route() {
  const admin = isAdminRoute();
  elements.publicView.classList.toggle("hidden", admin);
  elements.adminView.classList.toggle("hidden", !admin);
  document.querySelectorAll("[data-nav]").forEach((link) => {
    link.classList.toggle("active", link.dataset.nav === (admin ? "admin" : "home"));
  });
}

async function checkAdminSession() {
  try {
    await api("/api/admin/session");
    state.admin = true;
  } catch {
    state.admin = false;
  }
  elements.loginForm.classList.toggle("hidden", state.admin);
  elements.logoutButton.classList.toggle("hidden", !state.admin);
  elements.adminPanel.classList.toggle("hidden", !state.admin);
  if (state.admin) await loadAdminTasks();
}

function resetEditor() {
  state.editingId = "";
  elements.taskForm.reset();
  elements.taskForm.elements.enabled.checked = true;
}

function editTask(task) {
  state.editingId = task.id;
  elements.taskForm.elements.id.value = task.id;
  elements.taskForm.elements.title.value = task.title;
  elements.taskForm.elements.notes.value = task.notes || "";
  elements.taskForm.elements.triggerAt.value = task.triggerAt || "";
  elements.taskForm.elements.enabled.checked = Boolean(task.enabled);
  elements.taskForm.elements.completed.checked = Boolean(task.completed);
  elements.taskForm.scrollIntoView({ behavior: "smooth", block: "start" });
}

async function deleteTask(id) {
  await api(`/api/admin/tasks/${id}`, { method: "DELETE" });
  await Promise.all([loadTasks(), loadAdminTasks()]);
  resetEditor();
}

function collectFormTask() {
  const form = elements.taskForm.elements;
  return {
    title: form.title.value,
    notes: form.notes.value,
    triggerAt: form.triggerAt.value,
    enabled: form.enabled.checked,
    completed: form.completed.checked,
  };
}

function dismissedKey(task) {
  return `task-reminder-dismissed-${task.id}-${task.triggerAt}`;
}

function checkTriggers() {
  const now = Date.now();
  state.tasks.forEach((task) => {
    if (!task.triggerAt || localStorage.getItem(dismissedKey(task))) return;
    const due = new Date(task.triggerAt).getTime();
    if (!Number.isNaN(due) && due <= now) showReminder(task);
  });
}

function showReminder(task) {
  if (document.querySelector(`[data-toast-id="${task.id}"]`)) return;

  const toast = document.createElement("button");
  toast.type = "button";
  toast.className = "toast";
  toast.dataset.toastId = task.id;
  toast.innerHTML = `<strong></strong><span></span>`;
  toast.querySelector("strong").textContent = task.title;
  toast.querySelector("span").textContent = task.notes || "Пора вернуться к задаче.";
  toast.addEventListener("click", () => {
    localStorage.setItem(dismissedKey(task), "1");
    toast.remove();
  });
  elements.toastStack.append(toast);

  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(task.title, {
      body: task.notes || "Пора вернуться к задаче.",
      tag: task.id,
      requireInteraction: true,
    });
  }
}

elements.notifyPermission.addEventListener("click", async () => {
  if (!("Notification" in window)) {
    elements.notifyPermission.textContent = "Не поддерживается";
    return;
  }
  const permission = await Notification.requestPermission();
  elements.notifyPermission.textContent = permission === "granted" ? "Уведомления включены" : "Уведомления выключены";
});

elements.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  await api("/api/admin/login", {
    method: "POST",
    body: JSON.stringify({ password: elements.loginForm.elements.password.value }),
  });
  elements.loginForm.reset();
  await checkAdminSession();
});

elements.logoutButton.addEventListener("click", async () => {
  await api("/api/admin/logout", { method: "POST" });
  state.admin = false;
  resetEditor();
  await checkAdminSession();
});

elements.taskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const task = collectFormTask();
  if (state.editingId) {
    await api(`/api/admin/tasks/${state.editingId}`, { method: "PUT", body: JSON.stringify(task) });
  } else {
    await api("/api/admin/tasks", { method: "POST", body: JSON.stringify(task) });
  }
  resetEditor();
  await Promise.all([loadTasks(), loadAdminTasks()]);
});

elements.resetForm.addEventListener("click", resetEditor);

route();
await loadTasks();
if (isAdminRoute()) await checkAdminSession();
setInterval(loadTasks, 30000);
setInterval(checkTriggers, 5000);
