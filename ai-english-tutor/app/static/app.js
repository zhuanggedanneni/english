"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = (value = "") => String(value).replace(/[&<>'"]/g, char => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
}[char]));
const gloss = (meaning = "") => {
  const parts = String(meaning).split(/[；;，,]/).map(part => part.trim()).filter(Boolean);
  return [...new Set(parts)].slice(0, 2).join("；");
};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

const storedGrade = localStorage.getItem("echo.grade");
const state = {
  content: { topics: [], words: [], sentences: [], vocabulary: [], scenes: [] },
  progress: { known_words: 0, total_words: 0, review_ids: [], speaking_seconds: 0, word_states: {} },
  provider: { mode: "demo", label: "演示模式" },
  llm: { provider: "deepseek", label: "DeepSeek API", available: true },
  speechEval: { ready: false },
  photoGrading: { ready: false },
  grade: ["高一", "高二", "高三"].includes(storedGrade) ? storedGrade : "高一",
  vocabularyMeta: { grade_total: 0, grade_totals: {} },
  route: "home",
  selectedTopic: "campus",
  selectedDuration: 5,
  conversation: {
    session: null, startedAt: 0, timer: null, live: null, busy: false,
    sceneSource: "preset", customText: "", realtimeScore: true, translate: false,
  },
  shadow: { type: "word", topic: "all", items: [], index: 0, recorder: null, blob: null, url: null, duration: 0 },
  dictation: { mode: "keyboard", topic: "all", queue: [], index: 0, answered: false, paperIds: [], paperBlob: null, paperUrl: null, batch: 1, autoNext: true, nextTimer: null },
  vocabulary: { sceneIndex: 0, selectedWordId: null, wordbookFilter: "" },
  quiz: { mode: "adaptive", count: 20, scope: "all", questions: [], index: 0, correct: 0, answered: false, wrong: [], active: false, result: false, lastSync: null, autoNext: true, nextTimer: null },
  plan: null,
  voice: { panelOpen: false, listening: false, recognition: null, handsfree: false, busy: false, mode: "command" },
  camera: { stream: null, blob: null, returnTo: "paper" },
};

const topicNames = { campus: "校园生活", environment: "绿色行动", future: "未来规划" };
let alertTimer = null;

function showAlert(message, error = false) {
  const element = $("#appAlert");
  element.textContent = message;
  element.classList.toggle("is-error", error);
  element.hidden = false;
  clearTimeout(alertTimer);
  alertTimer = setTimeout(() => { element.hidden = true; }, error ? 6500 : 3800);
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === "string" ? detail : detail?.message || "请求没有完成，请稍后重试";
    const error = new Error(message);
    if (detail && typeof detail === "object") {
      error.hint = detail.hint || "";
      error.code = detail.code || "";
    }
    throw error;
  }
  return data;
}

function formatSeconds(seconds) {
  const safe = Math.max(0, Math.round(seconds));
  return `${String(Math.floor(safe / 60)).padStart(2, "0")}:${String(safe % 60).padStart(2, "0")}`;
}

/* ── 朗读语音选择 ─────────────────────────────────────────
   两个坑：
   1) Chrome 首次 getVoices() 常返回空数组（异步加载）。原代码没有监听
      voiceschanged，取不到就直接不设 voice，浏览器便退回系统默认语音
      —— macOS 上常是 Fred / Albert 这类老式机械音。
   2) en-US 里混着大量变声语音（Bubbles、Zarvox、Bad News…），
      原代码「取第一个本地 en-US」很可能正好挑中它们。
   ──────────────────────────────────────────────────────── */
const VOICE_BLOCKLIST = /^(albert|bad news|bahh|bells|boing|bubbles|cellos|deranged|good news|hysterical|jester|organ|superstar|trinoids|whisper|zarvox|wobble|fred|junior|ralph|kathy|grandma|grandpa|rocko)$/i;

const VOICE_PREFERENCE = [
  "Google US English",
  "Microsoft Aria Online (Natural)",
  "Microsoft Jenny Online (Natural)",
  "Microsoft Emma Online (Natural)",
  "Microsoft Aria",
  "Microsoft Zira",
  "Samantha", "Ava", "Allison", "Susan", "Serena", "Kate", "Karen",
  "Moira", "Tessa", "Fiona", "Nicky", "Alex", "Daniel", "Tom",
];

let cachedVoices = [];

function refreshVoices() {
  if (!("speechSynthesis" in window)) return cachedVoices;
  const list = window.speechSynthesis.getVoices();
  if (list && list.length) cachedVoices = list;
  return cachedVoices;
}

function rankVoice(voice) {
  const name = String(voice.name || "").toLowerCase();
  const hit = VOICE_PREFERENCE.findIndex(pref => name.startsWith(pref.toLowerCase()));
  return hit === -1 ? 500 : hit;
}

function pickVoice(lang = "en-US") {
  const voices = refreshVoices();
  if (!voices.length) return null;
  const base = String(lang).slice(0, 2).toLowerCase();
  // 先剔除变声语音，再在同语种里按偏好排序；同档优先本地语音（断网也能读）。
  const usable = voices.filter(voice => !VOICE_BLOCKLIST.test(String(voice.name || "").split("(")[0].trim()));
  const sameLang = usable.filter(voice => String(voice.lang || "").replace("_", "-").toLowerCase().startsWith(base));
  // 只有英语允许跨口音兜底（en-US 缺失时用 en-GB）；中文等其他语种找不到
  // 就不设语音，交给 utterance.lang 处理，避免用英文音去硬读中文。
  const pool = sameLang.length ? sameLang : (base === "en" ? usable : []);
  if (!pool.length) {
    if (base !== "en") return null;
    // 极端情况：可用语音全被过滤（系统只装了变声语音）。宁可给一个能出声的，
    // 也比不设 voice、静默退回系统默认要好。
    const fallback = voices.filter(voice => String(voice.lang || "").replace("_", "-").toLowerCase().startsWith(base));
    return fallback[0] || voices[0] || null;
  }
  const exact = String(lang).replace("_", "-").toLowerCase();
  return pool
    // 排序键：语种完全一致 > 偏好名次 > 本地语音
    .map(voice => ({
      voice,
      same: String(voice.lang || "").replace("_", "-").toLowerCase() === exact ? 0 : 1,
      rank: rankVoice(voice),
      local: voice.localService ? 0 : 1,
    }))
    .sort((a, b) => a.same - b.same || a.rank - b.rank || a.local - b.local)[0].voice;
}

if ("speechSynthesis" in window) {
  refreshVoices();
  try {
    window.speechSynthesis.addEventListener("voiceschanged", refreshVoices);
  } catch (error) {
    window.speechSynthesis.onvoiceschanged = refreshVoices;
  }
}

function speak(text, onEnd) {
  if (!("speechSynthesis" in window)) {
    showAlert("当前浏览器不支持示范朗读，仍可阅读文本并录音。", true);
    return;
  }
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = "en-US";
  utterance.rate = 0.86;
  utterance.pitch = 1;
  const englishVoice = pickVoice("en-US");
  if (englishVoice) utterance.voice = englishVoice;
  if (onEnd) utterance.onend = onEnd;
  window.speechSynthesis.speak(utterance);
}

function speakAsync(text, lang = "en-US", rate = 0.88) {
  return new Promise(resolve => {
    if (!("speechSynthesis" in window) || !text) { resolve(); return; }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = lang;
    utterance.rate = rate;
    const voice = pickVoice(lang);
    if (voice) utterance.voice = voice;
    let settled = false;
    const finish = () => { if (!settled) { settled = true; resolve(); } };
    utterance.onend = finish;
    utterance.onerror = finish;
    window.speechSynthesis.speak(utterance);
  });
}

function pcmToWav(chunks, sampleRate = 16000) {
  const dataBytes = chunks.reduce((total, chunk) => total + chunk.length * 2, 0);
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);
  const writeText = (offset, text) => {
    for (let index = 0; index < text.length; index += 1) view.setUint8(offset + index, text.charCodeAt(index));
  };
  writeText(0, "RIFF");
  view.setUint32(4, 36 + dataBytes, true);
  writeText(8, "WAVE");
  writeText(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  writeText(36, "data");
  view.setUint32(40, dataBytes, true);
  let offset = 44;
  chunks.forEach(chunk => {
    new Int16Array(buffer, offset, chunk.length).set(chunk);
    offset += chunk.length * 2;
  });
  return new Blob([buffer], { type: "audio/wav" });
}

class VoiceRecorder {
  constructor({ realtime = false, onText = () => {}, onStatus = () => {} }) {
    this.realtime = realtime;
    this.onText = onText;
    this.onStatus = onStatus;
    this.pcmChunks = [];
    this.isRecording = false;
    this.startedAt = 0;
    this.recognition = null;
    this.finalTranscript = "";
    this.audioContext = null;
    this.socket = null;
    this.stream = null;
    this.pcmNode = null;
    this.silentGain = null;
    this.source = null;
  }

  async start() {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext || !window.AudioWorkletNode) {
      throw new Error("当前浏览器不支持音频工作线程录音，请使用新版 Edge、Chrome 或 Safari。");
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.pcmChunks = [];
    this.finalTranscript = "";
    try {
      if (this.realtime && state.provider.mode === "doubao" && state.provider.realtime_asr) await this.startRealtimeSocket();
      this.audioContext = new AudioContext({ latencyHint: "interactive" });
      await this.audioContext.audioWorklet.addModule("/audio-worklet.js");
      this.source = this.audioContext.createMediaStreamSource(this.stream);
      this.pcmNode = new AudioWorkletNode(this.audioContext, "pcm-processor");
      this.silentGain = this.audioContext.createGain();
      this.silentGain.gain.value = 0;
      this.pcmNode.port.onmessage = event => {
        const chunk = new Int16Array(event.data);
        if (chunk.length) this.pcmChunks.push(chunk);
        if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(event.data);
      };
      this.source.connect(this.pcmNode);
      this.pcmNode.connect(this.silentGain);
      this.silentGain.connect(this.audioContext.destination);
      await this.audioContext.resume();
      this.startedAt = performance.now();
      this.isRecording = true;
      if (this.realtime && !(state.provider.mode === "doubao" && state.provider.realtime_asr)) this.startBrowserRecognition();
    } catch (error) {
      this.cleanup();
      throw error;
    }
  }

  startBrowserRecognition() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      this.onStatus("录音正常；当前浏览器不支持实时转写，请说完后在文本框补充英文。");
      return;
    }
    this.recognition = new Recognition();
    this.recognition.lang = "en-US";
    this.recognition.continuous = true;
    this.recognition.interimResults = true;
    this.recognition.onresult = event => {
      let finalText = "";
      let interimText = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const text = event.results[index][0].transcript;
        if (event.results[index].isFinal) finalText += text;
        else interimText += text;
      }
      if (finalText) this.finalTranscript = `${this.finalTranscript} ${finalText}`.trim();
      if (this.finalTranscript || interimText) this.onText(`${this.finalTranscript} ${interimText}`.trim(), Boolean(finalText) && !interimText);
    };
    this.recognition.onerror = event => {
      if (!["no-speech", "aborted"].includes(event.error)) this.onStatus("浏览器转写暂时不可用，录音仍会保存，可手动补充转写。");
    };
    try { this.recognition.start(); } catch { /* recognition can already be running */ }
    this.onStatus("正在录音并使用浏览器实时转写（演示）；停止后可修改文字。");
  }

  async startRealtimeSocket() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    this.socket = new WebSocket(`${protocol}://${location.host}/api/ws/asr`);
    this.socket.onmessage = event => {
      const message = JSON.parse(event.data);
      if (["partial", "final"].includes(message.type)) this.onText(message.text || "", message.type === "final");
      if (message.type === "error") this.onStatus(message.text);
    };
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("实时语音识别连接超时")), 8000);
      this.socket.onopen = () => { clearTimeout(timer); resolve(); };
      this.socket.onerror = () => { clearTimeout(timer); reject(new Error("实时语音识别连接失败")); };
    });
  }

  cleanup() {
    this.stream?.getTracks().forEach(track => track.stop());
    this.source?.disconnect();
    this.pcmNode?.disconnect();
    this.silentGain?.disconnect();
    if (this.recognition) {
      try { this.recognition.stop(); } catch { /* already stopped */ }
      this.recognition = null;
    }
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send("stop");
    if (this.socket) this.socket.close();
    const context = this.audioContext;
    this.audioContext = null;
    if (context && context.state !== "closed") context.close().catch(() => {});
  }

  async stop() {
    if (!this.isRecording) return null;
    this.isRecording = false;
    const duration = Math.max(0, this.pcmChunks.reduce((total, chunk) => total + chunk.length, 0) / 16000);
    this.cleanup();
    const blob = pcmToWav(this.pcmChunks, 16000);
    this.pcmChunks = [];
    return { blob, duration };
  }
}

function isRecording() {
  // 流式对话开始后麦克风常开，切页面必须先结束对话。
  return Boolean(state.conversation.live?.listening || state.shadow.recorder?.isRecording);
}

function navigate(route) {
  if (isRecording()) {
    showAlert("请先结束当前对话或录音，再切换内容。", true);
    return;
  }
  if (location.hash === `#${route}`) renderRoute();
  else location.hash = route;
}

