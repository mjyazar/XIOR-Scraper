/**
 * XIOR Zernike Tower availability watcher - Cloudflare Worker edition.
 *
 * Runs on a 1-minute cron trigger, which is the reason this exists alongside
 * the GitHub Actions runner: Cloudflare fires on time, GitHub's scheduler does
 * not. Free plan covers this comfortably (1440 invocations/day against a
 * 100k/day limit).
 *
 * State lives in KV. The free plan allows 1000 KV writes/day, so the worker
 * writes only when the durable part of the state actually changes - a quiet
 * day costs roughly 70 writes, not 1440.
 */

const AJAX_URL = "https://www.xiorstudenthousing.eu/wp-admin/admin-ajax.php";
const ORIGIN = "https://www.xiorstudenthousing.eu";
const STATE_KEY = "xior-state-v1";

const TARGETS = [
  {
    key: "zernike-long",
    label: "Zernike Tower (long stay)",
    url: `${ORIGIN}/netherlands/groningen/zernike-tower-student-accommodation/`,
    pageId: "1119",
    semesterId: "3281",
  },
  {
    key: "zernike-short",
    label: "Zernike Tower (short stay)",
    url: `${ORIGIN}/netherlands/groningen/zernike-tower-short-stay/`,
    pageId: "1118",
    semesterId: "18429",
  },
];

const PAGE_REFRESH_MINUTES = 20;
const ERROR_ALERT_AFTER = 12;
const HEARTBEAT_HOURS = 24;

const USER_AGENTS = [
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
];
const ua = () => USER_AGENTS[Math.floor(Math.random() * USER_AGENTS.length)];

const AVAILABLE = "AVAILABLE", EMPTY = "EMPTY", BROKEN = "BROKEN",
      ERROR = "ERROR", RATELIMITED = "RATELIMITED";

// XIOR returns 429 "Too many requests" per IP on this endpoint and keeps
// limiting while you keep knocking, so a hit means stop, not retry.
const RATELIMIT_COOLDOWN_SECONDS = 600;
const RATELIMIT_COOLDOWN_MAX = 3600;
const RATELIMIT_ALERT_AFTER = 6;
const isRateLimited = (status, body) =>
  status === 429 || String(body || "").slice(0, 400).includes("Too many requests");
const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------------ HTTP -- */

async function fetchRetry(url, opts, tries = 3) {
  let last = { status: 0, body: "" };
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(url, opts);
      const body = await r.text();
      // Transient 403s happen; a lone one is noise, so retry.
      if (r.ok) return { status: r.status, body };
      last = { status: r.status, body };
      // Never retry a rate limit - it only extends the ban.
      if (isRateLimited(r.status, body)) return last;
      if (![403, 500, 502, 503, 504].includes(r.status)) return last;
    } catch (e) {
      last = { status: 0, body: String(e) };
    }
    if (i < tries - 1) await sleep(700 * (i + 1) + Math.random() * 600);
  }
  return last;
}

const ajaxHeaders = () => ({
  "User-Agent": ua(),
  Accept: "*/*",
  "Accept-Language": "en-US,en;q=0.9",
  // Mirrors the headers the site's own front-end sends; all are required.
  "X-Requested-With": "XMLHttpRequest",
  Origin: ORIGIN,
  Referer: `${ORIGIN}/`,
  "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
});

/* ------------------------------------------------- site config discovery -- */

/**
 * Extract page config from the property page. Uses indexOf-anchored slices
 * rather than regex over the full ~1.2MB document to stay inside the free
 * plan's CPU budget.
 */
async function scrapePageConfig(url) {
  const r = await fetchRetry(url, {
    headers: {
      "User-Agent": ua(),
      Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9",
      "Sec-Fetch-Dest": "document",
      "Sec-Fetch-Mode": "navigate",
      "Sec-Fetch-Site": "none",
    },
  });
  if (r.status !== 200 || r.body.length < 20000) {
    return { error: `page fetch failed (status=${r.status}, bytes=${r.body.length})` };
  }
  const html = r.body;

  const near = (anchor, window, re) => {
    const i = html.indexOf(anchor);
    if (i === -1) return null;
    const m = html.slice(i, i + window).match(re);
    return m ? m[1] : null;
  };

  const pageId = near("propertyPageId", 200, /propertyPageId\s*=\s*(\d+)/);
  const semesterId =
    near('id="yardi-semester"', 300, /value="(\d*)"/) ??
    near('name="semester"', 300, /value="(\d*)"/);

  // Room ids cluster inside the booking modal; scan only that region.
  const modalAt = html.indexOf('id="yardi-modal"');
  const region = modalAt === -1 ? html.slice(0, 400000) : html.slice(modalAt, modalAt + 60000);
  const rooms = [...new Set([...region.matchAll(/data-room-id="(\d+)"/g)].map((m) => m[1]))].sort();

  if (!pageId) return { error: "no Yardi booking modal on page (layout changed?)" };
  return { pageId, semesterId: semesterId || "", roomTypeIds: rooms };
}

