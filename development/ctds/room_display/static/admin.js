/* CTDS C01 Admin configuration client.
   Fetches /api/v1/sources/capabilities and /api/v1/admin/display-fields;
   validates input locally; PATCHes to loopback API. */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let capabilities = {};

  function showNotice(text, isError = false) {
    const el = $("notice");
    el.textContent = text;
    el.className = "banner " + (isError ? "banner-error" : "banner-info");
    el.classList.remove("hidden");
  }

  async function loadData() {
    try {
      const [capRes, fieldsRes] = await Promise.all([
        fetch("/api/v1/sources/capabilities", { cache: "no-store" }),
        fetch("/api/v1/admin/display-fields", { cache: "no-store" }),
      ]);
      if (!capRes.ok || !fieldsRes.ok) {
        showNotice("载入配置失败", true);
        return;
      }
      const capData = await capRes.json();
      const fieldsData = await fieldsRes.json();

      capabilities = {};
      (capData.airgradient || []).forEach((c) => {
        capabilities[c.metric_id] = c;
      });

      renderTable(fieldsData.fields || []);
    } catch (err) {
      showNotice("网络错误：" + err.message, true);
    }
  }

  function renderTable(fields) {
    const tbody = $("fields-tbody");
    tbody.innerHTML = "";
    fields.forEach((f) => {
      const cap = capabilities[f.metric_id] || {};
      const tr = document.createElement("tr");
      tr.dataset.metricId = f.metric_id;
      tr.innerHTML = `
        <td><input type="checkbox" class="inp-enable" ${f.enabled ? "checked" : ""}></td>
        <td><input type="number" class="inp-order" value="${f.display_order}" min="1" max="99"></td>
        <td><input type="text" class="inp-label" value="${f.label_zh}" maxlength="20"></td>
        <td><code>${f.metric_id}</code></td>
        <td>${cap.canonical_unit || "--"}</td>
        <td><span class="badge">${cap.availability === "VERIFIED" ? "实测支持" : "文档规范"}</span></td>
      `;
      tbody.appendChild(tr);
    });
  }

  async function saveConfig(ev) {
    ev.preventDefault();
    const rows = $("fields-tbody").querySelectorAll("tr");
    const items = [];
    for (const tr of rows) {
      const metric_id = tr.dataset.metricId;
      const enabled = tr.querySelector(".inp-enable").checked;
      const display_order = parseInt(tr.querySelector(".inp-order").value, 10);
      const label_zh = tr.querySelector(".inp-label").value.trim();

      if (isNaN(display_order) || display_order < 1 || display_order > 99) {
        showNotice(`指标 ${metric_id} 顺序必须为 1–99`, true);
        return;
      }
      if (!label_zh || label_zh.length > 20) {
        showNotice(`指标 ${metric_id} 中文名称必须为 1–20 字符`, true);
        return;
      }
      items.push({ metric_id, enabled, display_order, label_zh });
    }

    try {
      const res = await fetch("/api/v1/admin/display-fields", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields: items }),
      });
      if (res.ok) {
        const out = await res.json();
        $("save-status").textContent = `保存成功（版本 rev ${out.config_revision}）`;
        setTimeout(() => { $("save-status").textContent = ""; }, 3000);
        renderTable(out.fields);
        $("notice").classList.add("hidden");
      } else {
        const err = await res.json();
        showNotice("保存失败: " + (err.detail || err.error), true);
      }
    } catch (err) {
      showNotice("网络错误: " + err.message, true);
    }
  }

  $("fields-form").addEventListener("submit", saveConfig);
  $("btn-reset").addEventListener("click", loadData);
  loadData();
})();
