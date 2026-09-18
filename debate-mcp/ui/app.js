// MAGI — ClaMi: cliente de la interfaz.
// Triángulo y estética de TomaszRewak/MAGI (MIT, © 2023 Tomasz Rewak).
// Interacción simplificada a pedido del operador: una sola caja estilo CLI,
// conversación limpia (sin metadata de kinds/ids), indicador "thinking…",
// todo el texto en inglés. El estado llega por SSE (LISTEN/NOTIFY).

const SLOTS = ["melchior", "balthasar", "casper"];
// color de IDENTIDAD por asiento (en el triángulo el color es el del voto;
// acá es fijo para reconocer quién habla)
const SEAT_COLORS = {
  melchior: "#52e691",
  balthasar: "#ff8d00",
  casper: "#3caee0",
  adrian: "#d8d8d8",
  magi: "#ff8d00",
};
// Colores que el server usa en badges/identidades. Los valores se interpolan
// en atributos style: si una fila manual o un tipo futuro trae otra cosa,
// mejor un color neutro que CSS inyectado.
const SAFE_COLORS = new Set(["#52e691", "#a41413", "#ff8d00", "#3caee0", "#d8d8d8", "gray"]);
function safeColor(c) { return SAFE_COLORS.has(c) ? c : "#d8d8d8"; }
const POSITION_COLORS = {
  yes: "#52e691",
  no: "#a41413",
  info: "#3caee0",
  conditional: "repeating-linear-gradient(56deg, rgb(82, 230, 145) 0px, rgb(82, 230, 145) 30px, #82cd68 30px, #82cd68 60px)",
  pending: "black",
};

// Token de sesión: lo inyecta magi_ui.py al servir index.html. Cada POST lo
// manda en X-Magi-Token; el SSE (que no puede mandar headers) va por ?token=.
const MAGI_TOKEN = window.MAGI_TOKEN || "";

function postJSON(url, payload) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Magi-Token": MAGI_TOKEN },
    body: JSON.stringify(payload),
  });
}

function fetchJSON(url) {
  return fetch(url, { headers: { "X-Magi-Token": MAGI_TOKEN } });
}

let state = null;
let focusedId = null;
let uiMode = "council";   // council | chat
let newDraft = false;
let sending = false;
let connected = false;
let replyAction = "resume";
let replyFocusId = null;  // decisión para la que replyAction tiene sentido
let magiSignature = "";
let conversationSignature = "";

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function focusPool() {
  return state?.decisions ?? [];
}

function focused() {
  if (newDraft) return null;
  const pool = focusPool();
  if (!pool.length) return null;
  if (focusedId && !pool.find(d => d.id === focusedId)) return null;
  if (!focusedId) {
    // default: la más reciente abierta; si no, la última actividad
    const open = pool.filter(d => ["open", "split", "executing"].includes(d.status)).sort((a, b) => b.id - a.id);
    focusedId = (open[0] ?? pool[0]).id;
  }
  return pool.find(d => d.id === focusedId);
}

function thinkingSeats(d) {
  if (!d || d.status !== "open") return [];
  return d.seats.filter(s => !s.voted && !s.error).map(s => s.seat);
}

// ------------------------------------------------------------- magi

// Palabra del voto dentro del polígono: el color del polígono nunca es el
// único canal (guía NERV funcional; WCAG 1.4.1).
const VOTE_WORDS = {yes: "YES", no: "NO", conditional: "CONDITIONAL", info: "INFO"};

// La línea viva bajo ADAPTIVE · ROUND. La misma frase va dentro del
// triángulo en escritorio y a la statusbar en móvil, donde el triángulo
// no tiene altura para que se lea.
function liveStatusLine(d) {
  const activeExecution = d?.status === "executing" ? d.execution_activity : null;
  const activeSeat = d?.seats?.find(seat => seat.activity);
  const synthesis = d?.synthesis;
  if (activeExecution) {
    const elapsed = activeExecution.started_at
      ? Math.max(0, Math.round((Date.now() - Date.parse(activeExecution.started_at)) / 1000)) : 0;
    const phases = ["READING APPROVED PLAN", "INSPECTING ISOLATED WORKTREE", "APPLYING AND VERIFYING CHANGES"];
    const phase = activeExecution.phase || phases[Math.floor(elapsed / 8) % phases.length];
    return `▸ ${String(activeExecution.seat || "executor").toUpperCase()}: ${phase} · ${elapsed}s`;
  }
  if (activeSeat?.activity) {
    const activity = activeSeat.activity;
    const elapsed = activity.started_at
      ? Math.max(0, Math.round((Date.now() - Date.parse(activity.started_at)) / 1000)) : 0;
    const phase = activity.turn === "synthesis" ? "SYNTHESIZING" : "INVESTIGATING";
    return `▸ ${activeSeat.seat.toUpperCase()}: ${phase} · ${elapsed}s`;
  }
  if (synthesis?.status === "generating") {
    const phase = synthesis.phase === "drafting" ? "DRAFTING COUNCIL ANSWER" : "REVIEWING COUNCIL ANSWER";
    return `▸ ${String(synthesis.current_head || "MAGI").toUpperCase()}: ${phase}`;
  }
  if (d?.status === "open") return "▸ MAGI: DELIBERATION IN PROGRESS";
  if (d?.status === "executing" && d.execution_state === "reviewing") return "▸ MAGI: IMPLEMENTATION UNDER REVIEW";
  if (d?.execution_state === "merged" && synthesis?.next_move && !d.continuation) return "▸ MAGI: NEXT MOVE READY FOR AUTHORIZATION";
  if (d?.execution_state === "merged") return "▸ MAGI: OBJECTIVE COMPLETE · STANDBY";
  if (d?.status === "closed") return "▸ MAGI: DECISION COMPLETE · STANDBY";
  if (d?.status === "split") return "▸ MAGI: WAITING FOR OPERATOR RULING";
  return "▸ MAGI: SYSTEM READY · STANDBY";
}

