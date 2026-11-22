"use strict";

/* ── 流式口语对话 ─────────────────────────────────────────────
   一条 WebSocket 承载整场对话：
     上行  16k PCM 音频帧（只在轮到你说话时发送）
     下行  JSON 事件 + 24k PCM 音频帧（豆包合成）
   半双工：外教思考或说话期间麦克风静音，播完最后一个字才收音；
   判停由服务端做（静音 2 秒 + 等识别收尾）。前端负责采集、播放、字幕和状态显示。
   ─────────────────────────────────────────────────────────── */

const CONV_RATE = 24000;      // 豆包合成采样率
const MIC_RATE = 16000;       // 豆包识别采样率
const SEND_SAMPLES = 800;     // 每 50ms 发一包，别让 WS 被碎片淹没
const PLAYBACK_TIMEOUT = 20000; // 播放结束未上报时的兜底

class PCMPlayer {
  constructor(sampleRate = CONV_RATE) {
    this.sampleRate = sampleRate;
    this.context = null;
    this.nextTime = 0;
    this.sources = new Set();
    this.playing = false;
    this.onDrained = null;
  }

  async unlock() {
    if (!this.context) {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return false;
      this.context = new Ctx();
    }
    if (this.context.state === "suspended") await this.context.resume();
    return this.context.state === "running";
  }

  push(arrayBuffer) {
    if (!this.context) return;
    const samples = new Int16Array(arrayBuffer);
    if (!samples.length) return;
    const buffer = this.context.createBuffer(1, samples.length, this.sampleRate);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < samples.length; index += 1) channel[index] = samples[index] / 32768;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const startAt = Math.max(this.context.currentTime + 0.03, this.nextTime);
    source.__startAt = startAt;
    source.start(startAt);
    this.nextTime = startAt + buffer.duration;
    this.playing = true;
    this.sources.add(source);
    source.onended = () => {
      this.sources.delete(source);
      this.playing = this.sources.size > 0;
      // 最后一个音频块播完 —— 这就是"外教说完"的时刻。
      if (!this.playing) this.onDrained?.();
    };
  }

  /** Hard stop: used when the lesson ends or the socket drops. */
  flush() {
    this.sources.forEach(source => { try { source.stop(); } catch { /* already stopped */ } });
    this.sources.clear();
    this.playing = false;
    if (this.context) this.nextTime = this.context.currentTime;
  }

  async close() {
    this.flush();
    const context = this.context;
    this.context = null;
    if (context && context.state !== "closed") await context.close().catch(() => {});
  }
}

class MicCapture {
  constructor() {
    this.context = null;
    this.stream = null;
    this.node = null;
    this.gain = null;
    this.source = null;
    this.pending = [];
    this.pendingSamples = 0;
    this.active = false;
    this.sending = false;   // 半双工：轮到学生说话时才为 true
    this.onChunk = null;
  }

  async start(onChunk) {
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioContext || !window.AudioWorkletNode) {
      throw new Error("当前浏览器不支持实时录音，请使用新版 Edge、Chrome 或 Safari。");
    }
    this.onChunk = onChunk;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    this.context = new AudioContext({ latencyHint: "interactive" });
    await this.context.audioWorklet.addModule("/audio-worklet.js");
    this.source = this.context.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.context, "pcm-processor");
    this.gain = this.context.createGain();
    this.gain.gain.value = 0;
    this.node.port.onmessage = event => {
      if (!this.sending) return;   // 外教在说话，这一帧直接丢
      const samples = new Int16Array(event.data);
      if (!samples.length) return;
      this.pending.push(samples);
      this.pendingSamples += samples.length;
      if (this.pendingSamples < SEND_SAMPLES) return;
      const merged = new Int16Array(this.pendingSamples);
      let offset = 0;
      this.pending.forEach(part => { merged.set(part, offset); offset += part.length; });
      this.pending = [];
      this.pendingSamples = 0;
      onChunk(merged.buffer);
    };
    this.source.connect(this.node);
    this.node.connect(this.gain);
    this.gain.connect(this.context.destination);
    await this.context.resume();
    this.active = true;
  }

  /** 开/关上行音频。关闭时连缓冲一起丢掉，避免外教声音被当成学生说话。 */
  setSending(on) {
    const next = Boolean(on);
    if (next === this.sending) return;
    this.sending = next;
    this.pending = [];
    this.pendingSamples = 0;
  }

  stop() {
    this.active = false;
    this.sending = false;
    this.stream?.getTracks().forEach(track => track.stop());
    this.source?.disconnect();
    this.node?.disconnect();
    this.gain?.disconnect();
    const context = this.context;
    this.context = null;
    if (context && context.state !== "closed") context.close().catch(() => {});
    this.pending = [];
    this.pendingSamples = 0;
  }
}