function renderRoute() {
  // v2.3 砍掉首页/跟读/听写/词汇，只留「外教对话」和「背单词」。
  const requested = location.hash.slice(1) || "conversation";
  const route = ["conversation", "quiz"].includes(requested) ? requested : "conversation";
  if (isRecording() && route !== state.route) {
    history.replaceState(null, "", `#${state.route}`);
    showAlert("请先结束当前对话，再切换页面。", true);
    return;
  }
  state.route = route;
  if (state.voice.handsfree && route !== "quiz") stopHandsfree();
  const heard = $("#voiceHeard");
  if (!state.voice.listening && heard) heard.textContent = quizPanelStateText();
  $$(".view").forEach(view => { view.hidden = view.dataset.view !== route; });
  $$("[data-route]").forEach(link => {
    const active = link.dataset.route === route;
    link.classList.toggle("is-active", active);
    if (link.classList.contains("nav-link")) {
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
  });
  // 语法卡属于对练视图，离开就收起。
  const grammarCard = $("#grammarCard");
  if (grammarCard && route !== "conversation") grammarCard.hidden = true;
  if (route === "conversation") renderConversation();
  if (route === "quiz") renderQuiz();
  window.scrollTo({ top: 0, behavior: "instant" });
}

function gradeWords() {
  return state.content.vocabulary || [];
}

function reviewWordsForGrade() {
  const states = state.progress.word_states || {};
  return gradeWords().filter(word => states[word.id]?.attempts && !states[word.id]?.known);
}

function renderHome() {
  if (!$("#homeGradeChip")) return;  // 首页已砍（v2.3），只留兜底不报错
  const progress = state.progress;
  const words = gradeWords();
  const reviewWords = reviewWordsForGrade();
  const known = words.filter(word => progress.word_states?.[word.id]?.known).length;
  $("#homeGradeChip").textContent = state.grade;
  $("#homeMinutes").textContent = Math.floor(progress.speaking_seconds / 60);
  $("#homeKnown").textContent = known;
  $("#homeTotal").textContent = words.length;
  $("#homeReview").textContent = reviewWords.length;
  $("#navReviewCount").textContent = reviewWords.length;
  $("#navReviewCount").hidden = reviewWords.length === 0;
  const sampleWords = reviewWords.length ? reviewWords.slice(0, 3) : words.slice(0, 3);
  $("#homeReviewWords").innerHTML = sampleWords.map(word => `<span class="review-word"><strong>${escapeHTML(word.word)}</strong><span>${escapeHTML(gloss(word.meaning))}</span></span>`).join("");
  $("#reviewDescription").textContent = reviewWords.length
    ? `这 ${reviewWords.length} 个词来自你的听写、刷词或词卡记录，复习后会同步更新。`
    : `从${state.grade}可练词中开始；听写、刷词和情景词汇共用同一份掌握状态。`;
  renderCoachPlan();
  const active = state.conversation.session?.status === "active";
  $("#resumeBanner").hidden = !active;
  if (active) $("#resumeText").textContent = `${topicNames[state.conversation.session.topic]} · 已完成 ${state.conversation.session.turns.length} 轮`;
}

const planHeroCopy = {
  review: { eyebrow: "今日建议 · 自适应复习", title: "先清掉<br>快要忘的词。", label: "开始智能复习" },
  quiz: { eyebrow: "今日建议 · 自适应复习", title: "先清掉<br>快要忘的词。", label: "开始智能复习" },
  speak: { eyebrow: "今日建议 · 先开口", title: "把词说出来，<br>记忆才留下。", label: "开始 5 分钟对话" },
  scene: { eyebrow: "今日建议 · 情景输入", title: "进入情景，<br>把新词串起来。", label: "进入今日情景" },
  dictation: { eyebrow: "今日建议 · 纸笔检验", title: "写完再拍，<br>让 AI 逐词核对。", label: "开始纸笔听写" },
  vocabulary: { eyebrow: "今日建议 · 情景学习", title: "先理解情景，<br>再记住单词。", label: "进入今日情景" },
  conversation: { eyebrow: "今日建议 · 口语输出", title: "把词说出来，<br>记忆才留下。", label: "开始 5 分钟对话" },
};

function renderCoachPlan() {
  const plan = state.plan;
  const focus = plan?.focus;
  const copy = planHeroCopy[focus?.type] || planHeroCopy.scene;
  $("#heroPlanEyebrow").textContent = copy.eyebrow;
  $("#heroPlanTitle").innerHTML = copy.title;
  $("#heroPlanMessage").textContent = plan?.coach?.message || "系统会结合正确率、错词和开口时长，安排今天的先后顺序。";
  const minutes = (plan?.steps || []).reduce((total, step) => total + (step.minutes || 0), 0);
  $("#planMinutes").textContent = minutes ? `今日约 ${minutes} 分钟` : "今日约 10 分钟";
  $("#homeSmartStart").innerHTML = `${copy.label}<svg><use href="#i-arrow"/></svg>`;
  const steps = plan?.steps || [];
  const freeIndex = String(steps.length + 1).padStart(2, "0");
  const free = `<button data-route="quiz"><span>${freeIndex}</span><div><strong>自由刷词</strong><small>不受计划限制，按自己的节奏练</small></div><svg><use href="#i-arrow"/></svg></button>`;
  $("#learningLoop").style.setProperty("--plan-cols", String(Math.max(1, steps.length + 1)));
  $("#learningLoop").innerHTML = steps.map((step, index) => `<button data-plan-step="${index}"><span>0${index + 1}</span><div><strong>${escapeHTML(step.title)}</strong><small>${escapeHTML(step.detail)}</small></div><svg><use href="#i-arrow"/></svg></button>`).join("") + free;
}

async function refreshPlan() {
  try {
    state.plan = await api(`/api/plan?grade=${encodeURIComponent(state.grade)}`);
    if (state.route === "home") renderCoachPlan();
  } catch { /* the practice UI keeps working when the plan endpoint is unavailable */ }
}

function executePlanStep(step) {
  if (!step) return;
  if (step.type === "quiz" || step.action === "quiz") {
    state.quiz.mode = step.mode || "adaptive";
    state.quiz.scope = step.scope || "review";
    state.quiz.count = step.count || 12;
    state.quiz.active = false;
    state.quiz.result = false;
    navigate("quiz");
    setTimeout(() => startQuiz(), 80);
    return;
  }
  if (step.type === "conversation" || step.action === "conversation") {
    if (state.conversation.session?.status === "active") {
      navigate("conversation");
    } else {
      state.selectedTopic = "campus";
      state.selectedDuration = 5;
      startConversation();
    }
    return;
  }
  if (step.type === "dictation" || step.action === "dictation") {
    state.dictation.mode = step.mode || "paper";
    state.dictation.topic = "all";
    state.dictation.queue = [];
    navigate("dictation");
    return;
  }
  navigate(step.action || "vocabulary");
}

async function continueSmartPlan(skipTypes = []) {
  await refreshPlan();
  const steps = state.plan?.steps || [];
  const next = steps.find(step => !skipTypes.includes(step.type))
    || steps.find(step => step.type !== "quiz")
    || steps[0];
  if (!next) {
    showAlert("今天的计划已完成，可以自由复习。");
    return;
  }
  executePlanStep(next);
}

/* ── 对话页：视图切换 ────────────────────────────────────────
   网络、音频、字幕的具体实现全部在 conversation.js 的 ConversationLive 里。
   这里只根据 session 状态决定显示哪一块，两套逻辑不会互相覆盖。 */

function renderConversation() {
  const session = state.conversation.session;
  $("#conversationSetup").hidden = Boolean(session);
  $("#conversationLive").hidden = !session || session.status !== "active";
  $("#conversationReport").hidden = !session || session.status !== "completed";
  // 左栏的语法卡只在对话进行中出现，其他路由不占位置。
  const grammarCard = $("#grammarCard");
  if (grammarCard) grammarCard.hidden = !session || session.status !== "active";
  if (!session) {
    renderSceneSetup();
    return;
  }
  if (session.status === "active") {
    updateConversationMeta();
    updateTimer();
    return;
  }
  renderConversationReport();
}

function updateConversationMeta() {
  const session = state.conversation.session;
  if (!session) return;
  const scene = session.scene || {};
  const grade = session.grade || "";
  // 标题下面只留「谁在说话 · 哪个年级」，不再重复情景名和一堆元信息。
  $("#liveTopicLabel").textContent = [scene.role_zh || scene.role || "AI 外教", grade].filter(Boolean).join(" · ");
  $("#liveSceneTitle").textContent = scene.goal || "和 AI 聊一聊";
}

function updateTimer() {
  const session = state.conversation.session;
  if (!session || session.status !== "active") return;
  const elapsed = Math.max(0, Math.floor((Date.now() - state.conversation.startedAt) / 1000));
  const remaining = Math.max(0, session.duration_seconds - elapsed);
  $("#timerText").textContent = formatSeconds(remaining);
  const degrees = Math.min(360, elapsed / session.duration_seconds * 360);
  $("#timerRing").style.setProperty("--progress", `${degrees}deg`);
  if (remaining === 0) $("#finishConversation").textContent = "时间到 · 结束并评估";
}

async function finishConversation() {
  const live = state.conversation.live;
  if (!live || state.conversation.busy) return;
  state.conversation.busy = true;
  const button = $("#finishConversation");
  button.disabled = true;
  button.textContent = "正在生成报告…";
  try {
    await live.finish();
  } catch (error) {
    showAlert(error.message, true);
  } finally {
    state.conversation.busy = false;
    button.disabled = false;
    button.textContent = "结束并评估";
  }
}

function renderConversationReport() {
  const session = state.conversation.session;
  const report = session?.report;
  if (!report) return;
  $("#conversationReportSummary").textContent = report.summary;
  $("#conversationScore").textContent = report.overall_score ?? "—";
  $("#conversationScoreUnit").textContent = report.demo ? "流程完成度 · 非真实语言能力分" : "综合表现";
  $("#conversationScoreBar").style.width = `${report.overall_score || 0}%`;
  $("#conversationDemoBadge").hidden = !report.demo;
  $("#reportWords").textContent = report.word_count;
  $("#reportAudios").textContent = `${report.audio_turn_count} 段`;
  $("#reportExpression").textContent = report.expression ?? "待评估";
  const detail = report.pronunciation_detail;
  const measured = detail && detail.overall !== null && detail.overall !== undefined
    ? `讯飞实测：平均 ${detail.overall} 分（准确 ${detail.accuracy} · 流利 ${detail.fluency} · 完整 ${detail.completeness}），共 ${detail.turns} 轮有录音。`
    : "";
  $("#pronunciationSummary").textContent = [measured, report.pronunciation_summary].filter(Boolean).join(" ");
  renderProfile(report.profile);
  $("#reportTurns").innerHTML = session.turns.length ? session.turns.map(turn => `
    <div class="report-turn"><span>0${turn.number}</span><div><p>${escapeHTML(turn.transcript || "仅保存录音")}</p><p>${escapeHTML(turn.tutor_reply)}</p></div>${turn.audio_url ? `<button class="audio-link" data-audio="${turn.audio_url}"><svg><use href="#i-play"/></svg>回放</button>` : ""}</div>`).join("") : "<p>本次没有完成的对话轮次，可以再试一次。</p>";
}

const PROFILE_ICONS = { grammar: "i-book", tense: "i-clock", fluency: "i-wave", pronunciation: "i-mic" };

function renderProfile(profile) {
  const dims = $("#profileDims");
  const rows = $("#profileRows");
  if (!dims || !rows) return;
  const list = profile?.dimensions || [];
  if (!list.length) {
    $("#profileSummary").textContent = "本场数据不足，先完成几轮对话。";
    dims.innerHTML = "";
    rows.innerHTML = "";
    return;
  }
  $("#profileSummary").textContent = profile.summary || "以下是本场的四个维度。";
  dims.innerHTML = list.map(item => `
    <div class="profile-dim">
      <svg><use href="#${PROFILE_ICONS[item.key] || "i-wave"}"/></svg>
      <span>${escapeHTML(item.label)}</span>
      <strong>${item.score ?? "—"}</strong>
    </div>`).join("");
  rows.innerHTML = list.map(item => `
    <tr>
      <td>${escapeHTML(item.label)}</td>
      <td>${item.score ?? "—"}</td>
      <td>${escapeHTML(item.note || "")}</td>
    </tr>`).join("");
}

function updateShadowItems(preferredId) {
  const source = state.shadow.type === "word" ? state.content.words : state.content.sentences;
  state.shadow.items = source.filter(item => state.shadow.topic === "all" || item.topic === state.shadow.topic);
  const preferredIndex = state.shadow.items.findIndex(item => item.id === preferredId);
  state.shadow.index = preferredIndex >= 0 ? preferredIndex : 0;
  clearShadowAttempt();
}

function clearShadowAttempt() {
  if (state.shadow.url) URL.revokeObjectURL(state.shadow.url);
  state.shadow.blob = null;
  state.shadow.url = null;
  state.shadow.duration = 0;
  $("#shadowAudioReady").hidden = true;
  $("#pronunciationResult").hidden = true;
  $("#shadowStatus").textContent = "先听一遍示范，再开始跟读。";
  setShadowRecordingUI(false);
  setPracticeStep(1);
}

function currentShadowItem() { return state.shadow.items[state.shadow.index]; }
function shadowText(item) { return state.shadow.type === "word" ? item?.word : item?.text; }

function renderShadowing() {
  $$("#shadowType button").forEach(button => {
    const selected = button.dataset.type === state.shadow.type;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  $("#shadowTopic").value = state.shadow.topic;
  if (!state.shadow.items.length) updateShadowItems();
  $("#shadowCount").textContent = `${state.shadow.items.length} 项`;
  $("#shadowItemList").innerHTML = state.shadow.items.map((item, index) => `
    <button class="picker-item ${index === state.shadow.index ? "is-selected" : ""}" data-shadow-index="${index}"><span><strong>${escapeHTML(shadowText(item))}</strong><small>${escapeHTML(state.shadow.type === "word" ? item.meaning : topicNames[item.topic])}</small></span><svg><use href="#i-chevron"/></svg></button>`).join("");
  renderShadowContent();
}

function renderShadowContent() {
  const item = currentShadowItem();
  if (!item) {
    $("#shadowContent").innerHTML = "<p>当前主题还没有跟读内容，请切换主题。</p>";
    return;
  }
  if (state.shadow.type === "word") {
    const focus = item.example || "先听清重音，再用自然语速读一遍。";
    $("#shadowContent").innerHTML = `<h2 class="shadow-word">${escapeHTML(item.word)}</h2><p class="shadow-phonetic">${escapeHTML(item.phonetic)} · ${escapeHTML(item.part_of_speech)}</p><p class="shadow-meaning">${escapeHTML(item.meaning)}</p><span class="shadow-focus">${escapeHTML(focus)}</span>`;
  } else {
    $("#shadowContent").innerHTML = `<h2 class="shadow-sentence">${escapeHTML(item.text)}</h2><p class="shadow-meaning">${escapeHTML(item.translation)}</p><span class="shadow-focus">${escapeHTML(item.focus)}</span>`;
  }
}

function setPracticeStep(step) {
  $$(".practice-steps li").forEach(item => item.classList.toggle("is-current", Number(item.dataset.step) === step));
}

function setShadowRecordingUI(recording) {
  const button = $("#shadowRecord");
  if (!button) return;
  button.classList.toggle("is-recording", recording);
  button.setAttribute("aria-pressed", String(recording));
  $("#shadowRecordLabel").textContent = recording ? "停止跟读" : "开始跟读";
  button.querySelector("use").setAttribute("href", recording ? "#i-stop" : "#i-mic");
}

async function toggleShadowRecording() {
  const shadow = state.shadow;
  if (!currentShadowItem()) return;
  if (shadow.recorder?.isRecording) {
    const result = await shadow.recorder.stop();
    shadow.blob = result?.blob || null;
    shadow.duration = result?.duration || 0;
    if (shadow.url) URL.revokeObjectURL(shadow.url);
    shadow.url = URL.createObjectURL(shadow.blob);
    $("#shadowAudioPlayback").src = shadow.url;
    $("#shadowAudioReady").hidden = false;
    $("#pronunciationResult").hidden = true;
    $("#shadowStatus").textContent = "录音完成。可以先回放，再提交发音评测。";
    setShadowRecordingUI(false);
    setPracticeStep(2);
    return;
  }
  window.speechSynthesis?.cancel();
  clearShadowAttempt();
  shadow.recorder = new VoiceRecorder({ realtime: false });
  try {
    await shadow.recorder.start();
    setShadowRecordingUI(true);
    setPracticeStep(2);
    $("#shadowStatus").textContent = "正在录音，请自然地读出当前内容；说完点击停止。";
  } catch (error) { showAlert(`无法开始录音：${error.message}`, true); }
}

async function submitPronunciation() {
  const shadow = state.shadow;
  const item = currentShadowItem();
  if (!shadow.blob || !item) return;
  const button = $("#submitPronunciation");
  button.disabled = true;
  button.textContent = "正在上传音频与标准文本…";
  try {
    const form = new FormData();
    form.append("target_text", shadowText(item));
    form.append("content_type", shadow.type);
    form.append("content_id", item.id);
    form.append("audio", shadow.blob, "shadowing.wav");
    const result = await api("/api/pronunciation/evaluate", { method: "POST", body: form });
    const assessment = result.assessment;
    const words = Array.isArray(assessment.word_results) ? assessment.word_results : [];
    const wordList = words.length ? `<div class="result-words"><span class="tiny-label">逐词结果</span><div>${words.map(item => `<span class="word-score is-${item.status}">${escapeHTML(item.word)}<b>${item.score}</b></span>`).join("")}</div></div>` : "";
    $("#pronunciationResult").innerHTML = `
      <div class="result-top"><span class="result-score">${assessment.overall_score ?? "—"}</span><div><strong>${assessment.demo ? "报告布局预览 · 非真实发音评分" : "本次发音结果 · 讯飞 ISE"}</strong><p>${escapeHTML(assessment.summary)}</p></div></div>
      <div class="result-metrics"><div><span>准确度</span><strong>${assessment.accuracy ?? "待评估"}</strong></div><div><span>完整度</span><strong>${assessment.completeness ?? "待评估"}</strong></div><div><span>流利度</span><strong>${assessment.fluency ?? "待评估"}</strong></div></div>
      ${wordList}
      <p class="result-suggestion">${escapeHTML(assessment.suggestion)}</p>
      ${result.audio_url ? `<button class="audio-link" data-audio="${result.audio_url}"><svg><use href="#i-play"/></svg>回放已上传的学生音频</button>` : ""}`;
    $("#pronunciationResult").hidden = false;
    $("#shadowAudioReady").hidden = true;
    $("#shadowStatus").textContent = "本次音频与标准英文已保存。";
    setPracticeStep(3);
  } catch (error) { showAlert(error.message, true); }
  finally { button.disabled = false; button.textContent = "提交发音评测"; }
}

function openShadowing(type, topic = "all", preferredId) {
  const sceneWords = (state.content.scenes || []).flatMap(scene => scene.words);
  const vocabularyWord = type === "word"
    ? [...state.content.vocabulary, ...sceneWords].find(word => word.id === preferredId)
    : null;
  state.shadow.type = type;
  if (vocabularyWord) {
    state.shadow.topic = "all";
    state.shadow.items = [vocabularyWord, ...state.content.words.filter(word => word.id !== vocabularyWord.id)];
    state.shadow.index = 0;
    clearShadowAttempt();
  } else {
    state.shadow.topic = topic;
    updateShadowItems(preferredId);
  }
  navigate("shadowing");
}

function shuffled(items) {
  const copy = [...items];
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const swap = Math.floor(Math.random() * (index + 1));
    [copy[index], copy[swap]] = [copy[swap], copy[index]];
  }
  return copy;
}

function buildDictationQueue(preferredId) {
  let source = gradeWords();
  if (state.dictation.topic === "review") source = reviewWordsForGrade();
  if (state.dictation.topic === "scene") {
    const scene = currentScene();
    const byId = new Map(gradeWords().map(word => [word.id, word]));
    source = scene ? scene.words.map(word => byId.get(word.id) || word).filter(Boolean) : [];
  }
  const queue = shuffled(source);
  if (preferredId) {
    const preferredIndex = queue.findIndex(word => word.id === preferredId);
    if (preferredIndex > 0) queue.unshift(queue.splice(preferredIndex, 1)[0]);
  }
  state.dictation.queue = queue;
  state.dictation.index = 0;
  state.dictation.answered = false;
}

function currentDictationWord() { return state.dictation.queue[state.dictation.index]; }

function renderDictation() {
  clearTimeout(state.dictation.nextTimer);
  $("#dictationTopic").value = state.dictation.topic;
  $("#dictationAuto").checked = state.dictation.autoNext;
  $("#dictationGradeHint").textContent = `${state.grade} · ${state.dictation.topic === "review" ? "待复习词" : "全部可练词"}`;
  $$("#dictationMode button").forEach(button => {
    const selected = button.dataset.mode === state.dictation.mode;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  $("#dictationKeyboard").hidden = state.dictation.mode !== "keyboard";
  $("#dictationPaper").hidden = state.dictation.mode !== "paper";
  if (state.dictation.mode === "paper") {
    renderPaperDictation();
    return;
  }
  if (!state.dictation.queue.length) buildDictationQueue();
  const word = currentDictationWord();
  const total = state.dictation.queue.length;
  $("#dictationPosition").textContent = `第 ${total ? state.dictation.index + 1 : 0} 题`;
  $("#dictationCount").textContent = `共 ${total} 题`;
  $("#dictationProgressBar").style.width = `${total ? (state.dictation.index + 1) / total * 100 : 0}%`;
  $("#dictationAnswer").value = "";
  $("#dictationAnswer").disabled = !word;
  $("#dictationError").hidden = true;
  $("#dictationFeedback").hidden = true;
  $("#dictationForm").querySelector("button[type=submit]").hidden = !word;
  $("#playDictationWord").disabled = !word;
  state.dictation.answered = false;
  if (word && state.dictation.autoNext) {
    state.dictation.nextTimer = setTimeout(() => {
      if (state.route === "dictation" && state.dictation.mode === "keyboard" && !state.dictation.answered) {
        speak(word.word);
      }
    }, 280);
  }
}

async function checkDictation(event) {
  event.preventDefault();
  if (state.dictation.answered) return;
  const word = currentDictationWord();
  if (!word) {
    showAlert("当前范围没有可听写的词，切换范围或先去背词。", true);
    return;
  }
  const answer = $("#dictationAnswer").value.trim();
  if (!answer) {
    $("#dictationError").textContent = "请先输入你听到的英文单词。";
    $("#dictationError").hidden = false;
    $("#dictationAnswer").focus();
    return;
  }
  const submit = $("#dictationForm").querySelector("button[type=submit]");
  submit.disabled = true;
  try {
    const result = await api("/api/dictation/attempt", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ word_id: word.id, answer }),
    });
    state.dictation.answered = true;
    $("#dictationAnswer").disabled = true;
    submit.hidden = true;
    const feedback = $("#dictationFeedback");
    feedback.classList.toggle("is-wrong", !result.correct);
    const meta = [result.book, result.unit].filter(Boolean).join(" · ");
    feedback.innerHTML = `<div class="feedback-heading"><strong>${result.correct ? "拼写正确" : "这个词已加入待复习"}</strong><span>${result.correct ? "再巩固一次发音" : `你的拼写：${escapeHTML(answer)}`}</span></div><p class="feedback-answer">${escapeHTML(result.answer)} <small>${escapeHTML(result.phonetic)}</small></p><p class="feedback-meaning">${escapeHTML(result.meaning)}${meta ? ` · ${escapeHTML(meta)}` : ""}</p>${result.example ? `<p class="feedback-example">${escapeHTML(result.example)}</p>` : ""}<div class="feedback-actions"><button class="text-button" id="feedbackListen">再听一次</button><button class="text-button" id="feedbackShadow">跟读这个词</button><button class="text-button" id="feedbackNext">下一题<svg><use href="#i-arrow"/></svg></button></div>`;
    feedback.hidden = false;
    $("#feedbackListen")?.addEventListener("click", () => speak(word.word));
    $("#feedbackShadow")?.addEventListener("click", () => openShadowing("word", "all", word.id));
    $("#feedbackNext")?.addEventListener("click", nextDictation);
    if (result.correct && state.dictation.autoNext) {
      clearTimeout(state.dictation.nextTimer);
      state.dictation.nextTimer = setTimeout(() => {
        if (state.route === "dictation" && state.dictation.mode === "keyboard" && state.dictation.answered) nextDictation();
      }, 1900);
    }
    await refreshProgress();
  } catch (error) { showAlert(error.message, true); }
  finally { submit.disabled = false; }
}

function nextDictation() {
  clearTimeout(state.dictation.nextTimer);
  if (!state.dictation.queue.length) return;
  state.dictation.index = (state.dictation.index + 1) % state.dictation.queue.length;
  renderDictation();
  if (state.dictation.mode === "keyboard") $("#dictationAnswer").focus();
}

function buildPaperBatch() {
  const scene = currentScene();
  let source = state.dictation.topic === "review" ? reviewWordsForGrade() : gradeWords();
  if (state.dictation.topic === "scene" && scene) source = scene.words;
  const pool = source.length >= 5 ? source : gradeWords();
  state.dictation.paperIds = shuffled(pool).slice(0, Math.min(5, pool.length)).map(word => word.id);
  state.dictation.paperBlob = null;
  if (state.dictation.paperUrl) URL.revokeObjectURL(state.dictation.paperUrl);
  state.dictation.paperUrl = null;
  const input = $("#paperImage");
  if (input) input.value = "";
}

function paperWords() {
  const sceneWords = (state.content.scenes || []).flatMap(scene => scene.words);
  const byId = new Map([...gradeWords(), ...sceneWords].map(word => [word.id, word]));
  return state.dictation.paperIds.map(id => byId.get(id)).filter(Boolean);
}

function renderPaperDictation() {
  if (!state.dictation.paperIds.length) buildPaperBatch();
  $("#paperBatchNumber").textContent = state.dictation.batch;
  $("#paperGraderLabel").textContent = state.photoGrading.ready
    ? `DeepSeek · ${state.photoGrading.model || "vision"} 已配置`
    : "照片可上传；批改服务待配置 Key";
  $("#paperPreview").hidden = !state.dictation.paperUrl;
  if (state.dictation.paperUrl) {
    $("#paperPreviewImage").src = state.dictation.paperUrl;
    $("#paperFileName").textContent = "答案纸照片已准备好";
  }
  $("#paperDrop").hidden = Boolean(state.dictation.paperUrl);
  $("#gradePaper").disabled = !state.dictation.paperBlob;
  $("#paperWordRows").innerHTML = paperWords().map((word, index) => `
    <li><span>0${index + 1}</span><p>在纸上第 ${index + 1} 行写下你听到的单词</p><button data-paper-word="${word.id}"><svg><use href="#i-play"/></svg>播放第 ${index + 1} 题</button></li>`).join("");
  if (!state.dictation.paperBlob) {
    $("#paperStatus").textContent = "先在纸上写完，再上传照片。";
    $("#paperResult").hidden = true;
  }
}

function attachPaperImage(file) {
  if (!file) return;
  if (!file.type.startsWith("image/")) {
    showAlert("请选择 JPG、PNG 或 WebP 照片。", true);
    return;
  }
  if (file.size > 12 * 1024 * 1024) {
    showAlert("照片不能超过 12 MB，请压缩或重新拍摄。", true);
    return;
  }
  if (state.dictation.paperUrl) URL.revokeObjectURL(state.dictation.paperUrl);
  state.dictation.paperBlob = file;
  state.dictation.paperUrl = URL.createObjectURL(file);
  $("#paperPreview").hidden = false;
  $("#paperPreviewImage").src = state.dictation.paperUrl;
  $("#paperFileName").textContent = file.name || "答案纸照片";
  $("#paperDrop").hidden = true;
  $("#gradePaper").disabled = false;
  $("#paperStatus").textContent = "照片已就绪，点击下方按钮提交批改。";
}

async function probeGraderService(force = true) {
  const button = $("#probeGrader");
  const detail = $("#probeDetail");
  if (!button || button.disabled) return null;
  const original = button.innerHTML;
  button.disabled = true;
  button.textContent = "正在检测…";
  $("#paperGraderLabel").textContent = "正在检测批改服务…";
  try {
    const result = await api(`/api/grader/probe${force ? "?force=true" : ""}`);
    const verdictText = {
      ok: "连通正常，凭证有效",
      insufficient_balance: "已连通 · 账户余额不足",
      auth_failed: "已连通 · 但 Key 被拒绝",
      endpoint_not_found: "已连通 · 但接口路径不对",
      rate_limited: "已连通 · 当前被限流",
      server_error: "已连通 · 服务端暂时异常",
      unconfigured: "尚未配置 DeepSeek Key",
      network_error: "网络无法连接到该地址",
    }[result.verdict] || result.message;
    $("#paperGraderLabel").textContent = verdictText;
    detail.hidden = false;
    detail.className = `probe-detail ${["ok", "insufficient_balance"].includes(result.verdict) ? "is-ok" : "is-warn"}`;
    const latency = result.latency_ms == null ? "—" : `${result.latency_ms} ms`;
    const status = result.http_status ? `HTTP ${result.http_status}` : "无响应";
    detail.innerHTML = `${escapeHTML(result.base_url)} · ${escapeHTML(result.model)} · ${status} · ${latency}${result.hint ? `<br>${escapeHTML(result.hint)}` : ""}`;
    return result;
  } catch (error) {
    $("#paperGraderLabel").textContent = "检测失败";
    detail.hidden = false;
    detail.className = "probe-detail is-warn";
    detail.textContent = error.message;
    return null;
  } finally {
    button.disabled = false;
    button.innerHTML = original;
  }
}

async function gradePaper() {
  if (!state.dictation.paperBlob || !state.dictation.paperIds.length) return;
  const button = $("#gradePaper");
  button.disabled = true;
  button.textContent = "正在上传照片…";
  $("#paperStatus").textContent = "正在保存照片并请求批改，请稍候。";
  try {
    const form = new FormData();
    form.append("word_ids", JSON.stringify(state.dictation.paperIds));
    form.append("image", state.dictation.paperBlob, state.dictation.paperBlob.name || "paper.jpg");
    const result = await api("/api/dictation/photo", { method: "POST", body: form });
    renderPaperResult(result);
    $("#paperStatus").textContent = result.message || "批改完成。";
    if (result.status === "graded") await refreshProgress();
  } catch (error) {
    showAlert(error.hint ? `${error.message} ${error.hint}` : error.message, true);
    $("#paperStatus").textContent = error.hint || "这次没有完成批改，可以重新上传或稍后再试。";
  } finally {
    button.disabled = false;
    button.innerHTML = '<svg><use href="#i-camera"/></svg>交给 AI 批改';
  }
}

function renderPaperResult(result) {
  const expected = paperWords();
  const byId = new Map(expected.map(word => [word.id, word]));
  const items = result.items || [];
  const graded = result.status === "graded";
  const heading = graded ? "手写听写批改" : result.status === "unreadable" ? "这张照片没看清" : "照片已保存";
  $("#paperResult").innerHTML = `
    <div class="paper-result-head"><h2>${heading}</h2><span>${escapeHTML(result.provider || "")}</span></div>
    ${graded ? `<div class="paper-result-score">${result.correct_count} / ${result.total}</div>` : ""}
    <p>${escapeHTML(result.message || "")}</p>
    ${result.hint ? `<p class="paper-result-hint">${escapeHTML(result.hint)}</p>` : ""}
    <div class="paper-result-list">${items.map(item => {
      const expectedWord = byId.get(item.word_id)?.word || item.expected;
      const detail = item.correct === null || item.correct === undefined
        ? "未看清，可重拍"
        : item.recognized
          ? `识别为 ${escapeHTML(item.recognized)}`
          : "标准答案";
      return `<div class="paper-result-item ${item.correct === false ? "is-wrong" : ""}"><b>${escapeHTML(expectedWord)}</b><span>${detail}</span></div>`;
    }).join("")}</div>
    <div class="paper-result-actions"><button class="button button-primary" id="paperNextBatch"><svg><use href="#i-refresh"/></svg>再听一组</button>${result.status === "graded" ? '<button class="button button-outline" id="paperToQuiz">去刷这些词</button>' : ""}</div>`;
  $("#paperResult").hidden = false;
  $("#paperNextBatch")?.addEventListener("click", () => {
    state.dictation.batch += 1;
    buildPaperBatch();
    renderPaperDictation();
  });
  $("#paperToQuiz")?.addEventListener("click", () => {
    state.quiz.scope = "all";
    navigate("quiz");
  });
}

function currentScene() {
  const scenes = state.content.scenes || [];
  if (!scenes.length) return null;
  if (state.vocabulary.sceneIndex >= scenes.length) state.vocabulary.sceneIndex = 0;
  return scenes[state.vocabulary.sceneIndex];
}

function sceneWord(item) {
  return `<button class="scene-word-item ${item.id === state.vocabulary.selectedWordId ? "is-selected" : ""}" data-vocab-word="${item.id}"><span><strong>${escapeHTML(item.word)}</strong><small>${escapeHTML(gloss(item.meaning))}</small></span><span class="word-pos">${escapeHTML(item.part_of_speech || "")}</span></button>`;
}

function renderVocabulary() {
  const scene = currentScene();
  const words = gradeWords();
  $("#gradeVocabGrade").textContent = state.grade;
  $("#gradeVocabCount").textContent = words.length;
  $("#wordbookCount").textContent = `${state.grade} · ${words.length} 个可练词`;
  if (!scene) {
    $("#scenePicker").innerHTML = "";
    $("#sceneBody").innerHTML = "<p>还没有可用的情景，请刷新页面。</p>";
    renderVocabularyProgress();
    return;
  }
  if (!scene.words.length) return;
  if (!scene.words.some(word => word.id === state.vocabulary.selectedWordId)) {
    state.vocabulary.selectedWordId = scene.words[0].id;
  }
  $("#scenePicker").innerHTML = (state.content.scenes || []).map((item, index) => `
    <button class="scene-tab ${index === state.vocabulary.sceneIndex ? "is-selected" : ""}" data-scene-index="${index}" role="tab" aria-selected="${index === state.vocabulary.sceneIndex}"><span class="scene-tab-index">${String(index + 1).padStart(2, "0")} · ${item.grade}</span><strong>${escapeHTML(item.title)}</strong><small>${escapeHTML(item.kicker)}</small></button>`).join("");
  $("#sceneIndex").textContent = `情景 ${String(state.vocabulary.sceneIndex + 1).padStart(2, "0")}`;
  $("#sceneKicker").textContent = scene.kicker.toUpperCase();
  $("#sceneTitle").textContent = scene.title;
  $("#sceneDescription").textContent = scene.description;
  $("#sceneBody").innerHTML = escapeHTML(scene.scene).replace(/\{\{([^}]+)\}\}/g, (_, word) => `<button class="scene-word ${word.toLowerCase() === (scene.words.find(item => item.id === state.vocabulary.selectedWordId)?.word || "").toLowerCase() ? "is-active" : ""}" data-scene-word="${escapeHTML(word)}">${escapeHTML(word)}</button>`);
  $("#sceneZh").textContent = scene.scene_zh;
  $("#scenePrompt").textContent = scene.prompt;
  $("#sceneWordCount").textContent = `${scene.words.length} 个`;
  $("#sceneWordList").innerHTML = scene.words.map(sceneWord).join("");
  renderVocabDetail(scene);
  renderVocabularyProgress();
  $("#wordbook").open = Boolean(state.vocabulary.wordbookOpen);
  $("#wordSearch").value = state.vocabulary.wordbookFilter || "";
  renderWordbook();
}

