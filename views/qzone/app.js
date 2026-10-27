const bridge = window.AstrBotPluginView;

const state = {
  config: {
    llm_enabled: false,
    publish_mode: "review",
    admin_only: true,
    ugc_right: 4,
    min_interval_seconds: 60,
  },
  posts: [],
  filter: "all",
  editingId: null,
};

const UGC_DESC = {
  1: "所有人可见",
  4: "好友可见",
  16: "部分好友可见",
  64: "仅自己可见",
  128: "部分好友不可见",
};
const STATUS_LABEL = {
  pending: "待审核",
  published: "已发布",
  rejected: "已拒绝",
  failed: "发布失败",
};

const $ = (id) => document.getElementById(id);

function toast(msg, ok = true) {
  const el = $("toast");
  el.textContent = msg;
  el.className = `toast show ${ok ? "ok" : "err"}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = "toast"), 3200);
}

function esc(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

async function call(fn, endpoint, arg) {
  try {
    return arg === undefined ? await fn(endpoint) : await fn(endpoint, arg);
  } catch (e) {
    toast(String(e.message || e), false);
    throw e;
  }
}

// ─────────────────────────── 加载 ───────────────────────────

async function loadStatus() {
  const data = await call(bridge.apiGet.bind(bridge), "status");
  Object.assign(state.config, data.config || {});
  renderConfig();
  renderChips(data.counts || {});
}

async function loadPosts() {
  const data = await call(bridge.apiGet.bind(bridge), "posts");
  state.posts = data.posts || [];
  renderPosts();
}

async function loadAll() {
  await Promise.all([loadStatus(), loadPosts()]);
}

function renderConfig() {
  const c = state.config;
  $("cfg-llm-enabled").checked = !!c.llm_enabled;
  $("cfg-admin-only").checked = !!c.admin_only;
  $("cfg-publish-mode").value = c.publish_mode || "review";
  $("cfg-ugc-right").value = String(c.ugc_right ?? 4);
  $("cfg-min-interval").value = c.min_interval_seconds ?? 60;
  $("pub-ugc-right").value = String(c.ugc_right ?? 4);
  toggleTargetField();
}

function renderChips(counts) {
  $("stat-chips").innerHTML = ["pending", "published", "rejected", "failed"]
    .map(
      (k) =>
        `<span class="chip chip-${k}">${STATUS_LABEL[k]} ${counts[k] ?? 0}</span>`
    )
    .join("");
}

// ─────────────────────────── 渲染列表 ───────────────────────────

function renderPosts() {
  const list = $("post-list");
  const items = state.posts.filter(
    (p) => state.filter === "all" || p.status === state.filter
  );
  if (!items.length) {
    list.innerHTML = `<div class="empty">暂无记录</div>`;
    return;
  }
  list.innerHTML = items.map(postCard).join("");
}

function postCard(p) {
  const imgs = (p.images || [])
    .map((u) => `<a class="img-link" href="${esc(u)}" target="_blank">🖼 图片</a>`)
    .join(" ");
  const reason = p.reject_reason
    ? `<div class="meta-line">拒绝理由：${esc(p.reject_reason)}</div>`
    : "";
  const err = p.error
    ? `<div class="meta-line err">错误：${esc(p.error)}</div>`
    : "";
  const tid = p.tid
    ? `<span class="chip chip-tid" title="点击复制">tid: ${esc(p.tid)}</span>`
    : "";
  const body =
    state.editingId === p.id
      ? editForm(p)
      : `<div class="content">${esc(p.content) || "<i>（纯图片）</i>"}</div>
         ${imgs ? `<div class="imgs">${imgs}</div>` : ""}`;
  const trigger = p.trigger
    ? `<div class="meta-line">💬 触发消息：${esc(p.trigger)}</div>`
    : "";
  const thinking = p.thinking
    ? `<details class="thinking">
         <summary>🧠 思维链（仅 WebUI 可见，不会发到空间）</summary>
         <div class="think-body">${esc(p.thinking)}</div>
       </details>`
    : "";

  return `<div class="post" data-id="${esc(p.id)}">
    <div class="post-head">
      <span class="chip chip-${esc(p.status)}">${STATUS_LABEL[p.status] || esc(p.status)}</span>
      <span class="chip chip-src">${p.source === "llm" ? "LLM" : "手动"}</span>
      <span class="chip chip-ugc">${UGC_DESC[p.ugc_right] || p.ugc_right}</span>
      ${tid}
      <span class="time">${fmtTime(p.created_at)}${p.published_at ? " → " + fmtTime(p.published_at) : ""}</span>
    </div>
    ${body}
    ${trigger}${thinking}
    ${reason}${err}
    <div class="actions">${actionButtons(p)}</div>
  </div>`;
}

function actionButtons(p) {
  const btns = [];
  if (state.editingId === p.id) {
    btns.push(`<button class="btn btn-primary btn-sm" data-act="save-edit">💾 保存</button>`);
    btns.push(`<button class="btn btn-ghost btn-sm" data-act="cancel-edit">取消</button>`);
    return btns.join("");
  }
  if (p.status === "pending") {
    btns.push(`<button class="btn btn-primary btn-sm" data-act="approve">✅ 通过并发布</button>`);
    btns.push(`<button class="btn btn-ghost btn-sm" data-act="edit">✏️ 编辑</button>`);
    btns.push(`<button class="btn btn-warn btn-sm" data-act="reject">🚫 拒绝</button>`);
  } else if (p.status === "published") {
    btns.push(`<button class="btn btn-warn btn-sm" data-act="delete-qzone">🗑 删除并撤回空间说说</button>`);
    btns.push(`<button class="btn btn-ghost btn-sm" data-act="delete">仅删记录</button>`);
  } else if (p.status === "failed") {
    btns.push(`<button class="btn btn-primary btn-sm" data-act="approve">🔁 重试发布</button>`);
    btns.push(`<button class="btn btn-ghost btn-sm" data-act="edit">✏️ 编辑</button>`);
    btns.push(`<button class="btn btn-ghost btn-sm" data-act="delete">删除</button>`);
  } else {
    btns.push(`<button class="btn btn-primary btn-sm" data-act="edit">✏️ 编辑并重新转为草稿</button>`);
    btns.push(`<button class="btn btn-ghost btn-sm" data-act="delete">删除</button>`);
  }
  return btns.join("");
}

function editForm(p) {
  const imgs = (p.images || []).join("\n");
  return `<div class="edit-form">
    <textarea class="input" data-f="content" rows="3">${esc(p.content)}</textarea>
    <textarea class="input" data-f="images" rows="2" placeholder="图片地址，每行一个">${esc(imgs)}</textarea>
    <div class="grid2">
      <select class="input" data-f="ugc_right">
        ${[1, 4, 16, 64, 128]
          .map(
            (v) =>
              `<option value="${v}" ${Number(p.ugc_right) === v ? "selected" : ""}>${UGC_DESC[v]}</option>`
          )
          .join("")}
      </select>
      <input class="input" data-f="target_uins" placeholder="目标 QQ 号，逗号分隔"
        value="${esc((p.target_uins || []).join(","))}" />
    </div>
    <textarea class="input" data-f="thinking" rows="3"
      placeholder="思维链/备注（仅 WebUI 可见，不会发到空间）">${esc(p.thinking || "")}</textarea>
  </div>`;
}

// ─────────────────────────── 动作 ───────────────────────────

function collectEditForm(postEl) {
  const get = (f) => postEl.querySelector(`[data-f="${f}"]`)?.value ?? "";
  return {
    id: postEl.dataset.id,
    content: get("content").trim(),
    images: get("images"),
    ugc_right: Number(get("ugc_right")) || 4,
    target_uins: get("target_uins"),
    thinking: get("thinking"),
  };
}

async function handleAction(act, postEl) {
  const id = postEl.dataset.id;
  const post = state.posts.find((p) => p.id === id);

  if (act === "edit") {
    state.editingId = id;
    renderPosts();
    return;
  }
  if (act === "cancel-edit") {
    state.editingId = null;
    renderPosts();
    return;
  }
  if (act === "save-edit") {
    await call(bridge.apiPost.bind(bridge), "posts/save", collectEditForm(postEl));
    state.editingId = null;
    toast("已保存为待审核草稿");
  }
  if (act === "approve") {
    const r = await call(bridge.apiPost.bind(bridge), "posts/approve", { id });
    toast(r.message || "已发布", r.ok);
  }
  if (act === "reject") {
    const reason = prompt("拒绝理由（可留空）：", "") ?? "";
    await call(bridge.apiPost.bind(bridge), "posts/reject", { id, reason });
    toast("已拒绝");
  }
  if (act === "delete" || act === "delete-qzone") {
    const label =
      act === "delete-qzone"
        ? "删除记录并撤回空间里的这条说说？"
        : "删除这条记录？（不影响空间里已发布的说说）";
    if (!confirm(label)) return;
    await call(bridge.apiPost.bind(bridge), "posts/delete", {
      id,
      delete_qzone: act === "delete-qzone",
    });
    toast("已删除");
  }
  await loadAll();
}

// ─────────────────────────── 事件绑定 ───────────────────────────

function toggleTargetField() {
  const right = Number($("pub-ugc-right").value);
  $("pub-target-wrap").style.display = right === 16 || right === 128 ? "" : "none";
}

async function init() {
  await bridge.ready();

  $("refresh-btn").onclick = () => loadAll().catch(() => {});
  $("save-config-btn").onclick = async () => {
    const payload = {
      llm_enabled: $("cfg-llm-enabled").checked,
      admin_only: $("cfg-admin-only").checked,
      publish_mode: $("cfg-publish-mode").value,
      ugc_right: Number($("cfg-ugc-right").value),
      min_interval_seconds: Number($("cfg-min-interval").value) || 0,
    };
    await call(bridge.apiPost.bind(bridge), "config/save", payload);
    toast("设置已保存");
    await loadAll();
  };

  $("pub-ugc-right").onchange = toggleTargetField;

  const publish = async (publish) => {
    const payload = {
      content: $("pub-content").value.trim(),
      images: $("pub-images").value,
      ugc_right: Number($("pub-ugc-right").value),
      target_uins: $("pub-target-uins").value,
      thinking: $("pub-thinking").value,
    };
    if (!payload.content && !payload.images.trim()) {
      toast("内容为空", false);
      return;
    }
    const endpoint = publish ? "posts/publish" : "posts/save";
    const r = await call(bridge.apiPost.bind(bridge), endpoint, payload);
    if (publish) {
      toast(r.message || (r.ok ? "已发布" : "失败"), !!r.ok);
    } else {
      toast("草稿已保存，待审核");
    }
    $("pub-content").value = "";
    $("pub-images").value = "";
    $("pub-target-uins").value = "";
    $("pub-thinking").value = "";
    await loadAll();
  };

  $("publish-now-btn").onclick = () => publish(true).catch(() => {});
  $("save-draft-btn").onclick = () => publish(false).catch(() => {});

  $("filter-tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (!tab) return;
    document.querySelectorAll("#filter-tabs .tab").forEach((t) => t.classList.remove("active"));
    tab.classList.add("active");
    state.filter = tab.dataset.filter;
    renderPosts();
  });

  $("post-list").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-act]");
    if (!btn) return;
    const postEl = btn.closest(".post");
    handleAction(btn.dataset.act, postEl).catch(() => {});
  });

  $("post-list").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip-tid");
    if (!chip) return;
    const text = chip.textContent.replace(/^tid:\s*/, "");
    navigator.clipboard?.writeText(text).then(() => toast("tid 已复制"));
  });

  await loadAll();
}

init();