function renderMagi(d) {
  const activeExecution = d?.status === "executing" ? d.execution_activity : null;
  const activeSeat = d?.seats?.find(seat => seat.activity);
  const synthesis = d?.synthesis;
  const live = activeExecution || activeSeat?.activity || synthesis?.status === "generating";
  const tick = live ? Math.floor(Date.now() / 5000) : 0;
  const signature = JSON.stringify(d ? [d.id, d.round, d.status, d.badge, d.seats,
    activeExecution, synthesis?.status, synthesis?.phase, synthesis?.current_head,
    synthesis?.next_move?.title, d.continuation, tick] : null);
  if (signature === magiSignature) return;
  magiSignature = signature;
  const magi = document.getElementById("magi");
  magi.querySelectorAll(".wise-man, .response, .system-status, .title").forEach(e => e.remove());

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = "MAGI";
  magi.appendChild(title);

  const thinking = thinkingSeats(d);

  const status = document.createElement("div");
  status.className = "system-status";
  const ext = d ? `${String(d.protocol).toUpperCase()} · ROUND ${d.round}` : "STANDBY";
  status.innerHTML = `<div>${esc(ext)}</div>`;
  const line = document.createElement("div");
  line.className = `execution-script${live ? " live" : " idle"}`;
  line.textContent = liveStatusLine(d);
  status.appendChild(line);
  magi.appendChild(status);

  (d?.seats ?? SLOTS.map(seat => ({seat, voted:false}))).slice(0, 3).forEach((seat, i) => {
    const slot = SLOTS[i];
    const isThinking = thinking.includes(seat.seat);
    const isExecuting = activeExecution?.seat === seat.seat;
    const isSynthesizing = synthesis?.status === "generating"
      && (synthesis.current_head === seat.seat || synthesis.current_head === "all heads");
    const isActive = isThinking || isExecuting || isSynthesizing || Boolean(seat.activity);
    const color = isExecuting ? SEAT_COLORS[seat.seat]
      : seat.voted ? POSITION_COLORS[seat.position] : POSITION_COLORS.pending;
    const outer = document.createElement("div");
    outer.className = `wise-man ${slot}${isExecuting ? " executor-active" : ""}`;
    outer.style.setProperty("--executor-color", safeColor(SEAT_COLORS[seat.seat] || "#ff8d00"));
    const inner = document.createElement("div");
    inner.className = "inner" + (isActive ? " flicker" : "");
    inner.style.background = color;
    if (seat.voted && ["yes", "conditional", "info"].includes(seat.position)) inner.style.color = "#080604";
    const name = document.createElement("span");
    name.textContent = `${seat.seat.toUpperCase()} • ${i + 1}`;
    const word = document.createElement("span");
    word.className = "vote-word";
    // la actividad (THINKING/EXECUTING) ya la dice la etiqueta bajo el polígono
    word.textContent = seat.error ? "ERROR"
      : seat.voted ? (VOTE_WORDS[seat.position] || String(seat.position).toUpperCase()) : "PENDING";
    inner.append(name, word);
    outer.appendChild(inner);
    if (isActive || seat.error) {
      const tag = document.createElement("div");
      tag.className = "thinking-tag";
      tag.textContent = seat.error ? "ERROR" : isExecuting ? "EXECUTING"
        : isSynthesizing ? "SYNTHESIZING" : "THINKING";
      outer.appendChild(tag);
    }
    outer.addEventListener("click", () => openModal(seat));
    outer.tabIndex = 0;
    outer.setAttribute("role", "button");
    outer.setAttribute("aria-label", `${seat.seat}: ${seat.voted ? seat.position : 'no vote yet'}. Read reasoning`);
    outer.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openModal(seat); }
    });
    magi.appendChild(outer);
  });

  const resp = document.createElement("div");
  resp.className = "response" + (d && d.badge.flicker ? " flicker" : "");
  resp.style.color = d ? safeColor(d.badge.color) : "#ff8d00";
  resp.style.borderColor = d ? safeColor(d.badge.color) : "#ff8d00";
  const inner = document.createElement("div");
  inner.className = "inner";
  inner.textContent = d ? d.badge.text : "STANDBY";
  resp.appendChild(inner);
  magi.appendChild(resp);
}

// ------------------------------------------------------------- conversación

function renderStatusBar(d) {
  const el = document.getElementById("statusbar");
  if (uiMode === "chat") {
    el.textContent = "OPEN CONVERSATION — the three heads answer in turn";
    return;
  }
  if (!d) { el.textContent = "MAGI SYSTEM — STANDBY"; return; }
  const conf = d.confidence != null ? ` · VOTE AGREEMENT ${Math.round(Number(d.confidence) * 100)}%` : "";
  el.textContent = `#${d.id} ${d.badge.text}${conf} — ${d.title}`;
  // Visible sólo en móvil (CSS): ahí el triángulo no tiene altura para la línea viva.
  const live = document.createElement("span");
  live.className = "live-line";
  live.textContent = liveStatusLine(d);
  el.append(live);
}

function verdictText(d) {
  if (!d) return "No decision selected";
  if (d.status === "open") return "The council is still deliberating";
  if (d.status === "split") return "The council needs your decision";
  if (d.status === "executing") return d.execution_state === "failed"
    ? "The approved plan needs attention" : "The approved plan is in execution";
  // Sin repositorio ni ejecución no hay nada que "aprobar": es una respuesta.
  // «Approved with conditions» para «¿qué es lo divino?» era vocabulario de
  // revisión de código aplicado a una pregunta.
  if (!d.artifact && !d.production) {
    return ({yes: "El consejo coincide", no: "El consejo lo rechaza", conditional: "Respuesta con matices",
             info: "Respuesta del consejo"}[d.ruling] || d.badge.text);
  }
  return ({yes: "Approved", no: "Rejected", conditional: "Approved with conditions"}[d.ruling] || d.badge.text);
}

