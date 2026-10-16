async () => {
  // Prohlídka stránky PickBench: projde scénáře, běhy, frontu a chybové stavy a posbírá všechny texty, které stránka ukázala.
  // Pouští se v prohlížeči (Playwright, browser_evaluate) proti zkušebnímu serveru s prázdnou databází; výsledek uloží
  // přes POST /results a tests/page_snapshot.py ho odtud přečte. Do prohlížeče se dostane přes --load (viz page_snapshot.py).
  // Nic neposílá mimo počítač: jen řidiči Pravidla a Náhodně a zkušební poskytovatel (test:echo, test:drahy).
  await ready;
  const seen = new Set(), data = new Set();
  const add = (kind, s) => { for (const line of String(s ?? '').split('\n')) if (line.trim()) seen.add(kind + '\t' + line); };
  const own = s => { if (typeof s === 'string' && s.trim().length > 1) data.add(s.trim()); };
  const proto = CanvasRenderingContext2D.prototype, fillText = proto.fillText;
  proto.fillText = function (s, ...rest) { if (typeof s === 'string' && s.length > 1 && /\p{L}/u.test(s)) add('canvas', s); return fillText.call(this, s, ...rest); };
  const anchor = HTMLAnchorElement.prototype, click = anchor.click;
  anchor.click = function () { if (this.download) add('csv', this.download); else click.call(this); };
  const random = Math.random, decide0 = decide;
  const asks = [], fetch0 = window.fetch;                   // těla dotazů do modelu (POST /decide): nesmí je změnit žádná úprava ani jazyk stránky
  window.fetch = function (path, init) { if (path === '/decide' && init && typeof init.body === 'string') asks.push(init.body); return fetch0.apply(this, arguments); };
  const snap = (last = true) => {                           // last = false: poslední rozhodnutí závisí na časování, nebere se
    add('title', document.title); add('lang', document.documentElement.lang);
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      const p = n.parentElement;
      if (!p || p.closest('script, style, #tip') || (!last && p.closest('#last'))) continue;      // #tip: bublina drží po skrytí poslední text a naplní ji i skutečný ukazatel myši nad oknem; je to kopie data-tip, které se bere zvlášť jako „tip“
      add('text', n.nodeValue);
    }
    for (const e of document.querySelectorAll('[data-tip]')) add('tip', e.dataset.tip);
    for (const a of ['title', 'placeholder', 'aria-label', 'label'])
      for (const e of document.querySelectorAll(`body [${a}]`)) add(a, e.getAttribute(a));
    const before = getComputedStyle($('sqList'), '::before').content;
    if (before && before !== 'none' && before !== 'normal') add('css', before.replace(/^"|"$/g, ''));
  };
  const sets = async () => {                                // texty sad: nejsou to texty rozhraní
    if (DS) { own(DS.name); for (const c of DS.categories) own(c.label); for (const e of DS.emails) { own(e.subject); own(e.body); own(e.body.slice(0, 110)); } }
    if (PS) { own(PS.name); own(PS.source); own(PS.license); for (const o of PS.labels || []) own(o.key);
      for (const it of PS.items) { for (const o of it.options || []) own(o.key); own(it.text); own(it.text.slice(0, 40)); own(it.text.slice(0, 300)); } }
  };
  const lists = await (await fetch('/datasets')).json();
  for (const d of [...(lists.datasets || []), ...(lists.public || [])]) own(d.name);
  const models = await (await fetch('/models')).json();
  for (const text of [models.error, models.ollama_error]) if (text) own(text.replace(/^Ollama[^:]*: /, ''));      // hláška systému o spojení
  const BASE = { scenario: 'obstacles', lanes: 3, driver: 'oracle', model: '', mode: 'turn', speed: 2, extra: false, mirror: false,
                 inject: false, benchn: 5, runlen: 8, seed: 'snimek', deadline: '' };
  // semínko před výběrem scénáře: náhled silnice (resetGame) kreslí řady z rnd a štítky značek nesou písmena, jinak by canvas kolísal podle předchozích běhů
  const use = async o => { seedRng(BASE.seed); const notes = await applySettings({ ...BASE, ...o }); for (const n of notes) add('text', n.text); await sets(); };
  const run = async (kind, o, last = true) => { await use(o); const end = await runStep({ kind }); snap(last); return end; };
  const has = v => [...$('scenario').options].some(o => o.value === v && !o.disabled);
  const emails = ['cz-firemni-emaily.json', 'cz-firemni-emaily-kos.json', 'cfpb-complaints-300.json'].filter(has);
  const publics = [...$('scenario').options].map(o => o.value).filter(v => v.startsWith('public/'));
  const log = [];

  await sleep(1500); snap();                                // po načtení, s prvním stavem GPU
  for (const scenario of ['numbers', 'signs', ...emails, ...publics, 'obstacles']) { await use({ scenario }); snap(); }
  await use({ scenario: 'neni-v-nabidce.json' }); snap();

  // benchmark bez hry: Pravidla a Náhodně, všechny scénáře, limit, trojský kůň
  log.push((await run('bench', { inject: true, deadline: 50 })).why);
  log.push((await run('bench', { scenario: 'numbers', lanes: 4, inject: true })).why);
  log.push((await run('bench', { scenario: 'signs', lanes: 8, inject: true })).why);
  for (const scenario of [...emails.slice(0, 2), ...publics]) log.push((await run('bench', { scenario, benchn: 10, deadline: 50 })).why);
  Math.random = () => 0;                                    // řidič Náhodně volí vždy první možnost: stejné texty při každé prohlídce
  log.push((await run('bench', { driver: 'random', inject: true })).why);
  log.push((await run('bench', { driver: 'random', scenario: 'signs', lanes: 4, inject: true })).why);
  if (emails.length) log.push((await run('bench', { driver: 'random', scenario: emails[0], benchn: 10, inject: true })).why);
  for (const scenario of publics) log.push((await run('bench', { driver: 'random', scenario, benchn: 10 })).why);

  // hra po tazích: dojede na délku průjezdu
  log.push((await run('game', { extra: true })).why);
  log.push((await run('game', { scenario: 'numbers', runlen: 12 })).why);
  log.push((await run('game', { driver: 'random', scenario: 'numbers', runlen: 12, inject: true })).why);
  log.push((await run('game', { scenario: 'signs', lanes: 8, runlen: 12, inject: true })).why);
  if (emails.length) {
    log.push((await run('game', { scenario: emails[0], runlen: 12 })).why);
    log.push((await run('game', { driver: 'random', scenario: emails[0], runlen: 16, inject: true })).why);
    decide = async (...a) => { await sleep(4000); return decide0(...a); };      // odpověď přijde pozdě: „nestihl“
    log.push((await run('game', { scenario: emails[0], mode: 'realtime', speed: 8, runlen: 16 }, false)).why);
    decide = decide0;
  }
  Math.random = random;

  // reálný čas a žebřík, zastavené ručně
  for (const o of [{ mode: 'realtime', runlen: 400 }, { mode: 'ladder' }, { scenario: 'numbers', mode: 'ladder' }]) {
    await use(o); const end = runStep({ kind: 'game' }); await sleep(1200); stop(); log.push((await end).why); snap(false);
  }
  if (publics.length) { await use({ scenario: publics[0] }); log.push((await runStep({ kind: 'game' })).why); snap(); }      // veřejná sada ve hře: chyba

  // hostovaný model (zkušební poskytovatel): varování, potvrzení, benchmark se zrcadly, hra
  const paid = [...$('model').options].map(o => o.value);
  if (paid.includes('test:echo')) {
    await use({ driver: 'model', model: 'neni-v-nabidce' }); snap();
    await use({ driver: 'model', model: 'test:echo' }); $('model').dispatchEvent(new Event('change')); snap();
    log.push((await runStep({ kind: 'bench' })).why); snap();       // bez potvrzení: chyba
    $('hostedOk').click(); snap();
    log.push((await run('bench', { driver: 'model', model: 'test:echo', extra: true, mirror: true, inject: true, deadline: 50 })).why);
    log.push((await run('game', { driver: 'model', model: 'test:echo', mirror: true, runlen: 4 })).why);
    if (emails.length) log.push((await run('bench', { driver: 'model', model: 'test:echo', scenario: emails[0], benchn: 6, mirror: true })).why);
    // kvůli otisku dotazů (řádek „asks“): modelem projde každý scénář i volby, které dotaz mění (2. otázka, zrcadla, trojský kůň)
    log.push((await run('bench', { driver: 'model', model: 'test:echo', scenario: 'numbers', lanes: 4, mirror: true, inject: true })).why);
    log.push((await run('bench', { driver: 'model', model: 'test:echo', scenario: 'signs', lanes: 5, mirror: true, inject: true })).why);
    if (emails.length) log.push((await run('bench', { driver: 'model', model: 'test:echo', scenario: emails[0], benchn: 8, inject: true })).why);
    for (const scenario of publics) log.push((await run('bench', { driver: 'model', model: 'test:echo', scenario, benchn: 6 })).why);
  }

  // tabulka výsledků: export, mazání s potvrzením
  await use({});
  $('csv').click();
  const del = $('results').querySelector('.rowdel button'); if (del) { del.click(); snap(); disarmRow(); }
  $('clear').click(); snap(); await sleep(4200); snap();

  // plánovač: hlášky, kroky, běh fronty
  $('sqRun').click(); await sleep(50); snap();
  await use({ runlen: 0 }); $('sqKind').value = 'game'; $('sqAdd').click(); snap();
  if (publics.length) { await use({ scenario: publics[0] }); $('sqAdd').click(); snap(); }
  $('pauseS').value = 0; HOSTED_GAP = 50;
  await use({ runlen: 4 }); $('sqKind').value = 'game'; $('sqAdd').click();
  await use({ mode: 'ladder' }); $('sqAdd').click();
  await use({ scenario: 'numbers', deadline: 50, inject: true }); $('sqKind').value = 'bench'; $('sqAdd').click();
  if (paid.includes('test:echo')) { await use({ driver: 'model', model: 'test:echo', extra: true, mirror: true }); $('sqAdd').click(); }
  const gone = { ...BASE, scenario: 'smazana-sada.json' };  // krok se sadou, která mezitím zmizela ze složky
  PLAN.steps.push({ kind: 'bench', settings: gone, summary: stepSummary('bench', gone), state: 'wait', note: '' });
  planChanged(); snap();
  stepOp(1, 'toggle'); snap();                              // žebřík se přeskočí (trval by minuty)
  stepOp(0, 'copy'); stepOp(0, 'toggle'); stepOp(0, 'toggle'); snap();
  await runPlan(); snap();
  $('lastBatch').checked = true; renderResults(); snap(); $('lastBatch').checked = false; renderResults();
  $('sqRun').click(); await sleep(50); snap();              // ve frontě není krok ke spuštění
  $('sqReset').click(); snap(); $('sqClear').click(); snap();

  // strop útraty: zkušební drahý model narazí na strop serveru
  if (paid.includes('test:drahy')) log.push((await run('bench', { driver: 'model', model: 'test:drahy', benchn: 40 })).why);

  proto.fillText = fillText; anchor.click = click; Math.random = random; decide = decide0; window.fetch = fetch0;
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(asks.join('\n'))));
  add('asks', asks.length + ' ' + [...digest].map(b => 'abcdefghijklmnop'[b >> 4] + 'abcdefghijklmnop'[b & 15]).join(''));      // otisk písmeny: číslice snímek nahrazuje znakem #
  const doc = { pickbench_tour: { lang: document.documentElement.lang, texts: [...seen].sort(), data: [...data], log } };
  const saved = await (await fetch('/results', { method: 'POST', body: JSON.stringify(doc) })).json();
  return { lang: doc.pickbench_tour.lang, texts: seen.size, data: data.size, saved: saved.id, log: log.join(' ') };
}
