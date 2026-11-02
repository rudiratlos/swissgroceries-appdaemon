// HTTP-Brücke für swissgroceries-mcp (spricht MCP über stdio).
//   POST /promotions   {"chains":["denner"], "query":"Käse", "endingWithinDays":3}
//                      -> JSON-RPC-Antwort des MCP-Servers (result.content[0].text = JSON-Liste)
//   GET  /health       -> {"status":"ok", ...}
// Der MCP-Prozess läuft dauerhaft; Anfragen werden strikt nacheinander abgearbeitet (Warteschlange),
// hängende oder abgestürzte Prozesse werden automatisch neu gestartet.
import http from 'node:http';
import { spawn } from 'node:child_process';
import { readFileSync, existsSync } from 'node:fs';
import { createInterface } from 'node:readline';

let options = {};
try { if (existsSync('/data/options.json')) options = JSON.parse(readFileSync('/data/options.json', 'utf8')); } catch { /* ohne Optionen weiter */ }

const PORT = Number(process.env.PORT || 8080);
const SCRIPT = process.env.MCP_SCRIPT || '/app/swissgroceries-mcp.mjs';
const TIMEOUT_MS = Number(process.env.REQUEST_TIMEOUT || options.request_timeout || 90) * 1000;
const GAP_MS = Number(process.env.GAP_MS ?? options.gap_ms ?? 500);
const DEBUG = (process.env.LOG_LEVEL || options.log_level || 'info') === 'debug';

const log = (...a) => console.log(new Date().toISOString().replace('T', ' ').slice(0, 19), ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const startedAt = Date.now();

class McpClient {
  constructor(script) { this.script = script; this.child = null; this.pending = new Map(); this.nextId = 1; this.ready = null; }

  ensure() {
    if (!this.ready) this.ready = this.#start().catch((e) => { this.ready = null; this.kill(); throw e; });
    return this.ready;
  }

  async #start() {
    log('Starte MCP-Prozess:', this.script);
    const child = spawn(process.execPath, [this.script], { stdio: ['pipe', 'pipe', 'pipe'] });
    this.child = child;
    createInterface({ input: child.stdout }).on('line', (line) => {
      let msg; try { msg = JSON.parse(line); } catch { return; }       // Log-Zeilen ignorieren
      const p = this.pending.get(msg.id);
      if (p) { this.pending.delete(msg.id); clearTimeout(p.timer); p.resolve(msg); }
    });
    child.stderr.on('data', (d) => { if (DEBUG) log('[mcp]', String(d).trim()); });
    child.stdin.on('error', () => { /* EPIPE: exit-Handler räumt auf */ });
    child.on('error', (e) => this.#gone(child, `Start fehlgeschlagen: ${e.message}`));
    child.on('exit', (code, sig) => this.#gone(child, `MCP-Prozess beendet (code ${code ?? sig})`));
    await this.request('initialize', {
      protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'ha-addon', version: '1.0' },
    }, 30000);
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
    log('MCP-Prozess bereit');
  }

  #gone(child, reason) {
    if (this.child !== child) return;               // veralteter Prozess
    log(reason);
    this.child = null; this.ready = null;
    for (const [, p] of this.pending) { clearTimeout(p.timer); p.reject(new Error(reason)); }
    this.pending.clear();
  }

  kill() { if (this.child) { try { this.child.kill('SIGKILL'); } catch { /* schon weg */ } } }

  request(method, params, timeoutMs = TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      if (!this.child) return reject(new Error('MCP-Prozess läuft nicht'));
      const id = this.nextId++;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Zeitüberschreitung nach ${timeoutMs / 1000} s`));
        this.kill();                                 // hängenden Prozess neu starten lassen
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
    });
  }
}

const client = new McpClient(SCRIPT);

// Warteschlange: immer nur EIN Abruf gleichzeitig, mit kurzer Pause dazwischen.
let tail = Promise.resolve();
let waiting = 0;
function enqueue(job) {
  const run = tail.then(job);
  tail = run.then(() => sleep(GAP_MS), () => sleep(GAP_MS));
  return run;
}

async function callPromotions(args) {
  await client.ensure();
  return client.request('tools/call', { name: 'get_promotions', arguments: args });
}

function sanitize(body) {
  const out = {};
  if (Array.isArray(body.chains)) out.chains = body.chains.filter((c) => typeof c === 'string' && /^[a-z0-9_-]{2,20}$/i.test(c));
  if (typeof body.query === 'string' && body.query.length <= 100) out.query = body.query;
  if (Array.isArray(body.storeIds)) out.storeIds = body.storeIds.filter((s) => typeof s === 'string').slice(0, 50);
  if (Number.isFinite(body.endingWithinDays)) out.endingWithinDays = body.endingWithinDays;
  return out;
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let data = '';
    req.on('data', (c) => { data += c; if (data.length > 65536) { reject(new Error('Anfrage zu groß')); req.destroy(); } });
    req.on('end', () => { try { resolve(JSON.parse(data || '{}')); } catch { reject(new Error('Ungültiges JSON')); } });
    req.on('error', reject);
  });
}

function send(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(body) });
  res.end(body);
}

function countItems(response) {
  try { const items = JSON.parse(response.result.content[0].text); return Array.isArray(items) ? items.length : '?'; } catch { return '?'; }
}

const server = http.createServer(async (req, res) => {
  const { pathname } = new URL(req.url, 'http://localhost');
  if (req.method === 'GET' && pathname === '/health') {
    return send(res, 200, { status: 'ok', mcp: client.child ? 'running' : 'idle', waiting, uptime_s: Math.round((Date.now() - startedAt) / 1000) });
  }
  if (req.method === 'POST' && pathname === '/promotions') {
    let body;
    try { body = await readJson(req); } catch (e) { return send(res, 400, { error: e.message }); }
    const args = sanitize(body);
    if (!args.chains || args.chains.length === 0) return send(res, 400, { error: '"chains" fehlt, z. B. {"chains":["denner"]}' });
    const label = args.chains.join(',');
    const t0 = Date.now();
    waiting++;
    if (waiting > 1) log(`${label}: eingereiht (${waiting - 1} davor)`);
    try {
      const response = await enqueue(() => callPromotions(args));
      log(`${label}: ${countItems(response)} Aktionen in ${((Date.now() - t0) / 1000).toFixed(1)} s`);
      return send(res, 200, response);
    } catch (e) {
      log(`${label}: Fehler: ${e.message}`);
      return send(res, 502, { error: e.message });
    } finally { waiting--; }
  }
  send(res, 404, { error: 'nicht gefunden' });
});

server.listen(PORT, '0.0.0.0', () => log(`Swissgroceries-MCP-Dienst lauscht auf Port ${PORT} (Timeout ${TIMEOUT_MS / 1000} s, Pause ${GAP_MS} ms)`));

for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => { log(`${sig}: beende ...`); client.kill(); server.close(() => process.exit(0)); setTimeout(() => process.exit(0), 3000).unref(); });
}