// Frases equivalentes salvo puntuación/mayúsculas: las condiciones de tres
// votos y el «Later» de la síntesis se repetían entre sí.
function dedupePhrases(items) {
  const seen = new Set();
  return (items || []).filter(text => {
    const key = String(text).toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function shortReason(body) {
  const clean = String(body || "").replace(/\s+/g, " ").trim();
  if (!clean) return "No position recorded yet.";
  const sentence = clean.match(/^(.{1,180}?[.!?])(?:\s|$)/)?.[1] || clean.slice(0, 180);
  return sentence.length < clean.length ? `${sentence}…` : sentence;
}

const outcomeDrafts = new Map();
let outcomeTarget = null;
let outcomeSending = false;
const outcomeKeys = ["status", "observation", "evidence", "lesson"];
function outcomeRequestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
function outcomeDraft() {
  return Object.fromEntries(outcomeKeys.map(k => [k, document.getElementById(`outcome-${k}`).value]));
}
function renderOutcome(d) {
  const target = d?.id ?? null;
  if (outcomeTarget !== target) {
    if (outcomeTarget !== null) {
      const old = outcomeDrafts.get(outcomeTarget) || {};
      outcomeDrafts.set(outcomeTarget, {...old, ...outcomeDraft()});
    }
    outcomeTarget = target;
    const draft = outcomeDrafts.get(target) || {};
    for (const k of outcomeKeys) document.getElementById(`outcome-${k}`).value = draft[k] || (k === "status" ? "unknown" : "");
    document.getElementById("outcome-notice").textContent = "";
  }
  document.getElementById("outcome-panel").hidden = !d || d.status === "open" || uiMode !== "council";
  document.getElementById("outcome-save").disabled = outcomeSending || !connected;
  const labels = {worked:"Funcionó", failed:"No funcionó", partial:"Parcial", unknown:"Sin confirmar"};
  document.getElementById("outcome-last").textContent = d?.outcome
    ? `Último reporte tuyo: ${labels[d.outcome.status]}. ${d.outcome.observation}`
    : "Este reporte conserva lo observado en la misma decisión. No abre otra deliberación.";
}
document.getElementById("outcome-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!outcomeTarget || outcomeSending || !connected) return;
  const target = outcomeTarget;
  const values = outcomeDraft();
  const signature = JSON.stringify(values);
  const prior = outcomeDrafts.get(target) || {};
  const request_id = prior.signature === signature ? prior.request_id : outcomeRequestId();
  outcomeDrafts.set(target, {...values, signature, request_id});
  outcomeSending = true; renderOutcome(focused());
  try {
    const response = await postJSON("/outcome", {decision_id:target, ...values, request_id});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "No se pudo guardar");
    const unchanged = outcomeTarget === target && JSON.stringify(outcomeDraft()) === signature;
    if (unchanged) outcomeDrafts.delete(target);
    if (outcomeTarget === target) {
      if (unchanged) for (const key of outcomeKeys.filter(k => k !== "status")) document.getElementById(`outcome-${key}`).value = "";
      document.getElementById("outcome-notice").textContent = "Resultado guardado. El grafo lo incorporará en la próxima sincronización.";
    }
  } catch (err) {
    if (outcomeTarget === target) document.getElementById("outcome-notice").textContent = err.message;
  } finally { outcomeSending = false; renderOutcome(focused()); }
});

function renderSummary(d) {
  const card = document.getElementById("summary-card");
  const title = document.getElementById("summary-title");
  const lead = document.getElementById("summary-lead");
  const meta = document.getElementById("summary-meta");
  const conditions = document.getElementById("summary-conditions");
  const seats = document.getElementById("summary-seats");
  const content = document.getElementById("summary-content");
  const continuation = document.getElementById("continuation-card");
  content.replaceChildren();
  continuation.replaceChildren();
  continuation.hidden = true;
  if (!d) {
    card.classList.add("empty"); title.textContent = "No decision selected";
    lead.textContent = "Ask a question to get a readable conclusion from all three heads.";
    meta.textContent = ""; conditions.hidden = true; seats.innerHTML = ""; return;
  }
  card.classList.remove("empty");
  title.textContent = verdictText(d);
  const voted = (d.seats || []).filter(s => s.voted);
  const counts = voted.reduce((out, s) => { out[s.position] = (out[s.position] || 0) + 1; return out; }, {});
  const voteLine = voted.length ? Object.entries(counts).map(([position, count]) => `${count} ${position}`).join(" · ") : "No votes yet";
  const confidence = d.confidence == null ? "votes pending" : `${Math.round(Number(d.confidence) * 100)}% vote agreement`;
  meta.textContent = `${voteLine}  ·  ${confidence}  ·  round ${d.round}`;
  lead.textContent = d.status === "open"
    ? `${voted.length} of ${(d.seats || []).length} heads have answered. The synthesis will settle when the round closes.`
    : d.status === "split" ? "The perspectives do not converge. Read the three reasons below, then choose how to continue."
    : "La respuesta conjunta todavía no está disponible para esta conversación. Los aportes completos están disponibles abajo.";
  if (d.status === "executing") {
    const activity = d.execution_activity;
    if (activity) {
      const elapsed = activity.started_at
        ? Math.max(0, Math.round((Date.now() - Date.parse(activity.started_at)) / 1000)) : 0;
      const worker = String(activity.seat || "executor").toUpperCase();
      title.textContent = `${worker} ESTÁ TRABAJANDO`;
      lead.textContent = `${worker} está implementando el plan en la rama aislada. Lleva ${elapsed}s en ejecución.`;
      meta.textContent = `PROCESO ACTIVO · PID ${activity.pid || "iniciando"} · ${activity.log || "preparando registro"}`;
      const progress = document.createElement("p");
      progress.className = "execution-progress";
      progress.textContent = "La pantalla se actualiza automáticamente. Al terminar, el consejo revisará el diff antes de integrar cambios.";
      content.append(progress);
    } else if (d.execution_state === "pending") {
      title.textContent = "EJECUCIÓN EN COLA";
      lead.textContent = "El plan está aprobado y espera un proceso ejecutor disponible.";
    } else if (d.execution_state === "reviewing") {
      title.textContent = "IMPLEMENTACIÓN TERMINADA · EN REVISIÓN";
      lead.textContent = "El ejecutor terminó y el consejo está revisando el diff antes de integrarlo.";
    } else if (d.execution_state === "failed") {
      title.textContent = "LA EJECUCIÓN NECESITA ATENCIÓN";
      lead.textContent = "El proceso terminó sin una implementación válida. El detalle y la opción de reintento están en la conversación.";
    }
  }
  const synthesis = d.synthesis;
  if (Object.keys(d.turn_errors || {}).length) {
    title.textContent = "Una cabeza no pudo completar su turno";
    lead.textContent = Object.entries(d.turn_errors).map(([seat, error]) => `${seat}: ${error.message}`).join("\n");
    const note = document.createElement("p");
    note.textContent = "No habrá reintentos automáticos. Los votos recibidos se conservan; corrige la causa y pulsa Reintentar cabezas fallidas.";
    content.append(note);
  }
  if (synthesis?.answer && ["reviewed", "partial", "generating"].includes(synthesis.status)) {
    lead.textContent = synthesis.answer;
    const approved = (synthesis.reviews || []).filter(r => r.approve).length;
    meta.textContent += ` · Revisión de fidelidad: ${approved}/${(d.seats || []).length} · ciclo ${synthesis.cycle}/2`;
    if (synthesis.status === "generating") {
      title.textContent += " · Borrador en revisión";
      const progress = document.createElement("p");
      progress.textContent = `${synthesis.current_head || "El consejo"}: ${synthesis.phase === "drafting" ? "corrigiendo el borrador" : "revisando la respuesta"}. Hasta 120 segundos por intervención.`;
      content.append(progress);
    }
    if (synthesis.content_state) {
      const note = document.createElement("p");
      const labels = {consensus:"Las cabezas aceptaron esta respuesta común; eso no demuestra que sea una verdad universal.",
        next_round:"Todavía hay objeciones al contenido. El consejo continuará con una ronda de corrección.",
        budget_exhausted:"Respuesta provisional: se agotaron las rondas sin acuerdo sobre el contenido.",
        unavailable:"Respuesta provisional: faltó una revisión válida del contenido."};
      note.textContent = labels[synthesis.content_state] || "";
      content.prepend(note);
      if (!synthesis.content_consensus) title.textContent += " · Sin consenso de contenido";
    }
    for (const [key, label] of [["agreements", "Puntos compartidos"], ["differences", "Diferencias"], ["open_questions", "Qué falta resolver"]]) {
      if (!(synthesis[key] || []).length) continue;
      const section = document.createElement("section");
      const heading = document.createElement("strong"); heading.textContent = label; section.append(heading);
      const list = document.createElement("ul");
      for (const text of synthesis[key]) { const item = document.createElement("li"); item.textContent = text; list.append(item); }
      section.append(list); content.append(section);
    }
    if (synthesis.status === "partial") {
      title.textContent += " · Borrador sin validación completa";
      const note = document.createElement("p");
      note.textContent = `Borrador: no todas las cabezas validaron esta síntesis. Ciclos realizados: ${synthesis.cycle}.`;
      content.prepend(note);
      for (const review of (synthesis.reviews || []).filter(r => !r.approve)) {
        const item = document.createElement("p"); item.textContent = `${review.seat}: ${review.feedback}`; content.append(item);
      }
    }
  } else if (synthesis?.status === "error") {
    lead.textContent = "No se pudo preparar la síntesis. Las aportaciones están conservadas; podés continuar esta conversación.";
  } else if (synthesis?.status === "generating") {
    lead.textContent = `${synthesis.current_head || "El consejo"} está redactando la respuesta conjunta. Ciclo ${synthesis.cycle || 1}/2; hasta 120 segundos por intervención.`;
  }
  const conceptual = !d.artifact && !d.production;
  // En una pregunta sin repositorio las "condiciones" de los votos son
  // matices, y la síntesis ya los integra en la respuesta.
  const allConditions = conceptual ? [] : dedupePhrases(d.approved_conditions?.length
    ? d.approved_conditions
    : (d.seats || []).filter(s => s.voted).flatMap(s => s.conditions || []));
  const later = conceptual ? [] : dedupePhrases([...allConditions, ...(d.deferred_items || [])]).slice(allConditions.length);
  conditions.hidden = !allConditions.length && !later.length;
  conditions.innerHTML = allConditions.length
    ? `<strong>Conditions:</strong> ${allConditions.map(esc).join(" · ")}` : "";
  if (later.length) {
    conditions.innerHTML += `${allConditions.length ? "<br>" : ""}<strong>Later:</strong> ${later.map(esc).join(" · ")}`;
  }
  const proposal = d.execution_state === "merged" ? d.synthesis?.next_move : null;
  if (proposal) renderContinuation(d, proposal, continuation);
  seats.innerHTML = (d.seats || []).map(s => {
    const color = safeColor(SEAT_COLORS[s.seat] || "#d8d8d8");
    const elapsed = s.activity?.started_at
      ? Math.max(0, Math.round((Date.now() - Date.parse(s.activity.started_at)) / 1000)) : null;
    const phase = s.activity?.turn === "synthesis" ? "SYNTHESIZING" : "INVESTIGATING";
    const position = s.voted ? String(s.position).toUpperCase() : s.error ? "ERROR"
      : s.activity ? `${phase} · ${elapsed}s` : "WAITING";
    return `<button class="summary-seat" data-seat="${esc(s.seat)}" style="--seat-color:${color}" aria-label="Read ${esc(s.seat)} reasoning">
      <span class="summary-seat-name">${esc(s.seat.toUpperCase())}</span><span class="summary-seat-vote">${esc(position)}</span>
      <span class="summary-seat-body">${s.error ? esc(s.error.message) : s.voted ? "Ver aportación completa" : s.activity ? `PID ${esc(s.activity.pid || "pending")} · ${esc(s.activity.turn || "turn")}` : "Esperando aportación"}</span></button>`;
  }).join("");
  seats.querySelectorAll(".summary-seat").forEach(button => {
    const seat = (d.seats || []).find(s => s.seat === button.dataset.seat);
    button.addEventListener("click", () => openModal(seat));
  });
}

// ------------------------------------------------------------- memoria

// Qué nodos del grafo vio el consejo en la ronda y la calificación humana
// (¿sirvió?). Las etiquetas son el conjunto calificado a mano que la
// evaluación de recuperación usa; nunca cambian votos ni reabren nada.
function renderMemoryFeedback(d) {
  const box = document.getElementById("memory-feedback");
  const sources = d?.memory_sources?.ids || [];
  box.hidden = !sources.length || uiMode !== "council";
  if (box.hidden) { box.replaceChildren(); return; }
  const feedback = d.memory_feedback;
  const signature = JSON.stringify([d.id, sources, feedback?.id, sending]);
  if (box.dataset.signature === signature) return;
  box.dataset.signature = signature;
  box.replaceChildren();
  const label = document.createElement("span");
  label.textContent = `Memoria consultada en la ronda ${d.memory_sources.round}: ${sources.length} fuente${sources.length === 1 ? "" : "s"}. ¿Sirvió?`;
  const list = document.createElement("span");
  list.className = "memory-sources";
  list.textContent = sources.join(" · ");
  const buttons = [["useful", true, "👍 Sirvió"], ["useless", false, "👎 No sirvió"]].map(([key, useful, text]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = text;
    button.setAttribute("aria-pressed", String(feedback ? feedback.useful === useful : false));
    button.disabled = sending || !connected;
    button.addEventListener("click", async () => {
      const status = document.getElementById("c-status");
      try {
        await postJSON("/memory-feedback", {decision_id: d.id, useful});
        status.textContent = useful ? "Gracias: esa memoria queda marcada como útil." : "Anotado: esa memoria no sirvió; la evaluación lo tendrá en cuenta.";
      } catch (error) {
        status.textContent = `No se pudo guardar la calificación: ${error.message}`;
      }
    });
    return button;
  });
  box.append(label, ...buttons);
  if (feedback) {
    const state = document.createElement("span");
    state.className = "memory-state";
    state.textContent = `Calificada: ${feedback.useful ? "sirvió" : "no sirvió"}${feedback.note ? " — " + feedback.note : ""}`;
    box.append(state);
  }
  box.append(list);
}

function renderConversation(d) {
  const el = document.getElementById("conversation");
  const input = document.getElementById("c-input");
  let msgs = [];
  if (uiMode === "chat") {
    msgs = state?.chat ?? [];
    input.placeholder = "Talk to the three heads — Enter to send";
  } else if (d) {
    msgs = d.journal ?? [];
    input.placeholder = d.status === "split"
      ? (replyAction === "resume" ? "Add the context the council needs for another round" : "Write your final ruling and explain why")
      : "Ask the council anything… Enter to send, Shift+Enter for a new line";
  } else {
    input.placeholder = "Ask the council anything… Enter to send, Shift+Enter for a new line";
  }
  const signature = JSON.stringify([uiMode, d?.id, msgs, thinkingSeats(d)]);
  if (signature === conversationSignature) return;
  conversationSignature = signature;
  if (!msgs.length) {
    el.innerHTML = uiMode === "chat"
      ? '<div class="welcome">Talk to the three heads — each answers from its own angle:<br>' +
        '<b>MELCHIOR</b> technical truth · <b>BALTHASAR</b> risk &amp; care · <b>CASPER</b> what you really want.<br>Just type below and press Enter.</div>'
      : '<div class="welcome">This is the <b>COUNCIL</b> — ask anything and three heads investigate, ' +
        'debate and vote:<br><b>MELCHIOR</b> what do the facts say · <b>BALTHASAR</b> who gets hurt if we\'re wrong · ' +
        '<b>CASPER</b> what do we actually want.<br>You get a verdict with confidence and dissent. Type below and press Enter. ' +
        'Switch to <b>CHAT</b> for open conversation without a vote.</div>';
    return;
  }
  // auto-scroll SOLO si el usuario estaba leyendo el final: cada frame SSE
  // re-renderiza, y bajar siempre el scroll no lo dejaba leer el historial.
  const estabaAbajo = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.innerHTML = msgs.map(m => {
    const color = safeColor(SEAT_COLORS[m.author] ?? "#d8d8d8");
    const who = m.author === "adrian" ? "YOU" : m.author.toUpperCase();
    return `<div class="msg"><span class="who" style="color:${color}">${esc(who)}</span>` +
           `<span class="body" style="border-color:${color}">${esc(m.body || "")}</span></div>`;
  }).join("");
  const thinking = uiMode === "chat" ? [] : thinkingSeats(d);
  if (thinking.length) {
    const t = document.createElement("div");
    t.className = "thinking-line";
    t.textContent = "· " + thinking.map(s => s.toUpperCase() + " is thinking…").join(" · ");
    el.appendChild(t);
  }
  // el snap va DESPUÉS de agregar la línea de thinking: si ajustaba antes,
  // la línea quedaba bajo el fold y el usuario no veía quién está pensando.
  if (estabaAbajo) el.scrollTop = el.scrollHeight;
}

function renderHistory() {
  const el = document.getElementById("history");
  const list = state?.decisions ?? [];
  if (!list.length) { el.innerHTML = ""; return; }
  el.innerHTML = '<span class="h-label">HISTORY&nbsp;</span>' + list.map(d =>
    `<a href="#" data-id="${d.id}" aria-current="${!newDraft && d.id === focusedId ? 'true' : 'false'}" style="color:${safeColor(d.badge.color)}">#${d.id} ${esc(d.badge.text)}` +
    ` <span class="h-title">— ${esc(d.title)}</span></a>`
  ).join(" &middot; ");
  el.querySelectorAll("a").forEach(a => a.addEventListener("click", ev => {
    ev.preventDefault();
    if (sending) return;
    focusedId = Number(a.dataset.id);
    newDraft = false;
    uiMode = "council";
    syncModes();
    render();
  }));
}

// Qué va a pasar con el próximo Enter, en palabras. Es la respuesta a "no sé
// qué hará mi mensaje": la UI anticipa la acción antes de que la escribas.
function executionIntent(text) {
  return /^\s*(?:(?:pues|bueno|entonces)\s+|ok[,;:]?\s+)?(?:arr[eé]gl(?:ar|alo|ala|enlo)|implement(?:ar|a|alo|enlo)|hazlo|h[aá]ganlo|ejecut(?:ar|a|alo|enlo)|aplic(?:ar|a|alo|enlo)|procede|apruebo\s+(?:tu\s+plan|el\s+plan|ese\s+plan|la\s+propuesta)|vamos\s+con\s+(?:eso|los\s+cambios|tu\s+plan|el\s+plan|ese\s+plan|tu\s+propuesta|la\s+propuesta)|sigamos\s+con\s+(?:eso|los\s+cambios|tu\s+plan|el\s+plan)|adelante\s+con\s+(?:el\s+plan|tu\s+plan|eso)|haz\s+lo\s+que\s+propones)\b/i.test(text || "");
}

function renderContinuation(d, proposal, card) {
  const disposition = d.continuation?.action;
  card.hidden = false;
  const recommendation = {
    execute: "MAGI recommends deliberating this move for execution.",
    discuss: "MAGI recommends discussing the scope before execution.",
    save: "MAGI recommends saving this opportunity for later.",
    stop: "MAGI recommends stopping here unless priorities change.",
  }[proposal.recommendation] || "MAGI found a related opportunity.";
  card.innerHTML = `<div class="continuation-kicker">NEXT MOVE PROPOSED</div>
    <h3 id="continuation-title">${esc(proposal.title)}</h3>
    <p>${esc(proposal.reason)}</p>
    <dl><dt>EXPECTED</dt><dd>${esc(proposal.expected_result)}</dd>
      <dt>SCOPE</dt><dd>${esc(proposal.scope)}</dd>
      <dt>RISK</dt><dd>${esc(proposal.risk)}</dd></dl>
    <p class="continuation-recommendation">${esc(recommendation)}</p>`;
  if (disposition) {
    const saved = document.createElement("p");
    saved.className = "continuation-state";
    saved.textContent = ({execute: "Approved · linked decision opened", discuss: "Discussion opened",
      save: "Saved for later", stop: "Cycle stopped"}[disposition] || disposition);
    card.append(saved);
    return;
  }
  const actions = document.createElement("div");
  actions.className = "continuation-actions";
  for (const [action, label] of [["execute", "Vamos con esto"], ["discuss", "Discutámoslo"],
                                  ["save", "Guardar para después"], ["stop", "Terminar"]]) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.action = action;
    button.textContent = label;
    button.addEventListener("click", () => chooseContinuation(d, action, button));
    actions.append(button);
  }
  card.append(actions);
}