function selectedVocabularyWord(scene = currentScene()) {
  const id = state.vocabulary.selectedWordId;
  return scene?.words.find(word => word.id === id) || gradeWords().find(word => word.id === id) || scene?.words[0] || null;
}

function contextSentence(word, scene) {
  if (!scene || !word?.word) return "";
  const plain = scene.scene.replace(/\{\{|\}\}/g, "");
  const sentences = plain.match(/[^.!?]+[.!?]?/g) || [];
  const target = word.word.toLowerCase();
  return (sentences.find(sentence => sentence.toLowerCase().includes(target)) || "").trim();
}

function renderVocabDetail(scene = currentScene()) {
  const word = selectedVocabularyWord(scene);
  if (!word) {
    $("#vocabDetailMain").innerHTML = '<div class="empty-state"><h2>选一个关键词</h2><p>点情景里的高亮词，或从全部词库中搜索。</p></div>';
    return;
  }
  const context = contextSentence(word, scene);
  const source = [word.book, word.unit].filter(Boolean).join(" · ") || `${word.grade}推荐词`;
  $("#vocabDetailMain").innerHTML = `
    <div class="inspector-top"><span class="soft-tag">${context ? "本场关键词" : "词库词条"}</span><span class="tiny-label">${escapeHTML(source)}</span></div>
    <div class="inspector-wordline"><h2>${escapeHTML(word.word)}</h2><span>${escapeHTML(word.phonetic)} ${word.part_of_speech ? `· ${escapeHTML(word.part_of_speech)}` : ""}</span><button class="word-play" id="playSelectedWord"><svg><use href="#i-play"/></svg>听发音</button></div>
    <p class="inspector-meaning">${escapeHTML(word.meaning)}</p>
    ${context ? `<p class="inspector-context">“${escapeHTML(context)}”</p>` : '<p class="inspector-context">把这个词放回一个真实句子里，再复述一次情景。</p>'}`;
  $("#playSelectedWord")?.addEventListener("click", () => speak(word.word));
  $("#vocabDetailActions").hidden = false;
}

