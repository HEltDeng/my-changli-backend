const state = {
  token: sessionStorage.getItem("myChangliAdminToken") || "",
  page: "overview",
  venues: [],
};

const titles = {
  overview: "概览",
  venues: "场地与库存",
  reservations: "预约记录",
  lost: "失物招领",
  announcements: "公告",
  config: "版本与功能",
  feedback: "用户反馈",
};

const $ = (selector) => document.querySelector(selector);
const content = $("#page-content");

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(Number(value)));
}

function statusText(value) {
  return {
    OPEN: "开放",
    CLOSED: "关闭",
    MAINTENANCE: "维护",
    ACTIVE: "已预约",
    CANCELLED: "已取消",
    LOST: "寻物",
    FOUND: "招领",
    SEARCHING: "寻找中",
    FOUND_STATUS: "已找回",
    WAITING: "等待认领",
    CLAIMED: "已认领",
    RETURNED: "已归还",
    REVIEWED: "已查看",
    PROCESSING: "处理中",
  }[value] || value || "—";
}

function badge(value) {
  const kind = ["OPEN", "ACTIVE", "WAITING", "SEARCHING"].includes(value)
    ? "ok"
    : ["MAINTENANCE", "PROCESSING", "CLAIMED"].includes(value)
      ? "warn"
      : ["CLOSED", "CANCELLED", "RETURNED"].includes(value)
        ? "bad"
        : "";
  return `<span class="badge ${kind}">${escapeHtml(statusText(value))}</span>`;
}