async function chooseContinuation(d, action, button) {
  if (sending) return;
  const status = document.getElementById("c-status");
  sending = true;
  button.closest(".continuation-actions").querySelectorAll("button").forEach(b => b.disabled = true);
  status.textContent = "registering next move…";
  try {
    const resp = await postJSON("/continuation", {decision_id: d.id, action});
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    d.continuation = {action, title: d.synthesis?.next_move?.title};
    if (data.action === "opened_follow_up") focusedId = data.decision_id;
    status.textContent = action === "execute"
      ? `next move opened as production decision #${data.decision_id} · council approval is required before execution`
      : action === "discuss" ? `next move opened for discussion as decision #${data.decision_id}`
      : action === "save" ? "next move saved for later" : "agentic cycle stopped";
  } catch (err) {
    status.textContent = `error: ${err.message}`;
  } finally {
    sending = false;
    render();
  }
}

function renderIntent(d) {
  const el = document.getElementById("c-intent");
  const repo = document.getElementById("c-repo").value.trim();
  let txt;
  if (uiMode === "chat") {
    txt = "↳ Enter talks to the three heads in the open thread — no vote, just their takes.";
  } else if (!d) {
    txt = repo
      ? `↳ Enter opens a decision on ${repo}. If you later ask to implement the approved plan, this same conversation evolves into isolated execution and review.`
      : "↳ Enter opens a NEW decision using the default repository. Choose a repository below to analyze another project.";
  } else if (d.status === "open") {
    txt = `↳ Enter adds CONTEXT to #${d.id} — the heads read it on their next turn (${d.round}° round).`;
  } else if (d.status === "split") {
    const pos = (d.seats ?? []).filter(s => s.voted).map(s => s.position);
    const faltaInfo = pos.includes("info");
    txt = faltaInfo
      ? `↳ #${d.id} no es un desacuerdo: el consejo te pidió información. Escribí "seguí" + el contexto que falta, o tu ruling para cerrar igual.`
      : `↳ Enter closes #${d.id} with YOUR ruling — o escribí "seguí" para otra ronda.`;
  } else if (d.status === "executing") {
    if (d.execution_state === "failed" || d.execution_state === "merge_blocked") {
      txt = `↳ #${d.id} needs attention — read the result below. "seguí" retries execution and opens a new review; ABORT closes it.`;
    } else if (d.execution_state === "reviewing") {
      txt = `↳ #${d.id} is waiting for its implementation review. Select the review to give the council context.`;
    } else {
      txt = `↳ Enter adds context to #${d.id} — the executor is working; the council will review the diff after.`;
    }
  } else {
    txt = executionIntent(document.getElementById("c-input").value) && ["yes", "conditional"].includes(d.ruling)
      ? `↳ Enter sends approved plan #${d.id} to isolated execution; MAGI will review the diff before merging.`
      : `↳ Enter continues #${d.id} in the same thread — previous reasoning and memory stay attached. Ask to implement it when you want this plan executed.`;
  }
  if (d && uiMode === "council") txt += ` Repository: ${d.artifact || "default (no folder selected)"}.`;
  el.textContent = txt;
  // acciones del STALEMATE: distinguir "desacuerdo real" de "falta info".
  // Con info en la mezcla no hubo choque de criterio: las cabezas pidieron
  // datos; la acción natural es dar contexto, no arbitrar.
  const sa = document.getElementById("stalemate-actions");
  const esSplit = uiMode === "council" && d && d.status === "split";
  sa.hidden = !esSplit;
  if (esSplit) {
    const pos = (d.seats ?? []).filter(s => s.voted).map(s => s.position);
    const faltaInfo = pos.includes("info");
    sa.querySelector("span").textContent = faltaInfo
      ? "the council lacks information — they asked you:"
      : "the council disagrees — it's your call:";
    document.getElementById("sa-segui").textContent = faltaInfo
      ? "Provide missing context"
      : "Continue with context";
    el.textContent = replyAction === "resume"
      ? `Your message starts another voting round for #${d.id}. Add the information the council needs.`
      : `Your message becomes the final ruling for #${d.id} and closes this decision.`;
  }
}

