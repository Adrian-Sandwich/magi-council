// Señales originales 100% sintetizadas como maquinaria industrial:
// relés, contactores, prensa hidráulica, motor, descarga neumática y sirena.
// Sin grabaciones ni audio de terceros.
const MagiSound = (() => {
  let context, enabled = false, volume = .25, last = -Infinity;
  try {
    const saved = JSON.parse(localStorage.getItem('clami.sound') || '{}');
    enabled = saved.enabled === true;
    if (Number.isFinite(saved.volume)) volume = Math.max(0, Math.min(1, saved.volume));
  } catch (_) { /* Storage is optional. */ }
  const voices = new Set();
  function save() {
    try { localStorage.setItem('clami.sound', JSON.stringify({enabled, volume})); } catch (_) {}
  }
  function silence() { for (const voice of voices) voice.stop(); voices.clear(); }
  async function unlock() {
    if (!enabled) return;
    try {
      context ||= new (window.AudioContext || window.webkitAudioContext)();
      if (context.state === 'suspended') await context.resume();
    } catch (_) { /* Unsupported audio must never interrupt a message. */ }
  }
  // Una voz con envolvente: frecuencia base, glide opcional, ataque y caída.
  // Frequency glides and inharmonic partials give the console a sci-fi hardware character.
  function tone({f, to, at = 0, dur = .2, type = 'sine', level = 1, attack = .01, detune = 0}) {
    const t0 = context.currentTime + at;
    const osc = context.createOscillator(), gain = context.createGain();
    osc.type = type;
    osc.detune.value = detune;
    osc.frequency.setValueAtTime(f, t0);
    if (to) osc.frequency.exponentialRampToValueAtTime(to, t0 + dur);
    gain.gain.setValueAtTime(0, t0);
    gain.gain.linearRampToValueAtTime(volume * .14 * level, t0 + attack);
    gain.gain.exponentialRampToValueAtTime(.0001, t0 + dur);
    osc.connect(gain); gain.connect(context.destination);
    voices.add(osc);
    osc.onended = () => { voices.delete(osc); osc.disconnect(); gain.disconnect(); };
    osc.start(t0); osc.stop(t0 + dur + .05);
  }
  // Pareja desafinada leve: el "coro" que da grosor cinematográfico.
  function pad(f, opts = {}) {
    tone({f, ...opts});
    tone({f: f * 1.004, to: opts.to ? opts.to * 1.004 : undefined,
          at: opts.at ?? 0, dur: opts.dur, type: opts.type,
          level: (opts.level ?? 1) * .7, attack: opts.attack});
  }
  function metallic({f, at = 0, dur = .18, level = .5}) {
    tone({f, at, dur, type: 'square', level, attack: .003, detune: -11});
    tone({f: f * 1.618, at, dur: dur * .72, type: 'triangle', level: level * .55, attack: .002, detune: 7});
  }
  function radio({at = 0, dur = .08, level = .28}) {
    const frames = Math.max(1, Math.floor(context.sampleRate * dur));
    const buffer = context.createBuffer(1, frames, context.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < frames; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / frames);
    const source = context.createBufferSource();
    const filter = context.createBiquadFilter(), gain = context.createGain();
    const t0 = context.currentTime + at;
    filter.type = 'bandpass'; filter.frequency.value = 2400; filter.Q.value = .8;
    gain.gain.setValueAtTime(volume * level, t0);
    gain.gain.exponentialRampToValueAtTime(.0001, t0 + dur);
    source.buffer = buffer; source.connect(filter); filter.connect(gain); gain.connect(context.destination);
    voices.add(source);
    source.onended = () => { voices.delete(source); source.disconnect(); filter.disconnect(); gain.disconnect(); };
    source.start(t0); source.stop(t0 + dur + .01);
  }
  function noise({at = 0, dur = .2, level = .3, filterType = 'lowpass', frequency = 500, q = .7}) {
    const frames = Math.max(1, Math.floor(context.sampleRate * dur));
    const buffer = context.createBuffer(1, frames, context.sampleRate);
    const data = buffer.getChannelData(0);
    for (let i = 0; i < frames; i++) data[i] = Math.random() * 2 - 1;
    const source = context.createBufferSource(), filter = context.createBiquadFilter(), gain = context.createGain();
    const t0 = context.currentTime + at;
    filter.type = filterType; filter.frequency.value = frequency; filter.Q.value = q;
    gain.gain.setValueAtTime(0, t0);
    gain.gain.linearRampToValueAtTime(volume * level, t0 + Math.min(.015, dur / 4));
    gain.gain.exponentialRampToValueAtTime(.0001, t0 + dur);
    source.buffer = buffer; source.connect(filter); filter.connect(gain); gain.connect(context.destination);
    voices.add(source);
    source.onended = () => { voices.delete(source); source.disconnect(); filter.disconnect(); gain.disconnect(); };
    source.start(t0); source.stop(t0 + dur + .01);
  }
  function impact(at = 0, level = 1) {
    tone({f: 78, to: 38, at, dur: .22, type: 'sine', level, attack: .002});
    noise({at, dur: .075, level: .42 * level, frequency: 310});
    metallic({f: 118, at: at + .012, dur: .16, level: .36 * level});
  }
  function relay(at = 0, level = 1) {
    noise({at, dur: .018, level: .38 * level, filterType: 'highpass', frequency: 2100, q: 1.4});
    tone({f: 920, to: 410, at, dur: .035, type: 'square', level: .25 * level, attack: .001});
  }
  const cues = {
    boot() {
      relay(0, .8); relay(.09, .8); relay(.18, 1);
      tone({f: 42, to: 86, at: .19, dur: .85, type: 'sawtooth', level: .48, attack: .18});
      noise({at: .72, dur: .32, level: .22, filterType: 'highpass', frequency: 3600});
    },
    // Envío: contactor doble cerrando un circuito.
    send() {
      relay(0, 1); relay(.075, .65);
      tone({f: 125, to: 82, at: .04, dur: .13, type: 'sawtooth', level: .32, attack: .006});
    },
    // Voto: sello de una prensa neumática y escape corto.
    vote() {
      noise({dur: .12, level: .28, filterType: 'highpass', frequency: 2800});
      impact(.07, .72);
      noise({at: .19, dur: .22, level: .16, filterType: 'highpass', frequency: 4200});
    },
    // El ejecutor arrancó: contactor, motor pesado y válvula de presión.
    machinery() {
      relay(0, 1); impact(.07, .7);
      tone({f: 38, to: 74, at: .1, dur: 1.15, type: 'sawtooth', level: .58, attack: .2});
      tone({f: 76, to: 148, at: .1, dur: 1.05, type: 'square', level: .18, attack: .22});
      noise({at: .82, dur: .42, level: .2, filterType: 'highpass', frequency: 3300});
    },
    // Veredicto: tres golpes de prensa y descarga final.
    result() {
      impact(0, .72); impact(.22, .86); impact(.48, 1.05);
      tone({f: 92, to: 46, at: .5, dur: .7, type: 'sawtooth', level: .42, attack: .03});
      noise({at: .68, dur: .5, level: .2, filterType: 'highpass', frequency: 3900});
    },
    // Alerta de planta: sirena disonante sobre vibración grave.
    attention() {
      impact(0, .72);
      tone({f: 47, to: 58, dur: 2.25, type: 'sawtooth', level: .55, attack: .12});
      tone({f: 188, to: 132, at: .08, dur: .82, type: 'square', level: .44, attack: .025});
      tone({f: 188, to: 132, at: 1.05, dur: .82, type: 'square', level: .44, attack: .025});
      relay(.94, .65); relay(1.92, .65);
    },
  };
  function play(kind) {
    if (!enabled || !context || context.state !== 'running' || document.hidden || !volume) return;
    const now = context.currentTime;
    if (now - last < .18) return;
    last = now;
    (cues[kind] || cues.send)();
  }
  return {unlock, play, get enabled() {return enabled;}, get volume() {return volume;},
    setEnabled(value) {enabled = value; if (!value) silence(); save();},
    setVolume(value) {volume = Math.max(0, Math.min(1, value)); silence(); save();}};
})();