function toast(message, isError = false) {
  const node = $("#toast");
  node.textContent = message;
  node.className = `toast show${isError ? " error-toast" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    node.className = "toast";
  }, 2800);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && state.token) showLogin();
    throw new Error(payload?.error?.message || `操作失败（${response.status}）`);
  }
  return payload.data;
}

function showLogin() {
  state.token = "";
  sessionStorage.removeItem("myChangliAdminToken");
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
  $("#login-password").value = "";
}

function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  navigate(state.page);
}

async function navigate(page) {
  state.page = page;
  $("#page-title").textContent = titles[page];
  document.querySelectorAll("#nav button").forEach((button) => {
    button.classList.toggle("active", button.dataset.page === page);
  });
  content.innerHTML = '<div class="empty">正在读取本地数据…</div>';
  try {
    await renderers[page]();
  } catch (error) {
    content.innerHTML = `<div class="empty">读取失败：${escapeHtml(error.message)}<br><button class="ghost" data-action="reload">重新加载</button></div>`;
  }
}

const renderers = {
  async overview() {
    const data = await api("/api/v1/admin/overview");
    const metrics = [
      ["当前有效预约", data.activeReservations],
      ["今日预约", data.todayReservations],
      ["待处理失物", data.lostFoundOpen],
      ["待处理举报", data.openReports],
      ["待处理反馈", data.openFeedback],
      ["已发布公告", data.enabledAnnouncements],
    ];
    content.innerHTML = `
      <div class="grid metric-grid">
        ${metrics.map(([label, value]) => `<article class="card metric"><span>${label}</span><strong>${value}</strong></article>`).join("")}
      </div>
      <article class="card section-title">
        <h3>本地运行说明</h3>
        <p class="muted">这里的修改会直接写入当前电脑上的数据库。平板 App 刷新对应页面后即可看到变化。本阶段没有真实支付、预约码或公网服务。</p>
      </article>`;
  },

  async venues() {
    state.venues = await api("/api/v1/admin/venues");
    content.innerHTML = `
      <div class="grid two-columns">
        ${state.venues.map((venue) => `
          <article class="card">
            <div class="card-header">
              <div>
                <h3>${escapeHtml(venue.name)}</h3>
                <p class="subtle">${venue.sport === "BADMINTON" ? "羽毛球" : "乒乓球"} · ${venue.court_count} ${escapeHtml(venue.unit_label)} · ${venue.price_yuan} 元/小时</p>
              </div>
              ${badge(venue.operating_status)}
            </div>
            <p class="subtle">${escapeHtml(venue.maintenance_note || "暂无维护说明")}</p>
            <div class="slot-list">
              ${venue.timeSlots.map((slot) => `<button class="slot ${slot.enabled ? "" : "off"}" data-action="toggle-slot" data-id="${escapeHtml(slot.id)}" data-enabled="${slot.enabled}">${escapeHtml(slot.timeSlot)}</button>`).join("")}
            </div>
            <div class="actions section-title">
              <button class="primary" data-action="inventory" data-id="${escapeHtml(venue.id)}">管理日期库存</button>
              <button class="ghost" data-action="edit-venue" data-id="${escapeHtml(venue.id)}">编辑场馆</button>
              <button class="ghost" data-action="add-slot" data-id="${escapeHtml(venue.id)}">新增时间段</button>
            </div>
          </article>`).join("")}
      </div>`;
  },

  async reservations(filters = {}) {
    const params = new URLSearchParams(filters);
    const [rows, venues] = await Promise.all([
      api(`/api/v1/admin/reservations?${params}`),
      state.venues.length ? Promise.resolve(state.venues) : api("/api/v1/admin/venues"),
    ]);
    state.venues = venues;
    content.innerHTML = `
      <form id="reservation-filter" class="toolbar">
        <label class="compact">场馆<select name="venueId"><option value="">全部场馆</option>${venues.map((v) => `<option value="${escapeHtml(v.id)}" ${filters.venueId === v.id ? "selected" : ""}>${escapeHtml(v.name)}</option>`).join("")}</select></label>
        <label class="compact">日期<input type="date" name="date" value="${escapeHtml(filters.date || "")}"></label>
        <label class="compact">状态<select name="status"><option value="">全部状态</option><option value="ACTIVE" ${filters.status === "ACTIVE" ? "selected" : ""}>已预约</option><option value="CANCELLED" ${filters.status === "CANCELLED" ? "selected" : ""}>已取消</option></select></label>
        <label>用户<input name="user" value="${escapeHtml(filters.user || "")}" placeholder="输入用户标识"></label>
        <button class="primary">筛选</button>
        <button type="button" class="ghost" data-action="clear-reservation-filter">清空</button>
      </form>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>场馆</th><th>日期 / 时段</th><th>场地</th><th>用户</th><th>金额</th><th>状态</th><th>操作</th></tr></thead><tbody>
        ${rows.map((row) => `<tr><td>${escapeHtml(row.venue_name)}</td><td>${escapeHtml(row.booking_date)}<br><span class="subtle">${escapeHtml(row.time_slot)}</span></td><td>${row.court_number} ${escapeHtml(row.unit_label)}</td><td><span class="subtle">${escapeHtml(row.username)}</span></td><td>${row.price_yuan} 元</td><td>${badge(row.status)}</td><td>${row.status === "ACTIVE" ? `<button class="danger" data-action="cancel-reservation" data-id="${escapeHtml(row.id)}">取消预约</button>` : "—"}</td></tr>`).join("")}
      </tbody></table></div>` : '<div class="empty">没有符合条件的预约</div>'}`;
  },

  async lost(filters = {}) {
    const params = new URLSearchParams(filters);
    const [posts, reports] = await Promise.all([
      api(`/api/v1/admin/lost-found?${params}`),
      api("/api/v1/admin/lost-found-reports"),
    ]);
    content.innerHTML = `
      <form id="lost-filter" class="toolbar">
        <label class="compact">类型<select name="type"><option value="">全部</option><option value="LOST" ${filters.type === "LOST" ? "selected" : ""}>寻物</option><option value="FOUND" ${filters.type === "FOUND" ? "selected" : ""}>招领</option></select></label>
        <label class="compact">状态<input name="status" value="${escapeHtml(filters.status || "")}" placeholder="如 WAITING"></label>
        <label>关键词<input name="q" value="${escapeHtml(filters.q || "")}" placeholder="物品、地点或描述"></label>
        <button class="primary">筛选</button>
      </form>
      ${posts.length ? `<div class="table-wrap"><table><thead><tr><th>物品</th><th>类型</th><th>地点 / 日期</th><th>发布者</th><th>状态</th><th>举报</th><th>操作</th></tr></thead><tbody>
        ${posts.map((post) => `<tr><td><strong>${escapeHtml(post.item_name)}</strong><br><span class="subtle">${escapeHtml(post.category)}</span></td><td>${badge(post.type)}</td><td>${escapeHtml(post.location)}<br><span class="subtle">${escapeHtml(post.event_date)}</span></td><td><span class="subtle">${escapeHtml(post.username)}</span></td><td><select data-action="lost-status" data-id="${escapeHtml(post.id)}" data-type="${post.type}">${lostStatusOptions(post.type, post.status)}</select></td><td>${post.open_reports || 0}</td><td><button class="danger" data-action="delete-lost" data-id="${escapeHtml(post.id)}">删除</button></td></tr>`).join("")}
      </tbody></table></div>` : '<div class="empty">没有符合条件的失物信息</div>'}
      <h3 class="section-title">举报处理</h3>
      ${reports.length ? `<div class="table-wrap"><table><thead><tr><th>物品</th><th>原因</th><th>提交者</th><th>时间</th><th>状态</th></tr></thead><tbody>
        ${reports.map((report) => `<tr><td>${escapeHtml(report.item_name)}</td><td>${escapeHtml(report.reason)}</td><td><span class="subtle">${escapeHtml(report.username)}</span></td><td>${formatTime(report.created_at)}</td><td><select data-action="report-status" data-id="${escapeHtml(report.id)}">${options(["OPEN","REVIEWED","CLOSED"], report.status)}</select></td></tr>`).join("")}
      </tbody></table></div>` : '<div class="empty">暂无举报</div>'}`;
  },

  async announcements() {
    const rows = await api("/api/v1/admin/announcements");
    content.innerHTML = `
      <article class="card">
        <h3>发布新公告</h3>
        <form id="announcement-form" class="stack">
          <label>标题<input name="title" maxlength="100" required></label>
          <label>内容<textarea name="content" maxlength="4000" required></textarea></label>
          <label class="checkbox-row"><input type="checkbox" name="enabled" checked> 发布后立即在 App 配置接口中可见</label>
          <div><button class="primary">发布公告</button></div>
        </form>
      </article>
      <h3 class="section-title">公告记录</h3>
      <div class="grid">
        ${rows.length ? rows.map((row) => `<article class="card"><div class="card-header"><div><h3>${escapeHtml(row.title)}</h3><span class="subtle">${formatTime(row.published_at)}</span></div>${row.enabled ? badge("OPEN") : badge("CLOSED")}</div><p>${escapeHtml(row.content).replaceAll("\n", "<br>")}</p><div class="actions"><button class="ghost" data-action="edit-announcement" data-id="${escapeHtml(row.id)}" data-title="${escapeHtml(row.title)}" data-content="${escapeHtml(row.content)}" data-enabled="${row.enabled}">编辑</button><button class="${row.enabled ? "danger" : "primary"}" data-action="toggle-announcement" data-id="${escapeHtml(row.id)}" data-enabled="${row.enabled}">${row.enabled ? "撤回" : "重新发布"}</button></div></article>`).join("") : '<div class="empty">还没有公告</div>'}
      </div>`;
  },

  async config() {
    const data = await api("/api/v1/admin/config");
    const version = data.version || {};
    content.innerHTML = `
      <div class="grid two-columns">
        <article class="card">
          <h3>Android 版本提示</h3>
          <form id="version-form" class="stack">
            <label>最新版本名称<input name="latestVersionName" value="${escapeHtml(version.latest_version_name || "")}" required></label>
            <label>最新版本号<input type="number" min="1" name="latestVersionCode" value="${version.latest_version_code || 1}" required></label>
            <label>最低支持版本号<input type="number" min="1" name="minimumVersionCode" value="${version.minimum_version_code || 1}" required></label>
            <label>更新说明<textarea name="updateMessage">${escapeHtml(version.update_message || "")}</textarea></label>
            <label>下载地址（可留空）<input name="downloadUrl" value="${escapeHtml(version.download_url || "")}"></label>
            <button class="primary">保存版本配置</button>
          </form>
        </article>
        <article class="card">
          <h3>远程功能开关</h3>
          <p class="muted">关闭后，客户端可根据公开配置停止相应入口或操作。</p>
          <div class="stack">
            ${data.flags.map((flag) => `<label class="checkbox-row"><input type="checkbox" data-action="toggle-flag" data-key="${escapeHtml(flag.key)}" ${flag.enabled ? "checked" : ""}> ${escapeHtml(flag.key)}</label>`).join("")}
          </div>
        </article>
      </div>`;
  },

  async feedback(filters = {}) {
    const params = new URLSearchParams(filters);
    const rows = await api(`/api/v1/admin/feedback?${params}`);
    content.innerHTML = `
      <form id="feedback-filter" class="toolbar">
        <label class="compact">状态<select name="status"><option value="">全部</option>${options(["OPEN","PROCESSING","CLOSED"], filters.status, true)}</select></label>
        <button class="primary">筛选</button>
      </form>
      ${rows.length ? `<div class="table-wrap"><table><thead><tr><th>分类</th><th>内容</th><th>用户</th><th>App 版本</th><th>时间</th><th>状态</th></tr></thead><tbody>
        ${rows.map((row) => `<tr><td>${escapeHtml(row.category)}</td><td>${escapeHtml(row.content)}</td><td><span class="subtle">${escapeHtml(row.username || "匿名设备")}</span></td><td>${escapeHtml(row.app_version || "—")}</td><td>${formatTime(row.created_at)}</td><td><select data-action="feedback-status" data-id="${escapeHtml(row.id)}">${options(["OPEN","PROCESSING","CLOSED"], row.status)}</select></td></tr>`).join("")}
      </tbody></table></div>` : '<div class="empty">暂无用户反馈</div>'}`;
  },
};