function renderVocabularyProgress() {
  const words = gradeWords();
  const states = state.progress.word_states || {};
  const known = words.filter(word => states[word.id]?.known).length;
  const review = words.filter(word => states[word.id]?.attempts && !states[word.id]?.known).length;
  const percent = words.length ? Math.round(known / words.length * 100) : 0;
  $("#knownPercent").textContent = `${percent}%`;
  $("#wordProgressRing").style.setProperty("--known", `${percent * 3.6}deg`);
  $("#vocabKnownCount").textContent = known;
  $("#vocabReviewCount").textContent = review;
  $("#vocabTotalCount").textContent = words.length;
}

function renderWordbook() {
  const query = (state.vocabulary.wordbookFilter || "").trim().toLowerCase();
  const words = gradeWords();
  const matches = (query
    ? words.filter(word => word.word.toLowerCase().includes(query) || word.meaning.toLowerCase().includes(query))
    : words).slice(0, 80);
  $("#wordbookHint").textContent = query ? `找到 ${words.filter(word => word.word.toLowerCase().includes(query) || word.meaning.toLowerCase().includes(query)).length} 个` : "按年级与教材单元整理";
  $("#wordbookList").innerHTML = matches.length ? matches.map(word => `<button class="wordbook-item" data-wordbook-word="${word.id}"><strong>${escapeHTML(word.word)}</strong><span>${escapeHTML(gloss(word.meaning))}</span></button>`).join("") : '<div class="empty-state"><h2>没有匹配的词</h2><p>换一个拼写或中文释义试试。</p></div>';
}