function chatScrollToBottom() {
  const box = $("#chatMessages");
  if (box) box.scrollTop = box.scrollHeight;
}

function makeBubble(who, text = "", extraClass = "") {
  const wrap = document.createElement("div");
  wrap.className = `chat-message ${who === "student" ? "is-student" : ""} ${extraClass}`.trim();
  const avatar = who === "student" ? "" : '<span class="chat-avatar">AI</span>';
  wrap.innerHTML = `${avatar}<div class="message-body"><div class="message-bubble"></div></div>`;
  wrap.querySelector(".message-bubble").textContent = text;
  $("#chatMessages").appendChild(wrap);
  chatScrollToBottom();
  return wrap;
}

function setBubbleText(wrap, text) {
  const bubble = wrap?.querySelector(".message-bubble");
  if (!bubble) return;
  if (bubble.textContent !== text) bubble.textContent = text;
  chatScrollToBottom();
}

class ConversationLive {
  constructor() {
    this.ws = null;
    this.player = new PCMPlayer(CONV_RATE);
    this.mic = new MicCapture();
    this.active = false;
    this.listening = false;
    this.asrAvailable = true;
    this.aiBubble = null;
    this.openingBubble = null;
    this.studentBubble = null;
    this.aiText = "";
    this.pendingReport = null;
    this.finished = false;
    this.fallbackQueue = [];
    this.fallbackPlaying = false;
    this.playbackPending = false;
    this.playbackTimer = null;
    this.realtimeScore = false;
    this.scoreReady = true;
    this.scoreHistory = [];
    this.pendingScores = new Set();
    this.translate = false;
    this.lastReplyBubble = null;
    this.grammarTurn = 0;
  }