function render() {
  const d = focused();
  if ((d?.id ?? null) !== replyFocusId) {
    // la decisión enfocada cambió (history click, frame SSE, cierre): la
    // acción elegida para el STALEMATE anterior no se hereda a la nueva —
    // sin esto, un Enter después de arbitrar cerraba la próxima split
    // "con tu ruling" sin intención.
    replyFocusId = d?.id ?? null;
    replyAction = "resume";
  }
  renderMagi(d);
  renderStatusBar(d);
  renderSummary(d);
  renderMemoryFeedback(d);
  renderOutcome(d);
  renderConversation(d);
  renderHistory();
  renderIntent(d);
  const active = d && ["open", "split", "executing"].includes(d.status);
  const newQuestion = uiMode === "council" && !active;
  document.querySelector(".composer-opts").hidden = !newQuestion;
  document.getElementById("repo-help").hidden = !newQuestion;
  if (!newQuestion) document.getElementById("fs-panel").hidden = true;
  const sendButton = document.getElementById("c-send");
  sendButton.textContent = sending ? "Sending…" : uiMode === "chat" ? "Send message"
    : newQuestion && !d ? "Ask council"
    : d.status === "closed" && executionIntent(document.getElementById("c-input").value) ? "Implement approved plan"
    : d.status === "closed" ? "Continue this decision"
    : d.status === "split" ? (replyAction === "resume" ? "Continue discussion" : "Close with my ruling") : "Add context";
  document.getElementById("sa-segui").setAttribute("aria-pressed", String(replyAction === "resume"));
  document.getElementById("sa-ruling").setAttribute("aria-pressed", String(replyAction === "arbitrate"));
  sendButton.disabled = sending || !connected || !document.getElementById("c-input").value.trim();
  document.querySelectorAll("#modes button, .decision-actions button, #stalemate-actions button").forEach(button => { button.disabled = sending; });
  const abortBtn = document.getElementById("c-abort");
  document.getElementById("c-retry").hidden = uiMode !== "council" || !Object.keys(d?.turn_errors || {}).length;
  document.getElementById("c-retry").disabled = sending || !connected;
  abortBtn.hidden = !(uiMode === "council" && d && ["open", "split", "executing"].includes(d.status));
  // NEW abre decisión nueva salteando la heurística; en CHAT no aplica
  document.getElementById("c-new").hidden = uiMode !== "council";
}

