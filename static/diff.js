"use strict";

// SRT 版本差异审校：输入旧版/新版 SRT，按块序全局最小代价对齐
// （允许 1:1、1:2 拆分、2:1 合并与单侧缺项），并下载 UTF-8 CSV
(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    oldSrt: $("dfOld"),
    newSrt: $("dfNew"),
    runBtn: $("dfRunBtn"),
    clearBtn: $("dfClearBtn"),
    status: $("dfStatus"),
    summary: $("dfSummary"),
    resultWrap: $("dfResultWrap"),
    body: $("dfBody"),
    downloadBtn: $("dfDownloadBtn"),
  };

  const SAMPLE_OLD = [
    "1",
    "00:00:01,000 --> 00:00:03,000",
    "你好，世界！",
    "",
    "2",
    "00:00:04,000 --> 00:00:07,000",
    "这一句在新版被拆成两块。",
    "",
    "3",
    "00:00:08,000 --> 00:00:10,000",
    "完全没有变化的一行。",
    "",
    "4",
    "00:00:11,000 --> 00:00:13,000",
    "这一行只调整了时间码。",
    "",
    "5",
    "00:00:14,000 --> 00:00:16,000",
    "新版里这一句被删除了。",
    "",
    "6",
    "00:00:17,000 --> 00:00:19,000",
    "新版相邻两块",
    "",
    "7",
    "00:00:19,500 --> 00:00:21,000",
    "在旧版合并成一块。",
    "",
  ].join("\n");

  const SAMPLE_NEW = [
    "1",
    "00:00:01,000 --> 00:00:03,000",
    "你好，世界！正文略有修改。",
    "",
    "2",
    "00:00:04,000 --> 00:00:05,500",
    "这一句在新版",
    "",
    "3",
    "00:00:05,800 --> 00:00:07,000",
    "被拆成两块。",
    "",
    "4",
    "00:00:08,000 --> 00:00:10,000",
    "完全没有变化的一行。",
    "",
    "5",
    "00:00:11,500 --> 00:00:13,500",
    "这一行只调整了时间码。",
    "",
    "6",
    "00:00:17,000 --> 00:00:21,000",
    "新版相邻两块在旧版合并成一块。",
    "",
    "7",
    "00:00:22,000 --> 00:00:24,000",
    "这是旧版没有的全新字幕。",
    "",
  ].join("\n");

  $("dfSampleOld").addEventListener("click", () => {
    els.oldSrt.value = SAMPLE_OLD;
  });
  $("dfSampleNew").addEventListener("click", () => {
    els.newSrt.value = SAMPLE_NEW;
  });

  els.clearBtn.addEventListener("click", () => {
    els.oldSrt.value = "";
    els.newSrt.value = "";
    els.summary.classList.add("hidden");
    els.resultWrap.classList.add("hidden");
    setStatus("", false);
  });

  function setStatus(msg, isError) {
    els.status.textContent = msg;
    els.status.classList.toggle("error", !!isError);
  }

  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  const STATUS_META = {
    unchanged: { label: "未变", badge: "badge-df-unchanged" },
    time_only: { label: "仅改时", badge: "badge-df-time" },
    text_changed: { label: "文字改动", badge: "badge-df-text" },
    split: { label: "拆分", badge: "badge-df-split" },
    merge: { label: "合并", badge: "badge-df-merge" },
    added: { label: "新增", badge: "badge-df-added" },
    deleted: { label: "删除", badge: "badge-df-deleted" },
  };

  function costClass(status, cost) {
    if (status === "unchanged") return "cost-low";
    if (cost >= 0.4) return "cost-high";
    if (cost >= 0.2) return "cost-mid";
    return "cost-low";
  }

  function blocksCell(blocks) {
    if (!blocks.length) return { idx: "", tc: "", text: '<span class="dim">—</span>' };
    return {
      idx: blocks.map((b) => b.index).join("<br>"),
      tc: blocks
        .map((b) => `${escapeHtml(b.start_tc)} → ${escapeHtml(b.end_tc)}`)
        .join('<div class="df-sep"></div>'),
      text: blocks
        .map((b) => escapeHtml(b.text))
        .join('<div class="df-sep"></div>'),
    };
  }

  function render(data) {
    const s = data.summary;
    $("dfCntUnchanged").textContent = s.unchanged;
    $("dfCntTime").textContent = s.time_only;
    $("dfCntText").textContent = s.text_changed;
    $("dfCntSplit").textContent = s.split;
    $("dfCntMerge").textContent = s.merge;
    $("dfCntAdded").textContent = s.added;
    $("dfCntDeleted").textContent = s.deleted;
    $("dfCntTotal").textContent = s.total_rows;
    $("dfTotalCost").textContent = s.total_cost.toFixed(4);

    const frag = document.createDocumentFragment();
    for (const r of data.rows) {
      const meta = STATUS_META[r.status];
      const old = blocksCell(r.old);
      const now = blocksCell(r.new);
      const tr = document.createElement("tr");
      tr.className = `row-df-${r.status}`;

      let costInner = `<span class="${costClass(r.status, r.cost)}">${r.cost.toFixed(3)}</span>`;
      if (r.time_delta_ms) {
        costInner += `<div class="df-dt" title="入点差 + 出点差（毫秒）">Δt ${r.time_delta_ms >= 1000
          ? (r.time_delta_ms / 1000).toFixed(2).replace(/\.?0+$/, "") + "s"
          : r.time_delta_ms + "ms"}</div>`;
      }

      tr.innerHTML = `
        <td><span class="badge ${meta.badge}">${meta.label}</span></td>
        <td class="cell-num">${costInner}</td>
        <td class="cell-idx">${old.idx}</td>
        <td class="cell-tc">${old.tc}</td>
        <td class="cell-text">${old.text}</td>
        <td class="cell-idx">${now.idx}</td>
        <td class="cell-tc">${now.tc}</td>
        <td class="cell-text">${now.text}</td>`;
      frag.appendChild(tr);
    }
    els.body.replaceChildren(frag);
    els.summary.classList.remove("hidden");
    els.resultWrap.classList.remove("hidden");
  }

  let lastRequest = null;

  async function run() {
    lastRequest = { old_srt: els.oldSrt.value, new_srt: els.newSrt.value };
    els.runBtn.disabled = true;
    setStatus("审校对齐中……", false);
    try {
      const resp = await fetch("/api/diff", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: `请求失败（${resp.status}）` }));
        throw new Error(err.detail || `请求失败（${resp.status}）`);
      }
      const data = await resp.json();
      render(data);
      const s = data.summary;
      setStatus(
        `完成：${s.total_rows} 行（未变 ${s.unchanged} · 仅改时 ${s.time_only} · ` +
        `文字改动 ${s.text_changed} · 拆分 ${s.split} · 合并 ${s.merge} · ` +
        `新增 ${s.added} · 删除 ${s.deleted}）`,
        false
      );
    } catch (e) {
      lastRequest = null;
      els.summary.classList.add("hidden");
      els.resultWrap.classList.add("hidden");
      setStatus(e.message, true);
    } finally {
      els.runBtn.disabled = false;
    }
  }

  async function downloadCsv() {
    if (!lastRequest) {
      setStatus("请先执行审校", true);
      return;
    }
    els.downloadBtn.disabled = true;
    try {
      const resp = await fetch("/api/diff.csv", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(lastRequest),
      });
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: "下载失败" }));
        throw new Error(err.detail || "下载失败");
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "srt-diff.csv";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setStatus(e.message, true);
    } finally {
      els.downloadBtn.disabled = false;
    }
  }

  els.runBtn.addEventListener("click", run);
  els.downloadBtn.addEventListener("click", downloadCsv);
})();