async function checkAvailability(pageId, semesterId) {
  const r = await fetchRetry(AJAX_URL, {
    method: "POST",
    headers: ajaxHeaders(),
    body: new URLSearchParams({
      action: "yardi_room_availability",
      "cf-turnstile-response": "",
      property_page_id: pageId,
      room_type_id: "0",
      semester_id: semesterId,
    }),
  });

  if (isRateLimited(r.status, r.body)) {
    return { outcome: RATELIMITED, units: [], detail: "429 too many requests" };
  }
  if (r.status !== 200) return { outcome: ERROR, units: [], detail: `http ${r.status}` };

  let payload;
  try {
    payload = JSON.parse(r.body);
  } catch {
    return { outcome: ERROR, units: [], detail: `non-JSON (${r.body.slice(0, 60)})` };
  }
  if (!payload.success) {
    const msg = payload?.data?.message || "unknown failure";
    if (/too many/i.test(msg)) return { outcome: RATELIMITED, units: [], detail: msg };
    return { outcome: BROKEN, units: [], detail: msg };
  }

  const data = payload.data || {};
  const units = data.units || [];
  if (units.length) return { outcome: AVAILABLE, units, detail: `${units.length} unit(s)` };

  // Zero units is ambiguous - distinguish real emptiness from a broken query.
  let ar = data.availability_response;
  if (typeof ar === "string") {
    try { ar = JSON.parse(ar); } catch { ar = { errorMessage: ar }; }
  }
  const code = ar?.errorCode;
  if (code === 204) return { outcome: EMPTY, units: [], detail: "no units (204)" };
  if (code) return { outcome: BROKEN, units: [], detail: `upstream ${code}: ${ar?.errorMessage || ""}` };
  if (!ar) return { outcome: EMPTY, units: [], detail: "no units" };
  return { outcome: BROKEN, units: [], detail: `unexpected upstream: ${ar?.errorMessage || ""}` };
}

/* --------------------------------------------------------- notifications -- */

async function notify(env, title, body, url, priority = "high") {
  const jobs = [];

  if (env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID) {
    // Plain text, no parse_mode: apartment names and Yardi URLs contain
    // _ * [ ] ( ) . - which Telegram's Markdown parser rejects with a 400, and
    // formatting is not worth a silently undelivered "room available" alert.
    let text = `${title}\n\n${body}`;
    if (url && !body.includes(url)) text += `\n\n${url}`;
    // Several ids allowed (comma or space separated) so one bot can alert
    // every device/account you own.
    const chats = env.TELEGRAM_CHAT_ID.split(/[,\s]+/).filter(Boolean);
    for (const chat of chats) {
      jobs.push(
        fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
          method: "POST",
          headers: { "Content-Type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({ chat_id: chat, text: text.slice(0, 4000) }),
        })
          .then((r) => [`telegram[${chat}]`, r.ok, r.status])
          .catch((e) => [`telegram[${chat}]`, false, String(e)])
      );
    }
  }

  if (env.NTFY_TOPIC) {
    const server = (env.NTFY_SERVER || "https://ntfy.sh").replace(/\/$/, "");
    const headers = {
      Title: title.replace(/[^\x20-\x7e]/g, "") || "XIOR alert",
      Priority: priority === "high" ? "urgent" : "default",
      Tags: priority === "high" ? "house,rotating_light" : "information_source",
    };
    if (url) headers.Click = url;
    jobs.push(
      fetch(`${server}/${env.NTFY_TOPIC}`, { method: "POST", headers, body })
        .then((r) => ["ntfy", r.ok, r.status]).catch((e) => ["ntfy", false, String(e)])
    );
  }

  if (env.DISCORD_WEBHOOK) {
    let content = `**${title}**\n${body}`;
    if (url) content += `\n${url}`;
    jobs.push(
      fetch(env.DISCORD_WEBHOOK, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: content.slice(0, 1900) }),
      }).then((r) => ["discord", r.ok, r.status]).catch((e) => ["discord", false, String(e)])
    );
  }

  if (!jobs.length) {
    console.log("!! no notification channel configured - alert not delivered");
    return [];
  }
  // All channels, not first-success: redundancy is the point.
  const res = await Promise.all(jobs);
  res.forEach(([ch, ok, info]) => console.log(`   notify ${ch}: ${ok ? "ok" : "FAILED " + info}`));
  return res;
}