// Prepare a separate inquiry without sending or discarding the current text.
document.getElementById("c-new").addEventListener("click", async () => {
  const input = document.getElementById("c-input");
  const status = document.getElementById("c-status");
  newDraft = true;
  status.textContent = "New question — write your request, then send it to the council.";
  render();
  input.focus();
});

// Explicit response choices preserve the text and determine the server action.
document.getElementById("sa-segui").addEventListener("click", () => {
  const input = document.getElementById("c-input");
  replyAction = "resume";
  render();
  input.placeholder = "Add context for the next voting round";
  input.focus();
});
document.getElementById("sa-ruling").addEventListener("click", () => {
  const input = document.getElementById("c-input");
  replyAction = "arbitrate";
  render();
  input.placeholder = "your ruling and why — Enter closes the decision";
  input.focus();
});

function repositoryChanged() {
  newDraft = true;
  render();
}
document.getElementById("c-repo").addEventListener("input", repositoryChanged);

// --- mini-explorador de carpetas: elegir el repo sin tipear paths
let fsCurrent = null;

async function fsLoad(path) {
  const url = path ? `/fs?path=${encodeURIComponent(path)}` : "/fs";
  const resp = await fetchJSON(url);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  fsCurrent = data.path;
  document.getElementById("fs-path").textContent = data.path;
  document.getElementById("fs-up").hidden = !data.parent;
  document.getElementById("fs-list").innerHTML = data.dirs.length
    ? data.dirs.map(d =>
        `<span class="dir" data-path="${esc(d.path)}">${d.git ? '<span class="git-star">★</span>' : "▸"} ${esc(d.name)}</span>`
      ).join("")
    : '<span class="dir">(sin subcarpetas)</span>';
  document.querySelectorAll("#fs-list .dir[data-path]").forEach(el => {
    const abrir = () => fsLoad(el.dataset.path).catch(err => {
      document.getElementById("c-status").textContent = `error: ${err.message}`;
    });
    el.addEventListener("click", abrir);
    // mouse-only era: las entradas también se recorren con teclado
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); abrir(); }
    });
  });
}

