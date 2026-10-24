const bridge = window.AstrBotPluginPage;

const SECTIONS = [
  {
    id: "basic",
    title: "基础与群聊",
    desc: "机器人接入哪个飞书文档、在哪些群工作、谁是管理员。",
    fields: [
      {
        key: "WIKI_URL",
        label: "飞书 Q&A 文档",
        type: "url",
        help: "wiki 或 docx 链接；留空则只用 fuuumusic 语料。",
      },
      {
        key: "ENABLED_GROUPS",
        label: "启用的群",
        type: "list",
        help: "群号或完整 UMO；留空 = 所有群都不启用；填 * 表示全部。",
      },
      {
        key: "ADMIN_USERS",
        label: "管理员 ID",
        type: "list",
        help: "可用 /sid 获取；管理员能用 /qa_status、/qa_sync、/kb_watch。",
      },
      {
        key: "SYNC_INTERVAL_HOURS",
        label: "飞书自动同步间隔",
        type: "int",
        help: "小时；0 = 关闭自动同步。",
        min: 0,
        max: 8760,
      },
    ],
  },
  {
    id: "answers",
    title: "回答与图片",
    desc: "确定性检索的置信闸门，以及回答时附带的截图。",
    fields: [
      {
        key: "HIGH_CONFIDENCE_THRESHOLD",
        label: "直答阈值",
        type: "float",
        help: "≥ 此分数直接回原文章节（零 LLM）；调高更保守。",
        min: 0,
        max: 100,
        step: 0.5,
      },
      {
        key: "MEDIUM_CONFIDENCE_THRESHOLD",
        label: "中置信阈值",
        type: "float",
        help: "介于两者之间交给主 Agent；低于此分算未命中。",
        min: 0,
        max: 100,
        step: 0.5,
      },
      {
        key: "TENTATIVE_ANSWER_ENABLED",
        label: "模糊档投递",
        help: "命中中置信区时投递「可能相关」的章节链接。",
      },
      { key: "ATTACH_IMAGES", label: "回答附带文档截图" },
      {
        key: "MAX_IMAGES",
        label: "单条回答最多附图",
        type: "int",
        min: 0,
        max: 10,
      },
    ],
  },
  {
    id: "proactive",
    title: "群消息主动答",
    desc: "旁听群消息、把碎片拼成一次提问，并决定什么时候开口。",
    fields: [
      {
        key: "PROACTIVE_MODE",
        label: "模式",
        type: "select",
        options: [
          ["off", "关闭"],
          ["shadow", "只观测（只写日志，不发言）"],
          ["human_only", "只兜底 @真人（等宽限后没人回才发）"],
          ["first_ask", "首问就答"],
        ],
      },
      {
        key: "PROACTIVE_GROUPS",
        label: "生效的群",
        type: "list",
        help: "留空 = 跟随「启用的群」；支持群号或完整 UMO。",
      },
      {
        key: "PROACTIVE_SETTLE_SECONDS",
        label: "碎片静默等待",
        type: "int",
        help: "最后一条消息后静默多少秒再判定；期间新消息会重新计时。",
        min: 0,
        max: 120,
      },
      {
        key: "PROACTIVE_HUMAN_GRACE_SECONDS",
        label: "@真人兜底等待",
        type: "int",
        help: "被 @ 的人一直没回，等这么久才以「给个参考」的身份出手。",
        min: 0,
        max: 3600,
      },
      {
        key: "PROACTIVE_MAX_PER_DAY",
        label: "每群每天上限",
        type: "int",
        help: "0 = 不限；建议 8 条，宁可漏答也不要刷屏。",
        min: 0,
        max: 100,
      },
      {
        key: "PROACTIVE_MAX_PER_USER_HOUR",
        label: "每人每小时上限",
        type: "int",
        help: "0 = 不限；防止同一人连续追问时连发。",
        min: 0,
        max: 20,
      },
    ],
  },
  {
    id: "fuuumusic",
    title: "fuuumusic 语料",
    desc: "小闻的奇妙屋：47 条中文问答 + 官方文档中文翻译。",
    fields: [
      { key: "FUUUMUSIC_ENABLED", label: "启用 fuuumusic 语料" },
      {
        key: "FUUUMUSIC_SECTIONS",
        label: "导入章节",
        type: "sections",
        reload: false,
        help: "留空 = 全部导入；勾选后保存，下一轮同步生效，不需要重载。",
      },
      {
        key: "FUUUMUSIC_SYNC_INTERVAL_HOURS",
        label: "自动同步间隔",
        type: "int",
        help: "小时；0 = 关闭。",
        min: 0,
        max: 8760,
      },
    ],
  },
  {
    id: "manual",
    title: "官方手册检查",
    desc: "每周看一次 BandLab 官方 PDF 有没有更新，有变化就通知。",
    fields: [
      { key: "KB_WATCH_ENABLED", label: "每周检查官方 PDF" },
      {
        key: "KB_WATCH_INTERVAL_HOURS",
        label: "检查间隔",
        type: "int",
        help: "小时；默认 168 = 每周。",
        min: 0,
        max: 8760,
      },
      { key: "KB_WATCH_PDF_URL", label: "PDF 地址", type: "url" },
      {
        key: "KB_WATCH_NOTIFY_UMO",
        label: "更新通知发给谁",
        reload: false,
        help: "填 UMO；留空 = 由「管理员 ID」自动推导私聊 UMO。",
      },
    ],
  },
  {
    id: "jev",
    title: "Jev 判定模型",
    desc: "可选：用决策模型复核检索结果，决定要不要直答/附链。",
    fields: [
      { key: "JEV_ENABLED", label: "启用 Jev 判定" },
      { key: "JEV_API_KEY", label: "API Key", type: "secret" },
      {
        key: "JEV_MODEL",
        label: "模型",
        placeholder: "留空 = 默认",
        help: "TypeSafe 上的模型名。",
      },
      { key: "JEV_ENDPOINT", label: "接口地址", type: "url" },
      {
        key: "JEV_TIMEOUT_SECONDS",
        label: "调用超时",
        type: "float",
        min: 0.5,
        max: 60,
        step: 0.5,
      },
      { key: "JEV_SELFTEST_ON_LOAD", label: "加载时自检连通性" },
      {
        key: "JEV_ANSWERABLE",
        label: "「答得上」门槛",
        type: "float",
        help: "0~1；越高越保守（更少接管）。",
        min: 0,
        max: 1,
        step: 0.05,
      },
      {
        key: "JEV_TAKEOVER_PROBABILITY",
        label: "自行回答门槛",
        type: "float",
        help: "最对的那条要够高才自己答。",
        min: 0,
        max: 1,
        step: 0.05,
      },
      {
        key: "JEV_LINK_PROBABILITY",
        label: "附链门槛",
        type: "float",
        help: "章节够格才附链接；高于自行回答门槛更严格。",
        min: 0,
        max: 1,
        step: 0.05,
      },
    ],
  },
  {
    id: "observability",
    title: "观测与学习",
    desc: "决策日志、判定缓存，以及 /learn 的行为。",
    fields: [
      { key: "DECISION_LOG_ENABLED", label: "记录决策日志" },
      {
        key: "DECISION_LOG_TEXT_MODE",
        label: "问题原文留存",
        type: "select",
        options: [
          ["truncate", "截断原文（默认）"],
          ["hash", "只存哈希（长期留存推荐）"],
          ["full", "保存全文"],
        ],
      },
      {
        key: "DECISION_LOG_MAX_TEXT_CHARS",
        label: "截断长度",
        type: "int",
        min: 0,
        max: 4000,
      },
      {
        key: "DECISION_CACHE_TTL_MINUTES",
        label: "判定记忆时长",
        type: "int",
        help: "分钟；同一问题在此时间内复用首次判定。",
        min: 0,
        max: 10080,
      },
      {
        key: "LEARN_CONTEXT_MESSAGES",
        label: "/learn 分析消息条数",
        type: "int",
        reload: false,
        min: 1,
        max: 500,
      },
      {
        key: "LEARNING_WRITEBACK_DOC_URL",
        label: "写回文档地址",
        type: "url",
        reload: false,
        help: "留空 = 关闭写回；只允许指向副本文档。",
      },
    ],
  },
];