/* --------------------------------------------------------------- format -- */

function describeUnit(u) {
  const bits = [`#${u.apartmentName || u.floorplanName || "room"}`];
  if (u.floorplanName && u.floorplanName !== u.apartmentName) bits.push(u.floorplanName);
  if (u.sqm || u.sqM) bits.push(`${u.sqm || u.sqM}m2`);
  if (u.minimumRent) {
    let p = `EUR ${u.minimumRent}`;
    if (u.maximumRent && u.maximumRent !== u.minimumRent) p += `-${u.maximumRent}`;
    bits.push(p + "/mo");
  }
  bits.push(`from ${u.availableDate || "now"}`);
  if (u.unitStatus) bits.push(`[${u.unitStatus}]`);
  return bits.join(" | ");
}

const unitKey = (u) => String(u.apartmentId || u.apartmentName || JSON.stringify(u));

/* ----------------------------------------------------------------- core -- */

async function runTarget(env, target, state) {
  const ts = (state.targets[target.key] ||= {});

  // Checked before anything else, including the page fetch: while rate limited
  // the correct response is to make no requests to this host at all.
  if (ts.rateLimitedUntil && Date.now() < Date.parse(ts.rateLimitedUntil)) {
    const left = Math.round((Date.parse(ts.rateLimitedUntil) - Date.now()) / 1000);
    console.log(`${target.key}: cooling down after rate limit, ${left}s left - skipping`);
    return;
  }

  const lastScrape = ts.lastPageScrape ? Date.parse(ts.lastPageScrape) : 0;
  if (Date.now() - lastScrape > PAGE_REFRESH_MINUTES * 60000) {
    const found = await scrapePageConfig(target.url);
    if (found.error) {
      console.log(`${target.key}: page scrape failed - ${found.error} (using cached config)`);
    } else {
      const prevSem = ts.semesterId;
      const prevRooms = (ts.roomTypeIds || []).join(",");

      // A changed term id means a new booking window - often the earliest
      // possible signal, before any unit is listed.
      if (prevSem !== undefined && found.semesterId !== prevSem) {
        console.log(`${target.key}: !! semester ${prevSem} -> ${found.semesterId}`);
        await notify(env, `XIOR ${target.label}: new booking term opened`,
          `The academic term id changed from ${prevSem} to ${found.semesterId}.\n` +
          `This usually means a new booking window is opening. Check now.`, target.url);
      }
      if (prevRooms !== undefined && prevRooms !== "" && found.roomTypeIds.join(",") !== prevRooms) {
        await notify(env, `XIOR ${target.label}: room types changed`,
          `Room type ids went from [${prevRooms}] to [${found.roomTypeIds.join(",")}].`, target.url);
      }

      ts.pageId = found.pageId;
      ts.semesterId = found.semesterId;
      ts.roomTypeIds = found.roomTypeIds;
      ts.lastPageScrape = nowIso();
    }
  }

  const pageId = ts.pageId || target.pageId;
  // || not ??: an empty scraped term must fall back to the known-good default,
  // otherwise the query fails with "The AcademicTermID field is required."
  const semesterId = ts.semesterId || target.semesterId || "";

  const { outcome, units, detail } = await checkAvailability(pageId, semesterId);
  console.log(`${target.key}: ${outcome} (${detail}) page_id=${pageId} term=${semesterId}`);

  if (outcome === RATELIMITED) {
    const streak = (ts.ratelimitStreak || 0) + 1;
    ts.ratelimitStreak = streak;
    const cool = Math.min(
      RATELIMIT_COOLDOWN_SECONDS * 2 ** (streak - 1),
      RATELIMIT_COOLDOWN_MAX
    );
    ts.rateLimitedUntil = new Date(Date.now() + cool * 1000).toISOString().replace(/\.\d+Z$/, "Z");
    console.log(`${target.key}: rate limited (streak ${streak}) - backing off ${cool}s`);
    if (streak === RATELIMIT_ALERT_AFTER) {
      await notify(env, `XIOR watcher blocked: ${target.label}`,
        `Rate limited ${streak} times in a row; the watcher is currently blind ` +
        `and cannot see new rooms.\n\nIf this persists, reduce the cron frequency.`,
        target.url);
    }
    return;
  }
  ts.ratelimitStreak = 0;
  ts.rateLimitedUntil = null;

  if (outcome === ERROR || outcome === BROKEN) {
    ts.consecutiveErrors = (ts.consecutiveErrors || 0) + 1;
    if (ts.consecutiveErrors === ERROR_ALERT_AFTER) {
      await notify(env, `XIOR watcher problem: ${target.label}`,
        `${ts.consecutiveErrors} consecutive failed checks.\nLast error: ${outcome} - ${detail}\n\n` +
        `The watcher is NOT currently able to see availability. Check the site manually.`,
        target.url);
    }
    return;
  }
  ts.consecutiveErrors = 0;

  const seen = new Set(ts.seenUnitIds || []);
  const currentKeys = units.map(unitKey);

  if (outcome === AVAILABLE) {
    const fresh = units.filter((u) => !seen.has(unitKey(u)));
    if (fresh.length) {
      const lines = fresh.map(describeUnit);
      const applyUrl = fresh.map((u) => u.applyOnlineURL).find(Boolean);
      let body =
        `${fresh.length} room(s) just became available at ${target.label}:\n\n` +
        lines.map((l) => `- ${l}`).join("\n");
      if (applyUrl) body += `\n\nDirect booking link:\n${applyUrl}`;
      body += `\n\nBook fast. Property page:\n${target.url}`;
      console.log(`${target.key}: *** ${fresh.length} NEW UNIT(S) ***`);
      await notify(env, `ROOM AVAILABLE - XIOR ${target.label}`, body, applyUrl || target.url);
    }
  }

  ts.seenUnitIds = currentKeys.sort();
}