document.getElementById("c-browse").addEventListener("click", async () => {
  const panel = document.getElementById("fs-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) {
    try {
      await fsLoad(document.getElementById("c-repo").value.trim() || null);
    } catch (err) {
      panel.hidden = true;
      document.getElementById("c-status").textContent = `error: ${err.message}`;
    }
  }
});

document.getElementById("fs-up").addEventListener("click", async () => {
  const resp = await fetchJSON(`/fs?path=${encodeURIComponent(fsCurrent)}`);
  const data = await resp.json();
  if (data.parent) await fsLoad(data.parent);
});

document.getElementById("fs-use").addEventListener("click", () => {
  document.getElementById("c-repo").value = fsCurrent;
  document.getElementById("fs-panel").hidden = true;
  repositoryChanged();
});

async function abortDecision() {
  const d = focused();
  if (!d) return;
  if (!confirm(`Abort decision #${d.id}? The heads' running turns get killed. This closes it as ABORTED.`)) return;
  const status = document.getElementById("c-status");
  status.textContent = "aborting…";
  try {
    const resp = await postJSON("/abort", { decision_id: d.id });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    status.textContent = `decision #${d.id} aborted — running turns killed`;
  } catch (err) {
    status.textContent = `error: ${err.message}`;
  }
}

document.getElementById("c-abort").addEventListener("click", abortDecision);

document.getElementById("c-retry").addEventListener("click", async () => {
  const d = focused();
  if (!d || sending || !connected) return;
  const status = document.getElementById("c-status");
  sending = true;
  render();
  try {
    const resp = await postJSON("/retry-turns", {decision_id: d.id,
      errors: Object.fromEntries(Object.entries(d.turn_errors).map(([seat, error]) => [seat, error.id]))});
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    status.textContent = "Reintento solicitado. Los votos anteriores se conservan.";
  } catch (err) {
    status.textContent = `error: ${err.message}`;
  } finally {
    sending = false;
    render();
  }
});

// ------------------------------------------------------------- modal

function openModal(seat) {
  if (seat.error) {
    document.getElementById("modal-title").textContent = `${seat.seat.toUpperCase()} — ERROR`;
    document.getElementById("modal-content").textContent = seat.error.message;
    document.getElementById("modal").showModal();
    return;
  }
  document.getElementById("modal-title").textContent =
    `${seat.seat.toUpperCase()} — ${seat.voted ? "POSITION: " + seat.position.toUpperCase() : "thinking…"}`;
  const cond = seat.conditions?.length ? `<br>CONDITIONS: ${esc(seat.conditions.join("; "))}` : "";
  document.getElementById("modal-content").innerHTML =
    `<div>position:</div><div>${seat.voted ? esc(seat.position) : "no vote yet this round"}${cond}</div>` +
    `<div>reasoning:</div><div style="white-space:pre-wrap">${esc(seat.body || "(still processing — this can take minutes on local models)")}</div>`;
  document.getElementById("modal").showModal();
}