const state = {
  values: {},
  original: {},
  schema: {},
  hasJevKey: false,
  clearSecrets: new Set(),
  liveSections: [],
  sectionsLoaded: false,
};

const el = (id) => document.getElementById(id);

function toast(text, kind = "") {
  const node = el("global-msg");
  node.textContent = text || "";
  node.className = `msg ${kind}`;
}

function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

function deepEqual(a, b) {
  return JSON.stringify(a) === JSON.stringify(b);
}

function normalizeValues(values) {
  const out = {};
  for (const [key, value] of Object.entries(values)) {
    const type = state.schema[key]?.type;
    if (type === "list") out[key] = Array.isArray(value) ? [...value] : [];
    else if (type === "int" || type === "float") out[key] = Number(value);
    else if (type === "bool") out[key] = Boolean(value);
    else out[key] = value ?? "";
  }
  return out;
}

function typeOf(field) {
  return field.type || state.schema[field.key]?.type || "string";
}

function updateDirty() {
  const dirty =
    !deepEqual(state.values, state.original) || state.clearSecrets.size > 0;
  el("dirty").classList.toggle("hidden", !dirty);
  el("save-state").textContent = dirty ? "有未保存的修改" : "所有修改已保存";
  for (const id of ["save", "save-bottom"]) el(id).disabled = !dirty;
  el("save-reload").disabled = !dirty;
}