async function maybeHeartbeat(env, state) {
  if (!HEARTBEAT_HOURS) return;
  const last = state.lastHeartbeat ? Date.parse(state.lastHeartbeat) : 0;
  if (Date.now() - last < HEARTBEAT_HOURS * 3600000) return;
  const lines = Object.entries(state.targets).map(
    ([k, ts]) => `- ${k}: term ${ts.semesterId ?? "?"}, ${(ts.seenUnitIds || []).length} unit(s) listed`
  );
  await notify(env, "XIOR watcher heartbeat",
    "Still watching (Cloudflare). Current status:\n\n" + lines.join("\n"), null, "normal");
  state.lastHeartbeat = nowIso();
}

/**
 * @param {"rotate"|"all"} mode
 *   "rotate" checks a single target per invocation, alternating between cron
 *   ticks. Measured budget is ~3 requests per 10s per IP, so firing every
 *   property on every tick is a fast way to get rate limited. With a 2-minute
 *   cron and two targets each property is still polled every 4 minutes.
 *   "all" checks everything (manual /check only).
 */
async function runAll(env, mode = "all") {
  const raw = (await env.XIOR_STATE.get(STATE_KEY)) || '{"targets":{}}';
  let state;
  try { state = JSON.parse(raw); } catch { state = { targets: {} }; }
  state.targets ||= {};
  const before = JSON.stringify(state);

  const due = mode === "rotate"
    ? [TARGETS[Math.floor(Date.now() / 120000) % TARGETS.length]]
    : TARGETS;

  for (const t of due) {
    try {
      await runTarget(env, t, state);
    } catch (e) {
      console.log(`${t.key}: unhandled ${e}`);
      const ts = (state.targets[t.key] ||= {});
      ts.consecutiveErrors = (ts.consecutiveErrors || 0) + 1;
    }
    if (mode === "all" && due.indexOf(t) < due.length - 1) {
      await sleep(20000);
    }
  }
  await maybeHeartbeat(env, state);

  // Only write when something durable changed - keeps us far under the
  // free plan's 1000 KV writes/day.
  const after = JSON.stringify(state);
  if (after !== before) {
    await env.XIOR_STATE.put(STATE_KEY, after);
    console.log("state written");
  }
  return state;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runAll(env, "rotate"));
  },

  // Manual endpoints: /check runs a pass, /test sends a test alert,
  // /state dumps current state. Guarded by WATCH_SECRET if it is set.
  async fetch(request, env) {
    const url = new URL(request.url);
    if (env.WATCH_SECRET && url.searchParams.get("key") !== env.WATCH_SECRET) {
      return new Response("unauthorized\n", { status: 401 });
    }
    if (url.pathname === "/test") {
      const res = await notify(env, "XIOR watcher test",
        "Notifications are wired up correctly.\nYou will get a message like this the moment a room appears.",
        TARGETS[0].url, "normal");
      return Response.json({ ok: res.some((r) => r[1]), channels: res });
    }
    if (url.pathname === "/state") {
      return Response.json(JSON.parse((await env.XIOR_STATE.get(STATE_KEY)) || "{}"));
    }
    if (url.pathname === "/check") {
      return Response.json(await runAll(env));
    }
    return new Response(
      "XIOR watcher. Endpoints: /check  /state  /test\n", { status: 200 }
    );
  },
};