document.getElementById("modal-close").addEventListener("click", () => {
  document.getElementById("modal").close();
});

// ------------------------------------------------------------- composer

function syncModes() {
  document.querySelectorAll("#modes button[data-mode]").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === uiMode));
}

document.querySelectorAll("#modes button[data-mode]").forEach(b =>
  b.addEventListener("click", () => { uiMode = b.dataset.mode; syncModes(); render(); }));

async function send(forceNew = false) {
  const status = document.getElementById("c-status");
  const input = document.getElementById("c-input");
  const body = input.value.trim();
  if (!body || sending || !connected) return;
  const originalValue = input.value;
  const payload = { mode: uiMode, body };
  const target = focused();
  if (uiMode === "council" && !forceNew) {
    if (target && ["open", "split", "executing", "closed"].includes(target.status)) {
      payload.decision_id = target.id;
      if (target.status === "split") payload.action = replyAction;
      if (target.status === "closed") payload.action = executionIntent(body) ? "execute" : "followup";
    } else {
      payload.force_new = true;
    }
  }
  // The repository is context. Execution intent can emerge later in this
  // same conversation, after the council has produced an approved plan.
  const repo = document.getElementById("c-repo").value.trim();
  if (uiMode === "council" && repo && (payload.force_new || forceNew)) {
    payload.artifact = repo;
  }
  if (forceNew) payload.force_new = true;
  sending = true;
  render();
  MagiSound.unlock();
  status.textContent = "sending…";
  // sin timeout, un POST colgado dejaba el composer bloqueado para siempre
  // (sending quedaba en true): a los 30s se aborta y el finally restaura.
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 30000);
  try {
    const resp = await postJSON("/message", payload);
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    if (data.action === "opened") {
      focusedId = data.decision_id;
      newDraft = false;
      status.textContent = data.production
        ? `production decision #${data.decision_id} opened — approve the plan and an executor implements it`
        : `decision #${data.decision_id} opened — the council is deliberating`;
    } else if (data.action === "reopened") {
      status.textContent = `decision #${data.decision_id} reopened — the heads recast with your context`;
    } else if (data.action === "hint") {
      status.textContent = data.message;
      if (input.value === originalValue) input.value = "";
      return;
    } else if (data.action === "arbitrated") {
      status.textContent = `decision #${data.decision_id} closed with your ruling`;
      replyAction = "resume";  // la próxima split empieza en "resume"
    } else if (data.action === "context") {
      status.textContent = "context added — the heads will see it on their next turn";
    } else if (data.action === "follow_up") {
      status.textContent = `decision #${data.decision_id} continued — the existing journal and memory stay attached`;
    } else if (data.action === "opened_follow_up") {
      focusedId = data.decision_id;
      status.textContent = `follow-up #${data.decision_id} opened from completed production #${data.source_decision_id}`;
    } else if (data.action === "execution_requested") {
      status.textContent = `decision #${data.decision_id} is entering isolated execution — the diff will be reviewed before merge`;
    } else if (data.action === "corrections_requested") {
      focusedId = data.decision_id;
      status.textContent = `review #${data.review_id} approved — corrections are executing in decision #${data.decision_id}`;
    } else {
      status.textContent = "sent — the council answers in turn";
    }
    if (input.value === originalValue) input.value = "";
    MagiSound.play("send");
  } catch (err) {
    status.textContent = err.name === "AbortError"
      ? "error: the server did not respond in 30s — try again"
      : `error: ${err.message}`;
    MagiSound.play("failure");  // el sonido confirma el resultado, no el click
  } finally {
    clearTimeout(timeoutId);
    sending = false;
    render();
  }
}

document.getElementById("c-input").addEventListener("keydown", ev => {
  if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); send(); }
});

// ------------------------------------------------------------- events

const events = new EventSource("/events?token=" + encodeURIComponent(MAGI_TOKEN));
let soundBaseline = false;
events.onmessage = e => {
  const next = JSON.parse(e.data);
  const previous = focused();
  const current = next.decisions.find(d => d.id === previous?.id);
  if (soundBaseline && previous && current) {
    // Familias distintas para que se aprendan: alerta = el consejo se trabó
    // y espera al operador; fallo = algo se rompió (ejecución, merge, cabeza).
    if (current.status !== previous.status || current.execution_state !== previous.execution_state) {
      MagiSound.play(current.status === "split" ? "attention"
        : ["failed", "merge_blocked"].includes(current.execution_state) ? "failure"
        : current.status === "executing" ? "machinery" : "result");
    } else if (Object.keys(current.turn_errors || {}).length > Object.keys(previous.turn_errors || {}).length) {
      MagiSound.play("failure");
    } else if (current.round === previous.round && current.seats.some(s => s.voted && !previous.seats.find(p => p.seat === s.seat)?.voted)) {
      MagiSound.play("vote");
    }
  }
  state = next; soundBaseline = true; connected = true;
  document.getElementById("connection-status").textContent = "● Live";
  render();
};
events.onerror = () => {
  connected = false; soundBaseline = false;
  document.getElementById("connection-status").textContent = "Reconnecting — sending paused";
  render();
};
document.getElementById("c-send").addEventListener("click", () => send());
document.getElementById("c-input").addEventListener("input", render);
const soundToggle = document.getElementById("sound-toggle");
function syncSound() {
  soundToggle.textContent = MagiSound.enabled ? "Sound: ON" : "Enable sound";
  soundToggle.setAttribute("aria-pressed", String(MagiSound.enabled));
  document.getElementById("sound-volume").value = MagiSound.volume * 100;
}
soundToggle.addEventListener("click", async () => {
  MagiSound.setEnabled(!MagiSound.enabled); syncSound();
  await MagiSound.unlock(); MagiSound.play("boot");
});
document.getElementById("sound-volume").addEventListener("input", event => MagiSound.setVolume(Number(event.target.value) / 100));
document.addEventListener("pointerdown", () => MagiSound.unlock());
document.addEventListener("keydown", () => MagiSound.unlock());
document.addEventListener("keydown", event => {
  if (event.key === "Escape") document.getElementById("modal-close").click();
});
syncSound(); syncModes(); render();