function setValue(key, value) {
  state.values[key] = value;
  updateDirty();
}

function fieldLabel(field) {
  const reload = field.reload === false ? "" : '<span class="reload-badge">需重载</span>';
  return `${field.label}${reload}`;
}

function renderSwitch(field, value) {
  const wrap = document.createElement("label");
  wrap.className = "switch";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = Boolean(value);
  input.addEventListener("change", () => setValue(field.key, input.checked));
  const span = document.createElement("span");
  span.textContent = value ? "已开启" : "已关闭";
  input.addEventListener("change", () => {
    span.textContent = input.checked ? "已开启" : "已关闭";
  });
  wrap.append(input, span);
  return wrap;
}

function renderSelect(field, value) {
  const select = document.createElement("select");
  for (const [optionValue, label] of field.options) {
    const option = document.createElement("option");
    option.value = optionValue;
    option.textContent = label;
    if (String(value) === optionValue) option.selected = true;
    select.append(option);
  }
  select.addEventListener("change", () => setValue(field.key, select.value));
  return select;
}

function renderNumber(field, value) {
  const input = document.createElement("input");
  input.type = "number";
  if (field.min !== undefined) input.min = String(field.min);
  if (field.max !== undefined) input.max = String(field.max);
  input.step = String(field.step ?? (typeOf(field) === "int" ? 1 : 0.1));
  input.value = value === null || value === undefined ? "" : String(value);
  input.addEventListener("input", () => {
    if (input.value === "") return;
    const parsed = typeOf(field) === "int" ? parseInt(input.value, 10) : parseFloat(input.value);
    if (!Number.isNaN(parsed)) setValue(field.key, parsed);
  });
  input.addEventListener("blur", () => {
    const parsed = typeOf(field) === "int" ? parseInt(input.value, 10) : parseFloat(input.value);
    if (Number.isNaN(parsed)) {
      input.value = String(state.values[field.key]);
    }
  });
  return input;
}

function renderText(field, value) {
  const input = document.createElement("input");
  input.type = field.type === "url" ? "url" : "text";
  input.value = value ?? "";
  if (field.placeholder) input.placeholder = field.placeholder;
  input.addEventListener("input", () => setValue(field.key, input.value));
  return input;
}

function renderSecret(field, value) {
  const row = document.createElement("div");
  row.className = "row";
  const input = document.createElement("input");
  input.type = "password";
  input.autocomplete = "new-password";
  input.placeholder = state.hasJevKey ? "已配置（留空不修改）" : "未配置";
  input.value = value ?? "";
  input.addEventListener("input", () => {
    state.clearSecrets.delete(field.key);
    setValue(field.key, input.value);
  });
  const clearLabel = document.createElement("label");
  clearLabel.className = "switch";
  const clear = document.createElement("input");
  clear.type = "checkbox";
  clear.checked = state.clearSecrets.has(field.key);
  clear.addEventListener("change", () => {
    if (clear.checked) {
      state.clearSecrets.add(field.key);
      input.value = "";
      input.disabled = true;
    } else {
      state.clearSecrets.delete(field.key);
      input.disabled = false;
    }
    updateDirty();
  });
  clearLabel.append(clear, document.createTextNode("清除"));
  input.disabled = state.clearSecrets.has(field.key);
  row.append(input, clearLabel);
  return row;
}

