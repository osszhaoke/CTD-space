/**
 * StarMap C01 · Browser Headless Automated Verification Script
 * Validates real browser interaction:
 * 1. Card click opens high-precision detail modal (modal .open class & visibility);
 * 2. AI button click opens DeepSeek diagnosis export modal;
 * 3. Zero Content Security Policy (CSP) violations.
 */
const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

async function runBrowserCheck() {
  const chromePath = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  if (!fs.existsSync(chromePath)) {
    console.log("Chrome binary not found at", chromePath);
    process.exit(0);
  }

  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "chrome-check-"));
  const port = 9224;
  const chrome = spawn(chromePath, [
    "--headless=new",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${tmpDir}`,
    "--window-size=1920,1080",
    "--no-first-run",
    "--no-default-browser-check"
  ]);

  await new Promise(r => setTimeout(r, 1500));

  try {
    const listRes = await fetch(`http://127.0.0.1:${port}/json/list`);
    const targets = await listRes.json();
    const pageTarget = targets.find(t => t.type === "page") || targets[0];
    const ws = new WebSocket(pageTarget.webSocketDebuggerUrl);

    let id = 1;
    const send = (method, params = {}) => new Promise((resolve) => {
      const msgId = id++;
      const handler = (evt) => {
        const msg = JSON.parse(evt.data);
        if (msg.id === msgId) {
          ws.removeEventListener("message", handler);
          resolve(msg.result);
        }
      };
      ws.addEventListener("message", handler);
      ws.send(JSON.stringify({ id: msgId, method, params }));
    });

    await new Promise(r => ws.addEventListener("open", r));

    const consoleLogs = [];
    ws.addEventListener("message", (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.method === "Log.entryAdded") consoleLogs.push(msg.params.entry);
      if (msg.method === "Runtime.consoleAPICalled") consoleLogs.push(msg.params);
    });

    await send("Log.enable");
    await send("Runtime.enable");
    await send("Page.enable");

    await send("Page.navigate", { url: "http://127.0.0.1:8765/" });
    await new Promise(r => setTimeout(r, 2000));

    // 1. Click card-co2 to open chart modal
    await send("Runtime.evaluate", {
      expression: `(() => {
        const card = document.getElementById("card-co2");
        if (card) card.click();
      })()`
    });
    // Wait for transition (0.28s cubic-bezier)
    await new Promise(r => setTimeout(r, 400));

    const evalCard = await send("Runtime.evaluate", {
      expression: `(() => {
        const backdrop = document.getElementById("chart-modal-backdrop");
        const style = window.getComputedStyle(backdrop);
        return {
          isOpen: backdrop.classList.contains("open"),
          opacity: style.opacity,
          visibility: style.visibility
        };
      })()`,
      returnByValue: true
    });
    const cardRes = evalCard.result.value || {};
    if (!cardRes.isOpen || cardRes.opacity !== "1" || cardRes.visibility !== "visible") {
      throw new Error("Chart modal failed to open with visible class: " + JSON.stringify(cardRes));
    }
    console.log("PASS: Card click opens high-precision chart modal (opacity: 1, visibility: visible)");

    // Close chart modal via close button
    const evalClose = await send("Runtime.evaluate", {
      expression: `(() => {
        const btn = document.getElementById("modal-close-btn");
        if (!btn) return { found: false };
        btn.click();
        const backdrop = document.getElementById("chart-modal-backdrop");
        return {
          found: true,
          isOpen: backdrop.classList.contains("open"),
          isHidden: backdrop.classList.contains("crosshair-hidden")
        };
      })()`,
      returnByValue: true
    });
    const closeData = evalClose.result.value || {};
    if (!closeData.found || closeData.isOpen || !closeData.isHidden) {
      throw new Error("Chart modal failed to close via modal-close-btn: " + JSON.stringify(closeData));
    }
    console.log("PASS: Chart modal closed successfully via modal-close-btn");
    await new Promise(r => setTimeout(r, 400));

    // 2. Click AI export button
    const evalAi = await send("Runtime.evaluate", {
      expression: `(() => {
        const btn = document.getElementById("ai-export-btn");
        if (btn) btn.click();
        const backdrop = document.getElementById("ai-modal-backdrop");
        const style = window.getComputedStyle(backdrop);
        return {
          isOpen: backdrop.classList.contains("open"),
          isHidden: backdrop.classList.contains("crosshair-hidden"),
          opacity: style.opacity,
          visibility: style.visibility,
          preLength: (document.getElementById("ai-modal-pre").textContent || "").length
        };
      })()`,
      returnByValue: true
    });
    const aiRes = evalAi.result.value || {};
    if (!aiRes.isOpen || aiRes.isHidden || aiRes.opacity !== "1" || aiRes.visibility !== "visible" || aiRes.preLength < 50) {
      throw new Error("AI modal failed to open or populate summary: " + JSON.stringify(aiRes));
    }
    console.log("PASS: AI export button opens diagnosis modal (opacity: 1, preLength: " + aiRes.preLength + ")");

    // 3. Zero CSP violations
    const cspErrors = consoleLogs.filter(l => (l.text || "").includes("Content Security Policy") || (l.text || "").includes("Refused to"));
    if (cspErrors.length > 0) {
      throw new Error("CSP violations detected: " + JSON.stringify(cspErrors));
    }
    console.log("PASS: Zero CSP violations detected in browser console");

    ws.close();
    console.log("ALL BROWSER INTERACTION AUTOMATED CHECKS PASS");
    process.exit(0);
  } finally {
    chrome.kill();
    try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (_) {}
  }
}

runBrowserCheck().catch(err => {
  console.error("BROWSER_CHECK_FAIL:", err);
  process.exit(1);
});