  async open() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${location.host}/api/ws/conversation`);
    this.ws = socket;
    socket.binaryType = "arraybuffer";
    socket.onmessage = event => {
      if (typeof event.data === "string") {
        try { this.handle(JSON.parse(event.data)); } catch (error) { console.warn("bad event", error); }
      } else {
        this.player.push(event.data);
      }
    };
    socket.onclose = () => { this.listening = false; };
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("对话连接超时，请确认后端在运行")), 8000);
      socket.onopen = () => { clearTimeout(timer); resolve(); };
      socket.onerror = () => { clearTimeout(timer); reject(new Error("无法连接对话服务")); };
    });
  }

  async start({ scene, grade, minutes, realtimeScore, translate }) {
    this.active = true;
    this.finished = false;
    this.realtimeScore = Boolean(realtimeScore);
    this.translate = Boolean(translate);
    this.resetGrammar();
    this.scoreReady = true;
    this.scoreHistory = [];
    this.pendingScores.clear();
    $("#chatMessages").innerHTML = "";
    const liveToggle = $("#liveRealtimeScore");
    if (liveToggle) liveToggle.checked = this.realtimeScore;
    this.renderScores();
    setLiveState("准备中");

    await this.player.unlock();
    await this.open();

    let micError = "";
    try {
      // 麦克风先打开但不发音频：要等服务端说"轮到你"（state:listening）。
      await this.mic.start(buffer => {
        if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(buffer);
      });
      this.mic.setSending(false);
      this.listening = true;
    } catch (error) {
      micError = error.message;
      this.listening = false;
    }

    this.ws.send(JSON.stringify({
      type: "start",
      scene,
      grade,
      minutes,
      realtime_score: realtimeScore,
      translate: this.translate,
    }));
    if (micError) showAlert(`麦克风不可用：${micError}。可以先打字，再点“我说完了”。`, true);
  }

  sendText(text) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: "turn_text", text }));
  }

  handle(message) {
    switch (message.type) {
      case "ready":
        this.asrAvailable = message.asr !== "browser";
        if (!this.asrAvailable) $("#liveTranscript").placeholder = "语音识别未接入，请直接打字";
        break;

      case "scene": {
        const knowledge = message.knowledge || [];
        const session = {
          id: message.session_id,
          status: "active",
          topic: message.topic,
          topic_title: message.title,
          grade: message.grade,
          knowledge,
          scene: { title_zh: message.title, role: message.role, role_zh: message.role_zh, goal: message.goal, setting: message.setting, knowledge },
          duration_seconds: message.duration_seconds,
          turns: [],
          opening: message.opening,
        };
        state.conversation.session = session;
        state.conversation.startedAt = Date.now();
        clearInterval(state.conversation.timer);
        state.conversation.timer = setInterval(updateTimer, 1000);
        updateConversationMeta();
        updateTimer();
        renderConversation();
        this.openingBubble = makeBubble("ai", message.opening, "is-opening");
        this.aiBubble = this.openingBubble;
        this.scoreReady = message.score_ready !== false;
        if (this.realtimeScore && !this.scoreReady) {
          $("#liveRealtimeScore").checked = false;
          this.realtimeScore = false;
          state.conversation.realtimeScore = false;
          this.renderScores();
          showAlert("当前没有配置发音评测服务，实时评估已自动关闭；对话和结课报告不受影响。");
        }
        break;
      }

      case "state":
        if (message.value === "transcribing") {
          // 服务端在等识别器把最后几个字吐完，先别收音频，也别让学生以为卡住。
          this.mic.setSending(false);
          setLiveState("整理中");
          setRecordButton(false, "整理中");
        } else if (message.value === "thinking" || message.value === "speaking") {
          this.mic.setSending(false);
          setLiveState(message.value === "thinking" ? "思考中" : "说话中");
          setRecordButton(false, message.value === "thinking" ? "外教思考中" : "外教说话中");
        } else if (message.value === "listening") {
          this.mic.setSending(true);
          setLiveState("聆听中");
          setRecordButton(true, "正在聆听");
        } else if (message.value === "reporting") {
          this.mic.setSending(false);
          setLiveState("生成报告");
          setRecordButton(false, "正在评估");
        } else if (message.value === "preparing") {
          this.mic.setSending(false);
          setLiveState("准备中");
        }
        break;

      case "asr": {
        this.lastAsr = message.text || "";
        $("#liveTranscript").value = message.text || "";
        if (!this.studentBubble) this.studentBubble = makeBubble("student", "");
        setBubbleText(this.studentBubble, message.text || "");
        break;
      }

      case "student": {
        if (!this.studentBubble) this.studentBubble = makeBubble("student", "");
        setBubbleText(this.studentBubble, message.text);
        $("#liveTranscript").value = "";
        break;
      }

      case "reply_delta": {
        // 开场白气泡只属于开场白：即使 opening_done 晚到或丢失，回复也要另起一条，
        // 否则第一轮回答会把学生刚听到的开场白覆盖掉。
        if (!this.aiBubble || this.aiBubble === this.openingBubble) {
          this.aiBubble = makeBubble("ai", "");
        }
        this.aiText += message.text;
        setBubbleText(this.aiBubble, this.aiText);
        this.aiBubble.classList.add("is-streaming");
        break;
      }

      case "tts_start":
        setLiveState("说话中");
        break;

      case "speak_fallback":
        // 豆包合成不可用时退回浏览器朗读；串行播完再交还频道。
        this.enqueueFallback(message.text);
        break;

      case "tts_end":
        break;

      case "opening_done":
        // 开场气泡已定型：清掉引用，否则第一轮 reply_delta 会接着它往下写，
        // 把开场白覆盖成回复。
        this.aiBubble = null;
        this.waitPlaybackEnd();
        break;

      case "reply_done": {
        if (this.aiBubble) {
          this.aiBubble.classList.remove("is-streaming");
          setBubbleText(this.aiBubble, message.text || this.aiText);
          this.lastReplyBubble = this.aiBubble;
        }
        this.aiBubble = null;
        this.aiText = "";
        // 所有音频帧已经到齐，等播放队列真正空了再告诉服务端"外教说完了"。
        this.waitPlaybackEnd();
        break;
      }

      case "turn": {
        const session = state.conversation.session;
        if (session && message.turn) session.turns.push(message.turn);
        this.studentBubble = null;
        updateConversationMeta();
        break;
      }

      case "score_pending":
        if (!this.realtimeScore) break;
        this.pendingScores.add(message.turn);
        this.renderScores();
        break;

      case "reply_translation":
        this.attachTranslation(message.zh);
        break;

      case "grammar_pending":
        this.renderGrammar({ pending: true, turn: message.turn });
        break;

      case "grammar":
        this.renderGrammar(message);
        break;

      case "translate_config":
        this.translate = Boolean(message.enabled);
        syncTranslateUI(this.translate);
        break;

      case "score":
        this.pendingScores.delete(message.turn);
        if (message.error || message.unavailable) {
          this.scoreHistory.unshift({ turn: message.turn, note: message.error || message.unavailable });
        } else {
          this.scoreHistory.unshift({
            turn: message.turn,
            assessment: message.assessment || {},
            coach: Boolean(message.coach),
            feedback: null,
          });
        }
        this.scoreHistory = this.scoreHistory.slice(0, 4);
        this.renderScores();
        break;

      case "score_feedback": {
        const item = this.scoreHistory.find(entry => entry.turn === message.turn);
        if (!item) break;
        item.feedback = message;
        this.renderScores();
        break;
      }

      case "score_config":
        if (message.enabled && message.ready === false) showAlert("发音评测服务未配置，本轮不会出分。", true);
        break;

      case "report":
        this.finished = true;
        if (state.conversation.session) {
          state.conversation.session.status = "completed";
          state.conversation.session.report = message.report;
          state.conversation.session.turns = message.session?.turns || state.conversation.session.turns;
        }
        clearInterval(state.conversation.timer);
        this.listening = false;
        renderConversation();
        this.close(true);
        if (this.pendingReport) this.pendingReport.resolve(message.report);
        break;

      case "asr_unavailable":
        this.asrAvailable = false;
        $("#liveTranscript").placeholder = "语音识别未接入，请直接打字";
        break;

      case "error":
        if (message.code === "no_speech") {
          showAlert(message.text);
        } else {
          showAlert(message.text, true);
        }
        break;

      default:
        break;
    }
  }

  /** 浏览器朗读兜底：串行播放，避免每句都 cancel 前一句。 */
  enqueueFallback(text) {
    const line = (text || "").trim();
    if (!line) return;
    this.fallbackQueue.push(line);
    this.pumpFallback();
  }

  pumpFallback() {
    if (this.fallbackPlaying) return;
    const text = this.fallbackQueue.shift();
    if (!text) {
      this.finishPlaybackIfReady();
      return;
    }
    this.fallbackPlaying = true;
    let settled = false;
    const next = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      this.fallbackPlaying = false;
      this.pumpFallback();
    };
    // 某些浏览器的 speechSynthesis 不发 onend，按字数兜一个上限。
    const timer = setTimeout(next, 4000 + text.split(/\s+/).length * 450);
    speak(text, next);
  }

  /** 服务端靠这个事件把麦克风还给学生；播放队列空了才算外教说完。 */
  waitPlaybackEnd() {
    this.playbackPending = true;
    clearTimeout(this.playbackTimer);
    this.playbackTimer = setTimeout(() => this.reportPlaybackDone(), PLAYBACK_TIMEOUT);
    this.player.onDrained = () => this.finishPlaybackIfReady();
    setTimeout(() => this.finishPlaybackIfReady(), 0);
  }

  finishPlaybackIfReady() {
    if (!this.playbackPending) return;
    if (this.player.playing || this.fallbackPlaying || this.fallbackQueue.length) return;
    this.reportPlaybackDone();
  }

  reportPlaybackDone() {
    if (!this.playbackPending) return;
    this.playbackPending = false;
    clearTimeout(this.playbackTimer);
    this.playbackTimer = null;
    this.player.onDrained = null;
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "playback_done" }));
  }

  /** 开关实时评估：中途也能改，服务端下一轮起生效。 */
  setRealtimeScore(on, { notify = true } = {}) {
    const enabled = Boolean(on);
    this.realtimeScore = enabled;
    state.conversation.realtimeScore = enabled;
    const setup = $("#realtimeScore");
    if (setup) setup.checked = enabled;
    const live = $("#liveRealtimeScore");
    if (live) live.checked = enabled;
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "score_toggle", enabled }));
    }
    if (!enabled) this.pendingScores.clear();
    this.renderScores();
    if (notify) {
      showAlert(enabled
        ? "已开启实时评估：每轮说完几秒后出分，不影响外教回复的速度。"
        : "已关闭实时评估；录音和结课报告照常生成。");
    }
  }

  /** 右侧评估面板：最新一轮给完整细节，之前的只留一行分数。 */
  renderScores() {
    const box = $("#liveScores");
    if (!box) return;
    const parts = [];
    this.pendingScores.forEach(turn => {
      parts.push(`<div class="score-pending"><span>第 ${turn} 轮</span><em>评估中…</em></div>`);
    });
    this.scoreHistory.forEach((item, index) => {
      if (index === 0 && !this.pendingScores.size && item.assessment) parts.push(this.scoreCard(item));
      else parts.push(this.scoreRow(item));
    });
    if (!parts.length) parts.push('<p class="score-empty">—</p>');
    box.innerHTML = parts.join("");
  }

  /** 译文挂在外教那条气泡里，跟原文一起读。 */
  attachTranslation(zh) {
    if (!zh || !this.lastReplyBubble) return;
    const bubble = this.lastReplyBubble.querySelector(".message-bubble");
    if (!bubble || bubble.querySelector(".bubble-translation")) return;
    const line = document.createElement("p");
    line.className = "bubble-translation";
    line.textContent = zh;
    bubble.appendChild(line);
    chatScrollToBottom();
  }

  setTranslate(enabled) {
    this.translate = Boolean(enabled);
    state.conversation.translate = this.translate;
    syncTranslateUI(this.translate);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "translate_toggle", enabled: this.translate }));
    }
  }

  resetGrammar() {
    this.grammarTurn = 0;
    const card = $("#grammarCard");
    if (!card) return;
    card.classList.remove("is-good", "is-weak");
    $("#grammarScore").textContent = "—";
    $("#grammarState").textContent = "等你说下一句";
    $("#grammarIssues").innerHTML = "";
    $("#grammarRewrite").hidden = true;
    $("#grammarRewrite").textContent = "";
  }

  /** 左侧栏的语法点评：只留一句分数 + 最多两条问题，不堆字。 */
  renderGrammar(message) {
    const card = $("#grammarCard");
    if (!card) return;
    card.hidden = false;

    if (message.pending) {
      card.classList.remove("is-good", "is-weak");
      $("#grammarScore").textContent = "…";
      $("#grammarState").textContent = "分析中";
      $("#grammarIssues").innerHTML = "";
      $("#grammarRewrite").hidden = true;
      return;
    }
    if (message.unavailable) {
      card.classList.remove("is-good", "is-weak");
      $("#grammarScore").textContent = "—";
      $("#grammarState").textContent = "这句没拿到点评";
      $("#grammarIssues").innerHTML = "";
      $("#grammarRewrite").hidden = true;
      return;
    }

    this.grammarTurn = message.turn;
    const score = Number(message.turn_score);
    const issues = message.issues || [];
    $("#grammarScore").textContent = Number.isFinite(score) ? score : "—";
    card.classList.toggle("is-good", Number.isFinite(score) && score >= 90);
    card.classList.toggle("is-weak", Number.isFinite(score) && score < 70);
    $("#grammarState").textContent = issues.length
      ? (message.summary || `第 ${message.turn} 句`)
      : "这句没问题";
    $("#grammarIssues").innerHTML = issues.map(item => `
      <div class="grammar-issue">
        <span class="grammar-tag">${escapeHTML(item.label || "问题")}</span>
        <p>${item.snippet ? `<s>${escapeHTML(item.snippet)}</s>` : ""}${item.snippet && item.fix ? " → " : ""}${item.fix ? `<b>${escapeHTML(item.fix)}</b>` : ""}</p>
        ${item.note ? `<small>${escapeHTML(item.note)}</small>` : ""}
      </div>`).join("");
    const rewrite = (message.rewrite || "").trim();
    $("#grammarRewrite").hidden = !rewrite || (Number.isFinite(score) && score >= 90);
    $("#grammarRewrite").textContent = rewrite ? `改写：${rewrite}` : "";
  }

  scoreRow({ turn, assessment, note }) {
    if (note) return `<div class="score-note"><span>第 ${turn} 轮</span><em>${escapeHTML(note)}</em></div>`;
    const score = assessment?.overall_score;
    return `<div class="live-score-row"><span>第 ${turn} 轮</span><strong>${score ?? "—"}</strong></div>`;
  }

  scoreCard({ turn, assessment, coach, feedback }) {
    const value = assessment || {};
    const dims = [["准确", value.accuracy], ["流利", value.fluency], ["完整", value.completeness]]
      .filter(([, number]) => typeof number === "number")
      .map(([label, number]) => {
        const percent = Math.max(0, Math.min(100, Math.round(number)));
        return `<div class="score-dim"><span>${label}</span><i><b style="width:${percent}%"></b></i><em>${percent}</em></div>`;
      }).join("");
    return `
      <div class="score-card">
        <div class="score-head"><span>第 ${turn} 轮</span><strong>${value.overall_score ?? "—"}</strong><em>分</em></div>
        <div class="score-dims">${dims}</div>
        ${this.wordGrid(value.word_results)}
        ${value.suggestion ? `<p class="score-tip">${escapeHTML(value.suggestion)}</p>` : ""}
        ${this.coachBlock(coach, feedback)}
      </div>`;
  }

  /* 逐词音标：本地模型给每个词一条 IPA 和音素级分数，色块就是结论。 */
  wordGrid(words) {
    const list = (words || []).filter(word => word && word.word);
    if (!list.length) return "";
    const shown = list.slice(0, 14);
    const chips = shown.map(word => {
      const score = Number(word.score) || 0;
      const tone = score >= 85 ? "is-good" : score >= 65 ? "is-warn" : "is-weak";
      const weak = (word.weak_phonemes || []).map(item => `/${item.symbol}/ ${item.score}`).join(" · ");
      const title = `${word.word} /${word.phonemes || ""}/ ${score} 分${weak ? ` · 弱音素 ${weak}` : ""}`;
      return `<span class="score-word ${tone}" title="${escapeHTML(title)}"><b>${escapeHTML(word.word)}</b><small>${escapeHTML(word.phonemes || "—")}</small></span>`;
    }).join("");
    const rest = list.length - shown.length;
    return `<div class="score-words">${chips}${rest > 0 ? `<span class="score-word is-more">+${rest}</span>` : ""}</div>`;
  }

  /* 教练分析来自另一次 DeepSeek 调用，和对话外教互不相干。 */
  coachBlock(coach, feedback) {
    if (!coach) return "";
    if (!feedback) return '<div class="score-coach is-loading">教练正在看这几个词…</div>';
    if (feedback.unavailable) return "";
    const advice = (feedback.advice || [])
      .map(item => `<li>${escapeHTML(item)}</li>`).join("");
    const drill = feedback.drill
      ? `<p class="coach-drill">跟读：${escapeHTML(feedback.drill)}</p>` : "";
    // 教练点名的词：音标 + 一句口型提示，这是"哪个单词比较差"的直接答案。
    const words = (feedback.weak_words || []).slice(0, 2).map(item => `
      <span class="coach-word"><b>${escapeHTML(item.word)}</b><small>/${escapeHTML(item.ipa || "")}/</small>${escapeHTML(item.tip || "")}</span>
    `).join("");
    if (!feedback.summary && !advice && !drill && !words) return "";
    return `<div class="score-coach">
      ${feedback.summary ? `<p>${escapeHTML(feedback.summary)}</p>` : ""}
      ${words ? `<div class="coach-words">${words}</div>` : ""}
      ${advice ? `<ul>${advice}</ul>` : ""}
      ${drill}
    </div>`;
  }

  async finish() {
    if (!this.active) throw new Error("当前没有进行中的对话。");
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) throw new Error("对话连接已断开。");
    setLiveState("生成报告");
    const promise = new Promise(resolve => { this.pendingReport = { resolve }; });
    this.ws.send(JSON.stringify({ type: "end" }));
    const timeout = new Promise((_, reject) => setTimeout(() => reject(new Error("报告生成超时，请重试")), 30000));
    try {
      await Promise.race([promise, timeout]);
    } finally {
      this.pendingReport = null;
    }
  }

  close(skipEnd = false) {
    this.active = false;
    this.listening = false;
    this.playbackPending = false;
    clearTimeout(this.playbackTimer);
    this.playbackTimer = null;
    this.fallbackQueue = [];
    this.fallbackPlaying = false;
    if ("speechSynthesis" in window) window.speechSynthesis.cancel();
    this.mic.stop();
    this.player.flush();
    if (!skipEnd && this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "end" }));
    this.ws?.close();
    this.ws = null;
  }
}

function syncTranslateUI(enabled) {
  const button = $("#liveTranslate");
  if (button) button.setAttribute("aria-pressed", String(Boolean(enabled)));
  const box = $("#translateReply");
  if (box) box.checked = Boolean(enabled);
}

function setLiveState(text) {
  const element = $("#liveState");
  if (!element) return;
  element.textContent = text;
  element.dataset.state = text;
}

/** 录音按钮只是状态指示：真正收音由服务端 state 事件控制。 */
function setRecordButton(recording, label) {
  const button = $("#conversationRecord");
  if (button) {
    button.classList.toggle("is-recording", Boolean(recording));
    button.setAttribute("aria-pressed", String(Boolean(recording)));
  }
  const text = $("#conversationRecordLabel");
  if (text && label) text.textContent = label;
}

function renderSceneSetup() {
  const topics = state.content.topics || [];
  const customMode = state.conversation.sceneSource === "custom";
  $("#topicList").innerHTML = topics.map((topic, index) => {
    const selected = !customMode && topic.id === state.selectedTopic;
    return `
    <button class="topic-option ${selected ? "is-selected" : ""}" data-topic="${topic.id}" role="radio" aria-checked="${selected}">
      <span class="topic-number">${String(index + 1).padStart(2, "0")}</span>
      <span class="topic-name">${topic.title}</span>
      <span class="topic-check"><svg><use href="#i-check"/></svg></span>
    </button>`;
  }).join("");

  $("#customScene").value = state.conversation.customText || "";
  $(".scene-custom")?.classList.toggle("is-active", customMode && Boolean(state.conversation.customText));

  $$("#durationOptions button").forEach(item => {
    const selected = Number(item.dataset.duration) === state.selectedDuration;
    item.classList.toggle("is-selected", selected);
    item.setAttribute("aria-checked", String(selected));
  });
  $("#realtimeScore").checked = Boolean(state.conversation.realtimeScore);
  syncTranslateUI(Boolean(state.conversation.translate));
}

async function startConversation() {
  if (state.conversation.busy) return;
  if (isRecording()) {
    showAlert("请先结束当前对话或录音。", true);
    return;
  }
  // 场景是单选：预设卡 vs 自定义，由 sceneSource 决定，不会两套同时生效。
  const customText = ($("#customScene")?.value || "").trim();
  const useCustom = state.conversation.sceneSource === "custom" && Boolean(customText);
  const scene = useCustom ? { kind: "custom", text: customText } : { kind: "preset", id: state.selectedTopic };

  state.conversation.busy = true;
  const button = $("#startConversation");
  button.disabled = true;
  button.textContent = "正在准备…";
  try {
    // 先切到对话视图，再开麦：开麦后 isRecording() 会拦住路由跳转。
    if (location.hash !== "#conversation") {
      history.replaceState(null, "", "#conversation");
      renderRoute();
    }
    state.conversation.customText = useCustom ? customText : "";
    state.conversation.realtimeScore = $("#realtimeScore").checked;
    state.conversation.translate = Boolean($("#translateReply")?.checked);
    state.conversation.session = {
      id: "pending", status: "active", turns: [], scene: {},
      duration_seconds: state.selectedDuration * 60,
    };
    state.conversation.startedAt = Date.now();
    renderConversation();

    const live = new ConversationLive();
    state.conversation.live = live;
    await live.start({
      scene,
      grade: state.grade,
      minutes: state.selectedDuration,
      realtimeScore: state.conversation.realtimeScore,
      translate: state.conversation.translate,
    });
  } catch (error) {
    state.conversation.live?.close();
    state.conversation.live = null;
    state.conversation.session = null;
    renderConversation();
    showAlert(error.message, true);
  } finally {
    state.conversation.busy = false;
    button.disabled = false;
    button.innerHTML = '开始对话<svg><use href="#i-mic"/></svg>';
  }
}

function resetConversation() {
  state.conversation.live?.close();
  state.conversation.live = null;
  state.conversation.session = null;
  clearInterval(state.conversation.timer);
  renderConversation();
}

$("#topicList")?.addEventListener("click", event => {
  const button = event.target.closest("[data-topic]");
  if (!button) return;
  state.selectedTopic = button.dataset.topic;
  // 场景是单选：选预设就退出自定义，避免两套来源同时生效。
  state.conversation.sceneSource = "preset";
  $("#customScene").value = "";
  state.conversation.customText = "";
  renderSceneSetup();
});

$("#customScene")?.addEventListener("input", event => {
  const text = event.target.value;
  state.conversation.customText = text;
  state.conversation.sceneSource = text.trim() ? "custom" : "preset";
  event.target.closest(".scene-custom")?.classList.toggle("is-active", Boolean(text.trim()));
  // 切到自定义后取消预设卡的高亮。
  $$("#topicList .topic-option").forEach(item => {
    item.classList.remove("is-selected");
    item.setAttribute("aria-checked", "false");
  });
});

$("#customScene")?.addEventListener("focus", event => {
  if (!event.target.value.trim()) return;
  state.conversation.sceneSource = "custom";
  event.target.closest(".scene-custom")?.classList.add("is-active");
});

$("#sceneChips")?.addEventListener("click", event => {
  const button = event.target.closest("[data-scene]");
  if (!button) return;
  $("#customScene").value = button.dataset.scene;
  state.conversation.customText = button.dataset.scene;
  state.conversation.sceneSource = "custom";
  $(".scene-custom")?.classList.add("is-active");
  $$("#topicList .topic-option").forEach(item => {
    item.classList.remove("is-selected");
    item.setAttribute("aria-checked", "false");
  });
  showAlert("已填入自定义情景，点“开始对话”即可。");
});

$("#durationOptions")?.addEventListener("click", event => {
  const button = event.target.closest("[data-duration]");
  if (!button) return;
  state.selectedDuration = Number(button.dataset.duration);
  $$("#durationOptions button").forEach(item => {
    const selected = item === button;
    item.classList.toggle("is-selected", selected);
    item.setAttribute("aria-checked", String(selected));
  });
});

$("#translateReply")?.addEventListener("change", event => {
  state.conversation.translate = event.target.checked;
  syncTranslateUI(state.conversation.translate);
  state.conversation.live?.setTranslate(state.conversation.translate);
});

$("#liveTranslate")?.addEventListener("click", () => {
  const live = state.conversation.live;
  const next = !(live ? live.translate : state.conversation.translate);
  if (live) live.setTranslate(next);
  else {
    state.conversation.translate = next;
    syncTranslateUI(next);
  }
});

$("#realtimeScore")?.addEventListener("change", event => {
  state.conversation.realtimeScore = event.target.checked;
  const live = state.conversation.live;
  if (live) live.setRealtimeScore(event.target.checked);
});

$("#liveRealtimeScore")?.addEventListener("change", event => {
  const live = state.conversation.live;
  if (!live) {
    state.conversation.realtimeScore = event.target.checked;
    return;
  }
  live.setRealtimeScore(event.target.checked);
});

$("#startConversation")?.addEventListener("click", startConversation);
$("#finishConversation")?.addEventListener("click", finishConversation);
$("#restartConversation")?.addEventListener("click", resetConversation);

$("#sendTurn")?.addEventListener("click", () => {
  const live = state.conversation.live;
  if (!live) return;
  const text = $("#liveTranscript").value.trim();
  // 手动改过的文字优先按文字提交，否则让服务端用语音转写判停。
  if (text && text !== (live.lastAsr || "")) {
    live.sendText(text);
    $("#liveTranscript").value = "";
    live.lastAsr = "";
  } else {
    live.ws?.send(JSON.stringify({ type: "turn_end" }));
  }
});

$("#conversationRecord")?.addEventListener("click", () => {
  const live = state.conversation.live;
  if (!live) return;
  showAlert("外教说话时麦克风会自动暂停；轮到你说时开口即可，说完停顿约 2 秒，等整句转成文字再发给外教。");
});