function renderTags(field, value) {
  const wrap = document.createElement("div");
  wrap.className = "tags";
  const items = Array.isArray(value) ? [...value] : [];

  const redraw = () => {
    wrap.replaceChildren();
    items.forEach((item, index) => {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = item;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.title = "移除";
      remove.addEventListener("click", () => {
        items.splice(index, 1);
        setValue(field.key, [...items]);
        redraw();
      });
      tag.append(remove);
      wrap.append(tag);
    });
    const input = document.createElement("input");
    input.type = "text";
    input.placeholder = "输入后回车添加";
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== ",") return;
      event.preventDefault();
      const text = input.value.trim();
      if (!text) return;
      if (!items.includes(text)) items.push(text);
      setValue(field.key, [...items]);
      input.value = "";
      redraw();
      wrap.querySelector("input")?.focus();
    });
    wrap.append(input);
  };

  redraw();
  return wrap;
}

function renderSections(field, value) {
  const wrap = document.createElement("div");
  const selected = new Set(Array.isArray(value) ? value : []);
  const names = [];
  for (const name of [...state.liveSections, ...selected]) {
    if (!names.includes(name)) names.push(name);
  }

  const toolbar = document.createElement("div");
  toolbar.className = "actions";
  toolbar.style.marginBottom = "6px";
  const refresh = document.createElement("button");
  refresh.type = "button";
  refresh.className = "btn ghost";
  refresh.textContent = state.sectionsLoaded ? "刷新线上章节" : "读取线上章节";
  refresh.addEventListener("click", loadLiveSections);
  toolbar.append(refresh);

  const box = document.createElement("div");
  box.className = "checks";
  if (!names.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "还没有读取到章节。点上面的按钮从站点读取。";
    box.append(empty);
  }
  for (const name of names) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = selected.has(name);
    input.addEventListener("change", () => {
      if (input.checked) selected.add(name);
      else selected.delete(name);
      setValue(field.key, names.filter((item) => selected.has(item)));
    });
    const span = document.createElement("span");
    span.textContent = name;
    if (state.sectionsLoaded && !state.liveSections.includes(name)) {
      const stale = document.createElement("span");
      stale.className = "stale";
      stale.textContent = "（站点暂无）";
      span.append(stale);
    }
    label.append(input, span);
    box.append(label);
  }
  wrap.append(toolbar, box);
  return wrap;
}

function renderField(field) {
  const type = typeOf(field);
  const value = state.values[field.key];
  const meta = state.schema[field.key] || {};

  const row = document.createElement("div");
  row.className = "field";

  const info = document.createElement("div");
  const label = document.createElement("div");
  label.className = "label";
  label.innerHTML = fieldLabel(field);
  info.append(label);
  const helpText = field.help || meta.hint || meta.description || "";
  if (helpText) {
    const help = document.createElement("div");
    help.className = "help";
    help.textContent = helpText;
    info.append(help);
  }
  if (meta.default !== undefined && type !== "secret") {
    const def = document.createElement("div");
    def.className = "default";
    def.textContent = `默认：${
      Array.isArray(meta.default) ? meta.default.join("、") || "（空）" : String(meta.default || "（空）")
    }`;
    info.append(def);
  }

  const control = document.createElement("div");
  control.className = "control";
  if (type === "bool") control.append(renderSwitch(field, value));
  else if (type === "select") control.append(renderSelect(field, value));
  else if (type === "int" || type === "float") control.append(renderNumber(field, value));
  else if (type === "list") control.append(renderTags(field, value));
  else if (type === "sections") control.append(renderSections(field, value));
  else if (type === "secret") control.append(renderSecret(field, value));
  else control.append(renderText(field, value));

  row.append(info, control);
  return row;
}

function renderForm() {
  const form = el("form");
  const toc = el("toc");
  form.replaceChildren();
  toc.replaceChildren();

  for (const section of SECTIONS) {
    const tocLink = document.createElement("a");
    tocLink.href = `#${section.id}`;
    tocLink.textContent = section.title;
    tocLink.addEventListener("click", (event) => {
      event.preventDefault();
      document.getElementById(section.id)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    toc.append(tocLink);

    const card = document.createElement("section");
    card.className = "card";
    card.id = section.id;
    const head = document.createElement("div");
    head.className = "section-head";
    const title = document.createElement("h2");
    title.textContent = section.title;
    head.append(title);
    card.append(head);
    if (section.desc) {
      const desc = document.createElement("p");
      desc.className = "section-desc";
      desc.textContent = section.desc;
      card.append(desc);
    }
    for (const field of section.fields) card.append(renderField(field));
    form.append(card);
  }
  updateDirty();
}

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
    el("manual-status").textContent = data.kb_watch || "–";
    el("manual-meta").textContent = data.kb_watch_checked_at
      ? `上次检查：${data.kb_watch_checked_at}`
      : "尚未检查";
  } catch (error) {
    toast(`读取状态失败：${error.message || error}`, "err");
  }
}