async function reviewSelectedWord(known) {
  const word = selectedVocabularyWord();
  if (!word) return;
  try {
    await api(`/api/vocabulary/${word.id}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ known }),
    });
    await refreshProgress();
    renderVocabulary();
    showAlert(known ? `已把 ${word.word} 标为掌握。` : `${word.word} 已留在待复习队列。`);
  } catch (error) { showAlert(error.message, true); }
}

function quizPool() {
  const scope = state.quiz.scope;
  if (scope === "review") return reviewWordsForGrade();
  if (scope === "scene") {
    const scene = currentScene();
    if (!scene) return [];
    const byId = new Map([...gradeWords(), ...(state.content.vocabulary || [])].map(word => [word.id, word]));
    return scene.words.map(word => byId.get(word.id) || word);
  }
  return gradeWords();
}

function adaptiveModeFor(word) {
  const stateForWord = state.progress.word_states?.[word.id] || {};
  const attempts = Number(stateForWord.attempts || 0);
  const accuracy = attempts ? Number(stateForWord.correct || 0) / attempts : 0;
  if (attempts === 0 || accuracy < 0.6) return "en-cn";
  if (!stateForWord.known) return "cn-en";
  return "write";
}

function buildQuizQuestion(word, modeOverride = null) {
  const mode = modeOverride || (state.quiz.mode === "adaptive" ? adaptiveModeFor(word) : state.quiz.mode);
  const all = gradeWords();
  const optionDisplay = mode === "en-cn" ? item => gloss(item.meaning) : item => item.word;
  const correctText = optionDisplay(word);
  const seen = new Set([correctText.toLowerCase()]);
  const distractors = [];
  if (mode !== "write") {
    for (const candidate of shuffled(all)) {
      if (candidate.id === word.id) continue;
      const text = optionDisplay(candidate);
      if (!text || seen.has(text.toLowerCase())) continue;
      seen.add(text.toLowerCase());
      distractors.push(text);
      if (distractors.length === 3) break;
    }
  }
  const options = shuffled([{ text: correctText, correct: true }, ...distractors.map(text => ({ text, correct: false }))]);
  return { word, mode, options };
}

function startQuiz(customWords = null) {
  let pool = customWords || quizPool();
  if (!pool.length) {
    showAlert(state.quiz.scope === "review" ? "还没有待复习的词，先做一次听写或刷词。" : "当前范围没有可用单词。", true);
    return;
  }
  const words = shuffled(pool).slice(0, Math.min(state.quiz.count, pool.length));
  state.quiz.questions = words.map(buildQuizQuestion);
  state.quiz.index = 0;
  state.quiz.correct = 0;
  state.quiz.wrong = [];
  state.quiz.answered = false;
  state.quiz.active = true;
  state.quiz.result = false;
  state.quiz.lastSync = null;
  renderQuiz();
}

function renderQuiz() {
  const quiz = state.quiz;
  $("#quizAutoNext").checked = quiz.autoNext;
  $("#quizSetup").hidden = quiz.active || quiz.result;
  $("#quizLive").hidden = !quiz.active;
  $("#quizResult").hidden = !quiz.result;
  $("#quizGradeHint").textContent = `${state.grade} · ${quiz.count} 题`;
  $$("#quizScopeOptions button").forEach(button => {
    const selected = button.dataset.scope === quiz.scope;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-checked", String(selected));
  });
  $$("[data-quiz-mode]").forEach(button => {
    const selected = button.dataset.quizMode === quiz.mode;
    button.classList.toggle("is-selected", selected);
    button.setAttribute("aria-checked", String(selected));
  });
  if (quiz.result) renderQuizResult();
  else if (quiz.active) renderQuizQuestion();
  else {
    const pool = quizPool();
    $("#quizPoolHint").textContent = quiz.scope === "scene"
      ? `从「${currentScene()?.title || "当前情景"}」的六个词中出题。`
      : quiz.scope === "review"
        ? `当前有 ${pool.length} 个待复习词。`
        : `从${state.grade}的 ${pool.length} 个可练词中随机抽题。`;
  }
}

function renderQuizQuestion() {
  const quiz = state.quiz;
  clearTimeout(quiz.nextTimer);
  const current = quiz.questions[quiz.index];
  if (!current) return;
  const { word } = current;
  const questionMode = current.mode || quiz.mode;
  const labels = { "en-cn": "英 → 中", "cn-en": "中 → 英", write: "看中文默写", adaptive: "智能混合" };
  $("#quizDirectionLabel").textContent = `刷单词 · ${labels[questionMode] || labels.adaptive}`;
  $("#quizProgressText").textContent = `${quiz.index + 1} / ${quiz.questions.length}`;
  $("#quizCorrectText").textContent = quiz.correct;
  $("#quizProgressBar").style.width = `${(quiz.index / quiz.questions.length) * 100}%`;
  if (questionMode === "en-cn") {
    $("#quizQuestion").innerHTML = `<p class="quiz-eyebrow">这个单词的意思是？</p><h2 class="quiz-word">${escapeHTML(word.word)}</h2><p class="quiz-hint">${escapeHTML(word.phonetic)} · ${escapeHTML(word.part_of_speech || "")}</p>`;
    $("#quizAudioRow").innerHTML = `<button class="text-button" data-quiz-speak><svg><use href="#i-play"/></svg>听单词发音</button>`;
  } else {
    $("#quizQuestion").innerHTML = `<p class="quiz-eyebrow">${questionMode === "write" ? "根据释义拼出英文" : "选出对应的英文单词"}</p><h2 class="quiz-meaning">${escapeHTML(word.meaning)}</h2>${questionMode === "write" ? '<p class="quiz-hint">不看选项，凭记忆写出完整拼写。</p>' : `<p class="quiz-hint">${escapeHTML(word.part_of_speech || "")}</p>`}`;
    $("#quizAudioRow").innerHTML = "";
  }
  $("#quizOptions").hidden = questionMode === "write";
  $("#quizWriteForm").hidden = questionMode !== "write";
  $("#quizOptions").innerHTML = questionMode === "write" ? "" : current.options.map((option, index) => `<button class="quiz-option" data-quiz-option="${index}"><span class="option-key">${String.fromCharCode(65 + index)}</span><span>${escapeHTML(option.text)}</span></button>`).join("");
  $("#quizWriteInput").value = "";
  $("#quizWriteInput").disabled = false;
  $("#quizWriteForm").querySelector("button").disabled = false;
  $("#quizFeedback").hidden = true;
  $("#quizNext").hidden = true;
  $("#quizNext").innerHTML = quiz.index === quiz.questions.length - 1 ? '查看结果<svg><use href="#i-arrow"/></svg>' : '下一题<svg><use href="#i-arrow"/></svg>';
  quiz.answered = false;
  if (questionMode === "write") setTimeout(() => $("#quizWriteInput")?.focus(), 30);
}

async function recordQuizAnswer(correct) {
  const word = state.quiz.questions[state.quiz.index]?.word;
  if (!word) return;
  try {
    await api(`/api/vocabulary/${word.id}/quiz`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ correct }),
    });
  } catch { /* a local quiz answer should not be blocked by progress sync */ }
}

function scheduleQuizAdvance(correct) {
  const quiz = state.quiz;
  clearTimeout(quiz.nextTimer);
  if (!correct || !quiz.autoNext || state.voice.handsfree) return;
  quiz.nextTimer = setTimeout(() => {
    if (state.route === "quiz" && quiz.active && quiz.answered && !state.voice.handsfree) quizAdvance();
  }, 1700);
}

function answerQuizOption(index) {
  const quiz = state.quiz;
  const current = quiz.questions[quiz.index];
  if (quiz.answered || !current || current.mode === "write") return;
  const selected = current.options[index];
  if (!selected) return;
  quiz.answered = true;
  $$("#quizOptions .quiz-option").forEach((button, optionIndex) => {
    const option = current.options[optionIndex];
    button.disabled = true;
    if (option.correct) button.classList.add("is-correct");
    else if (optionIndex === index) button.classList.add("is-wrong");
  });
  const correct = Boolean(selected.correct);
  if (correct) quiz.correct += 1;
  else quiz.wrong.push(current.word);
  $("#quizFeedback").classList.toggle("is-wrong", !correct);
  $("#quizFeedback").innerHTML = correct
    ? `<strong>答对了。</strong> <span>${escapeHTML(current.word.word)} · ${escapeHTML(current.word.meaning)}</span>`
    : `<strong>再看一眼。</strong> 正确答案是 <span>${escapeHTML(current.word.word)}</span> · ${escapeHTML(current.word.meaning)}。这个词会回到待复习。`;
  $("#quizFeedback").hidden = false;
  $("#quizNext").hidden = false;
  $("#quizCorrectText").textContent = quiz.correct;
  quiz.lastSync = Promise.all([quiz.lastSync || Promise.resolve(), recordQuizAnswer(correct)]);
  scheduleQuizAdvance(correct);
  return correct;
}

function submitQuizWrite(event) {
  event.preventDefault();
  const quiz = state.quiz;
  const current = quiz.questions[quiz.index];
  if (quiz.answered || !current || current.mode !== "write") return;
  const value = $("#quizWriteInput").value.trim();
  if (!value) { $("#quizWriteInput").focus(); return; }
  const normalize = text => text.toLowerCase().replace(/[^a-z0-9]/g, "");
  const correct = normalize(value) === normalize(current.word.word);
  quiz.answered = true;
  $("#quizWriteInput").disabled = true;
  $("#quizWriteForm").querySelector("button").disabled = true;
  if (correct) quiz.correct += 1;
  else quiz.wrong.push(current.word);
  $("#quizFeedback").classList.toggle("is-wrong", !correct);
  $("#quizFeedback").innerHTML = correct
    ? `<strong>拼写正确。</strong> <span>${escapeHTML(current.word.word)} · ${escapeHTML(current.word.meaning)}</span>`
    : `<strong>你的答案：${escapeHTML(value)}。</strong> 正确拼写是 <span>${escapeHTML(current.word.word)}</span>。这个词会回到待复习。`;
  $("#quizFeedback").hidden = false;
  $("#quizNext").hidden = false;
  $("#quizCorrectText").textContent = quiz.correct;
  quiz.lastSync = Promise.all([quiz.lastSync || Promise.resolve(), recordQuizAnswer(correct)]);
  scheduleQuizAdvance(correct);
  return correct;
}

async function quizAdvance() {
  const quiz = state.quiz;
  clearTimeout(quiz.nextTimer);
  if (!quiz.answered) return;
  if (quiz.index >= quiz.questions.length - 1) {
    try { await quiz.lastSync; } catch { /* keep the local result visible */ }
    quiz.active = false;
    quiz.result = true;
    renderQuiz();
    refreshProgress();
    refreshPlan();
    return;
  }
  quiz.index += 1;
  renderQuizQuestion();
}

function renderQuizResult() {
  const quiz = state.quiz;
  const total = quiz.questions.length;
  const percent = total ? Math.round(quiz.correct / total * 100) : 0;
  $("#quizResultPercent").textContent = `${percent}%`;
  $("#quizResultCorrect").textContent = quiz.correct;
  $("#quizResultTotal").textContent = total;
  $("#quizResultSummary").textContent = percent >= 90
    ? `这一轮完成得很稳。${state.grade}词库里还有更多词，可以换一组继续。`
    : `错词已进入待复习，下次会在听写和刷词里再遇到。`;
  const wrong = quiz.wrong.filter((word, index, list) => list.findIndex(item => item.id === word.id) === index);
  $("#quizWrongWords").innerHTML = wrong.length ? wrong.map(word => `<span class="quiz-wrong-chip">${escapeHTML(word.word)}<small>${escapeHTML(gloss(word.meaning))}</small></span>`).join("") : '<p class="microcopy">这轮没有错词。可以去听写一组新词。</p>';
}

async function refreshProgress() {
  state.progress = await api("/api/progress");
  const reviewChip = $("#navReviewCount");
  if (reviewChip) {
    const review = reviewWordsForGrade().length;
    reviewChip.textContent = review;
    reviewChip.hidden = review === 0;
  }
}

async function restoreSession() {
  const id = localStorage.getItem("echo.activeSession");
  if (!id) return;
  try {
    const session = await api(`/api/conversation/${id}`);
    if (session.status === "active") {
      state.conversation.session = session;
      state.conversation.startedAt = Date.parse(session.created_at);
      state.conversation.timer = setInterval(updateTimer, 1000);
    } else localStorage.removeItem("echo.activeSession");
  } catch { localStorage.removeItem("echo.activeSession"); }
}

function recognitionCtor() {
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function updateVoiceUI() {
  const trigger = $("#voiceTrigger");
  if (!trigger) return;
  trigger.classList.toggle("is-listening", state.voice.listening);
  trigger.classList.toggle("is-handsfree", state.voice.handsfree);
  trigger.setAttribute("aria-pressed", String(state.voice.listening || state.voice.handsfree));
  $("#voiceTriggerLabel").textContent = state.voice.handsfree
    ? "免手中 · 点此停止"
    : state.voice.listening ? "正在听…" : "语音助手";
}

function openVoicePanel(open = true) {
  state.voice.panelOpen = open;
  $("#voicePanel").hidden = !open;
  $("#voiceAssistant").classList.toggle("panel-open", open);
}

function setVoiceReply(text) {
  $("#voiceReply").textContent = text;
}

async function voiceSay(text, lang = "zh-CN", rate = 0.96) {
  openVoicePanel(true);
  setVoiceReply(text);
  await speakAsync(text, lang, rate);
}

function stopCommandRecognition() {
  const recognition = state.voice.recognition;
  state.voice.recognition = null;
  state.voice.listening = false;
  updateVoiceUI();
  if (recognition) {
    try { recognition.abort(); } catch { /* already stopped */ }
  }
}

function startCommandListening() {
  const Recognition = recognitionCtor();
  if (!Recognition) {
    openVoicePanel(true);
    setVoiceReply("当前浏览器不支持语音识别。可以直接在下方输入指令，例如「开始今天的训练」。");
    $("#voiceTextInput").focus();
    return;
  }
  if (state.voice.handsfree) {
    stopHandsfree();
    return;
  }
  stopCommandRecognition();
  window.speechSynthesis?.cancel();
  const recognition = new Recognition();
  state.voice.recognition = recognition;
  state.voice.listening = true;
  state.voice.mode = "command";
  updateVoiceUI();
  openVoicePanel(true);
  $("#voiceHeard").textContent = "我在听，请说……";
  setVoiceReply("");
  let handled = false;
  recognition.lang = "zh-CN";
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.onresult = event => {
    let interim = "";
    let finalText = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const text = event.results[index][0].transcript;
      if (event.results[index].isFinal) finalText += text;
      else interim += text;
    }
    if (interim) $("#voiceHeard").textContent = interim;
    if (finalText && !handled) {
      handled = true;
      $("#voiceHeard").textContent = `“${finalText.trim()}”`;
      state.voice.listening = false;
      updateVoiceUI();
      handleVoiceCommand(finalText.trim());
    }
  };
  recognition.onerror = event => {
    state.voice.listening = false;
    updateVoiceUI();
    if (["not-allowed", "service-not-allowed"].includes(event.error)) {
      setVoiceReply("麦克风权限没有开启。允许权限后再按空格，或在下方输入指令。");
    } else if (event.error === "no-speech") {
      setVoiceReply("没有听到声音。再按一次空格，或直接输入指令。");
    }
  };
  recognition.onend = () => {
    state.voice.listening = false;
    updateVoiceUI();
  };
  try { recognition.start(); }
  catch {
    state.voice.listening = false;
    updateVoiceUI();
  }
}

function parseChoice(text, current) {
  if (!current) return -1;
  const compact = String(text).toLowerCase().replace(/[。！？，、,.!?\s]/g, "");
  const letter = compact.match(/^(?:选|选择|我选|答案|答案是)?([abcd])(?:项|个)?$/);
  if (letter) return "abcd".indexOf(letter[1]);
  const ordinals = ["第一个", "第二个", "第三个", "第四个"];
  for (let index = 0; index < ordinals.length; index += 1) {
    if (compact.includes(ordinals[index])) return index;
  }
  const options = current.options || [];
  let best = -1;
  for (let index = 0; index < options.length; index += 1) {
    const optionText = String(options[index].text).toLowerCase().replace(/[。！？，、,.!?\s；;]/g, "");
    if (optionText && (compact.includes(optionText) || optionText.includes(compact))) best = index;
  }
  return best;
}

function quizQuestionSpeech(current) {
  if (!current) return "";
  const index = state.quiz.index + 1;
  const total = state.quiz.questions.length;
  const mode = current.mode;
  if (mode === "en-cn") {
    const options = current.options.map((option, position) => `${String.fromCharCode(65 + position)}，${option.text}`).join("；");
    return `第 ${index} 题，共 ${total} 题。单词 ${current.word.word}，是什么意思？选项：${options}。请说 A、B、C 或 D。`;
  }
  if (mode === "write") {
    return `第 ${index} 题，共 ${total} 题。释义：${gloss(current.word.meaning)}。请直接说出对应的英文单词。`;
  }
  const options = current.options.map((option, position) => `${String.fromCharCode(65 + position)}，${option.text}`).join("；");
  return `第 ${index} 题，共 ${total} 题。中文意思是：${gloss(current.word.meaning)}。选出对应的英文单词。选项：${options}。请说 A、B、C 或 D。`;
}

async function readQuizForVoice() {
  if (!state.voice.handsfree || !state.quiz.active) return;
  const current = state.quiz.questions[state.quiz.index];
  if (!current) return;
  state.voice.retry = 0;
  await voiceSay(quizQuestionSpeech(current), "zh-CN", 0.95);
  if (state.voice.handsfree && state.quiz.active && !state.quiz.answered) {
    setTimeout(() => listenForHandsfreeAnswer(), 250);
  }
}

function listenForHandsfreeAnswer() {
  if (!state.voice.handsfree || !state.quiz.active || state.quiz.answered) return;
  const Recognition = recognitionCtor();
  if (!Recognition) {
    stopHandsfree();
    setVoiceReply("这个浏览器不支持语音识别，可以改用键盘刷题。");
    return;
  }
  const current = state.quiz.questions[state.quiz.index];
  const recognition = new Recognition();
  state.voice.recognition = recognition;
  state.voice.listening = true;
  state.voice.mode = "answer";
  updateVoiceUI();
  recognition.lang = current.mode === "en-cn" ? "zh-CN" : "en-US";
  recognition.interimResults = true;
  recognition.continuous = false;
  let handled = false;
  recognition.onresult = event => {
    let interim = "";
    let finalText = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      const text = event.results[index][0].transcript;
      if (event.results[index].isFinal) finalText += text;
      else interim += text;
    }
    if (interim) $("#voiceHeard").textContent = `听到：${interim}`;
    if (finalText && !handled) {
      handled = true;
      $("#voiceHeard").textContent = `“${finalText.trim()}”`;
      state.voice.listening = false;
      updateVoiceUI();
      handleHandsfreeAnswer(finalText.trim(), current);
    }
  };
  recognition.onerror = event => {
    state.voice.listening = false;
    updateVoiceUI();
    if (!state.voice.handsfree) return;
    if (["no-speech", "aborted"].includes(event.error) && (state.voice.retry || 0) < 1) {
      state.voice.retry = (state.voice.retry || 0) + 1;
      setTimeout(() => listenForHandsfreeAnswer(), 500);
    }
  };
  recognition.onend = () => {
    state.voice.listening = false;
    updateVoiceUI();
  };
  try { recognition.start(); }
  catch {
    state.voice.listening = false;
    updateVoiceUI();
  }
}

async function handleHandsfreeAnswer(text, current) {
  if (!state.voice.handsfree || state.quiz.answered) return;
  const compact = String(text).toLowerCase().replace(/[。！？，、,.!?\s]/g, "");
  if (/停止|退出|结束/.test(compact)) {
    stopHandsfree();
    await voiceSay("已退出免手模式。", "zh-CN", 1);
    return;
  }
  if (/读题|再读|重复|没听清/.test(compact)) {
    await readQuizForVoice();
    return;
  }
  if (current.mode === "write") {
    const normalize = value => String(value).toLowerCase().replace(/[^a-z0-9]/g, "");
    const correct = normalize(text) === normalize(current.word.word);
    $("#quizWriteInput").value = text;
    $("#quizWriteForm").requestSubmit();
    await voiceAfterAnswer(correct, current);
    return;
  }
  const choice = parseChoice(text, current);
  if (choice < 0 || !current.options[choice]) {
    await voiceSay("没听清选项。请再说 A、B、C 或 D。", "zh-CN", 0.98);
    if (state.voice.handsfree) setTimeout(() => listenForHandsfreeAnswer(), 250);
    return;
  }
  const correct = answerQuizOption(choice);
  await voiceAfterAnswer(Boolean(correct), current);
}

async function voiceAfterAnswer(correct, current) {
  await delay(280);
  const message = correct
    ? "回答正确。"
    : `正确答案是 ${current.word.word}，${gloss(current.word.meaning)}。`;
  await voiceSay(message, "zh-CN", 0.98);
  await delay(420);
  await quizAdvance();
  if (!state.voice.handsfree) return;
  if (state.quiz.result) {
    await voiceSay(`本轮完成，答对 ${state.quiz.correct} 题，共 ${state.quiz.questions.length} 题。说再练一轮，或者退出。`, "zh-CN", 0.98);
    state.voice.handsfree = false;
    updateVoiceUI();
    return;
  }
  if (state.quiz.active) await readQuizForVoice();
}

async function startHandsfreeQuiz() {
  openVoicePanel(true);
  state.voice.handsfree = true;
  state.voice.retry = 0;
  updateVoiceUI();
  if (!state.quiz.active || state.quiz.result) {
    state.quiz.mode = "adaptive";
    state.quiz.count = 10;
    state.quiz.scope = reviewWordsForGrade().length ? "review" : "all";
    state.quiz.active = false;
    state.quiz.result = false;
    navigate("quiz");
    await delay(120);
    startQuiz();
  }
  if (!state.quiz.active) {
    state.voice.handsfree = false;
    updateVoiceUI();
    await voiceSay("当前没有可练的单词，先换一个范围。", "zh-CN");
    return;
  }
  await voiceSay("免手模式已开启。我会读题，听到选项字母就判分；说停止可以退出。", "zh-CN", 0.98);
  await readQuizForVoice();
}

function stopHandsfree() {
  state.voice.handsfree = false;
  stopCommandRecognition();
  window.speechSynthesis?.cancel();
  updateVoiceUI();
}

async function handleVoiceCommand(rawText) {
  const text = String(rawText || "").trim();
  if (!text) return;
  openVoicePanel(true);
  $("#voiceHeard").textContent = `“${text}”`;
  const compact = text.toLowerCase().replace(/[。！？，、,.!?\s]/g, "");

  if (/^(停止|停|退出|结束|别读了|关闭语音)/.test(compact)) {
    stopHandsfree();
    await voiceSay("已停止。需要我时，按空格或点麦克风。", "zh-CN", 1);
    return;
  }
  if ($("#cameraDialog").open) {
    if (/拍|好了|capture/.test(compact)) { await captureCamera(); return; }
    if (/重拍|重新/.test(compact)) { await restartCamera(); return; }
    if (/关闭|取消|退出/.test(compact)) { closeCamera(); setVoiceReply("摄像头已关闭。"); return; }
    if (/提交|批改/.test(compact)) { closeCamera(); await submitPaperByVoice(); return; }
    await voiceSay("摄像头已打开。说拍照，或点拍照按钮。", "zh-CN", 1);
    return;
  }
  if (/免手|不用手|语音刷题/.test(compact)) { await startHandsfreeQuiz(); return; }
  if (/帮助|能做什么|怎么用/.test(compact)) {
    await voiceSay("你可以说：开始今天的训练；刷十个单词；打开纸笔听写；打开摄像头；读出选项；或者直接说 A、B、C、D。停止说停止。", "zh-CN", 0.98);
    return;
  }
  if (/为什么|依据|怎么安排/.test(compact)) {
    const stats = state.plan?.stats;
    if (stats) {
      await voiceSay(`今天有 ${stats.weak} 个薄弱词、${stats.due} 个到期词，已经开口 ${Math.round(stats.speaking_seconds / 60)} 分钟。所以先复习，再进入情景，最后用纸笔听写检验。`, "zh-CN", 0.98);
    } else {
      await voiceSay("计划会综合正确率、遗忘时间和开口时长来安排。", "zh-CN", 0.98);
    }
    return;
  }

  if (state.voice.handsfree) {
    if (/再来一轮|再练一轮|重新开始/.test(compact)) { await startHandsfreeQuiz(); return; }
  }

  const quiz = state.quiz;
  if (quiz.active && !quiz.answered) {
    const current = quiz.questions[quiz.index];
    if (/读题|再读|重复|没听清|再说一遍/.test(compact)) {
      await voiceSay(quizQuestionSpeech(current), "zh-CN", 0.95);
      return;
    }
    const choice = parseChoice(text, current);
    if (choice >= 0 && current.mode !== "write") {
      const correct = answerQuizOption(choice);
      await voiceSay(correct ? "回答正确。" : `正确答案是 ${current.word.word}。说下一题，或者点下一题。`, "zh-CN", 1);
      return;
    }
  }
  if (quiz.active && quiz.answered && /下一题|继续|下一道/.test(compact)) {
    await quizAdvance();
    if (quiz.active) await voiceSay("下一题。", "zh-CN", 1);
    return;
  }
  if (quiz.result && /继续|下一步|今日计划/.test(compact)) {
    await voiceSay("好，进入今日计划的下一步。", "zh-CN", 1);
    await continueSmartPlan(["quiz"]);
    return;
  }

  if (/开始今天|智能训练|今日计划|开始学习|教练安排/.test(compact)) {
    const focus = state.plan?.focus;
    await voiceSay(focus ? `好，按学习教练的安排，先做${focus.title}。` : "好，进入智能训练。", "zh-CN", 1);
    executePlanStep(focus);
    return;
  }

  const gradeMatch = compact.match(/高([一二三])/);
  if (gradeMatch) {
    const grade = `高${gradeMatch[1]}`;
    await loadGrade(grade);
    await voiceSay(`已切换到${grade}词库。`, "zh-CN", 1);
    return;
  }

  if (/下一个情景|换个情景|换情景/.test(compact)) {
    if (state.route !== "vocabulary") {
      navigate("vocabulary");
      await delay(220);
    }
    state.vocabulary.sceneIndex = (state.vocabulary.sceneIndex + 1) % Math.max(1, state.content.scenes.length);
    state.vocabulary.selectedWordId = null;
    renderVocabulary();
    await voiceSay(`已切换到${currentScene()?.title || "下一个情景"}。`, "zh-CN", 1);
    return;
  }

  if (/打开摄像头|拍照|拍答案|纸笔/.test(compact)) {
    await voiceSay("纸笔听写已准备好，摄像头打开后把答案纸放进取景框。", "zh-CN", 1);
    await openCameraFlow();
    return;
  }
  if (/提交批改|提交答案页|批改照片/.test(compact)) {
    await submitPaperByVoice();
    return;
  }
  if (/听写/.test(compact)) {
    state.dictation.mode = /纸笔|拍照/.test(compact) ? "paper" : "keyboard";
    state.dictation.topic = "all";
    state.dictation.queue = [];
    navigate("dictation");
    await voiceSay(state.dictation.mode === "paper" ? "纸笔听写已打开。说打开摄像头，或者点拍照。" : "听写已打开，我会播放单词。", "zh-CN", 1);
    return;
  }
  if (/刷.*单词|刷词|练.*单词|练.*词/.test(compact)) {
    const numberMatch = compact.match(/(\d+)/);
    state.quiz.count = numberMatch ? Math.min(30, Math.max(5, Number(numberMatch[1]))) : 10;
    state.quiz.mode = /默写|拼写/.test(compact) ? "write" : /中译英|中文选英/.test(compact) ? "cn-en" : /英译中|英文选中/.test(compact) ? "en-cn" : "adaptive";
    state.quiz.scope = /复习|错词/.test(compact) ? "review" : "all";
    state.quiz.active = false;
    state.quiz.result = false;
    navigate("quiz");
    await delay(100);
    startQuiz();
    await voiceSay(`好，开始 ${state.quiz.count} 道智能题。`, "zh-CN", 1);
    return;
  }
  if (/情景|背单词|词汇|看词/.test(compact)) {
    navigate("vocabulary");
    await voiceSay("进入情景词汇。点高亮词可以听音，也可以说下一个情景。", "zh-CN", 1);
    return;
  }
  if (/跟读|发音|评测/.test(compact)) {
    navigate("shadowing");
    await voiceSay("进入跟读评测。先听示范，再点开始跟读。", "zh-CN", 1);
    return;
  }
  if (/对话|口语|聊天/.test(compact)) {
    if (state.conversation.session?.status === "active") {
      navigate("conversation");
      await voiceSay("回到进行中的对话。", "zh-CN", 1);
    } else {
      state.selectedTopic = /环保|环境/.test(compact) ? "environment" : /未来|规划/.test(compact) ? "future" : "campus";
      state.selectedDuration = 5;
      startConversation();
      await voiceSay("好，开始五分钟自由对话。", "zh-CN", 1);
    }
    return;
  }
  if (/首页|今天|回去/.test(compact)) {
    navigate("home");
    await voiceSay("回到今日学习。", "zh-CN", 1);
    return;
  }
  if (/掌握|记住了/.test(compact)) {
    if (state.route === "vocabulary") await reviewSelectedWord(true);
    await voiceSay("已标记为掌握。", "zh-CN", 1);
    return;
  }
  if (/不熟|没记住/.test(compact)) {
    if (state.route === "vocabulary") await reviewSelectedWord(false);
    await voiceSay("已放回待复习。", "zh-CN", 1);
    return;
  }
  if (/播放|听一遍|听发音/.test(compact)) {
    if (state.route === "quiz" && state.quiz.active) speak(state.quiz.questions[state.quiz.index]?.word.word);
    else if (state.route === "dictation") speak(currentDictationWord()?.word);
    else if (state.route === "vocabulary") speak(selectedVocabularyWord()?.word);
    else if (state.route === "shadowing") speak(shadowText(currentShadowItem()));
    setVoiceReply("正在播放。");
    return;
  }
  if (/开始|继续|下一步/.test(compact)) {
    const focus = state.plan?.focus;
    executePlanStep(focus);
    await voiceSay(focus ? `开始${focus.title}。` : "开始练习。", "zh-CN", 1);
    return;
  }
  await voiceSay("我还没理解这句话。可以说：开始今天的训练、刷十个单词、打开摄像头，或直接说选项字母。", "zh-CN", 1);
}

async function openCameraFlow() {
  if (state.route !== "dictation" || state.dictation.mode !== "paper") {
    state.dictation.mode = "paper";
    state.dictation.topic = "all";
    state.dictation.queue = [];
    navigate("dictation");
    await delay(160);
    state.dictation.mode = "paper";
    renderDictation();
  }
  const dialog = $("#cameraDialog");
  if (!navigator.mediaDevices?.getUserMedia) {
    showAlert("当前浏览器无法使用摄像头，已打开文件选择。", true);
    $("#paperImage").click();
    return;
  }
  if (typeof dialog.showModal === "function" && !dialog.open) dialog.showModal();
  else dialog.setAttribute("open", "");
  $("#cameraCapture").disabled = true;
  $("#cameraRetake").hidden = true;
  $("#cameraStatus").textContent = "正在打开摄像头…";
  try {
    closeCameraStream();
    state.camera.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" }, width: { ideal: 1600 }, height: { ideal: 1200 } },
      audio: false,
    });
    const video = $("#cameraVideo");
    video.srcObject = state.camera.stream;
    await video.play().catch(() => {});
    $("#cameraCapture").disabled = false;
    $("#cameraStatus").textContent = "保持纸面平整、光线均匀。说“拍照”，或点下方按钮。";
  } catch (error) {
    $("#cameraStatus").textContent = "无法打开摄像头。允许权限后重试，或从相册选择。";
    setVoiceReply("摄像头没有打开，可以点拍照区域从相册选择。");
  }
}

function closeCameraStream() {
  state.camera.stream?.getTracks().forEach(track => track.stop());
  state.camera.stream = null;
  const video = $("#cameraVideo");
  if (video) video.srcObject = null;
}

function closeCamera() {
  closeCameraStream();
  const dialog = $("#cameraDialog");
  if (dialog?.open) dialog.close();
  else dialog?.removeAttribute("open");
}

async function restartCamera() {
  closeCamera();
  await delay(50);
  await openCameraFlow();
}

function cameraBlobFromVideo() {
  return new Promise((resolve, reject) => {
    const video = $("#cameraVideo");
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth || 1280;
    canvas.height = video.videoHeight || 960;
    const context = canvas.getContext("2d");
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error("没有拍到画面")), "image/jpeg", 0.9);
  });
}

async function captureCamera() {
  if (!state.camera.stream) {
    await voiceSay("摄像头还没有准备好。", "zh-CN", 1);
    return;
  }
  try {
    const blob = await cameraBlobFromVideo();
    closeCamera();
    const file = new File([blob], `paper-${Date.now()}.jpg`, { type: "image/jpeg" });
    if (state.route !== "dictation" || state.dictation.mode !== "paper") {
      state.dictation.mode = "paper";
      navigate("dictation");
      await delay(80);
    }
    state.dictation.mode = "paper";
    renderDictation();
    attachPaperImage(file);
    $("#paperPreview").scrollIntoView({ behavior: "smooth", block: "center" });
    setVoiceReply("照片拍好了。检查清晰度后说“提交批改”，或点提交按钮。");
    await voiceSay("照片拍好了。检查清晰度，然后说提交批改。", "zh-CN", 1);
  } catch (error) {
    setVoiceReply(typeof error === "undefined" ? "拍照失败" : "这次没有拍到画面，请重拍。");
  }
}

async function submitPaperByVoice() {
  if (!state.dictation.paperBlob) {
    await voiceSay("还没有答案纸照片。说“打开摄像头”先拍一张。", "zh-CN", 1);
    return;
  }
  await voiceSay("正在提交批改。", "zh-CN", 1);
  await gradePaper();
}

function quizPanelStateText() {
  if (state.voice.handsfree) return "免手练习中：系统读题，你只说选项字母。";
  if (state.route === "quiz" && state.quiz.active) return "你可以直接说 A、B、C、D 作答。";
  return "说“开始今天的训练”“免手刷题”“打开摄像头”，或描述你想练的内容。";
}

function bindEvents() {
  document.addEventListener("click", event => {
    const routeLink = event.target.closest("[data-route]");
    if (routeLink) { event.preventDefault(); navigate(routeLink.dataset.route); return; }
    const planStep = event.target.closest("[data-plan-step]");
    if (planStep) { executePlanStep(state.plan?.steps?.[Number(planStep.dataset.planStep)]); return; }
    const audioButton = event.target.closest("[data-audio]");
    if (audioButton) new Audio(audioButton.dataset.audio).play().catch(() => showAlert("录音暂时无法播放，请稍后重试。", true));
    const speakButton = event.target.closest("[data-speak]");
    if (speakButton) speak(speakButton.dataset.speak);
  });
  window.addEventListener("hashchange", renderRoute);
  $("#homeSmartStart")?.addEventListener("click", () => {
    if (state.plan?.focus) executePlanStep(state.plan.focus);
    else navigate("quiz");
  });
  $("#homeDictation")?.addEventListener("click", () => { state.dictation.topic = "all"; buildDictationQueue(state.progress.review_ids[0]); navigate("dictation"); });
  // 对话页的所有监听都在 conversation.js 里绑定（流式协议与旧版不同）。
  $("#reportToShadowing")?.addEventListener("click", () => {
    const topic = state.conversation.session?.topic;
    // 自定义情景没有对应词表，回退到全部主题。
    openShadowing("sentence", topicNames[topic] ? topic : "all");
  });
  $("#reportSmartNext")?.addEventListener("click", () => continueSmartPlan(["conversation"]));

  $("#shadowType")?.addEventListener("click", event => {
    const button = event.target.closest("[data-type]");
    if (!button || isRecording()) return;
    state.shadow.type = button.dataset.type;
    updateShadowItems();
    renderShadowing();
  });
  $("#shadowTopic")?.addEventListener("change", event => { state.shadow.topic = event.target.value; updateShadowItems(); renderShadowing(); });
  $("#shadowItemList")?.addEventListener("click", event => {
    const button = event.target.closest("[data-shadow-index]");
    if (!button || isRecording()) return;
    state.shadow.index = Number(button.dataset.shadowIndex);
    clearShadowAttempt();
    renderShadowing();
  });
  $("#playShadowModel")?.addEventListener("click", () => { const text = shadowText(currentShadowItem()); if (text) { setPracticeStep(1); speak(text); $("#shadowStatus").textContent = "正在播放示范，听完后开始跟读。"; } });
  $("#shadowRecord")?.addEventListener("click", toggleShadowRecording);
  $("#submitPronunciation")?.addEventListener("click", submitPronunciation);
  $("#nextShadowItem")?.addEventListener("click", () => { if (isRecording() || !state.shadow.items.length) return; state.shadow.index = (state.shadow.index + 1) % state.shadow.items.length; clearShadowAttempt(); renderShadowing(); });

  $("#dictationTopic")?.addEventListener("change", event => {
    state.dictation.topic = event.target.value;
    state.dictation.queue = [];
    buildDictationQueue();
    renderDictation();
  });
  $("#dictationAuto")?.addEventListener("change", event => {
    state.dictation.autoNext = event.target.checked;
    if (!state.dictation.autoNext) clearTimeout(state.dictation.nextTimer);
  });
  $("#quizAutoNext")?.addEventListener("change", event => {
    state.quiz.autoNext = event.target.checked;
    if (!state.quiz.autoNext) clearTimeout(state.quiz.nextTimer);
  });
  $("#dictationMode")?.addEventListener("click", event => {
    const button = event.target.closest("[data-mode]");
    if (!button) return;
    state.dictation.mode = button.dataset.mode;
    renderDictation();
  });
  $("#playDictationWord")?.addEventListener("click", () => { const word = currentDictationWord(); if (word) speak(word.word); });
  $("#dictationForm")?.addEventListener("submit", checkDictation);
  $("#paperWordRows")?.addEventListener("click", event => {
    const button = event.target.closest("[data-paper-word]");
    if (!button) return;
    const word = paperWords().find(item => item.id === button.dataset.paperWord);
    if (word) speak(word.word);
  });
  $("#paperImage")?.addEventListener("change", event => attachPaperImage(event.target.files?.[0]));
  $("#removePaperImage")?.addEventListener("click", () => {
    state.dictation.paperBlob = null;
    if (state.dictation.paperUrl) URL.revokeObjectURL(state.dictation.paperUrl);
    state.dictation.paperUrl = null;
    $("#paperImage").value = "";
    $("#paperPreview").hidden = true;
    $("#paperDrop").hidden = false;
    $("#gradePaper").disabled = true;
    $("#paperStatus").textContent = "已移除照片，可以重新拍摄。";
  });
  $("#gradePaper")?.addEventListener("click", gradePaper);
  $("#probeGrader")?.addEventListener("click", () => probeGraderService(true));
  $("#newPaperBatch")?.addEventListener("click", () => {
    state.dictation.batch += 1;
    buildPaperBatch();
    renderPaperDictation();
  });

  $("#scenePicker")?.addEventListener("click", event => {
    const button = event.target.closest("[data-scene-index]");
    if (!button) return;
    state.vocabulary.sceneIndex = Number(button.dataset.sceneIndex);
    state.vocabulary.selectedWordId = null;
    renderVocabulary();
  });
  $("#sceneBody")?.addEventListener("click", event => {
    const button = event.target.closest("[data-scene-word]");
    if (!button) return;
    const scene = currentScene();
    const found = scene?.words.find(word => word.word.toLowerCase() === button.dataset.sceneWord.toLowerCase());
    if (found) { state.vocabulary.selectedWordId = found.id; renderVocabulary(); }
  });
  $("#sceneWordList")?.addEventListener("click", event => {
    const button = event.target.closest("[data-vocab-word]");
    if (!button) return;
    state.vocabulary.selectedWordId = button.dataset.vocabWord;
    renderVocabulary();
  });
  $("#playScene")?.addEventListener("click", () => {
    const scene = currentScene();
    if (scene) speak(scene.scene.replace(/\{\{|\}\}/g, ""));
  });
  $("#sceneToQuiz")?.addEventListener("click", () => {
    state.quiz.scope = "scene";
    state.quiz.active = false;
    state.quiz.result = false;
    navigate("quiz");
  });
  $("#sceneToDictation")?.addEventListener("click", () => {
    state.dictation.topic = "scene";
    state.dictation.queue = [];
    buildDictationQueue();
    navigate("dictation");
  });
  $("#wordUnfamiliar")?.addEventListener("click", () => reviewSelectedWord(false));
  $("#wordKnown")?.addEventListener("click", () => reviewSelectedWord(true));
  $("#wordToDictation")?.addEventListener("click", () => {
    const word = selectedVocabularyWord();
    if (!word) return;
    state.dictation.topic = "all";
    state.dictation.queue = shuffled([word, ...gradeWords().filter(item => item.id !== word.id)]);
    state.dictation.index = 0;
    state.dictation.answered = false;
    navigate("dictation");
  });
  $("#wordToShadow")?.addEventListener("click", () => {
    const word = selectedVocabularyWord();
    if (word) openShadowing("word", "all", word.id);
  });
  $("#wordbook")?.addEventListener("toggle", event => { state.vocabulary.wordbookOpen = event.target.open; });
  $("#wordSearch")?.addEventListener("input", event => {
    state.vocabulary.wordbookFilter = event.target.value;
    renderWordbook();
  });
  $("#wordbookList")?.addEventListener("click", event => {
    const button = event.target.closest("[data-wordbook-word]");
    if (!button) return;
    state.vocabulary.selectedWordId = button.dataset.wordbookWord;
    state.vocabulary.wordbookOpen = true;
    renderVocabulary();
    $("#vocabInspector").scrollIntoView({ behavior: "smooth", block: "center" });
  });

  $$("[data-quiz-mode]").forEach(button => button.addEventListener("click", () => {
    state.quiz.mode = button.dataset.quizMode;
    state.quiz.active = false;
    state.quiz.result = false;
    renderQuiz();
  }));
  $("#quizCountOptions")?.addEventListener("click", event => {
    const button = event.target.closest("[data-count]");
    if (!button) return;
    state.quiz.count = Number(button.dataset.count);
    $$("#quizCountOptions button").forEach(item => { const selected = item === button; item.classList.toggle("is-selected", selected); item.setAttribute("aria-checked", String(selected)); });
    $("#quizGradeHint").textContent = `${state.grade} · ${state.quiz.count} 题`;
  });
  $("#quizScopeOptions")?.addEventListener("click", event => {
    const button = event.target.closest("[data-scope]");
    if (!button) return;
    state.quiz.scope = button.dataset.scope;
    state.quiz.active = false;
    state.quiz.result = false;
    renderQuiz();
  });
  $("#startQuiz")?.addEventListener("click", () => startQuiz());
  $("#quizOptions")?.addEventListener("click", event => {
    const button = event.target.closest("[data-quiz-option]");
    if (button) answerQuizOption(Number(button.dataset.quizOption));
  });
  $("#quizAudioRow")?.addEventListener("click", event => {
    if (event.target.closest("[data-quiz-speak]")) {
      const word = state.quiz.questions[state.quiz.index]?.word;
      if (word) speak(word.word);
    }
  });
  $("#quizWriteForm")?.addEventListener("submit", submitQuizWrite);
  $("#quizNext")?.addEventListener("click", quizAdvance);
  $("#quitQuiz")?.addEventListener("click", () => { state.quiz.active = false; state.quiz.result = false; renderQuiz(); });
  $("#restartQuiz")?.addEventListener("click", () => { state.quiz.result = false; state.quiz.active = false; state.quiz.wrong = []; renderQuiz(); });
  $("#quizSmartNext")?.addEventListener("click", () => continueSmartPlan(["quiz"]));
  $("#retryWrongWords")?.addEventListener("click", () => {
    const wrong = state.quiz.wrong.filter((word, index, list) => list.findIndex(item => item.id === word.id) === index);
    if (!wrong.length) { showAlert("这一轮没有错词，可以直接开始新一组。"); return; }
    startQuiz(wrong);
  });
  $("#quizToDictation")?.addEventListener("click", () => {
    const words = state.quiz.wrong.filter((word, index, list) => list.findIndex(item => item.id === word.id) === index);
    if (!words.length) return;
    state.dictation.topic = "all";
    state.dictation.queue = shuffled(words);
    state.dictation.index = 0;
    navigate("dictation");
  });
  $("#voiceTrigger")?.addEventListener("click", () => {
    openVoicePanel(true);
    if (state.voice.handsfree) stopHandsfree();
    else if (state.voice.listening) stopCommandRecognition();
    else startCommandListening();
  });
  $("#voicePanelClose")?.addEventListener("click", () => {
    if (state.voice.handsfree) stopHandsfree();
    else stopCommandRecognition();
    openVoicePanel(false);
  });
  $("#voiceSuggestions")?.addEventListener("click", event => {
    const button = event.target.closest("[data-voice-command]");
    if (button) handleVoiceCommand(button.dataset.voiceCommand);
  });
  $("#voiceTextForm")?.addEventListener("submit", event => {
    event.preventDefault();
    const text = $("#voiceTextInput").value.trim();
    if (!text) return;
    $("#voiceTextInput").value = "";
    handleVoiceCommand(text);
  });
  $("#openPaperCamera")?.addEventListener("click", openCameraFlow);
  $("#cameraCapture")?.addEventListener("click", captureCamera);
  $("#cameraClose")?.addEventListener("click", closeCamera);
  $("#cameraCancel")?.addEventListener("click", closeCamera);
  $("#cameraRetake")?.addEventListener("click", restartCamera);
  $("#cameraDialog")?.addEventListener("cancel", () => closeCameraStream());

  document.addEventListener("keydown", event => {
    const target = event.target;
    const typing = target instanceof HTMLElement && (
      ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName) || target.isContentEditable
    );
    if (event.code === "Escape") {
      if (state.voice.handsfree) stopHandsfree();
      if ($("#cameraDialog").open) closeCamera();
      return;
    }
    if (typing) {
      if (event.key === "Enter" && state.route === "dictation" && state.dictation.mode === "keyboard" && state.dictation.answered) {
        event.preventDefault();
        nextDictation();
      }
      return;
    }
    if (event.code === "Space" && !event.repeat) {
      if (target instanceof HTMLElement && target.closest("button, a, [role='button']")) return;
      event.preventDefault();
      openVoicePanel(true);
      if (state.voice.handsfree) stopHandsfree();
      else if (state.voice.listening) stopCommandRecognition();
      else startCommandListening();
      return;
    }
    if (state.route === "quiz" && state.quiz.active) {
      const current = state.quiz.questions[state.quiz.index];
      if (!state.quiz.answered && current && current.mode !== "write") {
        const key = event.key.toLowerCase();
        const index = "1234".includes(key) ? Number(key) - 1 : "abcd".indexOf(key);
        if (index >= 0 && index < 4) {
          event.preventDefault();
          answerQuizOption(index);
          return;
        }
      }
      if (state.quiz.answered && (event.key === "Enter" || event.key === "ArrowRight")) {
        event.preventDefault();
        quizAdvance();
        return;
      }
    }
    if (state.route === "dictation" && state.dictation.mode === "keyboard" && state.dictation.answered && event.key === "Enter") {
      event.preventDefault();
      nextDictation();
    }
  });
  $("#gradeSelect")?.addEventListener("change", event => loadGrade(event.target.value));
  $("#llmToggle")?.addEventListener("click", toggleLLM);
  window.addEventListener("focus", syncLLM);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) syncLLM();
  });
}

async function syncLLM() {
  try {
    const data = await api("/api/llm");
    if (!data || data.provider === state.llm?.provider) return;
    state.llm = data;
    renderLLMToggle();
  } catch (error) {
    /* 网络抖动时不打扰用户，下一次轮询会重试 */
  }
}

function renderLLMToggle() {
  const button = $("#llmToggle");
  if (!button) return;
  const provider = state.llm?.provider || "deepseek";
  button.dataset.provider = provider;
  button.title = provider === "qwen"
    ? "当前：本地 Qwen，点击切回 DeepSeek"
    : "当前：DeepSeek API，点击切换到本地 Qwen";
  const label = $("#llmToggleLabel");
  if (label) label.textContent = provider === "qwen" ? "Qwen 本地" : "DeepSeek";
}

async function toggleLLM() {
  const target = state.llm?.provider === "qwen" ? "deepseek" : "qwen";
  const button = $("#llmToggle");
  if (button) button.disabled = true;
  try {
    const data = await api("/api/llm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: target }),
    });
    state.llm = data;
    renderLLMToggle();
    showAlert(`外教模型已切换到 ${data.label}${data.available ? "" : "（当前不可用，会使用备用回复）"}`);
  } catch (error) {
    showAlert(`切换模型失败：${error.message}`, true);
  } finally {
    if (button) button.disabled = false;
  }
}

async function loadGrade(grade) {
  if (!["高一", "高二", "高三"].includes(grade) || grade === state.grade) return;
  const select = $("#gradeSelect");
  select.disabled = true;
  try {
    const [content, plan] = await Promise.all([
      api(`/api/content?topic=all&grade=${encodeURIComponent(grade)}`),
      api(`/api/plan?grade=${encodeURIComponent(grade)}`),
    ]);
    state.grade = grade;
    state.content = content;
    state.plan = plan;
    state.vocabularyMeta = content.vocabulary_meta || {};
    localStorage.setItem("echo.grade", grade);
    state.vocabulary.sceneIndex = 0;
    state.vocabulary.selectedWordId = null;
    state.vocabulary.wordbookFilter = "";
    state.dictation.queue = [];
    state.dictation.index = 0;
    state.dictation.paperIds = [];
    state.dictation.batch = 1;
    if (state.dictation.paperUrl) URL.revokeObjectURL(state.dictation.paperUrl);
    state.dictation.paperUrl = null;
    state.dictation.paperBlob = null;
    state.quiz.active = false;
    state.quiz.result = false;
    state.quiz.questions = [];
    state.quiz.wrong = [];
    state.quiz.correct = 0;
    renderRoute();
    showAlert(`已切换到${grade}词库，共 ${content.vocabulary.length} 个可练词。`);
  } catch (error) {
    showAlert(error.message, true);
  } finally {
    select.disabled = false;
    select.value = state.grade;
  }
}

async function init() {
  const date = new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "short" }).format(new Date());
  if ($("#todayLabel")) $("#todayLabel").textContent = date;
  if ($("#dateStamp")) $("#dateStamp").textContent = date;
  $("#gradeSelect").value = state.grade;
  bindEvents();
  try {
    const [content, progress, health, plan] = await Promise.all([
      api(`/api/content?topic=all&grade=${encodeURIComponent(state.grade)}`),
      api("/api/progress"),
      api("/api/health"),
      api(`/api/plan?grade=${encodeURIComponent(state.grade)}`),
    ]);
    state.content = content;
    state.plan = plan;
    state.vocabularyMeta = content.vocabulary_meta || {};
    state.progress = progress;
    state.provider = health.provider;
    state.llm = health.llm || state.llm;
    state.speechEval = health.speech_eval || { ready: false };
    state.photoGrading = health.photo_grading || { ready: false };
    renderLLMToggle();
    await restoreSession();
    try { buildDictationQueue(); } catch { /* 听写已砍，队列不再需要 */ }
    updateVoiceUI();
    if ($("#voiceHeard")) $("#voiceHeard").textContent = quizPanelStateText();
    renderRoute();
  } catch (error) {
    showAlert(`无法连接学习服务：${error.message}。请先启动 Python 后端。`, true);
    renderRoute();  // 接口不通也要把界面渲染出来
  }
}

init();
