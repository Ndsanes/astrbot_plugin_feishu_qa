const bridge = window.AstrBotPluginPage;

const el = (id) => document.getElementById(id);
const msg = (id, text, kind = "") => {
  const node = el(id);
  node.textContent = text || "";
  node.className = `msg ${kind}`;
};

const state = { sections: [], selected: new Set(), loading: false };

async function loadStatus() {
  try {
    const data = await bridge.apiGet("status");
    el("st-entries").textContent = data.entries ?? "–";
    el("st-images").textContent = data.images ?? "–";
    el("st-sources").textContent =
      Object.entries(data.sources || {})
        .map(([name, kept]) => `${name}: ${kept}`)
        .join(" / ") || "–";
    el("st-revision").textContent = data.revision ?? "–";
    el("st-built").textContent = data.built_at ?? "–";
    el("st-proactive").textContent = data.proactive_mode ?? "–";
    el("manual-status").textContent = data.kb_watch || "–";
    el("manual-meta").textContent = data.kb_watch_checked_at
      ? `上次检查：${data.kb_watch_checked_at}`
      : "尚未检查";
  } catch (error) {
    msg("sections-msg", `读取状态失败：${error.message || error}`, "err");
  }
}

function renderSections() {
  const box = el("sections");
  box.replaceChildren();
  if (!state.sections.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = state.loading ? "正在读取线上章节…" : "没有可选章节";
    box.append(empty);
    return;
  }
  state.sections.forEach((name) => {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = state.selected.has(name);
    input.addEventListener("change", () => {
      if (input.checked) state.selected.add(name);
      else state.selected.delete(name);
    });
    const span = document.createElement("span");
    span.textContent = name;
    label.append(input, span);
    box.append(label);
  });
}

async function loadSections() {
  state.loading = true;
  renderSections();
  try {
    const data = await bridge.apiGet("fuuumusic/sections");
    state.sections = Array.isArray(data.sections) ? data.sections : [];
    state.selected = new Set(Array.isArray(data.selected) ? data.selected : []);
    // 已保存但站点上暂时消失的章节也要显示，避免"看不见=被清掉"
    state.selected.forEach((name) => {
      if (!state.sections.includes(name)) state.sections.push(name);
    });
    msg("sections-msg", `已读取 ${state.sections.length} 个章节`, "ok");
  } catch (error) {
    // 站点抓不到时至少把本地已保存的章节显示出来，别让用户以为配置丢了
    try {
      const status = await bridge.apiGet("status");
      const saved = Array.isArray(status.fuuumusic_sections)
        ? status.fuuumusic_sections
        : [];
      state.sections = [...saved];
      state.selected = new Set(saved);
    } catch {
      state.sections = [];
      state.selected = new Set();
    }
    msg(
      "sections-msg",
      `读取线上章节失败，已显示本地保存的 ${state.sections.length} 个章节：${
        error.message || error
      }`,
      "warn",
    );
  } finally {
    state.loading = false;
    renderSections();
  }
}

async function saveSections() {
  const button = el("save-sections");
  button.disabled = true;
  try {
    const data = await bridge.apiPost("fuuumusic/sections", {
      sections: [...state.selected],
    });
    const saved = Array.isArray(data.sections) ? data.sections : [];
    msg(
      "sections-msg",
      saved.length
        ? `已保存 ${saved.length} 个章节，下一轮同步生效`
        : "已保存：留空 = 导入全部条目",
      "ok",
    );
    await loadStatus();
  } catch (error) {
    msg("sections-msg", `保存失败：${error.message || error}`, "err");
  } finally {
    button.disabled = false;
  }
}

async function checkManual() {
  const button = el("check-manual");
  button.disabled = true;
  msg("manual-msg", "检查中…");
  try {
    const data = await bridge.apiPost("kb_watch/check", {});
    const status = data.status_line || "检查完成";
    const pending = Boolean(data.result && data.result.pending);
    msg("manual-msg", status, pending ? "warn" : "ok");
    await loadStatus();
  } catch (error) {
    msg("manual-msg", `检查失败：${error.message || error}`, "err");
  } finally {
    button.disabled = false;
  }
}

async function boot() {
  if (!bridge) {
    document.body.innerHTML = "<p>未找到 AstrBot Page bridge。</p>";
    return;
  }
  await bridge.ready();
  bridge.onContext(() => {
    // 主题/语言切换时重绘即可
  });
  el("refresh").addEventListener("click", () => {
    loadStatus();
    loadSections();
  });
  el("reload-sections").addEventListener("click", loadSections);
  el("select-all").addEventListener("click", () => {
    state.selected = new Set(state.sections);
    renderSections();
  });
  el("select-none").addEventListener("click", () => {
    state.selected.clear();
    renderSections();
  });
  el("save-sections").addEventListener("click", saveSections);
  el("check-manual").addEventListener("click", checkManual);
  await Promise.all([loadStatus(), loadSections()]);
}

boot();