async function loadSettings() {
  try {
    const data = await bridge.apiGet("settings");
    state.schema = data.schema || {};
    state.hasJevKey = Boolean(data.has_jev_key);
    state.values = normalizeValues(data.values || {});
    state.original = deepClone(state.values);
    state.clearSecrets.clear();
    renderForm();
    toast("");
    await loadLiveSections();
  } catch (error) {
    toast(`读取设置失败：${error.message || error}`, "err");
  }
}

async function loadLiveSections() {
  try {
    const data = await bridge.apiGet("fuuumusic/sections");
    state.liveSections = Array.isArray(data.sections) ? data.sections : [];
    state.sectionsLoaded = true;
  } catch (error) {
    state.liveSections = [];
    state.sectionsLoaded = false;
    toast(`读取线上章节失败（保留本地已保存的章节）：${error.message || error}`, "warn");
  }
  renderForm();
}

function collectChanges() {
  const values = {};
  for (const [key, value] of Object.entries(state.values)) {
    if (key === "JEV_API_KEY" && !value) continue;
    if (!deepEqual(value, state.original[key])) values[key] = value;
  }
  return { values, clear_secrets: [...state.clearSecrets] };
}

async function save({ reload = false } = {}) {
  const { values, clear_secrets } = collectChanges();
  if (!Object.keys(values).length && !clear_secrets.length) {
    toast("没有需要保存的修改", "warn");
    return false;
  }
  try {
    const result = await bridge.apiPost("settings", {
      values,
      clear_secrets,
    });
    state.original = deepClone(state.values);
    state.clearSecrets.clear();
    if (clear_secrets.includes("JEV_API_KEY")) {
      state.hasJevKey = false;
      state.values.JEV_API_KEY = "";
    } else if (values.JEV_API_KEY) {
      state.hasJevKey = true;
    }
    updateDirty();
    renderForm();
    let text = `已保存 ${(result.changed || []).length} 项`;
    if (result.reload_required) text += "；带「需重载」的项要重载后生效";
    toast(text, "ok");
    await loadStatus();
    if (reload && result.reload_required) await doReload();
    return true;
  } catch (error) {
    toast(`保存失败：${error.message || error}`, "err");
    return false;
  }
}

async function doReload() {
  try {
    const result = await bridge.apiPost("reload", {});
    if (!result.scheduled) {
      toast(result.message || "无法自动重载，请在 WebUI 手动重载", "warn");
      return;
    }
    toast("已请求重载插件，几秒后刷新页面…", "ok");
    setTimeout(() => window.location.reload(), 3500);
  } catch (error) {
    toast(`重载失败：${error.message || error}`, "err");
  }
}

function resetDefaults() {
  if (!window.confirm("把表单恢复为默认值？不会立即保存。")) return;
  const values = {};
  for (const [key, meta] of Object.entries(state.schema)) {
    values[key] = meta.default;
  }
  state.values = normalizeValues({ ...state.values, ...values });
  state.values.JEV_API_KEY = "";
  renderForm();
}

async function checkManual() {
  const button = el("check-manual");
  button.disabled = true;
  el("manual-msg").textContent = "检查中…";
  try {
    const data = await bridge.apiPost("kb_watch/check", {});
    const pending = Boolean(data.result && data.result.pending);
    el("manual-msg").textContent = data.status_line || "检查完成";
    el("manual-msg").className = `msg ${pending ? "warn" : "ok"}`;
    await loadStatus();
  } catch (error) {
    el("manual-msg").textContent = `检查失败：${error.message || error}`;
    el("manual-msg").className = "msg err";
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
  el("save").addEventListener("click", () => save());
  el("save-bottom").addEventListener("click", () => save());
  el("save-reload").addEventListener("click", () => save({ reload: true }));
  el("reload-plugin").addEventListener("click", doReload);
  el("reset-defaults").addEventListener("click", resetDefaults);
  el("check-manual").addEventListener("click", checkManual);
  await Promise.all([loadStatus(), loadSettings()]);
  updateDirty();
}

boot();