function options(values, selected, allowBlank = false) {
  return `${allowBlank ? '<option value="">全部</option>' : ""}${values.map((value) => `<option value="${value}" ${value === selected ? "selected" : ""}>${escapeHtml(statusText(value))}</option>`).join("")}`;
}

function lostStatusOptions(type, selected) {
  const values = type === "LOST"
    ? [["SEARCHING", "寻找中"], ["FOUND", "已找回"]]
    : [["WAITING", "等待认领"], ["CLAIMED", "已认领"], ["RETURNED", "已归还"]];
  return values.map(([value, label]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`).join("");
}

function openDialog(title, html, onConfirm, confirmText = "保存") {
  const dialog = $("#dialog");
  $("#dialog-content").innerHTML = `<h3>${escapeHtml(title)}</h3>${html}`;
  $("#dialog-confirm").textContent = confirmText;
  $("#dialog-confirm").type = "submit";
  dialog.returnValue = "";
  dialog.showModal();
  dialog.onclose = async () => {
    if (dialog.returnValue !== "default") return;
    try {
      await onConfirm(new FormData($("#dialog-form")));
    } catch (error) {
      toast(error.message, true);
    }
  };
}

async function openInventory(venueId) {
  const venue = state.venues.find((item) => item.id === venueId);
  if (!venue) return;
  const today = new Date().toISOString().slice(0, 10);
  openDialog(
    `${venue.name} · 日期库存`,
    `<label>日期<input id="inventory-date" type="date" value="${today}"></label><div id="inventory-content" class="empty">正在加载…</div>`,
    async () => {},
    "关闭",
  );
  $("#dialog-confirm").type = "button";
  $("#dialog-confirm").addEventListener("click", () => $("#dialog").close("cancel"), { once: true });
  const load = async () => {
    const selectedDate = $("#inventory-date").value;
    const blocked = await api(`/api/v1/admin/inventory?venueId=${encodeURIComponent(venue.id)}&date=${selectedDate}`);
    const blockedMap = new Map(blocked.map((item) => [`${item.time_slot}-${item.court_number}`, item]));
    $("#inventory-content").className = "inventory-grid";
    $("#inventory-content").innerHTML = venue.timeSlots.map((slot) =>
      Array.from({ length: venue.court_count }, (_, index) => {
        const court = index + 1;
        const item = blockedMap.get(`${slot.timeSlot}-${court}`);
        return `<button type="button" class="inventory-cell ${item ? "blocked" : ""}" data-action="toggle-inventory" data-venue="${escapeHtml(venue.id)}" data-date="${selectedDate}" data-slot="${escapeHtml(slot.timeSlot)}" data-court="${court}" data-block-id="${escapeHtml(item?.id || "")}"><strong>${court} ${escapeHtml(venue.unit_label)}</strong><span class="subtle">${escapeHtml(slot.timeSlot)}<br>${item ? "已关闭" : "可预约"}</span></button>`;
      }).join("")
    ).join("");
  };
  $("#inventory-date").addEventListener("change", load);
  await load();
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  try {
    const data = await api("/api/v1/auth/login", {
      method: "POST",
      body: {
        username: $("#login-username").value,
        password: $("#login-password").value,
      },
    });
    if (data.user.role !== "ADMIN") throw new Error("该账号没有管理员权限");
    state.token = data.token;
    sessionStorage.setItem("myChangliAdminToken", state.token);
    $("#login-password").value = "";
    showApp();
  } catch (error) {
    $("#login-error").textContent = error.message;
  }
});

$("#logout-button").addEventListener("click", async () => {
  try { await api("/api/v1/auth/logout", { method: "POST", body: {} }); } catch (_) {}
  showLogin();
});

$("#nav").addEventListener("click", (event) => {
  const button = event.target.closest("[data-page]");
  if (button) navigate(button.dataset.page);
});

content.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const values = Object.fromEntries(new FormData(form));
  try {
    if (form.id === "reservation-filter") return renderers.reservations(values);
    if (form.id === "lost-filter") return renderers.lost(values);
    if (form.id === "feedback-filter") return renderers.feedback(values);
    if (form.id === "announcement-form") {
      await api("/api/v1/admin/announcements", {
        method: "POST",
        body: { ...values, enabled: form.enabled.checked },
      });
      toast("公告已发布");
      return renderers.announcements();
    }
    if (form.id === "version-form") {
      await api("/api/v1/admin/version/android", {
        method: "PUT",
        body: {
          ...values,
          latestVersionCode: Number(values.latestVersionCode),
          minimumVersionCode: Number(values.minimumVersionCode),
        },
      });
      toast("版本配置已保存");
      return renderers.config();
    }
  } catch (error) {
    toast(error.message, true);
  }
});

content.addEventListener("change", async (event) => {
  const target = event.target;
  try {
    if (target.dataset.action === "lost-status") {
      await api(`/api/v1/admin/lost-found/${encodeURIComponent(target.dataset.id)}`, {
        method: "PATCH",
        body: { status: target.value },
      });
      toast("失物状态已更新");
    }
    if (target.dataset.action === "report-status") {
      await api(`/api/v1/admin/lost-found-reports/${encodeURIComponent(target.dataset.id)}`, {
        method: "PATCH",
        body: { status: target.value },
      });
      toast("举报处理状态已更新");
    }
    if (target.dataset.action === "feedback-status") {
      await api(`/api/v1/admin/feedback/${encodeURIComponent(target.dataset.id)}`, {
        method: "PATCH",
        body: { status: target.value },
      });
      toast("反馈状态已更新");
    }
    if (target.dataset.action === "toggle-flag") {
      await api(`/api/v1/admin/flags/${encodeURIComponent(target.dataset.key)}`, {
        method: "PUT",
        body: { enabled: target.checked, payload: {} },
      });
      toast("功能开关已更新");
    }
  } catch (error) {
    toast(error.message, true);
    navigate(state.page);
  }
});

content.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  try {
    if (action === "reload") return navigate(state.page);
    if (action === "clear-reservation-filter") return renderers.reservations();
    if (action === "inventory") return openInventory(button.dataset.id);
    if (action === "toggle-slot") {
      await api(`/api/v1/admin/time-slots/${encodeURIComponent(button.dataset.id)}`, {
        method: "PATCH",
        body: { enabled: button.dataset.enabled !== "true" },
      });
      toast("时间段状态已更新");
      return renderers.venues();
    }
    if (action === "edit-venue") {
      const venue = state.venues.find((item) => item.id === button.dataset.id);
      return openDialog("编辑场馆", `
        <div class="stack">
          <label>场馆名称<input name="name" value="${escapeHtml(venue.name)}" required></label>
          <label>场地数量<input name="courtCount" type="number" min="1" max="100" value="${venue.court_count}" required></label>
          <label>每小时价格（元）<input name="priceYuan" type="number" min="0" value="${venue.price_yuan}" required></label>
          <label>运行状态<select name="operatingStatus">${options(["OPEN","CLOSED","MAINTENANCE"], venue.operating_status)}</select></label>
          <label>维护说明<textarea name="maintenanceNote">${escapeHtml(venue.maintenance_note || "")}</textarea></label>
          <label class="checkbox-row"><input name="enabled" type="checkbox" ${venue.enabled ? "checked" : ""}> 在服务端启用此场馆</label>
        </div>`,
        async (form) => {
          await api(`/api/v1/admin/venues/${encodeURIComponent(venue.id)}`, {
            method: "PATCH",
            body: {
              name: form.get("name"),
              courtCount: Number(form.get("courtCount")),
              priceYuan: Number(form.get("priceYuan")),
              operatingStatus: form.get("operatingStatus"),
              maintenanceNote: form.get("maintenanceNote"),
              enabled: form.has("enabled"),
            },
          });
          toast("场馆配置已保存");
          renderers.venues();
        });
    }
    if (action === "add-slot") {
      return openDialog("新增开放时间段", `<div class="grid two-columns"><label>开始时间<input name="startTime" type="time" required></label><label>结束时间<input name="endTime" type="time" required></label></div>`,
        async (form) => {
          await api(`/api/v1/admin/venues/${encodeURIComponent(button.dataset.id)}/time-slots`, {
            method: "POST",
            body: { startTime: form.get("startTime"), endTime: form.get("endTime") },
          });
          toast("时间段已添加");
          renderers.venues();
        });
    }
    if (action === "cancel-reservation") {
      if (!confirm("确认取消这条预约？取消后该场地将重新可预约。")) return;
      await api(`/api/v1/admin/reservations/${encodeURIComponent(button.dataset.id)}/cancel`, { method: "POST", body: {} });
      toast("预约已取消");
      return renderers.reservations();
    }
    if (action === "delete-lost") {
      if (!confirm("确认永久删除这条失物信息？关联的收藏、认领和举报记录也会删除。")) return;
      await api(`/api/v1/admin/lost-found/${encodeURIComponent(button.dataset.id)}`, { method: "DELETE" });
      toast("失物信息已删除");
      return renderers.lost();
    }
    if (action === "toggle-announcement") {
      const next = button.dataset.enabled !== "true";
      if (!next && !confirm("确认撤回这条公告？App 将不再获取到它。")) return;
      await api(`/api/v1/admin/announcements/${encodeURIComponent(button.dataset.id)}`, {
        method: "PATCH",
        body: { enabled: next },
      });
      toast(next ? "公告已重新发布" : "公告已撤回");
      return renderers.announcements();
    }
    if (action === "edit-announcement") {
      return openDialog("编辑公告", `<div class="stack"><label>标题<input name="title" value="${escapeHtml(button.dataset.title)}" required></label><label>内容<textarea name="content" required>${escapeHtml(button.dataset.content)}</textarea></label></div>`,
        async (form) => {
          await api(`/api/v1/admin/announcements/${encodeURIComponent(button.dataset.id)}`, {
            method: "PATCH",
            body: { title: form.get("title"), content: form.get("content"), enabled: button.dataset.enabled === "true" },
          });
          toast("公告已更新");
          renderers.announcements();
        });
    }
  } catch (error) {
    toast(error.message, true);
  }
});

$("#dialog-content").addEventListener("click", async (event) => {
  const button = event.target.closest('[data-action="toggle-inventory"]');
  if (!button) return;
  try {
    if (button.dataset.blockId) {
      await api(`/api/v1/admin/inventory/block/${encodeURIComponent(button.dataset.blockId)}`, { method: "DELETE" });
      toast("该场地已恢复开放");
    } else {
      await api("/api/v1/admin/inventory/block", {
        method: "POST",
        body: {
          venueId: button.dataset.venue,
          date: button.dataset.date,
          timeSlot: button.dataset.slot,
          courtNumber: Number(button.dataset.court),
          reason: "后台临时关闭",
        },
      });
      toast("该场地已关闭");
    }
    $("#dialog").close("cancel");
    openInventory(button.dataset.venue);
  } catch (error) {
    toast(error.message, true);
  }
});

if (state.token) showApp();
else showLogin();
